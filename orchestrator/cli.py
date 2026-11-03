"""`uv run orchestrator <command>`: preflight, plan, status, run [--only S1], resume."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import click

from orchestrator.config import Config, load_config, project_root
from orchestrator.envfile import EnvFileError, sdk_env
from orchestrator.flow import FlowDeps, FlowOptions, RunOutcome, run_pipeline
from orchestrator.memory import MemoryStore
from orchestrator.pipeline import POST_STEPS, Pipeline, PipelineError, load_pipeline
from orchestrator.preflight import CheckResult, all_ok, run_preflight
from orchestrator.state import StateDB


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    out = [line(headers), line(["-" * w for w in widths])]
    out.extend(line(r) for r in rows)
    return "\n".join(out)


def _load(pipeline_path: str, *, check_files: bool = True) -> Pipeline:
    try:
        return load_pipeline(pipeline_path, check_files=check_files)
    except PipelineError as exc:
        raise click.ClickException(f"invalid pipeline {pipeline_path}:\n{exc}") from exc


def render_preflight(results: Sequence[CheckResult]) -> str:
    rows = [("PASS" if r.ok else "FAIL", r.name, r.detail) for r in results]
    text = format_table(("RESULT", "CHECK", "DETAIL"), rows)
    hints = [f"  - {r.name}: {r.fix_hint}" for r in results if not r.ok and r.fix_hint]
    if hints:
        text += "\n\nFix hints:\n" + "\n".join(hints)
    return text


def render_plan(pipeline: Pipeline) -> str:
    b = pipeline.budget
    lines = [
        f"pipeline: {pipeline.name}",
        f"repo:     {pipeline.repo}",
        f"plan_dir: {pipeline.plan_dir}",
        f"budget:   ${b.per_call:g}/call  ${b.per_session:g}/session  ${b.total:g} total",
        "",
    ]
    for n, s in enumerate(pipeline.order(), 1):
        deps = ", ".join(s.depends_on) or "-"
        lines.append(f"{n}. {s.id}   depends_on: {deps}")
        lines.append(f"   claude_task: {s.claude_task}")
        lines.append(f"   codex_task:  {s.codex_task or '-'}")
        lines.append(f"   handoff:     {s.handoff}")
        lines.append(f"   post_steps:  {', '.join(s.post_steps) or '-'}")
        lines.append(f"   memory:      {', '.join(s.memory_entities) or '-'}")
        lines.append(f"   schema_allowlist ({len(s.schema_allowlist)}):")
        lines.extend(f"     {g}" for g in s.schema_allowlist)
        lines.append(f"   codex_allowlist ({len(s.codex_allowlist)}):")
        lines.extend(f"     {g}" for g in s.codex_allowlist)
        if s.known_red:
            lines.append(f"   known_red ({len(s.known_red)}, not gated):")
            lines.extend(f"     {c}" for c, _ in s.known_red)
        lines.append(f"   tests ({len(s.tests)}):")
        for t in s.tests:
            first, *rest = t.splitlines()
            lines.append(f"     {first}" + (" ..." if rest else ""))
        lines.append("")
    todos = pipeline.todos()
    if todos:
        lines.append(f"TODO ({len(todos)}):")
        lines.extend(f"  {t}" for t in todos)
    return "\n".join(lines).rstrip()


def render_status(db: StateDB, config: Config, pipeline_name: str | None) -> str:
    run = db.latest_run(pipeline_name)
    if run is None:
        return "no runs recorded"
    b = config.budget
    lines = [
        f"run {run.id}: {run.pipeline}  status={run.status}  started={run.started_at}",
        f"cost: ${run.total_cost_usd:.2f} of ${b.total:g}",
        "",
    ]
    steps = db.steps_for_run(run.id)
    if steps:
        rows = [
            (
                s.session_id,
                str(s.phase),
                s.status,
                str(s.attempts),
                f"{s.cost_usd:.2f}",
                s.claude_session_id or "-",
                (s.last_error or "-")[:60],
            )
            for s in steps
        ]
        lines.append(
            format_table(
                ("SESSION", "PHASE", "STATUS", "TRIES", "USD", "CLAUDE SESSION", "LAST ERROR"), rows
            )
        )
    else:
        lines.append("no steps yet")
    lines.append(f"\ndecisions: {db.decision_count(run.id)}  events: {len(db.events(run.id))}")
    return "\n".join(lines)


@click.group()
def main() -> None:
    """Agent Orchestrator."""


@main.command()
@click.option(
    "--pipeline",
    "pipeline_path",
    required=True,
    type=click.Path(dir_okay=False),
    help="Pipeline yaml, e.g. pipelines/qinno-rbac-v2.yaml",
)
def preflight(pipeline_path: str) -> None:
    """Run every preflight check; exit 1 if any fails (do not start)."""
    pipeline = _load(pipeline_path, check_files=False)
    results = run_preflight(pipeline, load_config())
    click.echo(render_preflight(results))
    if not all_ok(results):
        click.echo("\nPREFLIGHT FAILED: do not start.", err=True)
        sys.exit(1)
    click.echo("\nPREFLIGHT OK")


@main.command()
@click.argument("pipeline_path", type=click.Path(dir_okay=False))
def plan(pipeline_path: str) -> None:
    """Print the session order, files, allowlists and tests of a pipeline."""
    click.echo(render_plan(_load(pipeline_path)))


@main.command()
@click.option("--pipeline", "pipeline_name", default=None, help="Filter by pipeline name.")
def status(pipeline_name: str | None) -> None:
    """Show the latest run recorded in state.db."""
    config = load_config()
    if not Path(config.state_db).exists():
        click.echo("no runs recorded (state.db does not exist yet)")
        return
    with StateDB(config.state_db) as db:
        click.echo(render_status(db, config, pipeline_name))


@main.command()
@click.option("--port", default=8765, show_default=True, help="Loopback port.")
def watch(port: int) -> None:
    """Live view of state.db at http://127.0.0.1:<port>; its only write is the post-step OK."""
    from orchestrator.watch import make_server

    config = load_config()
    server = make_server(config.state_db, port)
    click.echo(f"watching {config.state_db} at http://127.0.0.1:{port} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@main.command("post-steps")
def post_steps() -> None:
    """List the orchestrator-run post steps a pipeline may name."""
    rows = [(n, d.stage, d.command) for n, d in sorted(POST_STEPS.items())]
    click.echo(format_table(("NAME", "STAGE", "COMMAND"), rows))


# ---- run / resume ------------------------------------------------------------------------------


@contextmanager
def caffeinate() -> Iterator[None]:
    """Keep the Mac awake for the life of this process (`caffeinate -i -w <pid>`)."""
    proc = None
    if shutil.which("caffeinate"):
        proc = subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
    try:
        yield
    finally:
        if proc is not None:
            proc.terminate()


def build_deps() -> FlowDeps:
    """Real dependencies. The API key (if `.env.local` has one) goes into `sdk_env` only."""
    try:
        env = sdk_env(project_root())
    except EnvFileError as exc:
        raise click.ClickException(str(exc)) from exc
    return FlowDeps(memory=MemoryStore(), sdk_env=env)


def render_outcome(outcome: RunOutcome) -> str:
    lines = [f"run {outcome.run_id}: {outcome.status}"]
    for s in outcome.sessions:
        extra = f"  {s.reason}" if s.reason else ""
        lines.append(f"  {s.session_id}: {s.status}{extra}")
    if outcome.reason and not outcome.sessions:
        lines.append(f"  {outcome.reason}")
    return "\n".join(lines)


def _finish(outcome: RunOutcome) -> None:
    click.echo(render_outcome(outcome))
    if outcome.status == "preflight_failed":
        click.echo("\nPREFLIGHT FAILED: nothing was started.", err=True)
        sys.exit(1)
    if outcome.status != "done":
        sys.exit(2)


@main.command()
@click.argument("pipeline_path", type=click.Path(dir_okay=False))
@click.option("--only", "only", default=None, help="Run just this session id, e.g. S1.")
@click.option(
    "--model",
    default=None,
    help="Claude model for every agent phase, e.g. claude-sonnet-5-5 (default: the CLI's own). "
    "Codex is not affected.",
)
def run(pipeline_path: str, only: str | None, model: str | None) -> None:
    """Preflight, then run the pipeline (every session, or just --only S1)."""
    pipeline = _load(pipeline_path)
    config = load_config()
    options = FlowOptions(only=only, model=model)
    with caffeinate():
        outcome = asyncio.run(run_pipeline(pipeline, config, build_deps(), options))
    _finish(outcome)


@main.command()
@click.option(
    "--pipeline", "pipeline_name", default=None, help="Resume the latest run of this one."
)
@click.option(
    "--retry",
    is_flag=True,
    help="Also retry steps that ended blocked/failed (after you fixed the cause).",
)
@click.option(
    "--model",
    default=None,
    help="Claude model for every agent phase, e.g. claude-sonnet-5-5 (default: the CLI's own). "
    "Codex is not affected.",
)
def resume(pipeline_name: str | None, retry: bool, model: str | None) -> None:
    """Continue the latest unfinished run from its last unfinished step."""
    config = load_config()
    if not Path(config.state_db).exists():
        raise click.ClickException("no runs recorded (state.db does not exist yet)")
    with StateDB(config.state_db) as db:
        run_row = db.latest_run(pipeline_name)
        if run_row is None:
            raise click.ClickException("no runs recorded")
        if run_row.status == "done":
            click.echo(f"run {run_row.id} ({run_row.pipeline}) is already done; nothing to resume")
            return
        if not run_row.pipeline_path:
            raise click.ClickException(f"run {run_row.id} has no pipeline path recorded")
        only = None
        for e in db.events(run_row.id, "run_start"):
            only = (json.loads(e.detail or "{}") or {}).get("only")
        pipeline = _load(run_row.pipeline_path)
        with caffeinate():
            outcome = asyncio.run(
                run_pipeline(
                    pipeline,
                    config,
                    build_deps(),
                    FlowOptions(only=only, retry=retry, model=model),
                    run_id=run_row.id,
                    db=db,
                )  # fmt: skip
            )
    _finish(outcome)
