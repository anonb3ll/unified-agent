/**
 * Realistic hub answers for the Home and Inbox areas. Shapes follow the hub
 * handlers (webui/routes.py, webui/api.py, needs_operator.py,
 * self_organization.py, runs_projection.py).
 * Shared by the Vitest component tests and the Playwright specs.
 */
const HOUR = 60 * 60 * 1000;
export const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString();

export const APPROVAL_ID = 12;

export const pendingApprovalRow = {
  id: APPROVAL_ID,
  tool: "task_delete",
  requested_by: "Desktop_Agent2",
  status: "pending",
  created_at: iso(HOUR),
  links: { task: { kind: "task", id: "42", uri: "ua://task/42" } },
};

export const approvalsPayload = {
  pending: [pendingApprovalRow],
  approved_unconsumed: [{ id: 9, tool: "vault_write", requested_by: "Desktop_Agent5", status: "approved", created_at: iso(3 * HOUR) }],
  expired_passes: [],
};

export function approvalDetail(status = "pending", overrides: Record<string, unknown> = {}) {
  return {
    id: APPROVAL_ID,
    tool: "task_delete",
    status,
    requested_by: "Desktop_Agent2",
    device: "desktop",
    device_verified: true,
    created_at: iso(HOUR),
    updated_at: iso(HOUR),
    gate: { kind: "global_allowlist", label: "This tool is on the hub's approval list" },
    payload: { task_id: 42, reason: "duplicate of task 41" },
    links: { task: { kind: "task", id: "42", uri: "ua://task/42" } },
    ...(status === "pending" ? {} : { resolved_by: "operator", resolved_at: iso(1000) }),
    ...overrides,
  };
}

export const needsRows = [
  { kind: "approval", subject: String(APPROVAL_ID), title: "task_delete is waiting for approval", since: iso(HOUR), act: { pane: "approvals", params: { approval: String(APPROVAL_ID) } } },
  { kind: "operator_reply", subject: "msg-1", title: "Desktop_Agent2 is waiting for your reply (query)", since: iso(2 * HOUR), act: { pane: "messages", params: { conversation: "conv-1" } } },
  { kind: "red_tier_hold", subject: "77", title: "Rotate hub tokens", since: iso(5 * HOUR), act: { pane: "tasks", params: { task: "77" } } },
  { kind: "operator_task", subject: "78", title: "Renew the domain", since: iso(26 * HOUR), act: { pane: "tasks", params: { task: "78" } } },
  { kind: "wake_grant", subject: "sess-1", title: "Desktop_Agent5 session asks to be woken", since: iso(HOUR / 2), act: { pane: "sessions", params: { session: "sess-1" } } },
];

export function attentionPayload(options: { rows?: unknown[]; items?: unknown[]; informational?: unknown[]; needsErrors?: string[]; errors?: string[] } = {}) {
  const rows = options.rows ?? needsRows;
  const counts: Record<string, number> = { approval: 0, operator_task: 0, red_tier_hold: 0, held_pr: 0, operator_reply: 0, wake_grant: 0 };
  for (const r of rows as { kind: string }[]) counts[r.kind] = (counts[r.kind] ?? 0) + 1;
  const items = options.items ?? [
    { kind: "stale_claim", severity: "medium", source: { task_id: 55, agent_label: "Desktop_Agent9" }, source_subject: "task 55", title: "Claim has not moved for 3 hours", reason: "No heartbeat since the claim", age_s: 3 * 3600 },
  ];
  return {
    items,
    informational: options.informational ?? [],
    counts: {},
    total: items.length,
    errors: options.errors ?? [],
    needs_operator: { rows, counts, total: rows.length, errors: options.needsErrors ?? [] },
  };
}

/** The task row `POST /api/action/task` answers with (`task_update`'s return, abridged to the fields the UI reads). */
export function taskRow(id: number, title: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    title,
    notes: "",
    status: "open",
    priority: "med",
    parent_id: null,
    project: "unified-agent",
    lane: null,
    claimed_by: null,
    created_at: iso(30 * HOUR),
    updated_at: iso(1000),
    completed_at: null,
    archived_at: null,
    evidence_ref: null,
    worktree: null,
    ...extra,
  };
}

export const memoryProposals = {
  proposals: [
    {
      id: "prop-1",
      path: "00 - Inbox/learning-2026-11-09.md",
      kind: "session_learning",
      title: "Pin the Playwright port per worktree",
      content: "Two previews on 4183 collide. Use a per-worktree port.",
      source_date: "2026-11-09",
      ingest_status: "pending",
      filing_preview: { task_title: "Pin the Playwright port per worktree", task_notes: "From session learning 2026-11-09." },
    },
  ],
};

export const orgProposal = {
  id: "orgprop-1",
  title: "Add a review team",
  proposal_type: "team",
  author: "Desktop_Agent5",
  scope: "org",
  rationale: "Reviews queue up on one agent.",
  proposed_changes: { team: "review", members: ["Desktop_Agent1", "Desktop_Agent2"] },
  status: "reviewed",
  created_at: iso(10 * HOUR),
  reviewed_by: "operator",
  reviewed_at: iso(2 * HOUR),
};

export const threadPayload = {
  conversation_id: "conv-1",
  participants: ["Desktop_Agent2", "operator"],
  messages: [
    {
      seq: 5,
      message_id: "msg-1",
      act: "query",
      conversation_id: "conv-1",
      reply_to: null,
      from_agent: "Desktop_Agent2",
      to_agent: "operator",
      from_session: "sess-codex",
      to_session: null,
      body: "Should I ship <b>now</b>?",
      created_at: iso(2 * HOUR),
      expects_reply: true,
      read_at: null,
      acked_at: null,
      answered: false,
      answered_by: null,
      status: "delivered",
    },
  ],
  total: 1,
  next_seq: 6,
  oldest_seq: 5,
  has_older: false,
};

export const conversationsPayload = {
  viewer: "operator",
  scope: "all",
  filter: "all",
  participant: null,
  conversations: [
    {
      conversation_id: "conv-1",
      participants: ["Desktop_Agent2", "operator"],
      sessions: ["sess-codex"],
      total: 1,
      unread: 1,
      unread_mine: 1,
      waiting: 1,
      last_seq: 5,
      last: { message_id: "msg-1", seq: 5, act: "query", from_agent: "Desktop_Agent2", to_agent: "operator", from_session: "sess-codex", to_session: null, expects_reply: true, created_at: iso(2 * HOUR), preview: "Should I ship now?" },
    },
  ],
  truncated: false,
  next_before_seq: null,
  max_body_bytes: 4096,
};

export const agentsPayload = {
  agents: [
    { label: "Desktop_Agent2", status: "online", open_sessions: 1, active_tasks: 1, host: "desktop", last_seen: iso(60_000) },
    { label: "Desktop_Agent5", status: "idle", open_sessions: 1, active_tasks: 0, host: "desktop", last_seen: iso(10 * 60_000) },
    { label: "Mac_Claude", status: "offline", open_sessions: 0, active_tasks: 0, host: "mac", last_seen: iso(30 * HOUR) },
  ],
  counts: { online: 1, idle: 1, offline: 1 },
};

function session(id: string, agent: string, extra: Record<string, unknown>) {
  return {
    id,
    agent,
    kind: "claude-code",
    status: "running",
    started_at: iso(2 * HOUR),
    ended_at: null,
    last_heartbeat_at: iso(30_000),
    duration_s: null,
    heartbeat_age_s: 30,
    task_id: null,
    task_title: null,
    task_status: null,
    delegation_id: null,
    worktree_path: null,
    branch: null,
    external_session_id: null,
    transcript_path: null,
    host: "desktop",
    pid: null,
    exit_status: null,
    summary: null,
    model: null,
    meta: {},
    ...extra,
  };
}

export const openSessions = {
  sessions: [
    session("sess-codex", "Desktop_Agent2", { task_id: 42, task_title: "Ship the inbox" }),
    session("sess-1", "Desktop_Agent5", { task_id: 90, task_title: "Beta research" }),
  ],
  open: 2,
  total: 2,
  can_grant_wake: true,
};

export const allSessions = {
  sessions: [
    ...openSessions.sessions,
    session("sess-old", "Mac_Claude", { status: "ended", ended_at: iso(3 * HOUR), exit_status: "ok", task_id: 41, task_title: "Fix the flaky test" }),
  ],
  open: 2,
  total: 3,
  can_grant_wake: true,
};

export const runsPayload = {
  runs: [
    { run_id: "run-1", kind: "dispatch", task_id: 43, agent: "Mac_Claude", status: "running", started_at: iso(20 * 60_000), ended_at: null, abandoned: false },
    { run_id: "run-2", kind: "dispatch", task_id: 44, agent: "Mac_Claude", status: "succeeded", started_at: iso(5 * HOUR), ended_at: iso(4 * HOUR), abandoned: false },
  ],
  count: 2,
  truncated: false,
};

function qTask(id: number, title: string, status: string, project: string) {
  return { id, title, status, project, claimed_by: null, priority: "med" };
}

export const queueHealth = {
  desktop_open: [qTask(77, "Rotate hub tokens", "open", "unified-agent"), qTask(78, "Renew the domain", "open", "beta")],
  dispatch_ready: [],
  interactive_only: [],
  operator_only: [],
  doing: [qTask(42, "Ship the inbox", "doing", "unified-agent"), qTask(90, "Beta research", "doing", "beta"), qTask(43, "Index the vault", "doing", "unified-agent")],
  waiting_review: [qTask(46, "Tidy the docs", "waiting_review", "beta")],
  stale_claims: [qTask(55, "Stale thing", "doing", "unified-agent")],
  laptop_lane: [],
  blocked_archived: [],
  inconsistent: [],
  other: [],
  counts: {
    desktop_open: 2,
    dispatch_ready: 0,
    interactive_only: 0,
    operator_only: 0,
    doing: 3,
    waiting_review: 1,
    stale_claims: 1,
    laptop_lane: 0,
    blocked_archived: 0,
    inconsistent: 0,
    other: 0,
    total_active: 7,
  },
};

export const digestPayload = {
  status: "action_needed",
  changed_today: {
    tasks_completed: [{ id: 41, title: "Fix the flaky test", completed_at: iso(HOUR), claimed_by: "Mac_Claude", project: "unified-agent" }],
    total_completed: 1,
  },
  errors: [],
};

export const projectsPayload = {
  projects: [
    { project: "unified-agent", label: "Unified agent" },
    { project: "beta", label: "Beta" },
  ],
};

export const memoryProbe = { total: 3, items: [{ key: "a" }], results: [] };

export type Reply = { status?: number; body: unknown };
export type Responder = Reply | ((url: URL) => Reply);
export type HubTable = Record<string, Responder>;

/** Every Home and Inbox read, answered with realistic data. Keys are "METHOD /path" (query ignored, see responders). */
export function fullHub(): HubTable {
  return {
    "GET /api/attention": { body: attentionPayload() },
    "GET /api/approvals": { body: approvalsPayload },
    [`GET /api/approvals/${APPROVAL_ID}`]: { body: approvalDetail() },
    "GET /api/proposals": { body: memoryProposals },
    "GET /api/org-proposals": (url) => ({
      body: { proposals: url.searchParams.get("status") === "reviewed" ? [orgProposal] : [], count: 1, self_organization_enabled: true },
    }),
    "GET /api/org-proposals/orgprop-1": { body: { proposal: orgProposal } },
    "GET /api/messages/conversations": { body: conversationsPayload },
    "GET /api/messages/conversations/conv-1": { body: threadPayload },
    "GET /api/messages/conversations/conv-1/handoffs": { body: { handoffs: [] } },
    "GET /api/messages/sessions": { body: { sessions: [] } },
    "GET /api/sessions": (url) => ({ body: url.searchParams.get("status") === "open" ? openSessions : allSessions }),
    "GET /api/sessions/sess-1": { body: openSessions.sessions[1] },
    "GET /api/agents": { body: agentsPayload },
    "GET /api/runs": { body: runsPayload },
    "GET /api/queue-health": { body: queueHealth },
    "GET /api/digest": { body: digestPayload },
    "GET /api/projects": { body: projectsPayload },
    "GET /api/memory": { body: memoryProbe },
  };
}

/** An organization with nothing in it yet: empty reads everywhere. */
export function emptyHub(): HubTable {
  const emptyQueue = {
    ...queueHealth,
    desktop_open: [],
    doing: [],
    waiting_review: [],
    stale_claims: [],
    counts: { ...queueHealth.counts, desktop_open: 0, doing: 0, waiting_review: 0, stale_claims: 0, total_active: 0 },
  };
  return {
    "GET /api/attention": { body: attentionPayload({ rows: [], items: [] }) },
    "GET /api/approvals": { body: { pending: [], approved_unconsumed: [], expired_passes: [] } },
    "GET /api/proposals": { body: { proposals: [] } },
    "GET /api/org-proposals": { body: { proposals: [], count: 0, self_organization_enabled: true } },
    "GET /api/messages/conversations": { body: { ...conversationsPayload, conversations: [] } },
    "GET /api/messages/sessions": { body: { sessions: [] } },
    "GET /api/agents": { body: { agents: [], counts: {} } },
    "GET /api/runs": { body: { runs: [], count: 0, truncated: false } },
    "GET /api/queue-health": { body: emptyQueue },
    "GET /api/digest": { body: { status: "ok", changed_today: { tasks_completed: [], total_completed: 0 }, errors: [] } },
    "GET /api/projects": { body: { projects: [] } },
    "GET /api/memory": { body: { total: 0, items: [], results: [] } },
    "GET /api/sessions": { body: { sessions: [], open: 0, total: 0 } },
  };
}

/** Every Home and Inbox read failing with a 503. */
export function failingHub(): HubTable {
  const fail: Reply = { status: 503, body: { error: "upstream timeout after 30s (sqlite busy)" } };
  return Object.fromEntries(Object.keys(fullHub()).map((key) => [key, fail]));
}

export function resolve(responder: Responder, url: URL): Reply {
  return typeof responder === "function" ? responder(url) : responder;
}
