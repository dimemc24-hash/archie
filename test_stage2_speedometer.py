"""
test_stage2_speedometer.py — credits meter (speedometer) tests for hermes_segment.

Pattern: test_stage2_dedupe.py (importlib module load + monkeypatch of module
globals). No network: openrouter_credits.get_credits is stubbed via monkeypatch
of the module-level reference in stage2_build (stage2_build.orc).

Covers (build-spec.md §2 + council decision delta-attribution, Option a):
  1. both samples present → delta charged with trigger="credits-delta"
  2. before=None (one-side failure) → estimate fallback, trigger="estimate"
  3. after=None (one-side failure) → estimate fallback, trigger="estimate"
  4. floor breach (remaining < min) → SystemExit(4) + alert recorded
  5. floor skipped when meter unreachable (get_credits → None)
  6. concurrency-overcount: the FULL delta (including concurrent spend) is
     charged to the running segment — breaker trips early (safe direction).
     A concurrent spend that inflates the delta must appear in the charged cost.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "port"))

spec = importlib.util.spec_from_file_location("stage2_build", ROOT / "stage2_build.py")
s2 = importlib.util.module_from_spec(spec)
sys.modules["stage2_build"] = s2
spec.loader.exec_module(s2)


def _make_ledger():
    """A real Ledger with a tiny daily cap so charges are visible."""
    import harness_ledger as hl
    return hl.Ledger(daily_cap_usd=1000.0)


def _stub_segment_output(monkeypatch):
    """Stub run() so hermes_segment doesn't actually call hermes."""
    monkeypatch.setattr(s2, "run", lambda cmd, cwd, timeout=5400: (0, "STUB_OUT", "STUB_ERR"))
    # newest_sid doesn't matter for these tests
    monkeypatch.setattr(s2, "newest_sid", lambda cwd: "sid-stub")

def _set_credits_sequence(monkeypatch, seq):
    """Make orc.get_credits return values from seq in order, then repeat last."""
    it = iter(seq)
    last = [None]
    def fake_get_credits(transport=None):
        try:
            last[0] = next(it)
            return last[0]
        except StopIteration:
            return last[0]
    monkeypatch.setattr(s2.orc, "get_credits", fake_get_credits)


def _last_charge(ledger):
    """Return the last charged row, or None."""
    rows = ledger._rows
    return rows[-1] if rows else None


# ── 1. both samples present → delta charged with credits-delta trigger ──────

def test_delta_charged_with_credits_delta_trigger(tmp_path, monkeypatch):
    _stub_segment_output(monkeypatch)
    # floor ok, before: usage=30.0; after: usage=33.5 → delta = 3.5
    _set_credits_sequence(monkeypatch, [
        {"total_credits": 100.0, "total_usage": 30.0, "remaining": 70.0},  # floor
        {"total_credits": 100.0, "total_usage": 30.0, "remaining": 70.0},  # before
        {"total_credits": 100.0, "total_usage": 33.5, "remaining": 66.5},  # after
    ])
    ledger = _make_ledger()
    out, err, sid, rc = s2.hermes_segment("prompt", "z-ai/glm-5.2", str(tmp_path),
                                           ledger=ledger)
    row = _last_charge(ledger)
    assert row is not None
    assert row["trigger"] == "credits-delta"
    assert row["costUsd"] == pytest.approx(3.5)
    # metered rows carry zero tokens (cost is from the credits API, not token math)
    assert row["tokensIn"] == 0
    assert row["tokensOut"] == 0


# ── 2. before=None → estimate fallback ──────────────────────────────────────

def test_before_none_falls_back_to_estimate(tmp_path, monkeypatch):
    _stub_segment_output(monkeypatch)
    _set_credits_sequence(monkeypatch, [
        {"total_credits": 100.0, "total_usage": 30.0, "remaining": 70.0},  # floor ok
        None,  # before sample fails
        {"total_credits": 100.0, "total_usage": 33.5, "remaining": 66.5},  # after (unused)
    ])
    ledger = _make_ledger()
    s2.hermes_segment("prompt", "z-ai/glm-5.2", str(tmp_path), ledger=ledger)
    row = _last_charge(ledger)
    assert row["trigger"] == "estimate"
    # estimate constants: 2000 in, 1500 out
    assert row["tokensIn"] == 2000
    assert row["tokensOut"] == 1500
    # cost computed from price table, not zero
    assert row["costUsd"] > 0


# ── 3. after=None → estimate fallback ───────────────────────────────────────

def test_after_none_falls_back_to_estimate(tmp_path, monkeypatch):
    _stub_segment_output(monkeypatch)
    _set_credits_sequence(monkeypatch, [
        {"total_credits": 100.0, "total_usage": 30.0, "remaining": 70.0},  # floor ok
        {"total_credits": 100.0, "total_usage": 30.0, "remaining": 70.0},  # before
        None,  # after sample fails
    ])
    ledger = _make_ledger()
    s2.hermes_segment("prompt", "z-ai/glm-5.2", str(tmp_path), ledger=ledger)
    row = _last_charge(ledger)
    assert row["trigger"] == "estimate"
    assert row["tokensIn"] == 2000
    assert row["tokensOut"] == 1500


# ── 4. floor breach → SystemExit(4) + alert recorded ───────────────────────

def test_floor_breach_aborts_with_systemexit4(tmp_path, monkeypatch):
    _stub_segment_output(monkeypatch)
    # The floor check calls get_credits() once before the segment.
    # remaining = 5.0 < default min 10.0 → abort.
    _set_credits_sequence(monkeypatch, [
        {"total_credits": 100.0, "total_usage": 95.0, "remaining": 5.0},
    ])
    # stub alert: record the call instead of firing alert.sh
    alert_calls = []
    monkeypatch.setattr(s2, "_alert", lambda msg: alert_calls.append(msg))
    # make ALERT_SH not exist so _alert wouldn't call run anyway
    monkeypatch.setattr(s2, "ALERT_SH", "/nonexistent/alert.sh")

    ledger = _make_ledger()
    with pytest.raises(SystemExit) as exc_info:
        s2.hermes_segment("prompt", "z-ai/glm-5.2", str(tmp_path), ledger=ledger)
    assert exc_info.value.code == 4
    # the alert was fired with the BUILD_BLOCKED message
    assert len(alert_calls) == 1
    assert "BUILD_BLOCKED:credits-floor" in alert_calls[0]
    assert "5.00" in alert_calls[0]
    # no ledger charge was made (aborted before the segment ran)
    assert ledger.count() == 0


def test_floor_breach_respects_env_min(tmp_path, monkeypatch):
    """OPENROUTER_MIN_CREDITS is env-overridable."""
    _stub_segment_output(monkeypatch)
    monkeypatch.setenv("OPENROUTER_MIN_CREDITS", "50.0")
    # Reload the module constant by re-importing — but since OPENROUTER_MIN_CREDITS
    # is read at import time, we patch the module attribute directly.
    monkeypatch.setattr(s2, "OPENROUTER_MIN_CREDITS", 50.0)
    # remaining=40.0 < 50.0 → abort
    _set_credits_sequence(monkeypatch, [
        {"total_credits": 100.0, "total_usage": 60.0, "remaining": 40.0},
    ])
    monkeypatch.setattr(s2, "_alert", lambda msg: None)
    ledger = _make_ledger()
    with pytest.raises(SystemExit) as exc_info:
        s2.hermes_segment("prompt", "z-ai/glm-5.2", str(tmp_path), ledger=ledger)
    assert exc_info.value.code == 4


# ── 5. floor skipped when meter unreachable ────────────────────────────────

def test_floor_skipped_when_meter_unreachable(tmp_path, monkeypatch):
    """If get_credits returns None, the floor check is skipped — the segment
    runs, and the estimate fallback is charged."""
    _stub_segment_output(monkeypatch)
    # floor → None (skipped); before → None; after → None → estimate fallback
    _set_credits_sequence(monkeypatch, [None, None, None])
    ledger = _make_ledger()
    # must NOT raise
    out, err, sid, rc = s2.hermes_segment("prompt", "z-ai/glm-5.2", str(tmp_path),
                                           ledger=ledger)
    assert rc == 0
    row = _last_charge(ledger)
    assert row["trigger"] == "estimate"


# ── 6. concurrency-overcount: full delta charged to running segment ────────

def test_concurrency_overcount_charges_full_delta(tmp_path, monkeypatch):
    """Council decision (Option a): the FULL account-level delta is charged to
    the running segment, even if concurrent spend inflated it. This is the
    conservative direction — the breaker trips early under concurrency.

    Scenario: the segment itself cost $2.0, but a concurrent process (Archie's
    chat) spent $5.0 during the same window. The credits delta is $7.0 — the
    full $7.0 is charged to this segment. Per-run attribution is overcounted,
    but the account-level total is honest and the breaker sees the real burn.
    """
    _stub_segment_output(monkeypatch)
    # before: usage=30.0; after: usage=37.0 → delta=7.0 (segment $2 + concurrent $5)
    _set_credits_sequence(monkeypatch, [
        {"total_credits": 100.0, "total_usage": 30.0, "remaining": 70.0},  # floor ok
        {"total_credits": 100.0, "total_usage": 30.0, "remaining": 70.0},  # before
        {"total_credits": 100.0, "total_usage": 37.0, "remaining": 63.0},  # after
    ])
    ledger = _make_ledger()
    s2.hermes_segment("prompt", "z-ai/glm-5.2", str(tmp_path), ledger=ledger)
    row = _last_charge(ledger)
    assert row["trigger"] == "credits-delta"
    # the FULL delta (7.0) is charged — concurrent spend included, not split out
    assert row["costUsd"] == pytest.approx(7.0)


def test_concurrency_does_not_split_into_drift_row(tmp_path, monkeypatch):
    """Option (a) explicitly rejects the role='account-drift' split from Option (b).
    Verify NO row with role='account-drift' is ever created."""
    _stub_segment_output(monkeypatch)
    _set_credits_sequence(monkeypatch, [
        {"total_credits": 100.0, "total_usage": 30.0, "remaining": 70.0},
        {"total_credits": 100.0, "total_usage": 30.0, "remaining": 70.0},
        {"total_credits": 100.0, "total_usage": 37.0, "remaining": 63.0},
    ])
    ledger = _make_ledger()
    s2.hermes_segment("prompt", "z-ai/glm-5.2", str(tmp_path), ledger=ledger)
    drift_rows = [r for r in ledger._rows if r["role"] == "account-drift"]
    assert len(drift_rows) == 0, "Option (a) charges full delta to segment, no drift row"


def test_negative_delta_clamped_to_zero(tmp_path, monkeypatch):
    """If usage went DOWN (e.g. credits refund, or a race in the API), the
    delta is clamped to 0 — never negative. max(0, delta) per build-spec."""
    _stub_segment_output(monkeypatch)
    # before: usage=37.0; after: usage=35.0 → delta=-2.0 → clamped to 0
    _set_credits_sequence(monkeypatch, [
        {"total_credits": 100.0, "total_usage": 37.0, "remaining": 63.0},
        {"total_credits": 100.0, "total_usage": 37.0, "remaining": 63.0},
        {"total_credits": 100.0, "total_usage": 35.0, "remaining": 65.0},
    ])
    ledger = _make_ledger()
    s2.hermes_segment("prompt", "z-ai/glm-5.2", str(tmp_path), ledger=ledger)
    row = _last_charge(ledger)
    assert row["trigger"] == "credits-delta"
    assert row["costUsd"] == pytest.approx(0.0)
