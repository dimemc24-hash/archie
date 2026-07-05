"""Tests for the listings dashboard plugin backend.

Tests the write-enabled endpoints (price, approve, reject, publish) using
a stub transport that simulates the Supabase backend without network calls.

Run: python3 -m pytest dashboard-plugins/test_listings_plugin.py -q
Requires fastapi (the plugin imports it); skipped cleanly if absent.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

PLUG_DIR = Path(__file__).resolve().parent


def _load(alias: str, rel: str):
    spec = importlib.util.spec_from_file_location(alias, PLUG_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


listings_api = _load("listings_api", "listings/dashboard/plugin_api.py")


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Stub transport — a fake Supabase backend for all tests
# --------------------------------------------------------------------------- #

class StubTransport:
    """Records all requests and returns canned responses.

    Simulates a Supabase backend with a single `listings` table and a
    Storage bucket. Every response is an in-memory dict/list — no network.
    """

    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}
        self.uploads: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self._next_id = 0

    def __call__(self, req):
        self.requests.append({
            "method": req.get_method(),
            "url": req.full_url if hasattr(req, "full_url") else str(req),
            "headers": dict(req.header_items()) if hasattr(req, "header_items") else {},
        })
        method = req.get_method()
        url = req.full_url if hasattr(req, "full_url") else str(req)

        if method == "POST" and "/rest/v1/listings" in url:
            body = json.loads(req.data.decode())
            row_id = f"test-{self._next_id}"
            self._next_id += 1
            row = {"id": row_id, "created_at": "2026-07-03T00:00:00Z",
                   "updated_at": "2026-07-03T00:00:00Z", **body}
            self.rows[row_id] = row
            return [row]

        if method == "PATCH" and "/rest/v1/listings" in url:
            body = json.loads(req.data.decode())
            # Extract row id from query string.
            row_id = _extract_id_from_url(url)
            if row_id in self.rows:
                # Check status filter for advance().
                if "status=eq." in url:
                    expected_status = url.split("status=eq.")[1].split("&")[0]
                    if self.rows[row_id].get("status") != expected_status:
                        return []  # no rows updated — illegal transition
                self.rows[row_id].update(body)
                return [self.rows[row_id]]
            return []

        if method == "GET" and "/rest/v1/listings" in url:
            if "id=eq." in url:
                row_id = _extract_id_from_url(url)
                if row_id in self.rows:
                    return [self.rows[row_id]]
                return []
            # select_listings
            status_filter = None
            if "status=eq." in url:
                status_filter = url.split("status=eq.")[1].split("&")[0]
            rows = list(self.rows.values())
            if status_filter:
                rows = [r for r in rows if r.get("status") == status_filter]
            return rows

        if method == "POST" and "/storage/v1/bucket" in url:
            return {"id": "listing-photos"}  # bucket created/exists

        if method == "PUT" and "/storage/v1/object/" in url:
            self.uploads.append({"url": url, "size": len(req.data) if req.data else 0})
            return {}  # upload success

        if method == "POST" and "/storage/v1/object/sign/" in url:
            return {"signedURL": "https://stub.supabase.co/signed/..."}

        return {}


def _extract_id_from_url(url: str) -> str:
    if "id=eq." in url:
        return url.split("id=eq.")[1].split("&")[0]
    return ""


def _high_confidence_appraisal(era="1968", brand="Omega", condition="excellent"):
    """Helper: build an appraisal jsonb with high/high confidence (clean path)."""
    return {
        "era": era,
        "brand": brand,
        "condition": condition,
        "confidence": {"id": "high", "value": "high"},
    }


def _low_confidence_appraisal(ident="medium", val="low"):
    """Helper: build an appraisal jsonb with non-high confidence (flagged path)."""
    return {
        "era": "1968",
        "brand": "Unknown",
        "condition": "good",
        "confidence": {"id": ident, "value": val},
    }


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def stub(monkeypatch, tmp_path):
    """A fresh stub backend + client for each test, with artifacts dir patched."""
    t = StubTransport()

    # Patch the plugin to use our stub transport
    from antiques import common as common_mod
    monkeypatch.setattr(common_mod, "default_transport", t)

    # Create a mock SupabaseClient that uses the stub transport
    client = listings_api.SupabaseClient(
        url="https://stub.supabase.co",
        key="stub-key",
        transport=t,
    )
    monkeypatch.setattr(listings_api, "_client", lambda: client)

    # Patch artifacts dir for approve markers
    from antiques import approve as approve_mod
    artifacts_dir = tmp_path / "antiques"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(approve_mod, "ARTIFACTS_DIR", artifacts_dir)

    return client, t


@pytest.fixture
def draft_with_appraisal(stub):
    """Create a draft listing with high-confidence appraisal."""
    client, _ = stub
    row = client.insert_listing({
        "status": "draft",
        "title": "Vintage Omega Seamaster",
        "description": "Seamaster cal. 601",
        "category_guess": "Watches",
        "appraisal": _high_confidence_appraisal(),
        "photos": [],
    })
    return client, row


@pytest.fixture
def priced_listing(stub):
    """Create a priced listing ready for approval."""
    client, _ = stub
    row = client.insert_listing({
        "status": "draft",
        "title": "Vintage Omega",
        "appraisal": _high_confidence_appraisal(),
        "photos": [],
    })
    # Price it with manual comps
    from antiques.pricing import ManualComps, price_listing
    result = price_listing(row["id"], ManualComps([{"price": 200.0}]), client)
    # Add category_id (required for publish)
    pricing = result.get("pricing") or {}
    pricing["category_id"] = "31387"  # Wristwatches
    pricing["category_name"] = "Wristwatches"
    client.patch_listing(result["id"], {"pricing": pricing})
    # Re-fetch to get updated row
    result = client.get_listing(result["id"])
    return client, result


@pytest.fixture
def approved_listing(stub, tmp_path):
    """Create an approved listing ready for publishing."""
    client, _ = stub
    row = client.insert_listing({
        "status": "draft",
        "title": "Vintage Omega",
        "appraisal": _high_confidence_appraisal(),
        "photos": [],
    })
    # Price it
    from antiques.pricing import ManualComps, price_listing
    result = price_listing(row["id"], ManualComps([{"price": 200.0}]), client)
    # Add category_id (required for publish)
    pricing = result.get("pricing") or {}
    pricing["category_id"] = "31387"  # Wristwatches
    pricing["category_name"] = "Wristwatches"
    client.patch_listing(result["id"], {"pricing": pricing})
    # Approve it
    from antiques.approve import approve
    result = approve(result["id"], weight_oz=5.0, dims={"l": 6, "w": 4, "h": 2}, client=client)
    return client, result


# --------------------------------------------------------------------------- #
# Tests: price endpoint
# --------------------------------------------------------------------------- #

class TestPriceEndpoint:

    def test_price_draft_listing(self, stub):
        """Pricing a draft listing advances it to priced status."""
        client, _ = stub
        row = client.insert_listing({
            "status": "draft",
            "title": "Test Watch",
            "photos": [],
        })

        comps = [{"price": 150.0}, {"price": 200.0}, {"price": 250.0}]
        result = _run(listings_api.price_listing(row["id"], comps))

        assert result["status"] == "ok"
        assert result["listing"]["status"] == "priced"
        assert result["listing"]["price"] == 200.0  # median

    def test_price_non_draft_fails(self, stub):
        """Pricing a non-draft listing returns 409."""
        client, _ = stub
        row = client.insert_listing({
            "status": "priced",
            "title": "Test",
            "photos": [],
        })

        with pytest.raises(listings_api.HTTPException) as exc_info:
            _run(listings_api.price_listing(row["id"], [{"price": 100}]))

        assert exc_info.value.status_code == 409
        assert "must be 'draft'" in exc_info.value.detail

    def test_price_nonexistent_fails(self, stub):
        """Pricing a nonexistent listing returns 404."""
        with pytest.raises(listings_api.HTTPException) as exc_info:
            _run(listings_api.price_listing("nonexistent", []))

        assert exc_info.value.status_code == 404


# --------------------------------------------------------------------------- #
# Tests: approve endpoint
# --------------------------------------------------------------------------- #

class TestApproveEndpoint:

    def test_approve_priced_listing(self, priced_listing):
        """Approving a priced listing advances it to approved."""
        client, priced = priced_listing

        result = _run(listings_api.approve_listing(
            priced["id"],
            weight_oz=5.0,
            dims={"l": 6, "w": 4, "h": 2},
            approved_by="tester",
        ))

        assert result["status"] == "ok"
        assert result["listing"]["status"] == "approved"

    def test_approve_draft_with_price_override(self, draft_with_appraisal):
        """Approving a draft with price_override works."""
        client, draft = draft_with_appraisal

        result = _run(listings_api.approve_listing(
            draft["id"],
            weight_oz=3.0,
            price_override=150.0,
        ))

        assert result["status"] == "ok"
        assert result["listing"]["status"] == "approved"

    def test_approve_low_confidence_returns_409_with_payload(self, stub):
        """Low confidence approval returns 409 with confidence payload."""
        client, _ = stub
        row = client.insert_listing({
            "status": "priced",
            "title": "Uncertain Item",
            "appraisal": _low_confidence_appraisal(ident="medium", val="low"),
            "photos": [],
        })

        with pytest.raises(listings_api.HTTPException) as exc_info:
            _run(listings_api.approve_listing(
                row["id"],
                weight_oz=5.0,
                acknowledge_low_confidence=False,
            ))

        assert exc_info.value.status_code == 409
        detail = exc_info.value.detail
        assert detail["error"] == "low_confidence"
        assert detail["confidence"]["identification"] == "medium"
        assert detail["confidence"]["valuation"] == "low"

    def test_approve_low_confidence_with_ack_succeeds(self, stub):
        """Low confidence approval with ack succeeds."""
        client, _ = stub
        row = client.insert_listing({
            "status": "priced",
            "title": "Uncertain Item",
            "appraisal": _low_confidence_appraisal(ident="medium", val="low"),
            "photos": [],
        })

        result = _run(listings_api.approve_listing(
            row["id"],
            weight_oz=5.0,
            acknowledge_low_confidence=True,
            approval_reason="Ack accepted",
        ))

        assert result["status"] == "ok"
        assert result["listing"]["status"] == "approved"


# --------------------------------------------------------------------------- #
# Tests: reject endpoint
# --------------------------------------------------------------------------- #

class TestRejectEndpoint:

    def test_reject_draft_listing(self, draft_with_appraisal):
        """Rejecting a draft listing advances it to rejected."""
        client, draft = draft_with_appraisal

        result = _run(listings_api.reject_listing(
            draft["id"],
            reason="Not suitable for sale",
        ))

        assert result["status"] == "ok"
        assert result["listing"]["status"] == "rejected"

    def test_reject_priced_listing(self, priced_listing):
        """Rejecting a priced listing works."""
        client, priced = priced_listing

        result = _run(listings_api.reject_listing(
            priced["id"],
            reason="Too expensive",
        ))

        assert result["status"] == "ok"
        assert result["listing"]["status"] == "rejected"

    def test_reject_approved_listing_fails(self, approved_listing):
        """Rejecting an approved listing returns 409."""
        client, approved = approved_listing

        with pytest.raises(listings_api.HTTPException) as exc_info:
            _run(listings_api.reject_listing(
                approved["id"],
                reason="Test",
            ))

        assert exc_info.value.status_code == 409


# --------------------------------------------------------------------------- #
# Tests: publish endpoint
# --------------------------------------------------------------------------- #

class TestPublishEndpoint:

    def test_publish_dry_run(self, approved_listing):
        """Publishing a dry-run doesn't change status, returns preview."""
        client, approved = approved_listing

        result = _run(listings_api.publish_listing(approved["id"], apply=False))

        assert result["status"] == "ok"
        assert result["dry_run"] is True
        assert "provider_result" in result
        # Status should still be approved
        assert result["listing"]["status"] == "approved"

    def test_publish_apply(self, approved_listing):
        """Publishing with apply=true advances to listed."""
        client, approved = approved_listing

        result = _run(listings_api.publish_listing(approved["id"], apply=True))

        assert result["status"] == "ok"
        assert result["dry_run"] is False
        assert result["listing"]["status"] == "listed"

    def test_publish_non_approved_fails(self, priced_listing):
        """Publishing a non-approved listing returns 409."""
        client, priced = priced_listing

        with pytest.raises(listings_api.HTTPException) as exc_info:
            _run(listings_api.publish_listing(priced["id"], apply=True))

        assert exc_info.value.status_code == 409
        assert "must be 'approved'" in exc_info.value.detail


# --------------------------------------------------------------------------- #
# Tests: queue and detail endpoints (read-only, basic smoke)
# --------------------------------------------------------------------------- #

class TestReadEndpoints:

    def test_queue_empty(self, stub):
        """Queue with no listings returns empty groups."""
        result = _run(listings_api.queue())
        assert result["total"] == 0
        assert result["groups"] == []

    def test_queue_with_listings(self, draft_with_appraisal):
        """Queue returns listings grouped by status."""
        client, _ = draft_with_appraisal
        result = _run(listings_api.queue())

        assert result["total"] == 1
        assert len(result["groups"]) == 1
        assert result["groups"][0]["status"] == "draft"
        assert result["groups"][0]["count"] == 1

    def test_get_listing(self, draft_with_appraisal):
        """Get listing returns full details."""
        client, draft = draft_with_appraisal
        result = _run(listings_api.get_listing(draft["id"]))

        assert result["id"] == draft["id"]
        assert result["title"] == "Vintage Omega Seamaster"
        assert result["status"] == "draft"

    def test_get_nonexistent_listing(self, stub):
        """Get nonexistent listing returns 404."""
        with pytest.raises(listings_api.HTTPException) as exc_info:
            _run(listings_api.get_listing("nonexistent"))

        assert exc_info.value.status_code == 404


# --------------------------------------------------------------------------- #
# Tests: health endpoint
# --------------------------------------------------------------------------- #

class TestHealth:

    def test_health_reports_env(self, stub):
        """Health endpoint reports environment status."""
        result = _run(listings_api.health())

        assert result["status"] == "ok"
        assert "supabase_url" in result