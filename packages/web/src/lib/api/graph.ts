/**
 * Task, agent and work graphs.
 * Re-exported from `hub.ts` (task #1575).
 */

export type TaskGraphNode = {
  id: string;
  kind?: "task" | "plan" | "agent" | string;
  label?: string | null;
  status?: string | null;
  lane?: string | null;
  claimed_by?: string | null;
  task_id?: number | null;
  /** Agent bound to a plan/task node (distinct from `claimed_by` on tasks). */
  agent?: string | null;
  /** Originating device, part of the human → device → agent evidence chain. */
  device?: string | null;
  /** Plan node flagged as blocked on a human decision. */
  needs_human?: boolean;
  /** Evidence-chain summary surfaced by the Task & Plan graph canvas. */
  audit?: {
    chain?: string;
    human?: string;
    device?: string;
    agent?: string;
    unattributed?: boolean;
    approval_status?: string;
    evidence_ref?: string | null;
    has_evidence?: boolean;
    tamper_evident?: boolean;
  };
  [key: string]: unknown;
};

export type TaskGraphLink = {
  source: string;
  target: string;
  kind?: string;
  [key: string]: unknown;
};

type TaskGraphPayload = {
  nodes: TaskGraphNode[];
  links: TaskGraphLink[];
  counts?: {
    tasks?: number;
    plans?: number;
    agents?: number;
    dependencies?: number;
    delegations?: number;
    links?: number;
  };
  empty?: boolean;
  error?: string;
};

/**
 * Task & plan graph. The hub only exposes it inside the large /api/overview
 * payload (`task_graph`), so we fetch that and slice the one key we need.
 */
export async function fetchTaskGraph(
  signal?: AbortSignal,
): Promise<TaskGraphPayload> {
  const response = await fetch("/api/overview", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `task-graph HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as { task_graph?: TaskGraphPayload };
  const tg = payload.task_graph ?? { nodes: [], links: [], empty: true };
  return {
    nodes: Array.isArray(tg.nodes) ? tg.nodes : [],
    links: Array.isArray(tg.links) ? tg.links : [],
    counts: tg.counts,
    empty: tg.empty,
    error: tg.error,
  };
}

type AgentGraphNode = {
  id: string;
  label?: string | null;
  type?: "agent" | "task" | string;
  status?: string | null;
  [key: string]: unknown;
};

type AgentGraphLink = {
  source: string;
  target: string;
  kind?: "claim" | "subtask" | "delegates" | "message" | string;
  status?: string | null;
  act?: string | null;
  task_id?: number | null;
  [key: string]: unknown;
};

export type AgentGraphPayload = {
  nodes: AgentGraphNode[];
  links: AgentGraphLink[];
  error?: string;
};

type WorkGraphReady = {
  state: "ready" | "blocked" | "in_progress" | "stalled" | "terminal";
  reason: string;
  unmet_dependency_ids: number[];
};

type WorkGraphNode = {
  id: string;
  kind: "goal" | "plan" | "task" | "run" | "session" | "evidence" | "agent";
  label?: string | null;
  status?: string | null;
  goal_id?: number;
  task_id?: number;
  plan_id?: string;
  run_id?: string;
  session_id?: string;
  subtype?: string;
  ready?: WorkGraphReady;
  stub?: boolean;
  claimed_by?: string | null;
  ref?: string | null;
  drill_uri?: string | null;
  [key: string]: unknown;
};

type WorkGraphEdge = {
  kind: string;
  from: string;
  to: string;
};

type WorkGraphUnknown = {
  node_id: string;
  field: string;
  reason: string;
};

export type WorkGraphPayload = {
  nodes: WorkGraphNode[];
  edges: WorkGraphEdge[];
  scope: Record<string, unknown>;
  cursor: string | null;
  next_cursor: string | null;
  as_of: string;
  revision: string | null;
  unknowns: WorkGraphUnknown[];
  error?: string;
};

export async function fetchWorkGraph(
  options: { goalId?: number | null; cursor?: string | null; pageSize?: number } = {},
  signal?: AbortSignal,
): Promise<WorkGraphPayload> {
  const params = new URLSearchParams();
  if (options.goalId != null) params.set("goal_id", String(options.goalId));
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.pageSize) params.set("page_size", String(options.pageSize));
  const qs = params.toString();
  const response = await fetch(`/api/work-graph${qs ? `?${qs}` : ""}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `work-graph HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  return (await response.json()) as WorkGraphPayload;
}
