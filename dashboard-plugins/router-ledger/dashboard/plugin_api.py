"""Router Ledger dashboard plugin — backend API routes.

Mounted at /api/plugins/router-ledger/ by the dashboard plugin system.

Reads ~/harness/artifacts/<run-id>/ledger.json — the cost ledger written by
harness_ledger.py (the shared ledger spanning build agent + Fusion council).

Aggregates by role and model, computes daily totals against the $25/day
OpenRouter cap, and exposes the circuit-breaker state.

This layer is read-only — it never mutates harness state.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

HARNESS = Path.home() / "harness"
ARTIFACTS = HARNESS / "artifacts"

# harness_ledger.py DEFAULT_DAILY_CAP_USD
DAILY_CAP_USD = 25.0


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _ledger_path(run_id: str) -> Path:
    return ARTIFACTS / run_id / "ledger.json"


@router.get("/runs")
async def list_runs() -> dict:
    """List run-ids that have a ledger.json, newest first."""
    runs = []
    if ARTIFACTS.is_dir():
        for d in sorted(ARTIFACTS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir() and (d / "ledger.json").is_file():
                st = d.stat()
                runs.append({
                    "run_id": d.name,
                    "mtime": st.st_mtime,
                    "age_s": round(time.time() - st.st_mtime, 1),
                })
    return {"runs": runs[:50], "total": len(runs)}


def _aggregate_ledger(ledger: dict) -> dict:
    """Aggregate ledger.json into per-role, per-model, and per-day summaries."""
    rows = ledger.get("rows", []) if isinstance(ledger, dict) else []
    if not isinstance(rows, list):
        rows = []

    total_cost = 0.0
    by_role = defaultdict(float)
    by_model = defaultdict(float)
    by_day = defaultdict(lambda: defaultdict(float))  # day -> {role -> cost}
    by_role_model = defaultdict(lambda: defaultdict(float))  # role -> {model -> cost}
    n_entries = 0
    tokens_in = 0
    tokens_out = 0

    for r in rows:
        if not isinstance(r, dict):
            continue
        cost = float(r.get("costUsd", 0) or 0)
        role = r.get("role", "unknown")
        model = r.get("model", "unknown")
        day = (r.get("day") or "")[:10]  # YYYY-MM-DD
        tok_in = int(r.get("tokensIn", 0) or 0)
        tok_out = int(r.get("tokensOut", 0) or 0)

        total_cost += cost
        by_role[role] += cost
        by_model[model] += cost
        if day:
            by_day[day][role] += cost
        by_role_model[role][model] += cost
        n_entries += 1
        tokens_in += tok_in
        tokens_out += tok_out

    # Daily totals vs cap
    daily_totals = []
    for day in sorted(by_day.keys()):
        day_total = sum(by_day[day].values())
        daily_totals.append({
            "day": day,
            "total": round(day_total, 4),
            "cap": DAILY_CAP_USD,
            "pct_of_cap": round((day_total / DAILY_CAP_USD) * 100, 1) if DAILY_CAP_USD else 0,
            "by_role": {k: round(v, 4) for k, v in by_day[day].items()},
        })

    return {
        "total_cost_usd": round(total_cost, 4),
        "n_entries": n_entries,
        "daily_cap_usd": DAILY_CAP_USD,
        "by_role": {k: round(v, 4) for k, v in sorted(by_role.items(), key=lambda x: -x[1])},
        "by_model": {k: round(v, 4) for k, v in sorted(by_model.items(), key=lambda x: -x[1])},
        "by_role_model": {
            role: {k: round(v, 4) for k, v in sorted(models.items(), key=lambda x: -x[1])}
            for role, models in by_role_model.items()
        },
        "daily_totals": daily_totals,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "circuit_broken": ledger.get("circuit_broken", False) if isinstance(ledger, dict) else False,
        "next_id": ledger.get("next_id", 0) if isinstance(ledger, dict) else 0,
    }


@router.get("/runs/{run_id}")
async def get_ledger(run_id: str) -> dict:
    """Get the full aggregated ledger for a run."""
    p = _ledger_path(run_id)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"no ledger.json for run {run_id}")
    ledger = _read_json(p)
    if ledger is None:
        raise HTTPException(status_code=500, detail=f"ledger.json for {run_id} is corrupt or empty")
    return {
        "run_id": run_id,
        "aggregated": _aggregate_ledger(ledger),
        "raw_rows_count": len(ledger.get("rows", [])) if isinstance(ledger, dict) else 0,
    }


@router.get("/runs/{run_id}/rows")
async def get_ledger_rows(run_id: str, limit: int = 100) -> dict:
    """Get the raw ledger rows (most recent first, capped)."""
    p = _ledger_path(run_id)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"no ledger.json for run {run_id}")
    ledger = _read_json(p)
    if ledger is None:
        raise HTTPException(status_code=500, detail=f"ledger.json for {run_id} is corrupt")
    rows = ledger.get("rows", []) if isinstance(ledger, dict) else []
    if not isinstance(rows, list):
        rows = []
    # Return most recent first, capped
    reversed_rows = list(reversed(rows))[:limit]
    return {"run_id": run_id, "rows": reversed_rows, "total": len(rows)}


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "artifacts_dir": str(ARTIFACTS),
        "daily_cap_usd": DAILY_CAP_USD,
    }
