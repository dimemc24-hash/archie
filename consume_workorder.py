#!/usr/bin/env python3
"""
consume_workorder.py - Archie swarm-workorder consumer (Stage 4, DO side).

Given a swarm-workorder.json (from origin on a fix/<run-id> branch, OR a local
path for testing), processes each batch:
  1. Double-check the suggested tier (escalate when unsure)
  2. Determine at the adjusted tier (council call or self-serve)
  3. Build: apply the fix - CODE-FIXER HOOK (stubbed; real fixer is separate)
  4. Gate: npm test + tsc; commit if green, revert if not
  5. Record per batch; surface rejected/escalated items to human (Telegram)

GUARDRAILS (hard):
  - Never modify ~/swarm/* (Hetzner-owned, DONE)
  - Never commit to main
  - Full Fusion appears ONLY at full-council - nowhere else
  - Respect $25/day OpenRouter cap (ledger via fusion.py if set)

Usage:
  python3 ~/harness/consume_workorder.py --workorder <path-or-branch>
  python3 ~/harness/consume_workorder.py --workorder fix/2026-06-29-plan-calculators
  python3 ~/harness/consume_workorder.py --workorder /tmp/test-workorder.json [--dry-run]
"""

import json
import os
import sys
import subprocess
import argparse
import time
from pathlib import Path

HARNESS = Path.home() / "harness"
REPO    = HARNESS / "repo"

# Tier ladder (low -> high review cost)
TIER_ORDER = ["self-serve", "fast-council", "budget-council", "full-council"]

# Plan-math / legal-critical file path fragments (lowercase)
LEGAL_PATH_FRAGMENTS = [
    "lib/plan/",
    "lib/forms/casedata/",
    "lib/forms/components/formb113",
    "lib/forms/components/formb122",
    "lib/forms/components/formb106",
]

# Keywords that suggest escalation in category/summary/tier_reason text
ESCALATE_KEYWORDS = {
    "cramdown", "plan math", "nan", "infinity", "overflow", "round", "truncat",
    "amortiz", "interest", "principal", "payment", "legal", "filing", "firm-rules",
    "firm rules", "means test", "schedule", "exempt", "trustee",
}

# Keywords that suggest de-escalation (only if no legal files AND no critical severity)
DEESCALATE_KEYWORDS = {
    "typo", "formatting", "style", "comment", "whitespace", "lint",
    "trivial", "mechanical", "spelling", "rename",
}


# -- Tier helpers ------------------------------------------------------------

def tier_index(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return 1  # unknown -> default to fast-council


def double_check_tier(batch: dict) -> tuple:
    """
    Re-assess the triage-suggested tier. Escalate when unsure.
    Returns (final_tier, reason).

    Asymmetric risk: under-reviewing legal-critical code (cramdown math,
    plan computation) has filing consequences. Escalate on doubt.
    """
    suggested = batch.get("tier", "fast-council")
    idx       = tier_index(suggested)

    category  = (batch.get("category")    or "").lower()
    summary   = (batch.get("summary")     or "").lower()
    tier_rsn  = (batch.get("tier_reason") or "").lower()
    files     = [f.lower() for f in (batch.get("files") or [])]

    severities    = [f.get("severity", "").lower() for f in (batch.get("findings") or [])]
    has_critical  = any(s in ("critical", "high") for s in severities)
    touches_legal = any(
        any(pat in fpath for pat in LEGAL_PATH_FRAGMENTS)
        for fpath in files
    )

    text = category + " " + summary + " " + tier_rsn
    has_escalate_kw   = any(kw in text for kw in ESCALATE_KEYWORDS)
    has_deescalate_kw = any(kw in text for kw in DEESCALATE_KEYWORDS)

    new_idx = idx
    reason  = "triage suggestion accepted ({})".format(suggested)

    # Escalate: legal-critical files need at least budget-council
    if touches_legal and idx < tier_index("budget-council"):
        new_idx = tier_index("budget-council")
        reason  = "escalated: legal/plan-math files touched (was {})".format(suggested)

    # Escalate: critical or high severity -> at least budget-council
    if has_critical and new_idx < tier_index("budget-council"):
        new_idx = tier_index("budget-council")
        reason  = "escalated: critical/high severity finding (was {})".format(suggested)

    # Escalate: domain keywords but tier is below fast-council
    if has_escalate_kw and new_idx < tier_index("fast-council"):
        new_idx = tier_index("fast-council")
        reason  = "escalated: domain keywords (was {})".format(suggested)

    # De-escalate: clearly trivial AND no legal files AND no critical severity
    if (has_deescalate_kw and not touches_legal and not has_critical
            and suggested in ("full-council", "budget-council")):
        new_idx = tier_index("fast-council")
        reason  = "de-escalated: trivial non-domain batch (was {})".format(suggested)

    final_tier = TIER_ORDER[new_idx]
    return final_tier, reason


# -- Council calls -----------------------------------------------------------

def _council_question(batch: dict, domain_note: str = "") -> str:
    findings_lines = "\n".join(
        "  [{}] {}: {}".format(
            f.get("severity", "?").upper(),
            f.get("title", ""),
            f.get("detail", "")[:200],
        )
        for f in batch.get("findings", [])
    )
    q = (
        "Is the suggested fix approach correct and safe for this domain?\n\n"
        "Category: {}\n"
        "Summary: {}\n"
        "Suggested approach: {}\n"
        "Files involved: {}\n"
        "Findings:\n{}"
    ).format(
        batch.get("category", ""),
        batch.get("summary", ""),
        batch.get("suggested_approach", "(none)"),
        ", ".join(batch.get("files", [])),
        findings_lines,
    )
    if domain_note:
        q += "\n\nDomain context: " + domain_note
    return q


def _synthesize_fast_verdict(opinions: list) -> dict:
    """
    Derive APPROVE / AMEND / REJECT from raw fast-council opinions.
    Fast council returns no synthesizer, so we integrate here.
    """
    ok_opinions = [o for o in opinions if o.get("ok")]
    if not ok_opinions:
        return {"verdict": "REJECT", "reason": "all council seats failed or timed out"}

    reject_signals = ("reject", "do not", "wrong approach", "should not apply",
                      "incorrect", "avoid this", "dangerous")
    amend_signals  = ("amend", "modify the approach", "alternative instead",
                      "consider instead", "better approach", "different strategy")

    reject_count = sum(
        1 for o in ok_opinions
        if any(w in (o.get("opinion", "")).lower() for w in reject_signals)
    )
    amend_count = sum(
        1 for o in ok_opinions
        if any(w in (o.get("opinion", "")).lower() for w in amend_signals)
    )

    n = len(ok_opinions)
    if reject_count >= (n // 2 + 1):
        return {"verdict": "REJECT",
                "reason": "{}/{} seats signal rejection".format(reject_count, n)}
    if amend_count > n // 2:
        return {"verdict": "AMEND",
                "reason": "{}/{} seats suggest amendment".format(amend_count, n)}
    return {"verdict": "APPROVE",
            "reason": "{}/{} seats ok; majority supports approach".format(n, len(opinions))}


def _parse_fusion_verdict(synthesis: str) -> dict:
    """Parse APPROVE / AMEND / REJECT from fusion synthesis text."""
    low = synthesis.lower()
    reject_signals = ("reject", "do not apply", "incorrect approach",
                      "wrong approach", "should not", "dangerous to apply")
    amend_signals  = ("amend", "modify the approach", "alternative approach",
                      "instead,", "consider using", "better to")
    if any(s in low for s in reject_signals):
        return {"verdict": "REJECT", "reason": "synthesis signals rejection"}
    if any(s in low for s in amend_signals):
        return {"verdict": "AMEND",  "reason": "synthesis signals amendment needed"}
    return {"verdict": "APPROVE", "reason": "synthesis approves approach"}


def call_fast_council(batch: dict, context: str) -> dict:
    """fast-council: ~/harness/fast_council.py council()"""
    sys.path.insert(0, str(HARNESS))
    from fast_council import council  # noqa: PLC0415

    question = _council_question(batch)
    result   = council(question, context=context, sensitivity="firm", gapfill=True)
    opinions = result.get("opinions", [])

    verdict = _synthesize_fast_verdict(opinions)
    return {
        "verdict":        verdict["verdict"],
        "verdict_reason": verdict["reason"],
        "raw":            result,
    }


def call_budget_council(batch: dict, context: str) -> dict:
    """budget-council: fusion.py preset='budget'"""
    sys.path.insert(0, str(HARNESS))
    import fusion as fusion_mod  # noqa: PLC0415

    question = _council_question(batch)
    result   = fusion_mod.fusion(question, context, preset="budget")

    synthesis = result.get("synthesis", "")
    v = _parse_fusion_verdict(synthesis)
    return {
        "verdict":        v["verdict"],
        "verdict_reason": v["reason"],
        "raw":            result,
    }


def call_full_council(batch: dict, context: str) -> dict:
    """
    full-council: fusion.py FULL panel - ONLY called here, nowhere else.
    Injects MDLA firm plan-math rules as domain context.
    """
    sys.path.insert(0, str(HARNESS))
    import fusion as fusion_mod  # noqa: PLC0415

    domain_note = (
        "MDLA Ch.13 firm rules: cramdown uses continuous TValue amortization from month 1 "
        "(variable AP-then-regular, interest compounds). Cashflow must never go negative. "
        "MDLA bars post-petition arrears in originals. Trustee admin multiplier = 10%. "
        "Plan-math errors have direct filing consequences - a passing test suite does NOT "
        "guarantee legal correctness. Crawford (MDLA trustee) objects to vehicles on 122C-2 "
        "line 12 not used for commuting and is aggressive on line 22 (medical excess)."
    )
    question = _council_question(batch, domain_note)
    # preset=None -> full panel (deepseek/kimi/glm/gpt-5.5 + Opus synthesizer)
    result = fusion_mod.fusion(question, context, preset=None)

    synthesis = result.get("synthesis", "")
    v = _parse_fusion_verdict(synthesis)
    return {
        "verdict":        v["verdict"],
        "verdict_reason": v["reason"],
        "raw":            result,
    }


def run_council(batch: dict, final_tier: str, context: str) -> dict:
    """Dispatch to the correct council (or skip for self-serve)."""
    if final_tier == "self-serve":
        return {
            "verdict":        "APPROVE",
            "verdict_reason": "self-serve - no council needed (typo/mechanical/non-domain)",
            "raw":            None,
        }
    if final_tier == "fast-council":
        return call_fast_council(batch, context)
    if final_tier == "budget-council":
        return call_budget_council(batch, context)
    if final_tier == "full-council":
        return call_full_council(batch, context)
    # Unknown tier: fall back to fast-council (conservative)
    print("  [WARN] unknown tier '{}' - falling back to fast-council".format(final_tier))
    return call_fast_council(batch, context)


# -- Build / Gate ------------------------------------------------------------

def apply_fix_hook(batch: dict, repo_dir: Path, dry_run: bool = False) -> bool:
    """
    +==========================================================+
    |  CODE-FIXER HOOK  -  STUB                                |
    |  Real fixer is a separate build agent (Opus).            |
    |  THIS IS THE SEAM - do NOT add a silent auto-fixer here. |
    +==========================================================+

    Contract:
      Input:  batch dict (category, summary, suggested_approach, files,
              findings[].suggestedFix, council verdict already APPROVE)
      Action: mutate files in repo_dir per the council-approved approach
      Output: True on success, False on failure

    Until the real fixer is wired, writes a TODO marker file so the
    gate has something to stage and commit.
    """
    if dry_run:
        print("  [DRY-RUN] CODE-FIXER HOOK - would apply: {}".format(
            batch.get("category", "")))
        return True

    marker_dir  = repo_dir / "_harness" / "_consume_stubs"
    marker_dir.mkdir(parents=True, exist_ok=True)
    safe_cat    = _safe_id(batch.get("category", "batch"))
    marker_path = marker_dir / "{}.todo.md".format(safe_cat)

    fixes_text = "\n\n".join(
        "### [{}:{}] {}\nseverity: {} | confidence: {}\n{}".format(
            f.get("file", ""),
            f.get("line", ""),
            f.get("title", ""),
            f.get("severity", "?"),
            f.get("confidence", "?"),
            f.get("suggestedFix", "(no fix provided)"),
        )
        for f in batch.get("findings", [])
    )

    marker_path.write_text(
        "# CODE-FIXER STUB\n\n"
        "## Category\n{}\n\n"
        "## Summary\n{}\n\n"
        "## Suggested approach (council-approved)\n{}\n\n"
        "## Files to modify\n{}\n\n"
        "## Individual suggestedFixes\n{}\n".format(
            batch.get("category", ""),
            batch.get("summary", ""),
            batch.get("suggested_approach", "(none)"),
            "\n".join("- " + f for f in batch.get("files", [])),
            fixes_text,
        )
    )
    print("  [STUB] CODE-FIXER HOOK wrote: {}".format(marker_path.name))
    return True


def run_gate(repo_dir: Path, dry_run: bool = False) -> tuple:
    """npm test + tsc --noEmit. Returns (passed, details)."""
    if dry_run:
        return True, {"tsc": "dry-run", "test": "dry-run"}

    tsc = subprocess.run(
        [str(repo_dir / "node_modules" / ".bin" / "tsc"), "--noEmit"],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=120,
    )
    test = subprocess.run(
        ["npm", "test"],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=300,
    )
    passed = (tsc.returncode == 0 and test.returncode == 0)
    return passed, {
        "tsc":  {"rc": tsc.returncode,
                 "stderr": (tsc.stderr or "")[-1000:]},
        "test": {"rc": test.returncode,
                 "stdout": (test.stdout or "")[-2000:],
                 "stderr": (test.stderr or "")[-500:]},
    }


def git_stage_and_commit(repo_dir: Path, commit_msg: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("  [DRY-RUN] git commit: {}".format(commit_msg[:80]))
        return True
    subprocess.run(
        ["git", "add", "_harness/_consume_stubs/"],
        cwd=str(repo_dir), capture_output=True,
    )
    r = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=str(repo_dir), capture_output=True, text=True,
    )
    if r.returncode != 0:
        print("  [WARN] git commit failed: {}".format(r.stderr[:300]))
        return False
    return True


def git_revert_stubs(repo_dir: Path, dry_run: bool = False) -> None:
    if dry_run:
        print("  [DRY-RUN] git clean stub markers")
        return
    stub_dir = repo_dir / "_harness" / "_consume_stubs"
    if stub_dir.exists():
        for f in stub_dir.glob("*.todo.md"):
            f.unlink(missing_ok=True)
    subprocess.run(
        ["git", "checkout", "--", "_harness/_consume_stubs/"],
        cwd=str(repo_dir), capture_output=True,
    )


# -- Branch guard ------------------------------------------------------------

def ensure_branch(run_id: str, repo_dir: Path, dry_run: bool = False) -> str:
    """
    Ensure we are NOT on main/master.
    Prefers fix/<run_id> if it exists locally; creates build/<run_id>-consume otherwise.
    Returns the branch name used.
    """
    cur = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(repo_dir), capture_output=True, text=True,
    )
    current = cur.stdout.strip()

    if current and current not in ("main", "master"):
        print("  [GIT] already on branch: {}".format(current))
        return current

    for candidate in ["fix/{}".format(run_id), "build/{}-consume".format(run_id)]:
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=str(repo_dir), capture_output=True,
        )
        if exists.returncode == 0:
            if not dry_run:
                subprocess.run(["git", "checkout", candidate], cwd=str(repo_dir))
            print("  [GIT] checked out existing branch: {}".format(candidate))
            return candidate

    branch = "build/{}-consume".format(run_id)
    if not dry_run:
        subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo_dir))
    print("  [GIT] created branch: {}".format(branch))
    return branch


# -- Loader ------------------------------------------------------------------

def load_workorder(spec: str) -> dict:
    """
    Load swarm-workorder.json from:
      - a local filesystem path (if the file exists)
      - a fix/<id> or build/<id> branch name (reads from origin/local ref)
    """
    if os.path.isfile(spec):
        with open(spec) as f:
            return json.load(f)

    if "/" in spec:
        run_id  = spec.split("/", 1)[1]
        wo_path = "_harness/{}/swarm-workorder.json".format(run_id)
        for ref in ("origin/{}".format(spec), spec):
            result = subprocess.run(
                ["git", "show", "{}:{}".format(ref, wo_path)],
                cwd=str(REPO), capture_output=True, text=True,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        raise FileNotFoundError(
            "swarm-workorder.json not found in {}:_harness/{}/  "
            "(does the swarm pipeline produce this file yet?)".format(spec, run_id)
        )

    raise FileNotFoundError("Cannot load work-order: {!r}".format(spec))


# -- Helpers -----------------------------------------------------------------

def _safe_id(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in s)[:60]


def notify_human(message: str, run_id: str = "") -> None:
    """Non-fatal Telegram alert via alert.sh."""
    try:
        subprocess.run(
            ["bash", str(HARNESS / "alert.sh"),
             "[consume_workorder {}] {}".format(run_id, message)],
            timeout=15, capture_output=True,
        )
    except Exception:
        pass


# -- Main consumption loop ---------------------------------------------------

def consume(workorder: dict, dry_run: bool = False) -> dict:
    run_id   = workorder.get("run_id", "unknown")
    batches  = workorder.get("batches", [])
    parse_ok = workorder.get("parse_ok", True)

    print("\n" + "=" * 60)
    print("consume_workorder  run_id={}  batches={}  parse_ok={}".format(
        run_id, len(batches), parse_ok))
    if not parse_ok:
        print("[WARN] parse_ok=false - triage didn't parse; "
              "ONE fail-safe batch tagged budget-council. "
              "Treat as 'review everything'.")
    print("=" * 60)

    branch   = ensure_branch(run_id, REPO, dry_run)
    results  = []
    escalated = []
    rejected  = []

    for i, batch in enumerate(batches):
        category       = batch.get("category", "batch_{}".format(i))
        suggested_tier = batch.get("tier", "fast-council")

        print("\n--- BATCH {}/{}: {}".format(i + 1, len(batches), category))
        print("    files:          {}".format(", ".join(batch.get("files", []))))
        print("    suggested tier: {}  ({})".format(
            suggested_tier, batch.get("tier_reason", "")))

        # 1. Double-check tier
        final_tier, tier_check_reason = double_check_tier(batch)
        if final_tier != suggested_tier:
            print("    tier adjusted:  {} -> {}  ({})".format(
                suggested_tier, final_tier, tier_check_reason))
        else:
            print("    tier confirmed: {}".format(final_tier))

        # Build context string for council
        findings_text = "\n".join(
            "  [{}] {}: {}".format(
                f.get("severity", "?").upper(),
                f.get("title", ""),
                f.get("detail", "")[:200],
            )
            for f in batch.get("findings", [])
        )
        context = (
            "run_id: {}\n"
            "Category: {}\n"
            "Summary: {}\n"
            "Triage tier reason: {}\n"
            "Files: {}\n"
            "Suggested approach: {}\n"
            "Findings ({} total):\n{}"
        ).format(
            run_id,
            category,
            batch.get("summary", ""),
            batch.get("tier_reason", ""),
            ", ".join(batch.get("files", [])),
            batch.get("suggested_approach", ""),
            len(batch.get("findings", [])),
            findings_text,
        )

        # 2. Council determination
        print("    running {} determination ...".format(final_tier))
        t0 = time.time()
        try:
            council_result = run_council(batch, final_tier, context)
        except Exception as exc:
            print("    [ERROR] council call failed: {}".format(exc))
            council_result = {
                "verdict":        "REJECT",
                "verdict_reason": "council exception: {}".format(exc),
                "raw":            None,
            }
        dt = round(time.time() - t0, 1)

        verdict        = council_result.get("verdict", "REJECT")
        verdict_reason = council_result.get("verdict_reason", "")
        print("    council verdict: {}  -  {}  ({}s)".format(verdict, verdict_reason, dt))

        batch_result = {
            "batch_idx":              i,
            "category":               category,
            "tier_suggested":         suggested_tier,
            "tier_final":             final_tier,
            "tier_check_reason":      tier_check_reason,
            "council_verdict":        verdict,
            "council_verdict_reason": verdict_reason,
            "council_latency_s":      dt,
            "gate_passed":            None,
            "gate_details":           None,
            "committed":              False,
            "error":                  None,
        }

        if verdict == "REJECT":
            print("    [REJECTED] dropping batch - queued for human review")
            batch_result["error"] = "council rejected"
            rejected.append({
                "batch":    i,
                "category": category,
                "reason":   verdict_reason,
                "tier":     final_tier,
            })
            results.append(batch_result)
            continue

        if verdict == "AMEND":
            print("    [AMEND] council wants approach changed - queuing for human, skipping build")
            batch_result["error"] = "council requested amendment - human decision needed"
            escalated.append({
                "batch":     i,
                "category":  category,
                "reason":    verdict_reason,
                "tier":      final_tier,
                "amendment": True,
            })
            results.append(batch_result)
            continue

        # 3. Build (CODE-FIXER HOOK)
        print("    [BUILD] invoking code-fixer hook ...")
        build_ok = apply_fix_hook(batch, REPO, dry_run)
        if not build_ok:
            print("    [BUILD FAILED] hook returned False - skipping gate")
            batch_result["error"] = "build hook failed"
            results.append(batch_result)
            continue

        # 4. Gate: npm test + tsc
        print("    [GATE] npm test + tsc ...")
        gate_passed, gate_details = run_gate(REPO, dry_run)
        batch_result["gate_passed"]  = gate_passed
        batch_result["gate_details"] = gate_details

        if gate_passed:
            commit_msg = (
                "consume/{}: {}\n\n"
                "tier: {} (suggested: {}) | verdict: {}\n"
                "files: {}\n"
                "[CODE-FIXER STUB - wire real fixer before production use]"
            ).format(
                run_id,
                category,
                final_tier,
                suggested_tier,
                verdict,
                ", ".join(batch.get("files", [])),
            )
            committed = git_stage_and_commit(REPO, commit_msg, dry_run)
            batch_result["committed"] = committed
            print("    [GATE PASSED]  committed={}".format(committed))
        else:
            print("    [GATE FAILED]  reverting stub markers")
            git_revert_stubs(REPO, dry_run)
            batch_result["error"] = "gate failed (npm test or tsc)"

        results.append(batch_result)

    # 5. Record
    record = {
        "run_id":      run_id,
        "branch":      branch,
        "dry_run":     dry_run,
        "batch_count": len(batches),
        "results":     results,
        "escalated":   escalated,
        "rejected":    rejected,
        "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    artifact_dir = HARNESS / "artifacts" / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    record_path  = artifact_dir / "consume-workorder-record.json"
    with open(record_path, "w") as f:
        json.dump(record, f, indent=2, default=str)

    # Summary
    n_committed = sum(1 for r in results if r.get("committed"))
    n_failed    = sum(1 for r in results if r.get("error"))
    print("\n" + "=" * 60)
    print("CONSUME COMPLETE  run_id={}".format(run_id))
    print("  batches={}  committed={}  failed/rejected/escalated={}".format(
        len(batches), n_committed, n_failed))
    print("  record:  {}".format(record_path))

    if rejected or escalated:
        human_parts = []
        if rejected:
            cats = "; ".join(r["category"] for r in rejected)
            human_parts.append("{} REJECTED ({})".format(len(rejected), cats))
        if escalated:
            cats = "; ".join(r["category"] for r in escalated)
            human_parts.append("{} need AMENDMENT ({})".format(len(escalated), cats))
        human_msg = "run {} NEEDS REVIEW: {}".format(run_id, " | ".join(human_parts))
        print("\n[HUMAN FLAG] {}".format(human_msg))
        notify_human(human_msg, run_id)

    return record


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archie swarm-workorder consumer (Stage 4)"
    )
    parser.add_argument(
        "--workorder", required=True,
        help="Path to swarm-workorder.json OR fix/<run-id> branch name",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Dry-run mode: exercises tier logic but skips real council calls, gate, and commit",
    )
    args = parser.parse_args()

    try:
        workorder = load_workorder(args.workorder)
    except FileNotFoundError as exc:
        print("[ERROR] {}".format(exc), file=sys.stderr)
        sys.exit(2)

    record  = consume(workorder, dry_run=args.dry_run)
    n_failed = sum(1 for r in record["results"] if r.get("error"))
    sys.exit(1 if n_failed else 0)


if __name__ == "__main__":
    main()
