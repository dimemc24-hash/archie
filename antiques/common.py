"""
Stdlib-only Supabase REST client for the antiques listing pipeline.

Runs in the sandbox (Docker default profile: no pip, no host FS) AND on the
host. Uses only urllib for HTTP — every call goes through an injectable
``transport`` function so tests stub the network entirely.

Image persistence model (council decision, checkpoint ``image-persistence``):
  capture-time full upload to the private Storage bucket. On upload failure,
  the raw image bytes are base64-encoded and stored durably in the listing's
  ``notes`` field (as a JSON blob keyed by the failed photo index) BEFORE the
  function returns. This ensures a host-side retry worker can recover the
  image without relying on the ephemeral sandbox filesystem, which disappears
  on container exit. The listing is still created as a draft — a failed photo
  upload degrades to a draft row with a noted gap, never a lost listing.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Sequence

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 30
STORAGE_BUCKET = "listing-photos"

# Status machine — single source of truth.
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"priced", "approved", "rejected", "error"},
    "priced": {"approved", "rejected", "error"},
    "approved": {"listed", "rejected", "error"},
    "listed": {"sold", "error"},
    "sold": {"shipped", "error"},
    # terminal
    "shipped": set(),
    "rejected": set(),
    "error": set(),
}
TERMINAL_STATUSES = {"shipped", "rejected", "error"}

PhotoRef = dict[str, Any]
TransportFn = Callable[[urllib.request.Request], Any]


class IllegalTransition(Exception):
    """Raised when a status jump is not in LEGAL_TRANSITIONS."""

    def __init__(self, frm: str, to: str):
        self.from_status = frm
        self.to_status = to
        super().__init__(f"illegal status transition: {frm} → {to}")


class UploadError(Exception):
    """Raised when a Storage upload fails after all retries."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"upload failed for {path}: {reason}")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def default_transport(req: urllib.request.Request) -> Any:
    """Default urllib transport with SSL context that doesn't require CA bundle
    configuration in the sandbox."""
    ctx = ssl.create_default_context()
    try:
        return urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT, context=ctx)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise _HttpError(e.code, body, e.reason) from None


class _HttpError(Exception):
    def __init__(self, code: int, body: str, reason: str):
        self.code = code
        self.body = body
        self.reason = reason
        super().__init__(f"HTTP {code}: {reason} — {body}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SupabaseClient:
    """REST client for Archie's Supabase (PostgREST + Storage).

    ``url`` and ``key`` default from env ``ARCHIE_SUPABASE_URL`` /
    ``ARCHIE_SUPABASE_SERVICE_KEY``.  ``transport`` is injectable for tests.
    """

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        transport: TransportFn = default_transport,
    ):
        self.url = (url or os.environ.get("ARCHIE_SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("ARCHIE_SUPABASE_SERVICE_KEY", "")
        self.transport = transport
        if not self.url or not self.key:
            raise RuntimeError(
                "ARCHIE_SUPABASE_URL / ARCHIE_SUPABASE_SERVICE_KEY not set "
                "(need both for Supabase REST access)"
            )

    # -- request helpers ----------------------------------------------------

    def _headers(self, *, json_body: bool = False, return_repr: bool = False) -> dict[str, str]:
        h = {
            "apikey": self.key,
            "Authorization": "Bearer " + self.key,
        }
        if return_repr:
            h["Prefer"] = "return=representation"
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self.url + path
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        return self.transport(req)

    @staticmethod
    def _read(resp: Any) -> Any:
        """Read + JSON-decode an HTTP response (urllib HTTPResponse or test stub)."""
        if isinstance(resp, (dict, list)):
            return resp  # test stub returned a parsed object
        raw = resp.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None

    # -- PostgREST (listings table) -----------------------------------------

    def insert_listing(self, row: dict[str, Any]) -> dict[str, Any]:
        """Insert a new listing row, return the created row (with id)."""
        body = json.dumps(row).encode()
        resp = self._request(
            "POST", "/rest/v1/listings",
            body=body,
            headers=self._headers(json_body=True, return_repr=True),
        )
        data = self._read(resp)
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        raise RuntimeError("insert_listing: unexpected response shape")

    def patch_listing(self, row_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Patch a listing row by id, return the updated row."""
        body = json.dumps(patch).encode()
        resp = self._request(
            "PATCH", f"/rest/v1/listings?id=eq.{row_id}",
            body=body,
            headers=self._headers(json_body=True, return_repr=True),
        )
        data = self._read(resp)
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        raise RuntimeError("patch_listing: unexpected response shape")

    def get_listing(self, row_id: str) -> dict[str, Any]:
        resp = self._request(
            "GET", f"/rest/v1/listings?id=eq.{row_id}&limit=1",
            headers=self._headers(),
        )
        data = self._read(resp)
        if isinstance(data, list):
            return data[0] if data else {}
        if isinstance(data, dict):
            return data
        return {}

    def select_listings(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        path = f"/rest/v1/listings?order=created_at.desc&limit={limit}"
        if status:
            path += f"&status=eq.{status}"
        resp = self._request("GET", path, headers=self._headers())
        data = self._read(resp)
        return data if isinstance(data, list) else []

    # -- status machine -----------------------------------------------------

    def advance(
        self,
        row_id: str,
        from_status: str,
        to_status: str,
        patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically transition a listing's status.

        Refuses illegal jumps (see LEGAL_TRANSITIONS). ``patch`` may carry
        additional fields to set alongside the status change. Uses a PostgREST
        ``status=eq.<from>`` filter so the update is conditional: if the row
        is no longer in ``from_status`` (race), zero rows are returned and we
        raise IllegalTransition.
        """
        if to_status not in LEGAL_TRANSITIONS.get(from_status, set()):
            raise IllegalTransition(from_status, to_status)
        full_patch = dict(patch or {})
        full_patch["status"] = to_status
        body = json.dumps(full_patch).encode()
        resp = self._request(
            "PATCH",
            f"/rest/v1/listings?id=eq.{row_id}&status=eq.{from_status}",
            body=body,
            headers=self._headers(json_body=True, return_repr=True),
        )
        data = self._read(resp)
        if isinstance(data, list) and data:
            return data[0]
        # No rows updated — the row's status changed between read and write.
        raise IllegalTransition(from_status, to_status)

    # -- Storage ------------------------------------------------------------

    def ensure_bucket(self, bucket: str = STORAGE_BUCKET) -> None:
        """Create the Storage bucket if it doesn't exist (idempotent)."""
        body = json.dumps({"id": bucket, "name": bucket, "public": False}).encode()
        try:
            self._request(
                "POST", "/storage/v1/bucket",
                body=body,
                headers=self._headers(json_body=True),
            )
        except _HttpError as e:
            # 409 = bucket already exists — not an error.
            if e.code != 409:
                raise

    def upload_photo(
        self,
        listing_id: str,
        photo_bytes: bytes,
        index: int,
        *,
        content_type: str = "image/jpeg",
        bucket: str = STORAGE_BUCKET,
    ) -> PhotoRef:
        """Upload a photo to Storage at ``<listing-id>/<index>.jpg``.

        Returns a photo reference dict:
            {"bucket": ..., "path": ..., "content_type": ..., "uploaded_at": ...}

        On failure, raises UploadError — the caller (capture) is responsible
        for buffering the bytes durably before the sandbox exits.
        """
        ext = _ext_for_content_type(content_type)
        path = f"{listing_id}/{index}{ext}"
        url = f"/storage/v1/object/{bucket}/{path}"
        headers = self._headers()
        headers["Content-Type"] = content_type
        headers["x-upsert"] = "true"
        last_err = ""
        for attempt in range(3):
            try:
                self._request(
                    "PUT", url,
                    body=photo_bytes,
                    headers=headers,
                )
                return {
                    "bucket": bucket,
                    "path": path,
                    "content_type": content_type,
                    "uploaded_at": _now_iso(),
                }
            except _HttpError as e:
                last_err = f"HTTP {e.code}: {e.reason}"
                if e.code in (408, 429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break
            except Exception as e:
                last_err = str(e)
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break
        raise UploadError(path, last_err)

    def signed_url(
        self, path: str, *, expires_in: int = 604800, bucket: str = STORAGE_BUCKET
    ) -> str:
        """Generate a signed URL for a photo in the private bucket."""
        body = json.dumps({"expiresIn": expires_in}).encode()
        resp = self._request(
            "POST", f"/storage/v1/object/sign/{bucket}/{path}",
            body=body,
            headers=self._headers(json_body=True),
        )
        data = self._read(resp)
        if isinstance(data, dict) and "signedURL" in data:
            signed = data["signedURL"]
            if signed.startswith("/"):
                signed = self.url + signed
            return signed
        raise RuntimeError(f"signed_url: unexpected response: {data}")


# ---------------------------------------------------------------------------
# Buffer-on-failure (council decision: never strand image bytes in the sandbox)
# ---------------------------------------------------------------------------

def buffer_failed_photo(
    row_id: str,
    photo_bytes: bytes,
    index: int,
    content_type: str,
    reason: str,
    client: SupabaseClient,
) -> dict[str, Any]:
    """Durably buffer a failed-upload photo's raw bytes into the listing's
    ``notes`` field as a base64 JSON blob.

    This is the safety net mandated by the image-persistence checkpoint: the
    sandbox filesystem is ephemeral and disappears on container exit, so a
    local /tmp fallback is non-viable. Instead, the raw bytes are base64-
    encoded and stored in the listing row's ``notes`` text field (which is
    durable in Postgres) BEFORE capture.py reports the upload_failed status.

    A host-side retry worker can later read the buffer from notes, decode the
    base64, and re-attempt the Storage upload.

    The notes field is JSON (stored as text in Postgres). We merge the buffer
    into any existing notes JSON to avoid clobbering prior notes.
    """
    b64 = base64.b64encode(photo_bytes).decode("ascii")
    buffer_entry = {
        "type": "photo_upload_failed",
        "index": index,
        "content_type": content_type,
        "size_bytes": len(photo_bytes),
        "reason": reason,
        "buffered_at": _now_iso(),
        "data_base64": b64,
    }

    # Read current notes, merge, write back.
    row = client.get_listing(row_id)
    existing_notes = _parse_notes(row.get("notes"))

    if isinstance(existing_notes, dict):
        pending = existing_notes.get("pending_uploads", [])
        if not isinstance(pending, list):
            pending = []
        pending.append(buffer_entry)
        existing_notes["pending_uploads"] = pending
        new_notes = json.dumps(existing_notes)
    elif isinstance(existing_notes, str) and existing_notes.strip():
        # notes was plain text — wrap it.
        new_notes = json.dumps({
            "prior_text": existing_notes,
            "pending_uploads": [buffer_entry],
        })
    else:
        new_notes = json.dumps({"pending_uploads": [buffer_entry]})

    return client.patch_listing(row_id, {"notes": new_notes})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ext_for_content_type(ct: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(ct, ".jpg")


def _parse_notes(notes: Any) -> Any:
    """Try to parse notes as JSON; fall back to the raw string."""
    if notes is None:
        return None
    if isinstance(notes, (dict, list)):
        return notes
    if isinstance(notes, str):
        stripped = notes.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return notes
    return notes


__all__ = [
    "SupabaseClient",
    "IllegalTransition",
    "UploadError",
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATUSES",
    "STORAGE_BUCKET",
    "buffer_failed_photo",
    "default_transport",
]
