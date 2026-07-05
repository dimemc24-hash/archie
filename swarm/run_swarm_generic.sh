#!/usr/bin/env bash
# Stage-3 swarm transport entry — GENERIC (profile-driven). Invoked by DO over SSH
# AFTER it has pushed the build branch and a `gh-main` baseline into the target repo.
#
# This is the SIDE-BY-SIDE generic runner (decision: option c). It lives in the
# repo at swarm/run_swarm_generic.sh, is rsynced to ~/swarm/generic/ on each
# profile transport, and NEVER touches Morley's live ~/swarm/run_swarm.sh /
# peanut_wheel.py / triage.py (the NextChapter trio). The live files stay
# hand-managed on the box; this generic runner is repo-tracked and git-tuned.
#
# Reads per-repo config from .swarm.json at the CHECKED-OUT REPO ROOT (decision:
# verify-lane-shape option c — config travels with the code that reads it). The
# file is committed in each target repo and is present in the working tree after
# checkout — no cross-box config rsync needed. When .swarm.json is absent, the
# runner falls back to hardcoded NextChapter defaults AND emits a clear warning
# (silent-fallback blindspot: a missing config must never produce a false-green).
#
# Config fields (see swarm/README.md for the full schema):
#   - code-scope pattern (e.g. \.py$ vs \.(ts|tsx)$)
#   - deps step (e.g. "npm ci --legacy-peer-deps" or "none")
#   - dev-server / vision lane (enabled + command, or skipped)
#   - verify command (e.g. "python3 -m pytest antiques/test_antiques.py -q")
#
# The repo-agnostic contract is identical to the live runner:
#   - checks out build/<id> as fix/<id>
#   - derives changed-file scope from gh-main..build/<id>
#   - runs peanut_wheel.py (DETECT-ONLY) → swarm-findings.json
#   - runs triage.py → swarm-workorder.json
#   - commits _harness/<id>/{swarm-report.json,fix-log.json} onto fix/<id>
#   - writes sentinel ~/swarm/status/<id>.done (JSON) LAST on ANY exit
#
# Usage (called by transport.sh over SSH):
#   run_swarm_generic.sh <repo-name> <build-branch> [routes-csv] [waves]
#
# Where <repo-name> identifies the target repo (~/swarm/<repo-name>).
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

SWARM="$HOME/swarm"
GENERIC="$SWARM/generic"

REPO_NAME="${1:?usage: run_swarm_generic.sh <repo-name> <build-branch> [routes] [waves]}"
BUILD_BRANCH="${2:?usage: run_swarm_generic.sh <repo-name> <build-branch> [routes] [waves]}"
ROUTES="${3:-}"
WAVES="${4:-3}"
ID="${BUILD_BRANCH#build/}"
FIX_BRANCH="fix/${ID}"
BASE="gh-main"
SAFE_ID="${ID//[^A-Za-z0-9._-]/_}"
LOG="$SWARM/run_swarm_${SAFE_ID}.log"
STATUS_DIR="$SWARM/status"; mkdir -p "$STATUS_DIR"
DONE="$STATUS_DIR/${SAFE_ID}.done"
rm -f "$DONE" 2>/dev/null || true

STATUS="success"; RC=0

REPO="$SWARM/$REPO_NAME"

echo "[run_swarm_generic] $(date -Is) repo=$REPO_NAME build=$BUILD_BRANCH fix=$FIX_BRANCH routes='$ROUTES' waves=$WAVES" | tee "$LOG"

# Write the completion sentinel on ANY exit so a crashed run still signals transport.
finish() {
  local code="$1"
  local sha; sha="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  printf '{"id":"%s","rc":%s,"status":"%s","fix_sha":"%s","fixes":%s}\n' \
    "$ID" "$code" "$STATUS" "$sha" "${FIXES:-0}" > "$DONE"
  echo "[run_swarm_generic] sentinel: $(cat "$DONE")" | tee -a "$LOG" 2>/dev/null || true
  exit "$code"
}
fail() { STATUS="failed"; echo "[run_swarm_generic] FATAL: $1" | tee -a "$LOG" 2>/dev/null || true; finish "${2:-3}"; }

cd "$REPO" || fail "no $REPO (bootstrap needed? transport.sh should git init on first run)" 2

# ── Bootstrap check: repo must exist (transport.sh inits it if missing) ───────
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
echo "[run_swarm_generic] on $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)" | tee -a "$LOG"

# ── Per-repo config (from .swarm.json at the checked-out repo root) ──────────
# Decision: verify-lane-shape option (c) — the config travels with the code.
# .swarm.json is committed in the target repo and present in the working tree
# after checkout. No cross-box config rsync needed.
CFG="./.swarm.json"
CFG_SOURCE="(not loaded)"
if [ -f "$CFG" ]; then
  CFG_SOURCE="$CFG"
  read_cfg() { python3 -c "import json,sys; print(json.load(open('$CFG')).get('$1',''))" 2>/dev/null; }
  read_cfg_bool() { python3 -c "import json,sys; print('true' if json.load(open('$CFG')).get('$1') else 'false')" 2>/dev/null; }
  SCOPE_PATTERN="$(read_cfg scope_pattern)"
  DEPS_STEP="$(read_cfg deps_step)"
  DEV_SERVER_CMD="$(read_cfg dev_server_cmd)"
  VERIFY_CMD="$(read_cfg verify_cmd)"
  EXCLUDE_PATTERN="$(read_cfg exclude_pattern)"
  LOCKFILE="$(read_cfg lockfile)"
  echo "[run_swarm_generic] config: .swarm.json scope='$SCOPE_PATTERN' deps='$DEPS_STEP' devsrv='$DEV_SERVER_CMD' verify='$VERIFY_CMD'" | tee -a "$LOG"
else
  # FALLBACK: .swarm.json is absent — use hardcoded NextChapter defaults.
  # BLINDSPOT: a missing config must NEVER silently produce a false-green.
  # Emit a clear warning so the omission is visible.
  echo "[run_swarm_generic] ⚠ WARNING: no .swarm.json at repo root — falling back to hardcoded defaults" | tee -a "$LOG"
  echo "[run_swarm_generic] ⚠ These defaults (scope=.ts/.tsx, deps=npm ci, verify=tsc) may be WRONG for '$REPO_NAME'" | tee -a "$LOG"
  echo "[run_swarm_generic] ⚠ Add a .swarm.json at the repo root to silence this warning and ensure correct behavior" | tee -a "$LOG"
  CFG_SOURCE="(fallback — no .swarm.json)"
  SCOPE_PATTERN='\.(ts|tsx)$'
  DEPS_STEP='npm ci --legacy-peer-deps'
  DEV_SERVER_CMD='npm run dev'
  VERIFY_CMD='npx tsc --noEmit'
  EXCLUDE_PATTERN='(\.test\.|/__tests__/|^_harness/|\.d\.ts$)'
  LOCKFILE='package-lock.json'
fi

# ── Scope from the diff (base..build), filtered by per-repo pattern ────────────
CHANGED="$(git diff --name-only "$BASE".."$BUILD_BRANCH")"
echo "[run_swarm_generic] changed files:" | tee -a "$LOG"; echo "$CHANGED" | sed 's/^/  /' | tee -a "$LOG"

# Build the exclude regex: always exclude tests + _harness/; add per-repo exclude_pattern.
EXCLUDE_RE='(\.test\.|/__tests__/|^_harness/|\.d\.ts$)'
if [ -n "$EXCLUDE_PATTERN" ]; then
  EXCLUDE_RE="${EXCLUDE_RE}|${EXCLUDE_PATTERN}"
fi

# Filter changed files by scope pattern and exclude pattern.
CODE="$(echo "$CHANGED" | grep -E "$SCOPE_PATTERN" | grep -vE "$EXCLUDE_RE" | paste -sd, -)"
echo "[run_swarm_generic] code scope: ${CODE:-<none>}" | tee -a "$LOG"

# Gate visibility: did this build actually pass the hivemind forced-checkpoint gate?
CKPT="$REPO/_harness/$ID/checkpoint-log.json"
if [ -f "$CKPT" ]; then HIVEMIND_GATED=true; NCP="$(python3 -c "import json;print(len(json.load(open('$CKPT'))))" 2>/dev/null || echo 0)"
else HIVEMIND_GATED=false; NCP=0; fi
echo "[run_swarm_generic] hivemind_gated=$HIVEMIND_GATED (checkpoints consulted=$NCP)" | tee -a "$LOG"

# ── Deps step (per-repo config) ───────────────────────────────────────────────
if [ "$DEPS_STEP" != "none" ] && [ -n "$DEPS_STEP" ]; then
  # Only run if the lockfile (if any) changed — match the live runner's gate.
  if [ -n "$LOCKFILE" ] && echo "$CHANGED" | grep -qx "$LOCKFILE"; then
    echo "[run_swarm_generic] $LOCKFILE changed -> $DEPS_STEP" | tee -a "$LOG"
    bash -lc "$DEPS_STEP" >"$SWARM/deps-${SAFE_ID}.log" 2>&1 || echo "[run_swarm_generic] WARN deps step failed (see deps-${SAFE_ID}.log)" | tee -a "$LOG"
  elif [ -z "$LOCKFILE" ]; then
    echo "[run_swarm_generic] no lockfile configured -> skipping deps" | tee -a "$LOG"
  else
    echo "[run_swarm_generic] deps unchanged -> skip $DEPS_STEP" | tee -a "$LOG"
  fi
else
  echo "[run_swarm_generic] deps: none (stdlib or no deps step)" | tee -a "$LOG"
fi

# ── Dev server / vision lane (per-repo config) ───────────────────────────────
DEV_OK=1
if [ -n "$ROUTES" ] && [ -n "$DEV_SERVER_CMD" ]; then
  pkill -f "$DEV_SERVER_CMD" 2>/dev/null || true; sleep 1
  nohup bash -lc "$DEV_SERVER_CMD" >"$SWARM/devserver.log" 2>&1 &
  echo "[run_swarm_generic] dev server pid $!" | tee -a "$LOG"
  DEV_OK=0
  for i in $(seq 1 60); do curl -sf http://localhost:3000 >/dev/null 2>&1 && { DEV_OK=1; break; }; sleep 2; done
  if [ "$DEV_OK" = 1 ]; then echo "[run_swarm_generic] dev up" | tee -a "$LOG"
  else
    echo "[run_swarm_generic] WARN dev server never came up -> skipping vision lane (DEGRADED)" | tee -a "$LOG"
    STATUS="degraded"; ROUTES=""
  fi
elif [ -n "$ROUTES" ] && [ -z "$DEV_SERVER_CMD" ]; then
  echo "[run_swarm_generic] routes provided but no dev_server_cmd in config -> skipping vision lane" | tee -a "$LOG"
  ROUTES=""
fi

# Snapshot the global fix-log line count so the per-run handoff includes only THIS run's entries.
FIXLOG_OFFSET="$(wc -l < "$SWARM/fix-log.jsonl" 2>/dev/null | tr -d ' ')"; FIXLOG_OFFSET="${FIXLOG_OFFSET:-0}"

# ── Run the wheel — peanut_wheel commits each accepted fix onto $FIX_BRANCH ──
# Uses the LIVE peanut_wheel.py (repo-agnostic — it reads cwd = this repo).
#
# CWD CONTRACT (root cause of the operator-console false-green): the wheel and
# its critic subprocesses read in-scope files from their CWD. If the CWD is not
# the target repo checkout, critics read whatever repo the process happened to
# inherit (NextChapter) and produce findings about a completely unrelated tree.
# We bind cwd to $REPO explicitly via a subshell for BOTH the wheel and triage
# invocations — never relying on the top-level `cd "$REPO"` persisting through
# intervening subshells / login-shell deps steps.
#
# EMPTY-SCOPE SANITY GUARD: if the scope is non-empty but ZERO in-scope files
# exist under the runner's cwd, FAIL LOUD rather than let critics review an
# unrelated tree. A run that reviews the wrong repo must never report success.
WHEEL_RC=0
if [ -z "$CODE" ] && [ -z "$ROUTES" ]; then
  echo "[run_swarm_generic] no code scope, no routes -> skipping wheel (fix == build)" | tee -a "$LOG"
else
  # Sanity guard: verify in-scope files exist under $REPO before running the wheel.
  if [ -n "$CODE" ]; then
    MISSING_FILES=""
    EXISTING_N=0
    TOTAL_N=0
    IFS=',' read -ra SCOPE_FILES <<< "$CODE"
    for rel in "${SCOPE_FILES[@]}"; do
      [ -z "$rel" ] && continue
      TOTAL_N=$((TOTAL_N + 1))
      if [ -f "$REPO/$rel" ]; then
        EXISTING_N=$((EXISTING_N + 1))
      else
        MISSING_FILES="${MISSING_FILES:+$MISSING_FILES, }$rel"
      fi
    done
    if [ "$EXISTING_N" -eq 0 ] && [ "$TOTAL_N" -gt 0 ]; then
      fail "EMPTY-SCOPE GUARD: $TOTAL_N in-scope files were computed from the diff but NONE exist under '$REPO'. The runner's cwd does not match the target repo checkout — critics would review an unrelated tree. Computed scope: $CODE. Missing: $MISSING_FILES. Aborting to prevent a wrong-repo false-green." 9
    fi
    if [ -n "$MISSING_FILES" ]; then
      echo "[run_swarm_generic] WARN: $EXISTING_N/$TOTAL_N in-scope files exist under '$REPO' — missing: $MISSING_FILES" | tee -a "$LOG"
    else
      echo "[run_swarm_generic] scope guard: all $TOTAL_N in-scope files exist under '$REPO'" | tee -a "$LOG"
    fi
  fi

  ARGS=(--waves "$WAVES"); [ -n "$CODE" ] && ARGS+=(--code "$CODE"); [ -n "$ROUTES" ] && ARGS+=(--routes "$ROUTES")
  ARGS+=(--findings-out "$REPO/_harness/$ID/swarm-findings.json")
  echo "[run_swarm_generic] python3 -u $SWARM/peanut_wheel.py ${ARGS[*]} (detect-only; gated=$HIVEMIND_GATED)" | tee -a "$LOG"
  ( cd "$REPO" && python3 -u "$SWARM/peanut_wheel.py" "${ARGS[@]}" ) 2>&1 | tee -a "$LOG"
  WHEEL_RC="${PIPESTATUS[0]}"
  echo "[run_swarm_generic] wheel rc=$WHEEL_RC" | tee -a "$LOG"
  if [ "$WHEEL_RC" != 0 ]; then STATUS="failed"; RC="$WHEEL_RC"; fi
fi
FINDINGS_N="$(python3 -c "import json;print(len(json.load(open('$REPO/_harness/$ID/swarm-findings.json')).get('findings',[])))" 2>/dev/null || echo 0)"
echo "[run_swarm_generic] findings detected: $FINDINGS_N (raw work-order)" | tee -a "$LOG"

# ── Triage brain ────────────────────────────────────────────────────────────
# Uses the repo-tracked generic_triage.py wrapper (shipped from this repo to
# ~/swarm/generic/), NOT the live ~/swarm/triage.py directly. The live triage.py
# hardcodes ~/swarm/newchapter as the repo root; for a non-NextChapter repo it
# would look in the wrong place and crash (or worse, silently miss the findings).
# The wrapper redirects the live triage.py's hardcoded paths to the correct
# repo via a HOME override + symlink, and we bind cwd to $REPO here too.
#
# The triage exit code is now GATED: if triage crashes (FileNotFoundError or
# otherwise), the run FAILS instead of silently reporting success with zero
# batches. This was the second half of the operator-console false-green — the
# crash was swallowed by `tee` and the runner reported success.
BATCHES_N=0
if [ "${FINDINGS_N:-0}" -gt 0 ] 2>/dev/null; then
  echo "[run_swarm_generic] generic_triage.py over $FINDINGS_N findings (repo_root=$REPO)..." | tee -a "$LOG"
  ( cd "$REPO" && python3 -u "$GENERIC/generic_triage.py" "$ID" "$REPO" ) 2>&1 | tee -a "$LOG"
  TRIAGE_RC="${PIPESTATUS[0]}"
  echo "[run_swarm_generic] triage rc=$TRIAGE_RC" | tee -a "$LOG"
  if [ "$TRIAGE_RC" != 0 ]; then
    fail "triage exited $TRIAGE_RC for run_id=$ID repo=$REPO — findings were detected but triage crashed. Refusing to report success." "$TRIAGE_RC"
  fi
  BATCHES_N="$(python3 -c "import json;print(len(json.load(open('$REPO/_harness/$ID/swarm-workorder.json')).get('batches',[])))" 2>/dev/null || echo 0)"
fi
echo "[run_swarm_generic] triage: $BATCHES_N batches for $REPO_NAME" | tee -a "$LOG"

# ── Stage-4 handoff artifacts committed onto the fix branch ───────────────────
# (same JSON shape as the live runner — notify_stage4.sh greps swarm-report.json)
FIXES="$(git rev-list --count "$BUILD_BRANCH".."$FIX_BRANCH" 2>/dev/null || echo 0)"
mkdir -p "_harness/$ID"
python3 - "$ID" "$BUILD_BRANCH" "$FIX_BRANCH" "$FIXES" "$CODE" "$ROUTES" "$WAVES" "$FIXLOG_OFFSET" "$STATUS" "$HIVEMIND_GATED" "$NCP" "$FINDINGS_N" "$REPO_NAME" <<'PY'
import json, sys, os
id_, bb, fb, fixes, code, routes, waves, offset, status, gated, ncp, findings_n, repo_name = sys.argv[1:14]
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
           "waves": int(waves), "repo": repo_name},
          open(os.path.join(d, "swarm-report.json"), "w"), indent=2)
json.dump(fixlog, open(os.path.join(d, "fix-log.json"), "w"), indent=2)
print(f"[run_swarm_generic] wrote swarm-report.json (status={status} fixes={fixes}) + fix-log.json (entries={len(fixlog)})")
PY
git add "_harness/$ID"
git commit -qm "stage3 swarm report ($ID): $STATUS, $FIXES fixes" >/dev/null 2>&1 \
  && echo "[run_swarm_generic] committed report" | tee -a "$LOG" \
  || echo "[run_swarm_generic] no report change to commit" | tee -a "$LOG"

echo "[run_swarm_generic] DONE: $FIX_BRANCH @ $(git rev-parse --short HEAD) status=$STATUS ($FIXES fix commits)" | tee -a "$LOG"
finish "$RC"
