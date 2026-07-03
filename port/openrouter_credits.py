#!/usr/bin/env python3
"""
openrouter_credits.py — stdlib-only OpenRouter account-credits reader.

GET https://openrouter.ai/api/v1/credits  ->  {"data": {"total_credits", "total_usage"}}
We return {"total_credits": ..., "total_usage": ..., "remaining": credits - usage}.

Key resolution mirrors fusion.py._key() EXACTLY:
  - env OPENROUTER_API_KEY wins if set (stripped);
  - else ~/.hermes/auth.json -> credential_pool.openrouter[0].access_token.

The meter NEVER breaks a build: every failure path returns None. The key value
is never logged or included in error text (names only, per policy-note.md).
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable, Optional

CREDITS_URL = "https://openrouter.ai/api/v1/credits"
AUTH_JSON = os.path.expanduser("~/.hermes/auth.json")
_TIMEOUT = 15  # seconds; short — this is on the build hot path

# One stderr warning per process when the meter is unreachable, so a flaky
# endpoint does not spam the build log. Set False after the first warning.
_warned = False


def _key() -> Optional[str]:
    """Resolve the OpenRouter key, exactly like fusion.py._key().

    Returns None if the key cannot be resolved (env unset + auth.json
    missing/corrupt/wrong-shape). Never raises.
    """
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k.strip()
    try:
        with open(AUTH_JSON) as f:
            d = json.load(f)
        return d["credential_pool"]["openrouter"][0]["access_token"]
    except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError,
            IndexError, TypeError):
        return None


def _default_transport(url: str, headers: dict, timeout: int) -> bytes:
    """The production transport: a real urllib urlopen."""
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout).read()


def get_credits(transport: Optional[Callable[[str, dict, int], bytes]] = None
                ) -> Optional[dict]:
    """Query OpenRouter account credits. Returns a dict or None.

    Returns: {"total_credits": float, "total_usage": float, "remaining": float}
    on success. ANY failure (no key, network, bad JSON, missing fields) → None.
    Never raises, never logs the key value. Emits at most one stderr warning
    per process so an unreachable meter does not spam the build log.
    """
    global _warned
    key = _key()
    if not key:
        if not _warned:
            import sys
            sys.stderr.write(
                "[openrouter_credits] no API key resolved "
                "(OPENROUTER_API_KEY unset, auth.json unavailable) — "
                "meter disabled, using estimate fallback\n")
            _warned = True
        return None

    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://fsfai.harness",
        "X-Title": "harness-ledger",
    }
    fetch = transport if transport is not None else _default_transport
    try:
        raw = fetch(CREDITS_URL, headers, _TIMEOUT)
    except Exception:
        if not _warned:
            import sys
            sys.stderr.write(
                "[openrouter_credits] credits endpoint unreachable — "
                "meter disabled, using estimate fallback\n")
            _warned = True
        return None

    try:
        body = json.loads(raw) if isinstance(raw, (bytes, bytearray)) else json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    try:
        total_credits = float(data["total_credits"])
        total_usage = float(data["total_usage"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "total_credits": total_credits,
        "total_usage": total_usage,
        "remaining": total_credits - total_usage,
    }


__all__ = ["get_credits", "CREDITS_URL"]
