/**
 * Settings reads and writes (spec §9.7). Shapes are the hub's own: types
 * come from the legacy fetchers in `src/api/*` (imported read-only) and the
 * Python handlers in `webui/routes.py` / `webui/api.py`.
 *
 * Every fetcher here throws `SettingsReadError` for a non-2xx answer and for
 * a 200 whose body names a failure (`error` / `code`), so a failed read is
 * never shown as data (spec §5 data states).
 */
import type { HealthPayload } from "../../lib/api/health";
import type { CloudPayload } from "../../lib/api/cloud";
import type { PlatformManifestPayload } from "../../lib/api/platform";
import type {
  CostsPayload,
  CredentialPoolPayload,
  DeliverySloPayload,
  QuotaPayload,
  ToolAllowancePayload,
} from "../../lib/api/quota";
import type { EvidencePayload } from "../../lib/evidence/types";
import type { ProvenanceGraphPayload } from "../../lib/api/provenance";

export type {
  HealthPayload,
  CloudPayload,
  PlatformManifestPayload,
  CostsPayload,
  CredentialPoolPayload,
  DeliverySloPayload,
  QuotaPayload,
  ToolAllowancePayload,
  EvidencePayload,
  ProvenanceGraphPayload,
};

/** A read the hub refused or could not answer. `hubError` is the hub's own text, for the collapsed detail. */
export class SettingsReadError extends Error {
  readonly status: number | null;
  readonly hubError: string | null;
  readonly code: string | null;
  constructor(what: string, status: number | null, hubError: string | null, code: string | null = null) {
    super(`${what} ${status === null ? "failed" : `HTTP ${status}`}${hubError ? `: ${hubError}` : ""}`);
    this.name = "SettingsReadError";
    this.status = status;
    this.hubError = hubError;
    this.code = code;
  }
}

function errorOf(body: unknown): { error: string | null; code: string | null } {
  if (!body || typeof body !== "object" || Array.isArray(body)) return { error: null, code: null };
  const obj = body as Record<string, unknown>;
  const error = typeof obj.error === "string" && obj.error ? obj.error.slice(0, 500) : null;
  const code = typeof obj.code === "string" && obj.code ? obj.code : null;
  return { error, code };
}

/**
 * GET a hub JSON route. A non-2xx answer throws with the hub's message.
 * With `bodyErrorFails` (the default), a 200 that carries `error` throws too.
 */
export async function getJson<T>(
  path: string,
  what: string,
  signal?: AbortSignal,
  { bodyErrorFails = true }: { bodyErrorFails?: boolean } = {},
): Promise<T> {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  const text = await response.text().catch(() => "");
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  const { error, code } = errorOf(body);
  if (!response.ok) throw new SettingsReadError(what, response.status, error ?? (text.slice(0, 200) || null), code);
  if (body === null || typeof body !== "object") throw new SettingsReadError(what, response.status, "the hub answered with no data");
  if (bodyErrorFails && error) throw new SettingsReadError(what, response.status, error, code);
  return body as T;
}

export const SETTINGS_KEYS = {
  health: ["settings", "health"] as const,
  platform: ["settings", "platform", "manifest"] as const,
  harness: ["settings", "platform", "harness-config"] as const,
  cloud: ["settings", "cloud"] as const,
  quota: ["settings", "usage", "quota"] as const,
  costs: ["settings", "usage", "costs"] as const,
  slo: ["settings", "usage", "delivery-slo"] as const,
  allowance: ["settings", "usage", "tool-allowance"] as const,
  credentials: ["settings", "credentials"] as const,
  evidence: ["settings", "audit", "evidence"] as const,
  provenance: ["settings", "audit", "provenance"] as const,
};

// --- Health -------------------------------------------------------------------

/**
 * GET /api/health. The body may carry `error` next to real checks when the
 * latest run timed out and the hub served its last reading
 * (`api.health_payload`); that is kept as data with a notice. A body with no
 * checks array is a failed read.
 */
export async function fetchHealthReport(force: boolean, signal?: AbortSignal): Promise<HealthPayload> {
  const body = await getJson<HealthPayload>(`/api/health${force ? "?force=1" : ""}`, "health", signal, { bodyErrorFails: false });
  if (!Array.isArray(body.checks)) throw new SettingsReadError("health", 200, body.error ?? "the report had no checks");
  return {
    ...body,
    summary: body.summary && typeof body.summary === "object" ? body.summary : {},
    refresh_error: body.refresh_error && typeof body.refresh_error === "object" ? body.refresh_error : null,
  };
}

// --- Platform -----------------------------------------------------------------

export async function fetchManifest(signal?: AbortSignal): Promise<PlatformManifestPayload> {
  const body = await getJson<PlatformManifestPayload>("/api/platform/manifest", "platform manifest", signal);
  return { ...body, object_types: Array.isArray(body.object_types) ? body.object_types : [] };
}

/** `harness_config.hub_status()`: the active bundle and the latest 25 proposals. */
export type HarnessConfigStatus = {
  active: { version: string | number; published_at: string | null; published_by: string | null; bundle?: unknown } | null;
  pending_proposals: number;
  recent_proposals: {
    id: string | number;
    node: string | null;
    surface: string | null;
    path: string | null;
    bundle_version: string | number | null;
    status: string;
    created_at: string | null;
    approval_id: string | number | null;
  }[];
  apply_tool: string;
  proposal_mode: boolean;
};

/** The hub returns at most this many recent proposals (`LIMIT 25`). */
export const HARNESS_RECENT_LIMIT = 25;

export async function fetchHarnessConfig(signal?: AbortSignal): Promise<HarnessConfigStatus> {
  const body = await getJson<HarnessConfigStatus>("/api/harness-config", "harness configuration", signal);
  return { ...body, recent_proposals: Array.isArray(body.recent_proposals) ? body.recent_proposals : [] };
}

// --- Cloud --------------------------------------------------------------------

/** GET /api/cloud. Section errors stay in `errors` with `partial_coverage`; the page says so. */
export async function fetchCloudReport(signal?: AbortSignal): Promise<CloudPayload> {
  const body = await getJson<Partial<CloudPayload>>("/api/cloud", "cloud", signal);
  return {
    status: body.status ?? "unknown",
    project_id: body.project_id ?? null,
    sections: body.sections && typeof body.sections === "object" ? body.sections : {},
    partial_coverage: body.partial_coverage !== false,
    instance_state: body.instance_state ?? "unknown",
    instances: Array.isArray(body.instances) ? body.instances : [],
    budgets: body.budgets && typeof body.budgets === "object" ? { state: body.budgets.state ?? "unknown", items: body.budgets.items ?? [] } : { state: "unknown", items: [] },
    backups:
      body.backups && typeof body.backups === "object"
        ? { state: body.backups.state ?? "unknown", detail: body.backups.detail ?? "", buckets: Array.isArray(body.backups.buckets) ? body.backups.buckets : [] }
        : { state: "unknown", detail: "", buckets: [] },
    errors: Array.isArray(body.errors) ? body.errors : [],
    remediation: body.remediation ?? "manual",
    checked_at: body.checked_at ?? null,
    cached_age_s: typeof body.cached_age_s === "number" ? body.cached_age_s : 0,
  };
}

// --- Usage --------------------------------------------------------------------

export async function fetchQuotaReport(signal?: AbortSignal): Promise<QuotaPayload> {
  const body = await getJson<QuotaPayload>("/api/quota", "quota", signal);
  return { ...body, quotas: Array.isArray(body.quotas) ? body.quotas : [] };
}

export async function fetchCostReport(signal?: AbortSignal): Promise<CostsPayload> {
  return getJson<CostsPayload>("/api/costs", "costs", signal);
}

export async function fetchSloReport(signal?: AbortSignal): Promise<DeliverySloPayload> {
  return getJson<DeliverySloPayload>("/api/delivery_slo", "delivery targets", signal);
}

/**
 * GET /api/tool-allowance. `policy_error` means the limits could not be read,
 * so it fails the read; `ledger_error` only makes "calls today" unknown.
 */
export async function fetchAllowance(signal?: AbortSignal): Promise<ToolAllowancePayload> {
  const body = await getJson<ToolAllowancePayload>("/api/tool-allowance", "tool allowance", signal);
  if (body.policy_error) throw new SettingsReadError("tool allowance", 200, body.policy_error);
  return { ...body, tools: Array.isArray(body.tools) ? body.tools : [] };
}

// --- Credentials --------------------------------------------------------------

export async function fetchCredentialPool(signal?: AbortSignal): Promise<CredentialPoolPayload> {
  const body = await getJson<CredentialPoolPayload>("/api/credential_pool", "credential pool", signal);
  return { ...body, credentials: Array.isArray(body.credentials) ? body.credentials : [] };
}

/** `POST /api/action/session-credential-revoke`: exactly one selector (`routes.action_session_credential_revoke`). */
export const REVOKE_PATH = "/api/action/session-credential-revoke";
export type RevokeSelector = "credential_id" | "item_id" | "node";
export type RevokeBody = Partial<Record<RevokeSelector, string>>;
export type RevokeResult = { revoked: string[]; count: number };

/** The hub's own patterns (`session_credentials._CREDENTIAL_ID_RE`, `_ITEM_RE`, `node_registry._NODE_RE`). */
export const SELECTOR_PATTERNS: Record<RevokeSelector, RegExp> = {
  credential_id: /^[0-9a-f]{16}$/,
  item_id: /^nd-[0-9a-f]{16}$/,
  node: /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/,
};

// --- Audit --------------------------------------------------------------------

export type EvidenceFilters = {
  task_id: string;
  item: string;
  pr: string;
  agent: string;
  project: string;
  since: string;
  until: string;
  limit: string;
};

export const EMPTY_EVIDENCE_FILTERS: EvidenceFilters = { task_id: "", item: "", pr: "", agent: "", project: "", since: "", until: "", limit: "" };

/** Default and largest `limit` the hub accepts (`evidence_report._clamp_limit`: default 50, capped at 500). */
export const EVIDENCE_DEFAULT_LIMIT = 50;
export const EVIDENCE_MAX_LIMIT = 500;

/** The query string for `/api/evidence`, with `format` for an export. Blank filters are left out. */
export function evidenceQuery(filters: EvidenceFilters, format?: "pdf" | "csv"): string {
  const params = new URLSearchParams();
  for (const key of ["task_id", "item", "pr", "agent", "project", "since", "until", "limit"] as const) {
    const value = filters[key].trim();
    if (value) params.set(key, value);
  }
  if (format) params.set("format", format);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export async function fetchEvidenceReport(filters: EvidenceFilters, signal?: AbortSignal): Promise<EvidencePayload> {
  const body = await getJson<EvidencePayload>(`/api/evidence${evidenceQuery(filters)}`, "evidence", signal);
  const ci = body.chain_integrity;
  return {
    ...body,
    chain_integrity: {
      status: ci?.status ?? "unknown",
      verified: Boolean(ci?.verified),
      head_hash: ci?.head_hash ?? null,
      total_verified: typeof ci?.total_verified === "number" ? ci.total_verified : 0,
      unhashed_skipped: typeof ci?.unhashed_skipped === "number" ? ci.unhashed_skipped : 0,
    },
    records: Array.isArray(body.records) ? body.records : [],
    total_records: typeof body.total_records === "number" ? body.total_records : Array.isArray(body.records) ? body.records.length : 0,
    unattributed_excluded: Array.isArray(body.unattributed_excluded) ? body.unattributed_excluded : [],
    project_chain: body.project_chain ?? null,
  };
}

/** What a finished export download gives the page: a file to save, or a plain reason it failed. */
export type ExportResult = { ok: true; blob: Blob; filename: string } | { ok: false; status: number | null; message: string; detail: string | null };

/** Download the evidence report as PDF or CSV from the hub route, with the active filters. */
export async function downloadEvidence(filters: EvidenceFilters, format: "pdf" | "csv", signal?: AbortSignal): Promise<ExportResult> {
  let response: Response;
  try {
    response = await fetch(`/api/evidence${evidenceQuery(filters, format)}`, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: format === "pdf" ? "application/pdf" : "text/csv" },
      signal,
    });
  } catch (error) {
    return { ok: false, status: null, message: "The hub could not be reached. Check your connection and try again.", detail: error instanceof Error ? error.message : String(error) };
  }
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    let hubError: string | null = null;
    try {
      hubError = errorOf(JSON.parse(text)).error;
    } catch {
      hubError = text.slice(0, 200) || null;
    }
    const message =
      response.status === 422 || response.status === 503
        ? format === "pdf"
          ? "The hub cannot make a PDF right now. Export as CSV instead, or ask the operator to check the hub's PDF support."
          : "The hub cannot make this export right now. Try again in a moment."
        : response.status === 403
          ? "The signed-in identity may not export the evidence ledger, or this hub's edition does not include it."
          : response.status === 400
            ? "The hub did not accept these filters. Check them and try again."
            : "The hub had a problem making the export. Try again in a moment.";
    return { ok: false, status: response.status, message, detail: hubError ?? `HTTP ${response.status}` };
  }
  let blob: Blob;
  try {
    blob = await response.blob();
  } catch (error) {
    // The connection failed while the file was downloading.
    return { ok: false, status: null, message: "The hub could not be reached. Check your connection and try again.", detail: error instanceof Error ? error.message : String(error) };
  }
  const disposition = response.headers.get("content-disposition") ?? "";
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return { ok: true, blob, filename: match?.[1] ?? `evidence-report.${format}` };
}

export type ProvenanceFilters = {
  query: "" | "by_human" | "by_release";
  human: string;
  since: string;
  until: string;
  task_id: string;
  task_ids: string;
  project_id: string;
  agent: string;
  pr: string;
  limit: string;
};

export const EMPTY_PROVENANCE_FILTERS: ProvenanceFilters = {
  query: "",
  human: "",
  since: "",
  until: "",
  task_id: "",
  task_ids: "",
  project_id: "",
  agent: "",
  pr: "",
  limit: "",
};

export function provenanceQuery(filters: ProvenanceFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    const trimmed = String(value ?? "").trim();
    if (trimmed) params.set(key, trimmed);
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export async function fetchProvenance(filters: ProvenanceFilters, signal?: AbortSignal): Promise<ProvenanceGraphPayload> {
  const body = await getJson<ProvenanceGraphPayload>(`/api/platform/provenance-graph${provenanceQuery(filters)}`, "provenance graph", signal);
  return {
    ...body,
    nodes: Array.isArray(body.nodes) ? body.nodes : [],
    edges: Array.isArray(body.edges) ? body.edges : [],
    node_counts_by_kind: body.node_counts_by_kind && typeof body.node_counts_by_kind === "object" ? body.node_counts_by_kind : {},
  };
}
