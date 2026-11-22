# Database Operations: Backup, Restore, Migrations & Concurrency

For the separate Mac host coverage proposal, including Time Machine/NAS choice,
read-only inventory, and scratch-restore acceptance, see
Mac host backup proposal. The database controls
below cover `memory.db`; they do not constitute a whole-host backup.

This document outlines operational discipline for `memory.db` under Task #783 (Phase 3.3 Telemetry & Hardening).

---

## 1. Online Backup & Restore

`memory.db` operates in SQLite WAL (Write-Ahead Logging) mode. Traditional file copies of SQLite files during writes risk torn reads or partial checkpoint inconsistencies.

`storage.connect()` is the only read-write opener. Every connection it
returns sets:

| PRAGMA | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Readers keep a consistent snapshot while a writer holds a transaction |
| `busy_timeout` | `5000` | Wait up to 5 s on a lock instead of failing immediately |
| `foreign_keys` | `ON` | Enforce FK checks (off by default in SQLite) |

There is no application-level writer queue. Serialization is SQLite's file
locking plus that timeout (`tests/test_storage_wal.py`, task #964). A
non-SQLite regular file at the `memory.db` path — the Desktop cutover
tombstone — raises `LiveStoreError` with an actionable message rather than
`file is not a database`.

Unified Agent uses Python stdlib's native `sqlite3.Connection.backup()` API to take online, non-blocking, transactionally consistent snapshots while multiple daemons are writing.

### CLI Commands

```bash
# Take a timestamped online backup in UA_BACKUP_DIR (~/.unified_agent/backups)
unified-agent db backup

# Save backup to a specific location
unified-agent db backup --out /backups/memory-manual.db

# Check integrity and status of the current database
unified-agent db status --json
unified-agent db verify

# Restore from a backup (validates integrity before modifying target)
unified-agent db restore /backups/memory-manual.db

# Preview the one-shot #1433 key-constraint rebuild (read-only), or apply it now
unified-agent db migrate-constraints --dry-run
unified-agent db migrate-constraints

# Report the optional #1433 org_id sentinel backfill (read-only), apply or undo it
unified-agent db org-partition
UA_ORG_PARTITION=1 unified-agent db org-partition --apply
unified-agent db org-partition --downgrade
```

The constraint rebuild (task #2326) also runs once on its own at the next
`init_db`, after writing a verified copy of the affected tables to
`UA_BACKUP_DIR/constraints-2326-<stamp>.db`. See
[architecture/tenancy.md](architecture/tenancy.md#the-key-constraint-rebuild-task-2326).

The org_id sentinel backfill (task #1433) never runs on its own. Stop every
process that uses the store first, and run the downgrade before deploying a
build older than it. The enable and rollback steps are in
[architecture/tenancy.md](architecture/tenancy.md#enabling-and-rolling-back).

### Python API

```python
from unified_agent import storage_backup

# Online backup
res = storage_backup.backup_database()
print("Backup created:", res["backup_path"], "SHA-256:", res["sha256"])

# Restore with pre/post integrity verification
restore_res = storage_backup.restore_database("/backups/memory-manual.db")
```

---

## 2. Schema Migration Discipline

Unified Agent follows strict schema discipline for cross-machine and multi-daemon operations:

1. **Explicit Versioning**: Tracked via `SCHEMA_VERSION` in `storage.py` and stored in the `meta` table as `key='schema_version'`.
2. **Fail-Closed Guard**: `_guard_schema_version(conn)` runs before any statement in `init_db()`. If a database was initialized by a newer version of the codebase, older code immediately halts with `SchemaVersionError` to prevent table rebuilds from dropping unrecognized columns.
3. **Additive-First**: Columns are added via `ALTER TABLE ... ADD COLUMN` idempotently.
4. **Rebuild Index Preservation**: Any table rebuild (e.g. for CHECK constraints) explicitly re-asserts all required indexes within the same transaction.
5. **Integrity Validation**: `storage.verify_schema_integrity()` validates that core tables exist, indexes match expectations, and `PRAGMA quick_check` / `PRAGMA foreign_key_check` pass.

---

## 3. Fleet Soak Concurrency Verification

The hub database is tested under simulated fleet scale:
- **25 concurrent worker daemons** performing interleaved task creation, lifecycle updates, queries, and event-log publishing.
- **Concurrent online backups** executing simultaneously during peak write load.
- **Results**: 800+ operations in under 2 seconds (>400 ops/sec) with **0 database lock errors** (`sqlite3.OperationalError`) and 100% hash chain integrity preserved.

---

## 4. Continuous WAL Replication & Cold Standby — RETIRED 2026-09-25 (tasks #1410/#1411)

**Replication is off and the replica is deleted. Do not re-enable it
autonomously.** The nightly snapshot backups above are the supported recovery
path, at 24 h RPO.

Litestream replication ran as a Mac LaunchAgent (`com.unified-agent.litestream`),
streaming WAL frames from the hub's `memory.db` to `UA_REPLICA_GCS_URI` (30 s
`sync-interval`, RPO under 60 s). It was retired after it drained the GCP
account: the L0 retention loop could not prune, so it re-listed a
250,631-object replica every 15 s. GCS bills the JSON list *response payload*,
and that traffic was invoiced as the "Download Worldwide Destinations" SKU —
**~$65/day, ~$352 over six days** (Sep 20-25 2026). The bucket was private with
public access prevention enforced: this was our own fleet paginating metadata,
not an external actor.

The real root cause was the missing prune grant. The uploader identity held
`storage.objectCreator` without `storage.objects.delete`, so L0 retention could
never delete anything. The service-account credential expiring later merely
masked the loop — it did not cause the cost.

Guards now in place: a bucket lifecycle rule scoped to the `litestream/` prefix,
the `GCP runaway egress` alert policy, and the `UA_LITESTREAM_ENABLED` opt-in
gate in `run-litestream.sh`. The committed intent payloads and a read-only
verifier live in [`deploy/gcp/cost-guards/`](../deploy/gcp/cost-guards/README.md)
(`python3 deploy/gcp/cost-guards/verify_cost_guards.py`: exit 0 holds, 1
drift, 2 could not read live state). The script is **manual** (decision
2026-09-28, #1447) — it needs an authenticated `gcloud` (the Mac hub and the
Linux desktop desktop both have one). It never changes GCP. The LaunchAgent and its plist live in
`deploy/mac-hub/retired/`. Incident record: `Wiki/GCP-Cost-Incident-2026-09.md`.

### Roles — read this before enabling anything

- **The Mac hub is the sole write authority.** Replication is one-way,
  `memory.db → GCS replica`. Nothing may ever write into `memory.db` from the
  replica, and **no second host may ever run `litestream replicate` against the
  same `UA_REPLICA_GCS_URI`** — two writers to one replica corrupts it.
- The replica is a **cold standby**, not a failover target. Promotion is a
  manual, deliberate drill (below). There is no automatic promotion path and
  none should be added: split-brain against a tamper-evident ledger is worse
  than downtime.
- Snapshot backups (`unified-agent db backup` + `backup_runner`) continue
  unchanged — they are the longer-retention, operator-invoked restore path.
  The replica answers "the Mac is gone *right now*"; snapshots answer
  "we need last Tuesday".

### Re-enabling it (deliberate only)

There is no install-and-go path any more. Re-enabling replication is an
evidence-backed decision, because the failure mode is a silent
multi-hundred-dollar bleed:

```bash
# 0. Read deploy/mac-hub/retired/README.md and the incident record first.

# 1. Grant the uploader storage.objects.delete so L0 retention can PRUNE.
#    Without this the loop is un-prunable and the cost returns.

# 2. Install litestream (pin + checksum, same discipline as install.sh)
VERSION=0.5.17
curl -fsSL -o /tmp/litestream.tgz \
  "https://github.com/benbjohnson/litestream/releases/download/v${VERSION}/litestream-${VERSION}-darwin-arm64.tar.gz"
shasum -a 256 /tmp/litestream.tgz   # compare against checksums.txt in the release
tar -xzf /tmp/litestream.tgz -C /tmp
install -m 0755 /tmp/litestream ~/.local/bin/litestream

# 3. Set the replica target AND the explicit opt-in
echo 'UA_REPLICA_GCS_URI=gs://<bucket>/litestream/memory' >> ~/.unified_agent/service.env
echo 'UA_LITESTREAM_ENABLED=1' >> ~/.unified_agent/service.env

# 4. Install the archived plist, setting KeepAlive back to <true/>, then start it.
#    Re-enable first: the retirement persisted a disabled launchd override, and
#    bootstrap fails on a host that still carries it.
cp deploy/mac-hub/retired/com.unified-agent.litestream.plist ~/Library/LaunchAgents/
launchctl enable "gui/$(id -u)/com.unified-agent.litestream"
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.unified-agent.litestream.plist

# 5. Confirm: litestream status, litestream ltx -level all "$UA_REPLICA_GCS_URI"
#    and system_doctor's wal_replication check (LIVE, caught up)
```

`run-litestream.sh` refuses to start without `UA_LITESTREAM_ENABLED=1`. The
daemon needs `GOOGLE_APPLICATION_CREDENTIALS` (already in service.env for
`UA_BACKUP_GCS_URI` uploads); the runner sources that file, exports `UA_HOME` +
`UA_REPLICA_GCS_URI`, and execs `litestream replicate -config
deploy/litestream/litestream.yml`. Logs: `~/.unified_agent/litestream.{log,err}`.
Watch the egress alert for a full day before trusting it.

### Monitoring

`system_doctor` reports `wal_replication`. **While replication is retired and
`UA_REPLICA_GCS_URI` is unset it reports `DORMANT` — "litestream installed but
UA_REPLICA_GCS_URI unset — replication not configured". That is the correct
retired state, not a fault: do not "fix" it by re-adding the URI or minting a
credential.**

When replication is deliberately re-enabled, the check compares `litestream
status` (local txid, a local read) against the newest LTX object at the replica.
The probe checks L0 first and, when that level is empty after retention,
the higher compaction levels, using one shared time budget and a single
list to skip an empty level. A non-empty sample is cached for ten minutes
keyed by local txid, so the Agents → Hosts page does not enumerate every LTX file
on each request. LIVE when caught up or syncing inside the interval;
MISCONFIGURED when the replica is absent or falls behind beyond the 60 s
budget — the signal that the daemon died or GCS auth broke. A listing that
runs past its budget must not discard a sample already in hand.

### Restore rehearsal

`--from-replica` needs a live replica, so it **cannot run while replication is
retired** (the prefix is deleted). It is the rehearsal for a deliberately
re-enabled replica — run it after any change to the replication config.

The supported drill today restores from a local snapshot backup instead — the
default mode, i.e. without `--from-replica`:

```bash
uv run python scripts/db_restore_rehearsal.py --out /tmp/snapshot-rehearsal.json
```

**Know what green means.** The default mode seeds a *fresh* temporary `UA_HOME`,
takes a backup of that seeded database and restores it, comparing schema
version, table set and row counts. It never reads the live hub's database, the
nightly snapshot uploaded to `UA_BACKUP_GCS_URI`, or the audit hash chain —
green proves the backup/restore *code path* works on this host, not that
off-host recovery would succeed. To rehearse off-host recovery, fetch the newest
`memory-db/memory-*.db` from `UA_BACKUP_GCS_URI` and follow the cold-standby
promotion drill below into a scratch `UA_HOME` — never over a live `memory.db`.

The `--from-replica` path is the one that reads a real destination: it restores
into a scratch `UA_HOME`, opens the restored `memory.db`, and runs the audit
hash-chain verification against it. Green there means that backup is actually
promotable. Run the drill quarterly, and after any change to the backup or
replication config. Both paths are read-only against the live hub and write only
to a temp dir. The `--from-replica` path additionally uses `litestream restore
-integrity-check quick` and requires a live replica.

### Cold-standby promotion drill (manual only)

When the Mac is lost and a desktop (or new host) must become the hub:

1. **Declare it.** Promotion is an the operator decision, recorded on the task
   ledger — the event chain you're about to take over is the attribution
   record for why it happened.
2. Restore onto the new host. **The replica is gone, so restore from a
   snapshot**: `unified-agent db restore` against the newest
   `memory-db/memory-*.db` in `UA_BACKUP_GCS_URI`. Never restore over a host
   that still has a live `memory.db`. (If replication has been deliberately
   re-enabled, `litestream restore -o ~/.unified_agent/memory.db
   "$UA_REPLICA_GCS_URI"` is the faster path — litestream itself refuses to
   overwrite a non-empty file.)
3. `unified-agent-audit verify` on the restored db — the hash chain must be
   `valid` end-to-end before the hub starts. A broken chain means the backup
   is untrustworthy; stop and investigate rather than starting on it.
4. Point `UA_HOME`/`UA_AGENT_TOKENS`/service.env at the restored state,
   deploy the repo at the last-known-good tag, start the hub.
5. **Retire the old authority in the same breath**: disable
   `com.unified-agent.litestream` and the hub LaunchAgent on any revived
   Mac before it can write again. Two live writers is the only outcome
   worse than the outage.
6. File the promotion as an event-log entry + task note. **Replication does not
   resume by itself** — the new authority keeps the nightly snapshot, and only a
   deliberate decision re-enables replication (§4 above).

Do **not** automate steps 1–6. The DoD for #1197 is deliberately that this
stays a runbook: promotion automation is how cold standbys become split-brain
incidents.
