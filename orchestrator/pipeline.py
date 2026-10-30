"""Pipeline definition: yaml (contract §2) -> Pipeline / SessionSpec -> topological order.

The yaml is data. Values that the task files do not state are left as `TODO ...` strings; they
are reported by `Pipeline.todos()` instead of being guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from orchestrator.config import DEFAULT_BUDGET_USD, Budget
from orchestrator.policy import PolicyConfig, PolicyContext

# Orchestrator-run steps that touch a shared database (O4). Agents never run these; the
# orchestrator does, on develop2 only, after re-running the stage preflight checks.
# `run_post_step` itself lands in S3; S1 only defines the names so a pipeline can be validated.
REQUIRED_STAGE = "develop2"


@dataclass(frozen=True)
class PostStepDef:
    description: str
    command: str  # run from the pipeline repo root by the orchestrator, never by an agent
    stage: str = REQUIRED_STAGE


POST_STEPS: dict[str, PostStepDef] = {
    "drizzle_push_develop2": PostStepDef(
        "drizzle-kit push to develop2 (migrate-service `migrate` runs sync-secrets first)",
        "yarn workspace @qinno/migrate-service migrate",
    ),
    "seed_manager_develop2": PostStepDef(
        "per-system manager seed on develop2 (never the full `seed`)",
        "yarn workspace @qinno/migrate-service seed:manager",
    ),
    "seed_finance_develop2": PostStepDef(
        "per-system finance seed on develop2",
        "yarn workspace @qinno/migrate-service seed:finance",
    ),
    "seed_customer_develop2": PostStepDef(
        "per-system customer seed on develop2",
        "yarn workspace @qinno/migrate-service seed:customer",
    ),
    "seed_merchant_develop2": PostStepDef(
        "per-system merchant seed on develop2; run twice, 2nd run must be 0 added / 0 removed",
        "yarn workspace @qinno/migrate-service seed:merchant",
    ),
}


class PipelineError(ValueError):
    """The pipeline yaml or its plan directory is invalid. Message lists every problem."""


@dataclass(frozen=True)
class SessionSpec:
    id: str
    claude_task: str
    codex_task: str | None
    handoff: str
    depends_on: tuple[str, ...] = ()
    codex_allowlist: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    post_steps: tuple[str, ...] = ()
    memory_entities: tuple[str, ...] = ()
    # Schema files this session's Claude track may change (policy otherwise hard-stops on any
    # schema glob). Values come from the task file's ownership block, never guessed.
    schema_allowlist: tuple[str, ...] = ()
    # Extra deny entries for this session only (see PolicyConfig.tightened: can never loosen).
    policy: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Pipeline:
    name: str
    repo: Path
    plan_dir: str
    budget: Budget
    sessions: tuple[SessionSpec, ...]
    source: Path | None = field(default=None, compare=False)
    # Pipeline-wide extra deny entries, applied to every session on top of the defaults.
    policy: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # False for pipelines that do not touch a shared stage (the toy repo): skips the DB_NAME /
    # .sst/stage / AWS preflight checks and forbids post_steps.
    requires_stage: bool = True
    # Free-text placeholders the plan files left open; reported by `todos()`.
    open_items: tuple[str, ...] = ()

    @property
    def plan_path(self) -> Path:
        return self.repo / self.plan_dir

    def session(self, session_id: str) -> SessionSpec:
        for s in self.sessions:
            if s.id == session_id:
                return s
        raise KeyError(session_id)

    def order(self) -> list[SessionSpec]:
        """Dependency order; ties keep declaration order. Cycles raise PipelineError."""
        by_id = {s.id: s for s in self.sessions}
        remaining = {s.id: set(s.depends_on) for s in self.sessions}
        ordered: list[SessionSpec] = []
        while remaining:
            ready = [sid for sid in by_id if sid in remaining and not remaining[sid]]
            if not ready:
                cycle = ", ".join(sorted(remaining))
                raise PipelineError(f"depends_on cycle among sessions: {cycle}")
            sid = ready[0]
            ordered.append(by_id[sid])
            del remaining[sid]
            for deps in remaining.values():
                deps.discard(sid)
        return ordered

    def policy_config(self, session: SessionSpec) -> PolicyConfig:
        """Defaults + pipeline tightening + session tightening. Only ever stricter."""
        cfg = PolicyConfig.default()
        for extra in (self.policy, session.policy):
            if extra:
                cfg = cfg.tightened(extra)
        return cfg

    def policy_context(self, session: SessionSpec) -> PolicyContext:
        return PolicyContext(
            repo=self.repo,
            config=self.policy_config(session),
            allowed_schema_globs=session.schema_allowlist,
        )

    def todos(self) -> list[str]:
        """Every field whose value is a `TODO ...` placeholder, as `S3.codex_allowlist: ...`."""
        found: list[str] = [f"pipeline: {item}" for item in self.open_items]
        for s in self.sessions:
            for name, value in (
                ("claude_task", s.claude_task),
                ("codex_task", s.codex_task),
                ("handoff", s.handoff),
                *[("codex_allowlist", v) for v in s.codex_allowlist],
                *[("tests", v) for v in s.tests],
                *[("memory_entities", v) for v in s.memory_entities],
                *[("schema_allowlist", v) for v in s.schema_allowlist],
            ):
                if isinstance(value, str) and value.strip().upper().startswith("TODO"):
                    found.append(f"{s.id}.{name}: {value}")
        return found


def _as_str_tuple(value: object, where: str, errors: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where}: expected a list of strings")
        return ()
    return tuple(value)


def _parse_policy(raw: object, where: str, errors: list[str]) -> dict[str, tuple[str, ...]]:
    """`policy:` mapping of deny-list additions; unknown (= loosening) keys are errors."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{where}: expected a mapping of deny-list additions")
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        items = _as_str_tuple(value, f"{where}.{key}", errors)
        out[str(key)] = items
    try:
        PolicyConfig.default().tightened(out)
    except ValueError as exc:
        errors.append(f"{where}: {exc}")
    return out


def _parse_budget(raw: object, errors: list[str]) -> Budget:
    values = dict(DEFAULT_BUDGET_USD)
    if raw is not None:
        if not isinstance(raw, dict):
            errors.append("budget_usd: expected a mapping per_call/per_session/total")
        else:
            for key, value in raw.items():
                if key not in values:
                    errors.append(f"budget_usd.{key}: unknown key")
                elif isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                    errors.append(f"budget_usd.{key}: must be a positive number, got {value!r}")
                else:
                    values[key] = float(value)
    return Budget(**values)


def load_pipeline(path: Path | str, *, check_files: bool = True) -> Pipeline:
    path = Path(path).expanduser()
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise PipelineError(f"pipeline file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise PipelineError(f"{path}: invalid yaml: {exc}") from exc
    if not isinstance(raw, dict):
        raise PipelineError(f"{path}: top level must be a mapping")

    errors: list[str] = []
    for key in ("name", "repo", "plan_dir", "sessions"):
        if key not in raw:
            errors.append(f"missing top-level key: {key}")
    if errors:
        raise PipelineError("\n".join(errors))

    budget = _parse_budget(raw.get("budget_usd"), errors)
    sessions: list[SessionSpec] = []
    seen: set[str] = set()
    raw_sessions = raw["sessions"]
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise PipelineError("sessions: expected a non-empty list")

    for i, item in enumerate(raw_sessions):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            errors.append(f"sessions[{i}]: needs a string `id`")
            continue
        sid = item["id"]
        where = f"session {sid}"
        if sid in seen:
            errors.append(f"{where}: duplicate session id")
            continue
        seen.add(sid)
        for key in ("claude_task", "handoff"):
            if not isinstance(item.get(key), str):
                errors.append(f"{where}: `{key}` must be a string")
        codex_task = item.get("codex_task")
        if codex_task is not None and not isinstance(codex_task, str):
            errors.append(f"{where}: `codex_task` must be a string or null")
        post_steps = _as_str_tuple(item.get("post_steps"), f"{where}.post_steps", errors)
        for step in post_steps:
            if step not in POST_STEPS:
                known = ", ".join(sorted(POST_STEPS))
                errors.append(f"{where}: unknown post_step `{step}` (known: {known})")
        schema_allowlist = _as_str_tuple(
            item.get("schema_allowlist"), f"{where}.schema_allowlist", errors
        )
        session_policy = _parse_policy(item.get("policy"), f"{where}.policy", errors)
        allowlist = _as_str_tuple(item.get("codex_allowlist"), f"{where}.codex_allowlist", errors)
        if codex_task is None and allowlist:
            errors.append(f"{where}: codex_allowlist set but codex_task is null")
        sessions.append(
            SessionSpec(
                id=sid,
                claude_task=str(item.get("claude_task", "")),
                codex_task=codex_task if isinstance(codex_task, str) else None,
                handoff=str(item.get("handoff", "")),
                depends_on=_as_str_tuple(item.get("depends_on"), f"{where}.depends_on", errors),
                codex_allowlist=allowlist,
                tests=_as_str_tuple(item.get("tests"), f"{where}.tests", errors),
                post_steps=post_steps,
                memory_entities=_as_str_tuple(
                    item.get("memory_entities"), f"{where}.memory_entities", errors
                ),
                schema_allowlist=schema_allowlist,
                policy=session_policy,
            )
        )

    for s in sessions:
        for dep in s.depends_on:
            if dep not in seen:
                errors.append(f"session {s.id}: depends_on unknown session `{dep}`")
            elif dep == s.id:
                errors.append(f"session {s.id}: depends on itself")

    requires_stage = raw.get("requires_stage", True)
    if not isinstance(requires_stage, bool):
        errors.append("requires_stage: expected true or false")
        requires_stage = True
    if not requires_stage:
        for s in sessions:
            if s.post_steps:
                errors.append(f"session {s.id}: post_steps need requires_stage: true")
    pipeline = Pipeline(
        name=str(raw["name"]),
        repo=Path(str(raw["repo"])).expanduser(),
        plan_dir=str(raw["plan_dir"]),
        budget=budget,
        sessions=tuple(sessions),
        source=path,
        policy=_parse_policy(raw.get("policy"), "policy", errors),
        requires_stage=requires_stage,
        open_items=_as_str_tuple(raw.get("open_items"), "open_items", errors),
    )

    if check_files:
        if not pipeline.plan_path.is_dir():
            errors.append(f"plan_dir not found: {pipeline.plan_path}")
        else:
            for s in sessions:
                for name in (s.claude_task, s.codex_task):
                    if name and not name.upper().startswith("TODO"):
                        if not (pipeline.plan_path / name).is_file():
                            errors.append(f"session {s.id}: task file missing: {name}")

    if errors:
        raise PipelineError("\n".join(errors))
    pipeline.order()  # raises PipelineError on a dependency cycle
    return pipeline
