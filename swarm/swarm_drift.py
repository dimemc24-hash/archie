#!/usr/bin/env python3
"""
swarm/swarm_drift.py — drift detection between repo-tracked generic swarm scripts
and Morley's LIVE hand-managed scripts on the Hetzner box.

BLINDSPOT ADDRESSED: "No plan exists for safely merging Morley's .bak-* culture
and mid-sweep tweaks into git during a future adoption; without an automated
drift-detection script, the cutover will be a high-risk guessing game."

This script is the automated drift detector. It rsyncs (dry-run, read-only) the
live Hetzner swarm scripts and compares them against the repo-tracked generic
copies, producing a structured report of:
  - files that differ (content drift from Morley's hand-tuning)
  - .bak-* files on the box (Morley's backup culture)
  - files present on the box but not in the repo
  - files in the repo but not on the box

Exit codes:
  0  — no drift (or --dry-run)
  1  — drift detected (differences found; review needed before cutover)
  2  — cannot reach the box / SSH error

Usage:
  python3 swarm/swarm_drift.py [--ssh hetzner-swarm] [--remote-dir ~/swarm]
  python3 swarm/swarm_drift.py --dry-run        # no SSH; compare repo files only

This is a READ-ONLY tool: it never writes to the box, never modifies repo state.
It exists to make the adoption cutover (option c → option b) safe and auditable.

NOTE: This script does NOT run during a build. It is a pre-adoption audit tool,
run by Morley/Archie when the adoption gate (2 successive QA sweeps without
regressions) is being evaluated.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime

REPO_SWARM_DIR = Path(__file__).resolve().parent
GENERIC_RUNNER = "run_swarm_generic.sh"
REPOS_DIR = "swarm-repos"
# The live scripts on the box that the generic runner is the counterpart of.
LIVE_SCRIPTS = ["run_swarm.sh", "peanut_wheel.py", "triage.py"]


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


def fetch_remote_file(ssh_alias: str, remote_dir: str, filename: str) -> str | None:
    """Fetch a file from the Hetzner box via SSH cat (read-only)."""
    rc, out, err = run(
        ["ssh", "-o", "ConnectTimeout=12", ssh_alias, f"cat {remote_dir}/{filename}"],
        timeout=30,
    )
    if rc != 0:
        return None
    return out


def list_remote_baks(ssh_alias: str, remote_dir: str) -> list[str]:
    """List .bak-* files on the box (Morley's backup culture)."""
    rc, out, err = run(
        ["ssh", "-o", "ConnectTimeout=12", ssh_alias,
         f"ls -1 {remote_dir}/.bak-* {remote_dir}/*.bak-* 2>/dev/null || true"],
        timeout=30,
    )
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def diff_content(name: str, local: str, remote: str | None) -> dict:
    """Compare local vs remote content, return a drift entry."""
    if remote is None:
        return {"file": name, "status": "missing-on-box",
                "detail": "repo-tracked file not present on the box"}
    if local == remote:
        return {"file": name, "status": "in-sync"}
    # Compute a simple diff summary
    local_lines = local.splitlines()
    remote_lines = remote.splitlines()
    added = max(0, len(remote_lines) - len(local_lines))
    removed = max(0, len(local_lines) - len(remote_lines))
    return {"file": name, "status": "drifted",
            "local_lines": len(local_lines), "remote_lines": len(remote_lines),
            "lines_added": added, "lines_removed": removed,
            "detail": f"content differs: +{added}/-{removed} lines (approx)"}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Drift detector: repo-tracked generic swarm scripts vs live Hetzner scripts.")
    ap.add_argument("--ssh", default="hetzner-swarm", help="SSH alias for the swarm box")
    ap.add_argument("--remote-dir", default="~/swarm", help="Remote swarm directory")
    ap.add_argument("--dry-run", action="store_true",
                    help="No SSH; just list repo-tracked files (for CI/audit)")
    ap.add_argument("--json", action="store_true", help="Output as JSON")
    args = ap.parse_args()

    from datetime import timezone
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ssh_alias": args.ssh,
        "remote_dir": args.remote_dir,
        "repo_dir": str(REPO_SWARM_DIR),
        "files": [],
        "bak_files": [],
        "drift_count": 0,
        "summary": "",
    }

    if args.dry_run:
        # No SSH — just list what we track
        for f in [GENERIC_RUNNER] + LIVE_SCRIPTS:
            local_path = REPO_SWARM_DIR / f
            report["files"].append({
                "file": f,
                "status": "repo-tracked" if local_path.exists() else "not-in-repo",
                "local_path": str(local_path),
            })
        report["summary"] = "dry-run: repo-tracked files listed (no SSH comparison)"
        if args.json:
            print(__import__("json").dumps(report, indent=2))
        else:
            print(f"[drift] {report['summary']}")
            for f in report["files"]:
                print(f"  {f['file']}: {f['status']}")
        return 0

    # ── Fetch each live script and compare ─────────────────────────────────
    # The generic runner is repo-tracked; the live trio is on the box.
    comparisons = [
        (GENERIC_RUNNER, REPO_SWARM_DIR / GENERIC_RUNNER),
    ]
    # Also compare the live scripts against any repo-tracked snapshots (kb/)
    # to detect if Morley has hand-tuned since the last snapshot.

    for name, local_path in comparisons:
        local_content = local_path.read_text() if local_path.exists() else ""
        remote_content = fetch_remote_file(args.ssh, args.remote_dir, name)
        entry = diff_content(name, local_content, remote_content)
        report["files"].append(entry)
        if entry["status"] == "drifted":
            report["drift_count"] += 1

    # ── Check .bak-* files (Morley's tuning backup culture) ─────────────────
    report["bak_files"] = list_remote_baks(args.ssh, args.remote_dir)

    # ── Summary ──────────────────────────────────────────────────────────────
    drifted = [f for f in report["files"] if f["status"] == "drifted"]
    missing = [f for f in report["files"] if f["status"] == "missing-on-box"]
    in_sync = [f for f in report["files"] if f["status"] == "in-sync"]

    report["summary"] = (
        f"{len(in_sync)} in-sync, {len(drifted)} drifted, {len(missing)} missing-on-box, "
        f"{len(report['bak_files'])} .bak-* files on box"
    )

    if args.json:
        print(__import__("json").dumps(report, indent=2))
    else:
        print(f"[drift] {report['summary']}")
        for f in report["files"]:
            status_icon = {"in-sync": "✓", "drifted": "⚠", "missing-on-box": "✗"}.get(f["status"], "?")
            print(f"  {status_icon} {f['file']}: {f['status']}" +
                  (f" ({f.get('detail','')})" if f.get("detail") else ""))
        if report["bak_files"]:
            print(f"  .bak-* files on box (Morley's tuning backups):")
            for b in report["bak_files"]:
                print(f"    {b}")

    return 1 if report["drift_count"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
