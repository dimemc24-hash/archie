"""Harness Run dashboard plugin — backend API routes.

Mounted at /api/plugins/harness-run/ by the dashboard plugin system.

Reads ~/harness/artifacts/<run-id>/ artifacts (burn.log, checkpoint-log.json,
state.json, ledger.json, transport.log) and returns them as structured JSON.
Also lists available run-ids by scanning the artifacts directory.

This layer is read-only — it never mutates harness state. All mutations go
through the harness-control skills (run_stage1..4) which take the DO lock.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

HARNESS = Path.home() / "harness"
ARTIFACTS = HARNESS / "artifacts"


def _art_dir(run_id: str) -> Path:
    d = ARTIFACTS / run_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"no artifacts for run {run_id}")
    return d


def _read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_text_tail(path: Path, lines: int = 200) -> str:
    try:
        text = path.read_text(errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:
        return ""


@router.get("/runs")
async def list_runs() -> dict:
    """List all run-ids that have an artifacts directory, newest first."""
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
async def get_run(run_id: str) -> dict:
    """Get full state for a single run: state.json, checkpoint-log, burn.log tail,
    ledger summary, transport.log tail, and file listing."""
    d = _art_dir(run_id)

    state = _read_json(d / "state.json") or {}
    cklog = _read_json(d / "checkpoint-log.json") or []
    burn_tail = _read_text_tail(d / "burn.log", 100)
    transport_tail = _read_text_tail(d / "transport.log", 100)
    ledger = _read_json(d / "ledger.json") or {}

    # Summarise ledger
    ledger_summary = {"total_cost_usd": 0.0, "n_entries": 0, "by_role": {}}
    rows = ledger.get("rows", []) if isinstance(ledger, dict) else []
    for r in rows:
        cost = float(r.get("costUsd", 0) or 0)
        role = r.get("role", "unknown")
        ledger_summary["total_cost_usd"] += cost
        ledger_summary["n_entries"] += 1
        ledger_summary["by_role"][role] = ledger_summary["by_role"].get(role, 0.0) + cost
    ledger_summary["total_cost_usd"] = round(ledger_summary["total_cost_usd"], 4)

    # Distill checkpoint flags
    cp_flags = {"fallbacks": [], "blindspots": [], "coverage_failures": [], "n_entries": 0}
    entries = cklog if isinstance(cklog, list) else []
    cp_flags["n_entries"] = len(entries)
    for e in entries:
        if e.get("fallback") and e.get("checkpoint"):
            cp_flags["fallbacks"].append(e["checkpoint"])
        b = e.get("blindspots")
        if isinstance(b, list):
            cp_flags["blindspots"].extend(str(x)[:90] for x in b if x)
        elif b:
            cp_flags["blindspots"].append(str(b)[:90])
        if e.get("checkpoint") == "_coverage_failure" and e.get("unconsulted"):
            cp_flags["coverage_failures"].append(e["unconsulted"])

    # File listing
    files = []
    for f in sorted(d.iterdir()):
        if f.is_file():
            files.append({"name": f.name, "size": f.stat().st_size})

    return {
        "run_id": run_id,
        "state": state,
        "checkpoint_log": cklog,
        "checkpoint_flags": cp_flags,
        "burn_tail": burn_tail,
        "transport_tail": transport_tail,
        "ledger_summary": ledger_summary,
        "files": files,
    }


@router.get("/runs/{run_id}/burn")
async def get_burn_log(run_id: str, lines: int = 500) -> dict:
    """Get the tail of burn.log for a run."""
    d = _art_dir(run_id)
    return {"run_id": run_id, "tail": _read_text_tail(d / "burn.log", lines)}


@router.get("/runs/{run_id}/checkpoint-log")
async def get_checkpoint_log(run_id: str) -> dict:
    """Get the full checkpoint-log.json for a run."""
    d = _art_dir(run_id)
    return {"run_id": run_id, "log": _read_json(d / "checkpoint-log.json") or []}


@router.get("/health")
async def health() -> dict:
    """Health check — confirm the artifacts directory is accessible."""
    return {
        "status": "ok",
        "artifacts_dir": str(ARTIFACTS),
        "exists": ARTIFACTS.is_dir(),
    }
