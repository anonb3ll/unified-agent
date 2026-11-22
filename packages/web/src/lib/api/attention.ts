/**
 * Attention inbox (GET /api/attention).
 * Re-exported from `hub.ts` (task #1575).
 */
import type { AttentionPayload } from "../attention/types";
export type {
  AttentionItem,
  AttentionPayload,
  AttentionSeverity,
  AttentionSource,
} from "../attention/types";

/** TanStack Query key for GET /api/attention. */
export const ATTENTION_QUERY_KEY = ["attention"] as const;

/**
 * Load attention items via same-origin cookies (SSE/SPA path).
 */
export async function fetchAttention(
  signal?: AbortSignal,
): Promise<AttentionPayload> {
  const response = await fetch("/api/attention", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `attention HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as AttentionPayload;
  return {
    items: Array.isArray(payload.items) ? payload.items : [],
    informational: Array.isArray(payload.informational)
      ? payload.informational
      : [],
    counts:
      payload.counts && typeof payload.counts === "object"
        ? payload.counts
        : {},
    total: typeof payload.total === "number" ? payload.total : 0,
    errors: Array.isArray(payload.errors) ? payload.errors : [],
    omitted: payload.omitted,
    error: payload.error,
    needs_operator: payload.needs_operator,
  };
}
