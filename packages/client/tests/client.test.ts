import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { UnifiedAgentClient } from "../src/client.ts";
import type { FetchLike } from "../src/hub.ts";

function mockHub(handlers: Record<string, (args: Record<string, unknown>) => unknown>): FetchLike {
  let session = false;
  return async (_url, init) => {
    const body = init?.body ? JSON.parse(init.body) : {};
    if (body.method === "initialize") {
      session = true;
      return {
        ok: true,
        status: 200,
        headers: {
          get: (name: string) =>
            name.toLowerCase() === "mcp-session-id" ? "sess-client" : null,
        },
        text: async () =>
          JSON.stringify({ jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2025-06-18" } }),
      };
    }
    if (body.method === "notifications/initialized") {
      return {
        ok: true,
        status: 202,
        headers: { get: () => null },
        text: async () => "",
      };
    }
    if (body.method === "tools/list") {
      assert.equal(session, true);
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        text: async () =>
          JSON.stringify({ jsonrpc: "2.0", id: body.id, result: { tools: [] } }),
      };
    }
    if (body.method === "tools/call") {
      const name = body.params.name as string;
      const args = (body.params.arguments || {}) as Record<string, unknown>;
      const handler = handlers[name];
      if (!handler) throw new Error(`no handler for ${name}`);
      const payload = handler(args);
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        text: async () =>
          JSON.stringify({
            jsonrpc: "2.0",
            id: body.id,
            result: { structuredContent: payload },
          }),
      };
    }
    throw new Error(`unexpected ${body.method}`);
  };
}

describe("UnifiedAgentClient", () => {
  it("forwards event channel prefixes and channels together without rewriting them", async () => {
    const seen: Record<string, unknown>[] = [];
    const client = new UnifiedAgentClient({
      url: "http://hub.invalid/mcp",
      bearerToken: "fixture-token",
      fetch: mockHub({
        event_log_query: (args) => {
          seen.push(args);
          return { events: [], count: 0, cursor: null, source_event_count: 0, last_event_at: null };
        },
      }),
    });
    await client.eventLogQuery({
      channel_prefix: "agent.approval",
      channels: ["agent.task.*"],
      limit: 10,
    });
    await client.eventLogQuery();
    assert.deepEqual(seen, [
      { channel_prefix: "agent.approval", channels: ["agent.task.*"], limit: 10 },
      {},
    ]);
  });

  it("lists and updates tasks through the hub (mutations included)", async () => {
    const seen: Array<{ name: string; args: Record<string, unknown> }> = [];
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: mockHub({
        task_list: (args) => {
          seen.push({ name: "task_list", args });
          return {
            tasks: [
              {
                id: 559,
                title: "Slack chat",
                status: "doing",
                priority: "high",
                project: "unified-agent",
                created_at: "2026-08-24T00:00:00Z",
                updated_at: "2026-08-24T00:00:00Z",
              },
            ],
          };
        },
        task_update: (args) => {
          seen.push({ name: "task_update", args });
          return { id: args.id, status: args.status };
        },
        memory_save: (args) => {
          seen.push({ name: "memory_save", args });
          return { id: "abc", saved: true };
        },
        delegate: (args) => {
          seen.push({ name: "delegate", args });
          return { delegation_id: 9, task_id: 100 };
        },
        approval_resolve: (args) => {
          seen.push({ name: "approval_resolve", args });
          return { id: args.id, status: "approved" };
        },
      }),
    });

    await client.ping();
    const tasks = await client.taskList({ status: "doing" });
    assert.equal(tasks[0]?.id, 559);

    await client.taskUpdate({
      id: 559,
      status: "waiting_review",
      append_note: "[waiting-for: review]",
    });
    await client.memorySave({
      content: "client can mutate via hub",
      author: "Desktop_Agent4",
      tags: ["fact", "project:unified-agent"],
    });
    await client.delegate({
      task_spec: "### Objective\nShip client",
      from_agent: "Desktop_Agent4",
      to_agent: "Desktop_Agent2",
      callback: "none",
    });
    await client.approvalResolve({
      id: 3,
      decision: "approve",
      resolved_by: "Operator",
    });

    assert.deepEqual(
      seen.map((s) => s.name),
      ["task_list", "task_update", "memory_save", "delegate", "approval_resolve"],
    );
    assert.equal(seen[1]?.args.append_note, "[waiting-for: review]");
    assert.equal("claimed_by" in (seen[1]?.args ?? {}), false);
  });

  it("validates memory search hits as snippets returned by the hub", async () => {
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: mockHub({
        memory_search: () => ({
          results: [
            {
              id: "memory-1",
              title: "Protocol decision",
              tags: ["decision"],
              author: "Desktop_Agent2",
              updated_at: "2026-08-25T00:00:00Z",
              salience: 0.8,
              confidence: null,
              last_accessed: null,
              access_count: 1,
              superseded_by: null,
              source_host: "example-host.local",
              expires_at: null,
              feedback_helpful: 0,
              feedback_not_helpful: 0,
              feedback_stale: 0,
              feedback_wrong: 0,
              snippet: "The TypeScript protocol stays at the product boundary.",
              score: -0.5,
              similarity: null,
              combined_score: 1.2,
              recency: 1,
              explanation: {
                why: ["lexical"],
                lexical: 0.5,
                semantic: null,
                recency: 1,
                salience: 0.8,
                access: 0.2,
                feedback: 0,
                entity: 0,
                host: "example-host.local",
                superseded: false,
                expired: false,
                entity_matches: [],
                matched_tags: ["decision"],
              },
            },
          ],
        }),
      }),
    });

    const hits = await client.memorySearch({ query: "TypeScript protocol" });
    assert.equal(hits[0]?.snippet, "The TypeScript protocol stays at the product boundary.");
    assert.equal(hits[0]?.explanation.matched_tags[0], "decision");
  });

  it("reads a dispatch delivery receipt as a typed object", async () => {
    const seen: Array<{ name: string; args: Record<string, unknown> }> = [];
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: mockHub({
        dispatch_delivery_receipt: (args) => {
          seen.push({ name: "dispatch_delivery_receipt", args });
          return {
            schema_version: "1.0",
            task_id: 944,
            run_id: "run-abc",
            delegation_id: 12,
            executor_identity: "Desktop_Agent7",
            lease: { owner: "Desktop_Agent7", expires_at: "2026-09-21T12:00:00+00:00", active: false },
            retry_count: 1,
            event_cursor: "01J9AAAAAAAAAAAAAAAAAAAAAA",
            policy_outcome: "approved",
            approval_outcome: "approved",
            evidence_ref: "https://github.com/anonb3ll/unified-agent-sdk/pull/15",
            final_task_state: "done",
            run_state: "finished",
            terminal: true,
            transitions: [
              {
                ulid: "01J9AAAAAAAAAAAAAAAAAAAAAA",
                state: "finished",
                timestamp: "2026-09-21T11:00:00+00:00",
                actor: "Desktop_Agent7",
                failure_reason: null,
                evidence_ref: "sha:deadbeef",
                retry_count: 1,
              },
            ],
            executor_auth: {
              status: "LIVE",
              authenticated: true,
              probe_process: "separate",
            },
          };
        },
      }),
    });

    const receipt = await client.dispatchDeliveryReceipt({ run_id: "run-abc" });
    assert.equal(seen[0]?.args.run_id, "run-abc");
    assert.equal(receipt.run_id, "run-abc");
    assert.equal(receipt.terminal, true);
    assert.equal(receipt.executor_auth?.authenticated, true);
    assert.equal(receipt.transitions[0]?.state, "finished");
    assert.equal(receipt.lease.owner, "Desktop_Agent7");
  });

  it("exposes callTool for the rest of the hub surface", async () => {
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: mockHub({
        vault_journal: (args) => ({ written: true, agent: args.agent }),
      }),
    });
    const result = await client.callTool("vault_journal", {
      entry: "client scaffold",
      agent: "Desktop_Agent4",
    });
    assert.equal(result.written, true);
  });
});
