#!/bin/bash

# Fetch in background/fast-check
git fetch origin >/dev/null 2>&1 || true

LOCAL=$(git rev-parse HEAD 2>/dev/null || true)
REMOTE=$(git rev-parse '@{u}' 2>/dev/null || true)
BASE=$(git merge-base HEAD '@{u}' 2>/dev/null || true)

if [ -n "$LOCAL" ] && [ -n "$REMOTE" ]; then
    if [ "$LOCAL" = "$REMOTE" ]; then
        # Up to date
        true
    elif [ "$LOCAL" = "$BASE" ]; then
        # Behind origin
        echo "ua-audit: [Warning] Local repository is behind origin. Deploy through the gate in cockpit docs/runbooks/DEPLOY-STEWARD.md (~/agent-workspace/scripts/deploy-mac-hub.sh)." >&2
    fi
fi
# Always exit 0 so it doesn't fail/block startup
exit 0
