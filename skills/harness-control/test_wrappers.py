"""Wrapper tests for the harness-control skills.

Design contract (docs/plans/2026-07-01-archie-dashboard-design.md, "Testing &
verification"): thin wrapper tests — right underlying script invoked with the
right args, do-lock.sh taken (correct mode) before firing, busy-lock and
non-zero exits propagated to the caller (with alerts), never swallowed. Plus
the Stage-4 two-step dry-run/apply gate, including stale-marker refusal.

Everything runs against a fake ~/harness tree under tmp_path — no network,
no real locks, no real alerts, no real repos beyond throwaway local git.

Run: python3 -m pytest skills/harness-control/test_wrappers.py -q
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# _common first so the stage modules pick up the same instance from sys.modules.
C = _load("_common")
stage1 = _load("run_stage1")
stage2 = _load("run_stage2")
stage3 = _load("run_stage3")
stage4 = _load("run_stage4")


# -- fixtures ------------------------------------------------------------------

@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake ~/harness: pass-through do-lock, recording alert.sh, recorder
    stand-ins for the wrapped scripts. Returns the harness root."""
    h = tmp_path / "harness"
    (h / "artifacts").mkdir(parents=True)
    (h / "profiles").mkdir()
    (h / "locks").mkdir()

    # do-lock.sh: LOCK_FORCE_BUSY=1 simulates a held lock (exit 75, like the
    # real script); otherwise drop the mode arg and exec the wrapped command.
    (h / "do-lock.sh").write_text(
        "#!/usr/bin/env bash\n"
        'if [ -n "${LOCK_FORCE_BUSY:-}" ]; then echo "DO BUSY: test"; exit 75; fi\n'
        "shift\n"
        'exec "$@"\n'
    )
    # alert.sh: record instead of pinging Telegram.
    (h / "alert.sh").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$1" >> "{h / "alerts.log"}"\n'
    )
    # Recorder stand-ins for the wrapped scripts: write argv to $REC_FILE,
    # exit $FAKE_RC.
    py_recorder = (
        "#!/usr/bin/env python3\n"
        "import os, sys, pathlib\n"
        "pathlib.Path(os.environ['REC_FILE']).write_text(' '.join(sys.argv[1:]))\n"
        "sys.exit(int(os.environ.get('FAKE_RC', '0')))\n"
    )
    (h / "stage2_build.py").write_text(py_recorder)
    (h / "emit_spec.py").write_text(py_recorder)
    (h / "transport.sh").write_text(
        "#!/usr/bin/env bash\n"
        'echo "$@" > "$REC_FILE"\n'
        'exit "${FAKE_RC:-0}"\n'
    )

    monkeypatch.setattr(C, "HARNESS", str(h))
    monkeypatch.setattr(C, "ARTIFACTS", str(h / "artifacts"))
    monkeypatch.setattr(C, "PROFILES_DIR", str(h / "profiles"))
    monkeypatch.setattr(C, "DO_LOCK", str(h / "do-lock.sh"))
    monkeypatch.setattr(C, "ALERT", str(h / "alert.sh"))
    monkeypatch.setattr(C, "STAGE2", str(h / "stage2_build.py"))
    monkeypatch.setattr(C, "TRANSPORT", str(h / "transport.sh"))
    monkeypatch.setattr(C, "EMIT_SPEC", str(h / "emit_spec.py"))
    monkeypatch.setattr(C, "REPO", str(h / "repo"))

    monkeypatch.setenv("REC_FILE", str(h / "invoked.txt"))
    monkeypatch.delenv("FAKE_RC", raising=False)
    monkeypatch.delenv("LOCK_FORCE_BUSY", raising=False)
    return h


def _argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["prog", *args])


# -- dry-run command shape (right script, right args, right lock mode) ---------

def test_stage2_dry_run_build_lock_and_args(fake, monkeypatch, capsys):
    _argv(monkeypatch, "--run-id", "2026-01-01-t", "--profile", "archie", "--dry-run")
    assert stage2.main() == 0
    out = capsys.readouterr().out
    assert "do-lock (build)" in out
    assert "stage2_build.py --run-id 2026-01-01-t --base main --profile archie" in out
    assert not (fake / "invoked.txt").exists()  # dry-run never executes


def test_stage2_smoke_uses_attend_lock(fake, monkeypatch, capsys):
    _argv(monkeypatch, "--smoke", "--dry-run")
    assert stage2.main() == 0
    out = capsys.readouterr().out
    assert "do-lock (attend)" in out
    assert "--smoke" in out


def test_stage2_requires_run_id_without_smoke(fake, monkeypatch, capsys):
    _argv(monkeypatch, "--dry-run")
    with pytest.raises(SystemExit) as e:
        stage2.main()
    assert e.value.code == 1
    assert "--run-id is required" in capsys.readouterr().err


def test_stage1_dry_run_attend_lock_pushes_by_default(fake, tmp_path, monkeypatch, capsys):
    spec = tmp_path / "build-spec.md"; spec.write_text("s")
    prompt = tmp_path / "build-prompt.md"; prompt.write_text("p")
    manifest = tmp_path / "checkpoint-manifest.json"; manifest.write_text("{}")
    _argv(monkeypatch, "--run-id", "2026-01-01-t", "--spec", str(spec),
          "--prompt", str(prompt), "--manifest", str(manifest), "--dry-run")
    assert stage1.main() == 0
    out = capsys.readouterr().out
    assert "do-lock (attend)" in out
    assert "emit_spec.py" in out and "--push" in out


def test_stage1_no_push_omits_push(fake, tmp_path, monkeypatch, capsys):
    spec = tmp_path / "s.md"; spec.write_text("s")
    prompt = tmp_path / "p.md"; prompt.write_text("p")
    manifest = tmp_path / "m.json"; manifest.write_text("{}")
    _argv(monkeypatch, "--run-id", "2026-01-01-t", "--spec", str(spec),
          "--prompt", str(prompt), "--manifest", str(manifest),
          "--no-push", "--dry-run")
    assert stage1.main() == 0
    assert "--push" not in capsys.readouterr().out


def test_stage1_missing_bundle_file_dies_loudly(fake, tmp_path, monkeypatch, capsys):
    _argv(monkeypatch, "--run-id", "2026-01-01-t", "--spec", str(tmp_path / "nope.md"),
          "--prompt", str(tmp_path / "nope2.md"), "--manifest", str(tmp_path / "nope3.json"))
    with pytest.raises(SystemExit) as e:
        stage1.main()
    assert e.value.code == 1
    assert "[harness-control] ERROR" in capsys.readouterr().err


def test_stage3_dry_run_build_lock_routes_waves(fake, monkeypatch, capsys):
    _argv(monkeypatch, "--run-id", "2026-01-01-t", "--dry-run")
    assert stage3.main() == 0
    out = capsys.readouterr().out
    assert "do-lock (build)" in out
    assert "transport.sh 2026-01-01-t '' 3" in out


def test_stage3_baseline_exported_to_env(fake, monkeypatch, capsys):
    _argv(monkeypatch, "--run-id", "2026-01-01-t", "--baseline", "origin/dev", "--dry-run")
    assert stage3.main() == 0
    import os
    assert os.environ.get("HARNESS_BASELINE") == "origin/dev"


# -- lock + failure propagation -------------------------------------------------

def test_busy_lock_propagates_75_with_clear_message(fake, monkeypatch, capsys):
    monkeypatch.setenv("LOCK_FORCE_BUSY", "1")
    _argv(monkeypatch, "--run-id", "2026-01-01-t")
    assert stage2.main() == C.LOCK_BUSY
    err = capsys.readouterr().err
    assert "DO is BUSY" in err and "already in flight" in err


def test_underlying_failure_propagates_rc_and_alerts(fake, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RC", "3")
    _argv(monkeypatch, "--run-id", "2026-01-01-t")
    with pytest.raises(SystemExit) as e:
        stage2.main()
    assert e.value.code == 3  # the underlying rc, not a generic 1
    assert "exited 3" in capsys.readouterr().err
    alerts = (fake / "alerts.log").read_text()
    assert "Stage 2 build failed" in alerts


def test_success_invokes_underlying_with_args(fake, monkeypatch, capsys):
    _argv(monkeypatch, "--run-id", "2026-01-01-t", "--preset", "budget")
    assert stage2.main() == 0
    invoked = (fake / "invoked.txt").read_text()
    assert "--run-id 2026-01-01-t" in invoked and "--preset budget" in invoked


# -- Stage 4: two-step dry-run/apply gate ----------------------------------------

def _git(ws: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(ws), *args],
                       check=True, capture_output=True, text=True)
    return p.stdout.strip()


@pytest.fixture
def gitrepo(fake, tmp_path):
    """Local bare origin + workspace clone with main and a fix/<id> branch
    carrying a change plus a _harness/<id>/ bundle."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", "-q", str(origin), str(ws)], check=True)
    _git(ws, "config", "user.email", "t@test")
    _git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("base\n")
    _git(ws, "add", "-A"); _git(ws, "commit", "-qm", "base")
    _git(ws, "push", "-q", "origin", "main")

    rid = "2026-01-01-t"
    _git(ws, "checkout", "-qb", f"fix/{rid}")
    (ws / "feature.txt").write_text("the fix\n")
    hd = ws / "_harness" / rid
    hd.mkdir(parents=True)
    (hd / "swarm-report.json").write_text(
        '{"status": "green", "fixes_committed": 1, "waves": 2}')
    _git(ws, "add", "-A"); _git(ws, "commit", "-qm", "fix")
    _git(ws, "push", "-q", "origin", f"fix/{rid}")
    _git(ws, "checkout", "-q", "main")
    return ws, rid


def test_stage4_summarize_writes_marker_never_merges(gitrepo, capsys):
    ws, rid = gitrepo
    assert stage4.do_summarize(rid, str(ws), "main", dry_run=False) == 0
    marker = json.loads(Path(stage4._marker_path(rid)).read_text())
    assert marker["applied"] is False
    assert marker["fix_sha"] == _git(ws, "rev-parse", f"origin/fix/{rid}")
    assert marker["summary"]["swarm_report"]["status"] == "green"
    # never merges: origin/main still lacks the fix
    _git(ws, "fetch", "-q", "origin")
    tree = _git(ws, "ls-tree", "--name-only", "origin/main")
    assert "feature.txt" not in tree


def test_stage4_apply_merges_drops_harness_and_pushes(gitrepo, capsys):
    ws, rid = gitrepo
    stage4.do_summarize(rid, str(ws), "main", dry_run=False)
    assert stage4.do_apply(rid, str(ws), "main", dry_run=False) == 0
    _git(ws, "fetch", "-q", "origin")
    tree = _git(ws, "ls-tree", "-r", "--name-only", "origin/main")
    assert "feature.txt" in tree          # merged and pushed
    assert f"_harness/{rid}" not in tree  # bundle dropped
    marker = json.loads(Path(stage4._marker_path(rid)).read_text())
    assert marker["applied"] is True and marker["applied_fix_sha"]


def test_stage4_apply_refuses_stale_marker(gitrepo, capsys):
    ws, rid = gitrepo
    stage4.do_summarize(rid, str(ws), "main", dry_run=False)
    # the fix branch moves between summarise and apply
    _git(ws, "checkout", "-q", f"fix/{rid}")
    (ws / "sneaky.txt").write_text("changed after review\n")
    _git(ws, "add", "-A"); _git(ws, "commit", "-qm", "post-review change")
    _git(ws, "push", "-q", "origin", f"fix/{rid}")
    _git(ws, "checkout", "-q", "main")
    with pytest.raises(SystemExit) as e:
        stage4.do_apply(rid, str(ws), "main", dry_run=False)
    assert e.value.code == 1
    assert "STALE MARKER" in capsys.readouterr().err
    _git(ws, "fetch", "-q", "origin")
    assert "feature.txt" not in _git(ws, "ls-tree", "--name-only", "origin/main")


def test_stage4_apply_refuses_second_apply(gitrepo, capsys):
    ws, rid = gitrepo
    stage4.do_summarize(rid, str(ws), "main", dry_run=False)
    stage4.do_apply(rid, str(ws), "main", dry_run=False)
    with pytest.raises(SystemExit):
        stage4.do_apply(rid, str(ws), "main", dry_run=False)
    assert "already marked" in capsys.readouterr().err


def test_stage4_apply_without_marker_dies(gitrepo, capsys):
    ws, rid = gitrepo
    with pytest.raises(SystemExit):
        stage4.do_apply(rid, str(ws), "main", dry_run=False)
    assert "no pending-merge marker" in capsys.readouterr().err


def test_stage4_main_reinvokes_itself_under_attend_lock(fake, monkeypatch):
    recorded = {}

    def fake_with_lock(mode, cmd, *, dry_run=False):
        recorded["mode"] = mode
        recorded["cmd"] = cmd
        return 0

    monkeypatch.setattr(C, "with_lock", fake_with_lock)
    _argv(monkeypatch, "--run-id", "2026-01-01-t", "--apply")
    assert stage4.main() == 0
    assert recorded["mode"] == "attend"
    assert "--_locked" in recorded["cmd"] and "--apply" in recorded["cmd"]
