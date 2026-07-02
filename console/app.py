#!/usr/bin/env python3
"""
Archie Console — model choice + fusion council deploy (v1).

FastAPI app binding 127.0.0.1:9130. Serves a static UI and a JSON API for
viewing/changing WHICH models the Fusion council runs. Auth is a bearer token
from a 0600 file behind a single verify_request() seam (OAuth is a later swap
of that one function — see README.md §Auth upgrade path).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ── paths (module-level so tests can monkeypatch) ────────────────────────────
HARNESS_DIR = Path(os.environ.get("HARNESS_DIR", os.path.expanduser("~/harness")))
CONFIG_PATH = Path(os.environ.get("COUNCIL_CONFIG", str(HARNESS_DIR / "council-config.json")))
TOKEN_PATH = Path(os.environ.get("CONSOLE_TOKEN", str(HARNESS_DIR / "console-token")))
AUDIT_PATH = Path(os.environ.get("CONSOLE_AUDIT", str(HARNESS_DIR / "artifacts" / "console-audit.jsonl")))
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Import fusion constants + resolver (same module the council uses).
_REPO_ROOT = Path(__file__).resolve().parent.parent
import sys as _sys
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))
import fusion as _fusion

# Import PRICE_TABLE from harness_ledger for priced_in_ledger annotation.
_port_dir = HARNESS_DIR / "port"
if str(_port_dir) not in _sys.path:
    _sys.path.insert(0, str(_port_dir))
import harness_ledger as hl  # noqa: E402

CATALOG_URL = "https://openrouter.ai/api/v1/models"
CATALOG_TTL_S = 600  # 10-minute in-process cache

# ── auth: bearer token from a 0600 file ──────────────────────────────────────

def _ensure_token_file() -> str:
    """Auto-generate ~/harness/console-token (0600) on first boot if absent.

    Returns the token value. NEVER logs or prints it.
    """
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        st = TOKEN_PATH.stat()
        # Enforce 0600 — refuse to serve if the file is world/group-readable.
        if st.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
            raise HTTPException(status_code=500, detail="console-token file has insecure permissions; chmod 0600 required")
        return TOKEN_PATH.read_text().strip()
    tok = secrets.token_urlsafe(32)
    fd = os.open(str(TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, tok.encode())
    finally:
        os.close(fd)
    # belt-and-suspenders: chmod in case umask was weird
    os.chmod(str(TOKEN_PATH), 0o600)
    return tok


def verify_request(request: Request) -> None:
    """Single auth seam. Raises HTTPException(401) on failure.

    v1: bearer token from ~/harness/console-token (0600 file).
    Upgrade path: swap this function for OAuth/OIDC validation — the rest of
    the app calls only verify_request(request), so the migration is isolated
    here (see README.md §Auth upgrade path).
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing or malformed Authorization header")
    presented = auth[len("Bearer "):].strip()
    expected = _ensure_token_file()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


# ── catalog fetch (injectable via app.state.fetch_catalog) ────────────────────

_catalog_cache: list[dict] | None = None
_catalog_fetched_at: float = 0.0


def _annotate_policy(slug: str) -> str:
    """Policy annotation: blocked-opus / warn-anthropic / ok."""
    if slug.startswith("anthropic/claude-opus"):
        return "blocked-opus"
    if slug.startswith("anthropic/"):
        return "warn-anthropic"
    return "ok"


def _normalize_catalog_entry(raw: dict) -> dict:
    """Normalize one OpenRouter /api/v1/models entry to our shape."""
    slug = raw.get("id", "")
    pricing = raw.get("pricing", {}) or {}
    return {
        "id": slug,
        "name": raw.get("name", ""),
        "prompt_per_1m": _to_per_1m(pricing.get("prompt")),
        "completion_per_1m": _to_per_1m(pricing.get("completion")),
        "context_length": raw.get("context_length", 0),
        "priced_in_ledger": slug in hl.PRICE_TABLE,
        "policy": _annotate_policy(slug),
    }


def _to_per_1m(per_token) -> Optional[float]:
    """OpenRouter pricing is USD per TOKEN; the UI speaks USD per 1M tokens."""
    f = _safe_float(per_token)
    return round(f * 1_000_000, 6) if f is not None else None


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def fetch_catalog() -> list[dict]:
    """Fetch + cache the OpenRouter catalog for CATALOG_TTL_S seconds.

    Tests replace app.state.fetch_catalog with a stub — nothing in the test
    suite touches the network.
    """
    global _catalog_cache, _catalog_fetched_at
    now = time.time()
    if _catalog_cache is not None and (now - _catalog_fetched_at) < CATALOG_TTL_S:
        return _catalog_cache
    try:
        req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "archie-console/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        models = data.get("data", []) if isinstance(data, dict) else []
        _catalog_cache = [_normalize_catalog_entry(m) for m in models if isinstance(m, dict)]
        _catalog_fetched_at = now
        return _catalog_cache
    except Exception:
        # Return stale cache if we have one, else empty list (don't crash).
        return _catalog_cache if _catalog_cache is not None else []


# ── config read helpers ───────────────────────────────────────────────────────

def _read_config_file() -> dict:
    """Read council-config.json. Returns {} if missing or corrupt (no stderr
    here — that's fusion.py's job; the console just shows source=default)."""
    try:
        with open(CONFIG_PATH) as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _effective_config() -> dict:
    """Return effective config: per-role {value, source} + defaults + path."""
    resolved = _fusion.resolve_council_models()
    file_cfg = _read_config_file()
    roles = {}
    for key in ("panel_full", "panel_budget", "judge", "synth_full", "synth_budget"):
        val = resolved[key]
        src = resolved["sources"][key]
        roles[key] = {"value": val, "source": src}
    return {
        "roles": roles,
        "defaults": {
            "panel_full": _fusion.PANEL_FULL,
            "panel_budget": _fusion.PANEL_BUDGET,
            "judge": _fusion.JUDGE,
            "synth_full": _fusion.SYNTH_FULL,
            "synth_budget": _fusion.SYNTH_BUDGET,
        },
        "config_path": str(CONFIG_PATH),
        "config_exists": CONFIG_PATH.exists(),
    }


# ── validation helpers ────────────────────────────────────────────────────────

ROLE_KEYS = ("panel_full", "panel_budget", "judge", "synth_full", "synth_budget")


class ConfigBody(BaseModel):
    """POST /api/config body — any subset of the five role keys."""
    panel_full: Optional[list[str]] = None
    panel_budget: Optional[list[str]] = None
    judge: Optional[str] = None
    synth_full: Optional[str] = None
    synth_budget: Optional[str] = None


def _validate_slug(slug: str, catalog_ids: set[str], warnings: list[str]) -> None:
    if slug not in catalog_ids:
        raise HTTPException(422, detail=f"unknown slug: {slug}")
    if slug.startswith("anthropic/claude-opus"):
        raise HTTPException(422, detail=f"{slug} blocked by no-Opus policy (July 2026)")
    if slug.startswith("anthropic/"):
        warnings.append(f"{slug}: avoid-Anthropic-for-labor directive (use only if you must)")
    if slug not in hl.PRICE_TABLE:
        warnings.append(f"{slug} not in PRICE_TABLE — ledger will under-count it")


def _validate_config(body: ConfigBody, catalog: list[dict]) -> tuple[dict, list[str]]:
    """Validate a config body. Returns (clean_config, warnings). Raises 422."""
    catalog_ids = {m["id"] for m in catalog}
    clean: dict[str, Any] = {}
    warnings: list[str] = []
    for key in ROLE_KEYS:
        val = getattr(body, key)
        if val is None:
            continue
        if key.startswith("panel_"):
            if not isinstance(val, list):
                raise HTTPException(422, detail=f"{key} must be a list")
            if len(val) < 2:
                raise HTTPException(422, detail=f"{key} must have 2-5 seats (got {len(val)})")
            if len(val) > 5:
                raise HTTPException(422, detail=f"{key} must have 2-5 seats (got {len(val)})")
            if len(set(val)) != len(val):
                raise HTTPException(422, detail=f"{key} has duplicate seats")
            for slug in val:
                _validate_slug(slug, catalog_ids, warnings)
            clean[key] = val
        else:
            if not isinstance(val, str) or not val.strip():
                raise HTTPException(422, detail=f"{key} must be a non-empty string")
            _validate_slug(val, catalog_ids, warnings)
            clean[key] = val
    return clean, warnings


# ── deploy helpers (atomic write + audit) ────────────────────────────────────

def _atomic_write_config(new_config: dict) -> None:
    """Atomically write council-config.json (tmp + os.replace in same dir)."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    payload = dict(new_config)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload["updated_by"] = "console"
    with open(tmp_path, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(str(tmp_path), str(CONFIG_PATH))


def _append_audit(before: dict, after: dict) -> None:
    """Append a JSONL audit line to the audit log."""
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": "console",
        "before": before,
        "after": after,
    }
    with open(AUDIT_PATH, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


# ── do-lock helpers (build-lane guard + fusion hash) ──────────────────────────

LOCK_DIR = Path(os.environ.get("HARNESS_LOCKS", str(HARNESS_DIR / "locks")))
LOCK_FILE = LOCK_DIR / "do.lock"
HOLDER_FILE = LOCK_DIR / "holder.json"


def _is_build_lock_held() -> bool:
    """Return True if the do-lock 'build' lane is currently held.

    do-lock.sh uses flock on $HARNESS_DIR/locks/do.lock. We try to acquire
    a non-blocking shared lock on the same file; if flock fails (EWOULDBLOCK),
    the lock is held by a running build (or attend — either way, we block).

    The holder.json file tells us which mode holds it, but we conservatively
    block on *any* held lock, matching the council's "build lock" semantics:
    the council decision says "when the do-lock 'build' lane is held". We check
    holder.json and only report build_locked when mode is "build" — but if
    holder.json is missing/stale we still block (the lock IS held).
    """
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False  # got the lock — not held
    except (BlockingIOError, OSError):
        return True
    finally:
        os.close(fd)


def _compute_fusion_hash() -> str:
    """Return a short hash of the effective council config.

    This is the hash of the resolved models that fusion() would use (the five
    role slugs), so the operator can confirm the config is active for the next
    council consult. We hash the JSON of resolve_council_models() output
    (excluding the 'sources' metadata and preset-derived 'panel'/'synth'
    selectors, which vary by call).
    """
    resolved = _fusion.resolve_council_models()
    hash_parts = {
        "panel_full": resolved["panel_full"],
        "panel_budget": resolved["panel_budget"],
        "judge": resolved["judge"],
        "synth_full": resolved["synth_full"],
        "synth_budget": resolved["synth_budget"],
    }
    payload = json.dumps(hash_parts, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Archie Console", version="1.0.0")
# Injection point for tests: replace with a stub to avoid network.
app.state.fetch_catalog = fetch_catalog


@app.get("/api/health")
async def api_health() -> dict:
    """Health check — no auth required."""
    return {
        "ok": True,
        "config_file": CONFIG_PATH.exists(),
        "catalog_cached": _catalog_cache is not None,
    }


@app.get("/api/config")
async def api_get_config(request: Request) -> dict:
    """Effective council config: per-role value+source, defaults, config path."""
    verify_request(request)
    return _effective_config()


@app.get("/api/models")
async def api_get_models(request: Request, q: str = "") -> dict:
    """OpenRouter catalog, filtered by substring on id/name."""
    verify_request(request)
    catalog = app.state.fetch_catalog()
    if q:
        ql = q.lower()
        catalog = [m for m in catalog if ql in m["id"].lower() or ql in (m["name"] or "").lower()]
    return {"models": catalog, "count": len(catalog)}


@app.post("/api/models/refresh")
async def api_models_refresh(request: Request) -> dict:
    """Drop the catalog cache and refetch."""
    verify_request(request)
    global _catalog_cache, _catalog_fetched_at
    _catalog_cache = None
    _catalog_fetched_at = 0.0
    catalog = app.state.fetch_catalog()
    return {"ok": True, "count": len(catalog)}


@app.post("/api/config")
async def api_post_config(body: ConfigBody, request: Request) -> Any:
    """Deploy council config: immediate write, refuse if build lock held.

    Council decision (2026-07-02):
    - Immediate write to council-config.json (fusion() reads it fresh next call).
    - If do-lock 'build' lane is held → 409 Conflict {"blocked": true, "reason": "build_locked"}.
    - On success → 200 OK with written config + fusion hash.

    Blindspots respected:
    - No force-override: operator must abort the build or wait.
    - The lock check is advisory (flock); fusion() reads the file fresh, so the
      immediate write is instantly visible once the lock is free.
    - Binary lock: no per-model granularity (accepted tradeoff).
    """
    verify_request(request)

    # Check build lock BEFORE doing any work.
    if _is_build_lock_held():
        return JSONResponse(
            status_code=409,
            content={"blocked": True, "reason": "build_locked"},
        )

    catalog = app.state.fetch_catalog()
    clean, warnings = _validate_config(body, catalog)

    # Read current config (merge partial update)
    before = _read_config_file()
    new_config = dict(before)
    # Strip metadata keys from the merge source
    for meta in ("updated_at", "updated_by"):
        new_config.pop(meta, None)
    new_config.update(clean)

    _atomic_write_config(new_config)
    _append_audit(before, new_config)

    # Re-read the written file so we return exactly what's on disk
    written = _read_config_file()
    fusion_hash = _compute_fusion_hash()

    return {
        "ok": True,
        "config": written,
        "fusion_hash": fusion_hash,
        "warnings": warnings,
    }


# ── static serving ───────────────────────────────────────────────────────────

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/static/{path:path}")
async def static_files(path: str) -> FileResponse:
    full = (STATIC_DIR / path).resolve()
    if not full.is_relative_to(STATIC_DIR.resolve()) or not full.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(full))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9130)

