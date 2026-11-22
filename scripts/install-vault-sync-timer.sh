#!/usr/bin/env bash
# Install/refresh the desktop-side vault-sync systemd user timer (task #1288).
#
# Copies deploy/systemd/ua-vault-sync.{service,timer} into
# ~/.config/systemd/user/ (the authoritative-copy convention documented in
# deploy/systemd/README.md — installed copies are always `cp`, never
# symlinked, so a branch switch in the repo can't silently change what's
# live), reloads the systemd user manager, then enables and starts the timer.
#
# Idempotent — safe to re-run after pulling a unit-file change; re-copies and
# restarts the timer so an edited OnCalendar=/ExecStart= actually takes effect
# (systemctl daemon-reload alone does not restart an already-running timer).
#
# Usage:  ./scripts/install-vault-sync-timer.sh
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
unit_dir="$HOME/.config/systemd/user"

if [ ! -x "$repo_root/.venv/bin/python" ]; then
  echo "install-vault-sync-timer: $repo_root/.venv/bin/python missing — run 'uv sync --all-extras' first" >&2
  exit 1
fi

mkdir -p "$unit_dir"
cp "$repo_root/deploy/systemd/ua-vault-sync.service" "$repo_root/deploy/systemd/ua-vault-sync.timer" "$unit_dir/"

systemctl --user daemon-reload
systemctl --user enable --now ua-vault-sync.timer
systemctl --user restart ua-vault-sync.timer

echo "installed ua-vault-sync.timer — next fires:"
systemctl --user list-timers ua-vault-sync.timer --no-pager || true
echo
echo "Run a sync immediately: systemctl --user start ua-vault-sync.service"
echo "Dry-run without the timer: cd $repo_root && .venv/bin/python -m unified_agent.vault_sync --dry-run"
echo "Check status: unified-agent-doctor  (look for the 'vault_sync' capability), or system_doctor() over MCP"
