/**
 * Realistic hub payloads for the Work area, shared by the Vitest component
 * tests and the Playwright e2e and a11y specs.
 */
// api.board_payload cards; `domain` is on every task card from #2480 (origin/task/2480-cc2-hub-operations).
export const BOARD = {
  as_of: "2026-11-10T12:00:00Z",
  columns: [
    {
      status: "open",
      label: "Backlog",
      cards: [
        { kind: "task", id: 101, domain: "professional", alerts: ["aging_open"], title: "Write the release notes", status: "open", priority: "med", claimed_by: null, project: "unified-agent", lane: "default", parent_id: null, dep_count: 1, created_at: "2026-11-01T10:00:00+00:00", updated_at: "2026-11-09T08:00:00+00:00", subtasks: [] },
        { kind: "task", id: 104, domain: "hobby", title: "Add the export button", status: "open", priority: "low", claimed_by: null, project: "web", lane: "laptop", parent_id: 102, dep_count: 0, subtasks: [] },
        // api._board_delegation_card
        {
          kind: "delegation", id: "delegation:9", delegation_id: 9, title: "Review the board tests", status: "open", priority: "med",
          claimed_by: "Desktop_Agent2", from_agent: "Desktop_Agent1", to_agent: "Desktop_Agent2", task_id: 105, lane: "default", project: "unified-agent",
          parent_id: null, age_s: 7200, worktree_path: null, branch: null, dep_count: 0, blocked_reason: null,
          deadline: "2026-11-10T08:00:00+00:00", deadline_remaining_s: -3600, alerts: ["overdue"], subtasks: [], swimlane: "Desktop_Agent2",
        },
      ],
    },
    {
      status: "doing",
      label: "Doing",
      cards: [
        {
          kind: "task",
          id: 102,
          domain: "professional",
          alerts: ["aging_doing", "lock_expired"],
          title: "Ship the Work area",
          status: "doing",
          priority: "high",
          claimed_by: "Desktop_Agent1",
          project: "web",
          lane: "default",
          parent_id: null,
          dep_count: 0,
          subtasks: [{ kind: "task", id: 105, domain: "professional", title: "Write the board tests", status: "doing", priority: "med", claimed_by: "Desktop_Agent1", project: "web", lane: "default", parent_id: 102, subtasks: [] }],
        },
      ],
    },
    { status: "waiting_review", label: "Review", cards: [{ kind: "task", id: 103, domain: null, title: "Review the hub routes", status: "waiting_review", priority: "med", claimed_by: "Desktop_Agent2", project: "unified-agent", lane: "default", parent_id: null, subtasks: [] }] },
    { status: "blocked", label: "Blocked", cards: [{ kind: "task", id: 106, domain: "personal", title: "Wait for the design sign-off", status: "blocked", priority: "med", claimed_by: null, project: "web", lane: "default", parent_id: null, blocked_reason: "needs design input", subtasks: [] }] },
    { status: "done", label: "Done", cards: [{ kind: "task", id: 100, domain: "professional", title: "Draft the spec", status: "done", priority: "med", claimed_by: "operator", project: "unified-agent", lane: "default", parent_id: null, subtasks: [] }] },
    { status: "cancelled", label: "Cancelled", cards: [] },
  ],
  // api.board_payload wip: per queue (lane) and per agent, from config.lane_wip_limits / agent_wip_max.
  wip: {
    lanes: { default: { count: 3, cap: 2, mode: "soft", over: true }, laptop: { count: 0, cap: 1, mode: "hard", over: false } },
    agents: { Desktop_Agent1: { count: 2, cap: 3, over: false }, Desktop_Agent2: { count: 1, cap: 3, over: false } },
    agent_cap: 3,
  },
};

export const EMPTY_BOARD = { as_of: "2026-11-10T12:00:00Z", columns: BOARD.columns.map((c) => ({ ...c, cards: [] })) };

export const TASK_102 = {
  uri: "unified-agent://tasks/102",
  task: {
    id: 102,
    title: "Ship the Work area",
    status: "doing",
    priority: "high",
    notes: "Build **every** view.\n\n- list\n- board",
    parent_id: null,
    project: "web",
    domain: "professional",
    lane: "default",
    due_at: "2026-11-20T23:59:59Z",
    evidence_ref: "https://github.com/example/repo/pull/7",
    claimed_by: "Desktop_Agent1",
    claimed_at: "2026-11-10T10:00:00Z",
    current_run_id: "run-1",
    created_at: "2026-11-09T10:00:00Z",
    updated_at: "2026-11-10T11:00:00Z",
    completed_at: null,
    archived_at: null,
    untrusted: 0,
  },
};

export const TASK_101 = {
  uri: "unified-agent://tasks/101",
  task: { ...TASK_102.task, id: 101, title: "Write the release notes", status: "open", priority: "med", claimed_by: null, claimed_at: null, current_run_id: null, notes: "", evidence_ref: null, due_at: null },
};

// webui/api.py task_graph_payload (on origin/task/2480-cc2-hub-operations): task, plan and
// agent nodes with the audit chain; claim, subtask, depends_on (+dependency_id), delegates
// and authored links.
const audit = (agent: string | null, evidence: string | null) => ({
  human: "Operator", device: "desktop", agent: agent ?? "unattributed", claimed_by: agent, approval_status: "none",
  evidence_ref: evidence, has_evidence: Boolean(evidence), tamper_evident: false, unattributed: !agent,
  chain: `Operator → desktop → ${agent ?? "unattributed"} → task → ${evidence ? "evidence" : "unverified"}`,
});
const overviewTask = (id: number, title: string, status: string, owner: string | null, evidence: string | null = null) => ({
  id: `task:${id}`, kind: "task", task_id: id, label: `#${id} ${title}`, status, priority: "med", lane: "default", project: "web",
  claimed_by: owner, device: "desktop", agent: owner, approval_status: "none", evidence_ref: evidence, tamper_evident: false,
  unattributed: !owner, audit: audit(owner, evidence), parent_id: null,
});
export const TASK_GRAPH_OVERVIEW = {
  task_graph: {
    nodes: [
      overviewTask(101, "Write the release notes", "open", null),
      overviewTask(102, "Ship the Work area", "doing", "Desktop_Agent1", "https://github.com/example/repo/pull/7"),
      overviewTask(104, "Add the export button", "open", null),
      {
        id: "plan:3", kind: "plan", plan_id: "3", label: "Roll out Control Center v2", status: "active", needs_human: false, claimed_by: "operator",
        device: "hub", agent: "operator", approval_status: "none", evidence_ref: null, tamper_evident: false, unattributed: false, audit: audit("operator", null),
      },
      { id: "agent:Desktop_Agent1", kind: "agent", label: "Desktop_Agent1", claims: 1, delegations: 1 },
      { id: "agent:Desktop_Agent2", kind: "agent", label: "Desktop_Agent2", claims: 0, delegations: 1 },
      { id: "agent:operator", kind: "agent", label: "operator", claims: 0, delegations: 0 },
    ],
    links: [
      { source: "agent:Desktop_Agent1", target: "task:102", kind: "claim" },
      { source: "task:102", target: "task:104", kind: "subtask" },
      // depends_on runs prerequisite → dependent: 101 depends on 102, so 102 → 101.
      { source: "task:102", target: "task:101", kind: "depends_on", dependency_id: 55 },
      { source: "agent:Desktop_Agent1", target: "agent:Desktop_Agent2", kind: "delegates", status: "pending", delegation_id: 9 },
      { source: "agent:operator", target: "plan:3", kind: "authored" },
    ],
    counts: { tasks: 3, plans: 1, agents: 3, dependencies: 1, delegations: 1, links: 5 },
  },
};

export const RUNS = { runs: [{ run_id: "run-1", kind: "dispatch", task_id: 102, agent: "Desktop_Agent1", status: "open", started_at: "2026-11-10T10:00:00Z" }] };

export const EVIDENCE = {
  generated_at: "2026-11-10T12:00:00Z",
  total_records: 1,
  records: [
    {
      task_id: 102,
      project: "web",
      human: "operator",
      device: "desktop",
      agent: "Desktop_Agent1",
      action: { type: "task", title: "Ship the Work area", status: "doing", priority: "high", approvals: [] },
      proof: { evidence_ref: null, pr_url: "https://github.com/example/repo/pull/7", commit_sha: "abcdef1234567890", merge_verified: false, created_at: null, updated_at: null, completed_at: null, event_hashes: [], events_count: 2, tamper_evident: true },
    },
  ],
};

export const COMMENTS = { revision: 1, comments: [{ id: 1, author: "Desktop_Agent2", body: "Looks good so far.", parent_comment_id: null }] };

// goals.goal_tree / public_goal (src/unified_agent/goals.py): the goals row plus children.
// Status is open | achieved | abandoned; the author column is created_by.
const goalRow = (id: number, title: string, extra: Record<string, unknown> = {}) => ({
  id,
  title,
  statement: null as string | null,
  status: "open",
  project: "web",
  parent_goal_id: null as number | null,
  created_by: "operator",
  created_at: "2026-11-09T10:00:00+00:00",
  updated_at: "2026-11-10T09:00:00+00:00",
  // storage._add_revision_counters (#2480): every goal update advances it.
  revision: 4,
  org_id: null,
  ...extra,
});

export const GOALS = {
  tree: [
    {
      ...goalRow(7, "A usable Control Center", { statement: "One person can run the fleet from the browser." }),
      children: [{ ...goalRow(8, "Work area complete", { parent_goal_id: 7 }), children: [] }],
    },
  ],
  edge: "goal_ancestry",
  meaning: "child goal serves parent goal",
};

const { children: _goal7Children, ...GOAL_7_ROW } = GOALS.tree[0];
void _goal7Children;
// goals.get_goal: {goal, ancestry, edge, meaning}.
export const GOAL_7 = { goal: GOAL_7_ROW, ancestry: [GOAL_7_ROW], edge: "goal_ancestry", meaning: "child goal serves parent goal" };

// work_graph._goal_node / _task_node (src/unified_agent/work_graph.py).
const goalNode = (id: number, label: string, parent: number | null = null) => ({
  id: `goal:${id}`, kind: "goal", goal_id: id, label, status: "open", project: "web", parent_goal_id: parent, updated_at: "2026-11-10T09:00:00+00:00",
});
const taskNode = (id: number, label: string, status: string) => ({
  id: `task:${id}`, kind: "task", task_id: id, label, status, priority: "med", parent_id: null, claimed_by: null, lock_expires_at: null,
  lane: "default", project: "web", updated_at: "2026-11-10T09:00:00+00:00", evidence_ref: null, stub: false,
});

/** `GET /api/work-graph` with no goal_id: a page of root goals only (work_graph._root_page). */
export const WORK_GRAPH_ROOT = {
  nodes: [goalNode(7, "A usable Control Center")],
  edges: [],
  scope: { root_goals: true },
  cursor: null,
  next_cursor: null,
  as_of: "2026-11-10T12:00:00+00:00",
  revision: "r0",
  unknowns: [],
};

/** `GET /api/work-graph?goal_id=7`: the goal's subtree (work_graph._goal_subgraph). */
export const WORK_GRAPH_7 = {
  nodes: [
    goalNode(7, "A usable Control Center"),
    // _goal_subgraph adds `ready` to each task (work_graph._ready_info).
    { ...taskNode(102, "Ship the Work area", "doing"), ready: { state: "blocked", reason: "unmet dependency", unmet_dependency_ids: [100] } },
    { ...taskNode(100, "Draft the spec", "done"), ready: { state: "terminal", reason: "task is done", unmet_dependency_ids: [] } },
    { id: "plan:3", kind: "plan", plan_id: "3", label: "Roll out Control Center v2", status: "active", escalated: false, updated_at: "2026-11-10T10:00:00+00:00" },
    {
      id: "session:s-1", kind: "session", session_id: "s-1", drill_uri: "/api/platform/objects/session/s-1", task_id: 102, status: "open",
      started_at: "2026-11-10T10:00:00+00:00", last_heartbeat_at: "2026-11-10T11:00:00+00:00", ended_at: null, worktree_path: null, branch: null, summary: null,
    },
    { id: "evidence:102", kind: "evidence", task_id: 102, ref: "https://github.com/example/repo/pull/7", drill_uri: "/api/evidence?task_id=102" },
  ],
  edges: [
    { kind: "plan_goal", from: "plan:3", to: "goal:7" },
    { kind: "session_task", from: "session:s-1", to: "task:102" },
    { kind: "evidence_task", from: "evidence:102", to: "task:102" },
    { kind: "goal_task", from: "goal:7", to: "task:102" },
    { kind: "goal_task", from: "goal:7", to: "task:100" },
    { kind: "task_dependency", from: "task:102", to: "task:100" },
  ],
  scope: { goal_id: 7 },
  cursor: null,
  next_cursor: null,
  as_of: "2026-11-10T12:00:00+00:00",
  revision: "r1",
  unknowns: [],
};

// plan_manage list/get rows (tools/plan.py): the plans row, plus title and description
// split from goal (#2480: "<title>\n\n<description>").
const planRow = (id: string, title: string, extra: Record<string, unknown> = {}) => ({
  id,
  goal: title,
  title,
  description: "",
  scope: "default",
  status: "active",
  author: "operator",
  escalated: 0,
  needs_human: false,
  created_at: "2026-11-09T10:00:00+00:00",
  updated_at: "2026-11-10T10:00:00+00:00",
  // #2480: every write to the plan advances it; structure writes send it as expected_revision.
  revision: 12,
  ...extra,
});

export const PLANS = {
  plans: [planRow("3", "Roll out Control Center v2"), planRow("4", "Retire the classic panes", { status: "blocked", escalated: 1, needs_human: true })],
};

const step = (idx: number, description: string, status: string, owner: string | null) => ({
  plan_id: "3",
  id: 30 + idx,
  step_idx: idx,
  description,
  owner,
  status,
  note: null,
  // plan_handoff stores the step's outputs map, read by the next step's owner.
  outputs: idx === 0 ? { pr: "https://github.com/example/repo/pull/1503", summary: "Foundation merged" } : null,
  depends_on: [] as number[],
  retries: 0,
  done_at: status === "done" ? "2026-11-10T08:00:00+00:00" : null,
});

export const PLAN_3 = {
  plan: PLANS.plans[0],
  steps: [step(0, "Build the foundation", "done", "Desktop_Agent2"), step(1, "Build the Work area", "doing", "Desktop_Agent1"), step(2, "Cut over", "open", null), step(3, "Write the changelog", "open", null)],
  checkpoints: [{ seq: 1, note: "Foundation merged", created_at: "2026-11-10T09:00:00+00:00" }],
  next_step_idx: 2,
};

/** What every #2480 structure route answers: {plan, steps, next_step_idx}. */
export const PLAN_3_RESULT = { plan: { ...PLAN_3.plan, updated_at: "2026-11-10T12:00:00+00:00", revision: 13 }, steps: PLAN_3.steps, next_step_idx: 2 };

export const JOINT_3 = {
  plan_id: "3",
  revision: 2,
  goal_id: 7,
  org_unit_id: "web-office",
  pending: null,
  decisions: [],
  team: [
    { identity: "Desktop_Agent1", role: "lead" },
    { identity: "Desktop_Agent2", role: "member" },
  ],
  recovered: false,
  // joint_planning._view_on: the live steps, comment ids, why-chain and the caller's team entry.
  steps: PLAN_3.steps,
  next_step_idx: 2,
  actor: "Desktop_Agent1",
  role: "lead",
  action: "bind",
  comment_id: 9,
  comment_ids: [9],
  review: null,
  why_chain: [],
  caller: { identity: "Desktop_Agent1", role: "lead" },
};

/** What the hub answers a non-member (the operator in these fixtures) for the joint read. */
export const JOINT_REFUSED = { error: "scoped membership refused: operator is not a member of org unit web-office" };

export const AGENTS = {
  agents: [
    { label: "Desktop_Agent1", status: "online", open_sessions: 1, host: "desktop" },
    { label: "Desktop_Agent2", status: "idle", open_sessions: 0, host: "desktop" },
  ],
};

export const CANVAS = { board_id: "default", exists: true, updated_by: "operator", updated_at: "2026-11-10T10:00:00Z", scene: { type: "excalidraw", version: 2, elements: [] } };

/** A board from a hub before #2480: task cards carry no `domain` key. */
export const BOARD_WITHOUT_DOMAIN = JSON.parse(JSON.stringify(BOARD, (k, v) => (k === "domain" ? undefined : v))) as typeof BOARD;

// api.task_search_payload over task_search (src/unified_agent/task_search.py): ranked rows with an explanation.
export const TASK_SEARCH = {
  query: "release",
  truncated: false,
  limit: 20,
  tasks: [
    {
      id: 90, title: "Release checklist from last quarter", status: "done", project: "unified-agent", priority: "med", claimed_by: null,
      combined_score: 0.91,
      explanation: { why: ["lexical"], lexical: 0.88, semantic: null, recency: 0.12, salience: null, access: null, not_applicable: ["salience", "access"], snippet: "the <b>release</b> checklist" },
    },
    {
      id: 101, title: "Write the release notes", status: "open", project: "unified-agent", priority: "med", claimed_by: null,
      combined_score: 0.74,
      explanation: { why: ["lexical", "semantic"], lexical: 0.7, semantic: 0.6, recency: 0.9, salience: null, access: null, not_applicable: ["salience", "access"], snippet: null },
    },
  ],
};

const qhTask = (id: number, title: string, status: string, extra: Record<string, unknown> = {}) => ({ id, title, status, project: "unified-agent", claimed_by: null, priority: "med", ...extra });

// api.queue_health_payload buckets and counts, with queue_hygiene.queue_pressure as backpressure.
export const QUEUE_HEALTH = {
  desktop_open: [qhTask(101, "Write the release notes", "open")],
  dispatch_ready: [],
  interactive_only: [],
  operator_only: [],
  doing: [qhTask(102, "Ship the Work area", "doing", { claimed_by: "Desktop_Agent1" })],
  waiting_review: [qhTask(103, "Review the hub routes", "waiting_review")],
  stale_claims: [qhTask(107, "Old claimed task", "doing", { claimed_by: "Desktop_Agent9", lock_expires_at: "2026-11-08T10:00:00+00:00" })],
  laptop_lane: [],
  blocked_archived: [qhTask(108, "[archived] Superseded spike", "blocked")],
  inconsistent: [],
  other: [],
  counts: {
    desktop_open: 1, dispatch_ready: 0, interactive_only: 0, operator_only: 0, doing: 1, waiting_review: 1, stale_claims: 1,
    laptop_lane: 0, blocked_archived: 1, inconsistent: 0, other: 0, total_active: 5,
  },
  backpressure: { level: "pressure", signals: ["1 task in doing has no live claimant"], notes: ["backlog in default queue: 12 open"] },
};

/** Every read the Work area makes, answered with the realistic data above. */
export const WORK_READS: Record<string, unknown> = {
  "GET /api/board": BOARD,
  "GET /api/platform/objects/task/102": TASK_102,
  "GET /api/platform/objects/task/101": TASK_101,
  "GET /api/overview": TASK_GRAPH_OVERVIEW,
  "GET /api/runs": RUNS,
  "GET /api/evidence": EVIDENCE,
  "GET /api/tasks/102/comments": COMMENTS,
  "GET /api/tasks/101/comments": { revision: 0, comments: [] },
  "GET /api/tasks/102/why-chain": { goal_id: 7, chain: [{ id: 7, title: "A usable Control Center" }] },
  "GET /api/tasks/101/why-chain": { goal_id: null, chain: [] },
  "GET /api/tasks/holds": { holds: [] },
  "GET /api/goals": GOALS,
  "GET /api/goals/7": GOAL_7,

  "GET /api/plans": PLANS,
  "GET /api/plans/3": PLAN_3,
  "GET /api/agents": AGENTS,
  "GET /api/projects": { projects: [] },
  "GET /api/graph-canvas": CANVAS,
  "GET /api/tasks/search": TASK_SEARCH,
  "GET /api/queue-health": QUEUE_HEALTH,
  // routes.approval_detail for the approvals the parked-write tests create (status pending).
  "GET /api/approvals/77": { id: 77, tool: "task_update", status: "pending" },
  "GET /api/approvals/31": { id: 31, tool: "task_update", status: "pending" },
  "GET /api/approvals/40": { id: 40, tool: "task_add", status: "pending" },
};
