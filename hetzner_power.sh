#!/usr/bin/env bash
# Hetzner Cloud power control for the Stage-3 swarm box. Dependency-free (curl + Hetzner Cloud API +
# python3 for JSON). The box "powers off when idle"; `up` brings it back and waits for SSH so
# transport.sh never ships a build to a dead box (the silent Stage-3 killer).
#
# Reads HETZNER_API_TOKEN from ~/.hermes/.env. Without a token it degrades gracefully: `up` reports
# rc 3 so the caller can fall back to alert-and-abort.
#
#   hetzner_power.sh status   -> prints running|off|starting|unknown
#   hetzner_power.sh up       -> ensure running + SSH-ready; rc 0 ready, 3 no-token, 4 failed
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
ENV="$HOME/.hermes/.env"
set -a; . "$ENV" 2>/dev/null; set +a
TOKEN="${HETZNER_API_TOKEN:-}"
SWARM_IP="${SWARM_IP:-46.62.163.3}"
SSH_ALIAS="${SWARM_SSH_ALIAS:-hetzner-swarm}"
API="https://api.hetzner.cloud/v1"

api() { curl -s -m 20 -H "Authorization: Bearer $TOKEN" "$@"; }

server_id() {
  api "$API/servers" | python3 -c '
import sys, json
ip = sys.argv[1]
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for s in d.get("servers", []):
    if (s.get("public_net", {}).get("ipv4") or {}).get("ip") == ip:
        print(s["id"]); break
' "$SWARM_IP"
}

server_status() {
  api "$API/servers/$1" | python3 -c '
import sys, json
try: print(json.load(sys.stdin).get("server", {}).get("status", "unknown"))
except Exception: print("unknown")'
}

ssh_ready() { ssh -o ConnectTimeout=8 -o BatchMode=yes "$SSH_ALIAS" 'echo UP' 2>/dev/null | grep -q UP; }

case "${1:-}" in
  status)
    if [ -z "$TOKEN" ]; then ssh_ready && echo running || echo unknown; exit 0; fi
    ID="$(server_id)"; [ -z "$ID" ] && { echo unknown; exit 0; }
    server_status "$ID"; exit 0 ;;
  up)
    if ssh_ready; then echo "[power] already up + ssh-ready"; exit 0; fi
    if [ -z "$TOKEN" ]; then echo "[power] no HETZNER_API_TOKEN; cannot power on" >&2; exit 3; fi
    ID="$(server_id)"; [ -z "$ID" ] && { echo "[power] no server matches $SWARM_IP" >&2; exit 4; }
    ST="$(server_status "$ID")"
    echo "[power] server $ID status=$ST"
    if [ "$ST" != "running" ] && [ "$ST" != "starting" ]; then
      echo "[power] poweron $ID"
      api -X POST "$API/servers/$ID/actions/poweron" >/dev/null
    fi
    for i in $(seq 1 60); do
      ST="$(server_status "$ID")"
      if [ "$ST" = "running" ] && ssh_ready; then echo "[power] up + ssh-ready after ${i}x5s"; exit 0; fi
      sleep 5
    done
    echo "[power] timed out waiting for box (last status=$ST)" >&2; exit 4 ;;
  *) echo "usage: hetzner_power.sh status|up" >&2; exit 2 ;;
esac
