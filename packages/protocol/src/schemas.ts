/**
 * Phase 1 protocol surface for Unified Agent.
 *
 * Source of truth is Zod. JSON Schema is exported for cross-language consumers;
 * Pydantic models are generated from that JSON Schema (see scripts/).
 *
 * This package is a contract boundary — not a second policy implementation.
 * Python remains the execution/policy core (ADR TypeScript Platform Strategy).
 */

import { z } from "zod";

/** ISO-8601 timestamps as opaque strings (runtime parsing stays host-side). */
const IsoDateTime = z.string().min(1);

export const TaskStatus = z.enum([
  "open",
  "doing",
  "waiting_review",
  "blocked",
  "done",
  "cancelled",
]);
export type TaskStatus = z.infer<typeof TaskStatus>;

export const TaskPriority = z.enum(["high", "med", "low"]);
export type TaskPriority = z.infer<typeof TaskPriority>;

/** Task title accepted by task_add/task_update: non-empty after whitespace trim. */
const TaskTitle = z.string().min(1).regex(/\S/, "title must not be empty");

/**
 * Hub `task_list` / `task_claim` row.
 *
 * Known queue/review fields are modeled explicitly so they are not stripped by
 * Zod's default object mode (#929). Unknown hub keys are intentionally not
 * passed through: Zod 4 `.passthrough()` / `$loose` widens known fields (e.g.
 * `status`) to `any` for consumers. When the hub adds a stable field, extend
 * this schema and regenerate.
 */
export const Task = z.object({
  id: z.number().int().positive(),
  title: z.string().min(1),
  notes: z.string().nullable().optional(),
  /** Compact listings omit notes and report length instead. */
  notes_chars: z.number().int().nonnegative().optional(),
  status: TaskStatus,
  priority: TaskPriority.default("med"),
  /** Notes-derived urgency; null means the documented `next` sort default. */
  urgency: z.enum(["now", "next", "later"]).nullable().optional(),
  urgency_rank: z.number().int().min(0).max(2).optional(),
  parent_id: z.number().int().positive().nullable().optional(),
  /** Ancestor ids from the root down to the immediate parent (root-first). */
  lineage: z.array(z.number().int().positive()).optional(),
  project: z.string().min(1).default("unified-agent"),
  domain: z.string().nullable().optional(),
  lane: z.string().nullable().optional(),
  due_at: IsoDateTime.nullable().optional(),
  evidence_ref: z.string().nullable().optional(),
  source_host: z.string().nullable().optional(),
  claimed_by: z.string().nullable().optional(),
  claimed_at: IsoDateTime.nullable().optional(),
  lock_expires_at: IsoDateTime.nullable().optional(),
  /**
   * The caller's own verified label on an authenticated `task_claim` /
   * `task_update` reply (hub task #2037). It can differ from the run's agent:
   * a `doing` reassert keeps another agent's run.
   */
  verified_caller: z.string().optional(),
  worktree_path: z.string().nullable().optional(),
  branch: z.string().nullable().optional(),
  /** Stored next-move ownership (hub task #2388). */
  next_owner: z.string().nullable().optional(),
  waiting_on: z.string().nullable().optional(),
  terminal_predicate: z.string().nullable().optional(),
  ownership_generation: z.number().int().nonnegative().optional(),
  created_at: IsoDateTime,
  updated_at: IsoDateTime,
  completed_at: IsoDateTime.nullable().optional(),
  archived_at: IsoDateTime.nullable().optional(),
  /** Authoritative DB column (0/1 or boolean depending on driver). */
  untrusted: z.union([z.boolean(), z.number().int()]).optional(),
  /** Compact / queue-marker projections from `task_list(fields="compact")`. */
  waiting_for_review: z.union([z.boolean(), z.number().int()]).optional(),
  laptop_lane: z.union([z.boolean(), z.number().int()]).optional(),
  notes_archived: z.union([z.boolean(), z.number().int()]).optional(),
});
export type Task = z.infer<typeof Task>;

/** Who holds a task a `task_claim` did not take, and until when (hub task #2388). */
export const TaskSafeWait = z.object({
  owner: z.string().nullable(),
  waiting_on: z.string().nullable(),
  /** Lease expiry or the end of the wait; null for a lease with no expiry. */
  until: IsoDateTime.nullable(),
  generation: z.number().int().nonnegative(),
  claimed_by: z.string().nullable(),
});
export type TaskSafeWait = z.infer<typeof TaskSafeWait>;

/** A `task_claim` reply that took nothing: the task is owned, and the row is unchanged. */
export const TaskClaimWait = z.object({
  id: z.number().int().positive(),
  claimed: z.literal(false),
  safe_wait: TaskSafeWait,
  verified_caller: z.string().optional(),
});
export type TaskClaimWait = z.infer<typeof TaskClaimWait>;

/** `task_claim` answers with the claimed row, or a wait that is not a claim. */
export const TaskClaimResult = z.union([TaskClaimWait, Task]);
export type TaskClaimResult = z.infer<typeof TaskClaimResult>;

export function isTaskSafeWait(result: TaskClaimResult): result is TaskClaimWait {
  return "safe_wait" in result;
}

/** HTTP task creation request. Identity is derived from verified hub auth. */
export const TaskCreateRequest = z.object({
  title: TaskTitle,
  notes: z.string().optional(),
  priority: TaskPriority.default("med"),
  parent_id: z.number().int().positive().optional(),
  project: z.string().min(1).optional(),
  domain: z.string().nullable().optional(),
  due_at: IsoDateTime.optional(),
  evidence_ref: z.string().optional(),
  idempotency_key: z.string().min(1).optional(),
  lane: z.string().min(1).optional(),
}).strict();
// Callers may omit priority; parsing applies the service default before send.
export type TaskCreateRequest = z.input<typeof TaskCreateRequest>;

/**
 * HTTP task update request. Claim ownership and actor are never caller fields.
 *
 * `title` and `parent_id` are ordinary, policy-gated mutation fields — the
 * hub's `action_task` route already accepts and applies both (task #1300;
 * they were simply missing from this typed contract). `parent_id` follows
 * the hub tool's own sentinel: 0 detaches the task from its parent, any
 * other positive id reparents it, and omitting the field leaves it unchanged.
 *
 * `untrusted` is deliberately NOT part of this contract. It is a
 * hub-computed provenance/audit flag (`config.is_agent_untrusted`, keyed off
 * the verified caller's agent label) consumed directly by the executor's
 * trust-ladder gate. Exposing it as a writable field would let a typed
 * client declare `untrusted: false` for its own work and launder that
 * signal — see the follow-up filed against `action_task`'s override
 * behavior (`tools/tasks.py`), which lets a caller-supplied value overwrite
 * the computed one in either direction, unlike task_add's OR-only merge.
 */
export const TaskUpdateRequest = z.object({
  id: z.number().int().positive(),
  status: TaskStatus.optional(),
  title: TaskTitle.optional(),
  notes: z.string().optional(),
  append_note: z.string().optional(),
  priority: TaskPriority.optional(),
  parent_id: z.number().int().nonnegative().optional(),
  worktree: z.boolean().optional(),
  project: z.string().min(1).optional(),
  expected_project: z.string().min(1).optional(),
  domain: z.string().nullable().optional(),
  lane: z.string().min(1).optional(),
  due_at: IsoDateTime.optional(),
  clear_due_at: z.boolean().optional(),
  evidence_ref: z.string().optional(),
  idempotency_key: z.string().min(1).optional(),
}).strict();
export type TaskUpdateRequest = z.infer<typeof TaskUpdateRequest>;

export const TaskWriteResult = z.object({ id: z.number().int().positive() }).passthrough();
export type TaskWriteResult = z.infer<typeof TaskWriteResult>;

/** A write can park behind a one-shot approval instead of mutating state. */
export const PendingApprovalResult = z.object({
  approval_id: z.number().int().positive(),
  status: z.literal("pending"),
  tool: z.string().min(1),
  payload: z.record(z.string(), z.unknown()),
});
export type PendingApprovalResult = z.infer<typeof PendingApprovalResult>;

export const TaskWriteOutcome = z.union([TaskWriteResult, PendingApprovalResult]);
export type TaskWriteOutcome = z.infer<typeof TaskWriteOutcome>;

export const Tool = z.object({
  name: z.string().min(1),
  description: z.string().default(""),
  input_schema: z.record(z.string(), z.unknown()).optional(),
});
export type Tool = z.infer<typeof Tool>;

export const ToolInvocation = z.object({
  tool: z.string().min(1),
  arguments: z.record(z.string(), z.unknown()).default({}),
  caller: z.string().min(1).optional(),
  session_id: z.string().optional(),
});
export type ToolInvocation = z.infer<typeof ToolInvocation>;

export const Capability = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  description: z.string().default(""),
  tools: z.array(z.string()).default([]),
});
export type Capability = z.infer<typeof Capability>;

export const ApprovalStatus = z.enum([
  "pending",
  "approved",
  "denied",
  "consumed",
  "revoked",
  "expired",
]);
export type ApprovalStatus = z.infer<typeof ApprovalStatus>;

export const Approval = z.object({
  id: z.number().int().positive(),
  tool: z.string().min(1),
  payload: z.record(z.string(), z.unknown()).default({}),
  requested_by: z.string().nullable().optional(),
  status: ApprovalStatus,
  reason: z.string().nullable().optional(),
  resolved_by: z.string().nullable().optional(),
  created_at: IsoDateTime,
  updated_at: IsoDateTime,
  resolved_at: IsoDateTime.nullable().optional(),
  /** Present on approved-pass rows from `approval_pending` (#596). */
  expires_at: IsoDateTime.nullable().optional(),
  expires_in_hours: z.number().nullable().optional(),
  expired: z.boolean().optional(),
  /** Run recorded with the approval. Absent on older rows; null when none was recorded. */
  run_id: z.string().nullable().optional(),
});
export type Approval = z.infer<typeof Approval>;

/** Workspace file submission. The hub derives the producing identity from auth. */
export const WorkProductSubmitRequest = z.object({
  source_path: z.string().min(1).regex(/\S/),
  title: z.string().min(1).regex(/\S/),
  template_pack: z.string().min(1).regex(/\S/),
  tags: z.record(z.string(), z.string()).optional(),
  model: z.string().nullable().optional(),
}).strict();
export type WorkProductSubmitRequest = z.infer<typeof WorkProductSubmitRequest>;

/** A release retry names an existing submission; its reviewed inputs stay on the hub. */
export const WorkProductReleaseRequest = z.object({
  work_product_id: z.number().int().positive(),
}).strict();
export type WorkProductReleaseRequest = z.infer<typeof WorkProductReleaseRequest>;

export const WorkProductRow = z.object({
  id: z.number().int().positive(),
  title: z.string().min(1),
  content_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  source_path: z.string().min(1),
  size_bytes: z.number().int().nonnegative(),
  mime_type: z.string().nullable(),
  producing_agent: z.string().min(1),
  producing_model: z.string().nullable(),
  template_pack: z.string().min(1),
  tags: z.record(z.string(), z.string()),
  status: z.enum(["submitted", "approved", "rejected", "released", "tampered"]),
  previous_version_id: z.number().int().positive().nullable(),
  approval_id: z.number().int().positive().nullable(),
  released_path: z.string().nullable(),
  released_sha256: z.string().regex(/^[0-9a-f]{64}$/).nullable(),
  reject_reason: z.string().nullable(),
  submitted_by: z.string().min(1),
  resolved_by: z.string().nullable(),
  resolved_at: IsoDateTime.nullable(),
  released_at: IsoDateTime.nullable(),
  created_at: IsoDateTime,
  updated_at: IsoDateTime,
  note: z.string().optional(),
});
export type WorkProductRow = z.infer<typeof WorkProductRow>;

export const WorkProductPending = z.object({
  status: z.literal("pending"),
  approval_id: z.number().int().positive(),
  work_product_id: z.number().int().positive(),
});
export type WorkProductPending = z.infer<typeof WorkProductPending>;

export const WorkProductWriteOutcome = z.union([WorkProductPending, WorkProductRow]);
export type WorkProductWriteOutcome = z.infer<typeof WorkProductWriteOutcome>;

/** Queue-only HTTP approval request. This contract has no decision field. */
export const ApprovalRequest = z.object({
  tool: z.string().min(1),
  payload: z.record(z.string(), z.unknown()).default({}),
}).strict();
export type ApprovalRequest = z.infer<typeof ApprovalRequest>;

export const ApprovalRequestResult = z.object({
  approval_id: z.number().int().positive(),
  // An idempotent request can surface the existing decision without changing it.
  status: z.enum(["pending", "approved", "denied"]),
  tool: z.string().min(1),
  payload: z.record(z.string(), z.unknown()),
});
export type ApprovalRequestResult = z.infer<typeof ApprovalRequestResult>;

/** Human-only resolver. The hub derives the resolver identity from auth. */
export const ApprovalResolveRequest = z.object({
  id: z.number().int().positive(),
  decision: z.enum(["approve", "deny"]),
  reason: z.string().optional(),
}).strict();
export type ApprovalResolveRequest = z.infer<typeof ApprovalResolveRequest>;

export const ApprovalResolveResult = z.object({
  id: z.number().int().positive(),
  status: z.string().min(1),
}).passthrough();
export type ApprovalResolveResult = z.infer<typeof ApprovalResolveResult>;

/** Evidence event request. Originator and actor are derived from hub auth. */
export const EvidenceCreateRequest = z.object({
  channel: z.string().regex(/^sdk\./).max(120),
  payload: z.record(z.string(), z.unknown()),
  dedupe_key: z.string().min(1).max(256).optional(),
}).strict();
export type EvidenceCreateRequest = z.infer<typeof EvidenceCreateRequest>;

export const EvidenceCreateResult = z.object({
  event_ulid: z.string().min(1),
  inserted: z.boolean(),
});
export type EvidenceCreateResult = z.infer<typeof EvidenceCreateResult>;

export const EvidenceCreateOutcome = z.union([EvidenceCreateResult, PendingApprovalResult]);
export type EvidenceCreateOutcome = z.infer<typeof EvidenceCreateOutcome>;

/** One-shot pass matched on exact tool + payload fingerprint. */
export const ApprovalToken = z.object({
  approval_id: z.number().int().positive(),
  tool: z.string().min(1),
  payload_fingerprint: z.string().min(1),
  expires_at: IsoDateTime.nullable().optional(),
});
export type ApprovalToken = z.infer<typeof ApprovalToken>;

export const Principal = z.object({
  label: z.string().min(1),
  kind: z.enum(["agent", "human", "system"]).default("agent"),
  host: z.string().optional(),
});
export type Principal = z.infer<typeof Principal>;

export const Profile = z.object({
  name: z.string().min(1),
  identity: z.string().min(1),
  session_id: z.string().optional(),
  denied_tools: z.array(z.string()).default([]),
});
export type Profile = z.infer<typeof Profile>;

export const PolicyDecision = z.object({
  allow: z.boolean(),
  reason: z.string().min(1),
  rule: z.string().optional(),
  principal: Principal.optional(),
  tool: z.string().optional(),
});
export type PolicyDecision = z.infer<typeof PolicyDecision>;

export const ExecutionStatus = z.enum([
  "pending",
  "running",
  "completed",
  "blocked",
  "failed",
]);
export type ExecutionStatus = z.infer<typeof ExecutionStatus>;

export const Execution = z.object({
  id: z.string().min(1),
  task_id: z.number().int().positive().nullable().optional(),
  agent: z.string().min(1),
  status: ExecutionStatus,
  started_at: IsoDateTime.optional(),
  finished_at: IsoDateTime.nullable().optional(),
  summary: z.string().optional(),
});
export type Execution = z.infer<typeof Execution>;

export const Event = z.object({
  id: z.string().min(1).optional(),
  channel: z.string().min(1),
  type: z.string().min(1),
  payload: z.record(z.string(), z.unknown()).default({}),
  created_at: IsoDateTime,
  actor: z.string().optional(),
});
export type Event = z.infer<typeof Event>;

export const Artifact = z.object({
  id: z.string().min(1),
  kind: z.enum(["file", "diff", "log", "url", "blob"]).default("file"),
  uri: z.string().min(1),
  media_type: z.string().optional(),
  sha256: z.string().optional(),
});
export type Artifact = z.infer<typeof Artifact>;

export const Agent = z.object({
  label: z.string().min(1),
  profile: z.string().optional(),
  host: z.string().optional(),
  online: z.boolean().optional(),
});
export type Agent = z.infer<typeof Agent>;

export const MemoryRecord = z.object({
  id: z.string().min(1),
  title: z.string().nullable().optional(),
  content: z.string().min(1),
  tags: z.array(z.string()).default([]),
  author: z.string().nullable().optional(),
  salience: z.number().min(0).max(1).optional(),
  created_at: IsoDateTime.optional(),
  updated_at: IsoDateTime.optional(),
});
export type MemoryRecord = z.infer<typeof MemoryRecord>;

export const DispatchContext = z.enum([
  "task_notes",
  "recent_validation",
  "related_pr",
]);
export type DispatchContext = z.infer<typeof DispatchContext>;

/** Control-center dispatch payload (mirrors webui/contracts.DispatchRequest). */
export const DispatchRequest = z.object({
  agent: z.string().min(1),
  prompt: z.string().min(1).max(1000),
  task_id: z.number().int().positive().nullable().optional(),
  context: z.array(DispatchContext).default([]),
  project: z.string().min(1),
});
export type DispatchRequest = z.infer<typeof DispatchRequest>;

export const WorktreePolicy = z.object({
  require_task_claim: z.boolean().default(true),
  branch_prefix: z.string().default("cursor/"),
  allow_outside_workspace: z.boolean().default(false),
});
export type WorktreePolicy = z.infer<typeof WorktreePolicy>;

export const PathResolutionResult = z.object({
  requested: z.string().min(1),
  resolved: z.string().min(1),
  inside_root: z.boolean(),
  root: z.string().min(1),
  reason: z.string().optional(),
});
export type PathResolutionResult = z.infer<typeof PathResolutionResult>;

/** Named catalog used by JSON Schema export / codegen. */
export const PROTOCOL_SCHEMAS = {
  Task,
  TaskCreateRequest,
  TaskUpdateRequest,
  TaskWriteResult,
  PendingApprovalResult,
  TaskWriteOutcome,
  Tool,
  ToolInvocation,
  Capability,
  Approval,
  WorkProductSubmitRequest,
  WorkProductReleaseRequest,
  WorkProductRow,
  WorkProductPending,
  WorkProductWriteOutcome,
  ApprovalRequest,
  ApprovalRequestResult,
  ApprovalResolveRequest,
  ApprovalResolveResult,
  ApprovalToken,
  Principal,
  Profile,
  PolicyDecision,
  Execution,
  Event,
  EvidenceCreateRequest,
  EvidenceCreateResult,
  EvidenceCreateOutcome,
  Artifact,
  Agent,
  MemoryRecord,
  DispatchRequest,
  WorktreePolicy,
  PathResolutionResult,
} as const;

export type ProtocolSchemaName = keyof typeof PROTOCOL_SCHEMAS;
