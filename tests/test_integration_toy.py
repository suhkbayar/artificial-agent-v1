"""Toy-repo integration: runner -> gates -> state.db on a real throwaway git repo.

Two layers:
  * `test_*` without a marker use a scripted SDK and run on every `pytest`.
  * `@pytest.mark.integration` tests call the REAL Agent SDK on the toy repo only. They cost money
    and need credentials, so they run only with ORCHESTRATOR_INTEGRATION=1. qinno is never touched.
"""

import os
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

from orchestrator.config import Budget
from orchestrator.gates import (
    Attempt,
    breaker_tripped,
    changed_files,
    evaluate,
    take_snapshot,
)
from orchestrator.pipeline import SessionSpec
from orchestrator.policy import PolicyContext
from orchestrator.runner import StepRequest, StepStatus, run_step
from orchestrator.state import StateDB
from tests.test_runner import Fake, assistant, result

TOY_BUDGET = Budget(per_call=0.5, per_session=1.5, total=3.0)
SESSION = SessionSpec(
    id="S1", claude_task="TASK.md", codex_task=None, handoff="handoff.md",
    tests=("python3 test_calc.py",),
)  # fmt: skip
FIXED_CALC = (
    "def add(a, b):\n    return a + b\n\n\n"
    "def divide(a, b):\n    if b == 0:\n        raise ValueError('division by zero')\n"
    "    return a / b\n"
)


@pytest.fixture
def world(toy_repo, tmp_path):
    db = StateDB(tmp_path / "state.db")
    run_id = db.start_run("toy")
    step = db.ensure_step(run_id, "S1", 3)
    req = StepRequest(
        prompt=(toy_repo / "TASK.md").read_text(),
        cwd=toy_repo,
        policy=PolicyContext(repo=toy_repo),
        timeout_s=300,
        setting_sources=[],  # isolate from the user's hooks/CLAUDE.md/memory on real runs
        strict_mcp_config=True,
    )
    yield db, run_id, step.id, req, toy_repo, tmp_path / "checkpoints"
    db.close()


def edit_calc_and_write_handoff(repo: Path):
    def act(hook, options):
        (repo / "calc.py").write_text(FIXED_CALC)
        (repo / "handoff.md").write_text("STATUS: DONE\nfixed divide\n")

    return act


# ---- scripted SDK ----------------------------------------------------------------------------


async def test_one_step_done_then_gates_pass_and_everything_is_in_state_db(world):
    db, run_id, step_id, req, repo, cp = world
    before = take_snapshot(repo, cp / "before", "before")
    res = await run_step(
        req, db=db, step_id=step_id, budget=TOY_BUDGET,
        query_fn=Fake([assistant(), edit_calc_and_write_handoff(repo), result(cost=0.07)]),
    )  # fmt: skip
    assert res.status is StepStatus.DONE

    after = take_snapshot(repo, cp / "after", "after")
    assert changed_files(before, after) == ["calc.py", "handoff.md"]
    verdict = evaluate(session=SESSION, repo=repo, handoff=repo / "handoff.md", checkpoint=after)
    assert verdict.passed, verdict.failures
    db.update_step(step_id, checkpoint_path=str(after.directory))

    step = db.get_step(step_id)
    assert (step.status, step.cost_usd, step.claude_session_id) == ("done", 0.07, "sess-1")
    assert step.checkpoint_path == str(after.directory)
    assert db.get_run(run_id).total_cost_usd == 0.07


async def test_gates_fail_on_the_unfixed_toy_repo(world):
    db, run_id, step_id, req, repo, cp = world
    (repo / "handoff.md").write_text("STATUS: DONE\n")
    snap = take_snapshot(repo, cp / "s", "s")
    verdict = evaluate(session=SESSION, repo=repo, handoff=repo / "handoff.md", checkpoint=snap)
    assert not verdict.passed
    assert any("python3 test_calc.py" in f for f in verdict.failures)


async def test_forbidden_command_is_denied_and_logged_without_failing_the_step(world):
    db, run_id, step_id, req, repo, cp = world

    async def try_push(hook, options):
        out = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}, "t1", None
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    res = await run_step(
        req, db=db, step_id=step_id, budget=TOY_BUDGET, query_fn=Fake([try_push, result()])
    )
    assert res.status is StepStatus.DONE
    denies = db.events(run_id, kind="policy_deny")
    assert len(denies) == 1 and "git push" in denies[0].detail


async def test_hard_stop_is_blocked_in_state_db_with_a_blocker_event(world):
    db, run_id, step_id, req, repo, cp = world

    async def prod(hook, options):
        out = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "yarn migrate:production"}}, "t1", None
        )
        assert out["continue_"] is False

    res = await run_step(
        req, db=db, step_id=step_id, budget=TOY_BUDGET, query_fn=Fake([prod, result()])
    )
    assert res.status is StepStatus.BLOCKED
    step = db.get_step(step_id)
    assert step.status == "blocked" and "production" in step.last_error
    assert [e.kind for e in db.events(run_id, kind="blocker")] != []
    assert db.last_unfinished_step(run_id) is None  # blocked needs a human, not an auto-resume


async def test_three_empty_attempts_trip_the_circuit_breaker_in_the_gate(world):
    db, run_id, step_id, req, repo, cp = world
    attempts: list[Attempt] = []
    for _ in range(3):
        a = take_snapshot(repo, cp / f"a{len(attempts)}", "a")
        await run_step(
            req, db=db, step_id=step_id, budget=TOY_BUDGET,
            query_fn=Fake([assistant("thinking"), result(cost=0.01)]),
        )  # fmt: skip
        b = take_snapshot(repo, cp / f"b{len(attempts)}", "b")
        attempts.append(Attempt(files_changed=len(changed_files(a, b))))
    assert breaker_tripped(attempts)
    (repo / "handoff.md").write_text("STATUS: DONE\n")
    snap = take_snapshot(repo, cp / "final", "final")
    verdict = evaluate(
        session=SESSION, repo=repo, handoff=repo / "handoff.md", checkpoint=snap, attempts=attempts
    )
    assert any("circuit breaker" in f for f in verdict.failures)
    db.add_event("blocker", verdict.failures[0], run_id=run_id, session_id="S1")
    assert db.get_step(step_id).attempts == 3


# ---- real SDK (opt-in) -----------------------------------------------------------------------

real = pytest.mark.skipif(
    os.environ.get("ORCHESTRATOR_INTEGRATION") != "1",
    reason="real SDK calls cost money; set ORCHESTRATOR_INTEGRATION=1 to run",
)


@real
@pytest.mark.integration
async def test_real_sdk_step_fixes_the_toy_repo(world):
    db, run_id, step_id, req, repo, cp = world
    res = await run_step(req, db=db, step_id=step_id, budget=TOY_BUDGET)
    print("REAL STEP:", res)
    assert res.status is StepStatus.DONE, res.reason
    assert res.session_id and res.cost_usd > 0
    snap = take_snapshot(repo, cp / "real", "real")
    verdict = evaluate(session=SESSION, repo=repo, handoff=repo / "handoff.md", checkpoint=snap)
    assert verdict.passed, verdict.failures
    assert db.get_step(step_id).claude_session_id == res.session_id


@real
@pytest.mark.integration
async def test_real_sdk_forbidden_command_is_denied(world):
    db, run_id, step_id, req, repo, cp = world
    req.prompt = (
        "This is a sandbox policy test. Use the Bash tool to run exactly: git push origin main\n"
        "If it is denied, do not try anything else: reply with the denial message and stop."
    )
    res = await run_step(req, db=db, step_id=step_id, budget=TOY_BUDGET)
    print("REAL STEP:", res, [e.detail for e in db.events(run_id, kind="policy_deny")])
    denies = db.events(run_id, kind="policy_deny")
    assert denies and "git push" in denies[0].detail
    assert res.status in (StepStatus.DONE, StepStatus.PERMISSION_DENIED)


@real
@pytest.mark.integration
async def test_real_sdk_hard_stop_is_blocked(world):
    db, run_id, step_id, req, repo, cp = world
    req.prompt = (
        "This is a sandbox policy test. Use the Bash tool to run exactly: yarn migrate:production"
    )
    res = await run_step(req, db=db, step_id=step_id, budget=TOY_BUDGET)
    print("REAL STEP:", res)
    assert res.status is StepStatus.BLOCKED, res.reason
    assert db.get_step(step_id).status == "blocked"
    assert [e.kind for e in db.events(run_id, kind="blocker")] != []


def test_resultmessage_import_sanity():
    assert ResultMessage is not None
