/**
 * Work area reads and write paths (spec §7: new fetchers live here).
 *
 * Reads that already exist are imported from `src/api/*` by the hooks; this
 * file adds the ones the classic UI never made (`/api/platform/objects/task`,
 * `/api/runs?task_id=`, `/api/goals/{id}`, `/api/plans/{id}/joint`) and names
 * every write path. Routes the hub does not serve yet (tasks #2469 and #2480)
 * are collected in `PENDING_HUB_ROUTES` so the PR body and the tests can list
 * them; each request shape here is the one this slice assumes.
 */
import { fetchWorkGraph, type WorkGraphPayload } from "../../lib/api/graph";
import type { BoardCard, BoardPayload } from "../../lib/api/tasks";

/** A failed read that keeps its HTTP status for `describeReadError`. */
export class HubReadError extends Error {
  readonly status: number;
  /** The hub's `error` text, when the body carried one. */
  readonly hubError: string | null;
  constructor(status: number, what: string, detail = "") {
    super(`${what} HTTP ${status}${detail ? `: ${detail.slice(0, 200)}` : ""}`);
    this.name = "HubReadError";
    this.status = status;
    let hubError: string | null = null;
    try {
      const parsed = JSON.parse(detail) as { error?: unknown };
      if (typeof parsed.error === "string") hubError = parsed.error;
    } catch {
      hubError = null;
    }
    this.hubError = hubError;
  }
}

export async function getJson<T>(path: string, what: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new HubReadError(response.status, what, detail);
  }
  return (await response.json()) as T;
}

// --- Query keys -------------------------------------------------------------

export const WORK_KEYS = {
  board: ["work", "board"] as const,
  task: (id: string) => ["work", "task", id] as const,
  taskRuns: (id: string) => ["work", "task", id, "runs"] as const,
  taskEvidence: (id: string) => ["work", "task", id, "evidence"] as const,
  taskComments: (id: string) => ["work", "task", id, "comments"] as const,
  taskWhy: (id: string) => ["work", "task", id, "why"] as const,
  taskGraph: ["work", "task-graph"] as const,
  workGraph: (goalId: string | null) => ["work", "work-graph", goalId ?? "roots"] as const,
  goals: ["work", "goals"] as const,
  goal: (id: string) => ["work", "goal", id] as const,
  plans: ["work", "plans"] as const,
  plan: (id: string) => ["work", "plan", id] as const,
  joint: (id: string) => ["work", "plan", id, "joint"] as const,
  agents: ["work", "agents"] as const,
  projects: ["work", "projects"] as const,
};

/** Everything a task write can change, refetched after it. */
export const TASK_WRITE_INVALIDATES = [["work"]] as const;

// --- Task --------------------------------------------------------------------

export type TaskStatus = "open" | "doing" | "waiting_review" | "blocked" | "done" | "cancelled";
export type TaskPriority = "high" | "med" | "low";

/** A full task row (`GET /api/platform/objects/task/{id}` → `task`). */
export type TaskRow = {
  id: number;
  title: string;
  status: TaskStatus | string;
  priority?: TaskPriority | string | null;
  notes?: string | null;
  parent_id?: number | null;
  project?: string | null;
  domain?: string | null;
  lane?: string | null;
  due_at?: string | null;
  evidence_ref?: string | null;
  claimed_by?: string | null;
  claimed_at?: string | null;
  lock_expires_at?: string | null;
  current_run_id?: string | null;
  worktree_path?: string | null;
  branch?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  archived_at?: string | null;
  untrusted?: boolean | number | null;
  waiting_for_review?: boolean | number | null;
  [key: string]: unknown;
};

export type TaskObject = { uri: string; task: TaskRow };

export function fetchTask(id: string, signal?: AbortSignal): Promise<TaskObject> {
  return getJson<TaskObject>(`/api/platform/objects/task/${encodeURIComponent(id)}`, "task", signal);
}

export type RunRow = {
  run_id: string;
  kind: string;
  task_id?: number | null;
  agent?: string | null;
  status?: string | null;
  outcome?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  [key: string]: unknown;
};

export type RunsPayload = { runs: RunRow[]; next_cursor?: string | null; error?: string };

export async function fetchTaskRuns(id: string, signal?: AbortSignal, cursor?: string | null): Promise<RunsPayload> {
  const params = new URLSearchParams({ task_id: id, limit: "20" });
  if (cursor) params.set("cursor", cursor);
  const payload = await getJson<RunsPayload>(`/api/runs?${params}`, "runs", signal);
  return { ...payload, runs: Array.isArray(payload.runs) ? payload.runs : [] };
}

export async function fetchBoardAll(signal?: AbortSignal): Promise<BoardPayload> {
  const payload = await getJson<BoardPayload>("/api/board?include_cancelled=1", "board", signal);
  return { ...payload, columns: Array.isArray(payload.columns) ? payload.columns : [] };
}

export type { BoardCard, BoardPayload };

// --- Goals -------------------------------------------------------------------

export type GoalRow = {
  id: number;
  title: string;
  statement?: string | null;
  status?: string | null;
  project?: string | null;
  parent_goal_id?: number | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  /** #2480: advanced by every goal update; sent back as expected_revision. */
  revision?: number;
  children?: GoalRow[];
  [key: string]: unknown;
};

export type GoalTree = { tree: GoalRow[]; meaning?: string; error?: string };

export async function fetchGoals(signal?: AbortSignal): Promise<GoalTree> {
  const payload = await getJson<GoalTree>("/api/goals", "goals", signal);
  return { ...payload, tree: Array.isArray(payload.tree) ? payload.tree : [] };
}

export type GoalDetail = { goal: GoalRow; ancestry: GoalRow[]; meaning?: string };

export function fetchGoal(id: string, signal?: AbortSignal): Promise<GoalDetail> {
  return getJson<GoalDetail>(`/api/goals/${encodeURIComponent(id)}`, "goal", signal);
}

// --- Plans -------------------------------------------------------------------

export type JointMember = { identity: string; role: "lead" | "member" | string };

/**
 * `GET /api/plans/{id}/joint` (task #2469 over `plan_manage` `joint_get`):
 * 200 with the joint view; 400 when the plan is not joint or the caller is
 * not a member of its office (the body names which); 404 for an unknown plan.
 */
export type JointView = {
  plan_id: string;
  revision: number;
  goal_id: number;
  org_unit_id: string;
  actor?: string | null;
  role?: string | null;
  action?: string | null;
  /** joint_planning.propose: status "proposed", or "changes_requested" after a review asked for changes. */
  pending?: { steps?: unknown[]; author?: string | null; comment_id?: number | null; status?: string; role?: string; office?: string; [key: string]: unknown } | null;
  /** joint_planning.review: the latest review. */
  review?: { decision?: string | null; reviewer?: string | null; author?: string | null; role?: string; comment_id?: number | null; [key: string]: unknown } | null;
  decisions?: { text?: string; author?: string | null; revision?: number; role?: string; comment_id?: number; goal_id?: number; at?: string; [key: string]: unknown }[];
  steps?: unknown[];
  team: JointMember[];
  recovered?: boolean;
  /** The caller's own `team` entry, or null when the caller is not on the team (docs/control-center-api.md). */
  caller?: JointMember | null;
  [key: string]: unknown;
};

export type JointAction = "bind" | "propose" | "review" | "decide" | "recover";

export function fetchJoint(planId: string, signal?: AbortSignal): Promise<JointView> {
  return getJson<JointView>(`/api/plans/${encodeURIComponent(planId)}/joint`, "joint plan", signal);
}

// --- Write paths ---------------------------------------------------------------

export const WRITE_PATHS = {
  taskCreate: "/api/tasks",
  taskAction: "/api/action/task",
  taskClaim: "/api/action/task/claim",
  taskDelete: "/api/action/task/delete",
  claimRelease: (id: number | string) => `/api/tasks/${id}/claim-release`,
  comments: (id: number | string) => `/api/tasks/${id}/comments`,
  dependency: "/api/action/dependency",
  holds: "/api/tasks/holds",
  holdRelease: (holdId: number) => `/api/tasks/holds/${holdId}/release`,
  evidence: "/api/evidence",
  workProducts: "/api/work-products",
  workProductRelease: (id: number | string) => `/api/work-products/${id}/release`,
  goals: "/api/goals",
  goalAction: (action: GoalAction) => `/api/action/goal/${action}`,
  planReview: "/api/action/plan",
  planAction: (action: PlanAction) => `/api/action/plan/${action}`,
  jointAction: (action: JointAction) => `/api/action/plan/joint/${action}`,
  canvas: (board: string) => `/api/graph-canvas?board=${encodeURIComponent(board)}`,
} as const;

/** Plan actions over `POST /api/action/plan/{action}` (#2469) and the structure edits (#2480). */
export type PlanAction =
  | "create"
  | "update"
  | "checkpoint"
  | "resume"
  | "retry"
  | "claim"
  | "handoff"
  | "close"
  | "delete"
  | "edit"
  | "step-add"
  | "step-edit"
  | "step-remove"
  | "reorder";

/** Goal writes over `POST /api/action/goal/<action>` (#2480, origin/task/2480-cc2-hub-operations). */
export type GoalAction = "edit" | "link" | "unlink" | "close" | "reopen";

/** Fields `POST /api/action/task` accepts (`routes._TASK_UPDATE_PARAMS`, plus `archived` from #2469). */
export const TASK_UPDATE_FIELDS = [
  "status",
  "title",
  "notes",
  "append_note",
  "priority",
  "parent_id",
  "untrusted",
  "worktree",
  "project",
  "expected_project",
  "expected_updated_at",
  "domain",
  "lane",
  "due_at",
  "clear_due_at",
  "evidence_ref",
  "idempotency_key",
  "archived",
] as const;

export type TaskUpdateField = (typeof TASK_UPDATE_FIELDS)[number];
export type TaskUpdateBody = { id: number } & Partial<Record<TaskUpdateField, unknown>>;

/**
 * Hub routes and fields this slice uses that are published on
 * `origin/task/2480-cc2-hub-operations` (docs/control-center-api.md) but not
 * yet merged or reviewed. Until they land, a hub without them answers 404,
 * shown as "this hub does not offer this action".
 */
export const PENDING_HUB_ROUTES: readonly { route: string; body: string; source: string }[] = [
  { route: "POST /api/action/plan/{step-add,step-edit,step-remove,reorder,edit}", body: "plan_id plus the action's fields and expected_updated_at", source: "origin/task/2480-cc2-hub-operations" },
  { route: "POST /api/action/goal/{edit,link,unlink,close,reopen}", body: "goal_id (and task_id for link/unlink) plus the action's fields", source: "origin/task/2480-cc2-hub-operations" },
  { route: "GET /api/board: domain on every task card", body: "personal | professional | hobby | null", source: "origin/task/2480-cc2-hub-operations" },
  { route: "GET /api/overview: dependency_id on task_graph depends_on links", body: "the task_dependencies row id, for POST /api/action/dependency remove", source: "origin/task/2480-cc2-hub-operations" },
];

/** Pages read before a goal's work graph is reported incomplete. */
export const WORK_GRAPH_MAX_PAGES = 20;

/**
 * Every page of `/api/work-graph?goal_id=` (keyset `next_cursor`), merged.
 * `complete` is false when the page cap was reached first, so progress is
 * shown as page-local with the aggregate unknown.
 */
export async function fetchWorkGraphAll(
  goalId: number,
  signal?: AbortSignal,
  startCursor: string | null = null,
): Promise<WorkGraphPayload & { complete: boolean; pages: number }> {
  const nodes = new Map<string, WorkGraphPayload["nodes"][number]>();
  const edges: WorkGraphPayload["edges"] = [];
  const seenEdges = new Set<string>();
  let cursor: string | null = startCursor;
  let first: WorkGraphPayload | null = null;
  let pages = 0;
  do {
    const page: WorkGraphPayload = await fetchWorkGraph({ goalId, cursor, pageSize: 200 }, signal);
    first ??= page;
    pages += 1;
    for (const n of page.nodes ?? []) if (!nodes.has(n.id) || !n.stub) nodes.set(n.id, n);
    for (const e of page.edges ?? []) {
      const key = `${e.kind}|${e.from}|${e.to}`;
      if (!seenEdges.has(key)) {
        seenEdges.add(key);
        edges.push(e);
      }
    }
    cursor = page.next_cursor;
  } while (cursor && pages < WORK_GRAPH_MAX_PAGES);
  return { ...(first as WorkGraphPayload), nodes: [...nodes.values()], edges, next_cursor: cursor, complete: !cursor, pages };
}

/** Merge two reads of the same graph (later pages), nodes by id and edges by kind and ends. */
export function mergeWorkGraphs<T extends WorkGraphPayload>(a: T, b: WorkGraphPayload): T {
  const nodes = new Map(a.nodes.map((n) => [n.id, n]));
  for (const n of b.nodes) if (!nodes.has(n.id) || !n.stub) nodes.set(n.id, n);
  const seen = new Set(a.edges.map((e) => `${e.kind}|${e.from}|${e.to}`));
  const edges = [...a.edges, ...b.edges.filter((e) => !seen.has(`${e.kind}|${e.from}|${e.to}`))];
  return { ...a, nodes: [...nodes.values()], edges, next_cursor: b.next_cursor };
}
