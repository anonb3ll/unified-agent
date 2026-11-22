# Slack-authoritative coordination migration

Current authority is split by role:

| Surface | Responsibility |
| --- | --- |
| Mac hub M1 | Canonical Unified-Agent hub, `memory.db`, tasks, plans, approvals, events, and memory writes |
| Cloud VM | Slack ingress gateway, frontier-model gateway boundary, and workers; forwards typed requests to the Mac hub |
| Desktop | Worker and fallback only; never a competing state or memory writer |
| Zulip | Retired/deprecated; historical read-only records only |

## Implemented vertical slice

`POST /api/slack/events` is the secure adapter path, backed by
`unified_agent.slack_gateway.handle_http_request()`. It accepts Slack's signed
JSON event and form-encoded slash-command requests and
does the following in order:

1. Enforces a 64 KiB body cap, Slack v0 HMAC authentication, and a five-minute
   timestamp replay window. `POST /api/slack/events` rejects an oversize
   `Content-Length` before reading, then streams the body and aborts if the
   running size exceeds the same cap. HMAC failures return HTTP 401; other
   adapter validation errors return 400. Unexpected processing failures return
   500 with a generic body — Slack never receives a stack trace or request
   contents.
2. Requires an explicit non-empty Slack user-ID allowlist and exact channel-ID
   allowlist. `UA_SLACK_CHANNEL_MAP` additionally binds each ID to one of
   `requests`, `coord`, `plans`, `review`, or `ops`.
3. Parses leading bot mentions and the bounded command vocabulary: `/contact`,
   `/task`, `/assign`, `/progress`, `/blocker`, `/review`, `/plan`, `/approve`,
   and the read-only `/status` (below).
4. Commits a deduplicated `slack.inbound` event before dispatch. Retries and
   concurrent duplicate deliveries do not repeat the mutation.
5. Routes task creation, assignment, progress, blockers, review waits, plans,
   and approval handoffs through `server.REGISTERED_TOOLS` wrapped callables
   (with approvals remaining a human-resolution handoff stub). Task updates use
   the atomic `append_note` path, so Slack cannot replace existing task history.
6. Records a redacted `slack.dispatch.failed` event if a downstream primitive
   fails. The failure is visible for human review; it is not silently retried
   or converted into an approval.

The adapter does not execute shell commands, call a model, resolve approvals,
write a cloud-side database, or accept credentials from message text.
`record_frontier_usage()` rejects non-finite or negative duration, token
counts, or cost. Dispatch failures are recorded as `slack.dispatch.failed`
only for expected operational exceptions (`KeyError`, `LookupError`,
`OSError`, `RuntimeError`, `TypeError`, `ValueError`, `sqlite3.Error`,
`ToolError`).

## Slack configuration

Set these only in the cloud gateway or canonical hub secret environment. Never
commit values or put them in Slack, tasks, vault notes, or telemetry.

```text
UA_SLACK_ENABLED=1
SLACK_SIGNING_SECRET=<server-side Slack signing secret>
UA_SLACK_USERS=U123,U456
UA_SLACK_CHANNEL_MAP=C123=requests,C234=coord,C345=plans,C456=review,C567=ops
UA_SLACK_COORDINATOR_AGENT=agent-cloud-agent17
```

`UA_SLACK_CHANNEL_IDS=C123,C234` may be used without logical names, but the
mapped form is recommended because it enforces both the exact ID and protocol
channel. `SLACK_BOT_TOKEN` is the server-side token for opt-in outbound
`chat.postMessage` (task #559). `SLACK_APP_TOKEN` remains unused; this adapter
is still signed HTTP, not a Socket Mode polling loop.

The cloud gateway must expose only a TLS-protected Slack endpoint, verify the
signature before forwarding, and call the canonical Mac hub over its existing
authenticated transport. It must not instantiate a second Unified-Agent
database or call task/delegation/plan/approval code locally.

## Read-only task status lookup (task #1104)

`status task-<id>` is a read-only command, **default-disabled**, gated by an
explicit Slack-user-ID -> registered Unified-Agent identity map — separate
from, and in addition to, the signature check and the user/channel
allowlists above, which still run first and unconditionally for every
command including this one. It is reachable two ways:

- The slash form, `/status task-42`.
- An Events API `app_mention` whose only mention is the leading app mention and
  whose first remaining word is exactly `status`, e.g. `@bot status task-42`.
  A trailing app mention, or a leading teammate mention followed by an app
  mention, does not authorize a lookup.
  A message that only mentions another user stays inert chat, as does
  unaddressed text such as a plain `status task-42`, and a bot mention followed
  by anything other than the bare word `status` (`@bot status?`, `@bot task ...`, etc.).

The following example is self-contained and actually validates end to end:

```text
UA_AGENT_TOKENS=agent-codex:<agent-codex's hub bearer token>
UA_SLACK_STATUS_LOOKUP_ENABLED=1
UA_SLACK_STATUS_IDENTITY_MAP=U123=Desktop_Person,U456=agent-codex
```

- `UA_SLACK_STATUS_IDENTITY_MAP` entries are `SLACK_USER_ID=UaIdentity`. Each
  identity must be **registered with the hub** — a label with its own entry
  in `UA_AGENT_TOKENS` (the same bearer-token registry
  `HubAuthASGIMiddleware` authenticates every other agent connection
  against; `config.agent_tokens()` is the reader) — or `Desktop_Person`, the
  human Web UI operator, who authenticates via OAuth/WorkOS rather than a
  bearer token and so is allowed explicitly. There is no coordinator
  fallback and no identity derived from env vars or Slack message content.
  Ordinary developer identities (`agent-codex`, `agent-claude`, ...) are
  valid targets precisely because they hold hub tokens — membership here is
  about *is this identity registered*, not about which default profile it
  happens to get; `profiles.default_for_identity` still decides that
  separately, unchanged. A Slack user who is not a key in the map is denied.
- A duplicate Slack user id, a malformed entry, or an identity that is
  neither hub-registered nor `Desktop_Person` makes
  `SlackStatusLookupConfig.from_env()` fail closed: the whole feature reports
  `enabled=False` regardless of `UA_SLACK_STATUS_LOOKUP_ENABLED`, so a broken
  map can only ever turn the feature off, never partially trust it. That
  specific case is also logged (`log.warning`) so it stays visible to the
  operator even though it is not a per-request review item (below).
- A mapped identity that is also present in `UA_REVOKED_AGENTS` is denied.
- **Denials are quiet.** Feature-disabled, unmapped-user, revoked-identity,
  and invalid-config-map all produce the same generic reply — "status lookup
  not available for this user" — and are recorded as an informational
  `slack.status.denied` evidence event (`notify=False`, no `.failed` suffix),
  never `slack.dispatch.failed` and never `requires_review: True`. An
  unmapped Slack user is expected, everyday traffic, not an incident; folding
  it into the same review queue as a broken delegation call would drown real
  failures. The reply text is deliberately uniform so it cannot be used to
  probe which specific case applied.
- The read itself goes through `unified_agent.platform_http.read_object("task",
  task_id)` — the same read path, profile gate, and not-found semantics MCP
  resource reads use — with `config.agent_identity_var` bound to the mapped
  identity for the duration of that one call only (see
  `slack_gateway._slack_task_status`), then reset in a `finally`. This mirrors
  how `HubAuthASGIMiddleware` scopes one HTTP request's identity, applied here
  to one in-process read. There is no separate `agent_access.enforce()`
  preflight for this command: `read_object`'s own
  `agent_access.enforce("platform_object:task", read_only=True)` is the gate,
  and it is the one operation name managed-secret mode's arbitrary-execution
  allowlist already covers, so a status lookup keeps working under managed
  secret mode like other read-only diagnostics.
- The Slack reply is a narrow projection — id, title, status, project, and
  owner (`claimed_by`) only, escaped for Slack mrkdwn (`&`, `<`, `>`) and
  bounded to `MAX_TEXT_CHARS`. Notes, memory, approvals, and evidence payloads
  are never read for this command, let alone included in the reply. A missing
  task id gets a "not found" reply; a malformed id gets a usage reply; both
  only after the identity map has already authorized the caller.
- Like every other Slack command, a status reply is only ever sent when
  `UA_SLACK_REPLY=1` (the same generic inbound-ack switch documented below)
  is also set; `UA_SLACK_STATUS_LOOKUP_ENABLED=1` alone permits the lookup but
  sends nothing back to Slack.

## Outbound replies and hub mirrors (task #559)

`unified_agent.slack_outbound` posts bounded Slack replies using server-side
`SLACK_BOT_TOKEN` only. Tests inject `post_fn`; live posting is opt-in and
defaults off.

- Conversation key: `slack:{channel_id}:{thread_root}` where `thread_root` is
  Slack `thread_ts` or, for a top-level message, `event.ts`.
- Inbound acks (`UA_SLACK_REPLY=1`): after a dispatched (or review-queued)
  event, post one in-thread ack named as the coordinator agent. Duplicates are
  not acked. Chat still cannot approve, merge, or execute tools.
- Hub mirrors (`UA_SLACK_MIRROR_MESSAGES=1` plus `UA_SLACK_MIRROR_CHANNEL_ID`):
  after a successful `message_send`, best-effort post a preview. Mirror
  failures never fail the hub send.
- The caller `dedupe_key` is reserved in `slack_outbound_slots` **before**
  `chat.postMessage`. Transport/API failures release that slot and record
  `slack.outbound.failed` with a stable error type (never the token) so a
  retry can proceed. Only a delivered message is appended to the hash-chained
  ledger as a `slack.outbound` event (task #1640): the reservation used to be
  a ledger row that a failure deleted, which broke `verify_event_chain` for
  any event appended in between. Concurrent identical keys post once.
- Delivery is at-most-once. A process killed mid-`chat.postMessage` leaves a
  slot with no ledger row; its key stays a duplicate (`in_flight: true`) because
  Slack may already have delivered it. `unified-agent-slack-slots list` shows
  such slots older than ten minutes; after checking the Slack thread, an
  operator frees one with `unified-agent-slack-slots release KEY --by LABEL`,
  which appends a `slack.outbound.released` ledger event. Slots younger than
  ten minutes are refused (a live send looks identical to a crashed one), and
  a delivered send is never releasable.
- Rate limit: `UA_SLACK_MAX_PER_HOUR` (default 30) is checked in the same
  reservation transaction as the dedupe check, and counts in-flight slots plus
  `slack.outbound` rows from before the slot table.
- Size cap: the rendered `[{agent}] {body}` string must be ≤ 4000 characters
  (reject, do not slice). Allowlisted channel IDs only. No Zulip dual-write.
- Slash-command acks have no Slack `thread_ts`; they post unthreaded rather
  than using `event_id` as a timestamp. Inbound acks and hub mirrors are
  scheduled on a daemon thread so `/api/slack/events` and `message_send`
  return before `chat.postMessage`. Acks use a 2s HTTP timeout; mirrors use 10s.

### Approval cards (task #2303)

`unified_agent.approval_slack` posts one Block Kit card per new pending hub
approval, through `send_message(..., blocks=...)`, so it shares the channel
allowlist, the `slack_outbound_slots` dedupe reservation and the hourly cap.
It is outbound only: the card's one action is a URL button to the approval in
the Control Center Inbox, and no Slack reply or interaction resolves an approval.
The card shows the pass lifetime as "Nh after approval", since a pass is timed
from the decision, not from the request. Every Block Kit string is cut to its
block's limit (header 150, section 3000, field 2000, button 75 characters); a
Control Center URL over 3000 characters drops the button rather than cut it.

| Setting | Meaning |
|---|---|
| `UA_APPROVAL_SLACK_CHANNEL` | Slack channel ID for cards. Unset (default) turns cards off. |
| `UA_SLACK_CHANNEL_IDS` / `UA_SLACK_CHANNEL_MAP` | Must contain that channel; otherwise every card is refused and dead-lettered. |
| `UA_SLACK_ENABLED=1` | The Slack kill switch. Off refuses cards like any other outbound post. |
| `SLACK_BOT_TOKEN` | Bot token with `chat:write`; the bot must be in the channel. |
| `UA_PUBLIC_URL` (or the OAuth issuer) | Control Center origin for the button, as for the phone push. Without it the card has no button. |

- Dedupe keys: `approval:<id>:requested` for the card and
  `approval:<id>:decided:<status>` for the threaded follow-up. A repeat of an
  identical request reuses the pending row and posts nothing new.
- Durable intents: after the approval row commits, each card or follow-up is
  written to the `approval_slack_outbox` table; the Slack call runs on a
  worker, never on the approval path. Workers claim intents atomically, a hub
  restart resumes unfinished ones (post-init hook `approval_slack.startup_drain`),
  and a claim older than ten minutes from a dead process is reclaimed. A send
  that died mid-POST is dead-lettered as `unconfirmed-send` rather than risk a
  second card (delivery is at-most-once; see the slot release command above).
  More than 64 outstanding intents dead-letter new ones as `queue-overflow`.
- Retries: transport errors, HTTP 429 and 5xx, and the API errors `ratelimited`,
  `internal_error`, `fatal_error`, `service_unavailable` and `request_timeout`
  are rescheduled (three attempts, backoff 1 s then 4 s). A `Retry-After`
  header is honoured when longer, up to 300 s; a longer one dead-letters as
  `retry-after-exceeds-cap`. Other API errors (for example `not_in_channel`)
  fail at once.
- Ordering: a follow-up waits until its card is confirmed and is never posted
  unthreaded. If the card failed, cannot be threaded, or stays unconfirmed for
  ten minutes, the follow-up is dead-lettered (`parent-card-failed`,
  `parent-card-not-threadable`, `parent-card-unconfirmed`).
- Each attempt and every terminal loss appends `approval.delivery` with
  `approval_id`, `event`, `channel_id`, `attempt`, `outcome` (`delivered`,
  `duplicate`, `retry`, `failed`, `refused`, `dropped`), `dead_letter`, an
  `error` code and, on delivery, the Slack `ts`. No URL or token is recorded.
- Doctor row `approval_slack_cards`: DORMANT unset; MISCONFIGURED when the
  channel is not allowlisted, the channel map is malformed, `SLACK_BOT_TOKEN`
  is unset or `UA_SLACK_ENABLED` is off; DEGRADED after a dead letter in the
  last 24 hours or with intents left undrained; UNKNOWN when configured but no
  card has been delivered to that channel yet, or when the records cannot be
  read; LIVE once a delivery is recorded.
- Operator step to go live: set `UA_APPROVAL_SLACK_CHANNEL` on the hub unit,
  invite the bot to that channel, restart the hub, then request one real
  approval and confirm the card, the follow-up and the doctor row.

### Completion return (task #1751)

Whoever asked for a task in a thread hears back there when it finishes, so
nobody has to poll the queue.

- **Origin.** A `/task` created from an Events API message records a
  `task_origins` row (surface, channel, thread, requester) in the same
  transaction as the task. The values come from the signed, allowlisted
  request: the event's user, channel and thread root (`thread_ts`, or the
  message `ts` for a top-level message), never from the message text. A slash
  command with no thread records none. One origin per task; the first wins.
  Zulip has no task-creation path, so only its delivery side exists. Other
  adapters call `completion_return.record_origin(task_id, surface, channel,
  thread, requester)` with values from their own authenticated context. The
  Control Center conversation handoff (#1426) records a `messages` origin
  with a task it creates (#1993), in the task's transaction: the conversation
  id as channel and thread, the verified caller as requester. Linking an
  existing task records none.
- **Outbox.** When a task with an origin moves to `done`, `blocked` or
  `cancelled`, a `completion_deliveries` row is appended inside the same
  transaction as the status write, so a rolled-back write leaves none. This
  covers every status writer: `task_update` and the merge closeout queue it
  through `run_records.record_transition_in_tx`, and `complete_direct` (used
  by close-on-merge, the merge orchestrator and the reviewer auto-close)
  queues it from its own write. Other changes (claims, reopening to `open`
  or `doing`, hygiene releases) send nothing. The row is keyed by
  `(task_id, revision)`, where the revision is the task's `updated_at` for
  that transition, so one transition is queued once. Moving the task back out
  of one of those statuses supersedes any row not yet delivered: at once when
  the writer goes through the hook, and otherwise (a merge or routine reopen,
  a claim of a blocked task) at the latest just before sending, when the drain
  re-checks the status. Rows carry
  `org_id` and every read is tenant-fenced.
- **Drain.** `completion_return.drain_pending` runs after the commit (on the
  Slack outbound worker pool), at start-up (the
  `completion_return.startup_drain` post-init hook) and on each queue-hygiene
  pass that sends notifications (`send_notifications=False` skips it). It
  first abandons any row older than 24 hours (`DELIVERY_MAX_AGE_HOURS`) with
  `last_error` `expired`; an expired row is never sent, so enabling a surface
  after a backlog built up does not flood old threads. It then picks only
  rows that are due (not waiting out a backoff, not freshly claimed by
  another drain), so a backlog of waiting rows cannot starve a new one. It
  sends one message per task: older pending rows are marked superseded and
  the newest status is sent. The task's status is checked again right before
  the send, and a row whose task has since moved to another status (a reopen,
  another writer) is superseded, not sent; a reopen during the send leaves
  the row superseded, not delivered. Delivered rows are never resent. A
  failed send is retried with backoff up to five attempts (`attempts`,
  `last_error`), then marked `abandoned_at`. Rate limiting defers without
  counting anything. A Slack slot reserved with no ledger entry (a send that
  died mid-way) defers and is counted in `deferrals`; after ten
  (`MAX_STUCK_DEFERRALS`) the row is abandoned with `last_error`
  `slack slot stuck`.
- **Delivery guarantees.** Slack is at most once per row: the send's
  `dedupe_key` (`completion-return:<row id>`) makes a replay after a lost
  "delivered" mark a recorded duplicate, not a second message. Zulip is at
  least once: its send API takes no dedupe key, so a crash between the send
  and the "delivered" mark posts the notice again on the next drain.
- **Message.** Slack posts in-thread through `slack_outbound.send_message` as
  the coordinator agent, so the hourly cap, allowlist and ledger apply. It
  holds only the task id, the status, the title (secrets redacted, Unicode
  format characters such as bidirectional overrides removed, one line, at
  most 120 characters, inside a code span with `@`, `://` and `<>&`
  neutralised so it cannot ping `@channel` or carry a link), the
  `evidence_ref`, and a Control Center link when `UA_PUBLIC_URL` is set.
  Never note text. The `evidence_ref` is shown only when it is a commit SHA
  (as code) or an `https` URL. An `https` URL on `github.com` or on the
  `UA_PUBLIC_URL` host is a link; any other `https` URL is shown inside a code
  span, not as a link. Other schemes and other text are dropped.
- **Configuration.** Slack delivery needs the same switches as inbound acks:
  `UA_SLACK_ENABLED=1`, `SLACK_BOT_TOKEN` and `UA_SLACK_REPLY=1`. Zulip
  delivery needs `UA_ZULIP_RETIRED=0` and `UA_ZULIP_REPORT_AGENT`, and posts
  to the origin's channel and topic. The `messages` surface needs
  `UA_MESSAGES_REPORT_AGENT`, a service label on the roster; the notice is an
  `inform` hub message from that label to the requester in the origin's
  conversation, linked to the task (`task_id`). It is at most once per row:
  its `message_id` (`completion-return-<row id>`) makes a resend a recorded
  duplicate, counted as delivered only when the recorded message has this
  notice's recipient, conversation and task (otherwise the row is skipped
  with `skipped: notice id holds another message`). A notice the
  conversation handoff does not authorize (no handoff evidence, the requester
  no longer a participant) is skipped once with `skipped: not authorized by
  its conversation handoff`, not retried; handoff events are kept by event
  pruning. A task that changes status during the send is not an attempt: the
  next drain supersedes the row. A requester that hub mail cannot address is skipped, not
  retried: the row is closed at once with `abandoned_at` and `last_error`
  `skipped: operator recipient` (or `skipped: recipient not on the roster`).
  That is the human operator's login on a closed `UA_AGENT_LABELS` roster:
  `UA_OPERATOR_LABEL` is never a roster label, and the hub has no operator
  inbox for notices (attention items come from task state). The operator
  still sees the task's status in the Control Center. On an open roster the
  notice is delivered into the conversation to the operator label.
  Rows for a surface that is not configured stay pending and are sent once it
  is, if that is within 24 hours; after that they expire. The drain never
  raises.

### Opt-in continuous Slack chat (Gate 3 / Zulip #523 port)

`unified_agent.slack_chat` classifies natural-language Events API messages as
inert `chat` requests (not slash commands). They never create tasks, approve
work, execute tools, or resolve red-tier actions. Replies are disabled by
default and require the existing Slack gateway controls plus a target:

```text
UA_SLACK_CHAT=1
UA_SLACK_CHAT_CHANNELS=requests:coord
UA_SLACK_CHAT_MAX_PER_HOUR=30
UA_SLACK_OUTBOUND_WORKERS=4
UA_SLACK_OUTBOUND_MAX_QUEUE=100
```

Targeting is explicit (`[target: agent-hermes]` / `[to: …]`), a single named
agent mention (`@agent-cursor`), or an existing Slack thread binding stored
in `slack_chat_conversations`. Broadcast and ambiguous mentions are ignored.
Bot/app echoes and bot users are dropped before ingest and excluded from thread
history context so replies cannot recurse. Each `event_id` is atomically
claim-deduped with rolling hourly rate-limit enforcement under SQLite `BEGIN IMMEDIATE`;
the LLM reply is scheduled on a managed, bounded background worker pool
(eliminating raw unmetered daemon threads) and posted as `[{agent}] {body}` with a
conversation-only footer bounded to `MAX_TEXT_CHARS`. The body opens with a line saying
it is an LLM-generated reply in the agent's name, not from the agent itself (task #1863):
the reply is one LLM call, and the agent's own session never sees the Slack thread, so
nobody should read it as that agent's answer or commitment. If the background queue is saturated,
requests are rejected before acceptance. In-flight claimed replies that survive a worker/hub
restart can be recovered via `recover_stale_slack_chat_claims()`. Actions still require the
web UI or an approved slash command. Retire Zulip live chat only after this path dogfoods
green — no dual-write.

Live dogfood remains the operator-gated (bot token + flags + hub restart). This
branch does not claim a production Slack round-trip.

## Public TLS gateway (task #558)

Scaffold lives under `deploy/gcp/slack-gateway/`. It is a **stateless** Python
stdlib forwarder (`forwarder.py`) intended to run on the GCP Tailscale worker
(or any cloud host) behind a TLS terminator:

1. Slack POSTs to the public Request URL `https://<tls-host>/api/slack/events`.
2. The forwarder verifies `SLACK_SIGNING_SECRET` (v0 HMAC + five-minute replay
   window) and rejects invalid/replayed bodies with HTTP 401 **before** any
   hub call.
3. Authenticated requests are forwarded unchanged (body +
   `X-Slack-Request-Timestamp` + `X-Slack-Signature` + `Content-Type`) to
   `MAC_HUB_URL`, which must be the Mac hub path
   `http(s)://<mac-tailscale-host>:8765/api/slack/events`.
4. The Mac hub re-verifies the signature and performs allowlist checks, durable
   `slack.inbound` ingest, and dispatch. The cloud process never opens SQLite
   and never calls task/plan/approval primitives.

Operator details (env vars, health checks, rollback, the operator-only secret and
Request URL steps): `deploy/gcp/slack-gateway/RUNBOOK.md`.
Evidence placeholders: [`deploy/gcp/slack-gateway/evidence-template.md`](../deploy/gcp/slack-gateway/evidence-template.md).
Unit tests (no live Slack): `tests/test_slack_tls_forwarder.py`.

Live enablement remains the operator-gated: Signing Secret, user/channel allowlists,
public TLS hostname, Slack Request URL paste, Mac hub env + restart.

## Frontier-model boundary

`frontier_policy()` is a fail-closed integration boundary, not a provider
implementation. `record_frontier_usage()` is the paired redacted telemetry
writer. It records provider/model, agent/project, duration, token counts, cost,
outcome, and an optional request dedupe key—never prompts, responses, or
credentials. The policy requires all of the following:

- `UA_FRONTIER_PROVIDER` names a supported server-side credential environment
  (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or
  `OPENROUTER_API_KEY`).
- `UA_FRONTIER_MODELS` explicitly allowlists the requested model.
- `UA_FRONTIER_MAX_BUDGET_USD` and the per-request prompt/output limits are
  positive and respected.
- `UA_FRONTIER_KILL_SWITCH=1` denies every request immediately.

The policy returns provider/model/budget/timeout metadata only. A future cloud
provider client must inject the credential server-side, pass bounded limits,
record only redacted usage telemetry (provider, model, agent, project, duration,
token counts, cost, outcome), and never include prompt text, Slack text, keys, or
provider responses in logs. This branch deliberately does not claim that a
frontier provider call is implemented. No production code calls
`frontier_policy` or `record_frontier_usage`, so live Slack traffic is not
gated by `UA_FRONTIER_KILL_SWITCH` or the `UA_FRONTIER_*` limits.
`SlackConfig.from_env` still parses those variables, so a non-numeric limit
raises when the Slack configuration loads.

## Zulip retirement and rollback boundary

The existing Zulip worker, schemas, and historical event rows remain intact for
audit and migration. Stop its service only after Slack health checks pass. A
rollback is a human-operated decision: disable Slack, restart the canonical
hub, and use the existing Zulip reader in read-only mode. Do not enable dual
writes, delete Zulip records, or let old Zulip claims override current hub
state.

## The operator-only setup and follow-up

1. Create the Slack app, enable signing and the required event/slash-command
   subscriptions, and restrict installation to the five protocol channels.
2. Record the exact Slack channel IDs and approved user IDs in the secret
   environment; verify that the allowlist is non-empty before enabling traffic.
3. Provision the cloud gateway's Slack secrets and, separately, the frontier
   provider credential in a server-side secret manager. Do not copy either to
   the Mac hub unless the chosen deployment requires it.
4. Configure the cloud-to-Mac authenticated hub route and verify that only the
   Mac hub's `memory.db` changes after a signed test request.
5. Live outbound dogfood still needs the operator to set `SLACK_BOT_TOKEN`,
   `UA_SLACK_REPLY=1` (and optionally the mirror flags) on the canonical hub.
   Code and tests for threads, dedupe, rate-limit, and best-effort acks/mirrors
   are in-tree (`slack_outbound.py`). Do not enable dual-writes to Zulip.
   Remaining follow-ups: Socket Mode (not required), dispatch-failure replay
   operator, aggregate frontier budget ledger.

## Mac hub enablement checklist (task #529)

Agent-safe preflight (no secrets, no restart) — already verified in-repo:

1. Code path: `slack_gateway.handle_http_request` + `POST /api/slack/events` on
   `main` (shipped with #524 / PR #243).
2. Synthetic signed round-trip: `tests/test_slack_gateway.py` exercises HMAC
   accept/reject, exact user/channel allowlists, dedupe, and dispatch into
   `task_add` / `delegate` / plan / approval primitives
   (`test_http_adapter_enforces_exact_user_and_channel_ids`,
   `test_http_adapter_handles_signed_slash_command`,
   `test_dispatch_uses_existing_primitives_and_human_approval_handoff`).
3. Docs: this page + `docs/configuration.md` env table. Never put secrets in
   vault, tasks, or journal notes.

The operator-gated live enablement (Mac canonical hub):

1. Put secrets only in `~/.unified_agent/service.env` (or LaunchAgent
   EnvironmentFile), never in the vault:
   - `UA_SLACK_ENABLED=1`
   - `SLACK_SIGNING_SECRET=…`
   - `UA_SLACK_USERS=…` (non-empty exact user IDs)
   - `UA_SLACK_CHANNEL_MAP=…` (all five protocol channels)
   - Keep `UA_FRONTIER_KILL_SWITCH=1` until a separate frontier enablement
     decision.
2. Point the Slack app Request URL at the TLS ingress that forwards to the
   Mac hub `POST /api/slack/events` (cloud gateway or Tailscale Serve — never
   bind the hub to `0.0.0.0`).
3. Restart the Mac hub unit once (`launchctl` / `systemctl --user restart
   unified-agent`) so env is loaded. Agents must not restart the hub.
4. Send one signed synthetic event (or a real allowlisted mention) and confirm:
   - HTTP 200 / `dispatched: true`
   - one new `slack.inbound` event
   - one task/delegation mutation in Mac `memory.db`
   - duplicate delivery is a no-op (`duplicate: true`)
5. Only after that evidence: leave Zulip read-only; do not delete Zulip history.
   Rollback = `UA_SLACK_ENABLED=0`, restart hub, keep Zulip reader.

Evidence for #529 agent lane: pytest green on the Slack suite + this checklist.
Live signed production round-trip remains the operator-owned (secrets + Request URL
+ hub restart).
