#!/usr/bin/env bash
# Cockpit ua-sync refresh (deterministic — no LLM involved).
#
# Default unified-agent-sync no longer embeds Active-Tasks / Recent-Decisions
# snapshots (task #564). Those lists were the original reason this timer ran
# every 30 minutes; without them the rewrite is topology/Startup Grounding
# only and a 30-minute journal entry is noise.
#
# This script is a no-op unless UA_SYNC_CRON_FORCE=1 (or you pass
# --include-snapshots yourself). Leave the crontab in place if you still want
# the hook; set the env var only when you intentionally want a rewrite.
#
# Install:  crontab -e  →  */30 * * * * $HOME/dev/unified_agent/deploy/sync-cron.sh
set -u

SYNC_BIN="$HOME/dev/unified_agent/.venv/bin/unified-agent-sync"
LOG="$HOME/.unified_agent/sync.log"
COCKPIT_PATH="$HOME/dev/agent"

[ -x "$SYNC_BIN" ] || { echo "sync-cron: $SYNC_BIN not executable" >&2; exit 2; }
mkdir -p "$(dirname "$LOG")"

now=$(date -u +%FT%TZ)

if [ "${UA_SYNC_CRON_FORCE:-0}" != "1" ]; then
    echo "$now SKIP snapshots-disabled (set UA_SYNC_CRON_FORCE=1 to run)" >>"$LOG"
    exit 0
fi

out=$("$SYNC_BIN" --path "$COCKPIT_PATH" 2>&1)
rc=$?
if [ "$rc" -eq 0 ]; then
    echo "$now OK $COCKPIT_PATH" >>"$LOG"
else
    echo "$now FAIL $COCKPIT_PATH: $out" >>"$LOG"
fi
exit "$rc"
