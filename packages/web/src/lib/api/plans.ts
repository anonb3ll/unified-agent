/**
 * Plans, plan detail and plan review.
 * Re-exported from `hub.ts` (task #1575).
 */
import { readErrorDetail } from "./http";

/** One plan row from GET /api/plans (plan_manage list). */
export type PlanRow = {
  id: string;
  goal?: string | null;
  scope?: string | null;
  status?: string | null;
  author?: string | null;
  escalated?: number | boolean | null;
  needs_human?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
};

type PlansPayload = {
  plans: PlanRow[];
  error?: string;
};

export type PlanStepRow = {
  plan_id?: string;
  step_idx: number;
  description?: string | null;
  owner?: string | null;
  status?: string | null;
  note?: string | null;
  outputs?: Record<string, unknown> | null;
  depends_on?: unknown;
  retries?: number | null;
  [key: string]: unknown;
};

type PlanDetailPayload = {
  plan: PlanRow;
  steps: PlanStepRow[];
  checkpoints?: unknown[];
  next_step_idx?: number | null;
  error?: string;
};

type PlanReviewAction = "approve" | "changes";

export async function fetchPlans(
  signal?: AbortSignal,
  status = "all",
): Promise<PlansPayload> {
  const params = new URLSearchParams({ status });
  const response = await fetch(`/api/plans?${params}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `plans HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as PlansPayload;
  return {
    plans: Array.isArray(payload.plans) ? payload.plans : [],
    error: payload.error,
  };
}

export async function fetchPlanDetail(
  planId: string,
  signal?: AbortSignal,
): Promise<PlanDetailPayload> {
  const response = await fetch(`/api/plans/${encodeURIComponent(planId)}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || `plan detail HTTP ${response.status}`);
  }
  const payload = (await response.json()) as PlanDetailPayload;
  return {
    plan: payload.plan,
    steps: Array.isArray(payload.steps) ? payload.steps : [],
    checkpoints: payload.checkpoints,
    next_step_idx: payload.next_step_idx,
    error: payload.error,
  };
}

/** Build the fixed plan-review v1 intent schema for approve/changes. */
export function planReviewIntent(
  action: PlanReviewAction,
  planId: string,
): string {
  return `plan-review v1 ${action} ${planId}`;
}
