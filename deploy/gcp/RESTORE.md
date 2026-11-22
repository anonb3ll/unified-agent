# GCS backup restore rehearsal (task #569)

Offsite copies live in `gs://ua-worker-backups-000000000000/memory-db/`.
Local Mac snapshots under `~/.unified_agent/backups/` remain the source of
truth. This runbook rehearses **readiness**. It does not replace the live hub
database.

## Absolute refusals

- Do **not** copy a snapshot over Mac `~/.unified_agent/memory.db`.
- Do **not** restart `com.unified-agent.hub` for a drill.
- Do **not** point desktop or GCP at the copy as a second authority.

Throwaway Tailscale snapshot-serve is a separate rehearsal: FAILOVER.md.
It still must not replace the live Mac database.

`unified_agent.backup.stage_backup_snapshot` and
`deploy/gcp/restore-backup-to-temp.sh` refuse destinations inside `UA_HOME`.

## Drill (temporary destination)

From a machine with `gcloud`/`gsutil` and bucket read access:

```bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
export UA_GCP_PROJECT=ua-worker
./deploy/gcp/restore-backup-to-temp.sh /tmp/ua-restore
```

Expected: JSON with `verification.ok=true` and `counts` for `tasks`, `memory`,
`delegations`. Desktop `~/.unified_agent/memory.db` stays the 82-byte stub
marker. Mac live `memory.db` is untouched.

### Prove the app can open the copy

Use a **throwaway** `UA_HOME` that is not the live hub home. Copy the verified
file to `memory.db` in that directory (`stage_backup_snapshot(..., as_ua_home=True)`
or `cp`). Then:

```bash
UA_HOME=/tmp/ua-restore-home UA_STATE_SYNC=0 UA_VAULT_PATH= \
  UA_WORKSPACE=/tmp/ua-restore-ws \
  .venv/bin/python -c 'from unified_agent.tools.tasks import task_list; print(task_list(status="open", fields="compact", limit=3))'
```

Tear down with `rm -rf /tmp/ua-restore /tmp/ua-restore-home /tmp/ua-restore-ws`.
That is the rollback for this drill.

A separate HTTP listener on a non-default port is optional and is **not**
required to pass readiness. If you start one, bind `127.0.0.1` only, leave the
live Mac hub running, and kill the throwaway process when done.

## Production restore (the operator-owned)

Restoring **onto** the live Mac database is a different procedure: stop the
hub, replace `memory.db` from a verified snapshot, start the hub, confirm
`/healthz` and `task_list`. That path is not automated here and is not a
developer action.

## Hygiene companion

`./deploy/gcp/sweep-hygiene.sh` records instance shape, idle IPs, budget
alerts, backup recency, and prohibited AI API drift. A
`cloudaicompanion.googleapis.com` warning is an the operator decision (disable or
keep), not a restore failure.
