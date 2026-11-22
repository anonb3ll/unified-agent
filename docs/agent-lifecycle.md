# Agent lifecycle: pause, resume, terminate, retire (tasks #1351, #2305, #2315)

An operator can pause, resume, terminate, retire or unretire any agent from the
Control Center (the agent's page, `/ui/agents/profile/<label>`) or through
`coordination_manage` and the lifecycle route. An
agent is `active`, `paused`, `terminated` or `retired`; an agent never changed
is `active`.

## Control Center confirmation

Open **Agents → Org** and select **Pause** or **Terminate** on the intended
agent's card. The page shows that agent's label and the action's effect.
Enter a nonempty reason and select **Confirm pause** or **Confirm terminate**.
Leading and trailing spaces are removed. **Cancel** or **Escape** dismisses
this form without changing state and returns focus to the action button.
On success, focus stays in the agent's controls while its state refreshes.
**Resume** submits directly.

**Retire** opens the same form with a required reason and an optional owner of
record (an org office or identity label; left empty, the org registry office
stands). A retired agent shows `retired`, offers only **Unretire** (reason
optional, still confirmed), and hides Pause, Resume and Terminate. Each member
shows its owner of record when one is known. The hub authorizes every action:
retire and unretire are operator-only, and a refusal is shown inline on the
agent's controls.

Termination releases all of that agent's active `doing` claims; it is not a
single-task transfer. Check the intended agent and scope before confirming.
The form uses the existing authenticated, CSRF-guarded route and does not
change operator permissions. A rejected request leaves the reason available
and shows the error. Read stop results as reported: `unsupported` or
`requested` does not prove an interactive process has stopped. Verify native
writers separately before another agent edits their files.

## Where the state lives

Each change is an event on `event_log`, channel `agent.lifecycle`, kind
`lifecycle.changed`, carrying the new and previous state, the reason and the
actor. The current state is the newest such event. No table is added. The event
is hash-chained only when the `audit_hash_chain` feature is available
(`UA_EDITION=enterprise`, the default; `events.py` writes no hash under
`UA_EDITION=core`). An operator `prune_events` run deletes old rows and re-roots
the chain, recording an `agent.audit.pruned` event, so a chain head saved before
a prune no longer appears in the log. The stop requests a change produced are recorded as
`lifecycle.stops_requested`; an executor that actually terminates a child
because of a pause records `lifecycle.stop_confirmed`.

## What a pause does

- **Claims are refused.** `task_claim` and `task_update(status="doing")` refuse
  a paused or terminated agent inside their write transaction. Lock renewals
  are refused too, so a running executor cannot keep extending its lease.
- **Node work is refused (task #2315).** Node admission, claim, heartbeat and
  bound-attempt checks refuse an item whose device owner, dispatched target or
  profile identity is paused or terminated, the same sites that refuse a
  retired one. The refusal is a 403 with `code` `agent_paused` or
  `agent_terminated` and is recorded as `lifecycle.refused` with its `state`.
  A heartbeat for a running item that fails the check relays the cancel the
  stop fan-out already requested; it does not issue a second cancel. A node
  whose owner is paused stays registered and re-asks every `CLAIM_HOLD_S`
  (60 s), so it picks work up again within a minute of a resume. A refused
  claim still commits the queue's cancellation and expiry sweep, so the
  stopped owner's queued items reach `cancelled`. Items the pause cancelled
  stay cancelled; resume revives nothing. A mid-turn permission request checks
  the same three identities on every backend and is denied once its item is
  cancel-requested; an approval is consumed only after the item and the
  identities are re-checked in the consuming transaction.
- **Work in flight gets a stop request on every path the hub can reach**, and
  each result says what actually happened:

| Path | What the hub does | `control` |
|---|---|---|
| Dispatch lane A | `coordinator.cancel_run` on each live run | `confirmed`, `unknown` or `requested` (the run's termination outcome) |
| Node dispatch | `node_queue.cancel`; queued items never start, claimed ones are relayed on the node's next heartbeat | `requested` |
| Executor daemon (lane B) | the daemon's kill check reads the agent's state every 15 s and terminates the child | `cooperative` |
| Self-pickup session | nothing: the hub controls no process for an interactive harness | `unsupported` |

The executor treats a pause-triggered stop like a kill switch: the delegation
goes terminal rather than retrying, and needs a human to re-delegate. If the
executor cannot read the state mid-run it keeps the child running; the refused
renewals bound the run by its lease. It never kills work on a read failure.

## Terminate

Terminate does everything pause does, then releases the agent's `doing`
claims: the tasks go back to `open` and their self-pickup runs close
`abandoned` with the reason, so another agent can pick them up. Pause keeps
the claims. A terminated agent must be resumed before it can be paused again.

## Retire

Retirement answers "who owns this credential once the agent is decommissioned
or its owner reorganises?". It is a marker, never a delete: tasks, runs,
memory and events keep their attribution and stay readable.

- **Refused while the agent holds `doing` claims.** Unlike terminate, retire
  never releases claims for you. Release or cancel them first (`task_update`,
  `task_claim_release`, or `agent_terminate`), then retire. The check and the
  write share one `BEGIN IMMEDIATE` transaction, so no claim lands in between.
- **Revocation in the same transaction.** The label's active `agent_tokens`
  rows become `revoked` (event `security.agent_token.revoked`) and its session
  credentials are revoked with reason `agent_retired` (one
  `hub.session_credential.revoked` event each). Its device sign-ins
  (`unified-agent login`: OAuth refresh tokens and the access tokens minted
  from them) are deleted in the same transaction, under the OAuth provider's
  rotation lock, and the provider drops its in-memory copies (one
  `hub.oauth.sessions_revoked` event, reason `agent_retired`; task #2315). A
  managed bearer stops authenticating on the next request. **A bearer listed
  in `UA_AGENT_TOKENS` still authenticates**, because the database cannot
  revoke an env value, but the hub refuses it on every request (below).
  Remove it from `UA_AGENT_TOKENS` and restart the hub; `system_doctor`
  reports it until you do. The managed secret value stays in the store;
  deleting it is the operator's credential step.
- **Refused on every request (task #2315).** The hub's auth middleware refuses
  a retired label's agent credential with HTTP 403
  `{"error": ..., "code": "agent_retired"}`, not the generic 401, and records a
  rate-limited `lifecycle.refused` event (site `hub.auth.bearer`,
  `hub.auth.oauth` or `hub.auth.cookie`). Agent credentials are a bearer, a
  device token, and a web-UI cookie exchanged from an agent bearer; that
  exchange (`POST /api/session`) also refuses a retired label
  (`webui.session_create`). Only verified human sign-ins are exempt, so an
  operator cannot lock out its own recovery path: a WorkOS session, and a
  web-UI cookie for the operator's own label or alias.
- **Fails closed.** The lifecycle state is read at most once per 5 seconds per
  label, and a transition in the hub process clears it at once, so an allowed
  call costs no write. A cached "active" is trusted only for those 5 seconds.
  When the state cannot be read, an agent credential is refused for now with
  HTTP 503 `{"code": "lifecycle_unavailable"}` and `Retry-After: 5`; a cached
  retirement still answers 403 through the outage.
- **One spelling per identity.** The open roster accepts case variants of a
  catalog label (`agent-codex` for `agent-codex`). Auth, lifecycle state,
  the request-path cache and every revocation key on `config.canonical_identity`
  and match case-insensitively, so retiring the catalog label also stops a
  bearer configured under a variant, and a credential stored under a variant
  is revoked with it.
- **No new sign-in.** Device approval, authorization-code consent, the device
  poll and every token issue (code exchange, refresh rotation, client
  credentials) refuse a retired label; an unreadable state refuses too. A
  retire drops the label's outstanding approved device codes and unexchanged
  authorization codes, so an approval made before the retirement yields no
  token, now or after an unretire.
- **Stops.** Retire runs the same stop fan-out as pause (dispatch runs, node
  items, executor attempts), and claims and renewals are refused.
- **Refused everywhere a revoked label is.** Session-credential mint and
  per-request verify (check `agent_retired`); node work on every backend, where
  admission, claim, heartbeat and bound-attempt checks refuse an item whose
  device owner, dispatched target or profile identity is retired, and a node
  whose owner is retired gets 403 `agent_retired` on heartbeat, claim and
  receipt (`node_registry._owned_node`); node permission requests; team boot (`team_spec`); Slack status
  lookups; and the team plan's seat checks. Each refusal by a retired identity is
  recorded as `lifecycle.refused` on `agent.lifecycle` with the site, at most
  once a minute per label and site.
- **`resume` cannot undo it**, and pause and terminate are refused on a
  retired agent. Only an explicit operator `unretire` returns it to `active`.
  Unretire re-issues nothing: revoked tokens stay revoked, deleted device
  sign-ins stay deleted (each device runs `unified-agent login` again), and a
  new token is the operator's step.
- Retiring twice returns `changed: false` and writes nothing.
- **Every transition is judged against the state it follows.** The state
  read, the authorization, the transition check and the event append share one
  `BEGIN IMMEDIATE` transaction, so a pause that started before a retire cannot
  land after it and overwrite the retirement.

### Owner of record

Every agent has one, queryable through `agent_state` and `agent_states`
(`owner_of_record`, `owner_source`):

| `owner_source` | Meaning |
|---|---|
| `explicit` | the `owner` recorded on a retire: an org office (stored by id) or a known identity label |
| `org_registry` | the agent's first office in the org registry (`UA_ORG_REGISTRY_PATH`) |
| `unavailable` | the org registry could not be read |
| `null` | no recorded owner and no office; nobody is guessed |

`agent_states()` returns `agents` (every agent whose state changed, as before)
and `registered` (every label in `UA_AGENT_LABELS`, the org registry, or the
lifecycle log, each with its state and owner of record).

### Doctor

`system_doctor` row `retired_agent_credentials` must read LIVE with zero.
MISCONFIGURED names each retired label that still has an active `agent_tokens`
row, a session credential that is not revoked, a live device sign-in token
(OAuth refresh or access token), or a bearer in the hub's `UA_AGENT_TOKENS`
(labels and counts only, never a value).

### Not covered yet

The request-path cache invalidation and the OAuth provider's in-memory purge
(tokens, device grants, codes) are process-local. The hub runs as one worker
process, so a retire in that process takes effect at once. A deployment with
several hub processes would see another process's retire only after its
5-second cache entry lapses, and that process's in-memory OAuth state would not
be purged: such a deployment needs durable revalidation (for example a
lifecycle revision read on each request, or a shared invalidation channel)
before it can rely on these guarantees.

## Adding and removing an agent

`unified-agent agent add` and `unified-agent agent remove` (task #2453) turn
enrollment and decommissioning into one operator command each, for a local or
a cloud label. Run them on the hub host; full options are in
[cli-reference.md](cli-reference.md#unified-agent).

```bash
unified-agent agent add Local_Worker --office engineering --profile steward --expires-days 90
unified-agent agent list
unified-agent agent remove Local_Worker --reason "replaced by Local_Worker2"
```

**Authority.** Both need a lifecycle operator (`UA_LIFECYCLE_OPERATORS`, default
the operator label and aliases), the same authority retire needs. The command
acts as the configured operator label, or `--as <label>`.

**How the bearer authenticates.** The hub checks a bearer against
`config.agent_tokens()` on every request (`server.py`, `HubAuthASGIMiddleware`).
That map merges `UA_AGENT_TOKENS` with every active, unexpired `agent_tokens`
row whose sealed value is in the managed secret store
(`config.agent_token_entries`), so a managed bearer works from the next
request. Before #2453 the only way to write that store was `agent_token_issue`,
which needs `UA_MANAGED_SECRETS=1`, a mode that blocks unclassified tools
hub-wide; so on the live hub every bearer was issued by hand in
`UA_AGENT_TOKENS` and none had an issue date, issuer or expiry on record.
`add` uses `token_lifecycle.operator_issue`, which may write an `agent-token:`
secret outside that mode for a lifecycle operator only. Env-listed bearers keep
working unchanged; when a label has both, both authenticate until the env entry
is removed (`add` and `remove` say so).

**The seat.** `add` records the label's office, profile and kind as a
`roster.added` event on `event_log` channel `agent.roster`
(`agent_roster.py`), and the hub reads it as an overlay, cached for five
seconds per process:

- a closed `UA_AGENT_LABELS` roster includes seated labels, and an open roster
  accepts a seated `Desktop_`/`Laptop_`/`server_` label outside the catalog;
- the org registry lists the label in its office (the chart file still defines
  the offices), so the doctor's `org_registry` and `agent_label_drift` checks
  stay `LIVE` and the owner of record is the office;
- the seat's profile is the label's default when it is no more privileged than
  the code default (a seat, like a policy overlay, may only keep or lower it);
- `--kind cloud` makes the label cloud-hosted (`config.is_cloud_hosted_identity`):
  no hub workspace, so no `fs_*`, `run_cmd`, `git_*` and no merge or deploy
  path, Control Center routes closed, and the cloud vault guards, exactly as
  for `agent-cloud-agent8` and `agent-cloud-agent17`. The kind is also written on the token row
  (`agent_tokens.agent_kind`) in the insert that makes the bearer active, and
  the cloud check reads that row on every call rather than the cached seat, so
  the boundary holds from the bearer's first request, in every hub process.
  Only checks that fail closed while a seat is unseen (a closed roster, the
  seat's lower profile) ride the five-second cache.

The seat lives in the database rather than in the chart file because the chart
at `UA_ORG_REGISTRY_PATH` is a hand-maintained, often version-controlled file
whose comments and layout a YAML rewrite would lose, and because a database
seat is written next to the token and removed with it.

**The token is shown once.** `add` prints the bearer on stdout and nowhere
else, with the harness's hub entry (`ua mcp-entry`). The database keeps only
its SHA-256 digest and metadata (`security.agent_token.issued`). The hub must
read the value back to check each request, so the value itself sits in the
managed secret store (a mode-0600 file per label under the hub's data
directory), encrypted with `UA_MANAGED_SECRET_KEYS` (Fernet). `add` refuses
when that variable is unset, because the store would then write the bearer
verbatim. `--allow-plaintext-at-rest` overrides the refusal: the bearer is then
protected only by the file's permissions, readable by anything running as the
hub user, and `add` repeats that in its notes. Set the keys and rotate the
token to seal it later.
`add` then waits (120 seconds by default, `--wait`, `--no-wait`) for the first
authenticated request, which the agent registry records as its heartbeat.

**Cloud labels and OAuth.** A cloud bearer suits a client that sends an
`Authorization` header. A harness that connects only through OAuth (a
claude.ai connector) cannot use it; `add --kind cloud` says so, and its OAuth
client is a separate operator step.

**Remove.** `remove` refuses while the agent holds `doing` claims; with
`--force-release` it releases each through `task_claim_release`, the reason
recorded. It then retires the label (above): its managed tokens, session
credentials and device sign-ins are revoked in one transaction, the stored
bearer is deleted and the seat dropped. The next request with the old bearer is
refused. A bearer still in `UA_AGENT_TOKENS` is refused too (403
`agent_retired`) until the operator removes it from the list.

## Who may change what

- An **operator** may change any agent. Operators are `UA_LIFECYCLE_OPERATORS`
  (comma-separated labels), default the configured operator label, name and
  aliases (see [settings.md](settings.md#the-human-operator)). The human
  operator uses the Control Center route; the operator profile does not expose
  `coordination_manage`.
- An **agent** may pause or terminate itself, and may resume only from a pause
  it imposed on itself.
- Nobody else may change another agent's state.
- **Retire and unretire are operator actions only**, including on an agent's
  own label.

Pause, terminate and retire require a reason.

## Fail-closed stop handlers (#1723)

Pause and terminate fan out through two required handlers, registered at
import: `dispatch` (`dispatch.coordinator.lifecycle_stop_runs`) then
`node` (`node_queue.lifecycle_stop_items`). If this process has not
registered both (`agent_lifecycle.REQUIRED_STOP_HANDLERS`), the verb is
**refused before any state write** — nothing is paused or terminated, and
no `lifecycle.changed` event is recorded. The error names the missing
handler(s) and tells you to run the change through the hub (MCP
`coordination_manage` or the Control Center), which loads the routes that
register them. A trimmed custom entrypoint or a test harness that never
imported those modules looks like a broken lifecycle API; it is this
guard.

## Interfaces

- `coordination_manage` actions: `agent_state(agent)`, `agent_states()`,
  `agent_pause(agent, reason)`, `agent_resume(agent, reason="")`,
  `agent_terminate(agent, reason)`, `agent_retire(agent, reason, owner=None)`,
  `agent_unretire(agent, reason="")`.
- `GET /api/agent-lifecycle` lists every agent whose state was ever changed
  (`agents`) and every registered label with its owner of record (`registered`).
- `POST /api/agent-lifecycle` with
  `{"agent", "action": "pause"|"resume"|"terminate"|"retire"|"unretire", "reason", "owner"}`
  (CSRF-guarded; the session identity is the actor; `owner` on retire only).
  The Retire and Unretire buttons on the agent's page
  (`/ui/agents/profile/<label>`) call it (task #2315).
- **One task, not the whole agent (#1700).** `coordination_manage`
  `task_claim_release(task_id, expected_claimant, reason, expected_run_id=None, expected_claimed_at=None)`
  and `POST /api/tasks/{task_id}/claim-release` release a single live claim
  another agent holds. Lifecycle operators and the verified operator only;
  fenced on the claimant, claim and run (the HTTP route requires
  `expected_run_id`, or explicit null with the exact `expected_claimed_at`
  generation for a claim without a run); a `doing` task returns to `open`, a done or cancelled
  task is refused; recorded on `agent.task.claim_released` with the reason.
  Use it instead of `agent_terminate` when only one task needs a new owner.
  Release does not stop a running executor: cancel its run first
  (`dispatch_cancel`, or `task_update(request_cancel=True)` for a self-pickup
  run); a live dispatch run is refused. When `task_update` is approval-gated,
  the human operator's release needs a second approver: a single-human
  deployment cannot approve its own release (separation of duties).

## Session handoff notes (#2397)

Separate from the agent states above, a session's owner can leave a handoff
note for whoever resumes it: what is `done`, the `next_steps` (both required)
and any `open_questions`. It is written with the `coordination_manage` actions
`session_handoff_propose`, `session_handoff_update`, `session_handoff_seal`,
`session_handoff_discard`, `session_handoff_recover` and `session_handoff_ack`
([tools-reference.md](tools-reference.md)). A note persists as a draft that its
author can edit or discard; `seal` is the commit point. Recovery is
forward-only: it returns only the latest sealed, assigned or delivered note, never a
draft, and a sealed note cannot be edited, discarded or returned to draft
(SQLite triggers in `lifecycle/handoff.py` enforce the state machine). A newer
note sealed with `supersede=True` replaces the pending one. Per session there
is one pending note, a 5-minute cooldown between proposals and at most 5
proposals per hour. Each compaction recorded through `lifecycle_record` starts
a new context epoch, and every note records the epoch it was written in. The
session packet carries the sealed note (`handoff_note`), the current
`context_epoch` and `labels.source_tiers`, which marks each section's trust
tier: T0 operator (live approvals, operating profile), T1 hub state (task,
session, events, epochs), T2 agent-written prose (memory, experience, handoff
notes), which is context and never authority. Quota failover of a pending note
to a live peer in the owner's office is built in `lifecycle.handoff`; its
hub-side trigger lands with the node receipt path. Failover records how far the
note got: `assigned` when a receiver was chosen but the transport did not
confirm (the note stays pending and can be failed over again), `delivered` when
the transport accepted it or the receiver called `session_handoff_ack`. The
assignment commits (a compare-and-set) before anything is sent, so two
overlapping failovers never send one note twice, and a send that fails leaves
the note `assigned` and retryable.
