"""state.db — the orchestrator owns progress state (contract §3).

Agents only report a `STATUS:` line; everything else lives here so a crashed run can resume from
`steps`. Other modules touch the database only through `StateDB`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 2

# Step/run statuses are plain strings so S2's StepStatus enum can map onto them without a
# state.py -> runner.py import cycle.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
OPEN_STATUSES = ("pending", "running", "quota_wait")

_SCHEMA_V1 = """
CREATE TABLE runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline       TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    total_cost_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE steps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    session_id        TEXT NOT NULL,      -- pipeline session: S1..Sn
    phase             INTEGER NOT NULL,   -- 1..5 (readme §4 session flow)
    status            TEXT NOT NULL,
    claude_session_id TEXT,               -- Agent SDK session id, for `claude --resume`
    attempts          INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    last_error        TEXT,
    checkpoint_path   TEXT,
    started_at        TEXT,
    updated_at        TEXT NOT NULL,
    UNIQUE (run_id, session_id, phase)
);

CREATE TABLE decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    session_id TEXT NOT NULL,
    question   TEXT NOT NULL,
    options    TEXT,
    chosen     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    files      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(id),
    session_id TEXT,
    kind       TEXT NOT NULL,             -- quota_wait | blocker | gate | notice | ...
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_steps_run ON steps(run_id, session_id, phase);
CREATE INDEX idx_events_run ON events(run_id, id);
"""

# v2 (S3): `attempts` persists the circuit breaker's history so `resume` can rebuild it, and
# `runs.pipeline_path` lets `resume` find the pipeline without an argument.
_SCHEMA_V2 = """
ALTER TABLE runs ADD COLUMN pipeline_path TEXT;

CREATE TABLE attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    session_id    TEXT NOT NULL,
    phase         INTEGER NOT NULL,
    files_changed INTEGER NOT NULL,
    error         TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX idx_attempts_session ON attempts(run_id, session_id, id);
"""

_MIGRATIONS = {1: _SCHEMA_V1, 2: _SCHEMA_V2}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Run:
    id: int
    pipeline: str
    started_at: str
    finished_at: str | None
    status: str
    total_cost_usd: float
    pipeline_path: str | None = None


@dataclass(frozen=True)
class Step:
    id: int
    run_id: int
    session_id: str
    phase: int
    status: str
    claude_session_id: str | None
    attempts: int
    cost_usd: float
    last_error: str | None
    checkpoint_path: str | None
    started_at: str | None
    updated_at: str


@dataclass(frozen=True)
class Event:
    id: int
    run_id: int | None
    session_id: str | None
    kind: str
    detail: str | None
    created_at: str


class StateDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # -- lifecycle ---------------------------------------------------------------------------

    def _migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"state.db schema v{current} is newer than this orchestrator (v{SCHEMA_VERSION})"
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            with self._tx():
                self._conn.executescript(_MIGRATIONS[version])
                # PRAGMA cannot be parameterised; version is an int we control.
                self._conn.execute(f"PRAGMA user_version = {int(version)}")

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # -- runs --------------------------------------------------------------------------------

    def start_run(self, pipeline: str, pipeline_path: str | None = None) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO runs (pipeline, started_at, status, pipeline_path) "
                "VALUES (?, ?, ?, ?)",
                (pipeline, _now(), RUNNING, pipeline_path),
            )
            return int(cur.lastrowid)

    def set_run_status(self, run_id: int, status: str) -> None:
        """Change the status without finishing the run (e.g. back to `running` on resume)."""
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET status = ?, finished_at = NULL WHERE id = ?", (status, run_id)
            )

    def finish_run(self, run_id: int, status: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                (status, _now(), run_id),
            )

    def get_run(self, run_id: int) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run(**dict(row)) if row else None

    def latest_run(self, pipeline: str | None = None) -> Run | None:
        sql, args = "SELECT * FROM runs", ()
        if pipeline is not None:
            sql, args = sql + " WHERE pipeline = ?", (pipeline,)
        row = self._conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return Run(**dict(row)) if row else None

    def add_run_cost(self, run_id: int, cost_usd: float) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET total_cost_usd = total_cost_usd + ? WHERE id = ?",
                (cost_usd, run_id),
            )

    # -- steps -------------------------------------------------------------------------------

    def ensure_step(self, run_id: int, session_id: str, phase: int) -> Step:
        """Return the (run, session, phase) step, creating it as `pending` on first use."""
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO steps (run_id, session_id, phase, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, session_id, phase, PENDING, _now()),
            )
        return self._get_step(run_id, session_id, phase)

    def _get_step(self, run_id: int, session_id: str, phase: int) -> Step:
        row = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? AND session_id = ? AND phase = ?",
            (run_id, session_id, phase),
        ).fetchone()
        return Step(**dict(row))

    def update_step(
        self,
        step_id: int,
        *,
        status: str | None = None,
        claude_session_id: str | None = None,
        add_cost_usd: float = 0.0,
        increment_attempts: bool = False,
        last_error: str | None = None,
        checkpoint_path: str | None = None,
    ) -> Step:
        """Write the given fields right away (a crash must not lose the session id or cost)."""
        sets, args = ["updated_at = ?"], [_now()]
        if status is not None:
            sets.append("status = ?")
            args.append(status)
            if status == RUNNING:
                sets.append("started_at = COALESCE(started_at, ?)")
                args.append(_now())
        if claude_session_id is not None:
            sets.append("claude_session_id = ?")
            args.append(claude_session_id)
        if add_cost_usd:
            sets.append("cost_usd = cost_usd + ?")
            args.append(add_cost_usd)
        if increment_attempts:
            sets.append("attempts = attempts + 1")
        if last_error is not None:
            sets.append("last_error = ?")
            args.append(last_error)
        if checkpoint_path is not None:
            sets.append("checkpoint_path = ?")
            args.append(checkpoint_path)
        with self._tx() as c:
            c.execute(f"UPDATE steps SET {', '.join(sets)} WHERE id = ?", (*args, step_id))
            if add_cost_usd:
                c.execute(
                    "UPDATE runs SET total_cost_usd = total_cost_usd + ? "
                    "WHERE id = (SELECT run_id FROM steps WHERE id = ?)",
                    (add_cost_usd, step_id),
                )
        row = self._conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return Step(**dict(row))

    def get_step(self, step_id: int) -> Step:
        row = self._conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        if row is None:
            raise KeyError(f"no step {step_id}")
        return Step(**dict(row))

    def steps_for_run(self, run_id: int) -> list[Step]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY session_id, phase", (run_id,)
        ).fetchall()
        return [Step(**dict(r)) for r in rows]

    def last_unfinished_step(self, run_id: int) -> Step | None:
        """The step `resume` continues from: most recently touched step that is not finished."""
        marks = ",".join("?" for _ in OPEN_STATUSES)
        row = self._conn.execute(
            f"SELECT * FROM steps WHERE run_id = ? AND status IN ({marks}) "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (run_id, *OPEN_STATUSES),
        ).fetchone()
        return Step(**dict(row)) if row else None

    def session_cost(self, run_id: int, session_id: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM steps WHERE run_id = ? AND session_id = ?",
            (run_id, session_id),
        ).fetchone()
        return float(row[0])

    # -- circuit breaker history -------------------------------------------------------------

    def add_attempt(
        self, run_id: int, session_id: str, phase: int, files_changed: int, error: str | None
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO attempts (run_id, session_id, phase, files_changed, error, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, session_id, phase, files_changed, error, _now()),
            )
            return int(cur.lastrowid)

    def attempts_for(self, run_id: int, session_id: str) -> list[tuple[int, str | None]]:
        """(files_changed, error) per recorded attempt, oldest first."""
        rows = self._conn.execute(
            "SELECT files_changed, error FROM attempts WHERE run_id = ? AND session_id = ? "
            "ORDER BY id",
            (run_id, session_id),
        ).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def reset_step(self, step_id: int) -> None:
        """Back to `pending` after a human fixed the cause (`resume --retry`)."""
        with self._tx() as c:
            c.execute(
                "UPDATE steps SET status = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (PENDING, _now(), step_id),
            )

    # -- events / decisions ------------------------------------------------------------------

    def add_event(
        self,
        kind: str,
        detail: str | None = None,
        *,
        run_id: int | None = None,
        session_id: str | None = None,
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO events (run_id, session_id, kind, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, session_id, kind, detail, _now()),
            )
            return int(cur.lastrowid)

    def events(self, run_id: int | None = None, kind: str | None = None) -> list[Event]:
        sql, args, where = "SELECT * FROM events", [], []
        if run_id is not None:
            where.append("run_id = ?")
            args.append(run_id)
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [Event(**dict(r)) for r in rows]

    def count_events_since(self, kind: str, since: datetime) -> int:
        """Events of `kind` created at or after `since` (used for the hourly call limit)."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ? AND created_at >= ?",
            (kind, since.astimezone(UTC).isoformat(timespec="seconds")),
        ).fetchone()
        return int(row[0])

    def add_decision(
        self,
        run_id: int,
        session_id: str,
        question: str,
        chosen: str,
        reason: str,
        options: str | None = None,
        files: str | None = None,
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO decisions (run_id, session_id, question, options, chosen, reason, "
                "files, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, session_id, question, options, chosen, reason, files, _now()),
            )
            return int(cur.lastrowid)

    def decision_questions(self, run_id: int, session_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT question FROM decisions WHERE run_id = ? AND session_id = ?",
            (run_id, session_id),
        ).fetchall()
        return {r[0] for r in rows}

    def decision_count(self, run_id: int) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )
