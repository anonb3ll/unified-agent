# Fleet console: run the day from the Control Center

This guide is the operator's daily loop for a multi-device fleet (task #1332).
Every host runs one `ua-node`. Each node dials out to the hub, and you start,
watch, answer, steer, cancel and audit harness sessions on any host from the
Control Center in a browser or on a phone. You don't open a harness desktop
app or a manual SSH or IAP tunnel.

Reference pages, linked rather than repeated here:

| Need | Page |
|---|---|
| Launch, stream, cancel, follow-up and resume field by field | [web-ui.md, ua-node sessions](web-ui.md#ua-node-sessions-start-stream-cancel-task-1329) |
| What each harness path supports, per action | [harness-control-matrix.md](harness-control-matrix.md) |
| Installing and signing in a node | [harnesses.md, Device workers](harnesses.md#device-workers) |
| Long tests or builds that outlive a session | [harnesses.md, Supervised long commands](harnesses.md#supervised-long-commands-task-2413) |
| Fleet topology and network reach | [topologies.md, Multi-device fleet](topologies.md#3-multi-device-fleet) |

## Before the first day

1. **Install `ua-node` on each host.**
   - Linux: `deploy/systemd/ua-node.service` is a systemd user unit.
   - macOS: `deploy/mac-hub/com.unified-agent.node.plist` is a LaunchAgent template.
   - Sign each host in once with `unified-agent login --hub <url> --agent <Label>`. This is a device flow: approve the code at `<hub>/oauth/device`, which a phone can do. Each host gets its own credential; never copy one between hosts.
2. **Declare the fleet** on the hub so doctor can check each host by name. In `settings.toml`:

   ```toml
   [fleet]
   nodes = ["<always-on host>", "<hub host>", "<worker>"]
   optional_nodes = ["<sometimes-off host>"]
   ```

   - A host in `optional_nodes` is reported but never counted against the fleet. When it is off, it reads "unreported, optional by operator decision".
   - To make it required later, start its node, check that it reads `live (optional)`, then move its name to `nodes`.
3. **Reach the Control Center over the tailnet.** The hub binds to loopback and is published to the tailnet with Tailscale Serve (see [web-ui.md, Open it](web-ui.md#open-it)). A phone on the tailnet opens the same `/ui`. The public hostname, where configured, serves only `/mcp` and OAuth; it returns 404 for `/ui` and `/api/*`.
4. **Accept each new device** before it gets real work. See [Device acceptance](#device-acceptance-task-2112).

## Device acceptance (task #2112)

A device acceptance run checks that a newly enrolled node works end to end
before you give it real work. It runs a harmless job through the same claim,
heartbeat, events and receipt routes that real work uses. The job never starts
an agent harness, a model or a subprocess.

**Start a run** in any of these ways:

- **Devices → ua-nodes → Run acceptance** in the Control Center (task #2208)
- `ua node acceptance start <node>` from a shell (task #2208)
- `coordination_manage(action="node_acceptance_start", params={"node": "<node>"})`
- `POST /api/action/node-acceptance` with the body `{"action": "start", "node": "<node>"}`

The human operator's MCP profile is read-only, so the operator uses the
Control Center or `ua`; both call the HTTP route with the operator's own
credential.

Only the node's own agent or a lifecycle operator (`UA_LIFECYCLE_OPERATORS`)
can start or re-evaluate a run. The run does not need an approval.

**What runs.** The hub queues two items on the built-in `acceptance` backend.
Every ua-node advertises this backend, whatever `UA_NODE_BACKENDS` says.

1. The first item sends a launch event, then three progress events. It writes a
   small file (4 KB at most) under its session directory, reads the file back
   and posts a `finished` receipt with the file's SHA-256.
2. The second item sends the same launch and progress events, then waits. When
   it reports progress, the hub cancels it. The cancel reaches the device on
   its next heartbeat. The job stops, sends a confirmed `acceptance_stopped`
   event and posts a `killed` / `cancelled_by_hub` receipt. If no cancel
   arrives within its 240-second hold, the item fails, and so does the run.

**Checks.** The hub evaluates the run after each acceptance receipt and stores
the result in the `node_acceptance` table:

| Check | Passes when |
|---|---|
| `launch` | The first item sent its launch event |
| `progress` | The first item sent unique steps 1, 2, 3 in order after launch |
| `receipt` | The first item finished, and the SHA-256 in its receipt matches the one the hub computes from the job spec |
| `cancel_confirmed` | Launch and progress preceded a hub-observed cancel relay on heartbeat; a confirmed stop event arrived after that relay, then a killed receipt |
| `revocation` | The registry refuses this registration's claim for a foreign agent, for a foreign device (when the node is device-bound), and for the node's own agent once that label is in `UA_REVOKED_AGENTS` |

The run's state is `pending`, `accepted` or `failed`. Passing diagnostics alone
leaves it pending with `awaiting_operator_attestation` and `diagnostics_passed: true`.
An independent operator must verify the device output and cancellation outside
its self-reported API, then call:

```text
coordination_manage(action="node_acceptance_attest", params={
  "node": "<node>", "run_id": "acc-<run>", "evidence_ref": "<independent evidence>"
})
```

The HTTP twin uses `POST /api/action/node-acceptance` with `action: "attest"`
and the same fields, exposed in the Control Center under **Devices → ua-nodes →
Attest** and in the shell as
`ua node acceptance attest <node> --run-id acc-<run> --evidence-ref "<independent evidence>"`
(task #2299). Only a configured lifecycle operator, different from the
node's agent, may attest. The stored attestation is bound to the run, agent and
device registration. A later registration binding or a new run requires fresh
acceptance. All mutation actions respect the same policy gates as MCP.

Events, digest, and code SHA are node self-report, not independent proof of
filesystem execution or a trustworthy node binary. The registry revocation
probe is hub-side evidence, not a live revoke/login test. Independent operator
attestation is an explicit trust decision; the hub cannot prove a dishonest
operator's assertion. This hosted flow serves one operator organization; it
does not establish cross-tenant device authority.

A failed run names the
first check that failed. A run that has not finished 15 minutes after it
started fails as `timed_out`, including runs awaiting attestation. The 15
minutes include the operator's attestation, so start a run only when the
operator is ready to inspect and attest it. When every diagnostic passed and
only the attestation was missing, the reason says `timed_out
awaiting_operator_attestation`; the device itself needs no repair. Timeout is
terminal for that run; a periodic hub sweep cancels unfinished diagnostics so
an offline node cannot later execute them. Each result also records a timestamp and the
`code_sha` the node reported.

**Revocation is checked on the hub, not live.** A live revoke signs the
device out (`oauth_device_manage`), and the device then needs a new
`unified-agent login`. The probe instead runs the ownership check that every
claim, heartbeat and receipt passes, against the node's stored registration,
and writes nothing. A node whose agent is in `UA_REVOKED_AGENTS` is refused on
every node call (403).

**Read the result** in any of these places:

- `coordination_manage(action="node_acceptance_status", params={"node": "<node>"})` returns the live per-check result. Without `node`, it returns every node's stored summary.
- `GET /api/nodes/acceptance?node=<node>`, or `ua node acceptance status [<node>]`
- the `acceptance` field of each node in `GET /api/nodes`, shown in the
  devices table on Agents → Hosts (`/ui/agents/hosts`)
- the doctor row `fleet_node_acceptance`

`node_acceptance_evaluate`, `{"action": "evaluate"}`, the **Re-check** button on
Agents → Hosts and `ua node acceptance evaluate <node>` re-evaluate
the run and store the result.

**Require it.** `UA_NODE_REQUIRE_ACCEPTANCE` defaults on for hosted
`authenticated_public` mode, Railway or `UA_HOSTED=1`, and off for other
private/local modes. Hosted device operations require a bound OAuth sign-in
even if bearer compatibility was enabled. A legacy unbound registration can
re-register under its own agent's bound token, after which any earlier
acceptance must be repeated for the new binding. When it is `1`, the
queue refuses every backend except `acceptance` for a node whose latest run is
not `accepted`. The refusal is a 409 that says how to start a run. Scheduler
routing reports `node_not_accepted:<nodes>`. A refused request never uses up an
approval. Starting a new run replaces the previous result, so the node stays
not accepted until the new run passes and is independently attested. Existing
queued real work is held at claim time, while acceptance jobs behind it remain
claimable. Starting a new run also cancels any
unfinished items left from the previous one.

## Morning check (Doctor and Devices)

Open **Doctor** and read the fleet rows:

| Row | LIVE means |
|---|---|
| `fleet_nodes` | Every declared required host has a heartbeat within the last 90 seconds. Optional hosts are named either way. |
| `fleet_node_versions` | Every live node runs the hub's commit. |
| `fleet_vault_sync` | Every live node that syncs the vault reports a healthy sync. |
| `fleet_node_acceptance` | Every registered node passed [device acceptance](#device-acceptance-task-2112). A node offline for more than a day is named with its last run state but not counted, even when that run failed. A failed run on any other node is DEGRADED. A node that was never accepted is DEGRADED when `UA_NODE_REQUIRE_ACCEPTANCE=1`, and DORMANT when it is off. |

A DEGRADED `fleet_nodes` row names the host and when it last reported. That is
the cue to look at that machine. **Devices** shows each host's last heartbeat
and how fresh its doctor snapshot is.

## Retire a node (task #2181)

A device that registers again under a new hostname leaves its old node row
behind, offline for good. Retire the old row so it stops showing up as a fleet
host and is never considered for routing:

```text
coordination_manage(action="node_retire",
                    params={"node": "old-host.local", "reason": "renamed to new-host.local"})
```

The human operator's MCP profile is read-only, so the operator uses one of the
HTTP twins (task #2208), which run the same action as the caller's identity:

- **Control Center:** Devices → ua-nodes → **Retire** on the node's row. Enter
  a reason and press **Confirm retire**. A retired row stays in the table,
  marked retired, with **Unretire**.
- **Shell:** `ua node retire old-host.local --reason "renamed to new-host.local"`
  and `ua node unretire old-host.local --reason "device is back"`;
  `ua node list` shows the retired rows with who retired them, when and why.
  Retired rows are kept forever, so the list prints the first 100 (sorted by
  name) and `truncated` counts the rest (0 when none were left out; task
  #2210). Add `--format json` for the `ua.result/1` envelope. A refusal exits 4
  (not an operator) or 8 (conflict: queued or claimed work).
- **HTTP:** `POST /api/action/node-retire` with
  `{"action": "retire" | "unretire", "node": "<node>", "reason": "<why>"}`.
  CSRF-gated for a browser session. 401 without an identity, 403 for a caller
  who is not a lifecycle operator, 409 while the node has queued or claimed
  work, 400 for an unknown node or a missing reason. A call a policy rule parks
  for approval answers 202 `{"approval_id": N, "status": "pending"}`; the Hosts
  page shows the write as parked for that approval and `ua` exits 9 (`approval_required`).

- **Who.** Lifecycle operators only (`UA_LIFECYCLE_OPERATORS`; unset, the
  operator label and its aliases). The node's own agent cannot retire it.
- **What changes.** The row is kept and stamped `retired_at`, `retired_by` and
  `retired_reason`; nothing is deleted. One `agent.node` ledger event with
  `kind="retire"` records the actor, the reason and the row's last heartbeat.
- **Where it disappears.** The doctor's fleet rows (including
  `fleet_node_acceptance`), acceptance summaries, the Devices / fleet
  listing (including any doctor snapshot stored under the retired node's
  host name, unless that host is also an active node; task #2196), agent
  placement, and routing (automatic node selection and a
  direct enqueue). `fleet_nodes` lists retired rows under `retired` and counts
  them in its detail; a declared name that is retired reads "retired by an
  operator" instead of as a stale heartbeat. A label whose only node rows are
  retired fails closed with `node_retired:<nodes>`; it does not fall back to a
  hub-local spawn.
- **If the device comes back.** Its registration and heartbeats are refused
  (HTTP 403 with `"code": "node_retired"`) while the row is retired, so a
  retired name cannot slip back into routing. The ua-node logs once that an
  operator retired it and re-checks every 30 minutes; it does not ask for a
  login. Another identity probing the name gets the ordinary "bound to a
  different agent" refusal and learns nothing about retirement. To return it to service, an operator runs
  `coordination_manage(action="node_unretire", params={"node": ..., "reason": ...})`,
  **Unretire** on Agents → Hosts or `ua node unretire <node> --reason ...`
  (`kind="unretire"` on the ledger) and the device registers again.
  Routing waits for that registration or a fresh heartbeat: the heartbeat
  stored before retirement is kept (the doctor still reports when the node
  was last seen) but no longer counts as live (task #2196). The same holds
  for a direct enqueue (it waits), session credentials, team seats, agent
  status, Devices and the doctor's fleet rows.
- **Older hubs.** A ua-node talking to a hub that predates task #2181 gets a
  plain 403 with no `code` field. It cannot tell retirement from a refused
  credential, so it logs the refusal as an auth error and retries on the
  5-minute auth backoff, not the 30-minute retired one (task #2194).
- **Refused** (409) while the node has queued or claimed work: cancel it first.
  Repeating `node_retire` on a retired row, or `node_unretire` on an active
  one, changes nothing and returns `changed: false`.

## Start a session

**Sessions → Start harness session.** Pick:

- the device (node)
- the harness or backend; the list is what that node advertises
- the working directory, from the node's worktree roots
- a prompt
- optionally, a task id

For `acp:*` backends you also choose a model and a permission mode:
`supervised` (the default), `auto-accept-edits` or `full-access`. Anything the
harness cannot do is shown disabled and labelled "(unsupported)".

Starting a session is approval-gated (`node_dispatch`). The launcher shows
"Queued for approval #N". Approve it in **Approvals**, then start again with
the same payload.

## Watch, answer, steer

- **Watch.** The node session transcript streams the session's events (text, plan steps and tool calls), each labelled with its turn.
- **Answer permission prompts.** On ACP paths, every `session/request_permission` from the harness becomes an `acp_permission` row in **Approvals**.
  - The drawer shows the device, agent, ACP session, dispatch item, the action, and the options the harness offered.
  - Approving answers that one prompt only. Anything other than an approval tells the harness `cancelled`.
  - The node waits up to five minutes for an answer.
- **CLI paths** (non-ACP backends) only offer the launch approval. Their tool prompts are not relayed, and you see the final receipt rather than a live stream.
- **Queue or steer** a follow-up from the composer under the transcript.
  - On ACP paths, queue is supported and steer is emulated (cancel, then prompt).
  - Follow-ups you send as the operator do not need a second approval; any other identity's follow-ups do.

## Phone alert for a waiting approval

The hub can push a short alert to your phone when an approval is queued, so an
ACP permission prompt does not time out while **Approvals** is closed. It is
off until you set `UA_APPROVAL_PUSH_URL` (task #1988).

1. Install the ntfy app and subscribe to a topic. On ntfy.sh, the topic name is
   the only secret: pick a long random one, for example from
   `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`. A
   self-hosted ntfy can require an access token instead.
2. On the hub, add the full topic URL to `~/.unified_agent/service.env`, and the
   token if your server needs one. Edit the file in an editor; do not echo the
   values into a shell:

   ```bash
   UA_APPROVAL_PUSH_URL=https://ntfy.sh/<your-topic>
   UA_APPROVAL_PUSH_TOKEN=tk_...   # optional, sent as a bearer token
   ```

3. Restart the hub, then check that `system_doctor` reports `approval_push`
   LIVE with the push host.

Each alert reads "Approval waiting (N pending)". Tapping it opens
`/ui/inbox?kind=approval&item=<id>` when the hub knows its Control Center
origin (`UA_PUBLIC_URL`, else `UA_MCP_OAUTH_ISSUER`); otherwise it carries no
link. The alert never includes the tool, its arguments or the requester. A
burst of requests sends at most one alert every 30 seconds. If a push fails,
the approval is not affected; the hub logs a warning that names only the push
host.

The hub process sends the alert, so the phone does not need to be on the
tailnet when you use ntfy.sh. A self-hosted ntfy on the tailnet works too; the
hub posts to it directly. The push URL must be `https://`: a plain `http://`
URL is refused (doctor reports MISCONFIGURED), and the push never goes through
an `HTTPS_PROXY`. Put a self-hosted ntfy behind `tailscale serve` for a
certificate. A request queued by a short-lived CLI process rather than the hub
still sends its first alert before the process exits, but may lose a delayed
second alert.

## Stop, restart, resume, adopt

| Action | Where | Notes |
|---|---|---|
| Cancel | Transcript, while queued or claimed | Asks the node to stop the item |
| Start again | Transcript | A fresh session with no history; approval-gated |
| Resume | Transcript, when the harness can reload the session | When unavailable, the reason is shown and only "start again" is offered |
| Adopt | Sessions started outside the hub | Brings a native session under the hub through a chosen backend; approval-gated |

An item whose lease expired with an unconfirmed outcome must be reconciled
before it is started again (see [cli-reference.md](cli-reference.md)).

**Offline enqueue (task #2110).** Queueing work for a ua-node whose
heartbeat is not live is accepted only as **waiting**, with a reason
(`node_offline` or `node_never_heartbeat`) and a deadline
(`UA_NODE_QUEUE_WAIT_S`, default one hour). If the node has not claimed
the item by then, it fails with `waiting_expired_node_offline` and never
launches late; a linked dispatch run fails with the same reason. The HTTP
hub sweeps waiting items every `UA_NODE_WAIT_SWEEP_S` seconds (default 60;
`0` disables) even with dispatch off. See
[troubleshooting](troubleshooting.md#dispatch-accepted-but-the-node-never-ran).

## Read the evidence

Each node action is recorded on the hash-chained event log: enqueue, claim,
each ACP event, permission decisions, cancel and the receipt. The item's
evidence reference is `node:<node>:<item_id>`.

The session's page (`/ui/sessions/<id>`) shows the item's evidence reference and
links its task, if it has one; Settings → Audit
(`/ui/settings/audit?task_id=<id>`) shows that task's evidence. The task's
evidence report includes the enqueue and receipt hops; streamed ACP events
stay in the transcript and the event log.

## Not covered yet

These gaps are stated here so the loop is not overstated:

- **No off-tailnet reach.** A device or phone that is not on the tailnet cannot reach `/ui` or run a node. The relay is a separate task.
- **Session-scoped evidence.** Settings → Audit filters by task, not by session or dispatch item.
- **Mobile API.** `/api/mobile/v1` is read-mostly and cannot resolve approvals; approvals are answered in the web UI.

## Break-glass only

SSH and IAP recipes stay documented for recovery, not daily use:

- [cloud-mirror.md](cloud-mirror.md) (the IAP forward for the mirror shipper)
- the `deploy/gcp/` runbooks
- the SSH checks in cloud-agent5.md and [llm-subscriptions-leg-runbook.md](llm-subscriptions-leg-runbook.md)

Reach for them only when a node or the hub itself is down.

## Acceptance walkthrough

Record each step as evidence (screenshots or event ids) on the walkthrough
task. Use at least two hosts, both declared in `fleet.nodes`, and keep your
phone on the tailnet.

1. Doctor shows `fleet_nodes` LIVE. Note the detail line, which names each required host.
2. Start an ACP Claude Code session on one host and a Codex session on another. Approve both launches from the phone.
3. In the Claude Code session, trigger a tool permission prompt and approve it from the phone's **Inbox**.
4. Queue a follow-up on one session, and steer the other where the matrix allows it.
5. Cancel one session from the phone.
6. Resume the other, or record the unavailable reason the transcript shows.
7. Open **Evidence** from the transcript. Confirm the task's chain shows the enqueue and receipt, and that the approvals show the human resolver.
8. Note any harness action that was unavailable, with the reason the Control Center displayed.
