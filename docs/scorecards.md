# Agent scorecards and evals (task #1354)

A scorecard says how an agent's recent work went. It reads records the platform
already keeps and adds no table.

| Measure | Source | Notes |
|---|---|---|
| Runs and outcomes | self-pickup run record ([self-pickup-runs.md](self-pickup-runs.md)) | per claimant |
| Delivery rate | runs ending `completed` or `handed_off` ÷ finished runs | `released` and `cancelled` are neutral: counted, not in the denominator |
| Abandon rate | runs ending `abandoned` ÷ the same denominator | |
| Review approval rate | the latest `[review: …]` marker on each delivered task | per task, not per delivery (`unit: "task"`): approved/validated/auto-merged/merged vs rejected/red-tier/rework; unreviewed tasks are counted apart |
| Reopen rate | delivered runs whose task was claimed again afterwards | event order, not wall time |
| Harness health | `executor_attempts.failure_class` | provider quota, iteration budget, zero output and teardown failures are the harness's; `quality_success_rate` excludes them and administrative `killed` attempts |
| Eval pass rate | saved eval runs (below), per suite and model | runs that could not execute are excluded |
| CI first-pass rate | — | `unavailable`: no source attributes PR CI runs to an agent yet |

Every rate carries `numerator`, `denominator` and `sample` (`ok` at five or
more, `small` below, `none` at zero). A rate with no denominator is `null`,
never zero. The default window is 14 days. For run outcomes, delivery, abandon,
review and reopen rates, the cohort is **claims opened within the trailing
window** (`claimed_at >= since`), not finishes recorded in it. A finish
recorded within the window is omitted if its claim predates `since`. An open
claim has no outcome yet and is omitted until a finish is recorded or
abandonment can be inferred. An unclosed run whose task
is `done`, `waiting_review` or `cancelled` is tracked as unclosed, not scored
as abandoned. Executor attempts and eval runs use their own `started_at` and
`at` timestamps within the trailing window.

The executor ledger's history is the reason harness failures are split out:
its only populated stretch was 28 failures in 31 attempts by one codex
executor, retried in a loop. Scored as agent quality, that would have ranked a
broken harness as a bad agent.

## Evals

A suite is a YAML file in `src/unified_agent/evals/` (shipped in the package; override with `UA_EVALS_DIR`): `name` (equal to the
file stem), `version`, and `cases`, each with an `id`, a `task` fixture and a
list of `checks` a grader applies. `task-lifecycle.yaml` is the first suite.

Running a suite is outside the hub: a grader (human or agent) runs the cases
and saves the result with
`observability_manage("eval_record", {"suite", "agent", "results": [{case, passed, detail}], "model"})`.
Every case must be reported exactly once. A run that could not execute is saved
with `harness_error` and no results. Each run is one `eval.run` event on the
`agent.eval` channel of the event log.

## Reading it

- `observability_manage("scorecard", {"agent", "window_days"})`
- `observability_manage("eval_suites", {})` (malformed suites are listed under `errors`)
- `GET /api/scorecards?window_days=14` and the **Agent scorecards** section of
  Runs → Telemetry (`/ui/runs/telemetry`). The response includes `since` and
  `run_cohort: "claimed_at"` to identify the run cohort boundary.

Scorecards do not yet feed dispatch routing: the only router is dispatch lane
A, which is being retired (#1435). A routing consumer belongs with whatever
replaces it.
