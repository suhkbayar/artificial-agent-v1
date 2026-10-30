"""S3 3c/3d/3e: per-session schema allowlist + policy tightening, attempt persistence, quota shapes.

The Ownership scan reads the qinno plan files READ-ONLY and is skipped when qinno is absent.
"""

import json
import re
import sqlite3
from pathlib import Path

import pytest
import yaml
from claude_agent_sdk import AssistantMessage, TextBlock
from claude_agent_sdk._internal.message_parser import parse_message

from orchestrator.config import Budget
from orchestrator.gates import Attempt, breaker_tripped
from orchestrator.pipeline import PipelineError, load_pipeline
from orchestrator.policy import Allow, Deny, PolicyConfig, PolicyContext, decide
from orchestrator.runner import PolicyGuard, StepRequest, StepStatus, looks_like_quota, run_step
from orchestrator.state import _SCHEMA_V1, SCHEMA_VERSION, StateDB
from tests.test_runner import Fake, assistant, result

REAL = Path(__file__).parent.parent / "pipelines" / "qinno-rbac-v2.yaml"
QINNO_PLAN = Path("~/Documents/Projects/qinno/docs/planning/rbac-v2").expanduser()
S1_SCHEMA_FILES = [
    "packages/db/src/schemas/core/roles.ts",
    "packages/db/src/schemas/core/users.ts",
    "packages/db/src/schemas/core/merchant-users.ts",
]


@pytest.fixture(scope="module")
def pipeline():
    return load_pipeline(REAL, check_files=False)


def write(ctx, path, tool="Write"):
    return decide(tool, {"file_path": path}, ctx)


# ---- 3c: schema allowlist ----------------------------------------------------------------------


def test_yaml_gives_only_s1_a_schema_allowlist_from_its_ownership_block(pipeline):
    assert list(pipeline.session("S1").schema_allowlist) == S1_SCHEMA_FILES
    for sid in ("S2", "S3", "S4", "S5", "S6"):
        assert pipeline.session(sid).schema_allowlist == ()


@pytest.mark.parametrize("path", S1_SCHEMA_FILES)
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_rbac_v2_s1_is_not_blocked_when_it_edits_its_schema_files(pipeline, path, tool):
    ctx = pipeline.policy_context(pipeline.session("S1"))
    assert isinstance(write(ctx, path, tool), Allow)
    assert isinstance(write(ctx, str(pipeline.repo / path), tool), Allow)  # absolute form


@pytest.mark.parametrize(
    "cmd",
    [
        f"sed -i '' 's/a/b/' {S1_SCHEMA_FILES[0]}",
        f"echo x >> {S1_SCHEMA_FILES[1]}",
        f"tee {S1_SCHEMA_FILES[2]}",
    ],
)
def test_rbac_v2_s1_shell_edits_of_its_own_schema_files_are_allowed(pipeline, cmd):
    ctx = pipeline.policy_context(pipeline.session("S1"))
    assert isinstance(decide("Bash", {"command": cmd}, ctx), Allow)


def test_s1_still_hard_stops_on_any_other_schema_file(pipeline):
    ctx = pipeline.policy_context(pipeline.session("S1"))
    for path in (
        "packages/db/src/schemas/core/plans.ts",
        "packages/db/src/schemas/core/plan-features.ts",
        "packages/db/src/schemas/commerce/orders.ts",
    ):
        d = write(ctx, path)
        assert isinstance(d, Deny) and d.hard_stop, path


@pytest.mark.parametrize("sid", ["S2", "S3", "S4", "S5", "S6"])
def test_sessions_that_own_no_schema_file_hard_stop_on_roles_ts(pipeline, sid):
    d = write(pipeline.policy_context(pipeline.session(sid)), S1_SCHEMA_FILES[0])
    assert isinstance(d, Deny) and d.hard_stop


def test_shell_writes_to_an_unowned_schema_file_are_a_hard_stop_too(pipeline):
    ctx = pipeline.policy_context(pipeline.session("S2"))
    for cmd in (f"sed -i '' 's/a/b/' {S1_SCHEMA_FILES[0]}", f"echo x > {S1_SCHEMA_FILES[0]}"):
        d = decide("Bash", {"command": cmd}, ctx)
        assert isinstance(d, Deny) and d.hard_stop, cmd


def test_reading_a_schema_file_is_never_blocked(pipeline):
    ctx = pipeline.policy_context(pipeline.session("S2"))
    assert isinstance(decide("Read", {"file_path": S1_SCHEMA_FILES[0]}, ctx), Allow)
    assert isinstance(decide("Bash", {"command": f"cat {S1_SCHEMA_FILES[0]}"}, ctx), Allow)


def _ownership_paths(task_file: Path) -> list[str]:
    """Paths in the fenced block under `## Ownership`: brace-expanded, annotations stripped."""
    text = task_file.read_text()
    m = re.search(r"## Ownership[^\n]*\n+```[^\n]*\n(.*?)```", text, re.S)
    assert m, f"no Ownership block in {task_file.name}"
    out: list[str] = []
    for line in m.group(1).splitlines():
        line = re.sub(r"\s{2,}\(.*\)\s*$", "", line.split("#")[0]).strip()
        line = re.sub(r"\s+\(шинэ\)$", "", line)
        if not line or line.startswith("(") or " " in line:
            continue
        if b := re.search(r"\{([^}]+)\}", line):
            out += [line.replace(b.group(0), alt) for alt in b.group(1).split(",")]
        else:
            out.append(line)
    return out


@pytest.mark.skipif(not QINNO_PLAN.is_dir(), reason="qinno plan files not on this machine")
@pytest.mark.parametrize("sid, task", [(f"S{i}", None) for i in range(1, 7)])
def test_no_claude_ownership_file_of_any_rbac_v2_session_is_blocked_by_policy(pipeline, sid, task):
    """The real check for 'S1 must not be BLOCKED': every file a session owns is writable."""
    spec = pipeline.session(sid)
    ctx = pipeline.policy_context(spec)
    paths = _ownership_paths(QINNO_PLAN / spec.claude_task)
    assert paths, "ownership parse found nothing"
    blocked = {p: d.reason for p in paths if "*" not in p and isinstance(d := write(ctx, p), Deny)}
    assert not blocked, blocked


@pytest.mark.skipif(not QINNO_PLAN.is_dir(), reason="qinno plan files not on this machine")
def test_schema_allowlist_equals_the_schema_files_in_ownership(pipeline):
    for spec in pipeline.sessions:
        owned = [
            p
            for p in _ownership_paths(QINNO_PLAN / spec.claude_task)
            if p.startswith("packages/db/")
        ]
        assert sorted(spec.schema_allowlist) == sorted(owned), spec.id


# ---- 3c: policy tightening from yaml -----------------------------------------------------------


def _yaml(tmp_path, **extra):
    base = {
        "name": "t",
        "repo": str(tmp_path),
        "plan_dir": "plan",
        "sessions": [
            {"id": "A", "claude_task": "a.md", "handoff": "h.md"},
            {"id": "B", "claude_task": "b.md", "handoff": "h2.md", "depends_on": ["A"]},
        ],
    }
    base.update(extra)
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(base))
    return path


def test_pipeline_and_session_policy_only_add_to_the_deny_lists(tmp_path):
    raw = yaml.safe_load(_yaml(tmp_path).read_text())
    raw["policy"] = {"forbidden_write_globs": ["secrets/**"], "forbidden_commands": ["curl"]}
    raw["sessions"][1]["policy"] = {"forbidden_git_subcommands": ["tag"]}
    (tmp_path / "p.yaml").write_text(yaml.safe_dump(raw))
    p = load_pipeline(tmp_path / "p.yaml", check_files=False)
    a, b = p.session("A"), p.session("B")
    base = PolicyConfig.default()
    ca, cb = p.policy_config(a), p.policy_config(b)
    assert "secrets/**" in ca.forbidden_write_globs and "curl" in ca.forbidden_commands
    assert set(base.forbidden_write_globs) <= set(ca.forbidden_write_globs)
    assert "tag" in cb.forbidden_git_subcommands and "tag" not in ca.forbidden_git_subcommands
    assert "secrets/**" in cb.forbidden_write_globs  # pipeline-wide reaches every session
    ctx = p.policy_context(a)
    assert isinstance(write(ctx, "secrets/x.txt"), Deny)
    assert isinstance(decide("Bash", {"command": "curl example.com"}, ctx), Deny)


@pytest.mark.parametrize(
    "policy",
    [
        {"allowed_mcp_prefixes": ["mcp__x__"]},  # would loosen
        {"allowed_schema_globs": ["**"]},
        {"forbidden_write_globs": "not-a-list"},
        ["not", "a", "mapping"],
    ],
)
def test_loosening_or_malformed_policy_is_a_pipeline_error(tmp_path, policy):
    raw = yaml.safe_load(_yaml(tmp_path).read_text())
    raw["policy"] = policy
    (tmp_path / "p.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(PipelineError):
        load_pipeline(tmp_path / "p.yaml", check_files=False)


def test_requires_stage_false_forbids_post_steps(tmp_path):
    raw = yaml.safe_load(_yaml(tmp_path).read_text())
    raw["requires_stage"] = False
    raw["sessions"][0]["post_steps"] = ["drizzle_push_develop2"]
    (tmp_path / "p.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(PipelineError, match="requires_stage"):
        load_pipeline(tmp_path / "p.yaml", check_files=False)


def test_open_items_show_up_as_todos(pipeline):
    assert any("signing/updater" in t for t in pipeline.todos())


# ---- 3d: attempts persisted and restored -------------------------------------------------------


def test_v1_database_migrates_to_v2_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_V1)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO runs (pipeline, started_at, status) VALUES ('old', 'x', 'running')")
    conn.commit()
    conn.close()
    with StateDB(path) as db:
        assert db.schema_version == SCHEMA_VERSION == 2
        run = db.latest_run()
        assert run.pipeline == "old" and run.pipeline_path is None
        db.add_attempt(run.id, "S1", 3, 0, "boom")
        assert db.attempts_for(run.id, "S1") == [(0, "boom")]


def test_attempt_history_survives_reopen_and_still_trips_the_breaker(tmp_path):
    path = tmp_path / "s.db"
    with StateDB(path) as db:
        run = db.start_run("p", "/x/p.yaml")
        for _ in range(3):
            db.add_attempt(run, "S1", 3, 0, None)
        db.add_attempt(run, "S2", 3, 5, None)  # another session's history is separate
    with StateDB(path) as db:  # a new process after a crash
        run = db.latest_run().id
        rebuilt = [Attempt(n, e) for n, e in db.attempts_for(run, "S1")]
        assert len(rebuilt) == 3
        assert "circuit breaker" in breaker_tripped(rebuilt)
        assert breaker_tripped([Attempt(n, e) for n, e in db.attempts_for(run, "S2")]) is None
        assert db.get_run(run).pipeline_path == "/x/p.yaml"


def test_repeated_error_attempts_trip_the_same_error_rule_after_reload(tmp_path):
    with StateDB(tmp_path / "s.db") as db:
        run = db.start_run("p")
        for i in range(5):
            db.add_attempt(run, "S1", 3, 1, f"tests failed (exit 1) after {i} ms")
        rebuilt = [Attempt(n, e) for n, e in db.attempts_for(run, "S1")]
        assert "same error" in breaker_tripped(rebuilt)


# ---- 3e: quota shapes -------------------------------------------------------------------------
# Captured from a REAL quota stop in an interactive Claude Code 2.1.295 session on 2026-10-11 (the
# CLI's own transcript). The raw rate_limit_info keys are camelCase on the wire.
REAL_RATE_LIMIT_INFO = {
    "status": "rejected",
    "resetsAt": 1791720000,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "overageDisabledReason": "out_of_credits",
    "isUsingOverage": False,
}
REAL_QUOTA_TEXT = "You've hit your session limit · resets 8pm (Asia/Ulaanbaatar)"
BUDGET = Budget(per_call=2.0, per_session=10.0, total=100.0)


@pytest.fixture
def world(toy_repo, tmp_path):
    db = StateDB(tmp_path / "q.db")
    run_id = db.start_run("toy")
    step = db.ensure_step(run_id, "S1", 3)
    req = StepRequest(prompt="go", cwd=toy_repo, policy=PolicyContext(repo=toy_repo))
    yield db, run_id, step.id, req
    db.close()


def test_the_sdk_parses_the_real_rate_limit_payload():
    msg = parse_message(
        {"type": "rate_limit_event", "rate_limit_info": REAL_RATE_LIMIT_INFO, "uuid": "u",
         "session_id": "s"}
    )  # fmt: skip
    info = msg.rate_limit_info
    assert (info.status, info.rate_limit_type, info.resets_at) == (
        "rejected",
        "five_hour",
        1791720000,
    )


async def test_real_rejected_rate_limit_event_is_layer_1_quota_wait(world):
    db, run_id, step_id, req = world
    event = parse_message(
        {"type": "rate_limit_event", "rate_limit_info": REAL_RATE_LIMIT_INFO, "uuid": "u",
         "session_id": "sess-1"}
    )  # fmt: skip
    quota = AssistantMessage(
        content=[TextBlock(text=REAL_QUOTA_TEXT)], model="<synthetic>", error="rate_limit",
        session_id="sess-1",
    )  # fmt: skip
    res = await run_step(
        req, db=db, step_id=step_id, budget=BUDGET,
        query_fn=Fake([event, quota, result("error_during_execution", is_error=True, cost=0)]),
    )  # fmt: skip
    assert res.status is StepStatus.QUOTA_WAIT
    assert "five_hour" in res.reason
    events = db.events(run_id, kind="quota_wait")
    assert any("resets_at=1791720000" in e.detail for e in events)


async def test_real_quota_text_alone_is_layer_2_quota_wait(world):
    db, run_id, step_id, req = world
    res = await run_step(
        req, db=db, step_id=step_id, budget=BUDGET,
        query_fn=Fake([result("error_during_execution", is_error=True, result=REAL_QUOTA_TEXT)]),
    )  # fmt: skip
    assert res.status is StepStatus.QUOTA_WAIT


async def test_real_quota_text_in_a_successful_result_is_not_a_quota_stop(world):
    db, run_id, step_id, req = world
    res = await run_step(
        req, db=db, step_id=step_id, budget=BUDGET,
        query_fn=Fake([assistant(), result(result=f"docs say: {REAL_QUOTA_TEXT}")]),
    )  # fmt: skip
    assert res.status is StepStatus.DONE


def test_quota_phrases_seen_in_the_wild():
    assert looks_like_quota([REAL_QUOTA_TEXT])
    assert looks_like_quota(["usage_limit_reached"])
    assert looks_like_quota(["You've hit your weekly limit"])
    assert not looks_like_quota(["tests hit the time limit of the suite"])
    assert not looks_like_quota(["the limit of a function"])


def test_policy_guard_is_untouched_by_all_this():
    assert PolicyGuard(PolicyContext(repo=Path("/x"))).stop_requested is False
    assert json.dumps(REAL_RATE_LIMIT_INFO)
