"""Fixture-driven tests for the dashboard plugin backends.

Design contract (docs/plans/2026-07-01-archie-dashboard-design.md, "Testing &
verification"): plugin tabs are fixture-driven — known-good / idle /
stale-malformed artifact sets, assert the backend returns the right state for
each. No live run, no live dashboard, no Hetzner.

Run: python3 -m pytest dashboard-plugins/test_plugin_apis.py -q
Requires fastapi (the plugins import it); skipped cleanly if absent.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

PLUG_DIR = Path(__file__).resolve().parent


def _load(alias: str, rel: str):
    spec = importlib.util.spec_from_file_location(alias, PLUG_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


harness_api = _load("harness_run_api", "harness-run/dashboard/plugin_api.py")
ledger_api = _load("router_ledger_api", "router-ledger/dashboard/plugin_api.py")
swarm_api = _load("swarm_hetzner_api", "swarm-hetzner/dashboard/plugin_api.py")


def _run(coro):
    return asyncio.run(coro)


# -- fixtures ------------------------------------------------------------------

GOOD = "2026-01-01-good"
CORRUPT = "2026-01-01-corrupt"

LEDGER = {
    "rows": [
        {"costUsd": 0.5, "role": "build", "model": "z-ai/glm-5.2",
         "day": "2026-01-01", "tokensIn": 1000, "tokensOut": 200},
        {"costUsd": 1.25, "role": "council", "model": "anthropic/claude-sonnet-5",
         "day": "2026-01-01", "tokensIn": 500, "tokensOut": 100},
        {"costUsd": 2.0, "role": "build", "model": "z-ai/glm-5.2",
         "day": "2026-01-02", "tokensIn": 4000, "tokensOut": 800},
    ],
    "circuit_broken": True,
    "next_id": 3,
}

CHECKPOINT_LOG = [
    {"checkpoint": "design", "question": "q", "synthesis": "s"},
    {"checkpoint": "risky", "fallback": True, "blindspots": ["b1", "b2"]},
    {"checkpoint": "_coverage_failure", "unconsulted": ["lang"]},
]


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    """Known-good + corrupt runs under a fake artifacts dir; all three plugin
    modules re-pointed at it."""
    art = tmp_path / "artifacts"
    good = art / GOOD
    good.mkdir(parents=True)
    (good / "state.json").write_text(
        '{"sid": "s1", "segment": 2, "last_checkpoint": "risky"}')
    (good / "checkpoint-log.json").write_text(json.dumps(CHECKPOINT_LOG))
    (good / "burn.log").write_text("\n".join(f"line{i}" for i in range(300)))
    (good / "ledger.json").write_text(json.dumps(LEDGER))
    (good / "swarm-report.json").write_text(
        '{"status": "green", "fixes_committed": 2, "waves": 3}')
    (good / "fix-log.json").write_text('[{"fix": "one"}]')

    corrupt = art / CORRUPT
    corrupt.mkdir()
    (corrupt / "state.json").write_text("{not json")
    (corrupt / "checkpoint-log.json").write_text("[truncated")
    (corrupt / "ledger.json").write_text("{nope")

    for mod in (harness_api, ledger_api, swarm_api):
        monkeypatch.setattr(mod, "ARTIFACTS", art)
    return art


# -- harness-run ----------------------------------------------------------------

def test_harness_lists_runs_with_age(artifacts):
    out = _run(harness_api.list_runs())
    ids = [r["run_id"] for r in out["runs"]]
    assert GOOD in ids and CORRUPT in ids
    assert all("age_s" in r and "mtime" in r for r in out["runs"])


def test_harness_idle_state_empty_not_error(artifacts, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(harness_api, "ARTIFACTS", empty)
    assert _run(harness_api.list_runs()) == {"runs": [], "total": 0}


def test_harness_detail_aggregates_ledger_and_flags(artifacts):
    out = _run(harness_api.get_run(GOOD))
    assert out["state"]["segment"] == 2
    ls = out["ledger_summary"]
    assert ls["n_entries"] == 3 and ls["total_cost_usd"] == 3.75
    assert ls["by_role"]["build"] == 2.5
    cf = out["checkpoint_flags"]
    assert cf["fallbacks"] == ["risky"]
    assert cf["coverage_failures"] == [["lang"]]
    assert len(out["burn_tail"].splitlines()) == 100  # tail, not the whole log


def test_harness_missing_run_is_clean_404(artifacts):
    with pytest.raises(fastapi.HTTPException) as e:
        _run(harness_api.get_run("does-not-exist"))
    assert e.value.status_code == 404
    assert "does-not-exist" in e.value.detail


def test_harness_corrupt_artifacts_degrade_not_500(artifacts):
    """Stale/malformed artifacts (script died mid-write) must degrade to empty
    fields, not crash the panel."""
    out = _run(harness_api.get_run(CORRUPT))
    assert out["state"] == {}
    assert out["checkpoint_log"] == []
    assert out["ledger_summary"]["n_entries"] == 0


# -- router-ledger ----------------------------------------------------------------

def test_ledger_lists_only_runs_with_ledger(artifacts):
    # both GOOD and CORRUPT have ledger.json files; a run without one is hidden
    bare = artifacts / "2026-01-01-bare"
    bare.mkdir()
    ids = [r["run_id"] for r in _run(ledger_api.list_runs())["runs"]]
    assert GOOD in ids and "2026-01-01-bare" not in ids


def test_ledger_aggregates_daily_totals_vs_cap(artifacts):
    out = _run(ledger_api.get_ledger(GOOD))
    agg = out["aggregated"]
    assert agg["total_cost_usd"] == 3.75
    assert agg["by_role"] == {"build": 2.5, "council": 1.25}
    days = {d["day"]: d for d in agg["daily_totals"]}
    assert days["2026-01-01"]["total"] == 1.75
    assert days["2026-01-02"]["pct_of_cap"] == pytest.approx(8.0)
    assert agg["circuit_broken"] is True  # breaker state surfaced
    assert agg["tokens_in"] == 5500 and agg["tokens_out"] == 1100


def test_ledger_missing_run_404_corrupt_500(artifacts):
    with pytest.raises(fastapi.HTTPException) as e404:
        _run(ledger_api.get_ledger("does-not-exist"))
    assert e404.value.status_code == 404
    with pytest.raises(fastapi.HTTPException) as e500:
        _run(ledger_api.get_ledger(CORRUPT))
    assert e500.value.status_code == 500
    assert "corrupt" in e500.value.detail


def test_ledger_rows_most_recent_first_capped(artifacts):
    out = _run(ledger_api.get_ledger_rows(GOOD, limit=2))
    assert out["total"] == 3 and len(out["rows"]) == 2
    assert out["rows"][0]["day"] == "2026-01-02"  # reversed: newest first


# -- swarm-hetzner ----------------------------------------------------------------

def _fake_power(tmp_path, output: str) -> Path:
    script = tmp_path / "hetzner_power.sh"
    script.write_text(f"#!/usr/bin/env bash\necho \"{output}\"\n")
    return script


def test_swarm_liveness_running(artifacts, tmp_path, monkeypatch):
    monkeypatch.setattr(swarm_api, "HETZNER_POWER", _fake_power(tmp_path, "running"))
    out = _run(swarm_api.liveness())
    assert out["status"] == "running"
    assert "checked_at" in out


def test_swarm_liveness_off(artifacts, tmp_path, monkeypatch):
    monkeypatch.setattr(swarm_api, "HETZNER_POWER", _fake_power(tmp_path, "off"))
    assert _run(swarm_api.liveness())["status"] == "off"


def test_swarm_run_detail_reads_artifacts(artifacts):
    out = _run(swarm_api.get_swarm_data(GOOD))
    assert out["run_id"] == GOOD
    body = json.dumps(out)
    assert "green" in body  # swarm-report surfaced


def test_swarm_health_reports_power_script(artifacts):
    out = _run(swarm_api.health())
    assert out["status"] == "ok"
    assert "hetzner_power_exists" in out
