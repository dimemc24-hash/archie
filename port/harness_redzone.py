#!/usr/bin/env python3
"""
harness_redzone.py — red-zone path detection for the Python dev-harness.

Faithful port of the red-zone matching in archie-router/src/router/escalation.ts
(lines 79-86), using the red-zone glob list from the resolved profile
(profiles/newchapter.json) + global config (router.config.json).

The router compiles picomatch globs with {nocase: true, dot: true} and
normalizes backslashes to '/' before matching (escalation.ts lines 78-86).
Python's fnmatch with flags is the stdlib equivalent:
  - nocase  → fnmatch is already case-insensitive on its pattern translation
              when we lower() both the path and the glob
  - dot:true → fnmatch's '*' DOES cross a leading dot (unlike picomatch's
               default), so '**/*.env*' matches '.env' — the safe direction,
               matching the router's explicit dot:true opt-in
  - backslash normalization → f.replace('\\\\', '/') before matching

RED_ZONE_GLOBS is the exact list from profiles/newchapter.json (the only profile
authored). Empty globs are skipped (escalation.ts line 42 filters '').

detect_red_zone(changed_files) is the single entry point: it takes the list of
changed files (the `input.changedFiles` equivalent) and returns True if ANY
file matches ANY glob — the router's `files.some(f => matchers.some(match(f)))`
semantics (escalation.ts lines 80-86).
"""
from __future__ import annotations

import fnmatch
from typing import Iterable


# ── red-zone globs (profiles/newchapter.json → redZoneGlobs) ────────────────
# These are the exact globs the newchapter profile declares. The router loads
# them via cfg.redZoneGlobs (ResolvedConfig, types.ts line 149); the harness
# has only one profile so they are a module constant.
#
# NOTE: '**/*calc*/**' is the legal-correctness catch-all — it must match
# case-insensitively (camelCase 'meansCalc') so a calc path never silently
# under-escalates. See escalation.ts line 69 nocase note + the test at
# escalation.test.ts lines 60-68.
RED_ZONE_GLOBS: list[str] = [
    "lib/forms/**",
    "app/api/forms/**",
    "lib/ndc/**",
    "lib/secure/**",
    "supabase/migrations/**",
    "lib/desk/anchorMap*",
    "**/*calc*/**",
]

# Pre-filter empty globs once (escalation.ts line 42: globs.filter(g => g !== ""))
_GLOBS_ACTIVE = [g for g in RED_ZONE_GLOBS if g]


def _glob_to_regex(glob: str) -> str:
    """Translate a picomatch-style glob to a case-insensitive regex.

    fnmatch.translate gives us a Python regex, but picomatch's '**' is more
    permissive than fnmatch's '*'. We normalize the picomatch extensions:
      - '**/' and '/**' → match any number of path segments (including zero)
      - '**'  alone      → match anything (including '/')
      - '*'              → match anything except '/'
      - '?'              → match a single non-'/' char
    We translate manually for fidelity, then compile with re.IGNORECASE.
    """
    import re

    i = 0
    out = []
    while i < len(glob):
        c = glob[i]
        # '**' — picomatch double-star (matches across '/')
        if c == "*" and i + 1 < len(glob) and glob[i + 1] == "*":
            # consume the '**'
            i += 2
            # '**/' → match any number of leading path segments (or none)
            if i < len(glob) and glob[i] == "/":
                out.append("(?:.*/)?")
                i += 1  # consume the '/'
            else:
                out.append(".*")
            continue
        if c == "*":
            # single '*' matches anything except '/' (picomatch default)
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        # literal char — escape regex metachars
        out.append(re.escape(c))
        i += 1
    # fnmatch anchors implicitly; picomatch matches the full path, so anchor.
    return "^" + "".join(out) + "$"


# Compile the matchers ONCE (escalation.ts redZoneMatchers memoization, lines
# 38-46). nocase:true → re.IGNORECASE. dot:true → our '*' already matches a
# leading dot (no special-casing needed; picomatch needs dot:true because its
# '*' refuses leading dots by default — Python regex does not).
import re as _re
_COMPILED = [_re.compile(_glob_to_regex(g), _re.IGNORECASE) for g in _GLOBS_ACTIVE]


def detect_red_zone(changed_files: list[str]) -> bool:
    """Return True if any changed file matches any red-zone glob.

    Port of escalation.ts lines 80-86:
        const redZoneHit = matchers.length > 0 && files.some(f => {
            const norm = f.replace(/\\\\/g, "/");
            return matchers.some(match => match(norm));
        });

    - Backslashes are normalized to '/' first (Windows path safety, escalation.ts
      lines 75-78 + test at escalation.test.ts lines 213-228).
    - Case-insensitive (nocase, escalation.ts line 42 + test lines 60-68).
    - Empty glob list → False (matchers.length > 0 guard).
    """
    if not _COMPILED:
        return False
    if not isinstance(changed_files, (list, tuple)):
        raise TypeError(f"changed_files must be a list (got {type(changed_files).__name__})")
    for f in changed_files:
        if not isinstance(f, str):
            raise TypeError(f"changed file entries must be strings (got {type(f).__name__})")
        norm = f.replace("\\", "/")
        for matcher in _COMPILED:
            if matcher.search(norm):
                return True
    return False


__all__ = ["detect_red_zone", "RED_ZONE_GLOBS"]
