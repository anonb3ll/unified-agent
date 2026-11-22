# GCP slice-1 — operator runbook (task #522)

Additive private worker + GCS offsite backup copies. Interface and naming:
CONTRACT.md. Live non-secret values: Stream A fills CONTRACT
Live inventory and optional `ops-state.md`.

**Cloud control plane (task #2148).** The owner's 2026-10-17 decision makes a
Compute Engine VM in this project the production home of the cloud control
plane, with Railway as staging. Its scripts are in
[control-plane/](control-plane/) and its runbook is
[docs/deploy/gcp-control-plane.md](../../docs/deploy/gcp-control-plane.md).
The `ua-worker` VM described below stays an additive worker; it never
runs the hub.

## PATH

Interactive shells on this desktop often miss the SDK. Always:

```bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
```

Scripts under this directory prepend that path when the directory exists.

## What this slice is

| Piece | Role |
|---|---|
| Dedicated GCP project | Isolate billing; prefer `ua-worker-<short>` |
| `e2-micro` VM `ua-worker` | Tailscale node / private worker only |
| GCS bucket `ua-memory-backups-<short>` | Offsite copies of local `memory.db` snapshots |
| Firewall UDP `41641` | Tailscale; no public app/MCP ports |

## What stays on the canonical Mac hub until the Phase-5 cutover

The **Mac hub** hub stays authoritative until the Phase-5 cutover (epic
#2107). The cutover host is then chosen between the GCP control-plane VM and
Railway on recorded acceptance runs. Until it happens, these stay on the Mac:

1. HTTP MCP hub (`127.0.0.1:8765` on Mac) and LaunchAgent / `service.env`
2. Authoritative SQLite (`~/.unified_agent/memory.db` on Mac)
3. Obsidian vault (`UA_VAULT_PATH`)
4. Agent bearer tokens / secrets
5. Merge / review / PR authority

`ua-worker` joins Tailscale as an **additive** private worker. Do not point
hosts at it as the MCP hub. Do not restart or re-home the Mac hub as part of
this work; desktop must not become a second SQLite authority. The control-plane
VM serves no fleet writes before the cutover.

## Billing: budgets are detection, not hard caps

Create budget alerts (CONTRACT suggests $10 and $50) so spend is visible.
**Budget alerts do not stop spend** — they notify. Always-free eligibility still
depends on machine type, region, and quotas; a mis-sized disk or wrong region
can bill. Do not enable Vertex AI or burn AI trial credits on this project.

## Always-free `e2-micro` region constraints

- Prefer **`us-central1`** (fallback: `us-west1` / `us-east1` per Google always-free).
- Machine: **`e2-micro`** only for this slice.
- Boot disk: **STANDARD** persistent disk **≤ 30 GB**.
- One always-free `e2-micro` per billing account (eligibility rules change —
  verify against current Google Free Tier docs before assuming $0).

## Bootstrap order

```bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
export UA_GCP_PROJECT=ua-worker   # live project (task #522)

# Optional dry-runs first:
./deploy/gcp/bootstrap-e2-micro.sh --dry-run
./deploy/gcp/firewall-tailscale.sh --dry-run
./deploy/gcp/create-backup-bucket.sh --dry-run

# Live resources already created 2026-08-21 — see ops-state.md / CONTRACT Live inventory.
# Re-run scripts only for rebuilds:
./deploy/gcp/bootstrap-e2-micro.sh            # or --with-external-ip for first boot
./deploy/gcp/firewall-tailscale.sh
./deploy/gcp/create-backup-bucket.sh
```

Live (2026-08-21): project `ua-worker`, zone `us-central1-a`, bucket
`ua-worker-backups-000000000000`, URI
`gs://ua-worker-backups-000000000000/memory-db`.

Then on the VM (operator channel — **never commit auth keys**):

1. `gcloud compute ssh ua-worker --project=ua-worker --zone=us-central1-a --command='sudo tailscale up --ssh --hostname=ua-worker'` and open the printed login URL (or use a one-time auth key from the Tailscale admin console).
2. Confirm `tailscale status` from desktop and VM.
3. Prefer removing any ephemeral external IP once Tailscale works; delete `default-allow-ssh` when Tailscale SSH is confirmed.
4. Record MagicDNS hostname in Live inventory / `ops-state.md`.

## GCS offsite copies (`UA_BACKUP_GCS_URI`)

Local backups remain required on the **canonical Mac hub** (`UA_BACKUP_DIR`,
default `~/.unified_agent/backups`). Set in Mac
`~/.unified_agent/service.env` (already present after #522):

```bash
UA_BACKUP_GCS_URI=gs://ua-worker-backups-000000000000/memory-db
```

- Unset → local-only behavior unchanged.
- Set → after a successful local snapshot, copy to that GCS prefix.
- GCS failure must not fail the local backup success path (Stream B contract).
- Manual / cron helper: `deploy/gcp/sync-backup-to-gcs.sh`.
- Mac schedule: LaunchAgent `com.unified-agent.database-backup` via
  `deploy/mac-hub/run-database-backup.sh` (daily 02:15, mirrors Linux
  `ua-database-backup.timer`). Automatic GCS upload on Mac requires
  `gcloud`/`gsutil` on that host; otherwise local snapshot still succeeds and
  desktop can bridge with `sync-backup-to-gcs.sh`.

Suggested URI is printed by `create-backup-bucket.sh`. Live bucket:
`ua-worker-backups-000000000000`; object prefix `memory-db/`. See
`ops-state.md` for the #534/#535 verification packet.

## Scripts in this directory

| Script | Purpose |
|---|---|
| `control-plane/` | Task #2148 — cloud control-plane VM: bootstrap (dry-run by default), VM install, env template, verify. Runbook: [gcp-control-plane.md](../../docs/deploy/gcp-control-plane.md) |
| `bootstrap-e2-micro.sh` | Create or describe `ua-worker` (`e2-micro`) |
| `firewall-tailscale.sh` | Ingress UDP 41641 to tags `ua-worker,tailscale` |
| `create-backup-bucket.sh` | Create `ua-memory-backups-<short>` + suggest URI |
| `sync-backup-to-gcs.sh` | Stream B — push local snapshot to GCS |
| `restore-backup-to-temp.sh` | Task #569 — download newest GCS snapshot to scratch and verify |
| `RESTORE.md` | Restore rehearsal + rollback; live-DB replace is the operator-owned |
| `failover-rehearsal.sh` | Task #538 — Tailscale/loopback throwaway hub over a scratch snapshot |
| `run-failover-rehearsal-on-worker.sh` | Desktop-driven worker drill + teardown |
| `FAILOVER.md` | Failover rehearsal; Mac remains SoT; not a second database |
| `sweep-hygiene.sh` | Task #536 — budget burn & idle resource sweep |
| `slack-gateway/` | Task #558 — stateless public Slack TLS forwarder + runbook |
| `lib.sh` | Shared PATH / project / bucket helpers |
| `CONTRACT.md` | Lead-owned interface (read-only for scripts) |
| `ops-state.md` | Stream A optional live inventory |

### Slack TLS forwarder (task #558)

Scaffold (not live until the operator places secrets + Request URL):

- Code: [`slack-gateway/forwarder.py`](slack-gateway/forwarder.py)
- Docs index: [`docs/slack-coordination.md`](../../docs/slack-coordination.md)

The forwarder verifies Slack HMAC on the edge and POSTs to `MAC_HUB_URL` over
Tailscale. It does **not** open a cloud `memory.db` or dispatch tasks.

Env overrides: `UA_GCP_PROJECT`, `UA_GCP_ZONE`, `UA_GCP_BACKUP_BUCKET`,
`UA_GCP_REGION`, `UA_GCP_INSTANCE`.

## Verify

```bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
gcloud config set project "$UA_GCP_PROJECT"
gcloud compute instances describe ua-worker --zone=<zone>
gsutil ls "gs://<bucket>/memory-db/"   # or: gcloud storage ls …
# Via Tailscale once joined:
#   tailscale status
```

If Live inventory still shows `_TBD_`, do not invent project/bucket names in
git — leave placeholders and let ops fill CONTRACT / `ops-state.md`.

## MCP tools (task #537)

- `gcloud_inventory(project, sections)`: read-only JSON metadata for project, compute instances, GCS buckets, and billing budgets.
- `gcloud_ops(action, target, project, zone)`: bounded operational tool (actions: `instance_describe`, `instance_start`, `instance_stop`, `bucket_describe`, `bucket_list_objects`). Only `instance_start`/`instance_stop` are approval-gated; the read-only actions run without one.

## Cost guards

The egress alert policy and the `litestream/`-scoped lifecycle rules from the
September 2026 incident (#1410), plus a read-only drift check, live in
[cost-guards/](cost-guards/README.md).
