/**
 * Sessions, agents and wake grants.
 * Re-exported from `hub.ts` (task #1575).
 */
import type {
  AgentsPayload,
  SessionsFilter,
  SessionsPayload,
} from "../sessions/types";
export type {
  ActivityEvent,
  AgentRow,
  AgentsPayload,
  DispatchRouteRow,
  DispatchRoutesPayload,
  SessionRow,
  SessionsFilter,
  SessionsPayload,
} from "../sessions/types";

/** TanStack Query key prefix for GET /api/sessions (filter appended). */
const SESSIONS_QUERY_KEY = ["sessions"] as const;

/** TanStack Query key for GET /api/agents. */
export const AGENTS_QUERY_KEY = ["agents"] as const;

/** Full query key for a sessions view, including its agent/status filter. */
export function sessionsQueryKey(filter: SessionsFilter = {}) {
  return [
    ...SESSIONS_QUERY_KEY,
    filter.agent ?? "all",
    filter.status ?? "all",
  ] as const;
}

/**
 * Load recent agent sessions via same-origin cookies (SSE/SPA path).
 * `agent` / `status` map straight onto the /api/sessions query params.
 */
export async function fetchSessions(
  filter: SessionsFilter = {},
  signal?: AbortSignal,
): Promise<SessionsPayload> {
  const qs = new URLSearchParams();
  if (filter.agent) qs.set("agent", filter.agent);
  if (filter.status === "open") qs.set("status", "open");
  const suffix = qs.toString() ? `?${qs}` : "";
  const response = await fetch(`/api/sessions${suffix}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `sessions HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as SessionsPayload;
  return {
    sessions: Array.isArray(payload.sessions) ? payload.sessions : [],
    open: typeof payload.open === "number" ? payload.open : 0,
    total: typeof payload.total === "number" ? payload.total : 0,
    can_grant_wake: payload.can_grant_wake === true,
    error: payload.error,
  };
}

/** Load the agent roster + status counts via same-origin cookies. */
export async function fetchAgents(
  signal?: AbortSignal,
): Promise<AgentsPayload> {
  const response = await fetch("/api/agents", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `agents HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as AgentsPayload;
  return {
    agents: Array.isArray(payload.agents) ? payload.agents : [],
    counts:
      payload.counts && typeof payload.counts === "object"
        ? payload.counts
        : {},
    error: payload.error,
  };
}
