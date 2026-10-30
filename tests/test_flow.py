"""Session flow ①-⑤ on the toy repo with a scripted SDK and a faked Codex (offline)."""

import json
import os
from dataclasses import replace

import pytest
from claude_agent_sdk import ProcessError

from orchestrator import flow as flow_mod
from orchestrator.flow import (
    Plan,
    check_plan,
    parse_decisions,
    parse_plan,
    run_pipeline,
)
from orchestrator.policy import PolicyContext
from orchestrator.preflight import CheckResult, CmdResult
from tests.flowkit import (
    CODEX_OK_STREAM,
    DEFAULT,
    GOOD_PLAN,
    build_world,
    hook_decision,
    plan_text,
)
from tests.test_secrets import FAKE_KEY, dump_db


@pytest.fixture
def w(toy_repo, tmp_path, mem_dir):
    world = build_world(toy_repo, tmp_path, mem_dir)
    yield world
    world.db.close()


async def go(w, **opts):
    options = replace(w.options, **opts) if opts else w.options
    return await run_pipeline(w.pipeline, w.config, w.deps, options, db=w.db)


def step(w, run_id, session, phase):
    return w.db.ensure_step(run_id, session, phase)


# ---- pure helpers ------------------------------------------------------------------------------


def test_parse_plan_takes_the_last_valid_block():
    text = plan_text({"files": ["old"], "commands": [], "risky": False, "risks": []})
    text += "\nrevised:\n" + plan_text(
        {"files": ["new.py"], "commands": ["ls"], "risky": False, "risks": []}
    )
    assert parse_plan(text) == Plan(("new.py",), ("ls",), False, ())


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "no block here",
        "```json\n{not json}\n```",
        '```json\n["a"]\n```',
        '```json\n{"files": "x", "commands": [], "risks": []}\n```',
        '```json\n{"files": [1], "commands": [], "risks": []}\n```',
    ],
)
def test_parse_plan_rejects_malformed_blocks(text):
    assert parse_plan(text) is None


def test_check_plan_rules(toy_repo):
    ctx = PolicyContext(repo=toy_repo)
    ok = Plan(("calc.py",), ("python3 test_calc.py",), False, ())
    assert check_plan(ok, ctx) == (None, [])
    assert "risky" in check_plan(replace(ok, risky=True, risks=("touches prod",)), ctx)[0]
    assert (
        "hard stop"
        in check_plan(Plan(("packages/db/src/schemas/core/x.ts",), (), False, ()), ctx)[0]
    )
    assert "forbidden" in check_plan(Plan((".github/workflows/ci.yml",), (), False, ()), ctx)[0]
    assert "hard stop" in check_plan(Plan((), ("yarn migrate:production",), False, ()), ctx)[0]
    blocked, notices = check_plan(Plan((), ("git push origin main",), False, ()), ctx)
    assert blocked is None and "will be denied" in notices[0]  # the agent adapts; no block


def test_parse_decisions():
    md = (
        "# log\n\n## Which cache TTL?\n- Options: 30s, 60s\n- Chosen: 60s\n"
        "- Reason: matches README\n"
        "- Files: a.ts\n\n## Incomplete\n- Chosen: x\n"
    )
    [d] = parse_decisions(md)
    assert d["question"] == "Which cache TTL?" and d["chosen"] == "60s" and d["files"] == "a.ts"


# ---- the happy path ----------------------------------------------------------------------------


async def test_full_flow_with_codex_is_done(w):
    out = await go(w, only="S1")
    assert out.status == "done", out
    assert w.agent.phases() == ["plan", "contract", "claude", "verify"]
    assert (w.repo / "calc.py").read_text().count("ValueError") == 1
    assert (w.repo / "notes" / "codex.md").exists()  # written by the faked Codex
    assert (w.repo / "plan" / "handoff-s1.md").read_text().startswith("STATUS: DONE")

    run = w.db.latest_run()
    assert run.status == "done" and run.pipeline_path == str(w.yaml_path)
    assert [s.status for s in w.db.steps_for_run(run.id)] == ["done"] * 5
    assert step(w, run.id, "S1", 5).checkpoint_path  # the checkpoint the gate saw
    assert run.total_cost_usd == pytest.approx(0.2)  # 4 agent calls x $0.05; Codex has no USD
    assert [e.detail for e in w.db.events(run.id, "session_done")] == ["S1"]
    gate = [json.loads(e.detail) for e in w.db.events(run.id, "gate")]
    assert gate == [{"round": 0, "passed": True, "failures": []}]
    codex_ev = json.loads(w.db.events(run.id, "codex_call")[0].detail)
    assert codex_ev["ok"] and codex_ev["usage"]["input_tokens"] == 100


async def test_every_phase_is_a_fresh_sdk_session_with_the_right_mode(w):
    await go(w, only="S1")
    modes = {c.phase: c.options.permission_mode for c in w.agent.calls}
    assert modes == {"plan": "plan", "contract": "auto", "claude": "auto", "verify": "auto"}
    plan = w.agent.calls[0].options
    assert {"ExitPlanMode", "Write", "Edit", "MultiEdit"} <= set(plan.disallowed_tools)
    assert all(c.options.setting_sources == [] for c in w.agent.calls)
    assert "bypassPermissions" not in modes.values()


async def test_codex_gets_the_task_contract_and_allowlist_on_stdin_in_the_repo(w):
    await go(w, only="S1")
    [call] = w.router.codex_calls
    assert call["cwd"] == w.repo and call["cmd"][-1] == "-" and "workspace-write" in call["cmd"]
    prompt = call["input"]
    assert "Create notes/codex.md" in prompt  # the codex task file
    assert "CONTRACT: notes/codex.md must exist" in prompt  # ② output
    assert "- notes/*.md" in prompt and "memory graph" in prompt


async def test_sessions_run_in_dependency_order_and_status_is_reported(w):
    out = await go(w)
    assert out.status == "done" and [s.session_id for s in out.sessions] == ["S1", "S2"]
    assert w.agent.phases("S2") == ["plan", "claude", "verify"]  # no codex_task: ② skipped
    run = w.db.latest_run()
    assert step(w, run.id, "S2", 2).last_error == "skipped: no codex_task"
    assert step(w, run.id, "S2", 4).last_error == "skipped: no codex_task"


async def test_memory_entities_are_injected_read_only_into_prompts(w):
    await go(w, only="S1")
    assert "Memory graph context" in w.agent.calls[0].prompt
    assert "### Agent Orchestrator" in w.agent.calls[2].prompt


async def test_the_graph_is_writable_only_in_the_verify_phase(w):
    seen = {}

    async def probe(agent, call):
        seen[call.phase] = [
            await hook_decision(call, "mcp__memory__add_observations",
                                {"observations": []}, f"{call.phase}-w"),
            await hook_decision(call, "mcp__memory__open_nodes", {"names": []}, f"{call.phase}-r"),
            await hook_decision(call, "mcp__memory__read_graph", {}, f"{call.phase}-g"),
        ]  # fmt: skip
        return DEFAULT

    for phase in ("plan", "contract", "claude", "verify"):
        w.agent.handlers[phase] = probe
    await go(w, only="S1")
    assert seen["plan"] == seen["contract"] == seen["claude"] == ["deny", "allow", "deny"]
    assert seen["verify"] == ["allow", "allow", "deny"]  # read_graph is never allowed


async def test_the_api_key_reaches_the_sdk_but_nothing_else(w, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    w.deps.sdk_env = {"ANTHROPIC_API_KEY": FAKE_KEY}
    w.agent.handlers["plan"] = lambda agent, call: plan_text(prose=f"leak attempt {FAKE_KEY}")
    out = await go(w, only="S1")
    assert out.status == "done"
    assert all(c.options.env == {"ANTHROPIC_API_KEY": FAKE_KEY} for c in w.agent.calls)
    assert FAKE_KEY not in dump_db(w.config.state_db)
    assert FAKE_KEY not in (w.config.log_dir / "notices.log").read_text()
    for path in (w.config.checkpoint_dir).rglob("plan.md"):
        assert FAKE_KEY not in path.read_text()  # even the saved artefacts are redacted
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert FAKE_KEY not in repr(w.deps)


async def test_unknown_only_session_is_an_error(w):
    out = await go(w, only="S9")
    assert out.status == "failed" and "S9" in out.reason and w.db.latest_run() is None


async def test_failed_preflight_starts_nothing(w):
    w.deps.preflight = lambda p: [CheckResult("DB_NAME", False, "DB_NAME=qinno is PRODUCTION")]
    out = await go(w, only="S1")
    assert out.status == "preflight_failed" and "PRODUCTION" in out.reason
    assert w.db.latest_run() is None and w.agent.calls == [] and w.router.codex_calls == []


# ---- ① plan and the policy check ---------------------------------------------------------------


@pytest.mark.parametrize(
    "plan, needle",
    [
        ({**GOOD_PLAN, "risky": True, "risks": ["needs prod data"]}, "risky: needs prod data"),
        ({**GOOD_PLAN, "files": [".github/workflows/ci.yml"]}, "forbidden"),
        ({**GOOD_PLAN, "files": ["packages/db/src/schemas/core/roles.ts"]}, "hard stop"),
        ({**GOOD_PLAN, "commands": ["yarn migrate:production"]}, "hard stop"),
    ],
)
async def test_a_risky_plan_blocks_before_any_code_is_written(w, plan, needle):
    w.agent.handlers["plan"] = lambda agent, call: plan_text(plan)
    out = await go(w, only="S1")
    assert out.status == "blocked" and needle in out.reason
    assert w.agent.phases() == ["plan"]
    run = w.db.latest_run()
    assert step(w, run.id, "S1", 1).status == "blocked" and run.status == "blocked"
    assert needle in (w.config.log_dir / "notices.log").read_text()
    assert (w.repo / "calc.py").read_text().count("ValueError") == 0  # untouched


async def test_malformed_plan_gets_one_retry_then_blocks(w):
    w.agent.handlers["plan"] = lambda agent, call: "a plan with no json block"
    out = await go(w, only="S1")
    assert out.status == "blocked" and "```json" in out.reason
    assert w.agent.phases() == ["plan", "plan"]


async def test_malformed_plan_once_then_good_continues(w):
    replies = iter(["no block", plan_text()])
    w.agent.handlers["plan"] = lambda agent, call: next(replies)
    out = await go(w, only="S1")
    assert out.status == "done" and w.agent.count("plan") == 2


async def test_soft_denied_planned_command_is_only_a_notice(w):
    plan = {**GOOD_PLAN, "commands": ["yarn seed:merchant"]}
    w.agent.handlers["plan"] = lambda agent, call: plan_text(plan)
    out = await go(w, only="S1")
    assert out.status == "done"
    notes = [e.detail for e in w.db.events(w.db.latest_run().id, "notice")]
    assert any("will be denied" in n for n in notes)


# ---- ③④ Claude and Codex -----------------------------------------------------------------------


async def test_codex_failing_twice_hands_the_task_to_claude(w):
    bad = CmdResult(1, '{"type":"error","message":"boom"}', "")
    w.router.codex_script = [bad, bad]
    out = await go(w, only="S1")
    assert out.status == "done"
    assert len(w.router.codex_calls) == 2
    assert w.agent.phases() == ["plan", "contract", "claude", "takeover", "verify"]
    assert (w.repo / "notes" / "codex.md").read_text().endswith("(by claude)\n")
    run = w.db.latest_run()
    assert "Claude takes the task" in w.db.events(run.id, "codex_fallback")[0].detail
    assert step(w, run.id, "S1", 4).last_error == "codex failed; claude took over"


async def test_codex_changing_a_file_outside_its_allowlist_blocks_before_verify(w):
    def sneaky(repo, prompt):
        (repo / "calc.py").write_text("# codex was here\n")
        (repo / "notes").mkdir(exist_ok=True)
        (repo / "notes" / "codex.md").write_text("ok\n")
        return CmdResult(0, CODEX_OK_STREAM, "")

    w.router.codex_script = [sneaky]
    out = await go(w, only="S1")
    assert (
        out.status == "blocked"
        and "outside its allowlist" in out.reason
        and "calc.py" in out.reason
    )
    assert "verify" not in w.agent.phases()
    assert step(w, w.db.latest_run().id, "S1", 4).status == "blocked"


async def test_partial_codex_edits_outside_the_allowlist_count_even_when_codex_failed(w):
    def partial(repo, prompt):
        (repo / "README.md").write_text("tampered\n")
        return CmdResult(1, '{"type":"error","message":"died"}', "")

    w.router.codex_script = [partial, CmdResult(1, "", "died")]
    out = await go(w, only="S1")
    assert out.status == "blocked" and "README.md" in out.reason
    assert "takeover" not in w.agent.phases()


async def test_budget_exhaustion_blocks_with_a_clear_reason(w):
    w.reload(budget_usd={"per_call": 2, "per_session": 0.07, "total": 100})
    out = await go(w, only="S1")  # plan costs 0.05, the next call has 0.02 left, then none
    assert out.status == "blocked" and "session budget exhausted" in out.reason
    assert w.agent.phases() == ["plan", "contract"]  # ③ never started


# ---- ⑤ verify and gates ------------------------------------------------------------------------


async def test_failing_tests_trigger_a_repair_round_then_pass(w):
    state = {"n": 0}

    def claude(agent, call):
        state["n"] += 1
        if state["n"] == 1:  # first attempt does nothing useful but touches a file
            (w.repo / "scratch.txt").write_text("wip\n")
            return "tried"
        return DEFAULT

    w.agent.handlers["claude"] = claude
    w.agent.handlers["repair"] = lambda a, c: DEFAULT
    out = await go(w, only="S1")
    assert out.status == "done", out
    assert w.agent.phases() == ["plan", "contract", "claude", "verify", "repair", "verify"]
    gates = [json.loads(e.detail) for e in w.db.events(w.db.latest_run().id, "gate")]
    assert [g["passed"] for g in gates] == [False, True]
    assert "python3 test_calc.py" in gates[0]["failures"][0]
    assert "python3 test_calc.py" in w.agent.calls[4].prompt  # the repair prompt lists the failure


async def test_gate_runs_the_tests_itself_a_lying_handoff_does_not_pass(w):
    def touch(name):
        def handler(agent, call):
            (w.repo / name).write_text("x")
            return "done"

        return handler

    w.agent.handlers["claude"] = touch("scratch.txt")
    w.agent.handlers["repair"] = touch("scratch2.txt")
    out = await go(w, only="S1", max_repair_rounds=1)
    assert out.status == "blocked" and "python3 test_calc.py" in out.reason
    assert (w.repo / "plan" / "handoff-s1.md").read_text().startswith("STATUS: DONE")


async def test_verify_reporting_blocked_stops_without_repair(w):
    def verify(agent, call):
        (w.repo / "plan" / "handoff-s1.md").write_text("STATUS: BLOCKED\nacceptance 3 unmet\n")
        return "blocked"

    w.agent.handlers["verify"] = verify
    out = await go(w, only="S1")
    assert out.status == "blocked" and "handoff reports BLOCKED" in out.reason
    assert "repair" not in w.agent.phases()


async def test_verify_agent_may_not_change_source_files(w):
    def verify(agent, call):
        (w.repo / "calc.py").write_text("# verifier fixed it\n")
        (w.repo / "plan" / "handoff-s1.md").write_text("STATUS: DONE\n")
        return "ok"

    w.agent.handlers["verify"] = verify
    out = await go(w, only="S1")
    assert (
        out.status == "blocked" and "verify agent changed" in out.reason and "calc.py" in out.reason
    )


async def test_verify_may_write_the_handoff_and_decisions_file(w):
    def verify(agent, call):
        (w.repo / "plan" / "AUTONOMOUS-DECISIONS.md").write_text(
            "## Which TTL?\n- Options: 30s, 60s\n- Chosen: 60s\n- Reason: README says 60s\n"
            "- Files: calc.py\n"
        )
        return DEFAULT

    w.agent.handlers["verify"] = verify
    out = await go(w, only="S1")
    assert out.status == "done"
    run = w.db.latest_run()
    assert w.db.decision_count(run.id) == 1
    f = w.flow()
    f.run_id = run.id
    f._sync_decisions(w.pipeline.session("S1"), w.repo / "plan" / "AUTONOMOUS-DECISIONS.md")
    assert w.db.decision_count(run.id) == 1  # syncing again never duplicates


async def test_circuit_breaker_blocks_after_three_attempts_that_change_nothing(w):
    w.agent.handlers["claude"] = lambda a, c: "I looked at it"  # changes no file
    w.agent.handlers["repair"] = lambda a, c: "still looking"
    out = await go(w, only="S1", max_repair_rounds=5)
    assert out.status == "blocked" and "circuit breaker" in out.reason
    run = w.db.latest_run()
    attempts = w.db.attempts_for(run.id, "S1")
    # ③ plus two repairs made no change; the codex attempt changed a file but is separate
    assert [n for n, _ in attempts].count(0) >= 3


async def test_attempt_history_is_restored_from_state_db_on_resume(w):
    run_id = w.db.start_run(w.pipeline.name, str(w.yaml_path))
    for _ in range(2):  # two empty attempts recorded by an earlier process
        w.db.add_attempt(run_id, "S1", 3, 0, None)
    w.agent.handlers["claude"] = lambda a, c: "nothing to do"  # a third empty one
    out = await w.flow(only="S1").run(run_id)
    assert out.status == "blocked" and "circuit breaker" in out.reason
    assert w.agent.phases() == ["plan", "contract", "claude"]  # stopped right after ③


# ---- resume ------------------------------------------------------------------------------------


async def test_resume_continues_from_the_failed_step_without_redoing_earlier_phases(w):
    w.agent.handlers["verify"] = lambda a, c: ProcessError("cli crashed", exit_code=1, stderr="x")
    out = await go(w, only="S1")
    assert out.status == "failed"
    run = w.db.latest_run()
    assert [s.status for s in w.db.steps_for_run(run.id)] == ["done"] * 4 + ["failed"]
    calls_before = list(w.agent.phases())
    codex_before = len(w.router.codex_calls)

    # without --retry a failed step needs a human
    again = await w.flow(only="S1").run(run.id)
    assert again.status == "failed" and "resume --retry" in again.reason
    assert w.agent.phases() == calls_before

    w.agent.handlers.pop("verify")  # the cause is fixed
    done = await w.flow(only="S1", retry=True).run(run.id)
    assert done.status == "done", done
    assert w.agent.phases() == [*calls_before, "verify"]  # ONLY verify ran again
    assert len(w.router.codex_calls) == codex_before
    assert w.db.get_run(run.id).status == "done"


async def test_resume_after_quota_wait_needs_no_retry_flag(w):
    from claude_agent_sdk import RateLimitEvent, RateLimitInfo

    def quota(agent, call):
        return [RateLimitEvent(RateLimitInfo(status="rejected", rate_limit_type="five_hour",
                                              resets_at=1), "u", "s1")]  # fmt: skip

    w.agent.handlers[("claude", "S1")] = quota
    out = await go(w, only="S1", max_quota_waits=0)
    assert out.status == "quota_wait"
    run = w.db.latest_run()
    assert step(w, run.id, "S1", 3).status == "quota_wait"
    w.agent.handlers.pop(("claude", "S1"))
    done = await w.flow(only="S1").run(run.id)
    assert done.status == "done"
    assert w.agent.phases().count("plan") == 1  # ① was not repeated


async def test_resume_skips_sessions_that_are_already_done(w):
    w.agent.handlers[("plan", "S2")] = lambda a, c: ProcessError("boom", exit_code=1, stderr="")
    out = await go(w)
    assert out.status == "failed" and [s.status for s in out.sessions] == ["done", "failed"]
    run = w.db.latest_run()
    w.agent.handlers.pop(("plan", "S2"))
    s1_calls = w.agent.phases("S1")
    done = await w.flow(retry=True).run(run.id)
    assert done.status == "done"
    assert [s.reason for s in done.sessions][0] == "already done"
    assert w.agent.phases("S1") == s1_calls  # S1 was not touched again


async def test_a_blocked_run_cannot_be_resumed_without_retry(w):
    w.agent.handlers["plan"] = lambda a, c: plan_text({**GOOD_PLAN, "risky": True, "risks": ["x"]})
    await go(w, only="S1")
    run = w.db.latest_run()
    calls = len(w.agent.calls)
    out = await w.flow(only="S1").run(run.id)
    assert out.status == "blocked" and "needs a human" in out.reason
    assert len(w.agent.calls) == calls


# ---- memory graph ------------------------------------------------------------------------------


async def test_verify_runs_inside_the_write_window_with_a_backup(w):
    seen = {}

    def verify(agent, call):
        seen["locked"] = (w.mem_dir / ".reconcile.lock").is_dir()
        seen["backups"] = sorted((w.mem_dir / "backups-orchestrator").iterdir())
        return DEFAULT

    w.agent.handlers["verify"] = verify
    await go(w, only="S1")
    assert seen["locked"] and len(seen["backups"]) == 1
    assert not (w.mem_dir / ".reconcile.lock").exists()  # released afterwards


async def test_unhealthy_graph_continues_without_it_and_denies_every_memory_tool(w):
    (w.mem_dir / "memory.jsonl").write_text("{corrupt\n")
    seen = {}

    async def verify(agent, call):
        seen["tools"] = [
            await hook_decision(call, f"mcp__memory__{t}", {"names": []}, f"id-{t}")
            for t in ("open_nodes", "add_observations")
        ]
        seen["prompt"] = call.prompt
        return DEFAULT

    w.agent.handlers["verify"] = verify
    out = await go(w, only="S1")
    assert out.status == "done"
    assert seen["tools"] == ["deny", "deny"] and "UNAVAILABLE" in seen["prompt"]
    notes = [e.detail for e in w.db.events(w.db.latest_run().id, "notice")]
    assert any("memory graph unreadable" in n or "unavailable" in n for n in notes)
    assert not (w.mem_dir / ".reconcile.lock").exists()


async def test_a_foreign_lock_at_verify_time_degrades_to_no_graph(w):
    def claude(agent, call):  # reconcile starts while the session is in progress
        (w.mem_dir / ".reconcile.lock").mkdir()
        return DEFAULT

    w.agent.handlers["claude"] = claude
    out = await go(w, only="S1")
    assert out.status == "done"
    assert (w.mem_dir / ".reconcile.lock").is_dir()  # not ours, never removed
    assert "UNAVAILABLE" in w.agent.calls[-1].prompt  # the prompt matches the tools it was given
    notes = " ".join(e.detail for e in w.db.events(w.db.latest_run().id, "notice"))
    assert "graph write window refused" in notes


async def test_removing_a_graph_entity_is_ask_first_and_blocks(w):
    def verify(agent, call):
        lines = (w.mem_dir / "memory.jsonl").read_text().splitlines()
        keep = [ln for ln in lines if json.loads(ln).get("name") != "Agent Orchestrator"]
        (w.mem_dir / "memory.jsonl").write_text("\n".join(keep) + "\n")
        return DEFAULT

    w.agent.handlers["verify"] = verify
    out = await go(w, only="S1")
    assert out.status == "blocked" and "ask first" in out.reason
    pending = list((w.mem_dir / "logs").glob("pending-*"))
    assert len(pending) == 1 and "Agent Orchestrator" in pending[0].read_text()


async def test_graph_write_without_its_confidence_row_fails_the_gate(w):
    def verify(agent, call):
        with (w.mem_dir / "memory.jsonl").open("a") as fh:
            fh.write(
                json.dumps({"type": "entity", "name": "Decision: Agent Orchestrator — probe",
                            "entityType": "Decision", "observations": ["x"]}) + "\n"
            )  # fmt: skip
        return DEFAULT

    w.agent.handlers["verify"] = verify
    w.agent.handlers["repair"] = lambda a, c: DEFAULT
    out = await go(w, only="S1")
    gates = [json.loads(e.detail) for e in w.db.events(w.db.latest_run().id, "gate")]
    assert any("confidence.jsonl row not updated" in f for f in gates[0]["failures"])
    assert [g["passed"] for g in gates] == [False, True]  # repaired, then the baseline is clean
    assert out.status == "done"
    assert any(e.kind == "graph_write" for e in w.db.events(w.db.latest_run().id))


# ---- post steps (O4) ---------------------------------------------------------------------------


@pytest.fixture
def staged(toy_repo, tmp_path, mem_dir, monkeypatch):
    world = build_world(toy_repo, tmp_path, mem_dir, requires_stage=True)
    import yaml

    raw = yaml.safe_load(world.yaml_path.read_text())
    raw["sessions"][0]["post_steps"] = [
        "drizzle_push_develop2", "seed_merchant_develop2", "seed_merchant_develop2",
    ]  # fmt: skip
    world.yaml_path.write_text(yaml.safe_dump(raw))
    from orchestrator.pipeline import load_pipeline

    world.pipeline = load_pipeline(world.yaml_path)
    good = CheckResult("x", True, "ok")
    monkeypatch.setattr(flow_mod, "check_db_name", lambda repo: good)
    monkeypatch.setattr(flow_mod, "check_sst_stage", lambda repo: good)
    monkeypatch.setattr(flow_mod, "check_aws", lambda run, t: good)
    world.router.shell = {
        "service migrate": CmdResult(0, "pushed", ""),
        "seed:merchant": [
            CmdResult(0, "added 4, removed 0", ""),
            CmdResult(0, "0 added, 0 removed", ""),
        ],
    }
    yield world
    world.db.close()


async def test_post_steps_run_in_order_after_the_gates_and_the_repeat_must_be_a_noop(staged):
    out = await go(staged, only="S1")
    assert out.status == "done", out
    assert [c.split()[-1] for c in staged.router.shell_calls] == [
        "migrate",
        "seed:merchant",
        "seed:merchant",
    ]
    run = staged.db.latest_run()
    assert [e.detail for e in staged.db.events(run.id, "post_step")] == [
        "0:drizzle_push_develop2", "1:seed_merchant_develop2", "2:seed_merchant_develop2",
    ]  # fmt: skip


async def test_second_seed_that_changes_data_blocks(staged):
    staged.router.shell["seed:merchant"] = [
        CmdResult(0, "added 4", ""), CmdResult(0, "added 1, removed 0", ""),
    ]  # fmt: skip
    out = await go(staged, only="S1")
    assert out.status == "blocked" and "0 added / 0 removed" in out.reason


async def test_post_step_is_refused_when_the_stage_check_fails_and_never_runs(staged, monkeypatch):
    monkeypatch.setattr(
        flow_mod,
        "check_db_name",
        lambda repo: CheckResult("migrate-service DB_NAME", False, "DB_NAME=qinno is PRODUCTION"),
    )
    out = await go(staged, only="S1")
    assert out.status == "blocked" and "PRODUCTION" in out.reason
    assert staged.router.shell_calls == []


async def test_failed_post_step_blocks_and_resume_does_not_redo_finished_ones(staged):
    staged.router.shell["seed:merchant"] = [CmdResult(2, "", "seed failed")]
    out = await go(staged, only="S1")
    assert out.status == "blocked" and "seed_merchant_develop2 failed" in out.reason
    run = staged.db.latest_run()
    staged.router.shell["seed:merchant"] = [
        CmdResult(0, "added 4", ""),
        CmdResult(0, "0 added 0 removed", ""),
    ]
    done = await staged.flow(only="S1", retry=True).run(run.id)
    assert done.status == "done"
    assert [c.split()[-1] for c in staged.router.shell_calls].count("migrate") == 1  # push ran once
