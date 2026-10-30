"""A small, conservative shell command splitter for the policy.

It is not a shell. It exists to turn a Bash tool command into the simple commands that would run
(`a && b | c; $(d); `e`; bash -c '...'`), so policy rules apply to each of them instead of to a
substring of the whole line. Anything it cannot understand raises `ShellParseError`, and the policy
treats that as a Deny ("unknown means deny").

Handled: quoting and escapes, `&& || ; | |& & newline`, subshells/groups, `$(...)`, backticks,
process substitution `<(...)`/`>(...)`, redirects (targets recorded), `# comments`.
Rejected: heredocs (`<<`), unbalanced quotes/parens, nesting deeper than MAX_DEPTH.
Not handled (left as literal text): variable and glob expansion. A command word that still contains
`$`, `~`, or glob characters is reported by the policy as unresolvable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_DEPTH = 8


class ShellParseError(ValueError):
    pass


@dataclass
class SimpleCommand:
    argv: list[str] = field(default_factory=list)
    write_redirects: list[str] = field(default_factory=list)  # `> file`, `>> file`, `&> file`
    # `< file`, `<<< word`, `>&2` targets: not paths the command writes, but the policy still
    # needs to see `cat < .env`
    read_redirects: list[str] = field(default_factory=list)


_WRITE_OPS = {">", ">>", ">|", "&>", "&>>", "<>"}
_FD_ONLY = re.compile(r"\d+\Z")


def _skip_single(s: str, i: int) -> int:
    j = s.find("'", i + 1)
    if j < 0:
        raise ShellParseError("unterminated single quote")
    return j + 1


def _skip_double(s: str, i: int) -> int:
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        if c == "$" and s[j + 1 : j + 2] == "(":
            _, j = _balanced(s, j + 1)
            continue
        if c == "`":
            _, j = _backtick(s, j)
            continue
        j += 1
    raise ShellParseError("unterminated double quote")


def _balanced(s: str, i: int) -> tuple[str, int]:
    """`s[i] == '('`; return (inner text, index after the matching ')')."""
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            j = _skip_single(s, j)
            continue
        if c == '"':
            j = _skip_double(s, j)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[i + 1 : j], j + 1
        j += 1
    raise ShellParseError("unbalanced parenthesis")


def _backtick(s: str, i: int) -> tuple[str, int]:
    j = i + 1
    while j < len(s):
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == "`":
            return s[i + 1 : j], j + 1
        j += 1
    raise ShellParseError("unterminated backtick")


def parse_shell(cmd: str, _depth: int = 0) -> list[SimpleCommand]:
    if _depth > MAX_DEPTH:
        raise ShellParseError("command nesting too deep")

    commands: list[SimpleCommand] = []
    argv: list[str] = []
    redirects: list[str] = []
    reads: list[str] = []
    buf: list[str] = []
    started = False
    pending: str | None = None  # "write" | "read" when the next word is a redirect target

    def end_word() -> None:
        nonlocal started, pending
        if not started:
            return
        word = "".join(buf)
        buf.clear()
        started = False
        if pending == "write":
            redirects.append(word)
        elif pending == "read":
            reads.append(word)
        else:
            argv.append(word)
        pending = None

    def end_command() -> None:
        nonlocal argv, redirects, reads, pending
        end_word()
        if argv or redirects or reads:
            commands.append(SimpleCommand(argv, redirects, reads))
        argv, redirects, reads, pending = [], [], [], None

    def recurse(inner: str) -> None:
        commands.extend(parse_shell(inner, _depth + 1))

    s, i, n = cmd, 0, len(cmd)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                buf.append(s[i + 1])
                started = True
            i += 2
            continue
        if c == "'":
            j = _skip_single(s, i)
            buf.append(s[i + 1 : j - 1])
            started = True
            i = j
            continue
        if c == '"':
            j = _skip_double(s, i)
            body = s[i + 1 : j - 1]
            k = 0
            while k < len(body):
                ch = body[k]
                if ch == "\\" and k + 1 < len(body):
                    buf.append(body[k + 1])
                    k += 2
                elif ch == "$" and body[k + 1 : k + 2] == "(":
                    inner, k2 = _balanced(body, k + 1)
                    recurse(inner)
                    buf.append("$(...)")
                    k = k2
                elif ch == "`":
                    inner, k2 = _backtick(body, k)
                    recurse(inner)
                    buf.append("`...`")
                    k = k2
                else:
                    buf.append(ch)
                    k += 1
            started = True
            i = j
            continue
        if c == "$" and s[i + 1 : i + 2] == "(":
            inner, i = _balanced(s, i + 1)
            recurse(inner)
            buf.append("$(...)")
            started = True
            continue
        if c == "`":
            inner, i = _backtick(s, i)
            recurse(inner)
            buf.append("`...`")
            started = True
            continue
        if c in "<>" and s[i + 1 : i + 2] == "(":
            inner, i = _balanced(s, i + 1)
            recurse(inner)
            buf.append("<(...)")
            started = True
            continue
        if c == "&" and s[i + 1 : i + 2] == ">":
            op = "&>>" if s[i : i + 3] == "&>>" else "&>"
            end_word()
            i += len(op)
            pending = "write"
            continue
        if c in "<>":
            if s[i : i + 3] == "<<<":
                end_word()
                i += 3
                pending = "read"
                continue
            if s[i : i + 2] == "<<":
                raise ShellParseError("heredoc is not supported by the policy parser")
            # optional fd prefix like `2>`: the digits were collected as a word
            if started and _FD_ONLY.match("".join(buf)) and pending is None:
                buf.clear()
                started = False
            else:
                end_word()
            op = c
            if s[i + 1 : i + 2] in (">", "|") and c == ">":
                op = s[i : i + 2]
            elif s[i : i + 2] == "<>":
                op = "<>"
            elif c == ">" and s[i + 1 : i + 2] == "&":
                i += 2  # `>&2` / `>&-` : fd duplication, no path target
                pending = "read"
                continue
            i += len(op)
            pending = "write" if op in _WRITE_OPS else "read"
            continue
        if c == "&":
            end_command()
            i += 2 if s[i + 1 : i + 2] == "&" else 1
            continue
        if c == "|":
            end_command()
            i += 2 if s[i + 1 : i + 2] in ("|", "&") else 1
            continue
        if c in ";\n()":
            end_command()
            i += 1
            continue
        if c in " \t\r":
            end_word()
            i += 1
            continue
        if c == "#" and not started:
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        buf.append(c)
        started = True
        i += 1

    end_command()
    return commands
