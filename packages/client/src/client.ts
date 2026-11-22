/**
 * High-level typed client for the Unified Agent HTTP hub.
 *
 * Mutations (task_update, memory_save, delegate, approval_resolve, …) are
 * first-class. They still execute on the Python hub — this package does not
 * reimplement policy, approvals, or sandbox rules (ADR TypeScript Platform).
 */

import {
  Approval,
  Task,
  TaskClaimResult,
  ToolInvocation,
  type Approval as ApprovalType,
  type Task as TaskType,
  type TaskClaimResult as TaskClaimResultType,
} from "@unified-agent/protocol";
import { z } from "zod";

import { HubValidationError } from "./errors.js";
import { HubTransport, type CallToolOptions, type HubTransportOptions } from "./hub.js";

const TaskListResult = z.object({
  tasks: z.array(Task).default([]),
});

const MemorySearchExplanation = z.object({
  why: z.array(z.string()),
  lexical: z.number(),
  semantic: z.number().nullable(),
  recency: z.number(),
  salience: z.number(),
  access: z.number(),
  feedback: z.number(),
  entity: z.number(),
  host: z.string().nullable(),
  superseded: z.boolean(),
  expired: z.boolean(),
  entity_matches: z.array(z.object({ kind: z.string(), value: z.string() })),
  matched_tags: z.array(z.string()),
});

const MemorySearchHitSchema = z.object({
  id: z.string().min(1),
  title: z.string().nullable(),
  tags: z.array(z.string()),
  author: z.string().nullable(),
  updated_at: z.string().min(1),
  salience: z.number().min(0).max(1),
  confidence: z.number().nullable(),
  last_accessed: z.string().nullable(),
  access_count: z.number().int().nonnegative(),
  superseded_by: z.string().nullable(),
  source_host: z.string().nullable(),
  expires_at: z.string().nullable(),
  feedback_helpful: z.number().int().nonnegative(),
  feedback_not_helpful: z.number().int().nonnegative(),
  feedback_stale: z.number().int().nonnegative(),
  feedback_wrong: z.number().int().nonnegative(),
  snippet: z.string(),
  score: z.number().nullable(),
  similarity: z.number().nullable(),
  combined_score: z.number(),
  recency: z.number(),
  explanation: MemorySearchExplanation,
});
export type MemorySearchHit = z.infer<typeof MemorySearchHitSchema>;

const MemorySearchResult = z.object({
  results: z.array(MemorySearchHitSchema).default([]),
});

const ApprovalPendingResult = z
  .object({
    pending: z.array(Approval).default([]),
    approved_unconsumed: z.array(Approval).default([]),
    expired_passes: z.array(Approval).default([]),
    gated_tools: z.array(z.string()).default([]),
    pass_ttl_hours: z.number().nullable().optional(),
  })
  .passthrough();
export type ApprovalPending = z.infer<typeof ApprovalPendingResult>;

const ProfileStatusResult = z.object({
  profile: z.string().min(1),
  identity: z.string().min(1),
  session_scoped: z.boolean().optional(),
}).passthrough();
export type ProfileStatus = z.infer<typeof ProfileStatusResult>;

const CapabilityCatalogResult = z
  .object({
    read_only: z.boolean(),
    install_is_explicit: z.boolean(),
    approval_gated: z.boolean(),
    versions: z.record(z.string(), z.string()),
    agents: z.array(z.unknown()),
    mcp_tools: z.array(z.string()),
    mcp_client_matrix: z.unknown(),
    clis: z.array(z.string()),
    skills: z.array(z.unknown()),
    workflows: z.array(z.unknown()),
    health: z.unknown(),
    boundaries: z.unknown(),
    prerequisites: z.unknown().nullable(),
  })
  .passthrough();
export type CapabilityCatalogResult = z.infer<typeof CapabilityCatalogResult>;

export type CapabilityCatalogParams = {
  task_id?: number;
  agent_label?: string;
};

/** Live `system_doctor` payload — hub never sends a top-level `status` (#929). */
const SystemDoctorCheck = z
  .object({
    capability: z.string().min(1),
    status: z.string().min(1),
    detail: z.string().optional(),
    gate: z.string().optional(),
  })
  .passthrough();

const SystemDoctorResult = z
  .object({
    as_of: z.string().min(1),
    checks: z.array(SystemDoctorCheck).default([]),
    host: z.string().nullable().optional(),
    hosts: z.array(z.unknown()).default([]),
    summary: z
      .object({
        live: z.number().int().nonnegative(),
        dormant: z.number().int().nonnegative(),
        degraded: z.number().int().nonnegative().optional().default(0),
        misconfigured: z.number().int().nonnegative(),
        unknown: z.number().int().nonnegative(),
      })
      .passthrough(),
    hosts_error: z.string().optional(),
  })
  .passthrough();
export type SystemDoctorReport = z.infer<typeof SystemDoctorResult>;

/**
 * Boolean capability flags as reported by the `mcp_health_telemetry` tool —
 * a hub-specific runtime summary, distinct from the raw MCP `initialize`
 * `capabilities` object (see `HubServerCapabilities` in `hub.ts`). Fields
 * verified against a live hub response (2026-09-11); kept optional and
 * passthrough since this is an operator-facing summary the hub is free to
 * extend.
 */
const McpHealthServerCapabilities = z
  .object({
    tools: z.boolean().optional(),
    tool_annotations: z.boolean().optional(),
    structured_outputs: z.boolean().optional(),
    elicitation: z.boolean().optional(),
    progress: z.boolean().optional(),
    resources: z.boolean().optional(),
    resource_subscriptions: z.boolean().optional(),
  })
  .passthrough();

const McpHealthTelemetryResult = z
  .object({
    status: z.string().min(1),
    negotiated_protocol_version: z.string().nullable().optional(),
    target_protocol_version: z.string().nullable().optional(),
    server_capabilities: McpHealthServerCapabilities.optional(),
  })
  .passthrough();
export type McpHealthTelemetry = z.infer<typeof McpHealthTelemetryResult>;

/**
 * One row of the append-only swarm event log (`event_log_query`, task #157).
 * `payload` is writer-defined JSON — left as `z.unknown()` rather than typed
 * per-channel, since channels span task/approval/memory/journal mutations.
 */
const EventLogEntry = z
  .object({
    ulid: z.string().min(1),
    channel: z.string().min(1),
    payload: z.unknown(),
    originator: z.string().nullable(),
    episode_id: z.string().nullable(),
    causal_parent_id: z.string().nullable(),
    created_at: z.string().nullable(),
  })
  .passthrough();
export type EventLogEntry = z.infer<typeof EventLogEntry>;

const EventLogQueryResult = z
  .object({
    events: z.array(EventLogEntry).default([]),
    count: z.number().int().nonnegative(),
    cursor: z.string().nullable(),
    source_event_count: z.number().int().nonnegative(),
    last_event_at: z.string().nullable(),
  })
  .passthrough();
export type EventLogQueryResult = z.infer<typeof EventLogQueryResult>;

/**
 * `event_log_verify` (task #746) report on the tamper-evident hash chain.
 * Only `status`/`verified`/`total_verified` are guaranteed across every
 * outcome (`empty`/`valid`/`tampered`) — `head_hash`, `head_ulid`,
 * `unhashed_skipped`, and `tampered_at` vary by branch, so they stay
 * optional/passthrough rather than forcing one shape on all three.
 */
const EventLogVerifyResult = z
  .object({
    status: z.enum(["valid", "tampered", "empty"]),
    verified: z.boolean(),
    total_verified: z.number().int().nonnegative(),
    head_hash: z.string().nullable().optional(),
    head_ulid: z.string().nullable().optional(),
    unhashed_skipped: z.number().int().nonnegative().optional(),
    tampered_at: z
      .object({
        rowid: z.number().optional(),
        ulid: z.string().optional(),
        channel: z.string().optional(),
        reason: z.string().optional(),
        expected_prev_hash: z.string().nullable().optional(),
        actual_prev_hash: z.string().nullable().optional(),
      })
      .passthrough()
      .optional(),
  })
  .passthrough();
export type EventLogVerifyResult = z.infer<typeof EventLogVerifyResult>;

/** One task's evidence row inside a project chain (`evidence_report`). */
const ProjectEvidence = z
  .object({
    task_id: z.number(),
    evidence_ref: z.string().nullable(),
    pr_url: z.string().nullable(),
    commit_sha: z.string().nullable(),
    merge_verified: z.boolean(),
    completed_at: z.string().nullable(),
  })
  .passthrough();

const ProjectChain = z
  .object({
    project_id: z.number(),
    status: z.string(),
    task_ids: z.array(z.number()).default([]),
    done_task_ids: z.array(z.number()).default([]),
    evidence: z.array(ProjectEvidence).default([]),
    merged_prs: z.array(ProjectEvidence).default([]),
    last_merge: ProjectEvidence.nullable().default(null),
  })
  .passthrough();

/**
 * `evidence_report` (task #781) — the 5-link accountability chain
 * (human -> device -> agent -> action -> proof) for a task, PR, or window.
 * `records` and `chain_integrity`/`filter` stay `z.unknown()`/passthrough
 * objects: their per-record shape is evidence-source-dependent and not
 * worth over-constraining here — callers that need record internals can
 * read them straight off the parsed object.
 */
const EvidenceReportResult = z
  .object({
    report_type: z.string().min(1),
    schema_version: z.string().min(1),
    generated_at: z.string().min(1),
    chain_integrity: z.record(z.string(), z.unknown()),
    filter: z.record(z.string(), z.unknown()),
    total_records: z.number().int().nonnegative(),
    records: z.array(z.record(z.string(), z.unknown())).default([]),
    project_chain: ProjectChain.nullable().default(null),
  })
  .passthrough();
export type EvidenceReportResult = z.infer<typeof EvidenceReportResult>;

/**
 * `dispatch_delivery_receipt` executor-auth block — the hub's standalone
 * daemon-client doctor probe, attached to a receipt only when the hub ran it
 * (`executor_auth` is null otherwise, e.g. in `receipts_for_task` history).
 */
const ExecutorAuthProof = z.object({
  status: z.enum(["LIVE", "DORMANT", "MISCONFIGURED", "UNKNOWN"]),
  authenticated: z.boolean().nullable(),
  probe_process: z.enum(["separate", "hub_skipped", "not_run"]),
});
export type ExecutorAuthProof = z.infer<typeof ExecutorAuthProof>;

/**
 * One `agent.dispatch.transition` event-log row as embedded in a receipt.
 * The hub always writes these seven keys; payload-derived values are null
 * when the underlying event did not carry them.
 */
const DeliveryReceiptTransition = z
  .object({
    ulid: z.string().min(1),
    state: z.string().nullable(),
    timestamp: z.string().nullable(),
    actor: z.string().nullable(),
    failure_reason: z.string().nullable(),
    evidence_ref: z.string().nullable(),
    retry_count: z.number().int().nullable(),
  })
  .passthrough();
export type DeliveryReceiptTransition = z.infer<typeof DeliveryReceiptTransition>;

/** Dispatch-run lease snapshot embedded in a receipt. */
const DeliveryReceiptLease = z
  .object({
    owner: z.string().nullable(),
    expires_at: z.string().nullable(),
    active: z.boolean(),
  })
  .passthrough();
export type DeliveryReceiptLease = z.infer<typeof DeliveryReceiptLease>;

/**
 * `dispatch_delivery_receipt` — the read-only accountability receipt for one
 * dispatch run (`DeliveryReceipt` on the hub). Unknown run ids surface as
 * `HubRpcError` ("unknown dispatch run"), not as a null receipt.
 */
const DeliveryReceiptResult = z
  .object({
    schema_version: z.string().min(1),
    task_id: z.number().int(),
    run_id: z.string().min(1),
    delegation_id: z.number().int().nullable(),
    executor_identity: z.string().min(1),
    lease: DeliveryReceiptLease,
    retry_count: z.number().int().nullable(),
    event_cursor: z.string().nullable(),
    policy_outcome: z.string().min(1),
    approval_outcome: z.string().min(1),
    evidence_ref: z.string().nullable(),
    final_task_state: z.string().nullable(),
    run_state: z.string().min(1),
    terminal: z.boolean(),
    transitions: z.array(DeliveryReceiptTransition).default([]),
    executor_auth: ExecutorAuthProof.nullable(),
  })
  .passthrough();
export type DeliveryReceipt = z.infer<typeof DeliveryReceiptResult>;

export type EventLogQueryParams = {
  channels?: string[];
  /** Prefix filter, unioned with channels by the hub. */
  channel_prefix?: string;
  since?: string;
  limit?: number;
};

export type EvidenceReportParams = {
  task_id?: number;
  project_id?: number;
  since?: string;
  until?: string;
  agent?: string;
  project?: string;
  pr?: string;
  limit?: number;
};

const VaultSearchMatch = z.object({
  path: z.string().min(1),
  line_no: z.number().int().positive(),
  line: z.string(),
});

const VaultSearchResultSchema = z
  .object({
    matches: z.array(VaultSearchMatch).default([]),
    truncated: z.boolean(),
  })
  .passthrough();
export type VaultSearchResult = z.infer<typeof VaultSearchResultSchema>;

const VaultReadResultSchema = z
  .object({
    path: z.string().min(1),
    content: z.string(),
    total_lines: z.number().int().nonnegative(),
    truncated: z.boolean(),
  })
  .passthrough();
export type VaultReadResult = z.infer<typeof VaultReadResultSchema>;

const VaultQueryHit = z
  .object({
    path: z.string().min(1),
    title: z.string(),
    score: z.number(),
    lexical: z.number(),
    pagerank: z.number(),
    snippet: z.string(),
    links_out: z.number().int().nonnegative(),
    links_in: z.number().int().nonnegative(),
  })
  .passthrough();

const VaultQueryGraphStats = z
  .object({
    notes: z.number().int().nonnegative(),
    edges: z.number().int().nonnegative(),
    seeds: z.number().int().nonnegative(),
  })
  .passthrough();

const VaultQueryResultSchema = z
  .object({
    results: z.array(VaultQueryHit).default([]),
    graph: VaultQueryGraphStats,
  })
  .passthrough();
export type VaultQueryResult = z.infer<typeof VaultQueryResultSchema>;

export type UnifiedAgentClientOptions = HubTransportOptions & {
  /** Soft-parse hub payloads with protocol Zod schemas (default true). */
  validate?: boolean;
};

export type TaskListParams = {
  status?: string;
  priority?: string;
  parent_id?: number | null;
  notes_contain?: string;
  limit?: number;
  project?: string;
  domain?: string;
  lane?: string;
  fields?: "full" | "compact";
  frontier?: boolean;
  stale_claims?: boolean;
  archived?: boolean;
};

export type TaskAddParams = {
  title: string;
  notes?: string;
  priority?: "high" | "med" | "low";
  parent_id?: number;
  project?: string;
  domain?: string;
  lane?: string;
  due_at?: string;
  idempotency_key?: string;
};

export type TaskUpdateParams = {
  id: number;
  status?: string;
  notes?: string;
  append_note?: string;
  priority?: string;
  worktree?: boolean;
  project?: string;
  expected_project?: string;
  domain?: string;
  lane?: string;
  due_at?: string;
  clear_due_at?: boolean;
  evidence_ref?: string;
  idempotency_key?: string;
  next_owner?: string;
  waiting_on?: string;
  terminal_predicate?: string;
};

export type TaskClaimParams = {
  id: number;
  agent: string;
  timeout_seconds?: number;
  expected_project?: string;
  expected_lane?: string;
};

export type MemorySaveParams = {
  content: string;
  title?: string;
  tags?: string[];
  id?: string;
  author?: string;
  append?: boolean;
  overwrite?: boolean;
  idempotency_key?: string;
};

export type MemorySearchParams = {
  query: string;
  tags?: string[];
  limit?: number;
  include_expired?: boolean;
};

export type DelegateParams = {
  task_spec: string;
  from_agent: string;
  to_agent: string;
  deadline?: string;
  callback?: "notify" | "memory" | "none";
  priority?: "high" | "med" | "low";
  parent_task_id?: number;
  project?: string;
};

export type ApprovalResolveParams = {
  id: number;
  decision: "approve" | "deny";
  resolved_by: string;
  reason?: string;
};

export type VaultSearchParams = {
  pattern: string;
  path?: string;
  max_results?: number;
};

export type VaultReadParams = {
  path: string;
  offset?: number;
  limit?: number;
  expand_embeds?: boolean;
};

export type VaultQueryParams = {
  query: string;
  path?: string;
  max_results?: number;
};

/**
 * Hub-backed SDK. Prefer the typed methods; use {@link callTool} for the rest
 * of the ~150-tool surface without waiting for a generated wrapper.
 */
export class UnifiedAgentClient {
  readonly transport: HubTransport;
  private readonly validate: boolean;

  constructor(options: UnifiedAgentClientOptions) {
    this.transport = new HubTransport(options);
    this.validate = options.validate !== false;
  }

  async close(): Promise<void> {
    await this.transport.close();
  }

  /** Prove the bearer token + session handshake work (``tools/list``). */
  async ping(): Promise<void> {
    await this.transport.listTools();
  }

  /** Generic escape hatch — still hub-authorized. */
  async callTool(
    name: string,
    args: Record<string, unknown> = {},
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    ToolInvocation.parse({ tool: name, arguments: args });
    return this.transport.callTool(name, args, options);
  }

  async taskList(
    params: TaskListParams = {},
    options: CallToolOptions = {},
  ): Promise<TaskType[]> {
    const raw = await this.transport.callTool("task_list", compact(params), options);
    if (!this.validate) {
      const tasks = raw.tasks;
      return Array.isArray(tasks) ? (tasks as TaskType[]) : [];
    }
    return parseOrThrow(TaskListResult, raw, "task_list").tasks;
  }

  async taskAdd(
    params: TaskAddParams,
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.transport.callTool("task_add", compact(params), options);
  }

  async taskUpdate(
    params: TaskUpdateParams,
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.transport.callTool("task_update", compact(params), options);
  }

  /**
   * Claim a task. A task someone else owns is not an error: the result is then
   * a non-claim carrying `safe_wait`, so check `isTaskSafeWait` before using it.
   */
  async taskClaim(
    params: TaskClaimParams,
    options: CallToolOptions = {},
  ): Promise<TaskClaimResultType> {
    const raw = await this.transport.callTool("task_claim", compact(params), options);
    if (!this.validate) {
      return raw as TaskClaimResultType;
    }
    return parseOrThrow(TaskClaimResult, raw, "task_claim");
  }

  async memorySearch(
    params: MemorySearchParams,
    options: CallToolOptions = {},
  ): Promise<MemorySearchHit[]> {
    const raw = await this.transport.callTool("memory_search", compact(params), options);
    if (!this.validate) {
      const results = raw.results;
      return Array.isArray(results) ? (results as MemorySearchHit[]) : [];
    }
    return parseOrThrow(MemorySearchResult, raw, "memory_search").results;
  }

  async memorySave(
    params: MemorySaveParams,
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.transport.callTool("memory_save", compact(params), options);
  }

  async memoryGet(
    id: string,
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.transport.callTool("memory_get", { id }, options);
  }

  async approvalPending(
    params: { tool?: string; limit?: number } = {},
    options: CallToolOptions = {},
  ): Promise<ApprovalPending> {
    const raw = await this.transport.callTool(
      "approval_pending",
      compact(params),
      options,
    );
    if (!this.validate) {
      return {
        pending: Array.isArray(raw.pending) ? (raw.pending as ApprovalType[]) : [],
        approved_unconsumed: Array.isArray(raw.approved_unconsumed)
          ? (raw.approved_unconsumed as ApprovalType[])
          : [],
        expired_passes: Array.isArray(raw.expired_passes)
          ? (raw.expired_passes as ApprovalType[])
          : [],
        gated_tools: Array.isArray(raw.gated_tools)
          ? (raw.gated_tools as string[])
          : [],
        pass_ttl_hours:
          typeof raw.pass_ttl_hours === "number" || raw.pass_ttl_hours === null
            ? (raw.pass_ttl_hours as number | null)
            : undefined,
        ...raw,
      } as ApprovalPending;
    }
    return parseOrThrow(ApprovalPendingResult, raw, "approval_pending");
  }

  async approvalResolve(
    params: ApprovalResolveParams,
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.transport.callTool("approval_resolve", compact(params), options);
  }

  async delegate(
    params: DelegateParams,
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.transport.callTool("delegate", compact(params), options);
  }

  async vaultJournal(
    params: {
      entry: string;
      agent: string;
      tags?: string[];
    },
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.transport.callTool("vault_journal", compact(params), options);
  }

  async vaultSearch(
    params: VaultSearchParams,
    options: CallToolOptions = {},
  ): Promise<VaultSearchResult> {
    const raw = await this.transport.callTool("vault_search", compact(params), options);
    if (!this.validate) {
      return raw as VaultSearchResult;
    }
    return parseOrThrow(VaultSearchResultSchema, raw, "vault_search");
  }

  async vaultRead(
    params: VaultReadParams,
    options: CallToolOptions = {},
  ): Promise<VaultReadResult> {
    const raw = await this.transport.callTool("vault_read", compact(params), options);
    if (!this.validate) {
      return raw as VaultReadResult;
    }
    return parseOrThrow(VaultReadResultSchema, raw, "vault_read");
  }

  async vaultQuery(
    params: VaultQueryParams,
    options: CallToolOptions = {},
  ): Promise<VaultQueryResult> {
    const raw = await this.transport.callTool("vault_query", compact(params), options);
    if (!this.validate) {
      return raw as VaultQueryResult;
    }
    return parseOrThrow(VaultQueryResultSchema, raw, "vault_query");
  }

  async dispatchCancel(
    params: {
      run_id: string;
      reason?: string;
    },
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.transport.callTool("dispatch_cancel", compact(params), options);
  }

  async dispatchResume(
    params: {
      run_id: string;
    },
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.transport.callTool("dispatch_resume", compact(params), options);
  }

  /**
   * Read the accountability receipt for one dispatch run
   * (`dispatch_delivery_receipt`): policy/approval outcomes, lease, retry
   * count, transition history, and the executor-auth probe. Read-only; the
   * hub raises a tool error for an unknown `run_id`.
   */
  async dispatchDeliveryReceipt(
    params: {
      run_id: string;
    },
    options: CallToolOptions = {},
  ): Promise<DeliveryReceipt> {
    const raw = await this.transport.callTool(
      "dispatch_delivery_receipt",
      compact(params),
      options,
    );
    if (!this.validate) {
      return raw as DeliveryReceipt;
    }
    return parseOrThrow(DeliveryReceiptResult, raw, "dispatch_delivery_receipt");
  }

  async profileStatus(options: CallToolOptions = {}): Promise<ProfileStatus> {
    const raw = await this.transport.callTool("profile_status", {}, options);
    if (!this.validate) {
      return raw as ProfileStatus;
    }
    return parseOrThrow(ProfileStatusResult, raw, "profile_status");
  }

  /** Read the live MCP operator inventory; policy remains on the hub. */
  async capabilityCatalog(
    params: CapabilityCatalogParams = {},
    options: CallToolOptions = {},
  ): Promise<CapabilityCatalogResult> {
    if ((params.task_id === undefined) !== (params.agent_label === undefined)) {
      throw new HubValidationError("capability_catalog requires task_id and agent_label together");
    }
    const raw = await this.transport.callTool("capability_catalog", compact(params), options);
    if (!this.validate) return raw as CapabilityCatalogResult;
    return parseOrThrow(CapabilityCatalogResult, raw, "capability_catalog");
  }

  async systemDoctor(options: CallToolOptions = {}): Promise<SystemDoctorReport> {
    const raw = await this.transport.callTool("system_doctor", {}, options);
    if (!this.validate) {
      return raw as SystemDoctorReport;
    }
    return parseOrThrow(SystemDoctorResult, raw, "system_doctor");
  }

  async mcpHealthTelemetry(
    options: CallToolOptions = {},
  ): Promise<McpHealthTelemetry> {
    const raw = await this.transport.callTool("mcp_health_telemetry", {}, options);
    if (!this.validate) {
      return raw as McpHealthTelemetry;
    }
    return parseOrThrow(McpHealthTelemetryResult, raw, "mcp_health_telemetry");
  }

  /**
   * Read the append-only swarm event log (`event_log_query`, task #157),
   * oldest first. `since` is an exclusive ULID cursor — pass back the
   * previous call's `cursor` to resume with no re-processing and no gap.
   * `channel_prefix` accepts the hub's single-prefix filter.
   */
  async eventLogQuery(
    params: EventLogQueryParams = {},
    options: CallToolOptions = {},
  ): Promise<EventLogQueryResult> {
    const raw = await this.transport.callTool("event_log_query", compact(params), options);
    if (!this.validate) {
      return raw as EventLogQueryResult;
    }
    return parseOrThrow(EventLogQueryResult, raw, "event_log_query");
  }

  /**
   * Verify the tamper-evident hash chain of the event/audit log
   * (`event_log_verify`, task #746). Recomputes SHA-256 hashes across every
   * chained entry to confirm nothing was altered, deleted, or reordered.
   */
  async eventLogVerify(options: CallToolOptions = {}): Promise<EventLogVerifyResult> {
    const raw = await this.transport.callTool("event_log_verify", {}, options);
    if (!this.validate) {
      return raw as EventLogVerifyResult;
    }
    return parseOrThrow(EventLogVerifyResult, raw, "event_log_verify");
  }

  /**
   * Read attributable evidence (`evidence_report`, task #781) tracing the
   * 5-link accountability chain — human -> device -> agent -> action -> proof
   * — for a task, PR, agent, or time window, optionally for a complete
   * project task subtree.
   */
  async evidenceReport(
    params: EvidenceReportParams = {},
    options: CallToolOptions = {},
  ): Promise<EvidenceReportResult> {
    const raw = await this.transport.callTool("evidence_report", compact(params), options);
    if (!this.validate) {
      return raw as EvidenceReportResult;
    }
    return parseOrThrow(EvidenceReportResult, raw, "evidence_report");
  }
}

function compact(params: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      out[key] = value;
    }
  }
  return out;
}

function parseOrThrow<T>(
  schema: z.ZodType<T>,
  value: unknown,
  tool: string,
): T {
  const parsed = schema.safeParse(value);
  if (!parsed.success) {
    throw new HubValidationError(`hub ${tool} response failed schema validation`, {
      cause: parsed.error,
    });
  }
  return parsed.data;
}
