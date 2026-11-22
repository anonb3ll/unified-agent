# Self-pickup runs: what is running, how it ended

Task #1436. Self-pickup is how work actually gets done: an agent claims a task
with `task_claim` (or moves it to `doing` with `task_update`), does the work,
and moves the task on. Until this change the platform recorded task state but
not the run, so it could not say what was in flight, how long a run took, how
it ended, or whether an agent died mid-run
([execution-substrate-survey.md](execution-substrate-survey.md) §3b).

The goal is visibility, not replacement. Nothing here schedules, assigns or
gates work.

## What is recorded

Claim and finish events, plus an optional cancellation request, live on the
durable, hash-chained `event_log`, channel `agent.run.selfpickup`. No new run table.

| Event | Written by | Carries |
|---|---|---|
| `selfpickup.claimed` | `task_claim` (new claim), `task_update` entering `doing` | `run_id` (`sp-<ULID>`), `task_id`, `agent`, `claim_source`, `observed_from`, `claimed_at`, `lock_expires_at`, `lane`, `project` |
| `selfpickup.finished` | `task_update` leaving `doing` or releasing the claim; a takeover claim | `run_id`, `outcome`, `to_status`, `finished_by`, `finished_at`, `wall_s`, `usage`, `usage_reported`, `claim_recorded` |
| `selfpickup.cancel_requested` | `task_update(request_cancel=True)` on a current run | `run_id`, `task_id`, `requested_by`, `requested_at` |

Both kinds are declared on the existing `run` object (`object_model.py`)
alongside `dispatch.*`, and `unified-agent://runs/sp-<ULID>` resolves a
self-pickup run through the same reader as dispatch runs.

### Outcomes

| Outcome | When |
|---|---|
| `completed` | task moved to `done` |
| `handed_off` | task moved to `waiting_review` |
| `blocked` | task moved to `blocked` |
| `released` | task went back to `open`, or `release_claim=True` |
| `cancelled` | task cancelled |
| `abandoned` | another agent claimed the task after this run's lease lapsed |

A finish also carries `reason` (free text) and `reason_code` (task #1841):
`lease_expired` on every lease-lapse path (queue hygiene, the doctor's
stale-lock reclaim, a takeover after the lapse), `capacity_ceiling` when the
run reported that its harness hit a plan usage limit (see
[Capacity ceilings](#capacity-ceilings-task-1841)), and `null` otherwise.

### Usage and cost

A finishing `task_update` may pass `run_usage`, any subset of
`{provider, model, input_tokens, output_tokens, cost_usd, cost_complete, billing, phases,
contended, capacity}`. `billing` is `api`, `subscription` or `local`; `phases` maps a
phase name to seconds; `contended` flags a queued or rate-limited subscription
lane (task #1348, see [budgets.md](budgets.md)). Every field is
present on every finish; unreported ones are `null` and `usage_reported` is
`false`. `wall_s` (claim to finish) is always computed. Subscription CLI
sessions do not report usage today, so most runs will carry typed nulls:
unknown cost is shown as unknown, never as zero. `cost_complete: false` marks
a known dollar amount as a lower bound when some invocation was unpriced;
that uncertainty survives later heartbeat, finish and historical overlays.
Budgets retain known USD plus the unknown-cost reserve. An executed attempt
with no measured tokens or price can report only `cost_complete: false`;
quantitative values remain null while its unknown-cost marker is retained.

A run can also report usage while it is still open (task #1681). A live
renewal, `task_claim` by the agent that already holds the task, may pass
`run_usage` as a heartbeat. It is the run's cumulative usage so far, not a
delta: each heartbeat replaces the fields it reports and keeps the rest. The
latest snapshot is kept in `run_usage_heartbeats`, one row per run, and is
never appended to the event log, so renewing every poll tick adds nothing to
the hash chain. `run_history`, `get_run`, the run resource and the
`self_pickup` entries of `runs_in_flight` show it on an open run with
`usage_reported: true`, and subscribers to the run resource are notified when
it changes, including through the hub's poller when another process wrote it.
The row is deleted when the run finishes, when its task is deleted, or when an
event-log prune removes its claim. The run's finish event carries it whichever way the
run ends: completed, handed off, released, or abandoned after a lapsed lease.
A finishing `run_usage` overrides only the fields it reports. A heartbeat is
refused on a new claim or after the lease lapsed.

Authenticated late accounting uses `task_claim(usage_only=True)` with an
immutable `expected_run_id`. The original actor alone may report spend on
that task/run, including after cancellation, expiry or takeover. It appends
a deduplicated `selfpickup.usage_reported` event and keeps the latest totals
in the usage cache. Closed-run readers overlay that cache; the original
finish event, outcome and successor remain unchanged. Numeric totals never
decrease, and finished usage is excluded from open-run spend.

## Capacity ceilings (task #1841)

**What stops a run at a ceiling.** Nothing inside the platform caps a run's
wall time. The hub's limits are leases: `task_claim` defaults to 3600 s
(`config.TASK_CLAIM_DEFAULT_LEASE_SECONDS`) and callers may pass a longer
`timeout_seconds` (agent-claude claims carry 4 h). A lapsed lease is renewed
for a fixed 60 minutes while the claimant label was active on the hub in the
last 60 minutes (`queue_hygiene.renew_live_claims`), and released otherwise by
queue hygiene (every 900 s, from the review-pipeline cron) or by the doctor's
stale-lock reclaim. The ceiling that ended agent-claude runs on 2026-10-01
and 2026-10-02 was external: the operator's Claude plan usage limit. The
harness stops; the hub only sees silence, so the run's recorded `wall_s` is
claim to lease expiry plus any liveness renewals plus the sweep interval,
not the moment work stopped. On the hub ledger the lease-variant abandons
cluster at 14,424 to 14,595 s (4 h lease plus sweep lag), 18,027 to 18,052 s
(one 60-minute renewal more) and 22,000 to 27,000 s (renewed while other
sessions of the same label stayed active). The 24,009 to 24,075 s cluster on
2026-10-02 is 13 runs claimed at 03:54 to 03:55 and closed together at
10:35 to 10:36 as `completed`, so their equal durations come from one batch
close, not from a limit.

**How the harness signals it.** Claude Code prints the limit as text, for
example `Claude AI usage limit reached|<reset epoch>`, `5-hour limit reached
∙ resets 3am`, `You've hit your limit · resets 10am` or `Weekly limit
reached`; a headless `claude -p` run ends unsuccessfully with that text.
`quota_observe.classify_failure_reason` classifies all of these as
`provider_quota_exhausted`. The hub runs no Claude executor, so an interactive
Claude session's limit reaches the hub only if the session reports it.

**Recording.** `run_usage.capacity` is an optional object, accepted on a
finishing `task_update`, on a `task_claim` heartbeat and on `usage_only`
accounting:

| Field | Type | Meaning |
|---|---|---|
| `limit_reached` | bool | the harness reported its usage limit reached |
| `remaining_pct` | number 0 to 100 | share of plan capacity left, when the harness knows it |
| `resets_at` | string | when the limit resets, as the harness states it |
| `source` | string | who observed it (`executor`, `claude_cli`, ...) |
| `detail` | string | the harness message, up to 200 characters |

A run whose last report says `limit_reached: true` finishes with
`reason_code: "capacity_ceiling"` and a reason starting `capacity ceiling:`
when queue hygiene or the doctor releases its lapsed lease, when another agent
takes it over after the lapse, or when the run itself releases or blocks the
task. The outcome is unchanged (`abandoned`, `released`, `blocked`); a
completion, hand-off, cancellation or operator termination is never relabelled.
Without a recorded signal the lease variant stands: duration and timing are
never used to guess. The executor daemon adds the signal itself when a
backend fails with a plan usage-limit message, read from its error or the
last 400 characters of its output (`backends.capacity_from_run_result`). A
transient "rate limit reached" is not a ceiling and records nothing. Across
retries of one run the latest attempt decides: one that reports capacity
replaces the earlier value, and one that reports none (it succeeded, or failed
for another reason) clears a reached limit with `limit_reached: false`.

Capacity reports merge field by field: a heartbeat sending only
`{"remaining_pct": 0}` keeps an earlier `limit_reached: true`, and only an
explicit `limit_reached: false` clears it.

**Warning before the run dies.** `runs_in_flight` lists `capacity_warnings`:
open runs whose latest heartbeat reports `limit_reached: true` or
`remaining_pct` at or below 15 (`run_records.CAPACITY_WARN_PCT`), with the
task, run, agent, remaining share and reset time. A run whose lease already
lapsed stays listed with `run_state: "abandoned"` and its `abandon_reason`,
since it may have died at the ceiling; a live one says `run_state: "running"`. A session that knows its
remaining capacity should send it on its lease renewal:
`task_claim(id, agent, run_usage={"capacity": {"remaining_pct": 12}})`.
Leases are not lengthened for any of this.

## Cancellation and fencing

`task_update(id, request_cancel=True)` flags only the current run of a `doing`
task. The claimant or a configured lifecycle operator may request it. The
request is idempotent, appears immediately in `run_history` and the run
resource, and does not itself change task status. Executor backends poll the
flag while running, including over the hub from remote clients; they stop the
child at the next poll. The request is advisory for self-pickup agents that do
not use the executor. A new claim clears the prior run's flag; renewing the
cancelled run is refused inside `task_claim`'s write transaction.

Pass `expected_run_id` to `task_update` to reject a stale writer after its run
has been superseded. This is optional for existing callers; it is not a
default authorization or ownership check. Callers should retain the
`current_run_id` from `task_claim` and supply it on finishing updates. Executor
renewal supplies the same token to `task_claim` on remote hub clients, and an
executor does not start when the claim response lacks a run token.
A lease expires at its expiry instant: `lock_expires_at <= now`. A non-null
expiry is active only while `lock_expires_at > now`; a null expiry remains
indefinite. Claim admission, WIP, budget reserves, policy claim bindings,
next-work, stale listing, and recovery use that same boundary. Cleanup can
renew an active claimant's lapsed lease under the existing liveness checks;
otherwise it releases the claim.
An expired lease opens a new run even when the same agent reclaims it; the old
run token cannot renew or finish the successor. Delegation `release` and
`retry` require the claim generation returned by `delegate_claim`.
When a run is cancelled or superseded, its executor retires the in-flight
delegation only if its monotonic claim generation still matches. The retry
`attempts` counter can decrease on release and is not a fencing token.

## Guarantees and limits

- **Same transaction.** Claims, transitions, and cancellation events are
  written with their task row changes. Initial claims and transitions without
  a bound run token remain best-effort: a recorder failure rolls back its own
  savepoint and logs a `run_recorder` trace after the task commits. Once a run
  token is bound, a takeover or lifecycle transition requires its recorder
  action to succeed; a failure rolls back the entire task mutation. This
  prevents old tokens surviving an acknowledged takeover, release, or finish.
  Cancellation also requires its event to commit with the task flag.
- **Idempotent.** `event_log.dedupe_key` allows one claim per (task, agent,
  claim instant) and one finish per run.
- **Renewals are not runs.** A live renewal writes nothing. A renewal with no
  open run (a claim made before this shipped) opens a run marked
  `observed_from: "renewal"`, dated from the renewal, not backdated.
- **No backfill.** History before deployment is not fabricated. Tasks already
  in `doing` show as `unrecorded` until they finish or are renewed.
- **Paths that bypass `task_update`** (direct SQL, `task_delete`) cannot write
  a finish. Their open runs read as `abandoned` with reason `task_left_doing`
  or `task_missing`.

## Reads

All three are `observability_manage` actions.

- `runs_in_flight()`: the single answer to "what is running now". One item per
  task, listing every ledger that sees it: `self_pickup`, `executor`
  (`executor_attempts` with no finish), `dispatch` (live `dispatch_runs`) and
  `node` (claimed `node_dispatch` with no receipt). Each item's `state` is
  `running`, `abandoned` (open run whose lease lapsed, claimant changed, or task
  left `doing` unrecorded) or `unrecorded` (a `doing` task no ledger covers).
  `coverage` counts `doing` tasks each way. A task never claimed does not
  appear at all, so a claimed-but-never-finished task is distinguishable from
  one never claimed. `dispatch` lists every run holding a launch slot, with
  its actual state (a cancelled run with unconfirmed termination or a
  runtime-ceiling block keeps its slot). Each item's `claim_path` names how its
  current claim got in flight (`dispatcher`, `executor`, `direct_claim`,
  `task_update` or `unrecorded`); a stale executor attempt that started before
  the current claim does not decide it.
  `wip` reports the fleet cap `task_claim` and `task_update` into `doing`
  enforce (`fleet`: live claims, the `unclaimed_doing` part of them, soft and
  hard caps, state), the dispatcher's live-slot ceiling (`dispatch`)
  and `running_by_path` (tasks #1838, #1886; rules in
  [memory-tasks-vault.md](memory-tasks-vault.md#queue-lanes-and-wip-caps-task-705-actor-model-883)).
- `run_history(task_id=None, agent=None, since=None, limit=100)`: runs newest
  first with outcome, duration and usage; `truncated` says when `limit` cut it.
  This is the input for #1348 (cost ledger), #1354 (scorecards) and #1465
  (delivery SLO). Consume it; do not re-derive runs.
- `run_coverage(window_days=7, details=True, limit=200)`: of tasks completed in the window, how many
  have a recorded run, plus `recorder_failures`. `pct` is `null` for an empty
  cohort. This is the measurement for platform-invariants ADR clause 13b.

## Execution paths covered

| Path | How it is covered |
|---|---|
| Self-pickup via `task_claim` | claim and finish written here |
| Self-pickup via `task_update(status="doing")` | claim written here (`claim_source: task_update`) |
| Executor daemons (lane B) | claim through `task_claim` over the hub, so covered here; also listed from `executor_attempts` |
| Governed LangGraph workflow | runs on an existing `task_claim`; finishes through `task_update`, so covered here |
| bug_worker | `task_claim` / `task_update`, covered here |
| Dispatch lane A | `dispatch_runs`, listed in `runs_in_flight`; retirement is #1435 |
| Node transport | `node_dispatch`, listed in `runs_in_flight` |
