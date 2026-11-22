#!/usr/bin/env bash
# Anomaly check — deterministic rules with cooldown-based iMessage notify.
# Safe to run frequently: cooldown in anomaly_notifications (default 2h) prevents
# re-paging. Calls check_anomalies_and_notify() which runs all anomaly rules
# and pushes new/re-triggered findings through notify.notify.
#
# Install:  crontab -e  →  */30 * * * * $HOME/dev/unified_agent/deploy/anomaly-check-cron.sh
set -u

REPO_DIR="${UA_REPO:-${UA_CODE_ROOT:-$HOME/dev/unified_agent}}"
UA_HOME_DIR="${UA_HOME:-$HOME/.unified_agent}"

cd "$REPO_DIR" || exit 1

# If local store is retired (authoritative store moved to Mac hub), exit cleanly.
if [ -f "$UA_HOME_DIR/NO_LOCAL_MEMORY_DB" ]; then
    now=$(date -u +%FT%TZ)
    echo "[$now] anomaly-check: 0 active anomaly(s), 0 newly notified (local store retired: NO_LOCAL_MEMORY_DB)"
    exit 0
fi

PYTHONPATH= uv run python3 -c "
from datetime import datetime, timezone
from unified_agent.monitor import check_anomalies_and_notify

result = check_anomalies_and_notify()
new = len(result.get('newly_notified', []))
active = len(result.get('anomalies', []))
ts = datetime.now(timezone.utc).isoformat()
if new:
    print(f'[{ts}] anomaly-check: {active} active anomaly(s), {new} newly notified')
else:
    print(f'[{ts}] anomaly-check: {active} active anomaly(s), {new} newly notified (no new alerts)')
"
