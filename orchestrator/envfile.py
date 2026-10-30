"""`.env.local` loader and secret redaction (S3 task 3a).

The project-root `.env.local` is parsed as DATA (no shell `source`, no expansion, no command
substitution) and only an allowlist of names is ever returned. The result goes to the Agent SDK's
`env` option and nowhere else: it is never copied into `os.environ`, never logged, never written to
state.db / events and never part of an error message. `redact()` is the last line of defence for
text that is about to be stored (stderr tails, exception text, tool output).

A missing file is not an error: the SDK then falls back to the machine's claude.ai login.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

ENV_FILE_NAME = ".env.local"
# The only names read from the file. Everything else in it is ignored, so a stray AWS or DB
# credential in `.env.local` can never reach an SDK session through this loader.
LOADABLE_KEYS = frozenset({"ANTHROPIC_API_KEY"})
REDACTED = "[REDACTED]"

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_KNOWN_SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
MIN_SECRET_LEN = 8  # shorter values would redact ordinary words


class EnvFileError(ValueError):
    """The file exists but cannot be used. Never carries a value."""


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    # unquoted: a trailing ` # comment` is a comment, `#` inside a token is not
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def parse_env_text(text: str, keys: Iterable[str] = LOADABLE_KEYS) -> dict[str, str]:
    """Pure parser: `KEY=value` lines for the wanted keys only; the last assignment wins."""
    wanted = frozenset(keys)
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m and m.group(1) in wanted:
            value = _unquote(m.group(2))
            if value:
                found[m.group(1)] = value
            else:
                found.pop(m.group(1), None)  # `KEY=` unsets, as in a dotenv file
    return found


def load_sdk_env(project_root: Path, *, name: str = ENV_FILE_NAME) -> dict[str, str]:
    """Allowlisted variables from `<project_root>/.env.local`; `{}` if the file does not exist."""
    path = Path(project_root) / name
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        # strerror only: never echo file content
        raise EnvFileError(f"cannot read {path}: {type(exc).__name__}") from None
    return parse_env_text(text)


def secret_values(env: Mapping[str, str] | None) -> tuple[str, ...]:
    """Values that must never appear in stored text, longest first so overlaps redact whole."""
    values = {v for v in (env or {}).values() if len(v) >= MIN_SECRET_LEN}
    return tuple(sorted(values, key=len, reverse=True))


def redact(text: str | None, secrets: Iterable[str] = ()) -> str | None:
    """Replace every known secret value, and anything shaped like an Anthropic key."""
    if not text:
        return text
    for s in secrets:
        if s and len(s) >= MIN_SECRET_LEN:
            text = text.replace(s, REDACTED)
    return _KNOWN_SECRET.sub(REDACTED, text)


# Claude Code drops credential-class variables from the environment of the subprocesses its tools
# start (Bash, hooks, MCP) when this is set. Observed (docs/notes/agent-sdk.md §11): a fake
# AWS_SECRET_ACCESS_KEY is invisible to the agent's Bash with it and visible without it. The
# catch: the CLI then FORCES permission mode `default` ("allowed_non_write_users hardening"), so
# `auto`/`plan` are lost. Hence it is set only when there is a key to protect.
SCRUB_SUBPROCESS_ENV = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"
SCRUB_OPT_OUT_ENV = "ORCHESTRATOR_SCRUB_SUBPROCESS_ENV"  # =0: keep auto/plan, key stays visible


def sdk_env(project_root: Path, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The `env` for the Agent SDK: allowlisted `.env.local` values (+ the scrub flag if any)."""
    env = load_sdk_env(project_root)
    opted_out = (environ if environ is not None else os.environ).get(SCRUB_OPT_OUT_ENV) == "0"
    if env and not opted_out:
        env[SCRUB_SUBPROCESS_ENV] = "1"
    return env
