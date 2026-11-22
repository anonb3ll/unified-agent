# Frequently asked questions

Short answers to the questions new users ask most, each with a link to the page
that explains it in full. Unfamiliar word? See the [Glossary](glossary.md).

- [General](#general)
- [Installing and running](#installing-and-running)
- [Agents and harnesses](#agents-and-harnesses)
- [Human in the loop and evidence](#human-in-the-loop-and-evidence)
- [Security](#security)
- [Operating](#operating)
- [Building on it](#building-on-it)

## General

### What is unified-agent?

It is a meta harness: one hub that organizes many AI agents, harnesses,
devices, and people. The hub holds identity, shared work, policy, an approval
gate, and a hash-chained evidence ledger of who authorized what. See the
[Overview](overview.md).

### Is it an agent framework? Does it replace Claude Code or Codex?

No. It does not prompt models or plan tool calls, and it does not replace your
IDE or command-line agent. Your harness keeps running the model and keeps its
native tools. The hub adds shared state, one policy, and evidence next to them.
Some executors call a harness's headless command for you, but the harness still
runs the model. See [What it is not](overview.md#what-it-is-not).

### Who is it for?

It is for people who run several agents across several machines and want them
to act as one team. That includes individual developers, engineering teams,
organizations that must show which human authorized agent-produced work, and
builders who want identity, approvals, and audit without writing them from
scratch. See [Who it is for](overview.md#who-it-is-for).

### Does it need the cloud or Tailscale?

No. It is local first. Everything runs on hardware you control, with SQLite as
the store, and there is no hosted service to sign up for. Some features reach
the network when you use them: the `web_search` and `web_fetch` tools, the
one-time download of the embedding model for semantic search, `gemini_ask` when
you set an API key, connectors you turn on, and the timestamp-authority anchor
if you choose it. Tailscale is optional. Use it, or a reverse proxy, only when
other machines must reach a hub that binds to loopback. See
[Deployment topologies](topologies.md).

### Can several people use one hub? Is there a multi-tenant service?

Several people can direct one hub. Each person needs their own identity and
token so the ledger can tell them apart, and people can be grouped into
offices. A hub serves one organization. There is no hosted multi-tenant
service. A separate single-organization WorkOS pilot adapter exists for
identity-provider sign-in. See
[Multiple people](topologies.md#6-multiple-people).

### Does it run on Windows?

The documentation covers Linux and macOS only. The tutorial uses a systemd user
service on Linux, and launchd agents on macOS. Windows is not a documented
platform. See [Getting started](getting-started.md#before-you-begin).

### What license is it under?

The license has not been published yet. The repository has no `LICENSE` file at
the moment, so do not assume any terms until one is added.

### How mature is it?

It is at version 0.3.2 and runs a real multi-agent fleet daily. It is pre-1.0,
so expect some breaking changes between minor versions. The changelog records
each change. See the project status section of the
[README](../README.md#project-status).

## Installing and running

### How do I install it?

You need Python 3.11 or later (the declared floor) and
[`uv`](https://docs.astral.sh/uv/), and git. Development, CI, and the hub
image pin Python 3.14. Building the Control Center from source also needs
Node.js 22.22.2 or newer; CI's JavaScript job uses Node 26.
Clone the repository, run `uv tool install .`, then run `unified-agent init`
to create the database, a workspace, and a bearer token. The full walkthrough
takes about 15 minutes. See [Getting started](getting-started.md) and
[Installation](installation.md).

### stdio or an HTTP hub: which should I use?

Use an HTTP hub for real work. It authenticates each caller with its own token,
serves the Control Center, applies profiles, and gives every agent one shared
state. Use stdio to try the tools, on a machine that will never run a hub, or
for an isolated [satellite](glossary.md#satellite) project that needs its own
workspace. stdio has no authentication and no Control Center, and profiles do
not filter its tools. Do not add a project-scoped stdio entry next to a hub
registration: project scope outranks user scope and silently bypasses
authentication and approvals. See [Deployment topologies](topologies.md) and
[Installation](installation.md#hub-mode-shared--remote).

### Can I run it in Docker?

Yes. The repository has a `Dockerfile` and a compose file, and everything
durable lives in the `/data` volume. Release images are currently hosted in a
private registry, so outside that registry build the image from the repository.
The container enforces authentication (an opt-out exists for debugging only)
and prints its generated token once at first start. See [Installation: Docker](installation.md#docker).

### How do I keep the hub running?

Install the systemd user unit from `deploy/unified-agent.service` and run
`loginctl enable-linger` so it survives logout. On macOS, use the launchd
agents under `deploy/mac-hub/`. Then run `unified-agent doctor` to check
health. See [Getting started](getting-started.md#step-4-start-the-hub).

### How do I upgrade?

Pull the new source and run `uv tool install .` again, because the service runs
the installed command and not the checkout. Then restart the hub with
`systemctl --user restart unified-agent`, and start new harness sessions so
they pick up any changed tools. See [Installation](installation.md#hub-mode-shared--remote).

## Agents and harnesses

### Which harnesses work with it?

Any harness that can call an MCP server over streamable HTTP with a bearer
token can connect. The docs name
Claude Code, Codex, Cursor, GitHub Copilot, Antigravity, Hermes, Devin, and
OpenCode. Executors can also drive several of these through their headless
commands. See [Connecting agents and harnesses](harnesses.md).

### How do I connect an agent?

Give the agent its own token in `UA_AGENT_TOKENS`, then register the hub with
the harness. For Claude Code that is one `claude mcp add` command with an
`Authorization: Bearer` header. Register it once, user-scoped. See
[Getting started, step 6](getting-started.md#step-6-connect-an-agent).

### How many tools are there, and what can agents do?

The tools cover files, commands, git, tasks, plans, memory, the vault,
approvals, evidence, and more. The count changes between releases, so the
[Tools reference](tools-reference.md) is the source of truth. What one session
can see depends on its [profile](concepts.md#profiles): the default `developer`
profile is capped at 100 tools, and the allow-list profiles (`research`,
`operator`, and `public`) expose fewer. See [Architecture](architecture.md#operational-profiles).

### Why does my agent not see the new tools?

A harness reads the tool list once, when a session starts. Restarting the hub
does not refresh a session that is already open. After you register the hub,
upgrade it, or merge a tool change, restart the hub if needed and then start a
new harness session. See
[Troubleshooting](troubleshooting.md#stale-stdio-snapshots-after-merges--tool-changes).

### Can two agents work on the same repository without colliding?

Yes. A task claim is atomic, so two agents cannot take the same task. Each
claim carries a lease that the worker renews. Code work happens in a separate
git worktree per task, so agents never share a working directory. See
[Tasks, claims, and worktrees](concepts.md#tasks-claims-and-worktrees).

### Does the hub run my agents for me?

Only if you set that up. Agents can pick up tasks themselves through their own
harness. Optionally, an [executor](glossary.md#executor) daemon or a
[ua-node](glossary.md#ua-node) worker on a device claims approved work,
prepares a worktree, and calls a harness's headless command. The hub never
runs the model itself. See
[Runs, executors, receipts, and routines](concepts.md#runs-executors-receipts-and-routines).

### Do my agents share memory?

Yes. Memory is one shared store of attributed notes with full-text and
semantic search, so an agent can find what another decided yesterday. A guard
stops one agent from silently overwriting another agent's note. Semantic
search needs an embedding model, which downloads on first use. See
[Memory, tasks, and vault](memory-tasks-vault.md#memory).

## Human in the loop and evidence

### How does approval work?

You choose which tools need approval. A policy rule with `require_approval`
gates any tool. `UA_APPROVAL_TOOLS` is a shortcut for the tools whose code
consults it, such as `run_cmd` and `fs_write`. Listing another tool there does
not gate direct calls to it. A call to a gated tool does not run. It parks as pending, and you see the exact
tool and arguments in the Control Center. When a named human approves, the hub
creates a one-time pass bound to that exact request, and the agent's retry
spends it. See [The approval gate](human-in-the-loop.md#the-approval-gate).

### Why can't I approve a request?

The hub treats exactly one login as human: the configured operator,
`operator.label` in `settings.toml` (`UA_OPERATOR_LABEL`, default
`Desktop_Operator`). Its `operator.name` and `operator.aliases` also count.
`approval_resolve` and the Control Center refuse approvals from any other
identity with `approval resolution requires a human identity`. The Control
Center applies the check to denials too.

Set the operator with `unified-agent init --operator-label <label>` or
`unified-agent settings set operator.label <label>`, then sign in to the
Control Center with that label's bearer token. Keep the operator label out of
`UA_AGENT_LABELS`: a label or alias that names a catalogued or configured
worker is refused, so configuration cannot promote an agent to a human. See
[Settings](settings.md#the-human-operator).

Two other refusals look similar: the approver may not be the requester, and an
approval needs a named approver.

### Can an agent approve its own request?

No. `approval_resolve` refuses an approval by the identity that made the
request, and it refuses an anonymous approver, because without a name
separation of duties cannot be shown. Approval also needs a human identity, and
an agent identity is not one. For work product release the submitter can deny
its own request but can never approve it, under any profile. Naming the
operator in a tool argument does not change that: self-approval at the gate
is keyed off the transport-verified operator identity, for two named tools
only (`task_delete` and `langgraph_governed_call`). See
[Separation of duties](human-in-the-loop.md#separation-of-duties) and
[operator self-approval](human-in-the-loop.md#the-approval-gate).

### What does the evidence report prove, and what does it not?

It proves that the event hash chain is intact. It shows which credential
signed each approval or delegation outcome, whether the chain head has an
external anchor, and, for a signed export, that the file was not altered
afterwards. It does not prove that an outside effect happened: a
[receipt](glossary.md#receipt) shows that the hub recorded a request, and
only verified evidence shows what occurred. It does not treat an agent's valid
signature as human sign-off: `human_authority` is `credential-attributed`
only when a credential bound to a human identity signed, and `unknown`
otherwise. Even then it proves the credential, not a present human. A hostname alone is
never a verified device. `clean_pass` is a platform-wide gate, not an
attestation about one row. See
[Reading an evidence report](human-in-the-loop.md#reading-an-evidence-report)
and [Evidence report](evidence-report.md).

### Can the operator tamper with the ledger?

Editing, removing, or reordering events breaks the hash chain, and
`unified-agent audit verify` detects it. An operator who rewrites the whole
database and recomputes every hash is not detectable from the chain alone.
That is what [anchors](glossary.md#anchor) are for. An anchor records a chain
head somewhere the operator does not solely control: a timestamp authority
(`tsa`), a separate signed git repository (`git`), or a locked vault note
(`vault`, the default). The `vault` backend is external but is not independent
of the operator, so prefer `tsa` or `git` when that matters. An anchor covers
history up to its head, so re-anchor often. See
[The evidence ledger](human-in-the-loop.md#the-evidence-ledger).

### Can I approve from my phone?

Yes, if the phone can reach the hub. The Control Center is a web app served by
the hub, so a phone on your tailnet can open it and approve. Sign in with the
human operator's token, as on a desktop. See
[Multi-device fleet](topologies.md#3-multi-device-fleet).

### How do I stop a runaway agent?

Use several controls, from narrow to broad:

- **Pause or terminate the agent.** On the agent's page in the Control Center
  (`/ui/agents/profile/<label>`), or with
  `coordination_manage` (`agent_pause`, `agent_terminate`). A paused agent
  cannot claim work or renew leases. An executor's child process is stopped
  within about 15 seconds. Terminating also returns the agent's claimed tasks to
  `open`. For an interactive harness session the hub controls no process, so
  also stop the session in the harness itself. A pause does not revoke the
  agent's token. Only operators listed in `UA_LIFECYCLE_OPERATORS` may change
  another agent, and the default list holds only the reference deployment's
  operator labels, so set it to include your own operator identity. An agent may
  pause itself. See
  [Agent lifecycle](agent-lifecycle.md).
- **Kill the autonomous cycle.** `autonomous_cycle_kill` is never
  approval-gated. It stops new work from starting and bounds an in-flight run
  by its time budget. It does not send an instant signal. An executor that
  cannot read the kill switch stops.
- **Set budgets.** A `block` budget refuses new claims once spent. It does not
  stop a run already in flight. See [Budgets](budgets.md).
- **Cut its access.** Remove its token from `UA_AGENT_TOKENS` and restart the
  hub.

To prevent the problem, gate risky tools and set an
[autonomy mode](autonomy-modes.md).

### What are autonomy modes, and do I need them?

They set, per mission, how much a human sees and must acknowledge:
`observe`, `exception`, `checkpoint`, or `action-review`. A mission with no
mode set starts at the strictest, `action-review`. You do not need them to use
the approval gate. Modes add pauses on top of the other gates and never remove
one. See [Autonomy modes](autonomy-modes.md).

## Security

### Is it safe to give agents shell access?

The hub limits the damage but does not make a model safe on its own. `run_cmd`
runs without a shell, only from an allowlist, and only inside the workspace
sandbox. An allowlist does not make a command's arguments safe, and an approval
never bypasses the sandbox. Put `run_cmd` in `UA_APPROVAL_TOOLS` so a human
sees each command first. See
[Shell allowlist and no-shell execution](security.md#shell-allowlist-and-no-shell-execution)
and [Workspace sandbox](security.md#workspace-sandbox).

### How does authentication work, and can I expose the hub to the internet?

Each identity has its own bearer token in `UA_AGENT_TOKENS`, and the hub
attaches the verified identity to every call. Set `UA_ENFORCE_AUTH=1`
explicitly. Keep the token file (`service.env`) at mode 600 and out of version
control. Bind the hub to loopback and never to `0.0.0.0`. Reach it from other
machines through a tailnet or a reverse proxy. Do not expose it directly to the
internet. See [Hub token authentication](security.md#hub-token-authentication).

### How do I rotate a token?

Edit the token in `UA_AGENT_TOKENS` in `~/.unified_agent/service.env` and
restart the hub. That invalidates the old value. Update the harness that used
it. See the [Security model](security.md).

### How do I report a vulnerability?

Report it privately. Do not open a public issue. The process is in
[SECURITY.md](../SECURITY.md).

## Operating

### What does DORMANT mean in the doctor report?

`unified-agent doctor` (the `system_doctor` tool) gives each capability one
status:

- **LIVE.** Configured and healthy.
- **DORMANT.** An optional feature is not switched on or not installed on this
  host. This is normal on a fresh install, and it is not a failure.
- **DEGRADED.** Configured, but its check observed an unhealthy state.
- **MISCONFIGURED.** Set up wrongly or inconsistently.
- **UNKNOWN.** The check could not tell, for example because a probe was
  unavailable.

The command exits non-zero on `MISCONFIGURED` or `DEGRADED`. Add `--strict` to
fail on `DORMANT` and `UNKNOWN` too. See [CLI reference](cli-reference.md).

### Where is my data stored?

In `UA_HOME`, which defaults to `~/.unified_agent`. The database is
`memory.db`, a single SQLite file that holds memory, tasks, plans, approvals,
delegations, and the event log. `service.env` and the backups live there too.
A new `UA_HOME` is created mode 0700 even under umask 0002; the hub refuses
to open the store under a group- or world-writable parent (`chmod 700`
that directory) because other users could replace the database or WAL files.
The workspace (`UA_WORKSPACE`) and your vault are separate directories. See
[Configuration](configuration.md).

### How do I back it up?

Install the daily backup timer from `deploy/systemd/ua-database-backup.timer`.
It takes an integrity-checked online snapshot and keeps 7 daily and 4 weekly
copies. You can also run `unified-agent db backup` by hand and restore with
`unified-agent db restore <file>`. Rehearse a restore at least once. See
[Scheduled database backup](installation.md#scheduled-database-backup) and
[Database operations](database-operations.md).

### How do I see what agents are doing and what they cost?

The Control Center shows the task queue, sessions, approvals, devices, and
costs, and it updates live. Cost comes from run records, and budgets can stop
an agent from claiming more work. See [Web control center](web-ui.md) and
[Budgets](budgets.md).

### Something is wrong. Where do I start?

Run `unified-agent doctor` and read any `MISCONFIGURED` or `DEGRADED` rows.
Call `profile_status` from the agent to check its identity and profile. Then
see [Troubleshooting](troubleshooting.md).

## Building on it

### Can I add my own tools, workflows, or connectors?

Yes. Tools are Python functions registered in the server. Workflows are
reviewed YAML files whose steps call registered tools, so they pass through the
same gates. A connector ships a manifest that a conformance harness checks. See
[Building on unified-agent](building-on-unified-agent.md).

### Is there an API or an SDK?

Yes. The hub serves authenticated HTTP/JSON routes, and the repository
includes a typed TypeScript client in `packages/client`. Both call the same
registered functions under the same policy as MCP. There is also a CLI and a
terminal dashboard (`unified-agent-tui`). See
[Ways to use unified-agent](interfaces.md),
[Building on unified-agent](building-on-unified-agent.md), and the
[Tools reference](tools-reference.md).

### How do I contribute?

Read [CONTRIBUTING.md](../CONTRIBUTING.md) and the
[developer guide](developer-guide.md) before you open a pull request.
