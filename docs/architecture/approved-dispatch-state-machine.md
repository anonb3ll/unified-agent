# Approved-dispatch state machine and authority contract

Status: specification (task #547); coordinator (#548); named-agent routes (#549);
scheduler (#550); e2e smoke + observability (#551).
Owner lane: agent-cursor (spec + coordinator + routes + scheduler + smoke).
Last updated: 2026-08-24.

## Purpose

Make the path from **The operator-approved delegation** to a **named developer
agent run** durable, observable, and fail-closed — without creating a second
task mailbox, without enabling production Kanban/cron autonomy in this lane,
and without logging secrets.

This document is the acceptance contract for tasks #548–#551. Code that
contradicts it is a bug; silent dual queues are out of scope forever.

## Authority (non-negotiable)

| Concern | Authority |
| --- | --- |
| Task / delegation / approval rows | Mac hub `memory.db` only |
| Agent label roster | `UA_AGENT_LABELS` + cockpit roster (PascalCase `Desktop_*` / `Laptop_*`; additive `server_*`) |
| Approval decision | The operator (or an explicit `approval_resolve` by a human steward) |
| Merge / deploy / secrets / Red-tier config | The operator only |
| Process launch | Coordinator via stubbable launch boundary; scheduler poll is opt-in |

Hermes cron and Kanban may **poll** this queue via `dispatch.scheduler`
(`UA_DISPATCH_SCHEDULER_ENABLED`, default off). They must not own a parallel
ready-set. Interactive `/agentname` aliases (#549) must resolve through the
same named-agent router and cannot bypass approval.

## Observable states

States are recorded on the existing child task + `delegations` row +
append-only event/log notes. The `dispatch_runs` table is an **index over
these**, not a second mailbox.

```text
delegated
  → awaiting_legacy_operator_approval
  → approved | denied | cancelled | expired
  → claimed          (atomic single-winner claim)
  → launched         (one run_id minted)
  → running
  → finished | failed | retryable | blocked
```

Terminal outcomes:

- `finished` — child task `done` with `evidence_ref`; delegation callback fired.
- `failed` — non-retryable launch/process failure; operator-visible reason.
- `retryable` — transient launch/timeout; lease released; eligible for reclaim.
- `blocked` — waiting on human/input or dead-lettered after max attempts; not auto-relaunched.
- `denied` / `cancelled` / `expired` — never launch; reason recorded once.

## Transition record (every edge)

Every transition MUST record all of the following (redacted):

| Field | Source |
| --- | --- |
| `actor` | human label (`Operator`) or system actor (`dispatch-coordinator` / `dispatch-scheduler`) |
| `task_id` | child task id |
| `delegation_id` | `delegations.id` when present |
| `agent_label` | resolved named agent (`agent-claude`, …) |
| `run_id` | durable run identifier (minted at `claimed`) |
| `timestamp` | UTC ISO-8601 |
| `evidence_ref` | commit/PR/CI/path pointer when entering `finished` |
| `failure_reason` | short operator string; **never** secrets, prompts, tokens, or raw provider bodies |
| `retry_count` | `delegations.attempts` at transition time (task #551 operator telemetry) |

Forbidden in logs/events/notes: Slack signing secrets, API keys, bearer tokens,
prompt text, provider responses, raw Slack message bodies beyond the already-
redacted command vocabulary.

## Authoritative ready predicate

A task is **ready to claim** iff all hold:

1. Child task exists and is not `done` / `cancelled`.
2. Linked `delegations` row is `pending` (or an explicit in-flight recovery
   lease that has expired).
3. Notes carry an explicit the operator approval marker, e.g.
   `[plan: approved @ <ts>]` **or** an `approvals` row resolved `approve` for
   the exact gated payload the coordinator registered — **not** a Slack
   `/approve` alone without the hub primitive.
4. `to_agent` / notes resolve to a **known** roster label (fail closed on
   unknown / ambiguous).
5. No live `run_id` already claimed for this task (duplicate poll → no-op).

Anything else is refused with an operator-visible reason and **no** process
spawn.

## Fail-closed behaviors (acceptance)

| Case | Required behavior |
| --- | --- |
| Approved once | Exactly one atomic claim; exactly one `run_id` |
| Duplicate poll / double tick | Second claim loses; no second launch |
| Denied | Stay terminal `denied`; never launch |
| Expired approval / stale claim lease | Reclaim or refuse; never double-run |
| Cancelled parent/child | Cancel delegation; no launch |
| Unknown agent label | Refuse; record reason |
| Malformed / missing approval marker | Refuse; record reason |
| Crash mid-launch | Lease reclaimable as `retryable`; no silent success |
| Launched/running lease expiry | Reclaim as `retryable` with `failure_reason=timeout` |
| Max attempts exhausted | Dead-letter `blocked`; not auto-relaunched |
| Quota-exhausted lane | Refuse before the atomic claim (task #1251): no run minted, no attempt burned, delegation stays `pending`; refusal recorded on the transition ledger as `quota_exhausted:lane=<lane>` and noted on the task once per observation |
| Cron/Kanban disabled | Scheduler code may exist; production enablement is the operator-gated (`UA_DISPATCH_SCHEDULER_ENABLED`, default `0`) |

Quota gating (task #1251): `claim_and_dispatch` consults the
`model_quota_status` projection (`quota_observe.build_quota_status`) after
readiness and before the atomic claim. Only a positive
`availability: "unavailable"` observation gates — `unknown` lanes (no
exhaustion evidence) dispatch unchanged, so healthy quota is invisible to the
dispatch path. The scheduler builds the projection once per tick and threads
it through every claim in the batch; the same gate covers operator/UI
dispatch, which shares `claim_and_dispatch`.

## Integration with existing primitives

- **Create:** `delegate()` → child task + memory note + `delegations` row.
- **Approve:** human `approval_resolve` and/or explicit task note marker.
  Slack `/approve` only surfaces pending rows; it must not invent a second
  approval store.
- **Claim/launch:** coordinator (#548) — atomic UPDATE / insert of run
  record; stubbable process boundary.
- **Route:** named-agent/profile resolver (#549).
- **Schedule:** optional poller (#550) over the **same** ready predicate
  (`dispatch.scheduler.scheduler_tick` / CLI `unified-agent-dispatch-scheduler`).
- **Smoke:** #551 proves the loop with a fake worker; no production autonomy.
  Enablement/rollback: `docs/architecture/approved-dispatch-enablement.md`.

## Out of scope for #547–#551

- Enabling Kanban, Hermes cron dispatch, or unattended production launch by
  default (no systemd timer ships enabled for the dispatch scheduler).
- Editing Red-tier config, restarting the hub, merging PRs (except the PR that
  lands the smoke/docs themselves under the operator review).
- Frontier/Slack secrets, cloud second databases.
- Switchyard-style LLM routing (#514 remains parked).
- Production CLI process supervision of real agent binaries (still stubbed
  outside the fake-worker smoke path).

## Implementation pointer (task #548)

Coordinator package: `src/unified_agent/dispatch/` (`claim_and_dispatch`,
`evaluate_ready`, `reclaim_expired_leases`). Durable index table:
`dispatch_runs` (partial unique live run per task). Launch boundary is
stubbable (`dispatch.launch`). Focused tests:
`tests/test_dispatch_coordinator.py`.

## Named-agent routing (task #549)

Canonical resolver: `dispatch.routing.resolve_named_agent` /
`RoutedLaunchBoundary`. Supported launch labels (fail closed otherwise):

| Label | CLI backend | Profile | Host |
| --- | --- | --- | --- |
| `agent-claude` / `agent-laptop-agent1` | `claude` | `developer` | Desktop / Laptop |
| `agent-codex` / `agent-laptop-agent2` | `codex` | `developer` | Desktop / Laptop |
| `agent-agy` / `agent-laptop-agent3` | `agy` | `developer` | Desktop / Laptop |

Interactive aliases (`/agent-claude`, `desktop-agent1`, …) map through the
same resolver. Bare `/claude` / `/codex` / `/agy` are **ambiguous** and refuse.
Workspace binding uses `config.resolve_workspace_binding` (Laptop_* without an
explicit `UA_AGENT_WORKSPACES` entry fails closed). Launch requests carry
`run_id`, `task_id`, bounded redacted task spec, repo/worktree scope, and no
secrets. Scheduler ticks default to ``StubProcessSpawner``. Control Center
Dispatch uses ``CliProcessSpawner`` (native headless CLIs, task #568).
e2e smoke (#551) uses ``FakeWorkerProcessSpawner`` (bounded subprocess,
no real CLIs).

Focused tests: `tests/test_dispatch_routing.py`.

## Scheduler / Kanban poller (task #550)

Module: `dispatch.scheduler` (`scheduler_tick`, `list_ready_candidates`,
`record_run_outcome`, `dead_letter_exhausted`). CLI:
`unified-agent-dispatch-scheduler status|ready|tick|runs|observe|report`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `UA_DISPATCH_SCHEDULER_ENABLED` | `0` | Master kill switch; doctor reports DORMANT when off |
| `UA_DISPATCH_SCHEDULER_MAX_ATTEMPTS` | `3` | Retry ceiling before dead-letter `blocked` |
| `UA_DISPATCH_SCHEDULER_BATCH_LIMIT` | `10` | Max claims per tick (cap 50) |
| `UA_DISPATCH_SCHEDULER_LEASE_TTL_S` | `300` | Claim lease TTL for scheduler launches |

Operator one-shot without flipping production: `tick --force`. Duplicate ticks
and process restarts reclaim expired leases then claim at most one live run per
task. Outcomes append `[dispatch: …]` notes and `agent.dispatch.transition`
events (including `retry_count`). No external notifications are sent by the
scheduler.

Focused tests: `tests/test_dispatch_scheduler.py`.

## E2E smoke + observability (task #551)

Module: `dispatch.worker` (`FakeWorkerProcessSpawner`, `complete_smoke_run`).
Operator view: `coordinator.get_run_observability` / CLI `observe <run_id>`.

The smoke proves: Hermes-style `delegate` → the operator approval fixture →
scheduler claim → named-agent route → isolated fake worker start → completion
callback → task/run evidence. Denied approval and unknown agents refuse
without spawn; duplicate ticks do not double-launch.

Enablement / rollback (dry-run, kill switch, evidence retention):
[`approved-dispatch-enablement.md`](./approved-dispatch-enablement.md).

Focused tests: `tests/test_dispatch_e2e_smoke.py`.

## Verification for this task

Focused contract tests under `tests/test_dispatch_contract_spec.py` assert:

- accepted approval path records the required transition fields (fixture),
- denied / duplicate / unknown-agent cases refuse without launch,
- the ready predicate rejects missing approval markers.

Implementation of the coordinator is **#548**; named-agent routes are **#549**;
scheduler polling/recovery is **#550**; e2e smoke + observability is **#551**.
