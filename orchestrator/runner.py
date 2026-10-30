"""Runner: one orchestrator step = one fresh Agent SDK session (O2).

API facts (streaming input for `can_use_tool`, hook shapes, result/rate-limit types) are in
docs/notes/agent-sdk.md. The SDK is injected (`query_fn`) so everything below is unit-testable
with the real SDK message dataclasses and no network.

Outcome rules, in order (see `classify`):
  hard stop from policy        -> BLOCKED
  auth / billing error         -> FAILED  (retrying cannot help)
  quota, layer 1 (structured)  -> QUOTA_WAIT   RateLimitEvent rejected / AssistantMessage.error
  quota, layer 2 (error text)  -> QUOTA_WAIT   only on error results/exceptions, never on success
  timeout                      -> FAILED   (layer 3: a timeout is NOT a quota signal)
  blind retry / denied + error -> PERMISSION_DENIED
  result subtype/is_error      -> DONE or FAILED
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from orchestrator.config import Budget
from orchestrator.envfile import redact, secret_values
from orchestrator.policy import Allow, Decision, Deny, PolicyContext, decide
from orchestrator.state import StateDB


class StepStatus(StrEnum):  # values double as the strings stored in state.db
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    QUOTA_WAIT = "quota_wait"
    PERMISSION_DENIED = "permission_denied"


@dataclass
class StepResult:  # contract §4
    status: StepStatus
    session_id: str | None
    cost_usd: float
    reason: str | None  # human readable for BLOCKED / FAILED / ...
    output: str | None = None  # the agent's final message (plan / contract / summary), redacted


# ---- policy adapter --------------------------------------------------------------------------


class PolicyGuard:
    """Stateful wrapper around `policy.decide` shared by the hook and `can_use_tool`.

    Decisions are cached per `tool_use_id`, so a call seen by both the PreToolUse hook and the
    `can_use_tool` fallback is evaluated (and counted) once.
    """

    def __init__(
        self,
        ctx: PolicyContext,
        *,
        repeat_limit: int = 3,
        on_deny: Callable[[str, Mapping[str, Any], Deny], None] | None = None,
    ):
        self.ctx = ctx
        self.repeat_limit = repeat_limit
        self.on_deny = on_deny
        self.hard_stop: Deny | None = None
        self.blind_retry: str | None = None
        self.denied_ids: set[str] = set()
        self.deny_count = 0
        self._repeats: Counter[tuple[str, str]] = Counter()
        self._by_id: dict[str, Decision] = {}

    @property
    def stop_requested(self) -> bool:
        return self.hard_stop is not None or self.blind_retry is not None

    def check(
        self, tool_name: str, tool_input: Mapping[str, Any] | None, tool_use_id: str | None = None
    ) -> Decision:
        if tool_use_id and tool_use_id in self._by_id:
            return self._by_id[tool_use_id]
        if self.hard_stop is not None:  # the step is already ending; nothing else may run
            return Deny(self.hard_stop.reason, hard_stop=True)
        decision = decide(tool_name, tool_input, self.ctx)
        if isinstance(decision, Deny):
            self.deny_count += 1
            if tool_use_id:
                self.denied_ids.add(tool_use_id)
            if decision.hard_stop:
                self.hard_stop = decision
            else:
                key = (tool_name, json.dumps(tool_input or {}, sort_keys=True, default=str))
                self._repeats[key] += 1
                if self._repeats[key] >= self.repeat_limit and self.blind_retry is None:
                    self.blind_retry = (
                        f"{tool_name} was denied {self._repeats[key]} times with the same input: "
                        f"{decision.reason}"
                    )
            if self.on_deny:
                self.on_deny(tool_name, tool_input or {}, decision)
        if tool_use_id:
            self._by_id[tool_use_id] = decision
        return decision


def make_pre_tool_use_hook(guard: PolicyGuard) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Runs on EVERY tool call, in every permission mode; it only ever denies."""

    async def hook(input_data: Mapping[str, Any], tool_use_id: str | None, context: Any):
        decision = guard.check(
            str(input_data.get("tool_name", "")), input_data.get("tool_input") or {}, tool_use_id
        )
        if isinstance(decision, Allow):
            return {}
        out: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
            }
        }
        if guard.stop_requested:
            out["continue_"] = False
            out["stopReason"] = guard.hard_stop.reason if guard.hard_stop else guard.blind_retry
        return out

    return hook


def make_can_use_tool(guard: PolicyGuard) -> Callable[..., Awaitable[Any]]:
    """Answers immediately for calls that fall through to a prompt; nobody is there to ask."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], context: Any):
        decision = guard.check(tool_name, input_data, getattr(context, "tool_use_id", None))
        if isinstance(decision, Allow):
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message=decision.reason, interrupt=guard.stop_requested)

    return can_use_tool


# ---- quota detection (layer 2 text) ----------------------------------------------------------

# "hit your session limit · resets 8pm (...)" is the wording of a real stop captured on 2026-10-11
# (docs/notes/agent-sdk.md §11); the other alternatives are older guesses.
_QUOTA_TEXT = re.compile(
    r"rate[ _-]?limit|usage limit|limit (?:reached|exceeded)|too many requests|\b429\b|"
    r"quota (?:exceeded|reached)|out of (?:credits|usage)|"
    r"hit your (?:\w+ )?limit|usage_limit_reached",
    re.I,
)


def looks_like_quota(texts: Sequence[str | None], api_error_status: int | None = None) -> bool:
    if api_error_status == 429:
        return True
    return any(t and _QUOTA_TEXT.search(t) for t in texts)


# ---- request / stream state ------------------------------------------------------------------


@dataclass
class StepRequest:
    prompt: str
    cwd: Path
    policy: PolicyContext
    timeout_s: float = 1800
    permission_mode: str = "auto"  # readme §6; policy still enforces via hook + can_use_tool
    max_turns: int | None = None
    model: str | None = None
    system_prompt: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    # None = the SDK default (user + project + local). [] isolates the session from user settings.
    setting_sources: list[str] | None = None
    strict_mcp_config: bool = False
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    # Goes to the SDK only. repr=False: a logged/pytest-printed request must not show the API key.
    env: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass
class _Stream:
    session_id: str | None = None
    result: Any = None  # ResultMessage
    rate_rejected: Any = None  # RateLimitInfo
    assistant_errors: list[str] = field(default_factory=list)
    assistant_text_errors: list[str] = field(default_factory=list)
    last_text: str | None = None  # last assistant text, the fallback when result.result is empty
    timed_out: bool = False
    effective_mode: str | None = None
    exception: BaseException | None = None
    stderr_tail: list[str] = field(default_factory=list)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def classify(
    stream: _Stream, guard: PolicyGuard, timeout_s: float
) -> tuple[StepStatus, str | None]:
    """Pure mapping from what happened to a StepStatus (the ordered rules in the module docs)."""
    res = stream.result
    res_ok = res is not None and res.subtype == "success" and not res.is_error

    if guard.hard_stop is not None:
        return StepStatus.BLOCKED, f"policy hard stop: {guard.hard_stop.reason}"

    fatal = [e for e in stream.assistant_errors if e in ("authentication_failed", "billing_error")]
    if fatal and not res_ok:
        return StepStatus.FAILED, f"{fatal[0]}: retrying cannot help"

    if not res_ok:
        if stream.rate_rejected is not None or "rate_limit" in stream.assistant_errors:  # layer 1
            info = stream.rate_rejected
            window = (
                f" ({info.rate_limit_type})" if info is not None and info.rate_limit_type else ""
            )
            return StepStatus.QUOTA_WAIT, f"rate limit rejected{window}"
        texts: list[str | None] = [
            *(_as_list(res.errors) if res is not None else []),
            res.result if res is not None else None,
            *stream.assistant_text_errors,
            str(stream.exception) if stream.exception is not None else None,
            getattr(stream.exception, "stderr", None),
            "\n".join(stream.stderr_tail) if stream.exception is not None else None,
        ]
        status = getattr(res, "api_error_status", None) or getattr(
            stream.exception, "api_error_status", None
        )
        if looks_like_quota(texts, status):  # layer 2
            return StepStatus.QUOTA_WAIT, "quota/rate-limit text in the error output"

    if stream.timed_out:  # layer 3: never a quota
        return StepStatus.FAILED, f"timeout after {timeout_s}s"

    if guard.blind_retry:
        return StepStatus.PERMISSION_DENIED, f"stopped, not retrying blindly: {guard.blind_retry}"

    exc = stream.exception
    if res is None and exc is not None:
        name = type(exc).__name__
        detail = getattr(exc, "stderr", None) or "\n".join(stream.stderr_tail[-5:]) or str(exc)
        sub = getattr(exc, "subtype", None)
        term = getattr(exc, "terminal_reason", None)
        extra = "".join(f" {k}={v}" for k, v in (("subtype", sub), ("terminal_reason", term)) if v)
        return StepStatus.FAILED, f"{name}{extra}: {str(detail).strip()[:300]}"
    if res is None:
        return StepStatus.FAILED, "the session ended without a result message"

    if res_ok:
        return StepStatus.DONE, None

    denials = _as_list(getattr(res, "permission_denials", None))
    foreign = [
        d for d in denials if not (isinstance(d, dict) and d.get("tool_use_id") in guard.denied_ids)
    ]
    if foreign:
        names = ", ".join(
            str(d.get("tool_name", "?")) if isinstance(d, dict) else str(d) for d in foreign[:5]
        )
        return (
            StepStatus.PERMISSION_DENIED,
            f"permission denied by Claude Code (not retried): {names}",
        )

    subtype = res.subtype
    if subtype == "error_max_budget_usd":
        return StepStatus.FAILED, "per-call budget reached (error_max_budget_usd)"
    if subtype == "error_max_turns":
        return StepStatus.FAILED, "max turns reached (error_max_turns)"
    errs = "; ".join(str(e) for e in _as_list(res.errors))[:300]
    stop = f" stop_reason={res.stop_reason}" if res.stop_reason else ""
    term = f" terminal_reason={res.terminal_reason}" if res.terminal_reason else ""
    return StepStatus.FAILED, f"{subtype}{stop}{term} {errs}".strip()


# ---- one step --------------------------------------------------------------------------------

QueryFn = Callable[..., AsyncIterator[Any]]


def _remaining_call_budget(db: StateDB, step_id: int, budget: Budget) -> tuple[float, str | None]:
    step = db.get_step(step_id)
    run = db.get_run(step.run_id)
    left_session = budget.per_session - db.session_cost(step.run_id, step.session_id)
    left_total = budget.total - (run.total_cost_usd if run else 0.0)
    cap = min(budget.per_call, left_session, left_total)
    if cap <= 0:
        which = "session" if left_session <= left_total else "total"
        return (
            0.0,
            f"{which} budget exhausted (${left_session:.2f} session, ${left_total:.2f} total left)",
        )
    return cap, None


async def run_step(
    request: StepRequest,
    *,
    db: StateDB,
    step_id: int,
    budget: Budget,
    max_calls_per_hour: int = 60,
    query_fn: QueryFn | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StepResult:
    """Run one fresh session, persisting session id, cost and outcome to state.db as it goes."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        HookMatcher,
        RateLimitEvent,
        ResultMessage,
        SystemMessage,
    )

    if request.permission_mode == "bypassPermissions":
        raise ValueError("bypassPermissions is never allowed (readme §6)")
    step = db.get_step(step_id)
    run_id, sid = step.run_id, step.session_id

    cap, why = _remaining_call_budget(db, step_id, budget)
    if why:
        db.update_step(step_id, status=StepStatus.BLOCKED, last_error=why)
        db.add_event("blocker", why, run_id=run_id, session_id=sid)
        return StepResult(StepStatus.BLOCKED, None, 0.0, why)

    if db.count_events_since("sdk_call", now() - timedelta(hours=1)) >= max_calls_per_hour:
        why = f"local call limit reached ({max_calls_per_hour} SDK calls in the last hour)"
        db.update_step(step_id, status=StepStatus.QUOTA_WAIT, last_error=why)
        db.add_event("quota_wait", why, run_id=run_id, session_id=sid)
        return StepResult(StepStatus.QUOTA_WAIT, None, 0.0, why)

    stream = _Stream()
    secrets = secret_values(request.env)

    def clean(text: str | None) -> str | None:  # nothing secret reaches state.db or an event
        return redact(text, secrets)

    def record_deny(tool: str, tool_input: Mapping[str, Any], d: Deny) -> None:
        kind = "blocker" if d.hard_stop else "policy_deny"
        summary = json.dumps(tool_input, default=str)[:200]
        db.add_event(kind, clean(f"{tool} {summary} -> {d.reason}"), run_id=run_id, session_id=sid)

    def keep_stderr(line: str) -> None:
        if len(stream.stderr_tail) < 200:
            stream.stderr_tail.append(clean(line.rstrip()) or "")

    guard = PolicyGuard(request.policy, on_deny=record_deny)
    options = ClaudeAgentOptions(
        cwd=str(request.cwd),
        permission_mode=request.permission_mode,  # type: ignore[arg-type]
        can_use_tool=make_can_use_tool(guard),
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_hook(guard)])]},
        max_budget_usd=cap,
        max_turns=request.max_turns,
        model=request.model,
        system_prompt=request.system_prompt,
        allowed_tools=request.allowed_tools,
        disallowed_tools=request.disallowed_tools,
        setting_sources=request.setting_sources,  # type: ignore[arg-type]
        strict_mcp_config=request.strict_mcp_config,
        mcp_servers=request.mcp_servers,
        env=request.env,
        stderr=keep_stderr,
    )

    async def prompt_stream():  # streaming input is what makes can_use_tool work (notes §2)
        yield {"type": "user", "message": {"role": "user", "content": request.prompt}}

    if query_fn is None:
        import claude_agent_sdk

        query_fn = claude_agent_sdk.query

    db.update_step(step_id, status="running", increment_attempts=True)
    db.add_event("sdk_call", f"start cap=${cap:.2f}", run_id=run_id, session_id=sid)

    def observe_init(data: Mapping[str, Any]) -> None:
        """Record what the session really started with (docs/notes/agent-sdk.md §11)."""
        mode = data.get("permissionMode")
        servers = {
            str(m.get("name")): str(m.get("status"))
            for m in data.get("mcp_servers") or []
            if isinstance(m, Mapping)
        }
        stream.effective_mode = mode
        detail = {
            "permission_mode": mode,
            "requested": request.permission_mode,
            "api_key_source": data.get("apiKeySource"),  # a NAME ("ANTHROPIC_API_KEY"), no value
            "mcp_servers": servers,
            "hooks_tools": len(data.get("tools") or []),
        }
        db.add_event("session_init", json.dumps(detail), run_id=run_id, session_id=sid)
        if mode != request.permission_mode:
            db.add_event(
                "notice",
                f"permission mode fell back: requested {request.permission_mode!r}, "
                f"effective {mode!r}",
                run_id=run_id,
                session_id=sid,
            )

    async def consume() -> None:
        async for msg in query_fn(prompt=prompt_stream(), options=options):  # type: ignore[misc]
            session_id = getattr(msg, "session_id", None)
            if session_id and stream.session_id is None:
                stream.session_id = session_id
                db.update_step(step_id, claude_session_id=session_id)  # survives a crash
            if isinstance(msg, SystemMessage):
                if msg.subtype == "init":
                    observe_init(msg.data)
            elif isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                if info.status == "rejected":
                    stream.rate_rejected = info
                    db.add_event(
                        "quota_wait",
                        f"rate limit rejected type={info.rate_limit_type} "
                        f"resets_at={info.resets_at}",
                        run_id=run_id,
                        session_id=sid,
                    )
                elif info.status == "allowed_warning":
                    db.add_event(
                        "rate_limit_warning",
                        f"type={info.rate_limit_type} utilization={info.utilization}",
                        run_id=run_id,
                        session_id=sid,
                    )
            elif isinstance(msg, AssistantMessage):
                texts = [t for b in msg.content if (t := getattr(b, "text", None))]
                if texts and not msg.error:
                    stream.last_text = "\n".join(texts)
                if msg.error:
                    stream.assistant_errors.append(str(msg.error))
                    for block in msg.content:
                        text = getattr(block, "text", None)
                        if text:
                            stream.assistant_text_errors.append(text)
            elif isinstance(msg, ResultMessage):
                stream.result = msg

    try:
        async with asyncio.timeout(request.timeout_s):
            await consume()
    except TimeoutError:
        stream.timed_out = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # ResultError, ProcessError, CLINotFoundError, ... (all arrive here)
        stream.exception = exc
        # ResultError carries the result message's fields; surface them like a result
        if stream.session_id is None and getattr(exc, "session_id", None):
            stream.session_id = exc.session_id  # type: ignore[attr-defined]
            db.update_step(step_id, claude_session_id=stream.session_id)

    status, reason = classify(stream, guard, request.timeout_s)
    reason = clean(reason)
    cost = float(getattr(stream.result, "total_cost_usd", None) or 0.0)
    db.update_step(
        step_id,
        status=status,
        add_cost_usd=cost,
        last_error=reason if status is not StepStatus.DONE else None,
    )
    detail = {
        "status": status.value,
        "cost_usd": cost,
        "turns": getattr(stream.result, "num_turns", None),
        "policy_denials": guard.deny_count,
        "reason": reason,
    }
    db.add_event("sdk_call", json.dumps(detail), run_id=run_id, session_id=sid)
    if status is StepStatus.BLOCKED:
        db.add_event("blocker", reason or "", run_id=run_id, session_id=sid)
    output = None
    if status is StepStatus.DONE:
        output = clean(getattr(stream.result, "result", None) or stream.last_text)
    return StepResult(status, stream.session_id, cost, reason, output)


async def run_step_with_quota_retry(
    request: StepRequest,
    *,
    db: StateDB,
    step_id: int,
    budget: Budget,
    quota_wait_s: int = 3600,
    max_waits: int = 24,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **kwargs: Any,
) -> StepResult:
    """On QUOTA_WAIT wait `quota_wait_s` (readme §5: 60 min) and run a fresh session again."""
    step = db.get_step(step_id)
    waits = 0
    while True:
        result = await run_step(request, db=db, step_id=step_id, budget=budget, **kwargs)
        if result.status is not StepStatus.QUOTA_WAIT or waits >= max_waits:
            return result
        waits += 1
        db.add_event(
            "quota_wait",
            f"waiting {quota_wait_s}s before retry {waits}/{max_waits}: {result.reason}",
            run_id=step.run_id,
            session_id=step.session_id,
        )
        await sleep(quota_wait_s)
