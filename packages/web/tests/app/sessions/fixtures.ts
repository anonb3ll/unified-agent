/**
 * Hub responses for the Sessions area, shaped like the Python handlers
 * (`registry._session_result`, `node_queue` item rows, `routing.route_inventory`).
 * Shared by the Vitest component tests and the Playwright specs.
 */
import type { SessionRow } from "../../../src/lib/sessions/types";

export const NOW = "2026-11-10T12:00:00Z";

export function sessionRow(overrides: Partial<SessionRow> = {}): SessionRow {
  return {
    id: "s-live",
    agent: "Desktop_Agent2",
    kind: "acp",
    status: "running",
    started_at: "2026-11-10T11:00:00Z",
    ended_at: null,
    last_heartbeat_at: "2026-11-10T11:58:00Z",
    duration_s: 3600,
    heartbeat_age_s: 120,
    task_id: 42,
    task_title: "Fix the login form",
    task_status: "doing",
    delegation_id: null,
    worktree_path: "/home/dev/.worktrees/task-42",
    branch: "task/42-login",
    external_session_id: "acp-1",
    transcript_path: null,
    host: "desktop",
    pid: 4242,
    exit_status: null,
    summary: null,
    model: "gpt-5.5",
    meta: { node: "desktop", item_id: "item-1", usage: { input_tokens: 1200, output_tokens: 300, cost_usd: 0.0421 }, pr_url: "https://github.com/acme/app/pull/7" },
    wake: null,
    ...overrides,
  };
}

/**
 * `GET /api/sessions/{id}` returns `registry.get_session()` as it is: no
 * `wake` (only the list handler adds it) and no `can_grant_wake`.
 */
export function sessionDetail(overrides: Partial<SessionRow> = {}): Omit<SessionRow, "wake"> {
  const { wake: _wake, ...row } = sessionRow(overrides);
  return row;
}

export const SESSIONS = [
  sessionRow({ id: "s-old", agent: "Desktop_Agent1", status: "completed", ended_at: "2026-11-09T10:00:00Z", last_heartbeat_at: "2026-11-09T10:00:00Z", started_at: "2026-11-09T09:00:00Z", task_id: 7, task_title: "Write docs", host: "laptop", meta: {} }),
  sessionRow(),
  sessionRow({ id: "s-wait", agent: "Desktop_Agent1", status: "waiting", last_heartbeat_at: "2026-11-10T11:30:00Z", task_id: null, task_title: null, host: "laptop", meta: {} }),
];

export function sessionsPayload(sessions: SessionRow[] = SESSIONS, canGrantWake = true) {
  return { sessions, open: sessions.filter((s) => ["starting", "running", "waiting"].includes(s.status)).length, total: sessions.length, can_grant_wake: canGrantWake };
}

export const AGENTS = {
  agents: [
    { label: "Desktop_Agent2", status: "online", open_sessions: 1, active_tasks: 1 },
    { label: "Desktop_Agent1", status: "idle", open_sessions: 1, active_tasks: 0 },
    { label: "Mac_Hermes", status: "online", open_sessions: 0, active_tasks: 0 },
    { label: "operator", status: "online", open_sessions: 0, active_tasks: 0 },
  ],
  counts: { online: 3, idle: 1 },
};

export function nodeItem(overrides: Record<string, unknown> = {}) {
  return {
    id: "item-1",
    node: "desktop",
    agent: "Desktop_Agent2",
    backend: "acp:codex",
    prompt_preview: "Fix the login form validation",
    cwd: "/home/dev/app",
    task_id: 42,
    status: "claimed",
    cancel_requested: false,
    created_by: "operator",
    created_at: "2026-11-10T11:00:00Z",
    evidence_ref: "evidence:item-1",
    restartable: false,
    resume: { available: false, mode: null, label: null, reason: "the session is still running" },
    ...overrides,
  };
}

export function nodesPayload(itemOverrides: Record<string, unknown> = {}) {
  return {
    nodes: [
      {
        node: "desktop",
        agent: "Desktop_Agent2",
        backends: ["acp:codex", "cli:claude"],
        harnesses: {},
        worktree_roots: ["/home/dev/app"],
        acceptance: { state: "accepted" },
        controls: {
          "acp:codex": {
            advertised: true,
            path: "acp",
            source: "node",
            source_version: "1",
            capabilities: {
              queue: { state: "supported", detail: "native queue" },
              steer: { state: "emulated", detail: "cancel and re-prompt" },
            },
          },
        },
        session_settings: {
          "acp:codex": {
            available_modes: [
              { id: "ask", name: "Ask", permission_mode: "supervised" },
              { id: "edit", name: "Edit", permission_mode: "auto-accept-edits" },
            ],
            available_models: [{ id: "gpt-5.5", name: "GPT 5.5" }],
            current_mode_id: "ask",
            current_model_id: null,
            permission_mode: "supervised",
            mode_path: null,
            model_path: null,
          },
        },
      },
    ],
    recent_items: [nodeItem(itemOverrides)],
    acceptance_required: true,
  };
}

let seq = 0;
export function acpEvent(acpKind: string, extra: Record<string, unknown> = {}, at = "2026-11-10T11:01:00Z") {
  seq += 1;
  return {
    seq,
    ulid: `01EVT${String(seq).padStart(6, "0")}`,
    channel: "agent.node",
    originator: "desktop",
    created_at: at,
    payload: { kind: "acp_event", item_id: "item-1", acp_kind: acpKind, turn_id: "t1", ...extra },
  };
}

export function transcriptEvents() {
  return [
    acpEvent("agent_message_chunk", { text: "Looking at the " }, "2026-11-10T11:01:00Z"),
    acpEvent("agent_message_chunk", { text: "login form now." }, "2026-11-10T11:01:01Z"),
    acpEvent("tool_call", { data: { title: "Read src/login.tsx", status: "completed" }, text: "" }, "2026-11-10T11:01:02Z"),
    {
      seq: 900,
      ulid: "01EVTFOLLOW",
      channel: "agent.node",
      originator: "operator",
      created_at: "2026-11-10T11:02:00Z",
      payload: { kind: "followup_queued", item_id: "item-1", mode: "queue", text: "Also add a test please" },
    },
    acpEvent("turn_checkpoint", { data: { files: [{ status: "M", path: "src/login.tsx" }], files_total: 1, diff: "+ok" } }, "2026-11-10T11:03:00Z"),
  ];
}

export function eventsPayload(events = transcriptEvents()) {
  return { events, seq_cursor: events.length ? Math.max(...events.map((e) => e.seq)) : 0 };
}

export const ROUTES = {
  routes: [
    {
      agent_label: "Desktop_Agent2",
      aliases: ["codex"],
      backend: "codex",
      binary: "codex",
      binary_available: true,
      workspace: { ok: true, reason: "bound", workspace: "/home/dev/agent", binding_source: "config" },
      profile: "developer",
      approval_boundary: "governed_dispatch",
      status: "ready",
      click_spawn: { enabled: true, mode: "native_cli", next_action: "dispatch through Dispatch Studio" },
      scheduler_spawn: { enabled: false, mode: "retired", next_action: "scheduler retired by #1435" },
    },
    {
      agent_label: "Desktop_Agent1",
      aliases: ["claude"],
      backend: "claude",
      binary: "claude",
      binary_available: false,
      workspace: { ok: false, reason: "no workspace bound for Desktop_Agent1", workspace: null, binding_source: null },
      profile: "steward",
      approval_boundary: "governed_dispatch",
      status: "forbidden",
      click_spawn: { enabled: true, mode: "native_cli", next_action: "dispatch through Dispatch Studio" },
      scheduler_spawn: { enabled: false, mode: "retired", next_action: "scheduler retired by #1435" },
    },
  ],
  scheduler: { enabled: false, real_spawn: false, status: "retired", mode: "retired" },
  model_defaults: { UA_MODEL_CODEX: "gpt-5.5", UA_MODEL_DEFAULT: null },
  model_options: ["gpt-5.5", "claude-opus-5-5"],
};

export const OVERVIEW = {
  agents: AGENTS,
  projects: [
    { project: "unified-agent", label: "Unified agent", checked_out: true, repo_path: "/home/dev/ua" },
    { project: "website", label: "Website", checked_out: false },
  ],
  queue_health: null,
  workflows: { workflows: [] },
  approvals: { pending: [], approved_unconsumed: [], expired_passes: [] },
  tasks: {
    active: [
      { id: 42, title: "Fix the login form", status: "todo", project: "unified-agent" },
      { id: 43, title: "Add a signup page", status: "todo", project: "unified-agent" },
    ],
  },
};

export const CREW = {
  session: {},
  live: true,
  conversation_id: "conv-1",
  messages: [{ message_id: "m1", from_agent: "Mac_Hermes", to_agent: "Desktop_Agent2", body: "Review the form copy too.", created_at: "2026-11-10T11:05:00Z" }],
  message_total: 1,
  interventions_path: null,
  worktree_interventions: [],
};
