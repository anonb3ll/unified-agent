/**
 * Quota lanes, delivery SLO, costs, tool allowance and credential pool.
 * Re-exported from `hub.ts` (task #1575).
 */

/** One fleet lane row from GET /api/quota (task #1136). */
export type QuotaLaneRow = {
  lane: string;
  provider?: string;
  label?: string;
  availability: string;
  remaining?: string;
  source?: string;
  observed_at?: string | null;
  last_checked?: string;
  evidence?: string | null;
  failure_class?: string | null;
  used_percent?: number | null;
  window_minutes?: number | null;
  resets_in_seconds?: number | null;
  plan_type?: string | null;
  source_path?: string | null;
};

export type QuotaPayload = {
  quotas: QuotaLaneRow[];
  checked_at?: string | null;
  failure_class_counts?: Record<string, number>;
  credential_pool_error?: string;
  error?: string;
  code?: string;
};

/**
 * One Delivery SLO metric from GET /api/delivery_slo (task #1259, Epic C
 * #1188). `value` is `null` exactly when `status === "unavailable"` — never
 * treat a missing value as zero.
 */
export type DeliverySloMetric = {
  name: string;
  value: number | null;
  numerator: number;
  denominator: number;
  window_days: number;
  status: "ok" | "breach" | "unavailable";
  detail?: string | null;
};

export type DeliverySloPayload = {
  window_days: number;
  generated_at?: string;
  dispatch_success_rate?: DeliverySloMetric;
  p90_cycle_time_hours?: DeliverySloMetric;
  unattributed_completions?: DeliverySloMetric;
  ci_green_rate?: DeliverySloMetric;
  error?: string;
  code?: string;
};

/**
 * One agent's row in GET /api/costs (task #1348). `api_cost_usd` counts only
 * priced runs; `unknown_cost_runs` and `subscription_runs` are never priced
 * at zero — they are shown as counts.
 */
export type CostGroupRow = {
  agent: string;
  runs: number;
  usage_reported: number;
  api_cost_usd: number;
  priced_runs: number;
  unknown_cost_runs: number;
  subscription_runs: number;
  local_runs: number;
  input_tokens: number;
  output_tokens: number;
  subscription_input_tokens: number;
  subscription_output_tokens: number;
  wall_s: number;
  contended_runs: number;
};

type BrokerSpendRow = {
  agent: string;
  calls: number;
  actual_usd: number;
  unreconciled_reserve_usd: number;
};

type BudgetAgentStatus = {
  agent: string;
  period_start: string;
  committed_usd: number;
  remaining_usd: number;
  api_cost_usd: number;
  broker_usd: number;
  reserved_usd: number;
  unknown_cost_runs: number;
  subscription_runs: number;
  in_flight: number;
  state: "ok" | "warning" | "exhausted";
  subscription_quota: { lane: string | null; availability: string; remaining: unknown };
};

export type BudgetRow = {
  name: string;
  agents: string[];
  period: "day" | "month";
  limit_usd: number;
  warn_pct: number;
  run_reserve_usd: number;
  action: "block" | "warn";
  agents_status: BudgetAgentStatus[];
};

export type CostsPayload = {
  rollup?: {
    since: string;
    until: string | null;
    groups: CostGroupRow[];
    broker: BrokerSpendRow[];
    totals: {
      runs: number;
      api_cost_usd: number;
      unknown_cost_runs: number;
      subscription_runs: number;
      broker_usd: number;
    };
  };
  budgets?: { configured: boolean; error?: string; budgets: BudgetRow[] };
  error?: string;
  code?: string;
};

/**
 * One row of GET /api/tool-allowance (task #2144). `used`/`remaining`/`stopped`
 * come from the same in-process rate-limit counter the policy gate enforces
 * against; `used_today` is allowed calls since 00:00 UTC from the decision
 * ledger. `limit` is null for tools with usage but no configured limit, and a
 * `pattern` row is a configured rule no matching call has hit yet.
 */
export type ToolAllowanceRow = {
  agent: string | null;
  tool: string;
  rule: string | null;
  used: number;
  /** null when the ledger cannot attribute this subject's calls (see used_today_reason). */
  used_today: number | null;
  used_today_reason?: string;
  limit: number | null;
  remaining: number | null;
  window_seconds: number | null;
  stopped: boolean;
  resets_in_seconds: number | null;
  resets_at: string | null;
  current?: boolean;
  pattern?: boolean;
  /**
   * Scoped rows: whether the current scope would still be charged. `null`
   * when the view cannot confirm a foreign identity's role scope (its live
   * session profile is not visible); `applies_reason` then says why.
   */
  applies?: boolean | null;
  applies_reason?: string;
  bind?: Record<string, string>;
  roles?: string[];
};

export type ToolAllowancePayload = {
  agent: string | null;
  as_of?: string;
  today?: string;
  configured?: boolean;
  /** Live rate-limit bucket count vs MAX_BUCKETS (task #2144). */
  capacity?: { buckets: number; max_buckets: number };
  policy_error?: string;
  ledger_error?: string;
  tools?: ToolAllowanceRow[];
  error?: string;
  code?: string;
};

/**
 * One credential-pool row from GET /api/credential_pool (task #1121).
 * Metadata only — the pool never stores or returns a secret value, and the
 * server re-projects each row to this exact field set before it leaves the
 * hub, so there is no field here for a client to accidentally render as one.
 */
export type CredentialPoolRow = {
  id: string;
  provider: string;
  label: string;
  note?: string | null;
  status: string;
  active: boolean;
  created_at: string;
  updated_at: string;
};

export type CredentialPoolPayload = {
  credentials: CredentialPoolRow[];
  count?: number;
  error?: string;
  code?: string;
};
