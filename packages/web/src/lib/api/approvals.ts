/**
 * Approval queue, approval detail and decisions.
 * Re-exported from `hub.ts` (task #1575).
 */
import { fetchCsrfToken, readErrorDetail } from "./http";

/** TanStack Query key for GET /api/approvals. */
export const APPROVALS_QUERY_KEY = ["approvals"] as const;

/** Operator-facing approval row (payload/reason stripped by the hub). */
export type PublicApprovalRow = {
  id: number;
  tool?: string;
  tool_name?: string;
  requested_by?: string | null;
  /** Caller's current run at request time, or null. Display linkage only. */
  run_id?: string | null;
  status?: string;
  created_at?: string | null;
  requested_at?: string | null;
  updated_at?: string | null;
  source_host?: string | null;
  hub_origin?: string | null;
  outcome?: string | null;
  expires_at?: string | null;
  expires_in_hours?: number | null;
  expired?: boolean;
  platform?: { kind: string; id: string; uri: string } | null;
  links?: { task?: { kind: string; id: string; uri: string } | null };
};

export type ApprovalsPayload = {
  pending: PublicApprovalRow[];
  approved_unconsumed: PublicApprovalRow[];
  expired_passes: PublicApprovalRow[];
  /** How many expired passes exist; the list is capped to the newest (task #1786). */
  expired_passes_total?: number;
  expired_passes_truncated?: boolean;
  gated_tools?: string[];
  pass_ttl_hours?: number | null;
  error?: string;
  errors?: string[];
};

export type ApprovalDecision = "approve" | "deny";

/** Gate that parked an approval request. Today always `global_allowlist` —
 * see `tools.approvals.check_approval_gate`; the shape leaves room for a
 * second gate kind without a breaking change to the drawer. */
type ApprovalGate = {
  kind: string;
  label: string;
};

/** Full single-row detail for GET /api/approvals/{id} — the drawer payload
 * (task #1263). The server has already redacted secret-shaped values in
 * `payload` (see `sanitize_metadata`); the client never re-derives that. */
export type ApprovalDetail = {
  id: number;
  tool?: string;
  status?: string;
  outcome?: string;
  requested_by?: string | null;
  /** Caller's current run at request time, or null. Display linkage only. */
  run_id?: string | null;
  device?: string | null;
  /** #1217 honesty rule applied to devices: only true when the hub itself
   * observed the request (`last_seen_via === "hub_request"`); a
   * self-reported heartbeat host is never rendered as confirmed. */
  device_verified?: boolean;
  resolved_by?: string | null;
  reason?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  resolved_at?: string | null;
  gate?: ApprovalGate;
  payload?: Record<string, unknown>;
  /** All bounded review fields are displayed without truncation or redaction. */
  review_display_complete?: boolean | null;
  pr_url?: string | null;
  commit_sha?: string | null;
  platform?: { kind: string; id: string; uri: string } | null;
  links?: { task?: { kind: string; id: string; uri: string } | null };
  error?: string;
};

/** Prefix shared by every drawer detail key; invalidates all open drawers. */
export const APPROVAL_DETAIL_QUERY_PREFIX = ["approval-detail"] as const;

/**
 * Key for one approval's drawer detail. Deliberately NOT prefixed by
 * {@link APPROVALS_QUERY_KEY}: TanStack invalidation matches by prefix, so a
 * nested key made every list refresh (poll, SSE, decision) refetch an open
 * drawer too (task #1786). A decision invalidates its own detail explicitly,
 * approval events invalidate {@link APPROVAL_DETAIL_QUERY_PREFIX}, and an
 * open drawer polls on its own interval.
 */
export function approvalDetailQueryKey(id: number) {
  return [...APPROVAL_DETAIL_QUERY_PREFIX, id] as const;
}

/**
 * Load one approval's full detail for the Control Center drawer (task #1263).
 * Distinct from `fetchApprovals`: that list view strips `payload`/`reason`
 * for compactness; this opt-in read returns them redacted, not stripped.
 */
export async function fetchApprovalDetail(
  id: number,
  signal?: AbortSignal,
): Promise<ApprovalDetail> {
  const response = await fetch(
    `/api/approvals/${encodeURIComponent(String(id))}`,
    {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal,
    },
  );
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `approval detail HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  return (await response.json()) as ApprovalDetail;
}

/**
 * Last `/api/approvals` body and its ETag, for conditional polls (task #1786).
 * The ETag is a hash of the exact body, so a 304 means this payload is still
 * current for whoever is asking.
 */
let approvalsValidator: {
  etag: string;
  payload: ApprovalsPayload;
  generation: number;
} | null = null;
/**
 * Start order of `fetchApprovals` calls. Overlapping reads can finish out of
 * order; only a read that started after the stored validator's may replace
 * it, so an older snapshot finishing last never becomes the 304 baseline.
 */
let approvalsGeneration = 0;
/** Generation of the newest read that has stored (or cleared) the validator. */
let approvalsStoredGeneration = 0;

/** Forget the cached approvals validator (tests; sign-out). */
export function resetApprovalsValidator(): void {
  approvalsValidator = null;
  approvalsStoredGeneration = approvalsGeneration;
}

/**
 * Load the approvals queue via same-origin cookies.
 *
 * Sends `If-None-Match` with the last ETag; a 304 reuses the cached payload
 * instead of transferring and re-parsing an unchanged queue. `cache:
 * "no-store"` keeps the browser HTTP cache out of it, so the validator here is
 * the only one in play and a 304 always reaches this code.
 */
export async function fetchApprovals(
  signal?: AbortSignal,
): Promise<ApprovalsPayload> {
  const generation = ++approvalsGeneration;
  const cached = approvalsValidator;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (cached) headers["If-None-Match"] = cached.etag;
  const response = await fetch("/api/approvals", {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers,
    signal,
  });
  if (response.status === 304) {
    if (cached) return cached.payload;
    throw new Error("approvals HTTP 304 without a cached payload");
  }
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `approvals HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as ApprovalsPayload;
  const normalized: ApprovalsPayload = {
    pending: Array.isArray(payload.pending) ? payload.pending : [],
    approved_unconsumed: Array.isArray(payload.approved_unconsumed)
      ? payload.approved_unconsumed
      : [],
    expired_passes: Array.isArray(payload.expired_passes)
      ? payload.expired_passes
      : [],
    expired_passes_total:
      typeof payload.expired_passes_total === "number"
        ? payload.expired_passes_total
        : undefined,
    expired_passes_truncated: payload.expired_passes_truncated,
    gated_tools: payload.gated_tools,
    pass_ttl_hours: payload.pass_ttl_hours ?? null,
    error: payload.error,
    errors: payload.errors,
  };
  if (generation > approvalsStoredGeneration) {
    const etag = response.headers?.get?.("etag") ?? null;
    approvalsStoredGeneration = generation;
    approvalsValidator = etag ? { etag, payload: normalized, generation } : null;
  }
  return normalized;
}

/**
 * POST /api/action/approval with CSRF. On 403 csrf errors, refresh once and retry
 * (mirrors legacy app.js api()/submitAction).
 */
export async function postApprovalDecision(
  id: number,
  decision: ApprovalDecision,
  signal?: AbortSignal,
  reason?: string,
): Promise<unknown> {
  let csrf = await fetchCsrfToken(signal);

  const attempt = async (token: string, retried: boolean): Promise<unknown> => {
    const response = await fetch("/api/action/approval", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "x-ua-csrf": token,
      },
      body: JSON.stringify(
        reason && reason.trim()
          ? { id, decision, reason: reason.trim() }
          : { id, decision },
      ),
      signal,
    });

    if (response.status === 403 && !retried) {
      const detail = await readErrorDetail(response);
      if (/csrf/i.test(detail)) {
        csrf = await fetchCsrfToken(signal);
        return attempt(csrf, true);
      }
    }

    if (!response.ok) {
      const detail = await readErrorDetail(response);
      throw new Error(detail || `approval action HTTP ${response.status}`);
    }

    return response.json();
  };

  return attempt(csrf, false);
}

/**
 * A call the hub parked for human approval: 202 `{approval_id, status: "pending",
 * code}` (routes._approval_pending). `postCsrfJson` resolves it like any 2xx, so
 * callers must check for it before treating the answer as done.
 */
export type ApprovalPendingAnswer = {
  status: "pending" | "approval_pending";
  approval_id: number;
  code?: string;
};
