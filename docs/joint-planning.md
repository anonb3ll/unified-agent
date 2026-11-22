# Joint planning contract

Task #1455. Two agents revise one plan. The weekly planning loop still only writes vault projections. This contract is the shared-revision seam on plans that already exist.

## What it reuses

| Concern | Existing object |
|---|---|
| Plan and steps | `plans`, `plan_steps`, claim, handoff |
| Revision history | `plan_checkpoints` (`context.joint`) |
| Why | goal id and ancestry from #1349 |
| Conversation | comment id from #1423 |
| Team and roles | checked-in `org.yaml` membership. The office lead's role is `lead`; every other member's role is `member` |
| Provenance | `event_log` channels `plan.joint.bound`, `plan.joint.proposed`, `plan.joint.reviewed`, `plan.joint.decided` |

There is no new table and no scheduler. `decompose_into_goal_linked_tasks` still only files tasks.

## Authority

`joint_bind` attaches one active plan to one goal and one org unit. The caller must be a member of that office. The office must have at least two members, because every proposal needs a peer reviewer. A second active joint plan for the same goal is refused, so two agents cannot decompose it into two plans.

Any member may propose a step list or record a decision. A proposal does not change live steps. A different member reviews it:

- `accept` applies the staged steps.
- `changes` keeps the live steps and marks the proposal `changes_requested`. The next proposal replaces it.

The proposer cannot accept their own proposal. An agent outside the bound office is refused on every mutation. Roles are read from the charter on each call, so leaving the office removes authority. The checkpoint still records the role the actor held when they wrote.

## Optimistic revisions

Every mutation after bind carries `expected_revision`. The write is one immediate transaction. If the stored revision has moved, the call raises `JointConflict` and writes nothing. `joint_recover` (and `joint_get`) read the winning checkpoint, the live steps, the why-chain, and the current team. The loser retries against `current_revision` or reviews the proposal that won. A proposal that is still `proposed` can be replaced only by its author; everyone else waits for review. If the author has left the office, any current member may replace it, and review refuses to accept it.

A proposal also records a fingerprint of the live steps. Editing a step directly (`plan_update`, a claim, a retry) after the proposal was staged makes `accept` raise `JointConflict` with "live steps changed" and write nothing. Recover, then propose again against the current steps.

A restart reads the same checkpoint. Nothing about the contract is kept only in process memory.

## Replan rules

Accept may append steps and replace steps that are still `open`, `failed`, or `blocked`. A `doing` or `done` step stays at its index, with its description, dependencies, owner, and status. Dropping or rewriting one fails the accept and leaves the previous steps in place. Any step whose description, dependencies, and owner are unchanged keeps its row, so its id, status, note, retries, and outputs survive. There is no step-count cap beyond the plan's own.

A joint plan may be `active` or `blocked`. When bounded retries escalate a step and block the plan, a peer-reviewed revision that replaces the blocked step lifts the escalation and returns the plan to `active`. A revision that keeps a blocked step leaves the escalation in place.

## Access

Only members of the bound office can act on a joint plan. `joint_get` and `joint_recover` check the caller. The older plan actions (`update`, `checkpoint`, `resume`, `retry`, `claim`, `handoff`, `close`, `delete`, `review`, and the matching `plan_*` tools) refuse a caller outside the office, and refuse a call with no identity. Each write runs the check inside its own immediate transaction, so a concurrent `joint_bind` waits for it rather than slipping between the check and the write. A human can still approve a joint plan's `[waiting-for: plan-approval]` gate by editing the task notes. Callers are identified by the verified hub identity, or by the explicit `agent` argument when no verified identity exists. `plan_get` and `plan_list` stay open for the reviewer's plan-approval scan, which reads without an identity. `plan_get` strips the joint state from checkpoint contexts, so it shows steps but not proposals or decisions.

Joint checkpoints carry forward the previous checkpoint's `memory_ids`, `file_paths`, and `outputs`, so `plan_resume` keeps its recovery pointers. Each mutation returns the view it committed, even if another member writes right after it.

## Decisions

`joint_decide` appends a decision on the checkpoint. The decision stores the goal id and the comment id it cites. Every cited comment must be authored by the caller, so provenance cannot be borrowed. Comment `refs.decision_id` remains a memory-note link. This contract points at comments; it does not invent a second decision store.

## API

All of this is `plan_manage` actions: `joint_bind`, `joint_propose`, `joint_review`, `joint_decide`, `joint_get`, `joint_recover`. No new MCP tool is registered.
