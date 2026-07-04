"""
test_swarm_config.py — unit tests for swarm/swarm_config.py (profile-driven transport
plan resolution with .swarm.json at repo root).

Zero-network: all tests use temp dirs and fixture JSON. No SSH, no Hetzner, no git.

Covers (decision: verify-lane-shape option c — .swarm.json at repo root):
  1. Legacy fallback (no profile → NextChapter hardcoded defaults, live runner)
  2. Archie profile with .swarm.json → generic runner, correct scope/deps/verify
  3. NewChapter profile with .swarm.json → generic runner, TS/npm/tsc config
  4. Missing .swarm.json → hardcoded fallback defaults + WARNING emitted
  5. Schema versioning: correct version accepted, future version rejected, old version warned
  6. Profile with no swarm section → live runner with profile workspace
  7. format_plan produces readable output for both repos + fallback warning
  8. Profile not found raises FileNotFoundError

Run: python3 -m pytest test_swarm_config.py -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Load swarm_config.py via importlib (matches the house style of test_stage2_*.py)
spec = importlib.util.spec_from_file_location("swarm_config", ROOT / "swarm" / "swarm_config.py")
sc = importlib.util.module_from_spec(spec)
sys.modules["swarm_config"] = sc
spec.loader.exec_module(sc)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_setup(tmp_path):
    """Create temp profiles/ and workspace dirs, point swarm_config at them."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    # Workspaces: each profile gets a "repo root" dir where .swarm.json lives
    archie_ws = tmp_path / "workspaces" / "archie" / "repo"
    archie_ws.mkdir(parents=True)
    newchapter_ws = tmp_path / "workspaces" / "newchapter" / "repo"
    newchapter_ws.mkdir(parents=True)

    # Monkeypatch the module-level paths
    old_profiles = sc.PROFILES_DIR
    sc.PROFILES_DIR = str(profiles_dir)

    yield {
        "profiles": profiles_dir,
        "archie_ws": archie_ws,
        "newchapter_ws": newchapter_ws,
    }

    sc.PROFILES_DIR = old_profiles


def _write_profile(dir_path: Path, name: str, data: dict):
    (dir_path / f"{name}.json").write_text(json.dumps(data))


def _write_swarm_json(workspace: Path, data: dict):
    (workspace / ".swarm.json").write_text(json.dumps(data))


# ── Fixtures: .swarm.json data ────────────────────────────────────────────────

ARCHIE_SWARM_JSON = {
    "version": 1,
    "name": "archie",
    "hetzner_repo_path": "swarm/archie",
    "scope_pattern": "\\.(py)$",
    "exclude_pattern": "(__pycache__/|\\.pyc$|/test_)",
    "lockfile": "",
    "deps_step": "none",
    "dev_server_cmd": "",
    "dev_server_health": "",
    "verify_cmd": "python3 -m pytest antiques/test_antiques.py -q",
    "routes": "",
}

NEWCHAPTER_SWARM_JSON = {
    "version": 1,
    "name": "newchapter",
    "hetzner_repo_path": "swarm/newchapter",
    "scope_pattern": "\\.(ts|tsx)$",
    "exclude_pattern": "(\\.test\\.|/__tests__/|^_harness/|\\.d\\.ts$)",
    "lockfile": "package-lock.json",
    "deps_step": "npm ci --legacy-peer-deps",
    "dev_server_cmd": "npm run dev",
    "dev_server_health": "http://localhost:3000",
    "verify_cmd": "npx tsc --noEmit",
    "routes": "",
}


# ── Tests: legacy fallback (no profile) ───────────────────────────────────────

def test_legacy_no_profile_uses_nextchapter_defaults(temp_setup):
    """No profile → legacy NextChapter plan: live runner, hardcoded paths."""
    plan = sc.resolve_transport_plan("2026-07-04-test", profile=None)
    assert plan.is_legacy is True
    assert plan.runner == "live"
    assert plan.do_repo.endswith("/harness/repo")
    assert plan.hetzner_repo_path == "swarm/newchapter"
    assert plan.hetzner_remote == "hetzner-swarm:swarm/newchapter"
    assert "run_swarm.sh" in plan.runner_script
    assert plan.repo_config is None
    assert plan.bootstrap is False  # legacy doesn't bootstrap
    assert plan.config_fallback is False


def test_legacy_empty_profile_string_uses_defaults(temp_setup):
    """Empty string profile → same as None (legacy)."""
    plan = sc.resolve_transport_plan("test-id", profile="")
    assert plan.is_legacy is True
    assert plan.runner == "live"


# ── Tests: archie profile with .swarm.json ────────────────────────────────────

def test_archie_profile_resolves_generic_runner(temp_setup):
    """Archie profile + .swarm.json → generic runner, correct scope/deps/verify."""
    _write_profile(temp_setup["profiles"], "archie", {
        "name": "archie",
        "repo_url": "git@github-archie:dimemc24-hash/archie.git",
        "workspace": str(temp_setup["archie_ws"]),
        "swarm": {
            "repo_name": "archie",
            "hetzner_repo_path": "swarm/archie",
            "runner": "generic",
            "bootstrap": True,
        }
    })
    _write_swarm_json(temp_setup["archie_ws"], ARCHIE_SWARM_JSON)

    plan = sc.resolve_transport_plan(
        "2026-07-04-test", profile="archie",
        workspace_override=str(temp_setup["archie_ws"]),
    )
    assert plan.is_legacy is False
    assert plan.runner == "generic"
    assert plan.repo_name == "archie"
    assert plan.do_repo == str(temp_setup["archie_ws"])
    assert plan.hetzner_repo_path == "swarm/archie"
    assert plan.hetzner_remote == "hetzner-swarm:swarm/archie"
    assert plan.bootstrap is True
    assert "run_swarm_generic.sh" in plan.runner_script
    assert plan.config_path == ".swarm.json"
    assert plan.repo_config is not None
    assert plan.repo_config.scope_pattern == "\\.(py)$"
    assert plan.repo_config.deps_step == "none"
    assert plan.repo_config.verify_cmd == "python3 -m pytest antiques/test_antiques.py -q"
    assert plan.repo_config.used_fallback is False
    assert plan.repo_config.config_version == 1
    assert plan.config_fallback is False


def test_archie_profile_runner_command_format(temp_setup):
    """The runner SSH command must match the generic runner's expected args."""
    _write_profile(temp_setup["profiles"], "archie", {
        "name": "archie", "workspace": str(temp_setup["archie_ws"]),
        "swarm": {"repo_name": "archie", "runner": "generic", "hetzner_repo_path": "swarm/archie"}
    })
    _write_swarm_json(temp_setup["archie_ws"], ARCHIE_SWARM_JSON)

    plan = sc.resolve_transport_plan(
        "my-run-id", profile="archie", routes="/cases/1,/cases/2", waves=5,
        workspace_override=str(temp_setup["archie_ws"]),
    )
    assert "run_swarm_generic.sh" in plan.runner_script
    assert "'archie'" in plan.runner_script
    assert "build/my-run-id" in plan.runner_script
    assert "/cases/1,/cases/2" in plan.runner_script
    assert "'5'" in plan.runner_script


# ── Tests: newchapter profile with .swarm.json ────────────────────────────────

def test_newchapter_profile_resolves_from_swarm_json(temp_setup):
    """NewChapter profile + .swarm.json → correct TS/npm/tsc config from repo root."""
    _write_profile(temp_setup["profiles"], "newchapter", {
        "name": "newchapter",
        "workspace": str(temp_setup["newchapter_ws"]),
        "swarm": {
            "repo_name": "newchapter",
            "hetzner_repo_path": "swarm/newchapter",
            "runner": "generic",
        }
    })
    _write_swarm_json(temp_setup["newchapter_ws"], NEWCHAPTER_SWARM_JSON)

    plan = sc.resolve_transport_plan(
        "test-id", profile="newchapter",
        workspace_override=str(temp_setup["newchapter_ws"]),
    )
    assert plan.runner == "generic"
    assert plan.repo_config is not None
    assert plan.repo_config.scope_pattern == "\\.(ts|tsx)$"
    assert plan.repo_config.lockfile == "package-lock.json"
    assert plan.repo_config.deps_step == "npm ci --legacy-peer-deps"
    assert plan.repo_config.dev_server_cmd == "npm run dev"
    assert plan.repo_config.verify_cmd == "npx tsc --noEmit"
    assert plan.repo_config.used_fallback is False
    assert plan.config_fallback is False


# ── Tests: missing .swarm.json → fallback + warning ───────────────────────────

def test_missing_swarm_json_uses_fallback_defaults(temp_setup):
    """No .swarm.json in workspace → hardcoded fallback defaults used."""
    _write_profile(temp_setup["profiles"], "archie", {
        "name": "archie", "workspace": str(temp_setup["archie_ws"]),
        "swarm": {"repo_name": "archie", "runner": "generic", "hetzner_repo_path": "swarm/archie"}
    })
    # Deliberately do NOT write .swarm.json

    plan = sc.resolve_transport_plan(
        "test-id", profile="archie",
        workspace_override=str(temp_setup["archie_ws"]),
    )
    assert plan.runner == "generic"
    assert plan.repo_config is not None
    assert plan.repo_config.used_fallback is True
    assert plan.config_fallback is True
    # Fallback must use the NextChapter-shaped defaults
    assert plan.repo_config.scope_pattern == sc.FALLBACK_SCOPE_PATTERN
    assert plan.repo_config.deps_step == sc.FALLBACK_DEPS_STEP
    assert plan.repo_config.verify_cmd == sc.FALLBACK_VERIFY_CMD


def test_missing_swarm_json_emits_warning(temp_setup, capsys):
    """Missing .swarm.json must emit a clear warning to stderr (silent-fallback blindspot)."""
    _write_profile(temp_setup["profiles"], "archie", {
        "name": "archie", "workspace": str(temp_setup["archie_ws"]),
        "swarm": {"repo_name": "archie", "runner": "generic"}
    })
    # No .swarm.json written

    sc.resolve_transport_plan(
        "test-id", profile="archie",
        workspace_override=str(temp_setup["archie_ws"]),
    )
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert ".swarm.json" in captured.err
    assert "archie" in captured.err


# ── Tests: schema versioning ──────────────────────────────────────────────────

def test_swarm_config_version_constant():
    """The module exposes a SWARM_CONFIG_VERSION constant."""
    assert sc.SWARM_CONFIG_VERSION == 1


def test_future_schema_version_rejected(temp_setup):
    """A .swarm.json with version > CURRENT must raise ValueError (schema evolution blindspot)."""
    _write_profile(temp_setup["profiles"], "archie", {
        "name": "archie", "workspace": str(temp_setup["archie_ws"]),
        "swarm": {"repo_name": "archie", "runner": "generic"}
    })
    future = dict(ARCHIE_SWARM_JSON, version=99)
    _write_swarm_json(temp_setup["archie_ws"], future)

    with pytest.raises(ValueError, match="newer than supported"):
        sc.resolve_transport_plan(
            "test-id", profile="archie",
            workspace_override=str(temp_setup["archie_ws"]),
        )


def test_old_schema_version_accepted_with_warning(temp_setup):
    """A .swarm.json with version < CURRENT is accepted with a DeprecationWarning."""
    _write_profile(temp_setup["profiles"], "archie", {
        "name": "archie", "workspace": str(temp_setup["archie_ws"]),
        "swarm": {"repo_name": "archie", "runner": "generic"}
    })
    old = dict(ARCHIE_SWARM_JSON, version=0)
    _write_swarm_json(temp_setup["archie_ws"], old)

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        plan = sc.resolve_transport_plan(
            "test-id", profile="archie",
            workspace_override=str(temp_setup["archie_ws"]),
        )
    assert plan.repo_config is not None
    assert plan.repo_config.config_version == 0
    # A deprecation warning should have been emitted
    assert any(issubclass(x.category, DeprecationWarning) for x in w)


def test_load_swarm_config_directly(temp_setup):
    """load_swarm_config reads .swarm.json from a given directory."""
    _write_swarm_json(temp_setup["archie_ws"], ARCHIE_SWARM_JSON)
    rc = sc.load_swarm_config(str(temp_setup["archie_ws"]))
    assert rc.name == "archie"
    assert rc.scope_pattern == "\\.(py)$"
    assert rc.verify_cmd == "python3 -m pytest antiques/test_antiques.py -q"
    assert rc.used_fallback is False
    assert rc.config_version == 1
    assert ".swarm.json" in rc.config_source


def test_load_swarm_config_raises_on_missing():
    """load_swarm_config raises FileNotFoundError when .swarm.json is absent."""
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(FileNotFoundError, match="\\.swarm\\.json not found"):
            sc.load_swarm_config(d)


def test_fallback_repo_config():
    """fallback_repo_config produces NextChapter-shaped defaults with used_fallback=True."""
    rc = sc.fallback_repo_config("some-repo")
    assert rc.name == "some-repo"
    assert rc.used_fallback is True
    assert rc.config_version == 0
    assert rc.scope_pattern == sc.FALLBACK_SCOPE_PATTERN
    assert rc.deps_step == sc.FALLBACK_DEPS_STEP
    assert rc.verify_cmd == sc.FALLBACK_VERIFY_CMD


# ── Tests: profile with no swarm section ──────────────────────────────────────

def test_profile_without_swarm_section_falls_back(temp_setup):
    """Profile exists but has no 'swarm' key → live runner with profile workspace."""
    _write_profile(temp_setup["profiles"], "bare", {
        "name": "bare", "workspace": str(temp_setup["archie_ws"])
    })
    plan = sc.resolve_transport_plan("test", profile="bare")
    assert plan.is_legacy is False
    assert plan.runner == "live"
    assert plan.repo_name == "bare"
    assert plan.hetzner_repo_path == "swarm/bare"
    assert "run_swarm.sh" in plan.runner_script


# ── Tests: error cases ───────────────────────────────────────────────────────

def test_profile_not_found_raises(temp_setup):
    """Unknown profile → FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="profile not found"):
        sc.resolve_transport_plan("test", profile="nonexistent")


# ── Tests: format_plan ────────────────────────────────────────────────────────

def test_format_plan_legacy(temp_setup):
    """format_plan for legacy produces readable output with all key fields."""
    plan = sc.resolve_transport_plan("test-id", profile=None)
    out = sc.format_plan(plan)
    assert "DRY-RUN PLAN" in out
    assert "legacy NextChapter" in out
    assert "live" in out
    assert "swarm/newchapter" in out


def test_format_plan_archie(temp_setup):
    """format_plan for archie produces readable output with repo config details."""
    _write_profile(temp_setup["profiles"], "archie", {
        "name": "archie", "workspace": str(temp_setup["archie_ws"]),
        "swarm": {"repo_name": "archie", "runner": "generic", "hetzner_repo_path": "swarm/archie"}
    })
    _write_swarm_json(temp_setup["archie_ws"], ARCHIE_SWARM_JSON)
    plan = sc.resolve_transport_plan(
        "test-id", profile="archie",
        workspace_override=str(temp_setup["archie_ws"]),
    )
    out = sc.format_plan(plan)
    assert "archie" in out
    assert "generic" in out
    assert "swarm/archie" in out
    assert "\\.(py)$" in out
    assert "pytest antiques/test_antiques.py" in out
    assert "cfg_version" in out


def test_format_plan_shows_fallback_warning(temp_setup):
    """format_plan shows a fallback warning when .swarm.json is missing."""
    _write_profile(temp_setup["profiles"], "archie", {
        "name": "archie", "workspace": str(temp_setup["archie_ws"]),
        "swarm": {"repo_name": "archie", "runner": "generic"}
    })
    # No .swarm.json
    plan = sc.resolve_transport_plan(
        "test-id", profile="archie",
        workspace_override=str(temp_setup["archie_ws"]),
    )
    out = sc.format_plan(plan)
    assert "FALLBACK" in out
    assert ".swarm.json" in out


def test_format_plan_newchapter(temp_setup):
    """format_plan for newchapter shows TS/npm/tsc config."""
    _write_profile(temp_setup["profiles"], "newchapter", {
        "name": "newchapter", "workspace": str(temp_setup["newchapter_ws"]),
        "swarm": {"repo_name": "newchapter", "runner": "generic"}
    })
    _write_swarm_json(temp_setup["newchapter_ws"], NEWCHAPTER_SWARM_JSON)
    plan = sc.resolve_transport_plan(
        "test-id", profile="newchapter",
        workspace_override=str(temp_setup["newchapter_ws"]),
    )
    out = sc.format_plan(plan)
    assert "newchapter" in out
    assert "\\.(ts|tsx)$" in out
    assert "npm ci" in out
    assert "tsc" in out
