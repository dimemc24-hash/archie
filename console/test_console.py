"""Console tests — NO network. fetch_catalog is stubbed via app.state.

Covers: auth, config read (defaults vs file), validation rejections, warning
paths, atomic deploy (file written + audit appended), fusion.py seam
(override honored, corrupt → defaults + stderr).
"""
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── fixture: console app pointed at tmp paths ────────────────────────────────

STUB_CATALOG = [
    {"id": "z-ai/glm-5.2", "name": "GLM 5.2", "prompt_per_1m": 0.93,
     "completion_per_1m": 3.00, "context_length": 128000, "priced_in_ledger": True, "policy": "ok"},
    {"id": "deepseek/deepseek-v4-pro", "name": "DeepSeek V4 Pro", "prompt_per_1m": 0.43,
     "completion_per_1m": 0.87, "context_length": 64000, "priced_in_ledger": True, "policy": "ok"},
    {"id": "moonshotai/kimi-k2.6", "name": "Kimi K2.6", "prompt_per_1m": 0.55,
     "completion_per_1m": 3.20, "context_length": 200000, "priced_in_ledger": True, "policy": "ok"},
    {"id": "google/gemini-3.5-flash", "name": "Gemini 3.5 Flash", "prompt_per_1m": 1.50,
     "completion_per_1m": 9.00, "context_length": 1000000, "priced_in_ledger": True, "policy": "ok"},
    {"id": "openai/gpt-4o", "name": "GPT-4o", "prompt_per_1m": 2.50,
     "completion_per_1m": 10.00, "context_length": 128000, "priced_in_ledger": True, "policy": "ok"},
    {"id": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5", "prompt_per_1m": 2.00,
     "completion_per_1m": 10.00, "context_length": 200000, "priced_in_ledger": True, "policy": "warn-anthropic"},
    {"id": "anthropic/claude-opus-4.8", "name": "Claude Opus 4.8", "prompt_per_1m": 5.00,
     "completion_per_1m": 25.00, "context_length": 200000, "priced_in_ledger": True, "policy": "blocked-opus"},
    {"id": "openai/gpt-5.5", "name": "GPT-5.5", "prompt_per_1m": 5.00,
     "completion_per_1m": 30.00, "context_length": 256000, "priced_in_ledger": True, "policy": "ok"},
    {"id": "sakana/fugu", "name": "Fugu", "prompt_per_1m": 0.0,
     "completion_per_1m": 0.0, "context_length": 32000, "priced_in_ledger": True, "policy": "ok"},
    {"id": "unpriced/model-x", "name": "Unpriced X", "prompt_per_1m": 1.0,
     "completion_per_1m": 2.0, "context_length": 64000, "priced_in_ledger": False, "policy": "ok"},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with all paths pointed at tmp_path and catalog stubbed."""
    import console.app as appmod
    # Reset module-level state
    appmod._catalog_cache = None
    appmod._catalog_fetched_at = 0.0
    # Patch paths
    monkeypatch.setattr(appmod, "HARNESS_DIR", tmp_path)
    monkeypatch.setattr(appmod, "CONFIG_PATH", tmp_path / "council-config.json")
    monkeypatch.setattr(appmod, "TOKEN_PATH", tmp_path / "console-token")
    monkeypatch.setattr(appmod, "AUDIT_PATH", tmp_path / "console-audit.jsonl")
    monkeypatch.setattr(appmod, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(appmod, "LOCK_FILE", tmp_path / "locks" / "do.lock")
    # Stub catalog — NO network
    monkeypatch.setattr(appmod.app.state, "fetch_catalog", lambda: STUB_CATALOG)
    # Also reset fusion module's config path env
    monkeypatch.setenv("COUNCIL_CONFIG", str(tmp_path / "council-config.json"))
    # Reload fusion so it picks up the env
    import importlib
    importlib.reload(appmod._fusion)
    return TestClient(appmod.app)


def _bearer(client) -> str:
    """Read the auto-generated token (triggering first-boot creation)."""
    import console.app as appmod
    tok = appmod._ensure_token_file()
    return {"Authorization": f"Bearer {tok}"}


# ── auth tests ────────────────────────────────────────────────────────────────

def test_health_no_auth(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["config_file"] is False


def test_config_401_without_token(client):
    r = client.get("/api/config")
    assert r.status_code == 401


def test_config_200_with_token(client):
    r = client.get("/api/config", headers=_bearer(client))
    assert r.status_code == 200
    body = r.json()
    assert "roles" in body
    assert "defaults" in body


def test_models_401_without_token(client):
    r = client.get("/api/models")
    assert r.status_code == 401


# ── config read tests ─────────────────────────────────────────────────────────

def test_config_shows_defaults(client):
    r = client.get("/api/config", headers=_bearer(client))
    body = r.json()
    for role in ("panel_full", "panel_budget", "judge", "synth_full", "synth_budget"):
        assert body["roles"][role]["source"] == "default"
    assert body["defaults"]["judge"] == "google/gemini-3.5-flash"


def test_config_shows_file_values_after_deploy(client):
    # Write a config file directly (simulating a prior deploy)
    import console.app as appmod
    cfg = {"panel_full": ["z-ai/glm-5.2", "deepseek/deepseek-v4-pro"],
           "judge": "moonshotai/kimi-k2.6"}
    appmod._atomic_write_config(cfg)
    r = client.get("/api/config", headers=_bearer(client))
    body = r.json()
    assert body["roles"]["panel_full"]["source"] == "file"
    assert body["roles"]["judge"]["source"] == "file"
    assert body["roles"]["panel_full"]["value"] == ["z-ai/glm-5.2", "deepseek/deepseek-v4-pro"]
    assert body["roles"]["judge"]["value"] == "moonshotai/kimi-k2.6"


# ── validation rejection tests ───────────────────────────────────────────────

def test_post_rejects_unknown_slug(client):
    r = client.post("/api/config", json={"judge": "unknown/model"}, headers=_bearer(client))
    assert r.status_code == 422
    assert "unknown slug" in r.json()["detail"]


def test_post_rejects_1_seat_panel(client):
    r = client.post("/api/config", json={"panel_full": ["z-ai/glm-5.2"]}, headers=_bearer(client))
    assert r.status_code == 422
    assert "2-5 seats" in r.json()["detail"]


def test_post_rejects_6_seat_panel(client):
    panel = ["z-ai/glm-5.2"] * 6
    r = client.post("/api/config", json={"panel_full": panel}, headers=_bearer(client))
    assert r.status_code == 422
    assert "2-5 seats" in r.json()["detail"]


def test_post_rejects_duplicate_seats(client):
    panel = ["z-ai/glm-5.2", "z-ai/glm-5.2", "deepseek/deepseek-v4-pro"]
    r = client.post("/api/config", json={"panel_full": panel}, headers=_bearer(client))
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"]


def test_post_rejects_opus_slug(client):
    r = client.post("/api/config", json={"judge": "anthropic/claude-opus-4.8"}, headers=_bearer(client))
    assert r.status_code == 422
    assert "no-Opus" in r.json()["detail"]


# ── warning path tests ────────────────────────────────────────────────────────

def test_post_warns_on_anthropic_sonnet(client):
    r = client.post("/api/config", json={"judge": "anthropic/claude-sonnet-5"}, headers=_bearer(client))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert any("avoid-Anthropic" in w for w in body["warnings"])


def test_post_warns_on_unpriced_slug(client):
    r = client.post("/api/config", json={"judge": "unpriced/model-x"}, headers=_bearer(client))
    assert r.status_code == 200
    body = r.json()
    assert any("PRICE_TABLE" in w for w in body["warnings"])


# ── atomic write + audit tests ───────────────────────────────────────────────

def test_deploy_atomic_write_and_audit(client, tmp_path):
    import console.app as appmod
    payload = {"judge": "moonshotai/kimi-k2.6"}
    r = client.post("/api/config", json=payload, headers=_bearer(client))
    assert r.status_code == 200
    # File parses
    cfg = json.loads(appmod.CONFIG_PATH.read_text())
    assert cfg["judge"] == "moonshotai/kimi-k2.6"
    assert cfg["updated_by"] == "console"
    assert "updated_at" in cfg
    # No tmp leftovers
    assert not appmod.CONFIG_PATH.with_suffix(".tmp").exists()
    # Audit line appended and parses
    audit_lines = appmod.AUDIT_PATH.read_text().strip().splitlines()
    assert len(audit_lines) >= 1
    entry = json.loads(audit_lines[-1])
    assert entry["actor"] == "console"
    assert "before" in entry and "after" in entry


# ── fusion.py seam tests ──────────────────────────────────────────────────────

def test_fusion_seam_override_honored(tmp_path):
    """With COUNCIL_CONFIG pointing at a written file, fusion's resolved
    models reflect it."""
    import importlib
    cfg_path = tmp_path / "council-config.json"
    cfg_path.write_text(json.dumps({
        "panel_full": ["z-ai/glm-5.2", "deepseek/deepseek-v4-pro"],
        "judge": "moonshotai/kimi-k2.6",
    }))
    os.environ["COUNCIL_CONFIG"] = str(cfg_path)
    # Import fusion fresh
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    import fusion
    importlib.reload(fusion)
    resolved = fusion.resolve_council_models()
    assert resolved["panel"] == ["z-ai/glm-5.2", "deepseek/deepseek-v4-pro"]
    assert resolved["judge"] == "moonshotai/kimi-k2.6"
    assert resolved["sources"]["panel_full"] == "file"
    assert resolved["sources"]["judge"] == "file"
    assert resolved["sources"]["synth_full"] == "default"


def test_fusion_seam_corrupt_file_defaults(tmp_path, capfd):
    """Corrupt JSON → constants + stderr warning."""
    import importlib
    cfg_path = tmp_path / "council-config.json"
    cfg_path.write_text("{ this is not valid json")
    os.environ["COUNCIL_CONFIG"] = str(cfg_path)
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    import fusion
    importlib.reload(fusion)
    resolved = fusion.resolve_council_models()
    assert resolved["panel"] == fusion.PANEL_FULL
    assert resolved["judge"] == fusion.JUDGE
    assert all(s == "default" for s in resolved["sources"].values())
    captured = capfd.readouterr()
    assert "[fusion] council-config invalid" in captured.err


# ── POST /api/config deploy semantics tests ──────────────────────────────────


def test_deploy_returns_config_and_fusion_hash(client):
    """200 OK response includes the written config and a fusion_hash."""
    import console.app as appmod
    payload = {"judge": "moonshotai/kimi-k2.6"}
    r = client.post("/api/config", json=payload, headers=_bearer(client))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # config key contains the written file content
    assert "config" in body
    assert body["config"]["judge"] == "moonshotai/kimi-k2.6"
    assert body["config"]["updated_by"] == "console"
    # fusion_hash is a non-empty hex string
    assert "fusion_hash" in body
    assert isinstance(body["fusion_hash"], str)
    assert len(body["fusion_hash"]) == 16
    int(body["fusion_hash"], 16)  # must be valid hex


def test_deploy_blocked_by_build_lock(client):
    """409 Conflict when do-lock build lane is held."""
    import console.app as appmod
    import fcntl as _fcntl
    # Hold the lock to simulate a running build
    appmod.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(appmod.LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    _fcntl.flock(fd, _fcntl.LOCK_EX)
    try:
        r = client.post(
            "/api/config",
            json={"judge": "moonshotai/kimi-k2.6"},
            headers=_bearer(client),
        )
        assert r.status_code == 409
        body = r.json()
        assert body["blocked"] is True
        assert body["reason"] == "build_locked"
    finally:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        os.close(fd)


def test_deploy_succeeds_after_build_lock_released(client):
    """After the build lock is released, deploy succeeds (operator retry)."""
    import console.app as appmod
    import fcntl as _fcntl
    appmod.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(appmod.LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    _fcntl.flock(fd, _fcntl.LOCK_EX)
    _fcntl.flock(fd, _fcntl.LOCK_UN)
    os.close(fd)
    r = client.post(
        "/api/config",
        json={"judge": "moonshotai/kimi-k2.6"},
        headers=_bearer(client),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["config"]["judge"] == "moonshotai/kimi-k2.6"


def test_deploy_fusion_hash_changes_on_config_change(client):
    """Fusion hash differs before and after a config change."""
    import console.app as appmod
    # Hash with defaults (no config file yet)
    hash_before = appmod._compute_fusion_hash()
    # Deploy a change
    r = client.post(
        "/api/config",
        json={"judge": "moonshotai/kimi-k2.6"},
        headers=_bearer(client),
    )
    assert r.status_code == 200
    hash_after = r.json()["fusion_hash"]
    assert hash_before != hash_after


def test_deploy_partial_update_merges(client):
    """Partial POST merges with existing config (doesn't overwrite other roles)."""
    import console.app as appmod
    # Write an initial config
    appmod._atomic_write_config({
        "panel_full": ["z-ai/glm-5.2", "deepseek/deepseek-v4-pro"],
        "judge": "google/gemini-3.5-flash",
    })
    # POST only changing judge
    r = client.post(
        "/api/config",
        json={"judge": "moonshotai/kimi-k2.6"},
        headers=_bearer(client),
    )
    assert r.status_code == 200
    cfg = r.json()["config"]
    # panel_full preserved from prior config
    assert cfg["panel_full"] == ["z-ai/glm-5.2", "deepseek/deepseek-v4-pro"]
    # judge updated
    assert cfg["judge"] == "moonshotai/kimi-k2.6"

