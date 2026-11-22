/**
 * Evidence ledger (GET /api/evidence).
 * Re-exported from `hub.ts` (task #1575).
 */
import {
  emptyEvidencePayload,
  normalizeEvidencePayload,
} from "../evidence/types";
import type { EvidenceFilter, EvidencePayload } from "../evidence/types";
export type {
  EvidenceChainIntegrity,
  EvidenceFilter,
  EvidencePayload,
  EvidenceProof,
  EvidenceRecord,
  EvidenceProjectChain,
} from "../evidence/types";

/** Build the /api/evidence query string from the pane filter (skips blanks). */
export function buildEvidenceQuery(filter: EvidenceFilter = {}): string {
  const params = new URLSearchParams();
  const entries: Array<[string, string | undefined]> = [
    ["task_id", filter.task_id],
    ["item", filter.item],
    ["pr", filter.pr],
    ["agent", filter.agent],
    ["project", filter.project],
    ["since", filter.since],
    ["until", filter.until],
  ];
  for (const [name, value] of entries) {
    const trimmed = (value ?? "").trim();
    if (trimmed) params.set(name, trimmed);
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/**
 * Load the evidence ledger via same-origin cookies (SSE/SPA path).
 *
 * A 400 (non-digit task_id) or 403 (feature-gated) response carries a JSON
 * `{error}` body — expected, not exceptional. Those return a neutral payload
 * with `error` + `status` set so the pane renders an alert / feature notice.
 * Any other non-ok status throws like the sibling helpers.
 */
export async function fetchEvidence(
  filter: EvidenceFilter = {},
  signal?: AbortSignal,
): Promise<EvidencePayload> {
  const response = await fetch(`/api/evidence${buildEvidenceQuery(filter)}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });

  if (response.status === 400 || response.status === 403) {
    let message = `evidence HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { error?: string };
      if (body && typeof body.error === "string") message = body.error;
    } catch {
      // keep the default message
    }
    return {
      ...emptyEvidencePayload(),
      error: message,
      status: response.status,
    };
  }

  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `evidence HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }

  const payload = (await response.json()) as EvidencePayload;
  return normalizeEvidencePayload(payload);
}
