# J5 spike: Nango as the provider-OAuth catalog

Status: desk/source evaluation complete; live acceptance pending. Verdict:
**BUILD now, adopt later only if the conditions below are met**.
Filed 2026-09-24 by agent-claude. Hub task #1359. Parent design:
[Fleet Console and connector-style auth](2026-09-24-fleet-console-and-connector-auth.md#build-vs-adopt), J5.

## Verdict

**BUILD.** Add a small OAuth-client leg to `ConnectorManifest` and a hub-side
authorization-code + PKCE + refresh broker that stores refresh tokens as
tenant-scoped managed secrets. Do not adopt self-hosted Nango for J5.

The reasons, in order of weight:

1. **The free self-hosted edition is not meant for what we would ship.** Nango's
   docs call it "intended for hobby projects and evaluation". It covers Auth and
   Proxy only. It has no webhooks (so no refresh-failure notice), no audit trail
   and no RBAC. Production self-hosting is Enterprise-only: BYOC or Self-Managed
   under a fixed annual licence. The code is ELv2, which is source-available,
   not OSI open source.
2. **It would triple the footprint of the appliance.** We run one Python process
   and SQLite. Nango needs a Node server, Postgres 16 and Redis, all in Docker.
   The self-hosted image is built for `linux/amd64` only, so on the Apple
   Silicon Mac hub it would run under emulation.
3. **The catalog saves us little for the first two providers.** A GitHub OAuth
   App issues tokens that never expire, so there is nothing to refresh. Google
   is textbook RFC 6749 refresh plus two authorization parameters. Nango's
   catalog entries for both are about 25 lines of YAML each, and we would write
   the same few lines as manifest data.
4. **Nango would weaken the trust boundary.** It becomes a second credential
   store with its own master key, and that key cannot be rotated in the free
   edition. Its environment secret key can read every credential in the
   environment, because RBAC is paid. Tenant isolation would be tags the hub
   has to enforce anyway. The hub's evidence ledger must record every token use
   in either design, so Nango's logs add nothing we can rely on.

The estimated BUILD cost is about 650–750 lines of production code, 400–500
lines of tests, and 2–3 days. The time goes into the Control Center pane, not
the OAuth.

**Method and its limits.** This is a desk and source review, not a live
standup. There is no Docker on the desktop host, and creating GitHub/Google
OAuth apps and handling their client secrets is the operator-only (red tier). So
no provider consent, callback or refresh was exercised against Nango or
against a hub prototype. Nango claims below cite
[NangoHQ/nango](https://github.com/NangoHQ/nango) at commit
`5ee0e11e868feb1480a3e46c19ec52d1fb2a780d` (2026-09-24) as `path:line`, or its
docs at nango.dev. Hub claims cite this repo at `origin/main` `cd8d54fb`. The
checks a live verification must run are at the end.

## 1. Licence and edition limits

| Question | Finding | Source |
|---|---|---|
| Licence | Elastic License 2.0. It bars providing the software "as a hosted or managed service" that exposes "any substantial set of" its features. It also bars circumventing "license key functionality". | `LICENSE:1`, `LICENSE:9-12` |
| What free self-hosted includes | API Auth and Proxy: yes. Syncs, tool calls, webhooks, triggers, MCP server, RBAC, MFA, OpenTelemetry export, audit trail: no. Observability covers auth and proxy only. | `docs/guides/platform/free-self-hosting.mdx:7-28`; `docs/guides/platform/self-hosting/self-hosting.mdx:110-126` |
| Intended use of free self-hosted | "Hobby projects and evaluation"; "you run, configure, secure, scale, and update the instance yourself". | `free-self-hosting.mdx:7-9` |
| Production self-hosting | BYOC or Self-Managed (Helm), both "require an Enterprise plan": "a fixed annual license and maintenance fee, plus a fraction of the Cloud usage-based fees". | `self-hosting.mdx:7-28` |
| How features are gated | Mostly at build time. `Dockerfile.self_hosted` deletes `packages/jobs`, `packages/runner` and `packages/persist` from the base image, which removes the whole functions/syncs plane. The rest are runtime flags keyed on `NANGO_CLOUD` / `NANGO_ENTERPRISE`: `flagHasScripts` is false for a Docker "hosted" instance, and plan limits apply only when `FLAG_PLAN_ENABLED=true`. | `Dockerfile.self_hosted:7-10`; `packages/utils/lib/environment/detection.ts:11-16,30,37`; `packages/shared/lib/services/plans/plans.ts:94` |

**Fit for a pilot appliance.** Running Nango inside the hub we ship is
probably allowed under ELv2: the pilot uses our product, not Nango's features
as a service. That reading needs a lawyer before we rely on it. The bigger
problems are support and features. Our use would be production credential
handling on an edition Nango positions as evaluation-only. Refresh-failure
webhooks and the audit trail, which a governed product needs, are paid.
Adopting properly means an Enterprise contract per deployment or a negotiated
OEM licence. That is a commercial decision for the operator, not something a
self-hosted install can settle.

The parent design now correctly describes Nango as source-available (ELv2)
and links here.

## 2. Footprint

| | Nango, free self-hosted | Hub today |
|---|---|---|
| Processes | `nango-server` (Node; also serves Connect UI via `serve` in the same container) | one Python process (LaunchAgent) |
| Datastores | Postgres 16 (`nango-db`), Redis 7 (`nango-redis`); Elasticsearch/OpenSearch optional for logs | SQLite |
| Packaging | Docker Compose, image `nangohq/nango-server:hosted`, built `--platform linux/amd64` only | `uv` venv |
| Production sizing (Enterprise, full plane) | 5 Node services at 1 CPU / 2 GB each; Postgres 2 CPU / 8 GB / 128 GB; Redis 128 MB; ES 2 vCPU / 1 GB | all within an 8 GB Mac |

Sources: `docker-compose.yaml:3-68` (`platform: linux/amd64` at `:22`),
`packages/server/entrypoint.sh`, `scripts/build_docker_self_hosted.sh:26-36`,
`self-hosting.mdx:36-66`.

The auth-only edition should need far less than the Enterprise sizing, since
it drops jobs, runner and persist. As a guess, the server, Postgres and Redis
would idle at around 0.5–1 GB. That figure is **not measured**, and the live
check should measure it. Even so, it adds Docker, Postgres and Redis as
standing dependencies to a host that has none of them. On Apple Silicon the
amd64-only image runs under Rosetta/QEMU emulation. The compose file's bundled
Postgres and Redis are, by Nango's own docs, "not appropriate for production"
(`free-self-hosting.mdx:116`).

## 3. Token storage and tenancy

**Encryption.** Nango encrypts stored connection credentials with AES-256-GCM,
using a random 12-byte IV and an auth tag per record
(`packages/utils/lib/encryption.ts:18,53-58`,
`packages/shared/lib/utils/encryption.manager.ts:41-55`). The key is
`NANGO_ENCRYPTION_KEY`, or a KMS-wrapped `NANGO_ENCRYPTION_KEY_WRAPPED` for AWS
or GCP KMS (`packages/kms/lib/registry.ts:22-33`). Two caveats:

- Encryption is **off if the key is unset**. `shouldEncrypt()` returns false and
  credentials are stored in plaintext (`encryption.manager.ts:21-23,41-44`).
- The key **cannot be rotated**: "Rotation of NANGO_ENCRYPTION_KEY is not
  supported." (`encryption.manager.ts:214`; docs:
  `free-self-hosting.mdx:81-83`).

**Tenancy.** A connection is keyed by (environment, integration key,
`connection_id`). `connection_id` is a random UUID. Attribution comes from
free-form **tags** such as `organization_id` and `end_user_id`, set on the
Connect session (`packages/server/lib/controllers/connect/postSessions.ts:18-54`;
`docs/guides/auth/connection-tags-configuration-metadata.mdx:15-31`). Tags are
labels, not an access boundary. Any holder of the environment secret key can
read any connection in the environment, and scoped keys and roles are part of
paid RBAC. The only hard boundary is an **environment**. Its cap is lifted when
plans are off (`plans.ts:94`), so one environment per org is possible, but each
needs its own integration config and secret key.

**Mapping onto managed secrets.** Managed secrets are keyed by `(org_id, name)`
(#1343; `src/unified_agent/managed_secrets.py:1-8`). With Nango, the managed
secret would hold only the Nango secret key, plus a non-secret
`(org_id, connector) → connection_id` map. Isolation would then rest on the hub
checking `org_id` before every Nango call. That is the same check BUILD makes
when it reads `(org_id, "github_refresh_token")` directly, but with a
cross-tenant master key sitting behind it.

**What BUILD must add.** Managed secrets are JSON files at mode `0600` with the
**value in plaintext** (`managed_secrets.py:189-201`). Nango encrypts at rest
and we do not. At-rest encryption or a keychain backend for managed secrets is
a precondition for putting long-lived refresh tokens there. FileVault on the
Mac only mitigates this.

## 4. Refresh

Nango refreshes **on demand** when credentials are fetched. It also runs a
**background cron** that refreshes stale connections, so a rarely used token is
not revoked for inactivity:

- On demand: `refreshCredentialsIfNeeded` refreshes when the token is within
  `REFRESH_MARGIN_MS` = 15 min of expiry. Providers can override this with
  `token_expiration_buffer`. A Redis lock (`lock:refresh:<env>:<integration>:<connection>`)
  single-flights concurrent refreshes
  (`packages/shared/lib/services/connections/credentials/refresh.ts:366,445-457,587-602`;
  `packages/shared/lib/services/connections/utils.ts:11-12`). The code comment
  notes this "is not a distributed lock".
- Cron: every `CRON_REFRESH_CONNECTIONS_EVERY_MIN` (default 10), up to
  `CRON_REFRESH_CONNECTIONS_LIMIT` (default 100) connections not refreshed in
  a day (`packages/server/lib/crons/refreshConnections.ts:19-28,67`;
  `packages/utils/lib/environment/parse.ts:296-297`). Docs: "refreshes each
  access token at least once every 24 hours"
  (`docs/guides/auth/token-refreshing.mdx:9-11`).
- None of this needs a restart. The hub would call
  `GET /connection/{id}?provider_config_key=…` for each use, or cache the token
  until the `expires_at` it returns. `force_refresh=true` forces a refresh, and
  `refresh_token=true` also returns the refresh token; both need the
  `read_credentials` permission (`packages/server/lib/controllers/connection/connectionId/getConnection.ts:15-21,60-70`).
- Refresh-failure and recovery notices go out as **webhooks**
  (`token-refreshing.mdx:20-22`), which the free edition lists as unsupported
  (`free-self-hosting.mdx:22`). The server source still contains the auth-webhook
  send path (`packages/server/lib/hooks/hooks.ts:133,179,223,313`). Whether it
  fires on a hosted instance is unverified, so the live check must test it.
  Without it, the hub learns of a revoked grant only when a fetch fails. BUILD
  learns it the same way.

## 5. Integration shape and evidence

**Agents never see the token.** This holds in both designs, and for the same
reason: only the hub holds the credential that reaches the token. The
credential is the Nango secret key in ADOPT, or the managed-secret refresh
token in BUILD. Agents call hub tools, and the hub either calls the provider
itself or injects the token into a hub-side request. Nango's **Proxy**, which
the free edition includes, would let even the hub avoid handling access tokens,
at the cost of routing every provider call through Nango. Our J5/J7 design
already has the hub make provider calls, so the proxy saves nothing.

**Evidence.** Nango's free edition has no audit trail and logs auth and proxy
events to stdout only. Elasticsearch is needed for its logs UI
(`free-self-hosting.mdx:170-192`). So in ADOPT the evidence record still has to
come from the hub: one `record_event` per token mint or use
(`src/unified_agent/events.py:187`, which raises on a failed append so a gate
can fail closed). That record carries actor, device, org, connector, scopes,
the action it served, and a digest, never the value. BUILD writes the same
event in the same place. ADOPT adds a second, unsigned log in another system
that an auditor would have to reconcile.

## 6. Catalog value for GitHub + Google vs BUILD cost

**What the catalog encodes for our two providers** (`packages/providers/providers.yaml`):

- `github` (`:9781-9806`) is a GitHub OAuth App: authorize URL, token URL,
  proxy retry and pagination hints. OAuth App user tokens do not expire and
  carry no refresh token, so "refresh handled hub-side" is a no-op for them.
  Refresh applies only to a GitHub **App**'s expiring user tokens, which Nango
  models separately as `github-app-oauth` (`:9836`). GitHub supports PKCE (S256)
  for both since 2025-07-14
  ([changelog](https://github.blog/changelog/2025-07-14-pkce-support-for-oauth-and-github-app-authentication/);
  [authorizing OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)).
  GitHub's token endpoint returns form-encoded data unless the request sends
  `Accept: application/json`.
- `google` (`:10305-10330`): authorize and token URLs plus
  `access_type=offline` and `prompt=consent`, needed to get a refresh token. Its
  product aliases, such as `google-calendar`, add only scopes and a base URL.
- Nango sends PKCE unless a provider sets `disable_pkce`
  (`packages/server/lib/controllers/oauth.controller.ts:734,1807`).

For these two providers the catalog amounts to about ten facts, and we would
keep them as manifest data. The catalog pays for itself only across a long
tail of quirky providers: 1,000+ entries, with assertion/JWT grants, per-tenant
URLs, and rotating refresh tokens.

**What the hub already has** (from `grep -rn oauth src/`):

- The authorization-server side: `oauth.py` (RFC 8414/7591/9728, PKCE
  verification, device grant) and `oauth_store.py`, which persists client and
  token digests in SQLite and never stores plaintext.
- An outbound OAuth **client** pattern: `pilot_auth.py`, with a single-use
  `state` table carrying a 10-minute expiry (`:267-300`), and WorkOS code
  exchange.
- `httpx` and `cryptography` are already dependencies (`pyproject.toml:12,32`).
- Tenant-scoped, write-only secret storage (`managed_secrets.py`) and the
  append-only event log (`events.py`).
- Existing gap: the hub has no outbound token-endpoint client and no refresh
  broker.

**BUILD estimate:**

| Piece | Prod LOC | Notes |
|---|---|---|
| `ConnectorManifest.oauth` leg: `authorize_url`, `token_url`, `scopes`, `pkce`, `extra_authorize_params`, `token_accept_json`, `refresh_supported` + validation | 60–80 | https-only URLs; data for GitHub and Google |
| Flow module: begin (state + S256 verifier, pending row with 10-min TTL, single use) and callback (state check, code exchange via `httpx`, store the refresh or long-lived token as `(org_id, "<connector>_oauth")`) | 180–220 | mirrors `pilot_auth` state handling |
| Token broker: `access_token_for(org_id, connector, purpose)`, in-memory access-token cache, 5-min refresh margin, per-key lock, refresh-token rotation, `invalid_grant` → connector `needs_reconnect` | 150–180 | one `record_event` per mint/use |
| Managed-secret at-rest encryption (or macOS Keychain backend) | 80–120 | precondition above; useful beyond J5 |
| Control Center Connectors pane: Connect / Reconnect / Disconnect, status | 150–200 | red-tier consent copy |
| **Total** | **~650–750** | plus 400–500 test LOC, 2–3 days |

The OAuth mechanics themselves come to about 400 lines. The ADOPT column is not
zero: a Nango client wrapper, the connection-map store, Connect UI embedding or
session minting, Docker/Postgres/Redis lifecycle in the hub's doctor and
install paths, and backups of a second datastore come to an estimated 300–400
lines plus permanent operational weight.

## Conditions that would flip the verdict to ADOPT

1. **The provider list grows past about 8–10**, with non-standard flows:
   Salesforce instance URLs, Microsoft tenant endpoints, Atlassian rotating
   refresh tokens, JWT-bearer or assertion grants, per-customer subdomains. The
   catalog is then worth the footprint.
2. **The operator signs an Enterprise (Self-Managed/BYOC) or OEM agreement.** That
   brings webhooks, the audit trail, RBAC-scoped keys and support. Or a pilot
   customer already runs Nango, Postgres or Kubernetes and wants it reused.
3. **Syncs, tool calls or the Nango MCP server become a product need.** These
   are paid-only. Buy them if so; the free edition has none of them.
4. **Nango ships an arm64 self-hosted image and an SQLite-class footprint.**
   That removes the appliance objection, though not the licence one.

The same reasoning applies to a hosted broker. The hub already depends on
WorkOS, and **WorkOS Pipes** offers hosted connected-app OAuth with token
storage, automatic refresh, and a relay mode where the token never enters our
environment ([docs](https://workos.com/docs/pipes)). It is not self-hostable,
so it does not fit the private appliance. It is the better ADOPT candidate for
a cloud or enterprise edition than self-hosted Nango, and deserves its own
spike if condition 1 or 2 arrives.

## Live verification checklist (once the operator creates the OAuth apps)

For the BUILD implementation (J5), and repeatable against Nango if a flip
condition arrives:

1. **GitHub OAuth App, PKCE.** Connect from the Control Center, then check:
   the authorize request carries `code_challenge_method=S256`; the callback
   rejects a replayed or expired `state`; the token is stored under
   `(org_id, github_oauth)` and appears in no log, event payload, tool result
   or `ps` argv.
2. **Google, offline access.** Consent returns a `refresh_token`
   (`access_type=offline`, `prompt=consent`). Force expiry by clearing the
   cache or waiting an hour; the next use refreshes without a hub restart; a
   rotated refresh token is persisted and the old digest is replaced.
3. **Revocation.** Revoke each grant in its provider account settings. For
   Google, the refresh attempt's `invalid_grant` marks the connector
   `needs_reconnect`. GitHub OAuth App tokens do not expire or refresh: the
   broker must classify the provider API's invalid-credential response on
   the next use and mark that connector `needs_reconnect`. In both cases,
   record an evidence event and avoid a retry loop. Test permission and
   rate-limit errors separately so they do not trigger reconnect.
4. **Tenancy.** A second `org_id` cannot read or use the first org's
   connection, whether through the tool, the web UI or the org-agnostic
   read (#1343 fail-closed).
5. **Evidence.** Each token use writes one event naming actor, device,
   `org_id`, connector, scopes and purpose, with a digest and no value. The
   evidence report renders it.
6. **Agent isolation.** No MCP tool, `fs_*`, `run_cmd` or export path returns
   the token. Grep a full session transcript and the event log for the token's
   prefix and find nothing.
7. **If Nango is ever stood up:** also measure idle RSS of server, Postgres and
   Redis on the Mac under Rosetta; confirm whether auth webhooks fire on the
   hosted image; confirm the instance refuses to start with plaintext storage
   by setting `NANGO_ENCRYPTION_KEY` before the first connection; confirm the
   cron refresh at 10-minute cadence.
