"""Listings dashboard plugin — backend API routes.

Mounted at /api/plugins/listings/ by the dashboard plugin system.

Reads/writes to Archie's Supabase (PostgREST + Storage) using env vars
ARCHIE_SUPABASE_URL / ARCHIE_SUPABASE_SERVICE_KEY (read from ~/.hermes/.env
the same way other host code does). Returns:
  - Queue view: listings grouped by status with counts
  - Listing detail: all fields + signed photo URLs
  - Write operations: price, approve, reject, publish

All mutations go through the antiques pipeline modules (pricing, approve, publish).
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

# Import antiques modules for write operations.
from antiques.pricing import (  # noqa: E402
    ManualComps,
    NotConnected as PricingNotConnected,
)
from antiques.approve import (  # noqa: E402
    approve as antiques_approve,
    reject as antiques_reject,
    LowConfidenceError,
)
from antiques.publish import (  # noqa: E402
    DryRunProvider,
    publish_listing as antiques_publish,
)
from antiques.common import IllegalTransition  # noqa: E402

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


_CONF_LEVELS = {"high", "medium", "low", "unknown"}


def _confidence_summary(row: dict) -> dict:
    """Normalized appraisal confidence for the queue badge (missing -> unknown)."""
    appraisal = row.get("appraisal")
    conf = appraisal.get("confidence") if isinstance(appraisal, dict) else None
    if not isinstance(conf, dict):
        conf = {}
    ident = str(conf.get("id", conf.get("identification", "unknown"))).lower()
    val = str(conf.get("value", conf.get("valuation", "unknown"))).lower()
    return {
        "id": ident if ident in _CONF_LEVELS else "unknown",
        "value": val if val in _CONF_LEVELS else "unknown",
    }


def _summarize(row: dict) -> dict:
    """Compact summary for the queue view."""
    pricing = row.get("pricing") or {}
    return {
        "id": row.get("id", ""),
        "status": row.get("status", ""),
        "title": row.get("title", ""),
        "category_guess": row.get("category_guess"),
        "price": pricing.get("recommended") if isinstance(pricing, dict) else None,
        "n_photos": len(row.get("photos") or []),
        "confidence": _confidence_summary(row),
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
    }


# --------------------------------------------------------------------------- #
# Write-enabled endpoints
# --------------------------------------------------------------------------- #


@router.post("/listings/{listing_id}/price")
async def price_listing(
    listing_id: str,
    comps: list[dict[str, Any]],
) -> dict:
    """Price a listing using manual comps (no network calls).

    Body: {"comps": [{"price": 150.0}, {"price": 200.0}, ...]}
    Returns the updated listing row with status "priced".
    """
    c = _client()
    row = c.get_listing(listing_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"listing {listing_id} not found")

    if row.get("status") != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"listing {listing_id} is '{row.get('status')}' — must be 'draft' to price"
        )

    try:
        provider = ManualComps(comps)
        # Import here to avoid circular import at module load time.
        from antiques.pricing import price_listing as do_price
        result = do_price(listing_id, provider, c)
        return {"status": "ok", "listing": _summarize(result)}
    except (ValueError, IllegalTransition) as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/listings/{listing_id}/approve")
async def approve_listing(
    listing_id: str,
    weight_oz: float,
    dims: dict[str, float] | None = None,
    price_override: float | None = None,
    approved_by: str = "morley",
    acknowledge_low_confidence: bool = False,
    approval_reason: str | None = None,
) -> dict:
    """Approve a listing for publication.

    Body parameters: weight_oz (required), dims (optional), price_override (optional),
    approved_by (default: morley), acknowledge_low_confidence (default: false),
    approval_reason (required when confidence is not high/high).

    Returns the updated listing row with status "approved".
    Raises 409 if confidence is low and acknowledge_low_confidence is not set.
    Raises 409 if approval_reason is missing for non-high-confidence appraisals.
    """
    c = _client()
    row = c.get_listing(listing_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"listing {listing_id} not found")

    try:
        result = antiques_approve(
            listing_id,
            weight_oz=weight_oz,
            dims=dims,
            price_override=price_override,
            approved_by=approved_by,
            acknowledge_low_confidence=acknowledge_low_confidence,
            approval_reason=approval_reason,
            client=c,
        )
        return {"status": "ok", "listing": _summarize(result)}
    except LowConfidenceError as e:
        # Low confidence — return 409 with the confidence payload.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "low_confidence",
                "listing_id": e.listing_id,
                "confidence": {
                    "identification": e.confidence[0],
                    "valuation": e.confidence[1],
                },
                "message": str(e),
            }
        )
    except ValueError as e:
        # Handle missing approval_reason for non-high-confidence.
        if "approval_reason" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except IllegalTransition as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/listings/{listing_id}/reject")
async def reject_listing(
    listing_id: str,
    reason: str,
) -> dict:
    """Reject a listing.

    Body: {"reason": "..."}
    Returns the updated listing row with status "rejected".
    """
    c = _client()
    row = c.get_listing(listing_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"listing {listing_id} not found")

    try:
        result = antiques_reject(listing_id, reason, client=c)
        return {"status": "ok", "listing": _summarize(result)}
    except (ValueError, IllegalTransition) as e:
        raise HTTPException(status_code=409, detail=str(e))


# Default cooling period in seconds (3 seconds for testing, increase for production).
# This adds deliberate friction before a live listing.
_COOLING_SECONDS = 3

# Import the row_digest for payload verification in the two-step flow.
from antiques.approve import _row_digest as _get_row_digest  # noqa: E402


@router.post("/listings/{listing_id}/publish/request")
async def publish_request(
    listing_id: str,
) -> dict:
    """Start the two-step publish flow: request to publish.

    Validates the pending-publish marker exists and is not stale, computes
    the payload digest, and records the request timestamp. This is the
    "cooling gate" — the operator must wait before confirming.

    Returns the payload digest and request timestamp for UI display.
    The operator then calls publish/confirm to complete the publish.
    """
    c = _client()
    row = c.get_listing(listing_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"listing {listing_id} not found")

    if row.get("status") != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"listing {listing_id} is '{row.get('status')}' — must be 'approved' to publish"
        )

    # Validate the marker exists (stale check happens at confirm time).
    try:
        from antiques.approve import read_marker, validate_marker
        marker = read_marker(listing_id)
        # Validate marker is not already applied (would mean already published).
        if marker.get("applied"):
            raise HTTPException(
                status_code=409,
                detail=f"listing {listing_id} has already been published (marker applied)"
            )
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Compute the payload digest for UI display.
    payload_digest = _get_row_digest(row)
    pricing = row.get("pricing") or {}
    price = pricing.get("recommended") if isinstance(pricing, dict) else None
    n_photos = len(row.get("photos") or [])

    # Record the request timestamp in the listing's notes (temp storage for cooling gate).
    # This allows the server to track when the request was made.
    import json
    from antiques.common import _now_iso
    existing_notes = row.get("notes")
    try:
        notes = json.loads(existing_notes) if existing_notes else {}
    except (json.JSONDecodeError, TypeError):
        notes = {}
    if not isinstance(notes, dict):
        notes = {}

    notes["publish_request"] = {
        "requested_at": _now_iso(),
        "payload_digest": payload_digest,
        "price": price,
        "n_photos": n_photos,
    }
    c.patch_listing(listing_id, {"notes": json.dumps(notes)})

    return {
        "status": "ok",
        "cooling_seconds": _COOLING_SECONDS,
        "payload_digest": payload_digest,
        "price": price,
        "n_photos": n_photos,
        "requested_at": notes["publish_request"]["requested_at"],
        "message": f"Publish requested. Wait {_COOLING_SECONDS} seconds, then confirm.",
    }


@router.post("/listings/{listing_id}/publish/confirm")
async def publish_confirm(listing_id: str) -> dict:
    """Complete the two-step publish flow: confirm and actually publish.

    Validates:
    1. A publish request was previously made (cooling gate check).
    2. The cooling period has elapsed.
    3. The marker is still valid (not stale, not already applied).

    Then performs the actual publish and advances status to "listed".
    """
    import json
    from antiques.common import _now_iso

    c = _client()
    row = c.get_listing(listing_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"listing {listing_id} not found")

    if row.get("status") != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"listing {listing_id} is '{row.get('status')}' — must be 'approved' to publish"
        )

    # Check that a publish request exists and validate cooling period.
    existing_notes = row.get("notes")
    try:
        notes = json.loads(existing_notes) if existing_notes else {}
    except (json.JSONDecodeError, TypeError):
        notes = {}
    if not isinstance(notes, dict):
        notes = {}

    publish_request = notes.get("publish_request")
    if not publish_request:
        raise HTTPException(
            status_code=409,
            detail="No publish request found. Call /publish/request first."
        )

    # Validate cooling period has elapsed.
    requested_at = publish_request.get("requested_at", "")
    if requested_at:
        # Parse ISO timestamp and check elapsed time.
        from datetime import datetime
        try:
            requested_time = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            now_time = datetime.now(requested_time.tzinfo)
            elapsed = (now_time - requested_time).total_seconds()
            if elapsed < _COOLING_SECONDS:
                remaining = _COOLING_SECONDS - elapsed
                raise HTTPException(
                    status_code=409,
                    detail=f"Cooling period not elapsed. {remaining:.1f}s remaining. Wait before confirming."
                )
        except ValueError:
            pass  # If parsing fails, skip the time check (best effort).

    # Validate the payload digest hasn't changed (stale check).
    current_digest = _get_row_digest(row)
    if current_digest != publish_request.get("payload_digest"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"STALE PAYLOAD: digest changed after publish request. "
                f"Expected {publish_request.get('payload_digest')}, got {current_digest}. "
                f"Re-approve before publishing."
            )
        )

    # Now do the actual publish (re-validate marker, publish, advance).
    try:
        # First validate the marker on disk.
        from antiques.approve import read_marker, validate_marker, mark_applied
        marker = read_marker(listing_id)
        validate_marker(marker, row)

        # Perform the publish.
        provider = DryRunProvider()
        result = antiques_publish(listing_id, provider, client=c, apply=True)

        # Mark the marker as applied.
        mark_applied(listing_id)

        # Clean up the publish_request from notes.
        notes.pop("publish_request", None)
        c.patch_listing(listing_id, {"notes": json.dumps(notes) if notes else {}})

        # Refresh the listing to get updated state.
        updated = c.get_listing(listing_id)
        return {
            "status": "ok",
            "published": True,
            "provider_result": result.get("provider_result"),
            "listing": _summarize(updated),
            "message": "Listing published successfully.",
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except IllegalTransition as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/listings/{listing_id}/publish")
async def publish_listing(
    listing_id: str,
    apply: bool = False,
) -> dict:
    """Publish an approved listing (legacy single-step, deprecated).

    DEPRECATED: Use /publish/request + /publish/confirm for the two-step
    cooling gate flow. This endpoint remains for backward compatibility.

    Query param: apply (default: false). When false, performs a dry-run.
    When true, actually publishes and advances status to "listed".

    Returns the provider result (dry-run or actual).
    """
    c = _client()
    row = c.get_listing(listing_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"listing {listing_id} not found")

    if row.get("status") != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"listing {listing_id} is '{row.get('status')}' — must be 'approved' to publish"
        )

    try:
        provider = DryRunProvider()
        result = antiques_publish(listing_id, provider, client=c, apply=apply)
        # Refresh the listing to get updated state.
        updated = c.get_listing(listing_id)
        return {
            "status": "ok",
            "dry_run": result.get("dry_run", False),
            "provider_result": result.get("provider_result"),
            "listing": _summarize(updated),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except IllegalTransition as e:
        raise HTTPException(status_code=409, detail=str(e))
