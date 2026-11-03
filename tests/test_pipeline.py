from pathlib import Path

import pytest

from orchestrator.pipeline import PipelineError, load_pipeline

REAL = Path(__file__).parent.parent / "pipelines" / "qinno-rbac-v2.yaml"


def write(tmp_path, body: str, files=("a.md", "b.md")):
    plan = tmp_path / "plan"
    plan.mkdir(exist_ok=True)
    for f in files:
        (plan / f).write_text("x")
    p = tmp_path / "p.yaml"
    p.write_text(f"name: t\nrepo: {tmp_path}\nplan_dir: plan\n{body}")
    return p


SESSIONS = """sessions:
  - id: S1
    claude_task: a.md
    codex_task: null
    handoff: handoff-s1.md
  - id: S2
    claude_task: b.md
    codex_task: null
    handoff: handoff-s2.md
    depends_on: [S1]
"""


def test_loads_and_orders(tmp_path):
    p = load_pipeline(write(tmp_path, SESSIONS))
    assert [s.id for s in p.order()] == ["S1", "S2"]
    assert p.budget.total == 100.0  # default (O12) when the yaml omits budget_usd


def test_order_respects_depends_on_not_declaration(tmp_path):
    body = """sessions:
  - {id: B, claude_task: a.md, codex_task: null, handoff: h, depends_on: [A]}
  - {id: A, claude_task: b.md, codex_task: null, handoff: h}
"""
    assert [s.id for s in load_pipeline(write(tmp_path, body)).order()] == ["A", "B"]


def test_cycle_is_an_error(tmp_path):
    body = """sessions:
  - {id: A, claude_task: a.md, codex_task: null, handoff: h, depends_on: [B]}
  - {id: B, claude_task: b.md, codex_task: null, handoff: h, depends_on: [A]}
"""
    with pytest.raises(PipelineError, match="cycle"):
        load_pipeline(write(tmp_path, body))


def test_duplicate_id_unknown_dep_and_unknown_post_step(tmp_path):
    body = """sessions:
  - {id: A, claude_task: a.md, codex_task: null, handoff: h, depends_on: [Z], post_steps: [nope]}
  - {id: A, claude_task: b.md, codex_task: null, handoff: h}
"""
    with pytest.raises(PipelineError) as exc:
        load_pipeline(write(tmp_path, body))
    msg = str(exc.value)
    assert "duplicate session id" in msg
    assert "unknown session `Z`" in msg
    assert "unknown post_step `nope`" in msg


def test_missing_task_files_are_all_listed(tmp_path):
    with pytest.raises(PipelineError) as exc:
        load_pipeline(write(tmp_path, SESSIONS, files=()))
    assert "task file missing: a.md" in str(exc.value)
    assert "task file missing: b.md" in str(exc.value)
    load_pipeline(write(tmp_path, SESSIONS, files=()), check_files=False)


def test_missing_plan_dir(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(f"name: t\nrepo: {tmp_path}\nplan_dir: nope\n{SESSIONS}")
    with pytest.raises(PipelineError, match="plan_dir not found"):
        load_pipeline(p)


def test_allowlist_without_codex_task_rejected(tmp_path):
    body = """sessions:
  - {id: A, claude_task: a.md, codex_task: null, handoff: h, codex_allowlist: ["x/**"]}
"""
    with pytest.raises(PipelineError, match="codex_allowlist set but codex_task is null"):
        load_pipeline(write(tmp_path, body))


def test_bad_budget_and_missing_keys(tmp_path):
    with pytest.raises(PipelineError, match="budget_usd.per_call"):
        load_pipeline(write(tmp_path, "budget_usd: {per_call: '?'}\n" + SESSIONS))
    p = tmp_path / "q.yaml"
    p.write_text("name: x\n")
    with pytest.raises(PipelineError, match="missing top-level key: repo"):
        load_pipeline(p)
    with pytest.raises(PipelineError, match="not found"):
        load_pipeline(tmp_path / "absent.yaml")


def test_todos_are_reported(tmp_path):
    body = """sessions:
  - {id: A, claude_task: a.md, codex_task: null, handoff: "TODO later", tests: ["TODO cmd"]}
"""
    todos = load_pipeline(write(tmp_path, body)).todos()
    assert todos == ["A.handoff: TODO later", "A.tests: TODO cmd"]


def test_shipped_qinno_pipeline_parses():
    p = load_pipeline(REAL, check_files=False)
    assert [s.id for s in p.order()] == ["S1", "S2", "S3", "S4", "S5", "S6"]
    assert (p.budget.per_call, p.budget.per_session, p.budget.total) == (4.0, 15.0, 100.0)
    # Every session names its Codex task and a non-empty allowlist and test list.
    for s in p.sessions:
        assert s.codex_task and s.codex_allowlist and s.tests, s.id
    assert p.session("S1").post_steps[0] == "drizzle_push_develop2"
    # No test command may hide its exit code behind a pipe to tail.
    assert not [t for s in p.sessions for t in s.tests if "| tail" in t]


def test_shipped_qinno_pipeline_matches_real_plan_dir_when_present():
    p = load_pipeline(REAL, check_files=False)
    if not p.plan_path.is_dir():
        pytest.skip("qinno repo not present on this machine")
    load_pipeline(REAL, check_files=True)
