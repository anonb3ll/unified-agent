# Dedicated WorkOS pilot adapter

Task #1092. This is an opt-in, separate-process ASGI app for one organization.
It is not mounted in the private hub and changes no existing bearer/OAuth route.
Live WorkOS application setup and a real operator rehearsal remain unverified.

## Operator preparation (proposal only)

The operator creates the WorkOS test application and organization, chooses the HTTPS
pilot origin, and records an exact `/pilot/callback` redirect and sign-out URI.
Keep application/API/webhook/cookie secrets in the operator's protected secret
environment; never put their values in tasks, shell history, or this document.
Use a dedicated OS account/data directory where practical. Do not point this app
at a copy of the private hub database or mount it under the private hub app.

A fresh process requires these explicit settings:

| Setting | Meaning |
| --- | --- |
| `UA_HOME` | New directory used exclusively by the single-organization pilot; contains `memory.db` and `pilot-auth.sqlite`. It must sit **outside** `UA_PILOT_PRIVATE_HOME` — `PilotAuth` refuses a datastore nested under the private hub home |
| `UA_PILOT_PRIVATE_HOME` | Actual private hub data directory, checked for path overlap; defaults to `~/.unified_agent` |
| `UA_STATE_SYNC=0` | Prevent private vault snapshot writes |
| `UA_EVENT_BUS=0` | Disable implicit event publication; pilot approval events must not reach the private IPC bus |
| `UA_PILOT_ORIGIN` | Exact HTTPS origin, without a path or wildcard |
| `UA_PILOT_IDENTITY_MAP` | **Required**, non-empty. JSON mapping of reviewed WorkOS user IDs to audit labels; every mapping receives the fixed `pilot-reader` capability set. The app refuses to start without it |
| `WORKOS_ORGANIZATION_ID` | The one allowed organization; an auth database cannot be reopened for another organization |
| `WORKOS_CLIENT_ID`, `WORKOS_API_KEY` | Test application binding and API credential, provisioned by the operator |
| `WORKOS_REDIRECT_URI` | Exact HTTPS callback registered on the application |
| `WORKOS_COOKIE_PASSWORD`, `WORKOS_WEBHOOK_SECRET` | SDK-compatible Fernet sealing key (URL-safe base64 encoding of 32 random bytes) and lifecycle signing secret; protected environment only. **Both are required**: `WorkOSSDKProvider.from_environment` reads `WORKOS_WEBHOOK_SECRET` unconditionally, so the process will not start without it even when no lifecycle webhook is configured |
| `WORKOS_ISSUER` | Exact trusted HTTPS issuer, in the application-scoped form `https://api.workos.com/user_management/<client_id>` (see below); never copy an unverified token claim |

With the reviewed environment supplied by the operator, the entry point is:

```sh
uv run uvicorn unified_agent.pilot_app:app_from_environment --factory --host 127.0.0.1 --port 8767
```

This command is a launch proposal, not evidence that a pilot is running. HTTPS
termination, port selection, account configuration, exposure, and service setup
require a separate operator action. No production auth switch is implied.

## Authentication and revocation

`GET /pilot/login` starts AuthKit for the configured organization. The callback
requires an expiring, one-use state bound to the initiating browser through a
Secure/HttpOnly/SameSite=Lax cookie. The resulting pilot session cookie is
Secure/HttpOnly/SameSite=Lax and is scoped to `/pilot`. It is Lax rather than
Strict because sign-in arrives on a redirect chain that starts at the identity
provider, and browsers withhold Strict cookies for that whole chain, so the first
page after sign-in would answer 401. Lax keeps the cookie off cross-site POSTs,
and every mutation also needs the `x-ua-csrf` header and a same-origin `Origin`.

The maintained WorkOS Python SDK exchanges and authenticates a sealed session.
The adapter additionally verifies RS256 signatures with the SDK's application-
specific JWKS, the explicitly configured trusted issuer, required `exp`/`iat`/`sub`/`sid`/`org_id`,
organization and user/session consistency. AuthKit tokens need not carry `aud`;
application binding comes from client-specific JWKS and the configured code
exchange. If `aud` is present, it must match that client. Email domains and
WorkOS role names never grant UA capabilities.

There is no refresh or offline-grace fallback: expired sessions require login.
Every authenticated request checks current organization membership and exact
WorkOS session status. Revocation takes effect on the next request after WorkOS
reports it; provider outages deny access. In-flight operations already authorized
are not cancelled. No claim is made about WorkOS's upstream propagation delay.
Removing or changing the local user mapping also denies existing sessions.

`POST /pilot/lifecycle` verifies WorkOS's signature and a 180-second past/future
window. Relevant membership/session notifications only invalidate sessions;
they cannot enable a user. Exact duplicate IDs are idempotent. Delayed and
out-of-order notifications conservatively invalidate current sessions rather
than restoring stale access. A new login checks the authoritative provider again.
Wrong-organization notifications fail closed. Unrelated verified events are ignored.

`POST /pilot/logout` requires the existing `x-ua-csrf` convention and same-origin
check, revokes the local session first, clears cookies, and redirects to WorkOS
logout. The existing private adapter remains untouched.

## One governed workflow

`GET /pilot/discovery` exposes only the mapped label, organization, fixed profile,
and supported pilot capabilities. There are no task, approval-resolution,
dispatch, vault, platform-inventory, or MCP routes in this app.

`POST /pilot/workflow` accepts only `{"request_id":"<UUID>"}` with the session
cookie and `x-ua-csrf` header. It invokes the existing **forced** one-shot approval
gate for `pilot_record_verification`, bound to the exact organization, subject,
audit identity, and request ID. Pending returns 409 with an approval ID; denial
returns 403. WorkOS users cannot resolve approvals through this app.

A separate authenticated human operator against the **pilot's own data store**
reviews that exact payload using the existing approval tools. That operator must
also run with `UA_EVENT_BUS=0`; otherwise its resolution event can publish to the
private IPC socket. After approval,
repeating the request appends one sanitized, deduplicated `pilot.verification`
event to the existing hash-chain ledger and returns only event/approval IDs and
organization. Replaying a consumed pass requires fresh approval. This bounded
recording workflow demonstrates authority and evidence; it executes no external
customer work and contacts no customer.

## Verification, recovery, and rollback

Run `uv run pytest -q tests/test_pilot_auth.py tests/test_pilot_app.py`.
The suite uses disposable stores, a fake provider for deterministic lifecycle
changes, signed RSA tokens for the real adapter's JWT-validation path, and the
real platform approval/ledger functions. It covers browser-state theft/replay,
wrong organization/user, expiry, provider outage/revocation, mapping changes,
lifecycle duplicates/reordering, CSRF, private-route absence, database separation,
and pending/denied/approved/replayed requests. Synthetic evidence is not a live
WorkOS or customer-acceptance claim.

Code-side acceptance checks for #1092 are explicit:

| Acceptance item | Deterministic proof | Live limit |
| --- | --- | --- |
| Revocation window | `test_provider_revocation_outage_mapping_change_and_events` and `test_http_login_csrf_provider_revocation_logout_and_no_private_routes` prove denial on the next request after the provider reports revocation or becomes unavailable | WorkOS's upstream propagation time requires an account-side rehearsal |
| Org A/B isolation | `test_org_a_cannot_read_org_b_session_or_ledger_rows` creates separate org stores, a B session and B ledger event, then proves A's auth and platform store cannot resolve the B session or read that event; the runtime boundary also refuses B's app under A's data root | This proves the dedicated single-org store boundary, not multi-org row scoping in one shared store |
| Approved workflow | `test_real_pending_denied_approved_gate_and_sanitized_evidence` uses the real local approval gate, checks one recorded event, and verifies sanitized output with a fake identity provider | A live AuthKit sign-in and operator decision remain separate acceptance steps; this test does not verify hash-chain integrity |
| Rollback | `test_process_boundary_and_rollback` proves the pilot cannot mount on the private root and the private cookie path stays independent | Stopping the live pilot process and route is an operator action |

These checks do not read WorkOS account secrets, change service environment, or
claim a live OAuth round-trip. The dedicated pilot process is not promoted by
passing them.

For recovery, stop the dedicated pilot before backing up both SQLite files with
the existing SQLite backup procedure; restore only into a fresh scratch pilot
root and recheck the organization marker. Session restoration does not bypass
current provider validation. A real backup/restore drill remains required before
promotion. Do not copy pilot sessions or data into the private hub.

Rollback is to stop the separate pilot process and remove its customer-facing
routing through the operator's normal change process. No private auth route was
replaced and no private data migration is required. Verify private bearer/UI
access separately. No operator process or routing was changed in this PR.

Primary references: [Python AuthKit integration](https://workos.com/docs/authkit/vanilla/python),
[sessions](https://workos.com/docs/authkit/sessions), and
[organization memberships](https://workos.com/docs/authkit/users-organizations).

The issuer is application-scoped. Set:

```
WORKOS_ISSUER=https://api.workos.com/user_management/<client_id>
```

Two plausible-looking alternatives are both wrong and both fail every login
silently, because the adapter compares exactly against `WORKOS_ISSUER` and never
relaxes validation or infers trust from an incoming token:

- The AuthKit discovery-document issuer, `https://<environment>.authkit.app`.
  That value identifies the *authorization server*; the access token's `iss` is
  application-scoped and does not match it.
- `https://api.workos.com/` from WorkOS's sessions guide, which omits the
  `/user_management/<client_id>` path.

The application-scoped form is the one carried by a live access token's `iss`
claim, and matches the [emulator guide](https://workos.com/docs/cli/emulate).
Note that the *authorize* endpoint still lives on
`https://api.workos.com/user_management/authorize` — a different host and role
from the issuer used for JWKS and token verification. Both are correct.

## Optional hub auth leg

Everything above describes the separate-process pilot, which is unchanged and
remains the fallback. Task #1202 adds an *optional* third credential leg to the
private hub itself, alongside the bearer token and the OAuth access token, so a
human can reach the hub through their own identity provider instead of a shared
agent token.

It is off unless **both** are true: `UA_EDITION=enterprise`, and
`UA_HUB_WORKOS_ORGANIZATION_ID` names the one organization to accept. With
either missing, `workos_hub_auth.hub_auth()` returns `None` and the middleware
behaves exactly as it does today.

A caller presents an already-sealed AuthKit session under its own scheme:

```
Authorization: WorkOS <sealed session>
```

A distinct scheme rather than a cookie is deliberate. `/mcp` is bearer-only so
that a cross-site request riding a cookie can never reach a tool call; a header
credential is not sent automatically by a browser, so the leg is usable on
`/mcp` without reopening that hole.

Verification is the pilot's, through the same code
(`WorkOSSDKProvider.authenticate_sealed` then `.active`), not a second
implementation: JWKS signature, exact configured issuer, required
`exp`/`iat`/`sub`/`sid`/`org_id`, audience, `sub`/`sid` agreement, the one
configured organization, a live membership re-check and live session state.
`.active` runs on **every** request, so a revoked session or a withdrawn
membership stops working on the next call rather than at token expiry.

`UA_HUB_WORKOS_IDENTITY_MAP` is the entire authorization decision — a reviewed
JSON object from WorkOS user ID to an existing UA agent label:

```
UA_HUB_WORKOS_IDENTITY_MAP='{"user_01ABC...": "Desktop_Person"}'
```

An unmapped subject is denied, and a mapped label the roster does not already
know is denied too: this leg selects among identities the operator registered,
it never mints one. WorkOS roles, permissions, groups and email domains are not
read for anything. Provider-side authority does not become UA capability.

A resolved request binds the mapped label to `config.verified_agent_identity()`
exactly as the bearer leg does, and records non-secret provenance
(`auth_method`, WorkOS user/organization/session ids) in
`config.current_auth_metadata()`. A bearer or OAuth token still takes
precedence; the WorkOS leg is only consulted when neither resolved an identity.

Set `UA_ENFORCE_AUTH=1` alongside it. The hub's default is warn-only, where an
unauthenticated request is still served, so a WorkOS login without enforcement
gates nothing. The leg logs a warning naming `UA_ENFORCE_AUTH` when it is built
without it, but it does not flip an operator's enforcement setting for them.

Rollback is to unset `UA_HUB_WORKOS_ORGANIZATION_ID` and restart. No bearer
token, OAuth client or UI cookie behavior changes either way.
