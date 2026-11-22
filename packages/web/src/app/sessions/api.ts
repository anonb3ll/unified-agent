/**
 * Sessions area data layer (spec §7: new fetchers live here).
 *
 * Reads reuse the legacy fetchers where they exist (`src/api/*`, read-only).
 * This file adds the reads the legacy UI never made (one session by id, the
 * operator flag without a full list read) and names every write path the area
 * uses, so the hub contract is visible in one place.
 *
 * Hub routes the area needs that do not exist yet are marked NEEDS-FROM-HUB
 * with the request shape assumed (spec §8; no `docs/control-center-api.md`
 * was published on a hub branch when this slice was written).
 */
import type { SessionRow } from "../../lib/sessions/types";

export type { SessionRow };

/** Query keys owned by this area. */
export const sessionQueryKeys = {
  all: ["cc2", "sessions"] as const,
  list: (filter: { agent: string; status: string }) => ["cc2", "sessions", "list", filter.agent, filter.status] as const,
  detail: (id: string) => ["cc2", "sessions", "detail", id] as const,
  operator: ["cc2", "sessions", "operator"] as const,
  transcript: (itemId: string) => ["cc2", "sessions", "transcript", itemId] as const,
  crew: (id: string) => ["cc2", "sessions", "crew", id] as const,
  stackPreview: (taskIds: readonly number[], project: string) =>
    ["cc2", "sessions", "stack-preview", project, ...taskIds] as const,
  teamSnapshots: ["cc2", "sessions", "team-snapshots"] as const,
};

/** Every write this area makes, by hub route. */
export const WRITE_PATHS = {
  /** Start an agent on the hub machine: `{agent, project, prompt, task_id?, context?, model?}`; 202 accepted. */
  dispatch: "/api/action/dispatch",
  /** Launch the next dependency-ready task of a set: `{task_ids, project, expected_lane?}`. */
  dispatchStack: "/api/action/dispatch-stack",
  /** Start, cancel or restart work on a device: `{action, ...}`. */
  nodeSession: "/api/action/node-session",
  /** Queue or steer a live device session: `{item_id, mode, body, idempotency_key}`. */
  nodeFollowup: "/api/action/node-followup",
  /** Reopen a finished device session with a new message: `{item_id, prompt}`. */
  nodeResume: "/api/action/node-resume",
  /** Adopt a session found on a device and continue it: `{node, harness, external_session_id, prompt, backend?}`. */
  nodeAdopt: "/api/action/node-adopt",
  /** Create a delegated task for another agent: `{task_spec, to_agent, parent_task_id?}`. */
  handoff: "/api/action/handoff",
  /** Send a message into a crew conversation: `{session_id, body}`. */
  crewIntervene: "/api/action/crew-intervene",
  /** Grant or revoke idle wakes (operator only): `{grant}`. */
  wakeGrant: (sessionId: string) => `/api/sessions/${encodeURIComponent(sessionId)}/wake-grant`,
  /**
   * `session_end` for the caller's own session (hub task #2469,
   * docs/control-center-api.md): `{session_id, status, exit_status?, summary?}`
   * with `status` completed | failed | abandoned; 200
   * `{session_id, status, closed: true, owed?}`; 400 when the session belongs to
   * another identity; 404 for an unknown session.
   */
  sessionEnd: "/api/action/session/end",
  /**
   * NEEDS-FROM-HUB (task #2480, unmerged branch `task/2480-cc2-hub-operations`,
   * `webui/cc_work.session_operator_end`): the verified operator ends another
   * identity's session. Body `{session_id, reason, status?}` (`reason` required,
   * ≤ 2,000; `status` abandoned (default) | completed | failed). 200
   * `{session_id, owner, status, ended_by, closed: true, event_ulid,
   * outstanding: {claimed_task_ids, open_delegation_ids, note}}`. It ends the
   * session row only: a running process, the owner's claims and delegations
   * are untouched, and the row stays ended (no revive on heartbeat); 403 for a
   * non-operator; 404 `no such session`; 409 `conflict` when already ended.
   * A hub without the route answers a bare 404, shown as "not available yet".
   */
  sessionOperatorEnd: "/api/action/session/operator-end",
  teamSnapshots: "/api/teams/snapshots",
  teamRestore: (snapshotId: string) => `/api/teams/snapshots/${encodeURIComponent(snapshotId)}/restore`,
} as const;

export class HubReadError extends Error {
  status: number;
  constructor(what: string, status: number, detail: string) {
    super(`${what} HTTP ${status}${detail ? `: ${detail.slice(0, 200)}` : ""}`);
    this.name = "HubReadError";
    this.status = status;
  }
}

async function getJson<T>(path: string, what: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new HubReadError(what, response.status, detail);
  }
  return (await response.json()) as T;
}

/** GET /api/sessions/{id}: one session row, including history older than the list page. */
export async function fetchSession(id: string, signal?: AbortSignal): Promise<SessionRow> {
  const row = await getJson<SessionRow>(`/api/sessions/${encodeURIComponent(id)}`, "session", signal);
  return { ...row, meta: row.meta && typeof row.meta === "object" ? row.meta : {} };
}

/**
 * Whether the signed-in caller is the verified operator. The hub reports it
 * as `can_grant_wake` on the sessions list (`_operator_recovery`); one row is
 * enough to read it.
 */
export async function fetchOperatorFlag(signal?: AbortSignal): Promise<boolean> {
  const body = await getJson<{ can_grant_wake?: unknown }>("/api/sessions?limit=1", "sessions", signal);
  return body.can_grant_wake === true;
}

export type SessionsListPayload = {
  sessions: SessionRow[];
  open: number;
  total: number;
  canGrantWake: boolean;
  /** The hub answered but could not read every source. */
  error?: string;
};

/** GET /api/sessions with the hub-side filters it supports (agent, open). */
export async function fetchSessionList(
  filter: { agent: string; status: string },
  signal?: AbortSignal,
): Promise<SessionsListPayload> {
  const qs = new URLSearchParams();
  if (filter.agent) qs.set("agent", filter.agent);
  if (filter.status === "open") qs.set("status", "open");
  qs.set("limit", "200");
  const body = await getJson<{
    sessions?: unknown;
    open?: unknown;
    total?: unknown;
    can_grant_wake?: unknown;
    error?: unknown;
  }>(`/api/sessions?${qs}`, "sessions", signal);
  const sessions = Array.isArray(body.sessions) ? (body.sessions as SessionRow[]) : [];
  return {
    sessions: sessions.map((s) => ({ ...s, meta: s.meta && typeof s.meta === "object" ? s.meta : {} })),
    open: typeof body.open === "number" ? body.open : 0,
    total: typeof body.total === "number" ? body.total : sessions.length,
    canGrantWake: body.can_grant_wake === true,
    ...(typeof body.error === "string" && body.error ? { error: body.error } : {}),
  };
}

export type TeamSnapshotRow = {
  snapshot_id: string;
  team: string | null;
  taken_at: string | null;
  by: string | null;
  seats: number;
};

export type SeatPlan = {
  label: string | null;
  node: string | null;
  backend: string | null;
  task_id: number | null;
  item_id: string | null;
  planned: string;
  outcome?: string;
  reason: string | null;
  /** The approval a seat's start waits on (outcome `awaiting_approval`). */
  awaiting_approval?: number | null;
  /** The device job a restored seat queued. */
  new_item_id?: string | null;
};

export async function fetchTeamSnapshots(signal?: AbortSignal): Promise<TeamSnapshotRow[]> {
  const body = await getJson<{ snapshots?: unknown }>("/api/teams/snapshots", "team snapshots", signal);
  return Array.isArray(body.snapshots) ? (body.snapshots as TeamSnapshotRow[]) : [];
}

export async function fetchTeamPreview(snapshotId: string, signal?: AbortSignal): Promise<SeatPlan[]> {
  const body = await getJson<{ seats?: unknown }>(
    `/api/teams/snapshots/${encodeURIComponent(snapshotId)}/preview`,
    "team preview",
    signal,
  );
  return Array.isArray(body.seats) ? (body.seats as SeatPlan[]) : [];
}

/** Results of the writes above that the area reads fields from. */
export type DispatchResult = {
  status?: string;
  task_id?: number;
  session_id?: string | number | null;
  run_id?: string | number | null;
  spawned?: boolean;
  reason?: string | null;
  error?: string | null;
};

export type DispatchStackResult = {
  next_task_id?: number | null;
  dispatch?: { launched?: boolean; reason?: string; error?: string; run_id?: string | number; pid?: number };
};

export type NodeStartResult = { item_id?: string; node?: string; status?: string };

/**
 * NEEDS-FROM-HUB (task #2501): `GET /api/launch-attempts/{key}`, the true
 * state of a launch attempt by the `Idempotency-Key` it was sent with.
 * Coded against the agreed shape so only the fixture changes when it lands.
 */
export type LaunchAttemptState = "unknown" | "in_progress" | "parked" | "unsettled" | "started" | "failed";

export type LaunchAttemptRead = {
  key: string;
  state: LaunchAttemptState;
  approval_id?: string | number | null;
  delegation_id?: string | number | null;
  task_id?: number | null;
  session_id?: string | null;
  run_id?: string | number | null;
  item_id?: string | null;
  reason?: string | null;
  /** Also reported by the #2501 branch (cc_launch.lookup): the launch kind and timestamps. */
  kind?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

const ATTEMPT_STATES: readonly LaunchAttemptState[] = ["unknown", "in_progress", "parked", "unsettled", "started", "failed"];

/**
 * Whether the hub recorded any step of the attempt (an approval, a
 * delegation, a task, a run, a device item). A `failed` attempt with one may
 * already have created work, and a repeat with the same key continues it.
 */
export function attemptHasEvidence(attempt: LaunchAttemptRead): boolean {
  return [attempt.approval_id, attempt.delegation_id, attempt.task_id, attempt.run_id, attempt.item_id, attempt.session_id].some((v) => v != null && v !== "");
}

function asAttempt(body: unknown): LaunchAttemptRead | null {
  if (!body || typeof body !== "object") return null;
  const b = body as Record<string, unknown>;
  return typeof b.key === "string" && ATTEMPT_STATES.includes(b.state as LaunchAttemptState) ? (b as LaunchAttemptRead) : null;
}

/**
 * `routeAbsent` when this hub has no such route (a 404 that is not an
 * attempt record): the caller must not conclude anything from it.
 */
export async function fetchLaunchAttempt(
  key: string,
  signal?: AbortSignal,
): Promise<{ routeAbsent: true } | { routeAbsent: false; attempt: LaunchAttemptRead }> {
  const response = await fetch(`/api/launch-attempts/${encodeURIComponent(key)}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  const text = await response.text().catch(() => "");
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  const attempt = asAttempt(body);
  if (response.ok && attempt) return { routeAbsent: false, attempt };
  if (response.status === 404) return attempt ? { routeAbsent: false, attempt } : { routeAbsent: true };
  throw new HubReadError("launch attempt", response.status, text);
}
