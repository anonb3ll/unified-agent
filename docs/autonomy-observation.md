# Autonomy ladder observation (task #1465)

This is a read-only measurement protocol and the first snapshot, not an
autonomy-stage decision. The cockpit build guide
(`~/agent-workspace/docs/company/BUILD-GUIDE.md`)
defines L1 as two weeks with at least 90% dispatch success, zero unattributed
completions, and zero false-FAILED review verdicts. L2 requires a week in
which every `dispatch-ready` task starts without a human prompt. Self-pickup
records arrived with [PR #815](https://github.com/anonb3ll/unified-agent/pull/815),
cost and budget reads with [PR #818](https://github.com/anonb3ll/unified-agent/pull/818),
and scorecards with [PR #820](https://github.com/anonb3ll/unified-agent/pull/820).
No pre-deployment runs are backfilled.

## Baseline: 2026-09-29 00:22–00:24 UTC

The following are live Mac hub reads, not estimates from the old dispatch
tables. They are a short initial sample; counters may change on later reads.

| Source | Observed result | Interpretation |
|---|---:|---|
| `observability_manage(action="run_history", params={"limit":1000})` | 23 runs, `truncated=false`; 17 `dispatch-ready` claim attempts for 11 distinct tasks | Of those attempts, 8 completed, 6 released, 3 remain open. On each task's latest run, 8 are completed and 3 open. A release during takeover is shown separately, never silently treated as delivery or failure. |
| `observability_manage(action="runs_in_flight")` | 4 running, 0 abandoned, 0 unrecorded | Three are `dispatch-ready`; one is in another lane. All four current `doing` tasks have a run record. |
| `observability_manage(action="run_coverage", params={"window_days":7})` | 13 recorded / 184 completions (7.1%); 171 unrecorded; 0 recorder failures | This rolling window includes completions before PR #815 deployed. It does not measure the new recorder's steady-state coverage. |
| `observability_manage(action="scorecard")` | agent-codex 8 completed, 1 released; agent-claude 3 completed, 5 released; agent-cursor 2 completed | Fourteen-day claimant cohort. CI first-pass attribution is unavailable; no harness or eval attempts were recorded. These are agent scorecards, not an L1 dispatch success rate. |
| `observability_manage(action="cost_rollup")` and `budget_status` | 21 finish events, 21 unknown costs, no reported usage; budgets unconfigured | Two finishes (#1436 and #1497) have no matching claim event because their work began before recording deployed. Thus this finish-event count exceeds the 19 finished runs in `run_history`. API cost `0` is not evidence of zero spend; no budget enforcement claim follows from an unconfigured policy. |
| `monitor.delivery_slo(7)` | Dispatch success unavailable (0/0); p90 cycle 74.457 h over 184 completions, 108 using `created_at` fallback; unattributed 2/184 (breach); CI green unavailable | The retired dispatch lane has no terminal run in this window. The two unattributed completions already prevent an L1 pass for this rolling week. |

Evidence examples from that snapshot: task #1389 completed on run
`sp-01M3N6MP3C3RMKY5Q3D3F6JZXG`; tasks #1444 and #1390 were open on runs
`sp-01M3N7M2Y9PRAW7817046BHBA1` and `sp-01M3N8B7F2BCYAHCD9R3BZJXJ3`.
The two unattributed completions are tasks #1381 and #1345. These IDs make
the aggregate report auditable without copying task notes or credentials.

The review note ledger has two *candidate* post-PR-#691 rejection rows that
need human adjudication: [task #1297](https://github.com/anonb3ll/unified-agent/pull/715)
at 2026-09-23 09:16 UTC and [task #1398](https://github.com/anonb3ll/unified-agent/pull/782)
at 2026-09-25 11:53 UTC, both saying `no git worktree found` and both later
merged. Neither is classified as a false-FAILED verdict on this evidence
alone. There were no further `pytest timed out` rejection notes after
[PR #691](https://github.com/anonb3ll/unified-agent/pull/691) fixed that known
misclassification. The false-FAILED count is **unknown**, not zero.

**Current verdict:** L1 has not passed: the seven-day unattributed-completion
criterion is breached, dispatch success is unavailable, false-FAILED review
coverage is unresolved, and two prospective seven-day windows have not elapsed.
L2 is unavailable: a `task_claim` record proves a claim but not whether a
human prompted it. No scheduler-started dispatch run appears in this sample.
Neither rung is promoted by this document.

## Prospective cohorts and denominator

Start UTC observation at **2026-09-29 00:00** after the three prerequisite
deployments. Snapshot daily; assess L1 on two complete consecutive UTC weeks:
`[2026-09-29, 2026-10-06)` and `[2026-10-06, 2026-10-13)`. Assess L2 on a
complete week only after start provenance is recorded. Do not pause feature
delivery while the clock runs.

Keep the existing `/api/delivery_slo` dispatch metric unchanged: it counts
terminal `dispatch_runs` by creation date, and reports unavailable when none
exist. Self-pickup is an additional, explicitly named measure, **not** a
replacement label for dispatch success. For each week:

1. Select `run_history` claims with `claimed_at` inside `[start,end)` and
   `lane="dispatch-ready"`. Fetch every page; refuse to certify a rate if a
   response is truncated. Count claim attempts by `completed`, `handed_off`,
   `released`, `blocked`, `cancelled`, `abandoned`, and still open. A renewal is
   not a new attempt. Include `claim_source` and `observed_from`; renewal-only
   records must be separated because their true start predates observation.
2. Deduplicate by `task_id` for a task-level delivery cohort. The denominator
   for the **resolved-task completion rate** is distinct tasks whose latest
   outcome by the assessment cutoff is terminal; the numerator is those
   completed (`done`). Show blocked and cancelled as separate reasons. A
   release without a later claim leaves the task unresolved and belongs in
   right-censored WIP. Report still-open tasks as right-censored WIP,
   outside that ratio, with their count and age. Also report all attempts,
   including releases, so handoffs cannot disappear behind a successful
   successor run. Never call this ratio “dispatch success.”
3. Report all `dispatch-ready` tasks that entered the lane during the week,
   including ready-but-unclaimed work. A claim-only cohort omits neglected
   tasks and can make delivery look better. If lane-entry history cannot be
   reconstructed from task events, mark that coverage **unknown**, display
   the current open/ready stock, and withhold a full-cohort conclusion.
4. Report `run_coverage` with numerator/denominator and recorder failures.
   Missing run records are unknown outcomes, not successes. Scorecards describe
   claimant outcomes and sample sizes; cost rollups must retain unknown usage.
5. Report the existing SLO's dispatch numerator/denominator, p90 and fallback
   count, unattributed numerator/denominator, and CI provider state. Its
   `window_days=7` read is trailing, not an arbitrary fixed-week query. Capture
   that read at 00:00 UTC on each week end and record the response timestamp;
   only a boundary read represents the named UTC week. If the boundary read is
   missed, label that week's SLO fields unavailable instead of relabeling a
   later sliding window as the fixed week. Review
   every `rejected`/`failed` verdict against exact-head test evidence before
   classifying it as true, false, or unresolved. A timeout is inconclusive.
6. For L2, capture an independently verifiable start trigger for **every**
   ready-lane task, including those not yet claimed: scheduler or autonomous
   agent wake versus human prompt, with task/run/session evidence IDs. The
   current `claim_source=task_claim|task_update` is an API path, not trigger
   provenance. Until this is available, L2 remains unavailable regardless of
   how many self-pickup tasks complete.

For any rate, publish the numerator, denominator, time bounds, excluded and
unknown counts, and source version. Zero eligible observations means
`unavailable`; a short, nonempty cohort is `insufficient-sample` for an exit
decision even if its observed ratio exceeds 90%. Do not turn a small observed
rate into a policy threshold or an autonomy promotion. If any source is
truncated or unavailable, say so and preserve the raw counts.

## Daily reproducible read

On the live hub as a developer identity, save the timestamp and returned
`since`, `count`, and `truncated` fields with these read-only calls. Take the
`/api/delivery_slo?window_days=7` snapshot at the two UTC week boundaries
(2026-10-06 and 2026-10-13 at 00:00 UTC), recording the actual `fetched_at`
and API window bounds. A late read is a new trailing window, not evidence for
the fixed week. The other reads may run daily:

```text
observability_manage(action="run_history", params={"since":"2026-09-29T00:00:00+00:00","limit":1000})
observability_manage(action="runs_in_flight")
observability_manage(action="run_coverage", params={"window_days":7})
observability_manage(action="scorecard", params={"window_days":14})
observability_manage(action="cost_rollup")
observability_manage(action="budget_status")
GET /api/delivery_slo?window_days=7
task_list(status="open", lane="dispatch-ready", fields="compact")
```

Read the two verdict candidates' task notes and PR check histories for the
false-FAILED audit. Link the dated snapshot to its exact run IDs, task IDs,
PRs, and deployment commit. `run_history` has a limit but no page cursor: if
`truncated=true`, use bounded `since` intervals or task-specific queries and
deduplicate run IDs before reporting. The weekly planning loop may link the
snapshot from its decision packet; it cannot change the source task ledger or
decide an autonomy stage. The existing Settings → Usage page (`/ui/settings/usage`) projects the dispatch SLO
from `src/unified_agent/delivery_slo.py` through `monitor.delivery_slo` and
`GET /api/delivery_slo`; this document records the complementary self-pickup
cohort while that projection remains honest about absent dispatch data.
