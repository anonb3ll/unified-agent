/**
 * Provenance graph (GET /api/provenance-graph).
 * Re-exported from `hub.ts` (task #1575).
 */

/**
 * Provenance graph node/edge shape mirroring
 * `audit/provenance_graph.py` — node kinds are `human`, `device`, `agent`,
 * `task`, `action`, `approval`, `work_product` (`model`/`release` are
 * declared but never emitted, see docs/provenance-graph.md). `attrs` carries
 * kind-specific fields (e.g. a `human` node's `verified`/`actor_kind`).
 */
export type ProvenanceGraphNode = {
  id: string;
  kind: string;
  label: string;
  attrs: Record<string, unknown>;
};

export type ProvenanceGraphEdge = {
  source: string;
  target: string;
  relation: string;
  attrs: Record<string, unknown>;
};

/** GET /api/platform/provenance-graph response (schema_version "1.0"). */
export type ProvenanceGraphPayload = {
  schema_version?: string;
  scope: Record<string, unknown>;
  scope_hash: string;
  node_count: number;
  edge_count: number;
  node_counts_by_kind: Record<string, number>;
  nodes: ProvenanceGraphNode[];
  edges: ProvenanceGraphEdge[];
  canonical_hash: string;
  /** Present on a 400/403/404 response the pane renders inline. */
  error?: string;
  /** HTTP status attached by fetchProvenanceGraph so the pane can tell them apart. */
  status?: number;
};

/** Named queries `provenance_graph_payload` accepts, or "" for the plain task/project/time-window scope. */
type ProvenanceGraphQuery = "by_human" | "by_release" | "";

/** Draft/applied filter for the provenance graph pane form (all string-typed inputs). */
export type ProvenanceGraphFilter = {
  query?: ProvenanceGraphQuery;
  human?: string;
  since?: string;
  until?: string;
  task_id?: string;
  task_ids?: string;
  project_id?: string;
  agent?: string;
  pr?: string;
  limit?: string;
};

export const EMPTY_PROVENANCE_GRAPH_FILTER: ProvenanceGraphFilter = {
  query: "",
  human: "",
  since: "",
  until: "",
  task_id: "",
  task_ids: "",
  project_id: "",
  agent: "",
  pr: "",
  limit: "",
};

/** Build the /api/platform/provenance-graph query string (skips blanks). */
export function buildProvenanceGraphQuery(
  filter: ProvenanceGraphFilter = {},
): string {
  const params = new URLSearchParams();
  const entries: Array<[string, string | undefined]> = [
    ["query", filter.query],
    ["human", filter.human],
    ["since", filter.since],
    ["until", filter.until],
    ["task_id", filter.task_id],
    ["task_ids", filter.task_ids],
    ["project_id", filter.project_id],
    ["agent", filter.agent],
    ["pr", filter.pr],
    ["limit", filter.limit],
  ];
  for (const [name, value] of entries) {
    const trimmed = (value ?? "").trim();
    if (trimmed) params.set(name, trimmed);
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}
