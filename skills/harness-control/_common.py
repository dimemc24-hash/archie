#!/usr/bin/env python3
"""
Shared helpers for the harness-control skills (run_stage1..run_stage4).

These skills are the *in-agent* entry point to the 4-stage dev harness: they let
Archie's own agent loop (reached via the dashboard's embedded chat, or Telegram,
or an SSH session) drive the same underlying scripts an external actor runs today
(emit_spec.py / stage2_build.py / transport.sh / the Stage-4 review-merge). They
are deliberately THIN: they resolve paths, take the shared DO lock, shell out to
the existing, unchanged code path, and propagate the underlying script's outcome
(including aborts/alerts) back to the caller instead of swallowing it.

Design ref: this repo's _harness/<run-id>/kb/dashboard-design.md (or the PR that
introduced this dir).

Key invariants (do not break):
  * Every stage that mutates harness state goes through do-lock.sh (mode "build"
    for Stage 2/3, "attend" for Stage 1/4) so a chat-triggered run cannot collide
    with a Telegram- or SSH-triggered one. A busy lock surfaces as a clear
    "already running" message (do-lock.sh exits 75 / EX_TEMPFAIL), not a crash.
  * --dry-run never mutates anything: it prints the exact command that WOULD run.
  * Stage 4 (review/merge) NEVER auto-merges in a single call. It uses a
    two-step dry-run/apply split: the default (no --apply) call fetches the fix
    branch, computes a diff/artifact summary, and writes a pending-merge marker
    file keyed to the run/branch/SHA — it never merges. A second call, invoked
    only with an explicit --apply flag, reads that marker, re-validates it still
    matches the current branch state (refusing a stale/mismatched marker),
    performs the merge to main, and drops _harness/. No interactive prompt and
    no bare --confirm flag. The split forces a hard boundary between 'summarise'
    and 'mutate' as two separate tool invocations and produces a concrete
    artifact (the marker) that documents what was reviewed and when, giving a
    wrapping orchestration/policy layer a real point to inspect before the
    second call happens.
  * Nothing here clones or writes to ~/harness/repo beyond what the wrapped script
    already does.
"""
from __future__ import annotations

import os
import subprocess
import sys

HARNESS = os.path.expanduser("~/harness")
REPO = os.path.join(HARNESS, "repo")
ARTIFACTS = os.path.join(HARNESS, "artifacts")
PROFILES_DIR = os.path.join(HARNESS, "profiles")
DO_LOCK = os.path.join(HARNESS, "do-lock.sh")
ALERT = os.path.join(HARNESS, "alert.sh")

# Stage-1 emitter currently lives (un-versioned) under ~/.hermes/skills; this is
# the same path spec_worker.py uses. Flagged as an open question in the PR — it
# ought to move into this repo eventually, but this skill wraps it where it is
# today so behaviour is identical to the existing external-actor path.
EMIT_SPEC = os.path.expanduser(
    "~/.hermes/skills/harness/stage1-spec/scripts/emit_spec.py"
)
STAGE2 = os.path.join(HARNESS, "stage2_build.py")
TRANSPORT = os.path.join(HARNESS, "transport.sh")

# do-lock.sh exit code when the lock is already held (BSD EX_TEMPFAIL).
LOCK_BUSY = 75


def resolve_repo(profile):
    """Resolve the target repo checkout for this run.

    No --profile (None) preserves the exact legacy behavior: ~/harness/repo, the
    NewChapter checkout, untouched. A named profile gets its own workspace under
    ~/harness/workspaces/<profile>/repo, cloned on first use, so a non-NewChapter
    build never shares a working tree with (or risks colliding with) the
    NewChapter checkout. Same contract as emit_spec.py / stage2_build.py's
    resolve_repo — duplicated there because those are independently-invoked
    scripts; centralized here for the skills that have no underlying script to
    delegate to (run_stage4).
    """
    if not profile:
        return REPO
    cfg_path = os.path.join(PROFILES_DIR, f"{profile}.json")
    try:
        import json
        cfg = json.load(open(cfg_path))
    except Exception as e:
        die(f"unknown profile '{profile}': cannot read {cfg_path}: {e}")
        raise  # die() exits, but keep the analyzer happy
    workspace = os.path.expanduser(cfg["workspace"])
    if not os.path.isdir(os.path.join(workspace, ".git")):
        os.makedirs(os.path.dirname(workspace), exist_ok=True)
        rc = subprocess.run(
            ["git", "clone", cfg["repo_url"], workspace], cwd=HARNESS,
            capture_output=True, text=True,
        )
        if rc.returncode:
            die(f"failed to clone {cfg['repo_url']} into {workspace}: {rc.stderr}")
    return workspace


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def die(msg: str, code: int = 1):
    """Fail loudly with a chat-visible message (never swallowed)."""
    eprint(f"[harness-control] ERROR: {msg}")
    sys.exit(code)


def require_file(path: str, what: str):
    if not os.path.isfile(path):
        die(f"{what} not found at {path}. Cannot proceed.")


def alert(msg: str):
    """Best-effort alert to Morley via the existing alert.sh (never fatal)."""
    try:
        subprocess.run(["bash", ALERT, msg], timeout=30, check=False)
    except Exception:
        pass


def with_lock(mode: str, cmd: list[str], *, dry_run: bool = False) -> int:
    """Run ``cmd`` under do-lock.sh ``mode`` (build|attend).

    Returns the child's exit code. A busy lock (75) is translated into a clear,
    chat-friendly message that names the current holder, then re-returned as 75
    so callers can distinguish "collision" from "the run itself failed".
    """
    full = ["bash", DO_LOCK, mode, *cmd]
    if dry_run:
        print("[dry-run] would run under do-lock (" + mode + "):")
        print("  " + " ".join(_shq(c) for c in full))
        return 0
    require_file(DO_LOCK, "do-lock.sh")
    proc = subprocess.run(full)
    rc = proc.returncode
    if rc == LOCK_BUSY:
        holder = _read_holder()
        eprint(
            "[harness-control] DO is BUSY — a harness run is already in flight "
            f"({holder}). Not starting a second one. Try again once it finishes."
        )
    return rc


def run_direct(cmd: list[str], *, dry_run: bool = False) -> int:
    """Run ``cmd`` WITHOUT the lock (for read-only / --help / dry inspection)."""
    if dry_run:
        print("[dry-run] would run:")
        print("  " + " ".join(_shq(c) for c in cmd))
        return 0
    return subprocess.run(cmd).returncode


def _read_holder() -> str:
    try:
        with open(os.path.join(HARNESS, "locks", "holder.json")) as f:
            return f.read().strip()
    except Exception:
        return "unknown holder"


def _shq(s: str) -> str:
    import shlex

    return shlex.quote(s)


__all__ = [
    "HARNESS", "REPO", "ARTIFACTS", "PROFILES_DIR", "DO_LOCK", "ALERT",
    "EMIT_SPEC", "STAGE2", "TRANSPORT", "LOCK_BUSY",
    "eprint", "die", "require_file", "alert", "resolve_repo",
    "with_lock", "run_direct",
]
