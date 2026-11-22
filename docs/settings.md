# Settings

`settings.toml` personalizes one unified-agent install: who the human
operator is, where the workspace and vault live, how the hub listens. It sits
next to the database in `UA_HOME` (`~/.unified_agent/settings.toml`) and is
written by `unified-agent init`.

Every setting is still an `UA_*` environment variable underneath. The file
only supplies defaults:

```text
environment variable  >  settings.toml  >  built-in default
```

A value already in the environment (a systemd `Environment=` line,
`service.env`, your shell) always wins. That keeps service units, containers
and CI working unchanged, and lets you override one value for one run.

## Secrets do not go here

`settings.toml` is meant to be readable, diffable, and safe to copy between
machines. Bearer tokens, API keys and passphrases stay in `service.env`
(mode `0600`). The loader refuses any `[env]` entry whose name looks like a
credential (`*_TOKEN(S)`, `*_KEY`, `*_SECRET`, `*PASSWORD*`, `*PASSPHRASE*`,
`*CREDENTIALS*`) and says so on stderr.

`[env]` only accepts `UA_*` names. A copied settings file can't set `PATH`,
`PYTHONPATH`, `LD_PRELOAD` or another tool's variables for the hub and the
processes it starts. It also can't set security posture or its own location:
`UA_ENFORCE_AUTH`, `UA_LEGACY_OPEN_POSTURE`, `UA_APPROVAL_TOOLS`,
`UA_TRUST_STDIO`, `UA_REQUIRE_CALLER_IDENTITY`, `UA_LIFECYCLE_OPERATORS`,
`UA_POLICY_AUTHORIZERS`, `UA_POLICY_AUTHORIZATION_ALLOW_SELF`, `UA_HOME` and
`UA_SETTINGS_FILE` stay in the real environment.

The file names the human operator, so the loader ignores it (with a warning)
unless it is owned by the current user and not writable by group or other
users. `unified-agent settings set` and `init` write it as `0600`.

## Example

```toml
schema_version = 1

[operator]
name = "Alex Example"          # shown on approvals, evidence and the Control Center
label = "Desktop_Operator"         # the identity your own token/login resolves to
aliases = ["alex"]             # other names you sign with
timezone = "Europe/Berlin"

[paths]
workspace = "~/agent-workspace"
vault = "~/Obsidian/Main"      # "" disables the vault tools
vault_write_dirs = ["00 - Inbox", "Logs", "Wiki"]
dev_root = "~/code"

[server]
transport = "http"
host = "127.0.0.1"
port = 8765
deployment_mode = "authenticated_private"
allowed_hosts = ["127.0.0.1:8765", "hub.example-tailnet.ts.net:8765"]

[agents]
# Workers only. The operator label stays out of the roster.
labels = ["my_claude", "my_codex"]

[cloud]
project = "example-project"
worker = "example-worker"

[chat]
zulip_site = "https://zulip.example.test"

[env]
# Any other non-secret UA_* variable.
UA_STATE_SYNC = false
```

## Typed settings

| Setting | Variable | Meaning |
|---|---|---|
| `operator.name` | `UA_OPERATOR_NAME` | Your name as the human operator. Default: the first alias, else `Operator`. |
| `operator.label` | `UA_OPERATOR_LABEL` | Identity label of your own login. Default `Desktop_Operator`. |
| `operator.aliases` | `UA_OPERATOR_ALIASES` | Further names that mean you (comma-separated in the env). |
| `operator.timezone` | `UA_OPERATOR_TZ` | IANA zone for "today" windows. |
| `paths.workspace` | `UA_WORKSPACE` | Sandbox root for `fs_*`, `run_cmd`, `git_*`. Default `$UA_HOME/workspace`. Informational once `init` pins `UA_WORKSPACE` in `service.env` (see below). |
| `paths.vault` | `UA_VAULT_PATH` | Explicit Obsidian-style vault; unset or empty disables vault tools. |
| `paths.vault_write_dirs` | `UA_VAULT_WRITE_DIRS` | Vault folders agents may write. |
| `projects.entries` | `UA_PROJECTS_JSON` | Installation project ids, labels, checkout names, domains and optional repository seeds. |
| `paths.dev_root` | `UA_DEV_ROOT` | Directory holding your project checkouts. |
| `server.transport` | `UA_TRANSPORT` | `stdio` or `http`. |
| `server.host` / `server.port` | `UA_HTTP_HOST` / `UA_HTTP_PORT` | Hub bind address. |
| `server.deployment_mode` | `UA_DEPLOYMENT_MODE` | `local_trusted`, `authenticated_private`, `authenticated_public`. |
| `server.allowed_hosts` | `UA_HTTP_ALLOWED_HOSTS` | Host headers the hub answers. |
| `agents.labels` | `UA_AGENT_LABELS` | Closed agent roster; empty keeps it open. |
| `cloud.project` | `UA_GCP_PROJECT` | Explicit GCP project; unset cloud probes return UNKNOWN. |
| `cloud.worker` | `UA_GCP_WORKER` | VM eligible for health and approval-gated restart; no default. |
| `chat.zulip_site` | `UA_ZULIP_SITE` | Explicit HTTPS site; zuliprc must match. |
| `github.connector_repo` | `UA_GITHUB_CONNECTOR_REPO` | Explicit owner/repository for Approval Ledger polls; missing target is unconfigured. |
| `github.connector_base` | `UA_GITHUB_CONNECTOR_BASE` | Base branch both Approval Ledger poll transports watch. Default `main`; an invalid branch name is unconfigured. |
| `vault_sync.remote_host` / `vault_sync.remote_root` | `UA_VAULT_SYNC_REMOTE_HOST` / `UA_VAULT_SYNC_REMOTE_ROOT` | Both required for SSH vault sync. |
| `fleet.nodes` / `fleet.optional_nodes` | `UA_FLEET_NODES` / `UA_FLEET_OPTIONAL_NODES` | ua-node names that must be live, and names that may be absent by decision. Doctor's `fleet_nodes` row checks each. |
| `telemetry.lane_b_repos` | `UA_LANE_B_REPOS` | JSON list of unique slug/github owner/repo report targets; missing targets show evidence gaps. |
| `search.provider` | `UA_SEARCH_PROVIDER` | Backend for `web_search`. |
| `memory.embed_model` | `UA_EMBED_MODEL` | fastembed model for semantic memory. |
| `queue.liveness_sweep` | `UA_LIVENESS_SWEEP` | Task liveness sweep: `off`, `report` (default: one digest entry per task with no action path, nothing moved) or `enforce` (also moves such a task to `blocked` with a recovery note). See [work-queue.md](work-queue.md#liveness-every-task-has-a-next-move). |

Anything else goes in `[env]` under its variable name. See
[Configuration](configuration.md) for every variable.

Some settings have no code default but are required once a feature is on:
`github.connector_repo` when the Approval Ledger connector is enabled, and
`paths.vault` when the pr-watch job is installed. `system_doctor`'s
`required_settings` row reports either one missing as `MISCONFIGURED` (see
[tools reference](tools-reference.md#system_doctor---dict)).

## The human operator

Exactly one login is treated as human: `operator.label`. It gets the
`operator` profile, may resolve approvals, and is recorded as the human link
in evidence reports. `operator.name` and `operator.aliases` are the names the
same person signs delegations and approvals with; a request your login makes
under one of those names is accepted, any other mismatch is refused.

`operator.label` must be a well-formed label and must not name a catalogued
agent identity such as `agent-codex` or a worker in `UA_AGENT_LABELS`.
Either mistake falls back to `Desktop_Operator` with a warning, and
`unified-agent init` refuses such a label up front. The same rule drops any
name or alias that names a catalogued agent or a worker holding its own bearer
(in `UA_AGENT_TOKENS` or a managed token), so configuration can never promote
an agent to the human operator. Bare human names in the roster stay allowed.

Re-running `init` with a new `--operator-label` adds a token for the new
label to `service.env` and keeps the existing ones. Existing operator name
and label pins in `service.env` are reconciled when an identity migration
is requested, including without `--force`. Omitted name/login flags keep the
effective installed values rather than stale TOML values or defaults. When a
field has no installed `service.env` pin, an identity supplied through the real
environment takes precedence over TOML; prior TOML seeds are distinguished from
those declarations. An explicit identity migration also writes changed external
identity pins to `service.env` so sourcing it on restart matches the new bearer.
Unicode display names retain their spelling, while default
login labels use an ASCII slug with a stable fallback. Existing
alias pins, including an explicitly empty list, remain authoritative. Targeted
identity/token edits preserve unrelated raw lines, comments and shell expansion.
Simple operator pins such as `UA_OPERATOR_LABEL=$HUMAN_LABEL` or
`UA_OPERATOR_LABEL=${HUMAN_LABEL}` read the referenced variable from the caller's
environment. An unchanged pin stays an expression without `--force`. Missing
variables or other shell expressions stop initialization before settings or
credentials change; export the variable or replace the pin explicitly. `init`
never executes the installation file to resolve an identity.
If `UA_SETTINGS_FILE` disables the settings file,
`init` persists the operator identity directly in `service.env`. A failed
identity or settings publication restores the original token stores.

### Client machines (desktop, laptop)

A client that only talks to the hub has no `service.env` and never runs
`unified-agent init`: that command creates a database and mints bearers, and a
desktop marked `NO_LOCAL_MEMORY_DB` refuses to hold one. With no
`settings.toml` either, the local TUI and Control Center helpers fall back to
the generic default name `Operator`, so work started from that machine is
attributed to "Operator" in the transcript. The hub never uses this name to
decide who you are; your bearer decides that. Set the name once per client:

```bash
unified-agent settings set operator.name "Alex Example"
unified-agent settings get operator.name     # confirm
```

This creates `settings.toml` with only that key. Do not set `operator.label`
on a client: the label is the login identity the hub resolves your own token
to, and a client's agent bearer carries its own label. The same effect comes
from exporting `UA_OPERATOR_NAME`. `deploy/desktop-bootstrap.sh` does not write
this file.

An org chart (`org.yaml`) can refer to the operator as `$operator` (the
label) and `$operator_name` (the name) instead of naming a person.

## Commands

```bash
unified-agent settings path            # where the file is
unified-agent settings show [--json]   # effective value (including defaults) and its source
unified-agent settings keys            # typed settings and their variables
unified-agent settings get operator.name
unified-agent settings set operator.name "Alex Example"
unified-agent settings set env.UA_EDITION community
unified-agent settings unset paths.vault
unified-agent settings check           # exit 1 on unknown keys or bad values
```

`unified-agent init` accepts `--operator-name`, `--operator-label`,
`--workspace`, `--vault` and `--interactive`, and writes the file for you.

The workspace is the exception to "settings.toml supplies defaults": it is the
sandbox root, a security boundary, so `init` always pins it uncommented in
`service.env`, and from then on editing `paths.workspace` does not move it.
Use `unified-agent init --workspace PATH` instead. `init` warns when
`settings.toml` names a different path than the pin, and `system_doctor`'s
`workspace_pin` row reports it as `DEGRADED` (see [security](security.md)).
It is for a machine that runs its own hub; a client uses `settings set`
(see [Client machines](#client-machines-desktop-laptop)).

## Turning it off

Set `UA_SETTINGS_FILE` to another path to use a different file, or to the
empty string to ignore settings files entirely. The test suite does the
latter so a developer's own settings never leak into a run. Because the file
is read when the process starts, restart the hub after editing it.

## Optional cloud and chat integrations

Cloud health and recovery require both `cloud.project` and `cloud.worker`.
Without either, they return UNKNOWN with configuration guidance and do not run
gcloud. Recovery selects only this server-configured worker; caller project and
zone overrides remain refused, and the existing approval gate still governs starts.

Zulip requires an explicitly configured HTTPS `chat.zulip_site`. Credential files
cannot select the site: their site must match this setting. Outbound retirement
remains on by default, including when credentials and the site are configured.

## Installation fleet and organization

New installs ship an empty surface map and one operator office. The starter
chart assigns no agents, epics or default lanes. It describes the configured
operator through `$operator` and `$operator_name`; it grants no capabilities
and activates no profile.

Existing installations must preserve their organization chart outside the
repository and set `UA_ORG_REGISTRY_PATH` to that file before upgrading. Keep
existing surface bindings explicitly in `UA_SURFACE_MAP`. Both variables can
be set in service configuration or the non-secret `[env]` settings table:

```toml
[env]
UA_ORG_REGISTRY_PATH = "~/.unified_agent/installation-org.yaml"
UA_SURFACE_MAP = "workstation/hermes/builder:Builder_One,hub-host.local/kanban/reviewer:Reviewer_One"
```

Conflicting labels for one surface key fail closed; `NONE` or an empty label
leaves it unmapped. Explicit chart members still use the existing identity
profile rules. A configured agent roster requires corresponding offices; the
neutral starter does not silently enroll existing agents. Operator task-lane
vocabulary is a separate migration; this chart introduces no new lane name.

## Explicit integration targets

No source repository, image registry, GitHub poll repository or remote vault is
selected from the developer's machine. Before upgrading an existing fleet,
preserve its chosen values in external `settings.toml`, `service.env` or launcher
environment; the repository does not migrate or infer them.

- Source installs require `UA_REPO_URL` / `install.sh --repo`, or `--local`.
- Compose requires `UA_IMAGE_REPOSITORY`; `UA_IMAGE_TAG` pins the release.
- Image publication requires `UA_GCP_PROJECT`, `UA_BUILD_REGION` and
  `UA_IMAGE_REPOSITORY`. The Artifact Registry destination must match that project
  and region. Optional `UA_REPO_URL` fills the OCI source label.
- The Approval Ledger worker requires `UA_GITHUB_CONNECTOR_REPO=example/project`.
  Its existing service bearer and approval policy still apply.
- Vault sync requires both remote host and root in the sync service environment.
  Local vault tools also require an explicit `UA_VAULT_PATH` (`paths.vault`).
  An unset target exits unconfigured before local state or SSH writes. Doctor
  cannot certify a cached successful sync after the destination is removed.
- Lane B telemetry requires `UA_LANE_B_REPOS`, for example
  `[ {"slug":"sample","github":{"owner":"example","repo":"project"}} ]`.
  Missing or malformed targets yield unavailable totals, with no source probes.
- The write hook looks for Ruff in this order and uses the first match:
  `UA_RUFF_BIN` (when set, it is used alone, with no fallback), the checkout's
  own `.venv/bin/ruff`, then, when the checkout is a linked git worktree, the
  main checkout's `.venv/bin/ruff`, and finally `ruff` on `PATH`. A worktree
  without its own `.venv` therefore prefers the main checkout's Ruff over the
  one on `PATH`. When none is available it skips. Hook payloads are never
  logged.

## Installation project registry

Fresh installations contain only the `unified-agent` legacy queue and no
repository binding. Add explicit project tables to describe your portfolio:

```toml
[[projects.entries]]
id = "unified-agent"

[[projects.entries]]
id = "client-one"
label = "Client One"
dirname = "client_checkout"
domain = "professional"
repo = "example-org/client-repo"
default_branch = "main"
```

The real `UA_PROJECTS_JSON` environment variable wins over these tables.
Explicit entries replace the starter registry and must retain the legacy
queue id. An id-only default queue keeps its `unified_agent` checkout name.
Checkout names are single directory components under `paths.dev_root`;
checkout symlinks are refused. Project entries grant no agent filesystem or
tenant authority. Existing database repository bindings continue to win over
configured repository seeds. Home scope filters use the server registry.

Before upgrading an existing installation, preserve every existing project
id, display label, checkout name, domain and effective repository seed in its
settings outside Git. Task rows are retained and never used to discover new
projects implicitly. An omitted legacy stream remains in stored cross-project
results, but cannot accept new scoped work until it is explicitly configured.
Validate settings and the project registry before restarting the hub.
