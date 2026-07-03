#!/usr/bin/env python3
"""
capture.py — sandbox-side antiques listing capture.

Runs inside the Docker default profile (nikolaik/python-nodejs): NO pip, NO
host filesystem, only ``ARCHIE_SUPABASE_URL`` / ``ARCHIE_SUPABASE_SERVICE_KEY``
in env. Python stdlib ONLY — self-contained (no local imports), same pattern
as queue_spec.py.

What it does (council decision, checkpoint ``capture-boundary``: full capture):
  1. Reads listing fields (title, description, category, appraisal JSON) from
     args or stdin-JSON, plus local photo paths (from the chat turn).
  2. Inserts a draft listing row (gets the listing id first — so photos are
     keyed by the real listing id, not a temp name).
  3. Uploads every photo to the private Supabase Storage bucket at capture
     time (council decision ``image-persistence``: full upload, durable
     immediately — no Telegram file_id dependency, no deferred re-fetch).
  4. On upload failure: base64-encodes the raw image bytes and stores them
     durably in the listing's ``notes`` field BEFORE returning, so a host-side
     retry worker can recover the image after the ephemeral sandbox exits.
     The listing is still created as a draft — a failed photo upload degrades
     to a draft row with a noted gap, never a lost listing.
  5. Patches the listing row with the photo references.
  6. Prints the listing id + a one-line summary for the chat reply.

Usage:
  capture.py --title "..." --description "..." [--category "..."]
      [--appraisal '{...}'] [--caption "..."] [--photo /path/to/photo.jpg ...]
      [--source telegram]

  capture.py --stdin   # read JSON from stdin: {"title":..., "photos":[...], ...}
"""
import argparse
import base64
import json
import mimetypes
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STORAGE_BUCKET = "listing-photos"
DEFAULT_TIMEOUT = 30

# Status machine (mirrors antiques/common.py — the host-side source of truth).
# capture.py only ever creates 'draft' rows; these are for reference/validation.
LEGAL_TRANSITIONS = {
    "draft": {"priced", "approved", "rejected", "error"},
    "priced": {"approved", "rejected", "error"},
    "approved": {"listed", "rejected", "error"},
    "listed": {"sold", "error"},
    "sold": {"shipped", "error"},
    "shipped": set(),
    "rejected": set(),
    "error": set(),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class UploadError(Exception):
    """Raised when a Storage upload fails after all retries."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"upload failed for {path}: {reason}")


class _HttpError(Exception):
    def __init__(self, code: int, body: str, reason: str):
        self.code = code
        self.body = body
        self.reason = reason
        super().__init__(f"HTTP {code}: {reason} — {body}")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _default_transport(req):
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


# ---------------------------------------------------------------------------
# Supabase client (inlined — sandbox has no access to antiques/common.py)
# ---------------------------------------------------------------------------

class SupabaseClient:
    """Minimal Supabase REST client for the sandbox capture flow.

    Only the methods capture.py needs: insert_listing, patch_listing,
    get_listing, ensure_bucket, upload_photo.  The full client (with
    advance/status-machine, signed_url, select_listings) lives in
    antiques/common.py for host-side code.
    """

    def __init__(self, url=None, key=None, transport=_default_transport):
        self.url = (url or os.environ.get("ARCHIE_SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("ARCHIE_SUPABASE_SERVICE_KEY", "")
        self.transport = transport
        if not self.url or not self.key:
            raise RuntimeError(
                "ARCHIE_SUPABASE_URL / ARCHIE_SUPABASE_SERVICE_KEY not set "
                "(need both for Supabase REST access)"
            )

    def _headers(self, *, json_body=False, return_repr=False):
        h = {"apikey": self.key, "Authorization": "Bearer " + self.key}
        if return_repr:
            h["Prefer"] = "return=representation"
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _request(self, method, path, *, body=None, headers=None):
        req = urllib.request.Request(
            self.url + path, data=body, method=method, headers=headers or {}
        )
        return self.transport(req)

    @staticmethod
    def _read(resp):
        if isinstance(resp, (dict, list)):
            return resp
        raw = resp.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None

    def insert_listing(self, row):
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

    def patch_listing(self, row_id, patch):
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

    def get_listing(self, row_id):
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

    def ensure_bucket(self, bucket=STORAGE_BUCKET):
        body = json.dumps({"id": bucket, "name": bucket, "public": False}).encode()
        try:
            self._request(
                "POST", "/storage/v1/bucket",
                body=body,
                headers=self._headers(json_body=True),
            )
        except _HttpError as e:
            if e.code != 409:  # 409 = already exists
                raise

    def upload_photo(self, listing_id, photo_bytes, index, *,
                     content_type="image/jpeg", bucket=STORAGE_BUCKET):
        ext = _ext_for_content_type(content_type)
        path = f"{listing_id}/{index}{ext}"
        url = f"/storage/v1/object/{bucket}/{path}"
        headers = self._headers()
        headers["Content-Type"] = content_type
        headers["x-upsert"] = "true"
        last_err = ""
        for attempt in range(3):
            try:
                self._request("PUT", url, body=photo_bytes, headers=headers)
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


# ---------------------------------------------------------------------------
# Buffer-on-failure (council decision: never strand image bytes in the sandbox)
# ---------------------------------------------------------------------------

def buffer_failed_photo(row_id, photo_bytes, index, content_type, reason, client):
    """Durably buffer a failed-upload photo's raw bytes into the listing's
    ``notes`` field as a base64 JSON blob.

    The sandbox filesystem is ephemeral and disappears on container exit, so a
    local /tmp fallback is non-viable.  The raw bytes are base64-encoded and
    stored in the listing row's ``notes`` text field (durable in Postgres)
    BEFORE capture.py reports the upload_failed status.  A host-side retry
    worker can later read the buffer, decode, and re-attempt the upload.
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

def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ext_for_content_type(ct):
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(ct, ".jpg")


def _parse_notes(notes):
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


def _guess_content_type(path):
    ct, _ = mimetypes.guess_type(path)
    if ct and ct.startswith("image/"):
        return ct
    return "image/jpeg"


# ---------------------------------------------------------------------------
# Main capture flow
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Capture an antiques listing draft (sandbox).",
    )
    ap.add_argument("--title", default=None, help="listing title")
    ap.add_argument("--description", default=None, help="listing description")
    ap.add_argument("--category", default=None, help="category guess")
    ap.add_argument("--appraisal", default=None,
                    help="appraisal JSON string (from archie-visual-appraisal)")
    ap.add_argument("--caption", default=None, help="optional caption / extra notes")
    ap.add_argument("--source", default="telegram", help="capture source")
    ap.add_argument("--photo", action="append", default=[],
                    help="local photo path (repeatable)")
    ap.add_argument("--stdin", action="store_true",
                    help="read all fields as JSON from stdin (overrides flags)")
    ap.add_argument("--json-output", action="store_true",
                    help="emit machine-readable JSON instead of a chat line")
    a = ap.parse_args()

    # -- gather fields ------------------------------------------------------
    if a.stdin:
        raw = sys.stdin.read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            _fail(f"stdin is not valid JSON: {e}", a.json_output)
            return 1
        fields = _extract_fields(data)
    else:
        appraisal = None
        if a.appraisal:
            try:
                appraisal = json.loads(a.appraisal)
            except json.JSONDecodeError as e:
                _fail(f"--appraisal is not valid JSON: {e}", a.json_output)
                return 1
        fields = {
            "title": a.title,
            "description": a.description,
            "category_guess": a.category,
            "appraisal": appraisal,
            "caption": a.caption,
            "source": a.source,
            "photo_paths": a.photo or [],
        }

    if not fields.get("title"):
        _fail("title is required (never invent one — use the appraisal)", a.json_output)
        return 1

    # -- connect to Supabase ------------------------------------------------
    try:
        client = SupabaseClient()
    except RuntimeError as e:
        _fail(str(e), a.json_output)
        return 1

    # -- insert the draft row FIRST (so we have the listing id for photos) --
    row = _build_draft_row(fields)
    try:
        created = client.insert_listing(row)
    except Exception as e:
        _fail(f"failed to insert listing row: {e}", a.json_output)
        return 1

    listing_id = created["id"]

    # -- ensure the storage bucket exists (idempotent) ----------------------
    try:
        client.ensure_bucket()
    except Exception as e:
        sys.stderr.write(f"[capture] WARNING: ensure_bucket failed: {e}\n")

    # -- upload photos (capture-time full upload) ---------------------------
    photo_refs, failures = _upload_photos(
        client, listing_id, fields["photo_paths"],
    )

    # -- handle failures: buffer bytes durably before returning -------------
    for fail in failures:
        if not fail["bytes"]:
            # No bytes to buffer (file not found / read error) — record text gap.
            continue
        try:
            buffer_failed_photo(
                row_id=listing_id,
                photo_bytes=fail["bytes"],
                index=fail["index"],
                content_type=fail["content_type"],
                reason=fail["reason"],
                client=client,
            )
        except Exception as buf_err:
            _emergency_note(client, listing_id, fail, buf_err)

    # -- patch the row with photo refs + status note ------------------------
    patch = {"photos": photo_refs} if photo_refs else {}
    if failures:
        patch["notes"] = _notes_with_gap(created.get("notes"), failures)
    if patch:
        try:
            client.patch_listing(listing_id, patch)
        except Exception as e:
            sys.stderr.write(f"[capture] WARNING: patch_listing failed: {e}\n")

    # -- summary ------------------------------------------------------------
    n_ok = len(photo_refs)
    n_fail = len(failures)
    summary = f"listing {listing_id} — {n_ok} photo(s) uploaded"
    if n_fail:
        summary += f", {n_fail} failed (buffered in notes for retry)"

    if a.json_output:
        print(json.dumps({
            "listing_id": listing_id,
            "status": "upload_failed" if n_fail else "draft",
            "photos_uploaded": n_ok,
            "photos_failed": n_fail,
            "photo_refs": photo_refs,
            "failures": [
                {"index": f["index"], "reason": f["reason"],
                 "size_bytes": len(f["bytes"])}
                for f in failures
            ],
            "summary": summary,
        }))
    else:
        print(f"✅ {summary}")
        if n_fail:
            print(f"   ⚠️  {n_fail} photo(s) failed upload — bytes buffered in "
                  f"listing notes. A retry worker can recover them.")
        print(f"   listing id: {listing_id}")

    return 0


def _upload_photos(client, listing_id, photo_paths):
    """Upload every photo. Returns (photo_refs, failures).

    On failure, the raw bytes are captured in the failures list so the caller
    can buffer them into notes before the sandbox exits.
    """
    refs = []
    failures = []
    for idx, ppath in enumerate(photo_paths):
        p = Path(ppath)
        if not p.is_file():
            failures.append({
                "index": idx, "bytes": b"", "content_type": "image/jpeg",
                "reason": f"file not found: {ppath}",
            })
            continue
        try:
            raw = p.read_bytes()
        except OSError as e:
            failures.append({
                "index": idx, "bytes": b"", "content_type": "image/jpeg",
                "reason": f"read error: {e}",
            })
            continue
        ct = _guess_content_type(ppath)
        try:
            ref = client.upload_photo(listing_id, raw, idx, content_type=ct)
            refs.append(ref)
        except UploadError as e:
            failures.append({
                "index": idx, "bytes": raw, "content_type": ct,
                "reason": e.reason,
            })
        except Exception as e:
            failures.append({
                "index": idx, "bytes": raw, "content_type": ct,
                "reason": f"unexpected: {e}",
            })
    return refs, failures


def _build_draft_row(fields):
    row = {
        "status": "draft",
        "source": fields.get("source") or "telegram",
        "title": fields.get("title"),
        "description": fields.get("description"),
        "category_guess": fields.get("category_guess"),
        "appraisal": fields.get("appraisal"),
        "photos": [],
    }
    caption = fields.get("caption")
    if caption:
        row["notes"] = caption
    return {k: v for k, v in row.items() if v is not None}


def _extract_fields(data):
    appraisal = data.get("appraisal")
    if isinstance(appraisal, str):
        try:
            appraisal = json.loads(appraisal)
        except json.JSONDecodeError:
            appraisal = None
    photos = data.get("photos") or data.get("photo_paths") or []
    if isinstance(photos, str):
        photos = [photos]
    return {
        "title": data.get("title"),
        "description": data.get("description"),
        "category_guess": data.get("category_guess") or data.get("category"),
        "appraisal": appraisal,
        "caption": data.get("caption"),
        "source": data.get("source", "telegram"),
        "photo_paths": list(photos),
    }


def _notes_with_gap(existing_notes, failures):
    """Build a notes string that records the photo gap for cases where bytes
    were empty (file not found / read error). The buffer_failed_photo path
    handles durable storage for non-empty bytes separately."""
    existing = _parse_notes(existing_notes)
    gap_note = f"[capture] {len(failures)} photo(s) failed upload at capture."
    if isinstance(existing, dict):
        notes_list = existing.get("capture_notes", [])
        if not isinstance(notes_list, list):
            notes_list = []
        notes_list.append(gap_note)
        existing["capture_notes"] = notes_list
        return json.dumps(existing)
    if isinstance(existing, str) and existing.strip():
        return existing + "\n" + gap_note
    return gap_note


def _emergency_note(client, listing_id, fail, buf_err):
    """Last-resort: the buffer itself failed. Record a text note so the
    operator knows an image was lost (bytes could not be recovered)."""
    msg = (f"[capture] EMERGENCY: photo {fail['index']} upload failed ({fail['reason']}) "
           f"AND buffer-to-notes failed ({buf_err}). Image bytes may be lost.")
    try:
        row = client.get_listing(listing_id)
        existing = row.get("notes") or ""
        client.patch_listing(listing_id, {"notes": existing + "\n" + msg})
    except Exception:
        pass


def _fail(msg, json_output):
    if json_output:
        print(json.dumps({"error": msg}))
    else:
        print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
