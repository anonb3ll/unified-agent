#!/usr/bin/env bash
# One approved-dispatch scheduler tick (task #603).
#
# Deliberately NOT passing --force: with UA_DISPATCH_SCHEDULER_ENABLED unset or
# 0 this exits reporting "disabled" and changes nothing. Installing the timer is
# not what enables dispatch; the flag is.
set -euo pipefail

REPO="${UA_REPO:-$HOME/dev/unified_agent}"
SCHEDULER_BIN="$REPO/.venv/bin/unified-agent-dispatch-scheduler"
cd "$REPO"

[ -x "$SCHEDULER_BIN" ] || {
  echo "dispatch-tick-cron: $SCHEDULER_BIN not executable" >&2
  exit 2
}

exec "$SCHEDULER_BIN" tick --json
