/**
 * Sessions + live activity pane types.
 *
 * Mirrors:
 *  - GET /api/sessions  → webui/api.sessions_payload / registry.list_sessions
 *  - GET /api/agents    → webui/api.agents_payload / registry.list_agents
 *  - GET /api/events/stream "message" frames → events.query_events rows
 */

/** One row from registry.list_sessions (joined to its task title/status). */
export type SessionRow = {
  id: string;
  agent: string;
  kind: string;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  last_heartbeat_at: string | null;
  duration_s: number | null;
  heartbeat_age_s: number | null;
  task_id: number | null;
  task_title: string | null;
  task_status: string | null;
  delegation_id: number | null;
  worktree_path: string | null;
  branch: string | null;
  external_session_id: string | null;
  transcript_path: string | null;
  host: string | null;
  pid: number | null;
  exit_status: string | null;
  summary: string | null;
  model: string | null;
  meta: Record<string, unknown>;
  /** Idle-wake state (task #2050): the session's request and the operator's grant. */
  wake?: { requested: boolean; granted: boolean } | null;
  platform?: { kind: string; id: string; uri: string } | null;
  links?: {
    agent?: { kind: string; id: string; uri: string } | null;
    task?: { kind: string; id: string; uri: string } | null;
    delegation?: { kind: string; id: string; uri: string } | null;
  };
};

export type SessionsPayload = {
  sessions: SessionRow[];
  open: number;
  total: number;
  /** True only for the verified human operator, the one caller the grant route admits. */
  can_grant_wake?: boolean;
  error?: string;
};

export type AgentPersona = {
  display_name: string;
  avatar: string;
  color: string;
  colour?: string;
  bio?: string;
  model_routing_preference?: string | null;
};

/** One row from registry.list_agents, enriched by agents_payload. */
export type AgentRow = {
  label: string;
  kind?: string | null;
  host?: string | null;
  known?: boolean;
  first_seen?: string | null;
  last_seen?: string | null;
  last_seen_via?: string | null;
  pid?: number | null;
  version?: string | null;
  status: "online" | "idle" | "stale" | "offline" | "unknown" | string;
  meta?: Record<string, unknown>;
  open_sessions: number;
  active_tasks: number;
  is_token_user?: boolean;
  is_delegate?: boolean;
  display_name?: string | null;
  avatar?: string | null;
  color?: string | null;
  colour?: string | null;
  bio?: string | null;
  model_routing_preference?: string | null;
  persona?: AgentPersona;
};

export type AgentsPayload = {
  agents: AgentRow[];
  counts: Record<string, number>;
  error?: string;
};

export type DispatchRouteRow = {
  agent_label: string;
  aliases: string[];
  backend: string;
  binary: string;
  binary_available: boolean;
  workspace: {
    ok: boolean;
    reason: string;
    workspace: string | null;
    binding_source: string | null;
  };
  profile: string;
  approval_boundary: string;
  status: "ready" | "forbidden" | "unavailable" | string;
  click_spawn: { enabled: boolean; mode: string; next_action: string };
  scheduler_spawn: { enabled: boolean; mode: string; next_action: string };
};

export type DispatchRoutesPayload = {
  routes: DispatchRouteRow[];
  scheduler: { enabled: boolean; real_spawn: boolean; status?: string; mode?: string };
  model_defaults?: Record<string, string | null>;
  model_options?: string[];
};

/** A durable event_log row, delivered verbatim over the SSE "message" channel. */
export type ActivityEvent = {
  /** Commit order (event_log ROWID) when the source carried it. */
  seq?: number | null;
  ulid: string;
  channel: string;
  payload: unknown;
  originator: string | null;
  created_at: string | null;
  episode_id?: string | null;
  causal_parent_id?: string | null;
};

/** Query filter for GET /api/sessions (part of the query key). */
export type SessionsFilter = {
  agent?: string | null;
  /** "open" narrows to live sessions; anything else = all recent. */
  status?: "open" | "all";
};

const OPEN_STATUSES = new Set(["starting", "running", "waiting"]);

export function isOpenSession(status: string | null | undefined): boolean {
  return status != null && OPEN_STATUSES.has(status);
}

/** Format a duration in seconds — null/negative → em dash, never a fake zero. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d`;
}

/** Relative age in seconds → compact "12s" / "4m" / "3h" / "2d". */
export function formatAge(ageS: number | null | undefined): string {
  if (ageS == null || !Number.isFinite(ageS) || ageS < 0) return "—";
  const s = Math.floor(ageS);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

export type BadgeTone = "active" | "done" | "warn" | "idle";

/** Map a session/agent status onto a visual badge tone. */
export function statusTone(status: string | null | undefined): BadgeTone {
  const value = (status ?? "").toLowerCase();
  if (isOpenSession(value) || value === "online") return "active";
  if (value === "done" || value === "complete" || value === "completed") return "done";
  if (
    value === "error" ||
    value === "failed" ||
    value === "abandoned" ||
    value === "timeout" ||
    value === "offline" ||
    value === "stale"
  ) {
    return "warn";
  }
  return "idle";
}

/** Short channel label: drop a leading "agent." namespace, cap length. */
export function channelShortLabel(channel: string | null | undefined): string {
  if (!channel) return "?";
  const trimmed = channel.startsWith("agent.") ? channel.slice("agent.".length) : channel;
  return trimmed.length > 32 ? `${trimmed.slice(0, 31)}…` : trimmed;
}

/** One-line, human-facing summary of an event payload. */
export function summarizePayload(payload: unknown): string {
  if (payload == null) return "";
  if (typeof payload === "string") return payload;
  if (typeof payload !== "object") return String(payload);
  const record = payload as Record<string, unknown>;
  for (const key of ["summary", "message", "title", "detail", "path", "status", "raw"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  try {
    return JSON.stringify(payload);
  } catch {
    return "[unserializable payload]";
  }
}

export function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** Parse one SSE "message" frame into an ActivityEvent, or null when unusable. */
export function parseActivityEvent(data: string | undefined | null): ActivityEvent | null {
  if (!data) return null;
  try {
    const parsed = JSON.parse(data) as Partial<ActivityEvent>;
    if (!parsed || typeof parsed !== "object") return null;
    if (typeof parsed.ulid !== "string" || typeof parsed.channel !== "string") return null;
    return {
      seq: Number.isSafeInteger(parsed.seq) ? parsed.seq : null,
      ulid: parsed.ulid,
      channel: parsed.channel,
      payload: parsed.payload ?? null,
      originator: typeof parsed.originator === "string" ? parsed.originator : null,
      created_at: typeof parsed.created_at === "string" ? parsed.created_at : null,
      episode_id: typeof parsed.episode_id === "string" ? parsed.episode_id : null,
      causal_parent_id:
        typeof parsed.causal_parent_id === "string" ? parsed.causal_parent_id : null,
    };
  } catch {
    return null;
  }
}

/** Newest-first ring buffer helper: prepend, drop overflow from the tail. */
export function pushBounded<T>(buffer: T[], item: T, max: number): T[] {
  const next = [item, ...buffer];
  return next.length > max ? next.slice(0, max) : next;
}

export const ACTIVITY_BUFFER_MAX = 200;
