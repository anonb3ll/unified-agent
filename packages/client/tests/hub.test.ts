import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  HubTransport,
  decodeRpc,
  unwrapToolResult,
  type FetchLike,
} from "../src/hub.ts";
import { HubAuthError, HubRpcError, HubTransportError } from "../src/errors.ts";

describe("HubTransport helpers", () => {
  it("decodes SSE-framed JSON-RPC", () => {
    const message = decodeRpc(
      { get: () => "text/event-stream" },
      'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n\n',
    );
    assert.deepEqual(message.result, { ok: true });
  });

  it("decodes multi-frame SSE and surfaces tool-error text", () => {
    const message = decodeRpc(
      { get: () => "text/event-stream" },
      [
        'event: message',
        'data: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}',
        "",
      ].join("\n"),
    );
    assert.deepEqual(message.result, { ok: true });

    assert.throws(
      () =>
        unwrapToolResult({
          isError: true,
          content: [{ type: "text", text: "denied by policy" }],
        }),
      (err: unknown) => {
        assert.ok(err instanceof HubRpcError);
        assert.equal(err.message, "denied by policy");
        return true;
      },
    );
  });

  it("unwraps structuredContent and text JSON blocks", () => {
    assert.deepEqual(unwrapToolResult({ structuredContent: { tasks: [] } }), {
      tasks: [],
    });
    assert.deepEqual(
      unwrapToolResult({
        content: [{ type: "text", text: '{"id":1}' }],
      }),
      { id: 1 },
    );
  });

  it("handshakes then calls tools with session + bearer headers", async () => {
    const calls: Array<{ body?: string; headers?: Record<string, string> }> = [];
    const fetchMock: FetchLike = async (_url, init) => {
      calls.push({ body: init?.body, headers: init?.headers });
      const body = init?.body ? JSON.parse(init.body) : {};
      if (body.method === "initialize") {
        return {
          ok: true,
          status: 200,
          headers: {
            get: (name: string) =>
              name.toLowerCase() === "mcp-session-id" ? "sess-test-1" : null,
          },
          text: async () =>
            JSON.stringify({
              jsonrpc: "2.0",
              id: body.id,
              result: { protocolVersion: "2025-06-18" },
            }),
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
      if (body.method === "tools/call") {
        assert.equal(init?.headers?.["mcp-session-id"], "sess-test-1");
        assert.equal(init?.headers?.Authorization, "Bearer tok-secret");
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          text: async () =>
            JSON.stringify({
              jsonrpc: "2.0",
              id: body.id,
              result: {
                structuredContent: { tasks: [{ id: 1, title: "x", status: "open" }] },
              },
            }),
        };
      }
      throw new Error(`unexpected method ${body.method}`);
    };

    const transport = new HubTransport({
      url: "http://127.0.0.1:8765/mcp",
      bearerToken: "tok-secret",
      fetch: fetchMock,
    });
    const result = await transport.callTool("task_list", { status: "open" });
    assert.deepEqual(result, { tasks: [{ id: 1, title: "x", status: "open" }] });
    assert.equal(transport.mcpSessionId, "sess-test-1");
    assert.ok(calls.some((c) => c.body?.includes("initialize")));
  });

  it("surfaces 401 as HubAuthError without falling back", async () => {
    const transport = new HubTransport({
      url: "http://127.0.0.1:8765/mcp",
      bearerToken: "bad",
      fetch: async () => ({
        ok: false,
        status: 401,
        headers: { get: () => null },
        text: async () => "unauthorized",
      }),
    });
    await assert.rejects(() => transport.ensureSession(), HubAuthError);
  });

  it("cancels an in-flight request with an idempotent MCP notification", async () => {
    const methods: string[] = [];
    const fetchMock: FetchLike = async (_url, init) => {
      const body = init?.body ? JSON.parse(init.body) : {};
      methods.push(body.method);
      if (body.method === "initialize") {
        return {
          ok: true,
          status: 200,
          headers: { get: (name: string) => name.toLowerCase() === "mcp-session-id" ? "sess-1" : null },
          text: async () => JSON.stringify({ result: { protocolVersion: "2025-06-18" } }),
        };
      }
      if (body.method === "notifications/initialized" || body.method === "notifications/cancelled") {
        return { ok: true, status: 202, headers: { get: () => null }, text: async () => "" };
      }
      throw new Error(`unexpected method ${body.method}`);
    };
    const transport = new HubTransport({ url: "http://127.0.0.1:8765/mcp", bearerToken: "tok", fetch: fetchMock });
    await transport.cancel(7);
    await transport.cancel(7);
    assert.deepEqual(methods, ["initialize", "notifications/initialized", "notifications/cancelled", "notifications/cancelled"]);
    await assert.rejects(() => transport.cancel(0), HubTransportError);
  });
});
