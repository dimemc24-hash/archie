#!/usr/bin/env python3
"""
swarm/generic_triage.py — repo-tracked triage wrapper for the generic Stage-3
swarm lane.

ROOT CAUSE (operator-console false-green):
  The live ~/swarm/triage.py hardcodes ~/swarm/newchapter as the repo root.
  When the generic runner ran an archie-profile build, triage looked for
  swarm-findings.json at ~/swarm/newchapter/_harness/<id>/ (missing) instead
  of ~/swarm/archie/_harness/<id>/ (where the wheel wrote it). triage crashed
  with FileNotFoundError, but the runner reported success (sentinel rc=0)
  because the crash was swallowed by `tee` and the runner did not gate on
  triage's exit code.

FIX:
  This wrapper is shipped from the repo (rsynced to ~/swarm/generic/ alongside
  run_swarm_generic.sh) and is invoked as:

      generic_triage.py <run_id> <repo_root> [live_triage_path]

  It cannot edit the live triage.py (hand-managed on the box). Instead it runs
  the live triage.py with HOME overridden to a temp directory where
  swarm/newchapter is a symlink to the actual <repo_root>. The live triage.py's
  hardcoded ~/swarm/newchapter/... paths then resolve against the CORRECT
  repo, so it reads swarm-findings.json and writes swarm-workorder.json in the
  right place — without touching the live script.

BACKWARD COMPATIBILITY (zero NextChapter regression):
  When <repo_root> is ~/swarm/newchapter (or omitted), the symlink target IS
  the NextChapter checkout, so the live triage.py behaves exactly as before.
  The live run_swarm.sh path never calls this wrapper — it calls triage.py
  directly. This wrapper is only invoked by the generic runner.

EXIT CODES:
  0  — triage completed (findings processed, workorder written).
  1  — triage crashed or findings file missing.
  2  — mis-use (bad args).

Usage:
  python3 generic_triage.py <run_id> <repo_root> [live_triage_path]

  run_id            — the build run id (e.g. 2026-07-04-operator-console)
  repo_root         — absolute path to the target repo checkout on the box
                      (e.g. /home/hermes/swarm/archie)
  live_triage_path  — path to the live triage.py (default: ~/swarm/triage.py)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _fail(msg: str, code: int = 1) -> int:
    print(f"[generic_triage] FATAL: {msg}", file=sys.stderr, flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print(__doc__ or "", file=sys.stderr)
        return 2
    run_id = argv[0]
    repo_root = os.path.abspath(argv[1])
    live_triage = argv[2] if len(argv) > 2 else os.path.expanduser("~/swarm/triage.py")

    if not os.path.isdir(repo_root):
        return _fail(f"repo_root does not exist: {repo_root}")
    if not os.path.isfile(live_triage):
        return _fail(f"live triage.py not found: {live_triage} (is the box set up?)")

    # The findings file the live triage.py will try to read. Pre-check so we
    # can give a clear error rather than letting triage.py crash obscurely.
    findings = os.path.join(repo_root, "_harness", run_id, "swarm-findings.json")
    if not os.path.isfile(findings):
        return _fail(
            f"swarm-findings.json not found at {findings}. "
            f"The wheel may have failed to write findings, or the run_id/repo "
            f"mismatch. Refusing to run triage on a non-existent findings file."
        )

    # Override HOME so the live triage.py's hardcoded ~/swarm/newchapter path
    # resolves to the actual target repo via a symlink. This is the cleanest
    # way to redirect the live script's hardcoded paths without editing it.
    # The temp dir is cleaned up on exit; the symlink never touches the real
    # ~/swarm/newchapter.
    with tempfile.TemporaryDirectory(prefix="generic_triage_") as tmp_home:
        swarm_link_dir = os.path.join(tmp_home, "swarm")
        os.makedirs(swarm_link_dir)
        newchapter_link = os.path.join(swarm_link_dir, "newchapter")
        os.symlink(repo_root, newchapter_link)

        env = dict(os.environ)
        env["HOME"] = tmp_home

        # Run the live triage.py with cwd bound to the target repo (belt and
        # suspenders: the HOME override already redirects the hardcoded path,
        # but binding cwd ensures any relative-path reads also resolve here).
        rc = subprocess.call(
            ["python3", "-u", live_triage, run_id],
            cwd=repo_root,
            env=env,
        )
        if rc != 0:
            return _fail(f"live triage.py exited {rc} for run_id={run_id}", rc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
