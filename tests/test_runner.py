import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
)

from orchestrator.config import Budget
from orchestrator.policy import PolicyContext
from orchestrator.runner import (
    PolicyGuard,
    StepRequest,
    StepStatus,
    looks_like_quota,
    run_step,
    run_step_with_quota_retry,
)
from orchestrator.state import StateDB

BUDGET = Budget(per_call=2.0, per_session=10.0, total=100.0)


def result(subtype="success", *, is_error=False, cost=0.1, sid="sess-1", **kw):
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=3,
        session_id=sid, total_cost_usd=cost, **kw,
    )  # fmt: skip


def assistant(text="hi", *, error=None, sid="sess-1"):
    return AssistantMessage(content=[TextBlock(text=text)], model="m", error=error, session_id=sid)


def rate_event(status, sid="sess-1", **kw):
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(status=status, **kw), uuid="u", session_id=sid
    )


class Fake:
    """query_fn replacement: plays a script, optionally invoking the policy callbacks."""

    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.options = None
        self.first_prompt = None

    def __call__(self, *, prompt, options):
        self.calls += 1
        self.options = options
        return self._run(prompt, options)

    async def _run(self, prompt, options):
        async for first in prompt:
            self.first_prompt = first
            break
        hook = options.hooks["PreToolUse"][0].hooks[0]
        for item in self.script:
            if callable(item):
                out = item(hook, options)
                if asyncio.iscoroutine(out):
                    out = await out
                if out is not None:
                    yield out
            elif isinstance(item, BaseException):
                raise item
            else:
                yield item


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = StateDB(tmp_path / "state.db")
    run_id = db.start_run("t")
    step = db.ensure_step(run_id, "S1", 1)
    req = StepRequest(prompt="do it", cwd=repo, policy=PolicyContext(repo=repo), timeout_s=5)
    yield db, run_id, step.id, req
    db.close()


async def go(env, script, **kw):
    db, _, step_id, req = env
    fake = Fake(script)
    res = await run_step(req, db=db, step_id=step_id, budget=BUDGET, query_fn=fake, **kw)
    return res, fake


# ---- happy path and persistence --------------------------------------------------------------


async def test_done_persists_session_cost_and_events(env):
    db, run_id, step_id, _ = env
    res, fake = await go(env, [assistant(), result(cost=0.12)])
    assert (res.status, res.session_id, res.cost_usd, res.reason) == (
        StepStatus.DONE, "sess-1", 0.12, None,
    )  # fmt: skip
    step = db.get_step(step_id)
    assert (step.status, step.claude_session_id, step.attempts, step.cost_usd) == (
        "done", "sess-1", 1, 0.12,
    )  # fmt: skip
    assert db.get_run(run_id).total_cost_usd == 0.12
    kinds = [e.kind for e in db.events(run_id)]
    assert kinds.count("sdk_call") == 2  # start + end
    # streaming input, as can_use_tool requires; fresh session each time
    assert fake.first_prompt == {"type": "user", "message": {"role": "user", "content": "do it"}}
    assert fake.options.max_budget_usd == 2.0


async def test_options_never_bypass_and_always_wire_the_policy(env):
    _, fake = await go(env, [result()])
    o = fake.options
    assert o.permission_mode == "auto" and o.permission_mode != "bypassPermissions"
    assert o.can_use_tool is not None
    assert o.hooks["PreToolUse"][0].matcher is None  # every tool, every mode


async def test_bypass_permissions_is_refused(env):
    db, _, step_id, req = env
    req.permission_mode = "bypassPermissions"
    with pytest.raises(ValueError, match="never"):
        await run_step(req, db=db, step_id=step_id, budget=BUDGET, query_fn=Fake([result()]))


async def test_session_id_is_saved_before_a_crash(env):
    db, _, step_id, _ = env
    res, _ = await go(env, [assistant(sid="early"), RuntimeError("boom")])
    assert res.status is StepStatus.FAILED and "RuntimeError" in res.reason
    assert db.get_step(step_id).claude_session_id == "early"


# ---- quota: three layers ---------------------------------------------------------------------


async def test_layer1_structured_rate_limit_event(env):
    res, _ = await go(
        env, [rate_event("rejected", rate_limit_type="five_hour", resets_at=1),
              result("error_during_execution", is_error=True)],
    )  # fmt: skip
    assert res.status is StepStatus.QUOTA_WAIT and "five_hour" in res.reason


async def test_layer1_assistant_error_rate_limit(env):
    res, _ = await go(env, [assistant("limit", error="rate_limit"),
                            result("error_during_execution", is_error=True)])  # fmt: skip
    assert res.status is StepStatus.QUOTA_WAIT


async def test_layer2_error_text(env):
    bad = result("error_during_execution", is_error=True, errors=["API Error: 429 rate limit"])
    assert (await go(env, [bad]))[0].status is StepStatus.QUOTA_WAIT
    bad = result("error_during_execution", is_error=True, api_error_status=429)
    assert (await go(env, [bad]))[0].status is StepStatus.QUOTA_WAIT
    bad = result("error_during_execution", is_error=True, result="Claude usage limit reached")
    assert (await go(env, [bad]))[0].status is StepStatus.QUOTA_WAIT
    proc = ProcessError("exit 1", exit_code=1, stderr="Error: usage limit reached, resets 5pm")
    assert (await go(env, [proc]))[0].status is StepStatus.QUOTA_WAIT


async def test_layer2_text_in_a_successful_result_is_not_quota(env):
    # the agent legitimately wrote a rate limiter
    ok = result(result="Implemented rate limiting; handles HTTP 429 and 'rate limit' errors")
    assert (await go(env, [ok]))[0].status is StepStatus.DONE
    # even a stale rejection earlier in the stream does not override a successful result
    assert (await go(env, [rate_event("rejected"), result()]))[0].status is StepStatus.DONE


async def test_warning_is_not_a_stop(env):
    db, run_id, _, _ = env
    res, _ = await go(env, [rate_event("allowed_warning", utilization=0.9), result()])
    assert res.status is StepStatus.DONE
    assert any(e.kind == "rate_limit_warning" for e in db.events(run_id))


async def test_layer3_timeout_is_failed_not_quota(env):
    db, _, step_id, req = env
    req.timeout_s = 0.05

    def hang(hook, options):
        return asyncio.sleep(30)

    fake = Fake([assistant(), hang])
    res = await run_step(req, db=db, step_id=step_id, budget=BUDGET, query_fn=fake)
    assert res.status is StepStatus.FAILED and "timeout" in res.reason
    assert db.get_step(step_id).claude_session_id == "sess-1"


async def test_timeout_with_quota_words_in_stderr_is_still_a_timeout(env):
    db, _, step_id, req = env
    req.timeout_s = 0.05

    def hang(hook, options):
        options.stderr("rate limit 429 too many requests")
        return asyncio.sleep(30)

    res = await run_step(req, db=db, step_id=step_id, budget=BUDGET, query_fn=Fake([hang]))
    assert res.status is StepStatus.FAILED


def test_looks_like_quota_patterns():
    assert looks_like_quota(["Rate limit exceeded"]) and looks_like_quota([], 429)
    assert looks_like_quota(["HTTP 429 Too Many Requests"])
    assert not looks_like_quota(["TypeError: x is not a function", None])
    assert not looks_like_quota(["connection reset"], 500)


# ---- other outcomes --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script,needle",
    [
        ([result("error_max_budget_usd", is_error=True)], "per-call budget"),
        ([result("error_max_turns", is_error=True)], "max turns"),
        (
            [result("error_during_execution", is_error=True, errors=["tool crashed"])],
            "tool crashed",
        ),
        ([result(is_error=True, stop_reason="refusal")], "refusal"),
        ([assistant("x", error="authentication_failed"), result(is_error=True)], "authentication"),
        ([assistant("x", error="billing_error"), result(is_error=True)], "billing"),
        ([CLINotFoundError("no cli")], "CLINotFoundError"),
        ([ProcessError("boom", exit_code=2, stderr="segfault")], "segfault"),
        ([assistant()], "without a result"),
    ],
)
async def test_failures(env, script, needle):
    res, _ = await go(env, script)
    assert res.status is StepStatus.FAILED and needle in res.reason, res.reason


async def test_foreign_permission_denial_stops_without_retry(env):
    denials = [{"tool_name": "Bash", "tool_use_id": "t9", "tool_input": {}}]
    res, _ = await go(
        env, [result("error_during_execution", is_error=True, permission_denials=denials)]
    )
    assert res.status is StepStatus.PERMISSION_DENIED and "Bash" in res.reason
    # the same denial on a run that still succeeded is not a failure
    ok = result(permission_denials=denials)
    assert (await go(env, [ok]))[0].status is StepStatus.DONE


# ---- policy wiring ---------------------------------------------------------------------------


def tool(name, tool_input, tid):
    async def call(hook, options):
        await hook(
            {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": tool_input},
            tid,
            None,
        )

    return call


async def test_soft_deny_is_logged_and_the_step_still_completes(env):
    db, run_id, _, _ = env
    out = {}

    async def call(hook, options):
        out["hook"] = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "git push"}}, "t1", None
        )

    res, _ = await go(env, [call, result()])
    assert res.status is StepStatus.DONE
    hso = out["hook"]["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "git" in hso["permissionDecisionReason"]
    assert "continue_" not in out["hook"]  # soft: the agent keeps working
    assert [e.kind for e in db.events(run_id, kind="policy_deny")] != []


async def test_allowed_call_returns_an_empty_hook_result(env):
    out = {}

    async def call(hook, options):
        out["r"] = await hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t1", None)

    await go(env, [call, result()])
    assert out["r"] == {}


async def test_hard_stop_blocks_the_step_even_if_the_agent_reports_success(env):
    db, run_id, step_id, _ = env
    out = {}

    async def call(hook, options):
        out["hook"] = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "sst deploy --stage production"}},
            "t1", None,
        )  # fmt: skip

    res, _ = await go(env, [call, result()])
    assert res.status is StepStatus.BLOCKED and "production" in res.reason
    assert out["hook"]["continue_"] is False and "production" in out["hook"]["stopReason"]
    assert db.get_step(step_id).status == "blocked"
    assert [e.kind for e in db.events(run_id, kind="blocker")] != []


async def test_can_use_tool_path_and_dedup_by_tool_use_id(env):
    box = {}

    async def call(hook, options):
        inp = {"command": "git commit -m x"}
        await hook({"tool_name": "Bash", "tool_input": inp}, "same", None)
        box["deny"] = await options.can_use_tool(
            "Bash", inp, type("C", (), {"tool_use_id": "same"})()
        )
        box["allow"] = await options.can_use_tool(
            "Bash", {"command": "ls"}, type("C", (), {"tool_use_id": "t2"})()
        )
        box["hard"] = await options.can_use_tool(
            "mcp__memory__delete_entities",
            {"entityNames": ["x"]},
            type("C", (), {"tool_use_id": "t3"})(),
        )

    res, _ = await go(env, [call, result()])
    assert box["deny"].behavior == "deny" and box["deny"].interrupt is False
    assert box["allow"].behavior == "allow" and box["allow"].updated_input == {"command": "ls"}
    assert box["hard"].behavior == "deny" and box["hard"].interrupt is True
    assert res.status is StepStatus.BLOCKED


async def test_blind_retry_of_the_same_denied_call_stops_the_step(env):
    out = []

    async def call(hook, options):
        for i in range(3):
            out.append(
                await hook(
                    {"tool_name": "Bash", "tool_input": {"command": "git push"}}, f"t{i}", None
                )
            )

    res, _ = await go(env, [call, result()])
    assert res.status is StepStatus.PERMISSION_DENIED and "denied 3 times" in res.reason
    assert out[2]["continue_"] is False and "continue_" not in out[0]


def test_guard_counts_one_decision_per_tool_use_id(tmp_path):
    g = PolicyGuard(PolicyContext(repo=tmp_path), repeat_limit=2)
    for _ in range(5):
        g.check("Bash", {"command": "git push"}, "same-id")
    assert g.deny_count == 1 and g.blind_retry is None


# ---- budget, hourly limit, quota retry -------------------------------------------------------


async def test_call_cap_shrinks_to_what_the_session_has_left(env):
    db, run_id, step_id, _ = env
    other = db.ensure_step(run_id, "S1", 2)
    db.update_step(other.id, add_cost_usd=9.5)
    res, fake = await go(env, [result(cost=0.2)])
    assert fake.options.max_budget_usd == pytest.approx(0.5)
    assert res.status is StepStatus.DONE


async def test_exhausted_session_or_total_budget_blocks_without_calling_the_sdk(env):
    db, run_id, step_id, _ = env
    other = db.ensure_step(run_id, "S1", 2)
    db.update_step(other.id, add_cost_usd=10.0)
    res, fake = await go(env, [result()])
    assert res.status is StepStatus.BLOCKED and "session budget exhausted" in res.reason
    assert fake.calls == 0
    assert db.get_step(step_id).status == "blocked"

    db2 = StateDB(":memory:")
    rid = db2.start_run("p")
    s2 = db2.ensure_step(rid, "S1", 1)
    db2.update_step(db2.ensure_step(rid, "S2", 1).id, add_cost_usd=100.0)
    req = env[3]
    fake = Fake([result()])
    r = await run_step(req, db=db2, step_id=s2.id, budget=BUDGET, query_fn=fake)
    assert (
        r.status is StepStatus.BLOCKED and "total budget exhausted" in r.reason and fake.calls == 0
    )


async def test_hourly_call_limit_waits_instead_of_calling(env):
    db, run_id, _, _ = env
    for _ in range(3):
        db.add_event("sdk_call", "x", run_id=run_id)
    res, fake = await go(env, [result()], max_calls_per_hour=3)
    assert res.status is StepStatus.QUOTA_WAIT and "call limit" in res.reason and fake.calls == 0
    # events older than an hour do not count
    later = datetime.now(UTC) + timedelta(hours=2)
    res, fake = await go(env, [result()], max_calls_per_hour=3, now=lambda: later)
    assert res.status is StepStatus.DONE and fake.calls == 1


async def test_quota_retry_waits_an_hour_then_runs_a_fresh_session(env):
    db, run_id, step_id, req = env
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    scripts = iter([
        [rate_event("rejected"), result("error_during_execution", is_error=True)],
        [assistant(sid="sess-2"), result(sid="sess-2")],
    ])  # fmt: skip

    def qf(*, prompt, options):
        return Fake(next(scripts))(prompt=prompt, options=options)

    res = await run_step_with_quota_retry(
        req, db=db, step_id=step_id, budget=BUDGET, sleep=fake_sleep, query_fn=qf
    )
    assert res.status is StepStatus.DONE and res.session_id == "sess-2"
    assert sleeps == [3600]
    waits = [e.detail for e in db.events(run_id, kind="quota_wait")]
    assert any("waiting 3600s before retry 1/24" in w for w in waits)
    assert db.get_step(step_id).attempts == 2


async def test_quota_retry_gives_up_after_max_waits(env):
    db, _, step_id, req = env

    async def no_sleep(_):
        pass

    def qf(*, prompt, options):
        return Fake(
            [rate_event("rejected"), result(is_error=True, subtype="error_during_execution")]
        )(prompt=prompt, options=options)

    res = await run_step_with_quota_retry(
        req, db=db, step_id=step_id, budget=BUDGET, sleep=no_sleep, max_waits=2, query_fn=qf
    )
    assert res.status is StepStatus.QUOTA_WAIT
    assert db.get_step(step_id).attempts == 3


async def test_event_detail_is_machine_readable(env):
    db, run_id, _, _ = env
    await go(env, [result(cost=0.5)])
    last = [e for e in db.events(run_id, kind="sdk_call")][-1]
    d = json.loads(last.detail)
    assert d["status"] == "done" and d["cost_usd"] == 0.5 and d["turns"] == 3
