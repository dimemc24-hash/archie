#!/usr/bin/env python3
"""
run_stage2 — Stage 2 of the dev harness (segmented build + forced-Fusion
checkpoint loop), callable as a skill.

Wraps the existing ``stage2_build.py`` — the driver that sends Hermes/Opus
through a SEGMENTED build on a ``build/<run-id>`` branch, FORCING a Fusion
council consult at each checkpoint-manifest decision point and injecting the
synthesis back into the same session lineage. stage2_build.py owns the entire
sentinel-protocol loop (CHECKPOINT_REACHED detection, forced consult,
BUILD_COMPLETE coverage gate, pushback); this wrapper is deliberately thin.

The sentinel protocol is how the build agent communicates with the driver:
  * ``CHECKPOINT_REACHED:<id>``  → driver consults Fusion, injects synthesis
  * ``BUILD_COMPLETE``            → driver accepts (after coverage gate)

Chat-triggered Stage-2 runs stay headless by default (matches today —
``CHECKPOINT_REACHED`` auto-injects the Fusion synthesis, no forced pause),
but the dashboard's Harness Run panel surfaces the live checkpoint state as
it happens so you get visibility without being forced to babysit the run.

Runs under the "build" DO lock (Stage 2 is the long mutation).

Usage:
  run_stage2.py --run-id YYYY-MM-DD-slug [--base main] [--model M]
      [--preset budget|full] [--max-segments N] [--profile NAME] [--dry-run]

  run_stage2.py --smoke [--dry-run]              # synthetic 1-checkpoint build

  run_stage2.py --help-underlying                # show stage2_build.py --help
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 2: segmented build + forced-Fusion checkpoints.")
    ap.add_argument("--run-id", help="YYYY-MM-DD-slug (required unless --smoke)")
    ap.add_argument("--base", default="main", help="base branch to build off (default: main)")
    ap.add_argument("--model", default=None, help="base model tier (default: stage2_build.py's cheap)")
    ap.add_argument("--preset", default=None, choices=["budget", "full"],
                    help="Fusion council preset (budget=cheap, full=Opus)")
    ap.add_argument("--max-segments", type=int, default=None,
                    help="cap on segments before the driver gives up (default: 40)")
    ap.add_argument("--profile", default=None,
                    help="target repo profile (profiles/<name>.json); "
                         "omit for the legacy NewChapter checkout")
    ap.add_argument("--smoke", action="store_true",
                    help="run the synthetic 1-checkpoint smoke build (cheap, glm, tmpdir)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the stage2_build.py invocation without running it")
    ap.add_argument("--help-underlying", action="store_true",
                    help="show stage2_build.py --help and exit")
    a = ap.parse_args()

    C.require_file(C.STAGE2, "stage2_build.py (Stage-2 driver)")

    if a.help_underlying:
        return C.run_direct(["python3", C.STAGE2, "--help"])

    cmd = ["python3", C.STAGE2]
    if a.smoke:
        cmd += ["--smoke"]
    else:
        if not a.run_id:
            C.die("--run-id is required (or use --smoke)")
        cmd += ["--run-id", a.run_id, "--base", a.base]
        if a.model:
            cmd += ["--model", a.model]
        if a.preset:
            cmd += ["--preset", a.preset]
        if a.max_segments is not None:
            cmd += ["--max-segments", str(a.max_segments)]
        if a.profile:
            cmd += ["--profile", a.profile]

    # --smoke runs in a tmpdir and is cheap/read-only enough that the lock
    # is not strictly required, but taking it keeps the invariant simple:
    # every stage goes through do-lock.sh.  Smoke uses "attend" (short),
    # real builds use "build" (the long mutation lane).
    mode = "attend" if a.smoke else "build"
    rc = C.with_lock(mode, cmd, dry_run=a.dry_run)
    if rc == C.LOCK_BUSY:
        return rc
    if rc != 0 and not a.dry_run:
        C.alert(f"⚠️ Stage 2 build failed for {a.run_id or 'smoke'} (rc={rc}).")
        C.die(f"stage2_build.py exited {rc}. Build artifacts NOT pushed. "
              f"Check ~/harness/artifacts/{a.run_id or 'smoke'}/ for details.", rc)
    if not a.dry_run:
        if a.smoke:
            print("[harness-control] Stage 2 smoke test done. "
                  "Check ~/harness/artifacts/smoke/ for the burn log.")
        else:
            print(f"[harness-control] Stage 2 done for {a.run_id}. "
                  f"Ship it with: run_stage3.py --run-id {a.run_id}"
                  + (f" --profile {a.profile}" if a.profile else ""))
    return rc


if __name__ == "__main__":
    sys.exit(main())
