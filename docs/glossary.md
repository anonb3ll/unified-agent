# Glossary

This page defines the terms you meet in the `unified-agent` documentation, in
alphabetical order. Each entry is short and links to the page that explains
the idea in depth. For the ideas as a connected story, read
[Concepts](concepts.md).

[A](#a) · [B](#b) · [C](#c) · [D](#d) · [E](#e) · [F](#f) · [G](#g) ·
[H](#h) · [I](#i) · [K](#k) · [L](#l) · [M](#m) · [O](#o) · [P](#p) ·
[R](#r) · [S](#s) · [T](#t) · [U](#u) · [V](#v) · [W](#w)

## A

### Action class

A label for a kind of side effect, such as `merge`,
`deploy`, or `external_write`. An [autonomy mode](#autonomy-mode) decides per
action class whether work proceeds quietly or pauses for a human. An action
class is not always a tool name. See
[Autonomy modes](autonomy-modes.md#action-classes-not-tool-names).

### Adapter

A way into the hub: MCP, HTTP/JSON, the CLI and TUI, the
[Control Center](#control-center), and the TypeScript SDK. Every adapter calls
the same registered functions through the same policy wrapper, so a rule
applies whichever way a request arrives. See
[Concepts](concepts.md#the-hub-and-its-adapters).

### Agent identity

The verified name behind every call, such as
`agent-claude` or `ci_reviewer`. On an authenticated hub each identity maps
to its own bearer token or OAuth grant, and the hub attaches it to the call.
An agent cannot choose it by typing a different name. See
[Concepts](concepts.md#identities-agents-people-and-devices).

### Agent lifecycle

The `active`, `paused`, or `terminated` state of an
agent. A paused or terminated agent cannot claim work or renew a lease.
Terminating also releases the agent's claims. See
[Agent lifecycle](agent-lifecycle.md).

### Anchor

A record of one [chain head](#chain-head) kept somewhere the
operator does not solely control, so that rewriting the whole database can be
detected. See [Evidence report](evidence-report.md) and
[Human in the loop](human-in-the-loop.md#the-evidence-ledger).

### Anchor backend

Where an anchor is kept. There are three: `vault` (a
locked note in your vault, and the default), `tsa` (an RFC 3161 timestamp
authority), and `git` (a commit to a separate repository).
`UA_EVIDENCE_ANCHOR_BACKEND` selects one. The `vault` backend is an external
record but is not independent of the operator. See
[Evidence report](evidence-report.md).

### Approval gate

A hub-side queue that parks a gated call instead of
running it. A named human approves or denies that exact request. A call enters
the gate through a policy rule with `require_approval` (any tool), through
`UA_APPROVAL_TOOLS` (any listed registered tool; a tool that gates itself,
such as `gcloud_ops`, applies its own per-action rules), through the prod-action
gate, or through a
built-in high-impact list. See
[Human in the loop](human-in-the-loop.md#the-approval-gate).

### Approval pass (one-time)

What an approval creates. A pass is bound to one
exact tool and payload, and the caller's retry spends it. A changed argument
needs a new approval. An unused pass expires after
`UA_APPROVAL_PASS_TTL_HOURS` (default 24) and can be revoked before it is
used. See [Human in the loop](human-in-the-loop.md#the-approval-gate).

### Attribution coverage

A report-level gate. It passes when at least 95% of
recent completions (a trailing 30-day window by default) carry the identity
that claimed them. It covers the whole platform, not one task. See
[Reading an evidence report](human-in-the-loop.md#reading-an-evidence-report).

### Autonomous cycle

A bounded development loop with its own state machine
(`plan`, `delegated`, `executing`, `validating`, `reviewing`, `rework`, `pr`,
`blocked`, then `completed`, `failed`, or `killed`), budgets, and a kill
switch. Killing a cycle stops new work from starting. It does not send a
signal to a running process. See [Automation](automation.md).

### Autonomy mode

A per-mission setting for how much a human sees and must
acknowledge: `observe`, `exception`, `checkpoint`, or `action-review`. Modes
add pauses on top of the other gates and never remove one. A mission with no
mode set starts at `action-review`. See [Autonomy modes](autonomy-modes.md).

## B

### Backend (executor)

The part of an [executor](#executor) that turns a
prompt into a run inside a prepared worktree, by calling a harness's own
headless command. The daemon owns the lifecycle. The backend only runs the
prompt. See [Concepts](concepts.md#runs-executors-receipts-and-routines).

### Broadcast

A memory note tagged `broadcast` for changes that agents must
act on right away. Only a verified human identity may save one. Agents are
told to search for broadcasts at the start of a session. See
[Human in the loop](human-in-the-loop.md#recording-human-decisions).

### Budget

A spending ceiling for an agent, declared in the policy file. A
`block` budget refuses new claims once it is spent. A `warn` budget only
reports. Budgets stop new claims and do not kill a run already in flight. See
[Budgets](budgets.md).

## C

### Chain head

The hash of the newest event on the [evidence
ledger](#evidence-ledger). Each event includes the hash of the one before it,
so the head commits to the whole history. An [anchor](#anchor) witnesses one
head. See [Concepts](concepts.md#events-and-the-evidence-ledger).

### Claim

The atomic act of taking a task. Two agents cannot both claim the
same task. A claim carries a [lease](#lease). Paused and terminated agents, and
agents over a `block` budget, cannot claim. See
[Concepts](concepts.md#tasks-claims-and-worktrees).

### `clean_pass`

An evidence report field. It is true when the event hash
chain verifies and [attribution coverage](#attribution-coverage) passes. It is
a platform-wide gate, not an attestation about one row. See
[Reading an evidence report](human-in-the-loop.md#reading-an-evidence-report).

### Config revision

An immutable record of a change to the policy, profile,
approval-rule, or org documents: who changed what, when, and why. Rollback
adds a new revision and never deletes history. See
[Config revisions](config-revisions.md).

### Connector

A link between the hub and an outside system, such as GitHub,
Slack, Discord, Telegram, or an evidence-export sink. Each connector declares
a [manifest](#manifest). Connectors are off by default. See
[Connectors](connectors.md).

### Control Center

The hub's web app for people, served at `/ui`. It shows the
task queue, approvals, sessions, evidence, provenance, devices, and more, and
every button calls the same gated functions that agents call. See
[Web control center](web-ui.md).

## D

### Delegation

A request from one agent to another to do a piece of work. It
has an audit trail and a ledger of outcomes. An executor can turn an approved
delegation into a [run](#run). See
[Concepts](concepts.md#plans-delegations-and-handoffs).

### DEGRADED

A [doctor](#doctor-system_doctor) status. The capability is
configured and its health check observed something unhealthy. `unified-agent
doctor` exits non-zero on it.

### Device

A machine in the fleet. A device can sign in with a [device
login](#device-login) and run a [ua-node](#ua-node) worker. The Control
Center's Agents → Hosts page (`/ui/agents/hosts`) lists each host. See
[Concepts](concepts.md#identities-agents-people-and-devices).

### Device login

`unified-agent login`. It uses the OAuth device authorization
grant: the device shows a short code, a verified human approves it in any
browser, and the device receives a refreshable, device-bound token. No bearer
is copied between machines. It needs `UA_MCP_OAUTH=1` on the hub. See
[Agent access](agent-access.md#device-sign-in-oauth-device-flow).

### Doctor (`system_doctor`)

A read-only check of every config-gated
capability. It runs as the `system_doctor` tool, the `unified-agent doctor`
command, and in the Control Center. Each row has one status: [LIVE](#live),
[DORMANT](#dormant), [DEGRADED](#degraded), [MISCONFIGURED](#misconfigured), or
[UNKNOWN](#unknown). See [CLI reference](cli-reference.md) and
[Troubleshooting](troubleshooting.md).

### DORMANT

A [doctor](#doctor-system_doctor) status. An optional capability
is not switched on or not installed on this host. That is normal on a fresh
install and is not a failure. `unified-agent doctor --strict` treats it as one.

## E

### Edition

The `UA_EDITION` setting, `enterprise` (the default) or `core`.
`core` hides the evidence layer: evidence reports, the hash-chain audit, and
re-anchoring. Everything else stays available. See
[Open-core architecture](open-core-architecture.md).

### Event log

The append-only table (`event_log`) where every significant
action is recorded as an event. Each event holds the hash of the previous one.
It is the base of the [evidence ledger](#evidence-ledger).

### Evidence ledger

The hash-chained event log plus the layers built on it:
[signed producer events](#signed-producer-event), external
[anchors](#anchor), and signed exports. It records who authorized what and
resists tampering. See
[Human in the loop](human-in-the-loop.md#the-evidence-ledger).

### Evidence report

A read of the ledger as the five-link chain **human ->
device -> agent -> action -> proof**. It exports as JSON, Markdown, text, CSV,
or PDF, and is served at `/api/evidence`. It keeps a receipt apart from
verified evidence, and an actor apart from a human authority. Run it with
`unified-agent evidence`. See [Evidence report](evidence-report.md).

### Executor

A daemon that turns approved work into a [run](#run). It claims
the work, prepares a worktree, calls a harness's headless command through a
[backend](#backend-executor), renews the lease, applies retry policy, and marks
the result for review. It reads its kill switch before each dispatch and stops
if it cannot read it. See [Automation](automation.md).

## F

### Fail closed

When something needed for a decision is missing or broken, the
answer is no. A missing identity, an unparseable policy file, or a signature
that does not verify is a denial, never a silent pass. See
[Overview](overview.md#design-principles).

## G

### Goal

A higher-level outcome that tasks roll up to. Each goal keeps a
why-chain from its tasks up to the goal. A goal is also called a
[mission](#mission). Manage goals with `goal_manage`. See
[Concepts](concepts.md#plans-delegations-and-handoffs).

## H

### Handoff

Passing a task specification to another agent, with a deadline
and priority. A plan step can also hand its outputs to the next step. See
[Concepts](concepts.md#plans-delegations-and-handoffs).

### Harness

The program an AI agent runs inside, such as Claude Code, Codex,
Cursor, GitHub Copilot, Antigravity, Hermes, Devin, or your own agent loop.
The hub does not replace a harness. See [Overview](overview.md#what-it-is) and
[Connecting agents and harnesses](harnesses.md).

### Human authority

An evidence report finding, `human_provenance.human_authority`.
It is `credential-attributed` when a credential bound to a human identity
signed the claim; that proves the credential, not a present human.
`presence-verified` is reserved for statement-bound passkey assertions. It is
`unknown` for an agent's signature, however valid, and `conflict` when the
evidence disagrees with itself. See
[Credential-attributed human authority](human-in-the-loop.md#credential-attributed-human-authority).

### Human identity

An identity the hub classifies as a person. Only a human
identity may approve a request. In 0.3.2 the hub recognizes human identities
from a built-in list rather than from configuration. See
[Getting started](getting-started.md#step-3-define-who-is-who).

### Hub

One `unified-agent` process with one SQLite database. It is the
single authority for identity, policy, work, and evidence. See
[Overview](overview.md) and [Architecture](architecture.md).

## I

### Identity

See [agent identity](#agent-identity) and [human
identity](#human-identity).

## K

### Kill switch

A stop control for autonomous work. `autonomous_cycle_kill`
forces a cycle to `killed`, is never approval-gated, and stops new work from
starting. An executor that cannot read the switch stops. See
[Automation](automation.md) and
[Agent lifecycle](agent-lifecycle.md).

## L

### Lane

A task field that says who acts on the task, such as `default`,
`dispatch-ready`, a human-only lane, or an agent label. Lanes route work and
can carry work-in-progress caps. See
[Memory, tasks, and vault](memory-tasks-vault.md#tasks).

### Lease

The time limit on a [claim](#claim). The default is one hour. The
worker renews it while it works. When it lapses, the task can be recovered.
See [Concepts](concepts.md#tasks-claims-and-worktrees).

### LIVE

A [doctor](#doctor-system_doctor) status. The capability is
configured and its health check passes.

## M

### Manifest

For a [connector](#connector), a declaration of its name,
version, capabilities, required secret names, and entry points. A conformance
harness checks that it matches the code. A manifest is never a live grant.
See [Connectors](connectors.md#the-manifest). A *migration manifest* is a
separate thing: an export of hub state for moving to another machine. See
[Tools reference](tools-reference.md).

### MCP

The Model Context Protocol, the way agent harnesses call tools. MCP is
one of the hub's adapters, served over [stdio](#stdio) or [streamable
HTTP](#streamable-http). See [Ways to use unified-agent](interfaces.md) and
[Concepts](concepts.md#the-hub-and-its-adapters).

### Memory

A shared store of attributed notes with tags, salience, optional
expiry, and hybrid full-text and semantic search. A guard stops one agent from
silently overwriting another agent's note. Use memory for facts meant for
agents and the [vault](#vault) for writing meant for people. See
[Memory, tasks, and vault](memory-tasks-vault.md#memory).

### Meta harness

A layer above the harnesses your agents already run in. It
does not run the model. It gives every harness the same identity, shared
work, one policy, a human in charge, and proof. See
[Overview](overview.md).

### MISCONFIGURED

A [doctor](#doctor-system_doctor) status. A capability that
should work is set up wrongly or inconsistently. `unified-agent doctor` exits
non-zero on it.

### Mission

Another name for a [goal](#goal), used with [autonomy
modes](#autonomy-mode). A mission is a goal id. See
[Autonomy modes](autonomy-modes.md).

## O

### Office

A named group in the org chart (`org.yaml`) with a lead, members,
decision rights in plain language, and default lanes. Offices let you route
work by role. `system_doctor` flags an active identity that belongs to no
office. See [Concepts](concepts.md#offices).

### Operator

The person who runs the hub. The word also names the `operator`
[profile](#profile), the Control Center login, which can resolve approvals and
read status but cannot write the workspace or vault. See
[Concepts](concepts.md#profiles).

## P

### Plan

A stored list of steps with owners, dependencies, checkpoints, and
outputs. The hub stores a plan and does not run it. Agents claim steps
atomically and hand outputs to the next step. See
[Concepts](concepts.md#plans-delegations-and-handoffs).

### Policy

A YAML file of rules that allow, deny, or require approval for a
call by tool, role, or payload threshold, plus rate limits and scope
bindings. Deny beats allow. `UA_POLICY_FILE` selects it. A file that cannot
be parsed denies the call. See
[Policy rules](human-in-the-loop.md#policy-rules).

### Prod-action gate

A second gate for `merge`, `deploy`, `delete`, and
`restart` (`git_merge_task`, `task_delete`, `fleet_host_action`, and a
reserved `deploy`). For an identified caller, it refuses a revoked agent and,
when `UA_AGENT_SCOPES` is set, an agent without that scope. It then forces a
one-time approval even if the tool is not in `UA_APPROVAL_TOOLS`. See
[Security model](security.md).

### Profile

A capability overlay that decides which tools a session can see
and call. A profile is not an identity. It applies to authenticated HTTP
sessions only. See [Concepts](concepts.md#profiles) and
[Architecture](architecture.md#operational-profiles). The six profiles are:

- **`developer`.** A deny list that keeps coding tools and hides high-impact
  governance, merge, and notify tools. It is capped at 100 tools and is the
  default for engineering identities.
- **`steward`.** A broader deny list for reviewers and custodians. It cannot
  write the workspace or claim tasks.
- **`chief-of-staff`.** The full tool surface, for a coordinating agent.
- **`operator`.** An allow list of approvals and read-only status. It is the
  Control Center login.
- **`research`.** An allow list with no execution, no workspace writes, and no
  claims.
- **`public`.** The tightest allow list: task and project status reads, plus
  filing a task proposal. It is for a client reachable from the internet.

### Project

A named stream that partitions the task queue. A `[project-spec]`
task rolls up the status of its whole subtree. See
[Memory, tasks, and vault](memory-tasks-vault.md#tasks).

### Provenance graph

The evidence data served as nodes and edges (`human`,
`device`, `agent`, `task`, `action`, `approval`, `work_product`). It answers
"what did this person authorize?" and "what stands behind this release?". See
[Provenance graph](provenance-graph.md).

## R

### Receipt

The hub's record that a run was dispatched and how it ended.
Receipts are idempotent. A receipt proves that the hub recorded a request. It
does not prove that the outside effect happened. See
[Concepts](concepts.md#runs-executors-receipts-and-routines).

### Roster

The set of agent labels the hub accepts. With `UA_AGENT_LABELS`
unset, any well-formed label is accepted. Once it is set, the roster is
closed and an unknown label cannot send or receive work. See
[Configuration](configuration.md).

### Routine

Recurring, tracked work with an intake policy, fired on a
schedule or over HTTP. Routines are off until `UA_ROUTINES_ENABLED=1`. See
[Routines](routines.md).

### Run

One execution of a task by an agent, with its outcome, time, and
usage (provider, model, tokens, cost). A run comes from an executor, a
[ua-node](#ua-node) worker, or an agent that picks up work itself. See
[Self-pickup runs](self-pickup-runs.md) and
[Concepts](concepts.md#runs-executors-receipts-and-routines).

## S

### Sandbox

The boundary for file and command tools. Paths resolve inside
`UA_WORKSPACE` or the caller's active task worktree, symlinks are resolved
before the check, and commands run without a shell from an allowlist. An
approval never bypasses it. See
[Security model](security.md#workspace-sandbox).

### Satellite

A project or disposable environment that runs its own stdio
instance with its own `UA_WORKSPACE`. Its state is separate from the hub's and
is not governed by the hub's approvals. See
[Topologies](topologies.md#4-isolated-satellites-and-cloud-sessions).

### Separation of duties

The identity that asked for something cannot be the
one to approve it. `approval_resolve` refuses an approval by the requester and
refuses an anonymous approver. For work product release, the submitter can
deny its own request but can never approve it. See
[Human in the loop](human-in-the-loop.md#separation-of-duties).

### Signed producer event

An event written when an approval is resolved or a
delegation completes. It carries an HMAC over the verified identity and the
approval or delegation. It is keyed with that identity's `UA_AGENT_TOKENS`
token. A WorkOS session uses `WORKOS_COOKIE_PASSWORD` instead, a secret the
operator configures on the hub. When a token-keyed call arrives over the
tailnet, the event also records the device. The hash chain protects that
record, not the signature. The event proves which credential produced the
claim, not that a human stood behind it. See
[Evidence report](evidence-report.md).

### Standing grant

An exemption on a mission that lets one [action
class](#action-class) proceed without pausing in `checkpoint` or
`action-review` mode. Only a human with authority over the mission can grant
or revoke one. See [Autonomy modes](autonomy-modes.md#who-may-change-the-mode).

### stdio

The MCP transport where a harness starts a private `unified-agent`
process. It has no authentication and no Control Center, and profiles do not
filter its tools. Use it for trials and [satellites](#satellite). See
[Topologies](topologies.md#1-solo-stdio).

### Streamable HTTP

The MCP transport of a long-running hub, at `/mcp`. Many
clients share one hub, each with its own token. Only an HTTP hub can
authenticate callers and serve the Control Center. See
[Topologies](topologies.md#2-shared-hub-on-one-machine).

## T

### Tailnet

A private network made by Tailscale. It is optional. It lets
devices reach a hub that binds only to loopback, and it lets the hub identify
the calling device. See [Topologies](topologies.md#3-multi-device-fleet).

### Task

A durable unit of work with a title, notes, priority, project,
optional parent, lane, and one of six statuses: `open`, `doing`,
`waiting_review`, `blocked`, `done`, or `cancelled`. See
[Concepts](concepts.md#tasks-claims-and-worktrees).

- **`open`.** Not yet claimed.
- **`doing`.** Claimed and under a lease.
- **`waiting_review`.** Work submitted and awaiting review.
- **`blocked`.** Waiting for input.
- **`done`.** Reviewed and finished.
- **`cancelled`.** Abandoned.

### Template pack

A YAML file that gives [work product](#work-product)
release its vertical-specific rules: the reviewer role, required tags, and
release subfolder. Two synthetic packs ship with the package. Point
`UA_WORK_PRODUCT_TEMPLATE_DIR` at your own. See
[Work product release](work-product-release.md).

## U

### ua-node

A worker that runs on a device with `unified-agent node`. It
connects out to the hub, reports the device's OS and installed harnesses,
heartbeats, and runs dispatched work. It listens on no port. See
[CLI reference](cli-reference.md) and
[Topologies](topologies.md#3-multi-device-fleet).

### ua-sync block

A generated section of `AGENTS.md` and `CLAUDE.md`, between `ua-sync:begin`
and `ua-sync:end` HTML comment markers, written by `unified-agent-sync`. It
gives an agent its role and project. Edits inside the block are overwritten
the next time the command runs. See [CLI reference](cli-reference.md).

### UNKNOWN

A [doctor](#doctor-system_doctor) status. The check could not
determine the state, for example because a probe was unavailable.

## V

### Validate and hold

The rule for the review pipeline. It runs tests, audits,
and checks on submitted work, records the verdict, and holds the work. It never
merges, pushes, or opens a pull request. See
[Human in the loop](human-in-the-loop.md#review-validate-and-hold).

### Vault

An Obsidian-style folder of Markdown notes. Agents read it anywhere
and write only in configured folders. A note marked `locked: true` refuses
writes unless forced. Use the vault for writing meant for people. See
[Memory, tasks, and vault](memory-tasks-vault.md#vault-obsidian-bridge) and
[Security model](security.md#vault-sandbox).

## W

### Work product

A document produced by agent work, such as a report or a
filing, as opposed to a code change. Release always waits for a named human
who is not the submitter, and the chain gains a model and a document hash. It
is off until `UA_WORK_PRODUCT_ENABLED` is set. See
[Work product release](work-product-release.md).

### Workflow

A reviewed YAML file of steps. Each step calls a registered
tool, so a workflow passes through the same gates as any other call. See
[Concepts](concepts.md#runs-executors-receipts-and-routines).

### Workspace

The directory in `UA_WORKSPACE` that is the sandbox root for
file and command tools. It is separate from the hub's data directory,
`UA_HOME`, which holds `memory.db`. See
[Security model](security.md#workspace-sandbox).

### Worktree

A separate git checkout on its own branch, created per task when
an agent starts code work, so agents never share a working directory. See
[Concepts](concepts.md#tasks-claims-and-worktrees).
