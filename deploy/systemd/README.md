# deploy/systemd/ — user timer units (cron replacement)

The original six service+timer pairs replaced the user crontab on 2026-07-19. The
crontab was silently skipping jobs whenever the machine was off across a
scheduled time (e.g. 03:15–10:07 that day, which ate all four morning jobs) —
`cron` has no catch-up mechanism. `systemd` timers with `Persistent=true` do:
if the machine was off (or the user session wasn't running) when a timer's
`OnCalendar=` would have fired, the timer fires once at the next boot/login
instead of skipping the run.

Old crontab (kept for reference, rollback path): `~/.unified_agent/crontab.backup-2026-07-19`.

## Long-running coordination worker

`unified-agent-zulip-worker.service` is a supervised, read-only process rather
than a timer. It consumes one filtered message event queue per configured bot
identity across `ops`, `requests`, `coord`, `review`, and `meta`, while keeping
the per-channel REST cursor as its recovery boundary. Queue expiry or network
loss re-registers the queue and backfills through REST before resuming. It does
not execute message content, call an LLM, reply to inbound messages, or alter
task state.

The worker is intentionally not enabled by this repository change. After review
and explicit operator authorization, install and control it with:

```bash
cp deploy/systemd/unified-agent-zulip-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now unified-agent-zulip-worker
systemctl --user stop unified-agent-zulip-worker
```

Health is read through the existing `/api/overview` payload under
`zulip_worker`, including per-bot queue status, queue age, reset count, and the
existing per-channel recovery status.

Inbound target markers are explicit: use `[target: agent-codex]` (or `[to: ...]`)
for one bot, or `[broadcast]` for all five. Missing or unknown targets are kept
as informational events and are never actionable for any bot.

## Units

| Unit | Source script | Schedule | Persistent (catch-up) |
|---|---|---|---|
| ~~`hermes-drift-check`~~ | **Retired 2026-08-23** (the operator): owner-authorized Hermes config; timer removed from live desktop. Unit templates kept under `retired/` if ever needed manually. | — | — |
| `cluster-briefing` | `~/agent-workspace/projects/cluster-health/briefing-cron.sh` | daily 08:00 | yes |
| `cluster-health-check` | `~/agent-workspace/projects/cluster-health/health-check.sh` piped into `~/agent-workspace/projects/imessage-bridge/notify.sh` | daily 08:30 | yes |
| `ua-review-pipeline` | [`../review-pipeline-cron.sh`](../review-pipeline-cron.sh) | every 15 min | no |
| `ua-anomaly-check` | [`../anomaly-check-cron.sh`](../anomaly-check-cron.sh) | every 30 min | no |
| `ua-dispatch-tick` | [`../dispatch-tick-cron.sh`](../dispatch-tick-cron.sh) | every 2 min | no |

`ua-dispatch-tick` (task #603) is the only unit here whose firing can start an
agent process, and it is doubly gated. The script does not pass `--force`, so
with `UA_DISPATCH_SCHEDULER_ENABLED` unset or `0` a tick reports `disabled` and
changes nothing; and even with the scheduler enabled, `UA_DISPATCH_REAL_SPAWN`
(default `0`) decides whether a claimed run execs a real CLI or the stub, and
`UA_DISPATCH_NODE_ROUTING` (default `0`) decides whether a label bound to a
registered ua-node launches on that device instead.
Installing the unit is not what enables dispatch. See
[`../../docs/architecture/approved-dispatch-enablement.md`](../../docs/architecture/approved-dispatch-enablement.md).

The four daily jobs are `Persistent=true` — a missed run replays at next
boot/login. The three high-frequency jobs (2 min / 15 min / 30 min) are
`Persistent=false`: at that cadence a missed run is superseded by the next
scheduled one within minutes, so catch-up would just cause a redundant
back-to-back run.

All six services are `Type=oneshot`, triggered only by their timer (no
`[Install]` section on the `.service` files — only the `.timer` files carry
`WantedBy=timers.target`).

## Added since the cron migration

Additional timer pairs were added after the six above, following the same
`Type=oneshot` / `Persistent=true` / no-`[Install]`-on-the-`.service` shape.
This table was missing `ua-consolidate-proposal` from when it shipped
(2026-07-21 System Leverage Plan T1-2) until this row was added alongside
`ua-sleep-pass` — noted here so it doesn't happen again silently (`system_doctor`'s
`unit_template_drift` check catches config *drift* on an installed unit, not a
whole unit missing from this doc).

| Unit | Source script | Schedule | Persistent (catch-up) |
|---|---|---|---|
| `ua-sleep-pass` | [`../../src/unified_agent/sleep_pass.py`](../../src/unified_agent/sleep_pass.py) (`unified-agent-sleep-pass`; also emits skill-cluster Inbox proposals via `evolve.py`) | daily 03:00 | yes |
| `ua-planning-loop` | [`../../src/unified_agent/planning_loop.py`](../../src/unified_agent/planning_loop.py) (`unified-agent-planning-loop`; ledger-derived plan + decision packet) | Mondays 04:00 | yes |
| `ua-database-backup` | [`../../src/unified_agent/backup_runner.py`](../../src/unified_agent/backup_runner.py) | daily 02:15 | yes |
| `ua-audit-ledger-reanchor` | [`../../src/unified_agent/audit/reanchor.py`](../../src/unified_agent/audit/reanchor.py) | daily 02:45 | yes |
| `unified-agent-zulip-morning-digest` | [`../../src/unified_agent/zulip_automation.py`](../../src/unified_agent/zulip_automation.py) | daily 08:00 | yes |
| `unified-agent-zulip-evening-digest` | [`../../src/unified_agent/zulip_automation.py`](../../src/unified_agent/zulip_automation.py) | daily 17:00 | yes |
| `unified-agent-zulip-event-bridge` | [`../../src/unified_agent/zulip_automation.py`](../../src/unified_agent/zulip_automation.py) | every 5 min | no |
| `ua-vault-sync` | [`../../src/unified_agent/vault_sync.py`](../../src/unified_agent/vault_sync.py) (`unified-agent-vault-sync`; pulls hub-authoritative `Wiki/`, `Logs/`, `00 - Inbox` from the Mac hub over SSH) | every 10 min (`UA_VAULT_SYNC_INTERVAL_MINUTES`) | no |
| `ua-evidence-export` | [`../../src/unified_agent/audit/evidence_export_sink.py`](../../src/unified_agent/audit/evidence_export_sink.py) (`unified-agent-evidence-export`) | daily 02:00 | yes |

`ua-vault-sync` is desktop-only — direction is hub -> desktop, so this unit is
never installed on the Mac hub itself. `system_doctor`'s `vault_sync` check
reads DORMANT (not a failure) on a host where it isn't installed for exactly
that reason. Install/refresh with `scripts/install-vault-sync-timer.sh`; see
`../../docs/vault-sync-operations.md`
for the conflict rules, retired-path list, and rollback.

> **Do not** re-enable `ua-consolidate-proposal` — retired 2026-09-10 (task #884).
> Nightly sleep-pass covers near-duplicate clustering; the weekly 0.90 job never
> completed a production run. Units kept under `retired/` for history.
> Manual one-shot: `unified-agent-consolidate propose`.

### PATH

The systemd user manager's default `PATH` is
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin`
— it does **not** include `~/.local/bin` or `~/.bun/bin`, unlike an
interactive login shell. Two scripts shell out to bare (non-absolute-path)
binaries that live there:

- `cluster-briefing.service` — `briefing-cron.sh` runs `uv run python3 -c ...`
  for its hub-perf footer, and drives the `hermes` agent runtime, which may
  reach for `bun`-based tooling. `Environment=PATH=` adds `~/.local/bin` and
  `~/.bun/bin`.
- `ua-anomaly-check.service` — `anomaly-check-cron.sh` runs a bare `uv run`.
  `Environment=PATH=` adds `~/.local/bin`.

The other four scripts call their tool dependencies (`unified-agent-audit`,
`unified-agent-review`, `hermes` via `briefing-cron.sh`'s own absolute
default, `notify.sh`'s own absolute `bun` default) via absolute path, or only
use coreutils already on the default `PATH`, so no `Environment=` override
was needed for them.

## Authoritative copies vs installed copies

This directory (`deploy/systemd/` in the repo) is the source of truth.
Installed copies live in `~/.config/systemd/user/` — **copied**, not
symlinked, so an accidental `git checkout` / branch switch in the repo can't
silently change what's live. After editing a unit here, re-copy and reload:

```bash
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart <unit>.timer   # if the .timer itself changed
```

## Install (from scratch)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now \
  ua-steward-audit.timer \
  cluster-briefing.timer \
  cluster-health-check.timer \
  ua-review-pipeline.timer \
  ua-anomaly-check.timer \
  ua-sleep-pass.timer \
  ua-planning-loop.timer \
  ua-database-backup.timer \
  ua-audit-ledger-reanchor.timer \
  unified-agent-zulip-morning-digest.timer \
  unified-agent-zulip-evening-digest.timer \
  unified-agent-zulip-event-bridge.timer
```

> **Do not** re-enable `hermes-drift-check.timer` — retired 2026-08-23. Hermes
> governing-file changes are the operator-authorized; the daily FAIL→inbox loop was
> noise. Script remains at `scripts/hermes_drift_check.sh` for optional manual use.

> **Do not** re-enable `ua-consolidate-proposal.timer` — retired 2026-09-10
> (task #884). Sleep-pass covers near-dups; see `deploy/systemd/retired/`.

`ua-sleep-pass.service` needs `GEMINI_API_KEY` (via
`EnvironmentFile=%h/.unified_agent/sleep-pass.env`, chmod 600) for the AI Studio leg
of its `llm_client` fallback chain — see the unit file. It also set
`UA_VERTEX_PROJECT` until 2026-07-27; that leg has been removed (task #197) and the
variable is read by nothing, so an already-installed unit carrying it is harmless —
but re-run the install step below to drop it. `ua-sleep-pass`
was missing from this list entirely until 2026-07-23 — a from-scratch install
before that date silently skipped it.

## Verify

```bash
systemctl --user list-timers          # confirm all six, sane NEXT times
systemctl --user start ua-review-pipeline.service   # manual run
systemctl --user status ua-review-pipeline.service  # should be inactive/SUCCESS
journalctl --user -u ua-review-pipeline.service -n 20
systemctl --user start ua-database-backup.service
systemctl --user status ua-database-backup.service  # should be inactive/SUCCESS
```

## Known follow-up (not fixed by this migration)

`health-check.sh` greps `crontab -l` to confirm `steward-audit-cron`,
`hermes_drift_check`, and the daily-briefing skill are "scheduled". Now that
those run via systemd timers instead of cron, that check will always report
them as `MISSING from crontab` even though they're running fine. Worth a
follow-up to point that check at `systemctl --user list-timers` instead.
