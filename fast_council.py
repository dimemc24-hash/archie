#!/usr/bin/env python3
"""
fast_council.py - self-serve advisory council for Archie.

STRATEGY (2026-06-28): cheap-PAID models are the reliable backbone; FREE models are
opportunistic gap-fill (best-effort, dropped if throttled). Self-hosted (tier="local")
arrives later as a free-AND-private perk when hardware exists for some other reason.

Returns RAW opinions for the caller to integrate (no judge/synth). Distinct from
fusion.py (the forced build-checkpoint council).

Privacy gate: sensitivity="firm" -> PAID backbone ONLY (paid API tiers generally don't
train on inputs; free tiers do). Personal questions may add free gap-fill seats.
"""
import json, os, sys, time, urllib.request
import concurrent.futures as cf

OR="https://openrouter.ai/api/v1/chat/completions"
AUTH=os.path.expanduser("~/.hermes/auth.json")
ENV=os.path.expanduser("~/.hermes/.env")

REGISTRY=[
    {"name":"gpt-4o-mini","base_url":OR,"source":"openrouter","model":"openai/gpt-4o-mini","tier":"paid"},
    {"name":"deepseek","base_url":OR,"source":"openrouter","model":"deepseek/deepseek-v4-pro","tier":"paid"},
    {"name":"gemini-flash","base_url":OR,"source":"openrouter","model":"google/gemini-3.5-flash","tier":"paid"},
    {"name":"kimi","base_url":OR,"source":"openrouter","model":"moonshotai/kimi-k2.6","tier":"paid"},
    {"name":"nemotron","base_url":OR,"source":"openrouter","model":"nvidia/nemotron-3-super-120b-a12b:free","tier":"free"},
    {"name":"gpt-oss","base_url":OR,"source":"openrouter","model":"openai/gpt-oss-120b:free","tier":"free"},
    {"name":"qwen-coder","base_url":OR,"source":"openrouter","model":"qwen/qwen3-coder:free","tier":"free"},
    {"name":"llama-70b","base_url":OR,"source":"openrouter","model":"meta-llama/llama-3.3-70b-instruct:free","tier":"free"},
]

PANEL_SYS=(
    "You are a senior advisor on a fast council. Answer ONLY the question asked, grounded in any "
    "context. Give your single best recommendation, state the key tradeoff, and name the biggest risk "
    "in your own pick. Propose NOTHING beyond the question - no extra features, files, migrations, or "
    "scope. 150 words max.")

def _key(source):
    if source=="openrouter":
        k=os.environ.get("OPENROUTER_API_KEY")
        if k: return k.strip()
        return json.load(open(AUTH))["credential_pool"]["openrouter"][0]["access_token"]
    if os.environ.get(source): return os.environ[source].strip()
    try:
        for ln in open(ENV):
            if ln.startswith(source+"="): return ln.split("=",1)[1].strip()
    except Exception: pass
    return ""

def _call(entry,question,context,timeout=60):
    key=_key(entry["source"])
    user="QUESTION:\n"+question+(("\n\nCONTEXT:\n"+context) if context else "")
    body=json.dumps({"model":entry["model"],"temperature":0.4,
        "messages":[{"role":"system","content":PANEL_SYS},{"role":"user","content":user}]}).encode()
    req=urllib.request.Request(entry["base_url"],data=body,headers={
        "Authorization":"Bearer "+key,"Content-Type":"application/json",
        "HTTP-Referer":"https://fsfai.archie","X-Title":"fast-council"})
    t0=time.time()
    r=json.load(urllib.request.urlopen(req,timeout=timeout))
    return r["choices"][0]["message"]["content"].strip(), round(time.time()-t0,1)

def _seat(entry,question,context):
    err="no response"
    for attempt in (1,2):
        try:
            op,dt=_call(entry,question,context)
            if op:
                return {"provider":entry["name"],"model":entry["model"],"tier":entry["tier"],"latency_s":dt,"ok":True,"opinion":op}
        except Exception as e:
            err=type(e).__name__+": "+str(e)[:80]
        if attempt==1: time.sleep(1.0)
    return {"provider":entry["name"],"model":entry["model"],"tier":entry["tier"],"latency_s":None,"ok":False,"opinion":"[FAILED "+err+"]"}

def council(question,context="",sensitivity="personal",gapfill=True):
    paid=[e for e in REGISTRY if e["tier"]=="paid"]
    free=[e for e in REGISTRY if e["tier"]=="free"]
    seats=list(paid)
    if sensitivity!="firm" and gapfill: seats+=free
    with cf.ThreadPoolExecutor(max_workers=max(1,len(seats))) as ex:
        ops=list(ex.map(lambda e:_seat(e,question,context),seats))
    return {"question":question,"sensitivity":sensitivity,"opinions":ops}

if __name__=="__main__":
    args=sys.argv[1:]
    sens="personal"; gapfill=True
    if "--firm" in args: sens="firm"; args.remove("--firm")
    if "--no-gapfill" in args: gapfill=False; args.remove("--no-gapfill")
    q=args[0] if args else (
        "Secret-authed routes have no user JWT. How should firm_id be resolved for the wiki_topics / "
        "cases queries: (a) a single-firm constant or env var, (b) a required firm_id query param / body "
        "field, or (c) derive it from the sole firm row? Single-firm today; consider multi-firm safety.")
    res=council(q,sensitivity=sens,gapfill=gapfill)
    print("=== FAST council ("+res["sensitivity"]+") - "+str(len(res["opinions"]))+" seats ===\n")
    for o in res["opinions"]:
        print("### "+o["provider"]+" ["+o["tier"]+"] "+str(o["latency_s"])+"s ok="+str(o["ok"])+"\n"+o["opinion"][:380]+"\n")
    pok=sum(1 for o in res["opinions"] if o["ok"] and o["tier"]=="paid"); ptot=sum(1 for o in res["opinions"] if o["tier"]=="paid")
    fok=sum(1 for o in res["opinions"] if o["ok"] and o["tier"]=="free"); ftot=sum(1 for o in res["opinions"] if o["tier"]=="free")
    print("[summary] backbone "+str(pok)+"/"+str(ptot)+" paid ok; gap-fill "+str(fok)+"/"+str(ftot)+" free ok")
