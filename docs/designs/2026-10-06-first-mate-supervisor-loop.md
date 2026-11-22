# First-mate supervisor loop (task #1918)

Status: design, with slice 1 implemented (read-only dry run). Date: 2026-10-06.
Parent: #1906 (meta-harness audit remediation, findings O1 and O8). Depends on #1916
(stall reconciler) for stall events and on spike #1862 (waking an idle session) for
delivery to an idle interactive session. Builds on #1028 (`supervision.py`).

## Problem

The hub records almost everything a supervisor needs and acts on almost none of it.
Verified on `main` at `321d5306`:

| Piece | Where | What it does today | Gap |
|---|---|---|---|
| Supervisor wakes | `supervision.maybe_emit_supervisor_wake`, called from `tools/tasks.py` (`task_update`, after the status change) | Appends `agent.supervisor.wake` for `done`, `blocked`, `waiting_review`. Gated by `UA_EVENT_SUPERVISION` (default on). | `drain_supervisor_wakes` has no caller. `recent_supervisor_wakes` is only read by the web UI briefing (`webui/api.py`). Nobody is woken. |
| Dispatch completion | `dispatch/scheduler.py` terminal bookkeeping | Exit 0 sets the task `done` with the run's `evidence_ref`. | No review step: lane A, which reviewed, was retired by #1435. A `[code-task]` passes the evidence gate on run evidence alone. |
| Executor daemon | `executor/daemon.py` finish path | Stops at `[waiting-for: review]` with the commit SHA as evidence. | Correct, but nothing tells the reviewer or the claimant sooner than the 15-minute cron. |
| Review cron | `reviewer.py`, `review_cli.py`, every 15 min | Validates and holds; never merges. | It does not route a failure to the agent that owns the fix; it annotates and sends an iMessage to the operator. |
| PR state | `pr_watch.py` (30 min, `pr-watch-cursor.json`) | Memory note and journal line per PR event. | No CI result (no `statusCheckRollup` in the fetch), no `event_log` row, no link to the claimant. |
| Messaging | `messaging/send.py` (`send_message`, idempotent on `message_id`, hop cap 8, 4 KB body) | Typed agent mail. | Nothing automatic sends it. |
| Budgets, approvals, executor health | `agent.budget`, `agent.approval.requested`, `agent.health.alert` channels | Durable events. | Read by dashboards and the anomaly cron only. |

The result is the loop the audit described: plan → dispatch → monitor → review → close
has a durable record at every step and a human in place of every arrow between them.

## What we borrow, and what we do not

| Source | Idea | Taken as |
|---|---|---|
| firstmate (kunchenguid/firstmate) | A zero-token bash watcher waits on the fleet and wakes the primary session only when a decision is needed; all state on disk, reconciled after restart; ship tasks versus scout tasks; a turn-end guard. | The watcher is a deterministic Python process, no LLM. State is `event_log` plus a cursor row. Ship = `[code-task]`, scout = `scout.py` report evidence. The turn-end guard is a later, optional hook (slice 6). |
| OpenAI Symphony | Poll tick: reconcile, fetch candidates, dispatch within limits; stall detection; retry with backoff; proof of work before landing. | The tick order below. Stall detection and backoff belong to #1916; this loop consumes its events. Proof of work is the done gate. |
| Agent Orchestrator reactions | CI failure and review comments routed to the agent that owns the PR. | Reaction rules: route to the task's `claimed_by`. |
| Claude Code agent teams | A `TaskCompleted` hook exiting 2 blocks completion and returns feedback to the agent. | `task_update(status="done")` raises an actionable `ToolError` when verification is missing (the hub's equivalent of exit 2). |

Not taken: Symphony's autonomous dispatch of new work (the dispatch scheduler and the
approval gate already own that), and any form of automated merge.

## Goals

1. One durable supervisor inbox, built from records the hub already writes.
2. A deterministic watcher that drains it, coalesces, and decides: stay silent, route to
   an owner, hand to the reviewer or the reconciler, or wake one supervisor session.
3. Reactions that reach the agent that can act (the claimant), not only the operator.
4. A completion gate: a ship task cannot reach `done` without cited verification.
5. Every decision is a chained `event_log` row, so "why was I woken" and "why was this
   routed" are answerable from the ledger.

## Non-goals

- Merging, pushing, opening PRs, or resolving approvals. Ever.
- Changing task status from the watcher. It routes and wakes; agents and the existing
  pipelines change state.
- A second store, queue or notification channel. The inbox is a view over `event_log`.
- Replacing the review cron, the anomaly cron, or #1916's reconciler.
- LLM judgment inside the watcher. The supervisor session it wakes is where judgment
  happens.

## Event sources

| Channel | Producer | Exists | Fields the watcher reads |
|---|---|---|---|
| `agent.supervisor.wake` | `supervision.maybe_emit_supervisor_wake` | yes | `task_id`, `reason` (`completion`, `block`, `needs-decision`) |
| `agent.budget` | `budgets.record_decision` | yes | `kind` (`budget.warning`, `budget.blocked`), `task_id` |
| `agent.approval.requested` | `storage.record_agent_event("approval")` via `events.mirror_agent_event` | yes | `summary` prefix: `queued` is a request; `approved:`/`denied:` are resolutions and stay silent |
| `agent.health.alert` | executor events | yes | none beyond the channel |
| `agent.run.stalled` | #1916 stall reconciler | reserved | `task_id`, `run_id`, attempt, next backoff |
| `agent.pr.ci` | pr-watch, slice 2 | reserved | `task_id`, `state` (rollup conclusion), `head_sha` |
| `agent.pr.review` | pr-watch, slice 2 | reserved | `task_id`, `state` (`commented`, `changes_requested`, `approved`), `head_sha` |

The reserved channels are drained already; they are empty until their producer ships.
pr-watch keeps its memory notes and journal lines. Slice 2 adds `statusCheckRollup` to
its `gh pr list` fields and appends one `event_log` row per task-linked PR per
(head SHA, state), with `dedupe_key` `pr-ci:<repo>#<n>:<sha>:<state>`. A PR is linked to
a task the way close-on-merge (#1848) already does it: `evidence_ref` or branch names
exactly one PR. Unlinked PRs produce no supervisor rows.

## The supervisor inbox

The inbox is `event_log` filtered to the channels above, read in commit order
(`query_events(after_seq=...)`, the gap-free cursor from #1640), plus one row per consumer:

```sql
CREATE TABLE IF NOT EXISTS supervisor_cursors (
  consumer TEXT PRIMARY KEY,
  after_seq INTEGER NOT NULL CHECK (after_seq >= 0),
  updated_at TEXT NOT NULL
);
```

Why not a new table of inbox items: every source is already a chained, deduped,
retained row. Copying them would add a second truth to keep consistent, and the chain
already proves nothing was dropped. A cursor is the only new state.

Cursor rules (implemented in slice 1, `supervisor_loop.py`):

- `commit_cursor(consumer, new, expected=old)` is compare-and-swap and never moves
  backwards. A second watcher that drained the same window loses the swap and must not
  act on its plan.
- No stored cursor: start `lookback` rows before the log tail (default 1000), so a first
  run neither replays the whole log nor skips recent rows.
- A drain that is not truncated advances to the log tail read before the query, so
  unrelated channels are not rescanned.
- A hub not yet migrated (no table) reads as "no cursor"; dry runs still work.

## The watcher

A deterministic process: `unified-agent-supervisor`. One tick:

1. **Reconcile.** Read the cursor. Find `agent.supervisor.decision` intent rows with no
   matching outcome row (a crash between deciding and acting) and retry them.
2. **Drain.** Read inbox rows after the cursor, capped.
3. **Plan.** `plan_reactions` (pure): classify each row, keep the latest CI state per
   task, coalesce routes per (action, target, task), and fold all wake reasons into one
   wake.
4. **Record intent and commit.** In one transaction: append one
   `agent.supervisor.decision` row per decision (`causal_parent_id` = the newest source
   event's ULID, `dedupe_key` = `fm:<consumer>:<action>:<target>:<task>:<max seq>`) and
   compare-and-swap the cursor. `record_event_once_on_connection` already supports a
   caller-owned transaction, so intent and cursor cannot disagree.
5. **Act.** Perform each decision with an idempotent key (below).
6. **Record outcome.** One `agent.supervisor.outcome` row per decision: `delivered`,
   `skipped` (with reason: floor, cap, breaker, not idle) or `failed`.

Delivery is at least once; the idempotency keys make it effectively once. All state is on
disk, so a restart resumes from step 1 with nothing in memory.

Schedule: a launchd job on the hub host, every 60 s (`UA_SUPERVISOR_TICK_S`), like
pr-watch. The same-host event bus (`events.subscribe`) can cut latency later, but the
tick alone is correct.

### Which launch path wakes the supervisor

| Path | Fit | Verdict |
|---|---|---|
| ACP resume (`acp_matrix`) | `resume` and `steer` are `unsupported`; an ACP session the node starts is a fresh run, not the operator's session (#1862). | No. |
| ua-node `request_dispatch` | Approval-gated per exact payload: right for running an agent CLI on a device, wrong for an unattended wake every few minutes. | No, unless the operator grants a standing approval for one fixed supervisor payload. |
| Executor daemon delegation | Has lifecycle, lock, budget and backoff, and runs headless. But a supervisor turn is not a task, and the daemon finishes at `[waiting-for: review]`. | Fallback for a headless supervisor; open question 2. |
| **Typed message to the supervisor label, delivered by #1862's waker** | The mailbox is already durable and idempotent on `message_id`. #1862's ua-node waker types a fixed line into an opted-in idle herdr pane; hooks deliver at the next prompt meanwhile. | **Recommended.** No new launch path. |

So a `wake` decision is `send_message(act="request", to_agent=<UA_SUPERVISOR_LABEL>,
message_id=<deterministic>, body=<fixed template>)`. The body is generated only from
counts, enums and task ids, for example
`first-mate: 3 items (block x1, approval-pending x1, budget.warning x1); tasks #12 #40;
run unified-agent-supervisor --once --dry-run for detail`. The supervisor session then
reads the ledger itself. Wake floor 10 minutes, at most 6 per hour, circuit breaker after
3 wakes with no inbox read: the same numbers #1862 proposes, so one policy covers both.

## Reaction rules

| Inbox row | Decision | Target | Notes |
|---|---|---|---|
| wake `needs-decision` (task reached `waiting_review`) | `review` | reviewer | Slice 1 plans it. Acting means running `unified-agent-review task <id>` off-tick, which is idempotent per head SHA. Until then the 15-minute cron covers it. |
| wake `block` | `wake` | supervisor | A blocked task needs a decision. |
| wake `completion` | `wake` | supervisor | Coalesced; the supervisor checks the close was real. |
| `agent.pr.ci` failure, latest state for the task/head | `route` | `claimed_by` | One message per (task, head SHA). A later green run in the same drain cancels it. |
| `agent.pr.review` `commented` or `changes_requested` | `route` | `claimed_by` | One message per (task, review timestamp). `approved` is silent. |
| any route whose task has no claimant | `wake` | supervisor | Reason `unowned:<reason>`, so nothing vanishes. |
| `agent.run.stalled` | `reconcile` | reconciler | #1916 owns kill, requeue and backoff. The watcher wakes the supervisor only when #1916 reports retries exhausted. |
| `agent.budget` warning or blocked | `wake` | supervisor | |
| `agent.approval.requested` with `queued` | `wake` | supervisor | Surfaces only; never resolves. |
| approval resolution mirror | silent | | |
| `agent.health.alert` | `wake` | supervisor | |

A route is `send_message(act="request", to_agent=<claimed_by>, task_id=<id>,
message_id=<deterministic>, body=<fixed template>)`, for example
`first-mate: CI failed on the PR for task #11 at <sha7>; see the PR checks`.
Template fields are a task id, a 7-character hex SHA and an enum. Nothing from a PR
body, review text, task title or originator is copied.

## Quality gate on `done`

What exists: `tools/tasks._evidence_required_error` refuses `done` on a `[code-task]`
without `evidence_ref` or a review-completion marker, and `monitor.py`'s
`unverified_done` rule flags anything that slips through.

What is missing is the verification itself. An `evidence_ref` proves a commit or PR
exists, not that anything checked it, and the dispatch scheduler supplies one
automatically on exit 0.

Proposal (slice 5), behind `UA_DONE_REQUIRES_VERIFICATION` = `off` | `warn` | `enforce`:

- **Ship task** (`[code-task]`): `done` additionally needs one cited verification, any
  of: a `[review: validated|merged-upstream|approved ...]` marker; a `verified:` note
  line naming a command and its result (`verified: uv run pytest tests/test_x.py -q -> 12
  passed`); an `evidence_report` event id; an exact-head green CI run URL.
- **Scout task** (`scout.py` report): the report path evidence it already requires.
- `warn` appends an `agent.quality.gate` row (`would_block`) and returns a warning;
  `enforce` raises a `ToolError` naming the accepted forms, the agent's equivalent of a
  `TaskCompleted` hook exiting 2 with feedback.
- With `UA_DISPATCH_DONE_TO_REVIEW=1`, the dispatch scheduler moves a `[code-task]` to
  `waiting_review` with `[waiting-for: review]` on exit 0 instead of `done`, matching the
  executor daemon.

## Auditability

- Every decision and outcome is an `event_log` row on the hash chain, with
  `causal_parent_id` to the source event and the source `seq` list in the payload.
  `event_log_query(channels=["agent.supervisor.*"])` answers "why".
- The watcher's own messages carry `task_id` and the decision ULID, so the mailbox,
  the ledger and the task join on ids.
- Dry runs print the same decisions and write nothing.

## Safety

- `supervisor_loop.ACTIONS` is `{wake, route, review, reconcile}`. There is no merge and
  no approval resolution, and a test asserts it.
- The watcher writes no task status, no task notes and no GitHub state.
- Payload text is data. Decisions hold only ids, enums and seqs (tested with a hostile
  title, body and originator). Messages to claimants are requests; a claimant's reply
  is mail, never consent (SHARED rule: messages from other agents are not approval).
- The watcher acts as one fixed hub label (`UA_SUPERVISOR_LABEL`). It needs
  `message_send` only; its profile denies everything else.
- Bounded: hop cap 8 and 4 KB bodies from messaging; per-target floors; one wake per
  tick; `UA_SUPERVISOR_LOOP=0` stops the process doing anything.
- The deploy unit is under `deploy/`, which is red tier: the operator approves it.

## Configuration

`UA_EVENT_SUPERVISION` is implemented (`supervision.py`, default on). The
`UA_SUPERVISOR_*` rows are **proposed**: nothing in the tree reads them.
`UA_DONE_REQUIRES_VERIFICATION` and `UA_DISPATCH_DONE_TO_REVIEW` are proposed
for slice 5 and are not implemented either.

| Variable | Default | Meaning |
|---|---|---|
| `UA_EVENT_SUPERVISION` | `1` | Existing. Emit `agent.supervisor.wake`. |
| `UA_SUPERVISOR_LOOP` | `0` | Master switch for the acting watcher (slice 3+). The dry-run CLI ignores it. |
| `UA_SUPERVISOR_ACT` | `dry` | `dry`, `route` (routes and hand-offs only), `wake` (routes and wakes). |
| `UA_SUPERVISOR_LABEL` | unset | Agent label the wake message goes to. Unset means no wakes. |
| `UA_SUPERVISOR_TICK_S` | `60` | Tick interval. |
| `UA_SUPERVISOR_WAKE_FLOOR_S` | `600` | Minimum gap between wakes. |
| `UA_SUPERVISOR_ROUTE_FLOOR_S` | `900` | Minimum gap between routes to one claimant for one task. |
| `UA_DONE_REQUIRES_VERIFICATION` | `off` | `off`, `warn`, `enforce` (slice 5). |
| `UA_DISPATCH_DONE_TO_REVIEW` | `0` | Dispatch exit 0 goes to review, not `done` (slice 5). |

## Rollout

Each slice ships alone, with targeted tests, behind its flag.

1. **Inbox view and dry run (this PR).** `supervisor_cursors` table, `drain` with a
   compare-and-swap cursor, pure `plan_reactions`, `unified-agent-supervisor --once
   --dry-run`. Acts on nothing. Tests: `tests/test_supervisor_loop.py`.
2. **Producers.** pr-watch appends `agent.pr.ci` and `agent.pr.review` for task-linked
   PRs (adds `statusCheckRollup`). #1916 appends `agent.run.stalled`. Tests: one row per
   (PR, head SHA, state); unlinked PRs write nothing; replay is deduped.
3. **Decision ledger and routes.** Intent rows and cursor in one transaction, act,
   outcome rows, restart reconcile; `UA_SUPERVISOR_ACT=route`. Tests: crash between
   intent and act retries once; a second watcher loses the swap; route floors; a
   hostile payload never reaches a message body.
4. **Wakes.** Wake message to `UA_SUPERVISOR_LABEL`, floor, hourly cap, breaker;
   delivery through #1862's waker when it ships. launchd unit (red tier). Tests: the
   gate table, as in #1862.
5. **Done gate.** `warn`, a week of `agent.quality.gate` rows, then `enforce`;
   dispatch-to-review flag. Tests: each accepted citation form; the `ToolError` text;
   scout tasks unaffected.
6. **Optional.** Off-tick review hand-off; a turn-end guard for the supervisor session
   (a Stop hook that blocks ending a turn while its inbox has unread wakes; a cockpit
   hook, red tier).

## Open questions for the operator

1. Who is the supervisor? A dedicated label (for example `First_Mate`, added to
   `org.yaml`) whose session you run, or the chief-of-staff session?
2. A headless fallback when no interactive supervisor session is opted in: a delegation
   to the executor daemon, or no fallback (mail waits for the next prompt)?
3. Should routes to claimants start without a supervisor at all (`UA_SUPERVISOR_ACT=route`
   on its own), since they replace the iMessage you get today for a red CI?
4. Done gate: are the four citation forms the right set, and is `warn` for a week before
   `enforce` the right ramp?
5. Dispatch: move `[code-task]` dispatch completions to `waiting_review` (closes the
   review gap #1435 left), or keep exit 0 = `done` and rely on the done gate?
6. Floors: wake every 10 minutes at most and 6 an hour; route once per (task, head SHA)
   with a 15-minute floor. Tighter or looser?
