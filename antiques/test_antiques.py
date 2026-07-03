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
    ManualComps,
    NotConnected,
    SoldComps,
    price_listing,
    recommend_price,
)
from antiques.approve import (  # noqa: E402
    approve,
    reject,
    read_marker,
    validate_marker,
    mark_applied,
)
from antiques.publish import (  # noqa: E402
    DryRunProvider,
    EbayProvider,
    publish_listing,
    _build_ebay_requests,
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
        client.insert_listing({"status": "draft", "title": "Omega"})
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)

        result = approve("test-0", weight_oz=5.0, dims={"l": 6, "w": 4, "h": 2}, client=client)
        assert result["status"] == "approved"
        assert result["approval"]["weight_oz"] == 5.0

    def test_approve_writes_marker(self, stub, tmp_artifacts):
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Omega"})
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
        client.insert_listing({"status": "draft", "title": "Manual"})
        result = approve("test-0", weight_oz=3.0, price_override=99.0, client=client)
        assert result["status"] == "approved"
        assert result["pricing"]["recommended"] == 99.0

    def test_approve_wrong_status_raises(self, stub, tmp_artifacts):
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Test"})
        with pytest.raises(ValueError, match="must be 'priced'"):
            approve("test-0", weight_oz=5.0, client=client)

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
        """Helper: create a draft, price it, approve it."""
        client.insert_listing({
            "status": "draft",
            "title": "Omega Seamaster",
            "description": "Vintage watch",
            "category_guess": "Watches",
            "appraisal": {"era": "1968", "brand": "Omega", "condition": "excellent"},
            "photos": photos or [],
        })
        price_listing("test-0", ManualComps([{"price": 200.0}]), client)
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
# Fulfill
# --------------------------------------------------------------------------- #

class TestFulfill:

    def test_dry_run_fulfill_pass(self, stub, tmp_artifacts):
        """Fulfill dry-run processes sold listings without advancing."""
        client, _ = stub
        client.insert_listing({"status": "draft", "title": "Test"})
        price_listing("test-0", ManualComps([{"price": 100.0}]), client)
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
        client.insert_listing({"status": "draft", "title": "Test"})
        price_listing("test-0", ManualComps([{"price": 100.0}]), client)
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
