# Hosted hub on Railway

Task #2108, epic #2107. On 2026-10-15 the operator decided to move the hub to a cloud
control plane on Railway, which supersedes the #1441 verdict in
pilot-host-verdict.md. This page covers the deployment
surface: the image, the Railway service definition, and the first boot. Locked
decisions for this phase:

- **One organization.**
- **One service.** It serves the Python hub and the Control Center on one
  origin, so same-origin cookies and CSRF work unchanged.
- **One volume, one writer.** SQLite/WAL, `service.env` and the vault live on a
  Railway volume mounted at `/data`. Railway refuses replicas on a service with
  a volume, which matches the single authoritative writer this phase needs.
  This is not a high-availability design.
- **Devices execute.** Agents run on enrolled devices (`ua-node`), never on the
  cloud hub. The server-side execution boundary (#2109) refuses local agent
  launches, including direct ACP process construction and wrapped commands.

## What the image does on Railway

`deploy/docker/entrypoint.sh` treats a start as *hosted* when
`RAILWAY_ENVIRONMENT_ID` is set (Railway injects it) or `UA_HOSTED=1`.

| Concern | Self-hosted docker / compose | Hosted (Railway, `UA_HOSTED=1`) |
|---|---|---|
| Bootstrap token | Printed once to the container log | **Never printed.** It stays in `/data/service.env` (mode 0600). Inject `UA_AGENT_TOKENS` instead (see below) |
| Precedence | `service.env` on the volume wins over container env | **Platform variables win** over `service.env`, on every start, so a rotated dashboard value takes effect on the next deploy. Image `ENV` defaults are not platform variables: the entrypoint omits a key still equal to its Dockerfile default (`UA_EDITION=enterprise`, `UA_WORKSPACE=/data/workspace`, …), so `service.env` and a real runtime value win, and the default applies only while the key is unset |
| State location | Whatever is mounted at `/data` | Refuses to start (exit 64) on Railway with no volume attached, or with `UA_HOME` outside the volume. A throwaway test can set `UA_ALLOW_EPHEMERAL_STATE=1` |
| Port | 8765 | `PORT` (the service definition sets 8765) |
| Host allowlist | `127.0.0.1:*,localhost:*` plus `UA_PUBLIC_HOSTS` | Also `RAILWAY_PUBLIC_DOMAIN`, `RAILWAY_PRIVATE_DOMAIN` and `healthcheck.railway.app`, the Host header of Railway's healthcheck probe |
| `UA_PUBLIC_URL` | unset | `https://$RAILWAY_PUBLIC_DOMAIN` unless set. The deployment mode therefore resolves to `authenticated_public`, which refuses to start without credentials |
| Auth | Enforced; `UA_DOCKER_ALLOW_INSECURE=1` can skip it | Always enforced. The insecure escape hatch is ignored |
| User | uid 10001 | Railway volumes are root-owned, so the service sets `RAILWAY_RUN_UID=0`. The entrypoint starts as root only to take ownership of `/data`, then drops to uid 10001 before anything else runs. If the drop fails, the start fails. The hub never runs as root unless `UA_ALLOW_ROOT=1` |
| Logs | The process's own logging, including Rich text on stderr | JSON, one object per line. With `UA_TRANSPORT=http`, INFO and WARNING go to stdout with `"level":"info"` or `"warn"`; ERROR goes to stderr with `"level":"error"`. With stdio transport, all logs go to stderr so stdout carries only protocol messages. WorkOS org ids (`org_…`), user ids (`user_…`), and bearer tokens are redacted. `UA_LOG_FORMAT=json` turns this on without the hosted flag |

The Dockerfile has no `VOLUME` instruction. Railway rejects it ("docker VOLUME
... is not supported, use Railway Volumes"), and that is why both earlier
deploys failed. `compose.yaml` declares its own named volume.

## Service definition: `.railway/railway.ts`

Railway's Config as Code (`railway.json` / `railway.toml`) is deprecated, new
services cannot use it, and Railway stops reading it on 2026-12-01. The service
is therefore defined with Railway Infrastructure as Code in
`.railway/railway.ts`. It is a **named partial** (`unified-agent-hub`), so an
apply manages only the hub service and its volume and cannot delete anything
else in the project.

```bash
cd .railway
npm install                    # the railway IaC SDK (devDependency)
railway link                   # choose the project and environment
railway config plan            # read-only preview
railway config apply           # applies after an interactive confirmation
```

Run `railway` directly, not through `timeout` or another wrapper. The SDK
checks the CLI version through `$_`, which a wrapper replaces, and evaluation
then fails with "requires Railway CLI 5.42.1 or newer" whatever the real
version is.

Applied to the `modest-commitment` project on 2026-10-16 (the operator's go).
The apply set the GitHub source (`main`), the Dockerfile builder, the
healthcheck, and `PORT` and `RAILWAY_RUN_UID`, and it created the `ua-data`
volume (5000 MB, the Hobby-plan maximum, in `sfo`). An apply that asks for more
than the plan allows fails with "Max size of 5000 MB on current plan".

The restart policy (`ON_FAILURE`, 10 retries) did not persist: every later
`railway config plan` still shows it as a pending change (`null → ON_FAILURE`).
Until that clears, Railway's own default restart behaviour applies. Check the
service's restart settings in the dashboard before relying on the policy.

Every variable set in the dashboard must also appear in the file: a variable
set in the dashboard but missing from the file is deleted on the next apply.
Secrets and operator-specific values are declared `preserve()`
(`UA_AGENT_TOKENS`, `UA_OPERATOR_LABEL`, `UA_OPERATOR_NAME`).

## First boot

1. **Decide where tokens come from.**
   - **Recommended: inject them.** Set `UA_AGENT_TOKENS` as a *sealed*
     variable before the first deploy:
     `Desktop_Person:<operator token>,<agent label>:<agent token>,...`. The
     operator label is the one that signs in to the Control Center and
     approves. Generate each value locally with
     `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, and never
     paste one into a shared log. The definition marks the variable
     `preserve()`, so an apply never overwrites it.
   - **Or let the first boot generate them.** The log says where they are and
     does not show them. Read them with `railway ssh` and
     `cat /data/service.env`, then move them into a sealed `UA_AGENT_TOKENS`
     and redeploy.
2. **Generate a domain** for the service (Railway dashboard, or
   `railway domain`). The entrypoint picks up `RAILWAY_PUBLIC_DOMAIN` on the
   next start.
3. **Verify:**
   - `curl -s https://<domain>/healthz` returns `{"ok": true}` (liveness only:
     a process is listening).
   - `curl -s -o /dev/null -w '%{http_code}' https://<domain>/readyz` is `200`
     and the body has `"ok": true`. `/readyz` writes and reads the store on
     the volume and checks WAL. `503` means the database is missing, read-only,
     or not checkpointing. The `workos` field (`reachable`, `unreachable`, or
     `skipped`) never fails the probe. Its advisory network wait is bounded to
     one second, including DNS. One retained worker and a five-second completed
     result cache prevent stalled DNS from accumulating workers.
     The service definition's healthcheck
     path is `/readyz`.
   - An unauthenticated `POST /mcp` returns 401.
   - `railway ssh` then `/app/deploy/docker/entrypoint.sh unified-agent doctor`
     reports the deployment mode `authenticated_public` as LIVE.
   - Redeploy once: tasks and memory written before the redeploy are still
     there.

## Public ingress with WorkOS

Task #2114. On the public Railway origin a human signs in with WorkOS AuthKit
instead of a tailnet identity. One WorkOS organization; its members, mapped to
existing hub labels, are the only humans the hub accepts. WorkOS roles, groups
and email domains grant nothing.

### Variables

Set these on the service. The API key and cookie password are sealed
variables; the other values are not secret. `.railway/railway.ts` marks them
`preserve()` so an apply leaves them alone.

| Variable | Value |
|---|---|
| `UA_EDITION` | `enterprise` (the WorkOS leg is off in any other edition) |
| `UA_OPERATOR_LABEL` | Set the human operator's exact identity before first boot, e.g. `Desktop_Person` for this fleet. A fresh image defaults to `Desktop_Operator`; that does not make a WorkOS mapping to `Desktop_Person` the operator |
| `UA_OPERATOR_NAME` | The operator's human name, e.g. `Operator`; worker labels and aliases must remain distinct |
| `UA_HUB_WORKOS_ORGANIZATION_ID` | The one organization, `org_...` |
| `UA_HUB_WORKOS_IDENTITY_MAP` | JSON, WorkOS user id to an existing label: `{"user_...": "Desktop_Person"}`. An unmapped member cannot sign in |
| `WORKOS_CLIENT_ID` | The environment's client id, `client_...` |
| `WORKOS_API_KEY` | Sealed. The environment's secret key |
| `WORKOS_COOKIE_PASSWORD` | Sealed. A Fernet key: `python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`. It seals WorkOS sessions, keys the sign-in state and signs IdP producer stamps, so rotating it signs everyone out |
| `WORKOS_ISSUER` | `https://api.workos.com/user_management/<client_id>`, exactly. See [WorkOS pilot](../workos-pilot.md) for the two wrong-but-plausible values |
| `WORKOS_REDIRECT_URI` | `https://<domain>/oauth/approver/workos/callback`. The leg requires an HTTPS value; the approver and the Control Center build their own callbacks from the origin below |
| `UA_MCP_OAUTH` | `1`, for remote MCP clients and the approval pages |
| `UA_MCP_OAUTH_ISSUER` | `https://<domain>`. Also the origin of both WorkOS callbacks. Without it the hub falls back to `UA_PUBLIC_URL`, which the entrypoint sets from `RAILWAY_PUBLIC_DOMAIN`. Request Host headers are never used |
| `WORKOS_ORGANIZATION_ID` | Only the separate pilot app reads it. The hub ignores it |

The entrypoint already enforces auth (`UA_ENFORCE_AUTH=1`) and sets
`UA_PUBLIC_URL`. Do **not** add the Railway domain to `UA_HTTP_PUBLIC_HOSTS`:
that list serves only `/mcp` and `/oauth/*`, so the Control Center and its
sign-in would 404.

### Existing-store tenant preflight

The hosted organization also selects the tenant stamp used by every process
opening `/data/memory.db`. A new or consistent store is bound to that
organization and its schema generation; database triggers reject writes with
a missing or different tenant. Normal connection opens verify the binding.

Before upgrading an existing volume, inspect its tenant-bearing tables through
an authorized read-only connection and obtain a verified backup. If any domain
rows have a NULL or different tenant, startup refuses with an actionable
`Hosted store tenant transition refused` error. It never silently assigns
those rows or widens reads. Stop all writers and obtain operator-reviewed
ownership evidence before an offline migration; retain the original database
and WAL. Do not remove the durable binding to make startup pass.

`UA_TAILNET_PEER_MODE` is forced to `off` here. Railway's edge proxy connects
from `100.64.0.x`, inside the Tailscale CGNAT range, so `tailscale whois` must
never run on a hosted peer. Installing tailscaled in the container does not
make the public edge peer a verified device. Hosted mode also disables
`UA_MCP_OAUTH_PASSPHRASE_FALLBACK`, even if an old variable sets it to `1`:
public device and consent approvals require a verified WorkOS human.

### WorkOS dashboard

- **Redirect URIs**: `https://<domain>/oauth/approver/workos/callback` (OAuth
  consent and device approval) and `https://<domain>/api/session/workos/callback`
  (Control Center sign-in).
- **Sign-up off.** New accounts come only from an invitation to the
  organization.
- **Authentication methods**: Magic Auth and passkeys on. Password off unless
  you want it.
- **Organization membership**: invite each human to the one organization, then
  add their WorkOS user id to `UA_HUB_WORKOS_IDENTITY_MAP` and redeploy.
  Removing someone from the organization stops their sessions: every write is
  re-checked against WorkOS live, and reads at least once a minute.

### How a session works

- **Control Center.** The sign-in screen offers "Sign in with WorkOS" when
  `GET /api/session/options` says `{"workos": true}`. The login redirects to
  AuthKit for the one organization with a signed state and a PKCE challenge.
  The callback verifies the session and membership, maps the user to a label,
  and sets the normal `ua_ui` cookie. The WorkOS session itself stays on the
  hub, in `ui_session_workos`, sealed with `WORKOS_COOKIE_PASSWORD`. Requests on
  that cookie run as an IdP-verified human, so `approval_resolve` stamps
  `workos-idp-v1` and evidence shows `verified_via: idp`. Bearer sign-in is
  unchanged.
  Sign-out erases the sealed credential but retains its authentication-kind
  marker, so a concurrent request cannot fall back to bearer-style cookie
  authentication. A refresh cannot overwrite a sign-out marker.
- **Approval pages** (`/oauth/consent`, `/oauth/device`). "Sign in with WorkOS
  to approve" sets the `ua_oauth_workos` cookie on `/oauth` for an hour.
- **Refresh.** AuthKit access tokens last about five minutes. Both held
  sessions are refreshed through the SDK when the token is within 30 seconds of
  expiry, and the rotated session is stored or written back to the cookie. A
  session sent as `Authorization: WorkOS <sealed>` is never refreshed by the
  hub; its client owns it.

### Verification checklist

1. `GET https://<domain>/api/session/options` returns `{"workos": true}`.
2. `system_doctor`: `hub_auth` and `mcp_oauth` name the WorkOS leg as
   "configured (not verified live)". The doctor never contacts WorkOS, so only
   the sign-in in step 3 proves the API key and client id.
3. Open `https://<domain>/ui`, choose "Sign in with WorkOS", finish AuthKit.
   You land on `/ui` signed in as your mapped label.
4. Approve a pending approval from the Control Center. The approval row names
   your label, and its producer event has scheme `workos-idp-v1`.
5. Stay signed in for more than five minutes, then approve another. It still
   succeeds (the refresh path).
6. Sign in with an account that is not in the organization, or not in the map.
   The callback answers 403 and sets no cookie.
7. Remove a test member from the organization. Their next approval is refused,
   and their reads stop within a minute.
8. Bearer sign-in (`POST /api/session`) still works.

## Approval notifications on the hosted hub

`system_doctor`'s `approval_push` row reads the process environment when
`UA_HOSTED=1` or `RAILWAY_ENVIRONMENT_ID` is set. There is no systemd unit to
read, so an unset URL is `DORMANT`, not `UNKNOWN`. Set
`UA_APPROVAL_PUSH_URL` to an https topic and, when the topic requires it,
`UA_APPROVAL_PUSH_TOKEN`.

Client platforms that want the approval lifecycle as a callback set
`UA_APPROVAL_WEBHOOK_URL` and `UA_APPROVAL_WEBHOOK_SECRET`. The hub POSTs a
signed JSON body on approval requested and decided. The signature header is
`X-UA-Signature-256`. Neither value is written to the log.

Delivery is best effort: at most 32 notifications are pending, with two
delivery coordinators and two network workers. Duplicate delivery ids are
coalesced; overflow is dropped. A stalled network worker retains its slot
until it exits, even after the delivery deadline. Retries use the same signed
bytes. Consumers should deduplicate by delivery id and reconcile against the
hub's approval state after missed callbacks; callback arrival is not authority
to execute an approval.

## Enrolling a device

A hosted hub has no tailnet peer (`UA_TAILNET_PEER_MODE` is forced off), so a
device token cannot be bound to a Tailscale node. `unified-agent login` keeps
an Ed25519 key beside the token file (mode 0600) and sends the public key with
the device-flow request. The approval page shows the key's thumbprint; compare
it to the line login printed. The issued token's `device_node` is
`device-key:<thumbprint>`.

`unified-agent node` proves it holds that private key on register, heartbeat,
and claim. Copying the token onto a machine without the key is refused, and a
static bearer for the same agent label cannot register or drain the node. A
token from before this binding has no device-key claim: rerun
`unified-agent login` on the device, then register again.

Each signed request includes a unique request id. The hub atomically consumes
it after verifying the signature and request binding, and rejects replay even
within the freshness window. An ambiguous claim response must be reconciled
before intentionally submitting a new claim; the proof is not a cached response.

A hub that resolves a tailnet peer still binds the token to that peer. The
device key is not the binding then. Human approval on the public origin is the
WorkOS step above; passphrase approval stays disabled on a hosted hub.

## Offsite backups to Cloudflare R2

Task #2585, part of the hobby cloud setup (#2584, decided 2026-11-15). A
container has no systemd timer or launchd agent, and a Railway cron service
cannot mount this service's volume. When `UA_BACKUP_INTERVAL_S` is set, the
entrypoint therefore starts `unified-agent-backup-loop` beside the hub, as the
same unprivileged user. Each run takes the same integrity-checked snapshot as
the Mac's scheduled backup, keeps a few local generations on the volume, and
copies the snapshot to an S3-compatible bucket with one signed HTTPS PUT. The
upload needs no CLI. An offsite failure is logged in the run's JSON line and
never stops the hub or the schedule.

| Variable | Value on Railway |
|---|---|
| `UA_BACKUP_INTERVAL_S` | `86400` (daily; one hour is the floor) |
| `UA_BACKUP_S3_URI` | `s3://ua-hub-backups/hub` |
| `UA_BACKUP_S3_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` |
| `UA_BACKUP_S3_ACCESS_KEY_ID`, `UA_BACKUP_S3_SECRET_ACCESS_KEY` | Sealed. An R2 *Object Read & Write* token scoped to that one bucket |
| `UA_BACKUP_KEEP_DAILY`, `UA_BACKUP_KEEP_WEEKLY` | `2` and `0`. Hobby volumes stop at 5 GB, and the bucket keeps the history |

R2 charges nothing for egress, so a restore or a runaway listing cannot
repeat the September 2026 GCS egress bill. Put a lifecycle rule on the bucket
(for example, delete objects after 60 days), because the loop never deletes
anything remotely.

To check it: the deploy log shows `scheduled backups every 86400s`. After the
first delay (`UA_BACKUP_FIRST_DELAY_S`, 300 s by default), one JSON line reports
`"s3_upload": {"status": "ok", ...}`, and the object appears in the bucket.
Restore it into a scratch `UA_HOME` with `scripts/db_restore_rehearsal.py`
before trusting it; #2149 owns the restore proof.

## Not covered here

- **Vault authority and restore proof** (Phase 3). Database snapshots can go
  offsite (above), but neither they nor Railway volume backups are
  restore-tested yet (#2149).
- **Cut-over from the Mac hub** (Phase 5). The Mac stays authoritative until
  it happens.
- **The production host.** Since 2026-10-17 Railway is staging; the same image
  and env contract run on a GCP VM ([gcp-control-plane.md](gcp-control-plane.md)).
  Off Railway, `UA_STATE_MOUNT_PATH` gives the entrypoint the same
  refuse-without-persistent-state guard as `RAILWAY_VOLUME_MOUNT_PATH`.
