/**
 * Agents area data layer (spec §7: new fetchers live here; §9.3).
 *
 * Reads are rebuilt here with a typed error (`HubReadError`, carrying the HTTP
 * status and the hub's `code`), so a page can tell "only the operator may see
 * this" (403 `operator_required`) from "the hub failed". Types are reused from
 * the classic modules (read only). Every write path the area uses is named in
 * `WRITE_PATHS`, with its body, so the hub contract is visible in one place
 * (`docs/control-center-api.md`, section `agents`).
 *
 * The add answer carries a bearer. It is never cached: no query here reads it,
 * no write here invalidates or sets a query with it, and nothing stores it.
 */
import type { DevicesPayload } from "../../lib/api/devices";
import type { AgentGraphPayload } from "../../lib/api/graph";
import type { NodesPayload } from "../../lib/api/nodes";
import { normalizeCapabilityCatalogPayload, type CapabilityCatalogPayload } from "../../lib/capabilities/types";
import type { ApproverPasskeysPayload } from "../../lib/api/auth";
import { SPRINT_STATUSES, type OperatingResolved, type OperatingSprintsPayload, type OperatingView } from "../../lib/operating/api";
import type { ActivityEvent, AgentRow, AgentsPayload, SessionRow } from "../../lib/sessions/types";
import type { SkillsTeamsPayload } from "../../lib/skills-teams/types";
import type { TeamPlan, TeamSummary, TeamUpResult } from "../../lib/teams/types";

export type {
  ActivityEvent,
  AgentGraphPayload,
  ApproverPasskeysPayload,
  OperatingResolved,
  OperatingSprintsPayload,
  AgentRow,
  AgentsPayload,
  CapabilityCatalogPayload,
  DevicesPayload,
  NodesPayload,
  OperatingView,
  SessionRow,
  SkillsTeamsPayload,
  TeamPlan,
  TeamSummary,
  TeamUpResult,
};

export class HubReadError extends Error {
  status: number;
  /** The hub's machine code (`operator_required`, `not_found`), when it sent one. */
  code: string | null;
  /** The hub's `error` text, for the technical detail. */
  hubError: string | null;
  constructor(what: string, status: number, detail: string) {
    super(`${what} HTTP ${status}${detail ? `: ${detail.slice(0, 200)}` : ""}`);
    this.name = "HubReadError";
    this.status = status;
    let code: string | null = null;
    let hubError: string | null = null;
    try {
      const parsed = JSON.parse(detail) as { code?: unknown; error?: unknown };
      if (typeof parsed.code === "string" && parsed.code) code = parsed.code;
      if (typeof parsed.error === "string" && parsed.error) hubError = parsed.error;
    } catch {
      /* not JSON: keep the raw text in the message only */
    }
    this.code = code;
    this.hubError = hubError;
  }
}

export async function getJson<T>(path: string, what: string, signal?: AbortSignal): Promise<T> {
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

/** True for a read the hub refused because it is the human operator's alone. */
export function isOperatorOnly(error: unknown): boolean {
  return error instanceof HubReadError && error.status === 403;
}

// --- Query keys --------------------------------------------------------------

/** Every key starts with "agents"; a write invalidates `AGENTS_KEYS.all`. */
export const AGENTS_KEYS = {
  all: ["agents"] as const,
  roster: ["agents", "roster"] as const,
  fleet: ["agents", "fleet"] as const,
  lifecycle: ["agents", "lifecycle"] as const,
  org: ["agents", "org"] as const,
  detail: (label: string) => ["agents", "detail", label] as const,
  tasks: (label: string) => ["agents", "tasks", label] as const,
  devices: ["agents", "devices"] as const,
  nodes: ["agents", "nodes"] as const,
  teams: ["agents", "teams"] as const,
  teamPlan: (team: string) => ["agents", "teams", "plan", team] as const,
  skillsTeams: ["agents", "skills-teams"] as const,
  operating: ["agents", "operating"] as const,
  /** Every sprint list (any status filter) and every resolved run view; a sprint or run write invalidates both. */
  operatingSprints: ["agents", "operating", "sprints"] as const,
  operatingResolve: ["agents", "operating", "resolve"] as const,
  passkeys: ["agents", "passkeys"] as const,
  configRevisions: (kind: string) => ["agents", "config-revisions", kind] as const,
  capabilities: ["agents", "capabilities"] as const,
  graph: ["agents", "graph"] as const,
  harness: ["agents", "harness-config"] as const,
};

/** Every write this area makes, by hub route. */
export const WRITE_PATHS = {
  /**
   * `{label, office, profile?, kind?, expires_days?, note?, allow_plaintext_at_rest?}`.
   * 200 carries the bearer once (`token`); 409 `already_added`, `retired`,
   * `sealed_store_required`; 202 parked (not resumable: the answer carries the bearer).
   */
  agentAdd: "/api/action/agent/add",
  /** `{label, reason, force_release?}`; 409 `claims_held`; 404 `not_found`; 503 `unavailable`. Resumable. */
  agentRemove: "/api/action/agent/remove",
  /** `{agent, action: pause|resume|terminate|retire|unretire, reason, owner?}`. */
  lifecycle: "/api/agent-lifecycle",
  /** `{team, plan_sha256}`: start every seat of a team from its reviewed plan. */
  teamUp: "/api/action/team-up",
  /** `{action: start|evaluate, node}`: run or re-check a device's acceptance. */
  nodeAcceptance: "/api/action/node-acceptance",
  /** `{action: retire|unretire, node, reason}`. */
  nodeRetire: "/api/action/node-retire",
  /** `{action: "publish", bundle: {...}}`: publish a harness configuration bundle. */
  harnessConfig: "/api/action/harness-config",
  /**
   * `{op: add, name, profile?, org_unit?, overrides?, starts_at?, ends_at?}`,
   * `{op: update, sprint_id, name?, status?, profile?, org_unit?, overrides?}`,
   * `{op: link|unlink, sprint_id, task_id}`. 400 names the refusal; 202 parked.
   */
  operatingSprint: "/api/action/operating-sprint",
  /** `{op: set, task_id, profile?, overrides?}` (replaces the run layer) or `{op: clear, task_id}`. 202 parked. */
  operatingRun: "/api/action/operating-run",
  /** `{action: "restart_service", host, unit}`: the hub's own host only. 202 parked (`approval_id`). */
  fleet: "/api/action/fleet",
  /** `{device}` (every session bound to one device) or `{session_id}`. Approval-gated: 202 parked. */
  oauthRevoke: "/api/action/oauth-revoke",
  /** `{}` → WebAuthn creation options for the approver on this device. */
  passkeyOptions: "/api/oauth/passkeys/options",
  /** The signed attestation from `navigator.credentials.create`, with a label. */
  passkeyRegister: "/api/oauth/passkeys",
  /** `{credential_id}`: remove one of this device approver's passkeys. */
  passkeyDelete: "/api/oauth/passkeys/delete",
  /** `{kind, revision}`: restore an authority-config revision (task #1352). Approval-gated: 202 parked. */
  configRevision: "/api/action/config-revision",
  /**
   * Skill and team catalog actions (`{action, params}`): `publish`, `install`,
   * `team_install`, `starter_preview`, `starter_install`. A 200 can still be a
   * refusal (`blocked`, `ok: false` or `error`, with review findings).
   */
  skillsTeams: "/api/action/skills-teams",
} as const;

// --- Enrolment (operator only) -----------------------------------------------

export type RosterRow = {
  agent: string;
  office: string | null;
  profile: string | null;
  kind: "local" | "cloud" | string | null;
  added: boolean;
  state: string | null;
  token_source: "managed" | "env" | null | string;
  token_status: string | null;
  token_issued_at: string | null;
  token_expires_at: string | null;
  last_seen: string | null;
};

export type RosterPayload = {
  agents: RosterRow[];
  count: number;
  offices: { id: string; name: string }[];
  kinds: string[];
  default_profile: string;
  /** Whether the managed store encrypts bearers; false means the add form needs the opt-in. */
  sealed_at_rest: boolean;
  org: string | null;
};

/** GET /api/agent-roster. Never carries a bearer or a digest. */
export async function fetchRoster(signal?: AbortSignal): Promise<RosterPayload> {
  const body = await getJson<Partial<RosterPayload>>("/api/agent-roster", "agent roster", signal);
  const agents = Array.isArray(body.agents) ? body.agents : [];
  return {
    agents: agents.filter((a): a is RosterRow => !!a && typeof a.agent === "string"),
    count: typeof body.count === "number" ? body.count : agents.length,
    offices: Array.isArray(body.offices) ? body.offices : [],
    kinds: Array.isArray(body.kinds) ? body.kinds : ["local", "cloud"],
    default_profile: typeof body.default_profile === "string" ? body.default_profile : "developer",
    sealed_at_rest: body.sealed_at_rest === true,
    org: typeof body.org === "string" ? body.org : null,
  };
}

/** The 200 answer of an add: the only place a bearer ever appears. */
export type AgentAddResult = {
  agent: string;
  office: string;
  profile: string;
  kind: string;
  token: string;
  token_shown_once?: boolean;
  token_id?: number | string | null;
  token_hash_prefix?: string | null;
  issued_at?: string | null;
  expires_at?: string | null;
  warnings?: string[];
};

export type AgentAddBody = {
  label: string;
  office: string;
  profile?: string;
  kind?: "local" | "cloud";
  expires_days?: number;
  note?: string;
  allow_plaintext_at_rest?: boolean;
};

export type AgentRemoveBody = { label: string; reason: string; force_release?: boolean };

export type AgentRemoveResult = {
  agent: string;
  state: string;
  changed: boolean;
  released?: { task_id: number; event_ulid?: string }[];
  revoked_tokens?: number;
  revoked_session_credentials?: number;
  secret_deleted?: boolean;
  seat_removed?: boolean;
  warnings?: string[];
};

// --- Fleet, lifecycle, org ---------------------------------------------------

/** GET /api/agents: heartbeat state, host, open sessions and claims per agent. */
export async function fetchFleet(signal?: AbortSignal): Promise<AgentsPayload> {
  const body = await getJson<Partial<AgentsPayload>>("/api/agents", "agents", signal);
  return {
    agents: Array.isArray(body.agents) ? body.agents : [],
    counts: body.counts && typeof body.counts === "object" ? body.counts : {},
    ...(typeof body.error === "string" && body.error ? { error: body.error } : {}),
  };
}

export type LifecycleState = {
  agent: string;
  state: "active" | "paused" | "terminated" | "retired" | string;
  reason: string | null;
  actor: string | null;
  since: string | null;
  owner_of_record?: string | null;
  owner_source?: string | null;
};

export type LifecyclePayload = { agents: LifecycleState[]; registered?: LifecycleState[]; operators: string[] };

/** GET /api/agent-lifecycle: lifecycle state per label (agents never changed are active). */
export async function fetchLifecycle(signal?: AbortSignal): Promise<LifecyclePayload> {
  const body = await getJson<Partial<LifecyclePayload>>("/api/agent-lifecycle", "agent lifecycle", signal);
  return {
    agents: Array.isArray(body.agents) ? body.agents : [],
    registered: Array.isArray(body.registered) ? body.registered : [],
    operators: Array.isArray(body.operators) ? body.operators : [],
  };
}

export type OrgMember = {
  label: string;
  status: string;
  last_seen?: string | null;
  profile: string | null;
  display_name?: string;
  avatar?: string;
  color?: string;
  bio?: string;
};

export type Office = {
  id: string;
  name: string;
  lead: string;
  members: OrgMember[];
  owned_epics: number[];
  default_lanes: string[];
  decision_rights: string[];
  open_tasks: number;
};

export type OrgPayload = { offices: Office[]; as_of?: string; error?: string | null };

/** GET /api/org: offices from the role registry, projected onto heartbeats and open tasks. */
export async function fetchOrg(signal?: AbortSignal): Promise<OrgPayload> {
  const body = await getJson<Partial<OrgPayload>>("/api/org", "org", signal);
  return {
    offices: Array.isArray(body.offices) ? body.offices : [],
    ...(body.as_of ? { as_of: body.as_of } : {}),
    ...(typeof body.error === "string" && body.error ? { error: body.error } : {}),
  };
}

// --- One agent ---------------------------------------------------------------

export type AgentDetail = { agent: AgentRow; sessions: SessionRow[]; activity: ActivityEvent[] };

/**
 * GET /api/agents/{label}. `null` when the hub has never seen this label
 * heartbeat (404 `unknown agent`): the page still shows its seat and claims.
 */
export async function fetchAgentDetail(label: string, signal?: AbortSignal): Promise<AgentDetail | null> {
  try {
    const body = await getJson<Partial<AgentDetail>>(`/api/agents/${encodeURIComponent(label)}`, "agent", signal);
    if (!body.agent) return null;
    return {
      agent: body.agent,
      sessions: Array.isArray(body.sessions) ? body.sessions : [],
      activity: Array.isArray(body.activity) ? body.activity : [],
    };
  } catch (error) {
    if (error instanceof HubReadError && error.status === 404) return null;
    throw error;
  }
}

/** The `wait` block of a `?wait_for=connected` answer (`docs/control-center-api.md`). */
export type AgentWait = { for: string; since: string | null; timeout_s: number; satisfied: boolean; waited_s: number };

/** The longest hold the hub grants one read (`routes.AGENT_WAIT_MAX_S`). */
export const AGENT_WAIT_MAX_S = 25;

/**
 * One long-poll: `GET /api/agents/{label}?wait_for=connected&timeout=&since=`.
 * The hub holds it until the label is seen at or after `since`, or `timeoutS`
 * passes. Null for a 204 (the hub saw the client go away); a 429
 * (`rate_limited`), 400, 404 or other failure throws `HubReadError`.
 */
export async function fetchAgentWait(
  label: string,
  { since, timeoutS = AGENT_WAIT_MAX_S }: { since: string | null; timeoutS?: number },
  signal?: AbortSignal,
): Promise<{ detail: AgentDetail; wait: AgentWait } | null> {
  const params = new URLSearchParams({ wait_for: "connected", timeout: String(timeoutS) });
  if (since) params.set("since", since);
  const response = await fetch(`/api/agents/${encodeURIComponent(label)}?${params}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (response.status === 204) return null;
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new HubReadError("agent", response.status, detail);
  }
  const body = (await response.json()) as Partial<AgentDetail> & { wait?: Partial<AgentWait> };
  const wait = body.wait ?? {};
  return {
    detail: {
      agent: body.agent as AgentRow,
      sessions: Array.isArray(body.sessions) ? body.sessions : [],
      activity: Array.isArray(body.activity) ? body.activity : [],
    },
    wait: {
      for: typeof wait.for === "string" ? wait.for : "connected",
      since: typeof wait.since === "string" ? wait.since : null,
      timeout_s: typeof wait.timeout_s === "number" ? wait.timeout_s : timeoutS,
      satisfied: wait.satisfied === true,
      waited_s: typeof wait.waited_s === "number" ? wait.waited_s : 0,
    },
  };
}

export type ClaimedTask = { id: number | string; title: string | null; status: string | null; priority: string | null; project: string | null };

/**
 * The tasks this agent has claimed, from the board filtered by agent
 * (`GET /api/board?agent=`). Only cards whose `claimed_by` is the label count.
 */
export async function fetchClaimedTasks(label: string, signal?: AbortSignal): Promise<{ tasks: ClaimedTask[]; error?: string }> {
  const params = new URLSearchParams({ agent: label });
  const body = await getJson<{ columns?: { status?: string; cards?: Record<string, unknown>[] }[]; error?: unknown }>(
    `/api/board?${params}`,
    "board",
    signal,
  );
  const tasks: ClaimedTask[] = [];
  const seen = new Set<string>();
  const visit = (card: Record<string, unknown>, column: string | undefined) => {
    if (card.claimed_by === label && (card.kind === undefined || card.kind === "task")) {
      const id = card.id as number | string;
      if (!seen.has(String(id))) {
        seen.add(String(id));
        tasks.push({
          id,
          title: typeof card.title === "string" ? card.title : null,
          status: typeof card.status === "string" ? card.status : (column ?? null),
          priority: typeof card.priority === "string" ? card.priority : null,
          project: typeof card.project === "string" ? card.project : null,
        });
      }
    }
    if (Array.isArray(card.subtasks)) for (const sub of card.subtasks as Record<string, unknown>[]) visit(sub, column);
  };
  for (const column of Array.isArray(body.columns) ? body.columns : []) {
    for (const card of Array.isArray(column.cards) ? column.cards : []) visit(card, column.status);
  }
  return { tasks, ...(typeof body.error === "string" && body.error ? { error: body.error } : {}) };
}

// --- Hosts -------------------------------------------------------------------

export async function fetchDevices(signal?: AbortSignal): Promise<DevicesPayload> {
  const body = await getJson<Partial<DevicesPayload>>("/api/devices", "devices", signal);
  return {
    hosts: Array.isArray(body.hosts) ? body.hosts : [],
    local_host: body.local_host ?? null,
    agents: Array.isArray(body.agents) ? body.agents : [],
    unreported_peers: Array.isArray(body.unreported_peers) ? body.unreported_peers : [],
    // Signed-in devices (hub OAuth sessions); null when OAuth sign-in is off.
    oauth: body.oauth && typeof body.oauth === "object" ? { ...body.oauth, devices: Array.isArray(body.oauth.devices) ? body.oauth.devices : [] } : null,
    ...(body.tailnet_census ? { tailnet_census: body.tailnet_census } : {}),
    ...(typeof body.error === "string" && body.error ? { error: body.error } : {}),
    ...(Array.isArray(body.errors) ? { errors: body.errors } : {}),
  };
}

export async function fetchNodes(signal?: AbortSignal): Promise<NodesPayload> {
  const body = await getJson<Partial<NodesPayload>>("/api/nodes", "nodes", signal);
  return {
    nodes: Array.isArray(body.nodes) ? body.nodes : [],
    recent_items: Array.isArray(body.recent_items) ? body.recent_items : [],
    retired_nodes: Array.isArray(body.retired_nodes) ? body.retired_nodes : [],
    acceptance_required: body.acceptance_required === true,
    ...(typeof body.error === "string" && body.error ? { error: body.error } : {}),
  };
}

export type HarnessConfigStatus = {
  published?: { version?: number | string | null; sha256?: string | null; published_by?: string | null; published_at?: string | null } | null;
  [key: string]: unknown;
};

export function fetchHarnessConfig(signal?: AbortSignal): Promise<HarnessConfigStatus> {
  return getJson<HarnessConfigStatus>("/api/harness-config", "harness configuration", signal);
}

// --- Teams, skills, capabilities, topology -----------------------------------

export async function fetchTeams(signal?: AbortSignal): Promise<TeamSummary[]> {
  const body = await getJson<{ teams?: unknown }>("/api/teams", "teams", signal);
  const rows: unknown[] = Array.isArray(body.teams) ? body.teams : [];
  return rows.filter((row): row is TeamSummary => {
    const item = row as Partial<TeamSummary> | null;
    return !!item && typeof item.team === "string" && Array.isArray(item.seats) && Array.isArray(item.launch_order);
  });
}

export function fetchTeamPlan(team: string, signal?: AbortSignal): Promise<TeamPlan> {
  return getJson<TeamPlan>(`/api/teams/plan?team=${encodeURIComponent(team)}`, "team plan", signal);
}

/** Skills and registry teams; with `surfaceDir`, also how that directory's installed skills compare with the registry. */
export async function fetchSkillsTeams(signal?: AbortSignal, surfaceDir = ""): Promise<SkillsTeamsPayload> {
  const query = surfaceDir ? `?surface_dir=${encodeURIComponent(surfaceDir)}` : "";
  const body = await getJson<Partial<SkillsTeamsPayload>>(`/api/skills-teams${query}`, "skills and teams", signal);
  return {
    skills: Array.isArray(body.skills) ? body.skills : [],
    teams: Array.isArray(body.teams) ? body.teams : [],
    surfaces: Array.isArray(body.surfaces) ? body.surfaces : [],
    permissions: body.permissions ?? { publish: false, install: false, team_install: false, drift: false },
    ...(Array.isArray(body.starter_templates) ? { starter_templates: body.starter_templates } : {}),
    ...(typeof body.error === "string" && body.error ? { error: body.error } : {}),
  };
}

export async function fetchOperating(signal?: AbortSignal): Promise<OperatingView> {
  const body = await getJson<Partial<OperatingView>>("/api/operating", "operating profiles", signal);
  return {
    configured: Boolean(body.configured),
    default: body.default ?? null,
    units: body.units ?? {},
    profiles: body.profiles ?? {},
    fields: Array.isArray(body.fields) ? body.fields : [],
    enforced_fields: Array.isArray(body.enforced_fields) ? body.enforced_fields : [],
    offices: Array.isArray(body.offices) ? body.offices : [],
    ...(body.policy_edit ? { policy_edit: body.policy_edit } : {}),
    ...(typeof body.error === "string" && body.error ? { error: body.error } : {}),
    ...(typeof body.code === "string" && body.code ? { code: body.code } : {}),
  };
}

/** Sprints, optionally of one status (`planned`, `active`, `closed`), each with its linked task ids. */
export async function fetchOperatingSprints(status: string, signal?: AbortSignal): Promise<OperatingSprintsPayload> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  const body = await getJson<Partial<OperatingSprintsPayload>>(`/api/operating/sprints${query}`, "sprints", signal);
  return {
    sprints: (Array.isArray(body.sprints) ? body.sprints : []).map((sprint) => ({
      ...sprint,
      overrides: sprint.overrides ?? {},
      task_ids: Array.isArray(sprint.task_ids) ? sprint.task_ids : [],
    })),
    statuses: Array.isArray(body.statuses) ? body.statuses : [...SPRINT_STATUSES],
  };
}

/** How one task's next run resolves, with the layer that set each value and its stored run layer. */
export function fetchOperatingResolve(taskId: number, signal?: AbortSignal): Promise<OperatingResolved> {
  return getJson<OperatingResolved>(`/api/operating/resolve?task_id=${taskId}`, "resolved run settings", signal);
}

/** Approver passkeys and whether this browser's device may register one (task #1322). */
export async function fetchPasskeys(signal?: AbortSignal): Promise<ApproverPasskeysPayload> {
  const body = await getJson<Partial<ApproverPasskeysPayload>>("/api/oauth/passkeys", "approver passkeys", signal);
  return {
    ...body,
    passkeys: Array.isArray(body.passkeys) ? body.passkeys : [],
    approver_logins: Array.isArray(body.approver_logins) ? body.approver_logins : [],
  };
}

export async function fetchCapabilities(signal?: AbortSignal): Promise<CapabilityCatalogPayload> {
  return normalizeCapabilityCatalogPayload(await getJson<unknown>("/api/capability-catalog", "capability catalog", signal));
}

export async function fetchAgentGraph(signal?: AbortSignal): Promise<AgentGraphPayload> {
  const body = await getJson<Partial<AgentGraphPayload>>("/api/agent-graph", "agent graph", signal);
  return {
    nodes: Array.isArray(body.nodes) ? body.nodes : [],
    links: Array.isArray(body.links) ? body.links : [],
    ...(typeof body.error === "string" && body.error ? { error: body.error } : {}),
  };
}

// --- Authority-config revisions (task #1352) ----------------------------------

export const CONFIG_REVISION_KINDS = ["policy", "profile", "approval_rule", "org"] as const;

export type ConfigRevisionRow = { revision: number; action: string; actor: string; ts: string; restores_revision?: number | null };

export type ConfigRevisionList = { kind: string; revisions: ConfigRevisionRow[]; count: number; drift: boolean; latest_diff: string; error?: string };

export async function fetchConfigRevisions(kind: string, signal?: AbortSignal): Promise<ConfigRevisionList> {
  const body = await getJson<Partial<ConfigRevisionList>>(`/api/config-revisions?kind=${encodeURIComponent(kind)}`, "configuration revisions", signal);
  return {
    kind: body.kind || kind,
    revisions: Array.isArray(body.revisions) ? body.revisions : [],
    count: typeof body.count === "number" ? body.count : 0,
    drift: body.drift === true,
    latest_diff: typeof body.latest_diff === "string" ? body.latest_diff : "",
    ...(typeof body.error === "string" && body.error ? { error: body.error } : {}),
  };
}
