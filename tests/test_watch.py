"""`orchestrator watch`: read-only snapshot, redaction, loopback-only server, the OK button."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from orchestrator.state import USER_OK, USER_WAIT, StateDB
from orchestrator.watch import TEXT_LIMIT, make_server, snapshot

FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE"
JSON = "application/json"


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    with StateDB(path) as db:
        rid = db.start_run("p", "pipelines/p.yaml")
        step = db.ensure_step(rid, "S1", 1)
        db.update_step(step.id, status="blocked", last_error=f"boom {FAKE_KEY}")
        db.add_event("policy_deny", f"Bash x {FAKE_KEY}", run_id=rid, session_id="S1")
        db.add_event("notice", "x" * (TEXT_LIMIT * 2), run_id=rid, session_id="S1")
    return path


def test_snapshot_without_db(tmp_path: Path) -> None:
    assert snapshot(tmp_path / "nope.db")["run"] is None


def test_snapshot_redacts_and_clips(tmp_path: Path) -> None:
    snap = snapshot(_db(tmp_path))
    assert snap["run"]["pipeline"] == "p"
    assert snap["steps"][0]["status"] == "blocked"
    assert FAKE_KEY not in json.dumps(snap)
    assert all(len(e["detail"]) <= TEXT_LIMIT + 1 for e in snap["events"])


def test_snapshot_does_not_write(tmp_path: Path) -> None:
    path = _db(tmp_path)
    before = path.read_bytes()
    snapshot(path)
    assert path.read_bytes() == before


def test_server_is_loopback_and_serves(tmp_path: Path) -> None:
    server = make_server(_db(tmp_path), 0)
    try:
        assert server.server_address[0] == "127.0.0.1"
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{base}/") as r:
            assert b"Orchestrator watch" in r.read()
        with urllib.request.urlopen(f"{base}/api/snapshot") as r:
            assert json.load(r)["run"]["pipeline"] == "p"
    finally:
        server.shutdown()
        server.server_close()


# ---- O14: the OK button for user-run post steps ------------------------------------------------


def _waiting_db(tmp_path: Path) -> tuple[Path, int]:
    path = _db(tmp_path)
    with StateDB(path) as db:
        rid = db.latest_run().id
        script = f"cd /repo\nyarn workspace @qinno/migrate-service seed:merchant # {FAKE_KEY}"
        detail = json.dumps({"keys": ["0:seed_merchant_develop2"], "script": script})
        wait_id = db.add_event(USER_WAIT, detail, run_id=rid, session_id="S1")
    return path, wait_id


def _post(base: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict | bytes]:
    req = urllib.request.Request(f"{base}/api/ok", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as err:
        raw = err.read()
        return err.code, (json.loads(raw) if err.headers.get_content_type() == JSON else raw)


def test_snapshot_lists_the_open_wait_unclipped_and_redacted(tmp_path: Path) -> None:
    path, wait_id = _waiting_db(tmp_path)
    [wait] = snapshot(path)["waiting"]
    assert wait["id"] == wait_id and wait["session_id"] == "S1"
    assert "seed:merchant" in wait["script"] and FAKE_KEY not in wait["script"]


def test_ok_button_records_the_ok_once_and_rejects_foreign_requests(tmp_path: Path) -> None:
    path, wait_id = _waiting_db(tmp_path)
    server = make_server(path, 0)
    try:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        body = json.dumps({"wait_id": wait_id}).encode()
        own = {"Content-Type": JSON, "Origin": base}
        assert _post(base, body, {**own, "Origin": "http://evil.test"})[0] == 403
        assert _post(base, body, {**own, "Content-Type": "text/plain"})[0] == 403
        assert _post(base, b"{}", own)[0] == 400
        assert snapshot(path)["waiting"]  # nothing recorded by the refused requests
        assert _post(base, body, own) == (200, {"ok": True, "error": None})
        code, again = _post(base, body, own)
        assert code == 409 and "already OK" in again["error"]
        assert _post(base, json.dumps({"wait_id": 999}).encode(), own)[0] == 409
        assert snapshot(path)["waiting"] == []
        with StateDB(path) as db:
            assert [e.detail for e in db.events(kind=USER_OK)] == [str(wait_id)]
    finally:
        server.shutdown()
        server.server_close()
