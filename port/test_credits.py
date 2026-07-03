#!/usr/bin/env python3
"""
test_credits.py — tests for port/openrouter_credits.py.

No network: the transport is a stub callable. Key precedence (env over
auth.json) is tested with a tmp auth.json and env manipulation.

Run:  python3 -m pytest port/test_credits.py -q
  or: python3 port/test_credits.py   (script mode)
"""
import json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(__file__))
import openrouter_credits as oc

import pytest


def _make_transport(total_credits, total_usage):
    body = json.dumps({"data": {"total_credits": total_credits,
                                "total_usage": total_usage}}).encode()
    return lambda url, headers, timeout: body


@pytest.fixture(autouse=True)
def _suppress_warning():
    """Suppress the one-shot stderr warning during tests."""
    oc._warned = True
    yield
    oc._warned = True  # leave it suppressed for subsequent tests


@pytest.fixture(autouse=True)
def _pin_api_key(monkeypatch):
    """Key resolution runs BEFORE the stubbed transport; without a key
    get_credits returns None and every parsing test silently degrades.
    These passed on the droplet only because ~/.hermes/auth.json exists
    there — pin a dummy env key so they mean the same thing in CI."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "TEST_DUMMY_KEY")


# ── parsing + remaining math ────────────────────────────────────────────────

def test_parsing_and_remaining():
    res = oc.get_credits(transport=_make_transport(100.0, 30.0))
    assert res["remaining"] == pytest.approx(70.0)
    assert res["total_credits"] == pytest.approx(100.0)
    assert res["total_usage"] == pytest.approx(30.0)

def test_parsing_fractional():
    res = oc.get_credits(transport=_make_transport(50.5, 10.25))
    assert res["remaining"] == pytest.approx(40.25)

def test_zero_usage():
    res = oc.get_credits(transport=_make_transport(25.0, 0.0))
    assert res["remaining"] == pytest.approx(25.0)


# ── None on failure (every path) ───────────────────────────────────────────

def test_network_error_returns_none():
    def boom(url, headers, timeout):
        raise ConnectionError("network down")
    assert oc.get_credits(transport=boom) is None

def test_non_json_returns_none():
    assert oc.get_credits(transport=lambda u, h, t: b"not json at all") is None

def test_missing_data_key_returns_none():
    assert oc.get_credits(transport=lambda u, h, t: json.dumps({"foo": 1}).encode()) is None

def test_data_not_dict_returns_none():
    assert oc.get_credits(transport=lambda u, h, t: json.dumps({"data": [1, 2]}).encode()) is None

def test_missing_total_credits_returns_none():
    body = json.dumps({"data": {"total_usage": 5.0}}).encode()
    assert oc.get_credits(transport=lambda u, h, t: body) is None

def test_non_numeric_total_credits_returns_none():
    body = json.dumps({"data": {"total_credits": "abc", "total_usage": 5.0}}).encode()
    assert oc.get_credits(transport=lambda u, h, t: body) is None


# ── key precedence: env over auth.json ─────────────────────────────────────

def test_key_precedence_env_over_authjson(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"credential_pool": {"openrouter": [
        {"access_token": "KEY_FROM_AUTH_JSON"}]}}))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(oc, "AUTH_JSON", str(auth_path))

    seen = []
    def capture(url, headers, timeout):
        seen.append(headers.get("Authorization", ""))
        return json.dumps({"data": {"total_credits": 10.0, "total_usage": 1.0}}).encode()

    # env NOT set -> auth.json key used
    oc.get_credits(transport=capture)
    assert seen[-1] == "Bearer KEY_FROM_AUTH_JSON"

    # env SET -> env key wins
    monkeypatch.setenv("OPENROUTER_API_KEY", "KEY_FROM_ENV")
    oc.get_credits(transport=capture)
    assert seen[-1] == "Bearer KEY_FROM_ENV"

    # env key stripped (whitespace)
    monkeypatch.setenv("OPENROUTER_API_KEY", "  KEY_FROM_ENV_SPACED  ")
    oc.get_credits(transport=capture)
    assert seen[-1] == "Bearer KEY_FROM_ENV_SPACED"


def test_no_key_at_all_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(oc, "AUTH_JSON", str(tmp_path / "nonexistent.json"))
    oc._warned = True
    assert oc.get_credits() is None


# ── script-mode runner (for `python3 port/test_credits.py`) ────────────────

if __name__ == "__main__":
    # Re-run via pytest so the script invocation matches the CI invocation.
    import subprocess
    rc = subprocess.call([sys.executable, "-m", "pytest",
                          os.path.abspath(__file__), "-q"])
    sys.exit(rc)
