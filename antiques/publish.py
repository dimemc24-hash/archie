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
from typing import Any, Callable, Protocol

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

    ``url_resolver`` is an optional callable ``(photo_ref) -> https_url`` that
    resolves Storage photo refs to signed URLs at publish time.  The host-side
    CLI wires one backed by ``SupabaseClient.signed_url``.  When ``None``, the
    raw storage paths are used (for tests that don't need real URLs).
    """

    def __init__(
        self,
        *,
        transport=None,
        env: dict[str, str] | None = None,
        api_base: str | None = None,
        url_resolver: Callable[[dict[str, Any]], str] | None = None,
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
        self.url_resolver = url_resolver

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        h = {
            "Authorization": "Bearer " + self.token,
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            "Accept": "application/json",
        }
        if json_body:
            h["Content-Type"] = "application/json"
            h["Content-Language"] = "en-US"
        return h

    def publish(self, row: dict[str, Any]) -> dict[str, Any]:
        import urllib.request as ur

        sku = f"archie-{row['id']}"
        photos = row.get("photos") or []
        if not photos:
            raise ValueError(
                f"listing {row.get('id', '?')} has no photos — eBay hard-rejects "
                f"empty imageUrls (errorId 25717). Photos must exist BEFORE "
                f"approve; the approve marker digests photo count."
            )
        requests = _build_ebay_requests(
            row, sku=sku, provider=self, url_resolver=self.url_resolver,
        )

        # 1. Create (or update) inventory item
        req1 = ur.Request(
            f"{self.api_base}/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku)}",
            data=json.dumps(requests["inventory_item"]).encode(),
            method="PUT",
            headers=self._headers(json_body=True),
        )
        self.transport(req1)

        # 2. Create offer (retry-safe: if a duplicate offer already exists from
        #    a failed prior run, recover the existing offerId and PUT-update it
        #    instead of failing).
        offer_id = self._create_or_recover_offer(sku, requests["offer"])

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

    def _create_or_recover_offer(self, sku: str, offer_body: dict[str, Any]) -> str:
        """POST the offer; on a duplicate-offer error (25002 family), recover
        the existing offerId (from the error body or a GET by SKU), PUT-update
        it, and return the recovered id.  This prevents stranded offers after
        a step-3 failure leaves an offer behind."""
        import urllib.request as ur

        req2 = ur.Request(
            f"{self.api_base}/sell/inventory/v1/offer",
            data=json.dumps(offer_body).encode(),
            method="POST",
            headers=self._headers(json_body=True),
        )
        resp2 = self.transport(req2)
        data2 = _read_resp(resp2)
        if isinstance(data2, dict) and data2.get("offerId"):
            return data2["offerId"]

        # Duplicate-offer recovery: eBay returns errorId 25002 with the
        # existing offerId in the error parameters, or we can GET it by SKU.
        if isinstance(data2, dict) and self._is_duplicate_offer_error(data2):
            offer_id = _extract_offer_id_from_error(data2)
            if not offer_id:
                offer_id = self._get_offer_id_by_sku(sku)
            if offer_id:
                # PUT-update the existing offer with the current body.
                put_req = ur.Request(
                    f"{self.api_base}/sell/inventory/v1/offer/{urllib.parse.quote(offer_id)}",
                    data=json.dumps(offer_body).encode(),
                    method="PUT",
                    headers=self._headers(json_body=True),
                )
                self.transport(put_req)
                return offer_id

        # Unexpected response — let the caller see the error.
        raise RuntimeError(f"create offer: unexpected response: {data2}")

    @staticmethod
    def _is_duplicate_offer_error(data: dict[str, Any]) -> bool:
        errors = data.get("errors", [])
        if not isinstance(errors, list):
            return False
        for err in errors:
            if isinstance(err, dict) and err.get("errorId") in (25002, 25003):
                return True
        return False

    def _get_offer_id_by_sku(self, sku: str) -> str:
        """GET /sell/inventory/v1/offer?sku=<sku> to find an existing offer."""
        import urllib.request as ur
        req = ur.Request(
            f"{self.api_base}/sell/inventory/v1/offer?sku={urllib.parse.quote(sku)}",
            method="GET",
            headers=self._headers(),
        )
        resp = self.transport(req)
        data = _read_resp(resp)
        if isinstance(data, dict):
            offers = data.get("offers", [])
            if isinstance(offers, list) and offers:
                first = offers[0]
                if isinstance(first, dict):
                    return first.get("offerId", "")
        return ""


# --------------------------------------------------------------------------- #
# Request builders (shared by EbayProvider and DryRunProvider)
# --------------------------------------------------------------------------- #

def _build_ebay_requests(
    row: dict[str, Any],
    *,
    sku: str | None = None,
    provider: "EbayProvider | None" = None,
    url_resolver: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Build the eBay API request payloads (inventory item + offer).

    Used by both EbayProvider (sends them) and DryRunProvider (records them).
    ``url_resolver``: when provided (EbayProvider with a SupabaseClient-backed
    resolver), each photo ref is resolved to a real signed https URL.  When
    ``None`` (DryRunProvider / tests), placeholder ``supabase://`` URLs are
    recorded instead — no network.
    """
    sku = sku or f"archie-{row.get('id', 'unknown')}"
    pricing = row.get("pricing") or {}
    price = pricing.get("recommended") or 0.0
    photos = row.get("photos") or []

    if url_resolver:
        image_urls = [
            url_resolver(p)
            for p in photos if isinstance(p, dict)
        ]
    else:
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
        "categoryId": _resolve_category_id(row),
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
    """Build product.aspects from appraisal + category_guess.

    eBay requires every aspect value to be a ``list[str]`` (errorId 2004 if a
    bare string is sent). We wrap each value in a single-element list.
    """
    aspects: dict[str, Any] = {}
    appraisal = row.get("appraisal")
    if isinstance(appraisal, dict):
        if appraisal.get("era"):
            aspects["Decade"] = [str(appraisal["era"])]
        if appraisal.get("brand"):
            aspects["Brand"] = [str(appraisal["brand"])]
    if row.get("category_guess"):
        aspects["Type"] = [str(row["category_guess"])]
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


def _resolve_category_id(row: dict[str, Any]) -> str:
    """Resolve the eBay leaf categoryId for the offer payload.

    Per the category-strategy council decision (option b), the leaf category
    is resolved at PRICING time via Taxonomy ``get_category_suggestions`` and
    stored in ``pricing.category_id``.  Morley sees it at the approve gate and
    can correct it.  Publish is deterministic — no network calls here.

    If the pricing jsonb has no ``category_id`` (pricing ran without a
    TaxonomyResolver, or resolution returned nothing), this raises an
    actionable error telling the operator exactly what to set — never a silent
    default to ``"1"`` (which is a non-leaf node and triggers errorId 25005 at
    publishOffer).
    """
    pricing = row.get("pricing")
    if isinstance(pricing, dict):
        cat_id = pricing.get("category_id")
        if cat_id:
            return str(cat_id)
    raise ValueError(
        f"listing {row.get('id', '?')} has no pricing.category_id — "
        f"the leaf category was not resolved at pricing time. "
        f"Re-price with a TaxonomyResolver, or set category_id manually "
        f"in the pricing jsonb (e.g. via approve with price_override). "
        f"Do NOT use '1' — it is a non-leaf node (errorId 25005)."
    )


def _extract_offer_id_from_error(data: dict[str, Any]) -> str:
    """Extract an existing offerId from a duplicate-offer error body.

    eBay's 25002 error carries the existing offerId in the error parameters
    (name 'offerId' or embedded in a message).  Returns '' if not found.
    """
    errors = data.get("errors", [])
    if not isinstance(errors, list):
        return ""
    for err in errors:
        if not isinstance(err, dict):
            continue
        params = err.get("parameters", [])
        if isinstance(params, list):
            for p in params:
                if isinstance(p, dict) and p.get("name") in ("offerId", "offer_id"):
                    val = p.get("value", "")
                    if val:
                        return str(val)
    return ""


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
    ap.add_argument("--provider", choices=["dryrun", "ebay"], default="dryrun",
                    help="listing provider to use (default: dryrun)")
    args = ap.parse_args()

    try:
        if args.provider == "ebay":
            # Wire a SupabaseClient-backed URL resolver for photo signed URLs.
            from antiques.common import SupabaseClient
            supa = SupabaseClient()

            def resolver(photo_ref: dict[str, Any]) -> str:
                return supa.signed_url(photo_ref.get("path", ""))

            provider = EbayProvider(url_resolver=resolver)
        else:
            provider = DryRunProvider()

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
    "_build_ebay_requests",
    "_resolve_category_id",
]
