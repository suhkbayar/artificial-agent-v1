"""Memory graph access for the session flow (readme §7).

Division of labour:
  * READ  — `open_nodes(names)` parses `memory.jsonl` read-only (exact entity names, never a full
            graph dump) to put the session's `memory_entities` into prompts.
  * WRITE — only the ⑤ verify agent, only through the memory MCP tools (the file is never edited
            here). The orchestrator wraps that window: `write_window()` takes the same
            `.reconcile.lock` directory lock as `tools/weekly-reconcile.sh` (launchd
            `com.suhkbayar.graph-reconcile`, Sunday 10:00), refuses to start inside that cron
            window, and `backup()` runs first. `audit()` compares the graph before/after and flags
            "ask first" changes and entities whose `confidence.jsonl` row was not touched.
  * ASK FIRST actions are never performed; they go to `logs/pending-*.md` (`write_pending`).

If the health check fails the flow continues WITHOUT the graph and writes the intended updates to
pending files instead.

Tests must use a tmp copy of the memory directory, never the real one.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from orchestrator.preflight import Runner, run_command

DEFAULT_MEMORY_DIR = "~/Documents/my-memory"
LOCK_DIR_NAME = ".reconcile.lock"  # same mkdir lock as tools/weekly-reconcile.sh
BACKUP_DIR_NAME = "backups-orchestrator"  # NOT backups/: the reconcile job prunes that to 8 by name
KEEP_BACKUPS = 8
RECONCILE_WEEKDAY = 6  # Sunday (datetime.weekday)
RECONCILE_START = (9, 50)  # a little before the 10:00 launchd start
RECONCILE_END = (11, 0)
PROTECTED_ENTITIES = frozenset({"Memory Model", "Graph Legend", "Playbook"})  # ask first
STALE_PROCESS_AGE = timedelta(hours=1)

READ_TOOLS = ("open_nodes", "search_nodes")
WRITE_TOOLS = (
    "create_entities",
    "create_relations",
    "add_observations",
    "delete_entities",
    "delete_observations",
    "delete_relations",
)
ALL_TOOLS = (*READ_TOOLS, "read_graph", *WRITE_TOOLS)


def memory_tool(name: str) -> str:
    return f"mcp__memory__{name}"


def write_tool_names() -> frozenset[str]:
    return frozenset(memory_tool(n) for n in WRITE_TOOLS)


def all_tool_names() -> frozenset[str]:
    return frozenset(memory_tool(n) for n in ALL_TOOLS)


class MemoryBusy(RuntimeError):
    """The graph cannot be written now (lock held, reconcile window, or unhealthy)."""


@dataclass(frozen=True)
class Entity:
    name: str
    entity_type: str
    observations: tuple[str, ...]

    def fingerprint(self) -> str:
        blob = json.dumps([self.entity_type, list(self.observations)], ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def render(self, max_chars: int = 6000) -> str:
        body = "\n".join(f"- {o}" for o in self.observations)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n- ... (truncated; ask for the entity by exact name)"
        return f"### {self.name} ({self.entity_type})\n{body}"


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str


@dataclass(frozen=True)
class GraphState:
    entities: dict[str, str]  # name -> fingerprint
    confidence: dict[str, str]  # entity -> serialized row


@dataclass(frozen=True)
class GraphAudit:
    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    protected_touched: tuple[str, ...] = ()
    confidence_missing: tuple[str, ...] = ()
    bad_confidence_rows: bool = False

    @property
    def wrote(self) -> bool:
        return bool(self.added or self.changed or self.removed)

    @property
    def ask_first_violation(self) -> bool:
        return bool(self.removed or self.protected_touched)

    def problems(self) -> list[str]:
        out = []
        if self.removed:
            out.append(f"entities removed (ask first): {', '.join(self.removed)}")
        if self.protected_touched:
            out.append(
                f"protected entities changed (ask first): {', '.join(self.protected_touched)}"
            )
        if self.confidence_missing:
            out.append(
                "confidence.jsonl row not updated for: " + ", ".join(self.confidence_missing)
            )
        if self.bad_confidence_rows:
            out.append("confidence.jsonl has a line that is not valid JSON")
        return out


def parse_etime(text: str) -> timedelta | None:
    """`ps` elapsed time: [[dd-]hh:]mm:ss."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text.strip())
    if not m:
        return None
    d, h, mi, sec = (int(g or 0) for g in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=sec)


def in_reconcile_window(now: datetime) -> bool:
    """True on Sunday around the launchd graph-reconcile run (09:50-11:00 local)."""
    if now.weekday() != RECONCILE_WEEKDAY:
        return False
    hm = (now.hour, now.minute)
    return RECONCILE_START <= hm < RECONCILE_END


class MemoryStore:
    def __init__(
        self,
        memory_dir: Path | str | None = None,
        *,
        now: Callable[[], datetime] = datetime.now,
        run: Runner | None = None,
        pid_alive: Callable[[int], bool] | None = None,
    ):
        self.dir = Path(memory_dir or os.environ.get("ORCHESTRATOR_MEMORY_DIR", DEFAULT_MEMORY_DIR))
        self.dir = self.dir.expanduser()
        self.now = now
        self.run = run or run_command
        self._pid_alive = pid_alive or _pid_alive

    # -- paths -------------------------------------------------------------------------------

    @property
    def graph_path(self) -> Path:
        return self.dir / "memory.jsonl"

    @property
    def confidence_path(self) -> Path:
        return self.dir / "confidence.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.dir / LOCK_DIR_NAME

    @property
    def backup_root(self) -> Path:
        return self.dir / BACKUP_DIR_NAME

    @property
    def pending_dir(self) -> Path:
        return self.dir / "logs"

    # -- read --------------------------------------------------------------------------------

    def read_entities(self) -> dict[str, Entity]:
        """Parse the JSONL graph. Raises ValueError on a corrupt line (health check uses this)."""
        entities: dict[str, Entity] = {}
        with self.graph_path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{self.graph_path.name}:{n}: invalid JSON ({exc.msg})"
                    ) from None
                if isinstance(rec, dict) and rec.get("type") == "entity" and "name" in rec:
                    entities[str(rec["name"])] = Entity(
                        str(rec["name"]),
                        str(rec.get("entityType", "")),
                        tuple(str(o) for o in rec.get("observations", [])),
                    )
        return entities

    def open_nodes(self, names: Sequence[str]) -> tuple[list[Entity], list[str]]:
        """Exact-name lookup (the MCP `open_nodes` semantics). Returns (found, missing)."""
        entities = self.read_entities()
        found = [entities[n] for n in names if n in entities]
        missing = [n for n in names if n not in entities]
        return found, missing

    def render_context(self, names: Sequence[str], max_chars: int = 16000) -> str:
        """Prompt section for `memory_entities`; missing names are listed, never searched for."""
        if not names:
            return ""
        found, missing = self.open_nodes(names)
        parts = [e.render() for e in found]
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n... (context truncated)"
        if missing:
            text += "\n\n(entities not found in the graph: " + ", ".join(missing) + ")"
        return text

    # -- health ------------------------------------------------------------------------------

    def stale_processes(self) -> list[str]:
        """Orphaned (ppid 1) memory-server processes older than an hour (leaked by earlier runs)."""
        res = self.run(["ps", "-axo", "pid=,ppid=,etime=,command="], timeout=15)
        if res.returncode != 0:
            return []
        stale: list[str] = []
        for line in res.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4 or "server-memory" not in parts[3]:
                continue
            age = parse_etime(parts[2])
            if parts[1] == "1" and age is not None and age >= STALE_PROCESS_AGE:
                stale.append(f"pid {parts[0]} ({parts[2]})")
        return stale

    def health(self) -> Health:
        if not self.graph_path.is_file():
            return Health(False, f"{self.graph_path} not found")
        try:
            count = len(self.read_entities())
        except (OSError, ValueError) as exc:
            return Health(False, f"graph unreadable: {exc}")
        stale = self.stale_processes()
        if stale:
            return Health(False, f"{len(stale)} leaked memory-server process(es): {stale[0]}")
        if self.lock_path.exists() and self._lock_owner_pid() is None:
            return Health(False, f"{LOCK_DIR_NAME} is held by another process (reconcile?)")
        if in_reconcile_window(self.now()):
            return Health(False, "inside the Sunday graph-reconcile window (09:50-11:00)")
        return Health(True, f"{count} entities readable, no lock, no leaked processes")

    # -- lock / backup -----------------------------------------------------------------------

    def _lock_owner_pid(self) -> int | None:
        """PID in OUR lock's owner file; None if the lock is not ours (e.g. reconcile's)."""
        try:
            return int((self.lock_path / "orchestrator.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    @contextlib.contextmanager
    def write_window(self) -> Iterator[Path]:
        """Hold the shared lock while the verify agent may write the graph; back up first."""
        if in_reconcile_window(self.now()):
            raise MemoryBusy("inside the Sunday graph-reconcile window")
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            owner = self._lock_owner_pid()
            if owner is not None and not self._pid_alive(owner):
                shutil.rmtree(self.lock_path, ignore_errors=True)  # our own leftover
        try:
            self.lock_path.mkdir()
        except FileExistsError:
            raise MemoryBusy(f"{LOCK_DIR_NAME} is held (reconcile or another run)") from None
        try:
            (self.lock_path / "orchestrator.pid").write_text(str(os.getpid()))
            yield self.backup()
        finally:
            shutil.rmtree(self.lock_path, ignore_errors=True)

    def backup(self) -> Path:
        stamp = self.now().strftime("%Y-%m-%d-%H%M%S")
        dest = self.backup_root / stamp
        dest.mkdir(parents=True, exist_ok=True)
        for path in (self.graph_path, self.confidence_path):
            if path.is_file():
                shutil.copy2(path, dest / path.name)
        olds = sorted(p for p in self.backup_root.iterdir() if p.is_dir())
        for old in olds[:-KEEP_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)
        return dest

    # -- audit -------------------------------------------------------------------------------

    def state(self) -> GraphState:
        entities = {n: e.fingerprint() for n, e in self.read_entities().items()}
        confidence: dict[str, str] = {}
        bad = False
        if self.confidence_path.is_file():
            for line in self.confidence_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    bad = True
                    continue
                if isinstance(row, dict) and "entity" in row:
                    confidence[str(row["entity"])] = json.dumps(row, sort_keys=True)
        if bad:
            confidence["\0bad"] = "1"
        return GraphState(entities, confidence)

    @staticmethod
    def audit(before: GraphState, after: GraphState) -> GraphAudit:
        added = sorted(set(after.entities) - set(before.entities))
        removed = sorted(set(before.entities) - set(after.entities))
        changed = sorted(
            n for n in set(after.entities) & set(before.entities)
            if after.entities[n] != before.entities[n]
        )  # fmt: skip
        touched = [*added, *changed]
        missing = [n for n in touched if after.confidence.get(n) == before.confidence.get(n)]
        return GraphAudit(
            added=tuple(added),
            changed=tuple(changed),
            removed=tuple(removed),
            protected_touched=tuple(n for n in (*touched, *removed) if n in PROTECTED_ENTITIES),
            confidence_missing=tuple(missing),
            bad_confidence_rows="\0bad" in after.confidence,
        )

    # -- pending (ask first / graph unavailable) ---------------------------------------------

    def write_pending(self, title: str, body: str) -> Path:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "note"
        path = self.pending_dir / f"pending-{self.now().strftime('%Y-%m-%d-%H%M%S')}-{slug}.md"
        path.write_text(f"# {title}\n\n{body.strip()}\n", encoding="utf-8")
        return path


@dataclass
class MemoryPlan:
    """What the flow decided about the graph for one session."""

    context: str = ""
    degraded_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.degraded_reason is None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
