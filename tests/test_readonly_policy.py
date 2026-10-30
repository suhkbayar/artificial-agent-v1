"""The ① plan step is read-only by policy, independent of the SDK permission mode."""

import pytest

from orchestrator.policy import Allow, Deny, PolicyContext, decide
from tests.flowkit import build_world, hook_decision


@pytest.fixture
def ro(tmp_path):
    return PolicyContext(repo=tmp_path, read_only=True)


def bash(ctx, cmd):
    return decide("Bash", {"command": cmd}, ctx)


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_write_tools_are_denied(ro, tool):
    inp = {"file_path": "a.py", "notebook_path": "a.ipynb"}
    d = decide(tool, inp, ro)
    assert isinstance(d, Deny) and "read-only" in d.reason and not d.hard_stop


@pytest.mark.parametrize(
    "cmd",
    [
        "cat calc.py",
        "ls -la",
        "grep -rn divide .",
        "git status --short",
        "git diff --stat",
        "git log --oneline -5",
        "git show HEAD:calc.py",
        "find . -name '*.py'",
        "head -20 calc.py | grep def",
        "wc -l calc.py && echo done",
        "cd sub && ls",
        "cat calc.py 2>/dev/null",
        "jq .name package.json",
        "sort calc.py | uniq",
    ],
)
def test_read_commands_are_allowed(ro, cmd):
    assert isinstance(bash(ro, cmd), Allow), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "touch x",
        "mkdir d",
        "rm calc.py",
        "mv a b",
        "cp a b",
        "echo x > calc.py",
        "echo x >> calc.py",
        "cat a | tee out.txt",
        "sed -i '' s/a/b/ calc.py",
        "sort -o out.txt calc.py",
        "sort --output=out.txt calc.py",
        "find . -name '*.pyc' -delete",
        "find . -exec rm {} ;",
        "find . -fprint out.txt",
        "git add -A",
        "git branch -D x",
        "git config user.name x",
        "git tag v1",
        "python3 test_calc.py",
        "yarn test",
        "bash -c 'touch x'",
        "xargs touch",
        "env touch x",
        "awk 'BEGIN{print 1 > \"f\"}'",
        "npm install",
        "ls && touch x",
    ],
)
def test_anything_that_can_write_is_denied(ro, cmd):
    d = bash(ro, cmd)
    assert isinstance(d, Deny), cmd
    assert not d.hard_stop


def test_the_same_commands_are_fine_outside_the_planning_step(tmp_path):
    ctx = PolicyContext(repo=tmp_path)
    assert isinstance(bash(ctx, "touch x"), Allow)
    assert isinstance(decide("Write", {"file_path": "a.py"}, ctx), Allow)


async def test_the_flow_runs_the_plan_step_read_only(toy_repo, tmp_path, mem_dir):
    w = build_world(toy_repo, tmp_path, mem_dir)
    seen = {}

    async def plan(agent, call):
        seen["write"] = await hook_decision(call, "Write", {"file_path": "x.py"}, "a")
        seen["touch"] = await hook_decision(call, "Bash", {"command": "touch x"}, "b")
        seen["status"] = await hook_decision(call, "Bash", {"command": "git status"}, "c")
        seen["read"] = await hook_decision(call, "Read", {"file_path": "calc.py"}, "d")
        from tests.flowkit import DEFAULT

        return DEFAULT

    async def claude(agent, call):  # later phases are not read-only
        seen["claude_write"] = await hook_decision(call, "Write", {"file_path": "x.py"}, "e")
        from tests.flowkit import DEFAULT

        return DEFAULT

    w.agent.handlers["plan"] = plan
    w.agent.handlers["claude"] = claude
    from orchestrator.flow import FlowOptions, run_pipeline

    out = await run_pipeline(
        w.pipeline, w.config, w.deps, FlowOptions(only="S1", setting_sources=[]), db=w.db
    )
    w.db.close()
    assert out.status == "done"
    assert seen == {
        "write": "deny",
        "touch": "deny",
        "status": "allow",
        "read": "allow",
        "claude_write": "allow",
    }
