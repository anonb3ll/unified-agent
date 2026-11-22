/**
 * Flow pane types matching GET /api/flow-metrics and GET /api/execution-graph.
 */

import type { TaskGraphLink, TaskGraphNode } from "../hub";

export type FlowPercentiles = {
  p50_s: number | null;
  p90_s: number | null;
};

export type WipAgeBucket = {
  label: string;
  count: number;
};

export type AttributionCoverage = {
  total_done?: number;
  attributed_done?: number;
  coverage_ratio?: number | null;
};

export type FlowMetricsPayload = {
  cycle_time: FlowPercentiles;
  lead_time: FlowPercentiles;
  wip_age: {
    buckets: WipAgeBucket[];
    oldest_s: number | null;
  };
  attribution_coverage?: AttributionCoverage;
  window_days: number;
  as_of?: string;
  error?: string;
};

export type ExecutionGraphNode = {
  id: string;
  type: string;
  task_id?: number | null;
  label?: string | null;
  state?: string | null;
  priority?: string | null;
  owner?: string | null;
  delegation_id?: number | null;
  session_id?: string | null;
  from_agent?: string | null;
  to_agent?: string | null;
  agent?: string | null;
  [key: string]: unknown;
};

export type ExecutionGraphLink = {
  source: string;
  target: string;
  kind?: string;
  dependency_id?: number | null;
  [key: string]: unknown;
};

export type ExecutionGraphRun = {
  session_id?: string | null;
  task_id?: number | null;
  delegation_id?: number | null;
  agent?: string | null;
  kind?: string | null;
  state?: string | null;
  worktree?: string | null;
  branch?: string | null;
  validation?: string | null;
  review?: string | null;
  pr?: string | null;
  summary?: string | null;
  [key: string]: unknown;
};

export type ExecutionGraphPayload = {
  nodes: ExecutionGraphNode[];
  links: ExecutionGraphLink[];
  runs: ExecutionGraphRun[];
  counts: {
    tasks?: number;
    delegations?: number;
    dependencies?: number;
    handoffs?: number;
    runs?: number;
  };
  dependencies?: unknown[];
  handoffs?: unknown[];
  error?: string;
};

/** Per-task lineage from GET /api/execution-graph?task=N (task #730). */
export type LineageRefs = {
  pr_url?: string | null;
  worktree_path?: string | null;
  branch?: string | null;
};

export type LineageDelegation = {
  id?: number | null;
  status?: string | null;
  from_agent?: string | null;
  to_agent?: string | null;
  task_id?: number | null;
};

export type LineageDispatchRun = {
  id?: string | null;
  task_id?: number | null;
  status?: string | null;
};

export type LineageCycle = {
  id?: string | null;
  task_id?: number | null;
  status?: string | null;
};

export type LineagePayload = {
  task?: { id: number; title?: string | null } | null;
  lineage?: {
    delegations?: LineageDelegation[];
    dispatch_runs?: LineageDispatchRun[];
    autonomous_cycles?: LineageCycle[];
    refs?: LineageRefs;
  };
  empty?: boolean;
  error?: string;
};

export function isLineagePayload(
  value: ExecutionGraphPayload | LineagePayload,
): value is LineagePayload {
  if ("nodes" in value && Array.isArray((value as ExecutionGraphPayload).nodes)) {
    return false;
  }
  return "empty" in value || "lineage" in value || "task" in value;
}

/** Format seconds like legacy app.js duration(): null → em dash, never fake zero. */
export function formatDuration(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return null;
  }
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) {
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  }
  return `${Math.floor(s / 86400)}d`;
}

export function taskNodesFromGraph(graph: ExecutionGraphPayload): ExecutionGraphNode[] {
  return (graph.nodes ?? []).filter(
    (n) => n.type === "task" && typeof n.task_id === "number",
  );
}

/**
 * Task & Plan graph filters (lane / agent / status). `TaskGraphNode` /
 * `TaskGraphLink` / `TaskGraphPayload` themselves live in `../hub` — that
 * module is the single source of truth for the /api/overview task_graph
 * shape, so this file only adds what the graph canvas needs on top.
 */
export type TaskGraphFilters = {
  lane: string;
  agent: string;
  status: string;
};

/**
 * Whether `node` should be shown under `filters`. Non-task nodes (plans,
 * agents) always match — filtering only dims/hides task nodes. The agent
 * filter also matches via `claim`/`delegates` link connectivity, since a
 * task's bound agent is not always mirrored onto `claimed_by`.
 */
export function matchesTaskGraphFilters(
  node: TaskGraphNode,
  filters: TaskGraphFilters,
  links: TaskGraphLink[],
): boolean {
  if (node.kind !== "task") return true;
  const lane = filters.lane.trim().toLowerCase();
  const agent = filters.agent.trim().toLowerCase();
  const status = filters.status.trim().toLowerCase();
  if (lane && String(node.lane || "default").toLowerCase() !== lane) return false;
  if (agent) {
    const owners = [node.claimed_by]
      .filter(Boolean)
      .map((v) => String(v).toLowerCase());
    const connected = links.some(
      (l) =>
        (l.kind === "claim" || l.kind === "delegates") &&
        [l.source, l.target].some(
          (id) => String(id).toLowerCase() === `agent:${agent}`,
        ) &&
        [l.source, l.target].includes(node.id),
    );
    if (!owners.includes(agent) && !connected) return false;
  }
  if (status && String(node.status || "open").toLowerCase() !== status) {
    return false;
  }
  return true;
}
