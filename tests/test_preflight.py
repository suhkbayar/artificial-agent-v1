from pathlib import Path

from orchestrator import preflight as pf
from orchestrator.config import load_config
from orchestrator.pipeline import load_pipeline
from orchestrator.preflight import CmdResult


def fake(table):
    """Runner keyed on the command's first two tokens; records calls."""
    calls = []

    def run(cmd, *, timeout, cwd=None, env=None):
        calls.append(list(cmd))
        for prefix, result in table.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        raise AssertionError(f"unexpected command {cmd}")

    run.calls = calls
    return run


def ok(out="", rc=0):
    return CmdResult(rc, out, "")


# -- DB_NAME ---------------------------------------------------------------------------------


def env_repo(tmp_path, content):
    d = tmp_path / "services" / "migrate-service"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".env").write_text(content)
    return tmp_path


def test_db_name_develop2_passes(tmp_path):
    assert pf.check_db_name(env_repo(tmp_path, "X=1\nDB_NAME=qinno_develop2\n")).ok


def test_db_name_production_fails_with_fix_hint(tmp_path):
    r = pf.check_db_name(env_repo(tmp_path, "DB_NAME=qinno\n"))
    assert not r.ok and "PRODUCTION" in r.detail
    assert "sync-secrets -- --stage=develop2" in r.fix_hint


def test_db_name_quoted_other_and_missing(tmp_path):
    assert pf.check_db_name(env_repo(tmp_path, 'DB_NAME="qinno_develop2"\n')).ok
    assert not pf.check_db_name(env_repo(tmp_path, "DB_NAME=other\n")).ok
    assert not pf.check_db_name(env_repo(tmp_path / "x", "NOPE=1\n")).ok
    assert not pf.check_db_name(tmp_path / "does-not-exist").ok


def test_db_name_check_never_writes(tmp_path):
    repo = env_repo(tmp_path, "DB_NAME=qinno\n")
    f = repo / "services" / "migrate-service" / ".env"
    before = (f.read_text(), f.stat().st_mtime_ns)
    pf.check_db_name(repo)
    assert (f.read_text(), f.stat().st_mtime_ns) == before


# -- stage -----------------------------------------------------------------------------------


def test_sst_stage(tmp_path):
    (tmp_path / ".sst").mkdir()
    (tmp_path / ".sst" / "stage").write_text("develop2\n")
    assert pf.check_sst_stage(tmp_path).ok
    (tmp_path / ".sst" / "stage").write_text("production")
    assert not pf.check_sst_stage(tmp_path).ok
    assert not pf.check_sst_stage(tmp_path / "nope").ok


# -- AWS -------------------------------------------------------------------------------------

LIST_STATIC = """      Name                    Value             Type    Location
      ----                    -----             ----    --------
   profile                <not set>             None    None
access_key     ****************ABCD shared-credentials-file
secret_key     ****************WXYZ shared-credentials-file
"""


def test_aws_static_key_ok():
    run = fake({("aws", "configure"): ok(LIST_STATIC), ("aws", "sts"): ok("{}")})
    assert pf.check_aws(run, 5).ok


def test_aws_sso_rejected_before_calling_sts():
    sso = LIST_STATIC.replace("shared-credentials-file", "sso")
    run = fake({("aws", "configure"): ok(sso)})
    r = pf.check_aws(run, 5)
    assert not r.ok and "static" in r.detail
    assert ["aws", "sts", "get-caller-identity"] not in run.calls


def test_aws_no_key_sts_failure_and_missing_cli():
    nokey = "access_key     <not set>      None    None\n"
    assert not pf.check_aws(fake({("aws", "configure"): ok(nokey)}), 5).ok
    bad = fake({("aws", "configure"): ok(LIST_STATIC), ("aws", "sts"): ok("denied", rc=255)})
    assert not pf.check_aws(bad, 5).ok
    missing = CmdResult(127, "", "nf", not_found=True)
    assert "not found" in pf.check_aws(fake({("aws", "configure"): missing}), 5).detail


# -- codex / claude --------------------------------------------------------------------------


def test_codex_ready():
    key = ("codex", "exec")
    assert pf.check_codex_ready(fake({key: ok("ok")}), 5).ok
    assert not pf.check_codex_ready(fake({key: ok("nope")}), 5).ok
    assert not pf.check_codex_ready(fake({key: ok("ok", rc=1)}), 5).ok
    assert (
        "timed out"
        in pf.check_codex_ready(fake({key: CmdResult(-1, "", "", timed_out=True)}), 5).detail
    )
    assert not pf.check_codex_ready(fake({key: CmdResult(127, "", "", not_found=True)}), 5).ok


def test_codex_exec_uses_read_only_sandbox():
    run = fake({("codex", "exec"): ok("ok")})
    pf.check_codex_ready(run, 5)
    assert run.calls[0] == ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "Reply ok"]


def test_claude_version_threshold():
    key = ("claude", "--version")
    assert pf.check_claude_version(fake({key: ok("2.1.283 (Claude Code)")}), 5).ok
    assert pf.check_claude_version(fake({key: ok("2.1.259 (Claude Code)")}), 5).ok
    assert not pf.check_claude_version(fake({key: ok("2.1.258 (Claude Code)")}), 5).ok
    assert pf.check_claude_version(fake({key: ok("2.10.0")}), 5).ok  # numeric, not string, compare
    assert not pf.check_claude_version(fake({key: ok("garbage")}), 5).ok


def test_codex_version_is_informational():
    key = ("codex", "--version")
    r = pf.check_codex_version(fake({key: ok("codex-cli 0.147.0")}), 5)
    assert r.ok and "differs" not in r.detail
    r = pf.check_codex_version(fake({key: ok("codex-cli 0.200.1")}), 5)
    assert r.ok and "0.147.0" in r.detail


# -- git / power / memory --------------------------------------------------------------------


def test_git_state_reports_dirty_without_failing(tmp_path):
    run = fake(
        {
            ("git", "rev-parse", "--short"): ok("abc1234\n"),
            ("git", "rev-parse", "--abbrev-ref"): ok("main\n"),
            ("git", "status"): ok(" M a.ts\n?? b.ts\n"),
        }
    )
    r = pf.check_git_state(tmp_path, run, 5)
    assert r.ok and "main@abc1234" in r.detail and "dirty (2 paths)" in r.detail
    assert not any(c[1] in ("commit", "add", "checkout") for c in run.calls)


def test_git_state_fails_outside_a_repo(tmp_path):
    run = fake({("git", "rev-parse", "--short"): ok("fatal: not a git repository", rc=128)})
    assert not pf.check_git_state(tmp_path, run, 5).ok


def test_power(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda _: "/usr/bin/caffeinate")
    ac = "Now drawing from 'AC Power'\n -InternalBattery-0 100%"
    batt = "Now drawing from 'Battery Power'\n"
    assert pf.check_power(fake({("pmset",): ok(ac)}), 5).ok
    assert not pf.check_power(fake({("pmset",): ok(batt)}), 5).ok
    monkeypatch.setattr(pf.shutil, "which", lambda _: None)
    assert not pf.check_power(fake({}), 5).ok


def test_memory_health_stub_starts_nothing():
    assert pf.check_memory_health().ok


# -- run_command / orchestration -------------------------------------------------------------


def test_run_command_timeout_kills_group_and_reports():
    r = pf.run_command(["sleep", "30"], timeout=1)
    assert r.timed_out and r.returncode == -1


def test_run_command_missing_binary_and_stdout():
    assert pf.run_command(["definitely-not-a-binary-xyz"], timeout=2).not_found
    r = pf.run_command(["echo", "hi"], timeout=2)
    assert r.returncode == 0 and r.stdout.strip() == "hi"


def test_run_preflight_collects_everything_and_fails_if_any_fails(tmp_path, monkeypatch):
    repo = env_repo(tmp_path, "DB_NAME=qinno\n")  # production -> must fail
    (repo / ".sst").mkdir()
    (repo / ".sst" / "stage").write_text("develop2")
    (tmp_path / "plan").mkdir()
    yaml = tmp_path / "p.yaml"
    yaml.write_text(
        f"name: t\nrepo: {repo}\nplan_dir: plan\nsessions:\n"
        "  - {id: S1, claude_task: a.md, codex_task: null, handoff: h}\n"
    )
    monkeypatch.setattr(pf.shutil, "which", lambda _: "/usr/bin/caffeinate")
    run = fake(
        {
            ("aws", "configure"): ok(LIST_STATIC),
            ("aws", "sts"): ok("{}"),
            ("codex", "exec"): ok("ok"),
            ("claude", "--version"): ok("2.1.283"),
            ("codex", "--version"): ok("codex-cli 0.147.0"),
            ("git", "rev-parse", "--short"): ok("abc"),
            ("git", "rev-parse", "--abbrev-ref"): ok("m"),
            ("git", "status"): ok(""),
            ("pmset",): ok("Now drawing from 'AC Power'"),
        }
    )
    pipeline = load_pipeline(yaml, check_files=False)
    results = pf.run_preflight(pipeline, load_config(), run)
    assert len(results) == 9
    assert [r.name for r in results if not r.ok] == ["migrate-service DB_NAME"]
    assert not pf.all_ok(results)


def test_run_preflight_missing_repo(tmp_path):
    yaml = tmp_path / "p.yaml"
    yaml.write_text(
        f"name: t\nrepo: {tmp_path / 'gone'}\nplan_dir: plan\nsessions:\n"
        "  - {id: S1, claude_task: a.md, codex_task: null, handoff: h}\n"
    )
    results = pf.run_preflight(load_pipeline(yaml, check_files=False), load_config(), fake({}))
    assert len(results) == 1 and not results[0].ok
    assert isinstance(Path(yaml), Path)
