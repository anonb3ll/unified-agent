# Work graph view (task #1460)

One Control Center projection (`/ui/work?view=graph&scope=work`) over readers that already exist —
`src/unified_agent/work_graph.py`, served read-only at `GET /api/work-graph`.
It renders the goal -> plan -> task dependency graph with team/agent/run/
session links, a ready/blocked explanation per task, and drill-through to
runs, sessions, and evidence. Dispatch and self-pickup run IDs and session IDs
link to their existing read-only platform object endpoints. Autonomous-cycle
IDs link to the existing cycle detail route. Evidence links to the existing
evidence ledger filtered to the task. These links open existing JSON/detail
targets.
It adds no table, no scheduler, and no MCP tool — see `work_graph.py`'s module docstring for the full node/edge
vocabulary and how pagination and the reconnect `revision` work.

## Principle: no inferred edges

Every edge the projection draws maps 1:1 to a column or table another module
already declares as that relation (`goals.parent_goal_id`,
`task_goal_links`, `task_dependencies`, `tasks.parent_id`,
`dispatch_runs.task_id`, `agent_sessions.task_id`, a joint-plan checkpoint's
`context.joint.goal_id`, ...). It never parses a task's title or notes to
guess a relationship, even when the text names another object's id — a
regression test (`tests/test_work_graph.py::test_goal_subgraph_has_ancestry_and_goal_task_edges_not_from_prose`)
pins that a note that names a goal id never becomes a graph edge.

## Declared object-model gaps

These are relations the view would like to draw but that no reader
currently declares, so the projection surfaces them as absent rather than
inventing them:

1. **run -> session.** Neither `dispatch_runs` nor a self-pickup run record
   (`run_records.py`) carries a `session_id`. A dispatched run and the
   interactive/agent session that carried it out are two independent rows
   that both happen to reference the same `task_id` (and sometimes the same
   `delegation_id`) — there is no foreign key between them. The view draws
   `run_task` and `session_task` to the shared task node separately; it does
   not draw a `run_session` edge, because doing so from the shared task id
   alone would be exactly the kind of silent inference this task's spec
   rules out. **Proposed object-model change:** add a nullable
   `session_id` column to `dispatch_runs` (and to the self-pickup run
   payload), written by whichever side starts second, so a future edge can
   be drawn from a real reference instead of a coincidence.

2. **plan -> task (per step).** `plan_steps` has `owner` (free text) and
   `description` (free text) but no `task_id` column. A plan's steps
   therefore cannot be linked to the tasks that implement them — only the
   plan-level `plan_goal` edge is drawn, and only for a plan that was
   jointly bound to a goal via `joint_planning.bind` (#1455). A plan with no
   joint binding, or one whose steps were never bound to tasks, has no
   in-graph task drill-through at all; the plan node alone rides on the
   why-chain via its goal. **Proposed object-model change:** add an optional
   `task_id` column to `plan_steps`, written when a step is decomposed into
   a task (mirroring how `planning_loop.decompose_into_goal_linked_tasks`
   already links goal decomposition to tasks — plans just don't have the
   equivalent column yet).

3. **evidence, beyond `tasks.evidence_ref`.** The full human -> device ->
   agent -> action -> proof chain lives in `audit/evidence_report.py`
   (`build_evidence_report`), which is expensive to run once per task (it
   walks the event log, checks chain integrity, and computes attribution).
   Running it for every task on a work-graph page would make the page slow
   in proportion to page size for a report most operators only want for one
   task at a time. The projection therefore emits a lightweight `evidence`
   node from `tasks.evidence_ref` alone (whatever opaque string is already
   stored there: a PR URL, a commit SHA, or an evidence-event id) with a
   `drill_uri` pointing at the existing `GET /api/evidence?task_id=` route
   for the full chain. This is a scope decision, not a missing column: the
   full report reader already exists and is reused on drill-through, not
   duplicated.

## `graph_canvas_scenes` is unrelated

`src/unified_agent/storage.py` also defines a `graph_canvas_scenes` table
(task #807) — the verbatim Excalidraw scene one board id, edited in the
Canvas page (`/ui/work/canvas`) and parsed into task/dependency *suggestions* that still go
through the approval queue. It stores a sketch, not a declared relation, and
is not reused here: the work graph view renders structure that already
exists, not a proposal surface.

## Pagination and reconnect

`GET /api/work-graph` with no `goal_id` returns a keyset-paginated page of
root goals only (an overview). With `goal_id`, it returns that goal's
why-ancestry, its descendant sub-goals, any plan jointly bound to a goal in
that scope, and a keyset-paginated page (`cursor`/`next_cursor`, bounded by
`page_size` up to 200) of the tasks linked to a goal in that scope, each with
its dependency/parent/claim/run/session/evidence edges and a `ready`
explanation (`ready` | `blocked` with `unmet_dependency_ids` | `in_progress`
| `stalled` for an expired claim | `terminal`).

The dependency part of `ready` uses the same prerequisite rule as
`task_list(frontier=True)`: every declared prerequisite must be `done` or
`cancelled`. The graph also reports the task's own lifecycle status, so a
claimed, review-pending, or manually blocked task is not labelled ready just
because its prerequisites are complete. The frontier filter answers only the
prerequisite question; it can include those tasks. Missing prerequisite rows
are treated as unknown/unmet by the graph rather than assumed complete.
Session rows use the registry's normal stale-heartbeat reap before projection,
so a dead session cannot remain labelled running here after its heartbeat
expires.

`revision` is the max timestamp across every row folded into the response. A
`task_update`, a new run claim, a session heartbeat, or a plan checkpoint
normally advances it. It is advisory rather than a response-equality key:
removing a dependency or crossing a claim-expiry time can change readiness
without advancing any stored timestamp. The client accepts each refetch on
events, reconnect, and its 20-second poll. Nothing here is cached beyond the
single read — a reconnect is just another call with the same
`goal_id`/`cursor`.

## Unknown telemetry

`unknowns` lists `{node_id, field, reason}` entries for state this
projection could not resolve rather than guessing: an agent label with no
`agents` row (`registry.get_agent` returns `None`), or a task node whose
`run_history(limit=20)` read came back `truncated: true` (more self-pickup
runs exist than the 20 most recent ones shown). A cancelled run and a
restarted run both remain legible: `run_records.py` gives each claim/finish
cycle its own run id, so a released-then-reclaimed task shows as two
distinct run nodes on the same task, one `outcome: "released"` (or
`"cancelled"`/`"abandoned"`) and the newer one open or `"completed"` — never
collapsed into one row that silently overwrites the earlier attempt.

## When a prerequisite closes

Task #2454 makes the dependency edges act, not only display. When a task moves
to `done` or `cancelled`, every dependent it makes ready gets one notice. The
code is `src/unified_agent/ready_dependents.py`; it informs and never claims,
dispatches or changes the dependent's status (a `blocked` dependent stays
`blocked`, and the notice says its prerequisites are closed).

- **Ready** is the `task_list(frontier=True)` rule: the dependent is not `done`
  or `cancelled`, and every explicit `task_dependencies` prerequisite is `done`
  or `cancelled`. A cancelled prerequisite counts as closed; the notice lists
  cancelled prerequisites apart from done ones, because a cancelled one did not
  deliver its work. No edge is inferred from note text.
- **One chokepoint.** Every status writer passes through
  `run_records.queue_completion_return` inside its own transaction (the hook
  the completion return already uses): `task_update`, the merge closeout
  (`record_transition_in_tx`) and `run_records.complete_direct` (close-on-merge,
  the merge orchestrator, the reviewer auto-close). The readiness records are
  written there, in the closing transaction.
- **Exactly once per readiness generation.** Each ready dependent gets one
  `agent.task.ready` event whose `dedupe_key` is the dependent id, a digest of
  its prerequisite ids, and its epoch (the last time the dependent itself
  closed). A retried close, a second closer, a prerequisite reopened and closed
  again, or two prerequisites closing at once write the same key, so nothing is
  sent twice. A new prerequisite edge, or closing and reopening the dependent,
  starts a new generation.
- **Delivery.** The event is the outbox. After the commit the closer sends it as
  an `inform` hub message from the service sender `hub-graph`, with `task_id`
  set and `message_id` `task-ready-<event ulid>`, so a resend is a recorded
  duplicate. A crash before the send loses nothing: the queue-hygiene pass
  sends any event from the last 24 hours that has neither a message nor an
  `agent.task.ready.skipped` record. A dependent that is no longer ready by
  then, or a send the messaging guards refuse, is recorded as skipped once and
  not retried. The message sets `expects_reply`, so the existing wake gates can
  ring an idle session that opted in; the dead-letter sweep skips hub service
  senders.
- **Owner.** The dependent's `next_owner`, else its claimant, else the agent
  its lane names, else nobody. A ready dependent with nobody is recorded with
  `owner: null` and counted as `ready_unowned` by `queue_health`.
- **Tenant.** Only dependents in the closing task's org partition are
  considered.
- **Off switch.** `UA_READY_DEPENDENT_NOTIFY=0` (see
  [configuration.md](configuration.md)).

A dependent that becomes ready because an open prerequisite edge was removed,
rather than closed, is not notified: only a close triggers the check.
