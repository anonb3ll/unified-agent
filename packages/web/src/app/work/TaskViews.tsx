/**
 * The four views of the one task set (spec §9.2): List, Board, Tree, Graph.
 * Each receives the already filtered tasks; filters live in the URL.
 */
import { useMemo, useRef, useState, type DragEvent } from "react";
import {
  Badge,
  Button,
  ConfirmDialog,
  DataTable,
  GraphCanvas,
  IconButton,
  Menu,
  UiLink,
  type Column,
  type GraphEdge,
  type GraphNode,
  type SortState,
} from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import { UNKNOWN_COUNT } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import { WRITE_PATHS, type TaskUpdateBody } from "./api";
import type { BoardPayload } from "../../lib/api/tasks";
import {
  ALERT_LABEL,
  buildTree,
  EDGE_LEGEND,
  explainRefusal,
  priorityLabel,
  queueLabel,
  relationEdge,
  sortTasks,
  STATUS_ORDER,
  statusLabel,
  type BoardDelegation,
  type SortKey,
  type WorkTask,
} from "./model";
import { WriteFeedback } from "./shared";

const taskHref = (id: number) => `/work/tasks/${id}`;

// --- List -----------------------------------------------------------------------

const SORT_KEYS: readonly SortKey[] = ["id", "title", "status", "priority", "project", "owner", "updated"];

export function TaskListView({ tasks }: { tasks: readonly WorkTask[] }) {
  const { values, update } = useUrlParams({ sort: "id", dir: "descending" });
  const key = (SORT_KEYS as readonly string[]).includes(values.sort) ? (values.sort as SortKey) : "id";
  const sort: SortState = { columnId: key, direction: values.dir === "ascending" ? "ascending" : "descending" };
  const rows = useMemo(() => sortTasks(tasks, key, sort.direction === "ascending"), [tasks, key, sort.direction]);
  const columns: Column<WorkTask>[] = [
    { id: "id", header: "Task", sortable: true, cell: (t) => `#${t.id}`, width: "6rem" },
    { id: "title", header: "Title", sortable: true, rowLink: true, cell: (t) => t.title },
    { id: "status", header: "Status", sortable: true, cell: (t) => statusLabel(t.status) },
    { id: "priority", header: "Priority", sortable: true, cell: (t) => priorityLabel(t.priority) },
    { id: "project", header: "Project", sortable: true, cell: (t) => t.project },
    { id: "owner", header: "Owner", sortable: true, cell: (t) => t.owner ?? <span className="text-[var(--ua-muted)]">Unclaimed</span> },
    { id: "queue", header: "Queue", cell: (t) => queueLabel(t.lane) },
  ];
  return (
    <DataTable
      caption="Tasks"
      columns={columns}
      data={rows}
      getRowKey={(t) => t.id}
      getRowHref={(t) => taskHref(t.id)}
      sort={sort}
      onSortChange={(next) => update({ sort: next.columnId, dir: next.direction })}
      stickyHeader
      maxHeight="70vh"
    />
  );
}

// --- Board ----------------------------------------------------------------------

type PendingMove = { task: WorkTask; to: string; via: "keyboard" | "pointer" };

/**
 * Columns by status. A pointer drag moves a card; with the keyboard, the
 * card's "Move to" menu opens a confirmation first (spec §6: no write from a
 * bare key press; §12 defect 2). A refused move says why; a move that waits
 * for approval says so and changes nothing (§12 defect 1).
 */
export function TaskBoardView({
  tasks,
  complete = true,
  delegations = [],
  wip,
}: {
  tasks: readonly WorkTask[];
  complete?: boolean;
  /** Pending delegations, shown in their column; they move through the delegation flow, not here. */
  delegations?: readonly BoardDelegation[];
  /** board_payload wip: per queue and per agent. */
  wip?: BoardPayload["wip"];
}) {
  const [confirm, setConfirm] = useState<PendingMove | null>(null);
  const [last, setLast] = useState<PendingMove | null>(null);
  const lastRef = useRef<PendingMove | null>(null);
  const [dragOver, setDragOver] = useState<string | null>(null);
  const write = useHubWrite<TaskUpdateBody, unknown>({
    path: WRITE_PATHS.taskAction,
    invalidates: [["work"]],
    label: "Moving the task",
    successMessage: () => {
      const m = lastRef.current;
      return m ? `Task #${m.task.id} moved to ${statusLabel(m.to)}.` : "Task moved.";
    },
  });

  const move = async (m: PendingMove) => {
    lastRef.current = m;
    setLast(m);
    setConfirm(null);
    // The card's updated_at goes with the move, so a move from a stale board answers 409.
    await write.run({ id: m.task.id, status: m.to, ...(m.task.updatedAt ? { expected_updated_at: m.task.updatedAt } : {}) });
  };

  const byStatus = useMemo(() => {
    const map = new Map<string, WorkTask[]>(STATUS_ORDER.map((s) => [s, []]));
    for (const t of tasks) (map.get(t.status) ?? map.set(t.status, []).get(t.status))!.push(t);
    return map;
  }, [tasks]);

  const onDrop = (status: string) => (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    setDragOver(null);
    const id = Number(event.dataTransfer.getData("text/x-task-id"));
    const task = tasks.find((t) => t.id === id);
    if (task && task.status !== status) void move({ task, to: status, via: "pointer" });
  };

  return (
    <div className="flex flex-col gap-3">
      <p className="m-0 text-sm text-[var(--ua-muted)]">
        Drag a card to another column, or use its <strong>Move to</strong> menu.
      </p>
      {last && write.state.status === "error" && (
        <div role="alert" className="rounded-md border border-[var(--ua-danger)] bg-[var(--ua-danger-surface)] px-3 py-2 text-sm">
          <p className="m-0 font-medium">Task #{last.task.id} was not moved.</p>
          <p className="m-0 mt-0.5">{explainRefusal(last.to, write.state.message, write.state.detail)}</p>
          {write.state.detail && (
            <details className="mt-1 text-xs">
              <summary className="cursor-pointer">Technical detail</summary>
              <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{write.state.detail}</pre>
            </details>
          )}
        </div>
      )}
      {last && write.state.status === "pending_approval" && <WriteFeedback state={write.state} what={`Moving task #${last.task.id} to ${statusLabel(last.to)}`} />}
      {wip && (Object.keys(wip.lanes ?? {}).length > 0 || Object.keys(wip.agents ?? {}).length > 0) && (
        <section aria-label="Work in progress limits" className="rounded-md border border-[var(--ua-border)] px-3 py-2 text-sm">
          <h2 className="m-0 mb-1 text-sm font-semibold">Work in progress limits</h2>
          <p className="m-0 mb-1 text-xs text-[var(--ua-muted)]">How many tasks each queue and agent has in progress, against its limit.</p>
          <ul className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 p-0">
            {Object.entries(wip.lanes ?? {}).map(([lane, info]) => (
              <li key={`lane-${lane}`}>
                {queueLabel(lane)}: {complete ? info.count : UNKNOWN_COUNT} in progress{info.cap !== null && info.cap !== undefined ? `, limit ${info.cap}` : ", no limit"}
                {info.over ? <strong className="text-[var(--ua-danger)]"> (over the limit)</strong> : null}
              </li>
            ))}
            {Object.entries(wip.agents ?? {})
              .filter(([, info]) => info.over)
              .map(([agent, info]) => (
                <li key={`agent-${agent}`}>
                  {agent}: {complete ? info.count : UNKNOWN_COUNT} in progress, limit {info.cap ?? "none"} <strong className="text-[var(--ua-danger)]">(over the limit)</strong>
                </li>
              ))}
          </ul>
        </section>
      )}
      <div className="grid grid-flow-col auto-cols-[minmax(15rem,1fr)] gap-3 overflow-x-auto pb-2" role="list" aria-label="Board columns">
        {[...byStatus.entries()].map(([status, cards]) => (
          <section
            key={status}
            role="listitem"
            aria-labelledby={`col-${status}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(status);
            }}
            onDragLeave={() => setDragOver((s) => (s === status ? null : s))}
            onDrop={onDrop(status)}
            data-column={status}
            className={`flex min-h-40 flex-col gap-2 rounded-lg border p-2 ${dragOver === status ? "border-[var(--ua-accent)] bg-[var(--ua-selected)]" : "border-[var(--ua-border)] bg-[var(--ua-surface)]"}`}
          >
            <h2 id={`col-${status}`} className="m-0 flex items-center gap-2 px-1 text-sm font-semibold">
              {statusLabel(status)}
              <span className="font-mono text-xs text-[var(--ua-muted)]">{complete ? cards.length : UNKNOWN_COUNT}</span>
            </h2>
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {cards.length === 0 && delegations.every((d) => d.column !== status) && <li className="px-1 text-xs text-[var(--ua-muted)]">No tasks</li>}
              {delegations
                .filter((d) => d.column === status)
                .map((d) => (
                  <li key={d.id} className="flex flex-col gap-1 rounded-md border border-dashed border-[var(--ua-border-strong)] bg-[var(--ua-surface)] p-2 text-sm">
                    <span className="font-medium">{d.title}</span>
                    <span className="text-xs text-[var(--ua-muted)]">
                      Delegation from {d.from ?? "an agent"} to {d.to ?? "an agent"}
                      {d.taskId ? (
                        <>
                          {" for "}
                          <UiLink href={taskHref(d.taskId)} className="text-[var(--ua-link)] underline">
                            #{d.taskId}
                          </UiLink>
                        </>
                      ) : null}
                    </span>
                    {d.overdue && <Badge size="sm" variant="danger">Overdue</Badge>}
                  </li>
                ))}
              {cards.map((task) => (
                <li
                  key={task.id}
                  draggable
                  data-task-card={task.id}
                  onDragStart={(e) => {
                    e.dataTransfer.setData("text/x-task-id", String(task.id));
                    e.dataTransfer.effectAllowed = "move";
                  }}
                  className="flex flex-col gap-1.5 rounded-md border border-[var(--ua-border-strong)] bg-[var(--ua-raised)] p-2 text-sm"
                >
                  <UiLink href={taskHref(task.id)} className="font-medium text-[var(--ua-link)] underline-offset-2 hover:underline">
                    <span className="text-[var(--ua-muted)]">#{task.id}</span> {task.title}
                  </UiLink>
                  <div className="flex flex-wrap items-center gap-1.5 text-xs text-[var(--ua-muted)]">
                    <Badge size="sm" variant={task.priority === "high" ? "warning" : "neutral"}>{priorityLabel(task.priority)}</Badge>
                    <span>{task.owner ?? "Unclaimed"}</span>
                    {task.blockedReason && <span title={task.blockedReason}>Blocked</span>}
                    {task.alerts.map((a) => (
                      <Badge key={a} size="sm" variant={a === "lock_expired" ? "danger" : "warning"}>
                        {ALERT_LABEL[a] ?? a.replace(/_/g, " ")}
                      </Badge>
                    ))}
                  </div>
                  <Menu
                    label="Move to"
                    aria-label={`Move task #${task.id} to`}
                    size="sm"
                    items={STATUS_ORDER.filter((s) => s !== task.status).map((s) => ({
                      id: s,
                      label: statusLabel(s),
                      onSelect: () => setConfirm({ task, to: s, via: "keyboard" }),
                    }))}
                  />
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      <ConfirmDialog
        open={confirm !== null}
        title="Move this task?"
        objectName={confirm ? `#${confirm.task.id} ${confirm.task.title}` : undefined}
        consequence={confirm ? `Its status changes from ${statusLabel(confirm.task.status)} to ${statusLabel(confirm.to)}.` : ""}
        confirmLabel={confirm ? `Move to ${statusLabel(confirm.to)}` : "Move"}
        busy={write.isRunning}
        onConfirm={() => confirm && void move(confirm)}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}

// --- Tree -----------------------------------------------------------------------

/** Parent and subtasks, collapsible (WAI-ARIA tree: arrows move, Right/Left expand and collapse). */
export function TaskTreeView({ tasks }: { tasks: readonly WorkTask[] }) {
  const { roots, children } = useMemo(() => buildTree(tasks), [tasks]);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const toggle = (id: number) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const renderNode = (task: WorkTask, level: number) => {
    const kids = children.get(task.id) ?? [];
    const open = !collapsed.has(task.id);
    return (
      <li key={task.id} className="flex flex-col">
        <div className="flex items-center gap-2 py-1" style={{ paddingLeft: `${(level - 1) * 1.25}rem` }}>
          {kids.length > 0 ? (
            <IconButton
              size="sm"
              aria-label={`${open ? "Collapse" : "Expand"} subtasks of #${task.id}`}
              aria-expanded={open}
              icon={<span>{open ? "▾" : "▸"}</span>}
              onClick={() => toggle(task.id)}
            />
          ) : (
            <span aria-hidden="true" className="inline-block size-7 max-md:size-11" />
          )}
          <UiLink href={taskHref(task.id)} className="text-[var(--ua-link)] underline-offset-2 hover:underline">
            <span className="text-[var(--ua-muted)]">#{task.id}</span> {task.title}
          </UiLink>
          <span className="text-xs text-[var(--ua-muted)]">
            {statusLabel(task.status)}
            {kids.length > 0 ? ` · ${kids.length} subtask${kids.length === 1 ? "" : "s"}` : ""}
          </span>
        </div>
        {kids.length > 0 && open && <ul className="m-0 list-none p-0">{kids.map((k) => renderNode(k, level + 1))}</ul>}
      </li>
    );
  };

  return (
    <nav aria-label="Task tree">
      <ul className="m-0 list-none rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-2">{roots.map((r) => renderNode(r, 1))}</ul>
    </nav>
  );
}

// --- Graph ----------------------------------------------------------------------

export type GraphLinkInput = { kind: string; from: string; to: string };

const STATUS_TONE: Record<string, GraphNode["tone"]> = {
  open: "default",
  doing: "live",
  waiting_review: "decision",
  blocked: "danger",
  done: "ok",
  cancelled: "warn",
};

export const GRAPH_LEGEND = [
  { tone: "default" as const, label: "Backlog" },
  { tone: "live" as const, label: "Doing" },
  { tone: "decision" as const, label: "Waiting for review" },
  { tone: "danger" as const, label: "Blocked" },
  { tone: "ok" as const, label: "Done" },
];

/** Build the drawn graph from hub nodes and links with correct relation words. */
export function buildGraph(
  nodes: readonly { id: string; label: string; status?: string | null; kind?: string; description?: string }[],
  links: readonly GraphLinkInput[],
): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const ids = new Set(nodes.map((n) => n.id));
  const edges: GraphEdge[] = [];
  links.forEach((link, i) => {
    if (!ids.has(link.from) || !ids.has(link.to)) return;
    const edge = relationEdge(link.kind, link.from, link.to, `${link.kind}-${i}`);
    if (edge) edges.push(edge);
  });
  return {
    nodes: nodes.map((n) => ({
      id: n.id,
      label: n.label,
      description: n.description ?? (n.kind && n.kind !== "task" ? n.kind : n.status ? statusLabel(n.status) : undefined),
      tone: n.kind === "task" || !n.kind ? (STATUS_TONE[n.status ?? ""] ?? "default") : n.kind === "goal" || n.kind === "plan" ? "decision" : "default",
    })),
    edges,
  };
}

export function TaskGraphView({
  label,
  nodes,
  edges,
  onSelect,
}: {
  label: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  onSelect: (id: string) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <GraphCanvas label={label} nodes={nodes} edges={edges} onNodeSelect={onSelect} legend={GRAPH_LEGEND} height={520} />
      <div>
        <h2 className="m-0 mb-1 text-sm font-semibold">What the lines mean</h2>
        <ul aria-label="Line labels" className="m-0 flex list-none flex-col gap-0.5 p-0 text-xs text-[var(--ua-muted)]">
          {EDGE_LEGEND.map((e) => (
            <li key={e.label}>
              <strong className="text-[var(--ua-fg)]">{e.label}</strong>: {e.meaning}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export function ViewButton({ pressed, onClick, children }: { pressed: boolean; onClick: () => void; children: string }) {
  return (
    <Button size="sm" variant={pressed ? "primary" : "secondary"} aria-pressed={pressed} onClick={onClick}>
      {children}
    </Button>
  );
}
