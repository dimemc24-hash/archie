"""Listings dashboard plugin — backend API routes.

Mounted at /api/plugins/listings/ by the dashboard plugin system.

Reads from Archie's Supabase (PostgREST + Storage) using env vars
ARCHIE_SUPABASE_URL / ARCHIE_SUPABASE_SERVICE_KEY (read from ~/.hermes/.env
the same way other host code does). Returns:
  - Queue view: listings grouped by status with counts
  - Listing detail: all fields + signed photo URLs

This layer is read-only — it never mutates listing state. All mutations go
through the antiques pipeline modules (pricing, approve, publish, fulfill).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

# Load env from ~/.hermes/.env (house pattern — other host code does the same).
_ENV_FILE = Path.home() / ".hermes" / ".env"


def _load_env() -> None:
    if not _ENV_FILE.is_file():
        return
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env()

# Re-export common client (stdlib-only, same as the pipeline).
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# The dashboard mounts this file from ~/.hermes/plugins (symlinked); the repo
# root is NOT on sys.path there. Resolve it relative to this file's REAL path
# (resolve() follows the symlink) so `antiques` imports in any context.
import sys as _sys
from pathlib import Path as _Path
_repo_root = str(_Path(__file__).resolve().parents[3])
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)
from antiques.common import SupabaseClient  # noqa: E402

STATUS_ORDER = ["draft", "priced", "approved", "listed", "sold", "shipped", "rejected", "error"]


def _client() -> SupabaseClient:
    try:
        return SupabaseClient()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "supabase_url": os.environ.get("ARCHIE_SUPABASE_URL", "")[:40] + "..." if os.environ.get("ARCHIE_SUPABASE_URL") else "not set",
        "env_file": str(_ENV_FILE),
        "env_file_exists": _ENV_FILE.is_file(),
    }


@router.get("/queue")
async def queue() -> dict:
    """Queue view: listings grouped by status with counts."""
    c = _client()
    all_listings = c.select_listings(limit=200)
    by_status: dict[str, list[dict]] = {s: [] for s in STATUS_ORDER}
    for row in all_listings:
        status = row.get("status", "unknown")
        if status not in by_status:
            by_status[status] = []
        by_status[status].append(_summarize(row))

    groups = []
    for status in STATUS_ORDER:
        rows = by_status.get(status, [])
        if rows:
            groups.append({"status": status, "count": len(rows), "listings": rows})
    # Any unknown statuses
    for status, rows in by_status.items():
        if status not in STATUS_ORDER and rows:
            groups.append({"status": status, "count": len(rows), "listings": rows})

    return {
        "groups": groups,
        "total": len(all_listings),
    }


@router.get("/listings/{listing_id}")
async def get_listing(listing_id: str) -> dict:
    """Listing detail: all fields + signed photo URLs."""
    c = _client()
    row = c.get_listing(listing_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"listing {listing_id} not found")

    # Resolve signed URLs for photos.
    photos = row.get("photos") or []
    signed_photos = []
    if isinstance(photos, list):
        for i, p in enumerate(photos):
            if not isinstance(p, dict):
                continue
            path = p.get("path", "")
            try:
                url = c.signed_url(path)
            except Exception:
                url = ""
            signed_photos.append({
                "index": i,
                "path": path,
                "url": url,
                "content_type": p.get("content_type", ""),
            })

    row["signed_photos"] = signed_photos
    return row


def _summarize(row: dict) -> dict:
    """Compact summary for the queue view."""
    pricing = row.get("pricing") or {}
    return {
        "id": row.get("id", ""),
        "title": row.get("title", ""),
        "category_guess": row.get("category_guess"),
        "price": pricing.get("recommended") if isinstance(pricing, dict) else None,
        "n_photos": len(row.get("photos") or []),
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
    }
