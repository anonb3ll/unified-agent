/**
 * Agents fixtures, shaped like the hub's answers (`webui/cc_agents.py`,
 * `webui/api.py` agents_payload / agent_detail / org_payload, and the
 * classic routes the area reads). Labels are generic.
 */
import type { MockHandler } from "../support/fetchMock";

/** A bearer-shaped value the tests search for everywhere after the dialog closes. */
export const BEARER = "test-bearer-shown-once-0001";

export const ROSTER = {
  agents: [
    {
      agent: "Desktop_Agent1",
      office: "steward",
      profile: "steward",
      kind: "local",
      added: true,
      state: "active",
      token_source: "managed",
      token_status: "active",
      token_issued_at: "2026-11-01T10:00:00+00:00",
      token_expires_at: null,
      last_seen: "2026-11-11T09:00:00+00:00",
    },
    {
      agent: "cloud_helper",
      office: "platform",
      profile: "developer",
      kind: "cloud",
      added: true,
      state: "active",
      token_source: "managed",
      token_status: "active",
      token_issued_at: "2026-11-02T10:00:00+00:00",
      token_expires_at: "2027-11-02T10:00:00+00:00",
      last_seen: null,
    },
    {
      agent: "Laptop_Old",
      office: "platform",
      profile: "developer",
      kind: "local",
      added: false,
      state: "retired",
      token_source: null,
      token_status: "revoked",
      token_issued_at: null,
      token_expires_at: null,
      last_seen: "2026-10-01T10:00:00+00:00",
    },
  ],
  count: 3,
  offices: [
    { id: "steward", name: "Senior engineer & steward" },
    { id: "platform", name: "Platform Engineering" },
  ],
  kinds: ["local", "cloud"],
  default_profile: "developer",
  sealed_at_rest: true,
  org: null,
};

export const FLEET = {
  agents: [
    {
      label: "Desktop_Agent1",
      kind: "claude-code",
      host: "desktop",
      status: "online",
      last_seen: "2026-11-11T09:00:00+00:00",
      open_sessions: 1,
      active_tasks: 1,
      display_name: "Claude (desktop)",
      avatar: "🛠",
    },
    { label: "Desktop_Agent2", kind: "codex", host: "desktop", status: "idle", last_seen: "2026-11-11T08:00:00+00:00", open_sessions: 0, active_tasks: 0 },
  ],
  counts: { online: 1, idle: 1 },
};

export const LIFECYCLE = {
  agents: [{ agent: "Laptop_Old", state: "retired", reason: "replaced", actor: "operator", since: "2026-10-02T10:00:00+00:00" }],
  registered: [],
  operators: ["operator"],
};

export const OPEN_SESSION = {
  id: "sess-open-1",
  agent: "Desktop_Agent1",
  kind: "claude-code",
  status: "running",
  started_at: "2026-11-11T08:30:00+00:00",
  ended_at: null,
  last_heartbeat_at: "2026-11-11T09:00:00+00:00",
  duration_s: 1800,
  heartbeat_age_s: 5,
  task_id: 2473,
  task_title: "Agents area",
  task_status: "doing",
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
};

export const ENDED_SESSION = { ...OPEN_SESSION, id: "sess-old-1", status: "completed", task_id: 2400, task_title: "Older work", host: "laptop", ended_at: "2026-11-10T10:00:00+00:00" };

export const DETAIL = {
  agent: FLEET.agents[0],
  sessions: [OPEN_SESSION, ENDED_SESSION],
  activity: [{ ulid: "01EVT", channel: "agent.task.claim", payload: { task_id: 2473 }, originator: "Desktop_Agent1", created_at: "2026-11-11T08:31:00+00:00" }],
};

export const BOARD = {
  columns: [
    { status: "doing", cards: [{ kind: "task", id: 2473, title: "Agents area", status: "doing", priority: "high", claimed_by: "Desktop_Agent1", project: "unified-agent" }] },
    { status: "open", cards: [{ kind: "task", id: 9, title: "Someone else's", status: "open", claimed_by: null }] },
  ],
};

export const ORG = {
  offices: [
    {
      id: "steward",
      name: "Senior engineer & steward",
      lead: "Desktop_Agent1",
      members: [{ label: "Desktop_Agent1", status: "online", profile: "steward", display_name: "Claude (desktop)" }],
      owned_epics: [2466],
      default_lanes: ["review"],
      decision_rights: ["review verdicts"],
      open_tasks: 4,
    },
    {
      id: "platform",
      name: "Platform Engineering",
      lead: "Desktop_Agent2",
      members: [{ label: "Desktop_Agent2", status: "idle", profile: "developer", display_name: "Desktop_Agent2" }],
      owned_epics: [],
      default_lanes: ["dispatch-ready"],
      decision_rights: [],
      open_tasks: 2,
    },
  ],
  as_of: "2026-11-11T09:00:00+00:00",
  error: null,
};

export const TEAMS = {
  teams: [
    {
      team: "review-pair",
      description: "An author and a reviewer",
      operating_profile: "steady",
      spec_sha256: "abc",
      seats: [{ label: "Desktop_Agent1", harness: "claude-code" }, { label: "Desktop_Agent2", harness: "codex" }],
      edges: 1,
      launch_order: ["Desktop_Agent2", "Desktop_Agent1"],
    },
  ],
};

export const TEAM_PLAN = {
  team: "review-pair",
  ready: true,
  plan_sha256: "plan-sha-1",
  plan_text: "1. start Desktop_Agent2 on desktop\n2. start Desktop_Agent1 on desktop",
  refusals: [],
  warnings: ["Desktop_Agent1 is already running a session"],
};

export const SKILLS_TEAMS = {
  skills: [
    {
      name: "review",
      version: 3,
      content_hash: "h",
      source: "registry",
      published_by: "Desktop_Agent1",
      published_at: "2026-11-01T10:00:00+00:00",
      latest: true,
      installable: true,
      provenance: { source: "registry" },
    },
  ],
  teams: [
    {
      id: "steward",
      name: "Steward team",
      lead: "Desktop_Agent1",
      members: [{ label: "Desktop_Agent1", profile: "steward", role: "lead", skills: [{ name: "review", version: 3, content_hash: "h", source: "registry" }] }],
      responsibility: ["reviews"],
      permitted_scope: { owned_epics: [], default_lanes: ["review"], decision_rights: ["verdicts"] },
      version: 1,
      content_hash: "t",
      provenance: { source: "registry" },
      drift: false,
    },
  ],
  surfaces: [{ surface_dir: "~/.claude/skills", result: { synced: [{ name: "review" }], stale: [], missing: [], unregistered: [] } }],
  starter_templates: [
    {
      id: "review-pair",
      version: 1,
      content_hash: "c".repeat(64),
      description: "A builder and an independent reviewer.",
      guidance: "Pick two different harnesses.",
      roles: { builder: "Writes the change.", reviewer: "Reviews it." },
      independent_roles: ["builder", "reviewer"],
    },
  ],
  permissions: { publish: false, install: false, team_install: false, drift: true },
};

export const OPERATING = {
  configured: true,
  default: "steady",
  units: {},
  profiles: { steady: { description: "Normal pace", extends: null, settings: { budget: { daily: 10 } }, effective: { budget: { daily: 10 } } } },
  fields: ["budget"],
  enforced_fields: ["budget"],
  policy_edit: { tool: "policy_revision", action: "commit", kind: "policy", block: "operating_profiles", history_route: "/api/config-revisions" },
  offices: [{ id: "steward", name: "Senior engineer & steward", lead: "Desktop_Agent1", members: ["Desktop_Agent1"], profile: null }],
};

export const DEVICES = {
  local_host: "desktop",
  hosts: [
    {
      host: "desktop",
      version: "1.2.3",
      last_seen: "2026-11-11T09:00:00+00:00",
      staleness: "live",
      doctor_freshness: "live",
      doctor: { summary: { live: 3, dormant: 0, degraded: 1, misconfigured: 0 } },
      worktrees: { reported: true, count: 2, stale: 1 },
      agents: [{ label: "Desktop_Agent1" }, { label: "Desktop_Agent2" }],
      executor: { reported: true, unit_status: "active", claim: { task_id: 2473 } },
    },
    { host: "laptop", version: "1.2.2", last_seen: "2026-11-11T08:00:00+00:00", agents: [], executor: { reported: false } },
  ],
  agents: [
    { label: "Desktop_Agent1", host: "desktop", host_source: "doctor", status: "online" },
    { label: "cloud_agent17", host: null, host_source: null, unplaced_host: "cloud-box", status: "idle", credential: { issued_token_ids: [12], fingerprints: [], source: "lifecycle" } },
  ],
  unreported_peers: [{ name: "spare-laptop" }],
  oauth: {
    session_count: 2,
    oauth_enabled: true,
    devices: [
      { device_node: "laptop-node", device_node_id: "n123", bound: true, sessions: [{ session_id: "a".repeat(16), agent: "Laptop_Agent2", auth_method: "device_code" }] },
      { device_node: null, device_node_id: null, bound: false, sessions: [{ session_id: "b".repeat(16), agent: "Remote_Agent", auth_method: "authorization_code" }] },
    ],
  },
};

export const PASSKEYS = {
  passkeys: [
    { credential_id: "Y3JlZGVudGlhbC1pZC0xMjM0NQ", login: "approver@example.com", rp_id: "hub.example.test", alg: -7, label: "Laptop Touch ID", last_used_at: null },
    { credential_id: "b3RoZXItY3JlZGVudGlhbA", login: "other@example.com", rp_id: "hub.example.test", alg: -7, label: "Other approver", last_used_at: 1762851600 },
  ],
  approver_logins: ["approver@example.com", "other@example.com"],
  origin: "https://hub.example.test",
  this_device_login: "approver@example.com",
  passphrase_fallback: false,
};

export const SPRINTS = {
  sprints: [
    {
      id: 7,
      name: "Cutover",
      goal_id: null,
      org_unit: "steward",
      profile: "steady",
      overrides: { planning: "P2" },
      status: "active",
      starts_at: "2026-11-10",
      ends_at: "2026-11-20",
      created_by: "operator",
      task_ids: [2479],
    },
  ],
  statuses: ["planned", "active", "closed"],
};

export const RESOLVED = {
  configured: true,
  task_id: 41,
  agent: "Desktop_Agent2",
  sprint_id: 7,
  unit: "steward",
  settings: { budget: { daily: 10 }, planning: "P3" },
  sources: { "budget.daily": "office:steward", planning: "run:task-41:overrides" },
  layers: [],
  enforcement: { budget: "enforced", planning: "recorded" },
  fingerprint: "abc123",
  run_settings: { profile: "steady", overrides: { planning: "P3" }, set_by: "operator", set_at: "2026-11-11T09:00:00+00:00" },
};

export const CONFIG_REVISIONS = {
  kind: "policy",
  revisions: [
    { revision: 3, action: "update", actor: "operator", ts: "2026-11-10T10:00:00+00:00" },
    { revision: 2, action: "update", actor: "operator", ts: "2026-11-09T10:00:00+00:00" },
  ],
  count: 2,
  drift: true,
  latest_diff: "- approval: off\n+ approval: on",
};

export const NODES = {
  nodes: [
    {
      node: "desktop-node",
      agent: "Desktop_Agent2",
      backends: ["codex", "agy"],
      harnesses: {},
      worktree_roots: [],
      last_heartbeat: "2026-11-11T09:00:00+00:00",
      acceptance: { state: "accepted" },
    },
  ],
  recent_items: [],
  retired_nodes: [{ node: "old-node", retired_at: "2026-10-01T10:00:00+00:00", retired_by: "operator", retired_reason: "replaced" }],
  acceptance_required: true,
};

export const GRAPH = {
  nodes: [
    { id: "agent:Desktop_Agent1", label: "Desktop_Agent1", type: "agent", status: "online" },
    { id: "task:2473", label: "#2473 Agents area", type: "task", status: "doing" },
  ],
  links: [{ source: "agent:Desktop_Agent1", target: "task:2473", kind: "claim" }],
};

export const CAPABILITIES = {
  available: true,
  catalog: {
    schema_version: "1",
    families: [{ id: "agents", label: "Agents", pilot: false, definition: "Agent management" }],
    items: [
      {
        id: "cap-enrol",
        family: "agents",
        capability: "Agent enrolment",
        customer_purpose: "Add and remove agents",
        origin: { kind: "task", refs: [] },
        implementation_state: "shipped-and-used",
        consumer: "operator",
        entry_point: { kind: "route", ref: "/api/action/agent/add" },
        connection_state: "live",
        evidence: [],
        boundaries: { security: "", identity: "", approval: "", privacy: "", release: "", tenant_workspace: "" },
      },
    ],
    queue_coverage: [],
    traceable_paths: [],
  },
  meta: {},
};

export const HARNESS = { published: { version: 4, published_by: "operator", published_at: "2026-11-01T10:00:00+00:00" } };

export const ADD_RESULT = {
  agent: "Laptop_New",
  office: "platform",
  profile: "developer",
  kind: "local",
  token: BEARER,
  token_shown_once: true,
  token_id: 41,
  token_hash_prefix: "sha256:4f2a",
  issued_at: "2026-11-11T10:00:00+00:00",
  expires_at: null,
  warnings: ["Add the label to the device's harness configuration."],
};

export const REMOVE_RESULT = {
  agent: "Desktop_Agent1",
  state: "retired",
  changed: true,
  released: [{ task_id: 2473 }],
  revoked_tokens: 1,
  revoked_session_credentials: 2,
  secret_deleted: true,
  seat_removed: true,
  warnings: [],
};

export const parked = (tool: string, approvalId = 88) => ({ status: "approval_pending", approval_id: approvalId, code: "approval_required", tool });

/** Every read the Agents area makes, answered with the fixtures above. */
export const AGENTS_READS: Record<string, unknown> = {
  "GET /api/agent-roster": ROSTER,
  "GET /api/agents": FLEET,
  "GET /api/agent-lifecycle": LIFECYCLE,
  "GET /api/sessions": { sessions: [OPEN_SESSION], open: 1, total: 1 },
  "GET /api/agents/Desktop_Agent1": DETAIL,
  "GET /api/board": BOARD,
  "GET /api/org": ORG,
  "GET /api/teams": TEAMS,
  "GET /api/teams/plan": TEAM_PLAN,
  "GET /api/skills-teams": SKILLS_TEAMS,
  "GET /api/operating": OPERATING,
  "GET /api/operating/sprints": SPRINTS,
  "GET /api/operating/resolve": RESOLVED,
  "GET /api/oauth/passkeys": PASSKEYS,
  "GET /api/config-revisions": CONFIG_REVISIONS,
  "GET /api/devices": DEVICES,
  "GET /api/nodes": NODES,
  "GET /api/agent-graph": GRAPH,
  "GET /api/capability-catalog": CAPABILITIES,
  "GET /api/harness-config": HARNESS,
};

/** The same reads, answering with nothing in them. */
export const EMPTY_READS: Record<string, unknown> = {
  "GET /api/agent-roster": { ...ROSTER, agents: [], count: 0 },
  "GET /api/agents": { agents: [], counts: {} },
  "GET /api/agent-lifecycle": { agents: [], registered: [], operators: [] },
  "GET /api/sessions": { sessions: [], open: 0, total: 0 },
  "GET /api/board": { columns: [] },
  "GET /api/org": { offices: [], error: null },
  "GET /api/teams": { teams: [] },
  "GET /api/skills-teams": { ...SKILLS_TEAMS, skills: [], teams: [], surfaces: [], starter_templates: [] },
  "GET /api/operating": { configured: false, default: null, units: {}, profiles: {}, fields: [], enforced_fields: [], offices: [] },
  "GET /api/operating/sprints": { sprints: [], statuses: ["planned", "active", "closed"] },
  "GET /api/operating/resolve": { configured: false, task_id: 41, agent: null, sprint_id: null, unit: null, settings: {}, sources: {}, layers: [], enforcement: {}, fingerprint: "0", run_settings: null },
  "GET /api/oauth/passkeys": { passkeys: [], approver_logins: [] },
  "GET /api/config-revisions": { kind: "policy", revisions: [], count: 0, drift: false, latest_diff: "" },
  "GET /api/devices": { hosts: [], agents: [], unreported_peers: [] },
  "GET /api/nodes": { nodes: [], recent_items: [], retired_nodes: [], acceptance_required: false },
  "GET /api/agent-graph": { nodes: [], links: [] },
  "GET /api/capability-catalog": { available: true, catalog: { schema_version: "1", families: [], items: [], queue_coverage: [], traceable_paths: [] }, meta: {} },
  "GET /api/harness-config": { published: null },
};

/** Fetch handlers for every Agents read, overridable per test. */
export function agentsHub(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  const reads = Object.fromEntries(Object.entries(AGENTS_READS).map(([key, body]) => [key, { body } as MockHandler]));
  return {
    ...reads,
    // Any other label is unknown to the heartbeat table.
    ...overrides,
  };
}

export const OPERATOR_ONLY = { status: 403, body: { error: "this action is reserved for the human operator", code: "operator_required" } };
