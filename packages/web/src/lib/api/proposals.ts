/**
 * Proposals and proposal actions.
 * Re-exported from `hub.ts` (task #1575).
 */

/** Read-only preview of the task file_task would create (task #1157). */
type ProposalFilingPreview = {
  task_title?: string | null;
  task_notes?: string | null;
};

/** One pending proposal from GET /api/proposals (tasks #1148/#1157). */
export type ProposalRow = {
  id: string;
  path?: string | null;
  kind?: string | null;
  title?: string | null;
  content?: string | null;
  source_date?: string | null;
  ingest_status?: string | null;
  filing_preview?: ProposalFilingPreview | null;
  error?: string | null;
  [key: string]: unknown;
};

export type ProposalsPayload = {
  proposals: ProposalRow[];
  error?: string | null;
  code?: string | null;
};

export const PROPOSALS_QUERY_KEY = ["proposals"] as const;

export async function fetchProposals(
  signal?: AbortSignal,
): Promise<ProposalsPayload> {
  const response = await fetch("/api/proposals", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `proposals HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as ProposalsPayload;
  return {
    proposals: Array.isArray(payload.proposals) ? payload.proposals : [],
    error: payload.error,
    code: payload.code,
  };
}
