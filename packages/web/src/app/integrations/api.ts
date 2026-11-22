/**
 * Integrations area reads, write paths and the pure rules the pages share
 * (spec §7: new fetchers live in the area, §9.8).
 *
 * Response shapes follow the hub handlers, not invention:
 * - `GET /api/connectors`: `webui/api.connectors_payload` (servers, packages,
 *   doctor checks). A failure is HTTP 200 with `error: "horizon2 unavailable"`
 *   and `code: "horizon2_unavailable"` (`_horizon2_unavailable`).
 * - `GET /api/connectors/oauth/{provider}`: `connector_oauth.status`
 *   (`provider`, `status`, `scopes`, `updated_at`); 403 when the signed-in
 *   identity is not a human operator.
 * - `POST /api/connectors/oauth/{provider}/begin`: 201 with
 *   `{provider, authorize_url}` (`connector_oauth.begin`); 400 `{error}`.
 * - `POST /api/connectors/oauth/{provider}/disconnect`: 200 with the status.
 * - `GET /api/slack_gateway`: `webui/api.slack_gateway_payload`, failure
 *   `code: "slack_gateway_unavailable"`.
 * - `GET /api/notify_delivery`: `webui/api.notify_delivery_payload`, failure
 *   `code: "notify_delivery_unavailable"`.
 * - `GET /api/zulip`: `webui/api.zulip_coordination_payload`; an unconfigured
 *   site answers `worker.status: "unconfigured"` with `error` set to the
 *   configuration problem, a failed inbound read keeps the worker and sets
 *   `error`.
 *
 * Every read keeps its HTTP status (`IntegrationReadError`) so the page can
 * explain it, and a 200 that carries the hub's named `error` is a failed read.
 */
import type {
  ConnectorDoctorCheck,
  ConnectorPackageRow,
  ConnectorServerRow,
  NotifyDeliveryRow,
  SlackGatewayEventRow,
  ZulipChannelRow,
  ZulipInboundRow,
} from "../../lib/hub";

// --- Reads -------------------------------------------------------------------

/**
 * A failed read. `status` is the HTTP status, or 200 when the hub answered
 * with its named "unavailable" error, or null when nothing came back.
 * `hubError` is the hub's `error` text, for the collapsed detail only.
 */
export class IntegrationReadError extends Error {
  readonly status: number | null;
  readonly hubError: string | null;
  constructor(what: string, status: number | null, hubError: string | null) {
    super(`${what}${status === null ? "" : ` HTTP ${status}`}${hubError ? `: ${hubError.slice(0, 200)}` : ""}`);
    this.name = "IntegrationReadError";
    this.status = status;
    this.hubError = hubError;
  }
}

type JsonObject = Record<string, unknown>;

function asObject(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : null;
}

function errorField(body: unknown): string | null {
  const obj = asObject(body);
  return obj && typeof obj.error === "string" && obj.error ? obj.error : null;
}

async function getJson(path: string, what: string, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  const text = await response.text().catch(() => "");
  let body: unknown = null;
  try {
    body = text ? (JSON.parse(text) as unknown) : null;
  } catch {
    body = null;
  }
  if (!response.ok) throw new IntegrationReadError(what, response.status, errorField(body) ?? (text.slice(0, 200) || null));
  if (asObject(body) === null) throw new IntegrationReadError(what, response.status, "the answer was not readable");
  return body;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function objects<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value.filter((v) => asObject(v) !== null) as T[]) : [];
}

/** Plain-language explanation of a failed read; the raw text goes to `detail` (spec §5, §12 defect 10). */
export function describeIntegrationReadError(error: unknown, what: string): { message: string; detail: string | null } {
  const detail = error instanceof Error ? error.message : error ? String(error) : null;
  const status = error instanceof IntegrationReadError ? error.status : null;
  if (status === 401) return { message: "Your session has ended. Sign in again to see this.", detail };
  if (status === 403) return { message: `The signed-in identity may not see ${what}.`, detail };
  if (status === 404) return { message: `This hub does not offer ${what}.`, detail };
  if (status === 200) return { message: `The hub answered, but could not read ${what}. Try again in a moment.`, detail };
  if (status !== null && status >= 500) return { message: "The hub had a problem answering. Try again in a moment.", detail };
  if (status !== null) return { message: `The hub refused to show ${what}.`, detail };
  return { message: "The hub could not be reached. Check your connection and try again.", detail };
}

export const INTEGRATION_KEYS = {
  all: ["integrations"] as const,
  inventory: ["integrations", "connectors"] as const,
  oauth: (provider: OAuthProvider) => ["integrations", "oauth", provider] as const,
  slack: ["integrations", "slack"] as const,
  zulip: ["integrations", "zulip"] as const,
  notify: ["integrations", "notify"] as const,
};

/** A calm poll for reads with no event on the stream (spec: changes made elsewhere must appear). */
export const CALM_POLL_MS = 60_000;

// --- Connector inventory (Horizon-2) -------------------------------------------

export type ConnectorInventory = {
  servers: ConnectorServerRow[];
  packages: ConnectorPackageRow[];
  checks: ConnectorDoctorCheck[];
};

export async function fetchConnectorInventory(signal?: AbortSignal): Promise<ConnectorInventory> {
  const body = asObject(await getJson("/api/connectors", "connectors", signal)) as JsonObject;
  // `_horizon2_unavailable`: HTTP 200 with the named error. Never an empty catalog.
  if (errorField(body) || body.code === "horizon2_unavailable") {
    throw new IntegrationReadError("connectors", 200, errorField(body) ?? "horizon2 unavailable");
  }
  const doctor = asObject(body.doctor);
  return {
    servers: objects<ConnectorServerRow>(body.servers),
    packages: objects<ConnectorPackageRow>(body.packages),
    checks: objects<ConnectorDoctorCheck>(doctor?.checks),
  };
}

/** Lifecycle of a connector server or package, in plain words (`_connector_lifecycle_status`). */
export function lifecycleText(status: string): { label: string; tone: "ok" | "warn" | "off"; explain: string } {
  switch (status) {
    case "authorized":
      return { label: "Working", tone: "ok", explain: "Registered, reviewed and connected. Agents can use it." };
    case "registered":
      return {
        label: "Needs attention",
        tone: "warn",
        explain: "Registered, but not connected or its review has lapsed. Agents cannot use it until it is authorized again.",
      };
    case "revoked":
      return { label: "Revoked", tone: "off", explain: "Its registration was revoked. Agents cannot use it." };
    case "dormant":
      return { label: "Not set up", tone: "off", explain: "Packaged with the hub but not registered. Nothing uses it." };
    default:
      return { label: status || "Unknown", tone: "warn", explain: "The hub reported a status this page does not know." };
  }
}

/** Secrets of a package, by name and presence only (the hub never returns values). */
export function secretsText(secrets: ConnectorPackageRow["secrets"]): string | null {
  if (!secrets) return null;
  if (secrets.error) return "Could not check its credentials";
  if (secrets.satisfied) return "Credentials present";
  return `Missing credentials: ${secrets.missing.join(", ")}`;
}

// --- Connected accounts (OAuth) ----------------------------------------------

export type OAuthProvider = "github";

export const OAUTH_PROVIDERS: readonly {
  id: OAuthProvider;
  name: string;
  /** The only host the hub's authorize URL may send the person to (`connectors.GITHUB_OAUTH`). */
  host: string;
  /** What stops working when it is disconnected. */
  uses: string;
}[] = [
  {
    id: "github",
    name: "GitHub",
    host: "github.com",
    uses: "Connectors and agents that read or write GitHub through the hub's connection",
  },
];

export type OAuthConnection = {
  provider: OAuthProvider;
  status: "connected" | "needs_reconnect" | "disconnected" | string;
  scopes: string[];
  updatedAt: string | null;
};

export async function fetchOAuthConnection(provider: OAuthProvider, signal?: AbortSignal): Promise<OAuthConnection> {
  const body = asObject(await getJson(`/api/connectors/oauth/${provider}`, `the ${provider} connection`, signal)) as JsonObject;
  return {
    provider,
    status: typeof body.status === "string" ? body.status : "unknown",
    scopes: strings(body.scopes),
    updatedAt: typeof body.updated_at === "string" ? body.updated_at : null,
  };
}

export function connectionText(status: string): { label: string; tone: "ok" | "warn" | "off"; explain: string } {
  switch (status) {
    case "connected":
      return { label: "Connected", tone: "ok", explain: "The hub holds a working grant. Agents can use it through the hub." };
    case "needs_reconnect":
      return {
        label: "Needs reconnecting",
        tone: "warn",
        explain: "The grant stopped working (it expired, was revoked at the provider, or a change did not finish). Reconnect to restore it.",
      };
    case "disconnected":
      return { label: "Not connected", tone: "off", explain: "No grant is stored. Connect to let agents use it through the hub." };
    default:
      return { label: "Unknown", tone: "warn", explain: "The hub reported a status this page does not know." };
  }
}

export const oauthPaths = {
  begin: (provider: OAuthProvider) => `/api/connectors/oauth/${provider}/begin`,
  disconnect: (provider: OAuthProvider) => `/api/connectors/oauth/${provider}/disconnect`,
};

/**
 * Where an OAuth begin may send the person (open-redirect guard). Navigate
 * only to an `https:` URL on the provider's own host, or to the hub's own
 * origin. Anything else is refused and nothing is opened.
 */
export function checkAuthorizeUrl(
  raw: unknown,
  expectedHost: string,
  hubOrigin: string,
): { ok: true; url: string; host: string } | { ok: false; reason: string } {
  if (typeof raw !== "string" || !raw) return { ok: false, reason: "The hub did not return a sign-in address." };
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { ok: false, reason: "The hub returned a sign-in address that is not a valid web address." };
  }
  if (url.username || url.password) return { ok: false, reason: "The sign-in address carries a user name or password." };
  if (url.origin === hubOrigin) return { ok: true, url: url.href, host: url.host };
  if (url.protocol !== "https:") return { ok: false, reason: "The sign-in address is not a secure (https) address." };
  if (url.hostname.toLowerCase() !== expectedHost) {
    return { ok: false, reason: `The sign-in address points to ${url.hostname}, not ${expectedHost} as expected.` };
  }
  return { ok: true, url: url.href, host: url.host };
}

// --- Connector server and package administration (hub task #2470) -------------

/**
 * `POST /api/action/connector/register | register-package | authorize |
 * revoke` (`webui/cc_agents.py`, docs/control-center-api.md "Connectors").
 * Each adapts a registered Horizon-2 tool that forces its own one-shot
 * approval: the first call parks (202) and the identical request after a
 * different person approves it runs once. Register and register-package are
 * bound to the caller (the tool records the caller as `reviewed_by`), so only
 * the same identity can run the approved pass; authorize and revoke are not.
 * Every error answer carries a `code`.
 */
export const CONNECTOR_ADMIN_PATHS = {
  register: "/api/action/connector/register",
  registerPackage: "/api/action/connector/register-package",
  authorize: "/api/action/connector/authorize",
  revoke: "/api/action/connector/revoke",
} as const;

export type RegisterForm = {
  kind: "server" | "package";
  serverId: string;
  connector: string;
  packageName: string;
  provenance: string;
  scopes: string;
  reviewRef: string;
};

export const EMPTY_REGISTER: RegisterForm = {
  kind: "package",
  serverId: "",
  connector: "",
  packageName: "",
  provenance: "",
  scopes: "",
  reviewRef: "",
};

export type AuthorizeForm = {
  serverId: string;
  scope: string;
  taskId: string;
  traceId: string;
  connectionRef: string;
};

export const EMPTY_AUTHORIZE: AuthorizeForm = { serverId: "", scope: "", taskId: "", traceId: "", connectionRef: "" };

/** Split "a, b c" into scope names. */
export function parseScopes(raw: string): string[] {
  return [...new Set(raw.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean))];
}

export type FieldErrors<K extends string> = Partial<Record<K, string>>;

const ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/;

export function validateRegister(form: RegisterForm): FieldErrors<keyof RegisterForm> {
  const errors: FieldErrors<keyof RegisterForm> = {};
  if (form.kind === "server") {
    if (!form.serverId.trim()) errors.serverId = "Enter a server id.";
    else if (!ID_RE.test(form.serverId.trim())) errors.serverId = "Use letters, numbers, dots, dashes, colons or underscores.";
    if (!form.connector.trim()) errors.connector = "Enter the connector this server runs.";
    if (parseScopes(form.scopes).length === 0) errors.scopes = "Enter at least one scope.";
  } else {
    if (!form.packageName.trim()) errors.packageName = "Choose the package to register.";
    if (form.serverId.trim() && !ID_RE.test(form.serverId.trim())) {
      errors.serverId = "Use letters, numbers, dots, dashes, colons or underscores.";
    }
  }
  if (!form.provenance.trim()) errors.provenance = "Say where this connector comes from.";
  if (!form.reviewRef.trim()) errors.reviewRef = "Enter the review that approved it.";
  return errors;
}

/**
 * What a registration registers, as the attempt and result key's object: the
 * kind and the identifiers the hub keys it by (the server id; for a package,
 * the package and the optional server id it defaults from). Two different
 * targets never share an unknown outcome or a result.
 */
export function registerTarget(form: RegisterForm): string {
  return form.kind === "server"
    ? JSON.stringify(["server", form.serverId.trim()])
    : JSON.stringify(["package", form.packageName.trim(), form.serverId.trim()]);
}

/** The route a registration goes to: a server, or a packaged connector. */
export function registerPath(form: RegisterForm): string {
  return form.kind === "server" ? CONNECTOR_ADMIN_PATHS.register : CONNECTOR_ADMIN_PATHS.registerPackage;
}

/**
 * The request body: `horizon2_server_register` or `horizon2_package_register`
 * parameters, exactly the route's fields (the hub refuses an unknown one).
 */
export function registerBody(form: RegisterForm): Record<string, unknown> {
  if (form.kind === "server") {
    return {
      server_id: form.serverId.trim(),
      connector: form.connector.trim(),
      provenance: form.provenance.trim(),
      scopes: parseScopes(form.scopes),
      review_ref: form.reviewRef.trim(),
    };
  }
  const scopes = parseScopes(form.scopes);
  return {
    package: form.packageName.trim(),
    provenance: form.provenance.trim(),
    review_ref: form.reviewRef.trim(),
    ...(form.serverId.trim() ? { server_id: form.serverId.trim() } : {}),
    ...(scopes.length ? { scopes } : {}),
  };
}

export function validateAuthorize(form: AuthorizeForm): FieldErrors<keyof AuthorizeForm> {
  const errors: FieldErrors<keyof AuthorizeForm> = {};
  if (!form.serverId.trim()) errors.serverId = "Choose the server to authorize.";
  if (!form.scope.trim()) errors.scope = "Enter the scope to authorize.";
  const task = Number(form.taskId.trim());
  if (!form.taskId.trim()) errors.taskId = "Enter the task this use belongs to.";
  else if (!Number.isInteger(task) || task < 1) errors.taskId = "Enter a task number, such as 2478.";
  const trace = form.traceId.trim();
  if (!trace) errors.traceId = "Enter a trace id so the use can be followed.";
  else if (trace.length > 128) errors.traceId = "Use at most 128 characters.";
  return errors;
}

/** The request body: `horizon2_connector_authorize` parameters. */
export function authorizeBody(form: AuthorizeForm): Record<string, unknown> {
  return {
    server_id: form.serverId.trim(),
    scope: form.scope.trim(),
    task_id: Number(form.taskId.trim()),
    trace_id: form.traceId.trim(),
    ...(form.connectionRef.trim() ? { connection_ref: form.connectionRef.trim() } : {}),
  };
}

/** The request body: `horizon2_server_revoke` parameters. */
export function revokeBody(serverId: string): Record<string, unknown> {
  return { server_id: serverId };
}

/**
 * A connector write the hub refused, in plain words, by the `code` every
 * error answer carries (docs/control-center-api.md, "agents"). `object` names
 * the server or connector; `fallback` is the hub's own message.
 */
export function connectorErrorText(code: string | undefined, status: number | null, object: string, fallback: string): string {
  switch (code) {
    case "already_registered":
      return `${object} is already registered, so nothing was changed. Use a different server id, or authorize the one that is there.`;
    case "not_found":
      return `${object} is not registered on this hub (it may have been removed). The list on this page refreshes; check it before trying again.`;
    case "operator_required":
      return "Only the hub's human operator may do this. Sign in as the operator to continue.";
    case "policy_denied":
      return `A policy rule on the hub refused this, so nothing was changed. ${fallback}`;
    case "agent_access_denied":
      return "Agent access is switched off for this action on the hub, so nothing was changed.";
    case "approval_denied":
      return "Its approval was denied, so nothing was changed. Ask for a new approval by sending it again.";
    case "rate_limited":
      return "The hub is limiting how often this can be done. Wait a moment, then try again.";
    case "invalid_request":
      return `The hub refused the details, so nothing was changed: ${fallback}`;
    case "conflict":
      return `It changed on the hub since this page read it, so nothing was changed. Check the list, then try again.`;
  }
  if (status === 404) return "This hub does not offer this action yet. Ask the operator to update the hub.";
  if (status === 403) return "The signed-in identity may not change connectors.";
  return fallback;
}

// --- Slack ---------------------------------------------------------------------

export type SlackStatus = {
  status: "LIVE" | "DORMANT" | "MISCONFIGURED" | "UNKNOWN";
  reasons: string[];
  enabled: boolean | null;
  channelCount: number | null;
  userCount: number | null;
  signingSecretPresent: boolean | null;
  botTokenPresent: boolean | null;
  coordinator: string | null;
  events: SlackGatewayEventRow[];
  lastEventAt: string | null;
  configPointer: string | null;
};

/** `slack_gateway_payload` reads at most this many recent events (`event_log_query limit=20`). */
export const SLACK_EVENT_CAP = 20;

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

export async function fetchSlackStatus(signal?: AbortSignal): Promise<SlackStatus> {
  const body = asObject(await getJson("/api/slack_gateway", "the Slack status", signal)) as JsonObject;
  if (errorField(body) || body.code === "slack_gateway_unavailable") {
    throw new IntegrationReadError("the Slack status", 200, errorField(body) ?? "slack gateway status unavailable");
  }
  const status = body.status;
  return {
    status: status === "LIVE" || status === "DORMANT" || status === "MISCONFIGURED" ? status : "UNKNOWN",
    reasons: strings(body.reasons),
    enabled: bool(body.enabled),
    channelCount: num(body.channel_count),
    userCount: num(body.user_allowlist_count),
    signingSecretPresent: bool(body.signing_secret_present),
    botTokenPresent: bool(body.bot_token_present),
    coordinator: str(body.coordinator_agent),
    events: objects<SlackGatewayEventRow>(body.recent_events),
    lastEventAt: str(body.last_event_at),
    configPointer: str(body.config_pointer),
  };
}

export function slackStatusText(status: SlackStatus["status"]): { label: string; tone: "ok" | "warn" | "off"; explain: string } {
  switch (status) {
    case "LIVE":
      return { label: "Working", tone: "ok", explain: "The hub accepts messages from the allowed Slack channels and can reply." };
    case "MISCONFIGURED":
      return { label: "Set-up incomplete", tone: "warn", explain: "Slack is turned on, but something it needs is missing. Fix the items below." };
    case "DORMANT":
      return { label: "Off", tone: "off", explain: "Slack is not turned on for this hub, so nothing from Slack reaches agents." };
    default:
      return { label: "Unknown", tone: "warn", explain: "The hub could not tell whether Slack is working." };
  }
}

/** What to do about each reason `_slack_gateway_status` can give. Unknown reasons are shown as the hub wrote them. */
export function slackReasonAdvice(reason: string): string {
  if (reason.startsWith("UA_SLACK_ENABLED")) return "Slack is off. To turn it on, set UA_SLACK_ENABLED=1 in the hub's environment and restart the hub.";
  if (reason.startsWith("SLACK_SIGNING_SECRET")) {
    return "The signing secret is missing, so the hub cannot check that messages really come from Slack. Add SLACK_SIGNING_SECRET to the hub's environment and restart the hub.";
  }
  if (reason.startsWith("UA_SLACK_USERS")) return "No Slack users are allowed. List the allowed Slack user ids in UA_SLACK_USERS and restart the hub.";
  if (reason.startsWith("UA_SLACK_CHANNEL_IDS")) {
    return "No Slack channels are allowed. List them in UA_SLACK_CHANNEL_IDS (or UA_SLACK_CHANNEL_MAP) and restart the hub.";
  }
  if (reason.startsWith("SLACK_BOT_TOKEN")) {
    return "The bot token is missing, so the hub can read messages but cannot reply. Add SLACK_BOT_TOKEN to the hub's environment and restart the hub.";
  }
  return reason;
}

/** The evidence-log channels `slack_gateway_payload` lists, in plain words. */
export function slackEventText(channel: string | null): { label: string; failed: boolean } {
  switch (channel) {
    case "slack.inbound":
      return { label: "Message received", failed: false };
    case "slack.dispatch.failed":
      return { label: "Could not hand a message to an agent", failed: true };
    case "slack.outbound":
      return { label: "Reply sent", failed: false };
    case "slack.outbound.failed":
      return { label: "Reply failed", failed: true };
    default:
      return { label: channel ?? "Unknown event", failed: Boolean(channel?.endsWith(".failed")) };
  }
}

// --- Notifications ---------------------------------------------------------------

export type NotifyStatus = {
  deliveries: NotifyDeliveryRow[];
  last: NotifyDeliveryRow | null;
};

/** `notify_delivery_payload` asks `notify_delivery_status(limit=20)`. */
export const NOTIFY_CAP = 20;

export async function fetchNotifyStatus(signal?: AbortSignal): Promise<NotifyStatus> {
  const body = asObject(await getJson("/api/notify_delivery", "notification delivery", signal)) as JsonObject;
  if (errorField(body) || body.code === "notify_delivery_unavailable") {
    throw new IntegrationReadError("notification delivery", 200, errorField(body) ?? "notify delivery status unavailable");
  }
  const deliveries = objects<NotifyDeliveryRow>(body.deliveries);
  const last = asObject(body.last_delivery) as NotifyDeliveryRow | null;
  return { deliveries, last: last ?? deliveries[0] ?? null };
}

/**
 * Counts from the rows actually returned, never the hub's totals alone: the
 * read is capped at `NOTIFY_CAP` rows, so these are "of the last N".
 */
export function notifyCounts(status: NotifyStatus): { delivered: number; failed: number; total: number; capped: boolean } {
  const delivered = status.deliveries.filter((d) => d.delivered).length;
  return {
    delivered,
    failed: status.deliveries.length - delivered,
    total: status.deliveries.length,
    capped: status.deliveries.length >= NOTIFY_CAP,
  };
}

/** Failures in a row at the top of the list (newest first). */
export function failingStreak(deliveries: readonly NotifyDeliveryRow[]): number {
  let n = 0;
  for (const row of deliveries) {
    if (row.delivered) break;
    n += 1;
  }
  return n;
}

// --- Zulip ---------------------------------------------------------------------

export type ZulipWorker = {
  status: string;
  lastSuccessfulPoll: string | null;
  lastError: string | null;
  pollIntervalS: number | null;
  summary: Record<string, number>;
  queueSummary: Record<string, number>;
  queueError: string | null;
  channels: (ZulipChannelRow & { last_error?: string | null; consecutive_errors?: number | null })[];
};

export type ZulipStatus = {
  site: string | null;
  worker: ZulipWorker;
  /** Set when the Zulip site is not configured (`worker.status === "unconfigured"`). */
  configurationError: string | null;
  /** The inbound read; `null` with `inboundError` set when it failed. */
  inbound: ZulipInboundRow[] | null;
  inboundError: string | null;
  stats: { inbound24h: number | null; actionable24h: number | null; protocolTotal24h: number | null; protocolCompliant24h: number | null };
};

/** `zulip_coordination_payload` returns at most this many inbound rows (`ZULIP_INBOUND_UI_LIMIT`). */
export const ZULIP_INBOUND_CAP = 30;

function counts(value: unknown): Record<string, number> {
  const obj = asObject(value);
  if (!obj) return {};
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(obj)) if (typeof v === "number") out[k] = v;
  return out;
}

export async function fetchZulipStatus(signal?: AbortSignal): Promise<ZulipStatus> {
  const body = asObject(await getJson("/api/zulip", "the Zulip status", signal)) as JsonObject;
  const worker = asObject(body.worker) ?? {};
  const workerStatus = typeof worker.status === "string" ? worker.status : "unknown";
  const error = errorField(body);
  const unconfigured = workerStatus === "unconfigured";
  const stats = asObject(body.stats) ?? {};
  return {
    site: str(body.site),
    worker: {
      status: workerStatus,
      lastSuccessfulPoll: str(worker.last_successful_poll),
      lastError: str(worker.last_error),
      pollIntervalS: num(worker.poll_interval_s),
      summary: counts(worker.summary),
      queueSummary: counts(worker.queue_summary),
      queueError: str(worker.queue_error),
      channels: objects(worker.channels),
    },
    configurationError: unconfigured ? (error ?? str(worker.last_error) ?? "Zulip is not configured") : null,
    inbound: error ? null : objects<ZulipInboundRow>(body.recent_inbound),
    inboundError: error,
    stats: {
      inbound24h: num(stats.inbound_24h),
      actionable24h: num(stats.actionable_24h),
      protocolTotal24h: num(stats.protocol_total_24h),
      protocolCompliant24h: num(stats.protocol_compliant_24h),
    },
  };
}

/** The worker's overall status (`ZulipPollingWorker.health_payload`), in plain words. */
export function zulipStatusText(status: string): { label: string; tone: "ok" | "warn" | "off"; explain: string } {
  switch (status) {
    case "healthy":
      return { label: "Working", tone: "ok", explain: "The hub is reading the Zulip channels on schedule." };
    case "stale":
      return {
        label: "Behind",
        tone: "warn",
        explain: "The hub has not read some channels for longer than expected. Check that the hub's Zulip worker is running.",
      };
    case "error":
      return {
        label: "Failing",
        tone: "warn",
        explain: "Reading from Zulip is failing. Check the errors below; a wrong bot key or an unreachable Zulip site are the usual causes.",
      };
    case "not_started":
      return { label: "Not started", tone: "off", explain: "Zulip is set up, but the hub's Zulip worker has not read anything yet. Start the worker on the hub." };
    case "unconfigured":
      return { label: "Not set up", tone: "off", explain: "No Zulip site is configured on this hub, so nothing is read from Zulip." };
    case "retired":
      return { label: "Retired", tone: "off", explain: "The Zulip worker was stopped on purpose on this host." };
    default:
      return { label: "Unknown", tone: "warn", explain: "The hub reported a status this page does not know." };
  }
}

export function channelStatusText(status: string | null | undefined): string {
  switch (status) {
    case "healthy":
      return "Working";
    case "stale":
      return "Behind";
    case "error":
      return "Failing";
    case "not_started":
      return "Not started";
    default:
      return status || "Unknown";
  }
}

/** A seconds count as "45 s", "12 min", "3 h", "2 d". */
export function seconds(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "–";
  const s = Math.max(0, Math.round(value));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

/** A hub timestamp as a local date and time, or the text as given when it does not parse. */
export function when(value: string | null | undefined): string {
  if (!value) return "–";
  const t = Date.parse(value);
  if (Number.isNaN(t)) return value;
  return new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** An external link the page may render: `https:` only, no credentials in it. */
export function safeExternalUrl(raw: unknown): string | null {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" || url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}
