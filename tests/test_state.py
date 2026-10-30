import sqlite3

import pytest

from orchestrator.state import SCHEMA_VERSION, StateDB


@pytest.fixture
def db(tmp_path):
    with StateDB(tmp_path / "state.db") as d:
        yield d


def test_schema_version_and_tables(db):
    assert db.schema_version == SCHEMA_VERSION
    names = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "steps", "decisions", "events"} <= names


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        StateDB(path)


def test_reopen_keeps_data(tmp_path):
    path = tmp_path / "state.db"
    with StateDB(path) as d:
        rid = d.start_run("p")
    with StateDB(path) as d:
        assert d.get_run(rid).pipeline == "p"


def test_step_lifecycle_and_cost_rollup(db):
    rid = db.start_run("p")
    step = db.ensure_step(rid, "S1", 1)
    assert step.status == "pending" and step.attempts == 0
    again = db.ensure_step(rid, "S1", 1)
    assert again.id == step.id  # idempotent

    s = db.update_step(
        step.id,
        status="running",
        claude_session_id="abc",
        increment_attempts=True,
        add_cost_usd=0.5,
    )
    assert (s.status, s.claude_session_id, s.attempts, s.cost_usd) == ("running", "abc", 1, 0.5)
    assert s.started_at is not None
    s = db.update_step(step.id, add_cost_usd=0.25, last_error="boom")
    assert s.cost_usd == 0.75 and s.last_error == "boom"
    assert db.get_run(rid).total_cost_usd == 0.75
    assert db.session_cost(rid, "S1") == 0.75


def test_last_unfinished_step_for_resume(db):
    rid = db.start_run("p")
    a = db.ensure_step(rid, "S1", 1)
    b = db.ensure_step(rid, "S1", 2)
    db.update_step(a.id, status="done")
    assert db.last_unfinished_step(rid).id == b.id
    db.update_step(b.id, status="done")
    assert db.last_unfinished_step(rid) is None
    c = db.ensure_step(rid, "S2", 1)
    db.update_step(c.id, status="quota_wait")
    assert db.last_unfinished_step(rid).id == c.id


def test_blocked_step_is_not_resumable_automatically(db):
    rid = db.start_run("p")
    a = db.ensure_step(rid, "S1", 1)
    db.update_step(a.id, status="blocked", last_error="hard stop")
    assert db.last_unfinished_step(rid) is None


def test_events_and_decisions(db):
    rid = db.start_run("p")
    db.add_event("blocker", "prod db", run_id=rid, session_id="S1")
    db.add_event("notice", "hi")
    assert [e.kind for e in db.events(rid)] == ["blocker"]
    assert len(db.events()) == 2
    assert [e.detail for e in db.events(kind="notice")] == ["hi"]
    db.add_decision(rid, "S1", "q", "a", "because", options="a|b", files="x.ts")
    assert db.decision_count(rid) == 1


def test_latest_run_filter(db):
    db.start_run("a")
    second = db.start_run("b")
    assert db.latest_run().id == second
    assert db.latest_run("a").pipeline == "a"
    assert db.latest_run("zzz") is None
    db.finish_run(second, "done")
    assert db.get_run(second).status == "done"


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.ensure_step(999, "S1", 1)
