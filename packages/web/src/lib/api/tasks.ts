/**
 * Board, task actions, holds, escalation, search and comments.
 * Re-exported from `hub.ts` (task #1575).
 */
import { readErrorDetail } from "./http";
export type { Task, TaskStatusType } from "@unified-agent/protocol";

/** Board card from board_payload (task or delegation). */
export type BoardCard = {
  kind?: string;
  id: number | string;
  title?: string | null;
  status?: string | null;
  priority?: string | null;
  claimed_by?: string | null;
  from_agent?: string | null;
  to_agent?: string | null;
  project?: string | null;
  lane?: string | null;
  age_s?: number | null;
  alerts?: string[];
  blocked_reason?: string | null;
  subtasks?: BoardCard[];
  swimlane?: string | null;
  [key: string]: unknown;
};

type BoardColumn = {
  status: string;
  label?: string;
  cards: BoardCard[];
  count?: number;
  hidden_by_default?: boolean;
};

type BoardWipLane = {
  count: number;
  cap: number | null;
  over?: boolean;
  mode?: string;
};

export type BoardPayload = {
  as_of?: string;
  columns: BoardColumn[];
  swimlane_by?: string;
  wip?: {
    lanes?: Record<string, BoardWipLane>;
    agents?: Record<string, BoardWipLane>;
    agent_cap?: number | null;
  };
  alerts?: unknown[];
  filters?: Record<string, unknown>;
  thresholds?: Record<string, unknown>;
  error?: string;
  errors?: string[];
};

/**
 * Task subtree hold (task #1747) from GET /api/tasks/holds. Members are the
 * root's subtree as snapshotted when the hold was placed.
 */
export type TaskHold = {
  id: number;
  root_task_id: number;
  mode: "pause" | "cancel";
  status: "active" | "released";
  reason: string;
  release_policy: "manual" | "after_active_runs_finish";
  created_by: string;
  created_at: string;
  member_ids: number[];
};

/** TanStack Query key for GET /api/tasks/holds (active holds). */
export const TASK_HOLDS_QUERY_KEY = ["tasks", "holds"] as const;

function toTaskHold(raw: unknown): TaskHold | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.id !== "number" || typeof r.root_task_id !== "number") return null;
  return {
    id: r.id,
    root_task_id: r.root_task_id,
    mode: r.mode === "cancel" ? "cancel" : "pause",
    status: r.status === "released" ? "released" : "active",
    reason: typeof r.reason === "string" ? r.reason : "",
    release_policy:
      r.release_policy === "after_active_runs_finish" ? "after_active_runs_finish" : "manual",
    created_by: typeof r.created_by === "string" ? r.created_by : "",
    created_at: typeof r.created_at === "string" ? r.created_at : "",
    member_ids: Array.isArray(r.member_ids)
      ? r.member_ids.filter((v): v is number => typeof v === "number")
      : [],
  };
}

/** Active holds. A payload without a `holds` list reads as none held. */
export async function fetchTaskHolds(signal?: AbortSignal): Promise<TaskHold[]> {
  const response = await fetch("/api/tasks/holds", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || `task holds HTTP ${response.status}`);
  }
  const payload: unknown = await response.json();
  const holds = (payload as { holds?: unknown } | null)?.holds;
  return Array.isArray(holds)
    ? holds.map(toTaskHold).filter((h): h is TaskHold => h != null && h.status === "active")
    : [];
}

/** Task id -> the active hold that holds it (the oldest when several do). */
export function heldTaskMap(holds: readonly TaskHold[]): Map<number, TaskHold> {
  const map = new Map<number, TaskHold>();
  for (const hold of [...holds].sort((a, b) => a.id - b.id)) {
    for (const taskId of hold.member_ids) {
      if (!map.has(taskId)) map.set(taskId, hold);
    }
  }
  return map;
}

/**
 * Ranked task search (task #1419). One row from GET /api/tasks/search --
 * mirrors the MCP task_list(query=...) shape (task_list's normal fields plus
 * combined_score/explanation), kept loose with an index signature the same
 * way VaultSearchMatch is, rather than a full Zod schema, since this is a
 * read-only search projection, not the task object model itself.
 */
type TaskSearchExplanation = {
  why: string[];
  lexical: number | null;
  semantic: number | null;
  recency: number | null;
  salience: number | null;
  access: number | null;
  not_applicable: string[];
  snippet?: string | null;
  [key: string]: unknown;
};

export type TaskSearchResult = import("@unified-agent/client").QueueHealthTask & {
  combined_score: number;
  explanation: TaskSearchExplanation;
  [key: string]: unknown;
};

type TaskSearchPayload = {
  tasks: TaskSearchResult[];
  query?: string;
  error?: string;
  truncated?: boolean;
  limit?: number;
};

export async function fetchTaskSearch(
  q: string,
  signal?: AbortSignal,
): Promise<TaskSearchPayload> {
  const params = new URLSearchParams({ q });
  const response = await fetch(`/api/tasks/search?${params}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || `task search HTTP ${response.status}`);
  }
  const payload = (await response.json()) as TaskSearchPayload;
  if (payload.error) throw new Error(payload.error);
  return {
    tasks: Array.isArray(payload.tasks) ? payload.tasks : [],
    query: payload.query,
    error: payload.error,
    truncated: payload.truncated,
    limit: payload.limit,
  };
}

type TaskCommentRow = {
  id: number;
  author: string;
  body: string;
  parent_comment_id: number | null;
};

type TaskCommentsPayload = {
  revision: number;
  comments: TaskCommentRow[];
};

export async function fetchTaskComments(
  taskId: string,
  signal?: AbortSignal,
): Promise<TaskCommentsPayload> {
  const response = await fetch(`/api/tasks/${taskId}/comments`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`comments HTTP ${response.status}`);
  }
  const payload = (await response.json()) as TaskCommentsPayload;
  return {
    revision: payload.revision ?? 0,
    comments: Array.isArray(payload.comments) ? payload.comments : [],
  };
}

// --- Work graph (task #1460): goal -> plan -> task -> run/session/evidence ---
