# Installation

## Prerequisites

- Python ≥ 3.11 (`pyproject.toml`: `requires-python = ">=3.11"`). That is the
  declared floor. Development, CI, and the hub image pin Python 3.14
  (`.python-version`, which `uv` and GitHub Actions read, and
  `python:3.14-slim` in the `Dockerfile`).
- [`uv`](https://github.com/astral-sh/uv) for environment/dependency management.
- Claude Code and/or Google Antigravity (IDE + Agy CLI), whichever host(s) you're
  registering into.
- Optional: an Obsidian vault (for `vault_*` tools), Docker or Podman (for the container
  sandbox), a local Dash/Zeal docset install (for `dash_*` tools).

**Hosted hub (Railway).** The cloud control-plane path is
[docs/deploy/railway.md](deploy/railway.md): one service, one volume, devices
execute. Control Center sign-in there is **Sign in with WorkOS** when
`GET /api/session/options` returns `{"workos": true}` — not a pasted bearer.
Hosted starts force `UA_TAILNET_PEER_MODE=off` (Railway's edge proxy is not a
tailnet node) and never enable the OAuth passphrase fallback. This page stays
the self-hosted / source install.

## Install dependencies

```bash
git clone <this-repo> ~/dev/unified_agent   # or wherever; canonical path used throughout the docs
cd ~/dev/unified_agent
uv sync
```

This creates `.venv/` and installs the runtime dependencies (`mcp[cli]`, `ddgs`, `httpx`,
`trafilatura`, `fastembed`, `pyyaml`, `textual`) plus the dev group (`pytest`,
`pytest-asyncio`, `ruff`). Verify with:

```bash
uv run pytest
uv run ruff check .
```

## Cold start on a foreign machine

The steps above assume the canonical `~/dev` layout. The fastest path onto a
machine that has none of this topology — a fresh laptop, a container, a CI
runner — is `install.sh` at the repo root, which wraps the manual flow below
(verify its checksum before running, refuse nothing it does that you wouldn't
do by hand):

```bash
curl -fsSL <install.sh URL> -o install.sh
echo "<expected sha256>  install.sh" | sha256sum -c -
bash install.sh --repo https://github.com/example/project --ref v0.3.2 # installs unified-agent[pdf] from the tag
bash install.sh --local ~/dev/unified_agent   # or from a source tree — private repo, no creds
```

`install.sh` refuses to run as root, installs a pinned checksum-verified `uv`
only when `uv` is missing, runs `unified-agent init` + `unified-agent doctor`
(exit non-zero on MISCONFIGURED or degraded health for an explicitly registered
integration), and is idempotent — re-running converges to the same state. An
unconfigured integration stays DORMANT on a fresh install. It never prints the generated token; it lives in
`service.env` as usual. The `.github/workflows/install-gate.yml` job exercises
this exact path on a fresh runner for every release tag: fetch + checksum +
install + doctor + HTTP hub + one human-resolved gated action + evidence PDF.

The equivalent manual flow is `uv tool install` plus `unified-agent init`:

```bash
uv tool install "git+${UA_REPO_URL}"   # set your repository URL, or use a local path
# The git+https form needs credentials for the private remote; without them use
# a local path: transfer the source tree and run `uv tool install .` from it.
unified-agent init          # creates ~/.unified_agent, an empty DB, a starter env file
set -a; . ~/.unified_agent/service.env; set +a
unified-agent doctor        # must exit 0 — every opt-in feature reads DORMANT, nothing MISCONFIGURED
unified-agent               # run the server (stdio); UA_TRANSPORT=http to serve other machines
```

With `UA_TRANSPORT=http` and a package built without the Control Center (see
below), plain `unified-agent doctor` exits 1 on `web_ui`. Run
`unified-agent doctor --allow-unbuilt-ui` instead, as `install.sh` does: it still
reports `web_ui` but fails only on the other checks.

`unified-agent init` is idempotent — it never rewrites an existing
`service.env` (so it never rotates your token) unless you pass `--force`, and it
never touches an existing database. `--print-token` echoes the generated bearer
token; `--json` prints a machine-readable summary for a provisioning script.

`unified-agent doctor` wraps the `system_doctor` capability report for a shell:
it exits `1` for **MISCONFIGURED** or **DEGRADED** capabilities. On a cold
install every systemd timer, the Obsidian API, GCS backup, and the Zulip relay
report **DORMANT** — those are opt-in, not broken. The GCP integration also
remains DORMANT until explicitly registered; once active, unreadable inventory
or a stopped worker is DEGRADED. `--strict` also fails on DORMANT for an
operator who wants the whole surface live; `--json` emits the full report.

The installer rejects `UA_ENFORCE_AUTH=1` with `UA_TRANSPORT=stdio`: stdio
does not establish an HTTP-authenticated identity, so that combination
would give a false impression of enforced authentication. Set
`UA_TRANSPORT=http` in `service.env` before running the installer if you
intend to enforce auth. `UA_SKIP_DOCTOR=1` skips doctor only; the
transport/auth-provider gates still run.

The installed package carries the Control Center build and the capability
catalog (`unified_agent/_resources`), so an HTTP hub installed from a wheel or
sdist serves `/ui` with no Node, no checkout and no `UA_WEB_DIST_DIR`. Building
that wheel from a source checkout needs the web app built first, in dependency
order (`web` consumes the built `protocol` and `client`):
`(cd packages/protocol && npm ci && npm run build)`,
`(cd packages/client && npm install && npm run build)`, then
`(cd packages/web && npm ci && npm run build)`. Without it, `uv tool install .`,
`install.sh` and `git+` installs still succeed: the package build warns and
leaves the UI out, and `/ui` answers 503 with a build hint until you build the
web app and install again. On HTTP transport `install.sh` still reports
`web_ui` MISCONFIGURED in its doctor run but finishes
(`unified-agent doctor --allow-unbuilt-ui`); every other doctor failure, and a
`UA_WEB_DIST_DIR` that points at a missing build, still stops it. Only an explicit release build
(`UA_RELEASE_BUILD=1`) refuses to package without it
([Building a self-contained package](adopter-package-build.md)). An editable
install (`uv sync`) keeps serving the checkout's `packages/web/dist`, and `/ui`
answers 503 until it is built.

`UA_WEB_DIST_DIR` remains an override for a custom or development UI build:
point it at an absolute `dist` path. It takes precedence over the packaged UI,
so a path left over from an earlier install keeps serving that older build
after an upgrade; remove it from `service.env` unless you mean to override.
Doctor keeps `web_ui` MISCONFIGURED if the selected `index.html` is absent.
For an HTTP hub, `install.sh` notes whether the packaged UI or a
`UA_WEB_DIST_DIR` override will be served.

What `init` does *not* assume: it sets the workspace to
`~/.unified_agent/workspace` (the built-in default is `~/agent-workspace`, which does
not exist on a fresh machine) in `settings.toml` (`paths.workspace`), and pins
`UA_WORKSPACE` in `service.env` only when the settings file is disabled, so a
`paths.workspace` edit takes effect on restart. It leaves `UA_VAULT_PATH` unset
(the `vault_*` tools degrade gracefully without a vault), and leaves
`UA_ENFORCE_AUTH` unset. Stdio does not enforce authentication. HTTP does:
anonymous requests are `401`. Distribute the generated bearer before other
clients connect. `UA_ENFORCE_AUTH=0` opts into warn-mode and is not a supported
posture for a shared hub.

## Docker

Configure your image destination explicitly with `UA_IMAGE_REPOSITORY`, for example
`region-docker.pkg.dev/project/repository/unified-agent`. Authenticate Docker with
an account that can read that registry. No image repository is built in; Compose
requires this setting before starting a container. Pin an explicit release tag;
`UA_IMAGE_TAG` defaults to v0.3.2.

```bash
docker pull "${UA_IMAGE_REPOSITORY}:v0.3.2"
docker run -d --name unified-agent \
  -v ua-data:/data \
  -p 127.0.0.1:8765:8765 \
  "${UA_IMAGE_REPOSITORY}:v0.3.2"
docker logs unified-agent   # first boot prints the generated bearer token once
```

For remote source installs, set `UA_REPO_URL` or pass `install.sh --repo` an HTTPS
repository URL; `--local` needs no remote setting. URLs must not embed credentials.

To publish a release image, configure `UA_GCP_PROJECT`, `UA_BUILD_REGION` and
`UA_IMAGE_REPOSITORY` with a matching Artifact Registry destination, then run
`scripts/publish-image.sh v<version>` with a
gcloud account that can submit Cloud Build jobs. Optional `UA_REPO_URL` supplies
the OCI source label; a local build leaves that label empty. It builds `git archive` of the
tag on Google Cloud Build (`deploy/docker/cloudbuild.yaml`) and pushes it to
Artifact Registry, so no GitHub Actions minutes are used. The script takes the
build recipe from the checkout you run it in, so publish from a current `main`.
The older GHCR workflow (`.github/workflows/docker.yml`) builds and
smoke-tests the image after merges that change the Dockerfile, its assets or a
dependency manifest. Dispatch it on a release tag
(`gh workflow run docker.yml --ref v<version>`) to build that tag and run its
GHCR publish job.

Or with compose: `docker compose -f deploy/docker/compose.yaml up -d`
(override the tag with `UA_IMAGE_TAG`).

- **First start**: if `/data/service.env` does not exist, the entrypoint runs
  `unified-agent init`, which creates the DB, a starter `service.env`, and a
  bearer token. The token is printed to the container log in a clearly
  delimited block exactly once — capture it there; it is never printed again.
- **Token rotation**: edit `UA_AGENT_TOKENS` in the volume's
  `/data/service.env` and restart the container. `unified-agent init` never
  rewrites an existing `service.env`.
- **State**: everything durable lives under `/data` (`memory.db`,
  `service.env`, the default `UA_WORKSPACE` at `/data/workspace`). Back up the
  `ua-data` volume.
- **Ports**: the hub binds `0.0.0.0:8765` inside the container; publish on
  loopback (`-p 127.0.0.1:8765:8765`, the compose default) and put a tailnet
  or reverse proxy in front for remote reach — do not publish on all
  interfaces.
- **Host allowlist**: with no `UA_HTTP_ALLOWED_HOSTS` set, the entrypoint
  defaults it to `127.0.0.1:*,localhost:*`. To serve additional hostnames set
  `UA_PUBLIC_HOSTS` (comma-separated, e.g. `UA_PUBLIC_HOSTS=hub.example.com:*`),
  or set `UA_HTTP_ALLOWED_HOSTS` yourself to take full control.
- **Auth**: the container always re-applies `UA_ENFORCE_AUTH=1` after sourcing
  `service.env`. `UA_DOCKER_ALLOW_INSECURE=1` opts out with a loud startup
  warning — debugging only, not a supported posture.
- **Bind mounts**: the hub runs as uid 10001 (`ua`), so a host bind mount for
  `/data` must be writable by it — `chown 10001:10001 ./data`. Named volumes
  (`ua-data`) need nothing.
- **CLI commands in the container**: run them through the entrypoint so they
  inherit the sourced `service.env` and container overrides — bare
  `docker exec` sees only the Dockerfile `ENV`. Example:
  `docker exec unified-agent /app/deploy/docker/entrypoint.sh unified-agent doctor`.
- **Hosted image defaults vs `service.env`**: on Railway or `UA_HOSTED=1`,
  platform-injected variables win over the volume's `service.env`. Image
  `ENV` values (`UA_EDITION=enterprise`, `UA_WORKSPACE=/data/workspace`, and
  the other Dockerfile defaults) are not platform variables: the entrypoint
  leaves a key out of the platform snapshot when it still equals the image
  default, so `service.env` and a real runtime value win, and the default
  applies only while the key is unset. Editing `UA_EDITION` or `UA_WORKSPACE`
  in `service.env` therefore sticks after restart. See
  [hosted hub on Railway](deploy/railway.md#what-the-image-does-on-railway).

## Install the pre-commit security-audit gate

`.git/hooks` is not version-controlled, so a fresh clone has no gate until you run:

```bash
./scripts/install-hooks.sh
```

This writes a `pre-commit` hook that runs `unified-agent-audit . --fail-on high` and
blocks the commit if any high-severity finding is present. See
[security.md](security.md) for the rule catalog and
[cli-reference.md](cli-reference.md#unified-agent-audit) for the exit-code semantics.

## Registration

The server binary lives at `.venv/bin/unified-agent` after `uv sync`. **Use absolute
paths** in every host config — Claude Code launches MCP servers without a shell, so `~`
and `$HOME` are not expanded (see [troubleshooting.md](troubleshooting.md#absolute-paths-in-host-configs)).

### Which transport to register

**On a machine running the systemd hub, the HTTP hub is the canonical transport for
primary workspaces** — the shared workspace root (`~/agent-workspace`) and the
unified-agent repo itself. Register it once, user-scoped, per
[Hub mode](#hub-mode-shared--remote) below; do not also give those repos a
project-scoped stdio entry.

The reason is that the two legs are not equivalent under enforcement. Project scope
outranks user scope, so a stdio entry in a primary repo silently wins over the hub, and
the spawned process inherits neither `UA_ENFORCE_AUTH` nor `UA_AGENT_TOKENS` (both live
in `~/.unified_agent/service.env` via the unit's `EnvironmentFile`). The result is a leg
that answers tool calls without a bearer token, outside the approval queue, and invisible
to the control center's agent registry — the audit trail records the hub's clients, and a
stdio session is not one of them.

**stdio is for isolated satellite repos** — a project that needs its own `UA_WORKSPACE`
root, or a machine with no hub at all. It is not the laptop configuration in
this deployment. See [Satellite projects](#satellite-projects-project-scoped-mcpjson).

### Claude Code (stdio, user scope)

For a machine that runs no hub, this registers the server for every project on it:

```bash
claude mcp remove --scope user unified-agent   # if re-registering
claude mcp add --scope user unified-agent \
  --env UA_WORKSPACE=$HOME/dev/agent \
  -- $HOME/dev/unified_agent/.venv/bin/unified-agent
```

Verify with `claude mcp list`.

### Antigravity (IDE + Agy CLI)

Add to `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "unified-agent": {
      "command": "/home/you/dev/unified_agent/.venv/bin/unified-agent",
      "args": [],
      "env": { "UA_WORKSPACE": "/home/you/dev/agent" }
    }
  }
}
```

Restart Antigravity after editing. If the server doesn't appear, check
`~/Library/Logs/Antigravity/language_server.log` (macOS) for a JSON parse error in the
config file.

### Restart after registering

Both hosts read the tool list once at process start. **Restart each host** (or start a
fresh session) after adding/changing the registration.

### Adding an agent identity (enrollment)

**One command:** on the hub host, `unified-agent agent add <label> --office <office-id>`
issues the bearer, seats the label and prints its hub entry, with no
`service.env` edit or restart, and `unified-agent agent remove <label> --reason
"<text>"` retires it. See
[agent-lifecycle.md](agent-lifecycle.md#adding-and-removing-an-agent). The manual
steps below remain for env-listed bearers.

Registering a host is not the same as adding an agent. On an HTTP hub each agent
authenticates with its own **label** and **bearer token**, and the hub decides what
that label may do. Adding one is an operator step on the hub host: an agent cannot
mint credentials for another agent or for itself.

**Why agents cannot enroll themselves.** The token tools (`agent_token_issue`,
`agent_token_rotate`) need a verified caller identity, are pruned from the default
developer profile and absent from the operator profile's allow-list
(`profiles.py`), and every call parks a mandatory managed-secret approval that only
a different, human identity can grant (`tools/approvals.py`). The roster
(`UA_AGENT_LABELS`) and `UA_AGENT_TOKENS` live in the hub's `service.env`, which
no tool edits.

To add an agent on a default install (`unified-agent init` wrote
`~/.unified_agent/service.env`):

1. **Choose the label.** Use the identity the harness is configured to present
   (for example `Local_Worker`). It must be a well-formed label (a letter, then
   letters, digits, `_` or `-`, at most 64 characters) and must differ from the
   operator label and from every other agent's label. On an open roster, names
   with a `Desktop_`, `Laptop_` or `server_` prefix must also match the built-in
   fleet catalog, or the hub refuses the bearer (`config.is_known_agent_label`).
2. **Closed roster only: add the label to `UA_AGENT_LABELS`.** When that variable
   is set, the hub accepts only its members: a bearer for any other label does not
   authenticate, and `agent_list` and peer views leave the label out. Append it to
   the colon-separated list in `service.env`. The roster lists workers only:
   never add the operator label. `config.operator_label` rejects a label that
   appears there and falls back to the default, so the operator's login loses its
   human approval authority. On a closed roster the operator's bearer is therefore
   not accepted on `/mcp`; the operator signs in to the Control Center with it
   (that login checks only `UA_AGENT_TOKENS`, `webui/auth.verify_token`) and
   approves there. On an open roster (unset, the default) skip this step;
   `agent_list` then shows only catalog identities and registered ua-nodes, so a
   custom label working is not the same as it being listed.
3. **Add a bearer to the token store.** Generate a random value (for example
   `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, the generator
   `init` uses) and append a `<label>:<token>` entry to `UA_AGENT_TOKENS` in
   `service.env` (entries are comma-separated). If `UA_AGENT_TOKENS` instead holds
   a path, add the entry to that file on its own line and keep it mode `0600`.
   The same value must never appear under two labels: a shared token authenticates
   nobody (`config.agent_tokens`).
4. **Bind a workspace on the hub if the agent needs one.** An HTTP client's own
   `UA_WORKSPACE` does not reach the hub. Without an entry the agent uses the
   hub's `UA_WORKSPACE`; to give it a separate root (or `DENY`), add
   `<label>:<path>` to `UA_AGENT_WORKSPACES` in `service.env`. `Laptop_*` and
   `server_*` labels with no entry are denied hub-side file access
   (`config.resolve_workspace_binding`).
5. **Keep auth enforcement on for an HTTP hub.** `unified-agent init` leaves
   `UA_ENFORCE_AUTH` unset, and unset means HTTP enforces (`config.enforce_auth`).
   An explicit `0` wins over that default and is warn-only: the hub serves a
   request with no bearer, or with one it does not accept, anonymously, and
   logs a startup warning, so the new bearer protects nothing. Do not set
   `UA_ENFORCE_AUTH=0` on a hub other clients reach. With enforcement on (unset
   or `1`), the hub answers such requests with `401`. Agent bearers the hub
   accepts keep working.
   The operator token `init` generated keeps working for the Control Center login,
   and on `/mcp` too while the roster is open; keep the operator label out of
   `UA_AGENT_LABELS` (step 2). With enforcement on, the hub refuses to start if
   no bearer, OAuth or WorkOS credential is configured
   (`config.require_configured_http_credentials`). `init --force` keeps the value
   you set.
6. **Restart the hub** (`systemctl --user restart unified-agent`, or restart the
   container) so it reloads `service.env`. A token file is re-read on every
   request, so a change only to that file needs no restart; roster, workspace and
   enforcement changes always do. If `UA_AGENT_TOKENS` is a path that is missing
   or unreadable, the parser raises rather than treating it as an empty map
   (`config._parse_colon_pairs`). An empty map used to serve requests
   anonymously under `UA_ENFORCE_AUTH=1`. One token assigned to two labels
   authenticates nobody; `system_doctor`'s `agent_token_collisions` row names
   those labels without printing a token.
7. **Configure the agent's host.** Register the hub as described for that harness
   in [harnesses.md](harnesses.md#register-each-harness), using its token
   delivery: an environment variable the client reads (Codex
   `bearer_token_env_var`, Cursor `${env:...}`), a `*-with-hub` wrapper that loads
   this agent's token file at launch, or a header in the client's private MCP
   config file. Do not type the token as a command-line argument: it lands in
   shell history and is visible in the process list. For Claude Code, add the
   entry to the user-scope `mcpServers` in `~/.claude.json` with an editor,
   replacing the placeholders, and keep that file private:

   ```json
   { "mcpServers": { "unified-agent": {
     "type": "http",
     "url": "<hub-url>",
     "headers": { "Authorization": "Bearer <token>" } } } }
   ```

   Never commit the value or paste it into a shared file.
8. **Verify.** Restart the host, then call `profile_status` from it (or run
   `unified-agent verify --agent <label> --hub-url <hub-url>` there with the token
   in `UA_AGENT_BEARER_TOKEN`) and check that the identity is the new label. A
   label with no mapping in `profiles.py` gets the `developer` profile.

**Managed-secret alternative.** On a hub that already runs with
`UA_MANAGED_SECRETS=1`, tokens can be issued without editing the token store: a
steward or chief-of-staff session calls

```
agent_token_issue(agent_label="<label>", value="<token>", note="<why>")
```

and the operator approves the parked `managed_secret_set` request from a
different, human identity (Control Center or `approval_resolve`); the caller then
repeats the identical call to consume the one-shot pass. The value is stored only
in the managed-secret store and the `agent_tokens` table keeps its digest
(`tools/token_lifecycle.py`); the hub accepts it from the next request. Steps 1, 2,
4, 5, 7 and 8 still apply, with a hub restart (step 6) after any `service.env`
change. Do not turn managed-secret mode on just to enroll an
agent: it blocks arbitrary and unclassified tool execution hub-wide (see
[agent-access.md](agent-access.md#write-only-managed-values)). Without it the call
fails with `managed secret mode is disabled`.

To rotate or revoke a `UA_AGENT_TOKENS` bearer, replace or remove its entry and
restart the hub (a token-file edit takes effect on the next request). A managed
token is rotated with `agent_token_rotate`, under the same approval.

The generated startup block in `~/agent-workspace` also tells agents to ground
themselves from shared broadcasts and the live task queue before answering
integration questions, so the cockpit starts from shared state instead of a
process-local guess.

## Hub mode (shared / remote)

Instead of a stdio copy per host (each with its own process and, if `UA_HOME` differs,
its own database), run one HTTP instance and point every host at it — one shared
process, one shared database, one shared vault:

```bash
UA_TRANSPORT=http UA_HTTP_HOST=127.0.0.1 UA_HTTP_PORT=8765 unified-agent
```

Then register each host against the HTTP endpoint instead of spawning stdio. A
hub that other clients reach, or any hub that enforces auth (`UA_ENFORCE_AUTH=1`,
or unset on HTTP), needs each host to send its agent's bearer: register it as in
[Adding an agent identity](#adding-an-agent-identity-enrollment), step 7, with
the token in the client's config file, never on the command line. For Claude
Code that is the user-scope `mcpServers` entry in `~/.claude.json`:

```json
{ "mcpServers": { "unified-agent": {
  "type": "http",
  "url": "http://127.0.0.1:8765/mcp",
  "headers": { "Authorization": "Bearer <token>" } } } }
```

A hub in warn-only mode (`UA_ENFORCE_AUTH=0`) also accepts a
registration without a bearer, but every call is then anonymous. `init` does
not select that mode. Keep it to a single-user loopback hub; startup logs a
warning. This repository does not document how Antigravity sends
a bearer (see [harnesses.md](harnesses.md#register-each-harness)), so its entry
below has none:

```json
// Antigravity — note the key is "serverUrl", not "url"/"httpUrl"
{
  "mcpServers": {
    "unified-agent": { "serverUrl": "http://127.0.0.1:8765/mcp" }
  }
}
```

Remote Desktop/Laptop clients and executors must set `UA_HTTP_HUB_URL` to the
full intended `/mcp` endpoint in their client or executor environment. They do
not derive a hub URL from the local server bind variables and fail closed with
an actionable diagnostic when the URL is missing. A `server_*` process running
on the hub host may use its local loopback bind; an isolated satellite must set
its own explicit URL.

### systemd unit (persistent hub)

A portable template lives at `deploy/unified-agent.service`. It expects the
console script installed by `uv tool install .` at `%h/.local/bin` and uses the
private workspace created by `unified-agent init`; it does not depend on the
checkout path:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/unified-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now unified-agent
loginctl enable-linger $USER   # keep it running without an active login session
```

The unit sets `UA_TRANSPORT=http`, `UA_HTTP_HOST=127.0.0.1`, `UA_HTTP_PORT=8765`, and
`UA_WORKSPACE=%h/.unified_agent/workspace` by default; commented-out lines show how to override
`UA_VAULT_PATH`, disable state sync (`UA_STATE_SYNC=0`), or set
`UA_HTTP_ALLOWED_HOSTS` for remote clients. Logs: `journalctl --user -u unified-agent -f`.

**After merging changes to unified-agent's own code**, restart the hub and reconnect
every host pointed at it (a live session keeps its stale tool snapshot from the initial
handshake — see [troubleshooting.md](troubleshooting.md#stale-stdio-snapshots-after-merges--tool-changes)):

```bash
systemctl --user restart unified-agent
# then reconnect/restart every Claude Code / Antigravity session using it
```

### Scheduled database backup

**Install this on any machine that will hold real state.** `~/.unified_agent/memory.db`
is the single copy of memory notes, the task queue, plans, autonomous-cycle history,
delegations, approvals, the event log, and the agent registry. The vault's `Logs/`
mirror covers memory and tasks only, as derived Markdown — everything else exists
nowhere but that file. Nothing backs it up unless this timer is installed.

```bash
cp deploy/systemd/ua-database-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ua-database-backup.timer
```

The shipped unit's `WorkingDirectory` and `ExecStart` point at a development
checkout (`%h/dev/unified_agent/.venv`). After `uv tool install .`, point them at
the tool environment instead, then reload:

```bash
sed -i "s|^WorkingDirectory=.*|WorkingDirectory=%h|; s|^ExecStart=.*|ExecStart=$(uv tool dir)/unified-agent/bin/python -m unified_agent.backup_runner|" \
  ~/.config/systemd/user/ua-database-backup.service
systemctl --user daemon-reload
```

It runs daily at 02:15 with `Persistent=true`, so a machine that was asleep at 02:15
catches up on its next boot rather than silently skipping the day. Each run writes a
complete snapshot through SQLite's online backup API, verifies its integrity before
the file is moved into place — an interrupted or corrupt copy can never be mistaken
for a good one — and then applies 7-daily / 4-weekly retention.

**If you relocate `UA_HOME` or set `UA_BACKUP_DIR`, put them in
`~/.unified_agent/service.env`, not in one unit.** The backup timer is a separate
service from the hub and inherits nothing from it. Both shipped units now carry
`EnvironmentFile=-%h/.unified_agent/service.env`, so that file is the one place a
path is guaranteed to reach both — setting it on either unit alone is how the two
come to disagree about which database is the real one:

```bash
touch ~/.unified_agent/service.env && chmod 600 ~/.unified_agent/service.env
printf 'UA_BACKUP_DIR=%s\n' /mnt/backup-volume/unified-agent \
    >> ~/.unified_agent/service.env
systemctl --user restart unified-agent ua-database-backup.timer
```

`touch`, not `install -m 600 /dev/null`: that idiom is fine for first-time creation
but copies an empty file over an existing one, so on a host that already keeps
`UA_AGENT_TOKENS` and `UA_ENFORCE_AUTH` there it would discard them — and the restart
on the next line would bring the hub back unauthenticated.

Setting `UA_BACKUP_DIR` only on the hub is the failure this warns about: the hub
would honour it, the backup timer would not, and snapshots would keep landing in the
default `~/.unified_agent/backups/` while you believed they were on the external
volume. A relocated `UA_HOME` is worse — the timer would snapshot the *default*
database, which may not be the one you use, and report `LIVE` for it.

Point `UA_BACKUP_DIR` at a separately mounted or encrypted volume if you want
snapshots to survive losing the disk that holds the database. Note that the hub unit
also documents a `service.env` drop-in for secrets; it is the same file, so
`UA_AGENT_TOKENS` and friends can live alongside these.

**If that path is a mount point, make the unit depend on the mount.** `create_backup()`
creates its target directory when absent, which is right for the default location and
dangerous for an external one: if the volume is not mounted when the timer fires, the
directory is recreated on the underlying disk — the same disk holding the database —
and the run writes there, verifies successfully, and reports `LIVE`. You would have no
off-disk copy while every signal says otherwise. Observed directly:

```
UA_BACKUP_DIR=/…/fake-mount/backups   # nothing mounted there
{"path": "/…/fake-mount/backups/memory-….db", "verification": {"ok": true}}
```

A drop-in makes it fail closed instead:

```bash
mkdir -p ~/.config/systemd/user/ua-database-backup.service.d
printf '[Unit]\nAssertPathIsMountPoint=%s\n' /mnt/backup-volume \
    > ~/.config/systemd/user/ua-database-backup.service.d/require-mount.conf
systemctl --user daemon-reload
```

`AssertPathIsMountPoint=` is checked by the manager that starts the unit, against the
live filesystem, so an unmounted volume fails the run rather than quietly redirecting
it onto the wrong disk. A visible failure in
`systemctl --user status ua-database-backup.service` is the outcome you want here —
and if it goes unnoticed, the backup ages past 26 hours and the doctor check reports
`MISCONFIGURED`.

**Do not reach for `RequiresMountsFor=` here.** It is the obvious choice and it does
not work in this position: it builds a dependency on the *mount unit* for that path,
and these are `systemctl --user` units, while `/mnt/...` is mounted by the system
manager. The user manager has no such unit, the dependency resolves to nothing
enforceable, and the guard silently protects nothing — the same failure it was added
to prevent.

Swap `Assert` for `Condition` (`ConditionPathIsMountPoint=`) if you would rather the
run be skipped than recorded as failed. Conditions are evaluated the same way, so that
form is equally valid here; the tradeoff is only in the reporting, and skipping is
quieter, so the doctor's staleness check becomes your only signal.

Verify it, rather than assuming — the failure mode of a backup is silence:

```bash
systemctl --user start ua-database-backup.service     # run once now
systemctl --user status ua-database-backup.service    # inactive (dead), SUCCESS
systemctl --user list-timers ua-database-backup.timer # sane NEXT time
```

Then confirm the backup is real and recent. **Load the same environment the units
use first** — a bare shell has the default `UA_HOME`/`UA_BACKUP_DIR`, so without this
the check inspects paths the timer never writes to, and can report `DORMANT` for a
healthy backup or `LIVE` for an unrelated default database:

```bash
set -a; [ -f ~/.unified_agent/service.env ] && . ~/.unified_agent/service.env; set +a
uv run python -c "from unified_agent import storage; storage.init_db(); \
from unified_agent.tools import doctor; \
print(next(c for c in doctor.system_doctor()['checks'] if c['capability']=='database_backup'))"
```

(The `init_db()` call is there because `system_doctor` reads the `tasks` table and
raises `no such table: tasks` against a database that has never been created — which
is the state of a machine where the hub has not yet started. `init_db()` is idempotent,
so it is safe on an established install too.)

The authoritative reading is the hub's own, since it is the process holding the
configuration. Any host registered against it can call `system_doctor` as an MCP tool
and get the running service's answer rather than your shell's.

`LIVE` reports the age of the newest verified snapshot. `DORMANT` means none exists —
the timer is not installed or has never fired. `MISCONFIGURED` means the newest one is
corrupt or more than 26 hours old, which is how a quietly stopped timer surfaces
before you need it rather than after.

To prove a restore actually works, run the rehearsal — it backs up, restores into a
throwaway `UA_HOME`, and asserts schema version, table set, and row counts, without
touching real state:

```bash
uv run python scripts/db_restore_rehearsal.py   # 5/5 checks, exit 0
```

The other timers (review pipeline, anomaly check, consolidation, sleep pass,
steward audit, cluster briefing, PR-watch) are optional automation. On Linux
hosts install from [deploy/systemd/README.md](../deploy/systemd/README.md). On
the Mac hub use the LaunchAgent twins under `deploy/mac-hub/` — same
schedules. The first six map into `system_doctor` via `MAC_LAUNCHD_LABELS`;
PR-watch does not.

```bash
MAC_HOME="$HOME"
LA="$HOME/Library/LaunchAgents"
ROOT="$HOME/dev/unified_agent/deploy/mac-hub"
for label in review-pipeline anomaly-check sleep-pass planning-loop steward-audit pr-watch audit-ledger-reanchor; do
  python3 "$ROOT/render_launchagent.py" --home "$MAC_HOME" \
    "$ROOT/com.unified-agent.${label}.plist" "$LA/com.unified-agent.${label}.plist"
  chmod +x "$ROOT/run-${label}.sh"
  launchctl bootstrap "gui/$(id -u)" "$LA/com.unified-agent.${label}.plist"
done
# cluster-briefing retired on the Mac hub, task #1888 (the daily briefing is the
# chief of staff's). Keep it from loading at login:
launchctl bootout "gui/$(id -u)/com.unified-agent.cluster-briefing" 2>/dev/null || true
launchctl disable "gui/$(id -u)/com.unified-agent.cluster-briefing"
# consolidate-proposal retired task #884 — boot out leftover installs:
# launchctl bootout "gui/$(id -u)/com.unified-agent.consolidate-proposal" 2>/dev/null || true
# rm -f "$LA/com.unified-agent.consolidate-proposal.plist"

# Configure and validate sleep-pass credentials before loading that agent.
# The subshell is deliberate: set -e stops a failed check here (not the shell
# you pasted into), and OPENROUTER_API_KEY is cleared first so the check proves
# the file itself supplies a key — not an inherited shell export that launchd's
# environment won't have. The claude check resolves the CLI as the job will:
# UA_CLAUDE_CLI_BIN (from sleep-pass.env, else service.env) is the only
# candidate when set, and a non-executable one counts as missing; otherwise
# the absolute entries of the job's PATH (launchd's /usr/bin:/bin:/usr/sbin:/sbin
# unless sleep-pass.env sets PATH), then ~/.local/bin and Homebrew on Apple
# silicon or Intel. It then requires the flags the leg passes and the
# subscription login it insists on: authMethod claude.ai, apiProvider
# firstParty. It spends nothing.
(
  set -e
  OPENROUTER_API_KEY=""
  UA_CLAUDE_CLI_BIN=""
  chmod 600 "$HOME/.unified_agent/sleep-pass.env"  # enforce, don't assume umask
  test -s "$HOME/.unified_agent/sleep-pass.env"
  SHELL_PATH="$PATH"
  set -a
  . "$HOME/.unified_agent/sleep-pass.env"
  set +a
  test -n "$OPENROUTER_API_KEY"
  JOB_PATH=/usr/bin:/bin:/usr/sbin:/sbin
  if [ "$PATH" != "$SHELL_PATH" ]; then JOB_PATH="$PATH"; fi
  if [ -z "$UA_CLAUDE_CLI_BIN" ] && [ -f "$HOME/.unified_agent/service.env" ]; then
    UA_CLAUDE_CLI_BIN="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}UA_CLAUDE_CLI_BIN=//p' \
      "$HOME/.unified_agent/service.env" | tail -n 1 | tr -d "\"'")"
  fi
  CLAUDE_BIN=""
  if [ -n "$UA_CLAUDE_CLI_BIN" ]; then
    if [ -f "$UA_CLAUDE_CLI_BIN" ] && [ -x "$UA_CLAUDE_CLI_BIN" ]; then
      CLAUDE_BIN="$UA_CLAUDE_CLI_BIN"
    fi
  else
    OLD_IFS="$IFS"; IFS=:
    for d in $JOB_PATH "$HOME/.local/bin" /opt/homebrew/bin /usr/local/bin; do
      case "$d" in /*) ;; *) continue ;; esac  # relative or empty entries are skipped
      if [ -f "$d/claude" ] && [ -x "$d/claude" ]; then CLAUDE_BIN="$d/claude"; break; fi
    done
    IFS="$OLD_IFS"
  fi
  test -n "$CLAUDE_BIN"
  "$CLAUDE_BIN" --help | grep -q -e '--safe-mode'
  "$CLAUDE_BIN" --help | grep -q -e '--permission-prompts'
  "$CLAUDE_BIN" auth status --json | grep -q '"authMethod": *"claude.ai"'
  "$CLAUDE_BIN" auth status --json | grep -q '"apiProvider": *"firstParty"'
  python3 "$ROOT/render_launchagent.py" --home "$MAC_HOME" \
    "$ROOT/com.unified-agent.sleep-pass.plist" "$LA/com.unified-agent.sleep-pass.plist"
  chmod +x "$ROOT/run-sleep-pass.sh"
  launchctl bootstrap "gui/$(id -u)" "$LA/com.unified-agent.sleep-pass.plist"
)
```

`sleep-pass` runs the `llm_client` chain `claude_cli -> openrouter` (task
#2025); neither Ollama nor Google AI Studio is in it. Before enabling it, log
the `claude` CLI in on the hub machine as the hub's user with the claude.ai
subscription (`claude auth login`; a Console or API-key login is refused: the
leg checks `claude auth status` first and falls through when `authMethod` is
not `claude.ai`). The CLI must be new
enough to list `--safe-mode` and `--permission-prompts` in `claude --help`;
an older one fails the leg as `unsupported CLI version`. The job's PATH is
minimal, so the CLI is found at `UA_CLAUDE_CLI_BIN` (when set, the only
candidate), else on the absolute entries of PATH, else in `~/.local/bin`,
`/opt/homebrew/bin` or `/usr/local/bin`. Put
`OPENROUTER_API_KEY` in `~/.unified_agent/sleep-pass.env` (chmod 600) as the
second leg. The commands above validate both before bootstrapping that agent.
`GEMINI_API_KEY` is no longer needed by the pass; Gemini is used only by the
explicit `gemini_ask` tool. `pr-watch` needs the `gh` CLI on PATH (the wrapper prepends
`/opt/homebrew/bin`) and a GitHub login that can list the operator's repos;
see [cli-reference.md](cli-reference.md#unified-agent-pr-watch).
`dispatch-tick` is separate — see
[approved-dispatch-enablement.md](architecture/approved-dispatch-enablement.md).

### Tailscale exposure

Never bind `0.0.0.0`. Keep the bind on loopback and expose it over your tailnet only:

```bash
tailscale serve --bg --tcp 8765 127.0.0.1:8765
```

For any client dialing in over anything other than `localhost` (a Tailscale IP or
MagicDNS name), set `UA_HTTP_ALLOWED_HOSTS` — without it, the MCP SDK's built-in
DNS-rebinding protection answers non-localhost `Host` headers with HTTP 421:

```bash
UA_HTTP_ALLOWED_HOSTS=127.0.0.1:*,localhost:*,desktopname:*,desktopname.<tailnet>.ts.net:*,<desktop-tailnet-ipv4>:*
```

(comma-separated; each entry may carry a port pattern like `:*` for "any port"). See
[configuration.md](configuration.md) for the full variable reference.

## Read reach across project checkouts (task #497)

`UA_WORKSPACE` is the **write** boundary, not the read boundary.

| Tools | Resolve inside |
|---|---|
| `fs_read`, `fs_list`, `fs_search` | `UA_WORKSPACE` **or** any `~/dev/<project>` checkout |
| `fs_write`, `run_cmd`, symbol-index tools, `fs_repo_map`, `read_image` | `UA_WORKSPACE` **union** the active task worktree (task #995) |
| `git_*` | Path may sit in that union; git toplevel must still be inside `UA_WORKSPACE` |

The cockpit workspace holds no product code, so before this an agent asked
about `~/dev/unified_agent` had to shell out or work blind. Widening *reads*
fixes that without handing any agent write access to a live-serving checkout.
A `[code-task]` worktree under `<project>/.worktrees/` is the one extra write
root, and only while it is the calling agent's active worktree — not the
product checkout itself.

Relative paths still resolve against the workspace (or the active task
worktree) for both classes, so reaching a project checkout that is not the
active worktree requires an absolute path. Within the widened read
roots, hook directories, runtime/dependency trees (`.venv`, `node_modules`),
and secret-bearing filenames remain refused.

This is option C of the `UA_WORKSPACE` decision (task #495). Option A — moving
`UA_WORKSPACE` to `~/dev` — was rejected: `resolve_for_audit` unconditionally
prepends the workspace root, so it would have widened write, execute, and audit
scope for every agent and every profile at once.

Review and merge internals may run git against a known project checkout. That
path is never agent-supplied. See [Automation](automation.md#project-sourced-worktrees)
and [Security model](security.md#workspace-sandbox).

## Satellite projects (project-scoped `.mcp.json`)

Within a **single machine**, the agent launch/control workspace (`~/agent-workspace`, or whatever
`UA_WORKSPACE` points at) should be registered **once, user-scoped**, over HTTP as above.
(This is a per-machine convention — it does not mean every machine must join one hub; see
[Multiple machines](#multiple-machines) below.) A satellite project that
needs its *own* isolated workspace root (e.g. `~/dev/sample-myagent`,
`~/dev/sample-myagent-brain`) instead gets a **project-scoped** `.mcp.json` that spawns its
own stdio server with a project-specific `UA_WORKSPACE`, so its tools load only inside
that directory:

```json
{
  "mcpServers": {
    "unified-agent": {
      "command": "/home/you/dev/unified_agent/.venv/bin/unified-agent",
      "args": [],
      "env": { "UA_WORKSPACE": "/home/you/dev/myagent" }
    }
  }
}
```

Avoid adding extra user-scoped stdio copies for satellite projects — each one multiplies
the tool surface in every session and, per the restart requirement above, keeps serving
stale code after a merge until that specific host reconnects.

**A primary-workspace repo must not carry a project-scoped `unified-agent` entry.** The
agent launch/control workspace (`~/agent-workspace`) and this repo (`~/dev/unified_agent`) both did
until 2026-07-27; because project scope outranks user scope, every session launched in
either directory reached the server over stdio and never touched the hub — bypassing
bearer-token auth, the approval queue, and control-center logging, while `claude mcp list`
reported a healthy `unified-agent` the whole time. Both project-scoped entries are now
removed. The tracked `.mcp.json` and `.cursor/mcp.json` files intentionally declare no
project MCP servers; code exploration and review use `codebase-memory-mcp`, while
`unified-agent` resolves from user scope over authenticated HTTP.

To check a repo for the same shadowing, run `claude mcp list` from inside it and read the
**MCP config diagnostics** section — a `[Conflicting scopes]` block naming `unified-agent`
in both `user` and `project` scope means the project stdio entry is winning.

## Multiple machines

The Mac hub is the one HTTP hub. The Desktop (Linux desktop) and the laptop are
remote clients. They connect through Tailscale to the Mac MagicDNS endpoint.
Neither starts a local hub or a local stdio server. Desktop desired state:
`deploy/desktop-bootstrap.sh` and
the operator desktop manual.

| | Mac hub | Desktop | Laptop |
|---|---|---|---|
| transport | HTTP hub (`127.0.0.1:8765/mcp`) plus Tailscale Serve | HTTP client through the Mac MagicDNS endpoint | HTTP client through the Mac MagicDNS endpoint |
| `~/.unified_agent/memory.db` | One authoritative hub database | No local store (`NO_LOCAL_MEMORY_DB`) | Uses the Mac hub through MCP |

The Mac owns the service and database. Desktop and laptop requests use the
same hub tools, identity checks, approval queue, and durable task and memory
state.

A client must not retain a `unified-agent` stdio registration or a local
`unified-agent.service`. Either can create a second database and bypass the
Mac hub controls. Register each client with the Mac Tailscale URL and its
bearer token. On the Desktop, `storage.connect()` fails closed when
`NO_LOCAL_MEMORY_DB` is present so a stray import cannot grow a fresh empty
store.

A client has no `settings.toml` until you create one, so local tools name the
human "Operator". Run `unified-agent settings set operator.name "<your name>"`
once per client; see [Settings](settings.md#client-machines-desktop-laptop).
Do not run `unified-agent init` on a client.

Cross-machine source and reader-facing notes still need deliberate handoff:

1. **A synced notes vault** (here: `~/vaults/main` over Obsidian Sync) — the *prose* channel,
   and the only continuously-syncing one.
2. **A shared git repo** (here: `unified_brain`) — the *code* channel: shared projects and
   device-to-device handoffs.

Rule of thumb: **if it wants a diff, it goes in the repo; if it wants a reader, it goes in the
vault.** Hub memory and tasks are shared operational state. They do not replace
an explicit source or reader-facing handoff.

The `unified-agent-sync` CLI is a narrower third bridge: it writes a marker-scoped `ua-sync`
block of role/task/memory *references* into a workspace's `CLAUDE.md` / `AGENTS.md`. See
[cli-reference.md](cli-reference.md).

## GCP offsite backups + Tailscale worker (optional)

Task #522 adds an **additive** GCP `e2-micro` Tailscale node and optional GCS
copies of local database backups. This is not a second hub: MCP
(`127.0.0.1:8765`), authoritative `memory.db`, the vault, and secrets stay on
the Mac hub. Operator steps and naming live under
[deploy/gcp/](../deploy/gcp/README.md) (CONTRACT.md).

- **Local backups remain required** (`ua-database-backup` timer / `UA_BACKUP_DIR`).
- Optional offsite: set `UA_BACKUP_GCS_URI` in `~/.unified_agent/service.env`
  (live 2026-08-21:
  `gs://ua-worker-backups-000000000000/memory-db`). Unset keeps local-only
  behavior. GCS failure must not fail a good local backup.
- Bootstrap scripts: `deploy/gcp/` — see [gcp/README.md](../deploy/gcp/README.md)
  and CONTRACT.md. Prefer
  `export PATH="$HOME/google-cloud-sdk/bin:$PATH"` and
  `UA_GCP_PROJECT=ua-worker`.
- Tailscale on the worker still needs an interactive `tailscale up` login
  (see `deploy/gcp/ops-state.md`). Do not put Tailscale auth keys or
  service-account JSON in git. Do not expose MCP on public VM ports. Do not
  treat the GCP VM as the canonical hub.

## Next steps

- [configuration.md](configuration.md) — every `UA_*` variable and hard limit.
- [tools-reference.md](tools-reference.md) — what's now available once registered.
- [security.md](security.md) — the sandbox model these registrations rely on.
