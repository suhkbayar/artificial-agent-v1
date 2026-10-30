"""Gates (contract §4; readme §4, §5): snapshots, allowlist, circuit breaker, STATUS, tests.

Progress is measured by file snapshots, never by commits (O3). A snapshot records, for every path
that differs from HEAD, a content hash (the manifest), and keeps a recoverable copy of the work
tree changes (`tracked.patch` + copies of untracked files) in a directory OUTSIDE the repo.
Comparing two manifests tells which files a step changed, which is how Codex's changes are
separated from Claude's.

Git is only read here. `GIT_OPTIONAL_LOCKS=0` stops `git status` from touching the index.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.globs import matches_any, normalize_rel
from orchestrator.pipeline import SessionSpec
from orchestrator.preflight import CmdResult, Runner, run_command

CLEAN = "<clean>"  # manifest marker: path is no longer different from HEAD
DELETED = "<deleted>"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # `git hash-object -t tree /dev/null`
MAX_COPY_BYTES = 10 * 1024 * 1024
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}

NO_CHANGE_LIMIT = 3
SAME_ERROR_LIMIT = 5

_STATUS_RE = re.compile(r"^STATUS:\s*(DONE|BLOCKED)\s*$")


class GateError(RuntimeError):
    pass


# ---- snapshots -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    label: str
    directory: (
        Path  # <checkpoint_dir>/.../<label>/ ; holds manifest.json, tracked.patch, untracked/
    )
    manifest: dict[str, str] = field(default_factory=dict)  # path -> sha256 | DELETED

    @property
    def patch_path(self) -> Path:
        return self.directory / "tracked.patch"


def _git(repo: Path, *args: str, run: Runner | None = None, timeout: int = 120) -> CmdResult:
    runner = run or run_command
    return runner(["git", "-C", str(repo), *args], timeout=timeout, env={**os.environ, **GIT_ENV})


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _status_paths(repo: Path, run: Runner | None) -> list[str]:
    res = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", run=run)
    if res.returncode != 0:
        raise GateError(f"git status failed in {repo}: {res.output[:200]}")
    entries = res.stdout.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        xy, path = e[:2], e[3:]
        paths.append(path)
        if "R" in xy or "C" in xy:  # rename/copy: the next field is the original path
            if i < len(entries) and entries[i]:
                paths.append(entries[i])
            i += 1
    return sorted(set(paths))


def take_snapshot(
    repo: Path, directory: Path, label: str, *, run: Runner | None = None
) -> Snapshot:
    """Record the work tree's difference from HEAD. Never commits, stashes or checks out."""
    repo = Path(repo)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", run=run)
    base = "HEAD" if head.returncode == 0 else EMPTY_TREE
    diff = _git(repo, "diff", "--binary", "--no-ext-diff", base, run=run, timeout=300)
    if diff.returncode != 0:
        raise GateError(f"git diff failed: {diff.output[:200]}")
    (directory / "tracked.patch").write_text(diff.stdout)

    manifest: dict[str, str] = {}
    untracked_dir = directory / "untracked"
    if untracked_dir.exists():
        shutil.rmtree(untracked_dir)
    for rel in _status_paths(repo, run):
        full = repo / rel
        if full.is_symlink() or not full.exists():
            manifest[rel] = DELETED if not full.is_symlink() else f"symlink:{full.readlink()}"
            continue
        if full.is_dir():
            continue
        manifest[rel] = _sha256(full)
        if full.stat().st_size <= MAX_COPY_BYTES:
            dest = untracked_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full, dest)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (directory / "HEAD").write_text(head.stdout.strip() or "<no commits>")
    return Snapshot(label=label, directory=directory, manifest=manifest)


def load_snapshot(directory: Path, label: str = "") -> Snapshot:
    directory = Path(directory)
    manifest_file = directory / "manifest.json"
    if not manifest_file.is_file():
        raise GateError(f"no snapshot manifest in {directory}")
    return Snapshot(label or directory.name, directory, json.loads(manifest_file.read_text()))


def changed_files(before: Snapshot, after: Snapshot) -> list[str]:
    """Paths whose content differs between two snapshots (a path absent from a manifest = CLEAN)."""
    paths = set(before.manifest) | set(after.manifest)
    return sorted(p for p in paths if before.manifest.get(p, CLEAN) != after.manifest.get(p, CLEAN))


# ---- allowlist -------------------------------------------------------------------------------


def allowlist_violations(changed: Sequence[str], allowlist: Sequence[str]) -> list[str]:
    """Files that match no allowlist glob. An empty allowlist allows nothing."""
    return [p for p in changed if matches_any(list(allowlist), normalize_rel(p)) is None]


# ---- circuit breaker -------------------------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    files_changed: int
    error: str | None = None


_NOISE = re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{7,}\b|\d+", re.I)


def error_signature(error: str | None) -> str | None:
    """Same failure, different numbers/hashes/paths-with-ids, must compare equal."""
    if not error:
        return None
    return _NOISE.sub("N", " ".join(error.lower().split()))[:200]


def breaker_tripped(
    attempts: Sequence[Attempt],
    *,
    no_change_limit: int = NO_CHANGE_LIMIT,
    same_error_limit: int = SAME_ERROR_LIMIT,
) -> str | None:
    """Reason to stop, or None. Both rules look at the most recent *consecutive* attempts."""
    run = 0
    for a in reversed(attempts):
        if a.files_changed == 0:
            run += 1
        else:
            break
    if run >= no_change_limit:
        return f"circuit breaker: {run} consecutive attempts changed no files"

    last = error_signature(attempts[-1].error) if attempts else None
    if last is not None:
        same = 0
        for a in reversed(attempts):
            if error_signature(a.error) == last:
                same += 1
            else:
                break
        if same >= same_error_limit:
            return f"circuit breaker: the same error {same} times in a row: {last[:80]}"
    return None


# ---- STATUS line -----------------------------------------------------------------------------


def read_status(handoff: Path) -> str | None:
    """`DONE` / `BLOCKED` from the first non-empty line of the handoff file, else None."""
    try:
        for line in Path(handoff).read_text().splitlines():
            if line.strip():
                m = _STATUS_RE.match(line.strip())
                return m.group(1) if m else None
    except OSError:
        return None
    return None


# ---- tests -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # not a pytest class

    command: str
    returncode: int
    output_tail: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_tests(
    commands: Sequence[str],
    repo: Path,
    *,
    run: Runner | None = None,
    timeout: int = 1800,
    stop_on_failure: bool = False,
) -> list[TestResult]:
    """Run each command in a shell at the repo root. The exit code is the verdict (no `| tail`)."""
    runner = run or run_command
    results: list[TestResult] = []
    for cmd in commands:
        res = runner(["bash", "-c", cmd], timeout=timeout, cwd=repo)
        tail = "\n".join(res.output.splitlines()[-15:])
        results.append(TestResult(cmd, res.returncode, tail, res.timed_out))
        if stop_on_failure and not results[-1].ok:
            break
    return results


# ---- verdict ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    failures: list[str]
    test_results: tuple[TestResult, ...] = ()  # additive to contract §4: the evidence


def evaluate(
    *,
    session: SessionSpec,
    repo: Path,
    handoff: Path,
    checkpoint: Snapshot | None,
    attempts: Sequence[Attempt] = (),
    codex_before: Snapshot | None = None,
    codex_after: Snapshot | None = None,
    run: Runner | None = None,
    test_timeout: int = 1800,
) -> GateVerdict:
    """Contract §4: tests, STATUS, allowlist, checkpoint and circuit breaker in one place.

    `codex_before`/`codex_after` are the snapshots taken around the Codex run; when both are given
    every file Codex changed must be inside `session.codex_allowlist`.
    """
    failures: list[str] = []

    if (
        checkpoint is None
        or not checkpoint.patch_path.is_file()
        or not (checkpoint.directory / "manifest.json").is_file()
    ):
        failures.append("checkpoint: no saved snapshot (patch + manifest) for this step")

    status = read_status(handoff)
    if status is None:
        failures.append(f"STATUS: first line of {handoff.name} must be `STATUS: DONE` or `BLOCKED`")
    elif status != "DONE":
        failures.append(f"STATUS: handoff reports {status}")

    if codex_before is not None and codex_after is not None:
        outside = allowlist_violations(
            changed_files(codex_before, codex_after), session.codex_allowlist
        )
        if outside:
            shown = ", ".join(outside[:10]) + (" ..." if len(outside) > 10 else "")
            failures.append(f"allowlist: Codex changed {len(outside)} file(s) outside it: {shown}")

    if reason := breaker_tripped(attempts):
        failures.append(reason)

    results = tuple(run_tests(session.tests, repo, run=run, timeout=test_timeout))
    for r in results:
        if r.timed_out:
            failures.append(f"test timed out: {r.command}")
        elif r.returncode != 0:
            failures.append(f"test failed (exit {r.returncode}): {r.command}")

    return GateVerdict(not failures, failures, results)
