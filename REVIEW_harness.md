# Stage-2 Build Harness — Integration Seams Report

## 1. WHAT IT DOES

### 1.1 `stage2_build.py` — the Stage-2 segmented build driver

The driver is a deterministic control loop around the `hermes` CLI. The model only does judgment; the Python loop owns control flow, checkpoint detection, the forced Fusion consult, and session lineage.

**Setup (`real_run`, lines 116–134)**
- Pins `PROFILE = "hivemind"` (line 117) so every `hermes` invocation runs Opus at xhigh via that profile.
- Fetches the spec branch `harness/spec/<run_id>` (line 120), checks it out (121), and refuses the run if the checkout fails (122).
- Loads `checkpoint-manifest.json` and `build-prompt.md` from `_harness/<run_id>/` (lines 125–126). Refuses with `STAGE2_REFUSED.txt` if either is missing (128).
- Validates the manifest via `validate_manifest` (lines 53–62): every checkpoint must carry `{id, trigger, question, on_synthesis}`, ids unique.
- Checks out `--base` (default `main`) and creates `build/<run_id>` (line 134), then enters `core_loop`.

**The segmented loop (`core_loop`, lines 64–111)**
- Indexes checkpoints by id: `cp_by_id` (line 65).
- Opens `burn.log` and defines `lb()` logger (68–69).
- **Segment 0** (line 71): `hermes_segment(build_prompt, model, work_dir)` — first build prompt, no resume. Captures `sid`.
- Enters `while seg <= max_segments` (74, default 40 from line 171):
  - **BUILD_COMPLETE sentinel** (75–76): if `SENT_DONE` regex (`BUILD_COMPLETE`, line 31) matches the agent output, breaks.
  - **CHECKPOINT_REACHED sentinel** (77, 84): `SENT_CP` regex (`CHECKPOINT_REACHED:\s*([A-Za-z0-9_.-]+)`, line 30) extracts the checkpoint id `cid`. Looks up `cp_by_id.get(cid)` (84).
  - **No sentinel → nudge** (78–83): re-prompts the agent ("Continue. If finished emit BUILD_COMPLETE. If at a decision checkpoint emit CHECKPOINT_REACHED:<id> and stop."), resumes on `sid`, increments `seg`, loops. This is the only "stuck" handling.
  - **Unknown checkpoint** (85–87): injects a fallback string, no Fusion call.
  - **Forced Fusion consult** (88–104):
    - `pre = "budget"` if `cp.panel_override == "budget"` OR `preset == "budget"`, else full panel (line 89).
    - Calls `fusion.fusion(cp["question"], context, preset=pre)` where context is the last 3000 chars of agent output (line 92).
    - On exception, synthesizes a fallback dict `[FUSION FALLBACK …] proceed with best judgment` (94–96).
    - Appends to `cklog` with synthesis/contradictions/blindspots/fallback/resumed_sid (97–99) and persists `checkpoint-log.json` (100).
    - Builds the inject string (101–104): `DECISION FROM COUNCIL: <synthesis>`, contradictions, blindspots, `Apply on_synthesis: <cp.on_synthesis>`, then instruct the agent to proceed to the next checkpoint or emit BUILD_COMPLETE.
  - **Post-checkpoint segment** (105): `hermes_segment(inject, model, work_dir, resume_sid=sid)`.
  - **SID re-capture** (51, 42–45): `hermes_segment` calls `newest_sid(cwd)` which runs `hermes sessions list --limit 1` and takes the last token of the last line. This is mandatory because `hermes --resume <id>` forks a NEW session id each call (per the locked-design comment, lines 8–12).
  - Persists `state.json` (107–108). Increments `seg` (109).

**Final push (`real_run`, lines 135–139)**
- `git add -A; git commit -qm "stage2 build …"` (136).
- `git push -u origin build/<run_id>` (137).
- Prints summary. Returns 0.

**`hermes_segment` (lines 47–51)** — the single build-agent call site. Builds `hermes [--resume sid] -z prompt --pass-session-id --yolo --accept-hooks -m model --provider openrouter`, runs it, returns `(out, err, sid, rc)`. Note: it does NOT return token usage.

**`smoke` (lines 141–165)** — synthetic 1-checkpoint build with `z-ai/glm-5.2` and `preset="budget"`, verifying sentinel detection, SID re-capture, forced fusion, and injection all work end-to-end.

### 1.2 `transport.sh` — Phase-D cross-box transport

From `stage2_build`/`transport`'s perspective, the swarm (`peanut_wheel.py` / `run_swarm.sh`) is a black box invoked over SSH on the Hetzner box. `transport.sh` is the DO-side shuttle:

- **Baseline push** (17–19): `git fetch origin main`, then `git push -f hetzner origin/main:refs/heads/gh-main` — gives the swarm box a read-only GitHub baseline.
- **Build push** (21–23): verifies `build/<id>` exists locally (fatal exit 3 if not), `git push -f hetzner build/<id>:refs/heads/build/<id>`.
- **Swarm invocation** (25–27): `ssh hetzner-swarm "bash \$HOME/swarm/run_swarm.sh '$BUILD' '$ROUTES' '$WAVES'"`. From this side, `run_swarm.sh` takes the build branch name, a routes CSV, and a waves count; its internal `peanut_wheel.py` routing is invisible.
- **Fix fetch-back** (29–30): `git fetch hetzner +fix/<id>:refs/heads/fix/<id>` (fatal exit 4 if it can't fetch — meaning the swarm didn't produce a fix branch).
- **Stage-4 handoff** (32–33): `git push -f origin fix/<id>:refs/heads/fix/<id>`. Only DO touches GitHub; Hetzner holds no GitHub credential.

So the contract `transport.sh` imposes on the swarm is: given `build/<id>` on `hetzner`, produce `fix/<id>` on `hetzner`. There is no verification, no model-routing, no cost surface exposed across that SSH boundary — it's pure branch shuttle.

### 1.3 `fusion.py` — the council (referenced by Stage 2)

Panel → judge → synthesizer over OpenRouter, dependency-free urllib.
- `PANEL_FULL` (line 15): deepseek-v4-pro, kimi-k2.6, glm-5.2, gpt-5.5-pro.
- `PANEL_BUDGET` (line 16): gemini-3.5-flash, kimi-k2.6, deepseek-v4-pro.
- `JUDGE = google/gemini-3.5-flash` (17), `SYNTH = anthropic/claude-opus-4.8` (18).
- `_key()` (20–25): OpenRouter key from env or first entry of `auth.json` credential_pool (always index `[0]` — no rotation).
- `call_model` (27–42): the OpenRouter HTTP chokepoint; catches all exceptions and returns an `[ERROR …]` string rather than raising — so a panelist failure is silently swallowed.
- `fusion()` (61–82): runs panelists in parallel via `ThreadPoolExecutor` (63–64), then judge (70), then synth (78), JSON-extracts via `_extract_json` (44–51, falls back to raw text).

---

## 2. WHERE IT IS CRUDE vs a mature model-router

### 2.1 Model routing — hard-pinned, no escalation

- `BUILD_MODEL = "anthropic/claude-opus-4.8"` (line 28) is a module constant. It flows `main` (170) → `real_run` (116, param `model`) → `core_loop` (64, param `model`) → every `hermes_segment(prompt, model, …)` call at lines 71, 80, 105. The same model runs segment 0, every nudge, every post-checkpoint segment, and every retry.
- The Fusion side is equally static: panel composition is a fixed list keyed only on `preset ∈ {None, "budget"}` (lines 15–16, 62), `JUDGE` and `SYNTH` are constants (17–18). No signal drives panel composition either.
- There is no notion of escalation (cheap → premium on red-zone/retries) or de-escalation (premium → cheap on trivial/straightforward segments). A mature router would pick the model per segment from signals: checkpoint id, retry count, blast radius of changed files, stuck-ness. This harness has all those signals available in the loop but wires none of them to model selection.

### 2.2 Cost control — segment counter + provider-side key cap only

- The only in-process cost guard is `seg <= max_segments` (line 74, default 40 at line 171). That's a segment counter, not a cost ledger — a segment that burns $5 of Opus counts the same as a no-op nudge.
- The "per-day OpenRouter key cap" lives entirely outside the code: `_key()` (fusion.py 20–25) returns the first key from the pool and never rotates (always `[0]`). The per-day cap is enforced by OpenRouter against that key, not by the harness. When the key blows the cap, `call_model` returns an `[ERROR …]` string (42) that is silently treated as a panelist opinion (57–59) or — for the synth — falls through to raw-text synthesis (79). There is no circuit-breaker.
- No per-call cost ledger: `hermes_segment` (47–51) returns no token/usage data; `call_model` (27–42) discards the `usage` field from the OpenRouter response (only reads `choices[0].message.content`). So the harness literally cannot account for cost even if it wanted to.
- No per-role caps: build agent (Opus, expensive), panelists (mixed), judge (flash, cheap), synth (Opus, expensive) all draw from the same unbounded budget. A mature router would have separate caps per role and a global circuit-breaker that trips across all of them.

### 2.3 Verification — pure self-report, no independent gate

- Success is declared the instant `SENT_DONE.search(out)` matches (line 75–76). `out` is the build agent's own stdout. There is no independent build, no critic pass, no imports/lint gate, no test run between the agent claiming `BUILD_COMPLETE` and the driver committing.
- The final push (lines 135–138) runs `git add -A; git commit; git push` immediately after `core_loop` returns. Nothing verifies the tree compiles, imports resolve, or that the claimed build artifacts exist. A failed build is shipped to `origin/build/<run_id>` and then onto the swarm box via `transport.sh` (which also has no verification — it just shuttles branches).
- The Fusion consult (92) is advisory, not a gate: its synthesis is injected as text (101–104) and the agent is free to ignore it. Fusion cannot veto or block a segment.

### 2.4 No red-zone concept

- Red-zone paths (legal-correctness: forms, NDC, migrations, means-test) receive no special handling. The checkpoint manifest schema (`REQ_CP_KEYS`, line 32) is `{id, trigger, question, on_synthesis}` — no `red_zone`, `blast_radius`, or `legal` field. A checkpoint asking "which NDC format" is routed identically to one asking "what greeting language" (the smoke case, lines 150–153).
- No file-change inspection happens anywhere in the loop. The `git()` helper (113–114) exists but is only used for branch ops in `real_run` (120–137), never for diff inspection mid-build. So even if a segment touches `migrations/` or `means_test.py`, nothing escalates.
- The nudge branch (78–83) treats "no sentinel" identically regardless of what the agent just did — a stuck build in a red-zone area gets the same generic "Continue" prompt as a stuck build anywhere else.

---

## 3. INTEGRATION SEAMS

### 3.1 (a) Escalation engine — pick the model per segment

**Functions to modify:** `core_loop` (line 64) and the three `hermes_segment` call sites (lines 71, 80, 105). Introduce a new `pick_segment_model(signals) -> str` near the `BUILD_MODEL` constant (line 28).

**Why here:** `hermes_segment` already accepts `model` as a parameter (line 47) and passes it as `-m model` (line 49). The model is already per-call — it's just always fed the same constant. The loop already has every signal a router needs in scope:
- `cid` (line 84) — checkpoint identity → could map to a red-zone tier via the manifest.
- `seg` (line 73) — retry/stuck counter.
- The nudge branch (78–83) — explicit stuck signal.
- `out` (last agent output) — available for blast-radius diffing.

**Minimal change:**
- Replace `BUILD_MODEL` constant (line 28) with a tier ladder, e.g. `MODEL_TIERS = {"cheap": "z-ai/glm-5.2", "standard": "anthropic/claude-sonnet-4.8", "premium": "anthropic/claude-opus-4.8"}`.
- Change `core_loop`'s signature (line 64) so `model` becomes `base_model` (the default tier), and add a local `retries = 0`, `red_zone = False`, `blast = 0`.
- At each of the three call sites, replace the bare `model` argument with `pick_segment_model(base_model, seg=seg, retries=retries, red_zone=red_zone, blast=blast, cid=cid)`:
  - Line 71 (segment 0): `hermes_segment(build_prompt, pick_segment_model(base_model, seg=0, ...), work_dir)`.
  - Line 80–82 (nudge): `hermes_segment(nudge_prompt, pick_segment_model(base_model, seg=seg, retries=retries+1, ...), work_dir, resume_sid=sid)` — and increment a `retries` counter here so repeated nudges escalate.
  - Line 105 (post-checkpoint): `hermes_segment(inject, pick_segment_model(base_model, seg=seg, red_zone=red_zone, cid=cid, ...), work_dir, resume_sid=sid)`.
- `real_run` (line 135) and `smoke` (line 155) keep passing `model`/`"z-ai/glm-5.2"` as `base_model`; no signature break for callers.

### 3.2 (b) Cost ledger + circuit-breaker gating every model call

**Functions to modify:** `fusion.call_model` (fusion.py line 27) for the council side, and `hermes_segment` (stage2_build.py line 47) for the build-agent side. Introduce a shared `Ledger`/`Breaker` object (module-level singleton) imported by both files.

**Why here:** These are the two and only chokepoints where model calls actually happen.
- `call_model` (fusion.py 27–42) is the single HTTP point for all council traffic — every panelist (57–59), the judge (70), and the synth (78) go through it.
- `hermes_segment` (stage2_build.py 47–51) is the single subprocess point for all build-agent traffic (call sites 71, 80, 105).

**Minimal change on `call_model` (fusion.py 27):**
- Before `urlopen` (line 39): `breaker.check(model)` — raises `CircuitOpen` if the model's role-cap or global cap is exceeded. Catch in `fusion()` to fall back to a cheaper panelist.
- After `r = json.load(...)` (line 39): `ledger.charge(model, r.get("usage", {}))` — the `usage` block is already in the OpenRouter response but currently discarded (only `choices[0].message.content` is read, line 40). This is a one-line addition; the data is already on the wire.
- Wrap the existing `except Exception` (41) so a circuit-breaker exception is NOT swallowed into an `[ERROR]` string (distinguish `CircuitOpen` from transient HTTP errors).

**Minimal change on `hermes_segment` (stage2_build.py 47–51):**
- Before `subprocess.run` (line 50): `breaker.check(model)`.
- After the call (line 50–51): parse usage from `out` (hermes can emit a usage line) or fall back to a per-model per-segment estimate, then `ledger.charge(model, estimate)`. The exact parse depends on `hermes --pass-session-id` output format; if hermes emits no usage, the minimal change is to add `--print-usage` (or equivalent) to the `cmd` list at line 49 and parse it.
- Gate the loop: in `core_loop` at the top of the `while` (line 74), add `if breaker.global_tripped(): lb("CIRCUIT BREAKER"); break` so a blown budget stops the build rather than silently degrading.

**Note on `_key()` (fusion.py 20–25):** rotation across the credential pool (currently hardcoded `[0]`) belongs here too — a mature router would advance the index when the breaker trips for one key. This is a one-line change: `return d["credential_pool"]["openrouter"][key_index % len(...)]["access_token"]` with `key_index` bumped by the breaker.

### 3.3 (c) Red-zone detection derived from the changed files

**Functions to modify:** `core_loop` (line 64) — insert a `detect_red_zone(work_dir, base)` call after each `hermes_segment` returns, and feed its result into `pick_segment_model` (from 3.1) and into the inject string (lines 101–104).

**Why here:** The loop already owns `work_dir` (REPO for real runs) and already has a `git()` helper (lines 113–114). The build base is known at `real_run` line 134 (`git checkout -B build/<run_id>` off `--base`). The three points where the tree mutates are exactly the three `hermes_segment` returns (lines 71, 80–82, 105) — those are the only moments a red-zone-relevant file could have been touched.

**Minimal change:**
- Add a `RED_ZONE_PATTERNS` list near line 28 (e.g. `["migrations/", "forms/", "ndc", "means_test", "legal/", "compliance/"]`).
- Add `detect_red_zone(cwd, base_ref) -> bool`:
  ```
  _, names, _ = run(["git", "diff", "--name-only", base_ref, "HEAD"], cwd, timeout=60)
  return any(any(p in n for p in RED_ZONE_PATTERNS) for n in names.splitlines())
  ```
  Use `git()` (line 113) or `run()` (line 34). The base ref is `base` threaded from `real_run` (line 116) into `core_loop` — add it as a `core_loop` param.
- After line 71 (segment 0): `red_zone = detect_red_zone(work_dir, base)`.
- After the nudge segment (line 82): `red_zone = red_zone or detect_red_zone(work_dir, base)`.
- After line 105 (post-checkpoint): `red_zone = red_zone or detect_red_zone(work_dir, base)`.
- Feed `red_zone` into `pick_segment_model` at lines 71/80/105 (per 3.1) so a red-zone hit escalates to the premium tier regardless of `seg`.
- Optionally, when `red_zone` is true and a checkpoint is active, augment the inject (101–104) with a red-zone warning so the agent knows the council's synthesis is binding on a legal-correctness path — and surface it in `cklog` (97–99) for audit.
- The checkpoint manifest could also carry a declarative `red_zone: true` per checkpoint (extending `REQ_CP_KEYS` at line 32); the file-diff signal is the runtime confirmation, the manifest field is the upfront declaration. Both feed the same `red_zone` boolean.

---

### Cross-cutting note: the verification seam (referenced in §2.3)

Although not one of the three requested grafts, the natural seam for an independent build/critic/imports gate — the verification gap — is between `core_loop` returning (line 135) and the `git push` (line 137) in `real_run`. A mature router would insert a `verify(work_dir)` call there that runs the actual build, import check, and a critic pass, and refuses the push (writing `STAGE2_REFUSED.txt` like lines 128/132) on failure. Inside the loop, the BUILD_COMPLETE break (line 76) is the second seam: verification should run there and re-enter the loop with a critic-inject if it fails, rather than breaking immediately. Both seams already have the `git()`/`run()` helpers and the `art_dir` for refusal artifacts.
