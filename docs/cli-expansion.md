# Operator and agent CLI

`ua` is a lightweight client of the canonical Unified Agent hub. It is installed
alongside the existing `unified-agent` and `unified-agent-*` commands. The bare
`unified-agent` command retains its MCP-server startup behavior. `ua --help` and
argument errors do not load the server, executor, embedding model, configuration
or database. `python -m unified_agent.cli` provides the same client entry point.

This covers delivery slices 1 to 3 of the [approved expansion](designs/cli-expansion.md),
tracked by tasks #1935 to #1938. Slice 1 provides discovery and reads.
Slice 2 adds explicit daily-work writes: task add/claim/update and mail
read/ack/reply. Slice 3 adds [controlled execution](#controlled-execution):
delegation and dispatch, workflows and intervention through the existing gates,
with server-enforced retry keys, local receipts, bounded waits and a resumable
JSONL event stream. Approval resolution stays on the operator surface;
completion and the TUI are slice 4.

## Connect through the hub gateway

Desktop and laptop agents connect to the Mac hub over its authenticated HTTP(S)
MCP endpoint. They do not start a local hub or open a desktop database. The client
uses the same remote policy, profile, resource readers and tool handlers as an
agent's MCP connection. `--gateway` and `--hub` name the same connection option:

```bash
ua --gateway https://hub.example.test/mcp --identity My_Agent whoami
ua --hub https://hub.example.test/mcp --identity My_Agent task list
```

Set `UA_HTTP_HUB_URL` instead of passing the URL on each command. It must be an
explicit endpoint ending exactly in `/mcp`, with no userinfo, query or fragment.
For a reverse proxy, use its public HTTP(S) `/mcp` endpoint. Redirects are refused;
the client never follows them with the credential. Every initialize, discovery,
tool and resource request stays on that endpoint. HTTP proxy settings supported
by HTTPX remain available. Requests and responses are bounded to 1 MiB and 2 MiB.
The default per-request read budget is 10 seconds, or 60 for Doctor/Evidence
reports (including their generic invocation aliases), `delegate start`,
`reconcile` and `workflow run`. Override it with
`--timeout 1..120`. Decoded response chunks are checked against elapsed time,
so an SSE heartbeat cannot continually restart the budget. A pending network
receive remains bounded by HTTPX's idle timeout as well.

Authentication uses existing credentials, in this order:

1. An explicitly selected `--token-file` (or `UA_CLI_TOKEN_FILE`). This is a plain
   bearer file, not an environment file or an OAuth JSON document.
2. `UA_EXECUTOR_TOKEN` or `UA_AGENT_BEARER_TOKEN` from the process environment.
3. The device sign-in saved by the existing `unified-agent login` command,
   matched to the selected hub and, when supplied, identity. Normal device-token
   refresh uses that existing sign-in implementation.
4. A non-managed `UA_AGENT_TOKENS` entry matching `--identity`,
   `UA_CLI_IDENTITY`, or `UA_EXECUTOR_AGENT`.

For a configured desktop gateway, the existing private bearer file used by
its headers helper can also be read by the CLI:

```bash
ua --gateway https://hub.example.test/mcp \
  --identity My_Agent --token-file ~/.config/ua/existing-bearer whoami
```

This reads the existing credential; it neither invokes an arbitrary helper nor
changes MCP registration, host config or secrets. Tokens are never accepted as
literal command-line arguments, printed in context output, or persisted by the
CLI. `--identity` selects an existing credential and requires the hub-confirmed
identity to match. It cannot impersonate that identity. Without a label, an
explicit bearer or device sign-in can report its own authenticated identity.

`ua context show` and `ua status` verify the connection before showing its hub,
identity, profile and selected project/mailbox session. `--project` /
`UA_CLI_PROJECT` and `--session` / `UA_CLI_SESSION` supply defaults only to tools
whose live schema accepts `project` or `session_id`; conflicting structured input
is refused. For writes, the project scopes `task add` and becomes the
`expected_project` guard of `task claim` and `task update`; it never moves an
existing task. The session becomes the mail verbs' `session_id` and a reply's
`from_session`. Individual resource reads use their existing server-side permissions
and are not filtered by these optional list parameters. A domain mailbox session
is distinct from the short-lived MCP transport session created for this invocation.
The client ends its transport session when it exits; it does not end a business
session, cancel a run or release a claim.

## Read commands

```bash
ua whoami
ua status
ua context show
ua capabilities list
ua capabilities describe task.list --format json
ua task list --project unified-agent --frontier --limit 20
ua task list --cursor CURSOR --project unified-agent
ua task show 1936
ua inbox list --session SESSION_ID --after-seq 12
ua run get RUN_ID
ua needs            # everything waiting on the operator, and where to act (task #2411)
ua approval list
ua approval show APPROVAL_ID
ua evidence show --task 1936 --limit 100
ua doctor --timeout 60
```

`ua needs` is `GET /api/attention` → `needs_operator`: pending approvals,
operator-only tasks, red-tier holds, held PRs, unanswered operator mail and
wake grants, each with `act.pane` for where to act. The catalog is closed; a
producer that cannot be read is listed in `errors`, not as an empty queue.
See [web-ui.md](web-ui.md#attention-and-digest).

`inbox list` uses `message_list`, with `unread_only=true` unless `--all` is set.
It does not mark messages read or acknowledged. It retains the hub's per-session
mailbox scope and sequence cursor. The hub may refresh that session's liveness.

Task lists return one page, including `truncated` and `next_cursor`. Mail lists
retain `next_seq` and any count metadata returned by the hub. An empty page is
not represented as proof that an entire queue or mailbox is empty. Tool-catalog
discovery follows all pages, refusing duplicate names, repeated cursors and a
catalog exceeding 100 pages. Discovery shows what this credential's new MCP
session can see; a caller's overlay on a different transport session is not
copied. Runtime policy can still refuse a discovered tool.

## Write commands

```bash
ua task add --title "Fix the flaky export test" --priority high --idempotency-key fix-export-1
ua task add --input @task.json
ua task claim 1937 --lane dispatch-ready
ua task update 1937 --note "tests written" --expected-updated-at REVISION
ua task update 1937 --input '{"status":"waiting_review","evidence_ref":"PR-URL"}'
ua inbox read --session SESSION_ID
ua inbox ack MESSAGE_ID
ua inbox ack --through 42
ua inbox reply MESSAGE_ID --to Other_Agent --body "On it" --reply-id my-reply-1
ua inbox reply MESSAGE_ID --to Other_Agent --body-file reply.txt
ua inbox reply MESSAGE_ID --to Other_Agent --body "Yes" --input '{"to_session":"RECIPIENT_SESSION"}'
```

Each write has its own verb and maps to one existing hub tool. `command invoke`
stays read-only, so no write is ever inferred from a read.

| Verb | Hub tool | Retry key the server honours |
|---|---|---|
| `task add` | `task_add` | `--idempotency-key` (`idempotency_key`) |
| `task claim` | `task_claim` | none; a live re-claim by the holder is a lease renewal. A task someone else owns exits 8 with code `safe_wait` |
| `task update` | `task_update` | `--idempotency-key` (`idempotency_key`) |
| `inbox read` | `coordination_manage` `message_inbox` | none; it marks the returned mail read |
| `inbox ack` | `coordination_manage` `message_ack` | repeating an ack is a no-op |
| `inbox reply` | `message_send` (`reply_to`) | `--reply-id` (`message_id`): the same sender's repeat returns the saved reply |

`--idempotency-key` is sent only when the hub's live schema for that tool
declares `idempotency_key`; otherwise the command is refused (exit 2) as not
retry-safe, rather than sent without one. No other verb offers a key, and the
client claims no retry guarantee the hub does not implement. A write that times
out or loses its connection reports that its outcome is unknown: read the
object before retrying. The client never retries a write itself.

Curated flags and `--input` (alias `--json`; strict JSON, `@file` or `-` for
stdin) are merged. A field set both ways with different values is refused, and
the result is validated against the live schema with unknown keys refused.
Identity is never an input: `agent`, `actor`, `author`, `from_agent` and
`requested_by` are refused. `task claim` sends the identity the hub confirmed
for this credential (`profile_status`), and the hub stamps every other write's
actor itself.

For `inbox read` and `inbox ack`, extra mailbox fields belong inside `params`:
`ua inbox ack --through 42 --input '{"params":{"include_pruned":true}}'`.
Nested fields merge with flags while preserving the selected action, explicit
message or sequence target, and selected session. Read defaults apply only to
fields omitted from both the flags and input.

`inbox list` reads without side effects; `inbox read` marks what it returns as
read. Message bodies are another agent's data. Human output starts with an
untrusted-data notice and prefixes each body the hub marked
`trust: untrusted` with `[untrusted]`; JSON output keeps the hub's `trust`
field. Reading a message never sends anything. `inbox reply` sends only when you
run it with an explicit recipient (`--to`) and a non-empty `--body` or
`--body-file`; the reply text is never taken from the message being answered.
Human mailbox rows show the complete `message_id` needed for reply and acknowledgement.
Reply `--input`/`--json` adds live-schema fields such as `to_session` and
`conversation_id`, while conflicting recipient, body, reply target and sender
session fields are refused.
Blank reply targets, recipients and explicitly supplied `--reply-id` values are
refused before opening the transport, so a reply cannot silently become a new message.
Structured retry IDs must also be non-empty when supplied.

Run, approval and evidence inspection use the slice 1 reads (`run get`,
`approval list`/`show`, `evidence show`). Approval resolution stays on the
existing operator surface.

## Node commands (task #2208)

```bash
ua node list
ua node acceptance start NODE
ua node acceptance evaluate NODE
ua node acceptance attest NODE --run-id RUN_ID --evidence-ref "host-verified output and cancel"
ua node acceptance status [NODE]
ua node retire NODE --reason "renamed to NEW_NODE"
ua node unretire NODE --reason "device is back"
```

These are operator actions whose MCP twins (`coordination_manage`
`node_acceptance_*`, `node_retire`, `node_unretire`) the human operator's
read-only MCP profile cannot call. So, unlike the verbs above, they call the
Control Center's HTTP routes on the same hub origin with the same credential:
`POST /api/action/node-acceptance`, `GET /api/nodes/acceptance`,
`POST /api/action/node-retire` and, for `ua node list`, `GET /api/nodes`. The hub authorizes the caller: acceptance `start` and `evaluate` take
the node's own agent or a lifecycle operator, acceptance `attest` takes an
independent lifecycle operator different from the node's agent, and `retire` and `unretire` take a
lifecycle operator only. `--reason` (on retire/unretire) and `--run-id` + `--evidence-ref`
(on `acceptance attest`, task #2299) are required and recorded on the ledger.
A 403 exits 4 (`permission_denied`), a 409 exits 8 (`conflict`, for example a
retire while the node has queued or claimed work), a call parked for approval
exits 9 (`approval_required`, with the approval id), and the hub's bounded reason
is included in the error message. A retire that times out reports an unknown
outcome; run `ua node list` (its `retired_nodes` carry `retired_at`, `retired_by`
and `retired_reason`) or open Agents → Hosts (`/ui/agents/hosts`) before retrying. `ua node list`
reads `GET /api/nodes`: each live node's name, agent, version, last heartbeat and
acceptance summary, plus the retired rows. Retired rows are kept forever, so the
list shows the first 100 (sorted by name) and `truncated` counts the rest (0 when
none were left out; task #2210). A read the hub answers with an in-band
`error` (a failed store query) exits 1 (`hub_error`) instead of printing an empty
list.
See [fleet-console.md](fleet-console.md#retire-a-node-task-2181).

## One hub entry for a harness (task #1336)

`ua mcp-entry` prints the single MCP entry a harness needs once the hub's
[`mcp_gateway`](tools-reference.md#mcp-gateway-toolsmcp_gatewaypy) fronts the
downstream servers:

```bash
ua mcp-entry --hub https://hub.example.test/mcp --harness codex
ua mcp-entry --format json     # every harness; uses UA_HTTP_HUB_URL
```

`--harness` is `claude`, `codex`, `cursor`, `antigravity`, `opencode` or
`hermes`; omitted, it prints all of them. The command only prints. It reads no
credential, opens no connection and neither reads nor writes a harness file, so
registration stays a separate, reviewed step. A token appears as a placeholder
or an environment variable name, never a value.

## Machine interface

Use `--format json` for a single result envelope and `--no-input` to state the
noninteractive intent. This slice never prompts. Common options work before the
command, between family and action, or after the action. Machine stdout contains
only one JSON envelope; diagnostics go to stderr and errors have nonzero exits.

```json
{
  "schema_version": "ua.result/1",
  "client_request_id": "client-generated-correlation-id",
  "command": "task.list",
  "outcome": "completed",
  "data": {"tasks": [], "truncated": false, "next_cursor": null},
  "error": null
}
```

On error, `outcome` is `failed`, `data` is null, and `error` carries `code` and
`message`. A write refused because the object changed or is held by another
claimant has `outcome` and `code` `conflict` (exit 8). `task claim` on a task
someone else owns has `outcome` `conflict` and `code` `safe_wait` (exit 8), with
the wait in `error.reference`; nothing was claimed. A write parked in the
approval queue has `outcome` and `code` `approval_required` (exit 9) and
`error.approval_id`; retry the identical command after a human approves it.
When a typed approval refusal has no ID, it still reports `approval_required`
and exit 9, omits `error.approval_id`, and directs you to the operator approval surface.
`client_request_id` is a local correlation ID, not an idempotency key, server
receipt or durable lookup handle. The domain payload remains the hub's payload,
including its own schema versions, evidence and references.

| Exit | Meaning |
|---|---|
| 0 | Command completed, or help displayed |
| 1 | Hub/tool rejection or invalid response |
| 2 | Usage, configuration or input validation failure |
| 3 | Missing/rejected credential or identity mismatch |
| 4 | HTTP permission denial or mutation refused by this read client |
| 5 | Capability unavailable in the authorized catalog/RPC method |
| 6 | Gateway transport failure |
| 7 | Gateway timeout (for a write, the outcome is unknown) |
| 8 | Write conflict: stale revision, run fence, or a claim held by another agent |
| 9 | Write queued for human approval; `error.approval_id` names it |
| 10 | In progress: committed or held by the hub but not terminal when the wait ended; nothing was cancelled |
| 11 | Outcome unknown: the request may have committed. `delegate start` names a key: run `ua reconcile KEY`. `workflow run` has no key: inspect its events and objects before running it again. `delegate cancel`, `run cancel` and `run resume`: read the delegation or run (`ua delegate show`, `ua run get`) before retrying |
| 12 | Not committed: the hub holds no record of a keyed request; `ua reconcile KEY --retry` sends it once |

The hub names these write outcomes in the error result's `structuredContent`
(`{"code": "conflict"}`, `{"code": "permission_denied"}`, or
`{"code": "approval_required", "approval_id": N}`,
task #2054; see the error contract in [tools-reference.md](tools-reference.md)),
and the client reads that first. A hub without #2054 reports them only as MCP
error text, so the client falls back to the hub's fixed conflict, steward denial, and approval
templates, matched from the start of the message after the MCP tool prefix
(`task_claim`, `task_update` and the `UA_APPROVAL_TOOLS` gate), so text supplied
later in a message cannot forge them. **Deprecated:** the text fallback is
removed one release after #2054 deploys to the Mac hub. A gate that instead
returns a pending row (`gated: true` or `status: "pending-approval"`) is also
`approval_required`. A pending row without an ID keeps
that outcome and omits the handle; a supplied handle must be a positive integer,
otherwise the CLI reports an invalid response. Every other refusal remains
`tool_error`, and hub prose is never printed. Gateway rejection, lost connectivity,
timeout and missing credentials never fall back to local execution. This slice
does not silently retry a tools/call; a new invocation creates a new transport
session for retrying a read.

## Controlled execution

```bash
ua delegate start --to Other_Agent --spec-file spec.md
ua delegate start --to Other_Agent --spec-file - --priority high --wait --wait-timeout 600 < spec.md
ua delegate start --to Other_Agent --spec-file spec.md --idempotency-key release-notes-1
ua delegate lookup release-notes-1
ua reconcile release-notes-1
ua reconcile release-notes-1 --retry --wait
ua delegate show DELEGATION_ID
ua delegate wait DELEGATION_ID --wait-timeout 900
ua delegate cancel DELEGATION_ID --expected-claim-generation 2 --reason "superseded"
ua run wait RUN_ID
ua run cancel RUN_ID --reason "wrong branch"
ua run resume RUN_ID
ua workflow list
ua workflow run WORKFLOW --inputs @inputs.json
ua events follow --channel 'agent.delegation.*' --cursor-file ~/.local/state/ua/delegations.cursor --from-start
```

Each verb maps to one existing hub tool, so the same profile, policy, approval,
claim and evidence gates apply as for an MCP caller:

| Verb | Hub tool | Retry and fencing |
|---|---|---|
| `delegate start` | `delegation_manage` `create` | `--idempotency-key`, generated and saved locally when omitted |
| `delegate lookup`, `reconcile` | `delegation_manage` `lookup` | read only; no effect |
| `delegate show`, `delegate wait` | `delegation_manage` `get` | read only |
| `delegate cancel` | `delegation_manage` `cancel` | `--expected-claim-generation` lease fence, `--require-no-run` |
| `run wait` | the `unified-agent://runs/{id}` resource | read only |
| `run cancel`, `run resume` | `dispatch_cancel`, `dispatch_resume` | the hub admits only the run's own agent |
| `workflow list`, `workflow run` | `workflow_list`, `workflow_run` | none: `workflow_run` takes no retry key |
| `events follow` | `event_log_query` with `after_seq` | gap-free commit-order cursor |

`delegate start` reads the delegation spec only from `--spec-file` (a file, or
`-` for stdin). There is no inline prompt flag, and flag abbreviations are
refused, so `--spec` or `--prompt` is a usage error rather than an execution
input. The delegator is always the hub-verified identity. The hub still applies
the delegation spec standard, the trust boundary and the `delegate` approval
gate. A delegation addressed to an executor is launched only by the dispatch
scheduler after its own `dispatch_claim` approval; the client never starts an
agent, a shell or a model itself and handles no credential beyond the existing
context.

### Retry keys and receipts

Before sending `delegate start`, the client writes a receipt and flushes it to
disk: the idempotency key, the hub and identity, the validated arguments and the
outcome so far. Receipts live in `$UA_CLI_STATE_DIR/receipts` or
`$XDG_STATE_HOME/unified-agent/cli/receipts` (default
`~/.local/state/unified-agent/cli/receipts`), in a 0700 directory with 0600
files. They hold no credential. If the receipt cannot be written, nothing is
sent. If it cannot be updated after the request was sent, the command still
reports the hub's answer rather than claiming nothing was sent: a committed
delegation exits 0 with its delegation and task ids, `reference.receipt_updated:
false` and a `warnings` entry with code `receipt_not_updated`; an unknown,
parked, refused or reconciled outcome keeps its exit code and names the key.
Rerunning with `--idempotency-key KEY`, or `ua reconcile KEY`, recovers it
without a duplicate.

Without `--idempotency-key`, an identical command (same hub, identity and
arguments) whose earlier attempt is not yet committed reuses that attempt's key,
so rerunning a command after a timeout is a retry, not a second delegation.
`--new-key` starts a separate request instead. Reusing a key for a different
request is refused locally (exit 2) and by the hub (`conflict`, exit 8).

The retry is found through an index keyed by the request fingerprint (hub,
identity, tool and arguments) under `receipts/index/`, one read however many
receipts exist. If that index entry or the receipt it names cannot be read, the
command fails closed with `receipt_unreadable` (exit 2) and sends nothing,
rather than minting a key that could create a second delegation: pass
`--idempotency-key` with the original key, or `--new-key`. Committed receipts
older than 30 days are removed after a later commit, at most 500 files checked
per run; unsettled receipts are kept until their outcome is known.

Overlapping identical commands, in one process or several, agree on one key.
Finding the retry, minting a new key and publishing it in the index happen under
an exclusive per-fingerprint lock (`flock` on `receipts/index/<digest>.lock`),
so the second command reuses the first one's key and the hub sees one request.
An index entry is never repointed away from a receipt that is still unsettled,
including by `--new-key`, whose request stays reachable by its own key. Pruning
takes the same lock, skips a busy one, and removes an index entry only while it
still names the committed receipt being removed. A lock held for more than
30 seconds fails the command with `receipt_locked` (exit 2) before sending. Lock
files are left in place.

The hub enforces the key, scoped to the delegator. A retry of a committed key
returns the original result and inserts nothing, and needs no second approval.
A concurrent request with a key that is still being processed is a `conflict`.
A request parked by the approval gate keeps its key: after a human approves it,
`ua reconcile KEY --retry` (or the identical command) consumes the one-shot
pass. A revoked or denied approval is never consumed.

### Unknown outcomes and reconciliation

A timeout or lost connection on `delegate start` exits 11 with outcome `unknown`
and `error.reference` (the key and receipt path): the request may have
committed. The client never retries by itself. `ua reconcile KEY` asks the hub
for the key's receipt, with no effect, and records the answer locally:

| Hub receipt | `reconcile` result |
|---|---|
| committed | exit 0 with the original result and its delegation reference |
| approval required | exit 9 with `approval_id` |
| in progress | exit 10; reconcile again shortly |
| not found | exit 12 `not_committed`; `--retry` sends the saved request once with the same key |

`reconcile` refuses a receipt sent to another hub or as another identity.
`delegate lookup KEY` reads the hub receipt without a local file.

### Bounded waits

`--wait` (on `delegate start` and `reconcile`) and `delegate wait`/`run wait`
poll the object until a terminal state declared by the platform object model:
`completed`, `cancelled` or `failed` for a delegation; `finished`, `failed`,
`denied`, `cancelled` or `expired` for a run. `--wait-timeout` (1-3600 s, default
300) bounds the wait and `--poll-interval` (0.2-60 s, default 2) its cadence. A
terminal state returns exit 0 with `status`; inspect it, since `failed` and
`cancelled` are terminal too. No read starts once the budget is spent, even
when a poll's sleep lands on the deadline. When the budget ends first, the
command exits 10 with outcome `in_progress` and a durable reference (delegation
or run id, key, last status). Ending a wait, Ctrl-C, a lost connection or a refused credential
never cancels anything: a credential revoked mid-wait exits 3 and still reports
the committed reference. Cancelling is always its own explicit verb.

`--timeout` remains the per-request budget: 10 s by default, and 60 s for `doctor`,
`evidence show`, `delegate start`, `reconcile` and `workflow run`.

### Intervention

`delegate cancel` retires a delegation you sent or received.
`--expected-claim-generation` refuses unless the delegation is still at that
claim generation, so a cancel aimed at one attempt cannot retire a newer one;
`--require-no-run` refuses once a run is attached. `run cancel` and `run
resume` go through `dispatch_cancel` and `dispatch_resume`, which admit only the
run's own agent; confirming or retrying an unconfirmed cancellation stays with
the operator. A hub that changes nothing (already terminal, fence mismatch) is
reported as `conflict` (exit 8), never as success. A lost response is exit 11:
read the object before retrying.

### Workflows

`workflow run NAME --inputs JSON|@file|-` calls `workflow_run`; the inputs must
match the definition's declared inputs. `--instance-id` resumes a role
workflow. Outcomes stay distinct: `done` exits 0, `pending-approval` exits 9,
`denied` exits 4, `failed` exits 1 and `indeterminate` (a step's effect may have
committed without its record) exits 11. Refusals report step names and states,
never step error text. `workflow_run` has no retry key, so a timeout is exit 11
with no automatic retry: check the workflow's events and objects first.

### Event stream

`events follow` pages `event_log_query` by `after_seq`, the hub's gap-free
commit order. With `--format json`, stdout is JSONL: one
`{"schema_version": "ua.event/1", "type": "event", "seq": N, "event": {...}}`
line per event, then one `{"type": "end", "reason": ..., "cursor": N}` line
(`duration`, `max_events`, `once` or `interrupted`). A failure is a final
`ua.result/1` error envelope with `error.reference.after_seq`.

Pick the start explicitly: `--after-seq N`, an existing `--cursor-file`, or
`--from-start`. `--cursor-file` is rewritten atomically after each page, so a
later invocation resumes where the last one stopped. After a lost connection the
client opens a new transport session and resumes from the last emitted seq,
dropping any row at or below it: within one invocation there is no gap and no
duplicate.

Across invocations delivery is **at least once**. The cursor is saved after a
page's lines are printed, so a process killed between printing and saving
prints those lines again on the next run. It never skips one. Every line carries
its `seq`, so a consumer that keeps the highest `seq` it has processed and drops
anything at or below it sees each event exactly once. Up to
`--max-reconnects` (default 5) consecutive failures are absorbed. A refused
credential stops the stream (exit 3). `--channel` (repeatable, `prefix*`
allowed) and `--channel-prefix` filter; `--limit` (1-500) sizes pages;
`--duration` (1-86400 s, default 300) and `--max-events` bound the run. No read
starts once the duration is spent and a reconnect backoff never sleeps past it,
so a stream whose reads keep failing still ends on time with `reason: duration`
(exit 0) and saves its cursor.

## Schema-driven reads

```bash
ua command invoke task.list --input '{"status":"open","limit":10}' --format json
ua command invoke task.list --input @request.json --format json
ua command invoke task.list --input - --format json --no-input
```

Input must be a strict JSON object: duplicate keys, non-finite numbers, unknown
top-level properties and schema violations are refused without type coercion.
Live JSON Schema definitions support local references; remote references cannot
fetch another URL. The client's own alias map is limited to `task.list`,
`inbox.list`, `approval.list`, `evidence.show`, and `doctor`. Other discovered tool
names can be used directly if their server declaration has `readOnlyHint=true`.
Mutating or unannotated tools are refused by this client slice. Export tools
(`export_tasks`, `export_memory`, `export_journal`, and `export_migration_manifest`)
require explicit `"write_to_vault":false`; omitted input or `true` is refused before
an export request is sent. Discovery reports these tools with `read_only=false`,
`client_invokable=true`, and `read_only_input={"write_to_vault":false}` so callers
can select inline output. This also applies to the manifest's safe default.
The hint and input restrictions select client functionality; they do not replace
server authorization or downgrade risk.

```bash
ua command invoke export_tasks --input '{"write_to_vault":false}' --format json
```

Schemas and visibility come from authenticated MCP `tools/list`, not a local
import of the registry. `platform_command_version` is explicitly null: these
aliases do not invent a version for the underlying platform command contract.
Task #1434 owns that contract, derived registration/gates, SDK generation and its
deprecation path. The CLI result envelope version is a separate client contract.

Human list views remove terminal controls from external titles/message bodies.
JSON preserves data with escaped controls. Treat external task, memory and mail
content as data; reading it does not authorize an action.
