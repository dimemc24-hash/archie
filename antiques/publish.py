"""
Publish — push an approved listing to a marketplace provider.

Host-side module (hermes venv, stdlib only).

Provider seam pattern:
  - ``ListingProvider`` protocol: ``publish(row) -> provider_ids``
  - ``EbayProvider``: builds the REAL eBay Sell Inventory API sequence
    (inventory item → offer → publish) as request dicts, via an injectable
    HTTP transport. Without ``EBAY_*`` env → ``NotConnected``.
  - ``DryRunProvider``: records what WOULD be sent — no network.

CLI:
  publish.py --id <row> --apply    # re-validate marker, publish, advance approved → listed
  publish.py --id <row>            # dry-run: show what would be sent

The two-step marker split (approve writes it, publish --apply re-validates it)
mirrors run_stage4.py semantics — same stale-marker refusal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any, Protocol

from antiques.approve import mark_applied, read_marker, validate_marker
from antiques.common import IllegalTransition, SupabaseClient, _now_iso
from antiques.pricing import NotConnected

EBAY_API_BASE_DEFAULT = "https://api.sandbox.ebay.com"

# Required eBay env vars for publishing.
EBAY_PUBLISH_ENV = [
    "EBAY_OAUTH_TOKEN",
    "EBAY_MERCHANT_LOCATION_KEY",
    "EBAY_FULFILLMENT_POLICY_ID",
    "EBAY_PAYMENT_POLICY_ID",
    "EBAY_RETURN_POLICY_ID",
]


# --------------------------------------------------------------------------- #
# ListingProvider protocol + implementations
# --------------------------------------------------------------------------- #

class ListingProvider(Protocol):
    """Protocol for marketplace listing providers."""

    def publish(self, row: dict[str, Any]) -> dict[str, Any]:
        """Publish a listing. Returns provider ids dict."""
        ...


class DryRunProvider:
    """Records what WOULD be sent — no network. For testing/review."""

    def __init__(self, records: list[dict[str, Any]] | None = None):
        self._records = records if records is not None else []

    def publish(self, row: dict[str, Any]) -> dict[str, Any]:
        rec = {
            "kind": "dry_run",
            "sku": f"archie-{row.get('id', 'unknown')}",
            "title": row.get("title"),
            "price": (row.get("pricing") or {}).get("recommended"),
            "n_photos": len(row.get("photos") or []),
            "would_send": _build_ebay_requests(row),
            "recorded_at": _now_iso(),
        }
        self._records.append(rec)
        return {
            "kind": "dry_run",
            "sku": rec["sku"],
            "offer_id": "dry-run-offer",
            "listing_id": "dry-run-listing",
            "published_at": _now_iso(),
        }


class EbayProvider:
    """eBay Sell Inventory API publisher (real request shapes).

    Sequence:
      1. PUT /sell/inventory/v1/inventory_item/{sku}  — create inventory item
      2. POST /sell/inventory/v1/offer                 — create offer
      3. POST /sell/inventory/v1/offer/{offerId}/publish — publish

    Requires EBAY_OAUTH_TOKEN, EBAY_MERCHANT_LOCATION_KEY, and the three
    policy IDs. Without them → ``NotConnected`` at construction.
    The ``transport`` is injectable for tests.
    """

    def __init__(
        self,
        *,
        transport=None,
        env: dict[str, str] | None = None,
        api_base: str | None = None,
    ):
        env = env if env is not None else dict(os.environ)
        missing = [k for k in EBAY_PUBLISH_ENV if not env.get(k)]
        if missing:
            raise NotConnected(missing, "eBay Sell Inventory API needs all of these")

        self.token = env["EBAY_OAUTH_TOKEN"]
        self.merchant_location_key = env["EBAY_MERCHANT_LOCATION_KEY"]
        self.fulfillment_policy_id = env["EBAY_FULFILLMENT_POLICY_ID"]
        self.payment_policy_id = env["EBAY_PAYMENT_POLICY_ID"]
        self.return_policy_id = env["EBAY_RETURN_POLICY_ID"]
        self.api_base = (api_base or env.get("EBAY_API_BASE", EBAY_API_BASE_DEFAULT)).rstrip("/")
        self.transport = transport or _default_ebay_transport

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        h = {
            "Authorization": "Bearer " + self.token,
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            "Accept": "application/json",
        }
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def publish(self, row: dict[str, Any]) -> dict[str, Any]:
        import urllib.request as ur

        sku = f"archie-{row['id']}"
        requests = _build_ebay_requests(row, sku=sku, provider=self)

        # 1. Create inventory item
        req1 = ur.Request(
            f"{self.api_base}/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku)}",
            data=json.dumps(requests["inventory_item"]).encode(),
            method="PUT",
            headers=self._headers(json_body=True),
        )
        self.transport(req1)

        # 2. Create offer
        req2 = ur.Request(
            f"{self.api_base}/sell/inventory/v1/offer",
            data=json.dumps(requests["offer"]).encode(),
            method="POST",
            headers=self._headers(json_body=True),
        )
        resp2 = self.transport(req2)
        data2 = _read_resp(resp2)
        offer_id = data2.get("offerId", "") if isinstance(data2, dict) else ""

        # 3. Publish
        req3 = ur.Request(
            f"{self.api_base}/sell/inventory/v1/offer/{urllib.parse.quote(offer_id)}/publish",
            data=b"{}",
            method="POST",
            headers=self._headers(json_body=True),
        )
        resp3 = self.transport(req3)
        data3 = _read_resp(resp3)
        listing_id = data3.get("listingId", "") if isinstance(data3, dict) else ""

        return {
            "kind": "ebay",
            "sku": sku,
            "offer_id": offer_id,
            "listing_id": listing_id,
            "published_at": _now_iso(),
        }


# --------------------------------------------------------------------------- #
# Request builders (shared by EbayProvider and DryRunProvider)
# --------------------------------------------------------------------------- #

def _build_ebay_requests(
    row: dict[str, Any],
    *,
    sku: str | None = None,
    provider: "EbayProvider | None" = None,
) -> dict[str, Any]:
    """Build the eBay API request payloads (inventory item + offer).

    Used by both EbayProvider (sends them) and DryRunProvider (records them).
    """
    sku = sku or f"archie-{row.get('id', 'unknown')}"
    pricing = row.get("pricing") or {}
    price = pricing.get("recommended") or 0.0
    photos = row.get("photos") or []

    # Image URLs: in a full deployment these would be Supabase Storage signed
    # URLs. For the request shape, we use the storage paths as placeholders —
    # the host-side caller would resolve signed URLs before calling publish.
    image_urls = [
        f"supabase://{p.get('bucket', 'listing-photos')}/{p.get('path', '')}"
        for p in photos if isinstance(p, dict)
    ]

    inventory_item = {
        "product": {
            "title": row.get("title", ""),
            "description": row.get("description", ""),
            "imageUrls": image_urls,
            "aspects": _build_aspects(row),
        },
        "condition": _map_condition(row),
        "availability": {
            "shipToLocationAvailability": {"quantity": 1},
        },
    }

    offer = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "pricingSummary": {
            "price": {"value": str(price), "currency": "USD"},
        },
        "categoryId": _guess_category_id(row),
    }
    if provider:
        offer["merchantLocationKey"] = provider.merchant_location_key
        offer["listingPolicies"] = {
            "fulfillmentPolicyId": provider.fulfillment_policy_id,
            "paymentPolicyId": provider.payment_policy_id,
            "returnPolicyId": provider.return_policy_id,
        }
    else:
        offer["merchantLocationKey"] = "${EBAY_MERCHANT_LOCATION_KEY}"
        offer["listingPolicies"] = {
            "fulfillmentPolicyId": "${EBAY_FULFILLMENT_POLICY_ID}",
            "paymentPolicyId": "${EBAY_PAYMENT_POLICY_ID}",
            "returnPolicyId": "${EBAY_RETURN_POLICY_ID}",
        }

    return {"inventory_item": inventory_item, "offer": offer}


def _build_aspects(row: dict[str, Any]) -> dict[str, Any]:
    aspects: dict[str, Any] = {}
    appraisal = row.get("appraisal")
    if isinstance(appraisal, dict):
        if appraisal.get("era"):
            aspects["Decade"] = str(appraisal["era"])
        if appraisal.get("brand"):
            aspects["Brand"] = str(appraisal["brand"])
    if row.get("category_guess"):
        aspects["Type"] = row["category_guess"]
    return aspects


def _map_condition(row: dict[str, Any]) -> str:
    """Map appraisal condition to eBay condition enum."""
    appraisal = row.get("appraisal")
    if not isinstance(appraisal, dict):
        return "USED_EXCELLENT"
    cond = str(appraisal.get("condition", "")).lower()
    if "mint" in cond or "new" in cond:
        return "NEW"
    if "excellent" in cond or "near mint" in cond:
        return "USED_EXCELLENT"
    if "good" in cond:
        return "USED_GOOD"
    if "fair" in cond or "poor" in cond or "worn" in cond:
        return "USED_FAIR"
    return "USED_EXCELLENT"


def _guess_category_id(row: dict[str, Any]) -> str:
    """Best-effort eBay category ID from category_guess."""
    cat = (row.get("category_guess") or "").lower()
    # A few well-known eBay category IDs.
    return {
        "watches": "31387",
        "cards": "26395",
        "furniture": "3192",
        "coins": "11116",
        "books": "267",
    }.get(cat, "1")  # "1" = collectibles default


# --------------------------------------------------------------------------- #
# Transport helpers
# --------------------------------------------------------------------------- #

def _default_ebay_transport(req):
    """Default urllib transport for eBay API calls."""
    import ssl
    ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=30, context=ctx)


def _read_resp(resp: Any) -> Any:
    if isinstance(resp, (dict, list)):
        return resp
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


# --------------------------------------------------------------------------- #
# publish_listing (core logic)
# --------------------------------------------------------------------------- #

def publish_listing(
    row_id: str,
    provider: ListingProvider,
    *,
    client: SupabaseClient | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Publish an approved listing.

    Without ``apply`` (dry-run): validates the marker, builds the request shapes
    via the provider, but does NOT advance status. Returns what would be sent.

    With ``apply``: re-validates the pending-publish marker (stale → refuse),
    calls the provider, stores provider ids in ``provider`` jsonb, advances
    ``approved → listed``, marks the marker applied.
    """
    client = client or SupabaseClient()
    row = client.get_listing(row_id)
    if not row:
        raise ValueError(f"listing {row_id} not found")

    if row.get("status") != "approved":
        raise ValueError(
            f"listing {row_id} is '{row.get('status')}' — must be 'approved'"
        )

    # Always validate the marker (even in dry-run) so the operator sees stale
    # state early.
    marker = read_marker(row_id)
    validate_marker(marker, row)

    if not apply:
        # Dry-run: show what would be sent.
        result = provider.publish(row)
        return {"dry_run": True, "row_id": row_id, "provider_result": result}

    # Apply: publish for real.
    result = provider.publish(row)
    provider_data = {
        "kind": result.get("kind", "unknown"),
        "sku": result.get("sku"),
        "offer_id": result.get("offer_id"),
        "listing_id": result.get("listing_id"),
        "published_at": result.get("published_at", _now_iso()),
    }
    updated = client.advance(row_id, "approved", "listed", {"provider": provider_data})
    mark_applied(row_id)
    return {"dry_run": False, "row_id": row_id, "provider_result": result, "row": updated}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="Publish an approved antiques listing to a marketplace.",
    )
    ap.add_argument("--id", required=True, help="listing row id")
    ap.add_argument("--apply", action="store_true",
                    help="actually publish (default: dry-run)")
    ap.add_argument("--dry-run-provider", action="store_true", default=True,
                    help="use DryRunProvider (default; eBay not connected yet)")
    args = ap.parse_args()

    try:
        if args.dry_run_provider:
            provider = DryRunProvider()
        else:
            provider = EbayProvider()

        result = publish_listing(args.id, provider, apply=args.apply)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except NotConnected as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError, IllegalTransition) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ListingProvider",
    "DryRunProvider",
    "EbayProvider",
    "NotConnected",
    "publish_listing",
]
