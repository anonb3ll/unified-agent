import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import { cx } from "./cx";

export type GraphNode = {
  id: string;
  label: string;
  /** Second line on the node and in the table. */
  description?: string;
  /** Visual tone; also listed in the table. */
  tone?: "default" | "ok" | "live" | "warn" | "danger" | "decision";
  /** Fixed position; computed in layers from the edges when omitted. */
  position?: { x: number; y: number };
};

export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  /** Relation in words, read from source to target: "blocks", "parent of". */
  label: string;
};

export type GraphCanvasProps = {
  /** Names the graph: "Task dependencies". */
  label: string;
  nodes: readonly GraphNode[];
  edges: readonly GraphEdge[];
  /** Called when a node is chosen by click, Enter or Space. */
  onNodeSelect?: (id: string) => void;
  /** Key for the tones used (rendered under the graph). */
  legend?: readonly { tone: NonNullable<GraphNode["tone"]>; label: string }[];
  /** Start in table view. */
  defaultView?: "graph" | "table";
  height?: number;
  className?: string;
};

const TONE_BORDER: Record<NonNullable<GraphNode["tone"]>, string> = {
  default: "var(--ua-border-strong)",
  ok: "var(--ua-ok)",
  live: "var(--ua-live)",
  warn: "var(--ua-warn)",
  danger: "var(--ua-danger)",
  decision: "var(--ua-decision)",
};

/** Longest-path layers from the roots; cycles fall back to layer 0. */
export function layeredLayout(nodes: readonly GraphNode[], edges: readonly GraphEdge[]): Map<string, { x: number; y: number }> {
  const ids = new Set(nodes.map((n) => n.id));
  const rank = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  const usable = edges.filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target);
  for (let pass = 0; pass < nodes.length; pass++) {
    let changed = false;
    for (const e of usable) {
      const next = (rank.get(e.source) ?? 0) + 1;
      if (next > (rank.get(e.target) ?? 0) && next < nodes.length) {
        rank.set(e.target, next);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const rows = new Map<number, number>();
  const out = new Map<string, { x: number; y: number }>();
  for (const n of nodes) {
    const r = rank.get(n.id) ?? 0;
    const row = rows.get(r) ?? 0;
    rows.set(r, row + 1);
    out.set(n.id, n.position ?? { x: r * 240, y: row * 96 });
  }
  return out;
}

function NodeBody({ node }: { node: GraphNode }): ReactNode {
  return (
    <div className="max-w-48 text-left">
      <div className="truncate text-xs font-semibold">{node.label}</div>
      {node.description && <div className="truncate text-[11px] text-[var(--ua-muted)]">{node.description}</div>}
    </div>
  );
}

/**
 * A pan-and-zoom graph over `@xyflow/react` (Controls: zoom in, zoom out, fit).
 * Nodes are keyboard focusable and selectable with Enter or Space. Every
 * graph also renders as a table of nodes and links, the equivalent for
 * screen readers and for anyone who prefers a list.
 */
export function GraphCanvas({
  label,
  nodes,
  edges,
  onNodeSelect,
  legend,
  defaultView = "graph",
  height = 420,
  className,
}: GraphCanvasProps) {
  const [view, setView] = useState<"graph" | "table">(defaultView);
  // Loaded on first use, so importing `src/ui` elsewhere does not ship the
  // library's stylesheet with every page.
  useEffect(() => {
    void import("@xyflow/react/dist/style.css");
  }, []);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  // React Flow is controlled here, so its selection changes (click, and
  // Enter or Space on a focused node) arrive only through onNodesChange.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const onNodesChange = (changes: NodeChange[]) => {
    for (const change of changes) {
      if (change.type !== "select") continue;
      if (change.selected) {
        setSelectedId(change.id);
        onNodeSelect?.(change.id);
      } else {
        setSelectedId((current) => (current === change.id ? null : current));
      }
    }
  };

  const flowNodes: Node[] = useMemo(() => {
    const positions = layeredLayout(nodes, edges);
    return nodes.map((n) => ({
      id: n.id,
      position: positions.get(n.id) ?? { x: 0, y: 0 },
      data: { label: <NodeBody node={n} /> },
      ariaLabel: `${n.label}${n.description ? `, ${n.description}` : ""}`,
      style: { borderColor: TONE_BORDER[n.tone ?? "default"], borderWidth: 2 },
      connectable: false,
      draggable: false,
      selected: n.id === selectedId,
    }));
  }, [nodes, edges, selectedId]);

  const flowEdges: Edge[] = useMemo(
    () =>
      edges
        .filter((e) => byId.has(e.source) && byId.has(e.target))
        .map((e) => ({
          id: e.id,
          source: e.source,
          target: e.target,
          label: e.label,
          ariaLabel: `${byId.get(e.source)?.label} ${e.label} ${byId.get(e.target)?.label}`,
          markerEnd: { type: MarkerType.ArrowClosed },
          focusable: false,
        })),
    [edges, byId],
  );

  const toggle = (
    <div role="group" aria-label={`${label}: view`} className="flex gap-1">
      {(["graph", "table"] as const).map((v) => (
        <button
          key={v}
          type="button"
          aria-pressed={view === v}
          onClick={() => setView(v)}
          className={cx(
            "min-h-8 cursor-pointer rounded border px-2.5 text-xs font-medium max-md:min-h-11",
            view === v
              ? "border-[var(--ua-accent)] bg-[var(--ua-accent)] text-[var(--ua-accent-fg)]"
              : "border-[var(--ua-border-strong)] bg-[var(--ua-surface)] text-[var(--ua-fg)] hover:bg-[var(--ua-hover)]",
            "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-[var(--ua-accent)]",
          )}
        >
          {v === "graph" ? "Graph" : "Table"}
        </button>
      ))}
    </div>
  );

  return (
    <figure className={cx("m-0 flex flex-col gap-2", className)}>
      <figcaption className="flex flex-wrap items-center gap-2 text-sm font-medium">
        <span className="flex-1">{label}</span>
        {toggle}
      </figcaption>
      {view === "graph" ? (
        <div
          className="ua-graph rounded-md border border-[var(--ua-border)]"
          style={{ height }}
          onKeyDownCapture={(event) => {
            // Enter or Space on the node that is already selected opens it again;
            // React Flow reports no selection change for that.
            if (event.key !== "Enter" && event.key !== " ") return;
            const id = (event.target as HTMLElement).closest<HTMLElement>(".react-flow__node")?.dataset.id;
            if (id && id === selectedId) onNodeSelect?.(id);
          }}
        >
          <ReactFlowProvider>
            <ReactFlow
              nodes={flowNodes}
              edges={flowEdges}
              fitView
              nodesDraggable={false}
              nodesConnectable={false}
              nodesFocusable
              edgesFocusable={false}
              elementsSelectable
              onNodesChange={onNodesChange}
              onNodeClick={(_, node) => {
                if (node.id === selectedId) onNodeSelect?.(node.id);
              }}
              proOptions={{ hideAttribution: true }}
              aria-label={label}
            >
              <Background gap={24} size={1} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </ReactFlowProvider>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <div tabIndex={0} role="region" aria-label={`${label}: nodes`} className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <caption className="sr-only">{label}: nodes</caption>
              <thead>
                <tr className="border-b border-[var(--ua-border)] text-left text-xs text-[var(--ua-muted)]">
                  <th scope="col" className="px-2 py-1 font-medium">Node</th>
                  <th scope="col" className="px-2 py-1 font-medium">Detail</th>
                  <th scope="col" className="px-2 py-1 font-medium">Links out</th>
                </tr>
              </thead>
              <tbody>
                {nodes.map((n) => {
                  const out = edges.filter((e) => e.source === n.id && byId.has(e.target));
                  return (
                    <tr key={n.id} className="border-b border-[var(--ua-border)]">
                      <th scope="row" className="px-2 py-1 text-left font-medium">
                        {onNodeSelect ? (
                          <button
                            type="button"
                            onClick={() => onNodeSelect(n.id)}
                            className="cursor-pointer border-0 bg-transparent p-0 text-left text-[var(--ua-link)] underline focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)]"
                          >
                            {n.label}
                          </button>
                        ) : (
                          n.label
                        )}
                      </th>
                      <td className="px-2 py-1 text-[var(--ua-muted)]">{n.description ?? ""}</td>
                      <td className="px-2 py-1">
                        {out.length === 0 ? (
                          <span className="text-[var(--ua-muted)]">None</span>
                        ) : (
                          out.map((e) => `${e.label} ${byId.get(e.target)?.label}`).join("; ")
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {legend && legend.length > 0 && (
        <ul aria-label={`${label}: legend`} className="m-0 flex list-none flex-wrap gap-3 p-0 text-xs text-[var(--ua-muted)]">
          {legend.map((item) => (
            <li key={item.tone} className="flex items-center gap-1.5">
              <span aria-hidden="true" className="inline-block size-3 rounded-sm border-2" style={{ borderColor: TONE_BORDER[item.tone] }} />
              {item.label}
            </li>
          ))}
        </ul>
      )}
    </figure>
  );
}
