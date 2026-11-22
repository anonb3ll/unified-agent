#!/usr/bin/env bash
# Hub-restart broadcast (ExecStartPost) — Tier 1 easy win, System Leverage
# Plan 2026-07-20. Every previous restart needed a manual "RECONNECT" shout
# in a broadcast memory note because agents kept operating on a stale tool
# snapshot with no signal to reconnect on their own (pain point P4). This
# writes one journal line so any agent doing its normal boot-sync (checking
# today's vault journal, as Steward already does) sees it without a human
# having to remember to say so.
#
# Best-effort only, by design: a failure here must never fail the unit start
# (the unit file invokes this with the `-` ExecStartPost prefix), and a
# vault-write failure must never mask the stderr line below — that line
# alone is already visible via `journalctl --user -u unified-agent`.
set -u

cd "$HOME/dev/unified_agent" || exit 0

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "unified-agent: hub (re)started at ${TS} — reconnect to pick up a fresh tool snapshot" >&2

"$HOME/dev/unified_agent/.venv/bin/python3" -c "
from unified_agent.tools.vault import vault_journal
try:
    vault_journal(
        entry='unified-agent hub (re)started — reconnect for a fresh tool snapshot.',
        agent='unified-agent-cron',
        tags=['ops', 'automated'],
    )
except Exception:
    pass
" >/dev/null 2>&1 || true

exit 0
