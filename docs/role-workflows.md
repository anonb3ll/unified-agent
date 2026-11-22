# Role-bound workflows

Role workflows extend repo-reviewed workflow YAML with roles and a durable task
frontier. Definitions without `roles` keep the existing linear execution behavior.
The five original workflows require no rewrite. `build-review-example.yaml` shows
an opt-in builder/reviewer sequence; its `builders` and `reviewers` teams must
already exist in the installation's committed team specs.

Each role specifies `skills` and eligible seats through `preferred_seats`, `team`
and/or `office`. Team seats must advertise the required skills. When multiple
sources are supplied, eligibility is their intersection, with preferred seats
preserving their declared order. `role_bindings` may select an eligible seat;
it never changes caller identity, profile, permissions or team configuration.

Each step declares `actor_role`, `objective`, `allowed_exits` and optionally
`next_hop: {require: [role], suggest: [role]}`. Required next roles must include
the following step's role. Suggestions are advisory metadata. The linear step
order determines the next frontier. Role workflows disable automatic effect
retries; a failed or interrupted operation needs an explicit decision.

## Start, claim, close

1. Call `workflow_run(name, inputs=..., role_bindings=...)`. It creates an ordinary
   open task in the first seat's lane and returns `instance_id` and `frontier`.
   Every projected task's notes include the workflow name and instance id,
   so the next owner can discover the invocation from its queue item.
   Creating a workflow does not claim or dispatch a harness.
2. The bound agent claims `frontier.task_id` through normal `task_claim`, with
   `expected_lane` matching its label. Normal WIP, budgets, lifecycle and holds
   still apply. Read the returned task's `updated_at`.
3. Call `workflow_run(name, instance_id=..., closure={...})` as that agent.
   For a builder/reviewer handoff the closure is:

   ```json
   {
     "exit": "handoff",
     "reason": "handed_off_to",
     "target": "reviewer",
     "task_id": 123,
     "expected_updated_at": "2026-10-14T05:00:00+00:00"
   }
   ```

   The current step's tool runs through the same guarded callable as a linear
   workflow. The task closes, the successor task opens, and the closure trail
   appends in one storage transaction. A failure creating the successor rolls
   back the task close and trail together. The successful tool receipt remains
   durable, so retrying the same closure does not repeat its tool effect.
4. The next seat claims its own frontier task. The final step closes with
   `exit: done`, `reason: no_follow_on` and no target.

`waiting` and `blocked` require `blocked_on` with a bounded reference such as
`task:456`, or `escalation` with a declared role. They update the current task
without executing its tool. `failed` requires `denied` or `cancelled`, with no
target. A failed disposition is terminal; create a new instance after resolving
its cause. Both denied and cancelled failures cancel the frontier task, so it
cannot remain blocked in the queue. Every closure needs the current task id and
revision. Direct terminal updates, review reconciliation and merge closeout of
workflow tasks are refused without a workflow closure. Non-workflow tasks retain
their existing behavior.

The existing lifecycle operators may cancel a frontier directly. A current
owner may acknowledge a requested cancellation with its current run token,
or record `failed/cancelled` even when cancellation is already requested or
its seat is no longer eligible. These paths record a failed/cancelled closure
in the same task transaction and never execute a tool. An acknowledgement
requires the instance's bound role seat as well as its claimed run owner.
An unrelated claimant cannot cancel the workflow by selecting that seat's lane.
Restoring a cancellation hold retains a failed workflow's cancelled frontier
and reports `retained` with `detail: workflow_failed`; it does not reopen the task.
Deleting an active frontier task (including through a parent cascade) is refused.
Once the instance has failed or completed, normal approved task deletion is
allowed. The instance and closure trail remain readable after its task is deleted.

## Recovery and limits

Calling `workflow_run(name, instance_id=...)` reads the persisted frontier,
outputs and trail after a restart. Instances freeze their definition, inputs and
bindings, so an installed YAML update cannot change an in-flight run. The seat
must remain eligible in the live team/office/roster at execution time. Instances
and task reads use the caller's tenant fence. Instance snapshots are available
only to their creator and bound role seats, including after completion.

Execution reserves an `executing` marker before invoking the tool. An interrupted
call without a receipt remains uncertain and cannot execute again through the
workflow. The owner can still record a blocked/failed disposition without
executing anything. Capability checks run before reserving the execution marker;
an unexpected execution exception records an indeterminate receipt. A
concurrent cancellation cannot be overwritten by a late tool receipt.
A completed receipt permits retrying the task transition. Approval
pending and pre-execution denial remain retriable through the same approval and
policy checks. Other failures remain `indeterminate`; explicit blocked/failed
closure records the operator's disposition without repeating an effect.

Inputs and results are bounded to 200 KB, and persisted text uses the shared
secret redactor. Secret-bearing inputs are refused; pass artifact references
instead. A result requiring redaction is discarded, leaves execution
indeterminate, and cannot hand off or repeat its effect. The workflow tables
are created at database initialization for backup and schema verification.
Instance storage is coordination metadata alongside ordinary tasks,
not another work queue. It does not grant launch or approval authority and does
not replace review, the Approval Ledger or release gates.
