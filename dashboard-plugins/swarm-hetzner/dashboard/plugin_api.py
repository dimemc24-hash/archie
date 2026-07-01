"""Swarm/Hetzner dashboard plugin — backend API routes.

Mounted at /api/plugins/swarm-hetzner/ by the dashboard plugin system.

Reads:
  * Hetzner box liveness via ~/harness/hetzner_power.sh status
  * swarm-report.json + fix-log.json from ~/harness/artifacts/<id>/ if a copy
    is present, and otherwise opportunistically reads them from ~/harness/repo's
    fix/<id> branch via `git show` (read-only, never checks anything out).

This layer is read-only — it never mutates harness state.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

HARNESS = Path.home() / "harness"
ARTIFACTS = HARNESS / "artifacts"
REPO = HARNESS / "repo"
HETZNER_POWER = HARNESS / "hetzner_power.sh"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except Exception:
        return ""


def _git_show_json(repo_dir: str, ref: str, path: str):
    """Read a JSON file from a git branch via `git show <ref>:<path>`, or None."""
    try:
        r = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=repo_dir, capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


@router.get("/liveness")
async def liveness() -> dict:
    """Check Hetzner swarm box liveness via hetzner_power.sh status."""
    status = "unknown"
    raw = ""
    if HETZNER_POWER.is_file():
        try:
            r = subprocess.run(
                ["bash", str(HETZNER_POWER), "status"],
                capture_output=True, text=True, timeout=30,
            )
            raw = (r.stdout + r.stderr).strip()
            # hetzner_power.sh prints "running", "off", "starting", or "unknown"
            low = raw.lower()
            if "running" in low or "up" in low:
                status = "running"
            elif "off" in low or "down" in low:
                status = "off"
            elif "starting" in low:
                status = "starting"
            else:
                status = "unknown"
        except Exception as e:
            status = "error"
            raw = str(e)
    return {
        "status": status,
        "raw": raw[:200],
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _find_swarm_report(run_id: str):
    """Find swarm-report.json: first check artifacts/, then git show from fix/<id>."""
    # 1. Check artifacts/ for a copy
    art_path = ARTIFACTS / run_id / "swarm-report.json"
    if art_path.is_file():
        return _read_json(art_path), "artifacts"

    # 2. Opportunistically read from fix/<id> branch via git show
    if REPO.is_dir():
        sr = _git_show_json(str(REPO), f"origin/fix/{run_id}",
                            f"_harness/{run_id}/swarm-report.json")
        if sr:
            return sr, "git-show"
    return None, None


def _find_fix_log(run_id: str):
    """Find fix-log.json: first check artifacts/, then git show from fix/<id>."""
    art_path = ARTIFACTS / run_id / "fix-log.json"
    if art_path.is_file():
        return _read_json(art_path), "artifacts"

    if REPO.is_dir():
        fl = _git_show_json(str(REPO), f"origin/fix/{run_id}",
                            f"_harness/{run_id}/fix-log.json")
        if fl:
            return fl, "git-show"
    return None, None


@router.get("/runs")
async def list_runs() -> dict:
    """List run-ids that have artifacts (same as harness-run, for the selector)."""
    runs = []
    if ARTIFACTS.is_dir():
        for d in sorted(ARTIFACTS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir():
                st = d.stat()
                runs.append({
                    "run_id": d.name,
                    "mtime": st.st_mtime,
                    "age_s": round(time.time() - st.st_mtime, 1),
                })
    return {"runs": runs[:50], "total": len(runs)}


@router.get("/runs/{run_id}")
async def get_swarm_data(run_id: str) -> dict:
    """Get swarm-report.json + fix-log.json for a run, plus where each was found."""
    sr, sr_src = _find_swarm_report(run_id)
    fl, fl_src = _find_fix_log(run_id)

    # Distill fix-log into wave summaries
    wave_summaries = []
    if isinstance(fl, list):
        waves = {}
        for e in fl:
            w = e.get("wave", 0)
            if w not in waves:
                waves[w] = {"wave": w, "results": [], "severities": {}}
            waves[w]["results"].append({
                "severity": e.get("severity", "?"),
                "kind": e.get("kind", "?"),
                "title": e.get("title", ""),
                "file": e.get("file", ""),
                "line": e.get("line", ""),
                "confidence": e.get("confidence", ""),
            })
            sev = e.get("severity", "unknown").lower()
            waves[w]["severities"][sev] = waves[w]["severities"].get(sev, 0) + 1
        wave_summaries = sorted(waves.values(), key=lambda x: x["wave"])

    return {
        "run_id": run_id,
        "swarm_report": sr,
        "swarm_report_source": sr_src,
        "fix_log": fl,
        "fix_log_source": fl_src,
        "wave_summaries": wave_summaries,
    }


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "artifacts_dir": str(ARTIFACTS),
        "repo_dir": str(REPO),
        "repo_exists": REPO.is_dir(),
        "hetzner_power_exists": HETZNER_POWER.is_file(),
    }
