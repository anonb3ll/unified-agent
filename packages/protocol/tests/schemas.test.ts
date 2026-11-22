import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import { z } from "zod";

import {
  PROTOCOL_SCHEMAS,
  DispatchRequest,
  PathResolutionResult,
  PolicyDecision,
  Task,
  TaskClaimResult,
  isTaskSafeWait,
} from "../src/schemas.ts";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const schemaDir = join(root, "generated", "json-schema");
const fixturePath = join(dirname(fileURLToPath(import.meta.url)), "fixtures", "valid-protocol.json");
const fixtures = JSON.parse(readFileSync(fixturePath, "utf8")) as Record<string, unknown>;
const nonNullableDefaultFields = [
  ["Task", "priority"],
  ["Task", "project"],
  ["Tool", "description"],
  ["ToolInvocation", "arguments"],
  ["Capability", "description"],
  ["Capability", "tools"],
  ["Approval", "payload"],
  ["Principal", "kind"],
  ["Profile", "denied_tools"],
  ["Artifact", "kind"],
  ["MemoryRecord", "tags"],
  ["DispatchRequest", "context"],
  ["Event", "payload"],
  ["WorktreePolicy", "require_task_claim"],
  ["WorktreePolicy", "branch_prefix"],
  ["WorktreePolicy", "allow_outside_workspace"],
] as const;

describe("@unified-agent/protocol schemas", () => {
  it("exposes the ADR Phase 1 schema catalog", () => {
    const expected = [
      "Task",
      "TaskCreateRequest",
      "TaskUpdateRequest",
      "TaskWriteResult",
      "PendingApprovalResult",
      "TaskWriteOutcome",
      "Tool",
      "ToolInvocation",
      "Capability",
      "Approval",
      "WorkProductSubmitRequest",
      "WorkProductReleaseRequest",
      "WorkProductRow",
      "WorkProductPending",
      "WorkProductWriteOutcome",
      "ApprovalRequest",
      "ApprovalRequestResult",
      "ApprovalResolveRequest",
      "ApprovalResolveResult",
      "ApprovalToken",
      "Principal",
      "Profile",
      "PolicyDecision",
      "Execution",
      "Event",
      "EvidenceCreateRequest",
      "EvidenceCreateResult",
      "EvidenceCreateOutcome",
      "Artifact",
      "Agent",
      "MemoryRecord",
      "DispatchRequest",
      "WorktreePolicy",
      "PathResolutionResult",
    ];
    assert.deepEqual(Object.keys(PROTOCOL_SCHEMAS).sort(), [...expected].sort());
  });

  it("keeps the stored ownership fields on a Task row", () => {
    const row = Task.parse({
      ...(fixtures.Task as object),
      next_owner: "Desktop_Agent2",
      waiting_on: null,
      terminal_predicate: "merged",
      ownership_generation: 2,
    });
    assert.equal(row.next_owner, "Desktop_Agent2");
    assert.equal(row.waiting_on, null);
    assert.equal(row.ownership_generation, 2);
    assert.equal(Task.parse(fixtures.Task).next_owner, undefined);
  });

  it("types task_claim as a claimed row or a non-claim safe_wait", () => {
    const wait = TaskClaimResult.parse({
      id: 7,
      claimed: false,
      safe_wait: {
        owner: "Desktop_Agent2",
        waiting_on: null,
        until: null,
        generation: 1,
        claimed_by: "Desktop_Agent2",
      },
      verified_caller: "Desktop_Agent4",
    });
    assert.equal(isTaskSafeWait(wait), true);
    if (isTaskSafeWait(wait)) assert.equal(wait.safe_wait.owner, "Desktop_Agent2");

    const claimed = TaskClaimResult.parse(fixtures.Task);
    assert.equal(isTaskSafeWait(claimed), false);
    assert.equal(TaskClaimResult.safeParse({ id: 7, claimed: true }).success, false);
  });

  it("keeps approval run_id when set, null, or absent", () => {
    const base = fixtures.Approval as Record<string, unknown>;
    assert.equal(PROTOCOL_SCHEMAS.Approval.parse({ ...base, run_id: "sp-ABC" }).run_id, "sp-ABC");
    assert.equal(PROTOCOL_SCHEMAS.Approval.parse({ ...base, run_id: null }).run_id, null);
    assert.equal(PROTOCOL_SCHEMAS.Approval.parse(base).run_id, undefined);
  });

  it("parses a valid Task and rejects an invalid status", () => {
    const task = Task.parse({
      id: 556,
      title: "Land packages/protocol",
      status: "doing",
      priority: "high",
      project: "unified-agent",
      created_at: "2026-08-23T21:38:00+00:00",
      updated_at: "2026-08-24T03:52:22+00:00",
    });
    assert.equal(task.id, 556);
    assert.equal(Task.parse({ ...task, urgency: "now", urgency_rank: 0 }).urgency, "now");
    assert.throws(() => Task.parse({ ...task, urgency: "immediate" }), /Invalid/);
    assert.throws(() => Task.parse({ ...task, status: "nope" }), /Invalid/);
  });

  it("keeps a claim reply's verified_caller (hub task #2037)", () => {
    const reply = Task.parse({
      id: 2037,
      title: "Usage hook",
      status: "doing",
      created_at: "2026-10-14T03:45:04+00:00",
      updated_at: "2026-10-14T05:59:27+00:00",
      claimed_by: "agent-a",
      verified_caller: "agent-b",
    });
    assert.equal(reply.verified_caller, "agent-b");
    assert.equal(reply.claimed_by, "agent-a");
  });

  it("parses DispatchRequest like the control-center contract", () => {
    const req = DispatchRequest.parse({
      agent: "Desktop_Agent4",
      prompt: "scaffold protocol",
      task_id: 556,
      context: ["task_notes"],
      project: "unified-agent",
    });
    assert.equal(req.agent, "Desktop_Agent4");
    assert.throws(
      () =>
        DispatchRequest.parse({
          agent: "Desktop_Agent4",
          prompt: "x".repeat(1001),
          project: "unified-agent",
        }),
      /too big|Invalid|max/i,
    );
  });

  it("keeps HTTP writes strict and excludes caller-controlled identities", () => {
    assert.equal(PROTOCOL_SCHEMAS.TaskCreateRequest.safeParse({ title: "x", actor: "Operator" }).success, false);
    assert.equal(PROTOCOL_SCHEMAS.TaskUpdateRequest.safeParse({ id: 1, claimed_by: "Desktop_Agent2" }).success, false);
    assert.equal(PROTOCOL_SCHEMAS.ApprovalRequest.safeParse({ tool: "task_update", decision: "approve" }).success, false);
    assert.equal(PROTOCOL_SCHEMAS.WorkProductSubmitRequest.safeParse({ source_path: "/workspace/x", title: "Memo", template_pack: "legal", producing_agent: "Operator" }).success, false);
    assert.equal(PROTOCOL_SCHEMAS.WorkProductReleaseRequest.safeParse({ work_product_id: 27, title: "Changed" }).success, false);
    assert.equal(PROTOCOL_SCHEMAS.ApprovalResolveRequest.safeParse({ id: 1, decision: "approve", resolved_by: "Operator" }).success, false);
    assert.equal(PROTOCOL_SCHEMAS.EvidenceCreateRequest.safeParse({ channel: "task", payload: {}, originator: "Operator" }).success, false);
    assert.equal(PROTOCOL_SCHEMAS.ApprovalRequestResult.safeParse({ approval_id: 1, status: "approved", tool: "task_update", payload: {} }).success, true);
  });

  it("keeps work-product release bound to the stored submission", () => {
    const request = PROTOCOL_SCHEMAS.WorkProductSubmitRequest.parse(fixtures.WorkProductSubmitRequest);
    assert.equal(request.title, "Synthetic memo");
    assert.equal(PROTOCOL_SCHEMAS.WorkProductSubmitRequest.safeParse({ ...request, title: " \t " }).success, false);
    const schema = JSON.parse(readFileSync(join(schemaDir, "WorkProductSubmitRequest.json"), "utf8"));
    assert.equal(schema.properties.title.pattern, "\\S");
    assert.equal(PROTOCOL_SCHEMAS.WorkProductReleaseRequest.safeParse({ work_product_id: 0 }).success, false);
    assert.equal(PROTOCOL_SCHEMAS.WorkProductWriteOutcome.safeParse(fixtures.WorkProductPending).success, true);
    assert.equal(PROTOCOL_SCHEMAS.WorkProductWriteOutcome.safeParse(fixtures.WorkProductRow).success, true);
  });

  it("defaults omitted task-create priority to med while preserving optional input", () => {
    const parsed = PROTOCOL_SCHEMAS.TaskCreateRequest.parse({ title: "default priority" });
    assert.equal(parsed.priority, "med");

    const schema = JSON.parse(
      readFileSync(join(schemaDir, "TaskCreateRequest.json"), "utf8"),
    );
    assert.equal(schema.properties.priority.default, "med");
    assert.equal(schema.required.includes("priority"), false);
  });

  it("task #1300: TaskUpdateRequest accepts title/parent_id but rejects the untrusted provenance flag", () => {
    // title and parent_id are ordinary, policy-gated mutation fields the hub's
    // action_task route already applies (task #1300 part 2) — they were only
    // missing from this typed contract.
    assert.equal(
      PROTOCOL_SCHEMAS.TaskUpdateRequest.safeParse({ id: 1, title: "Renamed" }).success,
      true,
    );
    assert.equal(
      PROTOCOL_SCHEMAS.TaskUpdateRequest.safeParse({ id: 1, title: " \t " }).success,
      false,
    );
    const nonblankTitle = PROTOCOL_SCHEMAS.TaskUpdateRequest.parse({
      id: 1,
      title: "  Renamed  ",
    });
    assert.equal(nonblankTitle.title, "  Renamed  ");
    assert.equal(PROTOCOL_SCHEMAS.TaskCreateRequest.safeParse({ title: " \t " }).success, false);
    const updateSchema = JSON.parse(
      readFileSync(join(schemaDir, "TaskUpdateRequest.json"), "utf8"),
    );
    assert.equal(updateSchema.properties.title.pattern, "\\S");
    assert.equal(
      PROTOCOL_SCHEMAS.TaskUpdateRequest.safeParse({ id: 1, parent_id: 42 }).success,
      true,
    );
    // 0 is the hub's own "detach from parent" sentinel (tasks.py task_update).
    assert.equal(
      PROTOCOL_SCHEMAS.TaskUpdateRequest.safeParse({ id: 1, parent_id: 0 }).success,
      true,
    );
    assert.equal(
      PROTOCOL_SCHEMAS.TaskUpdateRequest.safeParse({ id: 1, parent_id: -1 }).success,
      false,
    );
    assert.equal(PROTOCOL_SCHEMAS.TaskUpdateRequest.safeParse({ id: 1, title: "" }).success, false);

    // `untrusted` is a hub-computed provenance/audit flag consumed by the
    // executor trust-ladder gate, not caller-settable data — a typed client
    // must not be able to launder it via a write. Deliberately excluded from
    // both TaskCreateRequest and TaskUpdateRequest.
    assert.equal(
      PROTOCOL_SCHEMAS.TaskCreateRequest.safeParse({ title: "x", untrusted: false }).success,
      false,
    );
    assert.equal(
      PROTOCOL_SCHEMAS.TaskUpdateRequest.safeParse({ id: 1, untrusted: false }).success,
      false,
    );
  });

  it("parses PolicyDecision and PathResolutionResult", () => {
    const decision = PolicyDecision.parse({
      allow: false,
      reason: "path escapes workspace",
      rule: "resolve_in_workspace",
      tool: "fs_read",
    });
    assert.equal(decision.allow, false);

    const path = PathResolutionResult.parse({
      requested: "../etc/passwd",
      resolved: "/home/you/dev/agent/etc/passwd",
      inside_root: false,
      root: "/home/you/dev/agent",
      reason: "escapes workspace root",
    });
    assert.equal(path.inside_root, false);
  });

  it("exports JSON Schema for every catalog entry", () => {
    const files = readdirSync(schemaDir).filter((f) => f.endsWith(".json"));
    assert.ok(files.includes("index.json"));
    for (const name of Object.keys(PROTOCOL_SCHEMAS)) {
      assert.ok(files.includes(`${name}.json`), `missing ${name}.json`);
      const doc = JSON.parse(readFileSync(join(schemaDir, `${name}.json`), "utf8"));
      assert.ok(doc.type || doc.$ref || doc.anyOf || doc.oneOf || doc.properties);
    }
  });

  it("round-trips JSON Schema export for Task via Zod", () => {
    const jsonSchema = z.toJSONSchema(Task, { target: "draft-07", io: "input" });
    assert.equal(jsonSchema.type, "object");
    assert.ok(jsonSchema.properties?.id);
  });

  it("accepts the shared valid fixture for every protocol schema", () => {
    for (const [name, schema] of Object.entries(PROTOCOL_SCHEMAS)) {
      const result = schema.safeParse(fixtures[name]);
      assert.equal(result.success, true, `${name} fixture should validate`);
    }
  });

  it("rejects null for every field with a non-null Zod default", () => {
    for (const [name, field] of nonNullableDefaultFields) {
      const schema = PROTOCOL_SCHEMAS[name as keyof typeof PROTOCOL_SCHEMAS];
      const input = { ...(fixtures[name] as Record<string, unknown>), [field]: null };
      assert.equal(schema.safeParse(input).success, false, `${name}.${field} should reject null`);
    }
  });

  it("rejects invalid values for representative required fields", () => {
    assert.equal(Task.safeParse({ ...fixtures.Task as object, title: "" }).success, false);
    assert.equal(PROTOCOL_SCHEMAS.Tool.safeParse({ ...fixtures.Tool as object, name: "" }).success, false);
    assert.equal(
      PolicyDecision.safeParse({ ...fixtures.PolicyDecision as object, reason: "" }).success,
      false,
    );
    assert.equal(
      DispatchRequest.safeParse({ ...fixtures.DispatchRequest as object, prompt: "" }).success,
      false,
    );
    const policy = fixtures.PolicyDecision as { principal: Record<string, unknown> };
    assert.equal(
      PolicyDecision.safeParse({
        ...policy,
        principal: { ...policy.principal, kind: null },
      }).success,
      false,
    );
  });
});
