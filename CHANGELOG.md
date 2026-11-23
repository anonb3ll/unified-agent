# Changelog

All notable changes to unified-agent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

- **event_log_query gates match its HTTP siblings (#1811).** `/api/events` keeps
  the operator's human-recovery exemption from `UA_AGENT_ACCESS_ENABLED=0`, as
  `/api/ledger/rows` and `/api/events/stream` do, through a grant scoped to that
  one read-only call; agents stay refused, and a refused read now answers
  403/429/202 instead of an error inside a 200. The recovery auth channels are
  one shared constant (`agent_access.OPERATOR_RECOVERY_VIA`). The registered-tool
  wrapper now gates any tool listed in `UA_APPROVAL_TOOLS`, with the governed
  reads' payload shape, and `routine_fire`'s dedicated branch does too. Tools
  that already gate themselves (`server._SELF_GATED_APPROVAL_TOOLS`, checked
  against the source by a test) keep their own per-action rules, so no call is
  gated twice. An allowed `event_log_query` call no longer
  writes an `agent.gateway.decision` row into the log it reads, so `after_seq`
  paging reaches an empty page; refusals, parked approvals and errors are still
  recorded.

- **HTTP security repair (#1880):** dispatch-stack launch fails closed when task or dependency previews fail, with two guarded reads authorized only during the admitted POST preview. Agent workflow steps retain explicit profile denials; verified human browser workflows retain their reviewed authority. Expected browser reads no longer emit missing-MCP-session diagnostics.

### Security

- **Session-scoped hub MCP credentials for node-launched ACP sessions (#1744), off by
  default.** With `UA_ACP_SESSION_MCP=on`, ua-node asks the hub for a per-session
  `uas_` credential when the harness advertises `mcpCapabilities.http` and passes it
  only in the HTTP `McpServer` header of `session/new`, `session/resume` or
  `session/load`. The hub stores its SHA-256 only, accepts it on `/mcp` only, from the
  node's own device, for nine tools (never more than the label's default profile),
  checks it against the database on every request, and revokes it on receipt, cancel,
  task close or delete, lease expiry, device revoke or operator action
  (`POST /api/action/session-credential-revoke`). Minting needs a device-code sign-in,
  never a static bearer. Every decision made with it carries a `session` block and
  `episode_id` `acp:<item>`. The `uas_` shape joins the shared credential patterns, and
  the hub now redacts credential shapes from node ACP events. Device-code sign-ins now
  carry a `signin_id` kept across refresh rotations, and a refresh rotation now
  replaces the stored row in one transaction; each session credential is bound to the
  sign-in that minted it and stops with it. New settings:
  `UA_ACP_SESSION_MCP`, `UA_ACP_SESSION_MCP_NODES`, `UA_ACP_SESSION_DEVICE_CHECK`,
  `UA_NODE_SESSION_MCP`.
### Fixed

- **A runtime-built key is the JWK type or a private member, not both (#2781).**
  The YAML block reader counted one interpolated key (`${name}:`, as in a
  JavaScript template literal of report text) as the mapping's key type and as a
  private member, so a report line in vendored plugin code (an interpolated
  key, a colon, a value, then a line break) was masked whole and the steward
  audit graded it HIGH. Such a key is now
  private only when another key in its mapping can be the type: a `kty` key, a
  second interpolated key, or a merge/alias document read conservatively. Typed
  mappings mask exactly as before. A lone `"${1:3000}": "<value>"` snippet key
  is no longer reported (#2222's test updated); the member scan already treats a
  lone `d` with no type the same way.

- **Safe steward audit alert diagnostics (#1891).** Failed notifications record
  an allowlisted error category, notifier exit code, validated run ID and UTC
  send start/finish times. Stderr capture is limited to 4096 bytes while excess
  output is drained. Exit 124 identifies a timeout even without error text;
  only recognized first-line error prefixes influence other categories. Raw
  sender output, recipient handles, credentials and message bodies are not logged.

- **Unknown `settings.toml` keys name their likely fix (#1805).** A mistyped section or key
  now reports "did you mean ...?" from the typed schema (`SettingSpec`) in the
  `unified-agent init` refusal, `settings check` and the startup warning. One `settings.unknown_keys()`
  helper feeds all three. Startup still warns and skips unknown keys rather than failing,
  and a `schema_version` mismatch still fails hard.
- **Headless prompts can no longer become CLI flags (#1874).** Following the claude and codex
  fix, cursor now puts `--` before the prompt, hermes binds it with `--oneshot=<prompt>`, and
  copilot and devin bind it with `--prompt=<prompt>` and `--print=<prompt>`. A one-word cursor
  prompt is refused, because the CLI runs a subcommand of that name (such as `logout`) even
  after `--`. agy is unchanged: its `-p`
  already takes the next token as the prompt.

### Added

- **Design: session-scoped hub MCP credentials for node-launched ACP sessions (#1744).**
  `docs/designs/session-scoped-mcp-credentials.md` proposes a short-lived, revocable,
  per-session credential passed in `session/new` `mcpServers` when the agent advertises
  `mcpCapabilities.http`, narrowed to a nine-tool profile and attributed in `event_log`.
  Approved as option A on 2026-10-06; phase 1 is under Security above.
- **Run coverage diagnostics (#1836).** Completed-task coverage now explains
  missing runs with full-cohort reason counts and up to 200 task details showing
  retained claim, matched finish, and separate unmatched finish evidence.
  Explicit totals and truncation identify omitted details. Totals and diagnostics
  share a read snapshot; existing coverage semantics stay unchanged, and absent
  retained evidence does not establish that a task was never claimed.

- **The organization chart can declare service identities (#1877).** A
  label that sends hub messages as a service (for example the in-process
  brain bridge) sits in a closed `UA_AGENT_LABELS` roster but holds no
  bearer, runs no sessions and belongs to no office. Listing it under
  `service_identities` in the chart tells `system_doctor`'s `org_registry`
  check that the missing office is expected; a declared service identity
  that the roster omits, or that an office also lists, is reported instead.
  Each entry must be a well-formed identity label; the chart loader and the
  org config-revision commit reject a malformed or non-list value before it
  is written. Catalog metadata only: it grants nothing and activates no
  profile.

- **Hard-deleted memory no longer leaves auxiliary rows (#1815).** Index-note
  cleanup and migration-import rollback remove vector mirrors and recall votes
  within the caller's transaction and tenant fence, before SQLite can reuse a
  note's rowid. Local maintenance can preview and explicitly repair historical
  auxiliary orphans, including stale vectors attached to reused rowids; no
  automatic sweep, schema migration or soft-delete behavior change is introduced.
- **Delayed recall/upvote bookkeeping does not recreate orphan votes (#1885).**
  Vote insertion rechecks the current note and caller's tenant in the same
  atomic write, preserving valid counters and query-quality logging after a
  concurrent hard delete. This is a separate follow-up to #1815's deletion
  cleanup, with no live repair or schema change.

- **Explicit agent-cloud-agent17 hub host relay (#1789).** A source-only module command reads
  authenticated context packets and explicitly selected onboarding notes, preserving them
  until explicit local consumption. Separately authorized handoffs use stable
  message IDs and reconcile stored intent before retry. The relay refuses
  credential/resource mismatches, MCP redirects and bodies the hub would redact;
  it installs no hooks, service, console entry point or cloud network access.

- **Design: waking an idle agent session on hub mail (#1862).** Spike note
  `docs/designs/2026-10-wake-idle-session.md` compares four mechanisms and recommends a
  host-local waker carried by ua-node that types a fixed "N new hub messages" line
  through the herdr agent API into opted-in sessions only. The proof of concept in
  `scripts/spikes/` runs on a private herdr server and exits non-zero if any of its
  consent, debounce, rate-limit, injection, busy-hold or failure checks fails.

- **Messaging bridges (#1863).** `unified-agent-bridge run-once [--brain] [--a2a]
  [--dry-run] [--baseline]` carries messages between the hub mailbox and the two
  buses outside it. Brain to hub: a unified_brain thread entry by a cloud roster
  member, addressed to a hub label, becomes one `act=request` message in
  conversation `brain:<thread-id>`, sent from the bridge's service label
  (`UA_BRIDGE_SENDER`, default `brain_bridge`) with the thread's claimed author
  marked unverified, and a derived `message_id` so a re-run never sends twice.
  Hub to brain: any hub agent's message in that conversation is appended to the
  thread under the replying agent's label, once (state file plus an in-thread
  marker), written in process in `brain.py reply`'s exact format; the bridge never
  executes code from the brain repo. Hub to Hermes, off by
  default (`UA_BRIDGE_A2A=1`): mail to a label with a loopback A2A endpoint in
  `UA_BRIDGE_A2A_PEERS` is sent to its live gateway session as an A2A v1.0
  `SendMessage`, and the answer returns as a hub reply. No credential is added; a
  remote gateway needs a per-peer A2A token, which is the operator's call. Relayed
  text is capped, framed as untrusted and never acted on. No timer ships; the
  always-on runner is the operator's decision (#1659). See
  `docs/messaging-bridges.md`.

- **Resume and adopt harness sessions (#1741).** ua-node records the ACP
  agent's resume capability at `initialize` (`sessionCapabilities.resume` →
  `session/resume`, `loadSession` → `session/load`) and the hub keeps it, with
  node, backend and cwd, on the `agent_sessions` row keyed by the ACP session
  id. The Sessions pane offers Resume (a new message into the same session,
  approval-gated) only where that is real, and otherwise says Start again
  opens a fresh session; an agent that cannot reopen a session is refused,
  never silently given a new one. Nodes list native Claude Code and Codex
  sessions under `UA_NODE_WORKTREE_ROOTS` (metadata only, symlink and `..`
  escapes excluded), and Adopt records one under the caller's identity,
  idempotent per session and refused for another label's session, then
  resumes it with `claude --resume <id>`, `codex exec resume <id>` or ACP.
  A finished ACP session idles `UA_NODE_ACP_IDLE_S` (default 60 s) for
  follow-ups before it is reaped, and stays resumable. New routes
  `GET /api/nodes/native-sessions`, `POST /api/action/node-resume` and
  `POST /api/action/node-adopt` (`session_list` + `crew_view` ceiling), new
  table `node_dispatch_resume`, node feature `session_resume`. See
  `docs/harness-control-matrix.md`.

- **Queue and steer follow-ups for live ACP sessions (#1740).** The Sessions
  transcript has a composer for a running `acp:*` dispatch. `queue` sends the
  message as the next `session/prompt` after the current turn ends (one per
  turn, in order); `steer` interrupts now, emulated as `session/cancel` then
  `session/prompt`, or injected through `_session/steering` when the agent
  advertises it at initialize. Each message carries an `idempotency_key`, so a
  retried click is answered with the first follow-up (`duplicate: true`).
  ACP events gain `turn_id` and the normalized `turn_start`, `turn_end`,
  `turn_aborted` and `session_end` kinds next to the raw `sessionUpdate`
  kinds; the mirrored `agent_sessions` row now closes on `session_end`. New
  routes `POST /api/action/node-followup` (CSRF; `session_list` and
  `crew_view` ceiling) and device-plane `POST /api/node/followups`, both in
  `route_scopes.py`. Each follow-up passes the `node_dispatch` approval gate on
  its own text (`202 approval_pending` until approved). The node acknowledges
  follow-ups it received, so a lost poll reply loses nothing, and every path
  that ends an item drops its undelivered follow-ups with a
  `followup_dropped` event. The hub refuses follow-ups for a node that does
  not report `node_features: ["acp_followups"]`. See
  `docs/harness-control-matrix.md`.

- **Control Center Messages pane (#1864).** Agents → Messages lists
  conversations (participants, last-message preview, unread and waiting-on-reply
  counts), opens a thread with read and answered indicators per message, and
  composes to any agent or one live session (act, `expects_reply`, `reply_to` when
  replying). New routes: `GET /api/messages/conversations`,
  `GET /api/messages/conversations/{conversation_id}`, `GET /api/messages/sessions`
  and CSRF-gated `POST /api/messages`. The sender is always the verified identity;
  the send runs through the registered `message_send` tool and is idempotent on a
  client `message_id` that the composer reuses on retry. Reads take the
  `message_list` ceiling and scope (operator login and chief-of-staff see all,
  others their own mail) and never change read state. Bodies render as text.
  Refresh is `agent.message.*` stream invalidation plus a 30-second fallback. The
  routes are `operator=True` (#1865): the verified human operator passes the
  ceiling its profile lacks; an agent on the operator profile does not. See
  `docs/web-ui.md`.

- **Inbox endpoints for hooks (#1861).** `POST /api/inbox/session` registers the
  caller's session (idempotent on its label and `external_session_id`, which also
  heartbeats) and `GET /api/inbox/summary` returns a read-only digest of its unread
  mail (`unread`, `newest_seq`, and the newest `limit` messages, at most 20, each
  with a 160-character preview). Both are bearer-only, so a cookie or OAuth session
  is refused and neither takes a CSRF token, and each costs one SQLite connection
  and indexed queries only: no MCP session and no read state change. A
  `session_id` must be the caller's own. Declared in `route_scopes.py`
  (`session_start` and `message_list` ceilings). See `docs/interfaces.md`.

- **Queue closure: stale, park, fold, close-on-merge (#1848).** A task with no
  claim, status change or note for 30 days (`UA_QUEUE_STALE_DAYS`) is surfaced as
  stale and can be PARKED with a required reason; children of a parked epic FOLD
  into it; merged work closes with its merge SHA as `evidence_ref`; nothing
  closes on a date. Parked is `archived_at` on an unfinished task plus a recorded
  `[parked @ ts by actor] reason` notes line (reversible with `queue_unpark`), so
  there is no new table or status; `task_list(parked=True)` is the parked view and
  `task_claim` refuses a parked task. New `coordination_manage` actions
  (`queue_health`, `queue_stale`, `queue_sweep`, `queue_park`, `queue_fold`,
  `queue_unpark`, `queue_close_merged`) keep the default profile under its tool
  cap; `unified-agent-review queue ...` and a `queue_closure` doctor row expose
  the reads. `queue_sweep` is a dry run that returns a PROPOSED list and writes
  nothing; park and fold never run automatically. Close-on-merge runs in the
  hygiene pass only where the task's `evidence_ref` or branch names exactly one PR
  in the pr-watch cursor (now carrying `merge_sha`) whose head branch names the
  task, never on operator-only, `tracking`, human-gated, red-tier or live-claimed
  rows. It proposes by default and applies only with `UA_CLOSE_ON_MERGE=1`. `queue_health` also counts review-ready milestones still unjudged
  (the numbers #1840 reads). See `docs/work-queue.md`.

- **Address one session of an agent (#1860).** `message_send` takes
  `to_session` (a hub `session_id`): the session's agent label becomes the
  recipient (`to_agent` is then optional) and only that session sees the
  message; mail addressed to the label still reaches every session of it. The
  target is stored in a new nullable `message_index.target_session` column (added
  by the idempotent ensure-schema pass, no schema version bump) and on the event
  payload, so a rebuilt index keeps it. `message_list`, `message_inbox` and
  `message_ack` take `session_id` to act as one of the caller's sessions, which
  must belong to the mailbox owner. Read state is per session for label mail: a
  read or ack through a session is a `message_reads` receipt keyed
  `(message_id, session_id)`, so label mail read by one session stays unread for
  the others; a read with no session still means "the agent read it" and counts
  for all sessions. `session_start` is now idempotent on `(agent,
  external_session_id)`, takes `host`, and a verified caller can register only
  under its own label. Sessions report `last_seen_at` and `live` (open and seen
  within `UA_SESSION_LIVE_MINUTES`, default 30); mailbox calls that pass a
  `session_id` refresh it. `session_list` gains `live_only`, and `agent_context`
  lists each peer's live sessions with host, branch, task and `last_seen_at`.
  `message_send` also takes `from_session` (validated as the sender's own
  session, recorded on the message), and a `reply_to` reply to a message that has
  one defaults its `to_session` to it, so sessions can converse. A new session
  sees older unread label mail. `crew_intervene` now reaches the targeted session only; the worktree
  `interventions.jsonl` line is documented as an audit copy, since nothing reads
  it for delivery. No new tools.

- **Typed messages gain mailbox semantics (#1859).** `message_send` now writes
  a derived per-recipient `message_index` row in the same transaction as the
  `agent.message.*` event (the event log stays the source of truth), and
  `message_list` reads that index, so an agent's older mail is no longer dropped
  by fleet-wide traffic (it used to filter the newest 200 events in Python). New
  `message_list` options: a `since` cursor, `unread_only`, `count_only`,
  `waiting_on_reply` and `message_id`; rows now carry `read_at`, `acked_at`,
  `expects_reply`, `answered_by` and a `delivered`/`read`/`answered` status. Read
  and ack state changes through the `coordination_manage` actions `message_inbox`
  and `message_ack` (no new tool, and `message_list` stays read-only). A reply
  with `reply_to` joins its parent's conversation and must name a message the
  sender sent or received. `message_send` takes `expects_reply`. The table is
  created and backfilled idempotently by `storage._migrate` (no schema version
  bump); backfilled mail older than three days starts read, newer mail
  unread.

- **Every Control Center route declares who may call it (#1745).**
  `src/unified_agent/webui/route_scopes.py` maps each served `(method, path)`
  to a scope: `public`, `own-auth`, `any-authenticated` or `capability`, with
  the profile tools the handler requires, a CSRF flag, and tags for the checks
  that stay in the handler. `web_routes()` wraps every route so the declared
  guard and profile capabilities run before the handler, and a route with no
  entry answers 403, so a new route cannot ship authenticated but with no
  authorization decision. `tests/test_route_scopes.py` fails on a served route
  with no entry, an entry matching no route (including the OAuth routes and
  mounted sub-apps), a mutating route without the CSRF guard, or a table that
  disagrees with the central authentication gate, and freezes the allowed
  profile matrix. The handlers keep their own checks. No change to who can do
  what; the only difference is error precedence: a profile that lacks a route's
  capability now gets 403 before the handler validates the request body, where
  an empty body used to answer 400 first (10 identity and route pairs, listed in
  the test).

- **The hub sees a failing desktop vault sync (#1813).** Each `ua-node`
  reports its own `vault_sync` doctor row on heartbeats (probed at most every
  5 minutes), and `system_doctor` gains `fleet_vault_sync`, which reports any
  live node whose sync is not LIVE. `deploy/desktop-bootstrap.sh` checks the
  sync target and `--apply` writes it to `settings.toml`, since #954 removed
  the built-in host and vault path.

- **`UA_WORKSPACE` precedence is documented and pinned by tests (#1806).**
  `docs/security.md` now states the chain (service environment, then
  `settings.toml` `paths.workspace`, then the code default) and when each
  source applies, including settings-enabled installs where `init` writes the
  `service.env` line commented out. Two regression tests in
  `tests/test_settings.py` prove `settings.toml` never overrides a preset
  workspace and fills it only when unset. No behavior change. (The
  commented-out line is gone as of #1825; see Changed.)

- **Policy-authorized Approval Ledger outcome (#1407).** A merge covered by a
  recorded standing authorization now settles as its own evidence class
  (`success`, "Policy-authorized: <policy_ref> by <label>") instead of
  "Merged without human approval". The authorization is a signed event-log
  row bound to the exact head SHA and to the task's branch (which must be the
  PR's head branch), recorded under a verified identity listed
  in `UA_POLICY_AUTHORIZERS` (default nobody), with author and authorizer
  kept apart unless `UA_POLICY_AUTHORIZATION_ALLOW_SELF=1`. It never covers
  another head, a human denial on any head of the PR, an unmerged close or red-tier files, and
  evidence reports show it as `proof.merge_authorization` with
  `human_approval: false`. Recording is a pre-merge step: a done or
  cancelled task, or one whose branch was cleared, is refused explicitly,
  and a record made before the task closes stays valid after it closes.
  The task check and the signed append share one write transaction, so a
  task closed concurrently cannot be authorized.
  See `docs/connector-activation.md`.
- **Bounded cloud vault stewardship (#1780, #968).** `agent-cloud-agent17` defaults to
  `cloud-vault-steward`: its eight assistant tools plus
  `vault_read/list/search/write/journal`. Reads, listings, searches, templates,
  embeds and general writes are limited to unlocked Wiki Markdown/text notes;
  PARA, Inbox, root/hidden files, force overrides and other tools remain denied.
  Native journaling alone appends verified-identity entries to daily Logs notes,
  with OS locking across callers/processes to preserve concurrent first writes.
  A note is locked for this scope if either the hub's `locked: true` line check
  or a YAML parse says so; Wiki writes and journaling stay within
  `UA_VAULT_WRITE_DIRS`; journal dates must be within one day of today.
  The eight-tool `cloud-assistant` profile remains available as a downgrade.
- **Operating profiles and per-run comms policy (#1732, #1735).** Named
  profiles in `policy.yaml` set comms, delivery, autonomy, planning rigor,
  model, run budget and standing instructions. They layer org default →
  office → sprint → run, with the setting layer reported for every value,
  and are frozen into each run's claim event. Comms policy (`direct`,
  `funnel` through a liaison, or `custom` allow rules) is enforced on
  `message_send`, `notify`, `batch_notify` and delegate notify callbacks.
  New `goal_manage` actions `operating_*` manage sprints and per-run
  settings. See `docs/operating-profiles.md`.
- **Operating profile fields are live (#1756).** Each run's frozen snapshot
  now drives delivery (sprint and run layers beat the project file and the
  note marker; org and office layers sit below the project file), the
  default autonomy mode of a mission with no explicit mode (the org ceiling
  still tightens it), the dispatch and executor model (same provider only),
  missing `autonomous_cycle_start` budget caps, and an "Operating profile"
  block (planning level and instructions) in dispatch prompts and the
  session packet. A new `relay: {autosend: [...]}` field is parsed and
  recorded for the public relay (#1734). The executor's finish stamp is now
  `[delivery-run: ...]` and is never read back as a task-note override, so a
  finished run's policy cannot govern the next run. A note that gives only
  `mode` takes `yolo` from the project entry before the org or unit layer.
  Sprint or run overrides with no named profile are frozen into the claim.
  The Operating profile block is the last part of CLI and node dispatch
  prompts, after assigned skills. A task's newest-run snapshot is chosen by a
  claim-ordered `claim_ulid`, not the wall clock, so a clock step cannot
  select an older run.

- **Control Center Operating pane (#1757).** One pane to see and set how
  each department, sprint and run operates: profiles with enforced versus
  recorded badges, office bindings, sprint create/status/profile/overrides
  and task links, and per-run settings with a resolved view that names the
  layer behind every value. New routes `GET /api/operating`,
  `/api/operating/resolve`, `/api/operating/sprints` and CSRF-gated
  `POST /api/action/operating-sprint` and `/api/action/operating-run`; an
  agent's write answers 202 with its approval id. Profile and binding edits
  stay on the policy revision path.

- **Public-reply relay with public-safety gating (#1734).** New
  `relay_manage(action, params)` (`check`, `draft`, `send`, `status`). An
  agent's draft for an external channel (`dry_run`, a GitHub issue or PR
  comment, or a Zulip stream) runs deterministic gates. Secrets, private keys,
  hidden Unicode, over-length text and `UA_RELAY_BLOCKED_TERMS` block it.
  Private and tailnet IPs, internal hostnames, home paths, hub ids on public
  channels, emails and phone numbers are redacted. The verdict is recorded as
  `relay.public.verdict` with masked excerpts only. A non-blocked draft is
  queued for human approval (approval tool `relay.public.send`), and the
  approved send is recorded as `relay.public.sending`, then `relay.public.sent`.
  `resolve_relay_autosend` is the hook for operating profiles (#1732) and
  returns false. The tool is pruned from the default developer profile.
  An uncertain send (timeout, connection error, 5xx) leaves the attempt open
  as `relay.public.send_uncertain`. GitHub comments carry an opaque attempt
  marker that is checked before anything is posted again, and an operator
  closes a stuck attempt with `resolve_attempt`.
- **Shared credential shapes (#1734).** `telemetry/redaction.py`
  `CREDENTIAL_SHAPE_PATTERNS` adds the following shapes to telemetry
  redaction, the audit engine's `check_secrets` and the relay:
  - GitHub fine-grained tokens
  - boundary-safe GitHub tokens
  - AWS `ASIA` key ids and secret keys
  - credentials in URLs
  - JWTs
  - Stripe keys
  - Slack app tokens and webhooks
  - Google OAuth tokens (relay and redaction only)
  - HTTP Basic credentials
  - case-insensitive private-key headers
  - PuTTY keys

### Changed

- **The Mac daily-briefing job is retired (#1888).** The daily briefing moved
  from the Mac hub's Hermes to the chief of staff (`agent-cloud-agent17`). On the hub,
  `com.unified-agent.cluster-briefing` is booted out and `launchctl disable`d,
  with its plist kept on disk, and Mac setup now boots out and disables the
  label instead of installing it, so a fresh or rebuilt Mac does not bring it
  back. On macOS, `system_doctor` reports `cluster_briefing_timer` through the
  retired-unit check: DORMANT only when the job is absent and launchd's
  disabled override is set, UNKNOWN when absent without the override,
  MISCONFIGURED if it is loaded. Linux hosts keep the ordinary timer check for
  their own optional `cluster-briefing.timer`.
- **Fleet WIP cap binds direct claims, soft then hard (#1838).** The documented
  fleet limit of 2 was only enforced on dispatcher launches
  (`UA_DISPATCH_MAX_LIVE_RUNS`, which counts `dispatch_runs` slots);
  `task_claim` checked per-agent and per-lane caps only, so direct claims by
  several agents reached six live runs silently. `task_claim` now counts live
  claimed tasks across all agents, a separate gate from the dispatcher's: at
  `UA_FLEET_WIP_MAX` (defaults to the same number as
  `UA_DISPATCH_MAX_LIVE_RUNS`; no slot pool is shared) a new claim returns
  `wip_warnings`, and at `UA_FLEET_WIP_HARD_MAX` (default 3 × soft) it is
  refused. `high`-priority and `[urgency: now]` tasks are warned, never refused;
  `operator-only` and `interactive-only` claims and live renewals are unchanged.
  A claim on a task that already holds a live dispatcher slot (`dispatch_runs`
  or a claimed node item) is not warned or refused, so a dispatched child's own
  claim is never refused after launch; it still counts for other claims.
  `runs_in_flight` adds `wip` (fleet count and caps, dispatcher slots and cap,
  running count by path) and a per-item `claim_path` taken from the source tied
  to the current claim, and now lists every dispatch run that holds a launch
  slot (including cancelled-unconfirmed and runtime-ceiling blocks) with its
  actual state.
- **Operator-authenticated ACP follow-ups queue without a second approval
  (#1872).** A Queue or Steer follow-up from the Sessions pane no longer waits
  on its own `node_dispatch` approval when it is sent with the operator's
  credential: the
  route's existing operator-recovery rule (`UA_OPERATOR_LABEL` as the
  authenticated identity, on a bearer, OAuth, cookie or WorkOS channel) decides
  it from the request, never from the body, headers or profile. Only the
  approval is skipped: revocation and `UA_AGENT_SCOPES` (`dispatch`) still
  apply, through the gate's shared `safety.check_prod_action_authority`, and
  run before admission on both paths, so a revoked or de-scoped sender's retry
  of a queued key is `403`, not a duplicate. Every
  other identity, including an agent whose session runs the operator profile,
  keeps the per-follow-up gate. `node_followups` rows and `followup_queued`
  events record `operator_authored` and `approval` (`not_required_operator` or
  `gate_passed`; columns added in place, no schema version bump). A newly
  queued follow-up now answers `201`; `202` means only `approval_pending`; an
  idempotency key another identity already used, or awaits approval for, on
  the item is `409`. `operator_authored` records the credential used, not that
  a person typed the text. The composer labels such a follow-up "sent with
  the operator's credential" from the reply.

- **`task_update` into `doing` passes the same WIP caps as `task_claim`
  (#1886).** Moving a task into `doing` with `task_update` skipped the
  per-agent, lane and fleet caps, and the row it left (no `claimed_by`) was
  invisible to the fleet count, so the generic update path could fill the
  fleet past its caps. A move into `doing` from another status now runs the
  same checks as a new claim, for the surviving claimant or else the verified
  caller, with the same exemptions, and returns the same `wip_warnings` or
  `ToolError`. Only rows already in `doing` are not checked. A caller with no
  verified identity gets the lane and fleet caps, and its `actor` is no longer
  charged: the run it opens has no agent. Slack `/progress` is such a caller:
  at the fleet hard cap it is refused unless the task is urgent, and the Slack
  reply now says the update was not applied because of the WIP cap instead of
  "queued for human review". Without hub auth, a follow-up worktree call that
  names the same `actor` as an unverified start is refused as
  `run_not_current`. A surviving claim whose lease has
  lapsed (most `blocked` tasks) is renewed when its holder moves the task back
  to `doing`, and dropped when anyone else does, so the row is always counted.
  A move into `doing` that leaves the row archived is refused, as `task_claim`
  refuses a parked task. `wip_warnings` are part of an idempotent receipt, so
  a keyed retry returns them too. The fleet count now includes unarchived `doing` rows with no
  claimant, each treated as holding a one-hour lease from its last write; the
  per-agent count includes such rows attributed to the agent through its open
  run (`current_run_agent`), and the lane count applies the same one-hour rule
  (it counted them indefinitely). An operator's web UI move into `doing` now
  counts against the operator label's per-agent cap. `runs_in_flight` reports
  the unclaimed part as `wip.fleet.unclaimed_doing`. The fleet refusal reads
  `fleet already has N live doing task(s)` (was `live claimed doing task(s)`).
- **`system_doctor` no longer reads a steward-audit alert-delivery failure as
  a broken timer (#1856).** When the scheduled audit runs, finds HIGH-severity
  findings and then cannot write the inbox note or send the alert, its wrapper
  exits 2 and records `delivery_fail`, now with the scan's own exit code as
  `audit_exit_code`. The `steward_audit_cron` check reports that run as
  DEGRADED with a delivery explanation instead of MISCONFIGURED "installed but
  inactive", but only when the scan exited 1 (findings). A scan that errored,
  a record without the scan code, or exit 2 without that run's own record (for
  example a missing or non-executable audit binary) stays MISCONFIGURED.
- **The CTO seats have their own `cto` profile (#1884).** `agent-hermes`
  moves from the developer default, and `agent-server-agent5` from chief-of-staff,
  to `cto`: the developer surface plus a reviewed set of tools developers
  reach only through chief-of-staff (`CTO_EXTRA_TOOLS`): `review_and_notify`,
  `scan_for_reviewable_tasks`, `audit_triage_tasks`, `injection_triage`,
  `event_log_verify`, `state_sync_verify`, `mcp_health_telemetry`,
  `model_quota_status` and `skill_registry`. The rest of the developer deny
  list still applies, so the CTO cannot resolve approvals, change authority
  config, merge, call `notify` directly, schedule routines, cancel dispatch
  or switch profile, and it cannot activate chief-of-staff. A policy overlay
  may downgrade it but never widen it. Vault scope is the developer baseline,
  so `agent-server-agent5` loses PARA writes. Dispatched runs for the CTO seats now
  carry `cto` (skills and `UA_DISPATCH_PROFILE` follow); every other label,
  chief-of-staff ones included, still dispatches as developer.

- **Roster: the desktop Agent5 seat is the CTO, not the chief of staff (#1788).**
  `agent-hermes` no longer has a `chief-of-staff` entry in
  `DEFAULT_PROFILES_BY_IDENTITY` and resolves to `developer` (since #1884,
  `cto`; see above), so it loses
  `approval_resolve`, `config_revision_manage`, `notify`, the `routine_*` tools,
  `git_merge_task`, `profile_activate` and the PARA vault write scope. `agent-cloud-agent17`,
  `Laptop_Agent5` and the `gcp_*` roles keep `chief-of-staff`. `agent-server-agent5`
  kept it too until #1884 moved it to `cto` (see above).
  Fleet enumeration, delegation and handoff stay available through the
  consolidated managers. Persona copy, the web badge and the profile docs follow.

- **HTTP action routes take their MCP twin's profile ceiling (#1865).** The ambient
  profile check only applied when a request carried an MCP session id, so Control
  Center action routes ran registered-tool logic with no profile ceiling: a public
  or research identity could run `/api/action/task`, `/api/action/workflow` and
  `/api/action/crew-intervene` over HTTP although MCP denies it those tools. Each
  action route in `route_scopes.py` now names its MCP twin (`tools`, or a
  consolidated-manager `actions` pair such as `delegation_manage` / `delegate`),
  judged against the profile resolved from the verified identity. The verified human
  operator passes these ceilings (`operator`), since the operator profile is
  read-only over MCP and the Control Center is its write surface; an agent on the
  operator profile does not. Fleet control and harness publish have no MCP twin
  and are the operator's alone. The `task_list` reads (goals, why-chain, comments,
  task search, next-work projection, org proposals) now apply over HTTP as well.
  Access that changes: research loses task update, workflow, crew intervene,
  dispatch, stack dispatch, node sessions, node dispatch, handoff, cycles, plan
  review, canvas write, device sign-in revoke, fleet control and harness publish;
  public loses the same plus dependency and proposal actions; steward loses
  workflow, crew intervene, dispatch, stack dispatch, node sessions, node dispatch,
  dependency, handoff, cycles, plan review, fleet control and harness publish; the
  developer default loses canvas write, device sign-in revoke, fleet control and
  harness publish; chief-of-staff loses fleet control and harness publish. The
  operator keeps every route. A test
  also pins every route `server.py` serves outside `web_routes()` as public or
  own-auth. See `docs/interfaces.md`.

- **Slack chat replies say they are LLM-generated (#1863).** Each
  `slack_chat` reply opens with a line saying it is an LLM-generated reply in the
  agent's name, not from the agent itself, so nobody reads it as that agent's
  answer.

- **`message_list` is scoped to the caller (#1859).** A verified caller lists
  only mail it sent or received; naming another agent's inbox is refused. The
  operator login and the `chief-of-staff` profile still list all mail, and a
  caller with no verified identity (unauthenticated stdio, in-process) keeps
  unrestricted reads. A `reply_to` that does not name a message the sender was
  party to is now refused (references in the `ts:` bridge namespace are
  stored unchecked).

- **The HTTP hub bounds its graceful shutdown (#1812).** On SIGTERM uvicorn
  waited forever for open MCP streams, so launchd SIGKILLed the hub on every
  restart and its cleanup never ran. The hub now cancels open connections
  after `UA_HTTP_GRACEFUL_SHUTDOWN_S` (default 10, at most 25) and exits 0 with its
  `finally` cleanup run. The Mac hub plist sets `ExitTimeOut` 30; an installed
  plist needs that key added by hand (deploys never rewrite it).

- **`service.env` pins the hub sandbox root after init (#1806, #1825).**
  Security: `settings.toml` can no longer move `UA_WORKSPACE` on an
  initialized hub. `unified-agent init` now always writes an uncommented
  `UA_WORKSPACE=` line in `service.env`, settings-enabled or not, choosing an
  explicit `--workspace`, else the existing pin, else `settings.toml`
  `paths.workspace`, else the code default. Re-running `init` on an older
  settings-enabled install replaces its commented-out `# UA_WORKSPACE=` line
  with the value it was running with, so the workspace does not move, and a
  re-run without `--workspace` no longer resets a custom pin to the default.
  `init` warns, naming both paths, when `settings.toml` disagrees with the pin,
  then records the pinned value there. A new `system_doctor` row,
  `workspace_pin`, is `LIVE` when the pin is set and `settings.toml` agrees or
  is silent, `DEGRADED` when `settings.toml` names another path,
  `MISCONFIGURED` when an installed `service.env` has no pin, and `DORMANT`
  without a `service.env`. Run `unified-agent init` once on an existing hub to
  pin it. See `docs/security.md`.

- **Packaged Control Center is fatal only for release builds (#1829, #1689).**
  A non-editable build (`uv tool install .`, `install.sh`, a `git+` install)
  of a tree without a Control Center build now succeeds: the build hook warns
  and packages no `unified_agent/_resources`, and the installed hub's `/ui`
  answers 503 with a build hint. Only `UA_RELEASE_BUILD=1` refuses incomplete
  UI or catalog assets; symlinked resources are refused in every build. A
  built tree is packaged either way, so the installed hub serves `/ui` without
  `UA_WEB_DIST_DIR`, which stays an override for custom or development builds.
  The install guides and `install.sh` messages say so. On HTTP transport
  `install.sh` runs `unified-agent doctor --allow-unbuilt-ui`, which still
  reports `web_ui` MISCONFIGURED but does not fail the install for that one
  expected state; a missing `UA_WEB_DIST_DIR` build and every other doctor
  failure still fail it.

- **Agent enrollment is documented (#1807).** `docs/installation.md` gains an
  "Adding an agent identity (enrollment)" section. On a default install the
  operator adds the label to a closed `UA_AGENT_LABELS` roster, appends a bearer
  to `UA_AGENT_TOKENS`, binds any separate workspace with `UA_AGENT_WORKSPACES`,
  sets `UA_ENFORCE_AUTH=1` for a networked hub (`init` writes `0`), restarts the
  hub, and registers the client without putting the token on a command line.
  The Claude Code registration examples in `docs/harnesses.md`,
  `docs/getting-started.md`, `docs/interfaces.md` and `README.md` likewise replace
  `claude mcp add --header` with a placeholder entry in `~/.claude.json`, and
  the Hub mode example registers with a bearer. The closed-roster step keeps
  the operator label out of `UA_AGENT_LABELS`, since `config.operator_label`
  rejects a label listed there; `README.md` and `docs/configuration.md` state
  the same operator exception for org-chart members. A hub already running with `UA_MANAGED_SECRETS=1` can
  instead issue the token with `agent_token_issue` from a steward or
  chief-of-staff session after a human approves it. Docs only.

- **`agent-cloud-agent17` is the chief of staff (#1799).** On the operator's 2026-10-02
  decision, `agent-cloud-agent17` now defaults to `chief-of-staff`, the profile the
  Hermes identities use, instead of `cloud-vault-steward`. It can now groom and
  claim tasks, comment, delegate and hand off, drive plans, read the approval
  queue, write briefings and file into the vault. A new
  `config.CLOUD_HOSTED_IDENTITIES` roster keeps its cloud-host boundaries
  on the identity rather than the profile, so the profile change does not
  drop them (Security):
  hub workspace access is always denied (a missing or path-valued
  `UA_AGENT_WORKSPACES` entry is ignored), and `resolve_for_audit` refuses it,
  so `security_audit`, `detect_dev_servers`, `skill_review` and skill registry
  publish/drift cannot read the hub's disk either; HTTP stays MCP-only and it cannot
  create a browser session; `profile_activate` refuses `developer`, `steward`
  and `operator` and allows only downgrades; vault writes keep the #968/#1794
  fail-closed lock check, `force` refusal (now also on `vault_move`), canonical
  daily journal target, one-day journal date bound and single-line tags,
  and `obsidian_append` is refused (the plugin writes after the lock check).
  Its vault write scope is the chief-of-staff scope, PARA folders included
  (operator decision 2026-10-02), with those guards on every write. Approvals, the `broadcast` tag and
  token issuance stay human-only through actor-kind checks. No other identity
  and no other profile changes, including `public`.
  `cloud-vault-steward` and `cloud-assistant` remain available as policy
  downgrades. Clients must reconnect after deployment.

- **`storage.init_db()` is schema only; start-up row work runs as post-init
  hooks (#1720).** The autonomy, task-search, skills-registry and
  run-heartbeat DDL moved into `table_schemas` (core). The legacy standing-grant
  migration, the `task_fts` backfill, the open-run cache backfill, the memory,
  task and symbol embedding backfills and the startup vault snapshot are now
  hooks that `post_init.register_default_hooks()` registers and every later
  `init_db()` runs, in the same order as before. The hub, CLIs, workers,
  scripts and the test suite register them, so their start-up is unchanged;
  a process that registers none gets the full schema without the row work.
  The memory embedding backfill moved to `memory_semantic`, and the vector
  math behind the `cosine_similarity()` SQL function to `vector_math`
  (`embeddings` re-exports it). `storage` no longer imports any module above
  core: seven layer-baseline lines are gone. A fresh database's
  `sqlite_master` is byte-identical.
- **Evidence reports say what the evidence proves (#1673).** Report schema
  1.7. `human_provenance.human_authority: "verified"` is now
  `"credential-attributed"`: a bearer or IdP credential bound to a human
  identity was used, which does not prove a present human.
  `"presence-verified"` is reserved for statement-bound passkey assertions.
  Records gain `credential_attributed`; the CSV column `human_verified` is
  now `human_credential_attributed`; Markdown and text render
  `credential-attributed-by-<via>`. New report-level `sod`:
  `not_applicable_single_operator` with one configured human, otherwise
  `pass`/`fail`/`no_data` over in-scope approvals. Older exports still render
  (`"verified"` reads as credential-attributed, on records and on the
  provenance graph's human nodes, whose `canonical_hash` is recomputed), and
  `unified-agent evidence render <export.json>` re-renders a saved export. A
  1.0 record's bare `verified: true` was actor authentication, so it reads as
  `"unknown"` human authority.

- **CI moves back to GitHub Actions with batch verification; Depot CI is
  dispatch-only (#1787).** `.github/workflows/` carries the automatic
  triggers again; every `.depot/workflows/` copy is `workflow_dispatch`-only,
  with notes in its header for switching back. To spend the remaining Actions
  minutes on merge batches rather than on every push, `ci.yml` runs only on PRs
  labelled `ci:full` and on dispatches (`gh workflow run ci.yml --ref
  batch/<name>`). There is no push or schedule trigger, markdown-only PRs never
  start a run, and skipped jobs bill nothing. See `docs/ci-batch-verify.md`.
  The pipeline is reshaped for per-job minute rounding: the classifier,
  held-PR rule and connector conformance are steps in `gates`, and protocol,
  client, web and the SDK-mirror check share one `js` job (12–13 jobs per full
  run down to 7). Checkouts are shallow (a PR's files come from the API), and
  uv, npm, the fastembed model (keyed on its version), Rust and buildx are
  cached. Timeouts are tightened, and a newer push or dispatch cancels the
  superseded run. Pytest shards are split by test count
  (`scripts/ci_changes.py candidates --shard/--total`); pytest-xdist per shard
  is opt-in (`pytest_workers` / `UA_CI_PYTEST_WORKERS`). Every job runs on
  `vars.UA_CI_RUNNER || 'ubuntu-latest'`. `deploy/ci-runner/` sets up a
  self-hosted runner on a GCP spot VM, with root-only credentials and one
  unprivileged user per slot. Docker and the install gate follow
  `UA_CI_ISOLATED_RUNNER` (GitHub-hosted by default), run only after merges
  that move their inputs and by hand, and use per-run container names and
  ports. Batch dispatches check held PRs (`scripts/check_batch_held_prs.py`).
- **The review cron never runs the full suite on the hub for a PR (#1787).**
  Without exact-head CI shards it runs only the test files the diff touches or
  names. When none, or more than `UA_REVIEW_TARGETED_MAX_FILES` (40), map, it
  holds the task as `awaiting-ci`. A task without a PR keeps the whole-suite
  run, and `UA_REVIEW_LOCAL_PYTEST=full` restores it everywhere. A `+yolo`
  merge reuses the review's test scope for the same head. A skipped duplicate
  check no longer masks a failure under the same name, and
  `.github/workflows/`, `.depot/` and `scripts/ci_changes.py` are red tier. The review
  gate, the held-PR check, the CI green-rate metric and `already-tested` accept
  both providers' check names (GitHub Actions reports `test (shard=0)` in
  workflow `CI`; Depot reports `CI / test (shard=0)`).

- **Comms policy: sender-scoped frozen runs and no unverified notify (#1775).**
  A message tied to a task is judged by the sender's own newest run of it,
  so another agent's later claim no longer retires the sender's frozen
  policy. While operating profiles are configured, `notify` and
  `batch_notify` refuse a caller with no verified identity; only named
  hub-internal deliveries (digest flush, anomaly alerts, review pipeline,
  queue hygiene) pass without a sender.

- **Approval Ledger evidence comments are anonymous (#1777).** The public PR
  comment records the resolver's role (`operator` or `human`) and a keyed
  pseudonym for the signing host instead of a login and hostname. The
  producer signature covers neither field, so verification is unchanged.

- **Four more upward imports removed from the layer map, 20 to 16 (#1724).**
  Shared read models and a client moved below their importers:
  `cycle_eligibility` (services) holds the green-path and auto-PR eligibility
  checks and their red-tier and worktree-HEAD seams, so `tools.cycles` no
  longer imports `reviewer`; `delivery_receipts` (services) builds the
  read-only dispatch receipts the evidence report lists; `dispatch_reads`
  (core) holds `list_runs` and the other dispatch-run readers `horizon2`
  uses; and `hub_client` (integrations) holds the hub HTTP transport and
  platform JSON helpers `planning_loop` posts through, which the executor's
  `HubClient` now subclasses to add its in-process fallback. The old modules
  re-export every moved name, so imports, console commands and
  `python -m` paths are unchanged. Tests that patch the eligibility seams now
  patch `unified_agent.cycle_eligibility`.

- **Installation data is explicit and operator/project names are generic.**
  Trusted settings configure project streams, vault/sync targets, repository,
  image publication and optional workers; unset integrations perform no I/O.
  The operator lane migration preserves task IDs, claims, runs and timestamps,
  with one-release read aliases and a canonical SDK-first rollout. Generated
  startup instructions no longer include a fixed private fleet or inbox path.
  Optional projection/capability namespaces use browser/workroom vocabulary
  with the same envelope and restrictions. The export scanner recognizes
  namespace tokens and uses distinct neutral legacy aliases.

- **Approval Ledger polls follow the configured base branch (#954).** Both
  poll transports filter on `github.connector_base`
  (`UA_GITHUB_CONNECTOR_BASE`, default `main`), so a repository whose default
  branch is not `main` is still polled; an invalid branch name is
  unconfigured. The scheduled `pr-watch` pass now exits non-zero when an
  enabled connector is unconfigured, as the worker CLI already did. Doctor's
  `vault_sync` freshness budget uses an interval set in the sync service's
  `settings.toml`. The operator-lane rollback and SDK-first deployment order
  are documented in `docs/operator-vocabulary-compatibility.md`.
  `unified-agent init --force` keeps a file-backed `UA_AGENT_TOKENS` path and
  rotates only this host's agent and operator bearers inside that file (other
  agents keep authenticating; rollback restores it). `init` pins
  `UA_WORKSPACE` in `service.env` only when settings.toml is disabled, so a
  `paths.workspace` edit takes effect; an existing pin is kept in step.

- **Operating-profile hardening (#1764, #1766).** Tied task contexts only
  narrow comms permissions; stored-profile drift degrades with warnings;
  policy revisions reject orphaned references. Digest flushes use system
  authority, run snapshots use keyed lookups with a one-time backfill,
  sprint renames validate their length, and profile read actions avoid the
  mixed-action write approval gate.

- **Unconfigured Zulip integrations keep overview pages readable (#946).**
  Coordination, attention and worker projections return unconfigured status
  and empty links before accessing Zulip credentials, storage or the network.
  Installed site behavior remains explicit; fleet rehearsal tests use their
  own synthetic organization and host mappings.

- **The Cloud pane reads a starting or stopping worker as unknown (#946).**
  Only `RUNNING` is up and only `TERMINATED`, `STOPPED` or `SUSPENDED` is
  stopped. A configured worker in a transitional Compute Engine state no
  longer shows as stopped.

- **Resume memory selection is caller-independent (#1459).** Tasks with ended
  sessions reserve half the injection token cap for experience before excluding
  the caller, so unequal session summaries cannot change the memory included
  in comparable packets. Tasks without ended sessions keep the full budget.

- **PR CI avoids spending a full test run on unfinished work (#1753).** Draft
  PR updates keep lint, smoke, security and named policy gates; expensive
  integration jobs run when the PR is marked ready and on every ready update.
  A final verification check rejects drafts and skipped required jobs, and
  identical-tree main reuse requires that final check. Pytest starts only
  after the cheap gates pass. The GitHub fallback stays manual with full tests.
  The CI green-rate provider samples ready heads only, so intentional draft
  verification failures do not count as failed integration runs.

- **The teamwork rehearsal names its own failures (#1644).** A failing
  `scripts/teamwork_rehearsal.py` report now carries per-phase timings plus
  the task rows, self-pickup run events, delegations, sessions and
  `memory.db*` files of its isolated store, including when the coordinator
  times out. Phase and coordinator limits are raised for 2-vCPU runners
  (60s per phase, 30s for the paused phase, 360s overall), and the fixture's
  phase marker is written atomically so a kill on its appearance never reads
  a partial file. Run fencing is unchanged.
- **Instruction files slimmed to repo specifics now that agents launch from
  `~/dev` (#1642).** `AGENTS.md` (521 to about 200 lines) keeps commands,
  architecture, conventions and gotchas; `CLAUDE.md` (304 to about 80) imports
  `AGENTS.md` and the fleet rules and keeps the cloud-session section. Fleet
  rules now live only in `~/dev/AGENTS.md` and `~/agent-workspace/rules/SHARED.md`.
  `docs/COLLABORATION.md` points cockpit lanes at
  `~/.herdr/worktrees/agent/<agent>-lane`, names the deploy steward as the
  `unified_agent` merge actor, and replaces the retired Hermes allowlist re-baseline with
  profile-based reviewer access. Memory ranking and vault/web_fetch details
  moved into `docs/memory-tasks-vault.md` and `docs/tools-reference.md`.
  `unified-agent-sync` now honours a `<!-- ua-sync: off -->` marker: an
  instruction file that carries it is never written, and both files here
  carry it.
- **Security audit triage files findings again (#1630, #1631).** Cancelled
  tasks no longer suppress a reappearing finding, and dedup is per audited
  root (keyed by basename plus a path digest) instead of per rule.
  INJECTION-BASE64 no longer flags lowercase word runs (URL paths,
  slash-joined API names, space-stripped prose) or credential token files
  whose content is a token store (JSON or a few long bare tokens). A word run
  is exempt only when every 20-character window also reads as words, so a
  short, control-broken or raw-deflate payload padded with prose still
  reports, as does any decode containing a readable stretch.
- **`idempotency_keys` is declared once (#1678, re-lands part of #858).** The
  table and its `created_at` index were created by three copies of the same
  DDL in `storage.py`. `_IDEMPOTENCY_SCHEMA` is now the only declaration:
  `_SCHEMA` no longer repeats it, and `_migrate` applies it through
  `_apply_idempotency_schema`, which runs one statement at a time so it does
  not commit `init_db`'s open transaction. Columns, the index and
  `SCHEMA_VERSION` are unchanged, so existing databases need no migration.
- **The LangGraph note and workflow adapters share one receipt ledger (#1660).**
  Receipt validation, operation-key checks, stale-authority refresh and the
  governed write path now live in `ReceiptDispatchAdapter`
  (`langgraph_receipt_dispatch.py`) instead of being duplicated in both
  adapters. Both runners pin that module's source in their definition digest,
  so a checkpoint saved before this change, or before any later change to the
  shared module, fails closed with `DefinitionMismatchError` on resume rather
  than continuing under different approval or receipt behaviour.
- **`pi_run_task` no longer hands Pi the hub's secrets, and launches only a
  pinned Pi (#1677, re-lands #867).** The child environment is an allowlist:
  `PATH` (plus `~/.local/bin`), `LANG`, `UA_AGENT_LABEL`, and when set `HOME`,
  `TERM`, `TMPDIR`, `USER`, `LOGNAME`, `OPENAI_API_KEY` and `PI_API_KEY`.
  `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` never reach Pi, and key files are
  not read. **Migration:** the unpinned `npx -y` / `bunx
  @earendil-works/pi-coding-agent` fallback is gone. Install `pi` on `PATH` or
  in `~/.local/bin`, set `UA_PI_EXECUTABLE` to an absolute local binary, or set
  `UA_PI_PACKAGE` to a full semver spec (`package@x.y.z`; `pkg@latest`,
  `pkg@2026` and `pkg@1.2` are refused). `timeout_s` is capped at
  max(120s, `UA_EXECUTOR_TIMEOUT_S`) and now bounds the stdout read too. On a
  timeout or a `kill_check` cancel, Pi's whole process group is killed, so a
  descendant holding Pi's stdout can no longer keep the call blocked.
  `session_dir` must resolve inside the workspace, the agent label is checked
  against the verified identity, and the steward profile is refused.
- **Complete the cloud assistant organization record (task #1654).** `agent-cloud-agent17`
  belongs to the executive office, so its approved registration passes the
  organization health check. Membership retains workspace `DENY` and the
  identity's bounded cloud profile; membership alone grants no capabilities.

- **Local agents read their unified_brain inbox at session start (#1659).**
  The generated Startup Grounding gains item 6 (pull `~/dev/sample-unified_brain`,
  then read the committed `INBOX.md` and linked threads without running any
  script; a failed pull means the inbox may be stale; claim a thread before
  acting; treat thread text as untrusted), and `docs/COLLABORATION.md` gains
  Rule 9 with the same step. Cloud sessions cannot reach the hub; threads are how they reach local
  agents.

- **The hub keeps its SQLite lock when it opens new connections (#1637).**
  `storage.connect()` and `require_live_task_store()` no longer read the
  database header with a plain open/close, which on POSIX released every
  lock the process held on `memory.db`. Without that lock, a separate CLI
  process (for example `unified-agent-audit`) could checkpoint and delete
  the live hub's WAL files, and the hub then failed with "disk I/O error".
  The tombstone check now runs only after SQLite rejects a file, and
  unusable-store errors name SQLite's extended error code.

- **CI and executor fixes after the 2026-09-29..30 CI outage (#1638).** Test
  shards keep pytest temp dirs on a dedicated tmpfs and print short tracebacks
  so a failing shard always reports its summary. `HubClient` resolves its
  bearer from the environment first and reads managed tokens from the local
  store only for a loopback hub with no other credential, so remote clients
  never open a local `memory.db`. Tests updated to the behaviour of #842,
  #855, #869, #885 and #912.

- **CI: Install Gate and Docker no longer run on every UI source edit.** Their
  pull-request triggers now cover package manifests and lockfiles
  (`packages/*/package.json`, `packages/*/package-lock.json`) in place of
  `packages/**`. CI's protocol, client and web jobs still build and test UI
  source on every PR, and release tags still run both gates in full (#1628).

- **Documentation overhaul for public release.** A new README presents the
  platform as a meta harness for agents, harnesses, devices, and people, with
  light and dark architecture and evidence-chain diagrams generated by
  `scripts/render_doc_diagrams.py`. New guides: overview, concepts, ways to use
  it (MCP, Control Center, CLI, TUI, HTTP API, SDK, chat, mobile), deployment
  topologies, connecting agents and harnesses, human in the loop and evidence,
  building on unified-agent, a glossary, and an FAQ. Getting started is now an
  end-to-end tutorial that gates one action behind a human approval and reads
  back its evidence. The docs home is reorganized by reader task, and pages
  that document the reference fleet are labeled as such. Adds CONTRIBUTING.md
  and SECURITY.md. Fixes stale anchors, and corrects `docs/security.md`: an
  HTTP hub enforces authentication by default.

### Added

- **Evidence-backed team memory and session experience (#1459).**
  `memory_save` accepts `epistemic` (`fact` | `inferred`; an inference must
  carry `evidence` refs such as `session:<id>` or `task:<id>`), `valid_from`
  (defaults to creation for a labelled note), `supersedes` (create-only;
  demotes a changed fact from search, packets and the tag/master indexes while
  `memory_get`/`memory_restore` still reach it) and `contradicts`. Peer,
  broadcast and other-tenant notes keep their guards, including update by id.
  `session_packet` labels injected memory and adds a bounded `experience`
  section of prior session outcomes on the task, all inside
  `UA_MEMORY_INJECT_TOKEN_CAP`. Learning proposals carry evidence ids,
  `observed_at` and same-tenant `contradicts` links; exports and migration
  manifests round-trip the new fields. No new MCP tool and no second store.

- **Team specs: declare a team and boot it with a plan preview (#1736).** A
  `teams:` block in the reviewed policy file declares, per org office, the
  seats for a run: an org.yaml roster label, an ACP harness, a model, a host,
  a workspace (repo plus `task`/`shared` worktree policy), startup guidance,
  skills and recorded edges (`delegates_to`, `reviews`, `can_observe`,
  `collaborates_with`, `escalates_to`), plus an `operating_profile` name and
  an optional `mission_id`. Specs are versioned as `policy` config revisions
  and validated on commit. `coordination_manage` gains `team_up` (a stable,
  diffable dry-run plan with per-seat readiness checks and refusals for
  unknown labels, unavailable harnesses, offline hosts, untrusted
  workspaces, WIP caps, budgets, trust presets, lifecycle and autonomy mode;
  `plan=False` boots a committed spec behind one approval, in edge order,
  through the ua-node queue, with one run record per seat on
  `agent.team.run`), `team_list`, `team_seat` and `team_spec_commit`.
  `profile_status` shows the caller's seats. HTTP: `GET /api/teams`,
  `GET /api/teams/plan` and CSRF-gated `POST /api/action/team-up`; the
  Skills & Teams pane shows each plan with a Boot button. Nodes now report
  their git checkouts with resolved paths and the harness-config skills they
  have applied, per skill-bearing surface (so a seat's own harness must
  receive its skills), so a plan trusts only node-measured workspaces and skills.
  Parsing and validation live in the core-layer `team_policy.py`, which
  validates the teams block on every policy commit, rollback and capture.
  See `docs/team-spec.md`.

- **`unified-agent-distro` drops a portable agent setup into a project
  (#1733).** `init <path>` plans by default and writes only with `--write`:
  an `AGENTS.md` constitution with harness-neutral hard rules, `CLAUDE.md` and
  `GEMINI.md` pointers, per-harness hub config over HTTP that names token
  environment variables and never holds a value (`.codex/config.toml`,
  `.cursor/mcp.json`, `opencode.json`, `.vscode/mcp.json`), a user-scope
  Claude Code registration helper bound to the generated hub URL, token-file
  wrappers in `.unified-agent/bin/` (`ua-copilot` `exec`s the Copilot CLI
  with its hub config passed through bash process substitution, so the token
  never touches disk, argv or Copilot's environment, even when the parent
  shell exported `UA_TOKEN` or `config_json`; a missing token file is an error
  unless `UA_COPILOT_TOKEN_SOURCE=env` is set; `init --write` removes the
  `.unified-agent/copilot-mcp.json` an earlier version generated, only when
  the Copilot harness is selected and the file still matches at write time),
  `.unified-agent/operating.yaml` naming the operating profile (re-runs keep
  the recorded one), and a starter skill. It is idempotent and all-or-nothing:
  a file with different content is a conflict that only `--force` replaces,
  and in an existing MCP config file `--force` replaces only the
  `unified-agent` entry and keeps every other server. A parent path that is
  not a directory, or a config that cannot be merged safely, blocks the write;
  symlinks are refused; files are staged through `O_EXCL` temp files, and a
  failed move rolls back the files already moved, keeping (and naming) any
  original it could not restore. `--write` needs `--hub-url` or
  `UA_HTTP_HUB_URL` (no local default; URLs with credentials, a query or a
  fragment are refused, as are wrapper overrides with a malformed host,
  unbalanced IPv6 brackets or a non-numeric or out-of-range port) and `--allow-dev-root` under `~/dev`, where
  project-scoped hub entries would shadow the user-scope registration.
  `check <path>` reports missing, drifted and non-executable files and literal
  secrets (masked).
- **`npm run design-system` rebuilds the Unified Agent design system from
  `packages/web` (#1755).** It writes `design-system/out/project/` for the
  claude.ai Design System artifact: `--ua-*` colours per theme from
  `src/index.css`, persona hues from `AgentPersonaBadge.tsx`, the `src/ui`
  components bundled as `window.UnifiedAgent`, Tailwind utilities and types.
  Usage notes and type scales live in `design-system/tokens.meta.json`. The
  build fails on drift in either direction: a theme, colour or distinct
  persona hue in the code without metadata, metadata without code, or a
  light-mode fallback that is not identical to a theme.

- **A layer map for `src/unified_agent`, enforced in CI.** The package's
  283 modules are placed in five layers (adapters, runtime, services,
  integrations, core) in `docs/architecture/layers.toml`, and
  `scripts/check_layers.py` fails when an import points up the map, when a
  module is in no layer, or when a baselined inversion has been fixed but its
  line is still there. The 54 upward imports found when the map was drawn
  are down to 20 (of 1,354), listed in
  `docs/architecture/layers-baseline.txt`, which only shrinks. A deliberate
  upward import can be declared as a `[[seam]]` with its reason. The check
  runs in the CI `gates` job, `scripts/local-gate.sh` and
  `tests/test_layer_check.py`. See `docs/architecture/layers.md`.

- **Lower layers no longer import the MCP server or each other upward.**
  `tool_surface` holds the governed tool registry and policy guard that
  `server.py` fills, so workflows, the LangGraph dispatcher, the chat worker
  and the skill catalog call governed tools without importing the server.
  `change_feed` carries task and run change notices to `mcp_live_state`.
  `table_schemas` holds the DDL `storage.init_db()` creates (the resulting
  schema is unchanged). Shared pieces moved down into `config`,
  `budget_policy`, `dispatch_states`, `node_registry`, `pr_models` and
  `slack_config`; old import paths still work. No command, `python -m` path
  or service unit changed.

- **Launch guide** (`docs/launch-guide.md`): where to launch the hub, the
  Control Center, workers, timers and the SDK, and which layer each starts in.
- **Heartbeat usage on self-pickup runs (task #1681, re-land of #866).** A live
  `task_claim` renewal may pass `run_usage`: the run's cumulative usage so far.
  `get_run`, `run_history` and `runs_in_flight` show the latest heartbeat on the
  open run, run-resource subscribers are notified (also when another process
  wrote it), and the run's finish or abandon event carries it, so a run whose
  agent dies keeps its reported cost. Heartbeats append no event-log row; the
  snapshot lives in a one-row-per-run `run_usage_heartbeats` table that is
  cleared when the run finishes, its task is deleted, or its claim is pruned.
  The release-wedge and a11y demos now record their claims on the run ledger.

- **Bounded cloud assistant (task #1654).** Introduced the non-escalating
  `cloud-assistant` profile: task/memory/context reads and agent messaging only.
  Unknown tools/actions and Control Center routes are denied; no execution,
  approval, delegation, workspace or vault writes are granted by that profile.
  It remains an available downgrade from the bounded Wiki profile above.

- **Desktop compatibility discovery (task #1568).** `/api/whoami` now returns
  the installed hub package version and an integer API contract version alongside
  the existing identity and CSRF fields.

- **Hermes profile control (task #1392).** Control Center Sessions can launch
  explicitly selected, discovered and mapped Hermes profiles through ua-node
  ACP, stream their updates, relay native permissions to Approvals, cancel
  and read per-profile status. Device and profile identities remain distinct;
  unavailable controls and unsupported resume or steer remain visible.

- **Self-organization as a toggleable capability (task #1431).** Agents propose
  organizational changes (roles, lanes, routing, delegation, routines) within
  their legitimate office boundaries (`ScopeEscapeError` on escape). Applying
  proposals is never automatic and stays strictly behind human/policy approval
  gates with separation-of-duties enforcement. Proposals can be toggled via
  `control_flags` (`key='self_organization'`, default enabled). Includes canonical
  `org_proposal` object model, served platform reader, unified MCP tool, and
  WebUI API endpoints.

- **Safe approval event fields (task #1615).** New approval requests include a
  bounded tool identifier and verified requester identity without action payloads.
  Request activity attribution also uses verified identity, and dotted or colon
  action names remain supported.
  Existing event fields, legacy mirrors and cloud projection allowlists remain
  compatible; notification consumers must refetch approval details before decisions.

- **Agent personas: visual identity, avatars, colors, bios, and model-routing preference (task #1427).**
  Adds canonical agent personas to the platform object model and Control Center presentation.
  Renders agent avatar, display name, and color border accents in Org view, Task Detail claim badges,
  and Task Comment author tags. Personas are purely presentational (Design Rule 3 compliant: no stored
  lifecycle/status column; liveness derived dynamically at read time) with zero privilege escalation or routing alteration.

- **Queue backpressure, intake and complete reads (task #1430).** A read-only
  `queue_hygiene.queue_pressure()` reports depth, claim age, WIP-cap pressure,
  contention and routine intake over existing rows (no new table). Faults (a
  `doing` task with no lease, an expired lease, a hard lane cap holding waiting
  work) degrade the new doctor row `queue_backpressure` and show as an alert on
  the Task Queue pane; aging backlog and contention are notes. Control Center
  task panes and the new `task_list_all()` follow `next_cursor` to the end.
  Routines are the intake policy; `docs/work-queue.md` maps every inbound
  producer to it and lists what #1430 deliberately does not add.

### Fixed

- **Locked notes are honoured on every vault write path (#1817) (Security).**
  `obsidian_append` never checked `locked: true`, so a non-cloud caller could append to
  a locked note that `vault_write(mode="append")` refuses. It now applies the same check
  before the PATCH (pointing to `vault_write(force=True)`), resolves the path once, and
  refuses any frontmatter patch targeting the `locked` key; a note that does not exist
  yet behaves as before. The plugin writes the file itself, so a lock added between the
  check and the PATCH cannot be prevented; a lock still present after the PATCH is reported
  (`locked_after_write` and a `warning`; the append is not undone), but one inserted after the
  plugin's own read can be overwritten and go undetected (#1890). The lock detector itself was also too narrow for local
  callers: CRLF frontmatter, a UTF-8 BOM, `locked: yes`/`on`/`True`, an inline comment and
  quoted keys all read as unlocked. Local and cloud callers now share one detector
  (`safety.vault_lock_state`: legacy line check plus YAML parse, truthy YAML 1.1 values
  lock, any value other than false/no/off/0 locks). Unparseable frontmatter that names a
  `locked` key fails closed for local callers; the cloud scope still refuses any
  frontmatter it cannot verify. The detector reads past a `...` end marker to the `---`
  block, widens its read (8 KiB, 64 KiB, 1 MiB) before judging cut-off frontmatter and fails
  closed beyond that, skips YAML parsing of absurdly nested frontmatter, and keys its cache on
  mtime, size, ctime and inode. Lock refusals raise `VaultLockedError`. `vault_journal` now
  refuses a locked `Logs/<day>.md`, and the Wiki custodian skips (with a notice) rather than
  overwrites a locked `Wiki/Health.md`.

- **Event stream resumes from `Last-Event-ID` and refuses a full house with 429 (#1811).**
  A browser `EventSource` reconnect sends only `Last-Event-ID`, which
  `GET /api/events/stream` ignored, so the stream restarted at the log tail and every event in
  the gap was lost. The header now sets the cursor and takes precedence over a `since` or
  `after_seq` query parameter (a re-opened stream keeps its cursor in its URL for life, so the
  header is always the newer position): an event ULID (the `id` of each `message` frame) or
  `seq:<n>` (the `id` the opening `cursor` frame now carries, so a reconnect before any event
  also resumes). A malformed header is a `400`, or ignored when the query supplies a cursor.
  A stream refused by `UA_UI_MAX_STREAMS` is now HTTP `429` with `Retry-After: 30` and a JSON error, not a `200` whose first frame was an
  error, and the slot is reserved before the response exists and released on disconnect, on a
  dropped response and on a response that is never consumed. A browser `EventSource` treats
  any non-200 as final and never retries, so the Control Center now re-opens a refused stream
  itself (`openEventStream`: 30 s, doubling to 5 min, reset on open, resuming from the last id).

- **Authenticated profile ceiling without an MCP session (#1880).** A verified
  identity keeps its default profile ceiling for tool discovery and calls even
  when session context is absent. Checked Control Center routes carry only their
  declared, identity-bound tools/actions through guarded calls, preserving operator
  browser writes without an MCP exemption or skipping policy/access/approval gates.
  All authenticated read panes that invoke guarded tools declare exact read helpers;
  metadata projections and per-object gates remain intact, and mixed ingest reads
  cannot grant an ingest write. HTTP workflows
  carry only the validated repo-defined step's capability through its guarded call;
  nested policy/access/approval checks remain active and MCP workflows stay restricted.
  Session IDs still scope profile overlays; a
  recreated session or reset overlay cache returns to the identity default.
  Missing-session fallback logs a diagnostic without identity or credential
  values. Legacy unauthenticated in-process/stdio exposure is unchanged. Isolated
  regressions cover the profile checks and MCP list/call handlers; the specific
  reported live restart/reconnect mechanism remains unverified.

- **Messaging follow-ups (#1869).** A session that only retries a committed
  `message_send` now counts as that session's heartbeat: a supplied `from_session`
  is checked and touched before the saved result is returned (still ahead of the
  recipient-session check), and a `from_session` that is not the sender's fails on a
  retry as it does on a first send. `list_sessions(live_only=True)` now uses one
  cutoff for its SQL filter and the `live` flag, rounded up to the whole second
  heartbeats are stored in, so a row just past the window no longer passes SQL,
  reads `live: false` and takes a `limit` slot from a live session. A `message_id` that another
  sender already used is now refused instead of returning that sender's receipt.

- **A hub restart no longer pins the CPU for minutes (#1850).** After a restart
  the Control Center drove about a thousand gateway calls in ten minutes and ran
  `system_doctor` back to back, so `/healthz` took seconds and `/mcp` clients
  timed out. `system_doctor` now shares one report for `UA_DOCTOR_CACHE_S`
  seconds (default 20; `0` disables) and says so: a reused report carries
  `cached: true`, `cached_at` and `cache_age_s`, and `system_doctor(refresh=True)`
  runs every probe now. Concurrent callers wait for one run. LLM leg probes are
  reused for `UA_LLM_PROBE_CACHE_S` seconds (default 60), failures included, so a
  down ollama is no longer probed about once a second; a reused probe's detail
  says how old it is. The Control Center's doctor reading is stamped when the
  run finished, not when it started (a slow run came back already expired), and
  its `?force=1` reaches the tool. The doctor's connector check initializes the
  store once per process (`init_db_once`), so it no longer restarts every
  finished embedding backfill scan. The `memory_vec` mirror backfill finds
  missing rows with one set-based query instead of a vec0 lookup per row, and
  writes them in batches; the memory and symbol backfills stop at the first
  model-load failure instead of retrying the load for every row, and every
  backfill pauses `UA_BACKFILL_BATCH_PAUSE_S` (default 0.05 s) between batches.
  In the Control Center every pane now follows the approvals pane's rules
  (#1786): hub events trigger at most one refetch per pane every 5 s and none
  while the tab is hidden, polls back off exponentially while reads fail, a
  failed read is retried once instead of three times, the Doctor pane polls once
  a minute instead of on every hub event (its Refresh forces a fresh run), and
  the header pill, Home and the Attention pane share one 60 s attention poll.
  Shared helpers live in `packages/web/src/polling.ts`.

- **The hub no longer runs out of file descriptors under load.** `init_db()`
  is also called at request time (`system_doctor` calls it once per connector,
  and the Control Center polls the doctor), and every call started a new
  thread per background backfill hook. Each thread held its own `memory.db`
  connection while it waited on the serialized embedding model, so threads
  and handles stacked up until the hub hit launchd's 256-descriptor limit and
  stopped accepting connections. A background hook still running for the same
  database is now reused instead of started again, concurrent callers on a cold
  cache build the embedding model once, and the hub LaunchAgent raises its soft
  `NumberOfFiles` limit to 8192. `system_doctor` initializes the store once per
  run instead of once per connector.
- **Hub follow-ups: backfill locking, doctor single-flight, init churn, store reach (#1853).**
  The memory and symbol embedding backfills now embed each batch of 50 with
  no transaction open and commit it, instead of holding one write lock across
  the whole pass. The Control Center's cached `system_doctor` reading is
  single-flight, so concurrent requests after expiry run the doctor once.
  Horizon 2 and the credential pool initialize the store at most once per
  process per database (`storage.init_db_once`) rather than on every request.
  `SECRETS-STORE-PERMS` no longer reports a readable store that sits under an
  owner-only directory, since group and other users cannot reach it.

- **The review pipeline validates a task only in its own repo (#1844).**
  `merge.find_task_worktree` now routes by the task row's project: a
  product-sourced task (a named project, or a `[code-task]` default-project
  row) is looked up only in that project's checkout, never in a same-named
  `.worktrees/task-<id>` of the workspace repo, and a miss falls through to
  the origin-branch and merged-PR fallbacks. An unmarked default-project task
  searches the product checkout first, then the workspace. The gate's pytest
  run also disables the host `settings.toml`, so no hub `UA_*` policy reaches it.
  The reconcile pass uses the same routing, and worktree matching now needs a
  task-id token: `pr-<N>`, `gate-<N>-<sha>` or `reland-<N>` never matches task N.
- **Surface audit false positives (#1846).** A vendor CLI credential store
  under a dot-directory (`~/.codex/auth.json`, `*-oauth-token`), outside git or
  untracked, no longer reports HIGH `SECRETS-KEY` "committed in source"; it
  reports nothing at mode 0600 and medium `SECRETS-STORE-PERMS` when group or
  other can read it. `SECRETS-KEY` ignores doc placeholders (`user:pass@`,
  `<token>`, `${VAR}`, `app:app@localhost`). `INJECTION-BASE64` skips commit
  SHAs, URL parts that decode to nothing, and gitignored files in the root's
  repo. A closed `[audit-triage:]` task whose notes carry `[accepted-risk]`
  stops that group being refiled.

- **Concurrent embedding inference no longer crashes the process (#1833).** The
  background backfills and foreground callers share one fastembed model whose
  tokenizer is not thread-safe; overlapping `embed()` calls segfaulted a CI test
  shard. `embeddings.generate_embedding` now serializes inference with a lock.

- **Control Center resource lookup picks the right root (#1829, #1689).** The
  UI build and the capability catalog are resolved independently, each from a
  genuine source checkout when it has it, else from the packaged bundle, so an
  editable install of an extracted sdist serves a rebuilt UI and still finds
  its embedded catalog; release builds map them the same way. A genuine
  checkout must declare `unified-agent` in `pyproject.toml`, have no
  installed-wheel `dist-info` beside the package and not sit under
  `site-packages`, so a wheel installed with `--target` into another
  project's `src` serves its own packaged UI, never that project's.

- **Migration import and rollback are tenant-fenced (#1795).**
  `rollback_migration_import` deleted tasks and memory notes by bare id, so a
  crafted token could delete another tenant's rows; it now deletes only the
  caller's tenant's rows, and another tenant's id answers exactly like a
  missing one. `import_migration_manifest` scopes every lookup (including the
  parent check) to the caller's tenant, so another tenant's row is never
  reported as `unchanged` or `conflicting_edit` and never adopted as a parent,
  and stamps every inserted task and note with the caller's `org_id`. An id
  that collides with another tenant's row is held as `insert_rejected`, the
  same reason any integrity failure gets, and that row is left untouched. The
  dry run now attempts the inserts and rolls them back, so its preview matches
  what `apply=True` does. Both calls take the write lock up front
  (`BEGIN IMMEDIATE`), so a concurrent writer waits instead of aborting the
  import, and `rolled_back` now counts the caller's rows actually deleted.

- **Governed admission for raw event-log reads (#1684).** `GET
  /api/ledger/rows` and `GET /api/events/stream` read the event log directly
  instead of through the registered `event_log_query` tool, so they checked
  auth and the profile ceiling but skipped the gates that tool call passes. A
  shared route-level admission check now runs before any row is read or any
  stream slot is taken: the profile ceiling, the agent-access switch, then
  policy rules, trust presets and the per-caller rate limit, charged to
  `event_log_query` so the HTTP and MCP reads share one budget, then the
  `UA_APPROVAL_TOOLS` gate. A policy deny answers 403, an exhausted rate or
  full rate-limit capacity 429, and a `require_approval` rule or gated tool
  202 with the approval id. The gate payload uses `event_log_query`'s argument
  names, so a pass carries over only where the arguments are equal. Refused,
  parked and failed reads record one `agent.gateway.decision` row. A stream
  records its decision once it holds a slot and its first replay query ran,
  or the capacity, startup or replay error that stopped it; decisions are
  written off the event loop, and a capacity refusal repeated by one caller
  within a minute is recorded once while its error frame asks the browser to
  retry after 30 s. An allowed ledger
  read writes no row, so paging the ledger to its tail still ends. The
  verified operator keeps the hub middleware's human-recovery exemption from
  the access switch (and only from it), and an admitted read keeps its
  `after_seq` / `since` cursor and replay behavior.
- **Small-fix batch (#1708, #1709, #1711, #1712, #1713, #1625, #1746, #1645,
  #1728).** `UA_TS_FETCH_LIMIT` above 50 is clamped to 50 with a warning
  instead of crash-looping the task-space worker. `UA_POST_MERGE_VERIFY` now
  honors `off` like `UA_REVIEW_TRUST_CI` (shared off-words helper
  `config.env_flag_off`). `dispatch-tick-cron.sh` and other deploy scripts
  default to `$HOME`-relative paths instead of a fixed user's home. The failover rehearsal
  script resolves its swap file under the worker's `$HOME`, not the desktop's. A bad
  `UA_FRONTIER_*` numeric value falls back to its default with a warning rather
  than breaking `SlackConfig.from_env`. The `delegation_ledger` docstring lists
  its real return keys. Docs: trailing whitespace removed; the T3 Code Control
  Center embedding is described as an unmerged experiment that upstream does not
  support; `docs/tool-catalog.md` now lists all 197 registered tools, with a
  test that keeps it complete. The Control Center drops its `nanoid` 4.0.2
  override (now 3.3.18, the version Excalidraw already used) and refreshes
  `dompurify` to 3.4.16, clearing all four `npm audit` findings.

- **`/api/approvals` no longer opens one database connection per row (#1786).**
  The Control Center queue enriched each row through its own platform read,
  so a 56-row queue cost 59 connections per poll, and a tab that refetched on
  every hub event held the hub near 300% CPU. Rows are now read in one batch
  (`platform_http.read_objects`, `platform_readers.read_approvals`,
  `tools.approvals.get_approvals`) behind the same access and profile gates,
  with the same per-row fallbacks: on a synthetic 60-row queue a warm build
  went from about 0.30 s and 63 connections to about 0.06 s and 4. The route
  sends an ETag and answers a matching `If-None-Match` with 304. Expired
  passes are capped to the newest 50, with `expired_passes_total` and
  `expired_passes_truncated` added; a failed queue read now reports `error`
  instead of an empty queue. In the web app, the approvals stream listens to
  `agent.approval.*` only, coalesces refetches to one per 2 s, holds them
  while the tab is hidden, and the poll backs off on errors up to 5 minutes.
  The drawer's detail query key is no longer nested under the list key, so a
  list refresh no longer refetches an open drawer; instead an open drawer
  refetches on approval events and polls on the list's interval, so a pass
  that crosses its TTL or is decided elsewhere does not stay actionable.
  `approval_pending(newest_expired=True)` returns the newest expired window
  and `expired_passes_total`, so the 50-row cap and the total stay correct
  past the tool's 100-row bound; the All chip sums the bucket counts; and an
  older conditional read finishing last no longer replaces a newer ETag.

- **Memory reads and maintenance are tenant-fenced (#1762).** `memory_get`,
  `memory_search` (FTS, semantic and identifier paths, plus the save-time
  dedup `similar` list), `memory_list`, `memory_restore`, `memory_delete`,
  `memory_feedback`, `memory_promote`, `memory_prune_candidates`,
  `recall_maintenance`, the `memory` platform resource reader, a task
  comment's `decision_id` ref and the consolidate, evolve and sleep-pass
  candidate reads (including sleep-pass preflight admission) now filter on `org_id IS tenant_org_id()`, the
  fence #951 put on updates and links. Another tenant's note answers exactly
  like a missing id. Index notes are built per tenant: they carry the
  tenant's `org_id`, and an enterprise tenant's ids end in a `-<digest>`
  suffix (`masterindex00-…`, `tagindex_…-…`). The untenanted hub keeps its
  ids and behavior.

- **Task nesting is tenant-fenced (#1810).** `task_add(parent_id=...)` and
  `task_update(parent_id=...)` looked the parent up by id alone, so one tenant
  could attach a task under another tenant's row (and the parent's
  `ON DELETE CASCADE` let that tenant's delete remove it), while a missing id
  answered differently (an existence oracle). The parent and the task being
  moved, plus the ancestor, depth, cycle, subtree, rollup and
  `task_list(parent_id=...)` walks and the `task_delete` subtree, now filter
  on `org_id IS tenant_org_id()`. Another tenant's id answers exactly like a
  missing one, and a stored pointer into another tenant ends the lineage
  there. `task_delete` first detaches (to a root) any other-tenant child that
  older data left under the deleted subtree, so the cascade cannot cross
  tenants. The untenanted hub keeps its behavior.

- **Antigravity hooks run on every machine (#1646).** `.agents/hooks.json` no
  longer hardcodes one machine's home directory. Antigravity runs each hook
  through `sh -c` from the directory holding `hooks.json`, so the commands now
  resolve `../.venv/bin/python3` and `../scripts/hook_*.py` relative to the
  checkout (falling back to `python3` where a worktree has no `.venv`). The pre-push
  and post-write hooks then find `ruff` and `unified-agent-audit` in the
  worktree's `.venv`, the main checkout's `.venv` or `PATH`; the pre-push hook
  blocks (instead of allowing) when the checks cannot run.

- **`system_doctor` no longer reports the Approval Ledger LIVE over a failing
  poller (#1783).** pr-watch now writes `$UA_HOME/pr-watch-status.json` after
  each scheduled run (exit, first error line, last success, consecutive
  failures), and a connector failure names its cause. A new `pr_watch_job` row
  is DEGRADED when the last run failed or the last success is older than the
  30-minute interval plus 15 minutes of grace. `github_approval_backlog` now
  reads LIVE only when that record is healthy, and UNKNOWN before the first
  recorded run. A new `required_settings` row reports MISCONFIGURED when an
  enabled feature lacks a setting that used to have a code default: the
  Approval Ledger connector without `UA_GITHUB_CONNECTOR_REPO`
  (`github.connector_repo`), or an installed pr-watch job without a vault
  directory in `UA_VAULT_PATH` (`paths.vault`). The registry is in
  `required_settings.py`; add to it when a default is removed. Both settings
  are judged against the pr-watch job's own environment (its LaunchAgent,
  `service.env` and `settings.toml`) when that LaunchAgent is readable, not
  the hub's. `run-pr-watch.sh` records a failure before Python starts in
  `$UA_HOME/pr-watch-launch-failure.json`, which reads DEGRADED, and an
  installed pr-watch job with no recorded run reads UNKNOWN, not DORMANT.
  The launcher stamps that failure in microseconds when the system `perl` is
  available (whole seconds otherwise), and a failure in the same whole second
  as the last run counts as newer, so a failed launch right after a success no
  longer reads LIVE.
- **`system_doctor` flags a hub checkout that moved without a restart
  (#1771).** The HTTP hub records the checkout HEAD it started on in
  `$UA_HOME/hub-start.json`, and the new `hub_checkout_drift` row is DEGRADED
  when the checkout's HEAD has since changed. `bootstrap-server-dev.sh` no
  longer runs `git pull` in, or replaces, the checkout the hub LaunchAgent (or
  that start record) runs from, including a linked worktree.

- `unified-agent init --force` no longer resets `UA_TRANSPORT`,
  `UA_ENFORCE_AUTH`, `UA_APPROVAL_TOOLS` or `UA_MCP_OAUTH` to the cold-start
  template defaults: it rotates bearers and re-asserts identity and workspace
  but keeps those deployment settings an existing `service.env` declares
  (#1774). Every setting it does not change keeps its assignment text as
  written, so a shell expression such as `UA_TRANSPORT=$MODE` is no longer
  rewritten into a quoted literal. The init summary now says "(in token file)" for bearers stored in a
  token file instead of "(in env file)".
- `unified-agent init` no longer mints a bearer for a fleet-looking label such
  as `desktop_agent` on a host named `desktop`, `laptop` or `server`: the hub
  refuses `Desktop_`/`Laptop_`/`server_` names outside the catalog, so every
  call answered 401. It falls back to `local_<host>_agent` (#1661).
- An `UA_OPERATOR_ALIASES` or `UA_OPERATOR_NAME` entry naming an agent that
  authenticates through a live OAuth grant, a pinned OAuth client or
  client-credentials entry, or a `UA_HUB_WORKOS_IDENTITY_MAP` label is now
  dropped like a bearer-backed worker name, so it cannot make that agent human
  (#1760). A malformed pin its own parser rejects (empty client id or secret)
  names no worker and withholds no name.
- Documented that desktop and laptop clients without `settings.toml` show the
  generic `Operator` name and how to set `operator.name` there (#1760).

- Vault listing/search no longer fail at the hosted-environment guard for an
  identity with an explicit workspace DENY (#1636). The guard checks the host's
  configured workspace; vault profile/path/lock checks and filesystem/shell
  denial remain enforced. Empty or missing hosted vaults still fail closed.
- `system_doctor`'s `symbol_embeddings` row no longer reports MISCONFIGURED
  when the caller has an explicit `UA_AGENT_WORKSPACES` DENY binding. Like the
  worktree survey, it now reads the hub's process workspace for that caller,
  so the row no longer depends on who asks (#1779).
- Office communications references degrade safely when the org registry is
  unreadable, including default and frozen run policies: funnel peer messages
  continue, operator/custom office access stays closed, and evidence records
  the unresolved office warning (#1764).

- **Doctor treats a retired Zulip without a site as unused (#1772).** After the
  built-in Zulip site was removed, leftover bot credentials made `zulip_relay`
  MISCONFIGURED on hosts where Zulip is retired. It is now DORMANT there; an
  un-retired Zulip with no `UA_ZULIP_SITE` is still MISCONFIGURED.

- **Task exports stay in their tenant (#1759, #1459).** CSV, JSON, Markdown,
  and vault exports now fence task rows by organization, including the
  private hub’s NULL scope. Migration manifests also attribute private
  agent events to their owning scope, withholding foreign/unowned events,
  and withhold out-of-range numeric references instead of aborting valid exports.

- **`init` keeps a symlinked `service.env` and an alias-only operator name (#951).**
  Successful `service.env` writes now update the symlink's target instead of
  replacing the link, so other readers of that target see new bearers and
  identity pins. An install whose operator is named only by `operator.aliases`
  keeps its first accepted alias as the saved name rather than the generic
  `Operator`.

- **Doctor judges the nightly LLM chain only where the sleep pass runs (#1731).**
  A host without the sleep-pass timer or LaunchAgent (the desktop) reported
  `llm_provider_chain` DEGRADED because its unused nightly chain had no leg;
  it now reports the hub chain alone. A probe that cannot tell whether the
  pass is installed still judges the nightly chain, so a broken nightly leg
  cannot hide behind a working hub chain. Fixes two `test_bootstrap`
  cold-start tests that failed on such hosts since #932.
- Initialization refuses to reuse an existing worker bearer as a human operator,
  operator labels and aliases reject configured custom workers, and adding an
  operator bearer restricts its token store to owner-only permissions.
- **Retired Zulip no longer bills chat replies (#1714).** `zulip_chat.maybe_reply`
  checks `UA_ZULIP_RETIRED` before claiming a message or calling the model.
  With `UA_ZULIP_CHAT=1` on a retired hub it used to generate a reply whose
  send was then refused, and the failed reply escaped the hourly cap.
- **A malformed `after_seq` gets 400, not 500 (#1683).** `/api/events`,
  `/api/events/stream` and `/api/ledger/rows` accept only an ASCII integer
  from 0 to 2^63-1. Non-ASCII digits such as `²` used to reach `int()`, and
  an overflowing value reached SQLite, so both returned 500, and the stream
  silently ignored a bad cursor.

- **Nightly sleep pass: diagnosable LLM failures and a working local leg
  (#1639).** A failed `llm_client` leg now records its exception class, HTTP
  status and one fixed category (`model not found`, `insufficient credits`,
  `rate limited`, `schema mismatch`, ...) in `llm_route`, and the sleep pass's
  telemetry keeps them. Provider error text is never recorded, because it can
  echo the private notes in the prompt. The Ollama, OpenRouter and
  subscription-pool legs now give the model the JSON schema, and Ollama uses
  constrained `json_schema` output; before this, every adjudication reply used
  the wrong key case and failed the schema. The scheduled pass takes unset
  `UA_OLLAMA_BASE_URL`, `UA_OLLAMA_MODEL` and `UA_OLLAMA_TIMEOUT_S` from
  `service.env`, so it uses the hub's local model. In `system_doctor`, the
  `llm_provider_chain` ollama row is up only when the configured model is
  pulled. The check adds an `ollama[sleep-pass]` row when `sleep-pass.env`
  points somewhere else, and a healthy `ai_studio` row says the prepaid
  credit is not probed.
- **The fetch SSRF guard blocks IPv4 addresses embedded in IPv6 (#1666, re-lands
  #864).** `check_fetch_host` now unwraps the IPv4 inside IPv4-mapped
  (`::ffff:0:0/96`), IPv4-translated (`::ffff:0:0:0/96`), NAT64 (`64:ff9b::/96`)
  and IPv4-compatible (`::/96`) addresses and refuses them when the inner
  address is not public. Hosts such as `64:ff9b::127.0.0.1` or
  `::ffff:0:169.254.169.254` passed before, because Python reports those IPv6
  forms as global.

- **Control Center lifecycle actions work in embedded browsers (#1707).**
  Pause and Terminate now show the target agent, consequences and a reason
  field in the page instead of relying on a native browser prompt. Cancel
  and Escape send no request; successful actions keep keyboard focus in the
  agent's controls when its lifecycle state refreshes.

- **Quota telemetry fixture isolation (task #1626).** Tests default to an empty
  per-test Codex session tree, preventing routing and quota projections from
  scanning operator logs. Explicit synthetic meter fixtures remain supported.

- **Deployment claim documentation (task #1623).** The tool reference and
  catalog include first-credential provisioning, approval and activation steps;
  documentation counts now match all 195 registered MCP tools.

- **Direct SSE profile authorization (task #1624).** The Web UI activity stream
  checks the verified caller's event-read profile before reading replay rows or
  opening a live stream. Restricted callers receive HTTP 403.

- **Event ledger integrity and a gap-free cursor (task #1640).** A failed Slack
  send no longer deletes a hash-chained `event_log` row: the send slot lives in
  a separate `slack_outbound_slots` table and only a delivered message is
  appended to the ledger, so `verify_event_chain` stays valid. Every append
  mints its ULID under the write lock, above the log's committed maximum, so
  ULID order now matches commit order across processes. Events carry `seq`
  (their ROWID); `event_log_query`, `/api/events` and the SSE stream accept an
  `after_seq` cursor, and `since=` keeps working. New read-only
  `GET /api/ledger/rows?after_seq=&limit=` returns raw rows with the stored
  `payload_raw` so external verifiers can re-hash them. `prune_events` keeps
  the newest row so `seq` never rewinds. A failed approval producer stamp now
  logs a warning and appends an `audit.evidence.gap` event instead of passing
  silently.

- **Scoped automatic session memory (task #1621).** Session packets exclude
  foreign project and organization notes before candidate truncation. Known
  project aliases and same-organization global notes remain supported. Expired,
  untrusted and below-threshold notes no longer crowd out eligible context.

- **Bug worker lookups past the first 500 tasks (task #1494).** `claim_one`,
  `submit_for_review`, the dry run and `bug_worker_check_eligibility` scanned
  only the first 500 rows of `status="all"`, so later tasks read as missing.
- **Web tests on Node 25+.** Node's built-in `localStorage` (undefined without
  `--localstorage-file`) shadowed jsdom's and failed the auth tests locally;
  the vitest setup restores jsdom's storage.

### Fixed

- **Audit: source paths are not base64, and task worktrees are not rescanned
  (task #1386).** A citation like `packages/server/lib/controllers/connection/
  connectionId/getConnection.ts` sits wholly in the base64 alphabet and was the
  repo's standing `INJECTION-BASE64` finding. A run of three or more word-like
  `/`-separated segments is now exempt, but only when it does not base64-decode
  to text (20k random blobs never qualify). `.worktrees/` is skipped: scanning
  the main checkout reported each finding once per worktree and hit the
  5000-file cap (4988 files, truncated; now 1498 files, not truncated).

### Changed

- **CI stops re-testing what cannot change (task #1498).** Depot bills per
  second and the four pytest shards dominate. Measured over the last 60 merged
  PRs: a push to `main` whose tree is byte-identical to its already-green PR
  head (22 of the last 40 merges) now skips the test jobs; markdown-only
  changes run lint, audit and the ~90 test files that read documentation in
  one shard instead of the full four-shard suite; Python changes that also
  touch markdown no longer run the protocol/client/web stack. Fixed an accuracy
  gap: `uv.lock`/`pyproject.toml` changes now run the protocol codegen check,
  which uses Python tooling pinned by `uv.lock`. Rules moved from inline bash
  into `scripts/ci_changes.py` with tests.

### Added

- **Honest pagination for `task_list` (task #1430, defect 1).** A sweep that hit
  `limit` used to return a plausible subset with nothing marking it partial.
  Every result now carries `truncated`, `next_cursor` and the effective `limit`,
  and a new `cursor` argument resumes a keyset walk (priority, `created_at`,
  `id`). Rows inserted mid-walk neither duplicate nor shift. The
  `unified-agent://board` resource reports `truncated`/`next_cursor` too.
- **Platform invariants ADR (task #1439).** `docs/architecture/platform-invariants-adr.md`
  classifies the 18 invariants from the 2026-09-25 architecture review into 30 clauses:
  16 enforced now, each citing the tests that prove it, and 14 deferred, each with a
  quantified trigger and the measurement that feeds it. Documentation only.
- **Operator local-day window for `task_list` (task #1430).** `local_day`
  (`today` or `YYYY-MM-DD`) resolves in `tz`, `UA_OPERATOR_TZ` or the host zone
  and matches tasks created, completed or updated that local day. Late-evening
  work that is already the next UTC day is no longer dropped. Adds offset-required
  `created_since` / `updated_since`. Stored timestamps stay UTC.
- **Acknowledging a live task's re-claim churn (task #1412).** `task_reassignment_thrash`
  had no way to stop paging for a task that is legitimately still open: #1368's
  terminal-status filter only covers finished tasks, so task #1285 — churn caused by the
  pre-#1337 lease-lapse defect, still open pending a human-gated deployment — kept
  reporting `critical` for the rest of the window. A task's notes may now carry an
  attributed, dated, rule-scoped acknowledgement:
  `[anomaly: understood task_reassignment_thrash @ <ISO-8601> by <agent>]`. It must be
  attributed and is honoured only for claims that strictly predate it, so a later claim —
  or one landing in the same second — re-pages on its own; a marker naming a different
  rule, missing `by <agent>`, backticked as a quoted example, unparseable, or dated in the
  future is ignored and the anomaly keeps reporting. Terminal tasks are unaffected — the
  status filter still runs first. Thresholds and the window are unchanged: this is the only
  suppression path for a live task, and it cannot hide new churn.

- **Verified-human OAuth consent (task #1322, Epic J3).** `/oauth/consent` and
  `/oauth/device` no longer accept the shared `UA_MCP_OAUTH_PASSPHRASE` by default. The
  approver must be a verified human: a WorkOS AuthKit session through the hub's WorkOS
  leg (enterprise; `Authorization: WorkOS` or the `ua_oauth_workos` cookie set by the new
  `/oauth/approver/workos/*` sign-in), or a tailnet node owned by
  `UA_MCP_OAUTH_APPROVER_LOGINS` plus a WebAuthn passkey registered once in Control
  Center → Devices (private). The passphrase approves only with
  `UA_MCP_OAUTH_PASSPHRASE_FALLBACK=1` and is recorded `human.verified=false`. Consent
  and device-approval audit events, `hub.oauth.token_issue`, the token's claims
  (`human_approval`, kept across refresh), `/oauth/whoami` and request auth metadata
  carry `verified_via` and the approver. The consent form gains a per-request CSRF token
  and a same-origin check. **Deploy note:** a hub configured with only the passphrase
  keeps serving existing tokens but approves nothing new (doctor: DEGRADED) until
  `UA_MCP_OAUTH_APPROVER_LOGINS` or the fallback flag is set.
- **`unified-agent node` (task #1327, Epic I1).** A device now runs as a ua-node:
  it connects out to the hub over the tailnet (no listening port), authenticates with
  the `unified-agent login` device token (refreshed near expiry), registers measured
  host facts (OS, harness binaries on `PATH` with versions, worktree roots),
  heartbeats so the Devices pane shows a `host_source: node` row with
  `reported: true` instead of an MCP-identity guess, long-polls its own dispatch
  queue, runs items through the existing executor backends, and posts one idempotent
  receipt per item. New bearer-only routes under `/api/node/*`; queuing work is the
  approval-gated `node_dispatch` prod action. Exponential backoff with jitter,
  hub-relayed cancellation through `kill_check`, clean SIGTERM shutdown, and
  systemd (with a cgroup memory ceiling) and launchd service templates.
- **Node-routed scheduler launches (task #1327, I1).** With
  `UA_DISPATCH_NODE_ROUTING=1` (off by default, independent of
  `UA_DISPATCH_REAL_SPAWN`), approved-dispatch scheduler ticks launch governed
  runs for an agent label onto that label's registered ua-node: live-node
  selection by freshest heartbeat with backend advertisement, fail-closed
  `node_offline:*` / `node_backend_unavailable:*` / `node_no_worktree_root`
  errors instead of silent hub-local spawns, and worktree-root-aware cwd
  choice. The node's claim/heartbeat drives the linked run's lease and
  `running` state; lease expiry settles fail-closed — unclaimed items retry
  (`node_unclaimed`), claimed items fail `lease_expired_outcome_unknown` for
  reconciliation, never a silent relaunch.

- **Client ID Metadata Documents (task #1323, Epic J4).** With `UA_MCP_OAUTH_CIMD=1`
  the hub accepts HTTPS URL `client_id`s, the MCP auth spec 2025-11-25 preferred
  registration, alongside DCR. Documents are fetched through the SSRF shield in its new
  strict mode (`safety.check_fetch_host(strict=True)`: globally routable addresses only,
  `UA_ALLOW_LOCAL_FETCH` ignored; IP-pinned, no redirects, size- and time-bounded,
  concurrent fetches deduplicated and capped), validated
  (`client_id` equals the URL, public client, `redirect_uris` present), and cached with
  a TTL; failures are negatively cached. `UA_MCP_OAUTH_CIMD_ALLOW` restricts hosts. The
  consent page always shows the document's host, so a remote name cannot pose as a
  fleet agent label. AS metadata advertises `client_id_metadata_document_supported`.

- **Tenant-scoped managed secrets (task #1343).** Managed secrets are keyed by
  `(org_id, name)`: `managed-secrets/orgs/<org_id>/<name>` for a tenant, the original
  `managed-secrets/<name>` otherwise (byte-identical). Two tenants can hold the same
  name without either resolving the other's value; a tenant never falls back to the
  root; an org-agnostic read of a name any tenant holds fails closed.
  `managed_secret_set` takes `org_id` (limited to the caller's own org under an
  enforced tenant boundary); Zulip and evidence-signing consumers resolve under
  `edition.tenant_org_id()`. New `managed_secrets.delete_secret` for per-tenant revoke. `agent_tokens` rows are
  tenant-scoped too (`org_id`, per-tenant hash uniqueness; SCHEMA_VERSION 34).

- **Durable OAuth store and device revoke (task #1321, Epic J2).** A hub restart no
  longer logs devices out. Dynamically registered clients, refresh tokens, and access
  tokens persist in the hub DB (`oauth_clients`, `oauth_refresh_tokens`,
  `oauth_access_tokens`; SCHEMA_VERSION 33). Tokens and confidential client secrets
  are stored only as SHA-256 digests; the device binding and auth method survive
  rotation after a restart. Revoke and rotation are durable and serialized. New
  `oauth_device_manage` tool (list / approval-gated revoke) and a **Signed-in
  devices** table with Revoke in the Devices pane (`POST /api/action/oauth-revoke`).
  New `system_doctor` row `oauth_store` with client, session, and device counts.

- **Device sign-in (OAuth device flow, task #1320).** `unified-agent login --hub <url>`
  signs a device in to the hub the way a connector does: approve a short code in
  any browser at `/oauth/device`, and the device receives a refreshable token for
  the built-in public client `unified-agent-cli`, bound to its tailnet node
  (`device_node` claim, shown by `/oauth/whoami`). RFC 8628 errors
  (`authorization_pending`, `slow_down`, `access_denied`, `expired_token`),
  single-use codes, client binding, public-Host identity pinning, and audit events
  with both device and approver nodes. AS metadata now advertises
  `device_authorization_endpoint`. Off unless `UA_MCP_OAUTH=1`. Design:
  `docs/designs/2026-09-24-fleet-console-and-connector-auth.md` (Epics I #1319, J #1318).

### Changed

- **Continuous WAL replication retired (tasks #1410/#1411).** The Litestream
  replica of the hub `memory.db` is deleted and the Mac LaunchAgent is archived
  to `deploy/mac-hub/retired/`. Its L0 retention loop could never prune —
  the uploader held `storage.objectCreator` without `storage.objects.delete` —
  so it re-listed a 250,631-object replica every 15 s. GCS bills the JSON list
  *response payload*, and that was invoiced as the "Download Worldwide
  Destinations" SKU at **~$65/day, ~$352 over six days** (Sep 20-25 2026)
  against an already-depleted account. `run-litestream.sh` now refuses to start
  without `UA_LITESTREAM_ENABLED=1`, `setup-agents-and-executors.sh` no longer
  stages the plist, and `tests/test_litestream_assets.py` guards the retirement
  (the plist must stay un-staged and `KeepAlive` false). The nightly snapshot
  (`run-database-backup.sh`, 24 h RPO) is the supported recovery path, and
  `system_doctor` reports `wal_replication` as `DORMANT` by design — not a
  fault. Re-enabling is documented and deliberately gated; see
  `deploy/mac-hub/retired/README.md`.

### Fixed

- **Vault sync no longer bursts SSH connections at the Mac hub (task #1492).**
  `SSHTransport` opened a fresh SSH connection for every call, and `_stage_pull` makes five
  per pulled file (stat, hash, pull, stat, hash). A ten-file pull opened ~50 connections in
  seconds, after which the hub refused every exec with `exec request failed on channel 0`
  for 20+ minutes: the pull failed with `ok: false`, later runs reported
  `reachable: false`, and unrelated `git push` to the Mac failed too. All calls in a run
  now share one multiplexed connection (`ControlMaster=auto`, private `0700` socket
  directory, `ControlPersist=60`), which `run()` closes with `ssh -O exit` even when the
  sync fails. Measured against the live hub: 40 stat calls over one TCP connection at
  0.08 s each, versus 0.34 s each unmultiplexed. `UA_VAULT_SYNC_SSH_MULTIPLEX=0` turns it off.

- **Live claims are renewed, not stolen (task #1337).** Queue hygiene and
  `system_doctor` reopened any `doing` task whose 1-hour lease lapsed, even while
  its claimant was online and working; the agent re-claimed it and tripped
  `task_reassignment_thrash` (11 tasks on 2026-09-24). A lapsed lease is now
  renewed when the claimant has hub traffic within `UA_HYGIENE_LIVE_CLAIMANT_MINUTES`
  (60) and the task was touched within `UA_HYGIENE_ACTIVE_TASK_HOURS` (24);
  renewal never bumps `updated_at`, so abandoned claims still release. A
  same-agent re-claim after a lapse is a renewal, not a new claim event. The
  thrash rule now requires 2+ agents, or one agent at twice the threshold.

### Added

- **`settings.toml` personalization.** A per-install settings file in
  `UA_HOME` supplies defaults for `UA_*` variables (environment still wins),
  with a `unified-agent settings` CLI (`show`, `get`, `set`, `unset`, `keys`,
  `check`). Secret-looking keys are refused. `unified-agent init` now takes
  `--operator-name`, `--operator-label`, `--workspace`, `--vault` and
  `--interactive`, writes the file, and mints a token for the human operator
  alongside the host agent token. See `docs/settings.md`.
- **Public-release tooling.** `scripts/public_release/` adds an anonymity
  ratchet (`make anonymity-check`) and an anonymized export with fresh
  history. `scripts/clean_install_smoke.sh` (`make clean-install-smoke`)
  installs into an empty `HOME` and requires a clean `doctor` and an
  operator-profile token. An interim `LICENSE` and `THIRD_PARTY_NOTICES.md`
  are added.

### Changed

- **The human operator is configured, not compiled in.**
  `operator.name`, `operator.label` and `operator.aliases`
  (`UA_OPERATOR_NAME`, `UA_OPERATOR_LABEL`, `UA_OPERATOR_ALIASES`) now decide
  which login is human. They govern approvals, the `operator` profile,
  persona, dispatch attribution, lifecycle authority and evidence. The
  defaults are `Operator` / `Desktop_Operator`. A label naming a catalogued
  agent or a `UA_AGENT_LABELS` worker is refused, and so is a name or alias
  held by a worker bearer; keep the operator out of the agent roster. A new
  `operator_identity` doctor row names the operator login and reports any
  refused label, refused name, or missing operator bearer. `org.yaml` refers
  to the operator as `$operator` / `$operator_name`. **Upgrading an existing
  hub:** set the operator's name and label (in `settings.toml` or
  `service.env`) to the identities it already uses before restarting, or that
  login loses operator authority.
- **`settings.toml` must be private to its owner.** A file not owned by the
  current user, or writable by group or others, is ignored with a warning.
  Its `[env]` table cannot set `UA_ENFORCE_AUTH`, `UA_LEGACY_OPEN_POSTURE`,
  `UA_APPROVAL_TOOLS`, `UA_TRUST_STDIO`, `UA_REQUIRE_CALLER_IDENTITY`,
  `UA_LIFECYCLE_OPERATORS`, `UA_HOME` or `UA_SETTINGS_FILE`.
- **Default workspace is `$UA_HOME/workspace`** when neither `UA_WORKSPACE`
  nor `paths.workspace` is set. Hubs that relied on the old `~/agent-workspace`
  default must set it explicitly.

### Fixed

- **Mailbox pages and acks by commit order, not ULID (#1870).** `message_list`,
  `message_inbox` and `message_ack` used a ULID cursor, which can skip a message
  that committed late behind a newer ULID, and an ack-through-ULID could mark such
  a message acked unseen. `message_index` now carries `event_seq` (the event's
  `event_log` ROWID, filled on every send, in backfill, and repaired from the
  event log at startup for existing rows; added by `ensure_schema`, no schema
  version bump). Rows are ordered by it, every row returns `seq`, and the lists
  take `after_seq` and return `next_seq` as the cursor. The ULID cursors were
  retired before any consumer used them (any ULID cursor is ambiguous when ULID and
  commit order differ): `since` now raises an explicit error, `next_cursor` is no
  longer returned, and `message_ack(through=...)` takes a seq integer or the
  `message_id` of a mailbox row and acks only rows at or below its seq. A digit
  string is a `message_id`, not a seq. Rows whose event was pruned before the
  column existed keep a null `seq`, are skipped by cursor paging, and are acked
  by `through` only with `include_pruned=True`. Session addressing (#1860) is
  unchanged. See `docs/tools-reference.md`.

## [0.3.2] — 2026-09-23

*Patch release. Adds the single-repository Approval Ledger PR poller and
improves private hub and install paths.*

### Added

- **Approval Ledger PR poller (#743).** The Mac PR watcher can submit main-branch
  PR heads to `github_app.process_pr_event`, resume named-human approvals, and
  recover an uncertain evidence-comment post without duplicating it. The
  connector remains opt-in and ships with a policy example requiring approval.

### Fixed

- **Private hub IAP loopback transport (#741).** The cloud mirror can use the
  local IAP endpoint while retaining its authenticated tunnel boundary.
- **Published image install default (#742).** Docker and Compose installs
  default to the published v0.3.0 image until a v0.3.2 image is available;
  source installs resolve to the v0.3.2 tag after it is published.

## [0.3.1] — 2026-09-23

*Patch release. Adds report-level identity attestation for evidence artifacts,
improves MCP session reliability and SDK patch semantics, hardens approval and
release retries, and expands operator controls. Depot CI now runs Python tests
in four duration-aware shards while retaining the audit and attribution gates;
clean web installs use the committed lockfile.*

### Added

- **Report-level attestation block (task #1267, Epic F/F4).** Evidence
  reports (JSON/CSV/Markdown/text/PDF, schema `1.4` → `1.5`) now carry a named
  `attestation` header field naming who attests to the report itself — the
  caller that generated it — distinct from per-record `human_provenance`.
  Human attestation is reserved for a caller authenticated through the hub's
  live WorkOS/IdP session; bearer-only callers remain explicitly
  `agent-bearer-only`, and unauthenticated builds render `unknown`. The block
  carries the signing key id (never key material), generation time, and report
  filter scope. `unified-agent evidence verify` reports the block and checks
  that human claims have IdP evidence and the attestation key id matches the
  signature sidecar. See docs/evidence-report.md "Report-level attestation".

- **`UA_MCP_SESSION_IDLE_TIMEOUT_S` — configurable hub MCP session idle
  timeout (task #1298).** The MCP SDK reaps a stateful streamable-HTTP
  session after 1800s (30min) with no request in flight
  (`DEFAULT_SESSION_IDLE_TIMEOUT`, `mcp/server/streamable_http_manager.py`),
  which was silently dropping 24/7 clients (hermes gateway symptom:
  `MCPError: Session not found` / `Session termination failed: 404` every
  ~20-90 min). `server._http_settings()` and `server.wrapped_http_app()` now
  both forward `session_idle_timeout` when the variable is set — unset/empty
  leaves the key out entirely so the SDK default is unchanged; `0` maps to
  the SDK's own disable value `None` (the SDK rejects a literal `0`); a
  positive integer passes through as seconds; negative or non-numeric values
  raise `ValueError` at startup. `config.mcp_session_idle_timeout_s()` /
  `config.mcp_session_idle_timeout_configured()`.

### Changed

- **Python CI tests run in four timing-aware shards (task #1301).** The
  complete pytest file set is split across four parallel Depot jobs. Each
  shard uploads JUnit timing data; after the first run, Depot uses observed
  durations to balance future runs. Lint, smoke, security-audit,
  attribution-coverage, and audit-ledger gates still run and must pass. The
  manual GitHub-hosted fallback remains a complete unsharded test run.

### Fixed

- **`work_product_submit` serializes and resumes release attempts (task
  #1297).** Concurrent identical retries could both read an approved pass;
  one consumed it while the other queued a second approval. A process exit
  after consumption could also leave the work product permanently blocked.
  Submissions now acquire a per-product OS advisory lock before checking the
  linked approval. A live concurrent attempt gets a clear in-progress error;
  the OS releases the lock after a process exits. A later retry that acquires
  the free lock resumes the idempotent release with the already-consumed pass,
  so it can complete an identical file copy and commit the release evidence
  without creating another approval. Barrier-based concurrent retry coverage
  and an abandoned-consumption recovery test are in
  `tests/test_work_product.py`. Requeued approvals replace only inert linked
  passes and must match the exact work-product payload; temporary release
  copies preserve normal umask-derived permissions. A coordinator lock allows
  successful attempts to remove their per-product lock file without allowing
  concurrent callers to lock different inodes.

- **Review pipeline: full timeout-cause chain walk, configurable bun-test
  ceiling, capped head-less retries (task #1295, review-sweep follow-up to
  #1254/#691).** `reviewer._pytest_timeout_cause` (renamed
  `_test_timeout_cause`) unwrapped exactly one `__cause__` level to recover a
  `PytestTimeoutError` hidden behind `git_merge_task`'s
  `ToolError(str(e)) from e` — correct for today's single wrap, but silently
  wrong the moment a second wrap was added anywhere in that path. It now
  walks the visible `__cause__`/`__context__` chain, honoring suppressed
  context, bounded and cycle-safe.
  `merge._run_bun_test` kept a hardcoded, unconfigurable 600s timeout and
  raised a bare `RuntimeError` on expiry — indistinguishable from a real
  bun-test failure, the same false-FAILED-verdict bug #1254 fixed for pytest
  but never for bun. Added a sibling `UA_REVIEW_BUN_TEST_TIMEOUT_S` knob
  (default 600s, unchanged) and a new `BunTestTimeoutError`, both sharing a
  new `merge.TestTimeoutError` base with `PytestTimeoutError` so
  `_test_timeout_cause` and `_validate_and_hold`'s timeout branch recognize
  either runner's timeout as verdict `timeout`, never `failed`. Separately,
  `_merge_done_task`'s head_sha resolution is best-effort. It uses the
  worktree record's SHA when rev-parse fails, so a new commit gets its own
  retry budget. Only when neither source has a SHA does
  `_timeout_count_total` apply a task-level fallback cap.

- **`docker.yml` and `install-gate.yml` now cancel superseded PR runs (task
  #1302).** `.depot/workflows/ci.yml` picked up a `concurrency:` block in
  #581 so a second push to a busy PR cancels the first run instead of
  billing and pending it to completion; `docker.yml` and `install-gate.yml`
  (and their `.github/workflows/` fallback mirrors) were missed. Both now
  share a workflow/ref concurrency group for pull requests; non-PR events use
  unique run-specific groups, so tag pushes and manual dispatches do not replace
  one another. `cancel-in-progress` is enabled only for pull requests.
  `tests/test_ci_workflow_parity.py` gained
  `test_depot_automatic_workflow_cancels_only_superseded_pr_runs` and
  `test_github_mirror_matches_depot_concurrency` so a future automatic
  workflow can't be added without the same guard.

- **`packages/web` npm install restored to `npm ci` in CI (task #1293).**
  The web job's `npm install` workaround (`.depot/workflows/ci.yml`,
  `.github/workflows/ci.yml`, and the Install Gate build step in both
  `install-gate.yml` mirrors) dated back to an unreproduced report that a
  clean `npm ci` failed on CI while passing locally. The local pass used a
  machine-level `legacy-peer-deps=true` setting, which masked the lockfile's
  incompatibility with CI's default npm configuration. Regenerated
  `packages/web/package-lock.json` with npm 10.9.8 and default peer resolution;
  a clean `npm ci` now installs successfully without machine-specific config.
  All four workflow files run `npm ci` for `packages/web`;
  `packages/client`'s separate, still-open `npm ci`-fails-on-CI report is
  untouched and out of scope here.

- **Typed SDK patch and work-product writes (tasks #1300 and #1305).**
  `TaskUpdateRequest.priority` now stays unset when omitted, so a generated
  Python model cannot silently reset a high/low task to medium on write-back.
  The generator preserves public enum classes and enum-valued fields;
  `TaskCreateRequest.priority` retains its schema-owned medium default.
  Typed task updates expose policy-gated `title` and `parent_id`, while
  caller-controlled `untrusted` remains excluded. The SDK now also exposes
  typed work-product submit/release writes; authenticated hub HTTP routes
  enforce the same profile, policy, approval, and producer checks as MCP.
  The first-party CLI uses those SDK methods for a hub-workspace file and an
  approval-bound release retry. The in-repo SDK mirror matches canonical SDK
  main, including generated schemas and its CLI adapter.

- **Cold-start doctor test hermetic against ambient LLM-provider env (task
  #1294).** `tests/test_bootstrap.py::TestColdStartIntegration::test_init_
  then_doctor_is_clean` already ran clean on `main`, but only because
  `tests/conftest.py`'s `isolated_env` fixture hand-cleared the exact env
  names `doctor._llm_provider_chain_check()` reads — a list that could drift
  from the check's real read surface with no test to catch it. Added
  `llm_client.PROVIDER_CHAIN_ENV_VARS`, the tuple of names that actually flip
  a leg's configured/explicit state in `leg_status()`; `isolated_env` now
  clears exactly that tuple instead of a hand-copied list. New regression
  test `test_hermetic_against_ambient_llm_provider_env` runs the cold-start
  test in a subprocess seeded with fake values for every name in the tuple
  *before* pytest starts, proving genuine OS-level env inheritance (not just
  monkeypatch-internal state) is neutralized. Doctor's gate/status logic is
  unchanged.

### Changed

- **Default (developer) profile shrunk to 99 exposed tools** (task #1230,
  Epic B/B2; measured and pinned by
  `tests/test_profiles.py::test_default_profile_tool_count_is_capped_at_100`,
  ceiling 100). 32 names were pruned from the default profile — retired
  transports (`zulip_*`), PARK'd surfaces per the #1121 consumer-disposition
  table (`horizon2_*`, `browser_derive_task_reference`, `graph_canvas_*`),
  operator/admin provisioning (`agent_token_*`, `credential_pool_*`,
  `managed_secret_set`), the notify family, in-process maintenance surfaces,
  and steward vault custodianship. Removal is by profile only: every pruned
  tool remains registered and reachable under steward/chief-of-staff, and a
  denied call logs the deprecated name so real consumers surface. Path to
  ≤60 by day 90: the prune set grows as that log shows zero real consumers.

### Added

- **Pilot onboarding kit for the H2 work-product release workflow (task
  #1280, Epic H/H4).** `docs/pilot/`: four synthetic policy template packs
  (professional services general, accounting/tax, healthcare administration
  — non-clinical, and legal), each a YAML file that loads through the real
  `unified_agent.tools.work_product.load_template_pack` schema plus a
  one-paragraph README, with an optional (documented-not-enforced)
  two-person-rule field; a reviewer quick guide for the Approvals pane
  (approve vs. deny, what "unverified device" and "agent-bearer only" mean);
  an evidence guide covering export, `unified-agent evidence verify`, the
  RFC-3161/git external anchor (task #1265), and the #1217 honesty rule that
  an agent's signature is never presented as human sign-off; and a support
  and rollback page (`UA_WORK_PRODUCT_ENABLED=0`, data export before exit).
  `tests/test_pilot_policy_templates.py` loads every template pack through
  the real loader so a broken pack fails CI instead of rotting silently.
  `docs/customer-readiness.md` and `docs/controls-narrative.md`'s "Gaps and
  roadmap" section were refreshed to cite the now-shipped F2 external anchor
  (PR #707) and provenance graph (PR #704) instead of listing them as
  pending.

- **Approvals detail drawer: the reviewer's primary screen, mobile-usable
  (task #1263, D3).** `GET /api/approvals/{id}` returns full single-row
  detail for the Control Center approvals table's new "Details" action: the
  gate that parked the request (today always the global allowlist), the
  requesting agent and its last-known device (marked `device_verified` only
  when the hub itself observed the request via `last_seen_via ==
  "hub_request"` — a self-reported heartbeat host renders as unverified,
  applying the #1217 honesty rule to devices, not just identities), task
  lineage, and a `pr_url`/`commit_sha` when the gated payload names one. The
  payload itself passes through `sanitize_metadata` before it reaches the
  browser — never returned verbatim. `ApprovalDrawer.tsx` renders this as a
  full-height sheet below the `sm` breakpoint (375px target, touch targets
  ≥44px, no page-level horizontal scroll), keeps Approve/Deny (with an
  optional deny reason) in the drawer, and surfaces the approval
  optimistic-concurrency error (CAS, PR #592 — "changed concurrently") as a
  clear inline message instead of a silent failure. Deep link
  `?view=approvals&approval=<id>` opens the drawer directly. No new mutation
  route; resolution stays on the existing `POST /api/action/approval`.

- **Provenance graph: the evidence ledger's 5-link chain as queryable graph
  objects (task #1287).** `src/unified_agent/audit/provenance_graph.py`
  derives nodes (human, device, agent, task, action, approval, work_product;
  `model`/`release` declared but not yet populated — no backing ledger
  column) and edges from the same tables `evidence_report.py` already reads,
  with a canonical sha256 hash over the sorted node/edge serialization —
  deterministic across rebuilds and a JSON round trip, and sensitive to a
  tampered or removed ledger event. Two named queries: `by_human` ("everything
  this human authorized in time range X") and `by_release` (every action
  behind a set of task ids — keyed on task ids because the ledger has no
  release/git-tag record yet; documented gap). Exposed at
  `GET /api/platform/provenance-graph` (alongside
  `/api/platform/objects/{kind}`, same `event_log_query` profile gate) and
  `unified-agent platform provenance-graph`; no new MCP tool, to keep the
  default profile's tool budget flat. `build_evidence_report()` gains a
  `provenance_graph` appendix (schema `1.2` → `1.3`) rendered in the
  Markdown/text reports. See [docs/provenance-graph.md](docs/provenance-graph.md).
- **Provenance graph Control Center view (task #1287, PR 2/2).** New
  `?view=provenance` panel (`packages/web/src/provenance/`), registered in
  the nav under the Evidence section. Queries
  `GET /api/platform/provenance-graph` by human, by release task ids, or by
  the plain task/project/time-window scope, and renders the result as an
  accessible layered layout — one column per node kind in accountability-
  chain order (human → device → agent → task/action → approval/work
  product) plus a relationships table of every edge — rather than a
  force-directed canvas, matching the accessibility call the Task Graph pane
  already made. Shows node/edge counts, per-kind counts, and the truncated
  canonical hash. A `human`/`device` node without `attrs.verified === true`
  renders an explicit "unverified" badge (#1217: never let an unresolved or
  agent-bearer identity look like a confirmed human in the UI). Clear 400/
  403/404/empty states. See
  [docs/provenance-graph.md](docs/provenance-graph.md#control-center-view).
- **Work product release: the non-code pilot wedge (task #1278, H2).** An
  agent-produced document/file (report, filing, remediation, patient note,
  audit workpaper) can now be submitted, named-reviewer approved, and
  released with a full evidence chain: human -> device -> agent -> model ->
  document hash -> approval. New `work_products` platform object
  (`object_model.OBJECT_TYPES`, `SCHEMA_VERSION` 32), the `work_product_submit`
  MCP tool (off by default via `UA_WORK_PRODUCT_ENABLED`, pruned from the
  default profile per Epic B, classified `WITHHELD`), and the
  `unified-agent work-product {submit,verify,list,evidence,diff,packs}` CLI. Vertical
  specifics (required tags, reviewer role, release subfolder) come from a
  data-driven template pack (`src/unified_agent/templates/work_product/*.yaml`,
  shipped in the wheel); two synthetic
  packs ship (`legal`, `accessibility-remediation`). Reuses the existing
  approval queue and the hash-chained evidence ledger. The submitter can
  never approve its own submission: `approval_resolve` now refuses an
  approval by the requesting identity for tools in
  `approvals.SEPARATION_OF_DUTIES_TOOLS` (`work_product_release`), under every
  profile. A resubmitted title links `previous_version_id` and `work-product
  diff` shows the reviewer what changed; `work-product evidence` reads the
  chain back from the ledger, marking the human link verified only from a
  signed producer event for that approval (and saying whether by IdP or agent
  bearer) and the device only from that event's tailnet node.
  `work_product_verify` re-hashes to flag tamper (after release the released
  copy is the record). See [docs/work-product-release.md](docs/work-product-release.md).

- **Continuous hub -> desktop vault sync + divergence alert (task #1288)**,
  extending task #816/PR #421's `deploy/vault-sync.sh`. Replaces that
  whole-repo `git pull --rebase` with a path-scoped SSH file-sync engine
  (`src/unified_agent/vault_sync.py`) that pulls the Mac hub's authoritative
  `Wiki/`, `Logs/`, `00 - Inbox` into the desktop vault every 10 minutes
  (`ua-vault-sync.timer`, `scripts/install-vault-sync-timer.sh`). A
  desktop-side edit newer than the hub's copy is never silently overwritten —
  the incoming hub content is preserved as a `<name>.conflict-<timestamp>.md`
  copy instead, with the desktop original left untouched; a retired path
  (`Logs/Laptop`, per task #1256) is filtered out of every pull so it can
  never be resurrected. `system_doctor`'s new `vault_sync` check reports
  LIVE (fresh sync, zero conflicts), MISCONFIGURED/`degraded:` (stale sync or
  outstanding conflicts, with counts), or DORMANT on a host that doesn't run
  this timer (the Mac hub itself, by design — direction is one-way).
  `docs/vault-sync-operations.md` has the conflict rules, config, doctor
  contract, and rollback path.
- **Pluggable evidence chain-head anchor backends (task #1265, Epic F/F2).**
  `unified-agent audit re-anchor --backend {vault,tsa,git}` now anchors the
  audit-ledger chain head to an external witness independent of the
  operator: an RFC-3161 Time-Stamp Authority (`audit/anchor_tsa.py`, real
  ASN.1/CMS request-build + offline signature verification against an
  operator-supplied cert, `UA_EVIDENCE_TSA_URL`/`UA_EVIDENCE_TSA_CERT_PATH`),
  or a commit to a separately configured git repo
  (`audit/anchor_git.py`, signed by whatever the operator's ambient git
  config already does — never generates or stores a key,
  `UA_EVIDENCE_ANCHOR_GIT_REPO`/`UA_EVIDENCE_ANCHOR_GIT_DIR`). The existing
  vault-note witness (task #768) stays the default (`UA_EVIDENCE_ANCHOR_BACKEND`)
  so nothing regresses. Evidence reports (JSON/CSV/Markdown/text/PDF, schema
  1.4) carry a new `anchor` header field with the reference and a
  third-party verification recipe; `unified-agent evidence verify` checks it
  when present (pass/fail with reason; absent = `not anchored`, not a
  failure). See docs/evidence-report.md "Chain-head anchoring".
- **`github-app` registered as a real `ConnectorManifest` (task #1264, D4).**
  `GITHUB_APP_MANIFEST` (`connectors.py`) declares `poll`/`check_runs`/
  `commit_statuses`/`evidence_comments` and the alternative App-mode and
  user-token secret groups. Its status hook checks only the active
  `UA_GITHUB_TOKEN_MODE` and reports an error as UNKNOWN if the hook fails.
  `system_doctor` now reports a `connector:github-app` row and the Connectors
  pane lists the manifest and secret status, same as Slack and Zulip.
  `github_app.GITHUB_APP_MANIFEST` keeps its prior mapping shape for callers;
  `github_app.github_app_manifest()` keeps its per-mode dict shape. Off by
  default via both `UA_GITHUB_APP_ENABLED` and `default_enabled=False`.
  App-mode credentials alone leave runtime provider readiness UNKNOWN until
  an installation-token provider is supplied at the call site.
  New conformance suite:
  `tests/connectors/test_github_app_conformance.py`. Docs:
  [`docs/connectors.md`](docs/connectors.md#github-app-a-registered-manifest-with-two-token-modes-task-1264),
  [`docs/connector-activation.md`](docs/connector-activation.md#registration-status-and-rollback-task-1264).
- **Controls narrative: shipped features mapped to EU AI Act / ISO 42001 /
  SOC 2 (task #1281, Epic H5).** `docs/controls-narrative.md` maps the
  evidence-ledger surface (hash-chained event log, `unified-agent-audit
  verify`, signed evidence reports, the approval gate, agent token
  lifecycle, least-privilege profiles, Litestream replication) to named
  controls — AI Act Art. 12/14/19/26 with a verified application timeline
  (the Art. 113 high-risk deadlines were postponed by the "Digital Omnibus
  on AI," Regulation (EU) 2026/1744, from 2 August 2026/2027 to 2 December
  2027/2 August 2028), ISO/IEC 42001:2023 AIMS control areas (clause-level
  text withheld — paywalled, not invented), and SOC 2 CC6/CC7/CC8. Every row
  cites a file path or test and states "supports"/"provides evidence
  toward," never "complies with" or "certified." Work-product release
  (PR #701) is merged to `main` at `071431d`; live deployment remains a
  separate verification gate.
- **curl|bash installer + fresh-machine install gate (UA #1195).** `install.sh`
  wraps the documented cold-start flow (`uv tool install` → `unified-agent
  init` → `unified-agent doctor`): refuses root, installs a pinned
  checksum-verified `uv` only when missing, is idempotent (`--force`
  reinstall, init never rewrites `service.env`), and never prints the
  generated token. `.github/workflows/install-gate.yml` runs it on every `v*`
  tag (and PRs touching install assets) on a fresh runner — fetched over HTTP
  with sha256 verification — then proves the install end-to-end: doctor clean,
  HTTP hub with auth enforced (bearer-less `tools/list` refused), exactly one
  gated action parked → resolved over HTTP by the operator profile → consumed
  on retry (`deploy/install-gate/policy.yaml` gates `system_doctor`), evidence
  rendered as Markdown + PDF, all uploaded as run artifacts.
- Continuous WAL replication of `memory.db` to GCS via Litestream
  (`deploy/litestream/litestream.yml`, `UA_REPLICA_GCS_URI`), running on the
  Mac hub as the `com.unified-agent.litestream` KeepAlive LaunchAgent —
  RPO under 60 s vs ~24 h for nightly snapshots. The replica is a cold
  standby; the Mac remains the sole write authority and promotion is a
  documented manual drill, deliberately not automated (task #1197).
- `system_doctor` `wal_replication` check: compares `litestream status`
  local txid against replica-side `litestream ltx` max txid and newest LTX
  age; LIVE when caught up or syncing inside the interval, MISCONFIGURED
  past the 60 s lag budget (task #1197).
- `scripts/db_restore_rehearsal.py --from-replica`: restores the GCS WAL
  replica into a scratch `UA_HOME` (`litestream restore -integrity-check
  quick`) and runs the audit hash-chain verification against the restored
  database — the promotability proof for the cold standby (task #1197).
- Runbook: `docs/database-operations.md` §4 covers enable steps,
  monitoring, the restore rehearsal, and the manual cold-standby promotion
  drill including old-authority retirement (task #1197).
- **Held-despite-green ⇒ same-day regression test** (task #1260, Epic C
  #1188, company OS step 5). `src/unified_agent/held_pr.py`'s
  `held_despite_green()` is the pure, unit-tested decision (a `[review:
  rework]` note or `verdict="rework"` plus green exact-head CI); `gh_pr_check_conclusions`
  / `label_held_pr` are the read/write `gh` wrappers. `unified-agent-review
  label-held <task_id>` (new CLI subcommand) is the one explicit,
  opt-in operator step that applies the GitHub `held` label — kept out of
  the unattended scan/task pipeline on purpose, since that pipeline stays
  strictly read-only against GitHub and never records a rework verdict
  itself. `.depot/workflows/ci.yml`'s new `held-pr-regression-test` job
  (`scripts/check_held_pr_regression_test.py`, mirrored in
  `.github/workflows/ci.yml`) fails a held-then-re-pushed PR that adds no
  test under `tests/` (or `packages/*/src/**/*.test.*` / `__tests__/`) and
  carries no `Regression-Test: <path>::<name>` trailer, and skips cleanly
  for a PR that was never held. See docs/automation.md's "Held-despite-green"
  section.

## [0.3.0] — 2026-09-19

*Second release, two months after 0.2.0. Headline: the platform's proof layer —
user-attributable evidence reports (Markdown/JSON/PDF) over the hash-chained
event log, with unattributed rows excluded rather than invented — and a
hub-served web Control Center at `/ui` over the same policy-gated tool
registry MCP clients use. Dispatch and the executor now route to each agent's
native headless CLI (claude/codex/agy/hermes, plus Copilot); task-bound git
worktrees have a real create/dispose lifecycle. MCP surface grew elicitation
with nonce-bound approval challenges and an OAuth connector for remote
clients. Ops hardened: cold-start `init`/`doctor`, GCS backup restore and
Tailscale failover rehearsals, perf-sample retention, and a weekly
disaster-recovery export.*

*Changelog-coverage note: several capabilities merged in this window carry no
entry below — the YAML policy engine (tool/role/threshold rules,
`docs/configuration.md`), the React Control Center port in `packages/web`
(`docs/web-ui.md`), the platform object model with ten served resource types
(`docs/platform-object-model.md`), HMAC-signed producer events and the
attribution-coverage gate (`docs/evidence-report.md`), the open-core edition
seam (`docs/open-core-architecture.md`), the WorkOS single-organization pilot
adapter (`docs/workos-pilot.md`), and the Devin executor backend. They are
recorded in their docs and in the hub task ledger; from 0.3.0 onward every PR
to `main` is expected to carry a changelog entry.*

### Added

- **Docker image as a release artifact (UA #1194).** Multi-stage root
  `Dockerfile` builds the hub (Python 3.12, `uv sync --frozen --no-dev
  --extra pdf`, packages/web Control Center bundle built in a node stage)
  and runs it as non-root `ua` with `/data` as the state volume. First boot
  runs `unified-agent init` and prints the generated bearer token exactly
  once; `deploy/docker/entrypoint.sh` re-applies `UA_TRANSPORT=http`,
  `UA_HTTP_HOST=0.0.0.0`, and `UA_ENFORCE_AUTH=1` after sourcing
  `service.env` (`UA_DOCKER_ALLOW_INSECURE=1` opts out with a warning), and
  defaults `UA_HTTP_ALLOWED_HOSTS` to `127.0.0.1:*,localhost:*` plus
  `UA_PUBLIC_HOSTS`. `deploy/docker/compose.yaml` publishes on loopback only.
  New `docker.yml` workflow builds + smoke-tests the image on every relevant
  PR (healthz poll, `unified-agent doctor`, one-time-token assertions) and
  pushes `ghcr.io/anonb3ll/unified-agent:v<tag>` on `v*` tags — private GHCR,
  no `:latest`. See [installation.md — Docker](docs/installation.md#docker).

- **Tamper-evident PDF evidence export (UA #956, pilot slice, sibling of #943's CSV export).**
  `GET /api/evidence?format=pdf` now returns a generated PDF carrying the same
  per-record chain-integrity self-certification as the CSV export
  (`chain_status`, `chain_head_hash` on every record) plus the full
  human/device/agent/action/proof row content. Dependency: `fpdf2` (pure
  Python, no compiled extension), added to the existing `pdf` extra alongside
  `pypdf` (which handles the read/extract side for `web_fetch`). Failure
  modes are explicit rather than silent: an empty record set or a chain
  reporting `status=tampered` returns HTTP 422 with a clear error instead of
  an empty or falsely self-certifying PDF; the `/api/evidence` auth boundary
  is unchanged (401 without a token, same as every other format).

- **Utilization wire-ups: dormant ops capabilities lit (UA #904 → #905–#910).**
  From the 2026-09-10 Product Utilization Audit. (1) `git_merge_task` now runs
  the `post-merge-verify` workflow after a successful merge — a bounded pytest
  slice (merge / workflow / git machinery), `security_audit`, and `system_doctor`
  — recording a non-fatal `[warning: post-merge verification failed …]` note on
  the task when it trips. Opt out with `UA_POST_MERGE_VERIFY=0`; full-suite
  regression stays with GitHub CI. (2) The merge flow closes the delegation
  ledger: pending/in-flight delegation rows for the merged task are marked
  `completed` with `callback_result='merged'` and the task gets a
  `[review: merged @ …]` marker, so the ledger stops reading as write-only.
  (3) `scripts/install_core_docsets.py` builds and indexes offline Python 3 /
  SQLite / Git Dash-Zeal docsets so `dash_list` / `dash_search` return results.
  (4) New `weekly-export` workflow + `deploy/weekly-export-cron.sh` +
  `ua-weekly-export.{service,timer}` (Sun 02:00) export tasks and memory to the
  vault for disaster recovery. `export_tasks` / `export_memory` / `system_doctor`
  added to the workflow tool registry.
- **Skill-cluster proposals /evolve analog (UA #839 / ECC-6).** High-trust
  promoted `fact`/`decision` memories are clustered (lexical Jaccard, plus
  embeddings when present) into ranked candidate skill outlines in
  `00 - Inbox/Skill-Cluster-Proposals.md`. Project/author/source_host stay
  isolated; one-note clusters are dropped; secrets and injection-shaped
  text are redacted or refused. Propose-only: no `memory_save`, no
  `SKILL.md` / host skill or rule writes, no auto-approve. `propose_skill_clusters`
  exposes a dry-run; nightly `unified-agent-sleep-pass` runs the same pass
  before any LLM work. CLI: `unified-agent-evolve`.
- **Plan review `::preview` widget (UA #838 / ECC-5).** `plan_manage("preview")`
  writes escaped numbered-section HTML and a `::preview{file=…}` directive;
  `plan_manage("review")` applies the fixed `plan-review v1` approve / changes /
  annotate intents onto the existing `[waiting-for: plan-approval]` gate.
  Hermes desktop already owns the iframe and `hermes.send` bridge. See
  [plan-preview-widget.md](docs/plan-preview-widget.md).
- **Session-end instinct extraction (UA #837 / ECC-4).** `session_end` still
  calls `propose_session_learnings`, which now emits reviewable Inbox
  candidates from concrete session evidence (summary error→fix / durable
  rules, plus sanitized lifecycle `tool_end` failure→success pairs) with
  `confidence` and provenance. Candidates are deduped against promoted
  memories and earlier proposals, never auto-promoted, and skipped for
  empty/low-signal sessions. No model, network, or transcript store.
- **AgentShield-lite config surface scanner (UA #835 / ECC-2).**
  `unified-agent-audit surfaces` reuses the existing audit engine over
  allowlisted agent roots (hub config, host MCP files, hooks, skills/rules,
  env/secrets), emits a stable A–F grade, exits 1 on high findings, and
  files `[audit-triage:]` tasks through the same dedup as `audit_triage_tasks`.
  Daily `steward-audit-cron.sh` (09:00 LaunchAgent / systemd timer) calls it.
  MCP registrations besides `.mcp.json` (`mcp.json`, `.cursor/mcp.json`,
  Gemini `config/mcp_config.json`) and host `hooks.json` are in scope; header
  Bearer literals are flagged, `${env:}` / `{file:}` interpolators are not.
- **Control Center project view (UA #847 / PROOF-B2).** A `[project-spec]`
  root card on the existing Board shows its mixed-status descendant tree,
  `#846` rollup, evidence count vs merge-verified PR count, and a copyable
  `unified-agent evidence report --project <id>` pointer. No new API
  endpoint or mutation surface.
- **Project status rollup (UA #846 / PROOF-B1).** A `[project-spec]` root
  reports derived status from its entire descendant subtree on `task_list`
  (`rollup.complete`, `derived_status`, `active`, `counts`) and on the
  existing web UI board card. Completing the root is refused while any
  descendant is still `open`/`doing`/`waiting_review`/`blocked`; the error
  lists those descendants. No new tables or workflow states.
- **`system_doctor` `llm_provider_chain` check (UA #1178).** Per-leg
  configured/reachable status for the `llm_client` fallback chain
  (ollama / ai_studio / openrouter / subscription_pool) via
  `llm_client.leg_status()` — config presence plus unbilled metadata GETs
  (`/models`, `/v1beta/models`, `/api/v1/auth/key`), never a generation call,
  so a leg's health no longer requires spending on the metered AI Studio leg
  to find out. The env probed is the hub unit's Environment(File) merged over
  the caller's, matching `_hub_setting`'s describe-the-service rule.


- **PROOF-A2 merge-proof binding (UA #845).** `merge_verified` now requires the
  reported PR URL to match a PR captured in the merge note, so a stale
  `[review: merged]` annotation cannot bless a newer `evidence_ref`. Docs state
  both limit defaults (50 general, 500 project subtree).

- **PROOF-A1 evidence CLI artifact (UA #844).** `unified-agent evidence report
  --project <id>` renders the existing `project_chain` (spec, nested steps,
  merged PRs, last merge) plus inline audit-verify status as one Markdown or
  JSON artifact. No schema change. Sample:
  `docs/samples/2026-09-03-project-evidence-report.md`.

- **PROJECT-BUILD increment 2 (UA #831).** `evidence_report(project_id=N)`
  now renders the project spec root and complete multi-level task subtree in
  tree order, preserving the existing human → device → agent → action → proof
  records while adding a typed project chain over completed-step evidence,
  merged PRs, and the latest merge. Missing roots and projects without merged
  evidence return explicit empty/partial reports.

- **PROJECT-BUILD increment 1 (UA #830).** Task nesting is fleet-wide and
  multi-level: `task_add(parent_id=)` accepts a parent that itself has a
  parent, `task_list` returns `lineage` (ancestor ids, root-first), and
  reparenting refuses cycles and a depth cap of 8. `project_build(parent_task_id=N)`
  registers a `[project-spec]` hub task as the project root (pointer-to-task;
  no auto-decomposition, no new state machine, not human-approval-gated) and
  records the calling identity on the `event_log` hash chain. Missing task,
  missing marker, and wrong claimant fail closed with no partial state.
  Existing one-level parents keep working.

- **Grok Bot OAuth MCP connector (UA #810).** Remote clients that cannot send a
  static bearer (Grok Bot on xAI's cloud VM) can register at
  `grok.com/connectors` against the Mac hub. Opt-in via `UA_MCP_OAUTH=1` plus
  issuer and passphrase; tokens authenticate as `agent-grok` on the research
  profile. Mac stays SQLite authority. See
  [Grok Bot MCP connector](docs/grok-bot-connector.md).

- **Grok as a governed hub citizen (UA #772).** `agent-grok` / `agent-laptop-agent8`
  are first-class identities for attributed claims and completions. They skip
  task git worktrees and are not dispatch spawn targets. See
  [configuration.md](docs/configuration.md) and
  [COLLABORATION.md](docs/COLLABORATION.md).

- **Governed LangGraph plan→build→verify→merge chain (UA #777).**
  `langgraph_workflow_run` runs a four-hop graph on the existing checkpoint
  store. Build and merge are gated `task_update` receipts (merge is the second
  mutating node). The chain does not call `git_merge_task` and does not
  dispatch agents. Own-hop task revisions are adopted; foreign edits fail
  closed. See
  [LangGraph execution-plane ADR](docs/architecture/langgraph-execution-plane-adr.md#decision-second-mutating-hop--planbuildverifymerge-chain-task-777).

- **Cold-start install: `unified-agent init` + `unified-agent doctor` (UA #759).**
  A machine with none of the canonical `~/dev` topology can now run a hub:
  `uv tool install` → `unified-agent init` (creates `~/.unified_agent`, an empty
  DB, and a starter `service.env` with a generated bearer token; pins
  `UA_WORKSPACE` off the non-existent `~/agent-workspace` default; idempotent, never
  rotates an existing token without `--force`) → `unified-agent doctor` (wraps
  `system_doctor` for a shell, exit 1 only on MISCONFIGURED — every opt-in
  feature reads DORMANT on a cold box, which is correct). Also standalone
  `unified-agent-init` / `unified-agent-doctor` console scripts.
  `system_doctor`'s `zulip_relay` check no longer reports MISCONFIGURED on a
  host that simply does not run the Zulip relay — that is DORMANT; MISCONFIGURED
  is now reserved for a *partial* fleet config. See
  [installation.md](docs/installation.md#cold-start-on-a-foreign-machine).

- **Lane B validation-gate evidence view (UA #731).**
  `python -m unified_agent.lane_b_telemetry` prints a read-only JSON report
  for `workroom` and `sample_app_b`: outside installs (GitHub clone uniques),
  completed workflows (hub memory + task markers), repeat users (vault
  evidence notes), and direct feedback (vault + hub). A reachable empty
  source is count `0`; an unreachable source is `unavailable` with
  `count: null`. No new tables and no web UI route.
- **Horizon 2 connector packages (UA #669).** Named packages (Slack, Drive,
  Obsidian, Readwise, Granola) map onto `governed_servers` via
  `horizon2_package_list` / `horizon2_package_register`. Register still
  consumes the canonical `horizon2_server_register` approval. Live
  Slack/Drive/Obsidian dogfood stays task #592.

- **Horizon 2 OTLP/JSON span export + trajectory replay (UA #668).**
  `OTLPTraceExporter` maps recorded `TelemetrySpan`s to OTLP JSON with
  gen-ai attributes. No OpenTelemetry SDK. Opt-in via `UA_H2_OTEL_EXPORT=1`;
  network I/O only when a transport is injected. `export_and_replay` exports
  then calls existing `evaluate_trajectory`.

- **Horizon 2 connector review expiry and live MCP bind (UA #667).**
  `ServerGovernance.authorize` refuses expired `created_at` reviews
  (`UA_H2_REVIEW_TTL_DAYS`, default 90) and, when `UA_H2_MCP_CONNECTIONS`
  is set, connectors that are not in that inventory. Provenance must be
  `sha256:<hex>`. Same `governed_servers` table as PR #341; not a second
  registry.

- **Horizon 2 live A2A discovery/exchange (UA #666).** `A2AAdapter.discover`
  reads `/.well-known/agent-card.json`; `exchange` sends `GetTask` /
  `SendMessage` and imports the remote task as an observation. Hub
  `task_id` and `authority_revision` stay authoritative.

- **Horizon 2 provider adapters (UA #665).** `adapter_for()` wires Codex,
  Claude, Agy, and LangGraph onto `ProviderExecutionAdapter`. The adapters
  project existing `dispatch_runs` / LangGraph checkpoints into
  `ExecutionEnvelope`; they do not spawn work or mutate tasks.

- **Per-agent delegation ledger in a monthly vault page (UA #703).** After
  every review-pipeline run, `unified-agent-review` appends one line per
  agent per task — `- <ts> | <agent> | #<task> | <verdict> | ci:<state>` —
  to `Logs/Delegation Ledger <YYYY-MM>.md`. The month rolls over
  automatically; missing months are bootstrapped with a minimal header;
  existing pages are append-only and locked pages are never forced. Only
  state changes get a line (idempotent `unchanged` polls stay out); the
  append is best-effort and never moves an exit code.


- **PR-event watcher (task #702, Buzz-emulation #1).** `unified-agent-pr-watch`
  polls `gh` for open-PR state, review annotations, and merges across
  anonb3ll's repos; each meaningful event (opened → reviewed → merged/closed,
  with review rationale when present) becomes a memory note tagged
  `project:<repo>` plus one `vault_journal` pointer line. Idempotent via a
  `$UA_HOME/pr-watch-cursor.json` cursor; `--dry-run` prints without writing.
  First run per repo seeds a silent baseline instead of flooding memory with
  every historical PR. Mac entrypoint: `deploy/mac-hub/run-pr-watch.sh`.

- **GitHub Copilot as a federated developer agent.** Roster labels
  `agent-copilot` / `agent-laptop-agent6`, dispatch route `copilot-with-hub`, and
  executor backend `copilot`. VS Code Copilot Chat and Copilot CLI claim, journal,
  and commit as that identity — not agent-cursor.

- **GCS backup restore rehearsal (task #569).** `stage_backup_snapshot` refuses
  destinations inside `UA_HOME`. `deploy/gcp/restore-backup-to-temp.sh` copies
  the newest offsite snapshot to scratch and verifies it. Live Mac `memory.db`
  is never a drill target; see `deploy/gcp/RESTORE.md`.

- **Tailscale failover rehearsal (task #538).** `assert_private_failover_bind`
  refuses `0.0.0.0` / public binds. `deploy/gcp/failover-rehearsal.sh` serves a
  scratch snapshot on loopback or Tailscale CGNAT only, then tears down. The
  Mac hub stays canonical; see `deploy/gcp/FAILOVER.md`.

- **Zulip live-chat replies (task #523).** With `UA_ZULIP_CHAT=1`, the Zulip
  worker posts bounded LLM replies for `[target: Desktop_*]` messages (and
  single-bot `@mentions` / topic follow-ups) on `requests`/`coord`/`meta`.
  Conversation only — no tool execution or approval bypass. See
  `docs/zulip-coordination.md`.


- **Index-coverage check in the wiki custodian** (`unindexed_pages`,
  `index_present`). Orphan detection asks "does anything link here?", which a
  freshly authored page linked from the journal or one neighbour passes — so a
  page could be absent from the curated `Wiki/Index.md` and never reported.
  That is exactly how two pages written during the 2026-07-27 audit slipped
  through an all-clean report. The custodian still never *writes* `Index.md`;
  it only reports what the index does not cover. An ambiguous index entry
  covers neither candidate, and a missing `Index.md` is reported as unknown
  coverage rather than as clean.

- **Web control center at `/ui`** (M1, read-only). A monitoring surface served by the
  hub itself: agent roster with live status, agent sessions, task queue, approval
  queue, anomalies, and a pushed activity feed. Mounted onto the existing FastMCP
  Starlette app inside `server.wrapped_http_app`, *before* `HubAuthASGIMiddleware`, so
  `/ui` and `/api` inherit the same identity plane as `/mcp`. No new dependencies and
  no build step. `/api` dispatches through the new `server.REGISTERED_TOOLS` map — the
  same `_guarded_and_instrumented` callables MCP clients reach — so UI requests
  traverse the identical policy gate and perf instrumentation. See `docs/web-ui.md`.
- **Agent registry and session tracking** (schema v14, additive). New `agents` and
  `agent_sessions` tables answer "which agents and sessions are alive right now",
  which nothing could previously do: liveness was inferred only from
  `check_anomalies`' `silent_agent` rule at two-day granularity, and Pi runs existed
  solely as unindexed JSONL transcripts. Status is derived at read time from
  `last_seen`, never stored — a stored status is written by the process whose death
  you are trying to detect. Heartbeats come from call sites that already fire (the
  auth middleware's verified identity, `storage.record_agent_event`'s ~18 call sites,
  and the executor's poll loop), throttled per label and unable to raise.
- New MCP tools `agent_heartbeat`, `agent_list`, `session_list`, `session_start`,
  `session_end`, so the registry is reachable from the TUI and scripts, not only the
  browser.
- **The event bus now actually runs.** `EventBusServer` is embedded in the hub process
  (`UA_EVENT_BUS_EMBED=0` to opt out when running `unified-agent-eventbus` standalone).
  Shipped in #157 but never started, so `publish()` had been silently returning `False`
  for every event the cluster ever produced; the durable `event_log` masked it. Hub
  startup is never blocked by a bus that cannot bind.
- `events.latest_ulid()` — `query_events` is oldest-first with a limit and so cannot
  answer "where does the log end?", which a live consumer needs in order to start at
  now rather than replaying history.
- Browser auth: `POST /api/session` trades a bearer token for an HttpOnly,
  `SameSite=Strict` cookie scoped to `/ui` and `/api`. **The cookie never authenticates
  `/mcp`** — tool calls stay bearer-only. `Secure` is set only behind an HTTPS proxy,
  since `tailscale serve` terminates TLS and forwards plaintext to loopback.
- An anonymous-request counter on channel `hub.auth.anonymous`, surfaced in the `/ui`
  footer. Previously the only trace of an unauthenticated request was a stderr warning,
  which made "is anything still calling without a token?" unanswerable and turned
  enabling `UA_ENFORCE_AUTH` into a leap rather than a decision. (On a hub that already
  enforces auth this reads zero and stays there; it is the rollout instrument for hubs
  that do not.)
- Three `system_doctor` checks: `web_ui` (catches a wheel that drops the static
  assets), `event_bus` (LIVE means a connection succeeded, not that a file exists), and
  `agent_label_drift` (labels active recently but absent from `UA_AGENT_LABELS`).


- `scripts/gate_replay.py`: read-only evidence report over `approvals`/
  `agent_perf_samples` for the phase-3 (conditional `fs_write` gating)
  decision. Classifies `run_cmd` history into a three-bucket auto/notify/
  hard-block model (other gated tools get a non-command bucket — their
  payload isn't a shell command); cross-tabs `fs_write` call volume. Never
  writes raw payload text, a hash, or a length — only tool/status/bucket/
  timestamps, after red-team review found hash+length together acted as an
  offline dictionary-attack oracle against the low-entropy JSON payload
  shape. Writes to the vault inbox with a stdout fallback.
- `TODOS.md`: adds the deferred `fs_write` path/worktree-relative logging
  item (the data phase-3's real decision needs but doesn't exist yet).
- Wiki/vault upgrades harvested from Andrej Karpathy's LLM-Wiki pattern and the
  obsidian-llm-wiki plugin (mined for algorithms, not installed):
  - `vault_query(query, path, max_results)`: embedding-free graph retrieval over
    the `[[wikilink]]` graph — lexical seeding followed by Personalized PageRank
    (Haveliwala 2002). No embedding index, no network, no third-party dependency;
    complements `vault_search` (exact regex) and any external semantic search, and
    works on a fresh vault with nothing indexed. `max_results` capped at 50.
  - `vault_ingest_status(path, mark, agent, pages)`: content-hash pre-ingest gate
    for inbox captures, backed by a new `vault_ingest_ledger` table (schema v11).
    Classifies a capture as `new`/`unchanged`/`changed`/`empty` so `steward-inbox`
    skips unchanged captures, re-ingests edited ones, and rejects blank files
    before any synthesis; `mark=True` (requires a verified `agent`) records the
    hash + fed wiki pages after ingest.
  - `vault_write(..., force=False)`: writes now refuse to overwrite or append to a
    note carrying `locked: true` frontmatter unless `force=True`, protecting
    human-curated pages inside the agent-writable `Wiki/` layer.
  - `vault_wiki_custodian` / `Wiki/Health.md`: alias-aware link resolution (an
    `aliases:` frontmatter entry now resolves `[[links]]` and clears orphan
    status), plus four new hygiene sections — duplicate candidates (identity-set
    collision across stem+aliases), missing aliases, empty/stub pages (<80 body
    chars), and stale pages (mtime older than `UA_WIKI_STALE_DAYS`, default 90).

- `system_doctor()`: new `unit_template_drift` check — compares every
  installed systemd user unit against its repo template
  (`deploy/unified-agent.service`, `deploy/systemd/*`) and reports
  `MISCONFIGURED` when a template directive is missing from the installed
  unit. `Environment=` entries compare by variable name only, so legitimate
  local values (a real `UA_HTTP_ALLOWED_HOSTS`, a moved workspace) never
  flag. Motivated by the 2026-07-21 post-merge sweep: the installed
  `unified-agent.service` had silently dropped both Exec hooks shipped in
  the repo template (`ExecStartPre` check-behind warning, `ExecStartPost`
  restart broadcast) because unit install is a manual `cp` — neither hook
  was ever live despite being "shipped" (task #121).

- `system_doctor()`: read-only LIVE/DORMANT/MISCONFIGURED report over every
  config-gated capability — hub auth, the approval queue, which search
  provider actually resolves, per-agent hub workspace bindings, the four
  automation timers this repo installs, the hub service itself, and the
  optional pdf/tree-sitter extras. Each finding carries the exact env var or
  systemd unit that gates it. Motivated by task #108: 5 of 7 features in one
  batch were dormant in production because activation is a config step, not
  a code change — this turns that class of discovery into a one-call check
  instead of an audit-day surprise (System Leverage Plan 2026-07-20, Tier 1).
- `host_low_memory` anomaly rule in `check_anomalies()`: pages when
  `/proc/meminfo`'s `MemAvailable` drops below `UA_ANOMALY_LOW_MEM_PERCENT`
  percent of total (default 15). Motivated by the 2026-07-20 desktop
  freeze — free RAM had a 4-hour visible run-up in the logs before a snapd
  watchdog SIGABRT and a full reboot, and nothing was watching it. Hosts
  without `/proc/meminfo` (non-Linux, no procfs) stay silent, same posture
  as every other probe-based rule.
- `deploy/broadcast-restart.sh`, wired as `ExecStartPost` on
  `unified-agent.service`: writes one vault journal line
  ("hub (re)started — reconnect for a fresh tool snapshot") plus a
  `journalctl`-visible stderr line on every hub (re)start. Every previous
  restart needed a manually-typed "RECONNECT" shout in a broadcast memory
  note because agents kept operating on a stale tool snapshot with nothing
  telling them to reconnect; this makes the signal part of the normal
  boot-sync journal check instead.
- `make smoke` (`scripts/smoke.py`): a live smoke suite of ~9 unmocked checks
  against the real installed venv and real (isolated, throwaway) data paths —
  module imports, the real fastembed model's output dimension, the sqlite-vec
  extension actually loading and answering a KNN query, real tree-sitter
  parsing of a `.ts` arrow function, a real pypdf round trip, search-provider
  resolution, a task claim/update round trip, a vault write/read round trip,
  and a real `security_audit` run. Optional extras that aren't installed
  report `SKIP`, never a silent `PASS`. Wired into `deploy/pull-and-restart.sh`
  right after `pytest`, before the hub restarts. Motivated directly by tasks
  #104/#110: tree-sitter arrow-function indexing shipped broken and stayed
  broken in production for days because its unit tests were fully mocked and
  never actually exercised the real parser.
- `sqlite-vec` accelerated KNN engine for `memory_search`'s semantic ranking
  and `memory_save`'s dedup check (`memory_vec`, a `vec0` virtual table
  mirroring `memory_embeddings` by `memory.rowid` — the same rowid-mirroring
  pattern `memory_fts` already uses via `content_rowid`). Replaces N
  Python-callback round trips (the `cosine_similarity()` SQL function, called
  once per row) with one native scan; `k` is always set to the full embedded
  note count, so results are identical to the exhaustive scan, not an
  approximation. `memory_embeddings` stays the source of truth throughout —
  every write site mirrors into `memory_vec` best-effort
  (`storage.upsert_memory_vec`), and every read falls back to the original
  per-row scan on any failure (extension unavailable on this host/platform,
  or a not-yet-backfilled note), so this degrades exactly like `fastembed`
  itself already does. A pre-existing DB's full embedding backlog is mirrored
  in by `storage._backfill_embeddings`'s new mirror pass on next startup.

### Changed

- **Laptop-lane monitor tuning (UA #761).** `vault_briefing_digest` no longer
  returns `laptop_stale_hours` / `stale` / `escalate` — every machine now
  journals to vault root `Logs/` against the one shared hub, so the old
  `Logs/Laptop/` staleness check reported a dead path as perpetually
  escalated. The `Logs/Laptop/{date}.md` merge stays for historical `date=`
  reads. New `UA_ANOMALY_SILENT_AGENT_IGNORE` (comma-separated, exact match)
  exempts named labels from the `silent_agent` rule — registered agents that
  are healthy but only journal when the operator actively uses them.

- **Operator docs for approved-dispatch Claude isolation (task #623).**
  Enablement, security, CLI, and troubleshooting pages now describe the
  fail-closed sandbox, scrubbed child env, and `dispatch_report_self` path.
  Steward `task_update` claim-ownership (task #591) and LaunchAgent idle-job
  doctor status (task #315) are documented on the same pass.

- **Dispatch and the Active Executor use each agent's native headless CLI
  (task #568).** Cursor and Hermes are dispatchable. Routes spawn `claude -p`,
  `codex exec`, `agy -p`, `agent-with-hub -p`, or `hermes --oneshot` under the
  canonical label — not Pi wearing someone else's name, and not a shared
  `--backend=` alias. `UA_EXECUTOR_BACKEND=pi` remains an explicit opt-in.
  Default dispatch spawn is still the stub; this change is the route and
  executor selection, not unattended production CLI launch.


- `_http_settings()` no longer hardcodes `allowed_origins=["null"]`. That admitted only
  `file://` pages, so any page served by the hub got a 403 from `/mcp` whenever
  `UA_HTTP_ALLOWED_HOSTS` was set — which it is on the installed unit. The new default
  keeps `"null"` (so `deploy/metrics.html` still works) and adds the hub's own origin
  plus each allowed host; override with `UA_HTTP_ALLOWED_ORIGINS`.
- `config.enforce_auth()` replaces an inline `os.environ` read in the middleware, so
  the web UI can report whether the hub is actually protected.
- The executor records a session row around each Pi run and heartbeats on every poll,
  so an idle-but-healthy executor no longer reads as offline.

### Removed

- **Desktop-to-laptop hub federation is retired. Do not build it.** Stages S3
  (read-only federation MVP) and S4 (federated UX) of the #276 roadmap are
  struck, `doc/federation-roadmap.md` is deleted, and the S3/S4 acceptance
  criteria are removed from `docs/baseline-2026-08-07.md` and replaced with a
  retirement note. The laptop reaches the desktop hub directly over Tailscale —
  `tailscale serve` maps the tailnet address to `127.0.0.1:8765` while the hub
  stays bound to loopback — so peer identity, the read-only peer allowlist,
  provenance wrapping and the peer error taxonomy all solved problems created
  by two sovereign hubs. There is one. This design had been filed twice
  (#239–#244, never developer-reviewed, later archived) and built once
  (`ec39c99`, reverted wholesale as `2917964` — 611 lines across
  `tools/peer.py`, `docs/federation.md` and `tests/test_peer.py`), so it is
  recorded here rather than merely deleted: a criteria list left in place reads
  as work to be done, and this would be the third attempt.

  What is *not* retired: the sovereign-machine topology stays intentional. Two
  machines keep separate `memory.db` files and raw cross-device SQLite sharing
  remains prohibited by #276's operating rules. Cross-device state moves
  through the vault, `unified_brain`, or the S2 typed projection contract —
  never by one hub reaching into another's database.

### Fixed

- **LaunchAgent timers false-MISCONFIGURED after a Mac reboot (UA #1178).**
  `system_doctor` reported `steward_audit_cron` and `cluster_briefing_timer`
  "installed but inactive" while both were healthy: launchd resets `runs` and
  `last exit code` when it reloads a job at boot, so a daily timer observed
  between boot and its first post-boot slot reads `runs = 0` — but the
  `launchd_first_seen` anchor still held its pre-boot timestamp and the
  zero-runs logic read that as overdue. `_launchd_zero_runs_status` now clamps
  the anchor to the last boot time (`sysctl kern.boottime`): a zero-runs
  window cannot be older than the boot that reset the counter. Confirmed on
  the live hub — cluster-briefing fired on schedule at 08:00 local the same
  morning it was being reported broken.
- **`notify` entry-point claims now carry their profile gate (UA #1178).**
  The capability catalog and tools reference asserted `notify` as a plain
  `mcp-tool` entry point, but under authenticated HTTP exposure it is denied
  to developer and steward profiles (`DEVELOPER_DENIED_TOOLS`). Both docs now
  name the assumption and point at the read-safe alternative already in the
  tree: the `bridge_disconnect` anomaly rule via `observability_manage`
  (`anomalies`), which preflights send.ts + `bun` without sending.

- **`memory.db` unbounded growth from perf telemetry (UA #919).** The anomaly
  watchdog flagged +134.9 MB / 7 days. Cause: `agent_perf_samples` (one row per
  instrumented tool call, ~89% of the DB at 3.4M rows / ~422 MB with indexes)
  had no retention — poll loops like `task_list` and `delegation_manage`
  dominate it. `backup_runner` now calls `storage.prune_perf_samples()` before
  each daily backup, deleting rows past `UA_PERF_SAMPLE_RETENTION_DAYS`
  (default 14; every reader looks back ≤24h). The redundant `idx_agent_perf_tool`
  index (~80 MB, served no query) is dropped by migration. Freed pages return to
  the OS via `PRAGMA incremental_vacuum` once the DB is `auto_vacuum=INCREMENTAL`
  (one-time hub migration). Feeds the scale data point into #911.

- **`weekly-export` failed on the memory step (UA #910).** `export_memory` /
  `export_tasks` capped every export at `WRITE_MAX_BYTES` (2 MB), including
  the `write_to_vault=True` path — but the memory table is already ~8.7 MB, so
  the weekly disaster-recovery export died with "over the … byte cap". Direct
  vault writes now use a separate `EXPORT_VAULT_MAX_BYTES` (32 MB) ceiling; the
  inline path (rides an MCP response) still uses `WRITE_MAX_BYTES`.
- **Anomaly watchdog cron reported the wrong count (UA #907).**
  `deploy/anomaly-check-cron.sh` read `result['new_anomalies']`, a key
  `check_anomalies_and_notify()` never returns, so it always logged
  "no new anomalies" even when it paged. Now reads `newly_notified` /
  `anomalies`, timestamps its log line, and exits cleanly when the local
  store is retired (`NO_LOCAL_MEMORY_DB`). The Mac LaunchAgent entrypoint's
  stale "approved-dispatch scheduler" header (copied from a dispatch-tick
  template) is corrected, and `ua-anomaly-check.service` gains
  `WorkingDirectory` + `EnvironmentFile`.
- **`dash_view` rejected paths carrying Dash index metadata tags (UA #906).**
  Angle-bracket tokens such as `<dash_entry_name=…>` that some docset
  `searchIndex` rows prepend to `path` are now stripped before resolution.
- **Project evidence artifacts no longer emit unattributed agent rows (UA #849 / PROOF-C2).**
  A `project_id` report excludes records whose agent cannot be resolved and
  lists those historical nullable ids on `unattributed_excluded`. Structural
  `project_chain` still includes them. Agent identity is never invented or
  backfilled from `source_host`. Elicitation challenges fail closed to the
  `/ui` queue when neither `requested_by` nor a verified identity is present.
  `run_cmd` and `gcloud_ops` now stamp `requested_by` from verified identity.
- **Review pipeline false `no git worktree found` FAILED (UA #851).** When a PR
  is merged on GitHub and its worktree cleaned while the hub task is still
  `waiting_review` — the shape Cursor's PR flow produces — reconcile could not
  see the merge if the PR was recorded only on the row's `evidence_ref` column
  and not in the notes prose. The task fell through to the review pass, which
  reported a false FAILED and notified. Reconcile now consults `evidence_ref`
  (PR URL or bare number) as a PR reference of last resort, in both the
  no-local-repo and the resolved-repo branches, and also picks up a markerless
  task sitting in `waiting_review` — marking it `done` only when its PR is
  actually merged, never applying the `stale` / `unresolved-repo` verdicts to
  it. The "no git worktree" race has recurred on this shape since 2026-07.
- **`vault_write` silently overwrote on an unknown keyword (UA #854).** Every
  tool's argument model is now `extra="forbid"`: a keyword a tool does not
  declare fails the call with a validation error instead of being dropped by
  pydantic's default `extra="ignore"`. Previously `vault_write(..., mode="…")`
  lost `mode`, `append` defaulted `False`, and the write clobbered the file
  (2026-09-08: six Wiki pages truncated, recovered from git). `vault_write`
  also gains a first-class `mode` parameter (`"overwrite"` | `"append"`);
  `append=True` stays as the legacy alias; any other `mode` is an error.
- **`vault_wiki_custodian` re-reported the same non-findings every sweep (UA #855).**
  Three false-positive classes eliminated: (1) `parse_markdown_file` now strips
  fenced/inline code before extracting wikilinks, so a page *documenting*
  wikilink syntax no longer counts `` `[[Example]]` `` as a link; (2) an embed
  of a non-note asset (`![[diagram.svg]]`, `![[drawing.excalidraw]]`) resolves
  against every file in the vault by basename, the way Obsidian does, instead
  of being flagged broken because only `.md` stems were indexed; (3) a
  landing/hub page and its deep page that share an alias, link each other, and
  are both in `Index.md` are recognised as a deliberate pair, not a duplicate;
  and the ambiguous-name check ignores the auto-generated `Logs/state_memory.*`
  / `state_tasks.*` mirrors (which echo a stale copy of the report's own tag
  directory) and exempts `[[Index]]`, which already canonicalises to the
  top-level page.


- **Mac Control Center Executor & Delegations pane (task #542).** `system_doctor`
  probed `unified-agent-executor.service` with `systemctl --user`, so the Mac
  hub's LaunchAgents (`com.unified-agent.executor-agy` / `executor-codex`)
  reported DORMANT. Darwin now uses `launchctl print`. Separately, the operator
  login `Desktop_Person` is `UA_AGENT_WORKSPACES` DENY; worktree survey used
  that per-request sandbox and painted the whole pane as failed even when
  `delegate_list` succeeded. Census now uses the hub process workspace, and the
  UI isolates worktree errors from the delegations tables.

- **Cockpit ua-sync stays role-neutral after the 2026-08-18 git init.**
  `is_cockpit_workspace()` no longer treats a real git repo named `agent` as a
  product checkout. `deploy/sync-cron.sh` defaults an undetected client to
  `claude`, so the old check stamped `You are agent-claude` into every
  agent's shared AGENTS.md/CLAUDE.md. Cursor is now a first-class sync client
  (`agent-cursor` / `agent-laptop-agent4`) on the cockpit roster.


- **Derived state mirrors are now host-scoped, so two machines cannot collide on
  one filename.** `Logs/state_memory.md` and its three siblings are *derived* —
  every machine regenerates them from its own `memory.db` — so two hosts syncing
  the same vault handed Obsidian Sync two writers and no merge strategy for
  markdown, and it deposited "(Conflicted copy <host> <time>)" duplicates of the
  largest generated files, polluting `vault_search` with obsolete snapshots.
  Filenames now carry the writing host (`state_memory.DesktopHost.md`),
  derived automatically from the identity already stamped on every write.
  `UA_STATE_SYNC_DIR` already offered a per-device directory and failed for the
  only reason that matters — it is opt-in and nobody set it — so the scoping had
  to be a default, not a setting. The host string reaches a filename and is
  sanitized as untrusted input. `state_sync_verify` prefers this host's mirror
  and falls back to the legacy unscoped name, so an existing vault keeps
  verifying instead of reporting permanent drift and resyncing on every call.
- **Interrupted snapshot writes no longer orphan their temp files forever.**
  `_write_atomic` cleaned up on exception, but nothing survives a SIGKILL
  between `mkstemp` and `os.replace`, so a process killed mid-write left a
  partial file that nothing ever collected — four such artifacts (~595 KB, the
  oldest nine days old) were found in the live vault. Each write now sweeps
  temp siblings of the exact file it is about to write, and only those older
  than an hour: an atomic write takes milliseconds, so anything recent belongs
  to a live process and removing it would break a working writer.

- **`task_update` created a git worktree for any edit that left a task in
  `doing`.** The check asked what the row's status *was*, not what the call
  *changed*, so appending notes to an in-flight task minted
  `.worktrees/task-<id>` and a branch — as did recording a claim for work
  already finished in another checkout. Worse, the branch outlived everything:
  `git worktree remove` does not delete it, and `.worktrees/` is gitignored, so
  `git status` is suppressed by design on exactly this path. Eight such
  branches, seven never committed to, accumulated in one clone in a single day.
  Creation is now keyed on a genuine transition into `doing`, with an explicit
  `worktree=False` for bookkeeping claims and `worktree=True` to materialize
  one on demand. Disposal is symmetric: reaching `done` releases the worktree
  *and* its branch, both gated on the same `_worktree_is_disposable` check used
  by `task_delete` (clean tree plus no content missing from the base branch,
  compared by content rather than ancestry — a squash-merged branch is not an
  ancestor of main and reads as unmerged forever). Anything that cannot be
  established is retained and reported via the new `worktree` key on the
  result, never force-deleted. `task_claim` is deliberately unchanged: the
  executor resolves its sandbox and merge target from the `worktree_path` it
  persists.

- **Wiki custodian audited same-named pages as one, silently.** `run_custodian`
  keyed its page inventory by file stem, so two pages sharing a filename in
  different `Wiki/` subdirectories overwrote each other: whichever `os.walk`
  reached last won, and the shadowed page was invisible to *every* check —
  broken links, orphans, duplicates, missing aliases, stubs, staleness — while
  the report still read all-clean. In the live vault that hid four pages behind
  two (`Antigravity/Overview` ↔ `Claude Code/Overview`, and the matching
  `Subagents` pair); the audited count went 47 → 49 on the fix, immediately
  surfacing a genuine orphan that had never been reportable. The inventory is
  now keyed by vault-relative path. Link resolution stays name-based — that is
  how Obsidian addresses pages — via a separate name → paths map, and a name
  resolving to several pages is reported as a new `ambiguous_names` finding
  rather than silently resolved to one; all candidates are credited with the
  backlink, since guessing would invent an orphan out of whichever page lost.
  Folder-qualified targets (`[[Antigravity/Overview]]`) now resolve exactly,
  and the `Index.md`/`Health.md` exemption matches the vault path rather than
  the basename, so a `Wiki/Section/Index.md` is audited like any other page.
  Findings identify a page by its bare stem when unique and by full path when
  it collides — the form Obsidian itself needs to resolve the link.


- `reviewer.reconcile_tasks()`: a task referencing its PR by bare number
  alone (`"PR #59 open for review"`, no URL) now resolves via
  `gh pr view 59` the same way a full URL already did. Root cause of the
  2026-07-20/21 false-`stale` flag on tasks #115-117: their worktrees were
  already cleaned up post-merge, no `branch <name>` note line existed
  either, and neither PR-URL regex matched a bare number — reconcile had
  nothing left to resolve and flagged them stale even though PR #59 was
  fully merged. See `reviewer._pr_ref_from_notes`.

- `check_anomalies_and_notify()` now journals every newly-notified batch via
  `vault_journal` (agent `unified-agent-cron`, tags `anomaly`/`automated`),
  best-effort — a journal-write failure never masks the `notify()` alert
  itself. Closes the one real gap between this already-shipped cron
  (`ua-anomaly-check.timer`) and the 04:12 design doc's originally-proposed
  "notify-only coordination routine": that routine turned out to be
  redundant with `check_anomalies`'s `stale_doing_task`/`silent_agent` rules
  and this timer, so rather than porting a second parallel detector
  (`coordination.py`, held on `feat/task-62-mesh-coordination`, left
  unmerged), this extends the existing one (task #117).
- `check_agent_capability(task_id, agent_label)`: given a task's
  `[requires: tool1, tool2]` note annotation and a candidate delegation
  target, reports whether that agent's tool allowlist actually covers what
  the task needs. Ported from a held, never-merged branch (`tools/capability.py`
  + tests) with its Hermes fallback list refreshed to match Hermes' current
  `tools.include`; the live config path is read via `UA_HERMES_CONFIG`
  (default `~/.hermes/config.yaml`) rather than hardcoded, for testability
  (task #116).
- `check_approval_gate()` wired into `delegate`, `fs_write`, and
  `git_commit_staged` (previously only `run_cmd` and `workflow_run`'s
  internal steps were gated, so `UA_APPROVAL_TOOLS` didn't actually cover
  what it appeared to). No-op unless a tool is added to `UA_APPROVAL_TOOLS`.
  `fs_write`'s gated payload carries a `content_sha256` + `bytes` instead of
  raw content, to keep the approvals table lightweight for large writes
  (task #115).

- Per-agent workspace binding on the HTTP hub: `UA_AGENT_WORKSPACES`
  (`label:path` pairs, same format/file-support as `UA_AGENT_TOKENS`) lets the
  hub's auth middleware bind a verified agent's `UA_WORKSPACE` for the
  duration of one request via a new `config.workspace_root_var` ContextVar —
  the same request-scoped-override pattern already used for
  `source_host`/`agent_identity`. A satellite project can now use the shared
  hub instead of spawning its own stdio process pinned to a fixed
  `UA_WORKSPACE`; an agent with no entry keeps today's process-wide env-var
  behavior unchanged. The mapping is server-side config keyed off the
  *verified* identity — a client can never claim its own workspace path, so
  the `fs_*`/`run_cmd` sandbox boundary is unaffected (task #112).
- `unified-agent-new-project` CLI: scaffolds a new satellite project's
  `.mcp.json` (project-scoped `UA_WORKSPACE`) and runs `unified-agent-sync`
  in one command, replacing a hand-rolled per-project setup (task #113).
- `security_audit(vault=True)`: audits a note or directory inside the Obsidian
  vault (the same read boundary as `vault_read`, not `UA_AUDIT_ROOTS`) instead
  of a workspace/code path, skipping the repo-hygiene rules that assume a
  code-project tree. `security_audit` and `run_audit` also now accept a
  single file, not just a directory. Together these make the pre-ingest
  scan the `/steward-inbox` skill and `docs/security.md`'s "Quarantine
  Ingest" section already documented actually possible — previously
  `security_audit` could not reach any vault path at all (task #105).
- `hermes_env` anomaly rule in `check_anomalies()`: probes the hermes-agent
  install (env `UA_ANOMALY_HERMES_DIR`, default `~/.hermes/hermes-agent`,
  empty string disables) and flags a missing venv or a venv whose
  site-packages lacks the `mcp` package — the recurring "venv rebuilt without
  the `[mcp]` optional extra" silent fail-open now pages through the existing
  anomaly cron instead of waiting for a human to notice missing tools.
- `fields="compact"` on `task_list`: drops notes bodies from results,
  replacing them with `notes_chars` and `claimed_by` (parsed from the claim
  line), so routine queue syncs no longer pull tens of kilobytes of task
  notes into agent context.
- Keyed `web_search` providers: `brave` (`BRAVE_API_KEY`) and `tavily`
  (`TAVILY_API_KEY`). `UA_SEARCH_PROVIDER` now defaults to `auto`, which
  resolves per call to the first keyed provider whose key is present and
  falls back to keyless DuckDuckGo otherwise — setting an API key is the
  only step needed to upgrade search quality. A keyed provider failing at
  runtime retries once via `ddg` and reports the downgrade in the result
  (`fallback_from` / `fallback_reason`), never silently.
- `review_wait` anomaly rule in `check_anomalies()`: a task annotated
  `[waiting-for: review]` with no `[review: …]` outcome after
  `UA_ANOMALY_REVIEW_WAIT_HOURS` (default `6`) pages through the anomaly
  cron — closes the blind spot where the review pipeline misses a task
  and it sits silently.
- `scripts/hermes-venv-rebuild.sh`: rebuilds the hermes-agent venv with the
  `[mcp]` extra pinned and refuses to declare success until `import mcp`
  works — the fix-at-source companion to the `hermes_env` detection rule,
  living outside the hermes checkout so upstream drift stays clean.


- `claimed-by:` note parsing no longer records placeholder text (e.g.
  `[waiting-for: dev-claim]`) as an agent label; labels must start
  alphanumeric. Stops garbage labels polluting `agent_events` and every
  per-agent anomaly rule.
- `silent_agent` now only holds agents that have journaled at least once to
  the journaling cadence — humans appearing via approval events (and any
  never-journaling label) are no longer flagged forever.

### Security

- **Elicitation approval challenge (UA #832).** Round-two elicitation
  approvals require a server-issued one-time nonce bound to `approval_id`
  (and the MCP session id when present). The nonce is generated with
  `secrets.token_urlsafe`, persisted as a SHA-256 hash in SQLite, echoed
  via MCP `request_state`, and consumed atomically on accept. Fabricated,
  replayed, or cross-row responses leave the approval row pending and do
  not mutate its decision. Decline/cancel do not consume the nonce. Thin
  clients and `/ui` are unchanged. Event log records issuance and
  consumption metadata, never the nonce.


- `resolve_in_workspace()` (`safety.py`) now refuses any path reaching into a
  hook-execution directory — `.git`, `.hg`, `.svn`, or `.husky` — compared
  case-insensitively. Previously, `fs_write` (ungated by default) could plant
  an executable git hook (e.g. `.git/hooks/post-commit`) in the shared
  `UA_WORKSPACE`, letting an ungated write turn into code execution on the
  next commit — a full bypass of the approval-gate system. Found and fixed
  during eng review of the soak-week evidence design; not exhaustive (a
  custom `core.hooksPath` with a different name is still uncovered).

### Documentation

- Operator docs for Control Center Memory / Devices / Evidence panes, local
  fleet restart, prod-action scopes (`merge|delete|restart`), `UA_EDITION`
  enforcement, 1.5 MB vault `.md` mirror cap, and attributed task completion.

## [0.2.0] — 2026-07-19

### Added

- `unified-agent-sync` CLI: detects the running agent's role (location + client),
  gathers the project's open tasks and project-tagged memory references from the
  shared DB, and maintains a marker-scoped `ua-sync` context block in the target
  workspace's `CLAUDE.md` / `.agents/AGENTS.md`. Content outside the markers is
  never touched; memory note bodies are referenced by id, not inlined.
- `service_down` / `service_crash_loop` anomaly rules in `check_anomalies()`: a
  systemd `--user` unit health probe covering `UA_ANOMALY_SERVICES` (default
  `unified-agent,gbrain`), flagging `service_down` when a unit isn't active and
  `service_crash_loop` when its restart count exceeds
  `UA_ANOMALY_SERVICE_RESTARTS_MAX` (default `3`) — catches silent service
  crash loops like the 2026-07-19 gbrain PGLite-lock incident that previously
  went unalerted.
- Systemd user timers (`deploy/systemd/`) replacing the user crontab for all
  six scheduled jobs; daily jobs run with `Persistent=true` so a run missed
  while the machine is off replays at next boot instead of being silently
  dropped.

## [0.1.0] — 2026-07-13

First alpha. One MCP tool server (stdio or streamable HTTP) exposing the same
39 tools to Claude Code and Google Antigravity, with persistent state shared
across hosts and machines.

### Tools

- **Web**: `web_search` (keyless DuckDuckGo, pluggable providers), `web_fetch`
  (readable-text extraction, SSRF-guarded against local/private hosts).
- **Docs**: `dash_list` / `dash_search` / `dash_view` over local Dash/Zeal docsets.
- **Files**: `fs_read` / `fs_write` / `fs_list` / `fs_search`, sandboxed to
  `UA_WORKSPACE`.
- **Symbol index**: `fs_index_symbols` / `fs_search_symbols` (substring or
  semantic) / `fs_list_symbols` — AST-based Python symbol index in the shared DB.
- **Shell**: `run_cmd` (allowlisted programs, no shell, optional Docker/Podman
  container sandbox) and `run_cmd_check` (dry-run vetting with block/warn/info
  findings and a `mutates` verdict).
- **Git**: staged diff/commit (Conventional Commits enforced), worktree
  list/add/remove, and `git_merge_task` — end-to-end validate → audit → test →
  merge → cleanup for task branches, with `dry_run`.
- **Quality**: `python_lint_and_format` (ruff), `security_audit` (rule-based
  AI-dev security checks with optional safe auto-remediation), `skill_review`
  (SKILL.md auto-invocation linter).
- **Memory**: `memory_save` / `memory_search` (hybrid FTS5 + fastembed semantic) /
  `memory_list` / `memory_get` / `memory_delete`, with author-attributed notes,
  cross-author overwrite guard, and auto-maintained per-tag index notes.
- **Tasks**: `task_add` / `task_update` / `task_list` / `task_delete` — durable
  shared queue with priorities, statuses, and one-level subtasks.
- **Vault**: read-anywhere / write-allowlisted Obsidian bridge (`vault_read` /
  `vault_write` / `vault_list` / `vault_search` / `vault_links`), agent-attributed
  daily journal (`vault_journal`), and `vault_wiki_custodian` (wiki health report).

### CLIs

- `unified-agent` — the MCP server (stdio, or HTTP hub mode via `UA_TRANSPORT=http`).
- `unified-agent-audit` — the audit engine for CI / pre-commit gating
  (`--fail-on`, `--exclude`, `--json`; `.ua-audit-ignore` support).
- `unified-agent-tui` — Textual dashboard over memory/tasks.
- `unified-agent-merge` — task-branch merge orchestrator (`--force`, `--dry-run`).
- `unified-agent-wiki` — vault wiki custodian (`--dry-run`).

### Infrastructure

- Single SQLite database (`~/.unified_agent/memory.db`) with FTS5 and background
  embedding backfill; state snapshots mirrored to the vault's `Logs/` for offline
  fallback (`UA_STATE_SYNC`).
- Safety chokepoints in `safety.py`: workspace/vault path resolution, shell
  allowlist, web-fetch SSRF guard, vault write-dir allowlist.
- Hub deployment: systemd user unit template (`deploy/unified-agent.service`),
  DNS-rebinding protection (`UA_HTTP_ALLOWED_HOSTS`), Tailscale-only exposure.
- CI (GitHub Actions) and pre-commit hook running the audit gate, pytest, and ruff.
- Reference documentation under [docs/](docs/README.md): installation,
  configuration, all tools and CLIs, security model, memory/tasks/vault guides,
  development, troubleshooting.
