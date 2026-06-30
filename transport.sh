#!/usr/bin/env bash
# Phase-D transport (DO side): ship build/<id> + a gh-main baseline to the Hetzner swarm box over
# SSH, run the swarm there, then bring fix/<id> back and push it to origin for Stage 4.
# Only DO touches GitHub; Hetzner needs no GitHub key.
#
#   transport.sh <run-id> [routes-csv] [waves]
#
# Hardened (2026-06-28): (1) ensures the idle-prone swarm box is powered on before shipping;
# (2) gates the origin push on the swarm's REAL outcome (no fake-green); (3) confirms completion via
# a sentinel so a dropped SSH pipe never causes a premature/stale fetch; (4) alerts Morley on any
# abort; (5) logs the whole run to ~/harness/artifacts/<id>/transport.log.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export GIT_SSH_COMMAND="ssh -o ConnectTimeout=12 -o ServerAliveInterval=30 -o ServerAliveCountMax=10"
HARNESS="$HOME/harness"
REPO="$HARNESS/repo"
ID="${1:?usage: transport.sh <run-id> [routes] [waves]}"
ROUTES="${2:-}"; WAVES="${3:-3}"
BUILD="build/$ID"; FIX="fix/$ID"
SAFE_ID="${ID//[^A-Za-z0-9._-]/_}"
LOGDIR="$HARNESS/artifacts/$ID"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/transport.log"
exec > >(tee -a "$LOG") 2>&1

alert(){ bash "$HARNESS/alert.sh" "$1" >/dev/null 2>&1 || true; }
abort(){ echo "[transport] ABORT: $1"; alert "⚠️ Stage 2→3 transport ($ID): $1"; exit "${2:-1}"; }

cd "$REPO" || { echo "[transport] FATAL: no $REPO"; exit 2; }
echo "[transport] $(date -Is) run=$ID routes='$ROUTES' waves=$WAVES"

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

git remote get-url hetzner >/dev/null 2>&1 || git remote add hetzner hetzner-swarm:swarm/newchapter

BASELINE="${HARNESS_BASELINE:-origin/main}"
echo "[transport] baseline: $BASELINE -> hetzner gh-main"
git fetch origin -q || abort "git fetch origin failed" 11
git push -f hetzner "$BASELINE:refs/heads/gh-main" 2>&1 | tail -1 || abort "push gh-main failed" 11

echo "[transport] push $BUILD -> hetzner"
git rev-parse --verify "$BUILD" >/dev/null 2>&1 || abort "local $BUILD missing" 3
git push -f hetzner "$BUILD:refs/heads/$BUILD" 2>&1 | tail -1 || abort "push $BUILD failed" 11

echo "[transport] run_swarm.sh on hetzner (routes='$ROUTES' waves=$WAVES)..."
ssh hetzner-swarm "bash \$HOME/swarm/run_swarm.sh '$BUILD' '$ROUTES' '$WAVES'"; SWARM_RC=$?
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
