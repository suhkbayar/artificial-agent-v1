"""Session flow ①-⑤ (readme §4) on top of pipeline + runner + gates + codex + memory.

    preflight -> ① plan (read-only) -> policy check of the plan (risky => BLOCKED)
              -> ② contract (only when the session has a Codex task)
              -> ③ Claude track -> snapshot A -> ④ Codex -> snapshot B (A->B = Codex's files)
              -> ⑤ verify (a DIFFERENT agent) -> gates -> post_steps (O4, develop2) -> next session

Every Claude phase is a fresh SDK session (O2). Progress lives in state.db (`steps`, `attempts`,
`events`); the heavy per-phase artefacts (plan, contract, snapshots) live in
`<checkpoint_dir>/<pipeline>/run-<id>/<session>/` so `resume` can pick up after a crash: phases
whose step is `done` are skipped, the rest run again.

Nothing here commits, pushes or checks out (O3) and production is out of reach (O8): post_steps
are the develop2-only names in `pipeline.POST_STEPS`, re-checked by the stage preflight right
before each one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from orchestrator import codex as codex_mod
from orchestrator.config import Config
from orchestrator.envfile import redact, secret_values
from orchestrator.gates import (
    Attempt,
    GateVerdict,
    Snapshot,
    allowlist_violations,
    breaker_tripped,
    changed_files,
    evaluate,
    load_snapshot,
    take_snapshot,
)
from orchestrator.memory import MemoryBusy, MemoryStore, all_tool_names, write_tool_names
from orchestrator.pipeline import POST_STEPS, REQUIRED_STAGE, Pipeline, SessionSpec
from orchestrator.policy import Allow, Deny, PolicyContext, decide
from orchestrator.preflight import (
    CheckResult,
    Runner,
    all_ok,
    check_aws,
    check_db_name,
    check_sst_stage,
    run_command,
    run_preflight,
)
from orchestrator.runner import QueryFn, StepResult, StepStatus, run_step_with_quota_retry
from orchestrator.state import StateDB

PHASE_PLAN, PHASE_CONTRACT, PHASE_CLAUDE, PHASE_CODEX, PHASE_VERIFY = 1, 2, 3, 4, 5
DECISIONS_FILE = "AUTONOMOUS-DECISIONS.md"
MAX_REPAIR_ROUNDS = 2
POST_STEP_TIMEOUT_S = 1800
_NOOP_OK = re.compile(r"\b0\s+added\b.*\b0\s+removed\b|\b0\s+removed\b.*\b0\s+added\b", re.I | re.S)


# ---- dependencies / options / outcomes -------------------------------------------------------


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass
class FlowDeps:
    """Everything with side effects outside state.db, injectable for tests."""

    memory: MemoryStore
    sdk_env: dict[str, str] = field(default_factory=dict, repr=False)  # API key lives here only
    query_fn: QueryFn | None = None
    runner: Runner | None = None  # subprocesses: tests, codex, git, post steps
    sleep: Callable[[float], Awaitable[None]] = _sleep
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    preflight: Callable[[Pipeline], list[CheckResult]] | None = None


@dataclass
class FlowOptions:
    only: str | None = None
    retry: bool = False  # resume: reset blocked/failed steps after a human fixed the cause
    setting_sources: list[str] | None = None  # None = user+project+local (hooks, CLAUDE.md)
    strict_mcp_config: bool = False
    max_turns: int | None = None
    model: str | None = None
    max_repair_rounds: int = MAX_REPAIR_ROUNDS
    codex_timeout_s: int = codex_mod.DEFAULT_TIMEOUT_S
    max_quota_waits: int = 24


@dataclass(frozen=True)
class SessionOutcome:
    session_id: str
    status: str  # done | blocked | failed | quota_wait | permission_denied
    reason: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    run_id: int | None
    status: str  # done | blocked | failed | quota_wait | permission_denied | preflight_failed
    sessions: tuple[SessionOutcome, ...] = ()
    reason: str | None = None


# ---- pure helpers ----------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


@dataclass(frozen=True)
class Plan:
    files: tuple[str, ...]
    commands: tuple[str, ...]
    risky: bool
    risks: tuple[str, ...]


def parse_plan(text: str | None) -> Plan | None:
    """The LAST ```json block of the plan message, validated; None if absent or malformed."""
    blocks = _JSON_BLOCK.findall(text or "")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    def strs(key: str) -> tuple[str, ...] | None:
        v = data.get(key, [])
        return tuple(v) if isinstance(v, list) and all(isinstance(x, str) for x in v) else None

    files, commands, risks = strs("files"), strs("commands"), strs("risks")
    if files is None or commands is None or risks is None:
        return None
    return Plan(files, commands, bool(data.get("risky", False)), risks)


def check_plan(plan: Plan, ctx: PolicyContext) -> tuple[str | None, list[str]]:
    """(blocking reason, notices). A plan that writes a forbidden path or flags itself risky stops
    the session; a planned command the policy would soft-deny only produces a notice (the agent
    adapts, e.g. leaves the migration to the orchestrator)."""
    if plan.risky:
        return "plan marked itself risky: " + ("; ".join(plan.risks) or "no reason given"), []
    notices: list[str] = []
    for path in plan.files:
        d = decide("Write", {"file_path": path}, ctx)
        if isinstance(d, Deny):
            kind = "hard stop" if d.hard_stop else "forbidden"
            return f"plan changes {path}: {kind}: {d.reason}", notices
    for cmd in plan.commands:
        d = decide("Bash", {"command": cmd}, ctx)
        if isinstance(d, Deny):
            if d.hard_stop:
                return f"plan runs `{cmd}`: hard stop: {d.reason}", notices
            notices.append(f"planned command `{cmd}` will be denied: {d.reason}")
    return None, notices


_DECISION_FIELD = re.compile(r"^[-*]\s*(Options|Chosen|Reason|Files)\s*:\s*(.*)$", re.I)


def parse_decisions(text: str) -> list[dict[str, str]]:
    """AUTONOMOUS-DECISIONS.md: `## <question>` + `- Options|Chosen|Reason|Files: ...` bullets."""
    out: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            if cur:
                out.append(cur)
            cur = {"question": line[3:].strip()}
        elif cur is not None and (m := _DECISION_FIELD.match(line.strip())):
            cur[m.group(1).lower()] = m.group(2).strip()
    if cur:
        out.append(cur)
    return [d for d in out if d.get("chosen") and d.get("reason")]


# ---- prompts ---------------------------------------------------------------------------------

RULES = (
    "Rules (enforced by the orchestrator, not negotiable):\n"
    "- Never run git commit/push/checkout/restore/reset/clean/stash; leave all changes "
    "uncommitted.\n"
    "- sst, aws, drizzle-kit and migrate/seed/deploy scripts are run by the orchestrator, "
    "not you.\n"
    "- Never touch production or staging. Never read or print environment variables or "
    ".env files.\n"
    "- Nobody can answer questions. If something is undecided, choose the most conservative, "
    "reversible option consistent with the plan files, and append it to "
    f"{DECISIONS_FILE} in the plan directory as: `## <question>` then bullets `- Options:`, "
    "`- Chosen:`, `- Reason:`, `- Files:`.\n"
)


def start_routine(session: SessionSpec, pipeline: Pipeline) -> str:
    deps = ", ".join(session.depends_on) or "none"
    handoffs = ", ".join(f"{pipeline.plan_dir}/handoff-{d.lower()}.md" for d in session.depends_on)
    return (
        "Session start routine (do this first, in order):\n"
        f"1. Read the handoff files of the sessions this one depends on (depends_on: {deps})"
        f"{': ' + handoffs if handoffs else ''}.\n"
        "2. Run `git status --short` and `git diff --stat` to see the current work tree.\n"
        "3. Run the smoke checks that already apply; fix nothing yet.\n"
        "4. Only then start the new work.\n"
    )


def memory_section(ctx_text: str) -> str:
    return f"\n## Memory graph context (read-only)\n{ctx_text}\n" if ctx_text else ""


def verify_prompt(
    *,
    graph_ok: bool,
    tests: str,
    handoff: Path,
    confidence_path: Path,
    failures: list[str],
    mem: str,
    plan_text: str,
    summary: str,
    claude_task_text: str,
    codex_task_text: str | None,
) -> str:
    return (
        f"{RULES}\n## Step 5 of 5: VERIFY (you are NOT the agent that wrote this code)\n"
        "Do not change source files. Your only writes: the handoff file, "
        f"{DECISIONS_FILE}, and (if allowed below) the memory graph.\n"
        "Rubric - check each against the task files and the code, with evidence:\n"
        "1. Every acceptance criterion in the task file(s) is met.\n"
        "2. The contract (SDL / types / contract tests) is honoured.\n"
        "3. The task file's standards/conventions are followed (read its 'Standard' sections), "
        "including raw-query counts where stated.\n"
        "4. All of these commands pass when YOU run them:\n"
        f"{tests or '- (none)'}\n"
        "5. If a `/codex-review` skill is available, run it on the diff and address findings "
        "by reporting them (do not edit code).\n"
        + (
            "6. Graph reconcile: update the memory graph for this session's decisions using "
            "ONLY the memory MCP tools (observations, new Decision:/Convention: entities). For "
            "every entity you create or change, also update its row in "
            f"{confidence_path} in the same step. Never delete entities and "
            "never touch 'Memory Model', 'Graph Legend' or 'Playbook'; write such wishes to "
            "the handoff under 'Ask first'.\n"
            if graph_ok
            else "6. The memory graph is UNAVAILABLE this run: do not call memory tools; list "
            "the graph updates you would make under a 'Pending graph writes' heading in the "
            "handoff.\n"
        )
        + f"\nWrite {handoff} now. Its FIRST line must be exactly `STATUS: DONE` (everything "
        "above holds) or `STATUS: BLOCKED` (anything does not), then findings with evidence "
        "and what remains.\n"
        + (
            "\n## Gate failures from the previous attempt (the code was repaired: re-verify)\n"
            + "\n".join(f"- {f}" for f in failures)
            + "\n"
            if failures
            else ""
        )
        + f"{memory_section(mem)}\n## Approved plan\n{plan_text}\n"
        f"## Claude track summary\n{summary}\n## Task\n{claude_task_text}"
        + (f"\n## Codex task\n{codex_task_text}" if codex_task_text else "")
    )


# ---- the flow --------------------------------------------------------------------------------


class _Stop(Exception):
    """Internal: unwinds a session with its outcome."""

    def __init__(self, outcome: SessionOutcome):
        self.outcome = outcome


class Flow:
    def __init__(
        self,
        pipeline: Pipeline,
        config: Config,
        db: StateDB,
        deps: FlowDeps,
        options: FlowOptions | None = None,
    ):
        self.pipeline = pipeline
        self.config = config
        self.db = db
        self.deps = deps
        self.opts = options or FlowOptions()
        self.repo = pipeline.repo
        self.secrets = secret_values(deps.sdk_env)
        self.run_id = 0

    # -- infrastructure ----------------------------------------------------------------------

    def _work_dir(self, session: SessionSpec) -> Path:
        d = self.config.checkpoint_dir / self.pipeline.name / f"run-{self.run_id}" / session.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _event(self, kind: str, detail: str, session: str | None = None) -> None:
        self.db.add_event(
            kind, redact(detail, self.secrets), run_id=self.run_id or None, session_id=session
        )

    def notify(self, session: str | None, headline: str) -> None:
        """MVP notice channel (O11): the events table plus logs/notices.log (dashboard reads it)."""
        self._event("notice", headline, session)
        try:
            self.config.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = self.deps.now().isoformat(timespec="seconds")
            with (self.config.log_dir / "notices.log").open("a") as fh:
                fh.write(f"{stamp} run={self.run_id} {session or '-'} {headline}\n")
        except OSError:
            pass

    def _policy(self, session: SessionSpec, phase: int, graph_usable: bool) -> PolicyContext:
        ctx = self.pipeline.policy_context(session)
        if phase == PHASE_PLAN:
            ctx = replace(ctx, read_only=True)  # holds even when the SDK forces mode `default`
        deny: set[str] = set()
        if phase != PHASE_VERIFY or not graph_usable:
            deny |= write_tool_names()  # the graph is written only by ⑤ verify
        if not graph_usable:
            deny |= all_tool_names()
        if deny:
            ctx = replace(ctx, config=ctx.config.tightened({"denied_mcp_tools": sorted(deny)}))
        return ctx

    def _snap(self, session: SessionSpec, label: str) -> Snapshot:
        return take_snapshot(
            self.repo, self._work_dir(session) / label, label, run=self.deps.runner
        )

    def _task_text(self, name: str) -> str:
        return (self.pipeline.plan_path / name).read_text()

    async def _agent(
        self,
        session: SessionSpec,
        phase: int,
        step_id: int,
        prompt: str,
        *,
        mode: str = "auto",
        disallowed: tuple[str, ...] = (),
        graph_usable: bool = False,
    ) -> StepResult:
        from orchestrator.runner import StepRequest

        request = StepRequest(
            prompt=prompt,
            cwd=self.repo,
            policy=self._policy(session, phase, graph_usable),
            timeout_s=self.config.call_timeout_s,
            permission_mode=mode,
            max_turns=self.opts.max_turns,
            model=self.opts.model,
            setting_sources=self.opts.setting_sources,
            strict_mcp_config=self.opts.strict_mcp_config,
            disallowed_tools=list(disallowed),
            env=dict(self.deps.sdk_env),
        )
        return await run_step_with_quota_retry(
            request,
            db=self.db,
            step_id=step_id,
            budget=self.pipeline.budget,
            quota_wait_s=self.config.quota_wait_s,
            max_waits=self.opts.max_quota_waits,
            sleep=self.deps.sleep,
            query_fn=self.deps.query_fn,
            max_calls_per_hour=self.config.max_calls_per_hour,
        )

    def _require_ok(self, session: SessionSpec, phase: int, res: StepResult) -> None:
        if res.status is StepStatus.DONE:
            return
        headline = f"{session.id} phase {phase}: {res.status.value}: {res.reason}"
        self.notify(session.id, headline)
        raise _Stop(SessionOutcome(session.id, res.status.value, res.reason))

    def _attempt(self, session: SessionSpec, phase: int, before: Snapshot, after: Snapshot,
                 error: str | None) -> None:  # fmt: skip
        self.db.add_attempt(
            self.run_id, session.id, phase, len(changed_files(before, after)),
            redact(error, self.secrets),
        )  # fmt: skip

    def _attempts(self, session: SessionSpec) -> list[Attempt]:
        return [Attempt(n, e) for n, e in self.db.attempts_for(self.run_id, session.id)]

    def _step_done(self, step_id: int) -> bool:
        return self.db.get_step(step_id).status == "done"

    def _mark_done(self, step_id: int, note: str | None = None) -> None:
        self.db.update_step(step_id, status="done", last_error=note)

    # -- phases ------------------------------------------------------------------------------

    async def _plan(self, session: SessionSpec, step_id: int, mem: str, graph_ok: bool) -> None:
        work = self._work_dir(session)
        if self._step_done(step_id):
            return
        prompt = (
            f"{start_routine(session, self.pipeline)}\n{RULES}\n"
            "## Step 1 of 5: PLAN (read-only)\n"
            "Read the task and the code it touches. Do NOT change any file. Write the plan as your "
            "final message and END it with exactly one fenced json block:\n"
            '```json\n{"files": ["repo-relative paths you will create or change"], '
            '"commands": ["shell commands you intend to run"], "risky": false, '
            '"risks": ["only if risky is true"]}\n```\n'
            "Set risky=true if the task can only be done by reaching production, changing money "
            "logic not in the task, schema outside the task, or deleting data.\n"
            f"{memory_section(mem)}\n## Task\n{self._task_text(session.claude_task)}"
        )
        plan: Plan | None = None
        for _ in range(2):  # one retry for a malformed plan block
            res = await self._agent(
                session, PHASE_PLAN, step_id, prompt, mode="plan",
                disallowed=("ExitPlanMode", "Write", "Edit", "MultiEdit", "NotebookEdit"),
                graph_usable=graph_ok,
            )  # fmt: skip
            self._require_ok(session, PHASE_PLAN, res)
            plan = parse_plan(res.output)
            if plan:
                (work / "plan.md").write_text(res.output or "")
                break
            self._event("notice", "plan has no valid json block; asking again", session.id)
        if plan is None:
            reason = "the plan message has no valid ```json block (files/commands/risky/risks)"
            self.db.update_step(step_id, status="blocked", last_error=reason)
            self.notify(session.id, f"{session.id} plan: BLOCKED: {reason}")
            raise _Stop(SessionOutcome(session.id, "blocked", reason))
        blocked, notices = check_plan(plan, self.pipeline.policy_context(session))
        for n in notices:
            self._event("notice", n, session.id)
        if blocked:
            self.db.update_step(step_id, status="blocked", last_error=blocked)
            self.notify(session.id, f"{session.id} plan: BLOCKED: {blocked}")
            raise _Stop(SessionOutcome(session.id, "blocked", blocked))
        self._mark_done(step_id)

    async def _contract(self, session: SessionSpec, step_id: int, mem: str, graph_ok: bool) -> None:
        if self._step_done(step_id):
            return
        if not session.codex_task:
            self._mark_done(step_id, "skipped: no codex_task")
            return
        work = self._work_dir(session)
        plan_text = (work / "plan.md").read_text() if (work / "plan.md").exists() else ""
        prompt = (
            f"{RULES}\n## Step 2 of 5: CONTRACT\n"
            "Write the shared contract the Codex track depends on (SDL, types, contract tests) "
            "exactly as the Claude task file prescribes, directly in the repo. Then reply with the "
            "contract Codex must follow: file paths, signatures, expectations, test names. "
            "Codex will run after you and may only change its allowlist.\n"
            f"{memory_section(mem)}\n## Approved plan\n{plan_text}\n"
            f"## Claude task\n{self._task_text(session.claude_task)}\n"
            f"## Codex task (for reference, do not do it)\n{self._task_text(session.codex_task)}"
        )
        res = await self._agent(session, PHASE_CONTRACT, step_id, prompt, graph_usable=graph_ok)
        self._require_ok(session, PHASE_CONTRACT, res)
        (work / "contract.md").write_text(res.output or "")
        self._mark_done(step_id)

    async def _claude_track(
        self, session: SessionSpec, step_id: int, mem: str, graph_ok: bool
    ) -> None:
        work = self._work_dir(session)
        if self._step_done(step_id):
            return
        plan_text = (work / "plan.md").read_text() if (work / "plan.md").exists() else ""
        contract = (work / "contract.md").read_text() if (work / "contract.md").exists() else ""
        prompt = (
            f"{start_routine(session, self.pipeline)}\n{RULES}\n## Step 3 of 5: CLAUDE TRACK\n"
            "Implement the Claude part of the task per the approved plan. Run the relevant tests. "
            "Do not do the Codex track's work. Finish with a short summary (what changed, what is "
            "left, any decision you recorded).\n"
            f"{memory_section(mem)}\n## Approved plan\n{plan_text}\n"
            + (f"## Contract written in step 2\n{contract}\n" if contract else "")
            + f"## Task\n{self._task_text(session.claude_task)}"
        )
        before = self._snap(session, "p3-before")
        res = await self._agent(session, PHASE_CLAUDE, step_id, prompt, graph_usable=graph_ok)
        after = self._snap(session, "codex-before")  # snapshot A
        self._attempt(session, PHASE_CLAUDE, before, after, res.reason)
        self._require_ok(session, PHASE_CLAUDE, res)
        (work / "claude-summary.md").write_text(res.output or "")
        if reason := breaker_tripped(self._attempts(session)):
            self.db.update_step(step_id, status="blocked", last_error=reason)
            self.notify(session.id, f"{session.id}: {reason}")
            raise _Stop(SessionOutcome(session.id, "blocked", reason))
        self._mark_done(step_id)

    async def _codex_track(
        self, session: SessionSpec, step_id: int, mem: str, graph_ok: bool
    ) -> tuple[Snapshot | None, Snapshot | None]:
        """Returns (codex_before, codex_after) for the allowlist gate, or (None, None)."""
        work = self._work_dir(session)
        if not session.codex_task:
            if not self._step_done(step_id):
                self._mark_done(step_id, "skipped: no codex_task")
            return None, None
        a = load_snapshot(work / "codex-before")
        if self._step_done(step_id) and (work / "codex-after" / "manifest.json").exists():
            b = load_snapshot(work / "codex-after")
            took_over = (work / "codex-fallback").exists()
            return (None, None) if took_over else (a, b)

        contract = (work / "contract.md").read_text() if (work / "contract.md").exists() else ""
        prompt = codex_mod.build_prompt(
            self._task_text(session.codex_task), contract, session.codex_allowlist
        )
        self.db.update_step(step_id, status="running", increment_attempts=True)

        attempts_seen: list[tuple[int, codex_mod.CodexResult]] = []

        def on_attempt(n: int, r: codex_mod.CodexResult) -> None:
            attempts_seen.append((n, r))  # runs in a worker thread: sqlite is written after it

        outcome = await asyncio.to_thread(
            codex_mod.run_codex_with_fallback,
            self.repo, prompt,
            timeout=self.opts.codex_timeout_s, run=self.deps.runner,
            secrets=self.secrets, on_attempt=on_attempt,
        )  # fmt: skip
        for n, r in attempts_seen:
            detail = {"attempt": n, "ok": r.ok, "error": r.error, "usage": r.usage}
            detail |= {"thread_id": r.thread_id, "events": r.events}
            self._event("codex_call", json.dumps(detail), session.id)
        b = self._snap(session, "codex-after")
        self._attempt(session, PHASE_CODEX, a, b, None if outcome.ok else outcome.last.error)
        outside = allowlist_violations(changed_files(a, b), session.codex_allowlist)
        if outside:  # checked even after failed attempts: partial edits count
            reason = f"Codex changed {len(outside)} file(s) outside its allowlist: " + ", ".join(
                outside[:10]
            )
            self.db.update_step(step_id, status="blocked", last_error=reason)
            self.notify(session.id, f"{session.id} codex: BLOCKED: {reason}")
            raise _Stop(SessionOutcome(session.id, "blocked", reason))

        if outcome.ok:
            self._mark_done(step_id)
            return a, b

        self._event(
            "codex_fallback",
            f"Codex failed {len(outcome.results)}x ({outcome.last.error}); Claude takes the task",
            session.id,
        )
        (work / "codex-fallback").write_text("claude took over\n")
        takeover = (
            f"{start_routine(session, self.pipeline)}\n{RULES}\n"
            "## Step 4 of 5: CODEX TASK TAKEN OVER BY CLAUDE\n"
            "Codex failed twice on this task; do it yourself now, exactly as its task file says. "
            "Finish with a short summary.\n"
            f"{memory_section(mem)}\n## Contract\n{contract}\n"
            f"## Codex task\n{self._task_text(session.codex_task)}"
        )
        res = await self._agent(session, PHASE_CODEX, step_id, takeover, graph_usable=graph_ok)
        c = self._snap(session, "codex-after")
        self._attempt(session, PHASE_CODEX, b, c, res.reason)
        self._require_ok(session, PHASE_CODEX, res)
        self._mark_done(step_id, "codex failed; claude took over")
        return None, None

    async def _verify(
        self, session: SessionSpec, step_id: int, mem: str, graph_ok: bool,
        codex: tuple[Snapshot | None, Snapshot | None], failures: list[str],
    ) -> tuple[GateVerdict, Snapshot, list[str]]:  # fmt: skip
        """One ⑤ pass: verify agent + gates. Returns (verdict, checkpoint, extra failures)."""
        work = self._work_dir(session)
        handoff = self.pipeline.plan_path / session.handoff
        decisions = self.pipeline.plan_path / DECISIONS_FILE
        plan_text = (work / "plan.md").read_text() if (work / "plan.md").exists() else ""
        summary = (
            (work / "claude-summary.md").read_text()
            if (work / "claude-summary.md").exists()
            else ""
        )
        tests = "\n".join(
            f"- `{t.splitlines()[0]}`" + (" ..." if "\n" in t else "") for t in session.tests
        )

        pre = self._snap(session, "verify-before")
        store = self.deps.memory
        mem_before = None
        extra: list[str] = []

        def prompt_for(graph: bool) -> str:
            return verify_prompt(
                graph_ok=graph, tests=tests, handoff=handoff, failures=failures, mem=mem,
                confidence_path=store.confidence_path, plan_text=plan_text, summary=summary,
                claude_task_text=self._task_text(session.claude_task),
                codex_task_text=self._task_text(session.codex_task) if session.codex_task else None,
            )  # fmt: skip

        stack = contextlib.ExitStack()
        if graph_ok:
            try:
                stack.enter_context(store.write_window())  # lock + backup, before the prompt
                mem_before = store.state()
            except MemoryBusy as exc:
                self._event(
                    "notice",
                    f"graph write window refused ({exc}); verifying without the graph",
                    session.id,
                )
                graph_ok = False
        with stack:
            res = await self._agent(
                session, PHASE_VERIFY, step_id, prompt_for(graph_ok), graph_usable=graph_ok
            )
            mem_after = store.state() if mem_before is not None else None
        self._require_ok(session, PHASE_VERIFY, res)

        if mem_before is not None and mem_after is not None:
            audit = store.audit(mem_before, mem_after)
            if audit.wrote:
                self._event(
                    "graph_write",
                    json.dumps({"added": audit.added, "changed": audit.changed}),
                    session.id,
                )
            if audit.ask_first_violation:
                path = store.write_pending(
                    f"{self.pipeline.name} {session.id}: graph change needs approval",
                    "\n".join(f"- {p}" for p in audit.problems())
                    + f"\n\nBackup before the run: {store.backup_root}\n",
                )
                reason = f"graph 'ask first' violation (see {path.name}): " + "; ".join(
                    audit.problems()
                )
                self.db.update_step(step_id, status="blocked", last_error=reason)
                self.notify(session.id, f"{session.id} verify: BLOCKED: {reason}")
                raise _Stop(SessionOutcome(session.id, "blocked", reason))
            extra += [f"memory: {p}" for p in audit.problems()]

        final = self._snap(session, "final")
        self.db.update_step(step_id, checkpoint_path=str(final.directory))
        rel = lambda p: p.resolve().relative_to(self.repo.resolve()).as_posix()  # noqa: E731
        try:
            allowed_verify_writes = [rel(handoff), rel(decisions)]
        except ValueError:
            allowed_verify_writes = []
        stray = allowlist_violations(changed_files(pre, final), allowed_verify_writes)
        if stray:
            extra.append(
                f"verify agent changed {len(stray)} file(s) it must not touch: "
                + ", ".join(stray[:10])
            )
        self._sync_decisions(session, decisions)

        verdict = evaluate(
            session=session, repo=self.repo, handoff=handoff, checkpoint=final,
            attempts=self._attempts(session), codex_before=codex[0], codex_after=codex[1],
            run=self.deps.runner, test_timeout=self.config.call_timeout_s,
        )  # fmt: skip
        return verdict, final, extra

    def _sync_decisions(self, session: SessionSpec, path: Path) -> None:
        try:
            entries = parse_decisions(path.read_text())
        except OSError:
            return
        known = self.db.decision_questions(self.run_id, session.id)
        for e in entries:
            if e["question"] not in known:
                self.db.add_decision(
                    self.run_id,
                    session.id,
                    *(redact(e[k], self.secrets) or "" for k in ("question", "chosen", "reason")),
                    options=e.get("options"), files=e.get("files"),
                )  # fmt: skip

    # -- post steps (O4) ---------------------------------------------------------------------

    def _post_steps(self, session: SessionSpec) -> str | None:
        """Run the develop2-only steps, each after a fresh stage check. Failure reason or None."""
        runner = self.deps.runner or run_command
        t = self.config.command_timeout_s
        seen: Counter[str] = Counter()
        done = {
            e.detail for e in self.db.events(self.run_id, "post_step") if e.session_id == session.id
        }
        for idx, name in enumerate(session.post_steps):
            repeat = seen[name]
            seen[name] += 1
            key = f"{idx}:{name}"
            if key in done:
                continue
            definition = POST_STEPS[name]
            if definition.stage != REQUIRED_STAGE:
                return f"post step {name}: stage {definition.stage!r} is not {REQUIRED_STAGE}"
            for check in (
                check_db_name(self.repo),
                check_sst_stage(self.repo),
                check_aws(runner, t),
            ):
                if not check.ok:
                    return f"post step {name} refused: preflight {check.name}: {check.detail}"
            res = runner(
                ["bash", "-c", definition.command], timeout=POST_STEP_TIMEOUT_S, cwd=self.repo
            )
            tail = "\n".join(res.output.splitlines()[-12:])
            self._event("post_step_output", f"{name} exit={res.returncode}\n{tail}", session.id)
            if res.timed_out or res.returncode != 0:
                return f"post step {name} failed (exit {res.returncode}, timed_out={res.timed_out})"
            if repeat and not _NOOP_OK.search(res.output):
                return (
                    f"post step {name} (run {repeat + 1}) did not report 0 added / 0 removed; "
                    "the second run must be a no-op"
                )
            self._event("post_step", key, session.id)
        return None

    # -- session / pipeline ------------------------------------------------------------------

    async def run_session(self, session: SessionSpec) -> SessionOutcome:
        steps = {p: self.db.ensure_step(self.run_id, session.id, p) for p in range(1, 6)}
        for step in steps.values():  # a human fixed the cause; `resume --retry`
            if step.status in ("blocked", "failed", "permission_denied") and self.opts.retry:
                self.db.reset_step(step.id)
        stuck = [s for s in (self.db.get_step(x.id) for x in steps.values())
                 if s.status in ("blocked", "failed", "permission_denied")]  # fmt: skip
        if stuck:
            s = stuck[0]
            return SessionOutcome(
                session.id, s.status,
                f"phase {s.phase} needs a human ({s.last_error}); fix it, then `resume --retry`",
            )  # fmt: skip

        store = self.deps.memory
        health = store.health()
        graph_ok = health.ok
        if not graph_ok:
            self._event("notice", f"memory graph unavailable: {health.detail}", session.id)
        try:
            mem = store.render_context(session.memory_entities) if graph_ok else ""
        except (OSError, ValueError) as exc:
            graph_ok, mem = False, ""
            self._event("notice", f"memory graph unreadable: {exc}", session.id)

        work = self._work_dir(session)
        if not (work / "start" / "manifest.json").exists():
            self._snap(session, "start")
        try:
            await self._plan(session, steps[1].id, mem, graph_ok)
            await self._contract(session, steps[2].id, mem, graph_ok)
            await self._claude_track(session, steps[3].id, mem, graph_ok)
            codex = await self._codex_track(session, steps[4].id, mem, graph_ok)

            failures: list[str] = []
            for round_no in range(self.opts.max_repair_rounds + 1):
                self.db.update_step(steps[5].id, status="pending")
                verdict, final, extra = await self._verify(
                    session, steps[5].id, mem, graph_ok, codex, failures
                )
                failures = [*verdict.failures, *extra]
                self._event(
                    "gate",
                    json.dumps({"round": round_no, "passed": not failures, "failures": failures}),
                    session.id,
                )
                if not failures:
                    break
                fatal = [
                    f for f in failures if f.startswith(("allowlist", "circuit", "verify agent"))
                ]
                handoff_blocked = any("handoff reports BLOCKED" in f for f in failures)
                if fatal or handoff_blocked or round_no >= self.opts.max_repair_rounds:
                    reason = "; ".join(failures)[:600]
                    self.db.update_step(steps[5].id, status="blocked", last_error=reason)
                    self.notify(session.id, f"{session.id} gates: BLOCKED: {reason}")
                    return SessionOutcome(session.id, "blocked", reason)
                before = self._snap(session, "repair-before")
                res = await self._agent(
                    session, PHASE_CLAUDE, steps[3].id,
                    f"{RULES}\n## Repair round {round_no + 1}\nThe gates failed. Fix the causes "
                    "(code, tests); do not weaken tests. Gate failures:\n"
                    + "\n".join(f"- {f}" for f in failures)
                    + f"\n\n## Task\n{self._task_text(session.claude_task)}",
                    graph_usable=graph_ok,
                )  # fmt: skip
                after = self._snap(session, "repair-after")
                self._attempt(
                    session, PHASE_CLAUDE, before, after, res.reason or "; ".join(failures)
                )
                self._require_ok(session, PHASE_CLAUDE, res)
                if reason := breaker_tripped(self._attempts(session)):
                    self.db.update_step(steps[5].id, status="blocked", last_error=reason)
                    self.notify(session.id, f"{session.id}: {reason}")
                    return SessionOutcome(session.id, "blocked", reason)

            self._mark_done(steps[5].id)
            if reason := self._post_steps(session):
                self.db.update_step(steps[5].id, status="blocked", last_error=reason)
                self.notify(session.id, f"{session.id} post step: BLOCKED: {reason}")
                return SessionOutcome(session.id, "blocked", reason)
        except _Stop as stop:
            return stop.outcome
        self._event("session_done", session.id, session.id)
        return SessionOutcome(session.id, "done")

    async def run(self, run_id: int | None = None) -> RunOutcome:
        order = self.pipeline.order()
        if self.opts.only:
            order = [s for s in order if s.id == self.opts.only]
            if not order:
                return RunOutcome(None, "failed", reason=f"unknown session {self.opts.only!r}")

        if run_id is None:
            self.run_id = self.db.start_run(
                self.pipeline.name, str(self.pipeline.source) if self.pipeline.source else None
            )
            self._event("run_start", json.dumps({"only": self.opts.only}))
        else:
            self.run_id = run_id
            self.db.set_run_status(run_id, "running")
            self._event("run_resume", "resumed")

        done_before = {e.session_id for e in self.db.events(self.run_id, "session_done")}
        outcomes: list[SessionOutcome] = []
        for session in order:
            if session.id in done_before:
                outcomes.append(SessionOutcome(session.id, "done", "already done"))
                continue
            outcome = await self.run_session(session)
            outcomes.append(outcome)
            if outcome.status != "done":
                self.db.finish_run(self.run_id, outcome.status)
                return RunOutcome(self.run_id, outcome.status, tuple(outcomes), outcome.reason)
        self.db.finish_run(self.run_id, "done")
        self.notify(None, f"run {self.run_id} finished: all sessions done")
        return RunOutcome(self.run_id, "done", tuple(outcomes))


async def run_pipeline(
    pipeline: Pipeline,
    config: Config,
    deps: FlowDeps,
    options: FlowOptions | None = None,
    *,
    run_id: int | None = None,
    db: StateDB | None = None,
) -> RunOutcome:
    """Preflight, then the flow. A failed preflight starts nothing (readme §6)."""
    check = deps.preflight or (lambda p: run_preflight(p, config, deps.runner))
    results = check(pipeline)
    if not all_ok(results):
        bad = "; ".join(f"{r.name}: {r.detail}" for r in results if not r.ok)
        return RunOutcome(None, "preflight_failed", reason=bad)
    own = db is None
    db = db or StateDB(config.state_db)
    try:
        return await Flow(pipeline, config, db, deps, options).run(run_id)
    finally:
        if own:
            db.close()


__all__ = [
    "Allow", "Flow", "FlowDeps", "FlowOptions", "RunOutcome", "SessionOutcome", "Plan",
    "check_plan", "parse_decisions", "parse_plan", "run_pipeline",
]  # fmt: skip
