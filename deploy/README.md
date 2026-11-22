# deploy/ — service + timer inventory

Everything the desktop hub runs outside an interactive session. As of
2026-07-19 all scheduled jobs run as systemd user timers, not cron — see
[systemd/README.md](systemd/README.md) for the full unit list, the `PATH`
gotchas (systemd user units don't inherit an interactive shell's `PATH`), and
why `Persistent=true` matters (a plain crontab silently drops any job whose
scheduled time falls while the machine is off; systemd timers with
`Persistent=true` replay a missed daily run at next boot instead).

## systemd (user)

| Unit | Source | What |
|---|---|---|
| `unified-agent.service` | [unified-agent.service](unified-agent.service) | HTTP hub on `127.0.0.1:8765/mcp` (template; the installed copy lives in `~/.config/systemd/user/`). Restart after merging tool changes: `systemctl --user restart unified-agent`. |
| `unified-agent-zulip-worker.service` | [systemd/unified-agent-zulip-worker.service](systemd/unified-agent-zulip-worker.service) | Supervised, read-only Zulip polling for the five bot identities and five allowlisted channels. Explicitly install/enable; it is not live until an operator does so. |
| `ua-steward-audit` / ~~`hermes-drift-check`~~ (retired 2026-08-23) / `cluster-briefing` / `cluster-health-check` / `ua-review-pipeline` / `ua-anomaly-check` | [systemd/](systemd/) | Former crontab jobs — see [systemd/README.md](systemd/README.md) for schedules, source scripts, and `PATH` details. |

The old crontab backup (pre-migration) lives at
`~/.unified_agent/crontab.backup-2026-07-19`; the live crontab is now empty.

## GCP (slice-1 — Tailscale worker + GCS offsite backups)

Additive private worker on GCP (`e2-micro` + Tailscale) and optional GCS copies of
local `memory.db` snapshots. The desktop remains the canonical hub, SQLite
authority, vault, and secrets store — see gcp/CONTRACT.md and
the operator runbook [gcp/README.md](gcp/README.md).

| Script / doc | What |
|---|---|
| [gcp/bootstrap-e2-micro.sh](gcp/bootstrap-e2-micro.sh) | Create/describe instance `ua-worker` |
| [gcp/firewall-tailscale.sh](gcp/firewall-tailscale.sh) | Allow UDP `41641` (Tailscale only; no public app ports) |
| [gcp/create-backup-bucket.sh](gcp/create-backup-bucket.sh) | Create backup bucket; print `UA_BACKUP_GCS_URI` |
| [gcp/sync-backup-to-gcs.sh](gcp/sync-backup-to-gcs.sh) | Sync local backup → GCS |
| [gcp/README.md](gcp/README.md) | Operator runbook (PATH, always-free constraints, what stays local) |
| [gcp/slack-gateway/](gcp/slack-gateway/) | Task #558 — public Slack TLS forwarder scaffold + runbook |

```bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
export UA_GCP_PROJECT=ua-worker
# Live (2026-08-21): UA_BACKUP_GCS_URI=gs://ua-worker-backups-000000000000/memory-db
```
