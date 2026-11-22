/**
 * Inbox fetchers that the legacy data layer does not have (spec §7: new
 * fetchers live in the area's `api.ts`). Everything else is imported
 * read-only from `src/api/*` and `src/messages/api.ts`.
 *
 * Only routes the hub serves today are called. Work proposals have no read
 * route and org proposals no decision route yet (hub task #2498); the pages
 * say so instead of calling a route that does not exist.
 */

export class HubReadError extends Error {
  readonly status: number;
  constructor(what: string, status: number, detail: string) {
    super(`${what} HTTP ${status}${detail ? `: ${detail.slice(0, 200)}` : ""}`);
    this.name = "HubReadError";
    this.status = status;
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

// --- org proposals (GET exists; the action route is task #2470) ---------------

export type OrgProposal = {
  id: string;
  title: string;
  proposal_type: string;
  author: string;
  scope: string;
  rationale: string;
  proposed_changes: unknown;
  status: "proposed" | "reviewed" | "applied" | "rejected" | string;
  approval_id?: number | null;
  created_at: string | null;
  updated_at?: string | null;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  review_notes?: string | null;
};

export const ORG_PROPOSALS_QUERY_KEY = ["cc2", "org-proposals", "open"] as const;

export function orgProposalQueryKey(id: string) {
  return ["cc2", "org-proposal", id] as const;
}

/** `list_org_proposals` caps `limit` at 200 (default 50); the Inbox asks for the most. */
export const ORG_PROPOSALS_LIMIT = 200;

export type OpenOrgProposals = { proposals: OrgProposal[]; /** A status returned a full page: more may exist. */ capped: boolean };

/** Proposals still waiting on a decision: proposed (not yet reviewed) and reviewed (not yet applied). */
/** The filters `GET /api/org-proposals` supports besides `status` (no paging: `list_org_proposals`). */
export type OrgProposalFilter = { author?: string; scope?: string };

export function orgProposalsQueryKey(filter: OrgProposalFilter = {}) {
  return [...ORG_PROPOSALS_QUERY_KEY, filter.author ?? "", filter.scope ?? ""] as const;
}

export async function fetchOpenOrgProposals(signal?: AbortSignal, filter: OrgProposalFilter = {}): Promise<OpenOrgProposals> {
  const [proposed, reviewed] = await Promise.all(
    ["proposed", "reviewed"].map((status) => {
      const qs = new URLSearchParams({ status, limit: String(ORG_PROPOSALS_LIMIT) });
      if (filter.author?.trim()) qs.set("author", filter.author.trim());
      if (filter.scope?.trim()) qs.set("scope", filter.scope.trim().toLowerCase());
      return getJson<{ proposals?: OrgProposal[] }>(`/api/org-proposals?${qs}`, "org proposals", signal);
    }),
  );
  const a = proposed.proposals ?? [];
  const b = reviewed.proposals ?? [];
  return { proposals: [...a, ...b], capped: a.length >= ORG_PROPOSALS_LIMIT || b.length >= ORG_PROPOSALS_LIMIT };
}

export async function fetchOrgProposal(id: string, signal?: AbortSignal): Promise<OrgProposal> {
  const body = await getJson<{ proposal?: OrgProposal }>(
    `/api/org-proposals/${encodeURIComponent(id)}`,
    "org proposal",
    signal,
  );
  if (!body.proposal) throw new HubReadError("org proposal", 404, "missing proposal");
  return body.proposal;
}


// --- sessions -----------------------------------------------------------------

export type SessionDetail = {
  id: string;
  agent: string;
  kind?: string | null;
  status?: string | null;
  started_at?: string | null;
  task_id?: number | null;
  task_title?: string | null;
  host?: string | null;
  branch?: string | null;
};

export function sessionDetailQueryKey(id: string) {
  return ["cc2", "session", id] as const;
}

export function fetchSessionDetail(id: string, signal?: AbortSignal): Promise<SessionDetail> {
  return getJson<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}`, "session", signal);
}

// --- write paths ----------------------------------------------------------------

export const PATHS = {
  approval: "/api/action/approval",
  task: "/api/action/task",
  memoryProposal: "/api/action/proposal",
  message: "/api/messages",
  wakeGrant: (sessionId: string) => `/api/sessions/${encodeURIComponent(sessionId)}/wake-grant`,
  handoff: (conversationId: string) => `/api/messages/conversations/${encodeURIComponent(conversationId)}/handoff`,
} as const;
