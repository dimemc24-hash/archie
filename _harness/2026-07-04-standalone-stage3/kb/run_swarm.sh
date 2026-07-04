#!/usr/bin/env bash
# Stage-3 swarm transport entry (Phase D). Invoked by DO over SSH AFTER it has pushed the build
# branch and a `gh-main` baseline into this repo. Checks out the build as fix/<id>, derives the
# changed-file scope, runs peanut_wheel.py (fixes commit onto fix/<id>), writes Stage-4 handoff
# artifacts, and leaves fix/<id> ready for DO to fetch.
#
#   run_swarm.sh <build-branch> [routes-csv] [waves]
#
# Self-fixes PATH: node/npm/npx/hermes live in ~/.local/bin, absent from the non-interactive PATH.
#
# Exit code IS the run outcome (transport.sh gates the origin push on it): 0 success/degraded,
# non-zero failure. A completion sentinel ~/swarm/status/<id>.done is written LAST (even on most
# failures) so transport can confirm completion if the SSH pipe drops mid-run.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

SWARM="$HOME/swarm"
REPO="$SWARM/newchapter"
BUILD_BRANCH="${1:?usage: run_swarm.sh <build-branch> [routes] [waves]}"
ROUTES="${2:-}"
WAVES="${3:-3}"
ID="${BUILD_BRANCH#build/}"
FIX_BRANCH="fix/${ID}"
BASE="gh-main"
SAFE_ID="${ID//[^A-Za-z0-9._-]/_}"
LOG="$SWARM/run_swarm_${SAFE_ID}.log"
STATUS_DIR="$SWARM/status"; mkdir -p "$STATUS_DIR"
DONE="$STATUS_DIR/${SAFE_ID}.done"
rm -f "$DONE" 2>/dev/null || true   # clear any stale sentinel so transport never reads an old run

STATUS="success"; RC=0
# Write the completion sentinel on ANY exit so a crashed run still signals transport (vs. a hang).
finish() {
  local code="$1"
  local sha; sha="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  printf '{"id":"%s","rc":%s,"status":"%s","fix_sha":"%s","fixes":%s}\n' \
    "$ID" "$code" "$STATUS" "$sha" "${FIXES:-0}" > "$DONE"
  echo "[run_swarm] sentinel: $(cat "$DONE")" | tee -a "$LOG" 2>/dev/null || true
  exit "$code"
}
fail() { STATUS="failed"; echo "[run_swarm] FATAL: $1" | tee -a "$LOG" 2>/dev/null || true; finish "${2:-3}"; }

echo "[run_swarm] $(date -Is) build=$BUILD_BRANCH fix=$FIX_BRANCH routes='$ROUTES' waves=$WAVES" | tee "$LOG"
cd "$REPO" || fail "no $REPO" 2

# Protect swarm-local (git-ignored) files from `git clean -fdq` across any branch checkout.
for f in swarm_capture.mjs .env.local; do
  grep -qxF "$f" .git/info/exclude 2>/dev/null || echo "$f" >> .git/info/exclude
done

# Sanity: DO must have pushed both refs into this repo.
for ref in "$BUILD_BRANCH" "$BASE"; do
  git rev-parse --verify "$ref" >/dev/null 2>&1 || fail "ref '$ref' not present (DO must push it first)" 3
done

# Fresh fix branch from the build.
git checkout -f "$BUILD_BRANCH" >/dev/null 2>&1 || fail "cannot checkout $BUILD_BRANCH" 3
git checkout -B "$FIX_BRANCH" >/dev/null 2>&1 || fail "cannot create $FIX_BRANCH" 3
echo "[run_swarm] on $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)" | tee -a "$LOG"

# Scope from the diff (base..build).
CHANGED="$(git diff --name-only "$BASE".."$BUILD_BRANCH")"
echo "[run_swarm] changed files:" | tee -a "$LOG"; echo "$CHANGED" | sed 's/^/  /' | tee -a "$LOG"
CODE="$(echo "$CHANGED" | grep -E '\.(ts|tsx)$' | grep -vE '(\.test\.|/__tests__/|^_harness/|\.d\.ts$)' | paste -sd, -)"
echo "[run_swarm] code scope: ${CODE:-<none>}" | tee -a "$LOG"

# Gate visibility: did this build actually pass the hivemind forced-checkpoint gate? checkpoint-log
# present == >=1 checkpoint was consulted. A swarm-borrow AND a turnstile-jump both show gated=false,
# so Stage 4 can tell a fully-gated autonomous build from one that bypassed the gate.
CKPT="$REPO/_harness/$ID/checkpoint-log.json"
if [ -f "$CKPT" ]; then HIVEMIND_GATED=true; NCP="$(python3 -c "import json;print(len(json.load(open('$CKPT'))))" 2>/dev/null || echo 0)"
else HIVEMIND_GATED=false; NCP=0; fi
echo "[run_swarm] hivemind_gated=$HIVEMIND_GATED (checkpoints consulted=$NCP)" | tee -a "$LOG"

# Deps: npm ci only if the lockfile changed.
if echo "$CHANGED" | grep -qx 'package-lock.json'; then
  echo "[run_swarm] package-lock changed -> npm ci" | tee -a "$LOG"
  npm ci --legacy-peer-deps >"$SWARM/npm-ci.log" 2>&1 || echo "[run_swarm] WARN npm ci failed (see npm-ci.log)" | tee -a "$LOG"
else
  echo "[run_swarm] deps unchanged -> skip npm ci" | tee -a "$LOG"
fi

# Dev server: only needed for the vision lane (routes). If it never comes up, drop the vision lane and
# mark the run DEGRADED rather than silently feeding the wheel routes it can't screenshot.
DEV_OK=1
if [ -n "$ROUTES" ]; then
  pkill -f "next dev" 2>/dev/null || true; sleep 1
  nohup npm run dev >"$SWARM/devserver.log" 2>&1 &
  echo "[run_swarm] dev server pid $!; waiting for :3000" | tee -a "$LOG"
  DEV_OK=0
  for i in $(seq 1 60); do curl -sf http://localhost:3000 >/dev/null 2>&1 && { DEV_OK=1; break; }; sleep 2; done
  if [ "$DEV_OK" = 1 ]; then echo "[run_swarm] dev up" | tee -a "$LOG"
  else
    echo "[run_swarm] WARN dev server never came up -> skipping vision lane (DEGRADED)" | tee -a "$LOG"
    STATUS="degraded"; ROUTES=""
  fi
fi

# Snapshot the global fix-log line count so the per-run handoff includes only THIS run's entries.
FIXLOG_OFFSET="$(wc -l < "$SWARM/fix-log.jsonl" 2>/dev/null | tr -d ' ')"; FIXLOG_OFFSET="${FIXLOG_OFFSET:-0}"

# Run the wheel — peanut_wheel commits each accepted fix onto $FIX_BRANCH.
WHEEL_RC=0
if [ -z "$CODE" ] && [ -z "$ROUTES" ]; then
  echo "[run_swarm] no code scope, no routes -> skipping wheel (fix == build)" | tee -a "$LOG"
else
  ARGS=(--waves "$WAVES"); [ -n "$CODE" ] && ARGS+=(--code "$CODE"); [ -n "$ROUTES" ] && ARGS+=(--routes "$ROUTES")
  ARGS+=(--findings-out "$REPO/_harness/$ID/swarm-findings.json")
  # Swarm is DETECT-ONLY: it commits NO fixes — just emits a raw findings work-order for the triage brain
  # (Hetzner) → which filters/consolidates/categorizes/tiers → Archie (DO) determines + builds.
  echo "[run_swarm] python3 -u peanut_wheel.py ${ARGS[*]} (detect-only; gated=$HIVEMIND_GATED)" | tee -a "$LOG"
  python3 -u "$SWARM/peanut_wheel.py" "${ARGS[@]}" 2>&1 | tee -a "$LOG"   # -u: flush progress live
  WHEEL_RC="${PIPESTATUS[0]}"
  echo "[run_swarm] wheel rc=$WHEEL_RC" | tee -a "$LOG"
  if [ "$WHEEL_RC" != 0 ]; then STATUS="failed"; RC="$WHEEL_RC"; fi
fi
FINDINGS_N="$(python3 -c "import json;print(len(json.load(open('$REPO/_harness/$ID/swarm-findings.json')).get('findings',[])))" 2>/dev/null || echo 0)"
echo "[run_swarm] findings detected: $FINDINGS_N (raw work-order)" | tee -a "$LOG"

# Stage 2 — TRIAGE BRAIN (Hetzner): budget-tier review filters/consolidates/categorizes/tiers the raw
# findings into _harness/<id>/swarm-workorder.json for Archie. The swarm is dumb; this is the brain.
BATCHES_N=0
if [ "${FINDINGS_N:-0}" -gt 0 ] 2>/dev/null; then
  echo "[run_swarm] triage.py over $FINDINGS_N findings..." | tee -a "$LOG"
  python3 -u "$SWARM/triage.py" "$ID" 2>&1 | tee -a "$LOG"
  BATCHES_N="$(python3 -c "import json;print(len(json.load(open('$REPO/_harness/$ID/swarm-workorder.json')).get('batches',[])))" 2>/dev/null || echo 0)"
fi
echo "[run_swarm] triage: $BATCHES_N batches for Archie" | tee -a "$LOG"

# Stage-4 handoff artifacts committed onto the fix branch (always — even on a failed wheel, so Stage 4
# can see what landed before the crash).
FIXES="$(git rev-list --count "$BUILD_BRANCH".."$FIX_BRANCH" 2>/dev/null || echo 0)"
mkdir -p "_harness/$ID"
python3 - "$ID" "$BUILD_BRANCH" "$FIX_BRANCH" "$FIXES" "$CODE" "$ROUTES" "$WAVES" "$FIXLOG_OFFSET" "$STATUS" "$HIVEMIND_GATED" "$NCP" "$FINDINGS_N" <<'PY'
import json, sys, os
id_, bb, fb, fixes, code, routes, waves, offset, status, gated, ncp, findings_n = sys.argv[1:13]
swarm = os.path.expanduser("~/swarm")
fixlog = []
p = os.path.join(swarm, "fix-log.jsonl")
if os.path.exists(p):
    for ln in open(p).readlines()[int(offset):]:
        ln = ln.strip()
        if ln:
            try: fixlog.append(json.loads(ln))
            except Exception: pass
d = os.path.join("_harness", id_); os.makedirs(d, exist_ok=True)
json.dump({"run_id": id_, "build_branch": bb, "fix_branch": fb, "status": status,
           "hivemind_gated": gated == "true", "checkpoints_consulted": int(ncp),
           "findings_detected": int(findings_n), "fixes_committed": int(fixes),
           "code_scope": [c for c in code.split(",") if c], "routes": [r for r in routes.split(",") if r],
           "waves": int(waves)}, open(os.path.join(d, "swarm-report.json"), "w"), indent=2)
json.dump(fixlog, open(os.path.join(d, "fix-log.json"), "w"), indent=2)
print(f"[run_swarm] wrote swarm-report.json (status={status} fixes={fixes}) + fix-log.json (entries={len(fixlog)})")
PY
git add "_harness/$ID"
git commit -qm "stage3 swarm report ($ID): $STATUS, $FIXES fixes" >/dev/null 2>&1 \
  && echo "[run_swarm] committed report" | tee -a "$LOG" \
  || echo "[run_swarm] no report change to commit" | tee -a "$LOG"

echo "[run_swarm] DONE: $FIX_BRANCH @ $(git rev-parse --short HEAD) status=$STATUS ($FIXES fix commits)" | tee -a "$LOG"
finish "$RC"
