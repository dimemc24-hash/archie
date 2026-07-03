"""
Fulfill — order polling, label purchase, and shipping for sold listings.

Host-side module (hermes venv, stdlib only). Designed as a worker skeleton for
a future cron job. Nothing in this build calls live APIs — every HTTP path
goes through an injectable transport.

Flow:
  1. ``poll_orders(provider)`` — query the marketplace for new orders, match
     to listings by SKU, advance ``listed → sold``.
  2. ``LabelProvider.buy_label(row)`` — purchase a shipping label.
     - ``EbayLabels``: NotConnected stub (eBay Logistics API is limited-access).
     - ``Shippo``: NotConnected stub with real request shape.
  3. Store label URL + tracking in ``shipping`` jsonb, advance ``sold → shipped``.
  4. Notify via ``~/harness/alert.sh`` (host side only).

CLI:
  fulfill.py --once --dry-run    # single pass, DryRunLabelProvider
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Protocol

from antiques.common import IllegalTransition, SupabaseClient, _now_iso
from antiques.pricing import NotConnected

EBAY_API_BASE_DEFAULT = "https://api.sandbox.ebay.com"
ALERT_SCRIPT = os.path.join(os.path.expanduser("~"), "harness", "alert.sh")


# --------------------------------------------------------------------------- #
# OrderProvider protocol + eBay implementation
# --------------------------------------------------------------------------- #

class OrderProvider(Protocol):
    """Protocol for marketplace order polling."""

    def poll_orders(self) -> list[dict[str, Any]]:
        """Return a list of order dicts: {order_id, sku, line_item_id, ...}."""
        ...


class EbayOrderProvider:
    """eBay Sell Fulfillment API order poller (real request shape).

    GET /sell/fulfillment/v1/order?filter=orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}
    Matches orders to listings via SKU (lineItems[].sku).

    Requires ``EBAY_OAUTH_TOKEN``. Without it → ``NotConnected``.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        api_base: str | None = None,
        transport=None,
    ):
        self.token = token or os.environ.get("EBAY_OAUTH_TOKEN", "")
        if not self.token:
            raise NotConnected(["EBAY_OAUTH_TOKEN"], "eBay Fulfillment API needs a user token")
        self.api_base = (api_base or os.environ.get("EBAY_API_BASE", EBAY_API_BASE_DEFAULT)).rstrip("/")
        self.transport = transport or _default_transport

    def poll_orders(self) -> list[dict[str, Any]]:
        import urllib.request as ur
        path = (
            "/sell/fulfillment/v1/order"
            "?filter=orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}"
        )
        req = ur.Request(self.api_base + path, method="GET")
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/json")
        resp = self.transport(req)
        data = _read_resp(resp)
        orders_raw = data.get("orders", []) if isinstance(data, dict) else []

        orders: list[dict[str, Any]] = []
        for o in orders_raw:
            line_items = o.get("lineItems", [])
            for li in line_items:
                orders.append({
                    "order_id": o.get("orderId", ""),
                    "sku": li.get("sku", ""),
                    "line_item_id": li.get("lineItemId", ""),
                    "buyer": o.get("buyer", {}).get("username", ""),
                })
        return orders


# --------------------------------------------------------------------------- #
# LabelProvider protocol + implementations
# --------------------------------------------------------------------------- #

class LabelProvider(Protocol):
    """Protocol for shipping label providers."""

    def buy_label(self, row: dict[str, Any]) -> dict[str, Any]:
        """Purchase a label. Returns {label_url, tracking_number, carrier}."""
        ...


class EbayLabels:
    """eBay Logistics API — NotConnected stub.

    eBay's Logistics API is limited-availability. This stub raises
    ``NotConnected`` so the operator knows it's not wired yet. The real shape:
      1. POST /sell/logistics/v1/shipping_quote
      2. POST /sell/logistics/v1/shipping_quote/{quoteId}/create_from_shipping_quote
    """

    def __init__(self):
        raise NotConnected(
            ["EBAY_LOGISTICS_ACCESS"],
            "eBay Logistics API is limited-availability — not yet connected",
        )

    def buy_label(self, row: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class Shippo:
    """Shippo label provider — NotConnected stub with real request shape.

    Real shape:
      POST https://api.goshippo.com/transactions/
      Body: {"shipment": {"address_from", "address_to", "parcels": [{weight/dims}]},
             "carrier_account", "servicelevel_token"}
      Auth: ShippoToken <SHIPPO_API_KEY>
      → label_url, tracking_number

    Requires ``SHIPPO_API_KEY``.
    """

    def __init__(self, *, api_key: str | None = None, transport=None):
        self.api_key = api_key or os.environ.get("SHIPPO_API_KEY", "")
        if not self.api_key:
            raise NotConnected(["SHIPPO_API_KEY"], "Shippo needs an API key")
        self.transport = transport or _default_transport

    def buy_label(self, row: dict[str, Any]) -> dict[str, Any]:
        import urllib.request as ur
        approval = row.get("approval") or {}
        body = {
            "shipment": {
                "address_from": _default_from_address(),
                "address_to": _default_to_address(),
                "parcels": [{
                    "weight": str(approval.get("weight_oz", 16)),
                    "distance_unit": "in",
                    "mass_unit": "oz",
                }],
            },
            "carrier_account": os.environ.get("SHIPPO_CARRIER_ACCOUNT", ""),
            "servicelevel_token": os.environ.get("SHIPPO_SERVICELEVEL", "usps_priority"),
        }
        req = ur.Request(
            "https://api.goshippo.com/transactions/",
            data=json.dumps(body).encode(),
            method="POST",
        )
        req.add_header("Authorization", "ShippoToken " + self.api_key)
        req.add_header("Content-Type", "application/json")
        resp = self.transport(req)
        data = _read_resp(resp)
        return {
            "label_url": data.get("label_url", ""),
            "tracking_number": data.get("tracking_number", data.get("tracking", "")),
            "carrier": "USPS",
        }


class DryRunLabelProvider:
    """Records what WOULD be sent — no network. For testing/cron dry-run."""

    def buy_label(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "label_url": "https://dry-run.example/label.pdf",
            "tracking_number": "DRYRUN" + str(row.get("id", ""))[:8].upper(),
            "carrier": "USPS",
        }


# --------------------------------------------------------------------------- #
# Fulfillment pass
# --------------------------------------------------------------------------- #

def fulfill_pass(
    *,
    order_provider: OrderProvider | None = None,
    label_provider: LabelProvider | None = None,
    client: SupabaseClient | None = None,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """Single fulfillment pass.

    1. Poll for new orders (if order_provider given).
    2. For each sold listing: buy a label, store shipping info, advance sold → shipped.
    3. Notify via alert.sh.

    In dry-run mode, uses DryRunLabelProvider and doesn't actually advance.
    Returns a list of result dicts for logging/inspection.
    """
    client = client or SupabaseClient()
    label_provider = label_provider or (DryRunLabelProvider() if dry_run else Shippo())
    results: list[dict[str, Any]] = []

    # Step 1: poll orders and advance listed → sold.
    if order_provider is not None:
        orders = order_provider.poll_orders()
        for order in orders:
            sku = order.get("sku", "")
            if not sku.startswith("archie-"):
                continue
            listing_id = sku[len("archie-"):]
            row = client.get_listing(listing_id)
            if not row or row.get("status") != "listed":
                continue
            try:
                client.advance(listing_id, "listed", "sold", {
                    "shipping": {"order_id": order.get("order_id"), "order": order},
                })
                results.append({"action": "sold", "listing_id": listing_id,
                                "order_id": order.get("order_id")})
            except IllegalTransition:
                continue  # already sold or status changed

    # Step 2: process sold listings → shipped.
    sold = client.select_listings(status="sold")
    for row in sold:
        listing_id = row.get("id")
        if not listing_id:
            continue
        try:
            label = label_provider.buy_label(row)
            shipping = row.get("shipping") or {}
            if not isinstance(shipping, dict):
                shipping = {}
            shipping["label_url"] = label["label_url"]
            shipping["tracking_number"] = label["tracking_number"]
            shipping["carrier"] = label["carrier"]
            shipping["shipped_at"] = _now_iso()

            if dry_run:
                results.append({
                    "action": "would_ship",
                    "listing_id": listing_id,
                    "tracking": label["tracking_number"],
                    "label_url": label["label_url"],
                })
            else:
                client.advance(listing_id, "sold", "shipped", {"shipping": shipping})
                _notify(listing_id, label)
                results.append({
                    "action": "shipped",
                    "listing_id": listing_id,
                    "tracking": label["tracking_number"],
                })
        except (IllegalTransition, NotConnected) as e:
            results.append({
                "action": "error",
                "listing_id": listing_id,
                "error": str(e),
            })

    return results


# --------------------------------------------------------------------------- #
# Notify
# --------------------------------------------------------------------------- #

def _notify(listing_id: str, label: dict[str, Any]) -> None:
    """Send a Telegram alert about the shipped listing."""
    msg = (
        f"📦 Listing {listing_id} shipped — "
        f"tracking: {label.get('tracking_number', '?')} "
        f"({label.get('carrier', '?')}) — "
        f"label: {label.get('label_url', '')}"
    )
    try:
        subprocess.run(
            [ALERT_SCRIPT, msg],
            timeout=30,
            capture_output=True,
        )
    except Exception:
        pass  # alert failure is non-fatal


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _default_from_address() -> dict[str, str]:
    return {
        "name": os.environ.get("SHIP_FROM_NAME", "Archie Antiques"),
        "street1": os.environ.get("SHIP_FROM_STREET", ""),
        "city": os.environ.get("SHIP_FROM_CITY", ""),
        "state": os.environ.get("SHIP_FROM_STATE", ""),
        "zip": os.environ.get("SHIP_FROM_ZIP", ""),
        "country": "US",
    }


def _default_to_address() -> dict[str, str]:
    return {
        "name": os.environ.get("SHIP_TO_NAME", "Buyer"),
        "street1": os.environ.get("SHIP_TO_STREET", ""),
        "city": os.environ.get("SHIP_TO_CITY", ""),
        "state": os.environ.get("SHIP_TO_STATE", ""),
        "zip": os.environ.get("SHIP_TO_ZIP", ""),
        "country": "US",
    }


def _default_transport(req):
    import ssl
    import urllib.request
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
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="Fulfillment worker for antiques listings (sold → shipped).",
    )
    ap.add_argument("--once", action="store_true",
                    help="run a single pass (for cron)")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="use DryRunLabelProvider, don't advance status")
    args = ap.parse_args()

    if not args.once:
        print("Use --once for a single pass (cron mode).", file=sys.stderr)
        return 1

    try:
        results = fulfill_pass(dry_run=args.dry_run)
        print(json.dumps(results, indent=2, default=str))
        return 0
    except NotConnected as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "OrderProvider",
    "EbayOrderProvider",
    "LabelProvider",
    "EbayLabels",
    "Shippo",
    "DryRunLabelProvider",
    "fulfill_pass",
]
