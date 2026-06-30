#!/usr/bin/env python3
"""
council_eval.py - does the FAST council help Archie, or hurt him?

Arms per question:
  solo : Archie's base model (glm-5.2) answers alone.
  fast : fast_council (cheap-paid backbone) -> glm INTEGRATES the opinions -> decision.
  esc  : escalation-aware: a STAKES classifier flags high-stakes Qs -> full Fusion (opus
         synth); low-stakes -> fast. (Divergence triggers fail on FALSE CONSENSUS, so the
         trigger is STAKES-based.)
Reference: T1 = recorded full-Fusion synthesis from artifacts.

  python3 council_eval.py --smoke   # firm_id through solo/fast/stakes, no judge/no full-Fusion (cheap)
"""
import json, os, sys, time, re, glob, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fast_council as fc

OR = fc.OR
GLM = "z-ai/glm-5.2"

def _chat(model, system, user, timeout=90, temperature=0.3):
    body=json.dumps({"model":model,"temperature":temperature,
        "messages":[{"role":"system","content":system},{"role":"user","content":user}]}).encode()
    req=urllib.request.Request(OR,data=body,headers={"Authorization":"Bearer "+fc._key("openrouter"),
        "Content-Type":"application/json","HTTP-Referer":"https://fsfai.archie","X-Title":"council-eval"})
    t0=time.time(); r=json.load(urllib.request.urlopen(req,timeout=timeout))
    return r["choices"][0]["message"]["content"].strip(), round(time.time()-t0,1)

def _pick(t):
    m=re.search(r"\(([abc])\)", t.lower())
    return m.group(1) if m else "?"

SOLO_SYS=("You are Archie, a senior engineering advisor. Give your single best recommendation, the key "
  "tradeoff, and the biggest risk. Be decisive. 150 words max.")
INTEGRATE_SYS=("You are Archie. Below are several advisors' opinions. Treat them as INPUT, not verdict - "
  "they may share blind spots and a unanimous panel can be confidently wrong. Weigh them with your own "
  "judgment and give YOUR single best recommendation, key tradeoff, and biggest risk. 150 words max.")
STAKES_SYS=("Classify whether a question is HIGH-STAKES for a software/legal-tech system. HIGH if it "
  "touches security, auth, access control, money, firm/client data exposure, cross-tenant isolation, or "
  "anything hard to reverse. Else LOW. Output ONLY one word: HIGH or LOW.")

def arm_solo(q, ctx=""):
    out,dt=_chat(GLM,SOLO_SYS,"QUESTION:\n"+q+(("\n\nCONTEXT:\n"+ctx) if ctx else ""))
    return {"answer":out,"latency_s":dt}

def arm_fast(q, ctx=""):
    res=fc.council(q,context=ctx,sensitivity="personal")
    ops=[o for o in res["opinions"] if o["ok"]]
    block="\n\n".join("Advisor "+str(i+1)+" ("+o["provider"]+"):\n"+o["opinion"] for i,o in enumerate(ops))
    out,dt=_chat(GLM,INTEGRATE_SYS,"QUESTION:\n"+q+"\n\nADVISOR OPINIONS:\n"+block)
    return {"answer":out,"latency_s":dt,"n":len(ops),"panel_picks":[_pick(o["opinion"]) for o in ops]}

def stakes(q):
    out,_=_chat(GLM,STAKES_SYS,q,temperature=0.0)
    return "HIGH" if "HIGH" in out.upper() else "LOW"

def _t1():
    items=[]; seen=set()
    for lg in sorted(glob.glob(os.path.expanduser("~/harness/artifacts/*/checkpoint-log.json"))):
        for r in json.load(open(lg)):
            q=r.get("question") or ""
            if q and q not in seen:
                seen.add(q); items.append({"q":q,"ref":str(r.get("synthesis"))})
    return items

def smoke():
    it=[x for x in _t1() if "firm_id" in x["q"].lower()][0]
    refpick=_pick(it["ref"])
    print("SMOKE - canonical hard case (firm_id)\nQUESTION:\n"+it["q"][:380]+"\n")
    print("REFERENCE full-Fusion: pick=("+refpick+")\n"+it["ref"][:180]+"\n"+"="*72)
    s=arm_solo(it["q"]); print("\n[SOLO glm] "+str(s["latency_s"])+"s  pick=("+_pick(s["answer"])+")\n"+s["answer"][:320])
    f=arm_fast(it["q"]); print("\n[FAST glm-integrates-council] "+str(f["latency_s"])+"s  pick=("+_pick(f["answer"])+")  panel_picks="+str(f["panel_picks"])+"\n"+f["answer"][:320])
    lvl=stakes(it["q"]); print("\n[ESC] stakes-classifier="+lvl+"  -> escalate="+str(lvl=="HIGH"))
    print("\n"+"="*72)
    print("VERDICT  reference=("+refpick+")  solo=("+_pick(s["answer"])+")  fast=("+_pick(f["answer"])+")  esc-trigger="+lvl)
    if _pick(s["answer"])!=refpick and lvl=="HIGH":
        print("=> solo is WRONG and stakes-trigger FIRES: escalation would route this to full-Fusion (which got it right).")

if __name__=="__main__":
    if "--smoke" in sys.argv: smoke()
    else: print("use --smoke")
