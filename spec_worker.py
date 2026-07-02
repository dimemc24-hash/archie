#!/usr/bin/env python3
"""
Stage-1 dev worker — the host side of the async bridge. Polls Archie's spec_request queue
(assistant_notes in his Supabase, written by the sandboxed chat via queue_spec.py), authors a full
build bundle with Opus, runs emit_spec.py --push, and pings Morley. Runs on the HOST (dev context:
GitHub key + ~/harness/repo). This is what makes Stage-1 phone-drivable without un-sandboxing chat.

Run from system cron (every ~5 min) or manually:  spec_worker.py [--dry-run]
"""
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request

HARNESS = os.path.expanduser("~/harness")
ENV = os.path.expanduser("~/.hermes/.env")
AUTH = os.path.expanduser("~/.hermes/auth.json")
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
SEEN = os.path.join(HARNESS, ".spec_worker_seen")
EMIT = os.path.expanduser("~/.hermes/skills/harness/stage1-spec/scripts/emit_spec.py")
FILTER = os.path.expanduser("~/.hermes/skills/archie/persona/persona_filter.py")

def envv(k):
    if os.environ.get(k):
        return os.environ[k]
    try:
        for ln in open(ENV):
            if ln.startswith(k + "="):
                return ln.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""

def or_key():
    return json.load(open(AUTH))["credential_pool"]["openrouter"][0]["access_token"]

def opus(system, user, max_tokens=3500):
    body = json.dumps({"model": "deepseek/deepseek-v4-pro", "max_tokens": max_tokens, "temperature": 0.3,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(OR_URL, data=body, headers={
        "Authorization": "Bearer " + or_key(), "Content-Type": "application/json",
        "HTTP-Referer": "https://fsfai.archie", "X-Title": "spec-worker"})
    return json.load(urllib.request.urlopen(req, timeout=240))["choices"][0]["message"]["content"]

def fetch_requests():
    url = envv("ARCHIE_SUPABASE_URL"); key = envv("ARCHIE_SUPABASE_SERVICE_KEY")
    q = "/rest/v1/assistant_notes?topic=eq.spec_request&order=created_at.desc&limit=20"
    req = urllib.request.Request(url.rstrip("/") + q, headers={"apikey": key, "Authorization": "Bearer " + key})
    return json.load(urllib.request.urlopen(req, timeout=30))

def telegram(text):
    tok = envv("TELEGRAM_BOT_TOKEN"); chat = envv("TELEGRAM_HOME_CHANNEL") or envv("TELEGRAM_ALLOWED_USERS").split(",")[0]
    if not tok or not chat:
        return
    try:
        text = subprocess.run(["python3", FILTER], input=text, capture_output=True, text=True, timeout=20).stdout.strip() or text
    except Exception:
        pass
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()), timeout=20)
    except Exception:
        pass

AUTHOR_SYS = (
    "You author a dev-harness Stage-1 spec bundle from Morley's feature request for the NewChapter app "
    "(Next.js 16 / React 19 / TypeScript strict / Tailwind / Supabase). Return ONLY one JSON object with keys: "
    "slug (kebab-case, 3-5 words), build_spec_md, build_prompt_md, checkpoint_manifest. "
    "build_prompt_md is the SELF-CONTAINED launch prompt the build agent executes (it only sees this file but can "
    "read repo files) and MUST embed the sentinel protocol verbatim: 'When you reach a decision checkpoint, emit "
    "exactly CHECKPOINT_REACHED:<id> and STOP; await injected guidance. When done AND `npm run build` is green, emit "
    "exactly BUILD_COMPLETE.' checkpoint_manifest = {\"checkpoints\":[{\"id\":..,\"trigger\":..,\"question\":..,"
    "\"blocking\":true,\"criteria\":[..],\"panel_override\":null,\"on_synthesis\":..}]} with ONE checkpoint at the "
    "single real design decision. Strongly prefer SCHEMA-FREE (no migrations/new tables). Point the agent at the "
    "specific files/area. Be precise and minimal.")

def main():
    dry = "--dry-run" in sys.argv
    seen = set(open(SEEN).read().split()) if os.path.exists(SEEN) else set()
    reqs = [r for r in fetch_requests() if r["id"] not in seen]
    print(f"[spec_worker] {len(reqs)} new spec_request(s)")
    for row in reqs:
        try:
            intent = json.loads(row.get("body") or "{}")
        except Exception:
            intent = {"feature": row.get("body", "")}
        raw = opus(AUTHOR_SYS, json.dumps(intent))
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            print(f"[spec_worker] author failed for {row['id']}"); continue
        try:
            b = json.loads(m.group(0))
        except Exception as e:
            print(f"[spec_worker] bad JSON for {row['id']}: {e}"); continue
        rid = time.strftime("%Y-%m-%d") + "-" + re.sub(r"[^a-z0-9-]", "", b.get("slug", "spec").lower())[:40]
        d = f"/tmp/spec_{rid}"; os.makedirs(os.path.join(d, "kb"), exist_ok=True)
        open(d + "/build-spec.md", "w").write(b["build_spec_md"])
        open(d + "/build-prompt.md", "w").write(b["build_prompt_md"])
        json.dump(b["checkpoint_manifest"], open(d + "/checkpoint-manifest.json", "w"), indent=2)
        if dry:
            print(f"[dry] request {row['id']} -> run-id {rid}\n--- build-prompt (head) ---\n{b['build_prompt_md'][:400]}\n")
            continue
        rc = subprocess.run(["python3", EMIT, "--run-id", rid, "--spec", d + "/build-spec.md",
                             "--prompt", d + "/build-prompt.md", "--manifest", d + "/checkpoint-manifest.json",
                             "--kb", d + "/kb", "--push"], capture_output=True, text=True)
        ok = rc.returncode == 0
        feat = (intent.get("feature", "") or "")[:90]
        telegram(f"Stage-1: I turned your chat request into a spec and pushed it — harness/spec/{rid} ({feat}). "
                 f"Build it anytime with stage2_build.py --run-id {rid}." if ok else
                 f"Stage-1 emit failed for your request '{feat}': {(rc.stdout + rc.stderr)[-200:]}")
        with open(SEEN, "a") as fh:
            fh.write(row["id"] + "\n")
        print(("[spec_worker] emitted " + rid) if ok else ("[spec_worker] FAILED " + rid + " :: " + rc.stdout[-200:] + rc.stderr[-200:]))

if __name__ == "__main__":
    main()
