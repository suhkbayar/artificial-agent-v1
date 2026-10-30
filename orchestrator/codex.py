"""Codex track (readme §4 ④; `codex-delegate` hard limit): `codex exec` as a subprocess.

    codex exec --sandbox workspace-write --skip-git-repo-check --json -C <repo> -

(the prompt goes on stdin).

`--json` prints one JSON event per line (observed with codex-cli 0.147.0):

    {"type":"thread.started","thread_id":"..."}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ok"}}
    {"type":"turn.completed","usage":{"input_tokens":..,"cached_input_tokens":..,"output_tokens":..}}

Failures arrive as `turn.failed` / `error` events and/or a non-zero exit. Codex reports tokens, not
dollars, so `CodexResult.cost_usd` is always 0.0 and the budget guard is the timeout; the token
usage is stored as an event.

Two failed attempts hand the task to Claude (`run_codex_with_fallback`). Codex never touches the
memory graph; the prompt says so. Files Codex changed are found by the caller from snapshots taken
around the call (gates.changed_files), never from what Codex says it did.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.envfile import redact
from orchestrator.preflight import Runner, run_command

MAX_ATTEMPTS = 2  # codex-delegate: after 2 failures Claude takes the task
DEFAULT_TIMEOUT_S = 1800


@dataclass(frozen=True)
class CodexResult:
    ok: bool
    message: str | None = None  # last agent_message text
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    thread_id: str | None = None
    returncode: int | None = None
    timed_out: bool = False
    events: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class CodexOutcome:
    ok: bool
    results: tuple[CodexResult, ...]
    fell_back_to_claude: bool

    @property
    def last(self) -> CodexResult:
        return self.results[-1]


def build_command(repo: Path, *, model: str | None = None) -> list[str]:
    cmd = [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--json",
        "-C",
        str(repo),
    ]
    if model:
        cmd += ["-m", model]
    return [*cmd, "-"]  # "-" = read the prompt from stdin


def build_prompt(task_text: str, contract: str, allowlist: Sequence[str]) -> str:
    """Task file + contract from step ② + the fixed rules Codex must follow."""
    files = "\n".join(f"- {g}" for g in allowlist) or "- (none: change nothing)"
    return (
        f"{task_text.strip()}\n\n"
        f"## Contract from the Claude track (step 2)\n{contract.strip() or '(none)'}\n\n"
        "## Rules enforced by the orchestrator\n"
        "- Change ONLY files matching this allowlist; anything else is reverted and fails the "
        f"gate:\n{files}\n"
        "- Do not run git commit, push, checkout, restore, reset, clean or stash. Leave changes "
        "uncommitted.\n"
        "- Do not read or write .env files. Do not touch the memory graph (memory MCP, "
        "memory.jsonl).\n"
        "- Run the verification commands from the task and finish with a short summary.\n"
    )


def parse_events(stdout: str) -> list[dict[str, Any]]:
    """JSONL -> dicts. Lines that are not JSON objects (progress noise) are skipped."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _error_text(event: dict[str, Any]) -> str | None:
    err = event.get("error")
    if isinstance(err, dict):
        err = err.get("message")
    msg = err or event.get("message")
    return str(msg) if msg else None


def interpret(
    stdout: str, returncode: int, *, timed_out: bool = False, stderr: str = ""
) -> CodexResult:
    """Pure: what a finished (or killed) `codex exec --json` run means."""
    events = parse_events(stdout)
    thread_id: str | None = None
    message: str | None = None
    usage: dict[str, Any] = {}
    completed = False
    errors: list[str] = []
    for ev in events:
        kind = ev.get("type")
        if kind == "thread.started":
            thread_id = ev.get("thread_id")
        elif kind == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                message = str(item["text"])
        elif kind == "turn.completed":
            completed = True
            usage = ev.get("usage") or {}
        elif kind == "turn.failed":
            completed = False
            if text := _error_text(ev):
                errors.append(text)
        elif kind == "error":  # may be a transient notice; only fatal if no turn completed
            if text := _error_text(ev):
                errors.append(text)

    if timed_out:
        return CodexResult(False, message, "timed out", usage, thread_id, None, True, len(events))
    if returncode != 0:
        detail = errors[-1] if errors else (stderr.strip().splitlines() or [""])[-1]
        return CodexResult(
            False, message, f"exit {returncode}: {detail}"[:400], usage, thread_id, returncode,
            False, len(events),
        )  # fmt: skip
    if not completed:
        detail = errors[-1] if errors else "no turn.completed event in the output"
        return CodexResult(False, message, detail[:400], usage, thread_id, 0, False, len(events))
    return CodexResult(True, message, None, usage, thread_id, 0, False, len(events))


def run_codex(
    repo: Path,
    prompt: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_S,
    model: str | None = None,
    run: Runner | None = None,
    secrets: Sequence[str] = (),
) -> CodexResult:
    runner = run or run_command
    res = runner(build_command(repo, model=model), timeout=timeout, cwd=repo, input_text=prompt)
    if res.not_found:
        return CodexResult(False, error="codex CLI not found", returncode=127)
    out = interpret(res.stdout, res.returncode, timed_out=res.timed_out, stderr=res.stderr)
    if out.error:  # error text may echo prompt/stderr content
        return CodexResult(
            out.ok, out.message, redact(out.error, secrets), out.usage, out.thread_id,
            out.returncode, out.timed_out, out.events,
        )  # fmt: skip
    return out


def run_codex_with_fallback(
    repo: Path,
    prompt: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_S,
    max_attempts: int = MAX_ATTEMPTS,
    run: Runner | None = None,
    model: str | None = None,
    secrets: Sequence[str] = (),
    on_attempt: Any = None,
) -> CodexOutcome:
    """Up to `max_attempts` tries; the caller hands the task to Claude if none succeeds."""
    results: list[CodexResult] = []
    for n in range(1, max_attempts + 1):
        result = run_codex(repo, prompt, timeout=timeout, model=model, run=run, secrets=secrets)
        results.append(result)
        if on_attempt:
            on_attempt(n, result)
        if result.ok:
            return CodexOutcome(True, tuple(results), False)
        if result.error == "codex CLI not found":  # retrying cannot help
            break
    return CodexOutcome(False, tuple(results), True)
