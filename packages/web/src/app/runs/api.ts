/**
 * Runs area data layer (spec §7: new fetchers live here).
 *
 * Every shape below is copied from the hub's Python handlers, named beside
 * each type. A read that answers 200 with an `error` field is a failed read
 * (the hub's `_safe` wrapper reports failures in-band), so `getJson` throws
 * for it; only a separate `errors` list (some sub-reads failed) is kept as a
 * partial read.
 *
 * NEEDS-FROM-HUB routes live on the unmerged branch
 * `task/2480-cc2-hub-operations` (`webui/cc_runs.py`,
 * `docs/control-center-api.md`): `GET /api/routines`,
 * `POST /api/action/routine/add|pause|resume|run` and
 * `POST /api/action/langgraph/run`. A hub without them answers a bare 404,
 * which the pages show as "Not available on this hub yet".
 */
import type { DelegationRow, ExecutorDelegationPayload } from "../../lib/api/executor";
import type { DispatchReceiptRow, DispatchReceiptsPayload } from "../../lib/api/dispatch";
import type { TelemetryException, TelemetryPayload, TelemetrySpan } from "../../lib/api/telemetry";
import type { ExecutionGraphNode, ExecutionGraphPayload, FlowMetricsPayload, LineagePayload } from "../../lib/flow/types";

export type { DelegationRow, DispatchReceiptRow, ExecutionGraphNode, TelemetryException, TelemetrySpan };

/** A failed read: the HTTP status (200 for an in-band `error`) and the hub's words. */
export class HubReadError extends Error {
  status: number;
  hubMessage: string;
  constructor(what: string, status: number, hubMessage: string) {
    super(`${what} HTTP ${status}${hubMessage ? `: ${hubMessage.slice(0, 200)}` : ""}`);
    this.name = "HubReadError";
    this.status = status;
    this.hubMessage = hubMessage;
  }
}

/** A read route answered 404: this hub does not serve it (yet). */
export function isRouteMissing(error: unknown): boolean {
  return error instanceof HubReadError && error.status === 404;
}

async function getJson<T>(what: string, path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { method: "GET", credentials: "same-origin", headers: { Accept: "application/json" }, signal });
  const text = await response.text().catch(() => "");
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = null;
  }
  const obj = body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : null;
  const hubError = obj && typeof obj.error === "string" ? obj.error : "";
  if (!response.ok) throw new HubReadError(what, response.status, hubError || text.slice(0, 200));
  if (!obj) throw new HubReadError(what, response.status, "the answer could not be read");
  if (hubError) throw new HubReadError(what, 200, hubError);
  return obj as T;
}

/** Query keys owned by this area. */
export const runsKeys = {
  all: ["cc2", "runs"] as const,
  runs: (limit: number) => ["cc2", "runs", "list", limit] as const,
  runCandidates: (taskId: number) => ["cc2", "runs", "candidates", "sessions", taskId] as const,
  workflows: ["cc2", "runs", "workflows"] as const,
  workflowEvents: ["cc2", "runs", "workflow-events"] as const,
  checkpoints: (runId: string) => ["cc2", "runs", "checkpoints", runId] as const,
  routines: ["cc2", "runs", "routines"] as const,
  cycles: ["cc2", "runs", "cycles"] as const,
  cycle: (id: string) => ["cc2", "runs", "cycle", id] as const,
  receipts: ["cc2", "runs", "receipts"] as const,
  telemetry: ["cc2", "runs", "telemetry"] as const,
  scorecards: ["cc2", "runs", "scorecards"] as const,
  executor: ["cc2", "runs", "executor"] as const,
  flowMetrics: ["cc2", "runs", "flow-metrics"] as const,
  executionGraph: ["cc2", "runs", "execution-graph"] as const,
  lineage: (taskId: number) => ["cc2", "runs", "lineage", taskId] as const,
};

/** Every write this area makes, by hub route. */
export const WRITE_PATHS = {
  /** `{name, inputs}`; 200 `WorkflowRunResult` (`webui/routes.action_workflow`). No idempotency key. */
  workflow: "/api/action/workflow",
  /** `{operation, ...}`; start answers 201 (`webui/routes.action_cycle`). */
  cycle: "/api/action/cycle",
  /** NEEDS-FROM-HUB (#2480): `{task_id, query, run_id?, engine?}`. */
  langgraphRun: "/api/action/langgraph/run",
  /** NEEDS-FROM-HUB (#2480). */
  routineAdd: "/api/action/routine/add",
  routinePause: "/api/action/routine/pause",
  routineResume: "/api/action/routine/resume",
  /** NEEDS-FROM-HUB (#2480): `Idempotency-Key` header, body `{routine_id}`. */
  routineRun: "/api/action/routine/run",
  /** `{id, append_note}` (`webui/routes.action_task`). */
  task: "/api/action/task",
} as const;

// ---- runs (runs_projection.list_runs) ----------------------------------------

export type RunRow = {
  run_id: string;
  kind: "dispatch" | "selfpickup" | "session";
  task_id: number | null;
  agent: string | null;
  status: string;
  outcome: string | null;
  started_at: string | null;
  ended_at: string | null;
  lease_expires_at?: string | null;
  abandoned?: boolean;
  abandon_reason?: string | null;
  session_kind?: string | null;
  failure_reason?: string | null;
  links?: Record<string, unknown>;
};

export type RunsPage = { runs: RunRow[]; count: number; truncated: boolean; next_cursor: string | null; limit: number };

export async function fetchRuns(opts: { limit: number; cursor?: string | null; kind?: string; taskId?: number }, signal?: AbortSignal): Promise<RunsPage> {
  const params = new URLSearchParams({ limit: String(opts.limit) });
  if (opts.cursor) params.set("cursor", opts.cursor);
  if (opts.kind) params.set("kind", opts.kind);
  if (opts.taskId !== undefined) params.set("task_id", String(opts.taskId));
  const page = await getJson<Partial<RunsPage>>("runs", `/api/runs?${params}`, signal);
  return {
    runs: Array.isArray(page.runs) ? page.runs : [],
    count: typeof page.count === "number" ? page.count : 0,
    truncated: page.truncated === true,
    next_cursor: page.next_cursor ?? null,
    limit: typeof page.limit === "number" ? page.limit : opts.limit,
  };
}

// ---- workflows (tools/workflow.workflow_list / workflow_run) ----------------

export type WorkflowDef = { name: string; description: string | null; steps: string[] | null; inputs: string[]; error?: string };

/** The reviewed workflow list is served only inside `GET /api/overview` (`workflows`). */
export async function fetchWorkflows(signal?: AbortSignal): Promise<WorkflowDef[]> {
  const overview = await getJson<{ workflows?: { workflows?: WorkflowDef[]; error?: string } }>("overview", "/api/overview", signal);
  const block = overview.workflows;
  if (!block || typeof block !== "object") throw new HubReadError("workflows", 200, "the hub did not include the workflow list");
  if (typeof block.error === "string" && block.error) throw new HubReadError("workflows", 200, block.error);
  return (Array.isArray(block.workflows) ? block.workflows : []).map((w) => ({
    name: String(w.name),
    description: w.description ?? null,
    steps: Array.isArray(w.steps) ? w.steps.map(String) : null,
    inputs: Array.isArray(w.inputs) ? w.inputs.map(String) : [],
    ...(w.error ? { error: String(w.error) } : {}),
  }));
}

export type WorkflowStepReport = { name: string; tool: string; status: string; attempts: number; error: string | null; result?: unknown };

export type WorkflowRunResult = {
  workflow: string;
  status: string;
  steps?: WorkflowStepReport[];
  outputs?: Record<string, unknown>;
  instance_id?: string;
  frontier?: unknown;
  trail?: unknown;
};

export type HubEvent = {
  seq?: number;
  ulid: string;
  channel: string;
  payload: { kind?: string; ref_id?: string | null; summary?: string | null; [k: string]: unknown };
  originator?: string | null;
  created_at?: string;
};

export type EventsPage = { events: HubEvent[]; count: number };

export const WORKFLOW_CHANNEL = "agent.workflow.updated";

/** `GET /api/events` (`tools/events.event_log_query`); `since` is an exclusive ULID cursor. */
export async function fetchEvents(opts: { channels: string[]; since?: string; limit: number }, signal?: AbortSignal): Promise<EventsPage> {
  const params = new URLSearchParams({ channels: opts.channels.join(","), limit: String(opts.limit) });
  if (opts.since) params.set("since", opts.since);
  const page = await getJson<Partial<EventsPage>>("events", `/api/events?${params}`, signal);
  return { events: Array.isArray(page.events) ? page.events : [], count: typeof page.count === "number" ? page.count : 0 };
}

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/** The smallest ULID at `ms`: a `since` cursor for "events after this time". */
export function ulidFloor(ms: number): string {
  let t = Math.max(0, Math.floor(ms));
  let out = "";
  for (let i = 0; i < 10; i++) {
    out = CROCKFORD[t % 32] + out;
    t = Math.floor(t / 32);
  }
  return `${out}${"0".repeat(16)}`;
}

// ---- LangGraph (tools/langgraph, webui/cc_runs.langgraph_run) ---------------

export type LangGraphCheckpoint = {
  thread_id?: string | null;
  checkpoint_ns?: string | null;
  checkpoint_id?: string | null;
  parent_checkpoint_id?: string | null;
  type?: string | null;
};

export type CheckpointsPayload = { checkpoint_db?: string; exists: boolean; count: number; checkpoints: LangGraphCheckpoint[] };

/** The hub caps a checkpoints read at 100 rows. */
export const CHECKPOINT_LIMIT = 100;

export async function fetchCheckpoints(runId: string, signal?: AbortSignal): Promise<CheckpointsPayload> {
  const params = new URLSearchParams({ limit: String(CHECKPOINT_LIMIT) });
  if (runId) params.set("run_id", runId);
  const p = await getJson<Partial<CheckpointsPayload>>("checkpoints", `/api/langgraph/checkpoints?${params}`, signal);
  const checkpoints = Array.isArray(p.checkpoints) ? p.checkpoints : [];
  return { checkpoint_db: p.checkpoint_db, exists: Boolean(p.exists), count: checkpoints.length, checkpoints };
}

/** `WorkflowRunner._result` plus the route's `session_id`; 202 adds `approval_id`. */
export type GraphRunResult = {
  status: string;
  run_id?: string;
  thread_id?: string;
  graph_name?: string;
  graph_version?: string | number;
  claimed_by?: string;
  pending_hop?: string | null;
  output?: Record<string, unknown> | null;
  interrupted_by?: { approval_id?: number | string | null; hop?: string; node_key?: string; tool_name?: string } | null;
  reason?: string | null;
  session_id?: string;
  approval_id?: number | string | null;
  run_status?: string;
  code?: string;
  tool?: string;
};

export const GRAPH_HOPS = ["plan", "build", "verify", "merge"] as const;
export const RUN_ID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

// ---- routines (routines.routine_list + cc_runs._next_run_at) ----------------

export type RoutineRun = {
  id: number;
  fire_key?: string;
  scheduled_for?: string | null;
  status: "created" | "queued" | "skipped" | "suppressed" | string;
  reason?: string | null;
  subject_key?: string | null;
  task_id?: number | null;
  created_at?: string;
  window_start?: string | null;
  gate_matched?: number | null;
};

export type Routine = {
  id: number;
  title: string;
  notes?: string | null;
  trigger: "cron" | "api" | "webhook" | string;
  schedule?: string | null;
  lane?: string;
  project?: string;
  domain?: string;
  concurrency: "skip" | "queue" | string;
  catch_up: string;
  paused: number | boolean;
  cursor_at?: string;
  created_at?: string;
  updated_at?: string;
  activity_gate?: string;
  activity_gate_scope?: string;
  recent_runs?: RoutineRun[];
  next_run_at: string | null;
};

export type RoutinesPayload = { routines: Routine[]; routines_enabled: boolean };

/** How many receipts per routine the list asks for (hub maximum 20). */
export const ROUTINE_HISTORY = 10;

export async function fetchRoutines(signal?: AbortSignal): Promise<RoutinesPayload> {
  const p = await getJson<Partial<RoutinesPayload>>("routines", `/api/routines?recent_runs=${ROUTINE_HISTORY}`, signal);
  return { routines: Array.isArray(p.routines) ? p.routines : [], routines_enabled: p.routines_enabled === true };
}

/** `routine_fire` receipt (`routines.routine_fire`). */
export type RoutineFireResult = { status: "created" | "queued" | "skipped" | "suppressed" | string; task_id: number | null };

// ---- cycles (tools/cycles, webui/api._public_cycle) -------------------------

export type Cycle = {
  id: string;
  task_id: number | null;
  plan_id?: string | null;
  delegation_id?: number | null;
  session_id?: string | null;
  state: string;
  max_rework?: number;
  rework_count?: number;
  max_time_s?: number;
  max_tokens?: number;
  max_retries?: number;
  max_cost_cents?: number;
  used_time_s?: number;
  used_tokens?: number;
  used_retries?: number;
  used_cost_cents?: number;
  allowed_capabilities?: string[] | null;
  stop_reason?: string | null;
  pr_url?: string | null;
  version?: number;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
};

export type CycleTransition = {
  id: number;
  cycle_id: string;
  from_state: string | null;
  to_state: string;
  idempotency_key?: string;
  actor?: string;
  reason?: string | null;
  created_at: string;
};

/** The hub's default (and this page's) cycles read size. */
export const CYCLE_LIMIT = 100;

export async function fetchCycles(signal?: AbortSignal): Promise<{ cycles: Cycle[]; capped: boolean }> {
  const p = await getJson<{ cycles?: Cycle[] }>("cycles", `/api/cycles?limit=${CYCLE_LIMIT}`, signal);
  const cycles = Array.isArray(p.cycles) ? p.cycles : [];
  return { cycles, capped: cycles.length >= CYCLE_LIMIT };
}

export async function fetchCycle(id: string, signal?: AbortSignal): Promise<{ cycle: Cycle; transitions: CycleTransition[] }> {
  const p = await getJson<{ cycle?: Cycle; transitions?: CycleTransition[] }>("cycle", `/api/cycles/${encodeURIComponent(id)}`, signal);
  if (!p.cycle) throw new HubReadError("cycle", 200, "the hub did not return the cycle");
  return { cycle: p.cycle, transitions: Array.isArray(p.transitions) ? p.transitions : [] };
}

// ---- history pages ----------------------------------------------------------

export async function fetchReceipts(signal?: AbortSignal): Promise<DispatchReceiptsPayload> {
  const p = await getJson<DispatchReceiptsPayload>("dispatch_receipts", "/api/dispatch_receipts", signal);
  return { ...p, receipts: Array.isArray(p.receipts) ? p.receipts : [] };
}

/** The legacy pane's window: the newest 500 spans. */
export const TELEMETRY_LIMIT = 500;

export async function fetchTelemetry(signal?: AbortSignal): Promise<TelemetryPayload> {
  const p = await getJson<Partial<TelemetryPayload>>("telemetry", `/api/telemetry?limit=${TELEMETRY_LIMIT}`, signal);
  return {
    waterfall: {
      traces: p.waterfall?.traces ?? [],
      spans: Array.isArray(p.waterfall?.spans) ? p.waterfall.spans : [],
      summary: p.waterfall?.summary ?? {},
    },
    exception_digest: {
      exceptions: Array.isArray(p.exception_digest?.exceptions) ? p.exception_digest.exceptions : [],
      total: p.exception_digest?.total,
      normal_spans_omitted: p.exception_digest?.normal_spans_omitted,
      scanned_spans: p.exception_digest?.scanned_spans,
    },
    cursor: p.cursor ?? null,
    source: p.source,
  };
}

export type Metric = { value: number | null; numerator: number; denominator: number; sample: "ok" | "small" | "none" };
export type Scorecard = {
  agent: string;
  runs: { finished: number; outcomes: Record<string, number> };
  delivery_rate: Metric;
  abandon_rate: Metric;
  review_approval_rate: Metric & { unreviewed?: number };
  reopen_rate: Metric;
  harness: { attempts: number; harness_failures: number; failure_classes: Record<string, number>; quality_success_rate: Metric };
  evals: { suite: string; suite_version: string; model: string | null; runs: number; harness_errors: number; pass_rate: Metric }[];
  ci_first_pass_rate?: { value: null; status: string; detail: string };
};
export type ScorecardsPayload = {
  window_days?: number;
  min_sample?: number;
  agents: Scorecard[];
  suites?: { name: string; cases: number }[];
  suite_errors?: { file: string; error: string }[];
};

export async function fetchScorecards(signal?: AbortSignal): Promise<ScorecardsPayload> {
  const p = await getJson<Partial<ScorecardsPayload>>("scorecards", "/api/scorecards", signal);
  return { ...p, agents: Array.isArray(p.agents) ? p.agents : [] };
}

/** `executor_delegation_payload` also reports a failed work-folder survey (`worktree_errors`, `worktree_error`). */
export type ExecutorPayload = ExecutorDelegationPayload & { worktree_errors?: string[]; worktree_error?: string };

export async function fetchExecutor(signal?: AbortSignal): Promise<ExecutorPayload> {
  const p = await getJson<Partial<ExecutorPayload>>("executor-delegation", "/api/executor-delegation", signal);
  const worktreeErrors = Array.isArray(p.worktree_errors) ? p.worktree_errors.map(String) : [];
  const worktreeError = typeof p.worktree_error === "string" && p.worktree_error ? p.worktree_error : worktreeErrors.join("; ");
  return {
    ...(worktreeErrors.length ? { worktree_errors: worktreeErrors } : {}),
    ...(worktreeError ? { worktree_error: worktreeError } : {}),
    delegations: Array.isArray(p.delegations) ? p.delegations : [],
    recent_failures: Array.isArray(p.recent_failures) ? p.recent_failures : [],
    recent_sessions: Array.isArray(p.recent_sessions) ? p.recent_sessions : [],
    stale_worktrees: Array.isArray(p.stale_worktrees) ? p.stale_worktrees : [],
    counts: p.counts,
    health: p.health,
    ...(Array.isArray(p.errors) && p.errors.length ? { errors: p.errors } : {}),
  };
}

export async function fetchFlowMetrics(signal?: AbortSignal): Promise<FlowMetricsPayload> {
  const p = await getJson<Partial<FlowMetricsPayload>>("flow-metrics", "/api/flow-metrics", signal);
  return {
    cycle_time: p.cycle_time ?? { p50_s: null, p90_s: null },
    lead_time: p.lead_time ?? { p50_s: null, p90_s: null },
    wip_age: { buckets: Array.isArray(p.wip_age?.buckets) ? p.wip_age.buckets : [], oldest_s: p.wip_age?.oldest_s ?? null },
    attribution_coverage: p.attribution_coverage,
    window_days: typeof p.window_days === "number" ? p.window_days : 30,
    as_of: p.as_of,
  };
}

export async function fetchExecutionGraph(signal?: AbortSignal): Promise<ExecutionGraphPayload> {
  const g = await getJson<Partial<ExecutionGraphPayload>>("execution-graph", "/api/execution-graph", signal);
  return {
    nodes: Array.isArray(g.nodes) ? g.nodes : [],
    links: Array.isArray(g.links) ? g.links : [],
    runs: Array.isArray(g.runs) ? g.runs : [],
    counts: g.counts && typeof g.counts === "object" ? g.counts : {},
  };
}

export async function fetchLineage(taskId: number, signal?: AbortSignal): Promise<LineagePayload> {
  const l = await getJson<LineagePayload>("execution-graph", `/api/execution-graph?task=${taskId}`, signal);
  return {
    task: l.task ?? null,
    lineage: l.lineage ?? { delegations: [], dispatch_runs: [], autonomous_cycles: [], refs: {} },
    empty: Boolean(l.empty),
  };
}
