"""Path globs shared by policy (forbidden paths) and gates (Codex allowlist).

Semantics: `*` and `?` stay inside one path segment, `**` crosses segments, and everything else is
literal. In particular `[` / `]` are NOT character classes: qinno has real files named
`pos/[orderId].tsx`, and an allowlist entry has to match that file exactly.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import PurePosixPath


@lru_cache(maxsize=2048)
def _compile(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                i += 2
                if pattern[i : i + 1] == "/":
                    i += 1
                    parts.append("(?:.*/)?")  # `**/` also matches zero directories
                else:
                    parts.append(".*")
                continue
            parts.append("[^/]*")
        elif c == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(c))
        i += 1
    return re.compile("".join(parts) + r"\Z", re.DOTALL)


def normalize_rel(path: str) -> str:
    """Repo-relative posix path without `./` and without `..` segments (kept if it escapes)."""
    p = PurePosixPath(path)
    out: list[str] = []
    for seg in p.parts:
        if seg in ("", ".", "/"):
            continue
        if seg == "..":
            if out and out[-1] != "..":
                out.pop()
            else:
                out.append("..")
            continue
        out.append(seg)
    return "/".join(out)


def glob_match(pattern: str, path: str) -> bool:
    return _compile(pattern).match(normalize_rel(path)) is not None


def matches_any(patterns: tuple[str, ...] | list[str], path: str) -> str | None:
    """Return the first matching pattern, or None."""
    rel = normalize_rel(path)
    for pat in patterns:
        if _compile(pat).match(rel):
            return pat
    return None


def pattern_could_hit(arg_pattern: str, forbidden: str) -> bool:
    """Could `arg_pattern` (a shell word with wildcards) expand to something `forbidden` covers?

    Conservative, segment-wise: for `rm apps/*`-style words whose real expansion is unknown.
    A match of the leading segments is enough (the arg may name the directory or an ancestor glob).
    """
    arg_segs = [s for s in normalize_rel(arg_pattern).split("/") if s]
    fb_segs = [s for s in forbidden.split("/") if s]
    # Compare against the forbidden pattern's leading literal-ish segments only.
    lead: list[str] = []
    for seg in fb_segs:
        if seg in ("**",) or seg == "**":
            break
        lead.append(seg)
    if not lead:  # `**/name`: any directory could contain it
        return True
    for a, f in zip(arg_segs, lead, strict=False):
        if a == "**":
            return True
        if not _compile(f).match(a) and not _compile(a).match(f):
            return False
    return True
