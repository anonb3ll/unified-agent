# GCP cost guards (project `ua-worker`)

In September 2026 an un-pruned litestream replica's listing loop billed about
600 GiB/day of egress, roughly $65/day, for six days (#1410). The fix added two
guards. This directory holds what was applied to GCP and a read-only check that
the live resources still match it.

| File | What it is |
|---|---|
| `egress_policy.json` | Monitoring alert policy: backups bucket over 5 GiB/h, or any bucket over 10 GiB/h, emails the operator |
| `lifecycle.json` | Backups-bucket lifecycle rules: clear `litestream/`, and delete `memory-db/` snapshots older than 30 days plus the noncurrent copies a delete leaves (versioning is on). Retention set by the operator 2026-11-18 |
| `verify_cost_guards.py` | Compares live state with the two payloads (10 checks, including that no rule reaches a snapshot under 30 days old and that the newest snapshot is under 48 h old, since the age rule would otherwise empty the prefix if the nightly job stopped) |

## Run it

From any host with an authenticated `gcloud`. The Mac hub has one at
`/opt/homebrew/bin/gcloud`, and the Linux desktop desktop has one too.

```bash
python3 deploy/gcp/cost-guards/verify_cost_guards.py
```

Exit codes: `0` the guards hold, `1` drift (the failing checks are listed), `2`
live state could not be read (no `gcloud`, not signed in, network). An exit of
`2` means unknown, not drift.

The script only reads. It never changes the alert policy, lifecycle rules,
bucket IAM or snapshots. To change a guard, apply the change in GCP, then update
the payload here in the same PR.

## Scheduled or manual

It stays **manual** (decision 2026-09-28, #1447). The alert policy is already
the live pager for real egress. This script proves the guards themselves have
not drifted, and they change only when someone edits GCP. Run it:

- after any change to the backups bucket, its lifecycle or the alert policy;
- after provisioning a hub host, until #1411 (the deploy script still installs
  litestream on fresh hosts) is closed, because a fresh install could re-arm
  the replica.

A scheduled run would need `gcloud` credentials on an always-on host and would
page on exit `2` as well as on real drift. Revisit if #1411 stays open.

Last verified: 2026-09-28 from the Mac hub, 9/9 PASS, exit 0.
