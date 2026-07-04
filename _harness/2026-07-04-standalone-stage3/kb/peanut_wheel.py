#!/usr/bin/env python3
"""
Stage-3 swarm wheel driver (Shape B) — deterministic control loop; the LLM does only judgment.
Capability lanes: code critics (glm-5.2) | vision critics (Playwright capture -> glm-4.6v) |
security lurker (sonnet-5). Fixer = glm-5.2, escalate sonnet-5 on green-fail. Green-gate = tests pass
+ tsc errors subset of baseline. Read-only critics enforced via git checkout after each wave.

Usage: peanut_wheel.py --code lib/fmt.ts,lib/income/project.ts --routes /cases/<id>/income --waves 6
"""
import argparse, json, os, re, subprocess, sys, random, time, concurrent.futures as cf

REPO   = os.path.expanduser("~/swarm/newchapter")
SWARM  = os.path.expanduser("~/swarm")
BASELINE_TSC = os.path.join(SWARM, "tsc.baseline")
CREDS  = os.path.join(SWARM, "seed-creds.txt")
SHOTS  = os.path.join(SWARM, "shots")
# artifacts live OUTSIDE the repo tree — git clean/checkout (read-only enforcement) must not clobber them
LEDGER = os.path.join(SWARM, "PEANUT_LEDGER.md")
REVIEW = os.path.join(SWARM, "PEANUT_REVIEW.md")
FIXLOG = os.path.join(SWARM, "fix-log.jsonl")
PROV   = ["--provider", "openrouter"]
M_CODE, M_CODE_DEEP, M_VISION, M_SEC = (   # A/B roster: glm = cheap/high-precision; deepseek = recall+depth
    "z-ai/glm-5.2", "deepseek/deepseek-v4-pro", "z-ai/glm-4.6v", "deepseek/deepseek-v4-pro")

JSON_FENCE = re.compile(r"```json\s*(.*?)```", re.S)

def parse_findings(raw):
    m = JSON_FENCE.search(raw) or re.search(r"(\[\s*\{.*\}\s*\])", raw, re.S)
    if not m: return []
    try:
        d = json.loads(m.group(1))
        return d if isinstance(d, list) else []
    except Exception:
        return []

def run(cmd, timeout=240, cwd=REPO):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"

def git(*args):
    return run(["git", *args], timeout=120)

def git_clean():
    git("checkout", "--", "."); git("clean", "-fdq")

# ---------- critics ----------
CODE_PERSONAS = {
 "Pedant": "spec/contract exactness, off-by-one, type holes, fail-open branches",
 "EdgeCaseGoblin": "null/empty/NaN/unicode/boundary inputs nobody tested",
 "DataIntegrity": "non-transactional writes that lose data, calc-vs-stored divergence, silent numeric coercion (NaN->null, currency strings), off-by-a-factor",
 "Maintainer": "coupling, hidden state, dead code, lying comments, duplication",
}
VISION_PERSONAS = {
 "BrowserUX": "layout/overflow/alignment, contrast/readability, empty/loading/error states, label clarity, obvious a11y, anything visually broken",
}
FIND_KEYS = "severity, kind, title, file, line, detail, suggestedFix, confidence"

# (hivemind targeting removed 2026-06-29 — checkpoint-log uncertainty routes to Archie + review, NOT the
#  swarm critics. The swarm hunts blind, by design.)

def code_brief(persona, desc, files):
    return (f"You are {persona}, a hostile-but-honest READ-ONLY code critic. Lens: {desc}. "
            f"Review ONLY these in-scope files in this repo (read them with your tools): {files}. "
            f"Find the most damning REAL flaws within this scope. Do NOT edit anything. "
            f"Return EXACTLY ONE fenced json array of objects with keys: {FIND_KEYS}. "
            f"severity in [critical,high,medium,low,petty]; kind in [fix-now,needs-decision,cosmetic]; "
            f"confidence 0-1. Output ONLY the fenced json block. Clean -> [].")

def vision_brief(persona, desc, route):
    return (f"You are {persona}. The attached image is a full-page screenshot of route {route} of a "
            f"bankruptcy case-management web app, logged in as an attorney. Lens: {desc}. Judge ONLY "
            f"what is visible. Return EXACTLY ONE fenced json array of objects with keys: {FIND_KEYS} "
            f"(file/line may be null for visual findings). severity in [critical,high,medium,low,petty]; "
            f"kind in [fix-now,needs-decision,cosmetic]. Output ONLY the fenced json block. Clean -> [].")

SEC_BRIEF = ("You are The Lurker, a deep security auditor. READ-ONLY. Examine the in-scope files {files} "
   "and the app's auth/RLS boundary for buried vulns volume misses: authz/IDOR gaps, fail-open checks, "
   "secrets in logs/errors, injection, missing firm-scoping on queries. Return EXACTLY ONE fenced json "
   f"array with keys: {FIND_KEYS}. Output ONLY the fenced json block. Clean -> [].")

CODE_MODELS = {  # A/B-backed: deepseek for the deep/business-logic lenses, glm for the cheap-precise ones
    "DataIntegrity": M_CODE_DEEP, "Pedant": M_CODE_DEEP,
    "EdgeCaseGoblin": M_CODE, "Maintainer": M_CODE,
}
def spawn_code(persona, files):
    desc = CODE_PERSONAS[persona]
    model = CODE_MODELS.get(persona, M_CODE)
    rc, out, err = run(["hermes", "-z", code_brief(persona, desc, files), "-m", model, *PROV, "--yolo"])
    return persona, parse_findings(out)

def spawn_vision(persona, route):
    desc = VISION_PERSONAS[persona]
    png = os.path.join(SHOTS, "w_" + re.sub(r"[^a-z0-9]", "_", route.lower()) + ".png")
    rc, out, err = run(["node", "swarm_capture.mjs", route, png], timeout=120)
    cap = parse_findings(out)  # capture prints a status json, not findings
    if not os.path.exists(png):
        return persona, [{"severity":"high","kind":"needs-decision","title":f"route {route} did not capture",
                          "detail":out[-300:]+err[-300:],"confidence":0.9,"file":None,"line":None}]
    rc, out, err = run(["hermes", "chat", "-Q", "-q", vision_brief(persona, desc, route),
                        "--image", png, "-m", M_VISION, *PROV], timeout=180)
    return persona, parse_findings(out)

def spawn_lurker(files):
    rc, out, err = run(["hermes", "-z", SEC_BRIEF.format(files=files), "-m", M_SEC, *PROV, "--yolo"], timeout=300)
    return "Lurker", parse_findings(out)

# (fixer / green-gate / refute removed 2026-06-29 — the swarm is DETECT-ONLY; fixing + determination
#  moved to Archie behind the tiered councils. See nc-architecture/swarm/specs/detect-triage-build.md.)

# ---------- ledger ----------
def fp(f):
    return f"{(f.get('file') or '')}:{f.get('line')}:{(f.get('title') or '')[:50]}"

def log_ledger(line):
    with open(LEDGER, "a") as fh: fh.write(line + "\n")

# ---------- wheel ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="")     # comma sep files
    ap.add_argument("--routes", default="")    # comma sep routes
    ap.add_argument("--waves", type=int, default=6)
    ap.add_argument("--quiesce", type=int, default=3)   # stop after N waves with no NEW findings
    ap.add_argument("--lurker-every", type=int, default=3)
    ap.add_argument("--findings-out", default="")        # work-order path: raw findings for the triage brain
    args = ap.parse_args()
    code_files = [s for s in args.code.split(",") if s]
    routes     = [s for s in args.routes.split(",") if s]
    os.makedirs(SHOTS, exist_ok=True)
    if not os.path.exists(LEDGER): log_ledger("# PEANUT_LEDGER\n")
    seen, streak, wave = set(), 0, 0
    all_findings = []
    print(f"[wheel] DETECT-ONLY: {len(code_files)} code files, {len(routes)} routes; quiesce={args.quiesce} max_waves={args.waves}")
    while wave < args.waves and streak < args.quiesce:
        wave += 1
        tasks = []
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for p in random.sample(list(CODE_PERSONAS), min(2, len(CODE_PERSONAS))):
                if code_files: tasks.append(ex.submit(spawn_code, p, ",".join(code_files)))
            for r in routes[:1]:  # ration vision: one per wave
                tasks.append(ex.submit(spawn_vision, "BrowserUX", r))
            if wave % args.lurker_every == 0 and code_files:
                tasks.append(ex.submit(spawn_lurker, ",".join(code_files)))
            results = [t.result() for t in tasks]
        git_clean()  # critics are READ-ONLY — discard any stray edits
        fresh = []
        for persona, fs in results:
            for f in fs:
                if fp(f) in seen: continue
                seen.add(fp(f)); f["_persona"] = persona; f["_wave"] = wave
                fresh.append(f); all_findings.append(f)
                log_ledger(f"- W{wave} FOUND [{f.get('severity')}] {fp(f)} ({persona})")
        streak = 0 if fresh else streak + 1   # quiesce = no NEW findings (not "no fixes")
        print(f"[wheel] wave {wave}: {len(fresh)} new findings (total {len(all_findings)}), streak={streak}")
    # Emit the raw work-order for the TRIAGE brain. The swarm is a DUMB DETECTOR — no fixing, no
    # real-vs-noise filtering, no tiering here; the hivemind (triage.py) does all of that downstream.
    out = args.findings_out or os.path.join(SWARM, "swarm-findings.json")
    try: os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    except Exception: pass
    json.dump({"scope": code_files, "routes": routes, "waves": wave, "findings": all_findings},
              open(out, "w"), indent=2)
    print(f"[wheel] DETECTED {len(all_findings)} findings over {wave} waves -> {out}")

if __name__ == "__main__":
    main()
