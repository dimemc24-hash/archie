#!/usr/bin/env python3
"""
run_stage1 — Stage 1 of the dev harness (spec emit), callable as a skill.

Wraps the existing ``emit_spec.py`` (the same script spec_worker.py and the
stage1-spec skill invoke). It takes a spec bundle authored elsewhere (three
files: build-spec.md, build-prompt.md, checkpoint-manifest.json, + optional kb/)
and pushes it as a ``harness/spec/<run-id>`` branch that Stage 2 consumes.

This is the *manual/agent-driven* Stage-1 path: Archie authors the bundle in the
chat (or a scratch dir) and then calls this to emit it — the counterpart to the
automated ``spec_worker.py`` poll loop. emit_spec.py already enforces the run-id
shape, manifest keys, and the sentinel protocol, and refuses a malformed bundle,
so this wrapper stays thin.

Runs under the "attend" DO lock (Stage 1 is a short attended push, not a build).

Usage:
  run_stage1.py --run-id YYYY-MM-DD-slug \\
      --spec build-spec.md --prompt build-prompt.md --manifest checkpoint-manifest.json \\
      [--kb KB_DIR] [--no-push] [--profile NAME] [--dry-run]

  run_stage1.py --help-underlying    # show emit_spec.py's own --help and exit
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 1: emit + push a spec bundle.")
    ap.add_argument("--run-id", help="YYYY-MM-DD-slug")
    ap.add_argument("--spec", help="path to build-spec.md")
    ap.add_argument("--prompt", help="path to build-prompt.md")
    ap.add_argument("--manifest", help="path to checkpoint-manifest.json")
    ap.add_argument("--kb", help="optional kb/ dir")
    ap.add_argument(
        "--no-push",
        action="store_true",
        help="commit the spec branch locally but do not push to origin",
    )
    ap.add_argument("--profile", default=None,
                    help="target repo profile (profiles/<name>.json); "
                         "omit for the legacy NewChapter checkout")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the emit_spec.py invocation without running it")
    ap.add_argument("--help-underlying", action="store_true",
                    help="show emit_spec.py --help and exit")
    a = ap.parse_args()

    C.require_file(C.EMIT_SPEC, "emit_spec.py (Stage-1 emitter)")

    if a.help_underlying:
        return C.run_direct(["python3", C.EMIT_SPEC, "--help"])

    # Validate required args here (rather than at argparse level) so
    # --help-underlying can run without them.
    missing = [n for n in ("run_id", "spec", "prompt", "manifest")
               if getattr(a, n) is None]
    if missing:
        C.die("missing required args: " + ", ".join("--" + m.replace("_", "-")
                                                     for m in missing))

    for label, path in (("--spec", a.spec), ("--prompt", a.prompt),
                        ("--manifest", a.manifest)):
        if not os.path.isfile(path):
            C.die(f"{label} file not found: {path}")
    if a.kb and not os.path.isdir(a.kb):
        C.die(f"--kb is not a directory: {a.kb}")

    cmd = ["python3", C.EMIT_SPEC, "--run-id", a.run_id,
           "--spec", a.spec, "--prompt", a.prompt, "--manifest", a.manifest]
    if a.kb:
        cmd += ["--kb", a.kb]
    if a.profile:
        cmd += ["--profile", a.profile]
    if not a.no_push:
        cmd += ["--push"]

    # Stage 1 is an attended push — take the "attend" lane of the shared lock so
    # it serialises against a Stage-2 build (mode "build") but is clearly labelled.
    rc = C.with_lock("attend", cmd, dry_run=a.dry_run)
    if rc == C.LOCK_BUSY:
        return rc
    if rc != 0 and not a.dry_run:
        C.alert(f"⚠️ Stage 1 (emit_spec) failed for {a.run_id} (rc={rc}).")
        C.die(f"emit_spec.py exited {rc}. Spec was NOT emitted/pushed.", rc)
    if not a.dry_run:
        print(f"[harness-control] Stage 1 done for {a.run_id}. "
              f"Build it with: run_stage2.py --run-id {a.run_id}"
              + (f" --profile {a.profile}" if a.profile else ""))
    return rc


if __name__ == "__main__":
    sys.exit(main())
