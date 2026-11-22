# Connector contract (task #1204, Epic G first child)

This page defines what "connector-shaped" means in this repo: the
`ConnectorManifest` every connector declares, how it is validated at Horizon 2
registration, the conformance harness every connector must pass, and how the
existing status surfaces (`system_doctor`, the Settings → Integrations page) read a
connector's state. Everything downstream in Epic G builds on this contract.

Code: [`src/unified_agent/connectors.py`](../src/unified_agent/connectors.py).
Harness: [`tests/connectors/`](../tests/connectors/). Reference implementation:
Slack (`slack_gateway.py`, `slack_outbound.py`, `slack_chat.py`). Zulip
(task #1268) is the second registered manifest. Real credentials are
owner-only `zuliprc` files the framework cannot name; `UA_ZULIP_SITE` is an
optional allowlist override with a runtime `DEFAULT_SITE` fallback, so it is
**not** listed in `required_secrets` (aligned 2026-09-24 / Devin F1 on PR
#700). Hub operators may still export `UA_ZULIP_SITE` explicitly to pin the
allowlist — see the rationale comment above `ZULIP_MANIFEST` in
`connectors.py`. The dedicated Zulip relay health check and the generic
connector row report different gates. GitHub App
(task #1264) is the third: it names every secret either of its two token
modes might read (`GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY` for the App path,
`UA_GITHUB_USER_TOKEN` for the operator-user-token path, task #1261) and
declares the union of both modes' capabilities, since a manifest is one
static declaration, not a snapshot of whichever `UA_GITHUB_TOKEN_MODE` is
active — see the rationale comment above `GITHUB_APP_MANIFEST` in
`connectors.py` and
[connector-activation.md](connector-activation.md#github-app-app-vs-operator-user-token-mode-task-1261)
for the two-mode setup guide.

GCP agent20 (`int-gcp-agent20`, task #1270) is also a manifest: it declares the
read-only inventory, bounded GCP operations, and worker recovery entry points.
It uses ambient `gcloud` credentials, so the manifest does not invent an
environment secret. Approval-gated `gcloud_ops` is authorization evidence only;
the connector's health is `LIVE` only when the read-only instances inventory is
available and `ua-worker` is `RUNNING`. Unreadable inventory (including
missing `compute.instances.list` permission), a missing worker, or a stopped
worker is `DEGRADED`. The lifecycle status remains separate: a healthy worker
without an active Horizon 2 registration is still `DORMANT` as a connector.

## The manifest

```python
@dataclass(frozen=True)
class ConnectorManifest:
    name: str  # identifier, e.g. "slack"
    version: str  # semver x.y.z
    capabilities: frozenset[str]  # what the connector's code actually does
    required_secrets: frozenset[str] = frozenset()  # env var NAMES only, never values
    entry_points: Mapping[str, str] = {}  # capability -> "module.path:callable"
    default_enabled: bool = False  # a manifest is never itself a live grant
    horizon2_registration_supported: bool = True  # False when no Horizon 2 transport exists
```

Validation happens once, in `ConnectorManifest.__post_init__`, so a malformed
manifest cannot be constructed at all:

- `name` matches the same identifier shape used elsewhere in Horizon 2
  (`^[a-z][a-z0-9_.-]{0,63}$`).
- `version` is `x.y.z`.
- `capabilities` is non-empty; each entry is a valid identifier.
- `required_secrets` entries are `UPPER_SNAKE_CASE` — an environment variable
  **name**. This module never accepts, stores, or logs a secret's value.
- `entry_points` covers every declared capability exactly once, and each value
  is a resolvable-looking `module.path:callable` string.
- `default_enabled` is a bool. It defaults to `False`: declaring a manifest
  never turns a connector on.
- `horizon2_registration_supported` is a bool. It defaults to `True`; set it
  to `False` for manifest-only contracts that have no Horizon 2 transport or
  authenticated consumer. Registration and authorization are then refused
  before consuming approval, and doctor plus the Integrations page keep even
  legacy active rows DORMANT. The serialized manifest includes this field only
  when it is `False`.

## Registration validates the manifest

`horizon2_server_register` (`src/unified_agent/tools/horizon2.py`) looks up
`connectors.get_manifest(connector)`. If a manifest is registered for that
connector name, registration calls
`connectors.validate_entry_points_resolve(manifest)` before writing the
`governed_servers` row — a manifest whose entry points do not import, or whose
functions have been renamed or deleted, is **refused with a clear error**
naming every failing capability. `horizon2_package_register` delegates to
`horizon2_server_register`, so both registration paths get this for free.

This deliberately does **not** validate the registration's `scopes` argument
against `manifest.capabilities`. Those are two different axes of the same
connector and conflating them would force fabricating capabilities a
connector does not have — see "Capabilities vs. scopes" below.

## Capabilities vs. Horizon 2 scopes

`horizon2.CONNECTOR_PACKAGES` is a pre-existing, separate catalog:
`{"slack": {"read", "search"}, "drive": {"read"}, ...}`. Those are
**approval-scoped data-access grants** for a live MCP connection — what an
authorized caller may fetch through a governed server. A `ConnectorManifest`'s
`capabilities` describe something else: **what the connector's own code
actually does**.

For Slack, these are genuinely different lists, and the manifest reports the
real one:

```python
SLACK_MANIFEST = ConnectorManifest(
    name="slack",
    version="1.0.0",
    capabilities=frozenset({"ingress", "outbound", "chat"}),
    required_secrets=frozenset({"SLACK_SIGNING_SECRET", "SLACK_BOT_TOKEN"}),
    entry_points={
        "ingress": "unified_agent.slack_gateway:handle_http_request",
        "outbound": "unified_agent.slack_outbound:send_message",
        "chat": "unified_agent.slack_chat:maybe_reply",
    },
    default_enabled=False,
)
```

Slack has no `search` capability in this checkout — nothing in
`slack_gateway.py`, `slack_outbound.py`, or `slack_chat.py` implements one
(see [`docs/connector-activation.md`](connector-activation.md)). Forcing the
manifest's capabilities to equal `{"read", "search"}` to match the Horizon 2
package catalog would describe an idealized connector, not the real one. This
is a genuine, reported seam rather than something this task papers over: a
future pass could either retire `search` from Slack's package scopes or wire
an actual Slack search consumer; until then, the two lists intentionally
disagree, and both are visible in `horizon2_package_list()`'s output
(`scopes` from the package catalog, `manifest.capabilities` from the
manifest).

## The five conformance clauses

`tests/connectors/harness.py` provides `assert_connector_conforms(manifest)`,
run against every connector with a registered manifest. Each clause uses a
temporary SQLite database so reusable connector tests cannot write rows or
hash-chain events into their caller's database. It asserts:

1. **Registers.** `connectors.register_manifest` (idempotent registry) plus
   `horizon2_server_register`/`horizon2_package_register` accept a
   well-formed manifest and refuse one whose entry points do not resolve
   (constructed with a monkeypatched bad entry point in the harness test, so
   the "refused with a clear error" path is exercised without touching a real
   connector's code).
2. **Reports state to `system_doctor` and the Integrations page.** Both read the
   same registry: `doctor._connectors_check()` and
   `webui.api.connectors_payload()` (via `horizon2_package_list()`) enrich
   their output with `manifest.to_dict()` and `connectors.secret_status()`.
   The harness asserts every registered manifest name appears in
   `system_doctor()`'s checks as `connector:<name>` and in
   `horizon2_package_list()`'s `manifest`/`secrets` fields. Manifest-only
   connectors are included with empty Horizon-2 `scopes`.
3. **Refuses to start without its named secrets.** Two layers, both asserted:
   - Generic: `connectors.ensure_can_start(manifest)` raises
     `ConnectorSecretsMissingError` (a `PermissionError`) when any
     `required_secrets` name is absent from the environment, and never
     inspects or reports a value — only names.
   - Connector-specific, for Slack: `SlackConfig.validate()`
     (`slack_gateway.py`) raises `SlackGatewayError("SLACK_SIGNING_SECRET is
     required")` when unset, and `send_message()` (`slack_outbound.py`)
     raises `SlackOutboundError("SLACK_BOT_TOKEN is required for outbound
     Slack")` when unset. Slack already enforced this before this task; the
     harness proves the manifest's `required_secrets` names exactly the
     secrets its real code already gates on, rather than inventing new
     runtime enforcement.
4. **Emits hash-chained lifecycle events.** `horizon2_server_register` and
   `horizon2_server_revoke` already call `events.record_event(...)`, which
   chains every row's `entry_hash` to the previous row's hash
   (`events.compute_entry_hash`, gated on the `audit_hash_chain` edition
   feature — on by default; see `edition.py`). The harness registers a
   connector and asserts the resulting event's `entry_hash`/`prev_hash` link
   into the existing chain (`event_log_verify` / `events.verify_chain`),
   rather than adding a second, parallel event path.
5. **Is invisible to profiles that lack it.** Connector administration (the
   `horizon2_*` tools) is reachable to `developer`/`steward` (deny-list
   profiles that do not name them) and absent from `research`, `operator`,
   and `public`'s allow-lists in `profiles.py`
   (`RESEARCH_ALLOWED_TOOLS`/`OPERATOR_ALLOWED_TOOLS`/`PUBLIC_ALLOWED_TOOLS`).
   The harness asserts `profiles.tool_is_allowed_for(profile, "horizon2_server_register")`
   is `False` for every profile that does not name it and `True` for
   `developer`/`steward` — reusing the existing exposure mechanism rather than
   inventing a connector-specific one.

## system_doctor and the Integrations page

Both now read live connector state from the same two sources:

- `system_doctor()` calls `doctor._connectors_check()`, which iterates
  `connectors.all_manifests()` and reports one `connector:<name>` row per
  manifest: `LIVE` (entry points resolve, secrets present, an active
  `governed_servers` row exists), `MISCONFIGURED` (entry points broken, or
  secrets missing while a registration is active), `UNKNOWN` (an active
  registration exists but the hub service environment cannot be read and the
  doctor process lacks a required secret), or `DORMANT` (no active
  registration). For GCP, the read-only runtime probe runs only after active
  registration opts the integration in; it adds `DEGRADED` when inventory
  cannot confirm a running worker. `doctor` exits non-zero for degraded health,
  and the daily digest reports it as degraded.
- The Integrations page (`/ui/settings/integrations`, `GET /api/connectors` -> `webui.api.connectors_payload`
  -> `horizon2_package_list()`) now includes every registered manifest, even
  when it is not in `CONNECTOR_PACKAGES`, with empty `scopes`. Each manifest
  row carries a `manifest` object (name/version/capabilities/
  required_secrets/entry_points/default_enabled) and a `secrets` object
  (`present`/`missing`/`satisfied` — names only). `packages/web/src/app/integrations/ConnectorsPage.tsx`
  shows the manifest version and capabilities, and whether
  required secrets are present. GCP runtime health reuses the cached cloud
  inventory and exposes its checked time/age, avoiding a new `gcloud` process
  on each page request.

Neither surface invents state: both are read-only projections of
`connectors.all_manifests()` plus the existing `governed_servers` table.

## Adding a new connector

1. Write the connector's real code first — its ingress/outbound/whatever
   entry points, and whatever env vars it needs to run.
2. In `src/unified_agent/connectors.py` (or the connector's own module, then
   call `connectors.register_manifest(...)` at import time), define a
   `ConnectorManifest`: `name`, `version`, `capabilities` describing what the
   code *actually* does (not an aspirational list), `required_secrets` naming
   every environment variable the connector's own code already refuses to run
   without, `entry_points` mapping each capability to its real
   `module.path:callable`, and leave `default_enabled=False`.
3. Add the connector to `horizon2.CONNECTOR_PACKAGES` only if it should also
   be reachable through the generic Horizon 2 MCP scope catalog — this is
   optional and orthogonal to the manifest (see "Capabilities vs. scopes").
4. Run the conformance harness against the new manifest:
   `uv run pytest tests/connectors -q`. A failing entry point, an
   undeclared/unused capability, or a required secret your code does not
   actually check will fail loudly with the offending detail.
5. `system_doctor` and the Integrations page pick up the new connector
   automatically — no additional wiring required, because both read
   `connectors.all_manifests()` and `governed_servers` directly.
6. If your PR touches connector manifests, registry, doctor, connector
   modules (`connectors/`, `slack_*`, `github_*`, `*_connector`, `*_gateway`),
   the harness, or docs, the `Connector conformance harness` step of CI's
   `gates` job (`.github/workflows/ci.yml`) runs the harness whenever that
   job classifies the change as connector-affecting and `full` (a merge-queue
   `ALL`, a dispatch, or the nightly; see [ci-batch-verify.md](ci-batch-verify.md)).
   The filter is scoped to connector-affecting paths so unrelated backend
   changes skip the step, while matching pattern conventions for future
   connector additions.

## What is not yet a connector

Drive, Obsidian, Readwise, and Granola remain entries in
`horizon2.CONNECTOR_PACKAGES` with no registered `ConnectorManifest` and no
verified live consumer in this checkout (unchanged from
`docs/connector-activation.md`). They are dormant packaging, not connectors
by this contract, until each gets a real manifest describing real code.

## GitHub App: a registered manifest with two token modes (task #1264)

`github-app` has real, dedicated code (`github_app.py`, task #1201, gated off
by `UA_GITHUB_APP_ENABLED`) and, as of task #1264, a real registered
`GITHUB_APP_MANIFEST` (`connectors.py`) -- it appears in `system_doctor`'s
`connector:github-app` check, the Integrations page
(`horizon2_package_list()`'s `github-app` row now carries `manifest` and
`secrets`, alongside its pre-existing `CONNECTOR_PACKAGES` `scopes`), and the
conformance harness (`tests/connectors/test_github_app_conformance.py`).

The registered manifest declares the **union** of what both token modes do,
because a `ConnectorManifest` is one static declaration, not a snapshot of
whichever `UA_GITHUB_TOKEN_MODE` happens to be active:

```python
GITHUB_APP_MANIFEST = ConnectorManifest(
    name="github-app",
    version="1.0.0",
    capabilities=frozenset({"poll", "check_runs", "commit_statuses", "evidence_comments"}),
    required_secret_alternatives=(
        frozenset({"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"}),
        frozenset({"UA_GITHUB_USER_TOKEN"}),
    ),
    secret_status_hook="unified_agent.github_app:manifest_secret_status",
    entry_points={
        "poll": "unified_agent.pr_watch:run",
        "check_runs": "unified_agent.github_app:GitHubAppHttpTransport",
        "commit_statuses": "unified_agent.github_app:GitHubUserTokenTransport",
        "evidence_comments": "unified_agent.github_app:process_pr_event",
    },
    default_enabled=False,
)
```

`required_secret_alternatives` documents the two credential sets (an OR of
ANDs: either whole group, never both). Readiness itself is **mode-aware**:
`secret_status_hook` points `connectors.secret_status()` (and so
`system_doctor` and the Integrations page) at
`github_app.manifest_secret_status`, which counts only the active
`UA_GITHUB_TOKEN_MODE`'s credentials — App mode needs `GITHUB_APP_ID` and
`GITHUB_APP_PRIVATE_KEY`; user mode needs the var named by
`UA_GITHUB_USER_TOKEN_VAR` (default `UA_GITHUB_USER_TOKEN`). The `gh auth
token` fallback is not probed by a status read, so a user-mode host relying
on it reads as missing its token var. App-mode credentials satisfy the named
secret check, but the status is UNKNOWN until a caller supplies the
installation-token provider required by `build_transport`. Manifests that don't use these two
optional fields (Slack, Zulip) serialize exactly as before.
`github_app.GITHUB_APP_MANIFEST` retains its earlier mapping shape, and
`github_app.github_app_manifest()` still returns the narrower, exact
per-mode dict every existing caller of that function expects.
Setup for both modes,
and the full env var reference, live in
[connector-activation.md](connector-activation.md#github-app-app-vs-operator-user-token-mode-task-1261).

The connector stays off by default at two independent layers regardless of
this manifest: `github_app.enabled()` gates on `UA_GITHUB_APP_ENABLED`
(unset/`"0"` by default -- also the rollback switch), and
`default_enabled=False` on the manifest itself. Registering the manifest, or
even having every named secret present, never turns the connector on by
itself.

## Discord: a manifest over the existing task-space worker (task #1269)

Discord's real code is not new and is not TypeScript: it has lived in
`unified_agent.ts_worker` since task #770 (`ts` is task-space, this
checkout's name for the Discord/Telegram chat-ingestion worker -- there is
no separate TypeScript Discord implementation here). It is pure outbound
REST over `httpx` (`GET`/`POST /channels/{id}/messages` polling with a
per-channel cursor, no websocket, no gateway intents, no public ingress --
see `ts_worker/transport.py`'s module docstring) and has been gated off by
the absence of `UA_TS_DISCORD_TOKEN` the whole time. Task #1269 only adds
the missing piece: a registered `DISCORD_MANIFEST` so that existing code is
visible the same way as Slack/Zulip/github-app.

```python
DISCORD_MANIFEST = ConnectorManifest(
    name="discord",
    version="1.0.0",
    capabilities=frozenset({"gateway", "worker"}),
    required_secrets=frozenset(
        {"UA_TS_DISCORD_TOKEN", "UA_TS_DISCORD_ALLOWED_USER_IDS", "UA_TS_DISCORD_CHANNELS"}
    ),
    entry_points={
        "gateway": "unified_agent.ts_worker.transport:DiscordGateway",
        "worker": "unified_agent.ts_worker.worker:TaskSpaceWorker",
    },
    default_enabled=False,
)
```

The two capabilities name real, resolvable entry points rather than an
aspirational connector shape:

- `gateway` is `DiscordGateway`, the send/receive REST adapter
  (`fetch_messages`/`send_message`). It takes an injectable `TsBotTransport`
  in its constructor, which is the seam both the conformance suite
  (`tests/connectors/test_discord_conformance.py`) and
  `tests/test_ts_worker.py`'s `StubDiscord` use to exercise it against a
  fake transport -- no network call, no token, no live gateway connection.
- `worker` is `TaskSpaceWorker`, the polling/classify/dispatch loop
  (`poll_discord`) that durably logs inbound messages and posts attributed
  replies, but only for senders in `UA_TS_DISCORD_ALLOWED_USER_IDS`.

`required_secrets` names every environment variable
`tools/doctor.py::_ts_worker_check`'s existing Discord gate already
requires for a non-DORMANT, non-MISCONFIGURED posture: the bot token, the
sender allowlist (a token without it is live but never actionable), and the
channel list (without it there is nothing to poll). Unlike Zulip's optional
`UA_ZULIP_SITE` override (not in `required_secrets`), Discord's allowlist and
channel list are environment variable names that participate in whether the
connector can meaningfully start, so they are named here.

Credentials themselves stay parked (tasks #725-#727) -- this manifest never
mints or reads a secret's value, and no bot token exists in this checkout.
The connector stays off by default at two independent layers regardless of
the manifest: `ts_transport.discord_token() is None` (the pre-existing
runtime gate) and `default_enabled=False` on the manifest itself. Discord's
own `system_doctor` row (`connector:discord`, from this manifest) is
separate from and additional to the pre-existing `ts_worker` row, which
reports the same underlying env state from the worker's point of view
(Discord and Telegram together); the two never diverge because both read
the same `ts_worker.transport` functions.

## Mobile projection/action routes (task #1311)

`mobile-watch` registers the existing `GET /api/mobile/v1/projection` and
`POST /api/mobile/v1/action` handlers as `projection` and `action`
capabilities. Both remain behind hub authentication; the action handler keeps
the existing origin and CSRF guard. The routes return 404 unless
`UA_MOBILE_WATCH_ENABLED=1`; the manifest is also `default_enabled=False`.
Doctor and the Integrations page report this opt-in by environment variable
name only.

The current consumer boundary is the authenticated iPhone relay. The existing
Watch does not call these routes, and the manifest does not imply a Watch
client, push delivery, or Watch credentials. Enabling the routes exposes the
existing mobile API to a configured authenticated client; it does not install
or provision that client.

## Evidence export: a scheduled outbound connector (task #1266)

`evidence-export` builds on the existing signed evidence export (task
#1267/F1: `audit/evidence_report.py` + `audit/evidence_signing.py`) and adds
one thing it didn't have — a scheduler-friendly entry point that lands a
signed export at a customer-controlled storage destination on a timer,
instead of only on an operator's explicit `unified-agent evidence report`/
`evidence verify` invocation. It is the reverse shape of every other
connector in this file: Slack/Zulip/github-app pull data *in*; evidence-export
pushes a signed artifact *out*. It reads its own current pair to verify
idempotency and integrity, and deletes only its own old keys under retention.

```python
EVIDENCE_EXPORT_MANIFEST = ConnectorManifest(
    name="evidence-export",
    version="1.0.0",
    capabilities=frozenset({"export"}),
    secret_status_hook="unified_agent.audit.evidence_export_sink:manifest_secret_status",
    entry_points={
        "export": "unified_agent.audit.evidence_export_sink:run_once",
    },
    default_enabled=False,
)
```

**Destinations.** `UA_EVIDENCE_EXPORT_DESTINATION` selects one of `local`
(`LocalPathDestination`, fully implemented and tested — no external
dependency), `gcs` (`GCSDestination`, lazy-imports `google-cloud-storage`,
not a project dependency), or `s3` (`S3Destination`, lazy-imports `boto3`,
also not a project dependency). Using a destination without its package
installed fails with a clear "install the package" `DestinationError`
instead of an opaque `ModuleNotFoundError`. Neither cloud SDK is added to
`pyproject.toml` — see `src/unified_agent/audit/evidence_export_sink.py`'s
module docstring for why (no strong reason to force the dependency on every
install when most deployments will use `local`, mirrored onward by rsync/a
sync agent, or never enable this connector at all).

**Secrets are named, never read.** Every destination's actual credentials
(a GCS service-account key file, AWS access keys, an instance role, ...) are
resolved by the SDK itself from its own conventional environment
(`GOOGLE_APPLICATION_CREDENTIALS`, `AWS_ACCESS_KEY_ID`/
`AWS_SECRET_ACCESS_KEY`, workload identity, ...) — this connector's own code
never reads or logs a credential value, only bucket/path names (not secrets)
that it hands to the SDK constructor. `manifest_secret_status` is
destination-aware the same way `github_app.manifest_secret_status` is
token-mode-aware: it reports only the bucket/path selector for the active
`UA_EVIDENCE_EXPORT_DESTINATION` as a hard requirement (`UA_EVIDENCE_EXPORT_LOCAL_PATH`
/ `_GCS_BUCKET` / `_S3_BUCKET`), because each cloud SDK's credential env vars
are only one of several equally valid ways it resolves credentials, not a
single named requirement a valid IAM-role deployment would fail. Signing is
also required: readiness reports satisfied only when one of
`UA_EVIDENCE_SIGNING_KEY` or `UA_EVIDENCE_SIGNING_SECRET_NAME` is configured,
matching the signing gate in `run_once()`.

**Idempotent, race-safe object naming.** Each immutable body/signature pair is
content-addressed under
`evidence/<org_id>/<export-date>/<chain-head>-<sha256(body)[:16]>.json` and its
`.sig.json` sidecar. After both objects are written, a commit marker at
`evidence/<org_id>/<export-date>/<chain-head>.committed` names the committed
pair. A run takes one SQLite read snapshot for the chain head and report, then
builds the complete report before deciding whether to skip. The marker's
fingerprint covers stable report contents, window size, and signing key ID;
generation-only timestamps are excluded. Before skipping, the sink verifies
the body hash against its key, the sidecar digest and chain head, the active
signing key, and the report fingerprint. Changed inputs, missing/corrupt
objects, or malformed markers trigger a new immutable pair and marker. Window
days are calendar-day windows beginning at midnight UTC. The export-date path
is independent from that report window, so retention measures artifact age.
Retention runs on both exported and skipped paths; a retention failure is
reported as a partial status while preserving the already committed export.
Concurrent cloud runs may publish distinct immutable pairs, but a marker can
only select a pair that validates on the next run. Local runs also serialize
through a file lock.

**Retention is prefix-scoped.** `UA_EVIDENCE_EXPORT_RETENTION_DAYS` (default
90; `<= 0` disables it) only ever lists and deletes objects under this
connector's own `evidence/<org_id>/` prefix — it cannot reach anything a
human or another connector wrote elsewhere in the same bucket/path.

**Off by default at two layers**, same pattern as `github-app`:
`evidence_export_sink.enabled()` gates on `UA_EVIDENCE_EXPORT_ENABLED`
(unset/`"0"` by default), and `default_enabled=False` on the manifest.
`run_once()` (the scheduler entry point), `unified-agent-evidence-export`
(the installed console script — same `main()` as
`python -m unified_agent.audit.evidence_export_sink`), and
`unified-agent evidence export` (the human-facing CLI in
`audit/evidence_cli.py`) all return `{"status": "disabled", ...}` rather
than touching storage when the gate is off. The inert systemd example
still execs the module form.

**Scheduling** ships as an inert example, not an activated timer:
`deploy/systemd/ua-evidence-export.{service,timer}` (systemd user timer,
daily at 02:00, same shape as `deploy/systemd/ua-vault-sync.{service,timer}`)
and `deploy/mac-hub/com.unified-agent.evidence-export.plist.example`
(launchd `StartCalendarInterval`, ".example" suffix so launchd never loads it
as-is). launchd has no `EnvironmentFile=` equivalent to the systemd unit's,
so the plist execs `deploy/mac-hub/run-evidence-export.sh` — a small
wrapper, matching `run-pr-watch.sh`/`run-weekly-export.sh` and
`retired/run-zulip-worker.sh` — which sources
`~/.unified_agent/service.env` (0600) before exec'ing the real binary, so no
setting (let alone a secret) ever lives inline in the plist. Neither unit
pair is installed or enabled by this change; see each file's header comment
for the manual install steps. Full env var reference:
`docs/connector-activation.md`.

Conformance: `tests/connectors/test_evidence_export_conformance.py` runs the
same five-clause harness every other manifest runs, plus dedicated tests for
`LocalPathDestination` end-to-end (export -> sign -> write ->
`evidence_signing.verify_export` on the written bytes), complete exports,
concurrent writer safety, idempotency and retention on both paths, private
file/directory modes, disabled-by-default behavior, and readiness reporting
by environment-variable name (GCS/S3 are exercised only through fakes — no
network in this suite).
