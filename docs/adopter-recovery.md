# Adopter enrollment and recovery boundaries

This guide describes the current dedicated-hub model and the checks an owner
must perform. It does not authorize changing a live installation, exporting
credentials, relaxing policy, or enabling multiple tenants.

## Owner and worker enrollment

Initialize a new installation in a private state directory with the desired
operator identity and workspace. The bootstrap creates separate worker and
operator credentials. Store them privately and use the operator credential for
the human Control Center login; a worker credential is not a human login.
Merged PR #978 repairs hostname labels, forced-init preservation and worker
aliases and is integrated in this change. Verify that the installed artifact
contains those repairs before repeating init on an existing deployment.

Operator aliases identify the same person. Do not enroll several colleagues as
aliases, or name a worker as an operator alias to make approvals work. The
current operator tool allowlist covers approvals and status but omits
`config_revision_manage`. A fresh human-plus-worker install therefore does not
yet provide a complete self-service policy administration/recovery path. The
Operating tab's draft (`/ui/agents/teams?tab=operating`) is not a policy commit interface. A reviewed owner
bootstrap/recovery design remains necessary; no broader cloud/steward role is
an appropriate substitute. Existing privileged fleet labels are not a neutral
onboarding contract.

Before handing over a hub, verify identity and profile for each credential,
explicit remote filesystem DENY bindings, and an actual governed operation:
worker requests, a different verified human approves, and an identical retry
consumes exactly that pass. Confirm rejection of missing/wrong credentials and
self-approval. Optional WorkOS mappings require a consistent admission
organization and row-stamping organization; IdP enrollment alone does not
establish application roles or tenant isolation.

Use a separate process, state home, credentials, workspace bindings and recovery
set per unrelated client organization today. PR #974's memory fences and the
other tenant follow-ups do not establish all-object multi-tenancy. Several
independent human approvers need an explicit principal/role design, not a setting
that silently broadens the current single-operator model.

## Database snapshots and installation recovery

`create_backup` is an integrity-checked SQLite snapshot of `memory.db`. It is not
a complete installation export. New snapshot temp/final files are owner-only
(`0600`) regardless of umask, including in an existing `0755` directory. A
directory writable by group/other users is refused. The function does not
chmod or migrate old snapshots; the owner should review historical backup
permissions separately before sharing or moving a backup destination.

A recovery manifest should list paths/references, versions and custody, without
secret values:

| Item | Recovery requirement |
|---|---|
| `memory.db` | Verified snapshot, schema version, capture time and integrity result; preserve evidence after the capture separately when needed. |
| `settings.toml` | Owner-reviewed effective settings and source precedence; record state/workspace/vault locations without vault contents. |
| `service.env`, token files and managed secrets | Authorized protected copy or documented re-enrollment; never attach them to a report or commit them. |
| Managed-secret encryption keys | Independent protected custody; a secret ciphertext file without its required key is not recovery. |
| External policy, organization and operating documents | Exact reviewed revisions and principal/workspace bindings; a default org is not equivalent to the installation's authority. |
| WorkOS/OAuth/device/operator/signing material | Owner-approved recovery or re-enrollment procedure; retain evidence-verification material as required. |
| Wheel/image/UI/catalog/SDK | Known versions/hashes and compatible artifacts; a checkout SHA does not attest the running process. |
| Supervisor definitions and schedules | Executable/state paths and deliberate environment overrides; document which jobs are enabled. |

Do not automate credential escrow as part of a database backup. Choose local
private backup storage first; off-site destination, encryption, custody and
retention need explicit owner configuration and a tested restore procedure.

## Upgrade and rollback rehearsal

Before upgrading, record the running artifact identity and effective config,
preserve a verified database snapshot and the matching owner-approved recovery
set, and rehearse against scratch state. Starting newer code can migrate the
database automatically. Older code explicitly refuses a newer schema, so a
code-only downgrade may fail. Keep the old artifact with its matching database
and configuration; deciding how to retain post-snapshot evidence is part of
rollback planning.

Use the existing scratch-only restore rehearsal guards. Confirm that destination
paths differ from the live state home/database, that providers and scheduled
jobs cannot act on production, and that the rebuilt hub passes integrity,
identity, approval and affected-behavior checks. A green scratch rehearsal does
not authorize swapping the production database or restarting services. Those
steps belong to the installation's designated owner/deploy procedure.

An evidence export timer that resumes after an outage exports its configured
window (by default the previous completed UTC day); it does not backfill every
missed date. Record coverage and explicitly plan missed-window exports. Export
artifact retention, database backup generations, telemetry retention and
intentional ledger pruning are separate controls.

## Task checkout recovery observed during this change

Task #1689 initially lacked `[code-task]`. Its claim created a cockpit worktree
with a cockpit source SHA. Merely adding a note or creating a second checkout
does not correct structured execution metadata. The author ended its own run
with `worktree=False`, added `[code-task]`, then reclaimed through the supported
task lifecycle. The new run preserved earlier checkouts and bound an absolute
product worktree/branch at the expected product SHA.

Verify the returned repository, absolute path, branch, source SHA and task
binding before code-bearing execution or review. Never validate the wrong
checkout, edit the hub database directly, delete another session's tree, or
widen an agent's filesystem permissions to repair this mismatch. A clearer
product-task creation/preflight contract remains an adopter follow-up.

See [the full disposition](adopter-audit-disposition.md) for remaining authority,
configuration, packaging and recovery work.
