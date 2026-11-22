# Autonomy modes contract (task #1457)

Design, then one vertical slice: `src/unified_agent/autonomy_modes.py`, wired
onto `goal_manage` (a **mission** is a goal id — `goals.py`, mission #1450's
why-chain). Tests: `tests/test_autonomy_modes.py`.

This contract defines what each mode means, who may change it, and the
invariants that hold regardless of mode. Modes add mission-scoped pauses on
top of existing gates; they never weaken those gates.

## Relationship to what already exists

Autonomy modes do **not** replace or duplicate:

- **#1351 agent lifecycle** (`agent_lifecycle.py`) — pause/resume/terminate an
  *agent*. That is per-agent and enforced by `task_claim`/`task_update`
  independent of any mission. A paused or terminated actor is denied here in
  every mode (see Invariants).
- **#1348 budgets** (`budgets.py`) — per-agent spend ceilings, read from
  `policy.yaml`. A `block`-action budget that is exhausted is a hard stop
  here in every mode too.
- **The global approval gate** (`tools/approvals.py`, task #62 · C) — the
  hub-wide `UA_APPROVAL_TOOLS` allowlist and its pending/approved/denied
  queue. Modes never bypass a denial, and a mode can never manufacture an
  approval pass that was not actually granted through that queue.
- **#1352 config revisions** (`config_revisions.py`) — immutable revision
  history for policy/profile/org documents. This module's own
  `autonomy_mode_revisions` table follows the same shape (who, when, from,
  to, reason) for mode changes specifically, scoped to a mission rather than
  to the whole authority config.

Autonomy modes answer a narrower question than any of the above: **for this
mission, right now, does a given class of action proceed quietly, or does a
human need to see/ack it first** — always on top of whatever those other
systems already decided.

## The four modes

Ordered loosest → strictest (`autonomy_modes.MODES`):

| Mode | What a human sees | What proceeds without pausing | What pauses |
|---|---|---|---|
| `observe` | Everything, live | Everything not already gated | Nothing beyond existing gates |
| `exception` | Only failures, denials, budget warnings | Everything not already gated | Nothing new; it is `observe` made quiet |
| `checkpoint` | Declared checkpoints, plus anything already gated | Everything except declared checkpoints | Declared checkpoint action classes (default: `merge`, `deploy`, `external_write`) |
| `action-review` | Every side-effecting action | Nothing, unless a standing grant names it | Every action class without an explicit standing grant |

Declared checkpoints are `autonomy_modes.DEFAULT_CHECKPOINT_ACTION_CLASSES`,
a constant for this slice. A mission's `standing_grants` (see below) can
exempt specific `action_class` values from `checkpoint`'s default pause, and
`action-review`'s default pause, without touching the checkpoint list itself.

### Action classes, not tool names

`action_class` is a caller-supplied label (`"merge"`, `"deploy"`,
`"external_write"`, `"read_status"`, ...), not necessarily an MCP tool name.
When it happens to match an entry in `config.approval_gated_tools()`,
`evaluate()` also consults the existing approval queue for that exact label —
see "Gate order" below. This lets a caller name action classes that make
sense for its own domain (a workflow step, a deploy stage) while still
getting an explicit-gate lookup for free when the label lines up with a
gated tool.

## Who may change the mode

- **A human with authority over the mission** may set the mode directly
  (`set_mode` / `goal_manage("autonomy_set", ...)`), grant a standing
  exemption (`grant_standing`), or revoke one (`revoke_grant`). "Authority
  over the mission" is, for this slice, any identity `config.actor_kind_for_identity`
  classifies as `"human"` — the codebase's existing human/agent
  classification (the configured operator label plus its name and aliases). A
  richer per-mission ownership model (only the mission's declared owner, or
  a role scoped to its project) is a natural extension once goals carry that
  metadata; it is not invented here.
- **An agent may only PROPOSE a mode change** (`propose_mode` /
  `goal_manage("autonomy_propose", ...)`) — task #1431's propose-on/
  apply-gated semantics. Calling `set_mode` (or `grant_standing`/
  `revoke_grant`) as a non-human identity is refused outright, with a message
  pointing at `autonomy_propose`.
- A proposal is parked in the **existing** global approval queue
  (`tools/approvals.check_approval_gate`, `force=True` so it is gated
  regardless of `UA_APPROVAL_TOOLS`, `request_only=True` so proposing never
  blocks the caller). No second store, no separate chat/notification
  channel — the proposal is visible in the same `approval_pending()` list as
  everything else, and a human resolves it with the existing
  `approval_resolve`.
- `apply_proposal` / `goal_manage("autonomy_apply", ...)` then consumes that
  one-shot pass and applies the change, **attributed to the human who
  resolved the approval row** (`resolved_by`) — never to the proposing agent,
  and never to whichever identity happens to call `apply_proposal` (an agent
  retrying its own proposal after approval still cannot make the change
  appear self-authorized; the revision names the actual approver).

## Org policy ceiling

A mission mode may only be **stricter-or-equal** to the org ceiling, never
looser — `org_ceiling()` reads a `autonomy_ceiling: <mode>` scalar from the
same policy file `policy.py`/`budgets.py` already read
(`config.policy_file_path()`, env `UA_POLICY_FILE`), purely read-only; this
module never writes that file. Attempting to set (or propose) a looser mode
raises with "scope escape denied" in the message.

**No ceiling configured → the default ceiling is `observe`, the LEAST strict
mode.** This is deliberate: an absent ceiling must not silently *add* a new
restriction that operators never opted into, and it must not silently *loosen*
anything either — `observe` as a ceiling permits every mode, i.e. no
organizational constraint yet, exactly matching today's un-configured
behavior. A malformed `autonomy_ceiling` value fails closed (`ToolError`),
the same posture `budgets.load_budgets` takes on its own file.

A mission with **no mode ever set** starts at the opposite end — the
**strictest** mode, `action-review` — not at the ceiling. The ceiling bounds
looseness; it is not a default. A brand-new mission gets full oversight until
a human actively relaxes it.

Unless an operating profile says otherwise (task #1756). When a mission has
no explicit mode and the deciding run's frozen operating snapshot sets
`autonomy`, that value is the default instead: for a merge, the run of the
task being merged (it must serve this mission); otherwise the actor's live
runs on the mission's tasks, strictest first, with a run that sets no
autonomy counting as `action-review`. The org ceiling still tightens it, and
an explicitly set mission mode is never replaced. Profiles are set through
reviewed policy revisions or by a human (see `docs/operating-profiles.md`).

## Effective-policy preview

`preview(mission_id)` / `goal_manage("autonomy_preview", ...)` returns, in
one call: the mission's current mode and whether it was ever explicitly set,
its standing grants, the org ceiling, the checkpoint action classes actually
in effect (declared checkpoints minus any granted exemption), every
not-yet-started pending action recorded for the mission with its decision
recomputed against live lifecycle, budget, approval, and mode state, and the
invariants below — so a human reviewing a
mission's autonomy posture does not have to reconstruct it from the mode
name alone.

## Revision history

Every mode change, grant, and revoke appends one row to
`autonomy_mode_revisions` (mission id, from-mode, to-mode, actor, attribution,
reason, and the backing `approval_id` when the change came from an applied
proposal). Rows are never updated or deleted — `get_mode()` returns the
newest 100, newest first. This mirrors `config_revisions.py`'s posture
(immutable, append-only, rollback is a new revision) at mission scope instead
of whole-document scope.

## Attribution vocabulary

Stamped on every revision row and every `evaluate()` decision:

- **`human-approved`** — a named human directly set the mode/grant, or
  approved an agent's proposal that was then applied.
- **`policy-authorized`** — the mode itself (not a specific human decision
  for this action) authorized or paused the action — e.g. "observe mode lets
  this proceed" or "action-review mode says pause" or "the agent is paused,
  denied in every mode."
- **`standing-grant`** — a human-granted, per-`(mission, action_class)`
  exemption from the mode's default pause. Still subject to every
  pre-existing gate (lifecycle, budget, approval) checked before the mode is
  even consulted.
- **`proposal-pending`** — an agent's mode-change proposal is parked in the
  approval queue, not yet resolved.

## `evaluate()`: gate order

`evaluate(mission_id, action_class, actor)` is the one function a caller
asks "may I do this, right now, for this mission." It checks, **in order,
read-only, never loosening any of them**:

1. **#1351 agent lifecycle** — `agent_lifecycle.agent_state(actor)`; a
   `paused` or `terminated` actor is denied, in every mode.
2. **#1348 budgets** — `budgets.budget_status(actor)`; an exhausted
   `action: block` budget is a hard stop, in every mode.
3. **The existing approval gate** — a read-only lookup (never inserting,
   consuming, or revoking a row) of the most recent `approved`/`denied` row
   for `(action_class, {mission_id, action_class, actor})` when
   `action_class` is in `config.approval_gated_tools()`. A missing pass pauses,
   and an explicit denial wins outright. A live approved pass satisfies this
   gate but does not waive a checkpoint or action-review pause.
4. **Only then, the mode** — `_mode_decision()` applies the table above,
   returning `policy-authorized` or `standing-grant`. Read-only action classes
   proceed after lifecycle and budget checks without a mission pause.

Lifecycle and budget read errors deny the action. A malformed budget policy
also denies it; an unavailable gate is never treated as permission.

The result is `{decision: proceed|pause_for_human|deny, reason, attribution,
surfaced, mission_id, action_class, actor, pending_id}`. `surfaced` is
`False` only for an `exception`-mode `proceed` without a budget warning — everything else surfaces,
matching "exception mode is quiet except failures/denials/budget warnings."

Each call also upserts a **not-yet-started pending row**
(`autonomy_pending_actions`, keyed by `(mission_id, action_class, actor)` for
this slice — see the module docstring for why that key, and its limits, are
a deliberate simplification). Tightening a mission's mode
(`set_mode`) or revoking a standing grant (`revoke_grant`) immediately
re-evaluates every not-started pending row for that mission and flips its
stored decision if any gate now disagrees. The mode or grant change and the
pending reevaluation commit in one transaction. A caller that begins executing a
`proceed` decision should call `goal_manage("autonomy_mark_started",
{"pending_id": id})` as the pending row's owning actor so that in-flight
work is not retroactively contested by a later tightening; only genuinely
pending (not-yet-started) work is re-decided.

A human can acknowledge exactly one paused action with
`goal_manage("autonomy_review_action", {"pending_id": id})`. The review is
bound to that pending row and, for a globally gated action, to its live
approval pass. Revoking that pass or replacing it requires a fresh mission
review. The acknowledgment does not create a standing grant. The owning
actor must still mark the resulting `proceed` decision started. A new action
gets a new pending row and requires its own review. The live organization
ceiling is applied when evaluating and again at `mark_started`, so a policy
change cannot leave an old `proceed` decision usable.

## Invariants

1. **No mode grants new privileges or removes an explicit gate.** Gate order
   above is enforced structurally: the mode is consulted last, and its own
   decision function (`_mode_decision`) has no path back into the lifecycle,
   budget, or approval tables — it cannot even see them.
2. **Cancellation, budget hard stops, and revoked/tightened grants are
   enforceable in every mode**, including the loosest (`observe`). Tests
   `test_explicit_denial_is_enforced_even_in_observe_mode`,
   `test_agent_lifecycle_stop_denies_in_every_mode`, and
   `test_budget_hard_stop_denies_in_every_mode` exercise this directly by
   looping over every mode in `MODES` and asserting the same deny.
3. **Attribution is truthful.** `human-approved` never appears on a row a
   human did not actually resolve; `apply_proposal` reads the attribution
   off the approval row's `resolved_by`, not off its own caller.

## Exposure: `goal_manage`, not a new tool

The slice adds no new top-level MCP tool — the default profile is already
near its 100-tool cap (`tests/test_profiles.py`). It adds eight actions to
the existing `goal_manage` consolidated tool (missions already *are* goals):
`autonomy_get`, `autonomy_preview`, `autonomy_set`, `autonomy_propose`,
`autonomy_apply`, `autonomy_grant`, `autonomy_revoke_grant`,
`autonomy_evaluate`, `autonomy_mark_started`, and `autonomy_review_action`. `goals.py`'s `goal_manage` dispatches any
`autonomy_*` action to a thin `_autonomy_action()` helper that only maps MCP
params onto `autonomy_modes.py` — the module stays the single owner of the
contract's logic.

`server.py` classifies `autonomy_get` and `autonomy_preview` as read-only,
including for read-only agent sessions. Mutating actions retain the normal
write guard.

Mission-linked local task merges now enforce these decisions automatically.
`git_merge_task` and direct `merge.orchestrate_merge` calls resolve the mission
only from the stored `task_goal_links` edge. The linked goal must exist and
belong to the task's project; unavailable or invalid authority denies the merge.
The actor is the verified caller, never the task claimant or a note/body label.
Tasks without a goal link retain the existing merge behavior.

Before validation, the boundary binds the action class `merge` to the exact
task ID, trusted repository path, source branch, source commit SHA, parsed
policy fingerprint, stored current run token and admitted task status. Completed
or direct tasks may have no current run token. A cancellation request denies
execution even with `force`. A change between the initial task precondition and
admission also requires a fresh attempt. A pause returns the pending decision for human review before validation or merge writes;
a denial stops execution. A review of another task or source revision does not
approve this action. After validation, the boundary re-reads the mission link,
caller, source, policy fingerprint, current run token and task status, rechecks
current profile, access mode, trust admission and production revocation/scope, then marks the exact action
started. A policy change invalidates the prior decision and review. That
atomic start also rechecks lifecycle, budgets, grants and human review before
checkout or pull. Its private admission callback reads the mission link, task
run/status/claimant, cancellation flag, open delegations and live approval
receipts on that same `BEGIN IMMEDIATE` connection, runs `git checkout` (the
first Git mutation) while that write fence is held, and rechecks the same
authority before commit. A cancellation or takeover therefore cannot commit
between admission and checkout. `force` does not bypass any mission gate. The
actual merge uses the reviewed commit SHA.

Dry runs validate and return a read-only decision preview without creating,
starting or consuming an autonomy action, merging, or cleaning up the source.
The validation commands themselves may create their normal test artifacts.
`git merge` of the reviewed SHA runs under a second write fence that repeats
the same authority, delegation and approval checks, so a cancellation,
takeover, delegation change or approval revocation that commits after checkout
stops the merge itself (`git pull` of the target branch runs between the two
fences and changes no task source).

Closeout runs under a third write fence that overlaps Git's native lock on the
source ref. It closes the task, run and admitted delegations only with a
compare-and-set on the admitted run, status and claimant. The
`[review: merged @ …]` marker is deferred until cleanup succeeds, so retained
work remains visible in the delegation review projection. If ownership changed, the task is
left to its current owner. If the source advanced, a delegation changed or the
source worktree has uncommitted work, the task stays outstanding (a task already
`done` reopens to `waiting_review`) and the successor source and worktree are
preserved. The exact merged source SHA and merge commit are recorded as an
`agent.merge.local` event in the same transaction. Only after that commit is
a new writer fence rechecks the post-closeout task/run/delegation authority
and holds it through worktree removal (never forced). A takeover in the commit
gap retains the successor checkout and branch. Source branch deletion runs
under a separate task authority fence plus expected-SHA `update-ref`. A later
takeover retains the branch and reports `branch_retained`, while the already
completed closeout remains successful. Legacy (non-mission) cleanup uses the
same expected-SHA delete and reports `cleanup_skipped` when the source tip is
no longer the merged commit, including under `force=True` (task #2373). If
removal fails, the same cleanup transaction reopens the task to
`waiting_review` under a compare-and-set on the closeout it just committed and
a second event records it as not completed. A failure before that commit never
removes the worktree. The CLI prints the pause decision and pending review ID
and exits with status 2.
Central tool/default/role/threshold policy gates also apply to direct/internal
`orchestrate_merge` calls. Rechecks evaluate current rules and reuse the
complete trusted bound arguments, including positional arguments and defaults.
A private adapter capture separately retains the exact historical approval
payload within one invocation, preserving queued pass matching and a single
rate reservation. No request field can select
or waive this internal admission context.

Production approval also applies to direct orchestration and remains a
separate one-shot gate. A cached in-invocation receipt is revalidated against
the live approval row (still consumed, unexpired, same grant) and against its
unique consumption event before reuse and again inside the start fence, so a
revoked, expired or re-consumed pass cannot authorize the older invocation.
A restore only returns the invocation's own consumption.
Joined approval receipts restore passes on mission pauses, denials, dry runs
and failures before the first Git mutation. The wrapper defers its execution
marker to that boundary; once checkout is attempted, the passes remain spent.
Policy approval and production approval may protect different exact payloads;
each is consumed once, without another pass for internal policy rechecks.

This integration covers the local task merge path. GitHub merges, deployment,
native adapters and other external execution paths still need their own
automatic mission boundaries; the decision API alone does not govern them.

## Storage

Five new tables, owned entirely by `autonomy_modes.py`:

- `autonomy_mission_mode` — one row per mission with an explicit mode:
  `mission_id` (PK), `mode`, `updated_at`, `updated_by`. Its legacy JSON
  `standing_grants` column is migrated at initialization and then cleared.
- `autonomy_standing_grants` — one row per mission and action class, with a
  unique key so concurrent grants cannot overwrite each other.
- `autonomy_mode_revisions` — append-only history: `mission_id`, `from_mode`,
  `to_mode`, `actor`, `attribution`, `reason`, `approval_id`, `created_at`.
- `autonomy_pending_actions` — the not-yet-started ledger `evaluate()` and
  the revoke/tighten paths use to answer "did this action's decision just
  change": `mission_id`, `action_class`, `actor`, `decision`, `reason`,
  `attribution`, `started`, `created_at`, `updated_at`, and immutable canonical
  JSON `action_context`. A partial unique index permits one outstanding row
  per mission, action class, actor and exact context. Legacy decisions have a
  null context and cannot stand in for a reviewed concrete merge.
- `autonomy_action_reviews` — one human acknowledgment per exact pending row,
  bound to its global approval pass when applicable.

`storage.init_db()` calls `autonomy_modes.ensure_schema(conn)` through
`storage._migrate`, alongside the other module-owned schemas. Initialization
upgrades legacy review/context columns and normalizes JSON standing grants
within a serialized transaction: the grants and cleared JSON source commit
together, so retrying initialization cannot resurrect a revoked grant.
No `SCHEMA_VERSION` bump is needed for these additive tables.

**Tenant fence (task #1978).** The four mission-keyed tables (mode, grants,
revisions, pending actions) carry a nullable `org_id`, added idempotently at
initialization and stamped at insert from `edition.tenant_org_id()`. Every read
and write by mission id or pending-action id is filtered with
`org_id IS <tenant>`. Goals carry no tenant, so a mission belongs to the tenant
whose autonomy rows it already has. For another tenant, that mission reads like
a missing goal (`unknown mission (goal): N`): it cannot read, set, grant,
revoke, propose, evaluate, review or start anything on it. Writers re-check
inside their write transaction. On the private hub `tenant_org_id()` is `None`,
so every row keeps `org_id IS NULL` and behavior is unchanged. Rows written
before the column existed stay NULL and count as untenanted. Reviews are keyed
by a pending-action id and are fenced through that row.

The hub initializes before serving MCP and Control Center requests; the
standalone `unified-agent-merge` CLI initializes before orchestration.
`unified-agent-review scan/task` first refuses missing, stub, or retired local
stores, then initializes the accepted store before any automated merge.
The platform CLI reads the initialized canonical hub over HTTP. Direct library
callers initialize once before using autonomy mutations. Request connections
are plain `storage.connect()` calls: they never issue DDL, migrate grants,
or commit a caller's transaction. Read-only previews retain compatibility
with an older store without changing it.

## Explicit seams for undecided work

- **#1407 (agent self-merge ledger decision)** is not decided or built. Its
  seam is the `attribution` vocabulary itself: when #1407 lands, its ledger
  decision is a new attribution value (e.g. `ledger-authorized`) alongside
  `human-approved`/`policy-authorized`/`standing-grant`, slotted into
  `_mode_decision`'s or `evaluate()`'s existing branching without changing
  the gate order or any other invariant. This module does not guess at
  #1407's outcome and does not add a merge-specific code path.
- **#1421 (trust presets)** adds tightening-only policy and filesystem
  restrictions; see [trust-presets.md](trust-presets.md). Presets do not bundle
  standing grants or alter this module's involvement modes. Budgets, grants,
  and org ceilings remain their existing governed mechanisms. Native execution
  under a preset is refused until provider containment is verified (#1355).

## Sequencing note (consumes, does not add, attention surfaces)

Per the task spec: this slice adds no new chat/notification store. A
proposal is surfaced through the existing `approval_pending()` queue; a
paused/exhausted/denied decision is surfaced through `evaluate()`'s own
`reason`/`surfaced` fields, for a caller (a future attention/checkpoint UI)
to render using the platform's existing approvals/attention/messages
primitives — not a parallel one.
