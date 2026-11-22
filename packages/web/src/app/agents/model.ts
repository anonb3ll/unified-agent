/**
 * Agents area model: merging the reads into one roster row per label, the
 * state words the spec names (working, idle, paused, offline, retired), and
 * plain-language text for every coded refusal of the enrolment routes.
 */
import type { StateKind } from "../../lib/ui";
import type { AgentRow, LifecycleState, RosterRow, SessionRow } from "./api";

export type AgentState = "working" | "idle" | "paused" | "stopped" | "offline" | "retired";

export const STATE_WORDS: Record<AgentState, { label: string; kind: StateKind; meaning: string }> = {
  working: { label: "Working", kind: "live", meaning: "Has an open session or a claimed task and is sending heartbeats." },
  idle: { label: "Idle", kind: "ok", meaning: "Sending heartbeats with nothing claimed." },
  paused: { label: "Paused", kind: "warn", meaning: "Paused by an operator: takes no new work." },
  stopped: { label: "Stopped", kind: "warn", meaning: "Terminated by an operator: its claims were released." },
  offline: { label: "Offline", kind: "unknown", meaning: "No recent heartbeat, or never seen by the hub." },
  retired: { label: "Retired", kind: "danger", meaning: "Removed: its tokens are revoked and the hub refuses it." },
};

export const STATE_OPTIONS = (Object.keys(STATE_WORDS) as AgentState[]).map((s) => ({ value: s, label: STATE_WORDS[s].label }));

export type HostKind = "local" | "cloud";

export type RosterEntry = {
  label: string;
  displayName: string | null;
  avatar: string | null;
  color: string | null;
  office: string | null;
  profile: string | null;
  /** Where it runs: from the seat when the operator's roster read answered, else from the label. */
  kind: HostKind;
  kindKnown: boolean;
  host: string | null;
  state: AgentState;
  /** Heartbeat status as the hub reported it (online, idle, stale, offline). */
  heartbeat: string | null;
  lastSeen: string | null;
  openSessions: number | null;
  activeTasks: number | null;
  currentTask: { id: number; title: string | null } | null;
  added: boolean | null;
  token: { source: string | null; status: string | null; issuedAt: string | null; expiresAt: string | null } | null;
  lifecycle: LifecycleState | null;
};

/** The hub's cloud-hosted labels; used only when the operator's roster (which has the seat's kind) is unavailable. */
export function kindFromLabel(label: string): HostKind {
  return /^(cloud|gcp)_/i.test(label) ? "cloud" : "local";
}

const LIVE_HEARTBEAT = new Set(["online", "idle", "running", "active"]);

export function deriveState(input: {
  lifecycle: string | null | undefined;
  rosterState: string | null | undefined;
  heartbeat: string | null | undefined;
  openSessions: number | null | undefined;
  activeTasks: number | null | undefined;
}): AgentState {
  const life = input.lifecycle ?? input.rosterState ?? "active";
  if (life === "retired") return "retired";
  if (life === "paused") return "paused";
  if (life === "terminated") return "stopped";
  if (!input.heartbeat || !LIVE_HEARTBEAT.has(input.heartbeat)) return "offline";
  return (input.openSessions ?? 0) > 0 || (input.activeTasks ?? 0) > 0 ? "working" : "idle";
}

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

/**
 * One row per label from every read that answered: the fleet heartbeat list,
 * the operator's roster (seat, kind, token metadata), the lifecycle states
 * and the open sessions (current task). A read that failed contributes
 * nothing; its fields stay null rather than guessed.
 */
export function mergeRoster(sources: {
  fleet?: AgentRow[] | null;
  roster?: RosterRow[] | null;
  lifecycle?: LifecycleState[] | null;
  sessions?: SessionRow[] | null;
}): RosterEntry[] {
  const labels = new Set<string>();
  const fleet = new Map((sources.fleet ?? []).map((a) => [a.label, a]));
  const roster = new Map((sources.roster ?? []).map((r) => [r.agent, r]));
  const lifecycle = new Map((sources.lifecycle ?? []).map((l) => [l.agent, l]));
  for (const key of [...fleet.keys(), ...roster.keys()]) labels.add(key);
  const current = new Map<string, { id: number; title: string | null }>();
  for (const s of sources.sessions ?? []) {
    if (s.task_id == null || current.has(s.agent)) continue;
    if (!["starting", "running", "waiting"].includes(String(s.status))) continue;
    current.set(s.agent, { id: Number(s.task_id), title: str(s.task_title) });
  }
  return [...labels].sort((a, b) => a.localeCompare(b)).map((label) => {
    const f = fleet.get(label);
    const r = roster.get(label);
    const l = lifecycle.get(label) ?? null;
    const persona = (f?.persona ?? null) as { display_name?: string; avatar?: string; color?: string } | null;
    const openSessions = f ? f.open_sessions : null;
    const activeTasks = f ? f.active_tasks : null;
    const heartbeat = f ? f.status : r?.last_seen ? "stale" : null;
    return {
      label,
      displayName: str(f?.display_name) ?? str(persona?.display_name),
      avatar: str(f?.avatar) ?? str(persona?.avatar),
      color: str(f?.color) ?? str(persona?.color),
      office: r?.office ?? null,
      profile: r?.profile ?? null,
      kind: r?.kind === "cloud" ? "cloud" : r?.kind === "local" ? "local" : kindFromLabel(label),
      kindKnown: r?.kind === "cloud" || r?.kind === "local",
      host: str(f?.host),
      state: deriveState({ lifecycle: l?.state, rosterState: r?.state, heartbeat, openSessions, activeTasks }),
      heartbeat,
      lastSeen: str(f?.last_seen) ?? r?.last_seen ?? null,
      openSessions,
      activeTasks,
      currentTask: current.get(label) ?? null,
      added: r ? r.added : null,
      token: r
        ? { source: r.token_source ?? null, status: r.token_status, issuedAt: r.token_issued_at, expiresAt: r.token_expires_at }
        : null,
      lifecycle: l,
    };
  });
}

export function filterRoster(rows: RosterEntry[], filter: { kind: string; state: string; q: string }): RosterEntry[] {
  const q = filter.q.trim().toLowerCase();
  return rows.filter((row) => {
    if (filter.kind && row.kind !== filter.kind) return false;
    if (filter.state && row.state !== filter.state) return false;
    if (!q) return true;
    return [row.label, row.displayName, row.office, row.host, row.profile].some((v) => v && v.toLowerCase().includes(q));
  });
}

/** "2026-11-11 09:12" in local time, or a plain word when unknown. */
export function formatWhen(value: string | null | undefined): string {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Token metadata in words; never a value. */
export function tokenText(token: RosterEntry["token"]): string {
  if (!token) return "Only the operator can see token details";
  if (!token.source && !token.status) return "No token";
  const where = token.source === "managed" ? "Managed token" : token.source === "env" ? "Token from the hub's configuration" : "Token";
  const status = token.status ? `, ${token.status}` : "";
  const expires = token.expiresAt ? `, expires ${formatWhen(token.expiresAt)}` : "";
  return `${where}${status}${expires}`;
}

/** Plain-language text for a refused add or remove, by the hub's `code`. */
export function enrolmentRefusal(code: string | undefined, label: string): string | null {
  switch (code) {
    case "already_added":
      return `${label} is already added: it has a seat or an active token in this organization. The hub never issues a second token for it. Remove it first, or choose another label.`;
    case "retired":
      return `${label} was removed (retired). A retired label cannot be added again; choose another label.`;
    case "sealed_store_required":
      return "Nothing was issued: the hub's secret store is not encrypted, so it refuses to keep a new token unless you accept that explicitly. Tick the box below to accept a token protected only by file permissions, or ask for the store to be sealed.";
    case "operator_required":
      return "Only the human operator can add or remove agents. Sign in as the operator to do this.";
    case "claims_held":
      return `${label} still holds claimed tasks. Release them first, or tick "Release its claimed tasks" to release them as part of the removal.`;
    case "not_found":
      return `The hub does not know ${label} in this organization. It may already have been removed.`;
    case "unavailable":
      return "The hub process cannot run this right now (its stop handlers are not registered). Try again after the hub restarts.";
    case "policy_denied":
    case "agent_access_denied":
      return "A policy rule on the hub refused this. Nothing was changed.";
    case "rate_limited":
      return "Too many requests: the hub's rate limit refused this. Wait a moment and try again.";
    default:
      return null;
  }
}

/** "Add an agent" step 1. */
export const WHERE_OPTIONS = [
  { value: "this", label: "This machine", description: "The machine the hub runs on. The agent connects to the hub locally." },
  { value: "other", label: "Another machine", description: "A desktop or laptop on your network that reaches the hub over the network." },
  { value: "cloud", label: "A cloud host", description: "A hosted agent. It needs a client that can send an Authorization header." },
] as const;

export type Where = (typeof WHERE_OPTIONS)[number]["value"];

export const HARNESS_OPTIONS = [
  { value: "claude-code", label: "Claude Code" },
  { value: "codex", label: "Codex" },
  { value: "cursor", label: "Cursor" },
  { value: "antigravity", label: "Antigravity" },
  { value: "hermes", label: "Hermes" },
  { value: "opencode", label: "OpenCode" },
  { value: "copilot", label: "Copilot" },
  { value: "other", label: "Another MCP client" },
] as const;

export const LABEL_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;

/**
 * The command or config to paste on the agent's host. The bearer is never
 * part of it: the person pastes the token into `UA_TOKEN` themselves, from
 * the copy-once panel, so the text below can be shown and copied freely.
 */
export function connectSnippet(input: { where: Where; harness: string; label: string; hubUrl: string }): string {
  const url = input.where === "this" ? "http://127.0.0.1:8765/mcp" : input.hubUrl;
  if (input.harness === "claude-code") {
    return [
      "# Paste the token from the panel into UA_TOKEN first (it is not in this command).",
      `claude mcp add --scope user --transport http unified-agent ${url} \\`,
      '  --header "Authorization: Bearer $UA_TOKEN"',
    ].join("\n");
  }
  if (input.harness === "codex") {
    return [
      "# ~/.codex/config.toml: export UA_TOKEN with the token from the panel.",
      "[mcp_servers.unified-agent]",
      `url = "${url}"`,
      'bearer_token_env_var = "UA_TOKEN"',
    ].join("\n");
  }
  return JSON.stringify(
    {
      mcpServers: {
        "unified-agent": {
          [input.harness === "antigravity" ? "serverUrl" : "url"]: url,
          headers: { Authorization: "Bearer ${UA_TOKEN}" },
        },
      },
      _note: `Agent label: ${input.label}. Set UA_TOKEN to the token shown once in the Control Center.`,
    },
    null,
    2,
  );
}

export type LifecycleAction = "pause" | "resume" | "terminate" | "retire" | "unretire";

/**
 * Why the signed-in identity may not take a lifecycle action, or null when
 * it may. Mirrors the hub's `agent_lifecycle._authorize`: an operator
 * (`operators` from `GET /api/agent-lifecycle`) may change any agent; an
 * agent may pause or terminate itself, and resume only from a pause or stop
 * it imposed itself; retire and unretire are operator actions, including for
 * the agent's own label. While who may act is not known, nothing is offered.
 */
export function lifecycleRefusal(
  action: LifecycleAction,
  who: { identity: string | null | undefined; label: string; operators: readonly string[] | null; current: { state: string; actor: string | null } | null },
): string | null {
  if (!who.identity || who.operators === null) return "Who may change this agent could not be checked, so these actions are not offered.";
  const operator = who.operators.includes(who.identity);
  if (action === "retire" || action === "unretire") {
    return operator ? null : "Only an operator can retire or unretire an agent, including its own label.";
  }
  if (operator) return null;
  if (who.identity !== who.label) return `Only an operator or ${who.label} itself can change ${who.label}'s state.`;
  if (action === "resume" && who.current && who.current.actor !== who.label) {
    return `${who.label} was ${who.current.state === "terminated" ? "stopped" : "paused"} by ${who.current.actor ?? "someone else"}; only an operator can resume it.`;
  }
  return null;
}
