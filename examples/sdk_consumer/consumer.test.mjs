/**
 * Hermetic test for sdk_consumer — the whole governed flow against a mock
 * MCP wire, with the real UnifiedAgentClient transport in the middle so the
 * JSON-RPC envelope, session lifecycle, and tool-error unwrap all get
 * exercised exactly as they would against the live hub.
 *
 * Run: node --test consumer.test.mjs   (or `npm test`)
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { UnifiedAgentClient } from "@unified-agent/client";

import { runConsumer } from "./consumer.mjs";

const NOW = "2026-09-19T00:00:00+00:00";

function jsonResponse(headers, body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  };
}

function toolResult(payload) {
  return { content: [{ type: "text", text: JSON.stringify(payload) }] };
}

function toolError(message) {
  return { isError: true, content: [{ type: "text", text: message }] };
}

function approvalRow(id, status, extra = {}) {
  return {
    id,
    tool: "horizon2_package_register",
    payload: { operation: "register" },
    requested_by: "external_consumer_demo",
    status,
    created_at: NOW,
    updated_at: NOW,
    resolved_at: null,
    ...extra,
  };
}

/**
 * Mock hub: initialize + notifications + tools/list handled generically,
 * tools/call dispatched through `handlers` (name -> payload | fn(args)).
 * `state` lets tests script stateful sequences (pending -> approved).
 */
function mockHub({ handlers, state = {} }) {
  const sessionId = "sess-mock";
  return async (_url, init) => {
    const body = JSON.parse(init?.body ?? "{}");
    const headers = {
      get: (name) =>
        name.toLowerCase() === "mcp-session-id"
          ? sessionId
          : name.toLowerCase() === "content-type"
            ? "application/json"
            : null,
    };
    if (body.method === "initialize") {
      return jsonResponse(headers, {
        jsonrpc: "2.0",
        id: body.id,
        result: { protocolVersion: "2025-11-25", capabilities: {} },
      });
    }
    if (body.method?.startsWith("notifications/")) {
      return jsonResponse({ get: () => null }, "", 202);
    }
    if (body.method === "tools/list") {
      return jsonResponse(headers, {
        jsonrpc: "2.0",
        id: body.id,
        result: { tools: Object.keys(handlers).map((name) => ({ name })) },
      });
    }
    if (body.method === "tools/call") {
      const name = body.params?.name;
      const args = body.params?.arguments ?? {};
      const handler = handlers[name];
      if (!handler) {
        return jsonResponse(headers, {
          jsonrpc: "2.0",
          id: body.id,
          result: toolError(`unknown tool ${name}`),
        });
      }
      const produced = typeof handler === "function" ? handler(args, state) : handler;
      return jsonResponse(headers, { jsonrpc: "2.0", id: body.id, result: produced });
    }
    return jsonResponse(headers, { jsonrpc: "2.0", id: body.id, result: {} });
  };
}

function evidenceReportPayload(identity) {
  return {
    report_type: "user_attributable_evidence",
    schema_version: "1.0",
    generated_at: NOW,
    chain_integrity: { status: "valid", verified: true, total_verified: 7 },
    filter: { agent: identity },
    total_records: 1,
    records: [
      {
        task_id: 9001,
        agent: identity,
        action: { approvals: [] },
        proof: { event_hashes: ["abc"] },
      },
    ],
    project_chain: null,
  };
}

function happyHub(state) {
  const server = {
    server_id: "sdk-consumer-granola-test",
    connector: "granola",
    provenance: "sha256:abc123",
    scopes: ["read"],
    reviewed_by: "external_consumer_demo",
    review_ref: "task:1137-sdk-consumer-proof",
    revoked_at: null,
    created_at: NOW,
    active: true,
  };
  return mockHub({
    state,
    handlers: {
      profile_status: {
        profile: "developer",
        identity: "external_consumer_demo",
        session_scoped: true,
      },
      task_add: () => ({ id: 9001 }),
      task_update: () => ({ id: 9001, status: "done" }),
      event_log_query: (args) => {
        if (args.channels?.includes("horizon2.connector.*")) {
          state.lastEventSince = args.since ?? state.lastEventSince;
          return {
            events: [
              {
                ulid: "01JEVT1",
                channel: "horizon2.connector.registered",
                payload: { server_id: "sdk-consumer-granola-test" },
                originator: "external_consumer_demo",
                episode_id: null,
                causal_parent_id: null,
                created_at: NOW,
                entry_hash: "abc",
              },
            ],
            count: 1,
            cursor: "01JEVT1",
            source_event_count: 1,
            last_event_at: NOW,
          };
        }
        return {
          events: [],
          count: 0,
          cursor: null,
          source_event_count: 0,
          last_event_at: null,
        };
      },
      event_log_verify: { status: "valid", verified: true, total_verified: 7 },
      evidence_report: () => evidenceReportPayload("external_consumer_demo"),
      horizon2_server_list: { servers: [server], count: 1 },
      horizon2_package_register: () => {
        state.calls = (state.calls ?? 0) + 1;
        if (state.calls === 1) {
          return toolError(
            "'horizon2_package_register' requires approval (global allowlist): " +
              "request 77 is queued. A human resolves it with approval_resolve(77, 'approve'|'deny'); " +
              "retry the identical call after approval to consume the one-shot pass.",
          );
        }
        return toolResult({ server, approval_id: 77, event_id: "01JEVT1" });
      },
      approval_pending: () => {
        state.polls = (state.polls ?? 0) + 1;
        return toolResult(
          state.polls === 1
            ? {
                pending: [approvalRow(77, "pending")],
                approved_unconsumed: [],
                expired_passes: [],
                gated_tools: ["horizon2_package_register"],
              }
            : {
                pending: [],
                approved_unconsumed: [
                  approvalRow(77, "approved", { resolved_by: "Operator", resolved_at: NOW }),
                ],
                expired_passes: [],
                gated_tools: ["horizon2_package_register"],
              },
        );
      },
    },
  });
}

describe("sdk_consumer governed flow", () => {
  it("proposes, waits for human approval, consumes the pass, exports evidence", async () => {
    const dir = mkdtempSync(join(tmpdir(), "sdk-consumer-"));
    const outPath = join(dir, "evidence.json");
    try {
      const state = {};
      const client = new UnifiedAgentClient({
        url: "http://hub.test/mcp",
        bearerToken: "tok",
        fetch: happyHub(state),
      });
      const { exitCode } = await runConsumer({
        client,
        package: "granola",
        serverId: "sdk-consumer-granola-test",
        out: outPath,
        pollSeconds: 0,
        waitSeconds: 30,
        logFn: () => {},
      });
      await client.close();

      assert.equal(exitCode, 0);
      assert.equal(state.calls, 2, "gated call retried exactly once after approval");
      assert.ok(state.polls >= 2, "approval queue polled until resolved");

      const evidence = JSON.parse(readFileSync(outPath, "utf8"));
      assert.equal(evidence.identity, "external_consumer_demo");
      assert.equal(evidence.governed_action.tool, "horizon2_package_register");
      assert.equal(evidence.governed_action.approval_id, 77);
      assert.equal(evidence.governed_action.outcome, "approved_executed");
      assert.equal(evidence.governed_server.server_id, "sdk-consumer-granola-test");
      assert.equal(evidence.governed_server.reviewed_by, "external_consumer_demo");
      assert.equal(evidence.event_log.verification.verified, true);
      assert.match(
        evidence.event_log.since_floor_ulid,
        /^[0-9A-HJKMNP-TV-Z]{10}0{16}$/,
        "run window is a synthesized ULID floor (timestamp prefix + zeroed randomness)",
      );
      assert.equal(
        evidence.event_log.events.events[0].channel,
        "horizon2.connector.registered",
      );
      assert.equal(evidence.governed_event.payload.server_id, "sdk-consumer-granola-test");
      assert.equal(evidence.evidence_report.records[0].agent, "external_consumer_demo");
      assert.equal(evidence.tracking_task.id, 9001);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("refuses to run as an Operator identity", async () => {
    const client = new UnifiedAgentClient({
      url: "http://hub.test/mcp",
      bearerToken: "tok",
      fetch: mockHub({
        handlers: {
          profile_status: { profile: "operator", identity: "Desktop_Person" },
        },
      }),
    });
    await assert.rejects(
      runConsumer({ client, logFn: () => {} }),
      /non-Operator/,
    );
    await client.close();
  });

  it("surfaces a denial as a governed outcome and still exports evidence", async () => {
    const dir = mkdtempSync(join(tmpdir(), "sdk-consumer-deny-"));
    const outPath = join(dir, "evidence.json");
    try {
      const state = {};
      const hub = happyHub(state);
      const wrapped = async (url, init) => {
        const body = JSON.parse(init?.body ?? "{}");
        if (body.method === "tools/call" && body.params?.name === "approval_pending") {
          return jsonResponse(
            { get: () => "application/json" },
            {
              jsonrpc: "2.0",
              id: body.id,
              result: toolResult({
                pending: [],
                approved_unconsumed: [],
                expired_passes: [],
                gated_tools: ["horizon2_package_register"],
              }),
            },
          );
        }
        if (body.method === "tools/call" && body.params?.name === "horizon2_package_register") {
          state.calls = (state.calls ?? 0) + 1;
          return jsonResponse(
            { get: () => "application/json" },
            {
              jsonrpc: "2.0",
              id: body.id,
              result:
                state.calls === 1
                  ? toolError("request 77 is queued")
                  : toolError("approval request 77 for 'horizon2_package_register' was DENIED: no"),
            },
          );
        }
        return hub(url, init);
      };
      const client = new UnifiedAgentClient({
        url: "http://hub.test/mcp",
        bearerToken: "tok",
        fetch: wrapped,
      });
      const { exitCode } = await runConsumer({
        client,
        package: "granola",
        serverId: "sdk-consumer-granola-test",
        out: outPath,
        pollSeconds: 0,
        waitSeconds: 30,
        logFn: () => {},
      });
      await client.close();
      assert.equal(exitCode, 3);
      const evidence = JSON.parse(readFileSync(outPath, "utf8"));
      assert.equal(evidence.governed_action.outcome, "denied");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
