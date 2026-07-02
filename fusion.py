#!/usr/bin/env python3
"""
Fusion — panel -> judge -> synthesizer council, via OpenRouter. Dependency-free (urllib).
Used by Stage 2 checkpoints (full panel, forced by the driver) and Stage 1 forks (--preset budget).

  fusion(question, context, preset=None) -> {synthesis, contradictions, blindspots, _panel, _judge}

CLI:  python3 fusion.py [--preset budget] "QUESTION"   (context on stdin)
"""
import json, os, sys, re, urllib.request, concurrent.futures as cf
sys.path.insert(0, os.path.join(os.path.expanduser("~/harness"), "port"))
import harness_ledger as hl  # noqa: E402  (CircuitOpen + cost gating)

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
AUTH_JSON = os.path.expanduser("~/.hermes/auth.json")

PANEL_FULL   = ["deepseek/deepseek-v4-pro", "moonshotai/kimi-k2.6", "z-ai/glm-5.2", "openai/gpt-5.5"]
PANEL_BUDGET = ["openai/gpt-4o", "moonshotai/kimi-k2.6", "deepseek/deepseek-v4-pro"]
JUDGE = "google/gemini-3.5-flash"
SYNTH_FULL   = "anthropic/claude-sonnet-5"  # was opus-4.6 ($5in/$25out); sonnet-5 is $2in/$10out, newer gen
SYNTH_BUDGET = "anthropic/claude-sonnet-4.6"

# ── council-config override seam ──────────────────────────────────────────────
# Reads ~/harness/council-config.json (env COUNCIL_CONFIG overrides for tests).
# Missing file → exactly today's behavior (constants). Corrupt JSON or wrong
# types → fall back to ALL constants + one stderr warning (never crash, never
# partially apply a corrupt file). Read fresh on every fusion() call (tiny file).
_CONFIG_PATH = os.environ.get(
    "COUNCIL_CONFIG",
    os.path.join(os.path.expanduser("~/harness"), "council-config.json"),
)


def _load_council_config():
    """Return (overrides_dict, source) where source is "file" or "default".

    On any error (missing file, bad JSON, wrong types) returns ({}, "default")
    and prints one stderr line for the bad-JSON case only (missing file is the
    normal no-config state, not worth a warning).
    """
    path = os.environ.get("COUNCIL_CONFIG", _CONFIG_PATH)
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}, "default"
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"[fusion] council-config invalid ({path}): {exc}\n")
        return {}, "default"
    # type-check: must be a dict; panel_* must be lists of str; judge/synth_* str.
    if not isinstance(raw, dict):
        sys.stderr.write(f"[fusion] council-config invalid ({path}): root is not a dict\n")
        return {}, "default"
    overrides = {}
    for key in ("panel_full", "panel_budget"):
        val = raw.get(key)
        if val is None:
            continue
        if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
            sys.stderr.write(f"[fusion] council-config invalid ({path}): {key} must be a list of strings\n")
            return {}, "default"
        overrides[key] = val
    for key in ("judge", "synth_full", "synth_budget"):
        val = raw.get(key)
        if val is None:
            continue
        if not isinstance(val, str):
            sys.stderr.write(f"[fusion] council-config invalid ({path}): {key} must be a string\n")
            return {}, "default"
        overrides[key] = val
    return overrides, "file"


def resolve_council_models(preset=None):
    """Return effective models for the five roles + a source label.

    Returns: {"panel": [...], "judge": str, "synth": str,
              "sources": {"panel_full": "file"|"default", ...}}
    Missing keys fall back to the module constants.
    """
    overrides, src = _load_council_config()
    panel_full = overrides.get("panel_full", PANEL_FULL)
    panel_budget = overrides.get("panel_budget", PANEL_BUDGET)
    judge = overrides.get("judge", JUDGE)
    synth_full = overrides.get("synth_full", SYNTH_FULL)
    synth_budget = overrides.get("synth_budget", SYNTH_BUDGET)
    sources = {
        "panel_full":   src if "panel_full" in overrides else "default",
        "panel_budget": src if "panel_budget" in overrides else "default",
        "judge":        src if "judge" in overrides else "default",
        "synth_full":   src if "synth_full" in overrides else "default",
        "synth_budget": src if "synth_budget" in overrides else "default",
    }
    panel = panel_budget if preset == "budget" else panel_full
    synth = synth_budget if preset == "budget" else synth_full
    return {
        "panel": panel, "judge": judge, "synth": synth,
        "sources": sources,
        "panel_full": panel_full, "panel_budget": panel_budget,
        "synth_full": synth_full, "synth_budget": synth_budget,
    }


def _key():
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k.strip()
    d = json.load(open(AUTH_JSON))
    return d["credential_pool"]["openrouter"][0]["access_token"]

# Shared ledger (set by the Stage-2 driver) so council spend counts toward the same caps/breaker.
_ledger = None
def set_ledger(l):
    global _ledger
    _ledger = l

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
    if _ledger:
        _ledger.check(model, role=role)   # circuit-breaker BEFORE the call; raises CircuitOpen on breach
    try:
        r = json.load(urllib.request.urlopen(req, timeout=timeout))
        if _ledger:
            _ledger.charge(model, r.get("usage", {}), role=role)   # usage already on the wire
        return r["choices"][0]["message"]["content"]
    except hl.CircuitOpen:
        raise   # do not swallow the breaker into [ERROR]
    except Exception as e:
        return f"[ERROR {model}: {e}]"

def _extract_json(raw):
    m = re.search(r"```json\s*(.*?)```", raw, re.S) or re.search(r"(\{.*\})", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None

PANEL_SYS = ("You are a senior engineering advisor on a council. Give your single best recommendation for the "
             "question, grounded in the provided context. Be decisive, state the key tradeoffs, and name the "
             "biggest risk or blind spot in your own recommendation. Answer ONLY the question asked; propose nothing beyond it (no extra features, files, migrations, or scope). 200 words max.")

def _panelist(model, question, context):
    out = call_model(model, PANEL_SYS, f"QUESTION:\n{question}\n\nCONTEXT:\n{context}", role="panel")
    return {"model": model, "opinion": out}

def fusion(question, context, preset=None):
    resolved = resolve_council_models(preset=preset)
    panel = resolved["panel"]
    with cf.ThreadPoolExecutor(max_workers=len(panel)) as ex:
        opinions = list(ex.map(lambda m: _panelist(m, question, context), panel))

    panel_block = "\n\n".join(f"### Advisor {i+1} ({o['model']})\n{o['opinion']}" for i, o in enumerate(opinions))
    judge_sys = ("You are the council judge. You are given a QUESTION and several advisor opinions. Produce a "
                 "neutral analysis: where they AGREE, where they CONTRADICT each other, and what BLIND SPOTS "
                 "they collectively miss. Do not pick a winner; surface the decision-relevant tensions. 250 words max.")
    judge = call_model(resolved["judge"], judge_sys, f"QUESTION:\n{question}\n\nOPINIONS:\n{panel_block}", role="judge")

    synth_sys = ("You are the council synthesizer. Given the QUESTION, the advisor opinions, and the judge's "
                 "analysis, produce the FINAL recommendation. Output EXACTLY one fenced json object with keys: "
                 "synthesis (the decisive recommendation + brief rationale), contradictions (array of the real "
                 "disagreements that remain), blindspots (array of risks/unknowns the human should watch). "
                 "The synthesis must answer ONLY the question asked and propose nothing beyond the spec (no new features, files, migrations, or scope). Output ONLY the fenced json block.")
    synth_user = f"QUESTION:\n{question}\n\nOPINIONS:\n{panel_block}\n\nJUDGE ANALYSIS:\n{judge}"
    try:
        synth_raw = call_model(resolved["synth"], synth_sys, synth_user, role="synth")
    except hl.CircuitOpen:
        synth_raw = "[CIRCUIT OPEN] proceed with best judgment from the judge analysis"
    parsed = _extract_json(synth_raw) or {"synthesis": synth_raw, "contradictions": [], "blindspots": []}
    parsed["_panel"] = opinions
    parsed["_judge"] = judge
    return parsed

if __name__ == "__main__":
    args = sys.argv[1:]
    preset = None
    if "--preset" in args:
        i = args.index("--preset"); preset = args[i + 1]; del args[i:i + 2]
    question = args[0] if args else "(no question)"
    context = sys.stdin.read() if not sys.stdin.isatty() else ""
    result = fusion(question, context, preset=preset)
    print(json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, indent=2))
    sys.stderr.write(f"\n[panel={len(result['_panel'])} models, preset={preset or 'full'}]\n")
