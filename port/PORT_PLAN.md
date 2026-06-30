# PORT_PLAN.md — exact edits to wire harness_routing / harness_ledger / harness_redzone into the Stage-2 harness

This plan follows the three integration seams in REVIEW_harness.md §3.1–§3.3.
The three new modules live in `~/harness/port/` and are imported, not vendored.
A human applies these edits to `stage2_build.py` and `fusion.py`; this document
is the exact diff specification.

## 0. imports (both files)

`stage2_build.py` (after line 22, `import fusion`):
```python
sys.path.insert(0, os.path.join(HARNESS, "port"))
import harness_routing as hr     # pick_segment_model, trigger_for
import harness_ledger   as hl     # Ledger, CircuitOpen
import harness_redzone  as hz     # detect_red_zone, RED_ZONE_GLOBS
```

`fusion.py` (after line 10, `import ... urllib.request`):
```python
import sys, os
sys.path.insert(0, os.path.join(os.path.expanduser("~/harness"), "port"))
import harness_ledger as hl
```

## 1. stage2_build.py — MODEL_TIERS + core_loop signature (REVIEW §3.1)

### 1a. replace the BUILD_MODEL constant (line 28)

BEFORE:
```python
BUILD_MODEL = "anthropic/claude-opus-4.8"
```
AFTER:
```python
# Tier ladder (REVIEW §3.1). base_model flows in from real_run/smoke;
# pick_segment_model escalates up this ladder on red_zone/retry/blast/stuck.
MODEL_TIERS = hr.MODEL_TIERS  # {"cheap": ..., "standard": ..., "premium": ...}
# Back-compat: callers still pass a model slug; the loop treats it as base_model.
BUILD_MODEL = hr.MODEL_TIERS["premium"]  # "anthropic/claude-opus-4.8"
```

### 1b. core_loop signature + ledger/redzone state (line 64)

BEFORE:
```python
def core_loop(build_prompt, manifest, model, preset, work_dir, art_dir, max_segments):
```
AFTER:
```python
def core_loop(build_prompt, manifest, model, preset, work_dir, art_dir, max_segments,
              ledger=None, base_ref="main"):
    # model is now the BASE tier (cheap for smoke, premium for real runs).
    # pick_segment_model escalates from it per-segment.
    ledger = ledger or hl.Ledger()  # in-process tally; swap for sqlite later
    retries = 0          # nudge counter (escalation.ts attempt-1)
    red_zone = False     # detect_red_zone result, OR'd across segments
    blast = 0            # changed-file count this segment
```

## 2. stage2_build.py — the three hermes_segment call sites (REVIEW §3.1)

### 2a. segment 0 (line 71)

BEFORE:
```python
    out, err, sid, rc = hermes_segment(build_prompt, model, work_dir)
    lb(f"segment 0 rc={rc} sid={sid}")
    seg = 1
```
AFTER:
```python
    seg_model = hr.pick_segment_model(model, seg=0, retries=retries,
                                      red_zone=False, blast=0, stuck=False)
    out, err, sid, rc = hermes_segment(build_prompt, seg_model, work_dir)
    lb(f"segment 0 rc={rc} sid={sid} model={seg_model}")
    # red-zone detection after the tree mutates (REVIEW §3.3)
    red_zone = hz.detect_red_zone(_changed_files(work_dir, base_ref))
    seg = 1
```

### 2b. nudge branch (lines 80–83)

BEFORE:
```python
            out, err, sid, rc = hermes_segment(
                "Continue. If finished emit BUILD_COMPLETE. If at a decision checkpoint emit "
                "CHECKPOINT_REACHED:<id> and stop.", model, work_dir, resume_sid=sid)
            seg += 1; continue
```
AFTER:
```python
            retries += 1  # repeated nudges escalate (REVIEW §3.1)
            stuck = True  # no sentinel = stuck signal (REVIEW §3.1)
            seg_model = hr.pick_segment_model(model, seg=seg, retries=retries,
                                              red_zone=red_zone, blast=blast, stuck=stuck)
            out, err, sid, rc = hermes_segment(
                "Continue. If finished emit BUILD_COMPLETE. If at a decision checkpoint emit "
                "CHECKPOINT_REACHED:<id> and stop.", seg_model, work_dir, resume_sid=sid)
            red_zone = red_zone or hz.detect_red_zone(_changed_files(work_dir, base_ref))
            seg += 1; continue
```

### 2c. post-checkpoint segment (line 105)

BEFORE:
```python
        out, err, sid, rc = hermes_segment(inject, model, work_dir, resume_sid=sid)
        lb(f"segment {seg} resumed rc={rc} sid={sid}")
```
AFTER:
```python
        # checkpoint resolved: clear stuck, keep red_zone/retry state
        seg_model = hr.pick_segment_model(model, seg=seg, retries=retries,
                                          red_zone=red_zone, blast=blast, stuck=False)
        out, err, sid, rc = hermes_segment(inject, seg_model, work_dir, resume_sid=sid)
        lb(f"segment {seg} resumed rc={rc} sid={sid} model={seg_model}")
        red_zone = red_zone or hz.detect_red_zone(_changed_files(work_dir, base_ref))
```

## 3. stage2_build.py — circuit-breaker gate (REVIEW §3.2)

At the top of the `while seg <= max_segments` loop (line 74), BEFORE the
BUILD_COMPLETE check:

BEFORE:
```python
    while seg <= max_segments:
        if SENT_DONE.search(out or ""):
```
AFTER:
```python
    while seg <= max_segments:
        if ledger.is_circuit_broken():
            lb("CIRCUIT BREAKER — daily cap reached, halting build"); break
        if SENT_DONE.search(out or ""):
```

## 4. stage2_build.py — hermes_segment ledger integration (line 47)

BEFORE:
```python
def hermes_segment(prompt, model, cwd, resume_sid=None, timeout=1800):
    cmd = _hbase() + ([\"--resume\", resume_sid] if resume_sid else [])
    cmd += [\"-z\", prompt, *HEADLESS, \"-m\", model, *PROV]
    rc, out, err = run(cmd, cwd, timeout=timeout)
    return out, err, newest_sid(cwd), rc
```
AFTER:
```python
def hermes_segment(prompt, model, cwd, resume_sid=None, timeout=1800, ledger=None, role="codex"):
    cmd = _hbase() + (["--resume", resume_sid] if resume_sid else [])
    cmd += ["-z", prompt, *HEADLESS, "-m", model, *PROV, "--print-usage"]
    # breaker check BEFORE the call (REVIEW §3.2)
    if ledger:
        ledger.check(model, role=role)
    rc, out, err = run(cmd, cwd, timeout=timeout)
    # charge AFTER the call — parse hermes usage line if present (REVIEW §3.2)
    if ledger:
        usage = _parse_usage(out)  # dict with prompt_tokens/completion_tokens
        ledger.charge(model, usage, role=role)
    return out, err, newest_sid(cwd), rc
```

NOTE: `--print-usage` and `_parse_usage` are the harness-side additions the
REVIEW anticipates ("the exact parse depends on hermes --pass-session-id output
format; if hermes emits no usage, the minimal change is to add --print-usage").
If hermes does not support `--print-usage`, fall back to a per-segment estimate:
```python
usage = {"prompt_tokens": 2000, "completion_tokens": 1500}  # conservative estimate
```

## 5. stage2_build.py — helpers + red-zone detection (REVIEW §3.3)

Add near the `git()` helper (line 113):

```python
def _changed_files(cwd, base_ref):
    """git diff --name-only between base and HEAD (REVIEW §3.3)."""
    rc, names, _ = run(["git", "diff", "--name-only", base_ref, "HEAD"], cwd, timeout=60)
    if rc: return []
    return [l.strip() for l in names.splitlines() if l.strip()]
```

`detect_red_zone` itself is `hz.detect_red_zone(changed_files_list)` — no wrapper
needed; the module constant `hz.RED_ZONE_GLOBS` is the glob list from
profiles/newchapter.json.

## 6. stage2_build.py — real_run / smoke call-site threading (REVIEW §3.1 last bullet)

`real_run` (line 135):
BEFORE:
```python
    cklog, segs = core_loop(build_prompt, manifest, model, preset, REPO, art, max_segments)
```
AFTER:
```python
    cklog, segs = core_loop(build_prompt, manifest, model, preset, REPO, art, max_segments,
                            ledger=hl.Ledger(json_path=os.path.join(art, "ledger.json")),
                            base_ref=base)
```

`smoke` (line 155): unchanged — passes `"z-ai/glm-5.2"` as `base_model` (the
cheap tier); `pick_segment_model` escalates from it. Add `ledger=hl.Ledger()`
to the `core_loop` call for cost visibility during smoke.

## 7. fusion.py — call_model ledger integration (REVIEW §3.2)

### 7a. module-level ledger singleton

Add after `_key()` (line 25):
```python
# Shared ledger — same instance the build driver uses (REVIEW §3.2).
_ledger = None
def set_ledger(l):
    global _ledger
    _ledger = l
```

### 7b. call_model (lines 27–42)

BEFORE:
```python
def call_model(model, system, user, timeout=180, temperature=0.4):
    body = json.dumps({
        \"model\": model,
        \"temperature\": temperature,
        \"messages\": [{\"role\": \"system\", \"content\": system}, {\"role\": \"user\", \"content\": user}],
    }).encode()
    req = urllib.request.Request(OR_URL, data=body, headers={
        \"Authorization\": \"Bearer \" + _key(),
        \"Content-Type\": \"application/json\",
        \"HTTP-Referer\": \"https://fsfai.harness\", \"X-Title\": \"fusion\",
    })
    try:
        r = json.load(urllib.request.urlopen(req, timeout=timeout))
        return r[\"choices\"][0][\"message\"][\"content\"]
    except Exception as e:
        return f\"[ERROR {model}: {e}]\"
```
AFTER:
```python
def call_model(model, system, user, timeout=180, temperature=0.4, role="panel"):
    body = json.dumps({
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(OR_URL, data=body, headers={
        "Authorization": "Bearer " + _key(),
        "Content-Type": "application/json",
        "HTTP-Referer": "https://fsfai.harness", "X-Title": "fusion",
    })
    # breaker BEFORE urlopen (REVIEW §3.2)
    if _ledger:
        try:
            _ledger.check(model, role=role)
        except hl.CircuitOpen as e:
            # Distinguish circuit-break from transient HTTP (REVIEW §3.2):
            # propagate so fusion() can fall back to a cheaper panelist.
            raise
    try:
        r = json.load(urllib.request.urlopen(req, timeout=timeout))
        # charge AFTER the call — usage block is already on the wire (REVIEW §3.2)
        if _ledger:
            _ledger.charge(model, r.get("usage", {}), role=role)
        return r["choices"][0]["message"]["content"]
    except hl.CircuitOpen:
        raise  # do NOT swallow into [ERROR] (REVIEW §3.2)
    except Exception as e:
        return f"[ERROR {model}: {e}]"
```

### 7c. fusion() — role tagging + circuit fallback

In `fusion()` (line 61), tag each call with its role so per-role caps apply:
- `_panelist` calls: `call_model(m, ..., role="panel")`
- judge call (line 70): `call_model(JUDGE, ..., role="judge")`
- synth call (line 78): `call_model(SYNTH, ..., role="synth")`

Wrap the synth call so a CircuitOpen falls back to raw-text synthesis (the
existing fallback at line 79):
```python
    try:
        synth_raw = call_model(SYNTH, synth_sys, synth_user, role="synth")
    except hl.CircuitOpen:
        synth_raw = "[CIRCUIT OPEN] proceed with best judgment from judge analysis"
```

## 8. integration verification checklist

After applying:
1. `python3 -m py_compile stage2_build.py fusion.py port/*.py` — no syntax errors.
2. `python3 port/test_port.py` — all ported test cases pass.
3. `python3 stage2_build.py --smoke` — smoke run uses `pick_segment_model` and
   `detect_red_zone`; `artifacts/smoke/ledger.json` is written.
4. A smoke run that touches a red-zone glob (e.g. create a file under
   `lib/forms/`) should escalate the segment model (visible in `burn.log`).
5. A blown `daily_cap_usd` (set to 0.01 in a test) trips the circuit-breaker
   gate and halts the loop with `CIRCUIT BREAKER` in `burn.log`.
