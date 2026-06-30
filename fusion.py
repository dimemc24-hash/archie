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
SYNTH_FULL   = "anthropic/claude-opus-4.6"
SYNTH_BUDGET = "anthropic/claude-sonnet-4.6"

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
    panel = PANEL_BUDGET if preset == "budget" else PANEL_FULL
    with cf.ThreadPoolExecutor(max_workers=len(panel)) as ex:
        opinions = list(ex.map(lambda m: _panelist(m, question, context), panel))

    panel_block = "\n\n".join(f"### Advisor {i+1} ({o['model']})\n{o['opinion']}" for i, o in enumerate(opinions))
    judge_sys = ("You are the council judge. You are given a QUESTION and several advisor opinions. Produce a "
                 "neutral analysis: where they AGREE, where they CONTRADICT each other, and what BLIND SPOTS "
                 "they collectively miss. Do not pick a winner; surface the decision-relevant tensions. 250 words max.")
    judge = call_model(JUDGE, judge_sys, f"QUESTION:\n{question}\n\nOPINIONS:\n{panel_block}", role="judge")

    synth_sys = ("You are the council synthesizer. Given the QUESTION, the advisor opinions, and the judge's "
                 "analysis, produce the FINAL recommendation. Output EXACTLY one fenced json object with keys: "
                 "synthesis (the decisive recommendation + brief rationale), contradictions (array of the real "
                 "disagreements that remain), blindspots (array of risks/unknowns the human should watch). "
                 "The synthesis must answer ONLY the question asked and propose nothing beyond the spec (no new features, files, migrations, or scope). Output ONLY the fenced json block.")
    synth_user = f"QUESTION:\n{question}\n\nOPINIONS:\n{panel_block}\n\nJUDGE ANALYSIS:\n{judge}"
    try:
        synth_model = SYNTH_BUDGET if preset == "budget" else SYNTH_FULL
        synth_raw = call_model(synth_model, synth_sys, synth_user, role="synth")
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
