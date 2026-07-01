"""Checkpoint-dedupe test for stage2_build.core_loop (runbook §5 known gap).

A build agent that re-emits CHECKPOINT_REACHED:<id> for an already-consulted
checkpoint must NOT trigger a second Fusion council call (duplicated spend,
scope-expansion opening). The prior synthesis is re-injected instead.

Run on the droplet: ~/.hermes/hermes-agent/venv/bin/python3 -m pytest test_stage2_dedupe.py -q
(Not in CI yet: importing stage2_build pulls in fusion, which expects the
box's ~/.hermes credentials at call time; the test itself stubs all of it.)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "port"))

spec = importlib.util.spec_from_file_location("stage2_build", ROOT / "stage2_build.py")
s2 = importlib.util.module_from_spec(spec)
sys.modules["stage2_build"] = s2
spec.loader.exec_module(s2)


def test_duplicate_checkpoint_consults_council_once(tmp_path, monkeypatch):
    calls = {"fusion": 0}

    def fake_fusion(question, context, preset=None):
        calls["fusion"] += 1
        return {"synthesis": "USE X", "contradictions": [], "blindspots": []}

    monkeypatch.setattr(s2.fusion, "fusion", fake_fusion)
    monkeypatch.setattr(s2.fusion, "set_ledger", lambda ledger: None)
    monkeypatch.setattr(s2, "_changed_files", lambda cwd, base: [])

    script = iter([
        "CHECKPOINT_REACHED:design",   # segment 0: first (legit) consult
        "CHECKPOINT_REACHED:design",   # resumed segment re-emits the SAME id
        "BUILD_COMPLETE",
    ])
    injects = []

    def fake_segment(prompt, model, cwd, resume_sid=None, timeout=1800,
                     ledger=None, role="codex"):
        injects.append(prompt)
        return next(script), "", "sid1", 0

    monkeypatch.setattr(s2, "hermes_segment", fake_segment)

    manifest = {"checkpoints": [{
        "id": "design", "trigger": "t", "question": "q?", "on_synthesis": "apply",
    }]}
    cklog, seg = s2.core_loop(
        "PROMPT", manifest, "z-ai/glm-5.2", "budget",
        str(tmp_path), str(tmp_path / "art"), max_segments=10,
    )

    assert calls["fusion"] == 1, "duplicate checkpoint must not re-consult the council"
    deduped = [e for e in cklog if e.get("deduped")]
    assert len(deduped) == 1 and deduped[0]["checkpoint"] == "design"
    # the prior decision is re-injected verbatim, marked as already settled
    assert any("ALREADY consulted" in p and "USE X" in p for p in injects)


def test_distinct_checkpoints_each_get_a_consult(tmp_path, monkeypatch):
    calls = {"fusion": 0}

    def fake_fusion(question, context, preset=None):
        calls["fusion"] += 1
        return {"synthesis": f"S{calls['fusion']}", "contradictions": [], "blindspots": []}

    monkeypatch.setattr(s2.fusion, "fusion", fake_fusion)
    monkeypatch.setattr(s2.fusion, "set_ledger", lambda ledger: None)
    monkeypatch.setattr(s2, "_changed_files", lambda cwd, base: [])

    script = iter([
        "CHECKPOINT_REACHED:one",
        "CHECKPOINT_REACHED:two",
        "BUILD_COMPLETE",
    ])

    def fake_segment(prompt, model, cwd, resume_sid=None, timeout=1800,
                     ledger=None, role="codex"):
        return next(script), "", "sid1", 0

    monkeypatch.setattr(s2, "hermes_segment", fake_segment)

    manifest = {"checkpoints": [
        {"id": "one", "trigger": "t", "question": "q1", "on_synthesis": "a"},
        {"id": "two", "trigger": "t", "question": "q2", "on_synthesis": "a"},
    ]}
    cklog, _ = s2.core_loop(
        "PROMPT", manifest, "z-ai/glm-5.2", "budget",
        str(tmp_path), str(tmp_path / "art"), max_segments=10,
    )

    assert calls["fusion"] == 2  # dedupe must not suppress genuinely new checkpoints
    assert not any(e.get("deduped") for e in cklog)
