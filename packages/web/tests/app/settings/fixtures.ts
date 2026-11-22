/**
 * Hub responses for the Settings area, copied from the Python handlers'
 * shapes: `api.health_payload` (system_doctor rows), `platform_http.
 * manifest_payload`, `harness_config.hub_status`, `api.cloud_payload`,
 * `api.quota_payload`, `api.costs_payload`, `monitor.delivery_slo`,
 * `_tool_allowance`, `api.credential_pool_payload`,
 * `audit.evidence_report.build_evidence_report` and
 * `platform_http.provenance_graph_payload`.
 */

export const HEALTH = {
  checks: [
    { capability: "hub_auth", status: "LIVE", detail: "bearer tokens configured for 9 agents", gate: "UA_ENFORCE_AUTH" },
    { capability: "event_bus", status: "LIVE", detail: "listening", gate: null },
    { capability: "semantic_search", status: "DEGRADED", detail: "embedding model missing; keyword search only", gate: "fastembed" },
    { capability: "workflow_definitions", status: "MISCONFIGURED", detail: "1 workflow(s) invalid: nightly: missing steps", gate: "workflows/*.yaml" },
    { capability: "zulip_chat", status: "DORMANT", detail: "UA_ZULIP_SITE unset", gate: "UA_ZULIP_SITE" },
    { capability: "ts_worker", status: "UNKNOWN", detail: "probe timed out", gate: null },
  ],
  summary: { live: 2, dormant: 1, degraded: 1, misconfigured: 1, unknown: 1 },
  cached_age_s: 42,
  cached_at: "2026-11-10T18:00:00+00:00",
  stale: false,
  refreshing: false,
  refresh_error: null,
};

export const HEALTH_EMPTY = { ...HEALTH, checks: [], summary: { live: 0, dormant: 0, degraded: 0, misconfigured: 0, unknown: 0 } };

export const MANIFEST = {
  schema_version: "1.2",
  product: "unified-agent",
  object_types: [
    {
      kind: "task",
      uri: "ua://tasks/{id}",
      title: "Task",
      description: "A unit of work in the shared queue.",
      collection: true,
      served: true,
      profile_tool: "task_list",
      lifecycle: ["open", "doing", "review", "done"],
    },
    { kind: "graph_run", uri: "ua://graph-runs/{id}", title: "Graph run", served: false, profile_tool: "langgraph_checkpoints", lifecycle: [] },
  ],
};

export const HARNESS = {
  active: { version: 7, published_at: "2026-11-09T12:00:00+00:00", published_by: "operator", bundle: { files: [] } },
  pending_proposals: 1,
  recent_proposals: [
    { id: "hp-1", node: "desktop-1", surface: "claude", path: "~/.claude/settings.json", bundle_version: 7, status: "pending", created_at: "2026-11-10T10:00:00+00:00", approval_id: 91 },
  ],
  apply_tool: "harness_config_apply",
  proposal_mode: true,
};

export const HARNESS_EMPTY = { active: null, pending_proposals: 0, recent_proposals: [], apply_tool: "harness_config_apply", proposal_mode: true };

export const CLOUD = {
  status: "degraded",
  project_id: "ua-worker-prod",
  sections: { instances: "ok", budgets: "unavailable", backups: "ok" },
  partial_coverage: true,
  instance_state: "running",
  instances: [{ name: "ua-worker-1", status: "RUNNING", zone: "us-central1-a", machineType: "e2-standard-4" }],
  budgets: { state: "unknown", items: [] },
  backups: { state: "present", detail: "1 bucket, versioning on", buckets: [{ name: "ua-backups", location: "US", versioning_enabled: true }] },
  errors: [{ section: "budgets", error: "PERMISSION_DENIED: billing.budgets.list" }],
  remediation: "manual",
  checked_at: "2026-11-10T18:00:00+00:00",
  cached_age_s: 30,
};

export const CLOUD_EMPTY = {
  ...CLOUD,
  status: "unknown",
  project_id: null,
  sections: {},
  instance_state: "unknown",
  instances: [],
  backups: { state: "unknown", detail: "", buckets: [] },
  errors: [],
  checked_at: null,
};

export const QUOTA = {
  quotas: [
    { lane: "claude", provider: "anthropic", availability: "available", used_percent: 41, window_minutes: 300, source: "codex_rollout", observed_at: "2026-11-10T17:55:00Z" },
    { lane: "codex", provider: "openai", availability: "unavailable", failure_class: "rate_limited", evidence: "429 from dispatch 812", source: "dispatch_failure" },
    { lane: "gemini", provider: "google", availability: "unknown", source: "" },
  ],
  checked_at: "2026-11-10T18:00:00Z",
  failure_class_counts: { rate_limited: 2 },
};

export const QUOTA_UNAVAILABLE = { error: "quota projection unavailable", code: "quota_projection_unavailable", quotas: [], checked_at: null, failure_class_counts: {} };

export const COSTS = {
  rollup: {
    since: "2026-11-01T00:00:00+00:00",
    until: null,
    groups: [
      {
        agent: "Desktop_Agent1",
        runs: 12,
        usage_reported: 10,
        api_cost_usd: 3.5,
        priced_runs: 8,
        unknown_cost_runs: 2,
        subscription_runs: 2,
        local_runs: 0,
        input_tokens: 1000,
        output_tokens: 500,
        subscription_input_tokens: 0,
        subscription_output_tokens: 0,
        wall_s: 3600,
        contended_runs: 1,
      },
    ],
    broker: [{ agent: "Desktop_Agent5", calls: 4, actual_usd: 0.4, unreconciled_reserve_usd: 0 }],
    totals: { runs: 12, api_cost_usd: 3.5, unknown_cost_runs: 2, subscription_runs: 2, broker_usd: 0.4 },
  },
  budgets: {
    configured: true,
    budgets: [
      {
        name: "monthly-api",
        agents: ["Desktop_Agent1"],
        period: "month",
        limit_usd: 50,
        warn_pct: 80,
        run_reserve_usd: 1,
        action: "block",
        agents_status: [
          {
            agent: "Desktop_Agent1",
            period_start: "2026-11-01",
            committed_usd: 42,
            remaining_usd: 8,
            api_cost_usd: 41,
            broker_usd: 0,
            reserved_usd: 1,
            unknown_cost_runs: 0,
            subscription_runs: 0,
            in_flight: 0,
            state: "warning",
            subscription_quota: { lane: null, availability: "unknown", remaining: null },
          },
        ],
      },
    ],
  },
};

export const COSTS_EMPTY = {
  rollup: { since: "2026-11-01T00:00:00+00:00", until: null, groups: [], broker: [], totals: { runs: 0, api_cost_usd: 0, unknown_cost_runs: 0, subscription_runs: 0, broker_usd: 0 } },
  budgets: { configured: false, budgets: [] },
};

export const SLO = {
  window_days: 7,
  generated_at: "2026-11-10T18:00:00Z",
  dispatch_success_rate: { name: "dispatch_success_rate", value: 0.92, numerator: 23, denominator: 25, window_days: 7, status: "ok" },
  p90_cycle_time_hours: { name: "p90_cycle_time_hours", value: 30.5, numerator: 0, denominator: 25, window_days: 7, status: "breach" },
  unattributed_completions: { name: "unattributed_completions", value: 0, numerator: 0, denominator: 25, window_days: 7, status: "ok" },
  ci_green_rate: { name: "ci_green_rate", value: null, numerator: 0, denominator: 0, window_days: 7, status: "unavailable", detail: "no CI runs recorded" },
};

export const ALLOWANCE = {
  agent: null,
  as_of: "2026-11-10T18:00:00Z",
  today: "2026-11-10",
  configured: true,
  capacity: { buckets: 3, max_buckets: 4096 },
  tools: [
    { agent: "Desktop_Agent1", tool: "web_fetch", rule: "web", used: 30, used_today: 31, limit: 30, remaining: 0, window_seconds: 3600, stopped: true, resets_in_seconds: 600, resets_at: "2026-11-10T18:10:00Z", current: true },
    { agent: "Desktop_Agent5", tool: "memory_save", rule: null, used: 4, used_today: 4, limit: null, remaining: null, window_seconds: null, stopped: false, resets_in_seconds: null, resets_at: null },
  ],
};

export const ALLOWANCE_EMPTY = { agent: "operator", today: "2026-11-10", configured: false, tools: [] };

export const POOL = {
  credentials: [
    { id: "cp-1", provider: "anthropic", label: "team key", note: "main", status: "registered", active: true, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" },
    { id: "cp-2", provider: "openai", label: "old key", note: null, status: "revoked", active: false, created_at: "2026-09-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z" },
  ],
  count: 2,
};

export const POOL_EMPTY = { credentials: [], count: 0 };
export const POOL_UNAVAILABLE = { error: "credential pool unavailable", code: "credential_pool_unavailable", credentials: [], count: 0 };

export const CREDENTIAL_ID = "0123456789abcdef";

function record(taskId: number, title: string) {
  return {
    task_id: taskId,
    project: "unified-agent",
    human: "operator",
    device: "desktop-1",
    agent: "Desktop_Agent1",
    action: { type: "task", title, status: "done", priority: "high", approvals: [] },
    proof: {
      evidence_ref: "https://github.com/anonb3ll/unified-agent/pull/1510",
      pr_url: "https://github.com/anonb3ll/unified-agent/pull/1510",
      commit_sha: "df9abb10aa11bb22cc33dd44ee55ff6677889900",
      merge_verified: true,
      created_at: "2026-11-09T10:00:00Z",
      updated_at: "2026-11-10T10:00:00Z",
      completed_at: "2026-11-10T10:00:00Z",
      event_hashes: [{ ulid: `01M7${taskId}AAAA`, channel: "task.done", created_at: "2026-11-10T10:00:00Z", entry_hash: "a1b2c3d4e5f60718293a4b5c6d7e8f90", prev_hash: null }],
      events_count: 1,
      tamper_evident: true,
    },
  };
}

export function evidence(records = [record(2493, "A held task's queued item is skipped")], overrides: Record<string, unknown> = {}) {
  return {
    report_type: "user_attributable_evidence",
    schema_version: "1.8",
    generated_at: "2026-11-10T18:00:00+00:00",
    chain_integrity: { status: "valid", verified: true, actor_authentication_verified: true, provenance_verified: true, head_hash: "feedface00112233", total_verified: 1520, unhashed_skipped: 3 },
    filter: { task_id: null, project_id: null, since: null, until: null, agent: null, project: null, pr: null, limit: 50 },
    total_records: records.length,
    records,
    unattributed_excluded: [12],
    project_chain: null,
    ...overrides,
  };
}

export const EVIDENCE = evidence();
export const EVIDENCE_EMPTY = evidence([], { unattributed_excluded: [], chain_integrity: { status: "empty", verified: true, head_hash: null, total_verified: 0, unhashed_skipped: 0 } });
export const EVIDENCE_TAMPERED = evidence(undefined, { chain_integrity: { status: "tampered", verified: false, head_hash: null, total_verified: 900, unhashed_skipped: 0 } });
export const manyRecords = (n: number) => evidence(Array.from({ length: n }, (_, i) => record(1000 + i, `Task ${1000 + i}`)));

export const PROVENANCE = {
  schema_version: "1.0",
  scope: { query: null },
  scope_hash: "scopehash0011",
  node_count: 4,
  edge_count: 3,
  node_counts_by_kind: { human: 1, device: 1, agent: 1, task: 1 },
  nodes: [
    { id: "human:operator", kind: "human", label: "operator", attrs: { verified: true, actor_kind: "human" } },
    { id: "device:desktop-1", kind: "device", label: "desktop-1", attrs: { verified: false } },
    { id: "agent:Desktop_Agent1", kind: "agent", label: "Desktop_Agent1", attrs: {} },
    { id: "task:2493", kind: "task", label: "2493", attrs: { title: "Held task skip", status: "done" } },
  ],
  edges: [
    { source: "human:operator", target: "device:desktop-1", relation: "used_device", attrs: {} },
    { source: "device:desktop-1", target: "agent:Desktop_Agent1", relation: "ran_agent", attrs: {} },
    { source: "agent:Desktop_Agent1", target: "task:2493", relation: "performed", attrs: {} },
  ],
  canonical_hash: "canonicalhash9988",
};

export const PROVENANCE_EMPTY = { ...PROVENANCE, node_count: 0, edge_count: 0, node_counts_by_kind: {}, nodes: [], edges: [] };

/** Every Settings read with realistic data, keyed "METHOD /path". */
export const SETTINGS_READS: Record<string, unknown> = {
  "GET /api/health": HEALTH,
  "GET /api/platform/manifest": MANIFEST,
  "GET /api/harness-config": HARNESS,
  "GET /api/cloud": CLOUD,
  "GET /api/quota": QUOTA,
  "GET /api/costs": COSTS,
  "GET /api/delivery_slo": SLO,
  "GET /api/tool-allowance": ALLOWANCE,
  "GET /api/credential_pool": POOL,
  "GET /api/evidence": EVIDENCE,
  "GET /api/platform/provenance-graph": PROVENANCE,
};

export const EMPTY_READS: Record<string, unknown> = {
  "GET /api/health": HEALTH_EMPTY,
  "GET /api/platform/manifest": { schema_version: "1.2", object_types: [] },
  "GET /api/harness-config": HARNESS_EMPTY,
  "GET /api/cloud": CLOUD_EMPTY,
  "GET /api/quota": { quotas: [], checked_at: null, failure_class_counts: {} },
  "GET /api/costs": COSTS_EMPTY,
  "GET /api/delivery_slo": { window_days: 7 },
  "GET /api/tool-allowance": ALLOWANCE_EMPTY,
  "GET /api/credential_pool": POOL_EMPTY,
  "GET /api/evidence": EVIDENCE_EMPTY,
  "GET /api/platform/provenance-graph": PROVENANCE_EMPTY,
};
