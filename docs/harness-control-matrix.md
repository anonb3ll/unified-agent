# Control Center harness control matrix

Task #1392, verified 2026-09-30; queue and steer added by task #1740
(2026-10-03); resume and adopt added by task #1741 (2026-10-03). This is the
implemented ua-node contract; installed binaries and native extensions alone
do not prove remote control.
Node backends are explicitly opted in through the existing `UA_NODE_BACKENDS`.
The Start a session page (`/ui/sessions/new`) displays only advertised launch options and reports missing
Cursor, Devin and Hermes paths as unavailable. Version metadata comes from each
node's reported facts; absent versions remain unknown. ACP initialization records
actual agent info/capabilities in session-open evidence.

| Path | Start | Prompt | Stream | Resume | Queue | Steer | Cancel | Permissions | Usage |
|---|---|---|---|---|---|---|---|---|---|
| Hermes `acp:hermes`, explicit discovered profile | New ACP session | Initial turn, then follow-ups | ACP updates → hub SSE | As advertised at initialize (see below); its native resume can create a session for a missing id | Next `session/prompt` after the turn ends | Emulated: `session/cancel`, then `session/prompt` | ACP cancel, bounded process teardown | ACP permission → one-shot Approvals | Native updates/result when supplied; missing is unknown |
| Cursor `acp:cursor` | New ACP session | Initial turn, then follow-ups | ACP updates → hub SSE | As advertised at initialize: `session/resume`, else `session/load`, else Start again only | Next `session/prompt` after the turn ends | Emulated: `session/cancel`, then `session/prompt` | ACP cancel, bounded process teardown | ACP permission → one-shot Approvals | Native updates/result when supplied; missing is unknown |
| Claude `acp:claude` (`claude-agent-acp`) | New ACP session | Initial turn, then follow-ups | ACP updates → hub SSE | As advertised at initialize: `session/resume`, else `session/load`, else Start again only | Next `session/prompt` after the turn ends | Emulated by contract; native `_session/steering` injection when the agent advertises it at initialize | ACP cancel, bounded process teardown | ACP permission → one-shot Approvals | Native updates/result when supplied; missing is unknown |
| Other `acp:<harness>` (gemini, codex, copilot, opencode) | New ACP session | Initial turn, then follow-ups | ACP updates → hub SSE | As advertised at initialize: `session/resume`, else `session/load`, else Start again only | Next `session/prompt` after the turn ends | Emulated: `session/cancel`, then `session/prompt` | ACP cancel, bounded process teardown | ACP permission → one-shot Approvals | Native updates/result when supplied; missing is unknown |
| Cursor `cursor` executor | New CLI run | Initial print prompt | Final receipt only | Unsupported | Unsupported | Unsupported | Process termination | Launch approval only; no tool-prompt relay | Unsupported by node contract |
| Devin `devin` executor | New CLI run | Initial print prompt | Final receipt only | Unsupported | Unsupported | Unsupported | Process termination | Launch approval only; no tool-prompt relay | Unsupported by node contract |
| Claude `claude` executor | New CLI run | Initial print prompt | Final receipt only | Adopted sessions: `claude -p --resume <id>`; hub-started runs record no id | Unsupported | Unsupported | Process termination | Launch approval only; no tool-prompt relay | Unsupported by node contract |
| Codex `codex` executor | New CLI run | Initial exec prompt | Final receipt only | Adopted sessions: `codex exec resume <id>`; hub-started runs record no id | Unsupported | Unsupported | Process termination | Launch approval only; no tool-prompt relay | Unsupported by node contract |
| Hermes `hermes` executor | New oneshot run; no profile picker | Initial prompt | Final receipt only | Unsupported | Unsupported | Unsupported | Process termination | Launch approval only; no tool-prompt relay | Unsupported by node contract |

Queue and steer need a ua-node that reports `node_features: ["acp_followups"]`
in its registered facts; for an older node both rows read `unavailable` and the
hub refuses follow-ups. `acp_matrix.control_capabilities` serves these rows to
the session pages (`queue`: `supported`; `steer`: `emulated`).

“Start again” is a fresh dispatch with the same stored prompt/profile, not resumed
history. “Resume” (task #1741, below) reopens the same harness session and is
offered only where the agent or CLI proved it can. Reconnect reads persisted
queue status and bounded event history. A node reconnect does not re-claim a
claimed item; expired unconfirmed outcomes require reconciliation before
restart or resume. The native capabilities are not flattened into a uniform
promise.

## Resume and adopt (task #1741, verified 2026-10-03)

**Capability detection.** This client speaks ACP v1. At `initialize` the
node reads `agentCapabilities.sessionCapabilities.resume` (an object:
`session/resume`, stabilized in v1 in April 2026, reconnects without a
replay) and `agentCapabilities.loadSession` (`true`: `session/load` reopens
the session and replays its history as `session/update` notifications). Resume
wins when both are advertised; the replay of a load is counted
(`replayed_updates`) and not re-streamed, because the hub already holds that
transcript. `session_open` records `resume_capability`: `resume`, `load` or
`none`.

**Persisted binding.** The hub mirrors each ACP session into `agent_sessions`
keyed by the ACP session id (`external_session_id`, since task #1328) and now
keeps the resume binding in `meta`: `node`, `backend`, `cwd` and
`resume_capability`. The binding survives the process exiting, the idle
reaper, a node restart and the registry's 30-minute abandoned-session reaper.
The `resume` row of `control_capabilities` reports the newest recorded value
per node and backend: `supported` (`resume` or `load`), `unsupported`
(`none`: Start again only), `unknown` (nothing recorded yet) or `unavailable`
(the backend is not advertised, or the ua-node predates resume).

**Resume.** `POST /api/action/node-resume` `{item_id, prompt}` queues a new
item bound to the earlier item's session id (`node_dispatch_resume` table),
through the same `node_dispatch` approval gate as Start again; the approval
payload names `resume_session_id`. It is refused (`409`) while the item is
live (send a follow-up), after an unconfirmed lease expiry, when no session id
was recorded, when the agent's capability is `none` or unrecorded, when the
node does not report `node_features: ["session_resume"]`, and while another
resume of the same session is queued or running (checked inside the enqueue
transaction, so two approved resumes cannot both queue). Only the identity the
session is attributed to, the identity that started the hub item, or the
human operator on a recovery channel may resume it (`403` otherwise); an
adopted session stays with the identity that adopted it. A denied approval
answers `403`. On the node the ACP backend
calls `session/resume` or `session/load`, never `session/new`; an agent that
advertises neither is answered `resume_unsupported`, never with a fresh
session. When the new item's `session_open` names the bound session, the hub
reopens the durable row and rebinds it to the new item (`resumed_items`); a
node cannot reopen a row its item was not bound to.

**Native discovery.** Each ua-node lists Claude Code and Codex sessions
started outside the hub, metadata only: harness, session id, cwd, last
activity and a title only when the user set one (Claude `custom-title`
records). Claude `summary` records and Codex thread names may be derived from
the conversation, so they are never reported. Stores:
`$CLAUDE_CONFIG_DIR/projects` (default `~/.claude/projects`,
`<encoded cwd>/<session id>.jsonl`) and `$CODEX_HOME/sessions` (default
`~/.codex/sessions`, `YYYY/MM/DD/rollout-<ts>-<id>.jsonl`, first record
`session_meta`). The node reads a bounded head of each file (60 lines,
128 KiB) to find the session id and cwd and drops everything else; no
transcript content is reported. A session is listed only when its cwd, fully
resolved (symlinks followed, `..` collapsed), is inside a
`UA_NODE_WORKTREE_ROOTS` root by path components; a session file symlinked out
of its store is skipped. The node reports each cwd as recorded and resolved
(`cwd_realpath`), and each root with its `realpath`. The hub cannot resolve a
node's paths, so it compares only those node-resolved forms: a root or session
without one admits nothing. It keeps only typed, bounded fields (50 rows).
Discovery runs at registration and then every `UA_NODE_DISCOVERY_S`
(default 300; `0` turns it off) on the heartbeat; the interval starts only after
a scan reaches the hub.

**Adopt.** `POST /api/action/node-adopt`
`{node, harness, external_session_id, prompt, backend?}` accepts only a
session that node reported. It creates an `agent_sessions` row of kind
`claude_native` or `codex_native`, idempotent on harness and session id and
attributed to the request's verified identity, and queues a resume through
the approval gate. A session already attributed to another label is refused
(`403`), as is one whose task another label has claimed: adopt never moves a
session to a different identity. Adoption is keyed on node, harness and
session id: a row bound to another node is refused (`409`), never rebound. A
repeated adopt while its item is open
returns that item (`200`, `duplicate: true`); a later adopt resumes the same
row. The backend defaults to the native CLI when the node advertises it
(`claude -p --output-format stream-json --verbose --resume <id> -- <prompt>`;
`codex exec --json ... resume <id> -- <prompt>`, flags verified against Claude
Code 2.1.287 and codex-cli 0.159.0 `--help`), else `acp:<harness>`. Adopt over
`acp:<harness>` is offered only once an ACP session of that backend on that
node recorded `resume` or `load`; otherwise the listing's `backend_note` says
why. The receipt of a native resume ends the adopted row and keeps its
binding; an adopt item that ends without running (cancelled, denied, lease
expired) closes the row from the same end-of-item sweep as follow-ups.
Nothing is typed into a running terminal: no PTY or tmux injection. Adopt and
resume never add a second writer to a live session: they are refused (`409`)
while the session's row is open, while a queued or claimed node item ran
under that session id, and adopt refuses a hub-started ACP session (use
Resume on its item) and a discovered session active within
`UA_NODE_ADOPT_QUIET_S` (default 120 s), which may still be open in a
terminal. The hub judges that from the last scan (each row carries
`scanned_at`), so the node re-checks the session file just before it spawns
an adopt, or a resume of an adopted session, and refuses with
`session_recently_active` instead of starting a second writer. A write the
node's own last run left (the file's mtime and size are unchanged since that
run; recorded in `node-sessions/native-fingerprints.json`) does not count, so
a Resume straight after an adopted turn runs. Only a run that finished is
recorded, but the guard cannot tell writers apart: a terminal writing to the
session while a node run is finishing successfully is recorded as that run's
own write. A missing session file is
refused as `session_not_found` even with `UA_NODE_ADOPT_QUIET_S=0`, which
turns off only the quiet window. A claimed ACP item records its session id at
`session_open`, so a session still running on the node is visible before its
receipt. The adopted row is reopened and rebound to its new item in one
transaction.

**Idle reaper.** After the last turn an ACP session stays up for
`UA_NODE_ACP_IDLE_S` seconds (default 60) so a queued follow-up can still join
it live (`session_idle`), but only for interactive work (the hub sets
`hold_for_followups` for an item a human started outside the scheduler) or a
session that already took a follow-up; a scheduler run ends at once. Past
that the node stops the process
(`session_idle_reaped`, `session_end.end_reason: idle_reaped`); the session row
closes with `exit_status: idle_reaped` and stays resumable. The node runs one
item at a time, so the window also delays its next claim.

**Not verified live.** No live harness was resumed or adopted for this
change; the paths are covered by a fake ACP agent and fake session stores in
`tests/test_session_resume.py`. Which installed ACP agents advertise
`loadSession` or `sessionCapabilities.resume` is learned from their first
`initialize` on each node, not assumed. Hermes' native resume can create a
session for a missing id, so a Hermes resume of a deleted session may start
empty; that is the agent's behaviour, not detected here.

## Follow-ups: queue and steer (task #1740, verified 2026-10-03)

The operator sends a follow-up from the session's page (`/ui/sessions/<id>`)
(`POST /api/action/node-followup`, `mode` = `queue` or `steer`, one
`idempotency_key` per message). The hub stores it (`node_followups`), the
node pulls it from `POST /api/node/followups` while a turn runs and once after
each turn, and reports what it did on the event stream.

- **Queue.** After a turn ends with any stop reason except `cancelled` or
  `refusal`, the node sends the oldest queued follow-up as the next
  `session/prompt` in the same ACP session. One follow-up per turn, in hub
  order (`seq`). Path `prompt`.
- **Steer, emulated (default).** ACP v1 has no mid-turn input. The node sends
  `session/cancel`; when the prompt returns `stopReason: cancelled` it records
  `turn_aborted` (`reason: steer`) and sends every pending steer message,
  joined, as the next `session/prompt`. Path `cancel_reprompt`. A steer that
  arrives between turns goes out as the next prompt ahead of queued
  follow-ups (path `prompt`).
- **Steer, native.** Only when `initialize` returns
  `_meta.steering.supported: true`. The node sends `_session/steering` with
  `_meta.steering.idleBehavior: "promptRequired"`; `injected` means the
  message joined the running turn (path `native_steer`, no new turn);
  `promptRequired` means the turn had settled and the message goes out as the
  next prompt, together with every later steer and in order (a later steer is
  never injected ahead of a held one). Any error falls back to the emulated
  path (`steer_fallback` event). `session_open` records
  `steering: native|emulated`.
- **Approval: operator versus agent (task #1872).** Which credential sent
  the follow-up decides whether it needs its own approval.
  - *Operator-authenticated.* A follow-up sent with the operator's credential
    is queued at once (`201`): operator decision 1B lets that identity skip
    the second approval. "Operator-authenticated" is the route-admission rule
    of `_operator_recovery`: the identity the hub's auth middleware resolved
    from the credential is `UA_OPERATOR_LABEL`, and it arrived on a bearer,
    OAuth, cookie or WorkOS channel. It is bound to the identity, never to the
    profile, and never read from the request body or headers. It proves which
    credential sent the request, not that a person typed the text. The row
    and its `followup_queued` event record `operator_authored: true`,
    `approval: "not_required_operator"`, with that same meaning. Only the
    approval is skipped: the gate's identity half
    (`safety.check_prod_action_authority`: revocation, then `UA_AGENT_SCOPES`
    for `dispatch`) still runs, so a revoked or unscoped operator gets `403`.
  - *Any other identity.* An agent's follow-up is new prompt text for the
    device, so it passes the same `node_dispatch` approval gate as the
    dispatch, on a payload naming the item, mode, idempotency key and full
    text: the dispatch approval does not cover it. The route answers
    `202 approval_pending` until a human approves; the same request re-sent
    afterwards queues it (`201`, `operator_authored: false`,
    `approval: "gate_passed"`). An agent's steer therefore takes effect once
    approved, not at the request. This holds for an agent whose session runs
    the operator profile, for the operator label on any other auth channel,
    and for a `UA_OPERATOR_LABEL` that names an agent (the hub ignores such a
    label).
  - *Retries and keys.* A retry of an already-queued key needs no new
    approval, but revocation and scope are checked before the key is looked
    up, on both paths, so a revoked or de-scoped sender's retry is `403`,
    never a `200` duplicate. A key belongs to its first sender: another
    identity that sends a key already queued, or waiting on an approval, for
    someone else on the item gets `409`, so one sender can never read back or
    strand another's.
  - A static bearer token mapped to the operator label is the operator's
    credential: whoever holds it can also sign in to the Control Center with
    it, so the hub cannot and does not tell a script holding it from the
    operator. Keep that token with the operator; give scripts their own
    labels.
- **Delivery is reported truthfully.** `followup_delivered` is emitted once
  the `session/prompt` (or `_session/steering`) is written to the agent; a
  write failure reports the follow-up dropped instead.
- **Lost replies lose nothing.** The node poll is two-phase: the hub hands a
  follow-up out until the node acknowledges it (`ack` on the next poll), and
  the node dedupes by follow-up id. The reply is bounded node-side (20
  entries, 20 000-character bodies, 4 MiB per hub reply).
- **Kill wins.** A hub cancel during a turn, or seen between turns, ends the
  session before another follow-up turn starts; pending follow-ups are
  reported `followup_dropped` (`reason: cancelled`). Whenever an item ends by
  any path (receipt, cancel before start, governed cancel, lease expiry,
  policy denial), its undelivered follow-ups are dropped with a
  `followup_dropped` event (`item_cancelled` or
  `session_ended_before_delivery`; `delivery_unconfirmed` for one the node had
  acknowledged and may have sent).
- **No new authority.** A follow-up runs in the same session, under the same
  permission relay as the first turn. The route requires the `session_list`
  and `crew_view` profile ceiling on top of identity and CSRF, and attribution:
  only the identity that started the item, the agent the session runs as, or
  the verified human operator on a recovery channel may send one (`403`
  otherwise, and `403` when another label holds the item's task claim). An
  approval consumed just before the item ends can still answer `409`.

Turn lifecycle events keep the raw ACP kinds and add `turn_start`
(`turn_id`, `index`, `source`, `followup_ids`), `turn_end` (`turn_id`,
`stop_reason`, `usage`), `turn_aborted` (`turn_id`, `reason`, `stop_reason`)
and `session_end` (`stop_reason`, `turns`). Every event carries `turn_id`
(null outside a turn). The mirrored `agent_sessions` row closes on
`session_end`; a `turn_end` without a turn id, from a node that predates
these events, still closes it.

Native steering evidence: the npm package
`@agentclientprotocol/claude-agent-acp`, versions 0.79.0, 0.81.1 and 0.81.2
(`dist/acp-agent.js`), advertises `_meta.steering.supported: true` in its
initialize response and implements `_session/steering`: it injects the message
into the in-flight turn (`injected`), and with `idleBehavior: promptRequired`
returns `promptRequired` when no turn is running instead of starting a
detached one. Its `session/prompt` also queues behind a running turn rather
than steering it. No other harness in the matrix was checked for the
extension, so each is treated as emulated unless its initialize response
advertises it. No live harness was driven for this change: the behaviour is
covered by a fake ACP agent in `tests/test_acp_followups.py`.

## Source and version evidence

- Installed Hermes source: `NousResearch/hermes-agent` commit
  `4cb28de2cd14fa8ee24f0f3e8fbff461f108a9a2`. The task's original v0.21.5 is
  not used as a verified runtime version. `hermes_cli/main.py` selects `-p`
  before imports; `hermes_cli/profiles.py:resolve_profile_env` and
  `hermes_constants.py` enforce live identity markers/tombstones and id grammar.
  `acp_adapter/server.py` implements initialize/new/prompt/update/cancel,
  permission requests, usage, and native load/resume. Native resume can create a
  session for a missing id, so strict history resume is not claimed here.
- Installed Cursor binary resolves to
  `~/.local/share/cursor-agent/versions/2026.09.28-64d2043/cursor-agent`.
  The existing ACP matrix uses `agent acp`; the CLI executor uses the existing
  `agent-with-hub -p --output-format stream-json` path. ACP binary presence is
  discovery, not a native protocol probe. The actual initialized source/version
  is retained in ACP session events when a run is authorized.
- Installed Devin binary resolves to
  `~/.local/share/devin/cli/_versions/3000.11.3/bin/devin`.
  `executor/backends.py:DevinBackend` and `dispatch/routing.py:headless_argv`
  implement `devin-with-hub --permission-mode <existing mode> -p <prompt>`.
  There is no Devin ACP entry and no claimed hosted-Devin API control here.
- UA sources: `acp_matrix.py`, `acp_backend.py`, `acp_client.py`,
  `node_agent.py`, `node_queue.py`, `node_followups.py`, `node_resume.py`,
  `native_sessions.py`, `executor/backends.py`,
  `dispatch/routing.py`, and the Sessions launcher/transcript. No native config,
  credential, permission mode, gateway or live peer process changed.

## Profile boundaries and acceptance

Every Hermes ACP launch requires an explicit profile, including `default`, so
Hermes' sticky `active_profile` cannot retarget it. Name-only discovery reads
file metadata, never profile config/secret contents; missing, tombstoned,
markerless and symlinked named profile directories cannot launch. The hub accepts
only a discovered profile with an existing canonical surface mapping, checks the
device owner and profile agent against native policy, and stores those identities
separately. Mapping drift fails at approval/enqueue/claim/restart/permission
boundaries; a device remains authenticated as itself. No profile is created,
reconfigured or impersonated.

Synthetic acceptance covers both authenticated HTTP launch paths, exact-payload
approval separation between profiles, profile argv, streamed text/tool updates,
least-privilege permission answers, usage, cancellation, additive migration,
restart, node re-registration, and selection disappearance during UI polling.
Existing node/ACP tests cover durable receipt spooling, event history/replay,
lease expiry and process failures. Live acceptance for each host is separate:
after its operator enables the already-installed ACP backend, select a mapped
profile, run a harmless prompt, observe its stream/status, deny/approve a native
permission prompt, cancel, reconnect, and start again. Such acceptance must name
the observed node version, profile mapping and initialized harness version.
No live peer profile was started in this implementation.

## Session models and permission modes (task #1742)

Every ACP launch defaults to `supervised` unless an explicit per-session choice
requests another reviewed mode. Older queued items with no mode field also
request supervised. Permission defaults never derive from autonomy or yolo settings.
A node must advertise `node_features: ["session_settings"]` both when work is
queued and when claimed. A downgraded node fails the queued item before work.
Native CLI paths have no equivalent setting negotiation in this contract.

The v1 client reads `modes.availableModes/currentModeId` and legacy
`models.availableModels/currentModelId` from new/load/resume responses. Where a
v1 `configOptions` select advertises category `mode` or `model`, its listed
values and `session/set_config_option` take precedence. Before the first prompt,
it calls reviewed permission setters and requested model setters, records discovery,
and records `session_open` only after confirmation or the explicit native-default
compatibility path described below. Unsupported explicit choices or clamped
negotiated values produce `settings_refused` and no prompt. V2 remains disabled.

ACP mode IDs are arbitrary, as described in the [v1 modes contract](https://agentclientprotocol.com/protocol/v1/session-modes).
Display labels, `ask`, `code`, `agent`, and even a provider's literal `supervised`
ID cannot establish authority. The reviewed mappings below were verified against
primary adapter sources on 2026-10-07. They apply only to the named adapter;
installed versions and advertised values must still match.

| Adapter | Supervised | Auto accept edits | Full access | Primary semantics |
|---|---|---|---|---|
| Claude | `default` | `acceptEdits` | `bypassPermissions` | [session-mode.ts](https://github.com/agentclientprotocol/claude-agent-acp/blob/main/src/session-mode.ts): manual changes approval, edits approval bypass, all permission bypass |
| Codex | `read-only` | `workspace-write` | `agent-full-access` | [AgentMode.ts](https://github.com/agentclientprotocol/codex-acp/blob/main/src/AgentMode.ts): read-only sandbox with on-request approval; workspace writes with boundary/network approval; unrestricted sandbox with never approval |
| Gemini | `default` | `autoEdit` | `yolo` | [policy engine](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/policy-engine.md): confirmation, edit allow rules, auto allow |

Codex read-only is a stricter compatible supervised regime: the UI displays the
raw native mode separately and does not promise that reviewed writes will work
inside its sandbox. Codex `agent` auto-review is deliberately unclassified.
Hermes modes `default`, `accept_edits`, and `dont_ask` are explicitly edit-approval
policies in its `acp_adapter/server.py`, not evidence of whole-session authority;
Hermes, Cursor, Copilot and OpenCode accept only a supervised **request** through
a compatibility path that preserves their native default and sends no unknown
mode setter. It records `mode_control: native_default_unclassified`, immutable
`baseline_mode_id` (possibly unreported), and `permission_mode: null`. This does
not establish native supervision or add native authority. Their existing permission
requests still use human approval; an adapter that bypasses requests cannot be made
supervised by this relay. Auto accept edits and full access remain refused.

The matrix's `modes` row marks this path `unclassified`, including before a catalog
has been observed. Reviewed Claude/Codex/Gemini adapters never take this fallback:
missing or clamped mapped modes still refuse. Other capability states distinguish
unavailable nodes, unknown advertisements, advertised setters, and unsupported
settings. Native mode drift or malformed mode reports stop the compatibility
session, including during model setup and idle; model-only updates can retain the
unchanged unknown baseline. No provider label is a security guarantee.

The launcher offers advertised model IDs and reviewed permission classes, or only
the supervised request for an unclassified native default. It displays observation
time and revalidates on every launch. Cached choices are advisory; the newest
eligible catalog is selected independently per node/backend/Hermes profile, so a
busy device cannot evict another device's last observation.

Model defaults resolve the existing task/run/agent operating profile chain. Model
selection uses an exact advertised ID, or a unique catalog alias that differs only
by a recognized single provider prefix (`anthropic/`, `openai/`, `google/`, etc.).
It does not guess shortened product names, versions, display names or substrings.
An unsupported profile preference preserves the native model and reports
`model_resolution: profile_default_unavailable`; explicit model choices refuse
when unresolved. Requested text remains in the exact approval and evidence;
`current_model_id` remains the actual native ID. Other resolution values are
`exact`, `catalog_alias`, and `native_default`.

Resume freezes the recorded effective native model (otherwise the original request
or native default), with strict exact-ID confirmation and no changed-profile lookup.
Queue provenance records `resumed_session` and preserves the model's original
profile/explicit/native-default origin. Start again always defaults supervised;
implicit profile/native defaults resolve again, including after Resume, while a
stored explicit model selection is retained. Full access requires a new choice.
`supervised_default` identifies the canonical default posture; it does not assert
whether the operator clicked that value. Broader choices use `explicit_session`.
Requested and effective values remain separate in evidence and cards.

Full access requires explicit per-session selection and the existing approval
of the exact dispatch payload, including model and permission mode. Any ACP
permission request still uses the existing human approval relay, regardless of
mode. Unexpected broader/unclassified changes on a mapped session, or mode drift
from an unclassified baseline, mark the session refused, cancel and terminate the peer, and invalidate pending permission replies.
Foreign-session updates/permission requests cannot change effective state or
receive approval. This detects a reported authority change; it cannot undo an
action already performed by a dishonest adapter before reporting. Adapter
implementations are the native enforcement trust boundary, not ACP names. Direct
internal backend callers do not replace the hub's dispatch approval boundary.

## Per-turn workspace checkpoints (task #1743)

For an ACP session whose working directory is inside a git repository, the
node snapshots the working tree at the start and end of every turn into hidden
refs, `refs/ua/checkpoints/<session>/<turn>/start` and `.../end`. A snapshot
uses a private temporary index (`GIT_INDEX_FILE`) and `commit-tree`: the
user's HEAD, branches, index and working tree are never touched, and files
matched by `.gitignore` are not captured.

After `turn_end` or `turn_aborted` the node emits a `turn_checkpoint` event:
the changed files (at most 100, with `files_total`) and a unified diff capped
at 16 KiB. Credential-shaped paths are omitted from the diff and marked
`withheld` on the file list. A file whose old or new blob holds a private-key
`BEGIN` marker is withheld as a whole (task #1990): the hub sees
`[content withheld: private key]` instead of the surrounding key body, even
when the hunk itself never included the `BEGIN` line (for example one line
edited in a TLS key embedded in `config.yaml`). Remaining text is then
redacted for credential shapes. The snapshot runs after the turn's end event,
so it never lengthens the recorded turn. A failed snapshot is reported as
`turn_checkpoint` with an `error` and never stops the session. On an aborted
turn the agent's process group is stopped first; if it cannot be confirmed
stopped, the end snapshot is skipped and reported as `turn_checkpoint` with
`skipped: agent_not_confirmed_stopped`. The session's page shows each as a
**changes · turn-N** entry with the file list and a collapsible diff. The hub
stores only that bounded summary: the node is
outbound-only, so there is no hub-to-node diff fetch, and full file contents
never leave the node.

Refs of the newest 20 sessions per repository are kept; older sessions' refs
are deleted when a session ends, and git's normal garbage collection reclaims
their objects. Revert is not offered: no backend here has verified
conversation rollback, and restoring files without it would desynchronize the
agent. Set `UA_TURN_CHECKPOINTS=0` on a node to turn checkpoints off.

## Team snapshot and restore (task #1738)

After a reboot, a deploy or the end of the day, a whole team is restored in one
action. **Sessions → Team snapshot and restore** (or `coordination_manage`
`team_snapshot` / `team_snapshots` / `team_restore_preview` / `team_restore`;
HTTP `GET|POST /api/teams/snapshots`, `GET /api/teams/snapshots/{id}/preview`,
`POST /api/teams/snapshots/{id}/restore`).

A snapshot records, per seat (a committed team's seats resolved to each seat's
latest launched node item, or an explicit list of node items): label, node,
backend, model, working directory, task, node item, harness session id, the
session's resume capability and the task's bounded session packet (8 KB,
credential shapes redacted). It is one append-only `agent.team.snapshot`
event; no transcript is copied.

The preview is read-only and gives each seat a planned outcome: **resumed**
(the session can be reopened with ACP `session/resume`/`session/load` or the
harness `--resume` flag), **rebuilt** (it never could, but a packet exists: a
fresh session starts with the packet as its prompt), **fresh** (neither),
**failed** (offline node, a task claim another agent holds, a seat that belongs
to another identity, no recorded session, or a session that was resumable when
the snapshot was taken and is not now) or **live** (the session, or a resume of
it, is still queued or running: nothing to do). Only a seat that was never
resumable may be rebuilt or started fresh.

Unless the caller is the verified human operator on the HTTP recovery channel,
the caller must be the seat's owner (the identity that started the node item,
or the harness session's attributed identity) or the task's current claimant,
and a task claimed by anyone else, live or expired, fails the seat. Resuming a
session reopens it as its owner, so for a claimant who does not own the session
a resumable seat fails (it never falls back to a fresh start); a seat that was
never resumable may still be rebuilt or started fresh. The preview
applies the same checks, so it plans what restore will do; operator standing is
never accepted as an MCP parameter.

Restore carries the plan out through the existing resume and approval-gated
node dispatch paths, so admission, approvals and claim rules still apply. A
seat waiting for its approval reports **awaiting_approval** with the approval
id and is counted separately, not as restored. A planned resume that fails is
reported as **failed**, never silently started fresh. Seats are always restored
on the node they were recorded on. Each route carries its `coordination_manage`
action twin plus `session_list`; restore also needs dispatch authority
(`delegation_manage` `delegate`, as for a ua-node session) and `crew_view`, or
the operator.
