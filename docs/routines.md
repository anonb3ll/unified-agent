# Routines: durable tracked work

Task #1350 is being delivered in reviewable slices. The current core adds
durable definitions and fire receipts, four MCP tools, a one-shot UTC cron
tick, and an authenticated HTTP trigger. It does not enable a timer or replace
an existing host cron.

## Create and inspect

`routine_add(title, trigger="cron", schedule="*/30 * * * *", lane="dispatch-ready")`
creates a paused-by-deployment definition: it is stored, but cron fires only
when the hub operator runs the tick with `UA_ROUTINES_ENABLED=1`. `routine_list`
lists definitions and `routine_pause(id, paused=True|False)` controls one.
These MCP tools are exposed to the chief-of-staff profile; the default developer
profile keeps its 100-tool cap and cannot change durable schedules.

The five schedule fields are UTC minute, hour, day of month, month, and day of
week. Values, ranges, lists, and steps are accepted. A match requires all five
fields; use `*` for an unconstrained field. An impossible schedule is rejected.

`trigger="api"` or `"webhook"` creates a definition without a cron schedule.
An authenticated MCP caller may invoke `routine_fire(id, idempotency_key=...)`.
The HTTP trigger route below can fire an `api` or `webhook` definition. The
key is the caller's stable event ID and is unique per routine. Replays return
the original task.
Manual fires also require `UA_ROUTINES_ENABLED=1` on the hub.

## Fire over HTTP

`POST /api/routines/{id}/fire` accepts `Authorization: Bearer <hub agent token>`
and `Idempotency-Key: <stable event ID>` (1–200 characters, no surrounding
whitespace). The token must
resolve to an identity whose default profile exposes `routine_fire`; currently
that is chief-of-staff. This route rejects UI cookies, OAuth browser sessions,
and unauthenticated requests even when hub auth enforcement is in warn mode.
The normal `/api` origin allowlist, agent-access checks, and registered tool
policy and approval gates also apply. No
separate webhook secret or public ingress is installed by this slice.

The request body is ignored. Callers should send a stable upstream event ID in
the header, then inspect the returned `{status, task_id}` receipt. Retrying the
same key for the same routine returns the stored receipt without creating a
second task or requesting another one-shot approval. A paused routine or cron
definition returns HTTP 409; an unknown
routine returns 404; disabled routines return 503. The route creates tracked
work, not evidence that an assignee completed it.
Replays still count as tool activity and performance samples. The hub serializes
receipt checks, approvals, manual fires, and pauses so simultaneous calls with
one event ID use one approval and receipt. With production agent access blocked,
development and validation hubs can still fire routines; production hubs deny them.

## Run one tick

`UA_ROUTINES_ENABLED=1 unified-agent-routines tick` runs a single scheduler
transaction against the hub database. Install a once-per-minute timer only on
the hub after reviewing the routines to enable. `--force` exists for a
one-shot operator test. No background thread is started by the MCP server.

The tick atomically advances each routine cursor and records a fire receipt
with its created task. Two ticks racing for the same minute serialize through
SQLite `BEGIN IMMEDIATE`; the unique `(routine_id, fire_key)` constraint is a
second duplicate guard. A tick processes at most 100 missed minutes per
routine; `backlog=true` means another tick is needed.

`catch_up=none` fires only if a scheduled minute is the current UTC minute; a
tick that passes over a slot records one skipped receipt for the latest of them.
`last` fires the latest missed occurrence; `all` fires each missed occurrence
up to the per-tick cap. `concurrency=skip` records a skipped receipt while an
earlier task is active. `queue` creates a blocked task and releases the oldest
queued task after all earlier routine tasks are terminal. Pausing stops future
cron fires; a previously queued task may still be released.

## Activity gate

A recurring sweep or briefing that would only report "nothing changed" can
skip itself. `routine_add(..., activity_gate="require_activity")` checks for
activity before each fire; the default `activity_gate="always"` fires on every
schedule exactly as before. `activity_gate_scope` picks where to look: `org`
(the default) is the routine's whole tenant, and `project` is the routine's
own project. A routine without a project cannot use `project` scope;
`routine_add` refuses it. There is no update tool: to change a gate, pause the
routine and add a new definition.

The window opens at the previous fire or suppression of the same routine
(its `routine_runs.created_at`), or at the routine's creation for its first
fire. A `skipped` receipt reported nothing, so it does not move the window.
Activity in the window, at whole-second precision, is:

- task rows created or updated in the scope;
- `event_log` rows in the routine's tenant. They carry no project, so `project`
  scope counts only events whose payload names a task in that project
  (`task_id`, or `ref_id` on a `kind: task` event).

Every count is fenced to the routine's tenant. Excluded: tasks created by any
routine, gated or not, and every event about them, and routine bookkeeping
events (dedupe keys `routine:` and `routine-run:`). Excluding every routine's
tasks, not only this routine's, is deliberate: otherwise two routines in one
scope would keep each other firing, each one's task counting as activity for
the other. The cost is that work done on a routine-created task (a status
change, a note, a comment) does not keep a gated routine alive; a follow-up
task created from it does.

A quiet scope records a `suppressed` receipt with `reason=no_signal` or
`self_echo` (see the [run ledger](#run-ledger)), its `window_start`,
`gate_matched=0` and no task. The cursor advances as for a
fire, so the next tick does not retry that minute, and the tick result counts
it under `suppressed`. An active scope fires as usual and records
`window_start` and `gate_matched` (the number of matching task and event
rows). `routine_fire` applies the same gate and returns
`{"status": "suppressed", "task_id": null}` for a quiet scope. Ungated
receipts leave the three columns empty.

## Run ledger

`routine_runs` is the ledger of every scheduling decision. Each row that made
no task says why in a typed `reason`, and `subject_key` says what the decision
was about:

| `reason` | `status` | Meaning | `subject_key` |
|---|---|---|---|
| `overlap` | `skipped` | `concurrency=skip` and an earlier task of this routine is active | `task:<active task id>` |
| `no_signal` | `suppressed` | The gate matched nothing, and no routine bookkeeping ran either | `project:<project>` or `org:<org id or default>` |
| `self_echo` | `suppressed` | The gate matched nothing, and all activity in the window was routine-created tasks or routine bookkeeping events | `project:<project>` or `org:<org id or default>` |
| `catch_up_none` | `skipped` | `catch_up=none` passed over the latest due slot, which was not the current minute | `routine:<id>` |

`created` and `queued` rows leave `reason` empty and use `routine:<id>`. A
`catch_up_none` receipt uses the slot's `cron:<timestamp>` fire key, so a
repeated or racing tick cannot write it twice. It creates no task and, like
every `skipped` receipt, does not move the activity window.

`routine_tick` adds `skip_reasons`, a `{reason: count}` map of the receipts
written by that tick (reasons that did not occur are left out).
`routine_list(recent_runs=N)` adds each routine's newest `N` receipts, newest
first, up to 100. The default of 0 leaves the output unchanged.

When a hub first gains `subject_key`, one migration pass sets `reason=overlap`
on older skipped rows and `reason=no_signal` on older quiet-gate rows. Those
rows keep an empty `subject_key`, because the old table did not record one.

The ledger records what a tick decided; it does not run one. No timer that
calls `routine_tick` ships with this change. A timer is a red-tier change that
needs the operator's explicit go, so until one is installed, cron routines fire
only when the tick is run by hand.

## Tasks and recovery

Task rows carry `[routine: ID] [scheduled: timestamp]` in notes and inherit
the routine's project, domain, lane, and tenant. A hub task is a tracked work
item; a fire receipt is not proof that an assignee completed it.
Definitions, cursors, and receipts live in the hub SQLite database and are
covered by the complete database backup. Vault state snapshots contain only
tasks and memory, so they are not a routine recovery source.

## Remaining acceptance gates for #1350

- Add assignee wake-up for newly created routine tasks.
- Show policy and run status on the Control Center's Runs → Routines page
  (`/ui/runs/routines`).
- Migrate one existing host cron to a routine on the live hub, then verify
  real fires, task creation, and recovery after a missed tick.
