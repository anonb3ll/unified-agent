/**
 * Evidence Ledger types mirroring GET /api/evidence (audit/evidence_report.build_evidence_report).
 * Traces the 5-link accountability chain: human → device → agent → action → proof.
 */

export type EvidenceChainIntegrity = {
  status: string;
  verified: boolean;
  head_hash: string | null;
  total_verified: number;
  unhashed_skipped: number;
};

export type EvidenceApproval = {
  id: number;
  tool: string | null;
  status: string | null;
  resolved_by: string | null;
  reason: string | null;
};

export type EvidenceAction = {
  type: string;
  title: string;
  status: string;
  priority: string;
  lane?: string;
  parent_id?: number | null;
  project_depth?: number | null;
  approvals: EvidenceApproval[];
};

export type EvidenceEventHash = {
  ulid: string;
  channel: string;
  created_at: string | null;
  entry_hash: string;
  prev_hash: string | null;
};

export type EvidenceProof = {
  evidence_ref: string | null;
  pr_url: string | null;
  commit_sha: string | null;
  merge_verified: boolean;
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  event_hashes: EvidenceEventHash[];
  events_count: number;
  tamper_evident: boolean;
};

export type EvidenceRecord = {
  task_id: number;
  project: string;
  human: string;
  device: string;
  agent: string;
  action: EvidenceAction;
  proof: EvidenceProof;
};

export type EvidenceProjectChainEntry = {
  task_id: number;
  evidence_ref: string | null;
  pr_url: string | null;
  commit_sha: string | null;
  merge_verified: boolean;
  completed_at: string | null;
};

export type EvidenceProjectStep = {
  task_id: number;
  title: string;
  status: string;
  project_depth: number | null;
  proof: EvidenceProof;
};

export type EvidenceProjectChain = {
  project_id: number;
  status: "complete" | "partial" | string;
  task_ids: number[];
  done_task_ids: number[];
  evidence: EvidenceProjectChainEntry[];
  merged_prs: EvidenceProjectChainEntry[];
  last_merge: EvidenceProjectChainEntry | null;
  steps: EvidenceProjectStep[];
};

export type EvidenceFilterEcho = {
  item?: string;
  task_id: number | null;
  project_id: number | null;
  since: string | null;
  until: string | null;
  agent: string | null;
  project: string | null;
  pr: string | null;
  limit: number;
};

export type EvidencePayload = {
  report_type?: string;
  schema_version?: string;
  generated_at?: string;
  chain_integrity: EvidenceChainIntegrity;
  filter?: EvidenceFilterEcho;
  total_records: number;
  records: EvidenceRecord[];
  unattributed_excluded: number[];
  project_chain: EvidenceProjectChain | null;
  /**
   * Present when the hub returns 400 (non-digit task_id) or 403 (feature-gated).
   * Both are expected, non-exceptional responses the pane renders inline.
   */
  error?: string;
  /** HTTP status attached by fetchEvidence so the pane can tell 400 from 403. */
  status?: number;
};

/** Draft/applied filter for the pane form (all string-typed inputs). */
export type EvidenceFilter = {
  item?: string;
  task_id?: string;
  pr?: string;
  agent?: string;
  project?: string;
  since?: string;
  until?: string;
};

export const EMPTY_EVIDENCE_FILTER: EvidenceFilter = {
  item: "",
  task_id: "",
  pr: "",
  agent: "",
  project: "",
  since: "",
  until: "",
};

/** Neutral payload used for the loading placeholder and for 400/403 responses. */
export function emptyEvidencePayload(): EvidencePayload {
  return {
    chain_integrity: {
      status: "unknown",
      verified: false,
      head_hash: null,
      total_verified: 0,
      unhashed_skipped: 0,
    },
    total_records: 0,
    records: [],
    unattributed_excluded: [],
    project_chain: null,
  };
}

/** Defensive normalisation of a 200 payload (arrays may be missing on older hubs). */
export function normalizeEvidencePayload(payload: EvidencePayload): EvidencePayload {
  const ci = payload.chain_integrity ?? emptyEvidencePayload().chain_integrity;
  return {
    ...payload,
    chain_integrity: {
      status: ci.status ?? "unknown",
      verified: Boolean(ci.verified),
      head_hash: ci.head_hash ?? null,
      total_verified: typeof ci.total_verified === "number" ? ci.total_verified : 0,
      unhashed_skipped: typeof ci.unhashed_skipped === "number" ? ci.unhashed_skipped : 0,
    },
    total_records:
      typeof payload.total_records === "number"
        ? payload.total_records
        : Array.isArray(payload.records)
          ? payload.records.length
          : 0,
    records: Array.isArray(payload.records) ? payload.records : [],
    unattributed_excluded: Array.isArray(payload.unattributed_excluded)
      ? payload.unattributed_excluded
      : [],
    project_chain: payload.project_chain ?? null,
  };
}

/** Truncated hash for dense table cells; full value belongs in a `title`. */
export function shortHash(hash: string | null | undefined, len = 12): string {
  if (!hash) return "—";
  if (hash.length <= len) return hash;
  return `${hash.slice(0, len)}…`;
}

/** Short commit sha (7 chars, git default). */
export function shortSha(sha: string | null | undefined): string {
  if (!sha) return "—";
  return sha.slice(0, 7);
}

/** Relative age from an ISO timestamp string. */
export function formatAge(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "—";
  const s = Math.floor((now - then) / 1000);
  if (s < 0) return "—";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d`;
}

export type ChainBadgeTone = "ok" | "warn";

/** Verified / unverified badge label + tone from chain_integrity. */
export function chainBadge(integrity: EvidenceChainIntegrity): {
  label: string;
  tone: ChainBadgeTone;
} {
  if (integrity.verified) {
    return { label: `verified (${integrity.status})`, tone: "ok" };
  }
  return { label: `unverified (${integrity.status})`, tone: "warn" };
}

/** Tamper-evident indicator for a proof cell. */
export function tamperIndicator(proof: EvidenceProof): {
  label: string;
  tone: ChainBadgeTone;
} {
  return proof.tamper_evident
    ? { label: "tamper-evident ✓", tone: "ok" }
    : { label: "not verified", tone: "warn" };
}

/** True when any of the human/device/agent links is unresolved. */
export function isUnattributed(record: EvidenceRecord): boolean {
  return [record.human, record.device, record.agent].some(
    (value) => !value || value === "unattributed" || value === "—",
  );
}
