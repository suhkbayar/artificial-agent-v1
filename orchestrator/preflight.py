"""Preflight (readme §6): every check returns a CheckResult; any failure means DO NOT START.

Checks only read. In particular the qinno `.env` and `.sst/stage` are never written; the fix for
a wrong DB_NAME is the user's `npm run sync-secrets -- --stage=develop2`.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import Config
from orchestrator.pipeline import REQUIRED_STAGE, Pipeline

EXPECTED_DB_NAME = "qinno_develop2"
PRODUCTION_DB_NAME = "qinno"
MIN_CLAUDE_VERSION = (2, 1, 259)
# `aws configure list` credential types that are long-lived keys (not SSO/role sessions that
# expire mid-night).
STATIC_AWS_CREDENTIAL_TYPES = frozenset({"env", "shared-credentials-file", "config-file"})


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix_hint: str = ""


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    not_found: bool = False

    @property
    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


# A runner is injected so every check is unit-testable without real subprocesses.
Runner = Callable[..., CmdResult]


def run_command(
    cmd: Sequence[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> CmdResult:
    """Run `cmd` (stdin = `input_text` or nothing); on timeout kill the whole process group."""
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        return CmdResult(127, "", f"{cmd[0]}: command not found", not_found=True)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return CmdResult(-1, out or "", err or "", timed_out=True)
    return CmdResult(proc.returncode, out, err)


# -- individual checks -----------------------------------------------------------------------


def parse_env_value(text: str, key: str) -> str | None:
    """Read KEY from dotenv text as data (no shell evaluation)."""
    for line in text.splitlines():
        m = re.match(rf"\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*?)\s*$", line)
        if m:
            value = m.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return None


def check_db_name(repo: Path) -> CheckResult:
    name = "migrate-service DB_NAME"
    env_file = repo / "services" / "migrate-service" / ".env"
    fix = "npm run sync-secrets -- --stage=develop2 (run by the user, in services/migrate-service)"
    try:
        text = env_file.read_text()
    except OSError as exc:
        return CheckResult(name, False, f"cannot read {env_file}: {exc.strerror}", fix)
    value = parse_env_value(text, "DB_NAME")
    if value == EXPECTED_DB_NAME:
        return CheckResult(name, True, f"DB_NAME={value}")
    if value == PRODUCTION_DB_NAME:
        return CheckResult(name, False, f"DB_NAME={value} is PRODUCTION", fix)
    return CheckResult(name, False, f"DB_NAME={value!r}, expected {EXPECTED_DB_NAME}", fix)


def check_sst_stage(repo: Path) -> CheckResult:
    name = ".sst/stage"
    stage_file = repo / ".sst" / "stage"
    try:
        stage = stage_file.read_text().strip()
    except OSError as exc:
        return CheckResult(
            name,
            False,
            f"cannot read {stage_file}: {exc.strerror}",
            f"echo {REQUIRED_STAGE} > .sst/stage (user)",
        )
    if stage == REQUIRED_STAGE:
        return CheckResult(name, True, f"stage={stage}")
    return CheckResult(
        name,
        False,
        f"stage={stage!r}, expected {REQUIRED_STAGE}",
        "switch the SST stage to develop2 (user)",
    )


def check_aws(run: Runner, timeout: int) -> CheckResult:
    name = "AWS credentials"
    listed = run(["aws", "configure", "list"], timeout=timeout)
    if listed.not_found:
        return CheckResult(name, False, "aws CLI not found", "install the AWS CLI")
    if listed.timed_out or listed.returncode != 0:
        return CheckResult(
            name,
            False,
            f"`aws configure list` failed: {listed.output[:200]}",
            "check the AWS CLI configuration",
        )
    cred_type = None
    for line in listed.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == "access_key":
            cred_type = parts[2] if len(parts) > 2 else None
            if len(parts) > 1 and parts[1] == "<not":
                cred_type = None
            break
    if cred_type is None:
        return CheckResult(
            name,
            False,
            "no access key configured",
            "configure a static access key (SSO sessions expire overnight)",
        )
    if cred_type not in STATIC_AWS_CREDENTIAL_TYPES:
        return CheckResult(
            name,
            False,
            f"credential type is {cred_type!r}, not a static key",
            "use a static access key; SSO/role sessions expire during unattended runs",
        )
    ident = run(["aws", "sts", "get-caller-identity"], timeout=timeout)
    if ident.timed_out:
        return CheckResult(name, False, "sts get-caller-identity timed out", "check network/AWS")
    if ident.returncode != 0:
        return CheckResult(
            name,
            False,
            f"sts get-caller-identity failed: {ident.output[:200]}",
            "the static key is invalid or expired",
        )
    return CheckResult(name, True, f"static key ({cred_type}), sts get-caller-identity ok")


def check_codex_ready(run: Runner, timeout: int) -> CheckResult:
    name = "codex exec"
    res = run(
        ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "Reply ok"], timeout=timeout
    )
    if res.not_found:
        return CheckResult(name, False, "codex CLI not found", "install codex-cli")
    if res.timed_out:
        return CheckResult(name, False, f"timed out after {timeout}s", "check codex login/network")
    if res.returncode != 0:
        return CheckResult(
            name, False, f"exit {res.returncode}: {res.output[-200:]}", "run `codex login` (user)"
        )
    if "ok" not in res.output.lower():
        return CheckResult(
            name,
            False,
            f"no 'ok' in reply: {res.output[-200:]}",
            'run `codex exec ... "Reply ok"` by hand',
        )
    return CheckResult(name, True, "codex replied ok")


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in m.groups()) if m else None


def check_claude_version(run: Runner, timeout: int) -> CheckResult:
    name = "claude version"
    need = ".".join(map(str, MIN_CLAUDE_VERSION))
    res = run(["claude", "--version"], timeout=timeout)
    if res.not_found:
        return CheckResult(name, False, "claude CLI not found", "install Claude Code")
    version = parse_version(res.output)
    if res.returncode != 0 or version is None:
        return CheckResult(name, False, f"cannot parse version from {res.output[:100]!r}", "")
    shown = ".".join(map(str, version))
    if version < MIN_CLAUDE_VERSION:
        return CheckResult(name, False, f"{shown} < {need}", "update Claude Code")
    return CheckResult(name, True, f"{shown} (>= {need})")


def check_codex_version(run: Runner, timeout: int) -> CheckResult:
    """Informational: 0.147.0 is noted in readme §6 but is not a hard requirement."""
    name = "codex version"
    res = run(["codex", "--version"], timeout=timeout)
    if res.not_found:
        return CheckResult(name, False, "codex CLI not found", "install codex-cli")
    version = parse_version(res.output)
    if res.returncode != 0 or version is None:
        return CheckResult(name, False, f"cannot parse version from {res.output[:100]!r}", "")
    shown = ".".join(map(str, version))
    note = "" if shown == "0.147.0" else " (differs from tested 0.147.0; not enforced)"
    return CheckResult(name, True, f"{shown}{note}")


def check_git_state(repo: Path, run: Runner, timeout: int) -> CheckResult:
    """Record the working-tree state. The orchestrator never commits (O3), so this only reports."""
    name = "git state"
    head = run(["git", "rev-parse", "--short", "HEAD"], timeout=timeout, cwd=repo)
    if head.returncode != 0:
        return CheckResult(
            name,
            False,
            f"not a git repo with commits: {head.output[:150]}",
            "check `repo:` in the pipeline yaml",
        )
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=timeout, cwd=repo)
    status = run(["git", "status", "--porcelain"], timeout=timeout, cwd=repo)
    if status.returncode != 0:
        return CheckResult(name, False, f"git status failed: {status.output[:150]}", "")
    dirty = len([ln for ln in status.stdout.splitlines() if ln.strip()])
    state = "clean" if dirty == 0 else f"dirty ({dirty} paths)"
    return CheckResult(name, True, f"{branch.output.strip()}@{head.output.strip()} {state}")


def check_memory_health(store: object | None = None) -> CheckResult:
    """Graph readable, no leaked memory-server processes, no foreign lock, not Sunday reconcile.

    A failure does NOT stop the run (readme §7): the flow continues without the graph and writes
    pending files. So it reports ok=True with a DEGRADED detail; the flow asks the store itself.
    """
    from orchestrator.memory import MemoryStore  # lazy: memory imports this module

    health = (store or MemoryStore()).health()  # type: ignore[attr-defined]
    if health.ok:
        return CheckResult("memory MCP", True, health.detail)
    return CheckResult(
        "memory MCP",
        True,
        f"DEGRADED (run continues without the graph, writes go to pending): {health.detail}",
    )


def check_power(run: Runner, timeout: int) -> CheckResult:
    name = "power (Mac)"
    if shutil.which("caffeinate") is None:
        return CheckResult(
            name, False, "caffeinate not found (is this macOS?)", "run on the Mac host (O10)"
        )
    res = run(["pmset", "-g", "batt"], timeout=timeout)
    if res.not_found or res.returncode != 0:
        return CheckResult(name, False, f"pmset failed: {res.output[:150]}", "")
    first = res.stdout.splitlines()[0] if res.stdout else ""
    if "AC Power" not in first:
        return CheckResult(
            name,
            False,
            f"not on AC power ({first.strip()})",
            "plug in the charger, keep the lid open, run under `caffeinate -i`",
        )
    return CheckResult(name, True, "AC power, caffeinate available")


# -- entry point -----------------------------------------------------------------------------


def run_preflight(
    pipeline: Pipeline, config: Config, run: Runner | None = None
) -> list[CheckResult]:
    runner = run or run_command
    t = config.command_timeout_s
    repo = pipeline.repo
    if not repo.is_dir():
        return [CheckResult("repo", False, f"repo not found: {repo}", "fix `repo:` in the yaml")]
    stage_checks = (
        [check_db_name(repo), check_sst_stage(repo), check_aws(runner, t)]
        if pipeline.requires_stage
        else []
    )
    return [
        *stage_checks,
        check_codex_ready(runner, max(t, 120)),
        check_claude_version(runner, t),
        check_codex_version(runner, t),
        check_git_state(repo, runner, t),
        check_memory_health(),
        check_power(runner, t),
    ]


def all_ok(results: Sequence[CheckResult]) -> bool:
    return all(r.ok for r in results)
