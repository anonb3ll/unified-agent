# WorkOS platform attachment map

Status: design only. This note maps WorkOS products onto existing product seams. It does not add a dependency, change authentication, expose an endpoint, or authorize a pilot.

## Boundary

WorkOS belongs at the product and customer-tenant edge. The current hub remains the private authority for agent credentials, profiles, tasks, approvals, events, sessions, and evidence. WorkOS claims must never self-approve a tool, dispatch work, change task ownership, or turn on a capability.

Task #1092 owns the dedicated single-organization pilot, its separate process/data store, server-side session validation, isolation tests, and revocation. Its constraint is binding here: the current task, approval, event, and checkpoint stores are hub-wide; profiles select capabilities, not data partitions. An `org_id` claim alone therefore cannot isolate rows. This note only identifies where a future tenant-aware adapter would attach; tenant-scoped enforcement is required before any future multi-organization sharing.

## Attachment table

| WorkOS product | Existing repo seam | Attachment and evidence boundary | First useful consumer | Decision or guard |
|---|---|---|---|---|
| AuthKit + SSO | `src/unified_agent/webui/auth.py:create_session`; `src/unified_agent/webui/routes.py:session_create`; `src/unified_agent/profiles.py:_allowed_tools_for_profile` | An external product surface would authenticate a human and resolve a minimum UA identity/profile before the existing UI session and profile capability check. Standalone SSO is also compatible with an existing auth stack; AuthKit is the fuller platform choice. On every callback, validate the returned WorkOS profile’s `organizationId` against the organization selected for the flow. Never use an email domain as the organization check: WorkOS explicitly calls that unsafe for guests and external addresses. | A customer-facing sign-in and organization handoff for a future dedicated pilot. It must end at a tenant-aware adapter before touching hub data. | The operator decides whether any external surface is exposed and whether AuthKit or standalone SSO is adopted. #1092 owns session validation, mapping, logout, and revocation; no live auth switch follows from this note. [SSO guide](https://workos.com/docs/sso) |
| Connect (OAuth applications and M2M) | `src/unified_agent/oauth.py` (existing OAuth authorization server and `client_credentials` grant); `src/unified_agent/webui/auth.py:create_session` | Connect is the machine/client boundary. A validated access token’s `org_id` can enter a future request context and be carried into the dedicated pilot’s product queries, run/session metadata, and sanitized exports. It must not be threaded into today’s hub-wide SQL as if it were isolation. | A separately approved customer workflow or external integration consuming the dedicated pilot surface. | #1092 establishes the one-organization pilot boundary first. Tenant-scoped storage/query/event/export enforcement and negative isolation tests are prerequisites for any later multi-organization sharing. The operator decides whether OAuth/M2M is needed and which object surface is exposed. [OAuth applications](https://workos.com/docs/authkit/connect/oauth) |
| RBAC | `src/unified_agent/profiles.py` (`ActiveProfile`, `tool_is_allowed_for`, `_allowed_tools_for_profile`); `src/unified_agent/tools/capability.py:get_agent_allowed_tools` | WorkOS organization memberships, roles, permissions, and IdP group mappings are an input to a future customer authorization adapter. They should map to explicit product permissions and then to the existing profile/tool decision point. Do not replace the profile gate with role-name checks; permissions must be evaluated at the action boundary. | A customer admin assigning who may discover, request, approve, or inspect a product capability. | RBAC is a successor direction behind the control-plane/object-model work; it is not a live replacement for `UA_AGENT_TOKENS` or profile allow/deny lists. Prefer Directory Sync group role assignment when available; SSO group assignment updates on authentication. [RBAC integration](https://workos.com/docs/rbac/integration) |
| Audit Logs | `src/unified_agent/events.py`; `src/unified_agent/tools/events.py:event_log_query`; `src/unified_agent/audit/evidence_report.py`; `src/unified_agent/tools/evidence.py` | Export customer-facing, sanitized action records from the existing append-only event/evidence chain. Preserve the local hash chain as the proof of what the hub recorded; WorkOS Audit Logs is the buyer/SIEM export contract, not a replacement authority. Event payloads need stable action, actor, target, context, organization, and idempotency fields at the product adapter boundary. | Compliance export or SIEM/log-stream handoff for a tenant-approved workflow. | Do not emit private topology, bearer values, prompts, vault content, or raw credentials. Define the customer event vocabulary and idempotency policy before implementation. [Audit Logs](https://workos.com/docs/audit-logs) |
| Directory Sync | `src/unified_agent/webui/routes.py` (webhook-style route seam); `src/unified_agent/events.py`; product-side membership mapping owned by #1092 | Directory lifecycle events would update the customer organization/membership projection in the dedicated tenant store. Process verified events asynchronously after prompt acknowledgement, use idempotent upserts, and treat inactive users as a deprovisioning signal. Directory groups can feed WorkOS role assignment; they must not directly mutate hub-wide task or approval rows. | Customer IT onboarding and deprovisioning for a dedicated pilot organization. | #1092 owns membership-to-UA mapping and revocation semantics. Prefer Directory Sync over SSO-only group assignment when real-time role propagation is required. [Directory Sync](https://workos.com/docs/directory-sync) |
| Admin Portal | `src/unified_agent/webui/routes.py`; `src/unified_agent/webui/auth.py`; future product settings surface | Generate a short-lived, single-use portal link from a customer settings surface so an IT contact can configure SSO or Directory Sync. The product receives the resulting verified organization/connection state through the tenant adapter; it does not expose hub credentials or a private dashboard. | First-pilot onboarding: customer IT configures its IdP/directory without bespoke settings UI. | Portal links are generated on demand and redirected from the app settings page; do not email them from the backend. The operator decides the pilot organization and whether this onboarding surface is exposed. [Admin Portal](https://workos.com/docs/admin-portal) |

## Sequencing

1. **Boundary decision (the operator).** Decide whether the product will expose a customer surface, and choose the initial AuthKit/SSO versus existing-auth integration. No live bearer path changes at this step.
2. **Pilot boundary (#1092).** Establish the dedicated single-organization process/data store, server-side session validation, and negative boundary tests. Treat `org_id` as descriptive context in the current hub; add tenant-scoped storage/query/event/export enforcement before any later multi-organization sharing.
3. **Identity and onboarding.** Attach AuthKit/SSO callback validation, Directory Sync membership lifecycle, and Admin Portal onboarding to the dedicated tenant adapter. Validate `organizationId`; never infer an organization from an email domain.
4. **Authorization.** Map verified WorkOS permissions and membership roles to product actions at `profiles.py`/capability checks. Keep the current profile/token path as the rollback adapter until an explicit promotion.
5. **Evidence export.** Define sanitized customer event types and idempotency, then project the local evidence/event chain to Audit Logs or a SIEM for one separately approved workflow.
6. **Connect expansion.** Add OAuth/M2M consumers only after the dedicated tenant and authorization boundaries are proven. Their `org_id` must be enforced by every storage, query, event, and export path.

## Buy versus build

Buy the enterprise edge: WorkOS already supplies SAML/OIDC connection handling, AuthKit organization membership and sessions, Directory Sync, role-assignment inputs, Admin Portal onboarding, and Audit Log export contracts. Building those protocols, lifecycle retries, provider quirks, admin flows, and compliance export semantics in this repository would create a second identity and enterprise-integration platform outside our differentiator.

Build only the product adapter: map a verified WorkOS organization and membership to the minimum UA identity/profile, enforce tenant scope in the dedicated store, apply existing capability decisions, and emit sanitized evidence. This keeps WorkOS replaceable and leaves the private hub authority unchanged.

## The operator-owned decisions

- Whether any customer-facing ingress or sign-in surface is exposed.
- Whether the first integration uses AuthKit or standalone SSO alongside the current bearer/OAuth adapter.
- The dedicated pilot organization, data boundary, and customer workflow.
- Whether WorkOS becomes a runtime dependency or remains an optional adapter; no SDK/package installation is authorized by this design.
- The customer event schema, export destination, retention, and approval gate for any external workflow, plus the evidence threshold for live promotion with rollback preserved.

## Explicit non-goals

- No new WorkOS package, environment variable, secret, credential, webhook, or client configuration.
- No production auth switch, bearer-token migration, public ingress, or customer exposure.
- No redesign of session validation, isolation, membership revocation, or pilot storage; those belong to task #1092.
- No assumption that the existing hub-wide stores become tenant-safe merely by adding `org_id` to a token or event.
- No WorkOS claim is an approval, task mutation, dispatch instruction, or capability enablement.

## Sources and verified repo seams

Official primary references used for the attachment claims:

- [WorkOS SSO](https://workos.com/docs/sso) — AuthKit versus standalone SSO, organization-scoped initiation, callback `organizationId` validation, and the email-domain warning.
- [WorkOS AuthKit organizations](https://workos.com/docs/authkit/users-organizations) — organization memberships as the B2B workspace boundary.
- [WorkOS Connect OAuth applications](https://workos.com/docs/authkit/connect/oauth) — selected organization in the `org_id` access-token claim.
- [WorkOS RBAC integration](https://workos.com/docs/rbac/integration) — organization membership roles, permissions, and IdP assignment.
- [WorkOS Audit Logs](https://workos.com/docs/audit-logs) — customer audit event and export surface.
- [WorkOS Directory Sync](https://workos.com/docs/directory-sync) and [Admin Portal](https://workos.com/docs/admin-portal) — onboarding and directory lifecycle surfaces.

Verified local seams are the named functions/modules in the table: profile capability filtering in `src/unified_agent/profiles.py`, UI sessions in `src/unified_agent/webui/auth.py`, agent sessions in `src/unified_agent/registry.py`, task/approval stores in `src/unified_agent/tools/tasks.py` and `src/unified_agent/tools/approvals.py`, the append-only event query in `src/unified_agent/tools/events.py`, and evidence projection in `src/unified_agent/audit/evidence_report.py` and `src/unified_agent/tools/evidence.py`.
