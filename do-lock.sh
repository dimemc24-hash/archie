#!/usr/bin/env bash
# Mutual-exclusion guard for DO: Stage-2 build vs attended Stage 1/4.
# usage: do-lock.sh <build|attend> <cmd...>
MODE="${1:?usage: do-lock.sh <build|attend> <cmd...>}"; shift
LOCKDIR="$HOME/harness/locks"; mkdir -p "$LOCKDIR"
LOCK="$LOCKDIR/do.lock"; HOLDER="$LOCKDIR/holder.json"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "DO BUSY: $(cat "$HOLDER" 2>/dev/null)" >&2
  exit 75   # EX_TEMPFAIL
fi
printf '{"mode":"%s","who":"%s","pid":%s,"started_at":"%s"}\n' \
  "$MODE" "${USER:-$(id -un)}" "$$" "$(date -Is)" > "$HOLDER"
trap 'rm -f "$HOLDER"' EXIT   # lock auto-frees when fd9 closes at exit; this cleans the info file
"$@"
