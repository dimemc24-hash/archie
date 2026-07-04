#!/usr/bin/env python3
"""Stage 2 of detect->triage->build: the TRIAGE BRAIN (runs on Hetzner, co-located with findings + code).
Reads the swarm's raw work-order (_harness/<id>/swarm-findings.json) and, via a budget-tier review
(Sonnet — best A/B *recall*, i.e. good at recognizing what's real), it:
  1. FILTERS real-vs-noise (the swarm's garbage dies here, not on Archie's desk)
  2. CONSOLIDATES duplicate/related findings into single issues
  3. CATEGORIZES + BATCHES the survivors into coherent work units
  4. SUGGESTS a determination tier per batch (Archie double-checks it)
Emits _harness/<id>/swarm-workorder.json for Archie. The swarm is dumb; this is the brain.

  triage.py <run-id>
Swap M_TRIAGE to a Fusion budget-preset call if you want the panel instead of a single Sonnet review.
"""
import json, os, re, subprocess, sys

SWARM = os.path.expanduser("~/swarm")
REPO  = os.path.join(SWARM, "newchapter")
M_TRIAGE = "deepseek/deepseek-v4-pro"   # budget-tier review (NOT the full council; triage is filter/sort)
PROV = ["--provider", "openrouter"]
TIERS = "self-serve | fast-council | budget-council | full-council"

def run(cmd, timeout=900):
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"

def main():
    if len(sys.argv) < 2:
        print("usage: triage.py <run-id>"); sys.exit(2)
    rid = sys.argv[1]
    hdir = os.path.join(REPO, "_harness", rid)
    outp = os.path.join(hdir, "swarm-workorder.json")
    findings = json.load(open(os.path.join(hdir, "swarm-findings.json"))).get("findings", [])
    if not findings:
        json.dump({"run_id": rid, "source_findings": 0, "batches": []}, open(outp, "w"), indent=2)
        print("[triage] no findings -> empty work-order"); return

    lines = []
    for i, f in enumerate(findings):
        lines.append(f"{i}\t[{f.get('severity')}/{f.get('kind')}/{f.get('_persona')}] "
                     f"{f.get('file')}:{f.get('line')} — {(f.get('title') or '')[:90]} :: "
                     f"{(f.get('detail') or '')[:180]}")
    prompt = (
        f"You are the TRIAGE BRAIN. {len(findings)} RAW findings were produced by cheap, noisy swarm critics "
        f"on a Louisiana bankruptcy case-management codebase. Read the cited files in this repo with your "
        f"tools to verify each. You do NOT fix anything — you triage:\n"
        f"1. FILTER: drop noise / false-positives / pure style. Keep only real, demonstrable defects.\n"
        f"2. CONSOLIDATE: merge duplicate/related findings into one issue (record their source idxs).\n"
        f"3. CATEGORIZE + BATCH: group survivors into coherent work units (by theme/subsystem/file).\n"
        f"4. TIER each batch by how much determination the FIX needs before Archie applies it:\n"
        f"   - self-serve: trivial/mechanical, non-domain (typo, dead code, comment, format)\n"
        f"   - fast-council: real but bounded, non-critical logic (UI, helpers)\n"
        f"   - budget-council: meaningful logic (data-integrity, API contracts, non-plan calc)\n"
        f"   - full-council: critical/high in DOMAIN-CRITICAL or irreversible code — plan math "
        f"(lib/plan/* incl compute/cfw/lint, means-test), forms/filings (lib/forms/*), auth/RLS, schema.\n"
        f"   The tier is a SUGGESTION; Archie double-checks and escalates when unsure.\n\n"
        f"Return ONLY a fenced ```json object:\n"
        f'{{"batches":[{{"category":str,"summary":str,"tier":"{TIERS}","tier_reason":str,'
        f'"suggested_approach":str,"finding_idxs":[int],"files":[str]}}],'
        f'"dropped_idxs":[int],"dropped_reason":str}}\n\n'
        f"FINDINGS:\n" + "\n".join(lines))

    rc, out, err = run(["hermes", "-z", prompt, "-m", M_TRIAGE, *PROV, "--yolo"])
    m = re.search(r"```json\s*(.*?)```", out, re.S) or re.search(r"(\{.*\})", out, re.S)
    try: triaged = json.loads(m.group(1)) if m else {}
    except Exception: triaged = {}
    batches = triaged.get("batches", []) or []
    for b in batches:  # resolve idxs -> the actual finding objects
        b["findings"] = [findings[i] for i in b.get("finding_idxs", [])
                         if isinstance(i, int) and 0 <= i < len(findings)]
    kept = sum(len(b.get("finding_idxs", [])) for b in batches)
    workorder = {"run_id": rid, "triage_model": M_TRIAGE, "source_findings": len(findings),
                 "kept": kept, "dropped_idxs": triaged.get("dropped_idxs", []),
                 "dropped_reason": triaged.get("dropped_reason", ""),
                 "parse_ok": bool(batches), "batches": batches}
    if not batches:  # never silently drop everything on a parse miss — hand Archie the raw findings
        workorder["batches"] = [{"category": "UNTRIAGED (triage parse failed — review raw)", "summary":
                                 "triage output unparseable; raw findings attached", "tier": "budget-council",
                                 "tier_reason": "fail-safe escalation", "suggested_approach": "manual review",
                                 "finding_idxs": list(range(len(findings))), "findings": findings, "files":
                                 sorted({f.get("file") for f in findings if f.get("file")})}]
    json.dump(workorder, open(outp, "w"), indent=2)
    print(f"[triage] {len(findings)} raw -> {len(workorder['batches'])} batches "
          f"(kept {kept}, dropped {len(workorder['dropped_idxs'])}) -> {outp}")
    for b in workorder["batches"]:
        print(f"  [{b.get('tier')}] {b.get('category')}: "
              f"{len(b.get('finding_idxs', []))} findings — {(b.get('summary') or '')[:70]}")

if __name__ == "__main__":
    main()
