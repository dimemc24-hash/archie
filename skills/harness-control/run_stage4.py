#!/usr/bin/env python3
"""
run_stage4 — Stage 4 of the dev harness (review + merge of fix/<run-id>),
callable as a skill.

Unlike Stages 1-3, Stage 4 has NO existing underlying script to wrap — it IS the
underlying script. Per the prior-investigation notes: "Stage 4: no dedicated
script exists." Morley does this manually today; ``notify_stage4.sh`` pings him
when a new ``fix/<id>`` lands on origin. This skill wraps the deterministic
mechanical parts (fetch fix branch, show diff/artifact summary, merge to main,
drop ``_harness/``) behind a confirmation gate so Archie's own agent loop can
drive it instead of requiring an external SSH actor.

CONFIRMATION GATE — two-step dry-run/apply split (council decision, checkpoint
``stage4-confirm-design``):

  DEFAULT (no --apply):
    1. Fetch ``origin/fix/<run-id>`` into the target workspace checkout.
    2. Compute a diff/artifact summary: diffstat vs the merge base on main,
       file count, swarm-report.json highlights, checkpoint-log.json flags.
    3. Write a **pending-merge marker** file (keyed to run-id, fix branch SHA,
       and a digest of the diffstat) to ``~/harness/artifacts/<run-id>/``.
    4. Print the summary and the marker path. NEVER merge.

  --apply:
    1. Read the pending-merge marker.
    2. Re-validate: refetch origin/fix/<run-id>, recompute the SHA + diffstat
       digest, and refuse if the marker is stale or mismatched (the branch
       changed between the two calls, or the marker is from an aborted run).
    3. Checkout main, merge fix/<run-id> (--no-ff so the merge commit is
       explicit), drop the ``_harness/`` directory from the merged tree, amend
       the merge commit to drop it (or commit the removal — see implementation).
    4. Push main to origin.

WHY TWO STEPS, not a prompt or a --confirm flag:
  * An interactive prompt doesn't work in a non-TTY agent tool-call loop and
    is trivially satisfied via piped stdin.
  * A same-call --confirm flag is just as easy for an agent to self-supply as
    the action it's meant to gate.
  * The two-step split forces a hard boundary between 'summarise' and 'mutate'
    as two SEPARATE tool invocations, produces a concrete artifact (the marker)
    that documents what was reviewed and when, and gives Morley (or any wrapping
    orchestration/policy layer) a real point to inspect before the second call
    happens. It does NOT enforce a hard access boundary — an agent with general
    shell access can call both steps in the same loop. True enforcement of human
    review depends on controls outside this script (restricting which
    invocations are exposed to the agent, or platform-level review gates). The
    marker-file split reduces accidental/same-turn auto-merge risk and creates
    an auditable review boundary; it is not a complete authority boundary.

Runs under the "attend" DO lock (Stage 4 is an attended merge, not a build).

Usage:
  run_stage4.py --run-id YYYY-MM-DD-slug [--profile NAME] [--dry-run]
      # default: fetch + summarise + write pending-merge marker (never merges)

  run_stage4.py --run-id YYYY-MM-DD-slug --apply [--profile NAME] [--dry-run]
      # reads marker, re-validates, merges to main, drops _harness/

  run_stage4.py --run-id YYYY-MM-DD-slug --marker-only
      # print the path to the pending-merge marker and exit (for inspection)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402


# -- git helpers (local to this skill — Stage 4 has no underlying script) -----

def _git(args: list[str], cwd: str, timeout: int = 300) -> tuple[int, str, str]:
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _git_ok(args: list[str], cwd: str, timeout: int = 300) -> str:
    """Run git, die on failure, return stripped stdout."""
    rc, out, err = _git(args, cwd, timeout=timeout)
    if rc != 0:
        C.die(f"git {' '.join(args)} failed in {cwd}: {err or out}")
    return out


# -- pending-merge marker -----------------------------------------------------

def _marker_path(run_id: str) -> str:
    art_dir = os.path.join(C.ARTIFACTS, run_id)
    os.makedirs(art_dir, exist_ok=True)
    return os.path.join(art_dir, "pending-merge.json")


def _diffstat_digest(diffstat: str) -> str:
    """Short hash of the diffstat so the apply step can detect a changed branch."""
    return hashlib.sha256(diffstat.encode()).hexdigest()[:16]


def _write_marker(marker_path: str, run_id: str, fix_sha: str, fix_short: str,
                  diffstat: str, summary: dict, repo_dir: str) -> None:
    rec = {
        "run_id": run_id,
        "fix_branch": f"fix/{run_id}",
        "fix_sha": fix_sha,
        "fix_short": fix_short,
        "diffstat_digest": _diffstat_digest(diffstat),
        "summary": summary,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo_dir": repo_dir,
        "applied": False,
    }
    with open(marker_path, "w") as f:
        json.dump(rec, f, indent=2)
    # Restrict perms — marker is an audit artifact.
    try:
        os.chmod(marker_path, 0o600)
    except OSError:
        pass


def _read_marker(marker_path: str) -> dict:
    try:
        with open(marker_path) as f:
            return json.load(f)
    except FileNotFoundError:
        C.die(f"no pending-merge marker at {marker_path}. "
              f"Run run_stage4.py --run-id <id> (without --apply) first to "
              f"fetch, summarise, and write the marker.")
    except json.JSONDecodeError as e:
        C.die(f"pending-merge marker at {marker_path} is corrupt: {e}")


# -- summary computation ------------------------------------------------------

def _summarize_fix(run_id: str, repo_dir: str, base: str) -> dict:
    """Compute the diff/artifact summary for fix/<run-id> vs the merge base on main.

    Returns a dict with: diffstat, file_count, merge_base, swarm_report,
    checkpoint_flags. Does NOT mutate anything — read-only git operations only.
    """
    fix_ref = f"origin/fix/{run_id}"

    # Ensure we have the latest fix branch.
    _git_ok(["fetch", "origin", f"fix/{run_id}"], repo_dir, timeout=120)

    fix_sha = _git_ok(["rev-parse", fix_ref], repo_dir)
    fix_short = _git_ok(["rev-parse", "--short", fix_ref], repo_dir)

    # Find the merge base between fix and main (where the fix branched off).
    mb_rc, merge_base, mb_err = _git(["merge-base", base, fix_ref], repo_dir)
    if mb_rc != 0:
        # base might not exist locally yet; fetch it.
        _git_ok(["fetch", "origin", base], repo_dir, timeout=120)
        merge_base = _git_ok(["merge-base", base, fix_ref], repo_dir)

    # Diffstat: fix vs merge-base (what the fix actually changed).
    rc, diffstat, _ = _git(
        ["diff", "--stat", merge_base, fix_ref], repo_dir, timeout=120,
    )
    if rc != 0:
        diffstat = "(unable to compute diffstat)"

    rc, name_only, _ = _git(
        ["diff", "--name-only", merge_base, fix_ref], repo_dir, timeout=120,
    )
    files = [l.strip() for l in name_only.splitlines() if l.strip()] if rc == 0 else []

    # Read swarm-report.json + checkpoint-log.json from the fix branch's
    # _harness/<id>/ (same approach as notify_stage4.sh).
    swarm_report = _read_branch_json(
        repo_dir, fix_ref, f"_harness/{run_id}/swarm-report.json"
    )
    checkpoint_log = _read_branch_json(
        repo_dir, fix_ref, f"_harness/{run_id}/checkpoint-log.json"
    )

    cp_flags = _checkpoint_flags(checkpoint_log)

    return {
        "fix_sha": fix_sha,
        "fix_short": fix_short,
        "merge_base": merge_base[:12],
        "diffstat": diffstat,
        "file_count": len(files),
        "files": files[:50],  # cap for the marker
        "swarm_report": swarm_report,
        "checkpoint_flags": cp_flags,
    }


def _read_branch_json(repo_dir: str, ref: str, path: str) -> dict | None:
    """Read a JSON file from a git branch (git show <ref>:<path>), or None."""
    rc, out, _ = _git(["show", f"{ref}:{path}"], repo_dir, timeout=60)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _checkpoint_flags(cklog) -> dict:
    """Distill checkpoint-log.json into human-readable flags (matches
    notify_stage4.sh's logic)."""
    if not cklog:
        return {}
    entries = cklog if isinstance(cklog, list) else []
    fallbacks = [e.get("checkpoint") for e in entries
                 if e.get("fallback") and e.get("checkpoint")]
    blindspots = []
    for e in entries:
        b = e.get("blindspots")
        if isinstance(b, list):
            blindspots += [str(x)[:90] for x in b if x]
        elif b:
            blindspots.append(str(b)[:90])
    coverage_failures = [e.get("unconsulted") for e in entries
                         if e.get("checkpoint") == "_coverage_failure"]
    return {
        "fallbacks": fallbacks,
        "blindspots": blindspots[:3],
        "coverage_failures": coverage_failures,
        "n_entries": len(entries),
    }


def _print_summary(run_id: str, s: dict) -> None:
    print()
    print("=" * 60)
    print(f"  Stage 4 review summary: fix/{run_id}")
    print("=" * 60)
    print(f"  fix SHA:     {s['fix_short']} ({s['fix_sha'][:12]}...)")
    print(f"  merge base:  {s['merge_base']}")
    print(f"  files changed: {s['file_count']}")
    print()
    print("  diffstat (fix vs merge-base):")
    for line in s["diffstat"].splitlines():
        print(f"    {line}")
    print()

    sr = s.get("swarm_report")
    if sr:
        print(f"  swarm report: status={sr.get('status','?')}  "
              f"fixes={sr.get('fixes_committed','?')}  "
              f"waves={sr.get('waves','?')}")
    else:
        print("  swarm report: (not found — not a harness fix branch?)")

    cf = s.get("checkpoint_flags") or {}
    if cf.get("fallbacks"):
        print(f"  ⚠️  FALLBACK checkpoints (no council): "
              f"{', '.join(cf['fallbacks'])}")
    if cf.get("blindspots"):
        print(f"  ⚠️  blindspots: {cf['blindspots'][0]}")
    if cf.get("coverage_failures"):
        print(f"  ⚠️  COVERAGE FAILURES: {cf['coverage_failures']}")
    if cf.get("n_entries"):
        print(f"  checkpoint-log: {cf['n_entries']} entries")

    files = s.get("files") or []
    if files:
        shown = files[:20]
        more = len(files) - len(shown)
        print()
        print(f"  changed files (first {len(shown)})"
              f"{f', +{more} more' if more else ''}:")
        for fn in shown:
            print(f"    {fn}")
    print("=" * 60)


# -- default invocation: fetch + summarise + write marker ----------------------

def do_summarize(run_id: str, repo_dir: str, base: str, dry_run: bool) -> int:
    if dry_run:
        print("[dry-run] would: fetch origin/fix/<run-id>, compute diff/artifact "
              "summary, write pending-merge marker to "
              f"{_marker_path(run_id)}")
        return 0

    summary = _summarize_fix(run_id, repo_dir, base)
    _print_summary(run_id, summary)

    marker = _marker_path(run_id)
    _write_marker(
        marker, run_id,
        summary["fix_sha"], summary["fix_short"],
        summary["diffstat"], summary, repo_dir,
    )
    print()
    print(f"[harness-control] Stage 4 summary done for {run_id}.")
    print(f"  pending-merge marker: {marker}")
    print(f"  Review it, then apply the merge with:")
    print(f"    run_stage4.py --run-id {run_id} --apply")
    return 0


# -- --apply invocation: re-validate marker, merge, drop _harness/ -------------

def do_apply(run_id: str, repo_dir: str, base: str, dry_run: bool) -> int:
    marker_path = _marker_path(run_id)
    marker = _read_marker(marker_path)

    if marker.get("applied"):
        C.die(f"pending-merge marker at {marker_path} is already marked "
              f"'applied' (applied at {marker.get('applied_at','?')}). "
              f"This merge was already done. Delete the marker manually only if "
              f"you are certain you need to re-merge.")

    if dry_run:
        print("[dry-run] would: re-validate pending-merge marker, merge "
              f"fix/{run_id} into {base} (--no-ff), drop _harness/, "
              f"push {base} to origin")
        return 0

    # 1. Re-validate: refetch and recompute SHA + diffstat digest.
    print(f"[harness-control] re-validating pending-merge marker for {run_id}...")
    fresh = _summarize_fix(run_id, repo_dir, base)

    if fresh["fix_sha"] != marker["fix_sha"]:
        C.die(
            f"STALE MARKER: pending-merge marker records fix SHA "
            f"{marker['fix_sha'][:12]}, but origin/fix/{run_id} is now at "
            f"{fresh['fix_sha'][:12]}. The fix branch changed between the "
            f"summarise and apply calls — refusing to merge. Re-run "
            f"run_stage4.py --run-id {run_id} (without --apply) to refresh "
            f"the marker."
        )

    fresh_digest = _diffstat_digest(fresh["diffstat"])
    if fresh_digest != marker["diffstat_digest"]:
        C.die(
            f"STALE MARKER: diffstat digest changed "
            f"(marker={marker['diffstat_digest']}, "
            f"current={fresh_digest}). The fix branch's diff changed between "
            f"the summarise and apply calls — refusing to merge. Re-run "
            f"run_stage4.py --run-id {run_id} (without --apply) to refresh "
            f"the marker."
        )

    print(f"[harness-control] marker validated: fix/{run_id} @ "
          f"{fresh['fix_short']} matches the reviewed state.")

    # 2. Checkout base (main), ensure it's up to date.
    _git_ok(["fetch", "origin", base], repo_dir, timeout=120)
    _git_ok(["checkout", base], repo_dir)
    _git_ok(["reset", "--hard", f"origin/{base}"], repo_dir)

    # 3. Merge fix/<run-id> with --no-ff (explicit merge commit).
    _git_ok(["merge", "--no-ff", f"origin/fix/{run_id}",
             "-m", f"stage4 merge: fix/{run_id} into {base}\n\n"
                   f"fix SHA: {fresh['fix_sha']}\n"
                   f"merge base: {fresh['merge_base']}\n"
                   f"files changed: {fresh['file_count']}"],
            repo_dir, timeout=300)

    # 4. Drop _harness/ from the merged tree.
    harness_dir = os.path.join(repo_dir, "_harness")
    if os.path.isdir(harness_dir):
        # Remove just this run-id's _harness subdir (other runs' artifacts may
        # coexist if multiple fix branches are merged sequentially). The design
        # says "drop _harness/" — but dropping the entire dir would nuke other
        # runs' spec bundles. We drop _harness/<run-id>/ — the run-specific
        # artifacts the swarm produced. If _harness/ becomes empty afterward,
        # remove it too.
        run_harness = os.path.join(harness_dir, run_id)
        if os.path.isdir(run_harness):
            _git_ok(["rm", "-r", "--ignore-unmatch",
                     f"_harness/{run_id}"], repo_dir)
        # git rm prunes _harness/ itself when <run-id> was its only entry,
        # so the dir may already be gone here (the common single-run case).
        if os.path.isdir(harness_dir) and not os.listdir(harness_dir):
            os.rmdir(harness_dir)
        _git_ok(["add", "-A"], repo_dir)
        if _has_staged_changes(repo_dir):
            _git_ok(
                ["commit", "-m",
                 f"stage4: drop _harness/{run_id}/ after merge"],
                repo_dir,
            )

    # 5. Push base to origin.
    rc, _, err = _git(["push", "origin", base], repo_dir, timeout=300)
    if rc != 0:
        C.alert(f"⚠️ Stage 4 merge succeeded but push failed for {run_id}: {err}")
        C.die(f"push of {base} to origin failed: {err}. The merge is local "
              f"only — resolve the push issue manually (the merge commit is "
              f"in {repo_dir} on {base}).")

    # 6. Mark the marker as applied (audit trail).
    marker["applied"] = True
    marker["applied_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    marker["applied_fix_sha"] = fresh["fix_sha"]
    with open(marker_path, "w") as f:
        json.dump(marker, f, indent=2)

    print()
    print(f"[harness-control] Stage 4 DONE for {run_id}.")
    print(f"  merged fix/{run_id} ({fresh['fix_short']}) into {base} and pushed.")
    print(f"  _harness/{run_id}/ dropped from the merged tree.")
    print(f"  marker (applied): {marker_path}")
    return 0


def _has_staged_changes(repo_dir: str) -> bool:
    rc, out, _ = _git(["diff", "--cached", "--name-only"], repo_dir)
    return rc == 0 and bool(out.strip())


# -- main ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 4: review + merge fix/<run-id> (two-step gate)."
    )
    ap.add_argument("--run-id", required=True,
                    help="YYYY-MM-DD-slug (the fix/<run-id> branch to review/merge)")
    ap.add_argument("--profile", default=None,
                    help="target repo profile (profiles/<name>.json); "
                         "omit for the legacy NewChapter checkout")
    ap.add_argument("--base", default="main",
                    help="base branch to merge into (default: main)")
    ap.add_argument("--apply", action="store_true",
                    help="perform the merge: read + re-validate the pending-merge "
                         "marker, merge fix/<run-id> into --base, drop _harness/, "
                         "push. Without this flag, only fetches + summarises + "
                         "writes the marker (never merges).")
    ap.add_argument("--marker-only", action="store_true",
                    help="print the path to the pending-merge marker and exit "
                         "(for inspection by a wrapping orchestration layer)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen without doing it")
    a = ap.parse_args()

    # --marker-only and --dry-run are read-only: no lock needed.
    if a.marker_only:
        print(_marker_path(a.run_id))
        return 0

    if a.dry_run:
        repo_dir = C.resolve_repo(a.profile)
        if a.apply:
            return do_apply(a.run_id, repo_dir, a.base, dry_run=True)
        return do_summarize(a.run_id, repo_dir, a.base, dry_run=True)

    # Real work (fetch/merge/push) mutates the repo — take the "attend" lock.
    # _common.with_lock wraps a command list and runs it under do-lock.sh; we
    # re-invoke ourselves with an internal --_locked flag so the work runs
    # inside the lock. This matches how run_stage1..3 wrap their scripts (the
    # lock wraps the actual mutation), except here the "script" is us.
    cmd = [
        sys.executable, os.path.abspath(__file__),
        "--run-id", a.run_id,
        "--base", a.base,
        "--_locked",
    ]
    if a.profile:
        cmd += ["--profile", a.profile]
    if a.apply:
        cmd += ["--apply"]

    rc = C.with_lock("attend", cmd, dry_run=False)
    if rc == C.LOCK_BUSY:
        return rc
    if rc != 0:
        C.alert(f"⚠️ Stage 4 failed for {a.run_id} (rc={rc}).")
        C.die(f"Stage 4 exited {rc} for {a.run_id}.", rc)
    return rc


def _locked_main() -> int:
    """Internal entry point — called when already inside the do-lock."""
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--base", default="main")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--marker-only", action="store_true")
    ap.add_argument("--_locked", action="store_true")
    a = ap.parse_args()

    repo_dir = C.resolve_repo(a.profile)

    if a.marker_only:
        print(_marker_path(a.run_id))
        return 0

    if a.apply:
        return do_apply(a.run_id, repo_dir, a.base, dry_run=False)
    return do_summarize(a.run_id, repo_dir, a.base, dry_run=False)


if __name__ == "__main__":
    # Check for the internal --_locked flag first. If present, we're already
    # inside the do-lock and should run the real work directly.
    if "--_locked" in sys.argv:
        sys.exit(_locked_main())
    sys.exit(main())
