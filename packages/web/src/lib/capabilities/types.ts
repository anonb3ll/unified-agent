/**
 * Capability catalog types mirroring GET /api/capability-catalog (task #1066).
 * State values are passed through verbatim — never upgraded or inferred.
 */

export type ConnectionState =
  | "live"
  | "degraded"
  | "error"
  | "unknown"
  | "dormant"
  | "misconfigured"
  | "blocked"
  | "not-applicable"
  | string;

export type ImplementationState =
  | "shipped-and-used"
  | "shipped-but-unconsumed"
  | "partially-wired"
  | "dormant/uncertain"
  | "superseded"
  | "speculative"
  | "blocked"
  | string;

export type CapabilityItemState = {
  implementation_state: ImplementationState;
  connection_state: ConnectionState;
};

export type CapabilityFamily = {
  id: string;
  label: string;
  pilot: boolean;
  definition: string;
};

export type CapabilityEntryPoint = {
  kind: string;
  ref: string;
  note?: string;
};

export type CapabilityEvidence = {
  type: string;
  ref: string;
  date?: string;
};

export type CapabilityBoundaries = {
  security: string;
  identity: string;
  approval: string;
  privacy: string;
  release: string;
  tenant_workspace: string;
};

export type CapabilityIncomplete = {
  owner?: string;
  task_ref?: string;
  decision?: string;
  smallest_next_action?: string;
  non_action?: string;
};

export type CapabilityVerification = {
  classification: string;
  classification_note?: string;
  evidence_maturity: string;
  freshness: {
    latest_evidence_date: string;
    stale: boolean;
    live_evidence_date: string;
  };
  consumer_present: boolean;
  evidence_present: boolean;
  missing: string[];
  durable_path: { kind: string; ref: string; label: string }[];
  recommendation: {
    source_signal: string;
    actionable: false;
    review_only: true;
    note?: string;
  } | null;
  next_action?: string;
  owner?: string;
  security_boundary?: string;
};

export type CapabilityItem = {
  id: string;
  family: string;
  capability: string;
  customer_purpose: string;
  origin: { kind: string; refs: unknown[] };
  implementation_state: ImplementationState;
  consumer: string;
  entry_point: CapabilityEntryPoint;
  connection_state: ConnectionState;
  evidence: CapabilityEvidence[];
  boundaries: CapabilityBoundaries;
  incomplete?: CapabilityIncomplete;
  wedge?: string;
  verification?: CapabilityVerification;
};

export type CatalogMeta = {
  as_of: string;
  source_baseline_sha: string;
  origin_main_sha: string;
  live_hub_checked_at: string;
  total_items: number;
  pilot_families: string[];
  counts_by_state: Record<string, number>;
  counts_by_implementation: Record<string, number>;
  source: string;
};

export type CapabilityCatalog = {
  schema_version: string;
  families: CapabilityFamily[];
  items: CapabilityItem[];
  queue_coverage: unknown[];
  traceable_paths: unknown[];
};

export type CapabilityCatalogPayload = {
  available: boolean;
  catalog: CapabilityCatalog | null;
  /** Platform object-model manifest when the hub includes it (task #1127). */
  manifest?: {
    schema_version?: string;
    product?: string;
    object_types?: unknown[];
    [key: string]: unknown;
  } | null;
  meta?: CatalogMeta;
  error?: string;
  status?: number;
};

export type StatusTone = "ok" | "warn" | "danger" | "muted" | "muted-strike";

/** Five pilot families, fixed display order for this slice. */
export const PILOT_FAMILY_ORDER = [
  "governance-evidence",
  "dispatch-delegation",
  "langgraph-planning",
  "provider-quota",
  "first-mate",
] as const;

/** Families intentionally not surfaced in this UI slice (acceptance #10). */
export const UNSURFACED_FAMILY_IDS = [
  "workroom-multi-harness",
  "telemetry-utilization",
  "integrations-surfaces",
  "memory-vault-gbrain",
  "browser-boundary",
] as const;

export function emptyCapabilityCatalogPayload(): CapabilityCatalogPayload {
  return {
    available: false,
    catalog: null,
    meta: {
      as_of: "",
      source_baseline_sha: "",
      origin_main_sha: "",
      live_hub_checked_at: "",
      total_items: 0,
      pilot_families: [],
      counts_by_state: {},
      counts_by_implementation: {},
      source: "",
    },
    status: 0,
  };
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asBool(value: unknown): boolean {
  return value === true;
}

function normalizeFamily(raw: unknown): CapabilityFamily | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  return {
    id: asString(row.id),
    label: asString(row.label),
    pilot: asBool(row.pilot),
    definition: asString(row.definition),
  };
}

function normalizeItem(raw: unknown): CapabilityItem | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  const origin =
    row.origin && typeof row.origin === "object"
      ? (row.origin as Record<string, unknown>)
      : {};
  const entry =
    row.entry_point && typeof row.entry_point === "object"
      ? (row.entry_point as Record<string, unknown>)
      : {};
  const boundaries =
    row.boundaries && typeof row.boundaries === "object"
      ? (row.boundaries as Record<string, unknown>)
      : {};
  const evidenceRaw = Array.isArray(row.evidence) ? row.evidence : [];
  const incomplete =
    row.incomplete && typeof row.incomplete === "object"
      ? (row.incomplete as Record<string, unknown>)
      : null;

  const item: CapabilityItem = {
    id: asString(row.id),
    family: asString(row.family),
    capability: asString(row.capability),
    customer_purpose: asString(row.customer_purpose),
    origin: {
      kind: asString(origin.kind),
      refs: Array.isArray(origin.refs) ? origin.refs : [],
    },
    implementation_state: asString(row.implementation_state),
    consumer: asString(row.consumer),
    entry_point: {
      kind: asString(entry.kind),
      ref: asString(entry.ref),
      ...(asString(entry.note) ? { note: asString(entry.note) } : {}),
    },
    connection_state: asString(row.connection_state),
    evidence: evidenceRaw
      .filter((e): e is Record<string, unknown> => !!e && typeof e === "object")
      .map((e) => ({
        type: asString(e.type),
        ref: asString(e.ref),
        ...(asString(e.date) ? { date: asString(e.date) } : {}),
      })),
    boundaries: {
      security: asString(boundaries.security),
      identity: asString(boundaries.identity),
      approval: asString(boundaries.approval),
      privacy: asString(boundaries.privacy),
      release: asString(boundaries.release),
      tenant_workspace: asString(boundaries.tenant_workspace),
    },
  };

  if (incomplete) {
    const cleaned: CapabilityIncomplete = {};
    for (const key of [
      "owner",
      "task_ref",
      "decision",
      "smallest_next_action",
      "non_action",
    ] as const) {
      const value = asString(incomplete[key]);
      if (value) cleaned[key] = value;
    }
    if (Object.keys(cleaned).length) item.incomplete = cleaned;
  }

  if (typeof row.wedge === "string") item.wedge = row.wedge;

  // Defensive read of server-computed verification — never invent a classification.
  const verificationRaw =
    row.verification && typeof row.verification === "object"
      ? (row.verification as Record<string, unknown>)
      : null;
  const freshnessRaw =
    verificationRaw?.freshness && typeof verificationRaw.freshness === "object"
      ? (verificationRaw.freshness as Record<string, unknown>)
      : {};
  const durableRaw = Array.isArray(verificationRaw?.durable_path)
    ? verificationRaw!.durable_path
    : [];
  const missingRaw = Array.isArray(verificationRaw?.missing)
    ? verificationRaw!.missing
    : [];
  const recRaw =
    verificationRaw?.recommendation &&
    typeof verificationRaw.recommendation === "object"
      ? (verificationRaw.recommendation as Record<string, unknown>)
      : null;

  item.verification = {
    classification: asString(verificationRaw?.classification),
    classification_note: asString(verificationRaw?.classification_note) || undefined,
    evidence_maturity: asString(verificationRaw?.evidence_maturity),
    freshness: {
      latest_evidence_date: asString(freshnessRaw.latest_evidence_date),
      stale: asBool(freshnessRaw.stale),
      live_evidence_date: asString(freshnessRaw.live_evidence_date),
    },
    consumer_present: asBool(verificationRaw?.consumer_present),
    evidence_present: asBool(verificationRaw?.evidence_present),
    missing: missingRaw.filter((v): v is string => typeof v === "string"),
    durable_path: durableRaw
      .filter((p): p is Record<string, unknown> => !!p && typeof p === "object")
      .map((p) => ({
        kind: asString(p.kind),
        ref: asString(p.ref),
        label: asString(p.label),
      })),
    recommendation: recRaw
      ? {
          source_signal: asString(recRaw.source_signal),
          actionable: false as const,
          review_only: true as const,
          ...(asString(recRaw.note) ? { note: asString(recRaw.note) } : {}),
        }
      : null,
    next_action: asString(verificationRaw?.next_action),
    owner: asString(verificationRaw?.owner),
    security_boundary: asString(verificationRaw?.security_boundary),
  };

  return item;
}

/** Defensive normalisation — missing arrays → [], missing fields → "". */
export function normalizeCapabilityCatalogPayload(
  raw: unknown,
): CapabilityCatalogPayload {
  if (!raw || typeof raw !== "object") {
    return emptyCapabilityCatalogPayload();
  }
  const body = raw as Record<string, unknown>;
  const catalogRaw =
    body.catalog && typeof body.catalog === "object"
      ? (body.catalog as Record<string, unknown>)
      : null;
  const metaRaw =
    body.meta && typeof body.meta === "object"
      ? (body.meta as Record<string, unknown>)
      : {};

  const families = (
    catalogRaw && Array.isArray(catalogRaw.families) ? catalogRaw.families : []
  )
    .map(normalizeFamily)
    .filter((f): f is CapabilityFamily => f !== null);

  const items = (
    catalogRaw && Array.isArray(catalogRaw.items) ? catalogRaw.items : []
  )
    .map(normalizeItem)
    .filter((i): i is CapabilityItem => i !== null);

  const available = body.available === true;
  const manifestRaw =
    body.manifest && typeof body.manifest === "object"
      ? (body.manifest as Record<string, unknown>)
      : null;
  return {
    available,
    catalog: catalogRaw
      ? {
          schema_version: asString(catalogRaw.schema_version),
          families,
          items,
          queue_coverage: Array.isArray(catalogRaw.queue_coverage)
            ? catalogRaw.queue_coverage
            : [],
          traceable_paths: Array.isArray(catalogRaw.traceable_paths)
            ? catalogRaw.traceable_paths
            : [],
        }
      : null,
    manifest: manifestRaw,
    meta: {
      as_of: asString(metaRaw.as_of),
      source_baseline_sha: asString(metaRaw.source_baseline_sha),
      origin_main_sha: asString(metaRaw.origin_main_sha),
      live_hub_checked_at: asString(metaRaw.live_hub_checked_at),
      total_items:
        typeof metaRaw.total_items === "number" ? metaRaw.total_items : items.length,
      pilot_families: Array.isArray(metaRaw.pilot_families)
        ? metaRaw.pilot_families.filter((v): v is string => typeof v === "string")
        : [],
      counts_by_state:
        metaRaw.counts_by_state && typeof metaRaw.counts_by_state === "object"
          ? (metaRaw.counts_by_state as Record<string, number>)
          : {},
      counts_by_implementation:
        metaRaw.counts_by_implementation &&
        typeof metaRaw.counts_by_implementation === "object"
          ? (metaRaw.counts_by_implementation as Record<string, number>)
          : {},
      source: asString(metaRaw.source),
    },
    error: typeof body.error === "string" ? body.error : undefined,
    status: typeof body.status === "number" ? body.status : undefined,
  };
}

export function connectionTone(state: string): StatusTone {
  switch (state) {
    case "live":
      return "ok";
    case "degraded":
    case "misconfigured":
      return "warn";
    case "error":
    case "blocked":
      return "danger";
    case "dormant":
    case "unknown":
    case "not-applicable":
    default:
      return "muted";
  }
}

export function implementationTone(state: string): StatusTone {
  switch (state) {
    case "shipped-and-used":
      return "ok";
    case "shipped-but-unconsumed":
    case "partially-wired":
      return "warn";
    case "superseded":
      return "muted-strike";
    case "blocked":
      return "danger";
    case "dormant/uncertain":
    case "speculative":
    default:
      return "muted";
  }
}

export function connectionLabel(state: string): string {
  return (state || "unknown").toUpperCase();
}

export function implementationLabel(state: string): string {
  return (state || "unknown").toUpperCase();
}

export function verificationTone(classification: string): StatusTone {
  switch (classification) {
    case "connected-and-live":
      return "ok";
    case "degraded":
    case "misconfigured":
    case "awaiting-approval":
      return "warn";
    case "blocked":
    case "error":
      return "danger";
    case "superseded":
      return "muted-strike";
    case "speculative":
    case "dormant":
    case "unknown":
    case "implemented-in-source":
    case "tested":
    case "not-applicable":
    case "":
    default:
      return "muted";
  }
}

export function verificationLabel(classification: string): string {
  return (classification || "unknown").toUpperCase();
}

/** THE ONLY live signal — everything else is NOT labeled live. */
export function isUsableNow(item: CapabilityItemState): boolean {
  return (
    item.implementation_state === "shipped-and-used" &&
    item.connection_state === "live"
  );
}

export function isAttentionState(connectionState: string): boolean {
  return (
    connectionState === "unknown" ||
    connectionState === "dormant" ||
    connectionState === "misconfigured"
  );
}

/** True when meta.as_of is older than 14 days (date-only or ISO). */
export function isCatalogStale(asOf: string, now: number = Date.now()): boolean {
  if (!asOf) return false;
  const parsed = Date.parse(asOf.includes("T") ? asOf : `${asOf}T00:00:00Z`);
  if (!Number.isFinite(parsed)) return false;
  return now - parsed > 14 * 24 * 60 * 60 * 1000;
}

export type HowToUseTarget =
  | { kind: "navigate"; view: string; label: string }
  | { kind: "none"; label: string };

const TASK_REFS = new Set(["task_list", "task_add", "task_claim", "task_update"]);

/** Deterministic one-example "how can I use this?" from entry_point / consumer. */
export function howToUse(item: CapabilityItem): HowToUseTarget {
  const ref = item.entry_point?.ref ?? "";
  const consumer = (item.consumer ?? "").toLowerCase();
  const id = item.id ?? "";

  if (
    TASK_REFS.has(ref) ||
    (item.family === "dispatch-delegation" && consumer.includes("task"))
  ) {
    return { kind: "navigate", view: "tasks", label: "Open Tasks view" };
  }
  if (ref.startsWith("approval_") || consumer.includes("approval")) {
    return { kind: "navigate", view: "approvals", label: "Open Approvals view" };
  }
  if (ref.startsWith("evidence_") || id === "gov-evidence-reports") {
    return { kind: "navigate", view: "evidence", label: "Open Evidence view" };
  }
  if (ref === "system_doctor" || id.includes("doctor")) {
    return { kind: "navigate", view: "doctor", label: "Open Doctor view" };
  }
  if (consumer.includes("telemetry") || id === "prq-usage-telemetry") {
    return { kind: "navigate", view: "telemetry", label: "Open Telemetry view" };
  }
  if (id === "fm-operator-attention" || consumer.includes("attention")) {
    return { kind: "navigate", view: "home", label: "Open Home attention" };
  }
  if (item.family === "langgraph-planning") {
    return { kind: "navigate", view: "langgraph", label: "Open LangGraph view" };
  }
  if (consumer.includes("sessions") || consumer.includes("device") || id === "fm-crew-watch") {
    return { kind: "navigate", view: "sessions", label: "Open Sessions view" };
  }
  return {
    kind: "none",
    label: "No verified consumer in this UI yet — see entry_point ref",
  };
}

export function nextActionText(item: CapabilityItem): string {
  const next = item.incomplete?.smallest_next_action;
  if (next) return next;
  if (item.consumer === "no verified consumer") return "no verified consumer";
  return "—";
}

export const TONE_CLASS: Record<StatusTone, string> = {
  ok: "border-[var(--ua-accent)] text-[var(--ua-fg)]",
  warn: "border-[var(--ua-warn)] text-[var(--ua-warn)]",
  danger: "border-[var(--ua-danger)] text-[var(--ua-danger)]",
  muted: "border-[var(--ua-border)] text-[var(--ua-muted)]",
  "muted-strike":
    "border-[var(--ua-border)] text-[var(--ua-muted)] line-through",
};
