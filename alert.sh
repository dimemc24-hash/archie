#!/usr/bin/env bash
# Reusable Telegram ops-alert for the harness (down-box / transition failures). Deterministic, no LLM,
# no persona filter — these are plain operational alerts to Morley. Reuses notify_stage4.sh's bot creds.
#   alert.sh "<message>"
set -uo pipefail
ENV="$HOME/.hermes/.env"
set -a; . "$ENV" 2>/dev/null; set +a
TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT="${TELEGRAM_HOME_CHANNEL:-}"; [ -z "$CHAT" ] && CHAT="${TELEGRAM_ALLOWED_USERS%%,*}"
MSG="${1:?usage: alert.sh <message>}"
[ -z "$TOKEN" ] && { echo "[alert] no TELEGRAM_BOT_TOKEN; would have sent: $MSG" >&2; exit 0; }
[ -z "$CHAT" ]  && { echo "[alert] no chat id; would have sent: $MSG" >&2; exit 0; }
curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage" \
     --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=${MSG}" >/dev/null \
  && echo "[alert] sent" || { echo "[alert] send failed" >&2; exit 1; }
