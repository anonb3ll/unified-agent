/**
 * Pure helpers for the Sessions area: plain-language states, list ordering
 * and filters, transcript entries built from hub events, and the guardrails
 * line derived from what the hub reports.
 */
import type { ActivityEvent, DispatchRouteRow, SessionRow } from "../../lib/sessions/types";
import type { CrewMessage } from "../../lib/api/crew";
import type { NodeDispatchItem } from "../../lib/api/nodes";
import type { StateKind } from "../../lib/ui";

const OPEN = new Set(["starting", "running", "waiting"]);

export function isOpen(status: string | null | undefined): boolean {
  return status != null && OPEN.has(status);
}

/** Plain words for a session status, and the pill that shows it. */
export function sessionState(status: string | null | undefined): { label: string; kind: StateKind } {
  switch ((status ?? "").toLowerCase()) {
    case "starting":
      return { label: "Starting", kind: "warn" };
    case "running":
      return { label: "Running", kind: "live" };
    case "waiting":
      return { label: "Waiting", kind: "warn" };
    case "completed":
    case "complete":
    case "done":
      return { label: "Finished", kind: "ok" };
    case "failed":
    case "error":
      return { label: "Failed", kind: "danger" };
    case "abandoned":
      return { label: "Abandoned", kind: "danger" };
    case "timeout":
      return { label: "Timed out", kind: "danger" };
    case "":
      return { label: "Unknown", kind: "unknown" };
    default:
      return { label: status ? status.charAt(0).toUpperCase() + status.slice(1) : "Unknown", kind: "unknown" };
  }
}

export type StatusFilter = "all" | "open" | "ended";

export function parseStatusFilter(value: string): StatusFilter {
  return value === "open" || value === "ended" ? value : "all";
}

/** Last time the session showed life: heartbeat, else end, else start. */
export function lastActivity(session: SessionRow): string | null {
  return session.last_heartbeat_at ?? session.ended_at ?? session.started_at ?? null;
}

function time(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Open sessions first, then the most recently active. */
export function orderSessions(sessions: readonly SessionRow[]): SessionRow[] {
  return [...sessions].sort((a, b) => {
    const open = Number(isOpen(b.status)) - Number(isOpen(a.status));
    if (open !== 0) return open;
    return time(lastActivity(b)) - time(lastActivity(a));
  });
}

export type ListFilter = { status: StatusFilter; agent: string; host: string; q: string };

/** Host and search filter in the browser; status and agent are also sent to the hub. */
export function filterSessions(sessions: readonly SessionRow[], filter: ListFilter): SessionRow[] {
  const q = filter.q.trim().toLowerCase();
  return sessions.filter((s) => {
    if (filter.status === "open" && !isOpen(s.status)) return false;
    if (filter.status === "ended" && isOpen(s.status)) return false;
    if (filter.agent && s.agent !== filter.agent) return false;
    if (filter.host && (s.host ?? "") !== filter.host) return false;
    if (!q) return true;
    const hay = [s.id, nodeItemId(s), s.agent, s.task_title, s.task_id != null ? `#${s.task_id}` : "", s.host, s.summary, s.branch, s.kind]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
}

export function hostOf(session: SessionRow): string | null {
  const node = session.meta?.node;
  return session.host || (typeof node === "string" && node ? node : null);
}

/** The device dispatch item behind a session, when it ran through a node. */
export function nodeItemId(session: SessionRow): string | null {
  const id = session.meta?.item_id;
  return typeof id === "string" && id ? id : null;
}

export function formatWhen(value: string | null | undefined): string {
  if (!value) return "Not reported";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  return new Date(parsed).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** Cost and token use, only when the hub reported them. */
export function usageOf(session: SessionRow, events: readonly ActivityEvent[] = []): { cost: string | null; tokens: string | null } {
  const meta = session.meta ?? {};
  const sources: Record<string, unknown>[] = [meta];
  if (meta.usage && typeof meta.usage === "object") sources.push(meta.usage as Record<string, unknown>);
  for (const event of events) {
    const data = (event.payload as { data?: unknown } | null)?.data;
    if (data && typeof data === "object") {
      const usage = (data as Record<string, unknown>).usage;
      if (usage && typeof usage === "object") sources.push(usage as Record<string, unknown>);
    }
  }
  let cost: number | null = null;
  let tokens: number | null = null;
  let input: number | null = null;
  let output: number | null = null;
  for (const source of sources) {
    const num = (key: string) => (typeof source[key] === "number" && Number.isFinite(source[key]) ? (source[key] as number) : null);
    cost = num("cost_usd") ?? num("cost") ?? cost;
    tokens = num("total_tokens") ?? num("tokens") ?? tokens;
    input = num("input_tokens") ?? num("prompt_tokens") ?? input;
    output = num("output_tokens") ?? num("completion_tokens") ?? output;
  }
  if (tokens === null && (input !== null || output !== null)) tokens = (input ?? 0) + (output ?? 0);
  return {
    cost: cost === null ? null : `$${cost.toFixed(cost < 1 ? 4 : 2)}`,
    tokens:
      tokens === null
        ? null
        : input !== null || output !== null
          ? `${tokens.toLocaleString()} (${(input ?? 0).toLocaleString()} in, ${(output ?? 0).toLocaleString()} out)`
          : tokens.toLocaleString(),
  };
}

/** A pull request link from the session's metadata, when one was recorded. */
export function pullRequestOf(session: SessionRow): string | null {
  const meta = session.meta ?? {};
  for (const key of ["pr_url", "pull_request", "pr"]) {
    const value = meta[key];
    if (typeof value === "string" && /^https?:\/\//.test(value)) return value;
    if (value && typeof value === "object") {
      const url = (value as Record<string, unknown>).url;
      if (typeof url === "string" && /^https?:\/\//.test(url)) return url;
    }
  }
  return null;
}

// ---------------------------------------------------------------- transcript

export type ChangedFile = { status: string; path: string; withheld: boolean };

export type TranscriptEntry =
  | { id: string; role: "operator"; author: string; text: string; at: string | null; note?: string }
  | { id: string; role: "agent"; author: string; text: string; at: string | null }
  | { id: string; role: "tool"; title: string; detail: string; at: string | null }
  | { id: string; role: "changes"; files: ChangedFile[]; total: number; diff: string; error: string | null; at: string | null }
  | { id: string; role: "status"; text: string; at: string | null };

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function pretty(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const CHANGE_WORD: Record<string, string> = { A: "added", D: "deleted", M: "modified", R: "renamed" };
export function changeWord(status: string): string {
  return CHANGE_WORD[status] ?? status;
}

const STATUS_TEXT: Record<string, string> = {
  session_open: "Session opening",
  session_ready: "Session ready",
  session_end: "Session ended",
  turn_start: "Turn started",
  turn_end: "Turn finished",
  turn_aborted: "Turn stopped",
  followup_dropped: "A follow-up was dropped before it was delivered",
};

function toolTitle(kind: string, data: Record<string, unknown>, toolCall: unknown): string {
  if (kind === "agent_thought_chunk") return "Thinking";
  if (kind === "plan") return "Plan";
  const call = toolCall && typeof toolCall === "object" ? (toolCall as Record<string, unknown>) : {};
  const title = str(data.title) || str(call.title) || str(call.name) || str(data.kind);
  const status = str(data.status) || str(call.status);
  const base = title ? `Tool: ${title}` : "Tool activity";
  return status ? `${base} (${status})` : base;
}

/**
 * Chat-style entries from one device item's events (oldest first), the
 * item's own prompt, and crew messages. Consecutive message chunks of one
 * turn join into one message; tool activity, thinking and plans become
 * collapsed tool entries.
 */
export function buildTranscript(input: {
  events: readonly ActivityEvent[];
  item?: Pick<NodeDispatchItem, "id" | "prompt_preview" | "created_by" | "created_at" | "agent"> | null;
  crew?: readonly CrewMessage[];
  agent: string;
}): TranscriptEntry[] {
  const entries: (TranscriptEntry & { sort: string })[] = [];
  if (input.item?.prompt_preview) {
    entries.push({
      id: `prompt-${input.item.id}`,
      role: "operator",
      author: input.item.created_by ?? "Operator",
      text: input.item.prompt_preview,
      at: input.item.created_at ?? null,
      note: "Starting prompt",
      sort: `${input.item.created_at ?? ""}#0`,
    });
  }
  let open: { entry: TranscriptEntry & { sort: string }; turn: string } | null = null;
  input.events.forEach((event, index) => {
    const payload = (event.payload ?? {}) as Record<string, unknown>;
    const sort = `${event.created_at ?? ""}#${String(event.seq ?? index).padStart(12, "0")}`;
    const at = event.created_at;
    const kind = str(payload.kind);
    const data = payload.data && typeof payload.data === "object" ? (payload.data as Record<string, unknown>) : {};
    if (kind === "followup_queued") {
      open = null;
      entries.push({
        id: event.ulid,
        role: "operator",
        author: event.originator ?? "Operator",
        text: str(payload.text) || str(payload.body),
        at,
        note: str(payload.mode) === "steer" ? "Steer" : "Follow-up",
        sort,
      });
      return;
    }
    if (kind !== "acp_event") {
      open = null;
      entries.push({ id: event.ulid, role: "status", text: STATUS_TEXT[kind] ?? kind.replace(/_/g, " "), at, sort });
      return;
    }
    const acp = str(payload.acp_kind);
    const turn = str(payload.turn_id);
    const text = str(payload.text) || str((data.content as Record<string, unknown> | undefined)?.text);
    if (acp === "agent_message_chunk" || acp === "user_message_chunk") {
      const role = acp === "agent_message_chunk" ? "agent" : "operator";
      if (open && open.entry.role === role && open.turn === turn && (open.entry.role === "agent" || open.entry.role === "operator")) {
        open.entry.text += text;
        return;
      }
      const entry =
        role === "agent"
          ? { id: event.ulid, role: "agent" as const, author: input.agent, text, at, sort }
          : { id: event.ulid, role: "operator" as const, author: "Operator", text, at, sort };
      entries.push(entry);
      open = { entry, turn };
      return;
    }
    open = null;
    if (acp === "turn_checkpoint") {
      const files = Array.isArray(data.files)
        ? data.files
            .filter((f): f is Record<string, unknown> => !!f && typeof f === "object")
            .map((f) => ({ status: String(f.status ?? "?"), path: String(f.path ?? ""), withheld: f.withheld === true }))
        : [];
      const error =
        typeof data.error === "string"
          ? data.error
          : !Array.isArray(data.files)
            ? "The change summary was too large to record."
            : null;
      entries.push({
        id: event.ulid,
        role: "changes",
        files,
        total: typeof data.files_total === "number" ? data.files_total : files.length,
        diff: typeof data.diff === "string" ? data.diff : "",
        error,
        at,
        sort,
      });
      return;
    }
    if (STATUS_TEXT[acp]) {
      entries.push({ id: event.ulid, role: "status", text: STATUS_TEXT[acp], at, sort });
      return;
    }
    const detail = [text, pretty(payload.tool_call), pretty(payload.plan), Object.keys(data).length ? pretty(data) : ""]
      .filter(Boolean)
      .join("\n\n");
    entries.push({ id: event.ulid, role: "tool", title: toolTitle(acp, data, payload.tool_call), detail, at, sort });
  });
  for (const [index, message] of (input.crew ?? []).entries()) {
    const body = str(message.body);
    if (!body) continue;
    const from = str(message.from_agent) || "Unknown";
    const at = typeof message.created_at === "string" ? message.created_at : null;
    entries.push({
      id: str(message.message_id) || `crew-${index}`,
      role: from === input.agent ? "agent" : "operator",
      author: from,
      text: body,
      at,
      sort: `${at ?? ""}#crew${String(index).padStart(6, "0")}`,
    } as TranscriptEntry & { sort: string });
  }
  entries.sort((a, b) => (a.sort < b.sort ? -1 : a.sort > b.sort ? 1 : 0));
  return entries.map(({ sort: _sort, ...entry }) => entry as TranscriptEntry);
}

/** Every file a session's turns reported changing, newest status wins. */
export function filesChanged(entries: readonly TranscriptEntry[]): ChangedFile[] {
  const byPath = new Map<string, ChangedFile>();
  for (const entry of entries) {
    if (entry.role !== "changes") continue;
    for (const file of entry.files) byPath.set(file.path, file);
  }
  return [...byPath.values()];
}

// ---------------------------------------------------------------- guardrails

export type Guardrail = { tone: "ok" | "warn" | "danger" | "unknown"; text: string };

/**
 * The guardrails line for a launch (spec §9.4; defect 3: never a constant).
 * Every clause comes from a hub read: the signed-in control state from
 * whoami, and the chosen route's profile, workspace binding, binary and
 * approval boundary from /api/dispatch-routes.
 */
export function guardrailsFor(input: {
  control: { enabled: boolean; reason: string | null } | null;
  route: DispatchRouteRow | null;
  routesState: "loading" | "error" | "ready";
  agent: string;
  hubChooses: boolean;
}): Guardrail[] {
  const out: Guardrail[] = [];
  if (input.control === null) out.push({ tone: "unknown", text: "Control state unknown: the hub has not said who is signed in yet." });
  else if (!input.control.enabled)
    out.push({ tone: "danger", text: `Starting work is turned off on this hub${input.control.reason ? `: ${input.control.reason}` : "."}` });
  else out.push({ tone: "ok", text: "Starting work is turned on for the signed-in identity." });

  if (input.hubChooses) {
    out.push({
      tone: "ok",
      text: "The hub starts only the next task whose dependencies are done, as the agent the task is assigned to, under that agent's profile.",
    });
    return out;
  }
  if (!input.agent) {
    out.push({ tone: "unknown", text: "Pick an agent to see the profile and workspace it would run under." });
    return out;
  }
  if (input.routesState === "loading") {
    out.push({ tone: "unknown", text: `Checking how ${input.agent} would run…` });
    return out;
  }
  if (input.routesState === "error") {
    out.push({ tone: "unknown", text: `Could not read how ${input.agent} would run. The hub still checks every rule when you start.` });
    return out;
  }
  const route = input.route;
  if (!route) {
    out.push({ tone: "warn", text: `${input.agent} has no launch route on the hub machine. Pick a device instead, or another agent.` });
    return out;
  }
  out.push({
    tone: "ok",
    text: `Runs under the "${route.profile}" permission profile${route.approval_boundary === "governed_dispatch" ? ", and the hub's approval rules apply" : ` (${route.approval_boundary})`}.`,
  });
  if (!route.workspace.ok) out.push({ tone: "danger", text: `No workspace is bound for this agent: ${route.workspace.reason}.` });
  else out.push({ tone: "ok", text: `Works only inside ${route.workspace.workspace ?? "its bound workspace"}.` });
  if (!route.binary_available) out.push({ tone: "warn", text: `The ${route.binary} program is not installed on the hub machine, so the agent may not start.` });
  return out;
}

/** A stable random id for idempotency keys. */
export function newKey(prefix: string): string {
  const api = (globalThis as { crypto?: Crypto }).crypto;
  if (api && typeof api.randomUUID === "function") return `${prefix}-${api.randomUUID()}`;
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}
