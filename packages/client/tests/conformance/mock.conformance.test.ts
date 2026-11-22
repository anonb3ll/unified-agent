/**
 * SDK conformance suite — deterministic mock leg (task #937, sdk-lane).
 *
 * Productizes the ad hoc live-hub probes that verified past SDK releases
 * (v0.2, v0.2.1) into a repeatable, hermetic suite. This leg never touches a
 * network: every JSON-RPC response is canned via `mockHub` (support.ts), so
 * it runs in every CI job on every push with no credentials.
 *
 * Coverage (minimum required by task #937):
 *   ping, tools/list, task_list (lineage + waiting_for_review), memory_search,
 *   profile_status, system_doctor, mcp_health_telemetry, approval_pending,
 *   session open/close. Task #1021 added the read-side vault surfaces:
 *   vault_search, vault_read, vault_query. Task #944 added the read-only
 *   dispatch_delivery_receipt surface.
 *
 * The companion `live.conformance.test.ts` re-runs an equivalent surface
 * against a real, explicitly-configured hub — see docs/compatibility-matrix.md
 * for what "conformant" means across hub/client version pairs.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { UnifiedAgentClient } from "../../src/client.ts";
import { ControlCenterClient } from "../../src/control-center.ts";
import { HubValidationError } from "../../src/errors.ts";
import { HubTransport } from "../../src/hub.ts";
import { mockControlCenter, mockHub } from "./support.ts";

function client(tools: Record<string, unknown>): UnifiedAgentClient {
  return new UnifiedAgentClient({
    url: "http://hub.invalid/mcp",
    bearerToken: "conformance-fixture-token",
    fetch: mockHub({ tools }),
  });
}

describe("SDK conformance — mock hub (deterministic)", () => {
  it("HTTP writes: task, approval, and evidence use hub-attributed contracts", async () => {
    const calls: Array<{ url: string; method?: string; headers?: Record<string, string>; body?: string }> = [];
    const control = new ControlCenterClient({
      baseUrl: "http://hub.invalid",
      bearerToken: "conformance-fixture-token",
      fetch: mockControlCenter({
        calls,
        writes: {
          "/api/tasks": { status: 201, payload: { id: 936 } },
          "/api/action/task": { status: 200, payload: { id: 936, status: "doing" } },
          "/api/approvals/request": {
            status: 202,
            payload: { approval_id: 17, status: "pending", tool: "task_update", payload: { id: 936 } },
          },
          "/api/action/approval": { status: 200, payload: { id: 17, status: "denied" } },
          "/api/evidence": { status: 201, payload: { event_ulid: "01JCONFORMANCE", inserted: true } },
        },
      }),
    });

    const task = await control.createTask({ title: "Conformance task", project: "unified-agent" });
    await control.updateTask({ id: task.id, status: "doing" });
    const approval = await control.requestApproval({ tool: "task_update", payload: { id: task.id } });
    const resolved = await control.resolveApproval({ id: approval.approval_id, decision: "deny" });
    const evidence = await control.createEvidence({ channel: "sdk.task", payload: { task_id: task.id } });

    assert.equal(approval.status, "pending");
    assert.equal(resolved.status, "denied");
    assert.equal(evidence.inserted, true);
    assert.deepEqual(calls.map(({ url, method }) => [new URL(url).pathname, method]), [
      ["/api/tasks", "POST"],
      ["/api/action/task", "POST"],
      ["/api/approvals/request", "POST"],
      ["/api/action/approval", "POST"],
      ["/api/evidence", "POST"],
    ]);
    for (const call of calls) {
      assert.equal(call.headers?.Authorization, "Bearer conformance-fixture-token");
      const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
      for (const field of ["actor", "identity", "originator", "resolved_by"]) {
        assert.equal(field in body, false, `caller controls forbidden field ${field}`);
      }
    }
    const approvalRequest = JSON.parse(calls[2]!.body ?? "{}") as Record<string, unknown>;
    assert.equal("decision" in approvalRequest, false, "request path cannot approve or deny");
  });

  it("ping: bearer token + session handshake succeed", async () => {
    const c = client({});
    await assert.doesNotReject(() => c.ping());
  });

  it("tools_list: hub tool catalog is reachable and non-empty", async () => {
    const transport = new HubTransport({
      url: "http://hub.invalid/mcp",
      bearerToken: "conformance-fixture-token",
      fetch: mockHub({
        tools: {},
        toolList: [
          { name: "task_list", description: "list tasks" },
          { name: "memory_search", description: "search memory" },
        ],
      }),
    });
    const result = (await transport.listTools()) as { tools?: Array<{ name: string }> };
    assert.ok(Array.isArray(result.tools) && result.tools.length > 0);
    const names = result.tools!.map((t) => t.name);
    assert.ok(names.includes("task_list"));
    assert.ok(names.includes("memory_search"));
  });

  it("capability_catalog: live MCP inventory remains typed and read-only", async () => {
    const c = client({
      capability_catalog: {
        read_only: true,
        install_is_explicit: true,
        approval_gated: true,
        versions: { unified_agent: "0.1.0" },
        agents: [],
        mcp_tools: ["capability_catalog"],
        mcp_client_matrix: {},
        clis: [],
        skills: [],
        workflows: [],
        health: {},
        boundaries: {},
        prerequisites: null,
      },
    });
    const inventory = await c.capabilityCatalog();
    assert.equal(inventory.read_only, true);
    assert.deepEqual(inventory.mcp_tools, ["capability_catalog"]);
  });

  it("capability_catalog: prerequisite discovery requires paired arguments", async () => {
    const c = client({ capability_catalog: {} });
    await assert.rejects(
      () => c.capabilityCatalog({ task_id: 1 }),
      /requires task_id and agent_label together/,
    );
  });

  it("task_list: rows round-trip lineage and waiting_for_review", async () => {
    const c = client({
      task_list: {
        tasks: [
          {
            id: 936,
            title: "Fix stale WebUI fallback tests",
            status: "waiting_review",
            priority: "high",
            parent_id: 935,
            lineage: [935],
            waiting_for_review: 1,
            project: "unified-agent",
            created_at: "2026-09-11T03:56:13+00:00",
            updated_at: "2026-09-11T04:51:27+00:00",
          },
        ],
      },
    });
    const tasks = await c.taskList({ status: "waiting_review" });
    assert.equal(tasks.length, 1);
    assert.deepEqual(tasks[0]!.lineage, [935]);
    assert.equal(tasks[0]!.waiting_for_review, 1);
  });

  it("memory_search: hits validate against the full hit schema", async () => {
    const c = client({
      memory_search: {
        results: [
          {
            id: "note-1",
            title: "conformance fixture",
            tags: ["sdk-lane"],
            author: "Desktop_Agent1",
            updated_at: "2026-09-11T00:00:00+00:00",
            salience: 0.5,
            confidence: null,
            last_accessed: null,
            access_count: 0,
            superseded_by: null,
            source_host: null,
            expires_at: null,
            feedback_helpful: 0,
            feedback_not_helpful: 0,
            feedback_stale: 0,
            feedback_wrong: 0,
            snippet: "…",
            score: 1.0,
            similarity: null,
            combined_score: 1.0,
            recency: 1.0,
            explanation: {
              why: ["lexical match"],
              lexical: 1.0,
              semantic: null,
              recency: 1.0,
              salience: 0.5,
              access: 0,
              feedback: 0,
              entity: 0,
              host: null,
              superseded: false,
              expired: false,
              entity_matches: [],
              matched_tags: ["sdk-lane"],
            },
          },
        ],
      },
    });
    const hits = await c.memorySearch({ query: "conformance" });
    assert.equal(hits.length, 1);
    assert.equal(hits[0]!.id, "note-1");
  });

  it("profile_status: required fields plus passthrough extras survive", async () => {
    const c = client({
      profile_status: {
        profile: "default",
        identity: "Desktop_Agent1",
        session_scoped: false,
        extra_field_from_a_newer_hub: "must not be dropped",
      },
    });
    const status = await c.profileStatus();
    assert.equal(status.profile, "default");
    assert.equal((status as Record<string, unknown>).extra_field_from_a_newer_hub, "must not be dropped");
  });

  it("system_doctor: capability-check report is accepted", async () => {
    const c = client({
      system_doctor: {
        as_of: "2026-09-11T00:00:00+00:00",
        checks: [{ capability: "active_executor_service", status: "live" }],
        host: "example-host.local",
        hosts: [],
        summary: { live: 1, dormant: 0, misconfigured: 0, unknown: 0 },
      },
    });
    const report = await c.systemDoctor();
    assert.equal(report.summary.live, 1);
    assert.equal(report.summary.degraded, 0);
    assert.equal(report.checks[0]!.capability, "active_executor_service");
  });

  it("mcp_health_telemetry: status is required and typed", async () => {
    const c = client({ mcp_health_telemetry: { status: "healthy", protocol: "2025-06-18" } });
    const telemetry = await c.mcpHealthTelemetry();
    assert.equal(telemetry.status, "healthy");
  });

  it("approval_pending: full surface (pending/approved_unconsumed/expired_passes/gated_tools) validates", async () => {
    const c = client({
      approval_pending: {
        pending: [
          {
            id: 1,
            tool: "fleet_control_action",
            payload: { action: "restart", host: "mac" },
            requested_by: "Desktop_Agent1",
            status: "pending",
            created_at: "2026-09-11T00:00:00+00:00",
            updated_at: "2026-09-11T00:00:00+00:00",
          },
        ],
        approved_unconsumed: [],
        expired_passes: [],
        gated_tools: ["fleet_control_action"],
        pass_ttl_hours: 24,
      },
    });
    const result = await c.approvalPending();
    assert.equal(result.pending.length, 1);
    assert.equal(result.pending[0]!.status, "pending");
    assert.deepEqual(result.gated_tools, ["fleet_control_action"]);
    assert.equal(result.pass_ttl_hours, 24);
  });

  it("session open/close: handshake mints an id, close tears it down", async () => {
    const transport = new HubTransport({
      url: "http://hub.invalid/mcp",
      bearerToken: "conformance-fixture-token",
      fetch: mockHub({ tools: {} }),
    });
    assert.equal(transport.mcpSessionId, null);
    await transport.ensureSession();
    assert.equal(transport.mcpSessionId, "sess-conformance");
    await transport.close();
    assert.equal(transport.mcpSessionId, null);
  });

  it("vault_search: matches and truncated flag round-trip", async () => {
    const c = client({
      vault_search: {
        matches: [{ path: "Wiki/Claude Code.md", line_no: 12, line: "See also [[MCP]]" }],
        truncated: false,
      },
    });
    const result = await c.vaultSearch({ pattern: "MCP" });
    assert.equal(result.matches.length, 1);
    assert.equal(result.matches[0]!.path, "Wiki/Claude Code.md");
    assert.equal(result.truncated, false);
  });

  it("vault_read: content, total_lines, and truncated survive", async () => {
    const c = client({
      vault_read: {
        path: "Wiki/Claude Code.md",
        content: "# Claude Code\n\nSome content.",
        total_lines: 3,
        truncated: false,
      },
    });
    const result = await c.vaultRead({ path: "Wiki/Claude Code.md" });
    assert.equal(result.path, "Wiki/Claude Code.md");
    assert.match(result.content, /Claude Code/);
    assert.equal(result.total_lines, 3);
  });

  it("vault_query: PPR results and graph stats validate", async () => {
    const c = client({
      vault_query: {
        results: [
          {
            path: "Wiki/MCP.md",
            title: "MCP",
            score: 0.9,
            lexical: 1.0,
            pagerank: 0.8,
            snippet: "MCP is a protocol",
            links_out: 2,
            links_in: 3,
          },
        ],
        graph: { notes: 42, edges: 17, seeds: 1 },
      },
    });
    const result = await c.vaultQuery({ query: "MCP" });
    assert.equal(result.results.length, 1);
    assert.equal(result.results[0]!.path, "Wiki/MCP.md");
    assert.equal(result.graph.notes, 42);
  });

  it("actionable failure: a malformed typed response raises HubValidationError naming the tool", async () => {
    const c = client({
      // Missing every required Task field — must fail closed, not silently coerce.
      task_list: { tasks: [{ id: "not-a-number" }] },
    });
    await assert.rejects(() => c.taskList(), (err: unknown) => {
      assert.ok(err instanceof HubValidationError);
      assert.match(err.message, /task_list/);
      return true;
    });
  });

  it("event_log_query: rows round-trip with cursor + liveness counters", async () => {
    const c = client({
      event_log_query: {
        events: [
          {
            ulid: "01J8ZQZQZQZQZQZQZQZQZQZQZQ",
            channel: "agent.task.updated",
            payload: { kind: "task", ref_id: 1020, status: "doing" },
            originator: "Desktop_Agent1",
            episode_id: "ep-1",
            causal_parent_id: null,
            created_at: "2026-09-15T00:00:00+00:00",
          },
        ],
        count: 1,
        cursor: "01J8ZQZQZQZQZQZQZQZQZQZQZQ",
        source_event_count: 1,
        last_event_at: "2026-09-15T00:00:00+00:00",
      },
    });
    const result = await c.eventLogQuery({ channels: ["agent.task.*"], limit: 10 });
    assert.equal(result.count, 1);
    assert.equal(result.events[0]!.channel, "agent.task.updated");
    assert.equal(result.cursor, "01J8ZQZQZQZQZQZQZQZQZQZQZQ");
    assert.equal(result.source_event_count, 1);
  });

  it("event_log_verify: valid chain report validates required fields", async () => {
    const c = client({
      event_log_verify: {
        status: "valid",
        verified: true,
        total_verified: 42,
        head_hash: "abc123",
        head_ulid: "01J8ZQZQZQZQZQZQZQZQZQZQZQ",
        unhashed_skipped: 0,
      },
    });
    const report = await c.eventLogVerify();
    assert.equal(report.status, "valid");
    assert.equal(report.verified, true);
    assert.equal(report.total_verified, 42);
  });

  it("event_log_verify: tampered chain surfaces tampered_at detail", async () => {
    const c = client({
      event_log_verify: {
        status: "tampered",
        verified: false,
        total_verified: 10,
        tampered_at: {
          rowid: 11,
          ulid: "01J8ZQZQZQZQZQZQZQZQZQZQZQ",
          channel: "agent.task.updated",
          reason: "prev_hash_mismatch",
          expected_prev_hash: "abc",
          actual_prev_hash: "def",
        },
      },
    });
    const report = await c.eventLogVerify();
    assert.equal(report.status, "tampered");
    assert.equal(report.tampered_at?.reason, "prev_hash_mismatch");
  });

  it("dispatch_delivery_receipt: full receipt validates including transitions + executor_auth", async () => {
    const c = client({
      dispatch_delivery_receipt: {
        schema_version: "1.0",
        task_id: 944,
        run_id: "run-944",
        delegation_id: null,
        executor_identity: "Desktop_Agent7",
        lease: { owner: null, expires_at: null, active: false },
        retry_count: null,
        event_cursor: "01J9BBBBBBBBBBBBBBBBBBBBBB",
        policy_outcome: "approved",
        approval_outcome: "not_requested",
        evidence_ref: null,
        final_task_state: "doing",
        run_state: "finished",
        terminal: true,
        transitions: [
          {
            ulid: "01J9BBBBBBBBBBBBBBBBBBBBBB",
            state: "claimed",
            timestamp: "2026-09-21T09:00:00+00:00",
            actor: "Desktop_Agent7",
            failure_reason: null,
            evidence_ref: null,
            retry_count: 0,
          },
        ],
        executor_auth: {
          status: "LIVE",
          authenticated: true,
          probe_process: "separate",
        },
      },
    });
    const receipt = await c.dispatchDeliveryReceipt({ run_id: "run-944" });
    assert.equal(receipt.run_id, "run-944");
    assert.equal(receipt.terminal, true);
    assert.equal(receipt.transitions.length, 1);
    assert.equal(receipt.executor_auth?.status, "LIVE");
  });

  it("dispatch_delivery_receipt: history-shaped receipt (null executor_auth, empty transitions) validates", async () => {
    const c = client({
      dispatch_delivery_receipt: {
        schema_version: "1.0",
        task_id: 1,
        run_id: "run-old",
        delegation_id: 7,
        executor_identity: "server_agent2",
        lease: { owner: "server_agent2", expires_at: "2026-09-01T00:00:00+00:00", active: false },
        retry_count: 2,
        event_cursor: null,
        policy_outcome: "denied",
        approval_outcome: "denied",
        evidence_ref: null,
        final_task_state: "blocked",
        run_state: "denied",
        terminal: true,
        transitions: [],
        executor_auth: null,
      },
    });
    const receipt = await c.dispatchDeliveryReceipt({ run_id: "run-old" });
    assert.equal(receipt.executor_auth, null);
    assert.equal(receipt.policy_outcome, "denied");
  });

  it("dispatch_delivery_receipt: malformed receipt fails closed with HubValidationError", async () => {
    const c = client({
      dispatch_delivery_receipt: {
        schema_version: "1.0",
        task_id: 944,
        run_id: "run-bad",
        // terminal missing — must fail closed, not silently coerce.
      },
    });
    await assert.rejects(
      () => c.dispatchDeliveryReceipt({ run_id: "run-bad" }),
      (err: unknown) => {
        assert.ok(err instanceof HubValidationError);
        assert.match(err.message, /dispatch_delivery_receipt/);
        return true;
      },
    );
  });

  it("evidence_report: 5-link accountability chain plus project_chain validates", async () => {
    const c = client({
      evidence_report: {
        report_type: "user_attributable_evidence",
        schema_version: "1.0",
        generated_at: "2026-09-15T00:00:00+00:00",
        chain_integrity: {
          status: "valid",
          verified: true,
          head_hash: "abc123",
          total_verified: 5,
          unhashed_skipped: 0,
        },
        filter: { task_id: 1020, project_id: null, since: null, until: null },
        total_records: 1,
        records: [
          {
            human: "Operator",
            device: "Desktop_Agent1",
            agent: "Desktop_Agent1",
            action: "task_update",
            proof: "https://github.com/anonb3ll/unified-agent-sdk/pull/1",
          },
        ],
        project_chain: {
          project_id: 7,
          status: "doing",
          task_ids: [1020],
          done_task_ids: [],
          evidence: [],
          merged_prs: [],
          last_merge: null,
        },
      },
    });
    const report = await c.evidenceReport({ task_id: 1020 });
    assert.equal(report.total_records, 1);
    assert.equal(report.records[0]!.agent, "Desktop_Agent1");
    assert.equal(report.project_chain?.project_id, 7);
  });
});
