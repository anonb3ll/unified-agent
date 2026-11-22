/**
 * Typed HTTP client for the Web Control Center API.
 *
 * This is a view adapter, not a second task or policy implementation. The
 * Python hub remains authoritative for queue classification, authentication,
 * policy, and mutations. This client validates request and response contracts.
 */

import { z } from "zod";

import {
  ApprovalRequest as ApprovalRequestSchema,
  ApprovalRequestResult,
  ApprovalResolveRequest as ApprovalResolveRequestSchema,
  ApprovalResolveResult,
  EvidenceCreateRequest as EvidenceCreateRequestSchema,
  EvidenceCreateOutcome,
  Task,
  TaskCreateRequest as TaskCreateRequestSchema,
  TaskUpdateRequest as TaskUpdateRequestSchema,
  TaskWriteOutcome,
  WorkProductReleaseRequest as WorkProductReleaseRequestSchema,
  WorkProductSubmitRequest as WorkProductSubmitRequestSchema,
  WorkProductWriteOutcome,
  type ApprovalRequest,
  type ApprovalResolveRequest,
  type EvidenceCreateRequest,
  type TaskCreateRequest,
  type TaskUpdateRequest,
  type WorkProductSubmitRequest,
} from "@unified-agent/protocol";

import { HubAuthError, HubTransportError, HubValidationError } from "./errors.js";
import type { FetchLike } from "./hub.js";

const PlatformObjectRef = z.object({
  kind: z.string().min(1),
  id: z.string().min(1),
  uri: z.string().min(1),
});

export type PlatformObjectRef = z.infer<typeof PlatformObjectRef>;

const QueueHealthTask = Task.pick({
  id: true,
  title: true,
  project: true,
  claimed_by: true,
  claimed_at: true,
  lock_expires_at: true,
  completed_at: true,
})
  // The queue-health endpoint deliberately preserves unknown future statuses
  // in `other`; the domain Task schema remains strict for task operations.
  .extend({
    status: z.string().min(1),
    priority: z.enum(["high", "med", "low"]).optional(),
    lane: z.string().nullable().optional(),
    created_at: z.string().optional(),
    urgency: z.enum(["now", "next", "later"]).nullable().optional(),
    urgency_rank: z.number().int().min(0).max(2).optional(),
    laptop_lane: z.boolean().optional(),
    notes_archived: z.boolean().optional(),
    waiting_for_review: z.boolean().optional(),
    platform: PlatformObjectRef.optional(),
    links: z
      .object({
        agent: PlatformObjectRef.nullable().optional(),
        run: PlatformObjectRef.nullable().optional(),
        approvals: z.array(PlatformObjectRef).optional(),
      })
      .passthrough()
      .optional(),
  })
  .passthrough();

export type QueueHealthTask = z.infer<typeof QueueHealthTask>;

/** Accept the previous wire key for one release, returning only the canonical key. */
function operatorBucketAlias(value: unknown): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  const record = value as Record<string, unknown>;
  if (record.operator_only !== undefined || record.legacy_operator_only === undefined) return value;
  return { ...record, operator_only: record.legacy_operator_only };
}

const QueueHealthCounts = z.preprocess(operatorBucketAlias, z.object({
  desktop_open: z.number().int().nonnegative(),
  dispatch_ready: z.number().int().nonnegative().default(0),
  interactive_only: z.number().int().nonnegative().default(0),
  operator_only: z.number().int().nonnegative().default(0),
  doing: z.number().int().nonnegative(),
  waiting_review: z.number().int().nonnegative(),
  stale_claims: z.number().int().nonnegative(),
  laptop_lane: z.number().int().nonnegative(),
  blocked_archived: z.number().int().nonnegative(),
  inconsistent: z.number().int().nonnegative(),
  other: z.number().int().nonnegative(),
  total_active: z.number().int().nonnegative(),
}));

export type QueueHealthCounts = z.infer<typeof QueueHealthCounts>;

export const QueueHealth = z.preprocess(operatorBucketAlias, z.object({
  desktop_open: z.array(QueueHealthTask),
  dispatch_ready: z.array(QueueHealthTask).default([]),
  interactive_only: z.array(QueueHealthTask).default([]),
  operator_only: z.array(QueueHealthTask).default([]),
  doing: z.array(QueueHealthTask),
  waiting_review: z.array(QueueHealthTask),
  stale_claims: z.array(QueueHealthTask),
  laptop_lane: z.array(QueueHealthTask),
  blocked_archived: z.array(QueueHealthTask),
  inconsistent: z.array(QueueHealthTask),
  other: z.array(QueueHealthTask),
  counts: QueueHealthCounts,
  dispatch_wip: z.object({
    cap: z.number().int().positive(),
    in_flight: z.number().int().nonnegative().nullable(),
    error: z.string().optional(),
  }).optional(),
  error: z.string().optional(),
}));

export type QueueHealth = z.infer<typeof QueueHealth>;

const NodeRow = z
  .object({
    node: z.string().min(1),
    agent: z.string().nullable().optional(),
    device_node: z.string().nullable().optional(),
    version: z.string().nullable().optional(),
    registered_at: z.string().nullable().optional(),
    last_heartbeat: z.string().nullable().optional(),
    os: z.string().nullable().optional(),
    backends: z.array(z.string()),
    harnesses: z.record(z.string(), z.unknown()).optional().default({}),
    worktree_roots: z.array(z.string()).optional().default([]),
  })
  .passthrough();

const NodeDispatchItem = z
  .object({
    id: z.string().min(1),
    node: z.string().min(1),
    agent: z.string().min(1),
    backend: z.string().min(1),
    cwd: z.string(),
    status: z.string().min(1),
    evidence_ref: z.string().min(1),
    prompt_preview: z.string().optional(),
    cancel_requested: z.boolean().optional(),
    outcome_unknown: z.boolean().optional(),
    restartable: z.boolean().optional(),
    task_id: z.number().int().nullable().optional(),
    run_id: z.string().nullable().optional(),
    session_id: z.string().nullable().optional(),
  })
  .passthrough();

export const NodesPayload = z.object({
  nodes: z.array(NodeRow),
  recent_items: z.array(NodeDispatchItem).optional().default([]),
  error: z.string().optional(),
});

export type NodesPayload = z.infer<typeof NodesPayload>;

export const SessionsListPayload = z.object({
  sessions: z.array(z.record(z.string(), z.unknown())),
  open: z.number().int().nonnegative().optional(),
  total: z.number().int().nonnegative().optional(),
  error: z.string().optional(),
});

export type SessionsListPayload = z.infer<typeof SessionsListPayload>;

const RunLink = z
  .object({
    kind: z.string().min(1),
    id: z.string().min(1),
    uri: z.string().min(1),
  })
  .passthrough();

const RunLinks = z
  .object({
    self: RunLink.nullable(),
    task: RunLink.nullable(),
    delegation: RunLink.nullable().optional(),
    dispatch_run: RunLink.nullable().optional(),
    evidence: z.array(RunLink),
  })
  .passthrough();

/** One uniform run row. Kind-specific fields pass through. */
export const Run = z
  .object({
    run_id: z.string().min(1),
    kind: z.enum(["dispatch", "selfpickup", "session"]),
    task_id: z.number().int().nullable(),
    agent: z.string().nullable(),
    status: z.string().min(1),
    outcome: z.string().nullable(),
    started_at: z.string().nullable(),
    ended_at: z.string().nullable(),
    lease_expires_at: z.string().nullable(),
    abandoned: z.boolean(),
    abandon_reason: z.string().nullable(),
    links: RunLinks,
  })
  .passthrough();

export type Run = z.infer<typeof Run>;

/** `GET /api/runs` page. `next_cursor` is null on the last page. */
export const RunsPage = z.object({
  runs: z.array(Run),
  next_cursor: z.string().nullable(),
  count: z.number().int().nonnegative(),
  truncated: z.boolean(),
  limit: z.number().int().positive(),
});

export type RunsPage = z.infer<typeof RunsPage>;

const RunDetail = z.object({
  run: Run,
});

const RunListQuery = z
  .object({
    kind: z.enum(["dispatch", "selfpickup", "session"]).optional(),
    agent: z.string().min(1).optional(),
    task_id: z.number().int().positive().optional(),
    limit: z.number().int().positive().optional(),
    cursor: z.string().min(1).optional(),
  })
  .strict();

export type RunListQuery = z.infer<typeof RunListQuery>;

const NodeSessionStartRequest = z.object({
  action: z.literal("start").optional(),
  node: z.string().min(1),
  prompt: z.string().min(1),
  cwd: z.string().min(1),
  backend: z.string().min(1),
  model: z.string().nullable().optional(),
  timeout_s: z.number().int().positive().nullable().optional(),
  task_id: z.number().int().positive().nullable().optional(),
  run_id: z.string().nullable().optional(),
});

const NodeSessionCancelRequest = z.object({
  action: z.literal("cancel"),
  item_id: z.string().min(1),
});

export const NodeSessionOutcome = z
  .object({
    item_id: z.string().optional(),
    node: z.string().optional(),
    status: z.string().optional(),
    created_at: z.string().optional(),
    approval_id: z.number().int().optional(),
    tool: z.string().optional(),
    error: z.string().optional(),
  })
  .passthrough();

export type NodeSessionOutcome = z.infer<typeof NodeSessionOutcome>;

export const NodeSessionCancelOutcome = z
  .object({
    item_id: z.string(),
    cancel_requested: z.boolean(),
  })
  .passthrough();

export type NodeSessionCancelOutcome = z.infer<typeof NodeSessionCancelOutcome>;

const PlatformObjectType = z
  .object({
    kind: z.string().min(1),
    uri: z.string().min(1),
    name: z.string().min(1),
    title: z.string().min(1),
    description: z.string(),
    mime_type: z.string().min(1),
    collection: z.boolean(),
    served: z.boolean(),
    table: z.string().min(1),
    id_param: z.string().nullable(),
    id_pattern: z.string().nullable(),
    profile_tool: z.string().min(1),
    lifecycle: z.array(z.string()),
    terminal_states: z.array(z.string()),
    event_kinds: z.array(z.string()),
  })
  .passthrough();

export const PlatformManifest = z
  .object({
    schema_version: z.string().min(1),
    product: z.string().min(1),
    object_types: z.array(PlatformObjectType),
  })
  .passthrough();
export type PlatformManifest = z.infer<typeof PlatformManifest>;

const CatalogEntry = z
  .object({
    id: z.string().min(1),
    name: z.string().optional(),
    description: z.string().optional(),
    consumer: z.string().optional(),
    connection_state: z.string().optional(),
    implementation_state: z.string().optional(),
    entry_point: z
      .object({ kind: z.string(), ref: z.string(), note: z.string().optional() })
      .optional(),
    evidence: z
      .array(z.object({ type: z.string(), ref: z.string(), date: z.string().optional() }))
      .optional(),
  })
  .passthrough();
const CapabilityCatalogPayload = z
  .object({
    schema_version: z.string(),
    families: z.array(CatalogEntry),
    items: z.array(CatalogEntry),
    queue_coverage: z.array(z.unknown()),
    traceable_paths: z.array(z.unknown()),
  })
  .passthrough();
export const CapabilityCatalog = z
  .object({
    available: z.boolean(),
    catalog: CapabilityCatalogPayload.nullable(),
    /** Platform object-model manifest (task #1127) — machine-readable object discovery. */
    manifest: PlatformManifest.nullable().optional(),
    meta: z
      .object({
        as_of: z.string(),
        source_baseline_sha: z.string(),
        origin_main_sha: z.string(),
        live_hub_checked_at: z.string(),
        total_items: z.number().int().nonnegative(),
        pilot_families: z.array(z.string()),
        counts_by_state: z.record(z.string(), z.number().int().nonnegative()),
        counts_by_implementation: z.record(z.string(), z.number().int().nonnegative()),
        source: z.string(),
      })
      .optional(),
    error: z.string().optional(),
    status: z.number().int(),
  })
  .passthrough();
export type CapabilityCatalog = z.infer<typeof CapabilityCatalog>;

function encodePlatformPathSegment(value: string, label: string): string {
  if (
    !value.trim() ||
    value === "." ||
    value === ".." ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new HubValidationError(`platform object ${label} is invalid`);
  }
  return encodeURIComponent(value);
}

export type ControlCenterClientOptions = {
  /** Hub origin, for example ``http://127.0.0.1:8765``. */
  baseUrl: string;
  /** The same bearer credential used for authenticated hub requests. */
  bearerToken: string;
  /** Optional override for tests. Defaults to global ``fetch``. */
  fetch?: FetchLike;
};

export type {
  ApprovalRequest,
  ApprovalResolveRequest,
  EvidenceCreateRequest,
  TaskCreateRequest,
  TaskUpdateRequest,
  WorkProductSubmitRequest,
};

export class ControlCenterClient {
  readonly baseUrl: string;
  private readonly bearerToken: string;
  private readonly fetchImpl: FetchLike;

  constructor(options: ControlCenterClientOptions) {
    if (!options.baseUrl?.trim()) {
      throw new HubTransportError("control center base URL is required");
    }
    if (!options.bearerToken?.trim()) {
      throw new HubAuthError(0, "control center bearer token is required");
    }
    this.baseUrl = options.baseUrl.replace(/\/$/, "");
    this.bearerToken = options.bearerToken;
    this.fetchImpl = options.fetch ?? (globalThis.fetch as FetchLike);
  }

  /** Fetch the queue-health view without exposing any mutation method. */
  async queueHealth(): Promise<QueueHealth> {
    let response: Awaited<ReturnType<FetchLike>>;
    try {
      response = await this.fetchImpl(`${this.baseUrl}/api/queue-health`, {
        method: "GET",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${this.bearerToken}`,
        },
      });
    } catch (cause) {
      throw new HubTransportError("control center request failed", { cause });
    }

    if (response.status === 401 || response.status === 403) {
      throw new HubAuthError(
        response.status,
        `control center rejected credentials (HTTP ${response.status})`,
      );
    }
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new HubTransportError(
        `control center HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
      );
    }

    const text = await response.text();
    let payload: unknown;
    try {
      payload = JSON.parse(text);
    } catch (cause) {
      throw new HubTransportError("control center returned invalid JSON", { cause });
    }
    return QueueHealth.parse(payload);
  }

  /** Fetch the platform object registry; this never grants object access. */
  async platformManifest(): Promise<PlatformManifest> {
    return this.getJson("/api/platform/manifest", PlatformManifest, "platform manifest");
  }

  /** Fetch static, provenance-bearing capability evidence. */
  async capabilityCatalog(): Promise<CapabilityCatalog> {
    return this.getJson("/api/capability-catalog", CapabilityCatalog, "capability catalog");
  }

  /** Read one authenticated platform object using a caller-owned Zod envelope schema. */
  async platformObject<T>(ref: PlatformObjectRef, schema: z.ZodType<T>): Promise<T> {
    const parsed = PlatformObjectRef.safeParse(ref);
    if (!parsed.success) {
      throw new HubValidationError("platform object reference is invalid", {
        cause: parsed.error,
      });
    }
    const path = `/api/platform/objects/${encodePlatformPathSegment(parsed.data.kind, "kind")}/${encodePlatformPathSegment(parsed.data.id, "id")}`;
    return this.getJson(path, schema, "platform object");
  }

  /** Create a task through the authenticated platform API. The hub attributes the actor. */
  async createTask(body: TaskCreateRequest): Promise<z.infer<typeof TaskWriteOutcome>> {
    return this.postJson("/api/tasks", TaskCreateRequestSchema, body, TaskWriteOutcome, "task create");
  }

  /** Update a task through the hub's authorized action route. */
  async updateTask(body: TaskUpdateRequest): Promise<z.infer<typeof TaskWriteOutcome>> {
    return this.postJson("/api/action/task", TaskUpdateRequestSchema, body, TaskWriteOutcome, "task update");
  }

  /** Resolve an approval. The hub requires a verified human identity. */
  async resolveApproval(body: ApprovalResolveRequest): Promise<z.infer<typeof ApprovalResolveResult>> {
    return this.postJson("/api/action/approval", ApprovalResolveRequestSchema, body, ApprovalResolveResult, "approval resolution");
  }

  /** Append evidence through the canonical, identity-attributed evidence writer. */
  async createEvidence(body: EvidenceCreateRequest): Promise<z.infer<typeof EvidenceCreateOutcome>> {
    return this.postJson("/api/evidence", EvidenceCreateRequestSchema, body, EvidenceCreateOutcome, "evidence append");
  }

  /** Queue an approval request without executing the requested tool. */
  async requestApproval(body: ApprovalRequest): Promise<z.infer<typeof ApprovalRequestResult>> {
    return this.postJson("/api/approvals/request", ApprovalRequestSchema, body, ApprovalRequestResult, "approval request");
  }

  /** Submit a hub-workspace file; the hub owns policy and queues human review. */
  async submitWorkProduct(body: WorkProductSubmitRequest): Promise<z.infer<typeof WorkProductWriteOutcome>> {
    return this.postJson(
      "/api/work-products",
      WorkProductSubmitRequestSchema,
      body,
      WorkProductWriteOutcome,
      "work product submit",
    );
  }

  /** Retry an approved submission using the hub's stored, approval-bound inputs. */
  async releaseWorkProduct(workProductId: number): Promise<z.infer<typeof WorkProductWriteOutcome>> {
    const request = WorkProductReleaseRequestSchema.safeParse({ work_product_id: workProductId });
    if (!request.success) {
      throw new HubValidationError("control center work product release request is invalid", { cause: request.error });
    }
    return this.postJson(
      `/api/work-products/${request.data.work_product_id}/release`,
      z.object({}).strict(),
      {},
      WorkProductWriteOutcome,
      "work product release",
    );
  }

  /** List registered ua-node devices and recent dispatches (Sessions launcher). */
  async listNodes(): Promise<z.infer<typeof NodesPayload>> {
    return this.getJson("/api/nodes", NodesPayload, "nodes");
  }

  /** List recent agent sessions (optional agent / open filter). */
  async listSessions(filter: { agent?: string; status?: "open" | "all" } = {}): Promise<
    z.infer<typeof SessionsListPayload>
  > {
    const qs = new URLSearchParams();
    if (filter.agent) qs.set("agent", filter.agent);
    if (filter.status === "open") qs.set("status", "open");
    const suffix = qs.toString() ? `?${qs}` : "";
    return this.getJson(`/api/sessions${suffix}`, SessionsListPayload, "sessions");
  }

  /** List uniform run rows. Filters and the cursor are optional. */
  async listRuns(filter: RunListQuery = {}): Promise<RunsPage> {
    const query = RunListQuery.safeParse(filter);
    if (!query.success) {
      throw new HubValidationError("control center runs list request is invalid", {
        cause: query.error,
      });
    }
    const qs = new URLSearchParams();
    if (query.data.kind) qs.set("kind", query.data.kind);
    if (query.data.agent) qs.set("agent", query.data.agent);
    if (query.data.task_id !== undefined) qs.set("task_id", String(query.data.task_id));
    if (query.data.limit !== undefined) qs.set("limit", String(query.data.limit));
    if (query.data.cursor) qs.set("cursor", query.data.cursor);
    const suffix = qs.toString() ? `?${qs}` : "";
    return this.getJson(`/api/runs${suffix}`, RunsPage, "runs");
  }

  /** Read one run row. An unknown id is a transport error with status 404. */
  async getRun(runId: string): Promise<Run> {
    if (!runId.trim()) {
      throw new HubValidationError("run id is invalid");
    }
    const detail = await this.getJson(
      `/api/runs/${encodeURIComponent(runId)}`,
      RunDetail,
      "run",
    );
    return detail.run;
  }

  /** Queue a harness session on a registered node (approval-gated). */
  async startNodeSession(
    body: z.input<typeof NodeSessionStartRequest>,
  ): Promise<z.infer<typeof NodeSessionOutcome>> {
    return this.postJson(
      "/api/action/node-session",
      NodeSessionStartRequest,
      { action: "start", ...body },
      NodeSessionOutcome,
      "node session start",
    );
  }

  /** Request cancel for a queued/claimed node_dispatch item. */
  async cancelNodeSession(itemId: string): Promise<z.infer<typeof NodeSessionCancelOutcome>> {
    return this.postJson(
      "/api/action/node-session",
      NodeSessionCancelRequest,
      { action: "cancel", item_id: itemId },
      NodeSessionCancelOutcome,
      "node session cancel",
    );
  }

  /** Re-queue a terminal item using its stored prompt (approval-gated). */
  async restartNodeSession(itemId: string): Promise<z.infer<typeof NodeSessionOutcome>> {
    return this.postJson(
      "/api/action/node-session",
      z.object({ action: z.literal("restart"), item_id: z.string().min(1) }),
      { action: "restart", item_id: itemId },
      NodeSessionOutcome,
      "node session restart",
    );
  }

  private async getJson<T>(path: string, schema: z.ZodType<T>, label: string): Promise<T> {
    let response: Awaited<ReturnType<FetchLike>>;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: "GET",
        headers: { Accept: "application/json", Authorization: `Bearer ${this.bearerToken}` },
      });
    } catch (cause) {
      throw new HubTransportError("control center request failed", { cause });
    }
    if (response.status === 401 || response.status === 403) {
      throw new HubAuthError(response.status, `control center rejected credentials (HTTP ${response.status})`);
    }
    if (!response.ok) throw new HubTransportError(`control center HTTP ${response.status}`, { status: response.status });
    let payload: unknown;
    try {
      payload = JSON.parse(await response.text());
    } catch (cause) {
      throw new HubTransportError("control center returned invalid JSON", { cause });
    }
    const parsed = schema.safeParse(payload);
    if (!parsed.success) throw new HubValidationError(`control center returned invalid ${label}`, { cause: parsed.error });
    return parsed.data;
  }

  private async postJson<TRequest, TResponse>(
    path: string,
    requestSchema: z.ZodType<TRequest>,
    body: unknown,
    responseSchema: z.ZodType<TResponse>,
    label: string,
  ): Promise<TResponse> {
    const request = requestSchema.safeParse(body);
    if (!request.success) {
      throw new HubValidationError(`control center ${label} request is invalid`, { cause: request.error });
    }
    let response: Awaited<ReturnType<FetchLike>>;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${this.bearerToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(request.data),
      });
    } catch (cause) {
      throw new HubTransportError("control center request failed", { cause });
    }
    if (response.status === 401 || response.status === 403) {
      throw new HubAuthError(response.status, `control center rejected credentials (HTTP ${response.status})`);
    }
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new HubTransportError(
        `control center HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
        { status: response.status },
      );
    }
    let payload: unknown;
    try {
      payload = JSON.parse(await response.text());
    } catch (cause) {
      throw new HubTransportError("control center returned invalid JSON", { cause });
    }
    const parsed = responseSchema.safeParse(payload);
    if (!parsed.success) {
      throw new HubValidationError(`control center returned invalid ${label} response`, { cause: parsed.error });
    }
    return parsed.data;
  }
}
