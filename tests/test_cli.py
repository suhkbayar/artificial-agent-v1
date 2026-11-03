from pathlib import Path

from click.testing import CliRunner

from orchestrator import cli
from orchestrator.config import HOME_DIR_ENV, STATE_DB_ENV
from orchestrator.preflight import CheckResult
from orchestrator.state import StateDB

REAL = str(Path(__file__).parent.parent / "pipelines" / "qinno-rbac-v2.yaml")


def test_plan_prints_order_allowlist_and_tests():
    # --no-files equivalent: patch loader so the test does not need the qinno repo
    from orchestrator.pipeline import load_pipeline

    pipeline = load_pipeline(REAL, check_files=False)
    text = cli.render_plan(pipeline)
    for sid in ("S1", "S2", "S3", "S4", "S5", "S6"):
        assert f". {sid}   depends_on" in text
    assert text.index("1. S1") < text.index("6. S6")
    assert "packages/common/src/enums/permission/role-key.ts" in text
    assert "yarn workspace @qinno/merchant-service test" in text
    assert "handoff-s6.md" in text
    assert "TODO (" not in text


def test_preflight_command_exit_code_and_table(monkeypatch):
    results = [
        CheckResult("migrate-service DB_NAME", False, "DB_NAME='qinno' is PRODUCTION", "sync it"),
        CheckResult("claude version", True, "2.1.283"),
    ]
    monkeypatch.setattr(cli, "run_preflight", lambda *a, **k: results)
    out = CliRunner().invoke(cli.main, ["preflight", "--pipeline", REAL])
    assert out.exit_code == 1
    assert "FAIL" in out.output and "PASS" in out.output and "sync it" in out.output
    assert "do not start" in out.output

    monkeypatch.setattr(cli, "run_preflight", lambda *a, **k: results[1:])
    out = CliRunner().invoke(cli.main, ["preflight", "--pipeline", REAL])
    assert out.exit_code == 0 and "PREFLIGHT OK" in out.output


def test_plan_command_reports_invalid_pipeline(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\n")
    out = CliRunner().invoke(cli.main, ["plan", str(bad)])
    assert out.exit_code != 0 and "missing top-level key" in out.output


def test_status_without_and_with_state(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(STATE_DB_ENV, str(tmp_path / "state.db"))
    out = CliRunner().invoke(cli.main, ["status"])
    assert out.exit_code == 0 and "no runs recorded" in out.output
    with StateDB(tmp_path / "state.db") as db:
        rid = db.start_run("qinno-rbac-v2")
        step = db.ensure_step(rid, "S1", 1)
        db.update_step(step.id, status="running", claude_session_id="sess-1", add_cost_usd=1.5)
    out = CliRunner().invoke(cli.main, ["status"])
    assert out.exit_code == 0
    assert "qinno-rbac-v2" in out.output and "sess-1" in out.output
    assert "$1.50 of $100" in out.output


def test_post_steps_lists_only_known_develop2_steps():
    out = CliRunner().invoke(cli.main, ["post-steps"])
    assert out.exit_code == 0 and "seed_merchant_develop2" in out.output
    assert "production" not in out.output.replace("never", "")


def test_resume_passes_model_to_every_agent_phase(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from orchestrator.flow import RunOutcome

    monkeypatch.setenv(HOME_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(STATE_DB_ENV, str(tmp_path / "state.db"))
    with StateDB(tmp_path / "state.db") as db:
        rid = db.start_run("qinno-rbac-v2", REAL)
        db.add_event("run_start", '{"only": "S2"}', run_id=rid)
        db.finish_run(rid, "blocked")
    seen = {}

    async def fake_run(pipeline, config, deps, options, **kw):
        seen["options"] = options
        return RunOutcome(rid, "done")

    monkeypatch.setattr(cli, "run_pipeline", fake_run)
    monkeypatch.setattr(cli, "build_deps", lambda: None)
    monkeypatch.setattr(cli, "caffeinate", nullcontext)
    out = CliRunner().invoke(cli.main, ["resume", "--retry", "--model", "claude-sonnet-5-5"])
    assert out.exit_code == 0, out.output
    opts = seen["options"]
    assert (opts.model, opts.retry, opts.only) == ("claude-sonnet-5-5", True, "S2")
