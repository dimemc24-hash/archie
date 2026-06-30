#!/usr/bin/env bash
# Stage-4 notify: ping Morley on Telegram (as Archie, via the bot API) when a new fix/<id> branch
# lands on origin — i.e. a build→swarm cycle finished and is ready for his first-person review.
# Deterministic (no LLM); runs from system cron on DO. Sends only on NEW <id>:<sha> (no re-spam).
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
ENV="$HOME/.hermes/.env"; REPO="$HOME/harness/repo"; SEEN="$HOME/harness/.stage4_seen"
SEED_ONLY="${1:-}"   # pass --seed to record current state WITHOUT notifying (baseline)

set -a; . "$ENV" 2>/dev/null; set +a
TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT="${TELEGRAM_HOME_CHANNEL:-}"; [ -z "$CHAT" ] && CHAT="${TELEGRAM_ALLOWED_USERS%%,*}"
[ -z "$TOKEN" ] && { echo "no TELEGRAM_BOT_TOKEN"; exit 0; }
[ -z "$CHAT" ]  && { echo "no chat id"; exit 0; }
touch "$SEEN"

git -C "$REPO" fetch -q -p origin 'refs/heads/fix/*:refs/remotes/origin/fix/*' 2>/dev/null || true

for ref in $(git -C "$REPO" for-each-ref --format='%(refname:short)' 'refs/remotes/origin/fix/*' 2>/dev/null); do
  id="${ref#origin/fix/}"
  sha="$(git -C "$REPO" rev-parse "$ref" 2>/dev/null)"
  key="${id}:${sha}"
  grep -qxF "$key" "$SEEN" 2>/dev/null && continue
  echo "$key" >> "$SEEN"
  [ "$SEED_ONLY" = "--seed" ] && { echo "seeded $id"; continue; }
  # Only notify for HARNESS fix branches — they carry _harness/<id>/swarm-report.json.
  # Plain dev fix/* branches don't, and must not trigger a Stage-4 ping.
  rep="$(git -C "$REPO" show "${ref}:_harness/${id}/swarm-report.json" 2>/dev/null)"
  [ -z "$rep" ] && { echo "skip ${id} (no _harness swarm-report — not a harness fix branch)"; continue; }
  fixes="$(printf '%s' "$rep" | python3 -c "import sys,json;print(json.load(sys.stdin).get('fixes_committed','?'))" 2>/dev/null || echo '?')"
  # Route the hivemind's signal to ARCHIE + review (NOT into the swarm critics): gate status + the
  # builder's own flagged uncertainty (FALLBACK checkpoints + a blindspot), distilled from checkpoint-log.
  gate="$(printf '%s' "$rep" | python3 -c "import sys,json;d=json.load(sys.stdin);print(('gated ✓ '+str(d.get('checkpoints_consulted',0))+' checkpoints') if d.get('hivemind_gated') else '⚠️ NOT hivemind-gated (borrow/skip)')" 2>/dev/null || echo '')"
  ck="$(git -C "$REPO" show "${ref}:_harness/${id}/checkpoint-log.json" 2>/dev/null)"
  flagged="$(printf '%s' "$ck" | python3 -c "
import sys,json
try: cps=json.load(sys.stdin)
except Exception: cps=[]
cps=cps if isinstance(cps,list) else []
fb=[c.get('checkpoint') for c in cps if c.get('fallback') and c.get('checkpoint')]
bs=[]
for c in cps:
    b=c.get('blindspots'); b=b if isinstance(b,list) else ([b] if b else [])
    bs+=[str(x)[:90] for x in b if x]
parts=[]
if fb: parts.append('FALLBACK (no council): '+', '.join(fb))
if bs: parts.append('blindspot: '+bs[0])
print(' — '.join(parts))
" 2>/dev/null || echo '')"
  raw="🟢 Stage 4 ready: fix/${id} — build→swarm green, ${fixes} fixes committed${gate:+ · ${gate}}. ${flagged:+Builder flagged → ${flagged}. }Pull it, review the diff + _harness/${id}/ artifacts, then LAUNCH (merge → main) or loop back (new spec → Stage 2)."
  FILTER="$HOME/.hermes/skills/archie/persona/persona_filter.py"
  msg="$(printf '%s' "$raw" | python3 "$FILTER" 2>/dev/null)"; [ -z "$msg" ] && msg="$raw"
  curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage" \
       --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=${msg}" >/dev/null && echo "notified: ${id} (${fixes} fixes)" || echo "send failed: ${id}"
done
