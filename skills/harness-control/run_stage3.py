#!/usr/bin/env python3
"""
run_stage3 — Stage 3 of the dev harness (transport to Hetzner swarm + fix
fetch-back), callable as a skill.

Wraps the existing ``transport.sh`` — the Phase-D transport script that ships
``build/<run-id>`` + a gh-main baseline to the Hetzner swarm box over SSH,
runs the swarm there, then brings ``fix/<run-id>`` back and pushes it to
origin for Stage 4. transport.sh already:

  * ensures the swarm box is powered on (via hetzner_power.sh, which auto-
    wakes the box through the Hetzner Cloud API — no new wake logic needed),
  * gates the origin push on the swarm's REAL sentinel outcome (no fake-green),
  * polls a completion sentinel so a dropped SSH pipe never causes a premature
    fetch,
  * alerts Morley on any abort,
  * logs the whole run to ~/harness/artifacts/<id>/transport.log.

This wrapper is thin: it resolves the run-id, takes the shared DO lock
(mode "build" — Stage 3 is the other long mutation), and shells out to
transport.sh, propagating its outcome (including aborts/alerts) back to the
caller instead of swallowing it.

Runs under the "build" DO lock so a chat-triggered Stage 3 cannot collide
with a Telegram- or SSH-triggered Stage 2/3 run.

Usage:
  run_stage3.py --run-id YYYY-MM-DD-slug [--routes ROUTES_CSV] [--waves N]
      [--baseline origin/main] [--dry-run]

  run_stage3.py --help-underlying    # show transport.sh usage and exit
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 3: transport to Hetzner swarm + fix fetch-back.")
    ap.add_argument("--run-id", required=False, help="YYYY-MM-DD-slug")
    ap.add_argument("--routes", default="", help="routes CSV passed to run_swarm.sh (default: '')")
    ap.add_argument("--waves", type=int, default=3, help="swarm waves (default: 3)")
    ap.add_argument("--baseline", default=None,
                    help="git baseline pushed as hetzner gh-main (default: origin/main via HARNESS_BASELINE)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the transport.sh invocation without running it")
    ap.add_argument("--help-underlying", action="store_true",
                    help="show transport.sh usage and exit")
    a = ap.parse_args()

    C.require_file(C.TRANSPORT, "transport.sh (Stage-3 transport)")

    if a.help_underlying:
        return C.run_direct(["bash", C.TRANSPORT])

    if not a.run_id:
        C.die("--run-id is required")

    cmd = ["bash", C.TRANSPORT, a.run_id, a.routes, str(a.waves)]
    if a.baseline:
        # transport.sh reads HARNESS_BASELINE from the env
        os.environ["HARNESS_BASELINE"] = a.baseline

    # Stage 3 is a long mutation (ships build, fetches fix) — "build" lock.
    rc = C.with_lock("build", cmd, dry_run=a.dry_run)
    if rc == C.LOCK_BUSY:
        return rc
    if rc != 0 and not a.dry_run:
        # transport.sh already alerted Morley via alert.sh internally, but
        # surface the failure clearly to chat too.
        C.die(f"transport.sh exited {rc} for {a.run_id}. "
              f"Stage 3 did NOT complete — check ~/harness/artifacts/{a.run_id}/transport.log.", rc)
    if not a.dry_run:
        print(f"[harness-control] Stage 3 done for {a.run_id}. "
              f"fix/{a.run_id} is on origin — review & merge with run_stage4.py --run-id {a.run_id}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
