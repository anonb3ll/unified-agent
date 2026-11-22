#!/usr/bin/env bash
# DEPRECATED (task #1288): superseded by `unified-agent-vault-sync`
# (src/unified_agent/vault_sync.py), installed via deploy/systemd/ua-vault-sync.{service,timer}.
# This whole-repo `git pull --rebase` has no per-path conflict handling — a
# desktop edit under a hub-authoritative tree either blocks the rebase or
# merges clean and silently overwrites it — and no retired-path filtering, which
# is exactly how task #1256 (`Logs/Laptop/` resurrected) happened. Kept only as
# a documented rollback path; see docs/vault-sync-operations.md "Rollback".
# Do not install this as ua-vault-sync.service's ExecStart going forward.
#
# Periodic bidirectional sync of ~/vaults/main with Mac hub authoritative git SSOT.
set -u

VAULT_DIR="$HOME/vaults/main"
REMOTE_HOST="mac"

cd "$VAULT_DIR" || exit 1

# If remote is unreachable, exit cleanly without error
if ! ssh -o BatchMode=yes -o ConnectTimeout=3 "$REMOTE_HOST" "true" 2>/dev/null; then
    exit 0
fi

# Discard any desktop-side modifications to Mac-owned state files
git checkout HEAD -- "Logs/state_*.hub-host.local.*" 2>/dev/null || true

# Snapshot local changes (excluding Mac-owned state snapshot files)
git add -A
git reset -- "Logs/state_*.hub-host.local.*" 2>/dev/null || true
if ! git diff --cached --quiet; then
    git commit --quiet -m "chore(vault): desktop sync $(date +'%Y-%m-%d %H:%M')"
fi

# Snapshot any uncommitted changes on Mac hub
ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_HOST" \
    'cd ~/vaults/main && git add -A && if ! git diff --cached --quiet; then git commit --quiet -m "chore: vault snapshot $(date +%F)"; fi' 2>/dev/null

# Rebase local commits on top of Mac hub SSOT
if ! git pull --rebase --autostash origin main --quiet; then
    # In case of conflict, abort cleanly to preserve working tree
    git rebase --abort 2>/dev/null
    exit 1
fi

# If desktop is ahead, ensure Mac working copy is committed right before push
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_HOST" \
        'cd ~/vaults/main && git add -A && if ! git diff --cached --quiet; then git commit --quiet -m "chore: vault snapshot $(date +%F)"; fi' 2>/dev/null
    git push origin main --quiet
fi
