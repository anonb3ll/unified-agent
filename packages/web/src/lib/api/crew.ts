/**
 * Live crew transcript and interventions.
 * Re-exported from `hub.ts` (task #1575).
 */
import type { SessionRow } from "../sessions/types";

/** One typed message row from GET /api/sessions/{id}/crew. */
export type CrewMessage = {
  message_id?: string;
  act?: string;
  from_agent?: string;
  to_agent?: string;
  body?: string;
  conversation_id?: string;
  created_at?: string | null;
  task_id?: number | null;
  delegation_id?: number | null;
  [key: string]: unknown;
};

/** Live crew snapshot for a session (task #1025). */
type CrewLivePayload = {
  session: SessionRow | Record<string, unknown>;
  live: boolean;
  conversation_id: string;
  messages: CrewMessage[];
  message_total: number;
  interventions_path: string | null;
  worktree_interventions: CrewMessage[];
  error?: string;
};

/** TanStack Query key prefix for GET /api/sessions/{id}/crew. */
export const CREW_LIVE_QUERY_KEY = ["crew-live"] as const;

/**
 * Load the FirstMate-style live crew view for one session
 * (messages + worktree intervention drop).
 */
export async function fetchCrewLive(
  sessionId: string,
  signal?: AbortSignal,
): Promise<CrewLivePayload> {
  const response = await fetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/crew`,
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
      `crew live HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as CrewLivePayload;
  return {
    session: payload.session ?? {},
    live: Boolean(payload.live),
    conversation_id:
      typeof payload.conversation_id === "string"
        ? payload.conversation_id
        : "",
    messages: Array.isArray(payload.messages) ? payload.messages : [],
    message_total:
      typeof payload.message_total === "number" ? payload.message_total : 0,
    interventions_path:
      typeof payload.interventions_path === "string"
        ? payload.interventions_path
        : null,
    worktree_interventions: Array.isArray(payload.worktree_interventions)
      ? payload.worktree_interventions
      : [],
    error: payload.error,
  };
}
