"""Policy (contract §5; readme §6): `decide(tool_name, tool_input, ctx) -> Allow | Deny`.

Pure and SDK-free so it can be unit tested exhaustively. `runner.py` wires it into the SDK twice
(a PreToolUse hook that sees every call, and `can_use_tool` for calls that fall through to a prompt;
see docs/notes/agent-sdk.md §2).

Two kinds of Deny:
  * soft  (`hard_stop=False`): a rule the agent should adapt to ("use the Write tool", "orchestrator
    runs migrations"). The agent keeps working.
  * hard  (`hard_stop=True`): the step ends BLOCKED and a human is told (production/staging reach,
    destructive data operations, graph "ask first" actions, schema outside the contract).

Unknown means deny: a Bash command the parser cannot understand, an unknown tool, or a command word
that is still a variable/glob is refused rather than guessed at.

Config is data. A pipeline may only *tighten* the defaults (`PolicyConfig.tightened`).
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from orchestrator.globs import matches_any, normalize_rel, pattern_could_hit
from orchestrator.shellparse import ShellParseError, parse_shell


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str
    hard_stop: bool = False


Decision = Allow | Deny


# ---- configuration --------------------------------------------------------------------------

_DEFAULT_FORBIDDEN_WRITE_GLOBS = (
    "apps/agent/**",
    "infra/**",
    "sst.config.ts",
    ".github/**",
    ".gitlab-ci.yml",
    ".circleci/**",
    # signing / updater: exact qinno paths are not stated in the plan files; these are a broad net
    # and a pipeline adds precise ones via `forbidden_write_globs`.
    "**/updater/**",
    "**/*signing*/**",
    "**/*.keystore",
    "**/*.p12",
    "**/*.pem",
    # secrets and the agent's own guard rails
    "**/.env",
    "**/.env.*",
    "**/.claude/settings.json",
    "**/.claude/settings.local.json",
    "**/.claude/hooks/**",
    # the memory graph is written through the memory MCP only
    "**/memory.jsonl",
)
_DEFAULT_HARD_STOP_WRITE_GLOBS = (
    ".sst/**",  # switching the SST stage
    "**/migrate-service/.env",
    "**/migrate-service/.env.*",
)
_DEFAULT_SCHEMA_GLOBS = ("packages/db/src/schemas/**", "packages/db/src/schema/**")

_DEFAULT_FORBIDDEN_GIT = frozenset(
    {
        "push",
        "commit",
        "checkout",
        "restore",
        "reset",
        "clean",
        "stash",
        # same effect as the above under another name, or rewrites history / the work tree
        "switch",
        "rebase",
        "merge",
        "cherry-pick",
        "revert",
        "am",
        "worktree",
        "filter-branch",
        "update-ref",
        "gc",
        "prune",
    }
)
_DEFAULT_FORBIDDEN_COMMANDS = frozenset(
    {
        "sudo",
        "su",
        "doas",
        "ssh",
        "scp",
        "sftp",
        "gh",
        "aws",
        "sst",
        "drizzle-kit",
        "psql",
        "pg_dump",
        "pg_restore",
        "mysql",
        "sqlite3",
        "mongosh",
        "redis-cli",
    }
)
_DEFAULT_ALLOWED_MCP_PREFIXES = ("mcp__memory__", "mcp__mcplens__", "mcp__codebase-memory__")
_DEFAULT_HARD_STOP_MCP = frozenset({"mcp__memory__delete_entities"})
_DEFAULT_DENIED_MCP = frozenset({"mcp__memory__read_graph"})
_DEFAULT_PROTECTED_ENTITIES = frozenset({"Memory Model", "Graph Legend", "Playbook"})

_TIGHTENABLE = {
    "forbidden_write_globs",
    "hard_stop_write_globs",
    "forbidden_git_subcommands",
    "forbidden_commands",
    "hard_stop_mcp_tools",
    "denied_mcp_tools",
    "protected_entities",
    "schema_globs",
}


@dataclass(frozen=True)
class PolicyConfig:
    forbidden_write_globs: tuple[str, ...] = _DEFAULT_FORBIDDEN_WRITE_GLOBS
    hard_stop_write_globs: tuple[str, ...] = _DEFAULT_HARD_STOP_WRITE_GLOBS
    schema_globs: tuple[str, ...] = _DEFAULT_SCHEMA_GLOBS
    forbidden_git_subcommands: frozenset[str] = _DEFAULT_FORBIDDEN_GIT
    forbidden_commands: frozenset[str] = _DEFAULT_FORBIDDEN_COMMANDS
    allowed_mcp_prefixes: tuple[str, ...] = _DEFAULT_ALLOWED_MCP_PREFIXES
    hard_stop_mcp_tools: frozenset[str] = _DEFAULT_HARD_STOP_MCP
    denied_mcp_tools: frozenset[str] = _DEFAULT_DENIED_MCP
    protected_entities: frozenset[str] = _DEFAULT_PROTECTED_ENTITIES

    @classmethod
    def default(cls) -> PolicyConfig:
        return cls()

    def tightened(self, extra: Mapping[str, Iterable[str]]) -> PolicyConfig:
        """Union `extra` into the deny lists. Keys that could loosen the policy are an error."""
        unknown = set(extra) - _TIGHTENABLE
        if unknown:
            raise ValueError(
                f"policy config may only tighten {sorted(_TIGHTENABLE)}; got {sorted(unknown)}"
            )
        changes: dict[str, object] = {}
        for key, values in extra.items():
            if isinstance(values, str):
                raise ValueError(f"{key}: expected a list of strings")
            current = getattr(self, key)
            merged = [*current, *values] if isinstance(current, tuple) else {*current, *values}
            changes[key] = (
                tuple(dict.fromkeys(merged)) if isinstance(current, tuple) else (frozenset(merged))
            )
        return replace(self, **changes)  # type: ignore[arg-type]


@dataclass
class PolicyContext:
    repo: Path
    config: PolicyConfig = field(default_factory=PolicyConfig.default)
    # Schema files this session's contract allows the agent to change. Empty = none.
    allowed_schema_globs: tuple[str, ...] = ()
    # Extra directories outside the repo the agent may write (system temp is always allowed).
    extra_writable_dirs: tuple[Path, ...] = ()
    # ① plan phase: nothing may be written, whatever the permission mode turns out to be. (A key in
    # the SDK env forces mode `default`, so SDK `plan` mode cannot be relied on to be read-only.)
    read_only: bool = False


# ---- tool sets ------------------------------------------------------------------------------

_WRITE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
# read-only tools that take a path-like argument; `.env*` files are secrets (S3 3b)
_READ_TOOLS = {
    "Read": ("file_path",),
    "NotebookRead": ("notebook_path",),
    "Grep": ("path", "glob"),
    "Glob": ("pattern", "path"),
}
_SAFE_TOOLS = frozenset(
    {
        "LS",
        "WebFetch",
        "WebSearch",
        "TodoWrite",
        "Task",
        "Agent",
        "Skill",
        "ExitPlanMode",
        "EnterPlanMode",
        "BashOutput",
        "KillBash",
        "KillShell",
        "ToolSearch",
        "TaskOutput",
        "TaskStop",
        "TaskCreate",
        "TaskUpdate",
        "TaskGet",
        "TaskList",
    }
)

# ---- bash analysis tables -------------------------------------------------------------------

_KEYWORDS = frozenset(
    {
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "do",
        "done",
        "while",
        "until",
        "for",
        "case",
        "esac",
        "select",
        "function",
        "coproc",
        "!",
        "{",
        "}",
        "time",
    }
)
_WRAPPERS = frozenset(
    {
        "env",
        "command",
        "builtin",
        "exec",
        "nohup",
        "nice",
        "ionice",
        "timeout",
        "stdbuf",
        "setsid",
        "xargs",
        "watch",
        "caffeinate",
        "unbuffer",
        "chronic",
        "parallel",
        "script",
    }
)
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_INLINE_CODE_FLAGS = {
    "python": {"-c"},
    "node": {"-e", "-p", "--eval", "--print"},
    "perl": {"-e", "-E"},
    "ruby": {"-e"},
    "php": {"-r"},
    "deno": {"eval"},
    "bun": {"-e"},
    "tsx": {"-e", "--eval"},
    "osascript": {"-e"},
}
_VERSION_SUFFIX = re.compile(r"[\d.]+\Z")  # python3.12 -> python, node22 -> node
_RUNNERS = frozenset(
    {"npx", "pnpx", "bunx", "yarn", "npm", "pnpm", "bun", "corepack", "node", "tsx"}
)
_RUNNER_FORBIDDEN_TOKEN = re.compile(
    r"^(?:sst|drizzle-kit|aws)(?:@.*)?$|(?:^|/)(?:sst|drizzle-kit)(?:\.js)?$"
)
_RUNNER_FORBIDDEN_SCRIPT = re.compile(r"^(?:migrate|seed|deploy|sync-secrets|release)(?::[\w-]+)?$")
_PROD_VALUES = frozenset({"production", "prod", "staging"})
_PROD_SCRIPT = re.compile(
    r"^(?:migrate|seed|deploy|release|db|sst)[\w-]*:(?:production|prod|staging)$"
)
_STAGE_FLAGS = frozenset({"--stage", "-s", "--env", "--environment"})
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.DOTALL)
_PROD_ASSIGN_KEYS = frozenset({"SST_STAGE", "STAGE"})
_PROD_DB = re.compile(r"^(?:qinno|qinno_(?:prod|production|staging))\Z")
_DESTRUCTIVE_SQL = re.compile(
    r"\b(?:drop\s+(?:table|database|schema|index|column)|truncate\b|delete\s+from)\b", re.I
)
_DB_CLIENTS = frozenset({"psql", "mysql", "sqlite3", "mongosh", "redis-cli", "pg_restore"})
_AWS_DESTRUCTIVE = re.compile(r"(?:^|-)(?:delete|remove|terminate|drop|rm|purge|destroy)", re.I)
_GIT_READONLY = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "ls-files",
        "blame",
        "rev-parse",
        "describe",
        "shortlog",
        "grep",
        "cat-file",
        "ls-tree",
        "rev-list",
        "show-ref",
        "for-each-ref",
        "name-rev",
        "branch",
        "tag",
        "remote",
        "config",
        "ls-remote",
        "version",
        "help",
        "add",
        "check-ignore",
        "diff-tree",
        "diff-index",
        "merge-base",
        "reflog",
        "var",
        "whatchanged",
    }
)
_READONLY_COMMANDS = frozenset(
    {
        "cat",
        "ls",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "wc",
        "diff",
        "cmp",
        "tree",
        "file",
        "stat",
        "jq",
        "yq",
        "awk",
        "sort",
        "uniq",
        "cut",
        "tr",
        "echo",
        "printf",
        "pwd",
        "which",
        "whereis",
        "type",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "du",
        "df",
        "date",
        "sleep",
        "true",
        "false",
        "test",
        "[",
        "[[",
        "xxd",
        "hexdump",
        "shasum",
        "sha256sum",
        "md5",
        "md5sum",
        "cd",
        "pushd",
        "popd",
        "wait",
        "read",
        "set",
        "export",
        "unset",
        "local",
        "declare",
        "tsc",
        "eslint",
        "prettier",
        "vitest",
        "jest",
        "pytest",
        "ruff",
        "mypy",
        "find",
        "xargs",
        "env",
        "printenv",
        "uname",
        "whoami",
        "id",
        "ps",
        "lsof",
    }
)
_WRITE_FLAGS = frozenset(
    {
        "--write",
        "-w",
        "--fix",
        "--fix-type",
        "-i",
        "--in-place",
        "format",
        "--outDir",
        "--out-dir",
        "--outFile",
        "-o",
        "--output",
        "--init",
    }
)


# ---- read-only phase (S3: the plan step) ----------------------------------------------------
# A deliberately SMALL allowlist: no awk/sed/xargs/env/shells (they can write or run anything).
_RO_COMMANDS = frozenset(
    {
        "cat", "ls", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "wc", "diff",
        "cmp", "tree", "file", "stat", "jq", "cut", "uniq", "sort", "echo", "printf", "pwd",
        "which", "basename", "dirname", "realpath", "readlink", "du", "df", "date", "sleep", "true",
        "false", "test", "[", "[[", "cd", "find", "git", "ps", "uname", "whoami", "id",
    }
)  # fmt: skip
_RO_GIT = frozenset(
    {
        "status", "diff", "log", "show", "ls-files", "blame", "rev-parse", "describe", "shortlog",
        "grep", "cat-file", "ls-tree", "rev-list", "show-ref", "for-each-ref", "name-rev",
        "merge-base", "diff-tree", "diff-index", "whatchanged", "check-ignore", "version", "help",
    }
)  # fmt: skip
_RO_FIND_WRITES = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)


def _read_only_violation(cands: list[list[str]], redirects: list[str]) -> Deny | None:
    why = "this is the read-only planning step: no file may be changed"
    for target in redirects:
        if target not in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            return Deny(f"redirect to {target}: {why}")
    for head, *args in cands:
        if head not in _RO_COMMANDS:
            return Deny(
                f"`{head}`: {why}; only read commands (cat, ls, grep, git status/diff/log...)"
            )
        if head == "git" and _git_sub(args) not in _RO_GIT:
            return Deny(f"`git {_git_sub(args)}`: {why}")
        if head == "find" and any(a in _RO_FIND_WRITES for a in args):
            return Deny(f"`find {' '.join(a for a in args if a in _RO_FIND_WRITES)}`: {why}")
        if head == "sort" and any(
            a in ("-o", "--output") or a.startswith("--output=") for a in args
        ):
            return Deny(f"`sort -o`: {why}")
    return None


# ---- secret exposure (S3 3b) ----------------------------------------------------------------
# The SDK gets ANTHROPIC_API_KEY in its process env, which every Bash tool call inherits. The agent
# must not be able to print it: no environment dumps, no references to secret-looking variables, no
# reading `.env*` files. (A script file the agent writes and runs can still read process.env; see
# docs/planning/orchestrator-v1/handoff-s3.md "Limits".)
_SECRET_VAR = re.compile(
    r"\$\{?[A-Za-z_]*(?:API_?KEY|TOKEN|SECRET|PASSW(?:OR)?D|CREDENTIAL)[A-Za-z_]*", re.I
)
_SECRET_NAME_WORD = re.compile(
    r"^(?:ANTHROPIC_[A-Z_]+|CLAUDE_CODE_OAUTH_TOKEN|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)\Z"
)
# `.env`, `.env.local`, `.env.production`, `foo.env` are secrets; examples/templates are not.
_ENV_FILE = re.compile(r"^(?:\.env(?:\..+)?|.+\.env)\Z", re.I)
_ENV_TEMPLATE = re.compile(r"\.(?:example|sample|template|dist|defaults)\Z", re.I)
_ENV_FILE_NAMES = (".env", ".env.local", ".env.production", ".env.development")
# these only look at a name/size/metadata, never at file content
_NO_CONTENT_READ = frozenset({"ls", "stat", "test", "[", "[[", "wc", "file", "du", "df", "rm"})
_ENV_OPTS_WITH_VALUE = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-P"})


def _is_env_file(name: str) -> bool:
    base = os.path.basename(name.rstrip("/"))
    return bool(_ENV_FILE.match(base)) and not _ENV_TEMPLATE.search(base)


def _word_names_env_file(word: str) -> bool:
    """`.env`, `./a/.env.local`, `HEAD:.env`, and wildcard words that could expand to one."""
    word = word.strip("'\"")
    for piece in {word, word.rsplit(":", 1)[-1]}:
        base = os.path.basename(piece.rstrip("/"))
        if _has_wild(base):
            # a shell glob never matches a leading dot unless the pattern spells it
            if base.startswith(".") and any(fnmatch.fnmatchcase(n, base) for n in _ENV_FILE_NAMES):
                return True
        elif _is_env_file(base):
            return True
    return False


def _env_dump(head: str, args: list[str]) -> str | None:
    """Reason if this command prints the process environment, else None."""
    flags = [a for a in args if a.startswith("-")]
    if head == "printenv":
        return "`printenv` is not available: it prints the process environment"
    if head == "env":
        rest, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in _ENV_OPTS_WITH_VALUE:
                skip = True
            elif a.startswith("-") or _ASSIGN.match(a):
                continue
            else:
                rest.append(a)
        if not rest:
            return "`env` without a command prints the process environment"
    if head == "export" and (not args or any("p" in f[1:] for f in flags if f[1:2] != "-")):
        return "`export` without a variable lists the environment"
    if head in ("declare", "typeset", "readonly") and (
        not args or any(set("px") & set(f[1:]) for f in flags if f[1:2] != "-")
    ):
        return f"`{head}` would list shell variables"
    if head == "set" and not args:
        return "`set` without arguments lists shell variables"
    if head == "compgen" and any(f in ("-e", "-v") for f in flags):
        return "`compgen -e/-v` lists environment variables"
    if head == "ps" and (
        any("E" in a[1:] for a in flags if f"{a}"[1:2] != "-")  # macOS `ps -E`
        or (args and re.fullmatch(r"[A-Za-z]+", args[0]) and "e" in args[0].lower())  # BSD `ps eww`
    ):
        return "`ps` with an environment flag prints other processes' environment"
    return None


def _secret_exposure(head: str, args: list[str], reads: list[str]) -> str | None:
    if reason := _env_dump(head, args):
        return reason
    for word in args:
        if _SECRET_VAR.search(word) or _SECRET_NAME_WORD.match(word.strip("'\"")):
            return (
                f"`{word}` refers to a secret-looking variable; secrets are not available to agents"
            )
    if head not in _NO_CONTENT_READ:
        for word in (*args, *reads):
            if _word_names_env_file(word):
                return (
                    f"`{head} {word}`: reading .env files is not allowed (secrets); "
                    ".env.example / .env.sample are fine"
                )
    return None


# ---- helpers --------------------------------------------------------------------------------


def _basename(word: str) -> str:
    return os.path.basename(word) if "/" in word else word


def _has_unresolved(word: str) -> bool:
    return any(ch in word for ch in "$*?[~{`")


def _norm_stage_value(v: str) -> bool:
    return v.strip("'\"").lower() in _PROD_VALUES


def _candidates(
    argv: list[str], assignments: list[str], redirects: list[str], depth: int = 0
) -> list[list[str]]:
    """Every argv that could actually execute, after unwrapping keywords/assignments/wrappers."""
    if depth > 6:
        raise ShellParseError("wrapper nesting too deep")
    argv = list(argv)
    while argv and (argv[0] in _KEYWORDS or _ASSIGN.match(argv[0])):
        if _ASSIGN.match(argv[0]):
            assignments.append(argv[0])
        argv = argv[1:]
    if not argv:
        return []
    head = _basename(argv[0])
    out: list[list[str]] = [[head, *argv[1:]]]

    if head in _WRAPPERS:
        for k in range(1, len(argv)):  # any suffix may be the wrapped command
            out.extend(_candidates(argv[k:], assignments, redirects, depth + 1))
    if head in _SHELLS or head in {"eval", "su"}:
        scripts: list[str] = []
        if head == "eval":
            scripts.append(" ".join(argv[1:]))
        for idx, a in enumerate(argv[1:], 1):
            if (
                a.startswith("-")
                and not a.startswith("--")
                and "c" in a[1:]
                and idx + 1 < len(argv)
            ):
                scripts.append(argv[idx + 1])
        for script in scripts:
            for sub in parse_shell(script, depth + 1):
                redirects.extend(sub.write_redirects)
                out.extend(_candidates(sub.argv, assignments, redirects, depth + 1))
    if head == "find":
        for idx, a in enumerate(argv):
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                out.extend(_candidates(argv[idx + 1 :], assignments, redirects, depth + 1))
    return out


# ---- bash ----------------------------------------------------------------------------------


def _check_git(args: list[str], cfg: PolicyConfig) -> Deny | None:
    i, sub = 0, None
    takes_value = {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--exec-path",
    }
    while i < len(args):
        a = args[i]
        if a in takes_value:
            if a == "-c" and i + 1 < len(args) and args[i + 1].lower().startswith("alias."):
                return Deny("git aliases can hide forbidden subcommands")
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        sub = a
        break
    if sub is None:
        return None
    if _has_unresolved(sub):
        return Deny(f"git subcommand {sub!r} cannot be resolved statically")
    if sub in cfg.forbidden_git_subcommands:
        return Deny(
            f"`git {sub}` is not allowed: the orchestrator owns the work tree "
            "(no commits, pushes or checkouts); leave your changes uncommitted"
        )
    return None


def _rm_recursive(args: list[str]) -> bool:
    for a in args:
        if a == "--recursive":
            return True
        if a.startswith("-") and not a.startswith("--") and ("r" in a[1:] or "R" in a[1:]):
            return True
    return False


def _virtual_path(word: str, cwd: Path, repo: Path) -> str | None:
    """Repo-relative path for a word, or None if it points outside the repo / is unresolvable."""
    if _has_unresolved(word):
        return None
    try:
        p = Path(word)
        full = (p if p.is_absolute() else cwd / p).resolve()
        return normalize_rel(str(full.relative_to(repo.resolve())))
    except (ValueError, OSError):
        return None


def _path_violation(rel: str, ctx: PolicyContext) -> Deny | None:
    if hit := matches_any(ctx.config.hard_stop_write_globs, rel):
        return Deny(
            f"{rel} matches protected path {hit}: changing it can reach production", hard_stop=True
        )
    if hit := matches_any(ctx.config.forbidden_write_globs, rel):
        return Deny(f"{rel} is off limits for agents (rule {hit})")
    # same rule for the Write tools and for shell writes (`sed -i`, `> file`, `tee`)
    if matches_any(ctx.config.schema_globs, rel) and not matches_any(ctx.allowed_schema_globs, rel):
        return Deny(
            f"{rel} is a database schema file outside this session's contract: "
            "schema changes need a human decision",
            hard_stop=True,
        )
    return None


def _is_writable_outside(path: Path, ctx: PolicyContext) -> bool:
    repo = ctx.repo.resolve()
    temp_roots = [
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),
        Path("/private/tmp").resolve(),
    ]
    # a temp dir that contains the repo itself would make the repo's siblings writable
    roots = [r for r in temp_roots if r != repo and r not in repo.parents]
    roots += [d.resolve() for d in ctx.extra_writable_dirs]
    return any(path == r or r in path.parents for r in roots)


def _decide_bash(command: str, ctx: PolicyContext) -> Decision:
    cfg = ctx.config
    try:
        simple = parse_shell(command)
    except ShellParseError as exc:
        return Deny(
            f"cannot analyse this shell command ({exc}); write files with the Write tool "
            "and keep commands simple"
        )
    if not simple:
        return Allow()

    repo = ctx.repo
    cwd = repo
    cwd_unknown = False
    soft: Deny | None = None

    for sc in simple:
        assignments: list[str] = []
        nested_redirects: list[str] = []
        try:
            cands = _candidates(sc.argv, assignments, nested_redirects)
        except ShellParseError as exc:
            return Deny(f"cannot analyse this shell command ({exc})")

        # --- hard stops first: they outrank every soft rule -------------------------------
        for a in assignments:
            m = _ASSIGN.match(a)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip("'\"")
            if (key in _PROD_ASSIGN_KEYS and _norm_stage_value(val)) or (
                key == "DB_NAME" and _PROD_DB.match(val)
            ):
                return Deny(f"{a}: production/staging access is a hard stop", hard_stop=True)
        for cand in cands:
            head, args = cand[0], cand[1:]
            prev = ""
            for tok in args:
                if (
                    tok.startswith("--stage=")
                    or tok.startswith("--env=")
                    or tok.startswith("--environment=")
                ):
                    if _norm_stage_value(tok.split("=", 1)[1]):
                        return Deny(
                            f"{tok}: production/staging access is a hard stop", hard_stop=True
                        )
                if prev in _STAGE_FLAGS and _norm_stage_value(tok):
                    return Deny(
                        f"{prev} {tok}: production/staging access is a hard stop", hard_stop=True
                    )
                if _PROD_SCRIPT.match(tok):
                    return Deny(
                        f"{tok}: production/staging scripts are a hard stop", hard_stop=True
                    )
                m = _ASSIGN.match(tok)
                if m and (
                    (m.group(1) in _PROD_ASSIGN_KEYS and _norm_stage_value(m.group(2)))
                    or (m.group(1) == "DB_NAME" and _PROD_DB.match(m.group(2).strip("'\"")))
                ):
                    return Deny(f"{tok}: production/staging access is a hard stop", hard_stop=True)
                prev = tok
            if head in _DB_CLIENTS and _DESTRUCTIVE_SQL.search(" ".join(args)):
                return Deny(
                    "destructive SQL through a database client is a hard stop", hard_stop=True
                )
            if head == "aws" and any(_AWS_DESTRUCTIVE.search(t) for t in args[:3]):
                return Deny("destructive aws command is a hard stop", hard_stop=True)

        # --- soft rules ------------------------------------------------------------------
        if ctx.read_only and (
            d := _read_only_violation(cands, [*sc.write_redirects, *nested_redirects])
        ):
            soft = soft or d
        for a in assignments:
            if _SECRET_VAR.search(a):
                soft = soft or Deny(
                    f"`{a.split('=', 1)[0]}=...` copies a secret-looking variable; secrets are not "
                    "available to agents"
                )
        for cand in cands:
            head, args = cand[0], cand[1:]
            if head not in ("[", "[[") and _has_unresolved(head):
                return Deny(f"command word {head!r} contains a variable/glob and cannot be checked")

            if reason := _secret_exposure(head, args, sc.read_redirects):
                soft = soft or Deny(reason)
            if head in cfg.forbidden_commands:
                soft = soft or Deny(
                    f"`{head}` is not available to agents: infrastructure, database and "
                    "deployment commands are run by the orchestrator"
                )
            if head == "git":
                if d := _check_git(args, cfg):
                    soft = soft or d
            if head == "rm" and _rm_recursive(args):
                soft = soft or Deny("recursive `rm` is not allowed; delete specific files")
            if head == "find" and ("-delete" in args):
                soft = soft or Deny("`find -delete` is not allowed")
            inline_key = _VERSION_SUFFIX.sub("", head)
            if inline_key in _INLINE_CODE_FLAGS and any(
                a in _INLINE_CODE_FLAGS[inline_key] for a in args
            ):
                soft = soft or Deny(
                    f"inline code for `{head}` cannot be analysed; put it in a script file"
                )
            if head in _RUNNERS:
                for tok in args:
                    if _RUNNER_FORBIDDEN_TOKEN.match(tok) or _RUNNER_FORBIDDEN_SCRIPT.match(tok):
                        soft = soft or Deny(
                            f"`{head} ... {tok}`: sst/drizzle/aws and migrate/seed/deploy scripts "
                            "are run by the orchestrator, not by agents"
                        )
                        break
            if head == "cd" or head in ("pushd",):
                target = next((a for a in args if not a.startswith("-")), None)
                if target is None:
                    cwd_unknown = True
                else:
                    vp = _virtual_path(target, cwd, repo)
                    if vp is None:
                        cwd_unknown = True
                    else:
                        cwd = (repo / vp).resolve()

            # path rules: anything that is not provably read-only must not touch forbidden paths
            read_only = head in _READONLY_COMMANDS or (
                head == "git" and _git_sub(args) in _GIT_READONLY
            )
            if read_only and head in {"tsc", "eslint", "prettier", "ruff", "find", "xargs", "env"}:
                read_only = not any(a in _WRITE_FLAGS or a.startswith("--fix") for a in args)
            for word in args:
                if word.startswith("-") or word in {"|", "&&", ";"}:
                    continue
                if read_only:
                    continue
                if cwd_unknown and _looks_like_path(word):
                    soft = soft or Deny(
                        "working directory is unknown after `cd`; use repo-relative paths in one "
                        "command"
                    )
                    continue
                if cwd == repo and "/" not in word and "." not in word and not _has_wild(word):
                    continue
                if _has_wild(word):
                    base = normalize_rel(word)
                    for pat in (*cfg.hard_stop_write_globs, *cfg.forbidden_write_globs):
                        if pattern_could_hit(base, pat):
                            soft = soft or Deny(
                                f"`{head} {word}` may touch protected paths ({pat}); name the "
                                "files explicitly"
                            )
                            break
                    continue
                rel = _virtual_path(word, cwd, repo)
                if rel is None:
                    continue
                if d := _path_violation(rel, ctx):
                    if d.hard_stop:
                        return d
                    soft = soft or d

        for target in (*sc.write_redirects, *nested_redirects):
            if _has_unresolved(target):
                soft = soft or Deny(
                    "redirect target contains a variable/glob and cannot be checked"
                )
                continue
            rel = _virtual_path(target, cwd, repo)
            if rel is None:
                p = Path(target) if Path(target).is_absolute() else cwd / target
                if target in ("/dev/null", "/dev/stdout", "/dev/stderr"):
                    continue
                if not _is_writable_outside(p.resolve(), ctx):
                    soft = soft or Deny(f"redirect to {target} writes outside the repository")
                elif d := _path_violation(p.resolve().as_posix().lstrip("/"), ctx):
                    if d.hard_stop:
                        return d
                    soft = soft or d
                continue
            if d := _path_violation(rel, ctx):
                if d.hard_stop:
                    return d
                soft = soft or d

    return soft or Allow()


def _git_sub(args: list[str]) -> str | None:
    i = 0
    takes_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
    while i < len(args):
        if args[i] in takes_value:
            i += 2
            continue
        if args[i].startswith("-"):
            i += 1
            continue
        return args[i]
    return None


def _has_wild(word: str) -> bool:
    return any(ch in word for ch in "*?")


def _looks_like_path(word: str) -> bool:
    return "/" in word or word.startswith(".") or _has_wild(word)


# ---- file tools ----------------------------------------------------------------------------


def _decide_write(tool: str, tool_input: Mapping[str, object], ctx: PolicyContext) -> Decision:
    raw = tool_input.get(_WRITE_TOOLS[tool])
    if not isinstance(raw, str) or not raw:
        return Deny(f"{tool}: missing {_WRITE_TOOLS[tool]}")
    p = Path(raw).expanduser()
    full = (p if p.is_absolute() else ctx.repo / p).resolve()  # resolves symlinks too
    try:
        rel = normalize_rel(str(full.relative_to(ctx.repo.resolve())))
    except ValueError:
        if _is_writable_outside(full, ctx):
            # an allowed outside directory still cannot hold secrets or the graph file
            return _path_violation(full.as_posix().lstrip("/"), ctx) or Allow()
        return Deny(f"{raw} is outside the repository; agents may only write inside it")
    return _path_violation(rel, ctx) or Allow()


# ---- MCP -----------------------------------------------------------------------------------


def _decide_mcp(tool: str, tool_input: Mapping[str, object], ctx: PolicyContext) -> Decision:
    cfg = ctx.config
    if tool in cfg.hard_stop_mcp_tools:
        return Deny(f"{tool} deletes graph entities: ask the user first", hard_stop=True)
    if tool in cfg.denied_mcp_tools:
        return Deny(f"{tool} is never allowed (use open_nodes with exact names)")
    if not tool.startswith(cfg.allowed_mcp_prefixes):
        return Deny(f"MCP tool {tool} is not on the allow list for unattended runs")
    if tool.startswith("mcp__memory__"):
        names: list[str] = []
        if isinstance(tool_input.get("entityName"), str):
            names.append(str(tool_input["entityName"]))
        for key in ("entities", "observations", "deletions", "relations"):
            items = tool_input.get(key)
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, Mapping):
                        for k in ("name", "entityName", "from", "to"):
                            if isinstance(it.get(k), str):
                                names.append(str(it[k]))
        for n in names:
            if n in cfg.protected_entities:
                return Deny(
                    f"changing graph entity {n!r} needs the user's approval", hard_stop=True
                )
    return Allow()


# ---- entry point ---------------------------------------------------------------------------


def decide(tool_name: str, tool_input: Mapping[str, object] | None, ctx: PolicyContext) -> Decision:
    tool_input = tool_input or {}
    if tool_name == "Bash":
        cmd = tool_input.get("command")
        if not isinstance(cmd, str):
            return Deny("Bash: missing command")
        return _decide_bash(cmd, ctx)
    if tool_name in _WRITE_TOOLS:
        if ctx.read_only:
            return Deny(f"{tool_name}: this is the read-only planning step; do not change files")
        return _decide_write(tool_name, tool_input, ctx)
    if tool_name == "AskUserQuestion":
        return Deny(
            "nobody is available to answer: decide yourself with the most conservative, reversible "
            "option and record it in AUTONOMOUS-DECISIONS.md (question, options, choice, reason, "
            "files)"
        )
    if tool_name.startswith("mcp__"):
        return _decide_mcp(tool_name, tool_input, ctx)
    if tool_name in _READ_TOOLS:
        for key in _READ_TOOLS[tool_name]:
            val = tool_input.get(key)
            if isinstance(val, str) and _word_names_env_file(val):
                return Deny(f"{tool_name} {val}: reading .env files is not allowed (secrets)")
        return Allow()
    if tool_name in _SAFE_TOOLS:
        return Allow()
    return Deny(f"unknown tool {tool_name!r}: not on the allow list")
