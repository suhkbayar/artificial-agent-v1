"""REAL Agent SDK + Codex on the toy repo (opt-in: ORCHESTRATOR_INTEGRATION=1; costs money).

Only a throwaway copy of tests/fixtures/toy-repo is touched, with a hard budget, a tmp COPY of the
memory graph and `strict_mcp_config` so no real MCP server (and so not the real graph) is loaded.
The API key comes from the project-root `.env.local` via the production loader; it is never printed.
"""

import os
import sys
from pathlib import Path

import pytest

from orchestrator.config import Budget
from orchestrator.envfile import SCRUB_SUBPROCESS_ENV, sdk_env
from orchestrator.flow import FlowOptions, run_pipeline
from orchestrator.policy import PolicyContext
from orchestrator.preflight import run_preflight
from orchestrator.runner import StepRequest, StepStatus, run_step
from orchestrator.state import StateDB
from tests.flowkit import _git, build_world

PROJECT_ROOT = Path(__file__).resolve().parent.parent
real = pytest.mark.skipif(
    os.environ.get("ORCHESTRATOR_INTEGRATION") != "1",
    reason="real SDK/Codex calls cost money; set ORCHESTRATOR_INTEGRATION=1",
)
pytestmark = [real, pytest.mark.integration]

BUDGET = Budget(per_call=0.60, per_session=1.50, total=3.0)
# Presence only, never a value. The fake credential-class variable stands in for the real key: a
# real key cannot be used while the API account has no credit (see the handoff).
PROBE = """\
import os
print("CRED_VAR_VISIBLE=%s PLAIN_VAR_VISIBLE=%s" % (
    "AWS_SECRET_ACCESS_KEY" in os.environ, "ORCH_PLAIN_VAR" in os.environ))
"""
FAKE_ENV = {"AWS_SECRET_ACCESS_KEY": "fake-not-a-secret-0123456789", "ORCH_PLAIN_VAR": "plain"}


def auth_env() -> dict[str, str]:
    """Production loader by default; ORCHESTRATOR_TEST_AUTH=login runs on the claude.ai login."""
    if os.environ.get("ORCHESTRATOR_TEST_AUTH") == "login":
        return {}  # no key, so (like production) no scrub flag: auto/plan modes stay effective
    return sdk_env(PROJECT_ROOT)


@pytest.fixture
def probe(toy_repo, tmp_path):
    db = StateDB(tmp_path / "real.db")
    run_id = db.start_run("real-probe")
    step = db.ensure_step(run_id, "S1", 3)
    (toy_repo / "probe_env.py").write_text(PROBE)
    yield db, run_id, step.id, toy_repo
    db.close()


def request(repo, prompt, env, **kw):
    return StepRequest(
        prompt=prompt, cwd=repo, policy=PolicyContext(repo=repo), timeout_s=300, env=env, **kw
    )


def event_details(db, run_id, kind):
    return [e.detail for e in db.events(run_id, kind=kind)]


async def test_real_init_message_reports_mode_key_source_and_mcp_servers(probe):
    """Answers: which permission mode is EFFECTIVE, where the credential came from, which MCP
    servers start headless (user settings + user MCP config load: setting_sources=None)."""
    db, run_id, step_id, repo = probe
    env = auth_env()
    req = request(repo, "Reply with the single word ok. Use no tools.", env, max_turns=2)
    res = await run_step(req, db=db, step_id=step_id, budget=BUDGET)
    inits = event_details(db, run_id, "session_init")
    print(
        "\nSTATUS:",
        res.status,
        "COST:",
        res.cost_usd,
        "HAS_KEY_IN_ENV:",
        "ANTHROPIC_API_KEY" in env,
    )
    print("INIT:", inits)
    print("NOTICES:", event_details(db, run_id, "notice"))
    assert res.status is StepStatus.DONE, res.reason
    assert inits, "no system/init message was seen"


async def test_real_bash_environment_with_and_without_the_scrub_flag(probe):
    db, run_id, step_id, repo = probe
    results = {}
    for label, env in (
        ("scrub", {**FAKE_ENV, SCRUB_SUBPROCESS_ENV: "1"}),
        ("noscrub", dict(FAKE_ENV)),
    ):
        step = db.ensure_step(run_id, label, 3)
        req = request(
            repo,
            "Run exactly `python3 probe_env.py` with the Bash tool and reply with its one output "
            "line verbatim. Nothing else.",
            env,
            max_turns=4,
            setting_sources=[],
            strict_mcp_config=True,
        )
        res = await run_step(req, db=db, step_id=step.id, budget=BUDGET)
        results[label] = (res.status.value, (res.output or "").strip()[:80])
    print("\nPROBE RESULTS:", results)
    assert "CRED_VAR_VISIBLE=False" in results["scrub"][1], results
    assert "PLAIN_VAR_VISIBLE=True" in results["scrub"][1], results


async def test_real_agent_cannot_dump_the_environment(probe):
    db, run_id, step_id, repo = probe
    env = auth_env()
    req = request(
        repo,
        "Run the shell command `env` with the Bash tool and tell me what happened. If it is "
        "refused, do not try other ways; just report the refusal.",
        env,
        max_turns=4,
        setting_sources=[],
        strict_mcp_config=True,
    )
    res = await run_step(req, db=db, step_id=step_id, budget=BUDGET)
    denies = event_details(db, run_id, "policy_deny")
    print("\nSTATUS:", res.status, "DENIES:", [d[:100] for d in denies])
    assert any("env" in d for d in denies)
    key = env.get("ANTHROPIC_API_KEY")
    if key:
        assert key not in (res.output or "")


# ---- the whole flow, real Claude + real Codex, toy repo only -----------------------------------

CLAUDE_TASK = """\
TASK-S1: `calc.divide(a, b)` raises ZeroDivisionError when b is 0. Make it raise
`ValueError("division by zero")` instead. Edit only `calc.py`. Run `python3 test_calc.py`; it must
print `ok`. Do not use git.
"""
CODEX_TASK = """\
Create the file `notes/divide.md` (new) with exactly two short lines: a title line `# divide` and a
line saying that divide(a, 0) raises ValueError("division by zero"). Change no other file.
Verification: `test -f notes/divide.md`.
"""


async def test_real_full_flow_on_the_toy_repo_with_codex(toy_repo, tmp_path, mem_dir):
    w = build_world(toy_repo, tmp_path, mem_dir)
    (toy_repo / "plan" / "claude-s1.md").write_text(CLAUDE_TASK)
    (toy_repo / "plan" / "codex-s1.md").write_text(CODEX_TASK)
    _git(toy_repo, "add", "-A")
    _git(toy_repo, "commit", "-q", "-m", "real tasks")
    import yaml

    raw = yaml.safe_load(w.yaml_path.read_text())
    raw["budget_usd"] = {"per_call": BUDGET.per_call, "per_session": 2.5, "total": 4.0}
    raw["sessions"][0]["codex_allowlist"] = ["notes/*.md"]
    raw["sessions"][0]["tests"] = ["python3 test_calc.py", "test -f notes/divide.md"]
    raw["sessions"][0]["memory_entities"] = []
    w.yaml_path.write_text(yaml.safe_dump(raw))
    from orchestrator.pipeline import load_pipeline

    pipeline = load_pipeline(w.yaml_path)
    w.deps.query_fn = None  # the real SDK
    w.deps.runner = None  # real git/bash/codex
    w.deps.sdk_env = auth_env()
    w.deps.preflight = lambda p: run_preflight(p, w.config)  # the real preflight, toy flavour
    opts = FlowOptions(only="S1", setting_sources=[], strict_mcp_config=True, max_turns=40)
    out = await run_pipeline(pipeline, w.config, w.deps, opts, db=w.db)

    run = w.db.latest_run()
    print("\nOUTCOME:", out)
    for s in w.db.steps_for_run(run.id) if run else []:
        print("STEP", s.session_id, s.phase, s.status, f"${s.cost_usd:.3f}", s.claude_session_id,
              (s.last_error or "")[:100])  # fmt: skip
    for e in w.db.events(run.id if run else None):
        if e.kind in ("session_init", "codex_call", "gate", "notice", "blocker", "policy_deny",
                      "codex_fallback"):  # fmt: skip
            print("EVENT", e.kind, (e.detail or "")[:300])
    print("TOTAL COST:", run.total_cost_usd if run else None)
    sys.stdout.flush()
    assert out.status == "done", out
    assert (toy_repo / "notes" / "divide.md").exists()
    assert (toy_repo / "plan" / "handoff-s1.md").read_text().lstrip().startswith("STATUS: DONE")
    assert run.total_cost_usd <= 4.0
