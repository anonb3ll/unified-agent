# Weekly planning loop

`unified-agent-planning-loop` reads the canonical task ledger and the repository's git tags. It writes a generated projection to `Wiki/Product-1-Approval-Ledger-Plan.md` and a dated decision packet under `Wiki/Decision-Packets/`. It does not change tasks or plan storage.

The forward plan includes every root carrying both `[project-spec]` and `[product-1]`, a status rollup from its descendants, and each root's DoD clauses. Add `[covers: DOD-1]` (or the normalized clause text) to a child task's notes when the relationship is not stated verbatim in its title or spec. Unmatched clauses remain visible as gaps. DoD clauses can be written as bullets under a `DoD:` heading or inline after `DoD:` and separated by semicolons.

Decision packet items are tasks in the `legacy-operator-only` lane or tasks whose notes carry `[decision-packet item]`. Include `Recommendation: yes|no|defer` (or another recorded recommendation) in the task notes. Non-human decisions default to that recommendation after two consecutive generated editions remain undecided. The operator-only decisions never default. Record a decision in the source task with `Decision: yes`, `Decision: no`, or `Decision: defer`.

To generate the weekly artifacts:

```
unified-agent-planning-loop
```

To preview without writing, or propose children for uncovered clauses:

```
unified-agent-planning-loop --no-write
unified-agent-planning-loop --goal "Make evidence exports verifiable" --epic-id 1191 --no-write
```

Goal decomposition writes a proposal note only. A human or the Chief of Staff must accept it and file any tasks; no task is auto-created. Monday 04:00 systemd and launchd timers run the weekly plan and packet generators.

Shared revisions of a live plan are a different seam. `plan_manage` actions `joint_bind`, `joint_propose`, `joint_review`, `joint_decide`, `joint_get`, and `joint_recover` let members of one org unit revise one plan, record decisions, and recover from a stale revision. The contract is in [joint-planning.md](joint-planning.md). It does not schedule work and it does not add a store.

## Current next work

`unified-agent-planning-loop --next-work` prints a read-only view of the cockpit's
`docs/queue/NEXT-WORK.json` against one live task snapshot. It suggests up to two
eligible, non-conflicting tasks and explains skipped rows. On the hub it reads
one SQLite snapshot. On a client with no local task store it reads the same
projection through the authenticated, read-only `POST /api/tasks/next-work-projection`
route. The hub evaluates the supplied manifest against one local snapshot and
returns only the finished view, not task notes or the dependency graph;
`UA_HTTP_HUB_URL` and the client's hub credential must be configured.

The manifest orders work and names collision groups. Live status, claims,
dependencies, and lane come from the hub. Local views show live task titles;
remote views keep manifest titles and report title drift without returning
hub titles for arbitrary requested IDs. A manifest row may set
`human_gate: true` for required human review; free-form `acceptance_gate` text
does not set that gate. A cancelled prerequisite blocks a suggestion unless
its task notes contain `[replaced-by: #N]` and replacement task `#N` is done.
The view is advisory and never claims or dispatches work.
