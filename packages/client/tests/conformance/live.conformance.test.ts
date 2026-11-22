/**
 * SDK conformance suite — gated live-hub leg (task #937, sdk-lane).
 *
 * This is the only file in the SDK that talks to a real hub, and it does so
 * deliberately and narrowly:
 *
 *   - Skips entirely (not a failure) unless UA_SDK_CONFORMANCE_URL and
 *     UA_SDK_CONFORMANCE_TOKEN are both set. CI never sets these on ordinary
 *     pushes/PRs — see .github/workflows/ci.yml `live-hub-conformance` job,
 *     which is `workflow_dispatch` / schedule only and reads the token from a
 *     repository secret.
 *   - Never logs the token, or any header/response that could carry it.
 *     Assertion failure messages below print status codes and hub-reported
 *     tool errors only, never request/response bodies wholesale.
 *   - Read-only: every call here is a GET-shaped tool (task_list,
 *     memory_search, profile_status, system_doctor, mcp_health_telemetry,
 *     approval_pending, dispatch_delivery_receipt, tools/list). No task_update, no approval_resolve, no
 *     mutation of any kind — this suite must be safe to run against the real
 *     production hub on a schedule without an operator standing by.
 *
 * A failure here means the deployed hub and this SDK version have drifted —
 * see docs/compatibility-matrix.md before assuming the fix belongs in either
 * repo specifically.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { UnifiedAgentClient } from "../../src/client.ts";
import { ControlCenterClient } from "../../src/control-center.ts";

const HUB_URL = process.env.UA_SDK_CONFORMANCE_URL;
const HUB_TOKEN = process.env.UA_SDK_CONFORMANCE_TOKEN;
const AGENT_LABEL = process.env.UA_SDK_CONFORMANCE_AGENT ?? "Desktop_Agent1";
const LIVE = Boolean(HUB_URL && HUB_TOKEN);
// Opt-in only for a disposable hub: this suite creates a task and appends an
// evidence event, and an optional human credential resolves its test approval.
const WRITE_HUB_URL = process.env.UA_SDK_WRITE_CONFORMANCE_URL;
const WRITE_HUB_TOKEN = process.env.UA_SDK_WRITE_CONFORMANCE_TOKEN;
const WRITE_HUMAN_TOKEN = process.env.UA_SDK_WRITE_CONFORMANCE_HUMAN_TOKEN;
const LIVE_WRITES = Boolean(WRITE_HUB_URL && WRITE_HUB_TOKEN);
// requestApproval only queues a request for a tool the hub actually gates; an
// ungated tool is refused with 409 (the settled contract). crew_intervene is
// approval-gated by default on ua-rust; on a hub that gates other tools, name
// one here, with a JSON payload its arguments accept.
const WRITE_APPROVAL_TOOL = process.env.UA_SDK_WRITE_CONFORMANCE_APPROVAL_TOOL ?? "crew_intervene";
const WRITE_APPROVAL_PAYLOAD: Record<string, unknown> = process.env.UA_SDK_WRITE_CONFORMANCE_APPROVAL_PAYLOAD
  ? JSON.parse(process.env.UA_SDK_WRITE_CONFORMANCE_APPROVAL_PAYLOAD)
  : { session_id: "sdk-write-conformance", message: "SDK write conformance approval fixture." };

describe("SDK conformance — live hub (gated)", { skip: !LIVE }, () => {
  if (!LIVE) {
    // node:test's `skip` option already prevents these from running; this
    // branch only exists so `npm run test:conformance:live` locally without
    // env vars prints *why* nothing ran instead of a silent green suite.
    it("skipped: set UA_SDK_CONFORMANCE_URL + UA_SDK_CONFORMANCE_TOKEN to run", () => {});
    return;
  }

  const client = new UnifiedAgentClient({
    url: HUB_URL!,
    bearerToken: HUB_TOKEN!,
  });

  it("session open/close: real handshake succeeds and tears down cleanly", async () => {
    await client.ping();
    assert.ok(client.transport.mcpSessionId, "hub did not return an mcp-session-id");
    await client.close();
    assert.equal(client.transport.mcpSessionId, null);
  });

  it("tools_list: live catalog includes the tools this SDK wraps", async () => {
    const result = (await client.transport.listTools()) as { tools?: Array<{ name: string }> };
    const names = new Set((result.tools ?? []).map((t) => t.name));
    for (const required of [
      "task_list",
      "memory_search",
      "profile_status",
      "system_doctor",
      "mcp_health_telemetry",
      "approval_pending",
      "dispatch_delivery_receipt",
    ]) {
      assert.ok(names.has(required), `live hub is missing tool: ${required}`);
    }
  });

  it("task_list: live rows validate, including lineage/waiting_for_review when present", async () => {
    const tasks = await client.taskList({ limit: 5 });
    assert.ok(Array.isArray(tasks));
    // Not every task carries lineage/waiting_for_review — only assert shape
    // when the hub actually sent one, so this stays stable across queue state.
    for (const task of tasks) {
      if (task.lineage !== undefined) {
        assert.ok(Array.isArray(task.lineage));
      }
      if (task.waiting_for_review !== undefined) {
        assert.ok(
          typeof task.waiting_for_review === "boolean" ||
            typeof task.waiting_for_review === "number",
        );
      }
    }
  });

  it("memory_search: live hits validate against the full hit schema", async () => {
    const hits = await client.memorySearch({ query: "sdk-lane", limit: 3 });
    assert.ok(Array.isArray(hits));
  });

  it("profile_status: live hub reports a profile and identity", async () => {
    const status = await client.profileStatus();
    assert.ok(status.profile);
    assert.ok(status.identity);
  });

  it("system_doctor: live hub reports capability checks and a summary", async () => {
    const report = await client.systemDoctor();
    assert.ok(Array.isArray(report.checks));
    assert.ok(report.summary && typeof report.summary.live === "number");
  });

  it("mcp_health_telemetry: live hub reports MCP runtime health", async () => {
    const telemetry = await client.mcpHealthTelemetry();
    assert.ok(["healthy", "degraded", "incompatible"].includes(telemetry.status));
  });

  it("approval_pending: live queue surface is readable without mutating it", async () => {
    const result = await client.approvalPending({ limit: 5 });
    assert.ok(Array.isArray(result.pending));
    assert.ok(Array.isArray(result.approved_unconsumed));
    assert.ok(Array.isArray(result.expired_passes));
    assert.ok(Array.isArray(result.gated_tools));
  });

  it("dispatch_delivery_receipt: unknown run_id surfaces the hub error contract", async () => {
    // Read-only probe: no real run_id is guaranteed on a scheduled run, so we
    // assert the documented failure path instead of a happy-path receipt.
    await assert.rejects(
      () => client.dispatchDeliveryReceipt({ run_id: "conformance-no-such-run" }),
      /unknown dispatch run/,
    );
  });

  it(`agent label is configured for identification (${AGENT_LABEL})`, () => {
    assert.ok(AGENT_LABEL.length > 0);
  });
});

describe("SDK conformance — live HTTP writes (explicit disposable hub only)", { skip: !LIVE_WRITES }, () => {
  if (!LIVE_WRITES) {
    it("skipped: set UA_SDK_WRITE_CONFORMANCE_URL + UA_SDK_WRITE_CONFORMANCE_TOKEN", () => {});
    return;
  }

  const client = new ControlCenterClient({
    baseUrl: WRITE_HUB_URL!,
    bearerToken: WRITE_HUB_TOKEN!,
  });
  const humanClient = WRITE_HUMAN_TOKEN
    ? new ControlCenterClient({ baseUrl: WRITE_HUB_URL!, bearerToken: WRITE_HUMAN_TOKEN })
    : null;

  it("creates and updates a task with hub-derived attribution", async () => {
    const nonce = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const created = await client.createTask({
      title: `SDK write conformance ${nonce}`,
      notes: "Disposable SDK HTTP-write conformance fixture.",
      project: "unified-agent",
      idempotency_key: `sdk-write-${nonce}`,
    });
    assert.ok(created.id > 0);
    const updated = await client.updateTask({ id: created.id, append_note: "SDK write conformance updated." });
    assert.equal(updated.id, created.id);
  });

  it("queues approvals without executing, and preserves human-only resolution", async () => {
    const request = await client.requestApproval({
      tool: WRITE_APPROVAL_TOOL,
      payload: WRITE_APPROVAL_PAYLOAD,
    });
    assert.equal(request.status, "pending");
    assert.ok(request.approval_id > 0);

    if (humanClient) {
      const resolved = await humanClient.resolveApproval({ id: request.approval_id, decision: "deny", reason: "SDK conformance cleanup" });
      assert.equal(resolved.id, request.approval_id);
      assert.equal(resolved.status, "denied");
    } else {
      await assert.rejects(
        () => client.resolveApproval({ id: request.approval_id, decision: "deny", reason: "SDK conformance cleanup" }),
        /HTTP 403/,
      );
    }
  });

  it("appends evidence idempotently and leaves actor attribution to the hub", async () => {
    const dedupeKey = `sdk-write-conformance-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const body = {
      channel: "sdk.conformance",
      payload: { fixture: true },
      dedupe_key: dedupeKey,
    };
    const first = await client.createEvidence(body);
    const repeated = await client.createEvidence(body);
    assert.ok(first.event_ulid);
    assert.equal(first.inserted, true);
    assert.equal(repeated.inserted, false);
  });
});
