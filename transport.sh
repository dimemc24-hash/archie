#!/usr/bin/env bash
# Phase-D transport (DO side): ship build/<id> + a gh-main baseline to the Hetzner swarm box over
# SSH, run the swarm there, then bring fix/<id> back and push it to origin for Stage 4.
# Only DO touches GitHub; Hetzner needs no GitHub key.
#
#   transport.sh <run-id> [routes-csv] [waves] [--profile <name>]
#   transport.sh <run-id> [routes-csv] [waves] [--dry-run]
#
# Profile-aware (2026-07-04): with --profile (or HARNESS_PROFILE env), resolves the DO-side
# repo dir, Hetzner target repo path, and swarm invocation from the profile config. Without
# one: today's NextChapter behavior, unchanged (zero regression — same refs, same remote, same
# runner invocation Morley's tooling greps).
#
# Hardened (2026-06-28): (1) ensures the idle-prone swarm box is powered on before shipping;
# (2) gates the origin push on the swarm's REAL outcome (no fake-green); (3) confirms completion via
# a sentinel so a dropped SSH pipe never causes a premature/stale fetch; (4) alerts Morley on any
# abort; (5) logs the whole run to ~/harness/artifacts/<id>/transport.log.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export GIT_SSH_COMMAND="ssh -o ConnectTimeout=12 -o ServerAliveInterval=30 -o ServerAliveCountMax=10"
HARNESS="$HOME/harness"
REPO_DIR="$HARNESS/repo"

# ── Parse args: <run-id> [routes] [waves] [--profile <name>] [--dry-run] ──────
ID=""; ROUTES=""; ROUTES_SET=""; WAVES="3"; PROFILE=""; DRY_RUN=false
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) 
      if [ -z "$ID" ]; then ID="$1"
      elif [ -z "$ROUTES_SET" ]; then ROUTES="$1"; ROUTES_SET=1
      else WAVES="$1"; fi
      shift
      ;;
  esac
done
[ -z "$ID" ] && { echo "usage: transport.sh <run-id> [routes] [waves] [--profile <name>] [--dry-run]"; exit 2; }
# HARNESS_PROFILE env is a fallback if --profile not passed on CLI.
[ -z "$PROFILE" ] && PROFILE="${HARNESS_PROFILE:-}"

BUILD="build/$ID"; FIX="fix/$ID"
SAFE_ID="${ID//[^A-Za-z0-9._-]/_}"
LOGDIR="$HARNESS/artifacts/$ID"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/transport.log"

# ── Resolve profile → transport plan (via swarm_config.py, stdlib only) ────────
# The plan resolves: DO repo dir, Hetzner repo path, runner type (live|generic), config.
REPO="$REPO_DIR"  # default (legacy)
HETZNER_REPO_PATH="swarm/newchapter"
HETZNER_REMOTE="hetzner-swarm:swarm/newchapter"
RUNNER="live"
RUNNER_SSH_CMD="bash \$HOME/swarm/run_swarm.sh"
REPO_NAME=""
GENERIC_CONFIG=""
BOOTSTRAP=false
RSYNC_GENERIC=false
CONFIG_FALLBACK=false

resolve_plan() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local swarm_cfg="$script_dir/swarm/swarm_config.py"
  if [ ! -f "$swarm_cfg" ]; then
    # swarm_config.py not present (old checkout?) — fall back to legacy defaults
    return
  fi
  # Prefer the repo-local profiles/ dir; fall back to ~/harness/profiles/.
  local profiles_dir="$script_dir/profiles"
  [ -d "$profiles_dir" ] || profiles_dir="$HARNESS/profiles"
  local plan_json
  plan_json="$(PROFILE="$PROFILE" HARNESS_PROFILES_DIR="$profiles_dir" python3 -c "
import json, os, sys
sys.path.insert(0, '$script_dir/swarm')
import swarm_config as sc
profile = os.environ.get('PROFILE', '')
plan = sc.resolve_transport_plan('$ID', profile=profile or None, routes='$ROUTES', waves=$WAVES)
print(json.dumps({
  'do_repo': plan.do_repo,
  'hetzner_repo_path': plan.hetzner_repo_path,
  'hetzner_remote': plan.hetzner_remote,
  'runner': plan.runner,
  'runner_script': plan.runner_script,
  'repo_name': plan.repo_name,
  'config_path': plan.config_path,
  'bootstrap': plan.bootstrap,
  'is_legacy': plan.is_legacy,
  'config_fallback': plan.config_fallback,
}))
" 2>/dev/null)" || { echo "[transport] WARN: swarm_config.py resolution failed — using legacy defaults" >&2; return; }

  # Extract fields from plan_json
  REPO="$(printf '%s' "$plan_json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["do_repo"])' 2>/dev/null)"
  HETZNER_REPO_PATH="$(printf '%s' "$plan_json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["hetzner_repo_path"])' 2>/dev/null)"
  HETZNER_REMOTE="$(printf '%s' "$plan_json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["hetzner_remote"])' 2>/dev/null)"
  RUNNER="$(printf '%s' "$plan_json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["runner"])' 2>/dev/null)"
  REPO_NAME="$(printf '%s' "$plan_json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["repo_name"])' 2>/dev/null)"
  BOOTSTRAP="$(printf '%s' "$plan_json" | python3 -c 'import sys,json; print(str(json.load(sys.stdin)["bootstrap"]).lower())' 2>/dev/null)"
  CONFIG_FALLBACK="$(printf '%s' "$plan_json" | python3 -c 'import sys,json; print(str(json.load(sys.stdin).get("config_fallback",False)).lower())' 2>/dev/null)"
  if [ "$RUNNER" = "generic" ]; then
    RUNNER_SSH_CMD="bash \$HOME/swarm/generic/run_swarm_generic.sh '$REPO_NAME' '$BUILD' '$ROUTES' '$WAVES'"
    GENERIC_CONFIG="$(printf '%s' "$plan_json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["config_path"])' 2>/dev/null)"
    RSYNC_GENERIC=true
  fi
}

if [ -n "$PROFILE" ]; then
  resolve_plan
fi

# ── Dry-run: print the plan and exit (no SSH, no mutations) ────────────────────
if [ "$DRY_RUN" = true ]; then
  exec > >(tee -a "$LOG") 2>&1
  echo "[transport] DRY-RUN — no SSH, no mutations"
  echo "[transport] $(date -Is) run=$ID profile='${PROFILE:-none}' routes='$ROUTES' waves=$WAVES"
  echo "[transport] runner=$RUNNER"
  echo "[transport] DO repo=$REPO"
  echo "[transport] Hetzner repo=$HETZNER_REPO_PATH"
  echo "[transport] Hetzner remote=$HETZNER_REMOTE"
  echo "[transport] runner_cmd=ssh hetzner-swarm \"$RUNNER_SSH_CMD\""
  if [ "$RSYNC_GENERIC" = true ]; then
    echo "[transport] rsync generic runner to ~/swarm/generic/"
    echo "[transport] config=$GENERIC_CONFIG (travels with the checkout — no rsync needed)"
    if [ "$CONFIG_FALLBACK" = true ]; then
      echo "[transport] ⚠ WARNING: no .swarm.json found — runner will use hardcoded defaults (may be WRONG)"
      echo "[transport] ⚠ Add .swarm.json at the repo root to silence this warning"
    fi
  fi
  echo "[transport] bootstrap=$BOOTSTRAP"
  echo "[transport] baseline=${HARNESS_BASELINE:-origin/main}"
  echo "[transport] build=build/$ID fix=fix/$ID"
  echo "[transport] END DRY-RUN"
  exit 0
fi

exec > >(tee -a "$LOG") 2>&1

alert(){ bash "$HARNESS/alert.sh" "$1" >/dev/null 2>&1 || true; }
abort(){ echo "[transport] ABORT: $1"; alert "⚠️ Stage 2→3 transport ($ID): $1"; exit "${2:-1}"; }

cd "$REPO" || { echo "[transport] FATAL: no $REPO"; exit 2; }
echo "[transport] $(date -Is) run=$ID profile='${PROFILE:-none}' runner=$RUNNER routes='$ROUTES' waves=$WAVES"

# 0) Ensure the Stage-3 box is up (auto power-on if HETZNER_API_TOKEN is set; else liveness probe).
echo "[transport] ensuring swarm box is up..."
bash "$HARNESS/hetzner_power.sh" up; PW_RC=$?
if [ "$PW_RC" != 0 ]; then
  if ssh -o ConnectTimeout=8 hetzner-swarm 'echo UP' 2>/dev/null | grep -q UP; then
    echo "[transport] power helper rc=$PW_RC but box is SSH-reachable; proceeding"
  else
    abort "swarm box is DOWN and could not be powered on (power rc=$PW_RC). Power it on, then re-run: transport.sh $ID" 10
  fi
fi

# ── Profile-driven: bootstrap target repo on Hetzner if it doesn't exist ──────
# (generic runner only; live runner uses the existing ~/swarm/newchapter repo)
if [ "$RUNNER" = "generic" ]; then
  # rsync ONLY the generic runner script to ~/swarm/generic/ on the box.
  # Per-repo config (.swarm.json) travels with the checkout — no rsync needed.
  echo "[transport] rsync generic runner to hetzner ~/swarm/generic/"
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  ssh hetzner-swarm "mkdir -p ~/swarm/generic" 2>/dev/null
  rsync -az --delete \
    "$SCRIPT_DIR/swarm/run_swarm_generic.sh" \
    "$SCRIPT_DIR/swarm/swarm_config.py" \
    "hetzner-swarm:~/swarm/generic/" 2>&1 | tail -1 || abort "rsync generic runner failed" 7

  # Bootstrap: init the target repo on Hetzner if it doesn't exist
  if [ "$BOOTSTRAP" = true ]; then
    echo "[transport] checking Hetzner repo at $HETZNER_REPO_PATH..."
    if ! ssh -o ConnectTimeout=8 hetzner-swarm "test -d ~/$HETZNER_REPO_PATH/.git" 2>/dev/null; then
      echo "[transport] bootstrap: initializing $HETZNER_REPO_PATH on Hetzner"
      ssh hetzner-swarm "mkdir -p ~/$HETZNER_REPO_PATH && cd ~/$HETZNER_REPO_PATH && git init" 2>&1 | tail -1 \
        || abort "bootstrap git init failed for $HETZNER_REPO_PATH" 8
    else
      echo "[transport] Hetzner repo exists at $HETZNER_REPO_PATH"
    fi
  fi
fi

git remote get-url hetzner >/dev/null 2>&1 || git remote add hetzner "$HETZNER_REMOTE"
# If the remote exists but points elsewhere (profile switch), update it.
CURRENT_REMOTE="$(git remote get-url hetzner 2>/dev/null || echo '')"
if [ "$CURRENT_REMOTE" != "$HETZNER_REMOTE" ]; then
  echo "[transport] updating hetzner remote: $CURRENT_REMOTE -> $HETZNER_REMOTE"
  git remote set-url hetzner "$HETZNER_REMOTE"
fi

BASELINE="${HARNESS_BASELINE:-origin/main}"
echo "[transport] baseline: $BASELINE -> hetzner gh-main"
git fetch origin -q || abort "git fetch origin failed" 11
git push -f hetzner "$BASELINE:refs/heads/gh-main" 2>&1 | tail -1 || abort "push gh-main failed" 11

echo "[transport] push $BUILD -> hetzner"
# Ship the freshest $BUILD: a fixpass pushed from another machine lands on
# origin/$BUILD (fetched above) while the local ref goes stale (bit us
# 2026-07-03: stale local build/ shipped a pre-fixpass tree to the swarm).
SRC="$BUILD"
if git rev-parse --verify "refs/remotes/origin/$BUILD" >/dev/null 2>&1; then
  if ! git rev-parse --verify "$BUILD" >/dev/null 2>&1; then
    SRC="refs/remotes/origin/$BUILD"
  elif git merge-base --is-ancestor "$BUILD" "refs/remotes/origin/$BUILD"; then
    SRC="refs/remotes/origin/$BUILD"
  elif git merge-base --is-ancestor "refs/remotes/origin/$BUILD" "$BUILD"; then
    SRC="$BUILD"
  else
    abort "local $BUILD and origin/$BUILD have diverged — reconcile before transport" 3
  fi
fi
git rev-parse --verify "$SRC" >/dev/null 2>&1 || abort "no $BUILD ref (local or origin)" 3
git push -f hetzner "$SRC:refs/heads/$BUILD" 2>&1 | tail -1 || abort "push $BUILD failed" 11

echo "[transport] run swarm on hetzner (runner=$RUNNER routes='$ROUTES' waves=$WAVES)..."
ssh hetzner-swarm "$RUNNER_SSH_CMD"; SWARM_RC=$?
echo "[transport] swarm ssh rc=$SWARM_RC"

# Confirm completion via the sentinel (survives a dropped SSH pipe: poll until the box finishes).
SENT="\$HOME/swarm/status/${SAFE_ID}.done"
read_sentinel(){ ssh -o ConnectTimeout=8 hetzner-swarm "cat $SENT 2>/dev/null"; }
DONE_JSON="$(read_sentinel)"
if [ -z "$DONE_JSON" ]; then
  echo "[transport] no completion sentinel yet — polling up to 30m (SSH pipe may have dropped mid-run)"
  for i in $(seq 1 90); do
    sleep 20
    DONE_JSON="$(read_sentinel)"; [ -n "$DONE_JSON" ] && break
    ssh -o ConnectTimeout=8 hetzner-swarm 'echo UP' 2>/dev/null | grep -q UP || { echo "[transport] box unreachable while waiting"; break; }
  done
fi
[ -z "$DONE_JSON" ] && abort "swarm produced no completion sentinel (box may have died mid-run). Not pushing $FIX." 5

STATUS="$(printf '%s' "$DONE_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","unknown"))' 2>/dev/null || echo unknown)"
SFIXES="$(printf '%s' "$DONE_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("fixes","?"))' 2>/dev/null || echo '?')"
echo "[transport] swarm status=$STATUS fixes=$SFIXES"

if [ "$STATUS" = "failed" ] || [ "$STATUS" = "unknown" ]; then
  abort "swarm run did not succeed (status=$STATUS). Inspect hetzner ~/swarm/run_swarm_${SAFE_ID}.log. Not pushing $FIX." 6
fi

echo "[transport] fetch $FIX from hetzner"
git fetch hetzner "+$FIX:refs/heads/$FIX" 2>&1 | tail -1 || abort "cannot fetch $FIX from hetzner" 4

echo "[transport] push $FIX -> origin (Stage 4 handoff)"
git push -f origin "$FIX:refs/heads/$FIX" 2>&1 | tail -1 || abort "push $FIX to origin failed" 11
echo "[transport] DONE: origin/$FIX @ $(git rev-parse --short "$FIX") status=$STATUS fixes=$SFIXES"
