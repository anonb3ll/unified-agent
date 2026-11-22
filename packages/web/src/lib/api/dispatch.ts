/**
 * Dispatch actions, dispatch stack, routes and receipts.
 * Re-exported from `hub.ts` (task #1575).
 */
import type { DispatchContextType } from "@unified-agent/protocol";
import type { DispatchRoutesPayload } from "../sessions/types";
import { readErrorDetail } from "./http";

export const DISPATCH_ROUTES_QUERY_KEY = ["dispatch-routes"] as const;

export async function fetchDispatchRoutes(
  signal?: AbortSignal,
): Promise<DispatchRoutesPayload> {
  const response = await fetch("/api/dispatch-routes", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`dispatch routes HTTP ${response.status}`);
  }
  const payload = (await response.json()) as DispatchRoutesPayload;
  return {
    routes: Array.isArray(payload.routes) ? payload.routes : [],
    scheduler: payload.scheduler ?? { enabled: false, real_spawn: false },
    model_defaults: payload.model_defaults ?? {},
    model_options: Array.isArray(payload.model_options)
      ? payload.model_options
      : [],
  };
}

/** Context flags accepted by POST /api/action/dispatch. */
export const DISPATCH_CONTEXT_OPTIONS = [
  "task_notes",
  "recent_validation",
  "related_pr",
] as const satisfies readonly DispatchContextType[];
/** The protocol `DispatchContext` enum. */
export type DispatchContextFlag = DispatchContextType;

type DispatchStackItem = {
  task_id: number;
  eligible?: boolean;
  reason?: string | null;
  title?: string | null;
  [key: string]: unknown;
};

export type DispatchStackPreview = {
  next_task_id?: number | null;
  items: DispatchStackItem[];
  error?: string;
};

export async function fetchDispatchStack(
  taskIds: number[],
  project: string,
  signal?: AbortSignal,
): Promise<DispatchStackPreview> {
  const qs = new URLSearchParams({
    task_ids: taskIds.join(","),
    project,
  });
  const response = await fetch(`/api/dispatch-stack?${qs}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || `dispatch-stack HTTP ${response.status}`);
  }
  const payload = (await response.json()) as DispatchStackPreview;
  return {
    next_task_id: payload.next_task_id ?? null,
    items: Array.isArray(payload.items) ? payload.items : [],
    error: payload.error,
  };
}

/**
 * One dispatch delivery-receipt summary row from GET /api/dispatch_receipts
 * (task #1182). A projection of the existing dispatch_runs ledger, not a
 * second one — call ``dispatch_delivery_receipt(run_id)`` via MCP for the
 * full accountability receipt (transitions, policy/approval outcome).
 */
export type DispatchReceiptRow = {
  run_id: string;
  task_id: number;
  delegation_id: number | null;
  agent_label: string;
  state: string;
  actor: string;
  lease_expires_at: string | null;
  failure_reason: string | null;
  failure_class: string | null;
  evidence_ref: string | null;
  termination_outcome: string | null;
  created_at: string;
  updated_at: string;
  terminal: boolean;
  has_evidence: boolean;
};

export type DispatchReceiptsPayload = {
  receipts: DispatchReceiptRow[];
  count?: number;
  terminal_count?: number;
  error?: string;
  code?: string;
};

/**
 * Load recent dispatch delivery-receipt summaries via same-origin cookies.
 * Named unavailable errors arrive as HTTP 200 with error/code fields.
 */
export async function fetchDispatchReceipts(
  signal?: AbortSignal,
): Promise<DispatchReceiptsPayload> {
  const response = await fetch("/api/dispatch_receipts", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || "dispatch delivery receipts unavailable");
  }
  const payload = (await response.json()) as DispatchReceiptsPayload;
  return {
    receipts: Array.isArray(payload.receipts) ? payload.receipts : [],
    count: payload.count,
    terminal_count: payload.terminal_count,
    error: payload.error,
    code: payload.code,
  };
}
