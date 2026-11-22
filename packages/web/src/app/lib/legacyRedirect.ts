import { APP_BASENAME } from "./entry";

/**
 * Legacy deep links (spec §10, §11). The classic Control Center addressed
 * every view as `/ui/?view=<id>` or `/ui/#<id>`, and those links live on in
 * memory notes, journals, Slack messages, push notifications and bookmarks.
 * `src/main.tsx` rewrites them to the new path before the router starts, so
 * the first page the person sees is the one the link meant.
 */
export const LEGACY_VIEW_PATHS: Readonly<Record<string, string>> = {
  home: "/home",
  attention: "/inbox",
  approvals: "/inbox?kind=approval",
  messages: "/inbox/messages",
  proposals: "/inbox?kind=proposal",
  tasks: "/work",
  board: "/work?view=board",
  "task-graph": "/work?view=graph",
  "work-graph": "/work?view=graph&scope=work",
  goals: "/work/goals",
  plans: "/work/plans",
  canvas: "/work/canvas",
  sessions: "/sessions",
  dispatch: "/sessions/new",
  org: "/agents/org",
  "agent-topology": "/agents/topology",
  "skills-teams": "/agents/teams",
  operating: "/agents/teams?tab=operating",
  capabilities: "/agents/capabilities",
  devices: "/agents/hosts",
  memory: "/knowledge/memory",
  vault: "/knowledge/vault",
  cycles: "/runs/cycles",
  langgraph: "/runs/graphs",
  executor: "/runs/executor",
  telemetry: "/runs/telemetry",
  dispatch_receipts: "/runs/receipts",
  flow: "/runs/insights",
  doctor: "/settings/health",
  platform: "/settings/platform",
  cloud: "/settings/cloud",
  quota: "/settings/usage",
  credential_pool: "/settings/credentials",
  evidence: "/settings/audit",
  provenance: "/settings/audit/provenance",
  connectors: "/settings/integrations",
  slack_gateway: "/settings/integrations/slack",
  zulip: "/settings/integrations/zulip",
  notify_delivery: "/settings/integrations/notifications",
};

/** Older names the classic app already redirected (spec §11, "Aliases"). */
export const LEGACY_VIEW_ALIASES: Readonly<Record<string, string>> = {
  actions: "approvals",
  decisions: "approvals",
  portfolio: "home",
  overview: "home",
  queue: "tasks",
  delivery: "tasks",
  kanban: "board",
  topology: "agent-topology",
  organization: "sessions",
  knowledge: "memory",
  analytics: "flow",
  audit: "evidence",
};

/** The canonical legacy view id for `raw` (an id or an alias), or null. */
export function resolveLegacyView(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const id = raw.trim().toLowerCase();
  if (Object.hasOwn(LEGACY_VIEW_PATHS, id)) return id;
  return Object.hasOwn(LEGACY_VIEW_ALIASES, id) ? LEGACY_VIEW_ALIASES[id] : null;
}

/** An id that is safe as one path segment: no `.`, `/` or anything else to escape (spec §3). */
const PATH_ID = /^[A-Za-z0-9_-]{1,128}$/;

/**
 * Legacy parameters that became a path segment or another name. Every other
 * parameter is carried over unchanged: the new pages read the same names
 * (`task` on Insights, `task_id` and `item` on Audit, `path` on Vault).
 */
function carryParams(view: string, params: URLSearchParams): { path?: string; params: URLSearchParams } {
  const out = new URLSearchParams(params);
  out.delete("view");
  if (view === "approvals") {
    const approval = out.get("approval");
    out.delete("approval");
    if (approval && /^\d+$/.test(approval)) out.set("item", approval);
  }
  if (view === "tasks") {
    const task = out.get("task");
    if (task && /^\d+$/.test(task)) {
      out.delete("task");
      return { path: `/work/tasks/${task}`, params: out };
    }
  }
  if (view === "plans") {
    const plan = out.get("plan");
    out.delete("plan");
    if (plan && PATH_ID.test(plan)) return { path: `/work/plans/${plan}`, params: out };
  }
  return { params: out };
}

function withQuery(path: string, params: URLSearchParams): string {
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

/**
 * Where a classic URL goes in the new app, with the `/ui` basename, or null
 * when the URL is not a classic link. Only the old landing (`/ui` or `/ui/`)
 * is rewritten; a path with a segment is already a new-app route.
 *
 * - `?view=<id>` (or an alias) goes to the §11 path, keeping its other
 *   parameters; an unknown id goes to Home.
 * - `#<id>` without `?view=` does the same; any other hash (`#main-content`)
 *   is not a view and is left alone.
 */
export function legacyRedirect(location: { pathname: string; search: string; hash: string }): string | null {
  if (location.pathname !== APP_BASENAME && location.pathname !== `${APP_BASENAME}/`) return null;
  const params = new URLSearchParams(location.search);
  const rawView = params.get("view");
  let view: string | null;
  if (rawView !== null) {
    view = resolveLegacyView(rawView);
    if (!view) return `${APP_BASENAME}/home`;
  } else {
    view = resolveLegacyView(decodeHash(location.hash));
    if (!view) return null;
  }
  const target = new URL(LEGACY_VIEW_PATHS[view], "http://x");
  const carried = carryParams(view, params);
  const merged = new URLSearchParams(carried.params);
  // The target's own parameters (kind, view, scope, tab) win over carried ones.
  for (const [key, value] of target.searchParams) merged.set(key, value);
  return `${APP_BASENAME}${withQuery(carried.path ?? target.pathname, merged)}`;
}

function decodeHash(hash: string): string {
  const raw = hash.replace(/^#/, "");
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}
