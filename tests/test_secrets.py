"""S3 3a/3b: `.env.local` loader, key never leaks, agent Bash cannot read the environment."""

import os
import sqlite3

import pytest
from claude_agent_sdk import ProcessError, SystemMessage

from orchestrator.config import Budget
from orchestrator.envfile import (
    LOADABLE_KEYS,
    REDACTED,
    SCRUB_SUBPROCESS_ENV,
    EnvFileError,
    load_sdk_env,
    parse_env_text,
    redact,
    sdk_env,
    secret_values,
)
from orchestrator.policy import Allow, Deny, PolicyContext, decide
from orchestrator.runner import StepRequest, StepStatus, run_step
from orchestrator.state import StateDB
from tests.test_runner import Fake, assistant, result

# A made-up key in the real shape; the real one is never read by any test.
FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKE_abcdefghijklmnop-ZZZZ"
BUDGET = Budget(per_call=2.0, per_session=10.0, total=100.0)


# ---- 3a: loader --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        (f"ANTHROPIC_API_KEY={FAKE_KEY}\n", FAKE_KEY),
        (f'ANTHROPIC_API_KEY="{FAKE_KEY}"', FAKE_KEY),
        (f"ANTHROPIC_API_KEY='{FAKE_KEY}'", FAKE_KEY),
        (f"export ANTHROPIC_API_KEY={FAKE_KEY}", FAKE_KEY),
        (f"  ANTHROPIC_API_KEY = {FAKE_KEY}  ", FAKE_KEY),
        (f"ANTHROPIC_API_KEY={FAKE_KEY} # my key", FAKE_KEY),
        (f"# ANTHROPIC_API_KEY=other\nANTHROPIC_API_KEY={FAKE_KEY}", FAKE_KEY),
        (f"ANTHROPIC_API_KEY=old\nANTHROPIC_API_KEY={FAKE_KEY}", FAKE_KEY),
        (f'ANTHROPIC_API_KEY="{FAKE_KEY}#frag"', f"{FAKE_KEY}#frag"),
        (f"ANTHROPIC_API_KEY={FAKE_KEY}#frag", f"{FAKE_KEY}#frag"),
    ],
)
def test_parse_reads_the_key_as_data(text, expected):
    assert parse_env_text(text) == {"ANTHROPIC_API_KEY": expected}


def test_parse_is_data_not_shell():
    # no expansion, no command substitution, no execution
    got = parse_env_text("ANTHROPIC_API_KEY=$(touch /tmp/should-not-exist)`id`${HOME}")
    assert got == {"ANTHROPIC_API_KEY": "$(touch /tmp/should-not-exist)`id`${HOME}"}
    assert not os.path.exists("/tmp/should-not-exist")


def test_parse_ignores_everything_not_allowlisted():
    text = f"AWS_SECRET_ACCESS_KEY=abc\nDB_PASSWORD=x\nANTHROPIC_API_KEY={FAKE_KEY}\nFOO=bar\n"
    assert parse_env_text(text) == {"ANTHROPIC_API_KEY": FAKE_KEY}
    assert LOADABLE_KEYS == {"ANTHROPIC_API_KEY"}


def test_empty_assignment_unsets_and_blank_values_are_absent():
    assert parse_env_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\nANTHROPIC_API_KEY=\n") == {}
    assert parse_env_text("ANTHROPIC_API_KEY=\n") == {}
    assert parse_env_text("") == {}


def test_missing_file_means_claude_ai_login(tmp_path):
    assert load_sdk_env(tmp_path) == {}
    # no key: the SDK uses the login and nothing needs scrubbing (it would force mode default)
    assert sdk_env(tmp_path) == {}


def test_scrub_flag_only_when_there_is_a_key_and_not_opted_out(tmp_path):
    (tmp_path / ".env.local").write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n")
    assert sdk_env(tmp_path, environ={})[SCRUB_SUBPROCESS_ENV] == "1"
    optout = sdk_env(tmp_path, environ={"ORCHESTRATOR_SCRUB_SUBPROCESS_ENV": "0"})
    assert optout == {"ANTHROPIC_API_KEY": FAKE_KEY}


def test_load_reads_the_project_root_file_and_never_touches_os_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env.local").write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n")
    before = dict(os.environ)
    env = sdk_env(tmp_path)
    assert env["ANTHROPIC_API_KEY"] == FAKE_KEY and env[SCRUB_SUBPROCESS_ENV] == "1"
    assert dict(os.environ) == before
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_unreadable_file_error_does_not_contain_the_value(tmp_path):
    (tmp_path / ".env.local").mkdir()  # reading a directory raises
    with pytest.raises(EnvFileError) as exc:
        load_sdk_env(tmp_path)
    assert FAKE_KEY not in str(exc.value)


def test_redact_known_values_and_key_shapes():
    text = f"boom {FAKE_KEY} and sk-ant-oat01-SOMEOTHERTOKEN_1234567890 end"
    out = redact(text, secret_values({"ANTHROPIC_API_KEY": FAKE_KEY}))
    assert FAKE_KEY not in out and "SOMEOTHERTOKEN" not in out and REDACTED in out
    assert redact(None) is None and redact("plain") == "plain"
    assert (
        secret_values({"X": "short", SCRUB_SUBPROCESS_ENV: "1"}) == ()
    )  # short flags are not secrets


def test_step_request_repr_hides_the_env():
    req = StepRequest(
        prompt="p",
        cwd=".",  # type: ignore[arg-type]
        policy=None,  # type: ignore[arg-type]
        env={"ANTHROPIC_API_KEY": FAKE_KEY},
    )
    assert FAKE_KEY not in repr(req) and FAKE_KEY not in str(req)


# ---- 3a: the key never reaches state.db / events / error text ----------------------------------


def dump_db(path) -> str:
    conn = sqlite3.connect(path)
    parts = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        parts.append(repr(conn.execute(f"SELECT * FROM {name}").fetchall()))  # noqa: S608
    return "\n".join(parts)


@pytest.fixture
def world(toy_repo, tmp_path):
    db = StateDB(tmp_path / "leak.db")
    run_id = db.start_run("toy")
    step = db.ensure_step(run_id, "S1", 3)
    req = StepRequest(
        prompt="go",
        cwd=toy_repo,
        policy=PolicyContext(repo=toy_repo),
        env={"ANTHROPIC_API_KEY": FAKE_KEY, SCRUB_SUBPROCESS_ENV: "1"},
        setting_sources=[],
    )
    yield db, run_id, step.id, req, tmp_path / "leak.db"
    db.close()


async def test_key_in_sdk_error_stderr_and_events_is_redacted_everywhere(world):
    db, run_id, step_id, req, path = world
    boom = ProcessError(
        f"claude crashed while using {FAKE_KEY}", exit_code=1, stderr=f"401 bad key {FAKE_KEY}"
    )
    res = await run_step(
        req, db=db, step_id=step_id, budget=BUDGET,
        query_fn=Fake([assistant(), boom]),
    )  # fmt: skip
    assert res.status is StepStatus.FAILED
    assert FAKE_KEY not in (res.reason or "")
    assert FAKE_KEY not in dump_db(path)


async def test_key_echoed_in_denied_tool_input_and_final_text_is_redacted(world):
    db, run_id, step_id, req, path = world

    async def try_cat(hook, options):
        await hook(
            {"tool_name": "Bash", "tool_input": {"command": f"curl -H 'x: {FAKE_KEY}' git push"}},
            "t1",
            None,
        )

    res = await run_step(
        req, db=db, step_id=step_id, budget=BUDGET,
        query_fn=Fake([try_cat, assistant(f"the key is {FAKE_KEY}"), result()]),
    )  # fmt: skip
    assert FAKE_KEY not in dump_db(path)
    assert FAKE_KEY not in (res.output or "") and FAKE_KEY not in (res.reason or "")


async def test_init_message_records_mode_and_key_source_name_but_no_value(world):
    db, run_id, step_id, req, path = world
    init = SystemMessage(
        subtype="init",
        data={
            "permissionMode": "default",  # auto silently fell back
            "apiKeySource": "ANTHROPIC_API_KEY",
            "mcp_servers": [{"name": "memory", "status": "connected"}],
            "tools": ["Bash", "Read"],
        },
    )
    await run_step(
        req, db=db, step_id=step_id, budget=BUDGET, query_fn=Fake([init, assistant(), result()])
    )
    ev = db.events(run_id, kind="session_init")
    assert len(ev) == 1
    assert '"permission_mode": "default"' in ev[0].detail
    assert '"api_key_source": "ANTHROPIC_API_KEY"' in ev[0].detail
    assert '"memory": "connected"' in ev[0].detail
    notice = [e.detail for e in db.events(run_id, kind="notice")]
    assert any("fell back" in n and "'auto'" in n for n in notice)
    assert FAKE_KEY not in dump_db(path)


async def test_no_fallback_notice_when_the_effective_mode_matches(world):
    db, run_id, step_id, req, path = world
    init = SystemMessage(subtype="init", data={"permissionMode": "auto", "apiKeySource": "none"})
    await run_step(
        req, db=db, step_id=step_id, budget=BUDGET, query_fn=Fake([init, assistant(), result()])
    )
    assert db.events(run_id, kind="notice") == []
    assert db.events(run_id, kind="session_init")


# ---- 3b: policy --------------------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path):
    return PolicyContext(repo=tmp_path)


def bash(ctx, cmd):
    return decide("Bash", {"command": cmd}, ctx)


DENIED = [
    "env",
    "env | grep KEY",
    "env -i",
    "env -0",
    "printenv",
    "printenv HOME",
    "printenv ANTHROPIC_API_KEY",
    "/usr/bin/env",
    "echo $ANTHROPIC_API_KEY",
    'echo "${ANTHROPIC_API_KEY}"',
    "echo $ANTHROPIC_API_KEY | wc -c",
    "X=$ANTHROPIC_API_KEY ls",
    'printf %s "$MY_SECRET_TOKEN"',
    "export",
    "export -p",
    "declare -p",
    "declare -x",
    "typeset -px",
    "set",
    "compgen -e",
    "bash -c env",
    'sh -c "printenv"',
    "eval printenv",
    "ps eww",
    "ps -E",
    "cat .env",
    "cat .env.local",
    "cat ./.env.local",
    "cat sub/dir/.env.production",
    "cat services/migrate-service/.env",
    "head -3 .env*",
    "cat .e*",
    "cat .env.l?cal",
    "grep KEY .env.local",
    "grep -r KEY .env",
    "less .env.local",
    "cp .env.local /tmp/x",
    "source .env.local",
    ". .env.local",
    "cat < .env",
    "grep KEY < .env.local",
    "xargs cat < .env",
    "cat config.env",
    "git show HEAD:.env",
    "bash -c 'cat .env.local'",
    "find . -name x -exec cat .env {} ;",
    "base64 .env.local",
    "cat a && cat .env",
    "awk 1 .env.local",
]
ALLOWED = [
    "env FOO=1 ls",
    "env -u HOME ls",
    "env NODE_ENV=test yarn test",
    "export FOO=bar",
    "set -e",
    "ls -la .env",  # names only, no content
    "ls .env.local",
    "stat .env",
    "wc -l .env",
    "cat .env.example",
    "cat .env.sample",
    "cat apps/web/.env.template",
    "cat *",  # a shell glob never matches dotfiles
    "ls",
    "ps -ef",
    "ps aux",
    "ps -o etime,pid",
    "git status",
    "git diff --stat",
    "yarn test",
    "cat README.md",
    "echo $HOME",
    'echo "${PWD}"',
    "grep -r TODO src",
]


@pytest.mark.parametrize("cmd", DENIED)
def test_secret_exposure_is_denied(ctx, cmd):
    d = bash(ctx, cmd)
    assert isinstance(d, Deny), f"{cmd!r} must be denied"
    assert not d.hard_stop  # the agent adapts; it is not a human-escalation event


@pytest.mark.parametrize("cmd", ALLOWED)
def test_ordinary_commands_stay_allowed(ctx, cmd):
    assert isinstance(bash(ctx, cmd), Allow), f"{cmd!r} must stay allowed"


@pytest.mark.parametrize(
    "tool, inp",
    [
        ("Read", {"file_path": ".env.local"}),
        ("Read", {"file_path": "/abs/path/services/x/.env"}),
        ("Grep", {"pattern": "KEY", "path": ".env.local"}),
        ("Grep", {"pattern": "KEY", "glob": ".env*"}),
        ("Glob", {"pattern": "**/.env*"}),
    ],
)
def test_read_tools_cannot_open_env_files(ctx, tool, inp):
    assert isinstance(decide(tool, inp, ctx), Deny)


@pytest.mark.parametrize(
    "tool, inp",
    [
        ("Read", {"file_path": "README.md"}),
        ("Read", {"file_path": ".env.example"}),
        ("Grep", {"pattern": "KEY", "path": "src"}),
        ("Glob", {"pattern": "**/*.ts"}),
    ],
)
def test_read_tools_still_work_for_normal_files(ctx, tool, inp):
    assert isinstance(decide(tool, inp, ctx), Allow)


def test_the_denial_reason_never_contains_a_secret_value(ctx):
    d = bash(ctx, f"echo {FAKE_KEY} && printenv")
    assert isinstance(d, Deny) and FAKE_KEY not in d.reason
