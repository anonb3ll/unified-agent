/**
 * Provenance graph pane helpers — rendering support over
 * `ProvenanceGraphPayload` (mirrors `audit/provenance_graph.py`, task #1287).
 */
import type { ProvenanceGraphEdge, ProvenanceGraphNode } from "../hub";

/**
 * Layered reading order for the graph columns: human -> device -> agent ->
 * task/action -> approval/work_product, matching the ledger's 5-link
 * accountability chain and the edge directions
 * `nodes_and_edges_from_records` emits. `model` / `release` are declared
 * node kinds the backend never populates yet (see docs/provenance-graph.md)
 * but are listed here so an unexpected future node still lands in a real
 * column instead of silently vanishing.
 */
export const KIND_ORDER: readonly string[] = [
  "human",
  "device",
  "agent",
  "task",
  "action",
  "approval",
  "work_product",
  "model",
  "release",
];

export const KIND_LABELS: Record<string, string> = {
  human: "Human",
  device: "Device",
  agent: "Agent",
  task: "Task",
  action: "Action",
  approval: "Approval",
  work_product: "Work product",
  model: "Model",
  release: "Release",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/** Group nodes into their layered columns, in `KIND_ORDER`, then by id. */
export function groupNodesByKind(
  nodes: ProvenanceGraphNode[],
): Array<{ kind: string; nodes: ProvenanceGraphNode[] }> {
  const byKind = new Map<string, ProvenanceGraphNode[]>();
  for (const node of nodes) {
    const list = byKind.get(node.kind) ?? [];
    list.push(node);
    byKind.set(node.kind, list);
  }
  const order = [...KIND_ORDER, ...[...byKind.keys()].filter((k) => !KIND_ORDER.includes(k))];
  const groups: Array<{ kind: string; nodes: ProvenanceGraphNode[] }> = [];
  for (const kind of order) {
    const list = byKind.get(kind);
    if (list && list.length) {
      groups.push({ kind, nodes: [...list].sort((a, b) => a.id.localeCompare(b.id)) });
    }
  }
  return groups;
}

const RELATION_LABEL: Record<string, string> = {
  used_device: "used device",
  ran_agent: "ran agent",
  performed: "performed",
  has_action: "has action",
  produced: "produced",
  gated_by: "gated by",
  resolved: "resolved",
};

export function relationLabel(relation: string): string {
  return RELATION_LABEL[relation] ?? relation.replace(/_/g, " ");
}

export function labelById(nodes: ProvenanceGraphNode[]): Map<string, string> {
  const m = new Map<string, string>();
  for (const n of nodes) m.set(n.id, n.label || n.id);
  return m;
}

/**
 * True when a `human` or `device` node is *not* affirmatively verified.
 * `attrs.verified` is `true`/`false`/absent — anything other than a literal
 * `true` must read as unverified (#1217: an unresolved or agent-bearer
 * identity must never look like a confirmed human/device in the UI, even
 * when the underlying value is merely missing rather than explicitly
 * `false`).
 */
export function isUnverified(node: ProvenanceGraphNode): boolean {
  if (node.kind !== "human" && node.kind !== "device") return false;
  return node.attrs?.verified !== true;
}

/** Truncated hash for dense display; the full value belongs in a `title`. */
export function shortHash(hash: string | null | undefined, len = 16): string {
  if (!hash) return "—";
  if (hash.length <= len) return hash;
  return `${hash.slice(0, len)}…`;
}

/** One human-readable summary line per node kind, for the node card body. */
export function nodeSummary(node: ProvenanceGraphNode): string | null {
  const a = node.attrs ?? {};
  switch (node.kind) {
    case "human":
      return [a.actor_kind, a.source].filter(Boolean).join(" · ") || null;
    case "device":
      return typeof a.user === "string" && a.user ? `user ${a.user}` : null;
    case "task":
      return typeof a.title === "string" && a.title ? a.title : null;
    case "action":
      return [a.type, a.status].filter(Boolean).join(" · ") || null;
    case "approval":
      return [a.tool, a.status].filter(Boolean).join(" · ") || null;
    case "work_product":
      return typeof a.commit_sha === "string" && a.commit_sha
        ? shortHash(a.commit_sha, 10)
        : null;
    default:
      return null;
  }
}

/** Edges rendered as one accessible relationships table row each. */
export type EdgeRow = {
  key: string;
  sourceLabel: string;
  sourceKind: string;
  relation: string;
  targetLabel: string;
  targetKind: string;
};

export function edgeRows(
  nodes: ProvenanceGraphNode[],
  edges: ProvenanceGraphEdge[],
): EdgeRow[] {
  const labels = labelById(nodes);
  const kindById = new Map(nodes.map((n) => [n.id, n.kind] as const));
  return edges.map((edge, index) => ({
    key: `${edge.source}->${edge.relation}->${edge.target}#${index}`,
    sourceLabel: labels.get(edge.source) ?? edge.source,
    sourceKind: kindById.get(edge.source) ?? "",
    relation: relationLabel(edge.relation),
    targetLabel: labels.get(edge.target) ?? edge.target,
    targetKind: kindById.get(edge.target) ?? "",
  }));
}
