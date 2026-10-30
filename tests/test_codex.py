"""codex.py: the subprocess is always mocked here (the one real call is in test_flow_real)."""

import json

from orchestrator.codex import (
    build_command,
    build_prompt,
    interpret,
    parse_events,
    run_codex,
    run_codex_with_fallback,
)
from orchestrator.preflight import CmdResult

# Recorded from `codex exec --sandbox workspace-write --skip-git-repo-check --json` (codex-cli
# 0.147.0, 2026-10-30), minus the thread id.
REAL_OK = "\n".join(
    [
        '{"type":"thread.started","thread_id":"01a18af2-7845-7d41-89e7-b0de5b2d6dff"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ok"}}',
        '{"type":"turn.completed","usage":{"input_tokens":20609,"cached_input_tokens":12800,'
        '"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}',
    ]
)


def ev(**kw):
    return json.dumps(kw)


def test_command_uses_the_required_flags_and_reads_the_prompt_from_stdin(tmp_path):
    cmd = build_command(tmp_path)
    assert cmd[:6] == ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check",
                       "--json"]  # fmt: skip
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)
    assert cmd[-1] == "-"
    assert (
        "danger-full-access" not in cmd and "--dangerously-bypass-approvals-and-sandbox" not in cmd
    )


def test_prompt_has_task_contract_allowlist_and_graph_rule():
    p = build_prompt("DO THE THING", "CONTRACT-X", ["src/a.ts", "src/b/*.ts"])
    assert "DO THE THING" in p and "CONTRACT-X" in p
    assert "- src/a.ts" in p and "- src/b/*.ts" in p
    assert "memory graph" in p and ".env" in p and "git commit" in p


def test_real_success_stream():
    r = interpret(REAL_OK, 0)
    assert r.ok and r.message == "ok" and r.error is None
    assert r.usage["input_tokens"] == 20609 and r.usage["output_tokens"] == 5
    assert r.thread_id.startswith("01a18af2") and r.events == 4
    assert r.cost_usd == 0.0


def test_progress_noise_between_events_is_ignored():
    noisy = "Reading additional input from stdin...\n" + REAL_OK + "\nnot json {"
    assert len(parse_events(noisy)) == 4
    assert interpret(noisy, 0).ok


def test_nonzero_exit_fails_with_the_error_event():
    out = "\n".join([ev(type="turn.started"), ev(type="error", message="model overloaded")])
    r = interpret(out, 1)
    assert not r.ok and "exit 1" in r.error and "model overloaded" in r.error


def test_turn_failed_fails_even_with_exit_zero():
    out = "\n".join(
        [ev(type="turn.started"), ev(type="turn.failed", error={"message": "context too long"})]
    )
    r = interpret(out, 0)
    assert not r.ok and "context too long" in r.error


def test_exit_zero_without_turn_completed_is_a_failure():
    r = interpret(ev(type="thread.started", thread_id="t"), 0)
    assert not r.ok and "turn.completed" in r.error


def test_transient_error_event_before_a_completed_turn_is_not_fatal():
    out = "\n".join([ev(type="error", message="reconnecting 1/5"), *REAL_OK.splitlines()])
    assert interpret(out, 0).ok


def test_timeout_is_a_failure_flagged_as_such():
    r = interpret("", -1, timed_out=True)
    assert not r.ok and r.timed_out and r.error == "timed out"


class FakeRunner:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, cmd, *, timeout, cwd=None, env=None, input_text=None):
        self.calls.append({"cmd": cmd, "timeout": timeout, "cwd": cwd, "input": input_text})
        return self.replies.pop(0)


OK = CmdResult(0, REAL_OK, "")
BAD = CmdResult(1, ev(type="error", message="nope"), "")


def test_run_codex_passes_the_prompt_on_stdin_with_cwd_and_timeout(tmp_path):
    run = FakeRunner(OK)
    r = run_codex(tmp_path, "PROMPT", timeout=77, run=run)
    assert r.ok
    call = run.calls[0]
    assert call["input"] == "PROMPT" and call["cwd"] == tmp_path and call["timeout"] == 77


def test_missing_codex_cli_is_reported_and_not_retried(tmp_path):
    run = FakeRunner(CmdResult(127, "", "codex: command not found", not_found=True))
    out = run_codex_with_fallback(tmp_path, "p", run=run)
    assert not out.ok and out.fell_back_to_claude and len(run.calls) == 1


def test_first_success_stops_the_loop(tmp_path):
    run = FakeRunner(OK, OK)
    out = run_codex_with_fallback(tmp_path, "p", run=run)
    assert out.ok and not out.fell_back_to_claude and len(run.calls) == 1


def test_one_failure_then_success_is_ok(tmp_path):
    run = FakeRunner(BAD, OK)
    seen = []
    out = run_codex_with_fallback(
        tmp_path, "p", run=run, on_attempt=lambda n, r: seen.append((n, r.ok))
    )
    assert out.ok and len(run.calls) == 2 and seen == [(1, False), (2, True)]


def test_two_failures_hand_the_task_to_claude(tmp_path):
    run = FakeRunner(BAD, CmdResult(-1, "", "", timed_out=True), OK)
    out = run_codex_with_fallback(tmp_path, "p", run=run)
    assert not out.ok and out.fell_back_to_claude
    assert len(run.calls) == 2  # exactly 2 attempts, the third reply is never used
    assert out.last.timed_out


def test_secrets_are_redacted_from_codex_error_text(tmp_path):
    leak = "sk-ant-api03-FAKEFAKEFAKEFAKE_abcdefghij"
    run = FakeRunner(CmdResult(1, ev(type="error", message=f"auth failed for {leak}"), ""))
    r = run_codex(tmp_path, "p", run=run, secrets=[leak])
    assert leak not in r.error
