/**
 * Integrations fixtures, copied from the hub handlers' real shapes:
 * - `webui/api.connectors_payload` and `_horizon2_unavailable`
 *   (`tools/horizon2._server_dict` rows enriched with status, policy and
 *   last_verified; package rows from `horizon2_package_list` with manifest
 *   and secret presence);
 * - `connector_oauth.status` and `begin` (`{provider, authorize_url}`, 201);
 * - `webui/api.slack_gateway_payload` / `_slack_gateway_unavailable`;
 * - `webui/api.notify_delivery_payload` / `_notify_delivery_unavailable`
 *   over `tools/notify.notify_delivery_status`;
 * - `webui/api.zulip_coordination_payload` with
 *   `ZulipPollingWorker.health_payload` and `_unconfigured_zulip_health`.
 */

export const SERVER_GITHUB = {
  server_id: "github-main",
  connector: "github",
  provenance: "packaged:int-github@1.4.0",
  scopes: ["issues:write", "repo:read"],
  reviewed_by: "operator",
  review_ref: "task-1324",
  revoked_at: null,
  created_at: "2026-11-02T09:14:00+00:00",
  active: true,
  status: "authorized",
  policy: "one-shot approval",
  last_verified: "2026-11-02T09:14:00+00:00",
  "last-verified": "2026-11-02T09:14:00+00:00",
};

export const SERVER_LINEAR_REVOKED = {
  server_id: "linear-old",
  connector: "linear",
  provenance: "packaged:int-linear@0.3.0",
  scopes: ["issues:read"],
  reviewed_by: "operator",
  review_ref: "task-990",
  revoked_at: "2026-10-01T12:00:00+00:00",
  created_at: "2026-08-12T08:00:00+00:00",
  active: false,
  status: "revoked",
  policy: "one-shot approval",
  last_verified: null,
  "last-verified": null,
};

const PACKAGE_GITHUB = {
  package: "github",
  scopes: ["issues:write", "repo:read"],
  manifest: {
    name: "github",
    version: "1.4.0",
    capabilities: ["read repositories", "comment on issues"],
    required_secrets: ["GITHUB_APP_KEY"],
    entry_points: { client: "unified_agent.connectors.github:Client" },
    default_enabled: false,
    horizon2_registration_supported: true,
  },
  secrets: { present: ["GITHUB_APP_KEY"], missing: [], satisfied: true },
  status: "authorized",
  health: "LIVE",
  health_detail: "Horizon-2 package github: authorized",
  health_checked_at: null,
  health_cached_age_s: null,
  policy: "one-shot approval",
  last_verified: "2026-11-02T09:14:00+00:00",
  "last-verified": "2026-11-02T09:14:00+00:00",
};

const PACKAGE_GCP = {
  package: "int-gcp-agent20",
  scopes: ["compute:read"],
  manifest: {
    name: "int-gcp-agent20",
    version: "0.9.2",
    capabilities: ["list cloud servers"],
    required_secrets: ["GCP_SERVICE_ACCOUNT"],
    entry_points: { client: "unified_agent.connectors.gcp:Client" },
    default_enabled: false,
    horizon2_registration_supported: true,
  },
  secrets: { present: [], missing: ["GCP_SERVICE_ACCOUNT"], satisfied: false },
  status: "dormant",
  health: "DORMANT",
  health_detail: "GCP runtime health DORMANT: no project configured",
  health_checked_at: null,
  health_cached_age_s: null,
  policy: "one-shot approval",
  last_verified: null,
  "last-verified": null,
};

export const CONNECTORS = {
  servers: [SERVER_GITHUB, SERVER_LINEAR_REVOKED],
  packages: [PACKAGE_GITHUB, PACKAGE_GCP],
  server_count: 2,
  package_count: 2,
  doctor: {
    label: "Horizon-2 connectors",
    checks: [
      {
        package: "github",
        capability: "connector:github",
        status: "authorized",
        health: "LIVE",
        health_checked_at: null,
        health_cached_age_s: null,
        detail: "Horizon-2 package github: authorized",
        gate: "horizon2_package_list / governed_servers",
      },
    ],
  },
};

export const CONNECTORS_EMPTY = {
  servers: [],
  packages: [],
  server_count: 0,
  package_count: 0,
  doctor: { label: "Horizon-2 connectors", checks: [] },
};

/** `_horizon2_unavailable()`: HTTP 200 with a named error, not an empty catalog. */
export const CONNECTORS_UNAVAILABLE = {
  error: "horizon2 unavailable",
  code: "horizon2_unavailable",
  ...CONNECTORS_EMPTY,
};

export const OAUTH_CONNECTED = {
  provider: "github",
  status: "connected",
  scopes: ["read:user", "repo"],
  updated_at: "2026-11-09T18:02:11+00:00",
};

export const OAUTH_DISCONNECTED = { provider: "github", status: "disconnected", scopes: [], updated_at: null };

export const OAUTH_NEEDS_RECONNECT = { ...OAUTH_CONNECTED, status: "needs_reconnect" };

/** `connector_oauth.begin`: the URL carries a one-use state and PKCE challenge. */
export const OAUTH_STATE = "s3cr3t-state-value-that-must-never-appear-in-the-page-0123456789";
export const OAUTH_BEGIN = {
  provider: "github",
  authorize_url: `https://github.com/login/oauth/authorize?client_id=Iv1.abc&redirect_uri=https%3A%2F%2Fhub.example.test%2Foauth%2Fconnectors%2Fcallback&response_type=code&scope=read%3Auser+repo&state=${OAUTH_STATE}&code_challenge=xyz&code_challenge_method=S256`,
};

export const SLACK_LIVE = {
  status: "LIVE",
  reasons: [],
  enabled: true,
  channel_count: 3,
  user_allowlist_count: 2,
  signing_secret_present: true,
  bot_token_present: true,
  coordinator_agent: "Desktop_Agent5",
  recent_events: [
    { ulid: "01M7A1", channel: "slack.outbound.failed", created_at: "2026-11-10T09:30:00+00:00" },
    { ulid: "01M7A0", channel: "slack.inbound", created_at: "2026-11-10T09:29:40+00:00" },
    { ulid: "01M79Z", channel: "slack.outbound", created_at: "2026-11-10T08:00:00+00:00" },
  ],
  recent_error_count: 1,
  last_event_at: "2026-11-10T09:30:00+00:00",
  config_pointer: "UA_SLACK_ENABLED, SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, UA_SLACK_USERS, UA_SLACK_CHANNEL_IDS / UA_SLACK_CHANNEL_MAP",
};

export const SLACK_MISCONFIGURED = {
  ...SLACK_LIVE,
  status: "MISCONFIGURED",
  reasons: ["SLACK_SIGNING_SECRET is not configured", "SLACK_BOT_TOKEN is not configured (outbound replies disabled)"],
  signing_secret_present: false,
  bot_token_present: false,
  recent_events: [],
  recent_error_count: 0,
  last_event_at: null,
};

export const SLACK_DORMANT = {
  status: "DORMANT",
  reasons: ["UA_SLACK_ENABLED is not set"],
  enabled: false,
  channel_count: 0,
  user_allowlist_count: 0,
  signing_secret_present: false,
  bot_token_present: false,
  coordinator_agent: null,
  recent_events: [],
  recent_error_count: 0,
  last_event_at: null,
  config_pointer: SLACK_LIVE.config_pointer,
};

export const SLACK_UNAVAILABLE = {
  error: "slack gateway status unavailable",
  code: "slack_gateway_unavailable",
  status: "UNKNOWN",
  reasons: [],
  recent_events: [],
};

const delivery = (i: number, delivered: boolean, error: string | null = null) => ({
  agent: i % 2 ? "Desktop_Agent1" : "Mac_Codex",
  delivered,
  chars: 120 + i,
  error,
  sent_at: `2026-11-10T0${Math.min(9, i)}:00:00+00:00`,
});

export const NOTIFY = {
  deliveries: [
    delivery(9, false, "send.ts timed out after 30s"),
    delivery(8, false, "send.ts exited 1: Spectrum connection refused"),
    delivery(7, true),
    delivery(6, true),
  ],
  count: 4,
  last_delivery: delivery(9, false, "send.ts timed out after 30s"),
  success_count: 2,
  failure_count: 2,
};

/** Twenty rows: the read's cap, so counts are "of the last 20". */
export const NOTIFY_FULL = {
  deliveries: Array.from({ length: 20 }, (_, i) => delivery(i, true)),
  count: 20,
  last_delivery: delivery(0, true),
  success_count: 20,
  failure_count: 0,
};

export const NOTIFY_EMPTY = { deliveries: [], count: 0, last_delivery: null, success_count: 0, failure_count: 0 };

export const NOTIFY_UNAVAILABLE = {
  error: "notify delivery status unavailable",
  code: "notify_delivery_unavailable",
  deliveries: [],
  count: 0,
  last_delivery: null,
};

const zulipChannel = (agent: string, channel: string, status: string, extra: Record<string, unknown> = {}) => ({
  agent_label: agent,
  channel,
  status,
  last_poll_at: "2026-11-10T09:30:00+00:00",
  last_success_at: "2026-11-10T09:30:00+00:00",
  last_error: null,
  messages_processed: 42,
  consecutive_errors: 0,
  next_retry_at: null,
  updated_at: "2026-11-10T09:30:00+00:00",
  cursor: 9001,
  cursor_age_seconds: 35,
  ...extra,
});

export const ZULIP = {
  site: "https://zulip.example.test",
  worker: {
    status: "error",
    poll_interval_s: 30,
    retry_base_s: 5,
    retry_max_s: 300,
    last_successful_poll: "2026-11-10T09:30:00+00:00",
    last_error: "HTTP 401 from Zulip",
    summary: { healthy: 1, error: 1, stale: 0, not_started: 0 },
    channels: [
      zulipChannel("Desktop_Agent1", "coord", "healthy"),
      zulipChannel("Mac_Codex", "requests", "error", { last_error: "HTTP 401 from Zulip", consecutive_errors: 3, cursor_age_seconds: 4000 }),
    ],
    queues: [],
    queue_summary: { healthy: 2, error: 0, reset: 0, not_started: 0 },
    queue_error: null,
  },
  recent_inbound: [
    {
      ulid: "01M7B1",
      created_at: "2026-11-10T09:20:00+00:00",
      message_id: 5511,
      channel: "coord",
      topic: "task-2478",
      task_id: 2478,
      sender: "Pat Operator",
      content_preview: "Please check the integrations slice",
      actionable_for: ["Desktop_Agent1"],
      target_reason: "mention",
      is_bot: false,
      protocol_ok: true,
      zulip_url: "https://zulip.example.test/#narrow/stream/coord/topic/task-2478",
    },
    {
      ulid: "01M7B0",
      created_at: "2026-11-10T09:00:00+00:00",
      message_id: 5510,
      channel: "review",
      topic: "general chatter",
      task_id: null,
      sender: "ci-bot",
      content_preview: "build green",
      actionable_for: [],
      target_reason: null,
      is_bot: true,
      protocol_ok: false,
      zulip_url: "javascript:alert(1)",
    },
  ],
  stats: { inbound_24h: 2, actionable_24h: 1, protocol_total_24h: 2, protocol_compliant_24h: 1 },
};

export const ZULIP_EMPTY = {
  ...ZULIP,
  worker: { ...ZULIP.worker, status: "healthy", channels: [], summary: { healthy: 0, error: 0, stale: 0, not_started: 0 }, last_error: null },
  recent_inbound: [],
  stats: { inbound_24h: 0, actionable_24h: 0, protocol_total_24h: 0, protocol_compliant_24h: 0 },
};

/** `_unconfigured_zulip_health`: a successful read that says Zulip is not set up. */
export const ZULIP_UNCONFIGURED = {
  site: "",
  worker: {
    status: "unconfigured",
    poll_interval_s: 30,
    retry_base_s: 5,
    retry_max_s: 300,
    last_successful_poll: null,
    last_error: "UA_ZULIP_SITE is not set",
    summary: { healthy: 0, error: 0, stale: 0, not_started: 0 },
    channels: [],
    queues: [],
    queue_summary: { healthy: 0, error: 0, reset: 0, not_started: 0 },
    queue_error: null,
  },
  recent_inbound: [],
  stats: {},
  error: "UA_ZULIP_SITE is not set",
};

/** The worker answered but the inbound log read failed. */
export const ZULIP_INBOUND_FAILED = { ...ZULIP, recent_inbound: [], stats: {}, error: "database is locked" };

/** The reads every integrations page makes, keyed "METHOD /path". */
export const INTEGRATION_READS: Record<string, unknown> = {
  "GET /api/connectors": CONNECTORS,
  "GET /api/connectors/oauth/github": OAUTH_CONNECTED,
  "GET /api/slack_gateway": SLACK_LIVE,
  "GET /api/notify_delivery": NOTIFY,
  "GET /api/zulip": ZULIP,
};

export const EMPTY_READS: Record<string, unknown> = {
  "GET /api/connectors": CONNECTORS_EMPTY,
  "GET /api/connectors/oauth/github": OAUTH_DISCONNECTED,
  "GET /api/slack_gateway": SLACK_DORMANT,
  "GET /api/notify_delivery": NOTIFY_EMPTY,
  "GET /api/zulip": ZULIP_EMPTY,
};

/** A parked write the way the hub marks one (`routes._approval_pending`). */
export function parkedBody(tool: string, approvalId = 77) {
  return { code: "approval_required", approval_id: approvalId, tool, pending: true, error: `${tool} requires approval` };
}
