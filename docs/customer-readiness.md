# Customer readiness guide

This guide describes the evidence needed before a customer pilot. It is a
readiness checklist, not a claim that customer authentication or tenant
isolation is implemented. For the non-developer onboarding materials this
guide summarizes — policy starting points, a reviewer's guide, evidence
checks, and support/rollback steps — see the
[pilot onboarding kit](pilot/README.md).

**Last checked against `main`: 2026-09-23.**

## First pilot result

Use one dedicated pilot organization and hub. The first result is an operator
who can identify the active agent, discover an allowed capability, authorize
one bounded task, see the approval outcome, and inspect the resulting evidence.
The existing Unified-Agent policy, task ownership, approval queue, and evidence
ledger remain authoritative.

WorkOS can provide authentication sessions and organization membership
([sessions](https://workos.com/docs/authkit/sessions),
[users and organizations](https://workos.com/docs/authkit/users-organizations)).
As of task #1202/#1249, the hub itself supports an **optional, off-by-default**
third credential leg so a human can sign in through the organization's own
WorkOS session instead of a shared agent token — set `UA_EDITION=enterprise`
and `UA_HUB_WORKOS_ORGANIZATION_ID` to turn it on; with either unset, the hub
behaves exactly as it did before (see
[WorkOS pilot: optional hub auth leg](workos-pilot.md#optional-hub-auth-leg)).
A WorkOS organization or role still does not, by itself, grant a
Unified-Agent profile, tool permission, task ownership, or approval — it
only supplies a verified human identity for an approval mapped through
`UA_HUB_WORKOS_IDENTITY_MAP`, which is what lets an approval reach the
strongest ("IdP-verified") tier in the evidence report (see
[evidence-report.md](evidence-report.md#idp-verified-producer-class-task-1249--epic-e-e2)).
A reported WorkOS account is still not evidence of a configured application,
callback, secret, mapping, or customer boundary beyond that one leg.

## What is shipped for a document-release pilot (H2)

A pilot whose first workflow is *an agent produces a document, a named human
approves it, and the release is provably tied back to who approved it* uses
the work-product release feature (task #1278): submit, named-reviewer
approval, release, tamper check, and an evidence chain (`human -> device ->
agent -> model -> document hash -> approval`). **It is off by default** —
`UA_WORK_PRODUCT_ENABLED=0` unless a hub operator explicitly turns it on. See
[work-product-release.md](work-product-release.md) for the full mechanism and
the [pilot onboarding kit](pilot/README.md) for vertical-specific policy
starting points (professional services, accounting/tax, healthcare
administration, and legal) and a reviewer's field guide.

The evidence behind a release can also be independently anchored outside the
operator's own database (RFC-3161 timestamp or a signed git commit, task
#1265) and queried as a provenance graph across tasks, approvals, and
delegations (task #1287) — see
[evidence-guide.md](pilot/evidence-guide.md#the-external-anchor-rfc-3161--signed-commit)
and [provenance-graph.md](provenance-graph.md). Both are read/verification
surfaces layered on the same evidence ledger described below; neither changes
what "Authorize" and "Prove" mean in the readiness path.

## Readiness path

1. **Accept:** record the pilot organization, operator, intended workflow, and
   support owner. Keep the pilot hub/data store dedicated to that organization.
2. **Authenticate:** validate the session server-side and map it explicitly to
   the minimum Unified-Agent identity/profile. Deny wrong organization,
   unknown mapping, revoked membership, expired session, and ambiguous state.
3. **Discover:** verify identity with `profile_status()`, then use one of the
   existing discovery contracts: MCP `capability_catalog`, HTTP
   `/api/capability-catalog`, or HTTP `/api/platform/manifest`.
4. **Authorize:** run one bounded user-authorized task. `task_claim` is the
   atomic ownership operation; approval-gated actions stop for human approval.
5. **Prove:** retain the task outcome, approval, stable event/evidence IDs, and
   the distinction between a platform receipt and an external execution result.
6. **Recover:** test forbidden, unknown, unavailable, revoked-session, restore,
   and export paths. Fail closed during ambiguity and document who can resume.

## Acceptance and support checklist

- [ ] Dedicated pilot organization and data boundary reviewed.
- [ ] Session, organization mapping, logout, revocation, and delayed-event tests pass.
- [ ] Discovery returns only the reviewed surface; customer data is not read from a private hub.
- [ ] One bounded task has approval, outcome, and evidence records.
- [ ] Forbidden/unknown/unavailable outcomes stop safely and are supportable.
- [ ] Backup restore rehearsal and sanitized task/memory/journal export are verified.
- [ ] Rollback to the prior authentication adapter and support owner are documented.

The pilot does not establish regulatory compliance, liability transfer, licensing
enforcement, or multi-organization isolation. Multiple organizations require
tenant-scoped storage, queries, events, exports, and negative isolation tests
before sharing a hub.

For implementation details, reuse [Installation](installation.md), [Tools
reference](tools-reference.md), [Evidence report](evidence-report.md),
[Work product release](work-product-release.md), [Provenance
graph](provenance-graph.md), and [Database operations](database-operations.md);
those pages define the commands and fields rather than this guide duplicating
them. For the non-developer-facing version of this checklist, see the
[pilot onboarding kit](pilot/README.md).
