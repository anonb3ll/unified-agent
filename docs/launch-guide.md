# Where to launch what

unified-agent is one Python package (`src/unified_agent`) plus a few
JavaScript packages (`packages/`). You do not launch packages; you launch
processes. Most of them are console commands that `uv tool install .` puts on
your `PATH`, and the hub runs the long-lived ones under systemd (Linux) or
launchd (macOS).

The [layer map](architecture/layers.md) refactor did not change any of this.
Every command, `python -m` path and service unit below works as it did before.

## The short version

| You want to | Run | Where | Details |
|---|---|---|---|
| Run the hub: MCP over HTTP, the JSON API and the Control Center | `UA_TRANSPORT=http unified-agent`, normally as `deploy/unified-agent.service` | The hub machine | [Getting started, step 4](getting-started.md#step-4-start-the-hub) |
| Give one satellite repo its own isolated instance | `unified-agent` (stdio is the default transport), with that repo's own `UA_WORKSPACE` and `UA_HOME` | That repo, via its `.mcp.json` | [Installation, Which transport to register](installation.md#which-transport-to-register) |
| Use the Control Center | Browse `http://127.0.0.1:8765/ui` | Any browser that reaches the hub | [Getting started, step 5](getting-started.md#step-5-open-the-control-center) |
| Work on the Control Center itself | `UA_DEV_HUB_URL=<hub> npm run dev` in `packages/web` | Your dev machine (loopback only while the proxy is on) | [packages/web/README.md](../packages/web/README.md), [web-ui.md](web-ui.md#developing-against-a-live-hub) |
| Watch the fleet from a terminal | `unified-agent-tui` | Any shell on the hub | [CLI reference](cli-reference.md#unified-agent-tui) |
| Set up or check a hub | `unified-agent init`, then `unified-agent doctor` | The hub machine | [CLI reference](cli-reference.md#unified-agent-init) |
| Query platform objects from a shell | `unified-agent platform ...` | Anywhere with a hub URL and token | [CLI reference](cli-reference.md#unified-agent-platform) |
| Sign a device in and run work on it | `unified-agent login`, then `unified-agent node` (`deploy/systemd/ua-node.service`) | Each worker device | [CLI reference](cli-reference.md#unified-agent-node) |
| Run the active executor | `unified-agent-executor` (`deploy/unified-agent-executor.service`) | The hub or a worker | [Harnesses: unattended work](harnesses.md#unattended-work-executors) |
| Call the hub from code | SDK: [`ua-rust`](https://github.com/anonb3ll/ua-rust) → `sdk/typescript`. Inside this repo, `@unified-agent/client` in `packages/client` (example in `examples/sdk_consumer`) | Your app | [Building on unified-agent](building-on-unified-agent.md) |
| Use the phone or watch app | The Swift package in `native/UnifiedAgentMobile` | Xcode | [Interfaces](interfaces.md) |

A stdio instance is a separate, local process. It does not go through the
hub's bearer authentication, approval queue or Control Center records, so use
it only for an isolated satellite repo. Never add a `unified-agent` entry to
the `.mcp.json` of this repo or `~/agent-workspace`: project scope outranks user
scope, so it would silently replace the authenticated hub for every session
there. See [installation](installation.md#satellite-projects-project-scoped-mcpjson).

## Everything else, by job

Commands marked *timer* are run on a schedule by the unit named next to them,
from `deploy/systemd/` on Linux. The macOS equivalents are the
`com.unified-agent.*.plist` files and `run-*.sh` scripts in
`deploy/mac-hub/`.

**Work and review**

| Job | Command | Scheduled by |
|---|---|---|
| Review pipeline | `unified-agent-review scan` / `task` | *timer* `ua-review-pipeline` |
| Merge a task branch through its test and audit gate | `unified-agent-merge` | Run by hand, or by the reviewer |
| Bounded bug-to-patch worker | `unified-agent-bug-worker` | Run by hand |
| Weekly plan and decision packet | `unified-agent-planning-loop` | *timer* `ua-planning-loop` |
| Reap orphaned Codex-lane processes | `unified-agent-lane-reaper --kill` | *timer* `ua-lane-reaper` |
| Surface GitHub PR events into memory | `unified-agent-pr-watch` | launchd `pr-watch` |
| Approved dispatch (**retired** 2026-09-28, task #1435) | `unified-agent-dispatch-scheduler`, by hand only | None. The `ua-dispatch-tick` timer and its LaunchAgent are disabled; self-pickup runs and the executors replaced it. |

**Chat and notifications**

| Job | Command | Scheduled by |
|---|---|---|
| Slack ingress | Served by the hub at `/api/slack/events`; nothing to launch | — |
| Zulip worker, digests and event bridge (**retired** 2026-09-29, Slack cutover) | `unified-agent-zulip-worker`, `unified-agent-zulip-automation` | None. Outbound Zulip is refused while `UA_ZULIP_RETIRED` is unset or `1`; the code stays for an approved rollback. |
| Discord and Telegram ingestion | `unified-agent-ts-worker` | service `unified-agent-ts-worker` |
| List or release abandoned Slack send slots | `unified-agent-slack-slots list` / `release` | Run by hand |

**Memory, vault and knowledge**

| Job | Command | Scheduled by |
|---|---|---|
| Nightly memory maintenance | `unified-agent-sleep-pass` | *timer* `ua-sleep-pass` |
| Memory consolidation proposals | `unified-agent-consolidate` | Run by hand |
| Skill-cluster proposals | `unified-agent-evolve` | Run by hand |
| Hub-authoritative vault sync | `python -m unified_agent.vault_sync` (or `unified-agent-vault-sync`) | *timer* `ua-vault-sync` |
| Wiki custodian | `unified-agent-wiki` | Run by hand |
| Sync agent roles and context into a workspace | `unified-agent-sync` | Run by hand |
| Scaffold a satellite project | `unified-agent-new-project` | Run by hand |

**Evidence, audit and data safety**

| Job | Command | Scheduled by |
|---|---|---|
| Security audit of a tree | `unified-agent-audit .` | CI `gates` job, pre-commit hook |
| Verify the audit ledger chain | `unified-agent-audit-ledger` | Run by hand |
| Re-anchor the ledger chain head | `python -m unified_agent.audit.reanchor` | *timer* `ua-audit-ledger-reanchor` |
| Signed evidence export | `unified-agent-evidence-export` (or `python -m unified_agent.audit.evidence_export_sink`) | *timer* `ua-evidence-export` |
| Database backup | `python -m unified_agent.backup_runner` | *timer* `ua-database-backup` |
| Metadata-only cloud mirror | `python -m unified_agent.cloud_mirror` | *timer* `ua-cloud-mirror` |
| Weekly export | `deploy/weekly-export-cron.sh` | *timer* `ua-weekly-export` |
| Anomaly check | `deploy/anomaly-check-cron.sh` | *timer* `ua-anomaly-check` |
| Event bus | `unified-agent-eventbus` (the hub also embeds one) | Run by hand |

## Which layer each process lives in

A module's layer in the [layer map](architecture/layers.md) describes what the
module does, not whether it has a `main()`. Most commands start in the top two
layers:

- **adapters**: `unified-agent` and its subcommands, the Control Center, the
  TUI, Slack ingress, and the `*_cli` commands (`unified-agent-review`,
  `-audit`, `-sync`, `-dispatch-scheduler`, `-bug-worker`, ...). The hub
  process imports `server.py`, which registers every governed tool.
- **runtime**: the executor, the sleep pass, vault sync, the PR watcher, the
  chat workers and the other loops and timer jobs.

A few library modules also carry a thin `main()`, so their command starts in a
lower layer: `unified-agent-merge`, `-wiki` and `-planning-loop` (services),
`-slack-slots` (integrations), and `-eventbus` and
`python -m unified_agent.audit.reanchor` (core). The import rule still holds
for them: they import only from their own layer and below.

A runtime job that calls a governed hub tool does it through `tool_surface`,
which loads the tool surface on first use. The call takes the same policy and
approval path as an MCP call.

## Developing

| You want to | Run |
|---|---|
| Run the tests that cover your change | `uv run python scripts/affected_tests.py --explain`, then `uv run pytest` on the test paths it prints. When stdout is only `ALL`, leave the full suite to CI (`scripts/local-gate.sh --affected` skips pytest on that sentinel). CI still runs the full suite. |
| Lint and format | `uv run ruff check .` and `uv run ruff format --check .` |
| Check import direction | `uv run python scripts/check_layers.py` (`--report` for the tables) |
| Live smoke checks against a temp `UA_HOME` | `make smoke` |
| Everything CI's `gates` and `test` jobs run, locally | `scripts/local-gate.sh` |
| Test a JavaScript package | `npm test` in `packages/protocol`, `packages/client` or `packages/web` |
| Build the Control Center for the hub to serve | `npm run build` in `packages/protocol`, then `packages/client`, then `packages/web` |

When you add a module, place it in a layer in
[`architecture/layers.toml`](architecture/layers.toml). If you add a console
command, add it to `pyproject.toml` and to the tables above.
