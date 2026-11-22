# Support and rollback

What to do if something goes wrong during the pilot, how to turn the
work-product release feature off without losing anything, and how to get
your data out before ending the pilot. Written for the pilot's operational
owner (a COO, risk owner, or IT lead), not a developer.

## How to reach support

**[Your hub operator fills this in before the pilot starts: a named contact,
an email or ticketing address, and the hours support is available. This
guide does not invent a contact — there is no default support address
shipped with the software.]**

Before contacting support, it helps to have on hand:

- The approximate time the problem happened.
- Whether it involves a specific document (`work_product_id`) or approval
  (approval `id`) — both are visible in the Control Center Inbox and in
  `unified-agent work-product list`.
- The output of `unified-agent doctor` or the hub's `system_doctor` /
  `mcp_health_telemetry` check, if you can run it — this gives support a
  snapshot of the system's health without needing live access.

## Turning the work-product release feature off

The H2 work-product workflow (submit -> named-reviewer approval -> release)
is **off by default** — it only runs when the hub operator has explicitly
set `UA_WORK_PRODUCT_ENABLED=1`. To turn it back off:

```bash
# On the machine or service running the hub, unset or zero the flag:
UA_WORK_PRODUCT_ENABLED=0
```

Set this in the hub service environment, then restart the hub by the normal
operator procedure. Running the line in a separate shell does not update an
already-running service. With the flag off:

- New documents can no longer be submitted for release, and the CLI
  (`unified-agent work-product submit`) refuses with a clear "disabled"
  message instead of a confusing error.
- **Nothing already recorded disappears.** Submitted, approved, and
  released documents remain readable through `unified-agent work-product
  list` and `unified-agent work-product evidence <id>`. The Inbox
  shows pending, approved-but-unconsumed, and expired passes; it is not a
  complete history of consumed or denied decisions. Disabling the write
  path does not delete existing records or evidence.

This flag controls only the work-product feature. It does not touch task
management, memory, the approval queue in general, or anything else the hub
does — those keep running normally.

## Rolling back further

If the pilot needs to stop entirely, or roll back to how things worked
before the pilot started:

- **Work-product release**: set `UA_WORK_PRODUCT_ENABLED=0` as above.
- **A WorkOS sign-in leg**, if your organization turned one on for reviewer
  identity verification: unset `UA_HUB_WORKOS_ORGANIZATION_ID` (and the
  related `WORKOS_*` variables) to remove that authentication leg. Approvals
  fall back to the existing account-credential authentication the hub used
  before — reviewers keep working, just without the stronger IdP-verified
  evidence tier described in [evidence-guide.md](evidence-guide.md).
- **An external evidence anchor**, if one was configured
  (`UA_EVIDENCE_ANCHOR_BACKEND`): this can be left running (it only adds
  evidence, it does not gate anything) or turned off by reverting the
  variable; nothing about turning it off deletes an anchor already recorded.

In every case, the evidence ledger itself is append-only and is not deleted
or rewritten by disabling a feature — only the ability to create *new*
entries of that kind changes.

## Exporting your data before you exit

Take a full database backup, and export the human-readable records
separately, before decommissioning a pilot hub.

**Full database backup** (everything: tasks, approvals, evidence, work
products):

```bash
unified-agent db backup --out /path/to/final-backup.db
unified-agent db verify --path /path/to/final-backup.db
```

`db backup` is an online, non-blocking, transactionally consistent snapshot
— it is safe to run while the hub is still in use. `db restore
/path/to/final-backup.db` on a fresh install brings that state back.

**Record exports** of specific types, if you want portable data rather
than the raw database:

```text
export_data(action="tasks")     # task list as CSV, JSON, or Markdown
export_data(action="memory")    # memory notes as JSON/JSONL
export_data(action="journal")   # activity journal as JSONL
```

These are available through the MCP tool surface or hub HTTP API.
They can return data inline or write it to the configured vault export
location. Task notes, memory, and journal entries may contain personal or
confidential information: review and redact the result before sharing it
outside the pilot organization.

**Evidence exports** for anything you want to retain as a standalone,
independently verifiable record (for example, before a document's evidence
chain is only reachable through a hub you are about to decommission):

```bash
unified-agent work-product evidence <work_product_id> > work-product-<id>-evidence.txt
unified-agent evidence --task <task_id> --json --out task-<id>-evidence.json
unified-agent evidence verify task-<id>-evidence.json   # confirm it before you file it away
```

See [evidence-guide.md](evidence-guide.md) for what these exports do and do
not prove, and [database-operations.md](../database-operations.md) for the
full backup/restore reference.
