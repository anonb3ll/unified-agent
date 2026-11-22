/**
 * Pure helpers for the Work area: the task set, its filters, plain-language
 * labels, graph relations and refusal explanations. No React here, so each
 * rule has a direct unit test.
 */
import type { BoardCard, BoardPayload } from "../../lib/api/tasks";
import type { GraphEdge } from "../../lib/ui";
import type { TaskStatus } from "./api";

/** One task in the shared set every view renders. */
export type WorkTask = {
  id: number;
  title: string;
  status: string;
  priority: string;
  project: string;
  domain: string | null;
  lane: string;
  owner: string | null;
  parentId: number | null;
  blockedReason: string | null;
  depCount: number;
  ageSeconds: number | null;
  updatedAt: string | null;
  evidenceRef: string | null;
  /** board_payload card alerts: aging_open, aging_doing, lock_expired. */
  alerts: string[];
};

/** A pending delegation on the board (api._board_delegation_card). */
export type BoardDelegation = {
  id: string;
  title: string;
  column: string;
  from: string | null;
  to: string | null;
  taskId: number | null;
  overdue: boolean;
  deadline: string | null;
};

export const ALERT_LABEL: Record<string, string> = {
  aging_open: "Waiting a long time in the backlog",
  aging_doing: "In progress for a long time",
  lock_expired: "Claim lock expired",
  overdue: "Overdue",
};

export function delegationsFromBoard(board: BoardPayload): BoardDelegation[] {
  const out: BoardDelegation[] = [];
  for (const column of board.columns)
    for (const card of column.cards ?? []) {
      if (card.kind !== "delegation") continue;
      out.push({
        id: String(card.id),
        title: card.title ?? "Delegation",
        column: card.status ?? column.status,
        from: card.from_agent ?? null,
        to: card.to_agent ?? null,
        taskId: typeof card.task_id === "number" ? card.task_id : null,
        overdue: Array.isArray(card.alerts) && card.alerts.includes("overdue"),
        deadline: typeof card.deadline === "string" ? card.deadline : null,
      });
    }
  return out;
}

export const STATUS_ORDER: readonly TaskStatus[] = ["open", "doing", "waiting_review", "blocked", "done", "cancelled"];

export const STATUS_LABEL: Record<string, string> = {
  open: "Backlog",
  doing: "Doing",
  waiting_review: "Waiting for review",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
};

export const PRIORITY_LABEL: Record<string, string> = { high: "High", med: "Medium", low: "Low" };

export function statusLabel(status: string | null | undefined): string {
  return STATUS_LABEL[status ?? ""] ?? (status ? status.replace(/_/g, " ") : "Unknown");
}

export function priorityLabel(priority: string | null | undefined): string {
  return PRIORITY_LABEL[priority ?? ""] ?? (priority || "Medium");
}

/** "default" is the hub's name for the unnamed queue. */
export function queueLabel(lane: string | null | undefined): string {
  return !lane || lane === "default" ? "Default queue" : lane;
}

function toTask(card: BoardCard): WorkTask | null {
  if (card.kind && card.kind !== "task") return null;
  const id = typeof card.id === "number" ? card.id : Number(card.id);
  if (!Number.isInteger(id) || id <= 0) return null;
  return {
    id,
    title: card.title ?? `Task ${id}`,
    status: card.status ?? "open",
    priority: card.priority ?? "med",
    project: card.project ?? "unified-agent",
    domain: typeof card.domain === "string" ? card.domain : null,
    lane: card.lane ?? "default",
    owner: card.claimed_by ?? null,
    parentId: typeof card.parent_id === "number" ? card.parent_id : null,
    blockedReason: card.blocked_reason ?? null,
    depCount: typeof card.dep_count === "number" ? card.dep_count : 0,
    ageSeconds: typeof card.age_s === "number" ? card.age_s : null,
    updatedAt: typeof card.updated_at === "string" ? card.updated_at : null,
    evidenceRef: typeof card.evidence_ref === "string" ? card.evidence_ref : null,
    alerts: Array.isArray(card.alerts) ? card.alerts.filter((a): a is string => typeof a === "string") : [],
  };
}

/** Whether this hub's board sends `domain` on its task cards (#2480); older hubs do not. */
export function boardHasDomain(board: BoardPayload): boolean {
  return board.columns.some((c) => (c.cards ?? []).some((card) => (!card.kind || card.kind === "task") && "domain" in card));
}

/** Flatten the board (cards nest same-status subtasks) into one task set, by id. */
export function tasksFromBoard(board: BoardPayload): WorkTask[] {
  const out = new Map<number, WorkTask>();
  const visit = (card: BoardCard) => {
    const task = toTask(card);
    if (task && !out.has(task.id)) out.set(task.id, task);
    for (const sub of card.subtasks ?? []) visit(sub);
  };
  for (const column of board.columns) for (const card of column.cards ?? []) visit(card);
  return [...out.values()].sort((a, b) => b.id - a.id);
}

export type TaskFilters = {
  status: string;
  project: string;
  domain: string;
  priority: string;
  owner: string;
  queue: string;
  q: string;
  mine: string;
  blocked: string;
  review: string;
};

export const FILTER_DEFAULTS: TaskFilters = {
  status: "",
  project: "",
  domain: "",
  priority: "",
  owner: "",
  queue: "",
  q: "",
  mine: "",
  blocked: "",
  review: "",
};

export function filterTasks(tasks: readonly WorkTask[], f: TaskFilters, me: string | null): WorkTask[] {
  const q = f.q.trim().toLowerCase();
  return tasks.filter((t) => {
    if (f.status && t.status !== f.status) return false;
    if (f.project && t.project !== f.project) return false;
    if (f.domain && t.domain !== f.domain) return false;
    if (f.priority && t.priority !== f.priority) return false;
    if (f.owner && t.owner !== f.owner) return false;
    if (f.queue && t.lane !== f.queue) return false;
    if (f.mine === "1" && (!me || t.owner !== me)) return false;
    if (f.blocked === "1" && t.status !== "blocked" && !t.blockedReason) return false;
    if (f.review === "1" && t.status !== "waiting_review") return false;
    if (q && !`#${t.id} ${t.title} ${t.project} ${t.owner ?? ""}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

export function distinct(tasks: readonly WorkTask[], pick: (t: WorkTask) => string | null): string[] {
  return [...new Set(tasks.map(pick).filter((v): v is string => Boolean(v)))].sort((a, b) => a.localeCompare(b));
}

export type SortKey = "id" | "title" | "status" | "priority" | "project" | "owner" | "updated";

const PRIORITY_RANK: Record<string, number> = { high: 0, med: 1, low: 2 };

export function sortTasks(tasks: readonly WorkTask[], key: SortKey, ascending: boolean): WorkTask[] {
  const value = (t: WorkTask): string | number => {
    switch (key) {
      case "id":
        return t.id;
      case "title":
        return t.title.toLowerCase();
      case "status":
        return STATUS_ORDER.indexOf(t.status as TaskStatus);
      case "priority":
        return PRIORITY_RANK[t.priority] ?? 1;
      case "project":
        return t.project;
      case "owner":
        return t.owner ?? "";
      case "updated":
        return t.updatedAt ?? "";
    }
  };
  const sorted = [...tasks].sort((a, b) => {
    const x = value(a);
    const y = value(b);
    return x < y ? -1 : x > y ? 1 : a.id - b.id;
  });
  return ascending ? sorted : sorted.reverse();
}

/** Parent → children for the tree; a child whose parent is not in the set is a root. */
export function buildTree(tasks: readonly WorkTask[]): { roots: WorkTask[]; children: Map<number, WorkTask[]> } {
  const ids = new Set(tasks.map((t) => t.id));
  const children = new Map<number, WorkTask[]>();
  const roots: WorkTask[] = [];
  for (const t of tasks) {
    if (t.parentId !== null && ids.has(t.parentId) && t.parentId !== t.id) {
      const list = children.get(t.parentId) ?? [];
      list.push(t);
      children.set(t.parentId, list);
    } else roots.push(t);
  }
  const byId = (a: WorkTask, b: WorkTask) => a.id - b.id;
  roots.sort(byId);
  for (const list of children.values()) list.sort(byId);
  return { roots, children };
}

// --- Graph relations (spec §12 defect 6) -----------------------------------------

/**
 * Edge kinds as the hub writes them, and the words each one means when read
 * in the direction it is drawn. Dependencies are drawn from the prerequisite
 * to the task that waits for it ("blocks"); parents are drawn to their
 * subtasks ("parent of"). The hub's own direction is in the comment.
 */
export type RelationLabel = "blocks" | "blocked by" | "parent of" | "subtask of" | "claimed by" | "serves" | "ran" | "evidence for" | "delegates to" | "part of" | "authored";

/** One hub link, turned into a drawn edge with the right words. Returns null for kinds the work views do not draw. */
export function relationEdge(kind: string, from: string, to: string, id: string): GraphEdge | null {
  switch (kind) {
    // overview task_graph (webui/api.py task_graph_payload): prerequisite → dependent, drawn as is.
    case "depends_on":
      return { id, source: from, target: to, label: "blocks" };
    // work graph (work_graph.py): dependent → prerequisite, reversed so it reads "blocks".
    case "task_dependency":
      return { id, source: to, target: from, label: "blocks" };
    // overview task_graph: parent → child.
    case "subtask":
      return { id, source: from, target: to, label: "parent of" };
    // work-graph: child → parent.
    case "task_parent":
      return { id, source: to, target: from, label: "parent of" };
    // overview: agent → task. work-graph: task → agent.
    case "claim":
      return from.startsWith("agent:") ? { id, source: to, target: from, label: "claimed by" } : { id, source: from, target: to, label: "claimed by" };
    case "goal_task":
      return { id, source: to, target: from, label: "serves" };
    case "plan_goal":
      return { id, source: from, target: to, label: "serves" };
    case "goal_ancestry":
      return { id, source: from, target: to, label: "part of" };
    case "run_task":
    case "session_task":
      return { id, source: from, target: to, label: "ran" };
    case "evidence_task":
      return { id, source: from, target: to, label: "evidence for" };
    case "delegates":
      return { id, source: from, target: to, label: "delegates to" };
    // overview: agent → plan, the plan's author.
    case "authored":
      return { id, source: from, target: to, label: "authored" };
    default:
      return null;
  }
}

/** How one task relates to another, read from the task's side, for the task page and the drawer. */
export function relationFor(taskNode: string, edge: GraphEdge): { label: RelationLabel | string; other: string } | null {
  if (edge.source === taskNode) return { label: edge.label, other: edge.target };
  if (edge.target === taskNode) {
    const inverse: Record<string, string> = { blocks: "blocked by", "parent of": "subtask of" };
    return { label: inverse[edge.label] ?? `${edge.label} (from)`, other: edge.source };
  }
  return null;
}

export const EDGE_LEGEND: readonly { label: string; meaning: string }[] = [
  { label: "blocks", meaning: "the first task must finish before the second can start" },
  { label: "parent of", meaning: "the second task is a subtask of the first" },
  { label: "claimed by", meaning: "the agent working on the task" },
  { label: "serves", meaning: "the task or plan works toward the goal" },
  { label: "delegates to", meaning: "one agent handed work to another" },
  { label: "authored", meaning: "the agent who wrote the plan" },
];

// --- Refusals (spec §9.2 board, §12 defect 2) ---------------------------------------

/** Explain why the hub refused a status move, in words a person can act on. */
export function explainRefusal(target: string, message: string, detail?: string): string {
  const text = `${message} ${detail ?? ""}`.toLowerCase();
  if (/evidence|pr url|pull request|merge/.test(text)) {
    return `Moving this task to ${statusLabel(target)} needs evidence first: a pull request link or other proof of the work. Open the task, add evidence, then move it again.`;
  }
  if (/review|reviewer|waiting_review/.test(text)) {
    return `Moving this task to ${statusLabel(target)} needs a review first. Send it to "Waiting for review" and let a reviewer move it on.`;
  }
  if (/claim|claimed|lock|held by|owner/.test(text)) {
    return `Another agent has claimed this task, so its status cannot change from here. Open the task to see who holds it.`;
  }
  return message;
}

/** Relative age in plain words: "3 days ago". */
export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "Unknown";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const future = t > now;
  const s = Math.round(Math.abs(now - t) / 1000);
  const say = (n: number, unit: string) => {
    const words = `${n} ${unit}${n === 1 ? "" : "s"}`;
    return future ? `in ${words}` : `${words} ago`;
  };
  if (s < 60) return future ? "in under a minute" : "just now";
  const m = Math.round(s / 60);
  if (m < 60) return say(m, "minute");
  const h = Math.round(m / 60);
  if (h < 24) return say(h, "hour");
  return say(Math.round(h / 24), "day");
}

/** `YYYY-MM-DD` for a date input from an ISO timestamp. */
export function dateInputValue(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : "";
}

/** Date input value → ISO the hub accepts (end of that day, UTC). */
export function dueIso(date: string): string {
  return `${date}T23:59:59Z`;
}

/** A link the hub recorded as evidence, when it is a URL. */
export function evidenceUrl(ref: string | null | undefined): string | null {
  if (!ref) return null;
  return /^https?:\/\//i.test(ref.trim()) ? ref.trim() : null;
}
