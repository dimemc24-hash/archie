"""
Pricing — comparable-sales research and price recommendation for listings.

Host-side module (hermes venv, stdlib only). Uses SupabaseClient from
common.py for row reads/writes; HTTP for comps goes through an injectable
transport so tests never touch the network.

Provider seam pattern:
  - ``CompsProvider`` protocol: ``search_comps(query) -> list[Comp]``
  - ``ManualComps``: operator-supplied list (no network, for testing/manual)
  - ``EbayBrowseComps``: real eBay Buy Browse API request/parse shapes, but
    constructor requires ``EBAY_OAUTH_TOKEN`` — missing → ``NotConnected`` with
    a friendly message naming the missing env var.
  - ``SoldComps``: stub raising ``NotConnected`` — true sold comps need the
    restricted Marketplace Insights API (not yet available).

``price_listing(row_id, provider)`` writes the ``pricing`` jsonb (comps,
recommended price + range, method, priced_at) and advances ``draft → priced``.
"""
from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol, Sequence

from antiques.common import (
    IllegalTransition,
    SupabaseClient,
    _now_iso,
    default_transport,
)

# --------------------------------------------------------------------------- #
# NotConnected
# --------------------------------------------------------------------------- #


class NotConnected(RuntimeError):
    """Raised when a provider lacks the env vars it needs.

    Carries ``.missing`` — a list of env-var NAMES (never values) — so callers
    can surface exactly what to set.
    """

    def __init__(self, missing: Sequence[str], detail: str = ""):
        self.missing = list(missing)
        names = ", ".join(self.missing)
        msg = f"provider not connected — missing env: {names}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


# --------------------------------------------------------------------------- #
# CompsProvider protocol + implementations
# --------------------------------------------------------------------------- #

EBAY_API_BASE_DEFAULT = "https://api.sandbox.ebay.com"


class CompsProvider(Protocol):
    """Protocol for comparable-sales providers."""

    def search_comps(self, query: str) -> list[dict[str, Any]]:
        """Return a list of comp dicts: title, price, currency, condition, url."""
        ...


class ManualComps:
    """Operator-supplied comps — no network. For manual pricing or testing."""

    def __init__(self, comps: list[dict[str, Any]]):
        self._comps = list(comps)

    def search_comps(self, query: str) -> list[dict[str, Any]]:
        return list(self._comps)


class EbayBrowseComps:
    """eBay Buy Browse API comps (active listings).

    Constructs the REAL request shape:
      GET /buy/browse/v1/item_summary/search?q=<query>&limit=25
         &filter=buyingOptions:{FIXED_PRICE}
      Headers: Authorization: Bearer <token>, X-EBAY-C-MARKETPLACE-ID: EBAY_US

    Requires ``EBAY_OAUTH_TOKEN``. Without it → ``NotConnected`` at construction.
    The ``transport`` is injectable for tests.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        api_base: str | None = None,
        transport=default_transport,
    ):
        self.token = token or os.environ.get("EBAY_OAUTH_TOKEN", "")
        self.api_base = (api_base or os.environ.get("EBAY_API_BASE", EBAY_API_BASE_DEFAULT)).rstrip("/")
        self.transport = transport
        if not self.token:
            raise NotConnected(
                ["EBAY_OAUTH_TOKEN"],
                "eBay Browse API needs a user access token",
            )

    def search_comps(self, query: str) -> list[dict[str, Any]]:
        path = (
            f"/buy/browse/v1/item_summary/search"
            f"?q={urllib.parse.quote(query)}"
            f"&limit=25"
            f"&filter=buyingOptions:{{FIXED_PRICE}}"
        )
        req = urllib.request.Request(self.api_base + path, method="GET")
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("X-EBAY-C-MARKETPLACE-ID", "EBAY_US")
        req.add_header("Accept", "application/json")

        resp = self.transport(req)
        raw = resp.read() if not isinstance(resp, (dict, list)) else None
        data = json.loads(raw) if raw else resp

        items = data.get("itemSummaries", []) if isinstance(data, dict) else []
        comps: list[dict[str, Any]] = []
        for item in items:
            price = item.get("price", {})
            comps.append({
                "title": item.get("title", ""),
                "price": float(price.get("value", 0)),
                "currency": price.get("currency", "USD"),
                "condition": item.get("condition", "UNKNOWN"),
                "url": item.get("itemWebUrl", ""),
                "source": "ebay_browse",
            })
        return comps


class SoldComps:
    """Stub for sold-comps via eBay Marketplace Insights API.

    The Insights API is restricted-access. This stub always raises
    ``NotConnected`` so callers know it's not wired yet.
    """

    def __init__(self):
        raise NotConnected(
            ["EBAY_MARKETPLACE_INSIGHTS_ACCESS"],
            "Sold comps need the restricted eBay Marketplace Insights API",
        )

    def search_comps(self, query: str) -> list[dict[str, Any]]:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# TaxonomyResolver — leaf-category resolution at pricing time (council option b)
# --------------------------------------------------------------------------- #

class TaxonomyResolver(Protocol):
    """Protocol for eBay Taxonomy leaf-category resolution."""

    def resolve_category(self, query: str) -> dict[str, Any] | None:
        """Resolve a search term to a leaf category.

        Returns ``{"category_id": str, "category_name": str}`` or ``None``
        if no suggestion is returned.
        """
        ...


class EbayTaxonomyResolver:
    """eBay Taxonomy API leaf-category resolver (real request shape).

    Calls ``GET /commerce/taxonomy/v1/category_tree/0/get_category_suggestions
    ?q=<query>`` with user-token Bearer auth.  Takes the top suggestion's
    ``categoryId`` (verified working: 'candlestick' → 4062, publish-verified).

    Requires ``EBAY_OAUTH_TOKEN``. Without it → ``NotConnected`` at
    construction.  The ``transport`` is injectable for tests.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        api_base: str | None = None,
        transport=default_transport,
    ):
        self.token = token or os.environ.get("EBAY_OAUTH_TOKEN", "")
        self.api_base = (api_base or os.environ.get("EBAY_API_BASE", EBAY_API_BASE_DEFAULT)).rstrip("/")
        self.transport = transport
        if not self.token:
            raise NotConnected(
                ["EBAY_OAUTH_TOKEN"],
                "eBay Taxonomy API needs a user access token",
            )

    def resolve_category(self, query: str) -> dict[str, Any] | None:
        if not query:
            return None
        path = (
            "/commerce/taxonomy/v1/category_tree/0/get_category_suggestions"
            f"?q={urllib.parse.quote(query)}"
        )
        req = urllib.request.Request(self.api_base + path, method="GET")
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/json")

        resp = self.transport(req)
        raw = resp.read() if not isinstance(resp, (dict, list)) else None
        data = json.loads(raw) if raw else resp

        suggestions = data.get("categorySuggestions", []) if isinstance(data, dict) else []
        if not suggestions or not isinstance(suggestions, list):
            return None
        first = suggestions[0]
        if not isinstance(first, dict):
            return None
        cat = first.get("category", {})
        if not isinstance(cat, dict):
            return None
        cat_id = cat.get("categoryId", "")
        if not cat_id:
            return None
        return {
            "category_id": str(cat_id),
            "category_name": cat.get("categoryName", ""),
        }


class CategoryNotResolvedError(RuntimeError):
    """Raised when Taxonomy resolution returns no leaf category.

    Actionable: tells the operator to set ``category_guess`` or manually
    set ``pricing.category_id`` — never a silent default to ``"1"``.
    """

    def __init__(self, query: str, listing_id: str = ""):
        self.query = query
        self.listing_id = listing_id
        loc = f" listing {listing_id}" if listing_id else ""
        super().__init__(
            f"category not resolved for query '{query}'{loc} — "
            f"eBay Taxonomy get_category_suggestions returned no suggestions. "
            f"Set category_guess on the listing to a more specific term, "
            f"or manually set pricing.category_id to a known leaf category ID. "
            f"Do NOT use '1' — it is a non-leaf node (errorId 25005)."
        )


# --------------------------------------------------------------------------- #
# Price recommendation
# --------------------------------------------------------------------------- #

def recommend_price(comps: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute a recommended price from comps.

    Method: median of comp prices, with a ±25% range.
    Returns: {recommended, low, high, method, n_comps}
    """
    prices = [c["price"] for c in comps if isinstance(c.get("price"), (int, float)) and c["price"] > 0]
    if not prices:
        return {
            "recommended": None,
            "low": None,
            "high": None,
            "method": "active-comps-median",
            "n_comps": 0,
        }
    median = statistics.median(prices)
    return {
        "recommended": round(median, 2),
        "low": round(median * 0.75, 2),
        "high": round(median * 1.25, 2),
        "method": "active-comps-median",
        "n_comps": len(prices),
    }


# --------------------------------------------------------------------------- #
# price_listing
# --------------------------------------------------------------------------- #

def price_listing(
    row_id: str,
    provider: CompsProvider,
    client: SupabaseClient | None = None,
    *,
    query: str | None = None,
    taxonomy_resolver: TaxonomyResolver | None = None,
) -> dict[str, Any]:
    """Price a listing: fetch comps, compute recommendation, write pricing jsonb,
    advance ``draft → priced``.

    ``query`` defaults to the listing title.
    ``taxonomy_resolver``: when provided, resolves the eBay leaf category via
    Taxonomy ``get_category_suggestions`` and stores ``category_id`` +
    ``category_name`` in the pricing jsonb so Morley can verify/correct it at
    the approve gate.  When resolution returns nothing, raises
    ``CategoryNotResolvedError`` (actionable — never a silent default).

    Returns the updated row.
    """
    client = client or SupabaseClient()
    row = client.get_listing(row_id)
    if not row:
        raise ValueError(f"listing {row_id} not found")

    q = query or row.get("title") or ""
    if not q:
        raise ValueError("no query and listing has no title — cannot search comps")

    comps = provider.search_comps(q)
    rec = recommend_price(comps)

    pricing: dict[str, Any] = {
        "comps": comps,
        "recommended": rec["recommended"],
        "range": {"low": rec["low"], "high": rec["high"]},
        "method": rec["method"],
        "n_comps": rec["n_comps"],
        "priced_at": _now_iso(),
    }

    # Leaf-category resolution at pricing time (council option b).  The
    # resolved category_id is stored in pricing jsonb so it surfaces at the
    # approve gate.  No resolution → actionable error, never a silent default.
    if taxonomy_resolver is not None:
        # Use category_guess if present (more specific than title), else title.
        cat_query = row.get("category_guess") or q
        resolved = taxonomy_resolver.resolve_category(cat_query)
        if resolved is None:
            raise CategoryNotResolvedError(cat_query, row_id)
        pricing["category_id"] = resolved["category_id"]
        pricing["category_name"] = resolved["category_name"]

    return client.advance(row_id, "draft", "priced", {"pricing": pricing})


__all__ = [
    "NotConnected",
    "CompsProvider",
    "ManualComps",
    "EbayBrowseComps",
    "SoldComps",
    "TaxonomyResolver",
    "EbayTaxonomyResolver",
    "CategoryNotResolvedError",
    "recommend_price",
    "price_listing",
]
