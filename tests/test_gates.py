import os
from pathlib import Path

import pytest

from orchestrator import gates
from orchestrator.gates import (
    Attempt,
    GateError,
    allowlist_violations,
    breaker_tripped,
    changed_files,
    evaluate,
    load_snapshot,
    read_status,
    run_tests,
    take_snapshot,
)
from orchestrator.pipeline import SessionSpec
from orchestrator.preflight import CmdResult
from tests.conftest import git


def session(tests=(), allowlist=()):
    return SessionSpec(
        id="S1",
        claude_task="a.md",
        codex_task="b.md",
        handoff="handoff.md",
        codex_allowlist=tuple(allowlist),
        tests=tuple(tests),
    )


# ---- snapshots -------------------------------------------------------------------------------


def test_snapshot_of_a_clean_tree_is_empty(toy_repo, tmp_path):
    snap = take_snapshot(toy_repo, tmp_path / "cp" / "a", "a")
    assert snap.manifest == {}
    assert (snap.directory / "manifest.json").is_file() and snap.patch_path.is_file()


def test_snapshot_diff_lists_modified_new_deleted_and_reverted(toy_repo, tmp_path):
    a = take_snapshot(toy_repo, tmp_path / "cp" / "a", "a")
    (toy_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")  # modified
    (toy_repo / "src" / "new.py").write_text("x = 1\n")  # untracked, nested
    (toy_repo / "README.md").unlink()  # deleted
    b = take_snapshot(toy_repo, tmp_path / "cp" / "b", "b")
    assert changed_files(a, b) == ["README.md", "calc.py", "src/new.py"]
    assert b.manifest["README.md"] == gates.DELETED

    # editing again changes the hash; reverting a file to HEAD makes it disappear from the diff
    (toy_repo / "calc.py").write_text("changed again\n")
    c = take_snapshot(toy_repo, tmp_path / "cp" / "c", "c")
    assert changed_files(b, c) == ["calc.py"]
    git(toy_repo, "show", "HEAD:calc.py")  # read-only sanity
    (toy_repo / "calc.py").write_text(git(toy_repo, "show", "HEAD:calc.py"))
    d = take_snapshot(toy_repo, tmp_path / "cp" / "d", "d")
    assert changed_files(c, d) == ["calc.py"]
    assert "calc.py" not in d.manifest


def test_identical_dirty_state_is_no_change(toy_repo, tmp_path):
    (toy_repo / "calc.py").write_text("v1\n")
    a = take_snapshot(toy_repo, tmp_path / "a", "a")
    b = take_snapshot(toy_repo, tmp_path / "b", "b")
    assert changed_files(a, b) == []


def test_checkpoint_is_recoverable_and_outside_the_repo(toy_repo, tmp_path):
    (toy_repo / "calc.py").write_text("tracked edit\n")
    (toy_repo / "src" / "n.py").write_text("untracked body\n")
    snap = take_snapshot(toy_repo, tmp_path / "cp" / "s", "s")
    assert toy_repo not in snap.directory.parents
    assert "tracked edit" in snap.patch_path.read_text()
    assert (snap.directory / "untracked" / "src" / "n.py").read_text() == "untracked body\n"
    again = load_snapshot(snap.directory)
    assert again.manifest == snap.manifest


def test_snapshot_never_commits_or_changes_the_index(toy_repo, tmp_path):
    (toy_repo / "calc.py").write_text("x\n")
    (toy_repo / "new.py").write_text("y\n")
    before = (git(toy_repo, "rev-parse", "HEAD"), git(toy_repo, "status", "--porcelain"))
    take_snapshot(toy_repo, tmp_path / "s", "s")
    assert (git(toy_repo, "rev-parse", "HEAD"), git(toy_repo, "status", "--porcelain")) == before
    assert git(toy_repo, "stash", "list") == ""


def test_snapshot_handles_odd_names_and_a_repo_without_commits(tmp_path):
    import subprocess

    repo = tmp_path / "fresh"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "has space.txt").write_text("a")
    (repo / "ünï.txt").write_text("b")
    snap = take_snapshot(repo, tmp_path / "s", "s")
    assert set(snap.manifest) == {"has space.txt", "ünï.txt"}


def test_snapshot_outside_a_git_repo_is_an_error(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(GateError):
        take_snapshot(tmp_path / "plain", tmp_path / "s", "s")


def test_load_snapshot_requires_a_manifest(tmp_path):
    with pytest.raises(GateError):
        load_snapshot(tmp_path)


# ---- allowlist -------------------------------------------------------------------------------


def test_allowlist_violations():
    allow = [
        "packages/common/src/**",
        "apps/m/src/pages/pos/[orderId].tsx",
        "apps/m/locales/*/t.json",
    ]
    assert (
        allowlist_violations(["packages/common/src/a/b.ts", "apps/m/locales/en/t.json"], allow)
        == []
    )
    assert allowlist_violations(["apps/m/src/pages/pos/[orderId].tsx"], allow) == []
    assert allowlist_violations(["packages/db/x.ts", "apps/m/locales/en/x/t.json"], allow) == [
        "packages/db/x.ts",
        "apps/m/locales/en/x/t.json",
    ]
    assert allowlist_violations(["a.ts"], []) == ["a.ts"]  # empty allowlist allows nothing


def test_codex_allowlist_uses_the_snapshot_diff(toy_repo, tmp_path):
    claude_edit = toy_repo / "calc.py"
    claude_edit.write_text("claude was here\n")  # before Codex runs: not Codex's change
    a = take_snapshot(toy_repo, tmp_path / "a", "a")
    (toy_repo / "src" / "ok.py").write_text("codex\n")
    (toy_repo / "apps" / "agent" / "main.py").write_text("codex touched a forbidden area\n")
    b = take_snapshot(toy_repo, tmp_path / "b", "b")
    assert changed_files(a, b) == ["apps/agent/main.py", "src/ok.py"]
    assert allowlist_violations(changed_files(a, b), ["src/**"]) == ["apps/agent/main.py"]


# ---- circuit breaker -------------------------------------------------------------------------


def test_breaker_three_attempts_without_file_changes():
    assert breaker_tripped([]) is None
    assert breaker_tripped([Attempt(0), Attempt(0)]) is None
    assert "3 consecutive attempts changed no files" in breaker_tripped([Attempt(0)] * 3)
    # a change in between resets the count
    assert breaker_tripped([Attempt(0), Attempt(0), Attempt(2), Attempt(0), Attempt(0)]) is None
    assert breaker_tripped([Attempt(4), Attempt(0), Attempt(0), Attempt(0)]) is not None


def test_breaker_same_error_five_times_normalising_noise():
    errs = [Attempt(1, f"TypeError at line {n} (0x{n:x}) in /tmp/run-{n}") for n in range(40, 45)]
    assert "same error 5 times" in breaker_tripped(errs)
    assert breaker_tripped(errs[:4]) is None
    mixed = errs[:2] + [Attempt(1, "different failure")] + errs[:2]
    assert breaker_tripped(mixed) is None  # only consecutive repeats count
    assert breaker_tripped([Attempt(1, "a")] * 4 + [Attempt(1, None)]) is None


def test_breaker_limits_are_parameters():
    assert breaker_tripped([Attempt(0)] * 2, no_change_limit=2) is not None
    assert breaker_tripped([Attempt(1, "e")] * 2, same_error_limit=2) is not None


# ---- STATUS ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("STATUS: DONE\nrest", "DONE"),
        ("\n\nSTATUS: BLOCKED\n", "BLOCKED"),
        ("STATUS:DONE", "DONE"),
        ("# handoff\nSTATUS: DONE", None),
        ("STATUS: MAYBE", None),
        ("status: done", None),
        ("", None),
        ("STATUS: DONE extra", None),
    ],
)
def test_read_status(tmp_path, text, expected):
    f = tmp_path / "h.md"
    f.write_text(text)
    assert read_status(f) == expected


def test_read_status_missing_file(tmp_path):
    assert read_status(tmp_path / "nope.md") is None


# ---- test commands ---------------------------------------------------------------------------


def test_run_tests_uses_exit_codes_in_a_shell(toy_repo):
    res = run_tests(["python3 test_calc.py", "exit 3", "true && true"], toy_repo)
    assert [r.ok for r in res] == [False, False, True]  # toy repo starts with the bug
    assert res[0].returncode == 1 and "ZeroDivisionError" in res[0].output_tail
    assert res[1].returncode == 3


def test_run_tests_does_not_hide_failure_behind_a_pipe_in_a_loop(toy_repo):
    cmd = "for f in a b; do test -f $f.missing || { echo MISSING $f; exit 1; }; done"
    assert run_tests([cmd], toy_repo)[0].returncode == 1


def test_run_tests_timeout_and_stop_on_failure(toy_repo):
    res = run_tests(["sleep 30", "true"], toy_repo, timeout=1)
    assert res[0].timed_out and not res[0].ok
    res = run_tests(["false", "true"], toy_repo, stop_on_failure=True)
    assert len(res) == 1


# ---- evaluate --------------------------------------------------------------------------------


def good_inputs(toy_repo, tmp_path):
    handoff = toy_repo / "handoff.md"
    handoff.write_text("STATUS: DONE\nall good\n")
    cp = take_snapshot(toy_repo, tmp_path / "cp", "cp")
    return handoff, cp


def test_evaluate_passes_when_everything_is_green(toy_repo, tmp_path):
    handoff, cp = good_inputs(toy_repo, tmp_path)
    v = evaluate(
        session=session(tests=["true", "echo hi"]), repo=toy_repo, handoff=handoff, checkpoint=cp
    )
    assert v.passed and v.failures == [] and len(v.test_results) == 2


def test_evaluate_collects_every_failure(toy_repo, tmp_path):
    handoff = toy_repo / "handoff.md"
    handoff.write_text("# no status line\n")
    a = take_snapshot(toy_repo, tmp_path / "a", "a")
    (toy_repo / "apps" / "agent" / "main.py").write_text("bad\n")
    b = take_snapshot(toy_repo, tmp_path / "b", "b")
    v = evaluate(
        session=session(tests=["exit 2", "true"], allowlist=["src/**"]),
        repo=toy_repo,
        handoff=handoff,
        checkpoint=None,
        attempts=[Attempt(0)] * 3,
        codex_before=a,
        codex_after=b,
    )
    assert not v.passed
    text = "\n".join(v.failures)
    for needle in (
        "checkpoint",
        "STATUS",
        "allowlist",
        "apps/agent/main.py",
        "circuit breaker",
        "test failed (exit 2): exit 2",
    ):
        assert needle in text, needle
    assert len(v.failures) == 5


def test_evaluate_blocked_status_and_timeout(toy_repo, tmp_path):
    handoff, cp = good_inputs(toy_repo, tmp_path)
    handoff.write_text("STATUS: BLOCKED\n")
    v = evaluate(
        session=session(tests=["sleep 30"]),
        repo=toy_repo,
        handoff=handoff,
        checkpoint=cp,
        test_timeout=1,
    )
    assert any("handoff reports BLOCKED" in f for f in v.failures)
    assert any("timed out" in f for f in v.failures)


def test_evaluate_skips_allowlist_without_codex_snapshots(toy_repo, tmp_path):
    handoff, cp = good_inputs(toy_repo, tmp_path)
    v = evaluate(session=session(allowlist=[]), repo=toy_repo, handoff=handoff, checkpoint=cp)
    assert v.passed


def test_evaluate_with_a_mocked_runner(toy_repo, tmp_path):
    handoff, cp = good_inputs(toy_repo, tmp_path)
    seen = []

    def fake(cmd, *, timeout, cwd=None, env=None):
        seen.append((cmd, cwd))
        return CmdResult(0, "fine", "")

    v = evaluate(
        session=session(tests=["yarn test"]),
        repo=toy_repo,
        handoff=handoff,
        checkpoint=cp,
        run=fake,
    )
    assert v.passed and seen == [(["bash", "-c", "yarn test"], toy_repo)]
    assert Path(os.fspath(toy_repo)).is_dir()
