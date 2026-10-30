"""Orchestrator-wide settings (not per-pipeline; those live in the pipeline yaml).

Config is data: an optional TOML file, never a sourced shell script. Environment variables
override the file, which overrides the defaults below.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME_DIR_ENV = "ORCHESTRATOR_HOME"
CONFIG_FILE_ENV = "ORCHESTRATOR_CONFIG"
STATE_DB_ENV = "ORCHESTRATOR_STATE_DB"

# O12 (readme §2): per call / per session / total, USD.
DEFAULT_BUDGET_USD = {"per_call": 2.0, "per_session": 10.0, "total": 100.0}


@dataclass(frozen=True)
class Budget:
    per_call: float
    per_session: float
    total: float


@dataclass(frozen=True)
class Config:
    home: Path
    state_db: Path
    checkpoint_dir: Path  # outside any repo (O3: no commits, snapshots live here)
    log_dir: Path
    budget: Budget = field(default_factory=lambda: Budget(**DEFAULT_BUDGET_USD))
    call_timeout_s: int = 1800
    quota_wait_s: int = 3600
    max_calls_per_hour: int = 60
    command_timeout_s: int = 60  # preflight subprocess checks


PROJECT_ROOT_ENV = "ORCHESTRATOR_PROJECT_ROOT"


def project_root() -> Path:
    """The orchestrator project's own root (where `.env.local` lives); not the target repo."""
    if PROJECT_ROOT_ENV in os.environ:
        return Path(os.environ[PROJECT_ROOT_ENV]).expanduser()
    return Path(__file__).resolve().parent.parent


def _default_home() -> Path:
    return Path(os.environ.get(HOME_DIR_ENV, "~/.agent-orchestrator")).expanduser()


def load_config(path: Path | None = None) -> Config:
    home = _default_home()
    if path is None and CONFIG_FILE_ENV in os.environ:
        path = Path(os.environ[CONFIG_FILE_ENV]).expanduser()
    if path is None:
        path = home / "config.toml"

    raw: dict = {}
    if path.is_file():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    budget_raw = {**DEFAULT_BUDGET_USD, **raw.get("budget_usd", {})}
    state_db = Path(os.environ.get(STATE_DB_ENV, raw.get("state_db", "state.db"))).expanduser()
    return Config(
        home=home,
        state_db=state_db,
        checkpoint_dir=Path(raw.get("checkpoint_dir", home / "checkpoints")).expanduser(),
        log_dir=Path(raw.get("log_dir", "logs")).expanduser(),
        budget=Budget(**{k: float(v) for k, v in budget_raw.items()}),
        call_timeout_s=int(raw.get("call_timeout_s", 1800)),
        quota_wait_s=int(raw.get("quota_wait_s", 3600)),
        max_calls_per_hour=int(raw.get("max_calls_per_hour", 60)),
        command_timeout_s=int(raw.get("command_timeout_s", 60)),
    )
