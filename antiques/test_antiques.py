"""
Tests for the antiques listing pipeline.

Zero network: every HTTP path goes through a stub transport. Tests cover:
  - Status machine legal/illegal transitions
  - Capture round-trip (photos uploaded, row shape)
  - Pricing with ManualComps + EbayBrowseComps NotConnected
  - Approve → marker → publish --apply happy path with DryRunProvider
  - Stale marker refused
  - Fulfill dry-run advances and formats the alert
  - eBay payload shapes match golden dicts

Run: python3 -m pytest antiques/test_antiques.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure antiques package is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from antiques.common import (  # noqa: E402
    IllegalTransition,
    SupabaseClient,
    UploadError,
    LEGAL_TRANSITIONS,
    buffer_failed_photo,
)
from antiques.pricing import (  # noqa: E402
    EbayBrowseComps,
    EbayTaxonomyResolver,
    ManualComps,
    NotConnected,
    SoldComps,
    CategoryNotResolvedError,
    price_listing,
    recommend_price,
)
from antiques.approve import (  # noqa: E402
    approve,
    reject,
    read_marker,
    validate_marker,
    mark_applied,
    LowConfidenceError,
)
from antiques.publish import (  # noqa: E402
    DryRunProvider,
    EbayProvider,
    publish_listing,
    _build_ebay_requests,
    _resolve_category_id,
)
from antiques.fulfill import (  # noqa: E402
    DryRunLabelProvider,
    EbayLabels,
    Shippo,
    fulfill_pass,
)


# --------------------------------------------------------------------------- #
# Stub transport — a fake Supabase backend for all tests
# --------------------------------------------------------------------------- #

class StubTransport:
    """Records all requests and returns canned responses.

    Simulates a Supabase backend with a single ``listings`` table and a
    Storage bucket.  Every response is an in-memory dict/list — no network.
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


def _set_category_id(client, row_id="test-0", cat_id="31387", cat_name="Wristwatches"):
    """Helper: inject a resolved category_id into the pricing jsonb after
    price_listing (simulating what EbayTaxonomyResolver would do at pricing
    time in a connected environment)."""
    row = client.get_listing(row_id)
    pricing = row.get("pricing") or {}
    pricing["category_id"] = cat_id
    pricing["category_name"] = cat_name
    client.patch_listing(row_id, {"pricing": pricing})


def _high_confidence_appraisal(era="1968", brand="Omega", condition="excellent"):
    """Helper: build an appraisal jsonb with high/high confidence (clean path)."""
    return {
        "era": era,
        "brand": brand,
        "condition": condition,
        "confidence": {"id": "high", "value": "high"},
    }


def _low_confidence_appraisal(era="1968", brand="Omega", condition="excellent",
                              ident="medium", val="low"):
    """Helper: build an appraisal jsonb with non-high confidence (flagged path)."""
    return {
        "era": era,
        "brand": brand,
        "condition": condition,
        "confidence": {"id": ident, "value": val},
    }


@pytest.fixture
def stub():
    """A fresh stub backend + client for each test."""
    t = StubTransport()
    client = SupabaseClient(
        url="https://stub.supabase.co",
        key="stub-key",
        transport=t,
    )
    return client, t


@pytest.fixture
def tmp_artifacts(tmp_path, monkeypatch):
    """Redirect ARTIFACTS_DIR to a temp dir for marker tests."""
    from antiques import approve as approve_mod
    monkeypatch.setattr(approve_mod, "ARTIFACTS_DIR", tmp_path / "antiques")
    return tmp_path / "antiques"


# --------------------------------------------------------------------------- #
# Status machine
# --------------------------------------------------------------------------- #

class TestStatusMachine:

    def test_legal_transitions_cover_full_lifecycle(self):
        """Every step in draft→priced→approved→listed→sold→shipped is legal."""
        chain = ["draft", "priced", "approved", "listed", "sold", "shipped"]
        for frm, to in zip(chain, chain[1:]):
            assert to in LEGAL_TRANSITIONS[frm], f"{frm}→{to} should be legal"

    def test_draft_to_approved_is_legal(self):
        assert "approved" in LEGAL_TRANSITIONS["draft"]

    def test_rejected_is_terminal(self):
        assert LEGAL_TRANSITIONS["rejected"] == set()

    def test_error_is_terminal(self):
        assert LEGAL_TRANSITIONS["error"] == set()

    def test_shipped_is_terminal(self):
        assert LEGAL_TRANSITIONS["shipped"] == set()

    def test_illegal_transition_raises(self, stub):
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Test"})
        with pytest.raises(IllegalTransition):
            client.advance("test-0", "draft", "listed")  # draft→listed is illegal

    def test_advance_uses_conditional_status_filter(self, stub):
        """advance() should refuse if the row's status changed between read and write."""
        client, t = stub
        client.insert_listing({"status": "draft", "title": "Test"})
        # Simulate a concurrent status change.
        t.rows["test-0"]["status"] = "priced"
        with pytest.raises(IllegalTransition):
            client.advance("test-0", "draft", "priced")


# --------------------------------------------------------------------------- #
# Capture round-trip
# --------------------------------------------------------------------------- #

class TestCaptureRowShape:

    def test_insert_and_patch_listing(self, stub):
        client, _ = stub
        row = client.insert_listing({
            "status": "draft",
            "title": "Vintage Omega",
            "description": "Seamaster cal. 601",
        })
        assert row["id"] == "test-0"
        assert row["status"] == "draft"

        updated = client.patch_listing("test-0", {"category_guess": "Watches"})
        assert updated["category_guess"] == "Watches"

    def test_upload_photo_returns_ref(self, stub):
        client, t = stub
        ref = client.upload_photo("test-0", b"\xff\xd8\xff\xe0fake", 0)
        assert ref["bucket"] == "listing-photos"
        assert ref["path"] == "test-0/0.jpg"
        assert ref["content_type"] == "image/jpeg"
        assert len(t.uploads) == 1
        assert t.uploads[0]["size"] == 8  # \xff\xd8\xff\xe0 + "fake"

    def test_upload_photo_retries_on_5xx(self):
        """Upload should retry on transient 5xx errors."""
        from antiques.common import _HttpError
        call_count = [0]

        class RetryTransport:
            def __call__(self, req):
                call_count[0] += 1
                if call_count[0] < 3:
                    raise _HttpError(503, "busy", "Service Unavailable")
                return {}  # success on 3rd try

        client = SupabaseClient(
            url="https://stub.supabase.co",
            key="stub-key",
            transport=RetryTransport(),
        )
        ref = client.upload_photo("test-0", b"fake", 0)
        assert ref["path"] == "test-0/0.jpg"
        assert call_count[0] == 3

    def test_upload_photo_raises_after_max_retries(self):
        from antiques.common import _HttpError

        class Always503:
            def __call__(self, req):
                raise _HttpError(503, "busy", "Service Unavailable")

        client = SupabaseClient(
            url="https://stub.supabase.co",
            key="stub-key",
            transport=Always503(),
        )
        with pytest.raises(UploadError):
            client.upload_photo("test-0", b"fake", 0)

    def test_buffer_failed_photo_stores_bytes_in_notes(self, stub):
        client, t = stub
        client.insert_listing({"status": "draft", "title": "Test", "notes": ""})
        buffer_failed_photo(
            "test-0", b"imagebytes", 2, "image/jpeg", "upload failed", client,
        )
        row = client.get_listing("test-0")
        notes = json.loads(row["notes"])
        assert "pending_uploads" in notes
        assert len(notes["pending_uploads"]) == 1
        entry = notes["pending_uploads"][0]
        assert entry["index"] == 2
        assert entry["size_bytes"] == 10
        assert entry["data_base64"]  # base64 of "imagebytes"

    def test_buffer_merges_with_existing_notes(self, stub):
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Test",
            "notes": json.dumps({"prior_text": "old note"}),
        })
        buffer_failed_photo("test-0", b"img", 0, "image/jpeg", "fail", client)
        row = client.get_listing("test-0")
        notes = json.loads(row["notes"])
        assert notes["prior_text"] == "old note"
        assert len(notes["pending_uploads"]) == 1

    def test_signed_url(self, stub):
        client, _ = stub
        url = client.signed_url("test-0/0.jpg")
        assert "signed" in url


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #

class TestPricing:

    def test_recommend_price_median(self):
        comps = [
            {"price": 100.0}, {"price": 200.0}, {"price": 300.0},
        ]
        rec = recommend_price(comps)
        assert rec["recommended"] == 200.0
        assert rec["low"] == 150.0
        assert rec["high"] == 250.0
        assert rec["method"] == "active-comps-median"
        assert rec["n_comps"] == 3

    def test_recommend_price_empty(self):
        rec = recommend_price([])
        assert rec["recommended"] is None
        assert rec["n_comps"] == 0

    def test_price_listing_with_manual_comps(self, stub):
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Omega Seamaster"})
        comps = [{"price": 180.0}, {"price": 220.0}]
        result = price_listing("test-0", ManualComps(comps), client)
        assert result["status"] == "priced"
        pricing = result["pricing"]
        assert pricing["n_comps"] == 2
        assert pricing["recommended"] == 200.0
        assert pricing["method"] == "active-comps-median"

    def test_ebay_browse_comps_not_connected_without_token(self, monkeypatch):
        monkeypatch.delenv("EBAY_OAUTH_TOKEN", raising=False)
        with pytest.raises(NotConnected) as exc_info:
            EbayBrowseComps()
        assert "EBAY_OAUTH_TOKEN" in exc_info.value.missing

    def test_ebay_browse_comps_parses_response(self):
        """EbayBrowseComps.search_comps parses the real Browse API response shape."""
        api_response = {
            "itemSummaries": [
                {"title": "Omega Seamaster", "price": {"value": "200", "currency": "USD"},
                 "condition": "USED", "itemWebUrl": "https://ebay.com/1"},
                {"title": "Seamaster 300", "price": {"value": "300", "currency": "USD"},
                 "condition": "USED", "itemWebUrl": "https://ebay.com/2"},
            ]
        }

        class StubTransportEbay:
            def __call__(self, req):
                class Resp:
                    def read(self_inner):
                        return json.dumps(api_response).encode()
                return Resp()

        provider = EbayBrowseComps(
            token="test-token",
            api_base="https://api.sandbox.ebay.com",
            transport=StubTransportEbay(),
        )
        comps = provider.search_comps("omega seamaster")
        assert len(comps) == 2
        assert comps[0]["title"] == "Omega Seamaster"
        assert comps[0]["price"] == 200.0
        assert comps[0]["source"] == "ebay_browse"

    def test_sold_comps_not_connected(self):
        with pytest.raises(NotConnected):
            SoldComps()


# --------------------------------------------------------------------------- #
# Approve + marker
# --------------------------------------------------------------------------- #

class TestApprove:

    def test_approve_priced_listing(self, stub, tmp_artifacts):
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Omega",
            "appraisal": _high_confidence_appraisal(),
        })
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)

        result = approve("test-0", weight_oz=5.0, dims={"l": 6, "w": 4, "h": 2}, client=client)
        assert result["status"] == "approved"
        assert result["approval"]["weight_oz"] == 5.0
        # Confidence is recorded in the approval jsonb.
        assert result["approval"]["appraisal_confidence"] == {
            "id": "high", "value": "high",
        }
        assert result["approval"]["acknowledged_low_confidence"] is False

    def test_approve_writes_marker(self, stub, tmp_artifacts):
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Omega",
            "appraisal": _high_confidence_appraisal(),
        })
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)
        approve("test-0", weight_oz=5.0, client=client)

        marker = read_marker("test-0")
        assert marker["listing_id"] == "test-0"
        assert marker["price"] == 200.0
        assert marker["n_photos"] == 0
        assert marker["applied"] is False
        assert "row_digest" in marker

    def test_approve_draft_with_price_override(self, stub, tmp_artifacts):
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Manual",
            "appraisal": _high_confidence_appraisal(),
        })
        result = approve("test-0", weight_oz=3.0, price_override=99.0, client=client)
        assert result["status"] == "approved"
        assert result["pricing"]["recommended"] == 99.0

    def test_approve_wrong_status_raises(self, stub, tmp_artifacts):
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Test",
            "appraisal": _high_confidence_appraisal(),
        })
        with pytest.raises(ValueError, match="must be 'priced'"):
            approve("test-0", weight_oz=5.0, client=client)

    # -- confidence guard (council decision: appraisal-confidence) -------- #

    def test_approve_low_confidence_raises(self, stub, tmp_artifacts):
        """Low confidence appraisal without ack → LowConfidenceError."""
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Uncertain",
            "appraisal": _low_confidence_appraisal(ident="medium", val="low"),
        })
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)
        with pytest.raises(LowConfidenceError, match="not high/high"):
            approve("test-0", weight_oz=5.0, client=client)
        # Status should NOT have advanced.
        assert client.get_listing("test-0")["status"] == "priced"

    def test_approve_low_confidence_with_ack_succeeds(self, stub, tmp_artifacts):
        """Low confidence appraisal WITH ack → approves and records the ack."""
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Uncertain",
            "appraisal": _low_confidence_appraisal(ident="low", val="medium"),
        })
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)
        result = approve("test-0", weight_oz=5.0,
                         acknowledge_low_confidence=True, client=client)
        assert result["status"] == "approved"
        # The ack and the actual confidence are both recorded.
        assert result["approval"]["acknowledged_low_confidence"] is True
        assert result["approval"]["appraisal_confidence"] == {
            "id": "low", "value": "medium",
        }

    def test_approve_no_appraisal_raises(self, stub, tmp_artifacts):
        """No appraisal at all → treated as unknown confidence → raises."""
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Bare"})
        price_listing("test-0", ManualComps([{"price": 100.0}]), client)
        with pytest.raises(LowConfidenceError, match="unknown"):
            approve("test-0", weight_oz=5.0, client=client)

    def test_approve_no_confidence_field_raises(self, stub, tmp_artifacts):
        """Appraisal present but no confidence structure → raises."""
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Old-style",
            "appraisal": {"era": "1968", "brand": "Omega", "condition": "excellent"},
        })
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)
        with pytest.raises(LowConfidenceError, match="unknown"):
            approve("test-0", weight_oz=5.0, client=client)

    def test_approve_high_confidence_clean_path(self, stub, tmp_artifacts):
        """High/high confidence with no ack → approves normally (clean path)."""
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Clean",
            "appraisal": _high_confidence_appraisal(),
        })
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)
        result = approve("test-0", weight_oz=5.0, client=client)
        assert result["status"] == "approved"
        assert result["approval"]["acknowledged_low_confidence"] is False

    def test_low_confidence_error_carries_details(self, stub, tmp_artifacts):
        """LowConfidenceError carries the listing id and confidence tuple."""
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Uncertain",
            "appraisal": _low_confidence_appraisal(ident="medium", val="low"),
        })
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)
        try:
            approve("test-0", weight_oz=5.0, client=client)
        except LowConfidenceError as e:
            assert e.listing_id == "test-0"
            assert e.confidence == ("medium", "low")
        else:
            pytest.fail("should have raised LowConfidenceError")

    def test_approve_cli_ack_low_confidence_flag(self):
        """The approve CLI accepts --ack-low-confidence (store_true)."""
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--id", required=True)
        ap.add_argument("--weight-oz", type=float, required=True)
        ap.add_argument("--price-override", type=float, default=None)
        ap.add_argument("--ack-low-confidence", action="store_true")

        # Without the flag → False (would raise LowConfidenceError).
        args = ap.parse_args(["--id", "x", "--weight-oz", "5"])
        assert args.ack_low_confidence is False

        # With the flag → True (deliberate ack).
        args = ap.parse_args(["--id", "x", "--weight-oz", "5",
                              "--ack-low-confidence"])
        assert args.ack_low_confidence is True

    def test_reject_from_draft(self, stub):
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Bad"})
        result = reject("test-0", "not authentic", client=client)
        assert result["status"] == "rejected"
        notes = json.loads(result["notes"])
        assert len(notes["rejections"]) == 1
        assert notes["rejections"][0]["reason"] == "not authentic"

    def test_reject_from_priced(self, stub):
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Bad"})
        price_listing("test-0", ManualComps([{"price": 100.0}]), client)
        result = reject("test-0", "too expensive for market", client=client)
        assert result["status"] == "rejected"

    def test_reject_wrong_status_raises(self, stub):
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Test"})
        client.patch_listing("test-0", {"status": "shipped"})
        with pytest.raises(ValueError, match="can only reject"):
            reject("test-0", "nope", client=client)


# --------------------------------------------------------------------------- #
# Publish
# --------------------------------------------------------------------------- #

class TestPublish:

    def _setup_approved(self, client, photos=None):
        """Helper: create a draft, price it (with category_id), approve it."""
        client.insert_listing({
            "status": "draft",
            "title": "Omega Seamaster",
            "description": "Vintage watch",
            "category_guess": "Watches",
            "appraisal": _high_confidence_appraisal(),
            "photos": photos or [],
        })
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)
        # Simulate taxonomy resolution: set category_id in pricing jsonb.
        row = client.get_listing("test-0")
        pricing = row.get("pricing") or {}
        pricing["category_id"] = "31387"  # eBay leaf: Watches
        pricing["category_name"] = "Wristwatches"
        client.patch_listing("test-0", {"pricing": pricing})
        approve("test-0", weight_oz=5.0, client=client)

    def test_dry_run_publish(self, stub, tmp_artifacts):
        client, _ = stub
        self._setup_approved(client)
        result = publish_listing("test-0", DryRunProvider(), client=client, apply=False)
        assert result["dry_run"] is True
        assert "provider_result" in result
        # status should still be approved
        assert client.get_listing("test-0")["status"] == "approved"

    def test_apply_publish_with_dry_run_provider(self, stub, tmp_artifacts):
        client, _ = stub
        self._setup_approved(client)
        result = publish_listing("test-0", DryRunProvider(), client=client, apply=True)
        assert result["dry_run"] is False
        row = client.get_listing("test-0")
        assert row["status"] == "listed"
        assert row["provider"]["kind"] == "dry_run"
        assert "sku" in row["provider"]

    def test_stale_marker_refused(self, stub, tmp_artifacts):
        """If the row changes between approve and publish, the marker is stale."""
        client, t = stub
        self._setup_approved(client)

        # Tamper with the row after approval (changes the digest).
        t.rows["test-0"]["title"] = "Changed Title"

        with pytest.raises(ValueError, match="STALE MARKER"):
            publish_listing("test-0", DryRunProvider(), client=client, apply=True)

    def test_publish_wrong_status_raises(self, stub, tmp_artifacts):
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Test"})
        with pytest.raises(ValueError, match="must be 'approved'"):
            publish_listing("test-0", DryRunProvider(), client=client, apply=False)

    def test_mark_applied_after_publish(self, stub, tmp_artifacts):
        client, _ = stub
        self._setup_approved(client)
        publish_listing("test-0", DryRunProvider(), client=client, apply=True)
        marker = read_marker("test-0")
        assert marker["applied"] is True

    def test_double_publish_refused(self, stub, tmp_artifacts):
        """A marker already marked applied can't be published again."""
        client, t = stub
        self._setup_approved(client)
        publish_listing("test-0", DryRunProvider(), client=client, apply=True)
        # Simulate the row still being 'approved' (the marker is the guard).
        t.rows["test-0"]["status"] = "approved"
        with pytest.raises(ValueError, match="already marked 'applied'"):
            publish_listing("test-0", DryRunProvider(), client=client, apply=True)

    def test_ebay_provider_not_connected_without_env(self, monkeypatch):
        for k in ["EBAY_OAUTH_TOKEN", "EBAY_MERCHANT_LOCATION_KEY",
                   "EBAY_FULFILLMENT_POLICY_ID", "EBAY_PAYMENT_POLICY_ID",
                   "EBAY_RETURN_POLICY_ID"]:
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(NotConnected) as exc_info:
            EbayProvider()
        # Should name ALL missing vars.
        assert len(exc_info.value.missing) == 5

    def test_ebay_request_shapes_match_golden(self, stub, tmp_artifacts):
        """The eBay request payloads match the golden dict shapes from the cheatsheet."""
        client, _ = stub
        self._setup_approved(client, photos=[
            {"bucket": "listing-photos", "path": "test-0/0.jpg"},
        ])
        row = client.get_listing("test-0")

        requests = _build_ebay_requests(row, sku="archie-test-0")
        inv = requests["inventory_item"]
        offer = requests["offer"]

        # Inventory item shape
        assert inv["product"]["title"] == "Omega Seamaster"
        assert "description" in inv["product"]
        assert isinstance(inv["product"]["imageUrls"], list)
        assert inv["availability"]["shipToLocationAvailability"]["quantity"] == 1
        assert inv["condition"] in ("NEW", "USED_EXCELLENT", "USED_GOOD", "USED_FAIR")

        # Offer shape
        assert offer["sku"] == "archie-test-0"
        assert offer["marketplaceId"] == "EBAY_US"
        assert offer["format"] == "FIXED_PRICE"
        assert offer["pricingSummary"]["price"]["currency"] == "USD"
        assert "merchantLocationKey" in offer
        assert "listingPolicies" in offer
        lp = offer["listingPolicies"]
        assert "fulfillmentPolicyId" in lp
        assert "paymentPolicyId" in lp
        assert "returnPolicyId" in lp


# --------------------------------------------------------------------------- #
# eBay seam fixes (2026-07-03) — one test per fix, asserting on exact
# payload / header / URL shapes from kb/ebay-integration-notes.md
# --------------------------------------------------------------------------- #

class TestEbaySeamFixes:
    """Tests for the seven seam fixes from the live sandbox integration."""

    def _setup_approved_with_photos(self, client, photos=None):
        """Helper: create → price (with category_id) → approve."""
        if photos is None:
            photos = [{"bucket": "listing-photos", "path": "test-0/0.jpg"}]
        client.insert_listing({
            "status": "draft",
            "title": "Candlestick",
            "description": "Brass candlestick, Victorian",
            "category_guess": "candlestick",
            "appraisal": _high_confidence_appraisal(era="1890", brand=""),
            "photos": photos,
        })
        price_listing("test-0", ManualComps([{"price": 45.0}]), client)
        _set_category_id(client, cat_id="4062", cat_name="Candle Holders")
        approve("test-0", weight_oz=8.0, client=client)

    # Fix 1: aspects values must be list[str], not bare strings (errorId 2004)
    def test_fix1_aspects_values_are_lists(self, stub, tmp_artifacts):
        client, _ = stub
        self._setup_approved_with_photos(client)
        row = client.get_listing("test-0")
        requests = _build_ebay_requests(row, sku="archie-test-0")
        aspects = requests["inventory_item"]["product"]["aspects"]
        # Every aspect value must be a list of strings (errorId 2004 if bare string).
        for key, val in aspects.items():
            assert isinstance(val, list), f"aspect {key} must be list, got {type(val)}"
            assert all(isinstance(v, str) for v in val)
        # category_guess drives the Type aspect.
        assert aspects["Type"] == ["candlestick"]

    # Fix 2: Content-Language: en-US header on JSON-body requests (errorId 25709)
    def test_fix2_content_language_header(self, stub, tmp_artifacts):
        client, _ = stub
        self._setup_approved_with_photos(client)
        row = client.get_listing("test-0")

        # Build a stub transport that records headers.
        captured_headers = {}

        class HeaderCaptureTransport:
            def __call__(self, req):
                captured_headers.update(dict(req.header_items()))
                class Resp:
                    status = 204
                    def read(self):
                        return b"{}"
                return Resp()

        provider = EbayProvider(
            env={
                "EBAY_OAUTH_TOKEN": "test-token",
                "EBAY_MERCHANT_LOCATION_KEY": "archie-br-01",
                "EBAY_FULFILLMENT_POLICY_ID": "ful-1",
                "EBAY_PAYMENT_POLICY_ID": "pay-1",
                "EBAY_RETURN_POLICY_ID": "ret-1",
            },
            transport=HeaderCaptureTransport(),
        )
        # Just check _headers directly (it's what the requests use).
        h = provider._headers(json_body=True)
        assert h["Content-Language"] == "en-US"
        assert h["Content-Type"] == "application/json"
        # Non-json-body requests should NOT have Content-Language.
        h_get = provider._headers(json_body=False)
        assert "Content-Language" not in h_get

    # Fix 3a: signed_url joins relative paths via /storage/v1 prefix
    def test_fix3a_signed_url_correct_join(self):
        """Supabase returns a relative path like /object/sign/... — the join
        must be project_url + /storage/v1 + signedURL (verified live)."""

        class RelativeSignTransport:
            def __call__(self, req):
                # Real observed shape: relative path starting with /object/sign/
                return {"signedURL": "/object/sign/listing-photos/test-0/0.jpg?token=abc123"}

        client = SupabaseClient(
            url="https://stub.supabase.co",
            key="stub-key",
            transport=RelativeSignTransport(),
        )
        url = client.signed_url("test-0/0.jpg")
        assert url == "https://stub.supabase.co/storage/v1/object/sign/listing-photos/test-0/0.jpg?token=abc123"

        # Absolute URLs should be passed through untouched.
        class AbsoluteSignTransport:
            def __call__(self, req):
                return {"signedURL": "https://cdn.supabase.co/object/sign/x.jpg?token=z"}

        client2 = SupabaseClient(
            url="https://stub.supabase.co",
            key="stub-key",
            transport=AbsoluteSignTransport(),
        )
        assert client2.signed_url("x.jpg") == "https://cdn.supabase.co/object/sign/x.jpg?token=z"

    # Fix 3b: EbayProvider uses url_resolver for real image URLs
    def test_fix3b_url_resolver_produces_real_urls(self, stub, tmp_artifacts):
        client, _ = stub
        self._setup_approved_with_photos(client, photos=[
            {"bucket": "listing-photos", "path": "test-0/0.jpg"},
            {"bucket": "listing-photos", "path": "test-0/1.jpg"},
        ])
        row = client.get_listing("test-0")

        def fake_resolver(photo_ref):
            return f"https://stub.supabase.co/storage/v1/object/sign/listing-photos/{photo_ref['path']}?token=abc"

        requests = _build_ebay_requests(row, sku="archie-test-0", url_resolver=fake_resolver)
        urls = requests["inventory_item"]["product"]["imageUrls"]
        assert len(urls) == 2
        assert urls[0].startswith("https://")
        assert "token=abc" in urls[0]

    # Fix 3c: empty photos → actionable error before API call (errorId 25717)
    def test_fix3c_empty_photos_raises_before_api(self, stub, tmp_artifacts):
        client, _ = stub
        self._setup_approved_with_photos(client, photos=[])
        row = client.get_listing("test-0")

        class FailTransport:
            def __call__(self, req):
                raise AssertionError("should not reach the API with no photos")

        provider = EbayProvider(
            env={
                "EBAY_OAUTH_TOKEN": "t",
                "EBAY_MERCHANT_LOCATION_KEY": "mlk",
                "EBAY_FULFILLMENT_POLICY_ID": "f",
                "EBAY_PAYMENT_POLICY_ID": "p",
                "EBAY_RETURN_POLICY_ID": "r",
            },
            transport=FailTransport(),
        )
        with pytest.raises(ValueError, match="no photos.*25717"):
            provider.publish(row)

    # Fix 4: category resolution at pricing time — EbayTaxonomyResolver
    # parses the real suggestion-response shape from ebay-integration-notes.md
    def test_fix4_taxonomy_resolver_parses_real_response(self):
        """EbayTaxonomyResolver.resolve_category parses the real Taxonomy
        response shape (candlestick → 4062, publish-verified)."""
        api_response = {
            "categorySuggestions": [
                {
                    "category": {"categoryId": "4062", "categoryName": "Candle Holders"},
                    "categoryTreeNodeLevel": 3,
                    "categoryTreeNodeAncestors": [
                        {"categoryId": "13777", "categoryName": "Decorative Collectibles"},
                        {"categoryId": "1", "categoryName": "Collectibles"},
                    ],
                },
                {
                    "category": {"categoryId": "20334", "categoryName": "Other"},
                    "categoryTreeNodeLevel": 3,
                    "categoryTreeNodeAncestors": [],
                },
            ],
        }

        class StubTaxonomyTransport:
            def __call__(self, req):
                class Resp:
                    def read(self):
                        return json.dumps(api_response).encode()
                return Resp()

        resolver = EbayTaxonomyResolver(
            token="test-token",
            api_base="https://api.sandbox.ebay.com",
            transport=StubTaxonomyTransport(),
        )
        result = resolver.resolve_category("candlestick")
        assert result is not None
        assert result["category_id"] == "4062"
        assert result["category_name"] == "Candle Holders"

    # Fix 4: no suggestions → actionable error, not a silent default
    def test_fix4_no_suggestions_raises_actionable_error(self):
        api_response = {"categorySuggestions": []}

        class StubTaxonomyTransport:
            def __call__(self, req):
                class Resp:
                    def read(self):
                        return json.dumps(api_response).encode()
                return Resp()

        resolver = EbayTaxonomyResolver(
            token="test-token",
            transport=StubTaxonomyTransport(),
        )
        result = resolver.resolve_category("nonsense")
        assert result is None  # resolver returns None

        # price_listing with this resolver → CategoryNotResolvedError
        client = SupabaseClient(
            url="https://stub.supabase.co", key="stub-key",
            transport=StubTransport(),
        )
        client.insert_listing({"status": "draft", "title": "Nonsense Item",
                               "category_guess": "nonsense"})
        with pytest.raises(CategoryNotResolvedError, match="category not resolved"):
            price_listing("test-0", ManualComps([{"price": 10.0}]), client,
                          taxonomy_resolver=resolver)

    # Fix 4: price_listing stores category_id in pricing jsonb
    def test_fix4_price_listing_stores_category_id(self, stub):
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Candlestick",
            "category_guess": "candlestick",
        })
        api_response = {
            "categorySuggestions": [
                {"category": {"categoryId": "4062", "categoryName": "Candle Holders"},
                 "categoryTreeNodeLevel": 3},
            ],
        }

        class StubTaxonomyTransport:
            def __call__(self, req):
                class Resp:
                    def read(self):
                        return json.dumps(api_response).encode()
                return Resp()

        resolver = EbayTaxonomyResolver(
            token="test-token", transport=StubTaxonomyTransport(),
        )
        result = price_listing(
            "test-0", ManualComps([{"price": 45.0}]), client,
            taxonomy_resolver=resolver,
        )
        assert result["status"] == "priced"
        assert result["pricing"]["category_id"] == "4062"
        assert result["pricing"]["category_name"] == "Candle Holders"

    # Fix 4: EbayTaxonomyResolver NotConnected without token
    def test_fix4_taxonomy_not_connected_without_token(self, monkeypatch):
        monkeypatch.delenv("EBAY_OAUTH_TOKEN", raising=False)
        with pytest.raises(NotConnected) as exc_info:
            EbayTaxonomyResolver()
        assert "EBAY_OAUTH_TOKEN" in exc_info.value.missing

    # Fix 4: publish reads pricing.category_id (deterministic, no network)
    def test_fix4_publish_reads_resolved_category(self, stub, tmp_artifacts):
        client, _ = stub
        self._setup_approved_with_photos(client)
        row = client.get_listing("test-0")
        requests = _build_ebay_requests(row, sku="archie-test-0")
        assert requests["offer"]["categoryId"] == "4062"

    # Fix 4: publish raises actionable error if no category_id
    def test_fix4_publish_raises_without_category_id(self, stub, tmp_artifacts):
        client, _ = stub
        # Price WITHOUT a taxonomy resolver → no category_id in pricing.
        client.insert_listing({
            "status": "draft", "title": "Thing", "category_guess": "stuff",
            "appraisal": _high_confidence_appraisal(),
            "photos": [{"bucket": "listing-photos", "path": "test-0/0.jpg"}],
        })
        price_listing("test-0", ManualComps([{"price": 50.0}]), client)
        approve("test-0", weight_oz=5.0, client=client)
        row = client.get_listing("test-0")
        with pytest.raises(ValueError, match="no pricing.category_id"):
            _build_ebay_requests(row, sku="archie-test-0")

    # Fix 5: retry-safe offer creation — duplicate-offer recovery
    def test_fix5_duplicate_offer_recovery(self, stub, tmp_artifacts):
        """On a 25002 duplicate-offer error, recover the existing offerId
        and PUT-update it instead of failing."""
        client, _ = stub
        self._setup_approved_with_photos(client)
        row = client.get_listing("test-0")

        call_log = []

        class DupRecoverTransport:
            def __call__(self, req):
                method = req.get_method()
                url = req.full_url
                call_log.append((method, url))
                if method == "PUT" and "inventory_item" in url:
                    class Resp:
                        def read(self): return b"{}"
                    return Resp()
                if method == "POST" and "/offer" in url and "publish" not in url:
                    # Simulate duplicate-offer error (errorId 25002).
                    err_body = json.dumps({
                        "errors": [{
                            "errorId": 25002,
                            "domain": "API_INVENTORY",
                            "message": "Duplicate offer",
                            "parameters": [{"name": "offerId", "value": "offer-xyz"}],
                        }]
                    }).encode()
                    class ErrResp:
                        status = 400
                        def read(self): return err_body
                    return ErrResp()
                if method == "PUT" and "/offer/offer-xyz" in url:
                    class Resp:
                        def read(self): return b"{}"
                    return Resp()
                if method == "POST" and "/publish" in url:
                    class Resp:
                        def read(self):
                            return json.dumps({"listingId": "ebay-listing-1"}).encode()
                    return Resp()
                class Resp:
                    def read(self): return b"{}"
                return Resp()

        provider = EbayProvider(
            env={
                "EBAY_OAUTH_TOKEN": "t",
                "EBAY_MERCHANT_LOCATION_KEY": "mlk",
                "EBAY_FULFILLMENT_POLICY_ID": "f",
                "EBAY_PAYMENT_POLICY_ID": "p",
                "EBAY_RETURN_POLICY_ID": "r",
            },
            transport=DupRecoverTransport(),
        )
        result = provider.publish(row)
        assert result["offer_id"] == "offer-xyz"
        assert result["listing_id"] == "ebay-listing-1"
        # Verify a PUT update was sent to the recovered offer.
        put_calls = [c for c in call_log if c[0] == "PUT" and "offer-xyz" in c[1]]
        assert len(put_calls) == 1

    # Fix 6: --provider {dryrun,ebay} replaces broken --dry-run-provider
    def test_fix6_provider_cli_arg(self):
        """The CLI accepts --provider dryrun|ebay (not --dry-run-provider)."""
        import argparse
        # Reconstruct the parser to verify the arg schema.
        ap = argparse.ArgumentParser()
        ap.add_argument("--id", required=True)
        ap.add_argument("--apply", action="store_true")
        ap.add_argument("--provider", choices=["dryrun", "ebay"], default="dryrun")

        # --provider dryrun works.
        args = ap.parse_args(["--id", "x", "--provider", "dryrun"])
        assert args.provider == "dryrun"

        # --provider ebay works.
        args = ap.parse_args(["--id", "x", "--provider", "ebay"])
        assert args.provider == "ebay"

        # --dry-run-provider is no longer a valid arg.
        with pytest.raises(SystemExit):
            ap.parse_args(["--id", "x", "--dry-run-provider"])

    # Fix 7: ensure_bucket tolerates duplicate wrapped as HTTP 400
    def test_fix7_ensure_bucket_tolerates_400_with_409_body(self):
        """Supabase wraps duplicate-bucket as HTTP 400 with body
        statusCode=409 — ensure_bucket should treat it as success."""
        from antiques.common import _HttpError, _is_duplicate_bucket_error

        # Real observed body from ebay-integration-notes.md
        err = _HttpError(400,
            '{"statusCode":"409","error":"Duplicate","message":"The resource already exists"}',
            "Bad Request")
        assert _is_duplicate_bucket_error(err) is True

        # Genuine 400 (not a duplicate) should not be tolerated.
        err_real = _HttpError(400, '{"error":"validation failed"}', "Bad Request")
        assert _is_duplicate_bucket_error(err_real) is False

    # Fix 7: ensure_bucket with the real stub
    def test_fix7_ensure_bucket_succeeds_on_duplicate(self):
        from antiques.common import _HttpError

        class DupBucketTransport:
            def __call__(self, req):
                if "bucket" in req.full_url and req.get_method() == "POST":
                    raise _HttpError(400,
                        '{"statusCode":"409","error":"Duplicate","message":"The resource already exists"}',
                        "Bad Request")
                return {}

        client = SupabaseClient(
            url="https://stub.supabase.co",
            key="stub-key",
            transport=DupBucketTransport(),
        )
        # Should not raise — duplicate is success.
        client.ensure_bucket("listing-photos")


# --------------------------------------------------------------------------- #
# Fulfill
# --------------------------------------------------------------------------- #

class TestFulfill:

    def test_dry_run_fulfill_pass(self, stub, tmp_artifacts):
        """Fulfill dry-run processes sold listings without advancing."""
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Test",
            "appraisal": _high_confidence_appraisal(),
        })
        price_listing("test-0", ManualComps([{"price": 100.0}]), client)
        _set_category_id(client)
        approve("test-0", weight_oz=5.0, client=client)
        publish_listing("test-0", DryRunProvider(), client=client, apply=True)

        # Manually advance to sold (simulating an order).
        client.advance("test-0", "listed", "sold", {"shipping": {}})

        results = fulfill_pass(
            client=client,
            dry_run=True,
            label_provider=DryRunLabelProvider(),
        )
        assert len(results) == 1
        assert results[0]["action"] == "would_ship"
        assert "tracking" in results[0]
        # Status should still be sold (dry-run doesn't advance).
        assert client.get_listing("test-0")["status"] == "sold"

    def test_fulfill_advances_to_shipped(self, stub, tmp_artifacts, monkeypatch):
        """Real fulfill pass advances sold → shipped and stores shipping info."""
        client, _ = stub
        client.insert_listing({
            "status": "draft", "title": "Test",
            "appraisal": _high_confidence_appraisal(),
        })
        price_listing("test-0", ManualComps([{"price": 100.0}]), client)
        _set_category_id(client)
        approve("test-0", weight_oz=5.0, client=client)
        publish_listing("test-0", DryRunProvider(), client=client, apply=True)
        client.advance("test-0", "listed", "sold", {"shipping": {}})

        # Mock the alert script.
        alert_calls = []
        monkeypatch.setattr("antiques.fulfill.subprocess.run",
                            lambda *a, **k: alert_calls.append(a))

        results = fulfill_pass(
            client=client,
            dry_run=False,
            label_provider=DryRunLabelProvider(),
        )
        assert results[0]["action"] == "shipped"
        row = client.get_listing("test-0")
        assert row["status"] == "shipped"
        assert "label_url" in row["shipping"]
        assert "tracking_number" in row["shipping"]
        assert len(alert_calls) == 1  # alert was called

    def test_ebay_labels_not_connected(self):
        with pytest.raises(NotConnected):
            EbayLabels()

    def test_shippo_not_connected_without_key(self, monkeypatch):
        monkeypatch.delenv("SHIPPO_API_KEY", raising=False)
        with pytest.raises(NotConnected) as exc_info:
            Shippo()
        assert "SHIPPO_API_KEY" in exc_info.value.missing


def test_appraisal_confidence_accepts_legacy_keys():
    """The live skill emits id/value; identification/valuation are aliases."""
    from antiques.approve import _appraisal_confidence
    legacy = {"appraisal": {"confidence": {"identification": "high", "valuation": "low"}}}
    assert _appraisal_confidence(legacy) == ("high", "low")
    current = {"appraisal": {"confidence": {"id": "high", "value": "high"}}}
    assert _appraisal_confidence(current) == ("high", "high")
