"""Test kit for the session flow: a toy pipeline, a scripted Agent SDK, a routing subprocess fake.

Everything here runs offline. Real git/bash run for snapshots and test commands (they act on the
throwaway toy repo); `codex` and the develop2 post-step commands are faked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from orchestrator.config import Config, load_config
from orchestrator.flow import FlowDeps, FlowOptions
from orchestrator.memory import MemoryStore
from orchestrator.pipeline import Pipeline, load_pipeline
from orchestrator.preflight import CmdResult, run_command
from orchestrator.state import StateDB

MON = datetime(2026, 10, 26, 14, 0)
FIXED_CALC = (
    "def add(a, b):\n    return a + b\n\n\n"
    "def divide(a, b):\n    if b == 0:\n        raise ValueError('division by zero')\n"
    "    return a / b\n"
)
GOOD_PLAN = {
    "files": ["calc.py", "plan/handoff-s1.md"],
    "commands": ["python3 test_calc.py"],
    "risky": False,
    "risks": [],
}
CODEX_OK_STREAM = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t-1"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"done"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":9}}',
    ]
)


def plan_text(plan: dict[str, Any] | None = None, prose: str = "My plan.") -> str:
    return f"{prose}\n\n```json\n{json.dumps(plan or GOOD_PLAN)}\n```"


def phase_of(prompt: str) -> str:
    for marker, name in (
        ("Step 1 of 5", "plan"),
        ("Step 2 of 5", "contract"),
        ("Step 3 of 5", "claude"),
        ("Step 4 of 5", "takeover"),
        ("Step 5 of 5", "verify"),
        ("## Repair round", "repair"),
    ):
        if marker in prompt:
            return name
    return "unknown"


def session_of(prompt: str) -> str:
    return "S2" if "TASK-S2" in prompt else "S1"


@dataclass
class Call:
    phase: str
    session: str
    prompt: str
    options: Any


class Agent:
    """Scripted `query()`: behaviour per (phase) via `handlers`, defaults make a happy toy run."""

    def __init__(self, repo: Path, plan_dir: str = "plan"):
        self.repo = repo
        self.plan_dir = plan_dir
        self.calls: list[Call] = []
        self.handlers: dict[str, Any] = {}
        self.init_data: dict[str, Any] | None = None

    def phases(self, session: str | None = None) -> list[str]:
        return [c.phase for c in self.calls if session in (None, c.session)]

    def count(self, phase: str) -> int:
        return self.phases().count(phase)

    # defaults ---------------------------------------------------------------------------------
    def default(self, phase: str, session: str, prompt: str, options: Any) -> str:
        handoff = self.repo / self.plan_dir / f"handoff-{session.lower()}.md"
        if phase == "plan":
            return plan_text()
        if phase == "contract":
            return "CONTRACT: notes/codex.md must exist and mention divide."
        if phase in ("claude", "repair"):
            if session == "S1":
                (self.repo / "calc.py").write_text(FIXED_CALC)
            else:
                (self.repo / "s2.txt").write_text("s2 work\n")
            return "implemented"
        if phase == "takeover":
            (self.repo / "notes").mkdir(exist_ok=True)
            (self.repo / "notes" / "codex.md").write_text("divide handled (by claude)\n")
            return "took over"
        if phase == "verify":
            handoff.write_text("STATUS: DONE\nall criteria met\n")
            return "verified"
        raise AssertionError(f"unexpected prompt:\n{prompt[:200]}")

    # SDK surface ------------------------------------------------------------------------------
    def __call__(self, *, prompt, options):
        return self._run(prompt, options)

    async def _run(self, prompt, options):
        async for first in prompt:
            text = first["message"]["content"]
            break
        phase, session = phase_of(text), session_of(text)
        call = Call(phase, session, text, options)
        self.calls.append(call)
        n = len(self.calls)
        sid = f"sess-{n}"
        if self.init_data is not None:
            yield SystemMessage(subtype="init", data=self.init_data)
        handler = self.handlers.get(phase) or self.handlers.get((phase, session))
        if handler is None:
            out = self.default(phase, session, text, options)
        else:
            out = handler(self, call)
            if asyncio.iscoroutine(out):
                out = await out
            if out is DEFAULT:
                out = self.default(phase, session, text, options)
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, list):  # explicit messages
            for m in out:
                yield m
            return
        yield AssistantMessage(content=[TextBlock(text=out)], model="m", session_id=sid)
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
            session_id=sid, total_cost_usd=0.05, result=out,
        )  # fmt: skip


DEFAULT = object()  # handler return value: "do the default, then I add my own side effects"


class Router:
    """Subprocess fake: codex and the develop2 commands are scripted, everything else is real."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.codex_calls: list[dict[str, Any]] = []
        self.codex_script: list[Any] = []  # callable(repo, prompt) -> CmdResult | CmdResult
        self.shell_calls: list[str] = []
        self.shell: dict[str, Any] = {}  # substring of the command -> CmdResult | callable | list

    def __call__(self, cmd, *, timeout, cwd=None, env=None, input_text=None):
        if cmd[0] == "codex":
            self.codex_calls.append({"cmd": cmd, "input": input_text, "cwd": cwd})
            step = self.codex_script.pop(0) if self.codex_script else self.default_codex
            return step(self.repo, input_text) if callable(step) else step
        if cmd[:2] == ["bash", "-c"]:
            for key, reply in self.shell.items():
                if key in cmd[2]:
                    self.shell_calls.append(cmd[2])
                    if isinstance(reply, list):
                        reply = reply.pop(0)
                    return reply() if callable(reply) else reply
        return run_command(cmd, timeout=timeout, cwd=cwd, env=env, input_text=input_text)

    @staticmethod
    def default_codex(repo: Path, prompt: str) -> CmdResult:
        (repo / "notes").mkdir(exist_ok=True)
        (repo / "notes" / "codex.md").write_text("divide handled\n")
        return CmdResult(0, CODEX_OK_STREAM, "")


@dataclass
class World:
    repo: Path
    pipeline: Pipeline
    config: Config
    db: StateDB
    agent: Agent
    router: Router
    deps: FlowDeps
    mem_dir: Path
    tmp: Path
    yaml_path: Path
    options: FlowOptions = field(default_factory=FlowOptions)

    def flow(self, **opts):
        from orchestrator.flow import Flow

        return Flow(
            self.pipeline, self.config, self.db, self.deps,
            dataclasses.replace(self.options, **opts) if opts else self.options,
        )  # fmt: skip

    def reload(self, **raw_updates) -> None:
        """Rewrite the pipeline yaml with top-level changes and reload it."""
        raw = yaml.safe_load(self.yaml_path.read_text())
        raw.update(raw_updates)
        self.yaml_path.write_text(yaml.safe_dump(raw))
        self.pipeline = load_pipeline(self.yaml_path)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )  # fmt: skip


def build_world(toy_repo: Path, tmp_path: Path, mem_dir: Path, *, requires_stage=False) -> World:
    plan = toy_repo / "plan"
    plan.mkdir()
    (plan / "claude-s1.md").write_text("TASK-S1: make calc.divide(1, 0) raise ValueError.\n")
    (plan / "codex-s1.md").write_text("Create notes/codex.md mentioning divide.\n")
    (plan / "claude-s2.md").write_text("TASK-S2: write s2.txt.\n")
    _git(toy_repo, "add", "-A")
    _git(toy_repo, "commit", "-q", "-m", "plan files")

    raw = {
        "name": "toy-flow",
        "repo": str(toy_repo),
        "plan_dir": "plan",
        "requires_stage": requires_stage,
        "budget_usd": {"per_call": 2, "per_session": 10, "total": 100},
        "sessions": [
            {
                "id": "S1", "claude_task": "claude-s1.md", "codex_task": "codex-s1.md",
                "handoff": "handoff-s1.md", "codex_allowlist": ["notes/*.md"],
                "tests": ["python3 test_calc.py"], "memory_entities": ["Agent Orchestrator"],
            },
            {
                "id": "S2", "claude_task": "claude-s2.md", "codex_task": None,
                "handoff": "handoff-s2.md", "depends_on": ["S1"],
                "tests": ["test -f s2.txt"],
            },
        ],
    }  # fmt: skip
    yaml_path = tmp_path / "toy-pipeline.yaml"
    yaml_path.write_text(yaml.safe_dump(raw))
    pipeline = load_pipeline(yaml_path)

    config = dataclasses.replace(
        load_config(),
        checkpoint_dir=tmp_path / "checkpoints",
        log_dir=tmp_path / "logs",
        call_timeout_s=120,
        state_db=tmp_path / "flow.db",
    )
    db = StateDB(config.state_db)
    agent, router = Agent(toy_repo), Router(toy_repo)

    async def no_sleep(_seconds: float) -> None:
        return None

    deps = FlowDeps(
        memory=MemoryStore(
            mem_dir, now=lambda: MON, run=lambda cmd, **kw: CmdResult(0, "", ""),
            pid_alive=lambda pid: True,
        ),
        sdk_env={},
        query_fn=agent,
        runner=router,
        sleep=no_sleep,
        preflight=lambda p: [],
    )  # fmt: skip
    opts = FlowOptions(setting_sources=[], strict_mcp_config=True)
    return World(
        toy_repo, pipeline, config, db, agent, router, deps, mem_dir, tmp_path, yaml_path, opts
    )


async def hook_decision(call: Call, tool: str, tool_input: dict[str, Any], tid: str) -> str:
    """'allow' or 'deny' for a tool call run through the PreToolUse hook the phase started with."""
    hook = call.options.hooks["PreToolUse"][0].hooks[0]
    out = await hook({"tool_name": tool, "tool_input": tool_input}, tid, None)
    return "deny" if out else "allow"
