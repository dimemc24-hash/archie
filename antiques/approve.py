"""
Approve — human approval gate for listings.

Host-side module (hermes venv, stdlib only).

``approve(row_id, weight_oz, dims, price_override=None)``:
  - Validates status is ``priced`` (or ``draft`` with a manual price override).
  - Records approval jsonb (approved_by, approved_at, weight_oz, dims,
    price_override).
  - Advances to ``approved``.
  - Writes a **pending-publish marker** to
    ``~/harness/artifacts/antiques/<id>/pending-publish.json`` — keyed to a
    digest of the row's content (title + price + photo count), same two-step
    marker pattern as run_stage4.py. publish.py --apply re-validates this
    marker before publishing.

``reject(row_id, reason)``:
  - Advances ``draft``/``priced`` → ``rejected`` with a reason in notes.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from antiques.common import IllegalTransition, SupabaseClient, _now_iso, _parse_notes

# --------------------------------------------------------------------------- #
# Marker paths
# --------------------------------------------------------------------------- #

HARNESS_DIR = Path.home() / "harness"
ARTIFACTS_DIR = HARNESS_DIR / "artifacts" / "antiques"


def _marker_dir(listing_id: str) -> Path:
    d = ARTIFACTS_DIR / listing_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker_path(listing_id: str) -> Path:
    return _marker_dir(listing_id) / "pending-publish.json"


# --------------------------------------------------------------------------- #
# Row digest (detect changes between approve and publish)
# --------------------------------------------------------------------------- #

def _row_digest(row: dict[str, Any]) -> str:
    """Short hash of the row's publish-relevant fields.

    If the title, price, or photo count changes between approve and publish,
    the digest won't match and publish refuses — same stale-marker semantics
    as run_stage4.py.
    """
    pricing = row.get("pricing") or {}
    price = pricing.get("recommended") if isinstance(pricing, dict) else None
    photos = row.get("photos") or []
    n_photos = len(photos) if isinstance(photos, list) else 0
    payload = f"{row.get('title','')}|{price}|{n_photos}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Approve
# --------------------------------------------------------------------------- #

def approve(
    row_id: str,
    weight_oz: float,
    dims: dict[str, float] | None = None,
    *,
    price_override: float | None = None,
    approved_by: str = "morley",
    client: SupabaseClient | None = None,
) -> dict[str, Any]:
    """Approve a listing for publication.

    The row must be ``priced`` (or ``draft`` if a ``price_override`` is given —
    manual pricing at approve time). Advances to ``approved`` and writes the
    pending-publish marker.
    """
    client = client or SupabaseClient()
    row = client.get_listing(row_id)
    if not row:
        raise ValueError(f"listing {row_id} not found")

    status = row.get("status", "")
    if status == "priced":
        pass  # normal path
    elif status == "draft" and price_override is not None:
        pass  # manual price at approve time
    elif status == "approved":
        raise ValueError(f"listing {row_id} is already approved")
    else:
        raise ValueError(
            f"listing {row_id} is '{status}' — must be 'priced' "
            f"(or 'draft' with price_override)"
        )

    # If price_override given, patch pricing before advancing.
    patch: dict[str, Any] = {}
    if price_override is not None:
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            pricing = {}
        pricing["recommended"] = price_override
        pricing["method"] = pricing.get("method", "manual") + "+override"
        pricing["priced_at"] = _now_iso()
        patch["pricing"] = pricing

    approval = {
        "approved_by": approved_by,
        "approved_at": _now_iso(),
        "weight_oz": weight_oz,
        "dims": dims or {},
        "price_override": price_override,
    }
    patch["approval"] = approval

    updated = client.advance(row_id, status, "approved", patch)

    # Write the pending-publish marker.
    marker = {
        "listing_id": row_id,
        "row_digest": _row_digest(updated),
        "price": (updated.get("pricing") or {}).get("recommended"),
        "n_photos": len(updated.get("photos") or []),
        "approved_at": approval["approved_at"],
        "approved_by": approved_by,
        "written_at": _now_iso(),
        "applied": False,
    }
    mpath = _marker_path(row_id)
    mpath.write_text(json.dumps(marker, indent=2))
    try:
        os.chmod(mpath, 0o600)
    except OSError:
        pass

    return updated


# --------------------------------------------------------------------------- #
# Reject
# --------------------------------------------------------------------------- #

def reject(
    row_id: str,
    reason: str,
    *,
    client: SupabaseClient | None = None,
) -> dict[str, Any]:
    """Reject a listing. The row must be ``draft`` or ``priced``.

    Records the reason in notes and advances to ``rejected``.
    """
    client = client or SupabaseClient()
    row = client.get_listing(row_id)
    if not row:
        raise ValueError(f"listing {row_id} not found")

    status = row.get("status", "")
    if status not in ("draft", "priced"):
        raise ValueError(
            f"listing {row_id} is '{status}' — can only reject from 'draft' or 'priced'"
        )

    # Merge rejection reason into notes.
    existing = _parse_notes(row.get("notes"))
    reject_note = {"rejected_at": _now_iso(), "reason": reason}
    if isinstance(existing, dict):
        notes_list = existing.get("rejections", [])
        if not isinstance(notes_list, list):
            notes_list = []
        notes_list.append(reject_note)
        existing["rejections"] = notes_list
        new_notes = json.dumps(existing)
    elif isinstance(existing, str) and existing.strip():
        new_notes = json.dumps({"prior_text": existing, "rejections": [reject_note]})
    else:
        new_notes = json.dumps({"rejections": [reject_note]})

    return client.advance(row_id, status, "rejected", {"notes": new_notes})


# --------------------------------------------------------------------------- #
# Marker read/validate (used by publish.py)
# --------------------------------------------------------------------------- #

def read_marker(listing_id: str) -> dict[str, Any]:
    """Read the pending-publish marker. Raises if missing or corrupt."""
    mpath = _marker_path(listing_id)
    if not mpath.is_file():
        raise FileNotFoundError(
            f"no pending-publish marker at {mpath}. "
            f"Run approve() first to approve the listing."
        )
    try:
        return json.loads(mpath.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"pending-publish marker at {mpath} is corrupt: {e}") from e


def validate_marker(marker: dict[str, Any], row: dict[str, Any]) -> None:
    """Check the marker's row_digest matches the current row. Raises if stale."""
    if marker.get("applied"):
        raise ValueError(
            f"pending-publish marker is already marked 'applied' "
            f"(applied at {marker.get('applied_at', '?')}). "
            f"This publish was already done."
        )
    fresh_digest = _row_digest(row)
    if fresh_digest != marker.get("row_digest"):
        raise ValueError(
            f"STALE MARKER: pending-publish marker records digest "
            f"{marker.get('row_digest')}, but the listing row now has "
            f"{fresh_digest}. The listing changed after approval — "
            f"re-approve before publishing."
        )


def mark_applied(listing_id: str) -> None:
    """Mark the pending-publish marker as applied (after a successful publish)."""
    mpath = _marker_path(listing_id)
    if not mpath.is_file():
        return
    marker = json.loads(mpath.read_text())
    marker["applied"] = True
    marker["applied_at"] = _now_iso()
    mpath.write_text(json.dumps(marker, indent=2))


__all__ = [
    "approve",
    "reject",
    "read_marker",
    "validate_marker",
    "mark_applied",
    "ARTIFACTS_DIR",
]
