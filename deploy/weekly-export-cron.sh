#!/usr/bin/env bash
# Weekly export — tasks + memory to vault for disaster recovery.
# Safe to run frequently: exports overwrite previous vault export files.
#
# Install:  crontab -e  →  0 2 * * 0 $HOME/dev/unified_agent/deploy/weekly-export-cron.sh
set -u

REPO_DIR="${UA_REPO:-${UA_CODE_ROOT:-$HOME/dev/unified_agent}}"
UA_HOME_DIR="${UA_HOME:-$HOME/.unified_agent}"

cd "$REPO_DIR" || exit 1

# If local store is retired (authoritative store moved to Mac hub), exit cleanly.
if [ -f "$UA_HOME_DIR/NO_LOCAL_MEMORY_DB" ]; then
    now=$(date -u +%FT%TZ)
    echo "[$now] weekly-export: skipped (local store retired: NO_LOCAL_MEMORY_DB)"
    exit 0
fi

PYTHONPATH= uv run python3 -c "
from datetime import datetime, timezone
from unified_agent.tools.workflow import workflow_run

ts = datetime.now(timezone.utc).isoformat()
try:
    result = workflow_run(name='weekly-export', author='weekly-export-cron')
    status = result.get('status', 'unknown')
    steps = len(result.get('steps', []))
    print(f'[{ts}] weekly-export: status={status}, steps_run={steps}')
except Exception as e:
    print(f'[{ts}] weekly-export: FAILED — {e}')
    raise SystemExit(1)
"
