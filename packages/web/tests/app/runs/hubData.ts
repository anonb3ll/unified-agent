/**
 * Hub answers for the Runs area, copied from the Python handlers' real
 * shapes (`runs_projection.list_runs`, `tools/workflow.workflow_list` via
 * `/api/overview`, `tools/langgraph.langgraph_checkpoints`,
 * `webui/cc_runs.routine_list`, `tools/cycles`, `/api/dispatch_receipts`,
 * `/api/telemetry`, `/api/scorecards`, `/api/executor-delegation`,
 * `/api/flow-metrics`, `/api/execution-graph`, `/api/events`).
 *
 * Keys are "METHOD /path". Values are `{ status?, json }` (Playwright's
 * `mockHub`); `forVitest` converts them for `mockFetch`.
 */
import type { MockHandler } from "../support/fetchMock";

export type Reply = { status?: number; json: unknown };

const NOW = Date.now();
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString();

export const RUN_ID = "01JD7Z3K4M5N6P7Q8R9S0T1V2W";

export function runRow(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run-42-a1b2c3",
    kind: "dispatch",
    task_id: 42,
    agent: "Desktop_Agent2",
    status: "finished",
    outcome: "finished",
    started_at: iso(90),
    ended_at: iso(60),
    lease_expires_at: null,
    abandoned: false,
    abandon_reason: null,
    delegation_id: null,
    evidence_ref: "ev-1",
    failure_reason: null,
    failure_class: null,
    termination_outcome: null,
    links: { self: { kind: "run", id: "run-42-a1b2c3", uri: "ua://run/run-42-a1b2c3" }, evidence: [] },
    ...overrides,
  };
}

export const RUNS = {
  runs: [
    runRow({ run_id: "run-43-live", task_id: 43, status: "running", outcome: null, started_at: iso(12), ended_at: null }),
    runRow({ run_id: "sp-01JD7Z00000000000000000001", kind: "selfpickup", task_id: 44, agent: "Desktop_Agent1", status: "open", outcome: null, started_at: iso(5), ended_at: null, claim_source: "self" }),
    runRow(),
    runRow({ run_id: "run-40-failed", task_id: 40, status: "failed", outcome: "failed", failure_reason: "tests failed", started_at: iso(300), ended_at: iso(280) }),
    runRow({ run_id: "01JD7Z9SESSION0000000000001", kind: "session", task_id: null, agent: "Mac_Hermes", status: "completed", outcome: "completed", session_kind: "node", exit_status: "completed", started_at: iso(400), ended_at: iso(380) }),
  ],
  count: 5,
  truncated: false,
  next_cursor: null,
  limit: 50,
};

export const WORKFLOWS = {
  workflows: [
    { name: "smoke-echo", description: "Echo a message through the hub to check workflows run.", steps: ["echo"], inputs: ["message"] },
    { name: "review-and-PR", description: "Review a task's branch and open a pull request.", steps: ["run_review", "notify"], inputs: ["task_id"] },
    { name: "weekly-export", description: "Export the task list.", steps: ["export_tasks"], inputs: [] },
  ],
};

export const OVERVIEW = { workflows: WORKFLOWS };

export const WORKFLOW_EVENTS = {
  events: [
    { seq: 10, ulid: "01JD7Z0000000000000000000A", channel: "agent.workflow.updated", payload: { kind: "workflow", ref_id: "smoke-echo", summary: "run started (1 steps)" }, originator: "operator", created_at: iso(30) },
    { seq: 11, ulid: "01JD7Z0000000000000000000B", channel: "agent.workflow.updated", payload: { kind: "workflow", ref_id: "smoke-echo", summary: "run done (1/1 steps done)" }, originator: "operator", created_at: iso(29) },
  ],
  count: 2,
  cursor: "01JD7Z0000000000000000000B",
  seq_cursor: 11,
};

export const CHECKPOINTS = {
  checkpoint_db: "/srv/ua/langgraph.sqlite",
  exists: true,
  count: 2,
  checkpoints: [
    { thread_id: `ua-lg:${RUN_ID}`, checkpoint_ns: "", checkpoint_id: "1ef-0002", parent_checkpoint_id: "1ef-0001", type: "msgpack" },
    { thread_id: `ua-lg:${RUN_ID}`, checkpoint_ns: "", checkpoint_id: "1ef-0001", parent_checkpoint_id: null, type: "msgpack" },
  ],
};

export function routine(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    title: "Weekly dependency check",
    notes: null,
    trigger: "cron",
    schedule: "0 9 * * 1",
    lane: "default",
    project: "unified-agent",
    domain: "hobby",
    untrusted: 0,
    concurrency: "skip",
    catch_up: "last",
    paused: 0,
    cursor_at: iso(60 * 24),
    created_at: iso(60 * 24 * 30),
    updated_at: iso(60 * 24),
    org_id: null,
    activity_gate: "always",
    activity_gate_scope: "org",
    recent_runs: [
      { id: 31, fire_key: "cron:2026-11-09T09:00", scheduled_for: iso(60 * 24), status: "created", reason: null, subject_key: null, task_id: 501, created_at: iso(60 * 24), window_start: null, gate_matched: null },
      { id: 30, fire_key: "cron:2026-11-02T09:00", scheduled_for: iso(60 * 24 * 8), status: "skipped", reason: "overlap", subject_key: null, task_id: null, created_at: iso(60 * 24 * 8), window_start: null, gate_matched: null },
    ],
    next_run_at: new Date(NOW + 3 * 86_400_000).toISOString(),
    ...overrides,
  };
}

export const ROUTINES = {
  routines: [routine(), routine({ id: 8, title: "Triage the inbox", trigger: "api", schedule: null, paused: 1, concurrency: "queue", recent_runs: [], next_run_at: null })],
  routines_enabled: true,
};

export function cycle(overrides: Record<string, unknown> = {}) {
  return {
    id: "c0ffee123456",
    task_id: 42,
    plan_id: null,
    delegation_id: null,
    session_id: null,
    state: "executing",
    max_rework: 2,
    rework_count: 0,
    max_time_s: 3600,
    max_tokens: 100000,
    max_retries: 3,
    max_cost_cents: 1000,
    used_time_s: 1200,
    used_tokens: 42000,
    used_retries: 1,
    used_cost_cents: 230,
    allowed_capabilities: ["task_update"],
    stop_reason: null,
    pr_url: null,
    version: 3,
    created_by: "operator",
    created_at: iso(120),
    updated_at: iso(10),
    ...overrides,
  };
}

export const CYCLES = {
  cycles: [cycle(), cycle({ id: "deadbeef0001", task_id: 40, state: "blocked", stop_reason: "autonomy budget exhausted: tokens", used_tokens: 120000, version: 5 }), cycle({ id: "feedface0002", task_id: 39, state: "completed", pr_url: "https://github.com/example/repo/pull/9", version: 8 })],
};

export const CYCLE_DETAIL = {
  cycle: cycle(),
  transitions: [
    { id: 1, cycle_id: "c0ffee123456", from_state: "plan", to_state: "delegated", idempotency_key: "k1", actor: "operator", reason: null, created_at: iso(110) },
    { id: 2, cycle_id: "c0ffee123456", from_state: "delegated", to_state: "executing", idempotency_key: "k2", actor: "Desktop_Agent2", reason: null, created_at: iso(100) },
  ],
};

export const RECEIPTS = {
  receipts: [
    { run_id: "run-42-a1b2c3", task_id: 42, delegation_id: null, agent_label: "Desktop_Agent2", state: "finished", actor: "operator", lease_expires_at: null, failure_reason: null, failure_class: null, evidence_ref: "ev-01JD7Z", termination_outcome: "delivered", created_at: iso(90), updated_at: iso(60), terminal: true, has_evidence: true },
    { run_id: "run-40-failed", task_id: 40, delegation_id: null, agent_label: "Desktop_Agent2", state: "failed", actor: "operator", lease_expires_at: null, failure_reason: "tests failed", failure_class: "quality", evidence_ref: null, termination_outcome: null, created_at: iso(300), updated_at: iso(280), terminal: true, has_evidence: false },
  ],
  count: 2,
  terminal_count: 2,
};

export const TELEMETRY = {
  waterfall: {
    traces: [],
    spans: [
      { trace_id: "trace-lg-abc", span_id: "span-1", kind: "tool", outcome: "success", duration_ms: 120, agent: "Desktop_Agent2", task_id: 42, usage: { tokens_total: 1200, cost_usd: 0.0123 }, evidence_ref: { ulid: "01JD7ZEVIDENCE00000000001", created_at: iso(20) } },
      { trace_id: "trace-lg-abc", span_id: "span-2", kind: "model", outcome: "error", duration_ms: 900, agent: "Desktop_Agent2", task_id: 42, usage: { tokens_total: 300, cost_usd: null }, evidence_ref: null },
    ],
    summary: { traces: 1, spans: 2, duration_ms: 1020, tokens_total: 1500, cost_usd: 0.0123 },
  },
  exception_digest: {
    exceptions: [{ trace_id: "trace-lg-abc", kind: "model_error", severity: "high", count: 1, message: "the model refused the request", task_id: 42, evidence_ref: { ulid: "01JD7ZEVIDENCE00000000002", created_at: iso(19) } }],
    total: 1,
    normal_spans_omitted: 1,
    scanned_spans: 2,
  },
  cursor: null,
  source: { channel: "agent.telemetry", event_count: 2 },
};

const metric = (value: number | null, n: number) => ({ value, numerator: value === null ? 0 : Math.round(value * n), denominator: n, sample: n === 0 ? "none" : n < 5 ? "small" : "ok" });

export const SCORECARDS = {
  window_days: 14,
  run_cohort: "claimed_at",
  min_sample: 5,
  agents: [
    {
      agent: "Desktop_Agent2",
      runs: { finished: 12, outcomes: { delivered: 10, abandoned: 2 } },
      delivery_rate: metric(0.83, 12),
      abandon_rate: metric(0.17, 12),
      review_approval_rate: { ...metric(0.9, 10), unreviewed: 2 },
      reopen_rate: metric(0.1, 10),
      harness: { attempts: 12, harness_failures: 1, failure_classes: { timeout: 1 }, quality_success_rate: metric(0.75, 4) },
      evals: [{ suite: "core", suite_version: "3", model: null, runs: 4, harness_errors: 0, pass_rate: metric(1, 4) }],
      ci_first_pass_rate: { value: null, status: "unavailable", detail: "not recorded" },
    },
  ],
  suites: [{ name: "core", cases: 20 }],
  suite_errors: [],
};

export const EXECUTOR = {
  delegations: [{ id: 12, task_id: 42, from_agent: "operator", to_agent: "Desktop_Agent2", status: "running", deadline: iso(-60) }],
  recent_failures: [{ id: 9, task_id: 40, from_agent: "operator", to_agent: "Desktop_Agent2", status: "error", deadline: null }],
  recent_sessions: [{ id: "s-1", agent: "Desktop_Agent2", kind: "executor", status: "completed", model: "gpt-5", task_id: 42 }],
  stale_worktrees: [{ path: "/srv/worktrees/task-40", reason: "kept after failure", detail: "3 days old" }],
  counts: { pending: 0, running: 1, completed: 14, cancelled: 1, error: 1 },
  health: { check: { capability: "active_executor_service", status: "LIVE", detail: "heartbeat 12s ago" } },
};

export const FLOW_METRICS = {
  cycle_time: { p50_s: 5400, p90_s: 86400 },
  lead_time: { p50_s: 7200, p90_s: 172800 },
  wip_age: { buckets: [{ label: "<1d", count: 3 }, { label: "1-3d", count: 1 }], oldest_s: 200000 },
  attribution_coverage: { total_done: 20, attributed_done: 18, coverage_ratio: 0.9 },
  window_days: 30,
  as_of: iso(1),
};

export const EXECUTION_GRAPH = {
  nodes: [
    { id: "task:42", type: "task", task_id: 42, label: "Fix the login form", state: "doing" },
    { id: "task:40", type: "task", task_id: 40, label: "Upgrade the parser", state: "blocked" },
  ],
  links: [],
  runs: [],
  counts: { tasks: 2, delegations: 1, runs: 2 },
};

export const LINEAGE_42 = {
  task: { id: 42, title: "Fix the login form" },
  lineage: {
    delegations: [{ id: 12, status: "running", from_agent: "operator", to_agent: "Desktop_Agent2", task_id: 42 }],
    dispatch_runs: [{ id: "run-42-a1b2c3", task_id: 42, status: "finished" }],
    autonomous_cycles: [{ id: "c0ffee123456", task_id: 42, status: "executing" }],
    refs: { pr_url: "https://github.com/example/repo/pull/12", branch: "task/42-login", worktree_path: null },
  },
  empty: false,
};

/** Realistic answers for every Runs read. */
export const REALISTIC: Record<string, Reply> = {
  "GET /api/runs": { json: RUNS },
  "GET /api/overview": { json: OVERVIEW },
  "GET /api/events": { json: WORKFLOW_EVENTS },
  "GET /api/langgraph/checkpoints": { json: CHECKPOINTS },
  "GET /api/routines": { json: ROUTINES },
  "GET /api/cycles": { json: CYCLES },
  "GET /api/cycles/c0ffee123456": { json: CYCLE_DETAIL },
  "GET /api/dispatch_receipts": { json: RECEIPTS },
  "GET /api/telemetry": { json: TELEMETRY },
  "GET /api/scorecards": { json: SCORECARDS },
  "GET /api/executor-delegation": { json: EXECUTOR },
  "GET /api/flow-metrics": { json: FLOW_METRICS },
  "GET /api/execution-graph": { json: EXECUTION_GRAPH },
};

/** Empty but successful answers. */
export const EMPTY: Record<string, Reply> = {
  "GET /api/runs": { json: { runs: [], count: 0, truncated: false, next_cursor: null, limit: 50 } },
  "GET /api/overview": { json: { workflows: { workflows: [] } } },
  "GET /api/events": { json: { events: [], count: 0, cursor: null, seq_cursor: null } },
  "GET /api/langgraph/checkpoints": { json: { checkpoint_db: "/srv/ua/langgraph.sqlite", exists: false, checkpoints: [] } },
  "GET /api/routines": { json: { routines: [], routines_enabled: false } },
  "GET /api/cycles": { json: { cycles: [] } },
  "GET /api/cycles/c0ffee123456": { json: CYCLE_DETAIL },
  "GET /api/dispatch_receipts": { json: { receipts: [], count: 0, terminal_count: 0 } },
  "GET /api/telemetry": { json: { waterfall: { traces: [], spans: [], summary: {} }, exception_digest: { exceptions: [], total: 0 } } },
  "GET /api/scorecards": { json: { agents: [], window_days: 14 } },
  "GET /api/executor-delegation": { json: { delegations: [], recent_failures: [], recent_sessions: [], stale_worktrees: [], counts: {} } },
  "GET /api/flow-metrics": { json: { cycle_time: { p50_s: null, p90_s: null }, lead_time: { p50_s: null, p90_s: null }, wip_age: { buckets: [], oldest_s: null }, window_days: 30 } },
  "GET /api/execution-graph": { json: { nodes: [], links: [], runs: [], counts: {} } },
};

/** Every read fails: 500s, a 200 carrying `error`, and a hub without the routines route. */
export const FAILING: Record<string, Reply> = {
  "GET /api/runs": { status: 500, json: { error: "database is locked" } },
  "GET /api/overview": { json: { workflows: { workflows: [], error: "workflow_list failed: boom" } } },
  "GET /api/events": { status: 503, json: { error: "event log unavailable" } },
  "GET /api/langgraph/checkpoints": { json: { checkpoint_db: "", exists: false, count: 0, checkpoints: [], error: "langgraph_checkpoints failed: disk I/O error" } },
  "GET /api/routines": { status: 404, json: { error: "Not Found" } },
  "GET /api/cycles": { json: { cycles: [], error: "autonomous_cycle_list failed: boom" } },
  "GET /api/cycles/c0ffee123456": { status: 404, json: { error: "no autonomous cycle with id 'c0ffee123456'" } },
  "GET /api/dispatch_receipts": { json: { error: "dispatch delivery receipts unavailable", code: "dispatch_receipts_unavailable", receipts: [] } },
  "GET /api/telemetry": { status: 500, json: { error: "boom" } },
  "GET /api/scorecards": { status: 500, json: { error: "boom" } },
  "GET /api/executor-delegation": { json: { delegations: [], recent_failures: [], stale_worktrees: [], error: "delegation_list failed: boom" } },
  "GET /api/flow-metrics": { status: 500, json: { error: "boom" } },
  "GET /api/execution-graph": { status: 500, json: { error: "boom" } },
};

export function forVitest(table: Record<string, Reply>): Record<string, MockHandler> {
  return Object.fromEntries(Object.entries(table).map(([k, v]) => [k, { status: v.status, body: v.json }]));
}
