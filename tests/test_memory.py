"""memory.py on a tmp COPY of the graph (fixture `mem_dir`); the real graph is never written."""

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from orchestrator.memory import (
    KEEP_BACKUPS,
    MemoryBusy,
    MemoryStore,
    all_tool_names,
    in_reconcile_window,
    parse_etime,
    write_tool_names,
)
from orchestrator.preflight import CmdResult, check_memory_health

REAL = Path("~/Documents/my-memory/memory.jsonl").expanduser()
MON = datetime(2026, 10, 26, 14, 0)  # a Monday
SUN_CRON = datetime(2026, 10, 25, 10, 0)  # a Sunday, 10:00
NO_PROCS = CmdResult(0, "  100     1    05:00 /sbin/launchd\n", "")


def _real_fingerprint():
    root = REAL.parent
    if not REAL.is_file():
        return None
    return (
        hashlib.sha256(REAL.read_bytes()).hexdigest(),
        (root / ".reconcile.lock").exists(),
        (root / "backups-orchestrator").exists(),
        sorted(p.name for p in (root / "logs").glob("pending-*"))
        if (root / "logs").is_dir()
        else [],
    )


_REAL_BEFORE = _real_fingerprint()


def store(mem_dir, *, now=MON, ps=NO_PROCS, alive=lambda pid: True):
    return MemoryStore(mem_dir, now=lambda: now, run=lambda cmd, **kw: ps, pid_alive=alive)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def put_entity(mem_dir, name, obs=("o",), etype="Decision"):
    with (mem_dir / "memory.jsonl").open("a") as fh:
        fh.write(
            json.dumps(
                {"type": "entity", "name": name, "entityType": etype, "observations": list(obs)}
            )
            + "\n"
        )


def put_conf(mem_dir, entity, note="x"):
    with (mem_dir / "confidence.jsonl").open("a") as fh:
        fh.write(
            json.dumps({"entity": entity, "conf": "V", "on": "2026-10-30", "note": note}) + "\n"
        )


# ---- read --------------------------------------------------------------------------------------


def test_open_nodes_is_exact_name_lookup(mem_dir):
    put_entity(mem_dir, "Decision: Probe", ["one", "two"])
    found, missing = store(mem_dir).open_nodes(["Decision: Probe", "Decision: Nope"])
    assert [e.name for e in found] == ["Decision: Probe"] and missing == ["Decision: Nope"]
    assert found[0].observations == ("one", "two")
    assert store(mem_dir).open_nodes(["Decision"])[0] == []  # no fuzzy matching


def test_render_context_lists_entities_and_missing_names(mem_dir):
    put_entity(mem_dir, "Decision: Probe", ["alpha"])
    text = store(mem_dir).render_context(["Decision: Probe", "Nope"])
    assert "### Decision: Probe" in text and "- alpha" in text and "not found" in text
    assert store(mem_dir).render_context([]) == ""


def test_render_context_truncates_a_huge_entity(mem_dir):
    put_entity(mem_dir, "Big", ["x" * 20000])
    text = store(mem_dir).render_context(["Big"])
    assert len(text) < 8000 and "truncated" in text


def test_the_real_graph_has_the_project_root_entity(mem_dir):
    # mem_dir is a copy of the real graph when it exists
    found, _ = store(mem_dir).open_nodes(["Agent Orchestrator"])
    assert found and found[0].entity_type


# ---- health ------------------------------------------------------------------------------------


def test_healthy_graph(mem_dir):
    h = store(mem_dir).health()
    assert h.ok and "entities readable" in h.detail


def test_missing_and_corrupt_graph_are_unhealthy(mem_dir):
    (mem_dir / "memory.jsonl").write_text("{broken\n")
    h = store(mem_dir).health()
    assert not h.ok and "unreadable" in h.detail
    (mem_dir / "memory.jsonl").unlink()
    assert not store(mem_dir).health().ok


def test_leaked_memory_server_processes_make_it_unhealthy(mem_dir):
    ps = CmdResult(
        0,
        "  200     1  02:10:00 node /x/.npm/_npx/abc/node_modules/.bin/mcp-server-memory\n"
        "  201     1  02:10:00 npm exec @modelcontextprotocol/server-memory\n"
        "  300  4000     03:00 node /x/server-memory (fresh child, not an orphan)\n",
        "",
    )
    h = store(mem_dir, ps=ps).health()
    assert not h.ok and h.detail.startswith("2 leaked") and "pid 200" in h.detail


def test_a_lock_we_did_not_create_blocks_writes_and_health(mem_dir):
    (mem_dir / ".reconcile.lock").mkdir()  # as weekly-reconcile.sh does
    s = store(mem_dir)
    assert not s.health().ok
    with pytest.raises(MemoryBusy), s.write_window():
        pass
    assert (mem_dir / ".reconcile.lock").exists()  # not ours: left alone


def test_sunday_reconcile_window(mem_dir):
    assert in_reconcile_window(SUN_CRON)
    assert in_reconcile_window(datetime(2026, 10, 25, 9, 50))
    assert not in_reconcile_window(datetime(2026, 10, 25, 11, 0))
    assert not in_reconcile_window(datetime(2026, 10, 25, 9, 49))
    assert not in_reconcile_window(datetime(2026, 10, 24, 10, 0))  # Saturday
    s = store(mem_dir, now=SUN_CRON)
    assert not s.health().ok
    with pytest.raises(MemoryBusy), s.write_window():
        pass


def test_preflight_check_reports_degraded_but_does_not_fail(mem_dir):
    ok = check_memory_health(store(mem_dir))
    assert ok.ok and "DEGRADED" not in ok.detail
    (mem_dir / "memory.jsonl").write_text("{broken\n")
    bad = check_memory_health(store(mem_dir))
    assert bad.ok and bad.detail.startswith("DEGRADED") and "pending" in bad.detail


def test_parse_etime():
    assert parse_etime("05:00") == timedelta(minutes=5)
    assert parse_etime("02:10:00") == timedelta(hours=2, minutes=10)
    assert parse_etime("3-01:00:00") == timedelta(days=3, hours=1)
    assert parse_etime("garbage") is None


# ---- write window / backup ---------------------------------------------------------------------


def test_write_window_locks_backs_up_and_releases(mem_dir):
    s = store(mem_dir)
    before = hashlib.sha256((mem_dir / "memory.jsonl").read_bytes()).hexdigest()
    with s.write_window() as backup:
        assert (mem_dir / ".reconcile.lock").is_dir()
        assert (backup / "memory.jsonl").is_file() and (backup / "confidence.jsonl").is_file()
        assert hashlib.sha256((backup / "memory.jsonl").read_bytes()).hexdigest() == before
        with pytest.raises(MemoryBusy), s.write_window():  # the window is exclusive
            pass
    assert not (mem_dir / ".reconcile.lock").exists()
    assert backup.parent.name == "backups-orchestrator"  # never the reconcile job's backups/


def test_lock_is_released_when_the_body_raises(mem_dir):
    with pytest.raises(RuntimeError), store(mem_dir).write_window():
        raise RuntimeError("boom")
    assert not (mem_dir / ".reconcile.lock").exists()


def test_a_dead_orchestrators_leftover_lock_is_cleaned_up(mem_dir):
    lock = mem_dir / ".reconcile.lock"
    lock.mkdir()
    (lock / "orchestrator.pid").write_text("999999")
    with store(mem_dir, alive=lambda pid: False).write_window():
        assert lock.is_dir()
    assert not lock.exists()


def test_a_live_orchestrators_lock_is_respected(mem_dir):
    lock = mem_dir / ".reconcile.lock"
    lock.mkdir()
    (lock / "orchestrator.pid").write_text(str(os.getpid()))
    with pytest.raises(MemoryBusy), store(mem_dir, alive=lambda pid: True).write_window():
        pass


def test_only_the_last_backups_are_kept(mem_dir):
    for i in range(KEEP_BACKUPS + 4):
        store(mem_dir, now=MON + timedelta(minutes=i)).backup()
    assert len(list((mem_dir / "backups-orchestrator").iterdir())) == KEEP_BACKUPS
    assert not (mem_dir / "backups").exists()


# ---- audit -------------------------------------------------------------------------------------


def test_audit_clean_run(mem_dir):
    s = store(mem_dir)
    a = s.audit(s.state(), s.state())
    assert not a.wrote and a.problems() == []


def test_new_entity_with_its_confidence_row_is_fine(mem_dir):
    s = store(mem_dir)
    before = s.state()
    put_entity(mem_dir, "Decision: Agent Orchestrator — probe")
    put_conf(mem_dir, "Decision: Agent Orchestrator — probe")
    a = s.audit(before, s.state())
    assert a.added == ("Decision: Agent Orchestrator — probe",) and a.problems() == []


def test_entity_change_without_a_confidence_row_update_is_flagged(mem_dir):
    s = store(mem_dir)
    put_entity(mem_dir, "Decision: Probe", ["v1"])
    put_conf(mem_dir, "Decision: Probe", "v1")
    before = s.state()
    text = (mem_dir / "memory.jsonl").read_text().replace('"v1"', '"v1","v2"')
    (mem_dir / "memory.jsonl").write_text(text)
    a = s.audit(before, s.state())
    assert a.changed == ("Decision: Probe",)
    assert a.confidence_missing == ("Decision: Probe",)
    assert any("confidence.jsonl row not updated" in p for p in a.problems())


def test_removed_and_protected_entities_are_ask_first(mem_dir):
    s = store(mem_dir)
    put_entity(mem_dir, "Playbook", ["p"])
    before = s.state()
    lines = [line for line in (mem_dir / "memory.jsonl").read_text().splitlines()
             if json.loads(line).get("name") != "Playbook"]  # fmt: skip
    (mem_dir / "memory.jsonl").write_text("\n".join(lines) + "\n")
    a = s.audit(before, s.state())
    assert a.removed == ("Playbook",) and a.ask_first_violation
    assert "Playbook" in a.protected_touched


def test_bad_confidence_line_is_reported(mem_dir):
    s = store(mem_dir)
    before = s.state()
    with (mem_dir / "confidence.jsonl").open("a") as fh:
        fh.write("{not json\n")
    assert s.audit(before, s.state()).bad_confidence_rows


# ---- pending / tool sets -----------------------------------------------------------------------


def test_pending_file_goes_to_logs(mem_dir):
    p = store(mem_dir).write_pending("S1: delete entity X?", "- reason")
    assert p.parent == mem_dir / "logs" and p.name.startswith("pending-2026-10-26-")
    assert p.read_text().startswith("# S1: delete entity X?")


def test_tool_name_sets():
    assert "mcp__memory__create_entities" in write_tool_names()
    assert "mcp__memory__open_nodes" not in write_tool_names()
    assert "mcp__memory__read_graph" in all_tool_names()


def test_real_memory_dir_was_not_touched_by_this_module():
    """Guard for the whole file: every test above used tmp copies (fixture `mem_dir`)."""
    assert _real_fingerprint() == _REAL_BEFORE
