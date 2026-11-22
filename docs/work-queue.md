# Work queue: honest reads, intake and backpressure

Task #1430. the operator's direction (2026-09-26): improve the existing work queue,
do not replace or supersede it. Everything below sits on the seams that already
exist: the `tasks` table, lanes, `task_claim` leases, WIP caps, and routines.

## Reads that say when they are partial

`task_list` returns `truncated`, `next_cursor` and the effective `limit`. A
`cursor` argument resumes a keyset walk (priority, `created_at`, `id`), so rows
inserted mid-walk neither duplicate nor shift. The `unified-agent://board`
resource reports the same two fields.

Code that needs the whole result, such as a lookup by id, calls
`tools.tasks.task_list_all(**filters)`. It follows the cursor to the end and
raises instead of returning a partial list. The Control Center's Work pages
follow the cursor the same way and say `truncated` if they stop early.

## Operator time

Task timestamps stay UTC in storage (`docs/task-lifecycle-contract.md`).
Operator reads render the local day: `task_list(local_day="today")` resolves
in `tz`, `UA_OPERATOR_TZ` or the host zone, so work created at 23:30 local
(already the next UTC day) lands in the day it happened. `created_since` and
`updated_since` require an explicit UTC offset.

## Backpressure

`queue_hygiene.queue_pressure()` is a read over existing rows. It adds no
table and writes nothing. It reports queue depth by status and lane, the oldest
open task per claimable lane, claim ages, per-agent and per-lane WIP against
`UA_AGENT_WIP_MAX` / `UA_LANE_WIP`, contention (live tasks claimed by two or
more agents in 7 days), and routine intake.

It separates two kinds of finding:

| Kind | What | Effect |
|---|---|---|
| `signals` (faults) | a `doing` task with no claimant (no lease protects it); a lease past expiry; a hard lane cap holding back waiting work | `level: pressure`; doctor `queue_backpressure` is DEGRADED; the queue panels on Work show an alert |
| `notes` (backlog shape) | a `dispatch-ready` task unclaimed for over 72h; contention; routine fires queued or skipped | reported, but the queue stays `ok` and doctor stays LIVE |

A long backlog is not a broken queue, and a row that is always DEGRADED stops
meaning anything. So aging work is a note, not a fault.

Surfaces: `system_doctor` row `queue_backpressure`, `/api/queue-health`
`backpressure`, and the queue panels on Work (`/ui/work`).

## Intake policy

Repeatable inbound work goes through a **routine**. The routines object
(`docs/routines.md`, #1350) already carries the intake vocabulary:

- `lane`: where the work lands;
- `concurrency`: `skip` drops a fire while a previous run is still active;
  `queue` creates it `blocked` and releases it when the run finishes;
- `catch_up`: `none`, `last` or `all` missed cron fires;
- `trigger`: `cron`, or `api`/`webhook` with an idempotent per-event fire key.
- `activity_gate`: `require_activity` suppresses a fire when its scope was quiet.

The backpressure read reports routine outcomes (queued and skipped fires) as
intake notes.

Producers that create tasks outside routines, and how each avoids duplicates:

| Producer | Dedupe |
|---|---|
| Slack gateway | `idempotency_key=slack:<event_id>:task` |
| Telegram/Discord worker (`ts_worker`) | `idempotency_key=ts:<message_id>:task` |
| Audit triage | `[audit-triage: <root>:<key>:<class>]` notes marker, checked with `task_list_all` |
| Session-learnings filing | candidate dedupe before filing |
| Agents via MCP `task_add` | optional `idempotency_key` |

A new source of repeatable inbound work should be a routine (`trigger="api"`
or `"webhook"`) rather than another ad-hoc `task_add` caller.

**A proposal-spawned task is a frozen contract.** Approving a work-proposal
item (`org_proposal`) creates its task from the approved item, and the task's
title plus the notes prefix — the item's own notes followed by the
`[proposal: …]`, `[contract: …]`, `[scope: …]` and `[reporting: …]` markers —
are that contract. `task_claim` recomputes the approved item's digest and
compares it with the task's title, its marker and that exact prefix, so a
changed title, or any edit inside the prefix, makes the task fail closed at
claim time with "the task notes differ from the approved contract"
(`work_proposals.check_contract`, task #2393). Recovering it is not supported:
no operation rewrites the prefix back, so the task is permanently unclaimable.
Note that `task_update` does not check the contract itself — the refusal
arrives at the claim. Append notes *below* the prefix for progress and status;
appending is the supported write. When the approved scope is genuinely wrong,
close the task and file a new proposal item: amending an approval is a new
approval, not an edit.

## Closure: stale, parked, folded, merged

Task #1848. Operator rules: an untouched task is **parked** with a stated reason
(not closed); children of a parked epic **fold** into it; merged work **closes**
with its SHA as evidence; nothing closes on a date alone. All of it is built on
existing objects. There is no new table and no new status. Source:
[`queue_closure.py`](../src/unified_agent/queue_closure.py).

**Representation.** *Parked* is `archived_at` set on a task that is not
`done`/`cancelled`, plus a recorded notes line
`[parked @ <ts> by <actor>] <reason>`. A folded child carries
`[folded-into: #<root> @ <ts> by <actor>] <reason>` and keeps its `parent_id`.
`queue_unpark` clears `archived_at` and appends `[unparked @ <ts> by <actor>]
<reason>`; the last such line is the task's closure state and earlier lines are
history. `archived_at` already hides a task from default views, which is what
parking should do, and a `done` task that is archived is filed work, never
"parked". A parked task keeps its status, so parking is not cancelling.
`task_claim` refuses a parked task until it is unparked.

**Views.** `task_list(parked=True)` (use `status="all"` for every status)
returns parked and folded tasks with a `parked` object
`{kind, reason, by, at, root}`; it is exclusive with `archived`. Parked tasks
are counted separately from open ones in the health read.

**Staleness (a read, never an action).** A task is stale when its latest sign of
life is `UA_QUEUE_STALE_DAYS` (default 30) days old, inclusive. Sign of life is
the latest of `updated_at` (status, notes and field edits), `claimed_at` and the
task's comment thread. The queue-hygiene stamps (stale-claim release, aging
demote) write `updated_at`, so a hygiene touch restarts the clock once. A task
with a live lease, an open run or a live delegation is being worked and is never
stale. Finished, archived and parked tasks are not stale.

**Actions** (through `coordination_manage`, no new tool; the default profile is
at its tool cap):

| Action | Kind | What it does |
|---|---|---|
| `queue_health` | read | `open`, `parked`, `stale` (plus `stale_actionable`) and `milestones_review_ready_unjudged`. `ownership_states` and `ownership_examples` classify unfinished tasks as sleeping (a hold is running), zombie (a live lease with its run over, or an open run with no lease), dead (in `doing` with no live lease or open run), shelved (an owner's hold ran out) or void (an owner with no claim or hold); report only (task #2388). `ready_unowned` counts unparked active tasks whose explicit prerequisites are all closed and that have no next owner, claimant or agent lane, so no ready notice reached anyone (task #2454). `unified-agent-review queue health` and the `queue_closure` doctor row show the same numbers. |
| `queue_stale` | read | Stale tasks, oldest first, with age and any protection. |
| `queue_sweep` | read, dry run | The PROPOSED list: `{task_id, title, proposed_action: park\|fold\|close, reason, evidence, auto}`, plus `skipped` (with the reason) and `ambiguous_merges`. Writes nothing. |
| `queue_park(task_id, reason, approved_by=None, fold_children=True)` | write | Park with a required reason; open descendants fold in the same step. Refuses a live claim on the task or any descendant, and refuses to leave open children under a parked root. |
| `queue_fold(task_id, into, reason)` | write | Fold one open descendant into an already-parked ancestor (a child added after the park). |
| `queue_unpark(task_id, reason)` | write | Reverse a park; restores the children folded into it. A folded child is restored through its root only. |
| `queue_close_merged(dry_run=True)` | write | Close-on-merge; `dry_run=False` performs it. |

Park and fold never run automatically. Only the sweep proposes them, and an
operator approves each by calling the action.

**Never proposed, never auto-closed.** Operator-only and interactive-only lanes;
`tracking` rows (the one exception is a fold under a parked parent); rows with a
human gate (`status=waiting_review` or a `[waiting-for: ...]` marker); rows
marked red-tier; anything with a live claim, lease, open run or live delegation.
Parking such a row by hand needs `approved_by`, which is recorded in the notes line as an
attestation (who may call the action is the profile and policy gate's decision, as for
every `coordination_manage` action).

**Close-on-merge.** A task closes only when its `evidence_ref` (a PR URL,
`owner/repo#N`, or a commit SHA) or its `branch` (which must name the task id)
matches **exactly one** PR in the pr-watch cursor and that PR is merged with a
recorded merge SHA. The task is completed through `run_records.complete_direct`
with the merge SHA as `evidence_ref`; its previous `evidence_ref` and the PR URL
go in a `[queue-closure: closed-on-merge @ <ts>]` notes line. Two matching PRs,
or an open PR next to the merged one, is ambiguous and is reported, never
closed. A task with open children, or a PR whose SHA the poller has not yet
recorded, is reported as blocked. The data comes from the existing pr-watch
event stream: `pr_watch` now stores `mergeCommit` as `merge_sha` in its cursor
(it is not a PR event) and adds a `Merge commit:` line to the merged note. No
network call is made by the closure code. The rule runs inside
`run_hygiene_pass` (so every `unified-agent-review scan` tick), honors `--dry-run`,
but only proposes by default: it applies only with `UA_CLOSE_ON_MERGE=1`, which the
operator turns on after reviewing the first proposed list. The PR's head branch must
name the task, so a follow-up task that cites the PR it follows up on is reported as
blocked, never closed. Nothing here reads a date.

**Review-ready milestones** (the numbers #1840 needs). A *milestone* is an
unfinished task whose notes carry `[milestone-review]`. Its *gate* is the task
ids on its `Gate:` line plus its `task_dependencies` prerequisites. It is
*review-ready* when it has a gate and every gate task is `done`, and *unjudged*
when its notes hold no `VERDICT:` line. A milestone judged FAIL stays judged
until the operator clears the verdict. `queue_health` reports the count and ids;
it flags nothing in place and closes nothing.

## Subtree holds: pause, cancel, resume, restore

An operator can stop a whole unit of work, an epic or a sprint subtree, in one
action and let it resume safely, instead of cancelling tasks one by one (task
#1747, [`task_holds.py`](../src/unified_agent/task_holds.py)). The surfaces are
`coordination_manage` `task_hold`, `task_holds` and `task_hold_get`, and over
HTTP `GET` and `POST /api/tasks/holds` and `POST /api/tasks/holds/{id}/release`.
The Control Center marks held tasks with a **Held** badge and offers hold and
release on the task page (`/ui/work/tasks/<id>`).

- **One active hold per root.** A hold records the root's subtree as it is at
  hold time, with each member's status then. A task added under the root later
  is not held; re-hold after release to include it. A released hold is kept as
  history.
- **What a hold refuses.** New work on a member: `task_claim`, a move into
  `doing` through `task_update`, approved dispatch (readiness reason `held`),
  and node dispatch (`POST /api/node/dispatch` and the node queue itself, 409
  before the approval gate, so a held task never consumes an approval). The
  error names the hold. These checks sit next to the agent lifecycle pause
  check.
- **Queued node items stay queued (task #2493).** A claim that finds an item
  already queued for a held task **skips** it: the item stays `queued`
  instead of failing, so when the hold releases the same item can run. A
  paused target is skipped the same way. New enqueue still 409s before the
  work exists.
- **What a hold allows.** A live renewal, the claimant re-claiming its own
  `doing` task before its lease lapses, is not refused, so an in-flight run can
  finish and report. Refusing it would let the lease lapse mid-run, and an
  executor treats a refused renewal as lost ownership. A re-claim after the
  lease lapsed, and a takeover, are refused. A claim with no lease expiry
  (`lock_expires_at` empty) is not renewable while held, since it would renew
  forever.
- **Modes.** `pause` holds. `cancel` holds and then requests cancellation of
  each member's live work through the existing paths: `task_update(
  request_cancel=True)` on a live self-pickup run (the run's claimant or a
  lifecycle operator) and `node_queue.cancel` on a queued or claimed node item
  (its creator, the task's claimant or an operator). The verified human
  operator on the web counts as an operator for node items but not for runs,
  which accept only `UA_LIFECYCLE_OPERATORS`, so another agent's run reports
  `refused` there. Each result is reported per run or item; a refusal does not
  stop the rest. Resume and restore are refused while the cancel requests are
  still being recorded. Only the run's own verified agent can acknowledge a cancel
  (a fenced cancel of that run); on an unauthenticated stdio hub there is no
  verified caller, so restore leaves such members cancelled. Only a request the hold
  made itself, where the flag went from clear to set, is recorded as the
  hold's, together with the run it cancelled; a request that was already
  standing is reported as `already_requested` and is never cleared by restore.
  Until the requests are recorded the hold shows `cancelling` and restore
  refuses. `resume` releases and changes nothing else.
- **Restore.** `restore` undoes the hold's own cancel requests and releases
  the hold once every one is undone. Cancellation lives in the run ledger, and
  the executor stops a run whose ledger entry says cancelled, so restore cannot
  un-cancel a run that is still going. A member whose cancelled run is still
  open is reported `pending` ("run still stopping"): nothing is cleared and the
  hold stays active. Run `restore` again once the run has closed. For a closed
  run, restore clears a still-set cancel flag and reopens the task only if that
  run acknowledged the cancel (the executor's fenced cancel of its own run) and
  nothing has changed the task's status since. A task its claimant cancelled
  on its own, one cancelled again later for another reason, and one that was
  `done` or `cancelled` before the hold all stay as they are. Node items the
  cancel stopped are not restarted; dispatch them again after the release.
- **Who may release.** `resume` and `restore` are open to the hold's creator,
  a lifecycle operator (`UA_LIFECYCLE_OPERATORS`, the set `request_cancel`
  uses) and, over HTTP, the verified human operator. Anyone else gets an
  error (403 over HTTP). Without a verified identity only the same `actor`
  that created the hold may release it. Restore also applies
  `request_cancel`'s rule per member: undoing a cancel needs the cancelled
  run's claimant or an operator, and any other member is reported `refused`
  and keeps the hold active. The release route passes the hold id through,
  so it never releases a different hold placed on the same root meanwhile.
- **Release policy.** `manual` waits for resume or restore.
  `after_active_runs_finish` releases the hold once no member has live work: a
  claimed `doing` row whose lease has a future expiry (an empty lease does not
  count), a live dispatch run, or a queued or claimed node item. It is checked
  when a hold is read and when a claim meets the hold; there is no daemon. A
  release that happens inside a claim is published after the claim commits.
  The policy is for `pause` only: it is refused with `cancel`, and a pause hold
  using it cannot be escalated to `cancel`, because a hold that released
  itself could no longer be restored. A new hold with this policy needs at
  least one live run in the subtree; on an idle subtree it would release at
  once, so it is refused ("nothing is running; use manual").
- **Who and where.** Holding needs `task_update` in the caller's profile; over
  HTTP the verified operator may hold too. Every hold, release, cancel and
  restore (including a `restore_pending` that leaves the hold active) is an
  `event_log` row on `agent.task.hold`. Reads and writes are fenced to the
  caller's tenant.

## Liveness: every task has a next move

Task #1749. A task in `doing`, or `open` in an agent's lane, must have someone
responsible for its next move. `task_liveness.action_path(task, now=...,
context=...)` names that responsibility. It is a pure function: the facts it
reads from other tables are gathered in batch by `gather_context`, so the
classifier is table-tested without a database. The first path that holds wins:

| Kind | Holds when |
|---|---|
| `run` | a live claim lease (a claim with no expiry counts as live, as it does for `task_claim`), a live dispatch run, a claimed ua-node item, an unfinished executor attempt inside its lease (below), or an unclaimed `doing` row's self-pickup run written within the implicit one-hour lease or whose run agent is seen acting (below) |
| `review_participant` | status `waiting_review`, or a live `[waiting-for: review]` marker |
| `approval` | a pending approval whose payload names the task (`task_id` or `parent_task_id`; a generic `id` key is not matched, since for most gated tools it names another object) |
| `blocked_on:<ids>` | `task_dependencies` prerequisites that are not `done`/`cancelled` |
| `declared_wait` | a declared wait whose `until` is still ahead |
| `monitor` | a one-shot monitor due in the future |
| `human_owner` | lane `operator-only` or `interactive-only`, or claimed by the operator's label |
| `queued` | a pending delegation, a retryable dispatch run, a queued ua-node item, an unassigned `open` task in a shared lane (the pool is the queue), or an `open` task in the lane of an agent seen on the hub within 24 hours (its backlog) |
| `NONE` | none of the above |

`done`, `cancelled` and `tracking` tasks classify as `out_of_scope`.

**Agent lanes.** An `open` task in an agent's own lane is that agent's backlog
and classifies as `queued` while the agent was seen on the hub within
`AGENT_LANE_SEEN_HOURS` (24). Once the agent has not been seen for longer than
that, nobody is going to pick the task up and it classifies as `NONE`. "Seen"
is the same test queue hygiene uses to renew a live claimant
(`registry.seen_acting`): the agent's `last_seen`, ignoring stamps written by
`system_doctor` or a backfill.

**Unclaimed `doing` with a live agent.** A `doing` row with no claimant but an
open self-pickup run counts as `run` inside the implicit one-hour lease, and
after it for as long as the run's agent (`current_run_agent`, else the agent
on the run record) is seen acting on the hub within
`UA_HYGIENE_LIVE_CLAIMANT_MINUTES` (60). Only activity at least a minute after
the run opened counts, because opening the run stamps `last_seen` itself; this
is the rule `queue_hygiene.renew_live_claims` applies after a claim.

**Executor attempts.** An `executor_attempts` row has no timeout of its own.
An unfinished one counts as an open run only while it is younger than the
executor's task lease (the default one-hour claim lease), while the task's
claim is live, or while its delegation is `in_flight` and was claimed within
the scheduler lease TTL (`UA_DISPATCH_SCHEDULER_LEASE_TTL_S`). An attempt
whose process died without a finish stops counting after that.

**Sweep.** `run_liveness_sweep` runs at the end of the queue-hygiene pass (the
review cron tick); there is no separate daemon. It covers `doing` tasks and
`open` tasks whose lane is an agent label, skips parked rows, and leaves alone
any task written in the last hour, so a task just moved has time to be picked
up. `UA_LIVENESS_SWEEP` sets the mode:

| Mode | Effect |
|---|---|
| `off` | nothing, including monitor wakes |
| `report` (default) | one `agent.task.liveness` event per `NONE` task per UTC day (`kind: no_action_path`); nothing moves |
| `enforce` | the same entry, and the task moves to `blocked` through `task_update` with the note `[liveness: no action path @ <ts>] <detail>; recovery: claim it, declare a wait, or set a monitor` |

The default is `report`, so deploying the sweep never mass-moves tasks. Turn on
`enforce` only after `report` has run for a while and its findings have been
reviewed: every finding `report` shows is a task `enforce` would move to
`blocked`. The
sweep never completes, reassigns or releases anything, and never touches a task
with a live claim or an open run: those classify as `run`, and the enforce move
is fenced on the revision it read (`expected_updated_at`). Re-running it is a
no-op: entries are deduplicated per task per day, and a blocked task has left
scope.

**Exception digest.** `check_anomalies` turns the last 24 hours of entries
into `task_liveness` findings (severity `warning`), one per task, for tasks
that are not terminal and have not changed since the entry was written. They
reach the Control Center attention list and `/api/digest` "stuck" section like
any anomaly. They are digest entries, not pages: `check_anomalies_and_notify`
skips them.

**Monitors.** `task_update(monitor_at=<ISO>, monitor_note=<text>)` sets a
one-shot monitor; `clear_monitor=True` cancels it. When it is due, the sweep
clears it and counts the attempt in one transaction, then delivers once: a
hub message (`act="inform"`, sender `liveness-sweep`) to the task's claimant,
or a digest entry when the task is unclaimed or the message is refused. A
closed roster (`UA_AGENT_LABELS`) must list `liveness-sweep` for the message
path. Clearing before delivery means a crash can lose a wake but never send a
second one. `task_liveness.monitor_attempts`, `monitor_fired_at` and
`monitor_result` keep the record.

**A pull request's owner hears about it (task #2396).** Each `pr-watch` pass sends
the task's claimant one hub message per PR head (sender `pr-watch`) for a failing
check, a request for changes, a merge or a close without one. The task is linked by
the PR URL in its `evidence_ref`, and exactly one live claim must match. Every
decision lands once on `agent.pr.notify`, with a typed reason when skipped
(`no_signal`, `no_owner`, `ambiguous_task`, `send_refused`). On a closed roster,
list `pr-watch` in `UA_AGENT_LABELS`, or each send records `send_refused`.
A refused send also records `detail`: the refusal's type and first line, at most
200 characters and never a message body, so the ledger says why (task #2434).

**Declared waits.** `task_update(declared_wait={"reason": "...", "until":
"<ISO>"})` says the task is waiting on something outside the hub. `until` must
be ahead and at most 14 days out; `declared_wait={}` clears it. An expired
wait no longer counts. Optional `next_step` is free text of what happens after
the wait.

**Named deployment (task #2414).** `declared_wait` also takes optional
`deployment: {"target": "hub"|"<ua-node>", "sha": "<full commit>"}`. `target`
is `hub` or a registered ua-node; `sha` is a full 40-character SHA-1 or
64-character SHA-256. The wait clears only when that target reports that exact
commit: the hub's is the SHA its running process started from (and only while
that pid is alive), a node's is the `code_sha` it reports while online. A
merge, another target, or a different commit leaves the wait in place. While
the wait is live (`until` still ahead), `deployment_wait_blocker` stops
treating a merge as done. An expired wait holds nothing; `queue_health` lists
waiting and expired deployment waits. The claimant is told once when the wait
clears.

**Caps.** `monitor_at` and `declared_wait.until` may be at most 14 days ahead
(`DECLARED_WAIT_MAX`); set a new one when the old one fires or lapses. Both
must carry a UTC offset (`Z` or `+00:00`, or any offset, which is converted to
UTC); a naive time is refused as ambiguous, as `task_list(created_since=...)`
refuses it.

**Wait chains.** A wait declared while the previous one is still live extends
an unbroken chain; `wait_first_declared_at` records when the chain began, and
`task_update` returns it as `declared_wait.first_declared_at`. Clearing the
wait, or letting it expire before the next one, starts a new chain. When a
task's chain has run longer than `WAIT_CHAIN_MAX_DAYS` (28), the sweep records
one digest entry per task per UTC day (`kind: wait_chain`). It does not change
the classification: the task stays `declared_wait` and `enforce` does not move
it. The entry is there so a person looks at a wait that keeps being renewed.

**Authority.** Setting or clearing a monitor or a declared wait silences the
liveness check, so only these callers may do it: the task's claimant; when the
task is unclaimed, the agent its lane names; a lifecycle operator
(`UA_LIFECYCLE_OPERATORS`, by default the operator's label and aliases); and
the human operator's label (`UA_OPERATOR_LABEL`). Anyone else, or a call with
no attributed caller, gets an error and nothing is written. In the gateway's
args ledger `declared_wait.until` is recorded readable and only
`declared_wait.reason` is hashed.

Monitors and waits live in the self-contained `task_liveness` table (one row
per task); no column was added to `tasks`.

**Retry exhaustion.** A self-pickup run whose lease lapses is released by
hygiene and recorded `abandoned` on the run ledger with `reason_code:
lease_expired` (a takeover after the lapse, "claimed after the lease lapsed",
writes the same code). When a task's last three such lease-lapse finishes
(`SELFPICKUP_MAX_ABANDONED`) in the last 30 days all came with no other
outcome in between, and nothing else is responsible for it, the sweep
escalates the way the dispatch scheduler dead-letters a delegation: a digest
entry (`kind: retries_exhausted`, once per abandoned run), and under `enforce`
a move to `blocked` with a `[liveness: retries exhausted @ <ts>] ...
last_run=<run id>; recovery: ...` note. Native dispatch runs already count
retries on `delegations.attempts` and dead-letter through
`dispatch.scheduler.dead_letter_exhausted` (blocked run, failed delegation,
task note, and a `blocked_task` digest row). ua-node items never retry: an
expired claim is failed for the operator to reconcile. Neither needs a second
counter. Abandonments that are not a lease lapse ("finish not recorded", "task
re-entered doing", a repaired completion, a terminated agent) neither count
nor reset the streak.

The constants (one-hour grace, 14-day monitor and wait bound, 28-day wait
chain, 24-hour agent-lane window, three abandoned runs in 30 days, 24-hour
digest window) are deliberately simple; tuning them belongs to the
operating-profile work (#1732).

## What this deliberately does not add

- No second task store, queue table, or broker. `tasks` remains the queue.
- No new task status and no change to status semantics; the lifecycle stays
  pinned by `docs/task-lifecycle-contract.md` and the SQL CHECK constraints.
- No replacement of lanes, routines, the dispatcher, or `task_claim` leases.
- No new intake object: routines are the intake policy.
- No automatic action on backpressure. The read never reassigns, reaps or
  re-prioritizes; `queue_hygiene.run_hygiene_pass` remains the only writer
  (its close-on-merge rule is described above).
- No change to stored timestamps (UTC).
