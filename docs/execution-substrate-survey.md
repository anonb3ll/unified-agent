# Execution substrate survey

**Task:** #1432 (platform remodel prerequisite).
**Author:** agent-hermes, 2026-09-26.
**Source revision surveyed:** `95444440` (desktop checkout `~/dev/unified_agent`).
**Nature:** read-only. This document changes no code and authorizes no refactor.

## 1. Why this survey exists

A platform-remodel recommendation claimed that unified-agent has "six or more
execution substrates, each re-implementing claim/lease/retry/receipt/cancel."
That count was inferred **partly from filenames**. A convergence plan built on a
filename is worthless, so this survey replaces inference with fact.

The question it must answer: **how many schedulers are there really, and what
does each own?**

## 2. Method, and its limits

Evidence used:

1. Module contracts — each module's own docstring, which states what it owns and
   what it deliberately does not.
2. Schemas — the `CREATE TABLE` definitions in `storage.py` for every state table.
3. Config gates — the `UA_*` switches that enable each substrate in `config.py`.
4. **Live service state on the Mac hub** — `launchctl list` plus each
   LaunchAgent's scheduling keys. This is what distinguishes "installed" from
   "actually running".

Limits, stated honestly:

- Findings come from module contracts, schema and live service state. **No
  runtime tracing and no profiling were performed.**
- I did not read every line of `dispatch/supervise.py` or the executor daemon
  loop.
- Whether two paths ever contend for the *same* task in practice is inferred
  from code paths, not observed.

## 3. What is actually running on the hub (2026-09-26)

Read live via `launchctl list` on the Mac hub:

| LaunchAgent | State | Scheduling |
|---|---|---|
| `com.unified-agent.hub` | running (pid 12171) | resident |
| `com.unified-agent.executor-codex` | **running (pid 12484)** | `KeepAlive=true`, `RunAtLoad=true` |
| `com.unified-agent.executor-agy` | **running (pid 17403)** | `KeepAlive=true`, `RunAtLoad=true` |
| `com.unified-agent.node` | running (pid 13703) | resident |
| `com.unified-agent.dispatch-tick` | fires periodically | **`StartInterval=120`** (every 2 minutes) |

Gates set in the hub's `service.env`: `UA_DISPATCH_SCHEDULER_ENABLED`,
`UA_DISPATCH_REAL_SPAWN`, `UA_DISPATCH_MAX_LIVE_RUNS`, `UA_EXECUTOR_AGENT`.

**Conclusion: two dispatch paths are installed and enabled — but see section 3b.
Neither is currently carrying traffic.** A periodic dispatch tick (every 120 s)
and two resident executor daemons (one per backend). The processes are live; the
work is not flowing through them.

## 3b. The ledgers are empty

The table above shows what is *installed*. The state tables show what has
actually *run*. Queried read-only against the hub database on 2026-09-26:

| Ledger | Rows | Last activity | Contents |
|---|---|---|---|
| `dispatch_runs` (path A) | 10 | **2026-08-27** | finished 4, retryable 3, failed 2, blocked 1. Tasks 609–630. |
| `executor_attempts` (path B) | 31 | **2026-09-13** | **failure 28**, waiting_review 3. Task 990 alone has 5 attempts. |
| `node_dispatch` (path C) | **0** | never | The device queue has never been used. |

Meanwhile the platform completed roughly **330 tasks between 2026-09-15 and
2026-09-26** — 62 on 09-19, 48 on 09-23, 29 on 09-25 alone.

**None of that work appears in any ledger.** A spot check of the ten most
recently completed tasks (including #1415, merged the same day) returns
`in_dispatch = 0` and `in_executor = 0` for every one.

**So the honest headline is not "two engines duplicate each other." It is: the
platform records task state, but not execution.**

Precision matters here, and an earlier draft of this section overstated it —
Devin Review flagged it on PR #799 and the flag is correct. What **does** get
written:

- `task_claim` (`tools/tasks.py:1812-1844`) sets `claimed_by`, `claimed_at` and
  `lock_expires_at` on the task row and emits an `agent_events` row of kind
  `task` — but only for a **new** claim, not a renewal, and only when the claim
  goes through `task_claim`.
- `task_update` records `completed_at` on completion.

What is **missing**, and it is the part that matters:

- **No run record.** A claim timestamp plus a completion timestamp is not a run:
  no duration, no outcome, no cost, and no record of what the agent did.
- **Coverage is partial by construction.** An agent that sets status to `doing`
  through generic `task_update` rather than `task_claim` leaves `claimed_by`
  null and writes no claim event. This survey's own author did exactly that while
  claiming #1432 — the canonical `claimed-by:` token had to go in the notes
  field because the column stayed null.
- **Abandonment is invisible.** An agent that claims and dies leaves `claimed_by`
  set and the task in `doing`; nothing records that the run ended without
  finishing. Only a lapsed `lock_expires_at` hints at it.
- **No single in-flight answer.** `dispatch_runs`, `executor_attempts` and
  `node_dispatch` each answer part of the question; none answers it whole.
- **No cost or usage data at all.**

So the gap is real, but it is a gap in **coverage and outcome**, not a total
absence of records. Self-pickup is the working model — it produced ~330 tasks in
twelve days — and the goal is to make it visible, not to replace it.

The consequences reach further than tidiness:

- L2's "self-dispatch on truthful capacity" has no capacity data to read.
- #1348 (cost ledger) and #1354 (evals/scorecards) would be built on an empty
  table. **They have an unrecorded prerequisite: execution must be recorded at
  all.**
- L1's exit test — "90% dispatch success" — cannot be measured, because dispatch
  is not the path the work takes.

**Update (task #1436, 2026-09-28):** self-pickup now writes a run record on
claim and finish, and `observability_manage(action="runs_in_flight")` answers
"what is in flight" across all four ledgers. See
[self-pickup-runs.md](self-pickup-runs.md).

## 4. Substrate inventory

### A. Approved-dispatch pipeline — `dispatch/`

- **Owns:** the approval-to-dispatch transition. Consumes an approved delegation
  exactly once, mints a durable `run_id`, records transitions on the event log.
- **Trigger:** the `dispatch-tick` LaunchAgent, `StartInterval=120`. Gated by
  `UA_DISPATCH_SCHEDULER_ENABLED`.
- **State:** `dispatch_runs`.
- **Claim:** atomic `claim_and_dispatch`, then `lease_owner` / `lease_expires_at`.
- **Retry:** `UA_DISPATCH_SCHEDULER_MAX_ATTEMPTS` plus dead-lettering;
  `reclaim_expired_leases` on each tick.
- **Cancel:** state `cancelled`. **`dispatch_cancel` / `dispatch_resume` govern
  this path.**
- **Components:** `coordinator`, `scheduler`, `routing`, `launch`, `supervise`,
  `receipt`, plus a fake `worker` used only for e2e smoke.
- **In flight?** Partially — `dispatch_runs` answers for path A alone.

`routing.py` resolves roster labels and interactive `/agentname` aliases to an
agent's native headless CLI, developer profile and execution identity, and fails
closed on unknown or ambiguous input.

### B. Active Executor daemon — `executor/`

- **Owns:** the task lifecycle for autonomous background execution — delegation
  claim, task claim, worktree isolation, **lock renewal**, failure/backoff
  policy, the finish-to-review transition, and crash recovery on startup.
- **Trigger:** resident LaunchAgents `executor-agy` and `executor-codex`,
  `KeepAlive=true`. Configured by `UA_EXECUTOR_AGENT`, `UA_EXECUTOR_BACKEND`.
- **State:** `executor_attempts`.
- **Claim:** delegation claim + task claim + lock renewal.
- **Retry:** `UA_EXECUTOR_MAX_ATTEMPTS`, `UA_EXECUTOR_BACKOFF_BASE_S`,
  `UA_EXECUTOR_BACKOFF_FACTOR`, plus a circuit breaker.
- **Cancel:** outcome `killed`. **`dispatch_cancel` does not reach this path.**
- **In flight?** Partially — `executor_attempts` answers for path B alone.

This path is **deliberately split**, and `backends.py` documents the split
explicitly: the daemon owns the lifecycle, a backend owns only execution ("turn
a prompt into a run inside an already-prepared working directory and report what
happened"). That split is why adding Codex was additive rather than invasive.

### C. Node transport — `node_queue.py` (hub) + `node_agent.py` (device)

- **Owns:** the ua-node contract. A node connects **out** to the hub over HTTP;
  the device never listens on a port. It registers host facts, heartbeats,
  long-polls its own queue, runs claimed work, and posts **one receipt per item**.
- **Trigger:** `com.unified-agent.node`, resident.
- **State:** `nodes`, `node_dispatch`.
- **Claim:** `claimed_at` + `lease_expires_at` per item; `receipt_json` non-NULL
  is the idempotency gate.
- **Cancel:** `cancel_requested` flag.
- **In flight?** Yes, for node work.

**This is a transport, not an independent scheduler.** `node_dispatch` *can*
carry a `run_id` and link into path A's run ledger, but the linkage is
**conditional**: `node_queue.enqueue` takes `run_id` and `task_id` as optional
fields (`node_queue.py:446-450`), so directly-requested node work produces a
receipt with no linked dispatch run. Treat the linkage as available, not
guaranteed. Its docstring says it "runs claimed work through an existing executor
backend". Identity is bound: a node name is tied to the identity that first
registered it, so one device cannot drain another's queue.

### D. Routines — `routines.py`

- **Owns:** durable, opt-in scheduled **task creation**.
- **Trigger:** cron / api / webhook, with `concurrency` (skip|queue) and
  `catch_up` (none|last|all).
- **State:** `routines` plus fire receipts.
- **Claim:** a **unique fire key** — idempotency, not a lease. One transaction
  owns each tick, its task rows and its receipts, so a repeated tick or API retry
  is harmless.
- **Cancel:** the `paused` flag.
- **In flight?** **No — it creates tasks, it does not execute them.**

### E. LangGraph workflow engine — `langgraph_*` (7 modules, 4 roles)

The central question was whether these are seven engines. **They are not.** They
are one execution core plus an adapter, two workflow definitions and a sink —
each documented as such:

| Module | Role |
|---|---|
| `langgraph_pilot` | The execution plane. "Deliberately has no MCP registration, service entrypoint, task-state writer, or approval resolver." |
| `langgraph_dispatch` | The governed adapter to Unified-Agent authority. "Intentionally a hub-side library, not an MCP tool or daemon." Uses the authenticated request context, the profile/policy-wrapped tool registry, the canonical approval queue, the task/delegation lifecycle and the shared idempotency store. |
| `langgraph_workflow` + `_dispatch` | Workflow definition: a governed plan→build→verify→merge chain; "uncertain writes are never replayed". |
| `langgraph_note` + `_dispatch` | Workflow definition: one terminal, approval-governed progress receipt with no task-completion effects. |
| `langgraph_observability` | Telemetry sink. External exporters "are sinks only: they never authorize effects, mutate tasks, or become a source of truth." |

So: **1 engine, 1 boundary, 2 workflow shapes, 1 sink.** LangGraph is responsible
only for execution and checkpoint state; authority stays with the hub.

### F. Autonomous cycles — `tools/cycles.py`

- **State machine + tool API, not a scheduler.** It has no timer of its own; it is
  driven by callers through the `autonomous_cycle_*` tools.
- **State:** `autonomous_cycles`, `autonomous_cycle_transitions`.
- **Cancel:** `autonomous_cycle_kill`.

### G. bug_worker — bounded specialized cycle

- **Owns:** orchestration for one bounded SYSTEM bug patch cycle.
- **Trigger:** gated by `UA_BUG_WORKER_ENABLED` (default **off**);
  `UA_BUG_WORKER_MAX_ATTEMPTS`.
- Has its own `eligibility`, `gates` and `state` modules.

### H. Ingestion boundaries — not execution substrates

`zulip_worker.py`, `ts_worker/worker.py`, `github_app_worker.py`. Each is an
**ingestion boundary first**: fetch bounded messages, classify, write durably to
the event log, and advance a cursor **only after that write succeeds**. `zulip`
keeps cursors and health in `zulip_cursors` / `zulip_worker_health`; `ts_worker`
writes `ts.inbound` events with a surface-scoped dedupe key.

These do not schedule or execute work, and should not be counted as substrates.

### I. Read/query layers — not substrates

- `monitor.py` — the query side of the agent activity index. "No LLM anywhere:
  every function is plain SQL plus date arithmetic."
- `dispatch/receipt.py` — "read-only delivery accountability receipts over
  existing dispatch records."

## 5. Overlap matrix

Which substrate re-implements which concern:

| Substrate | claim | lease | retry | receipt | cancel | Own state table |
|---|---|---|---|---|---|---|
| **A** dispatch | atomic `claim_and_dispatch` | `lease_owner`/`lease_expires_at` | max_attempts + dead-letter | state machine | `cancelled`; **dispatch_cancel reaches it** | `dispatch_runs` |
| **B** executor | delegation + task claim | **lock renewal** | max_attempts + backoff + circuit breaker | outcome enum | `killed`; **dispatch_cancel does not reach it** | `executor_attempts` |
| **C** node | `claimed_at` | `lease_expires_at` | `attempts` | **`receipt_json` as idempotency gate** | `cancel_requested` | `node_dispatch` |
| **D** routines | unique fire key | — | repeated tick harmless | fire receipt | `paused` | `routines` |
| **E** langgraph | approval-governed | — | never replay uncertain writes | checkpoint | — | checkpoints |
| **G** bug_worker | eligibility + gates | — | max_attempts | state | — | bug worker state |

**Claim, lease, retry and cancel are implemented four separate times** (A, B, C,
and D/G variants).

## 6. How many schedulers?

Structurally: **three.** The dispatch scheduler (A), the executor daemons (B, two
instances), and routines (D). A, B and the LangGraph pilot (E) are the three
execution engines.

Functionally the count is misleading, and section 3b is why:

- **A and B are structurally duplicative** — both claim work, hold a lease, launch
  a named agent CLI, retry with backoff and finish to review, each with its own
  config namespace, state table and crash recovery.
- **Neither is the path real work takes.** A last ran 2026-08-27; B last ran
  2026-09-13. Both are enabled and idle.
- **A is the one that is genuinely retired.** the operator confirmed 2026-09-26 that
  the dispatch tick "was meant to be retired." Nothing replaced it, and nothing
  writes down what replaced it.

So the live question is not "which of two engines wins." It is: **what records
the work that actually happens?**

**Update 2026-09-28 — A is retired (task #1435).** Self-pickup is now recorded
(`run_records.py`, task #1436, deployed at `e92a75a1`), so the precondition in
8.2/8.4 is met. On the hub `com.unified-agent.dispatch-tick` was
`launchctl disable`d and booted out at 21:01Z (plist kept on disk; reversible
with `launchctl enable` + `bootstrap`). The structural count is now **two**
schedulers: the executor daemons (B) and routines (D). `dispatch/` is marked
deprecated in its package docstring, the `UA_DISPATCH_*` gates are documented
as retired in `docs/configuration.md`, and `system_doctor` reports
`dispatch_tick_timer` and `dispatch_scheduler` DORMANT "retired by #1435"
(MISCONFIGURED if the tick is ever loaded again).

## 7. Hazards found

**7.1 — CORRECTED: the double-execution window is closed, not live.**
This section originally described a live hazard. Devin Review flagged it on PR
#799 and the flag is correct. `coordinator.py:1619-1652` (task #602) renews the
lease for any run in `launched` or `running` whose recorded pid is genuinely ours
and whose wall-clock ceiling has not been passed, so a real spawn no longer falls
through to the timeout branch at lease expiry. The passage in
`dispatch/supervise.py` that this survey quoted describes the **historical** bug
that #602 fixed — the narrow Popen-to-launched window — not current behaviour.

What remains is deliberate: when the pid cannot be proven ours, or the wall-clock
ceiling is passed, the run is timed out rather than signalled, because the code
refuses to signal a bare pid. That is a conservative choice, not a defect.

**There is no live double-execution hazard in path A.** The recommendation that
depended on it (8.5) is withdrawn.

**7.2 — Three tables answer "what ran for this task".**
`dispatch_runs` (states + lease), `executor_attempts` (backend + outcome),
`node_dispatch` (node + receipt). All three key on `task_id`, all three carry an
agent and a status. **None is the single answer to "what is in flight right
now."**

**7.3 — Two config namespaces for one job.** `UA_DISPATCH_*`
(max_live_runs, run_max_s, lease_ttl, batch_limit, max_attempts) and
`UA_EXECUTOR_*` (max_attempts, backoff_base, backoff_factor, timeout, circuit
breaker). An operator tuning capacity or timeouts must know which path owns the
work, and nothing tells them.

## 8. Recommendation (revised after the ledger findings)

**8.1 — Retire A. Done 2026-09-28 (task #1435)** — LaunchAgent disabled and
booted out, `dispatch/` deprecated in place (code kept), gates documented as
retired; see the update at the end of section 6. Original recommendation:
**The intent is confirmed.** The dispatch tick was meant to be
retired and was not; it has sat enabled and idle since 2026-08-27 while the
platform ran ~330 tasks through another path. Retiring it means disabling the
LaunchAgent *and* recording that `dispatch/` is deprecated, so the next reader
does not re-derive it as live infrastructure. Disable first, confirm nothing
regresses, then deprecate — do not delete the code in the same step.

**8.2 — The real work is: record the self-pickup path.** This is what the
platform has "been meaning to" do. Self-pickup is the working model — it produced
~330 tasks in twelve days — so the goal is **visibility, not replacement.** A run
record must be written when an agent claims a task and when it finishes, so that
"what is in flight", "what did it cost" and "did it succeed" become answerable.
This is a prerequisite for #1348 and #1354, not a competitor to them.

**8.3 — My earlier hypotheses are withdrawn.** The LangGraph layer is not
duplication (one engine, clean boundary) and needs no convergence work. The node
transport is a transport, not a scheduler. The ingestion workers are cursors.
`node_dispatch` is not a viable single substrate — excellent receipt/cancel
model, but no approval consumption, no routing, no backoff, no lifecycle
ownership. **None of these is the problem.**

**8.4 — Convergence becomes a consequence, not a goal.** Once self-pickup writes
a run record, A can be retired outright and B becomes the single executor — with
one lease/retry/cancel implementation and one capacity config namespace. Sequence
the convergence *after* the recording, because recording tells you whether B is
adequate before you delete A.

**8.5 — Withdrawn.** This section previously recommended fixing the
double-execution window in `dispatch/supervise.py` before giving path B more
traffic. That window was already closed by task #602 (see the corrected 7.1);
Devin Review caught the error on PR #799. The remaining concern about path B is
empirical rather than architectural, and it stands: **28 of 31 recorded attempts
failed**, and task 990 was retried five times inside ten minutes. Investigate
that failure rate before path B carries more traffic — it is the only populated
evidence we have about whether the executor daemons work at all.

## 9. What this survey did not establish

- **Where the ~330 completed tasks' execution is recorded, if anywhere.** The
  spot check covered the ten most recent completions; a full sweep was not run.
  A record may exist somewhere not yet inspected — but it is not in the three
  ledgers, and `agent_events` carries only 4 `executor` events in total.
- Why B's failure rate is 28/31. That is a separate investigation, and arguably
  more urgent than any convergence work.
- The exact fire-receipt table name for routines.
- Whether the executor daemons behave correctly under real load, since they have
  not carried any since 2026-09-13.
- Nothing here authorizes a refactor. Retiring A and recording self-pickup each
  need their own task.
