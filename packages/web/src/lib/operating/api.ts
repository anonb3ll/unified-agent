/** A profile, its own settings, and its settings with `extends` applied. */
type OperatingProfile = {
  description: string;
  extends: string | null;
  settings: Record<string, unknown>;
  effective: Record<string, unknown>;
};

type OperatingOffice = {
  id: string;
  name: string;
  lead: string;
  members: string[];
  /** Profile bound to this office in policy, or null (org default applies). */
  profile: string | null;
};

type OperatingPolicyEdit = {
  tool: string;
  action: string;
  kind: string;
  block: string;
  history_route: string;
};

export type OperatingView = {
  configured: boolean;
  default: string | null;
  units: Record<string, string>;
  profiles: Record<string, OperatingProfile>;
  fields: string[];
  enforced_fields: string[];
  offices: OperatingOffice[];
  policy_edit?: OperatingPolicyEdit;
  error?: string;
  code?: string;
};

export type OperatingSprint = {
  id: number;
  name: string;
  goal_id: number | null;
  org_unit: string | null;
  profile: string | null;
  overrides: Record<string, unknown>;
  status: "planned" | "active" | "closed";
  starts_at: string | null;
  ends_at: string | null;
  created_by: string;
  task_ids: number[];
};

export type OperatingSprintsPayload = {
  sprints: OperatingSprint[];
  statuses: string[];
};

export type OperatingResolved = {
  configured: boolean;
  task_id: number | null;
  agent: string | null;
  sprint_id: number | null;
  unit: string | null;
  settings: Record<string, unknown>;
  /** Field (or `field.key` for mapping fields) -> layer label that set it. */
  sources: Record<string, string>;
  layers: { layer: string; settings: Record<string, unknown> }[];
  enforcement: Record<string, "enforced" | "recorded">;
  fingerprint: string;
  /** The task's stored run layer (what `set` replaces), or null when none is set. */
  run_settings?: {
    profile: string | null;
    overrides: Record<string, unknown>;
    set_by: string;
    set_at: string;
  } | null;
};
export const SPRINT_STATUSES = ["planned", "active", "closed"] as const;
/** Fields whose value is a mapping; they merge key by key across layers. */
const MAPPING_FIELDS = new Set(["comms", "delivery", "budget"]);

export type SprintAction =
  | {
      op: "add";
      name: string;
      profile?: string;
      org_unit?: string;
      overrides?: Record<string, unknown>;
      starts_at?: string;
      ends_at?: string;
    }
  | {
      op: "update";
      sprint_id: number;
      name?: string;
      status?: string;
      profile?: string | null;
      org_unit?: string | null;
      overrides?: Record<string, unknown>;
    }
  | { op: "link" | "unlink"; sprint_id: number; task_id: number };

export type RunAction =
  | { op: "set"; task_id: number; profile?: string; overrides?: Record<string, unknown> }
  | { op: "clear"; task_id: number };

/** Parse an overrides textarea. Empty is `{}`; anything but a JSON object throws. */
export function parseOverrides(text: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  let value: unknown;
  try {
    value = JSON.parse(trimmed);
  } catch {
    throw new Error("Overrides must be valid JSON");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Overrides must be a JSON object");
  }
  return value as Record<string, unknown>;
}

/**
 * The `operating_profiles` policy block, rebuilt from the view, as the
 * starting point for an edit committed through the policy revision path.
 */
export function policyBlockDraft(view: OperatingView): Record<string, unknown> {
  const profiles: Record<string, Record<string, unknown>> = {};
  for (const [name, profile] of Object.entries(view.profiles)) {
    const entry: Record<string, unknown> = {};
    if (profile.description) entry.description = profile.description;
    if (profile.extends) entry.extends = profile.extends;
    Object.assign(entry, profile.settings);
    profiles[name] = entry;
  }
  const block: Record<string, unknown> = { profiles };
  if (view.default) block.default = view.default;
  if (Object.keys(view.units).length) block.units = { ...view.units };
  return block;
}

/** One row per resolved value: mapping fields expand to `field.key`. */
export type ResolvedRow = {
  key: string;
  field: string;
  value: unknown;
  source: string | undefined;
  enforcement: "enforced" | "recorded";
};

export function resolvedRows(resolved: OperatingResolved): ResolvedRow[] {
  const rows: ResolvedRow[] = [];
  for (const [field, value] of Object.entries(resolved.settings)) {
    const enforcement = resolved.enforcement[field] ?? "recorded";
    if (MAPPING_FIELDS.has(field) && value && typeof value === "object" && !Array.isArray(value)) {
      for (const [sub, subValue] of Object.entries(value as Record<string, unknown>)) {
        const key = `${field}.${sub}`;
        rows.push({ key, field, value: subValue, source: resolved.sources[key], enforcement });
      }
    } else {
      rows.push({ key: field, field, value, source: resolved.sources[field], enforcement });
    }
  }
  return rows;
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
