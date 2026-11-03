import os

import pytest

from orchestrator.globs import glob_match, pattern_could_hit
from orchestrator.policy import Allow, Deny, PolicyConfig, PolicyContext, decide
from orchestrator.shellparse import ShellParseError, parse_shell


@pytest.fixture
def ctx(tmp_path):
    repo = tmp_path / "repo"
    for d in (
        "apps/agent",
        "apps/merchant/src",
        "infra",
        "packages/db/src/schemas",
        ".sst",
        "services/migrate-service",
        "src",
    ):
        (repo / d).mkdir(parents=True)
    return PolicyContext(repo=repo)


def bash(ctx, cmd):
    return decide("Bash", {"command": cmd}, ctx)


def assert_soft(d, *needles):
    assert isinstance(d, Deny) and not d.hard_stop, d
    for n in needles:
        assert n in d.reason


def assert_hard(d):
    assert isinstance(d, Deny) and d.hard_stop, d


# ---- globs -----------------------------------------------------------------------------------


def test_glob_semantics():
    assert glob_match("apps/agent/**", "apps/agent/src/a.ts")
    assert glob_match("apps/agent/**", "apps/agent/a.ts")
    assert not glob_match("apps/agent/**", "apps/agent2/a.ts")
    assert glob_match("**/.env", ".env") and glob_match("**/.env", "a/b/.env")
    assert glob_match("apps/*/x.ts", "apps/m/x.ts") and not glob_match(
        "apps/*/x.ts", "apps/m/n/x.ts"
    )
    # brackets are literal: a real qinno file name
    assert glob_match(
        "apps/merchant/src/pages/pos/[orderId].tsx", "apps/merchant/src/pages/pos/[orderId].tsx"
    )
    assert not glob_match("a/[ab].ts", "a/a.ts")
    assert glob_match("sst.config.ts", "./sst.config.ts")
    assert not glob_match("infra/**", "../infra/x")


def test_pattern_could_hit():
    assert pattern_could_hit("apps/*", "apps/agent/**")
    assert pattern_could_hit("*", "apps/agent/**")
    assert not pattern_could_hit("*.log", "apps/agent/**")
    assert not pattern_could_hit("src/*", "apps/agent/**")
    assert pattern_could_hit("anything/*", "**/.env")


# ---- parser ----------------------------------------------------------------------------------


def test_parser_splits_chains_substitutions_and_redirects():
    cmds = parse_shell("a x && b | c; d $(e f) `g h` 'i j' > out 2>&1 &")
    argvs = [c.argv for c in cmds]
    assert ["a", "x"] in argvs and ["b"] in argvs and ["c"] in argvs
    assert ["e", "f"] in argvs and ["g", "h"] in argvs
    d = next(c for c in cmds if c.argv and c.argv[0] == "d")
    assert d.argv == ["d", "$(...)", "`...`", "i j"] and d.write_redirects == ["out"]


@pytest.mark.parametrize(
    "bad", ["cat <<EOF\nx\nEOF", "echo 'oops", 'echo "oops', "echo $(ls", "echo `ls"]
)
def test_parser_rejects_what_it_cannot_understand(bad):
    with pytest.raises(ShellParseError):
        parse_shell(bad)


# ---- allowed ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "ls -la",
        "yarn workspace @qinno/core test",
        "npx tsc --noEmit -p services/merchant-service",
        "git status --short && git diff --stat",
        "git log -n 3 --oneline | head",
        "cat apps/agent/README.md",  # reading forbidden areas is fine
        "rg -n 'DELETE FROM' src",  # SQL text in a search is not a destructive command
        "rg production src/",
        "echo hi > src/out.txt",
        "NODE_ENV=production yarn build",  # a build env is not the production stage
        "yarn build:production",
        "[ -f src/a.ts ] && echo yes",
        "for f in a b; do echo $f; done",
        "python3 script.py --flag",
        "prettier --check src/a.ts",
        "cd src && ls",
    ],
)
def test_allowed_commands(ctx, cmd):
    assert isinstance(bash(ctx, cmd), Allow), cmd


# ---- forbidden: git --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sub", ["push", "commit", "checkout", "restore", "reset", "clean", "stash"]
)
def test_each_forbidden_git_subcommand(ctx, sub):
    assert_soft(bash(ctx, f"git {sub} -x"), sub)


@pytest.mark.parametrize(
    "cmd",
    [
        "git push",
        "git -C . push",
        "git -c user.name=x commit -m hi",
        "git --no-pager stash",
        "/usr/bin/git push",
        "\\git push",
        "'git' push",
        "g''it push",
        'gi"t" push',
        "GIT_DIR=.git git push",
        "ls && git push",
        "ls; git push",
        "ls | git push",
        "ls || git push",
        "(git push)",
        "{ git push; }",
        "if true; then git push; fi",
        "echo $(git push)",
        'echo "$(git push)"',
        "echo `git push`",
        "bash -c 'git push'",
        'sh -c "echo ok; git commit -am x"',
        "bash -lc 'git reset --hard'",
        "bash -c \"bash -c 'git push'\"",
        "eval 'git push'",
        "eval git push",
        "env git push",
        "env -i FOO=1 git push",
        "env FOO=1 bash -c 'git push'",
        "command git push",
        "exec git push",
        "nohup git push &",
        "timeout 10 git push",
        "nice -n 5 git push",
        "xargs git push",
        "echo x | xargs -I{} git commit -m {}",
        r"find . -name x -exec git checkout {} \;",
        "git -c alias.p=push p",
        "git switch main",
        "git rebase main",
    ],
)
def test_git_bypass_attempts_are_denied(ctx, cmd):
    d = bash(ctx, cmd)
    assert isinstance(d, Deny), cmd


def test_command_word_that_is_a_variable_or_glob_is_denied(ctx):
    for cmd in ["$GIT push", "${CMD}", "g*t push", "$(echo git) push", "`echo git` push"]:
        assert isinstance(bash(ctx, cmd), Deny), cmd


# ---- forbidden: rm, sst, aws, etc ------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf build",
        "rm -fr build",
        "rm -r build",
        "rm -Rf build",
        "rm --recursive x",
        "ls && rm -rf x",
        "bash -c 'rm -rf x'",
        "xargs rm -rf",
        "find . -delete",
        "find . -exec rm -rf {} +",
    ],
)
def test_recursive_rm_and_find_delete(ctx, cmd):
    assert_soft(bash(ctx, cmd))


def test_plain_rm_of_a_file_is_allowed(ctx):
    assert isinstance(bash(ctx, "rm src/old.ts"), Allow)


@pytest.mark.parametrize(
    "cmd",
    [
        "npx sst deploy",
        "npx sst@latest deploy",
        "yarn sst deploy",
        "npm run sst -- deploy",
        "pnpm dlx sst deploy",
        "bunx sst dev",
        "./node_modules/.bin/sst deploy",
        "sst deploy --stage develop2",
        "aws sts get-caller-identity",
        "aws s3 ls",
        "drizzle-kit push",
        "npx drizzle-kit push",
        "yarn drizzle-kit push",
        "yarn workspace @qinno/migrate-service migrate",
        "yarn workspace @qinno/migrate-service seed:merchant",
        "npm run sync-secrets",
        "psql -c 'select 1'",
        "ssh host",
        "sudo ls",
        "gh pr create",
    ],
)
def test_infra_and_db_commands_are_soft_denied(ctx, cmd):
    assert_soft(bash(ctx, cmd))


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 -c 'import os'",
        "python -c 1",
        "python3.12 -c 1",
        "node -e 'x()'",
        "perl -e 1",
        "ruby -e 1",
        "bash -c 'node -p 1'",
    ],
)
def test_inline_interpreter_code_is_denied(ctx, cmd):
    assert_soft(bash(ctx, cmd), "inline code")


def test_heredoc_and_unbalanced_are_denied_as_unanalysable(ctx):
    assert_soft(bash(ctx, "cat <<EOF > x\nhi\nEOF"), "cannot analyse")
    assert_soft(bash(ctx, "echo 'x"), "cannot analyse")


# ---- hard stops: production / staging --------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "yarn workspace @qinno/migrate-service migrate:production",
        "yarn migrate:staging",
        "npm run seed:production",
        "npm run sync-secrets -- --stage=production",
        "npm run sync-secrets -- --stage staging",
        "sst deploy --stage production",
        "npx sst deploy --stage=prod",
        "SST_STAGE=production yarn x",
        "env SST_STAGE=production yarn x",
        "FOO=1 STAGE=staging yarn x",
        "bash -c 'SST_STAGE=production yarn seed'",
        "ls && sst deploy --stage production",
        "DB_NAME=qinno yarn x",
        "DB_NAME=qinno_production yarn x",
        "aws s3 rm s3://bucket --recursive",
        "aws dynamodb delete-table --table-name x",
        "psql -c 'DROP TABLE users'",
        'psql -c "delete from users"',
        "mysql -e 'TRUNCATE users'",
    ],
)
def test_production_and_data_destruction_are_hard_stops(ctx, cmd):
    assert_hard(bash(ctx, cmd))


def test_hard_stop_outranks_an_earlier_soft_deny(ctx):
    assert_hard(bash(ctx, "git push && SST_STAGE=production yarn x"))


# ---- file tools and paths --------------------------------------------------------------------


def w(ctx, tool, path, **extra):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return decide(tool, {key: path, **extra}, ctx)


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
@pytest.mark.parametrize(
    "path",
    [
        "apps/agent/src/main.ts",
        "infra/db.ts",
        "sst.config.ts",
        ".github/workflows/ci.yml",
        "apps/merchant/.env",
        ".env.local",
        "src/updater/x.ts",
        "apps/x/signing/key.json",
        "release.keystore",
        ".claude/settings.json",
        ".claude/hooks/guard.sh",
        "../outside.txt",
    ],
)
def test_forbidden_paths_for_every_write_tool(ctx, tool, path):
    assert_soft(w(ctx, tool, path))


def test_absolute_paths_dotdot_and_symlinks_are_resolved(ctx):
    repo = ctx.repo
    assert_soft(w(ctx, "Write", str(repo / "infra" / "x.ts")))
    assert_soft(w(ctx, "Write", "src/../infra/x.ts"))
    os.symlink(repo / "infra", repo / "src" / "link")
    assert_soft(w(ctx, "Write", "src/link/x.ts"))
    assert isinstance(w(ctx, "Write", "src/ok.ts"), Allow)
    assert isinstance(w(ctx, "Edit", "apps/merchant/src/a.tsx"), Allow)


def test_stage_and_migrate_env_writes_are_hard_stops(ctx):
    assert_hard(w(ctx, "Write", ".sst/stage"))
    assert_hard(w(ctx, "Edit", "services/migrate-service/.env"))
    assert_hard(bash(ctx, "echo production > .sst/stage"))
    assert_hard(bash(ctx, "echo DB_NAME=qinno > services/migrate-service/.env"))


def test_schema_files_need_the_contract(ctx):
    path = "packages/db/src/schemas/roles.ts"
    assert_hard(w(ctx, "Edit", path))
    ok = PolicyContext(repo=ctx.repo, allowed_schema_globs=("packages/db/src/schemas/roles.ts",))
    assert isinstance(w(ok, "Edit", path), Allow)
    assert_hard(w(ok, "Edit", "packages/db/src/schemas/users.ts"))  # not in the contract


def test_writes_outside_repo_only_in_temp_or_extra_dirs(ctx, tmp_path):
    assert_soft(w(ctx, "Write", "/etc/hosts"))
    assert isinstance(w(ctx, "Write", "/tmp/scratch.txt"), Allow)
    extra = tmp_path / "memory"
    extra.mkdir()
    ok = PolicyContext(repo=ctx.repo, extra_writable_dirs=(extra,))
    assert isinstance(w(ok, "Write", str(extra / "confidence.jsonl")), Allow)
    assert_soft(w(ok, "Write", str(extra / "memory.jsonl")))  # graph file: MCP only
    assert_soft(bash(ok, f"echo x > {extra}/memory.jsonl"))
    assert isinstance(bash(ok, f"echo x > {extra}/confidence.jsonl"), Allow)


@pytest.mark.parametrize(
    "cmd",
    [
        "echo x > infra/a.ts",
        "echo x >> apps/agent/a.ts",
        "echo x &> infra/a",
        "cat a >| infra/a",
        "ls | tee infra/a",
        "cp src/a.ts infra/a.ts",
        "mv src/a.ts apps/agent/a.ts",
        "sed -i s/a/b/ infra/a.ts",
        "touch sst.config.ts",
        "rm infra/a.ts",
        "cd infra && touch x",
        "cd apps && rm agent/x.ts",
        "bash -c 'echo x > infra/a'",
        "xargs rm infra/a",
        "echo x > /etc/passwd",
        "cp -r src apps/*",
        "mv * dest",
        "rm apps/*/x.ts",
        "yarn prettier --write infra/a.ts",
        "echo x > $F",
    ],
)
def test_bash_cannot_write_forbidden_paths(ctx, cmd):
    assert isinstance(bash(ctx, cmd), Deny), cmd


@pytest.mark.parametrize(
    "cmd",
    ["echo x > /dev/null", "ls 2>&1", "ls 2>/dev/null", "echo x > /tmp/a.txt", "cat infra/a.ts"],
)
def test_harmless_redirects_and_reads(ctx, cmd):
    assert isinstance(bash(ctx, cmd), Allow), cmd


# ---- other tools / MCP -----------------------------------------------------------------------


def test_ask_user_question_is_answered_by_policy(ctx):
    d = decide("AskUserQuestion", {"questions": []}, ctx)
    assert_soft(d, "AUTONOMOUS-DECISIONS.md")


def test_read_only_tools_and_unknown_tools(ctx):
    for t in ("Read", "Glob", "Grep", "WebFetch", "TodoWrite", "Task"):
        assert isinstance(decide(t, {}, ctx), Allow)
    assert_soft(decide("SomeNewTool", {}, ctx), "unknown tool")


def test_memory_mcp_rules(ctx):
    assert isinstance(decide("mcp__memory__open_nodes", {"names": ["x"]}, ctx), Allow)
    assert isinstance(
        decide(
            "mcp__memory__add_observations",
            {"observations": [{"entityName": "Agent Orchestrator", "contents": ["x"]}]},
            ctx,
        ),
        Allow,
    )
    assert_hard(decide("mcp__memory__delete_entities", {"entityNames": ["x"]}, ctx))
    assert_soft(decide("mcp__memory__read_graph", {}, ctx), "never allowed")
    for name in ("Memory Model", "Graph Legend", "Playbook"):
        assert_hard(
            decide(
                "mcp__memory__add_observations",
                {"observations": [{"entityName": name, "contents": ["x"]}]},
                ctx,
            )
        )
        assert_hard(decide("mcp__memory__create_entities", {"entities": [{"name": name}]}, ctx))
        assert_hard(
            decide(
                "mcp__memory__create_relations",
                {"relations": [{"from": "a", "to": name, "relationType": "r"}]},
                ctx,
            )
        )
    assert_soft(decide("mcp__slack__post_message", {}, ctx), "allow list")


# ---- config can only tighten -----------------------------------------------------------------


def test_pipeline_config_tightens_but_never_loosens(ctx):
    base = PolicyConfig.default()
    tight = base.tightened(
        {"forbidden_write_globs": ["docs/secret/**"], "forbidden_git_subcommands": ["add"]}
    )
    assert "docs/secret/**" in tight.forbidden_write_globs
    assert set(base.forbidden_write_globs) <= set(tight.forbidden_write_globs)
    assert base.forbidden_git_subcommands <= tight.forbidden_git_subcommands
    c = PolicyContext(repo=ctx.repo, config=tight)
    assert_soft(w(c, "Write", "docs/secret/a.md"))
    assert_soft(bash(c, "git add -A"))
    with pytest.raises(ValueError):
        base.tightened({"allowed_mcp_prefixes": ["mcp__slack__"]})
    with pytest.raises(ValueError):
        base.tightened({"forbidden_write_globs": "docs/**"})


def test_extra_writable_file_opens_exactly_that_file(ctx, tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    ok = PolicyContext(repo=ctx.repo, extra_writable_files=(d / "confidence.jsonl",))
    assert isinstance(w(ok, "Write", str(d / "confidence.jsonl")), Allow)
    assert isinstance(w(ok, "Edit", str(d / "confidence.jsonl")), Allow)
    assert_soft(w(ok, "Write", str(d / "other.txt")))  # same directory, other file
    assert_soft(w(ok, "Write", str(d / "confidence.jsonl.bak")))
    assert_soft(w(ok, "Write", str(d / "sub" / "confidence.jsonl")))
    assert_soft(w(ok, "Write", str(d / "memory.jsonl")))  # graph file: MCP only
    assert_soft(w(ctx, "Write", str(d / "confidence.jsonl")))  # not opened by default
    # a symlink in the repo cannot smuggle a write to another file in that directory
    (ctx.repo / "link").symlink_to(d / "memory.jsonl")
    assert_soft(w(ok, "Write", str(ctx.repo / "link")))
    assert_soft(bash(ok, f"echo x > {d}/other.txt"))
    assert isinstance(bash(ok, f"echo x >> {d}/confidence.jsonl"), Allow)


def test_extra_writable_file_still_denies_heredoc_and_inline_python(ctx, tmp_path):
    f = tmp_path / "confidence.jsonl"
    ok = PolicyContext(repo=ctx.repo, extra_writable_files=(f,))
    assert not isinstance(bash(ok, f"cat <<EOF >> {f}\nx\nEOF"), Allow)
    assert not isinstance(bash(ok, f"python3 -c \"open('{f}','a').write('x')\""), Allow)
