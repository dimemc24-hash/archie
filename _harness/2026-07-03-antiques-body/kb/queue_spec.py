#!/usr/bin/env python3
"""
Queue a Stage-1 spec request from a phone chat. Runs in the SANDBOXED default profile — pure REST to
Archie's own Supabase using the forwarded ARCHIE_SUPABASE_* creds (no host access needed). The host-side
dev worker (spec_worker.py) picks it up, authors the bundle with Opus, and pushes harness/spec/<id>.

This is the bridge that makes Stage-1 phone-drivable without un-sandboxing chat.

Usage:
  queue_spec.py --feature "<what to build>" [--area "<files/route>"] [--acceptance "<done when>"] [--notes "<forks/decisions>"]
"""
import argparse, json, os, sys, urllib.request

URL = os.environ.get("ARCHIE_SUPABASE_URL")
KEY = os.environ.get("ARCHIE_SUPABASE_SERVICE_KEY")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feature", required=True)
    ap.add_argument("--area", default="")
    ap.add_argument("--acceptance", default="")
    ap.add_argument("--notes", default="")
    a = ap.parse_args()
    if not URL or not KEY:
        sys.exit("ERROR: ARCHIE_SUPABASE_URL/SERVICE_KEY not in env (need the sandbox docker_forward_env entries)")
    payload = {
        "topic": "spec_request",
        "body": json.dumps({"feature": a.feature, "area": a.area, "acceptance": a.acceptance, "notes": a.notes}),
        "tags": ["spec_request", "queued"],
    }
    req = urllib.request.Request(
        URL.rstrip("/") + "/rest/v1/assistant_notes",
        data=json.dumps(payload).encode(),
        headers={"apikey": KEY, "Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json", "Prefer": "return=representation"})
    r = json.load(urllib.request.urlopen(req, timeout=30))
    print("QUEUED spec_request", (r[0]["id"] if r else "?"), "— the dev worker will author + push it and ping Morley.")

if __name__ == "__main__":
    main()
