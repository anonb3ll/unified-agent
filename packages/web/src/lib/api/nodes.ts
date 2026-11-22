/**
 * Nodes, node sessions, follow-ups, takeover and native sessions.
 * Re-exported from `hub.ts` (task #1575).
 */
import type { ActivityEvent } from "../sessions/types";

type HermesProfile = {
  kind: "hermes";
  name: string;
  agent: string | null;
  mapping: string;
  launchable: boolean;
  reason: string | null;
  last_item?: {
    item_id: string;
    status: string;
    profile_agent: string;
    session_id: string | null;
    cancel_requested: boolean;
    updated_at: string;
  } | null;
};

export type HarnessControl = {
  advertised: boolean;
  path: string;
  source: string;
  source_version: string | null;
  capabilities: Record<string, { state: string; detail: string }>;
};

type NegotiatedSessionSettings = {
  available_modes: { id: string; name: string; permission_mode: string | null }[];
  available_models: { id: string; name: string }[];
  current_mode_id: string | null;
  current_model_id: string | null;
  permission_mode: string | null;
  mode_control?: "negotiated" | "native_default_unclassified" | "unclassified";
  baseline_mode_id?: string | null;
  model_resolution?: "exact" | "catalog_alias" | "native_default" | "profile_default_unavailable" | null;
  mode_path: string | null;
  model_path: string | null;
  observed_at?: string | null;
};

/** One registered ua-node row from GET /api/nodes (task #1329). */
export type NodeRow = {
  node: string;
  agent?: string | null;
  device_node?: string | null;
  version?: string | null;
  registered_at?: string | null;
  last_heartbeat?: string | null;
  os?: string | null;
  backends: string[];
  harnesses: Record<string, unknown>;
  worktree_roots: string[];
  hermes_profiles?: HermesProfile[];
  controls?: Record<string, HarnessControl>;
  session_settings?: Record<string, NegotiatedSessionSettings>;
  /** Latest device acceptance run, or null when never run (task #2112). */
  acceptance?: NodeAcceptanceSummary | null;
};

/** Stored device acceptance summary for one node (task #2112). */
export type NodeAcceptanceSummary = {
  state: "pending" | "accepted" | "failed" | string;
  agent?: string | null;
  run_id?: string | null;
  reason?: string | null;
  checks?: Record<string, string | null>;
  code_sha?: string | null;
  started_at?: string | null;
  evaluated_at?: string | null;
  accepted_at?: string | null;
};

/** A retired ua-node row, left out of `nodes` (tasks #2181, #2208). */
export type RetiredNodeRow = {
  node: string;
  agent?: string | null;
  last_heartbeat?: string | null;
  retired_at?: string | null;
  retired_by?: string | null;
  retired_reason?: string | null;
};

/** Recent node_dispatch row for the Sessions launcher/transcript. */
export type NodeDispatchItem = {
  id: string;
  node: string;
  agent: string;
  backend: string;
  hermes_profile?: string | null;
  profile_agent?: string | null;
  /** Short preview only — full prompt stays on the hub for restart. */
  prompt_preview?: string;
  cwd: string;
  model?: string | null;
  timeout_s?: number | null;
  run_id?: string | null;
  task_id?: number | null;
  status: string;
  cancel_requested?: boolean;
  session_id?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  claimed_at?: string | null;
  evidence_ref: string;
  outcome_unknown?: boolean;
  restartable?: boolean;
  /** Harness session this item reopened (task #1741), if it was a resume. */
  resume_session_id?: string | null;
  /** Whether Resume is real for this item, and how (task #1741). */
  resume?: NodeResumeOption;
};

type NodeResumeMode = "acp_resume" | "acp_load" | "native_flag";

type NodeResumeOption = {
  available: boolean;
  mode: NodeResumeMode | null;
  label: string | null;
  /** Why Resume is not offered; Start again (a fresh session) may still be. */
  reason: string | null;
};

export type NodesPayload = {
  nodes: NodeRow[];
  recent_items: NodeDispatchItem[];
  /** Retired rows, for Unretire on the Devices pane (task #2208). */
  retired_nodes?: RetiredNodeRow[];
  acceptance_required?: boolean;
  error?: string;
};

export const NODES_QUERY_KEY = ["nodes"] as const;

export async function fetchNodes(signal?: AbortSignal): Promise<NodesPayload> {
  const response = await fetch("/api/nodes", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `nodes HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as NodesPayload;
  return {
    nodes: Array.isArray(payload.nodes) ? payload.nodes : [],
    recent_items: Array.isArray(payload.recent_items)
      ? payload.recent_items
      : [],
    retired_nodes: Array.isArray(payload.retired_nodes)
      ? payload.retired_nodes
      : [],
    acceptance_required: payload.acceptance_required === true,
    error: payload.error,
  };
}

/**
 * Load durable event_log rows (for historical node transcripts). `itemId`
 * narrows the read to one dispatch item's events (payload `item_id`, task
 * #1992); `afterSeq` pages by commit order, and `seqCursor` is the next page's
 * cursor.
 */
export async function fetchEvents(
  opts: {
    channels?: string[];
    limit?: number;
    since?: string;
    afterSeq?: number;
    itemId?: string;
  } = {},
  signal?: AbortSignal,
): Promise<{
  events: ActivityEvent[];
  error?: string;
  seqCursor?: number | null;
}> {
  const qs = new URLSearchParams();
  if (opts.channels?.length) qs.set("channels", opts.channels.join(","));
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.since) qs.set("since", opts.since);
  if (opts.afterSeq != null) qs.set("after_seq", String(opts.afterSeq));
  if (opts.itemId) qs.set("item_id", opts.itemId);
  const suffix = qs.toString() ? `?${qs}` : "";
  const response = await fetch(`/api/events${suffix}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `events HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as {
    events?: unknown[];
    error?: string;
    seq_cursor?: unknown;
  };
  const events: ActivityEvent[] = [];
  if (Array.isArray(payload.events)) {
    for (const row of payload.events) {
      if (!row || typeof row !== "object") continue;
      const rec = row as Record<string, unknown>;
      if (typeof rec.ulid !== "string" || typeof rec.channel !== "string")
        continue;
      events.push({
        seq: Number.isSafeInteger(rec.seq) ? (rec.seq as number) : null,
        ulid: rec.ulid,
        channel: rec.channel,
        payload: rec.payload ?? null,
        originator: typeof rec.originator === "string" ? rec.originator : null,
        created_at: typeof rec.created_at === "string" ? rec.created_at : null,
        episode_id: typeof rec.episode_id === "string" ? rec.episode_id : null,
        causal_parent_id:
          typeof rec.causal_parent_id === "string"
            ? rec.causal_parent_id
            : null,
      });
    }
  }
  return {
    events,
    error: payload.error,
    seqCursor:
      typeof payload.seq_cursor === "number" ? payload.seq_cursor : null,
  };
}

/** One native Claude Code / Codex session a node discovered (metadata only). */
export type NativeSession = {
  node: string;
  harness: "claude" | "codex";
  external_session_id: string;
  cwd: string;
  last_activity: string | null;
  title: string | null;
  backends: string[];
  /** Why an advertised ACP backend is not offered (no resume/load recorded yet). */
  backend_note?: string | null;
  resume_ready: boolean;
  adopted: { session_id: string; agent: string; status: string } | null;
  adoptable: boolean;
  reason: string | null;
};

export const NATIVE_SESSIONS_QUERY_KEY = ["native-sessions"] as const;

export async function fetchNativeSessions(
  signal?: AbortSignal,
): Promise<{ sessions: NativeSession[]; error?: string }> {
  const response = await fetch("/api/nodes/native-sessions", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `native sessions HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as { sessions?: NativeSession[]; error?: string };
  if (payload.error) throw new Error(payload.error);
  return {
    sessions: Array.isArray(payload.sessions) ? payload.sessions : [],
    error: payload.error,
  };
}
