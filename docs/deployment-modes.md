# Deployment Modes & Instance Claim Flow

This document defines the canonical deployment mode architecture and claim flow for Unified Agent instances (task #1422, Parity 17).

---

## 1. Canonical Deployment Modes

Unified Agent defines three mutually exclusive canonical deployment modes configured via `UA_DEPLOYMENT_MODE`:

| Mode | Environment / Ingress | Authentication Posture | Typical Use Case |
|---|---|---|---|
| `local_trusted` | Localhost loopback / open stdio | Unforced (`UA_ENFORCE_AUTH=0` or `UA_LEGACY_OPEN_POSTURE=1`) | Single-workstation developer loop, initial bootstrap before claiming |
| `authenticated_private` | Private tailnet (`100.x.y.z`) / private VPC | Enforced (`UA_ENFORCE_AUTH=1`) | Team hub, multi-agent fleet on private network |
| `authenticated_public` | Public origin / reverse proxy / Internet-facing | Enforced (`UA_ENFORCE_AUTH=1`) + Credentials required | Public appliance, external on-ramps, Internet ingress |

Aliases such as `local-trusted`, `authenticated-private`, `authenticated+private`, `authenticated-public`, `authenticated+public`, `private`, and `public` are accepted and normalized to canonical tokens.

---

## 2. Resolution Precedence

When `UA_DEPLOYMENT_MODE` is evaluated by `config.deployment_mode()`:

1. **Explicit Setting**: If `UA_DEPLOYMENT_MODE` is set in the environment:
   - Must match one of the canonical modes or accepted aliases.
   - Any unknown or invalid value fails closed with `DeploymentModeError`.
2. **Inferred Default (Unset)**:
   - If authentication is **not enforced** (`enforce_auth() == False`, e.g. `UA_LEGACY_OPEN_POSTURE=1` or `UA_ENFORCE_AUTH=0`): resolved as `local_trusted`.
   - If authentication is **enforced** (`enforce_auth() == True`):
     - If public ingress is indicated (`UA_AUTHENTICATED_PUBLIC=1`, `UA_PUBLIC_URL` set, or `UA_HTTP_PUBLIC_HOSTS` configured): resolved as `authenticated_public`.
     - Otherwise: resolved as `authenticated_private`.

The helper `config.deployment_mode_from_settings(mode_raw, enforce_raw, transport_raw, legacy_raw, public_raw, public_url_raw, public_hosts_raw)` performs this resolution statelessly without ambient environment reads.

---

## 3. Fail-Closed Validation & Mismatch Guards

To prevent accidental public exposure without credentials or invalid configuration:

- `authenticated_public`: Refuses startup and doctor checks fail if `enforce_auth()` is false or if authentication credentials cannot be resolved.
- `authenticated_private`: Refuses unauthenticated access or open legacy posture.
- `local_trusted`: Refuses public internet ingress or public tunnel endpoints.

Validation is enforced by `config.validate_deployment_mode()` and verified continuously by `system_doctor()`.

---

## 4. Doctor Verification

`system_doctor()` runs the `deployment_mode` capability check:

- **LIVE**: Mode is valid, consistent with transport and authentication settings (e.g. `canonical mode 'authenticated_private' (UA_DEPLOYMENT_MODE)`).
- **MISCONFIGURED**: Fatal configuration mismatch (e.g. public mode with authentication unforced).

---

## 5. Instance Claim Flow (`deployment_claim`)

A fresh instance can provision its first credential in `local_trusted` mode.
Claiming records that credential; switching to enforced authentication is a
separate operator activation step.

1. **Prepare the instance and caller:**
   - Keep the instance in `local_trusted` mode, reachable only through its trusted local ingress.
   - Enable the managed-secret sink with `UA_MANAGED_SECRETS=1` in the hub's configuration before calling the tool.
   - The current tenant must have zero active token lifecycle rows, and the target label must name a recognized fleet identity. An existing `agent-token:<label>` managed secret is never overwritten.
   - Use an administrative tool surface whose operational profile exposes `deployment_claim`; the normal policy and agent-access gates still apply. The tool is pruned from the default developer profile.
2. **Request the claim:** an unverified caller must generate a unique, unpredictable `client_nonce` of at least 8 characters after trimming and keep it for the approval retry. For example, the Python tool signature is:

   ```python
   deployment_claim(
       agent_label="Desktop_Person",
       value="<secret-token>",
       client_nonce="<unique-random-nonce-at-least-8-characters>",
   )
   ```

   A verified identity does not require `client_nonce`. The forced
   `managed_secret_set` approval binds the claimant identity or label/nonce,
   credential digest, and tenant scope. A pending approval does not provision
   the credential; after human approval, retry the same claim parameters.
   Optional `ttl_days` must be a positive integer.
3. **Provision and activate:**
   - The approved call stores the secret under `agent-token:<label>` and records active token metadata plus issuance and `security.deployment.claimed` audit events. The response contains the token hash and metadata, never its raw value.
   - The managed credential is recognized by `config.agent_tokens()` for bearer authentication. A second claim with active token rows fails closed; use authenticated `agent_token_issue` for later issuance.
   - The response reports `claimed: true`, current `deployment_mode: local_trusted`, `next_mode: authenticated_private`, and an `activation_step`. It does not change the hub's authentication configuration.
   - Follow that activation step: set `UA_ENFORCE_AUTH=1`, update any explicit `UA_DEPLOYMENT_MODE` to `authenticated_private`, apply the hub configuration, and authenticate with the claimed bearer token. Verify the resulting mode with `system_doctor()`.

---

## 6. One Tenant Environment per Data Directory

The tenant a process stamps on the rows it writes comes from that process's
own environment: `edition.tenant_org_id()` reads `UA_EDITION` and
`WORKOS_ORGANIZATION_ID`, and only `enterprise` with a non-blank organization
id is tenanted. The hub reads and ends only rows stamped with its own tenant.
So **every process that opens the same data directory (`UA_HOME`) must carry
the hub's `UA_EDITION` and `WORKOS_ORGANIZATION_ID`**: the hub, the Active
Executor, the workers (Zulip, task-space, GitHub App), the cron and
LaunchAgent jobs (review pipeline, pr-watch, consolidate) and any CLI an
operator runs against the store. An executor started without
`WORKOS_ORGANIZATION_ID` against a tenanted store writes sessions and tasks
with a NULL `org_id`, which the hub can neither list nor end (task #1995).
Put both variables in the env file every unit sources (`service.env`,
`executor.env`), not in one unit's own `Environment=`.

`system_doctor()` checks this with the `tenant_consistency` row (task #1996).
Each process that opens the store writes a beacon,
`$UA_HOME/tenant-beacons/<pid>.json`, naming its process, edition and the
`org_id` it stamps (no other environment value), refreshed every 10 minutes
while it runs and pruned after 7 days. The row compares the beacons seen in
the last 24 hours with the tenant of the hub unit (its `UA_EDITION`, or the
`[env]` value in its `settings.toml` when the unit sets none, and its
`WORKOS_ORGANIZATION_ID`). A relative `UA_HOME` or `UA_SETTINGS_FILE` in the
unit is resolved against the unit's working directory; when that cannot be
read the row is `UNKNOWN`:

| Status | Meaning |
|---|---|
| `DEGRADED` | At least one process stamped a different tenant than the hub. The detail names each one (`<process> (pid N) stamps <org or no tenant>`). Fix that process's environment; its beacon ages out after 24 hours, or is rewritten at its next start. |
| `LIVE` | The hub is tenanted and every recent process stamped the hub's tenant. |
| `DORMANT` | Neither the hub nor any recent process is tenanted (the private hub today). |
| `UNKNOWN` | The hub is tenanted but no process recorded a beacon in the last 24 hours, or the beacons could not be read. |
