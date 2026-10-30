"""`orchestrator run / run --only / status / resume` end to end on the toy repo (scripted SDK)."""

import pytest
from claude_agent_sdk import ProcessError
from click.testing import CliRunner

from orchestrator import cli
from orchestrator.config import load_config
from orchestrator.envfile import SCRUB_SUBPROCESS_ENV
from orchestrator.state import StateDB
from tests.flowkit import build_world
from tests.test_secrets import FAKE_KEY, dump_db


@pytest.fixture
def w(toy_repo, tmp_path, mem_dir, monkeypatch):
    world = build_world(toy_repo, tmp_path, mem_dir)
    world.db.close()  # the CLI opens its own connection from the same file
    monkeypatch.setenv("ORCHESTRATOR_STATE_DB", str(world.config.state_db))
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "config.toml").write_text(
        f'checkpoint_dir = "{world.config.checkpoint_dir}"\nlog_dir = "{world.config.log_dir}"\n'
    )
    deps = world.deps
    monkeypatch.setattr(cli, "build_deps", lambda: deps)  # scripted SDK, faked codex, no preflight
    monkeypatch.setattr(cli, "caffeinate", cli.caffeinate)  # the real one: must not break a test
    return world


def invoke(*args):
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)


def test_run_only_s1_prints_a_summary_and_exits_zero(w):
    res = invoke("run", str(w.yaml_path), "--only", "S1")
    assert res.exit_code == 0, res.output
    assert "run 1: done" in res.output and "S1: done" in res.output and "S2" not in res.output
    assert w.agent.phases() == ["plan", "contract", "claude", "verify"]


def test_run_all_sessions(w):
    res = invoke("run", str(w.yaml_path))
    assert res.exit_code == 0 and "S1: done" in res.output and "S2: done" in res.output


def test_blocked_run_exits_two_and_status_shows_why(w):
    w.agent.handlers["verify"] = lambda a, c: ProcessError("crash", exit_code=1, stderr="")
    res = invoke("run", str(w.yaml_path), "--only", "S1")
    assert res.exit_code == 2 and "S1: failed" in res.output
    status = invoke("status")
    assert status.exit_code == 0
    assert "status=failed" in status.output
    assert "S1" in status.output and "failed" in status.output
    assert "cost: $0.15" in status.output  # plan + contract + claude; the crashed call reports none
    assert "sess-" in status.output  # the Claude session id, for `claude --resume`


def test_failed_preflight_exits_one_and_starts_nothing(w):
    w.deps.preflight = lambda p: [
        __import__("orchestrator.preflight", fromlist=["CheckResult"]).CheckResult(
            "DB_NAME", False, "production"
        )
    ]
    res = invoke("run", str(w.yaml_path), "--only", "S1")
    assert res.exit_code == 1 and "PREFLIGHT FAILED" in res.output + (res.stderr or "")
    assert w.agent.calls == []


def test_resume_continues_from_the_last_unfinished_step(w):
    w.agent.handlers["verify"] = lambda a, c: ProcessError("crash", exit_code=1, stderr="")
    assert invoke("run", str(w.yaml_path), "--only", "S1").exit_code == 2
    calls = list(w.agent.phases())
    w.agent.handlers.pop("verify")

    stuck = invoke("resume")  # failed = needs a human
    assert stuck.exit_code == 2 and "resume --retry" in stuck.output
    assert w.agent.phases() == calls

    res = invoke("resume", "--retry")
    assert res.exit_code == 0 and "done" in res.output
    assert w.agent.phases() == [*calls, "verify"]  # nothing earlier was redone
    assert "already done" in invoke("resume").output


def test_resume_remembers_the_only_flag(w):
    w.agent.handlers["verify"] = lambda a, c: ProcessError("crash", exit_code=1, stderr="")
    invoke("run", str(w.yaml_path), "--only", "S1")
    w.agent.handlers.pop("verify")
    invoke("resume", "--retry")
    assert "S2" not in " ".join(w.agent.phases("S2"))  # S2 never started


def test_resume_without_runs_is_a_clear_error(w):
    res = CliRunner().invoke(cli.main, ["resume"])
    assert res.exit_code != 0 and "no runs recorded" in res.output


def test_status_without_state_db(w):
    (w.config.state_db).unlink()
    assert "state.db does not exist" in invoke("status").output


def test_build_deps_loads_the_key_from_the_project_root_only(tmp_path, monkeypatch):
    root = tmp_path / "project-root"
    (root / ".env.local").write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\nOTHER=1\n")
    monkeypatch.undo()  # drop the autouse isolation so build_deps is the real one...
    monkeypatch.setenv("ORCHESTRATOR_PROJECT_ROOT", str(root))
    monkeypatch.setenv("ORCHESTRATOR_MEMORY_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_SCRUB_SUBPROCESS_ENV", raising=False)
    deps = cli.build_deps()
    assert deps.sdk_env == {"ANTHROPIC_API_KEY": FAKE_KEY, SCRUB_SUBPROCESS_ENV: "1"}
    import os

    assert "ANTHROPIC_API_KEY" not in os.environ
    assert FAKE_KEY not in repr(deps)


def test_build_deps_without_env_file_uses_the_claude_login(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_PROJECT_ROOT", str(tmp_path / "nothing-here"))
    assert cli.build_deps().sdk_env == {}


def test_the_key_never_lands_in_state_db_through_the_cli(w):
    w.deps.sdk_env = {"ANTHROPIC_API_KEY": FAKE_KEY}
    w.agent.handlers["plan"] = lambda a, c: (
        "plan " + FAKE_KEY + "\n```json\n"
        '{"files": ["calc.py"], "commands": [], "risky": false, "risks": []}\n```'
    )
    assert invoke("run", str(w.yaml_path), "--only", "S1").exit_code == 0
    assert FAKE_KEY not in dump_db(load_config().state_db)
    assert StateDB(load_config().state_db).latest_run().status == "done"
