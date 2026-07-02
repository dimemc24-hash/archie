#!/usr/bin/env python3
"""
Stage-2 build driver (Shape-B). Drives Hermes/Opus through a SEGMENTED build, FORCING a Fusion
council consult at each checkpoint-manifest decision point and injecting the synthesis back into
the SAME session lineage. The deterministic driver owns control flow + the forced consults; the
model does only judgment.

Verified Hermes mechanics (harness-124-locked-design.md):
  - `hermes --resume <id>` FORKS a NEW session id each call -> RE-CAPTURE after every segment.
  - never use `-c` in the loop (globally racy).
  - "forcing" lives here: we ALWAYS call fusion at a CHECKPOINT_REACHED sentinel; Hermes has no
    native tool_choice:required.

Usage:
  stage2_build.py --smoke                         # synthetic 1-checkpoint build, glm-5.2 + budget Fusion
  stage2_build.py --run-id <id> [--base main] [--model M] [--preset budget|full] [--max-segments N]
"""
import argparse, json, os, re, subprocess, sys, time, shutil, tempfile

HARNESS = os.path.expanduser("~/harness")
sys.path.insert(0, HARNESS)
import fusion  # noqa: E402
sys.path.insert(0, os.path.join(HARNESS, "port"))
import harness_routing as hr  # noqa: E402
import harness_ledger as hl   # noqa: E402
import harness_redzone as hz  # noqa: E402

REPO      = os.path.join(HARNESS, "repo")  # default (legacy/no --profile): the NewChapter checkout
PROFILES_DIR = os.path.join(HARNESS, "profiles")
ARTIFACTS = os.path.join(HARNESS, "artifacts")
PROV      = ["--provider", "openrouter"]
HEADLESS  = ["--pass-session-id", "--yolo", "--accept-hooks"]
MODEL_TIERS = hr.MODEL_TIERS
BUILD_MODEL = hr.MODEL_TIERS["cheap"]  # builder runs CHEAP (glm-5.2) by DESIGN — cost. pick_segment_model
# escalates to standard(Sonnet)/premium(Opus) only on red-zone/stuck. Opus is reserved for the GATES
# (Fusion checkpoint synth, swarm Lurker + escalated fixes, certification judge, Stage 4), not the build.
PROFILE   = None  # set per-run: "hivemind" (Opus@xhigh) for real builds; None for --smoke (cheap default)
SENT_CP   = re.compile(r"CHECKPOINT_REACHED:\s*([A-Za-z0-9_.-]+)")
SENT_DONE = re.compile(r"BUILD_COMPLETE")
REQ_CP_KEYS = {"id", "trigger", "question", "on_synthesis"}

def run(cmd, cwd, timeout=5400):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout.decode(errors="ignore") if e.stdout else "")
        return 124, out, f"SEGMENT-TIMEOUT after {timeout}s"
    return p.returncode, p.stdout, p.stderr

def _hbase():
    # profile flag must be on EVERY hermes call (incl. sessions list) — sessions are per-profile
    return ["hermes"] + (["-p", PROFILE] if PROFILE else [])

def newest_sid(cwd):
    rc, out, _ = run(_hbase() + ["sessions", "list", "--limit", "1"], cwd, timeout=60)
    lines = [l for l in out.splitlines() if l.strip()]
    return lines[-1].split()[-1] if lines else None

def hermes_segment(prompt, model, cwd, resume_sid=None, timeout=5400, ledger=None, role="codex"):
    cmd = _hbase() + (["--resume", resume_sid] if resume_sid else [])
    cmd += ["-z", prompt, *HEADLESS, "-m", model, *PROV]
    rc, out, err = run(cmd, cwd, timeout=timeout)
    if ledger:
        # hermes -z emits no usage line; charge a conservative per-segment estimate (REVIEW §3.2 fallback)
        ledger.charge(model, {"prompt_tokens": 2000, "completion_tokens": 1500}, role=role)
    return out, err, newest_sid(cwd), rc

def validate_manifest(m):
    if not isinstance(m, dict) or "checkpoints" not in m:
        return "missing 'checkpoints'"
    seen = set()
    for cp in m["checkpoints"]:
        miss = REQ_CP_KEYS - set(cp or {})
        if miss: return f"checkpoint missing keys {miss}"
        if cp["id"] in seen: return f"duplicate checkpoint id {cp['id']}"
        seen.add(cp["id"])
    return None

def core_loop(build_prompt, manifest, model, preset, work_dir, art_dir, max_segments,
              ledger=None, base_ref="main"):
    cp_by_id = {cp["id"]: cp for cp in manifest["checkpoints"]}
    cklog = []
    os.makedirs(art_dir, exist_ok=True)
    burn = open(os.path.join(art_dir, "burn.log"), "a")
    def lb(msg): burn.write(f"{time.strftime('%H:%M:%S')} {msg}\n"); burn.flush(); print("[stage2]", msg)

    # --- checkpoint ENFORCEMENT (Inject+Enforce design): a model is trained to answer, not ask, so the
    # forced consult cannot depend on the agent self-announcing. Track real consults; push back on a
    # BUILD_COMPLETE that skipped a required checkpoint; consult on agent-invented checkpoints too. ---
    consulted = set()
    def required_ids():
        return [i for i, c in cp_by_id.items() if c.get("blocking", True)]  # default: declared == required
    def roster():
        rem = [i for i in required_ids() if i not in consulted]
        if not rem: return "All required checkpoints have been consulted."
        return ("Remaining REQUIRED checkpoints you MUST stop at (emit CHECKPOINT_REACHED:<id>) before "
                "BUILD_COMPLETE:\n" + "\n".join(f"  - {i}: {cp_by_id[i].get('trigger','')}" for i in rem))
    def consult(cid, question, on_synth, pre, build_state, sid, manifest_gap=False):
        lb(f"FORCED fusion '{cid}'{' [manifest-gap]' if manifest_gap else ''} preset={pre or 'full'}")
        try:
            res = fusion.fusion(question, f"BUILD SO FAR:\n{(build_state or '')[-3000:]}", preset=pre); fb = False
        except Exception as e:
            res = {"synthesis": f"[FUSION FALLBACK {e}] proceed with best judgment",
                   "contradictions": [], "blindspots": []}; fb = True
        consulted.add(cid)
        cklog.append({"checkpoint": cid, "question": question, "synthesis": res.get("synthesis"),
                      "contradictions": res.get("contradictions"), "blindspots": res.get("blindspots"),
                      "fallback": fb, "manifest_gap": manifest_gap, "resumed_sid": sid})
        json.dump(cklog, open(os.path.join(art_dir, "checkpoint-log.json"), "w"), indent=2)
        return (f"DECISION FROM COUNCIL: {res.get('synthesis')}\n"
                f"Contradictions: {res.get('contradictions')}\nBlindspots to respect: {res.get('blindspots')}\n"
                f"Apply: {on_synth}\nProceed. {roster()}\n"
                f"At the next checkpoint emit CHECKPOINT_REACHED:<id> and stop; when ALL required checkpoints "
                f"are done and the build is complete, emit BUILD_COMPLETE.")

    # `model` is the BASE tier; pick_segment_model escalates above it on red_zone/retry/blast/stuck.
    # One shared ledger spans the build agent + the Fusion council (global circuit-breaker).
    ledger = ledger or hl.Ledger()
    fusion.set_ledger(ledger)
    retries = 0; red_zone = False; blast = 0

    seg_model = hr.pick_segment_model(model, retries=retries, red_zone=red_zone, blast=blast, stuck=False)
    out, err, sid, rc = hermes_segment(build_prompt, seg_model, work_dir, ledger=ledger)
    lb(f"segment 0 rc={rc} sid={sid} model={seg_model}")
    red_zone = red_zone or hz.detect_red_zone(_changed_files(work_dir, base_ref))
    completion_pushbacks = 0; MAX_PUSHBACK = 3  # bounded: a model that refuses can't loop forever
    seg = 1
    while seg <= max_segments:
        if ledger.is_circuit_broken():
            lb("CIRCUIT BREAKER — daily/global cost cap reached, halting build"); break
        if SENT_DONE.search(out or ""):
            # #1 completion-coverage gate: BUILD_COMPLETE is not accepted while a required checkpoint
            # was never brought to council. Push the agent back to it (bounded), else flag & proceed.
            missing = [i for i in required_ids() if i not in consulted]
            if missing and completion_pushbacks < MAX_PUSHBACK:
                completion_pushbacks += 1
                cid0 = missing[0]
                lb(f"BUILD_COMPLETE but {len(missing)} required checkpoint(s) unconsulted {missing}; "
                   f"pushback {completion_pushbacks}/{MAX_PUSHBACK}")
                inject = (f"You emitted BUILD_COMPLETE, but a REQUIRED decision was never brought to council: "
                          f"'{cid0}' — {cp_by_id[cid0].get('trigger','')}. This is mandatory and cannot be "
                          f"skipped. Return to that decision point now; when you are there, emit "
                          f"CHECKPOINT_REACHED:{cid0} and STOP.\n{roster()}")
                seg_model = hr.pick_segment_model(model, retries=retries, red_zone=red_zone, blast=blast, stuck=True)
                out, err, sid, rc = hermes_segment(inject, seg_model, work_dir, resume_sid=sid, ledger=ledger)
                lb(f"segment {seg} (completion-pushback) rc={rc} sid={sid}")
                red_zone = red_zone or hz.detect_red_zone(_changed_files(work_dir, base_ref))
                seg += 1; continue
            if missing:
                lb(f"COVERAGE FAILURE: BUILD_COMPLETE with unconsulted required {missing} after "
                   f"{MAX_PUSHBACK} pushbacks — flagging for Stage 4")
                cklog.append({"checkpoint": "_coverage_failure", "unconsulted": missing, "fallback": True})
                json.dump(cklog, open(os.path.join(art_dir, "checkpoint-log.json"), "w"), indent=2)
            else:
                lb("BUILD_COMPLETE detected (all required checkpoints consulted)")
            break
        m = SENT_CP.search(out or "")
        if not m:
            lb(f"seg {seg}: no sentinel; nudging")
            retries += 1  # repeated nudges = stuck -> escalate (REVIEW §3.1)
            if retries > 6:
                # Nudge cap (2026-07-02 postmortem: DeepSeek ignored the sentinel
                # protocol for 36 paid segments and drained the OpenRouter credit).
                # Six fruitless nudges across two escalation tiers = the model is
                # not going to comply; fail loudly instead of burning budget.
                lb(f"seg {seg}: NUDGE CAP (6) exceeded - aborting run as BUILD_BLOCKED:nudge-cap")
                raise SystemExit(3)
            seg_model = hr.pick_segment_model(model, retries=retries, red_zone=red_zone, blast=blast, stuck=True)
            out, err, sid, rc = hermes_segment(
                "Continue. If finished emit BUILD_COMPLETE. If at a decision checkpoint emit "
                f"CHECKPOINT_REACHED:<id> and stop.\n{roster()}", seg_model, work_dir, resume_sid=sid, ledger=ledger)
            red_zone = red_zone or hz.detect_red_zone(_changed_files(work_dir, base_ref))
            seg += 1; continue
        cid = m.group(1); cp = cp_by_id.get(cid)
        pre = "budget" if (preset == "budget" or (cp or {}).get("panel_override") == "budget") else None
        if cid in consulted:
            # DEDUPE (runbook §5 known gap): re-emitting an already-consulted checkpoint
            # used to re-run the council — duplicated spend and an opening for the council
            # to expand scope. The decision is already made: re-inject it instead.
            prior = next((e for e in reversed(cklog)
                          if e.get("checkpoint") == cid and e.get("synthesis")), None)
            lb(f"seg {seg}: checkpoint '{cid}' already consulted -> DEDUPE (no council call)")
            cklog.append({"checkpoint": cid, "deduped": True, "resumed_sid": sid})
            json.dump(cklog, open(os.path.join(art_dir, "checkpoint-log.json"), "w"), indent=2)
            inject = (f"""Checkpoint '{cid}' was ALREADY consulted. PRIOR COUNCIL DECISION (binding, unchanged):
{(prior or {}).get('synthesis', '(see checkpoint-log.json)')}
Do NOT re-open this decision or re-emit CHECKPOINT_REACHED:{cid}. Apply it and continue. {roster()}
At the next checkpoint emit CHECKPOINT_REACHED:<id> and stop; when ALL required checkpoints are done and the build is complete, emit BUILD_COMPLETE.""")
        elif not cp:
            # #2 honor unknown: the agent self-identified a decision not in the manifest — consult anyway
            # (its instinct is signal) and flag the manifest gap for the Checkpoint Filter / Stage-1 review.
            lb(f"seg {seg}: UNKNOWN checkpoint '{cid}' -> consulting anyway (manifest gap)")
            q = (f"The builder self-identified a decision point '{cid}' that is NOT in the checkpoint "
                 f"manifest. Given the build so far, what is the right call? Compare approaches, name the "
                 f"trade-offs and any blind spots.")
            inject = consult(cid, q, "adopt the synthesized recommendation", pre, out, sid, manifest_gap=True)
        else:
            lb(f"seg {seg}: checkpoint '{cid}' -> FORCED fusion")
            inject = consult(cid, cp["question"], cp["on_synthesis"], pre, out, sid)
        seg_model = hr.pick_segment_model(model, retries=retries, red_zone=red_zone, blast=blast, stuck=False)
        out, err, sid, rc = hermes_segment(inject, seg_model, work_dir, resume_sid=sid, ledger=ledger)
        lb(f"segment {seg} resumed rc={rc} sid={sid} model={seg_model}")
        red_zone = red_zone or hz.detect_red_zone(_changed_files(work_dir, base_ref))
        json.dump({"sid": sid, "segment": seg, "last_checkpoint": cid},
                  open(os.path.join(art_dir, "state.json"), "w"))
        seg += 1
    burn.close()
    return cklog, seg

def git(args, cwd=None):
    # cwd resolved at CALL time (not def time) so a --profile reassigning the global REPO earlier
    # in real_run() is honored — a bare `cwd=REPO` default would freeze the module-load-time value.
    return run(["git", *args], cwd if cwd is not None else REPO, timeout=300)

def resolve_repo(profile):
    """Resolve the target repo checkout for this run. No --profile (None) preserves the exact
    legacy behavior: ~/harness/repo, the NewChapter checkout, untouched. A named profile gets its
    own workspace under ~/harness/workspaces/<profile>/repo, cloned on first use, so a non-NewChapter
    build never shares a working tree with (or risks colliding with) the NewChapter checkout."""
    if not profile:
        return os.path.join(HARNESS, "repo")
    cfg_path = os.path.join(PROFILES_DIR, f"{profile}.json")
    try:
        cfg = json.load(open(cfg_path))
    except Exception as e:
        raise SystemExit(f"[stage2] unknown profile '{profile}': cannot read {cfg_path}: {e}")
    workspace = os.path.expanduser(cfg["workspace"])
    if not os.path.isdir(os.path.join(workspace, ".git")):
        os.makedirs(os.path.dirname(workspace), exist_ok=True)
        rc, _, err = run(["git", "clone", cfg["repo_url"], workspace], HARNESS)
        if rc:
            raise SystemExit(f"[stage2] failed to clone {cfg['repo_url']} into {workspace}: {err}")
    return workspace

def _changed_files(cwd, base_ref):
    # Files changed base_ref..HEAD, for red-zone detection (REVIEW §3.3). Empty on non-repo/error.
    rc, names, _ = run(["git", "diff", "--name-only", base_ref, "HEAD"], cwd, timeout=60)
    if rc:
        return []
    return [l.strip() for l in names.splitlines() if l.strip()]

def real_run(run_id, base, model, preset, max_segments, repo_profile=None):
    global PROFILE, REPO
    PROFILE = "hivemind"   # real builds run Opus@xhigh via the hivemind profile
    REPO = resolve_repo(repo_profile)  # must happen BEFORE any git()/checkout below
    spec_branch = f"harness/spec/{run_id}"
    art = os.path.join(ARTIFACTS, run_id); os.makedirs(art, exist_ok=True)
    git(["fetch", "origin", spec_branch])
    rc, _, err = git(["checkout", spec_branch])
    if rc: print(f"[stage2] REFUSED: cannot checkout {spec_branch}: {err}"); return 1
    spec_dir = os.path.join(REPO, "_harness", run_id)
    try:
        manifest = json.load(open(os.path.join(spec_dir, "checkpoint-manifest.json")))
        build_prompt = open(os.path.join(spec_dir, "build-prompt.md")).read()
    except Exception as e:
        open(os.path.join(art, "STAGE2_REFUSED.txt"), "w").write(f"missing spec artifacts: {e}")
        print(f"[stage2] REFUSED: {e}"); return 1
    bad = validate_manifest(manifest)
    if bad:
        open(os.path.join(art, "STAGE2_REFUSED.txt"), "w").write(f"manifest invalid: {bad}")
        print(f"[stage2] REFUSED: manifest invalid: {bad}"); return 1
    git(["checkout", base]); git(["checkout", "-B", f"build/{run_id}"])
    cklog, segs = core_loop(build_prompt, manifest, model, preset, REPO, art, max_segments,
                            ledger=hl.Ledger(json_path=os.path.join(art, "ledger.json")), base_ref=base)
    # Stage-3 targeting needs the checkpoint-log INSIDE the build branch (core_loop writes it to art/,
    # outside the repo). Copy it into _harness/<id>/ so the commit below carries it to Hetzner. _harness
    # is not gitignored, and run_swarm excludes ^_harness/ from code scope, so it's inert to the gate.
    cksrc = os.path.join(art, "checkpoint-log.json")
    if os.path.exists(cksrc):
        ckdst = os.path.join(REPO, "_harness", run_id); os.makedirs(ckdst, exist_ok=True)
        shutil.copy(cksrc, os.path.join(ckdst, "checkpoint-log.json"))
    git(["add", "-A"]); git(["commit", "-qm", f"stage2 build {run_id} ({len(cklog)} checkpoints, {segs} segments)"])
    rc, _, err = git(["push", "-u", "origin", f"build/{run_id}"])
    print(f"[stage2] pushed build/{run_id} rc={rc}; checkpoints={len(cklog)}; artifacts={art}")
    return 0

def smoke():
    global PROFILE; PROFILE = "dev"  # local terminal so the agent writes to the host work dir (default is docker-sandboxed); -m glm keeps it cheap
    work = tempfile.mkdtemp(prefix="stage2_smoke_")
    art = os.path.join(ARTIFACTS, "smoke"); shutil.rmtree(art, ignore_errors=True)
    build_prompt = (
        "Build task: create a file named `greeting.txt` in the current directory containing a single "
        "one-word greeting. BUT the greeting LANGUAGE is a decision checkpoint — BEFORE writing any file, "
        "emit exactly the line `CHECKPOINT_REACHED:lang` and STOP, awaiting council guidance. After you "
        "receive the council's language decision, create greeting.txt with a one-word greeting in that "
        "language, then emit `BUILD_COMPLETE`.")
    manifest = {"checkpoints": [{
        "id": "lang", "trigger": "before writing greeting.txt",
        "question": "Which language for a one-word greeting file: English, Spanish, or French? Pick exactly one.",
        "on_synthesis": "use the council's chosen language", "panel_override": "budget"}]}
    print(f"[smoke] work={work}")
    cklog, segs = core_loop(build_prompt, manifest, "z-ai/glm-5.2", "budget", work, art, max_segments=6,
                            ledger=hl.Ledger())
    files = os.listdir(work)
    print(f"[smoke] segments={segs} checkpoints_logged={len(cklog)} files={files}")
    if cklog: print(f"[smoke] council synthesis: {str(cklog[0]['synthesis'])[:160]}")
    greet = [f for f in files if f.lower().endswith('.txt')]  # any greeting file the agent named (glm may pick e.g. hello.txt)
    if greet:
        print(f"[smoke] {greet[0]} = {open(os.path.join(work, greet[0])).read()[:60]!r}")
    ok = bool(cklog) and bool(greet)
    print("[smoke] PASS — sentinel+SID-recapture+forced-fusion+injection all worked" if ok else "[smoke] CHECK output above")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 2

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id"); ap.add_argument("--base", default="main")
    ap.add_argument("--model", default=BUILD_MODEL); ap.add_argument("--preset", default=None)
    ap.add_argument("--max-segments", type=int, default=40); ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--profile", default=None,
                     help="target repo profile (profiles/<name>.json); omit for the legacy NewChapter checkout")
    a = ap.parse_args()
    if a.smoke: sys.exit(smoke())
    if not a.run_id: ap.error("--run-id required (or --smoke)")
    sys.exit(real_run(a.run_id, a.base, a.model, a.preset, a.max_segments, a.profile))

if __name__ == "__main__":
    main()
