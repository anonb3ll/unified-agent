import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Task } from "@unified-agent/protocol";

import { UnifiedAgentClient } from "../src/client.ts";
import { HubRpcError } from "../src/errors.ts";
import {
  HubTransport,
  decodeRpc,
  unwrapToolResult,
  type FetchLike,
} from "../src/hub.ts";

type JsonRpcBody = {
  method?: string;
  id?: number;
  params?: { name?: string; arguments?: Record<string, unknown> };
};

function sessionAwareFetch(
  onCall: (body: JsonRpcBody) => {
    status?: number;
    payload?: unknown;
    headers?: Record<string, string | null>;
  },
): FetchLike {
  return async (_url, init) => {
    const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
    if (body.method === "initialize") {
      return {
        ok: true,
        status: 200,
        headers: {
          get: (name: string) =>
            name.toLowerCase() === "mcp-session-id" ? "sess-fidelity" : null,
        },
        text: async () =>
          JSON.stringify({
            jsonrpc: "2.0",
            id: body.id,
            result: { protocolVersion: "2025-06-18" },
          }),
      };
    }
    if (
      body.method === "notifications/initialized" ||
      body.method === "notifications/cancelled"
    ) {
      return {
        ok: true,
        status: 202,
        headers: { get: () => null },
        text: async () => "",
      };
    }
    const outcome = onCall(body);
    const status = outcome.status ?? 200;
    const headers = outcome.headers ?? {};
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: {
        get: (name: string) =>
          headers[name.toLowerCase()] ??
          (name.toLowerCase() === "content-type" ? "application/json" : null),
      },
      text: async () =>
        typeof outcome.payload === "string"
          ? outcome.payload
          : JSON.stringify(
              outcome.payload ?? {
                jsonrpc: "2.0",
                id: body.id,
                result: { structuredContent: {} },
              },
            ),
    };
  };
}

describe("#929 typed-surface fidelity", () => {
  it("keeps hub queue fields on Task instead of stripping them", () => {
    const parsed = Task.parse({
      id: 929,
      title: "fidelity",
      status: "open",
      priority: "high",
      project: "unified-agent",
      created_at: "2026-09-11T00:00:00Z",
      updated_at: "2026-09-11T00:00:00Z",
      lineage: [927],
      waiting_for_review: 1,
      laptop_lane: 0,
      notes_archived: false,
      untrusted: 0,
      notes_chars: 42,
      future_hub_key: "dropped-by-design",
    });
    assert.deepEqual(parsed.lineage, [927]);
    assert.equal(parsed.waiting_for_review, 1);
    assert.equal(parsed.notes_chars, 42);
    assert.equal(
      "future_hub_key" in parsed,
      false,
      "unknown keys stay strip-mode; extend Task when the hub adds stable fields",
    );
  });

  it("returns all five approval_pending keys through the public client", async () => {
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        assert.equal(body.params?.name, "approval_pending");
        return {
          payload: {
            jsonrpc: "2.0",
            id: body.id,
            result: {
              structuredContent: {
                pending: [
                  {
                    id: 1,
                    tool: "run_cmd",
                    payload: {},
                    status: "pending",
                    created_at: "2026-09-11T00:00:00Z",
                    updated_at: "2026-09-11T00:00:00Z",
                  },
                ],
                approved_unconsumed: [],
                expired_passes: [],
                gated_tools: ["run_cmd"],
                pass_ttl_hours: 24,
              },
            },
          },
        };
      }),
    });

    const queue = await client.approvalPending({ limit: 10 });
    assert.equal(queue.pending.length, 1);
    assert.deepEqual(queue.approved_unconsumed, []);
    assert.deepEqual(queue.expired_passes, []);
    assert.deepEqual(queue.gated_tools, ["run_cmd"]);
    assert.equal(queue.pass_ttl_hours, 24);
  });

  it("keeps approval run_id through validated approval_pending reads", async () => {
    const row = {
      tool: "run_cmd",
      payload: {},
      status: "pending",
      created_at: "2026-09-11T00:00:00Z",
      updated_at: "2026-09-11T00:00:00Z",
    };
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        assert.equal(body.params?.name, "approval_pending");
        return {
          payload: {
            jsonrpc: "2.0",
            id: body.id,
            result: {
              structuredContent: {
                pending: [
                  { ...row, id: 1, run_id: "sp-ABC" },
                  { ...row, id: 2, run_id: null },
                  { ...row, id: 3 },
                ],
                approved_unconsumed: [],
                expired_passes: [],
                gated_tools: [],
              },
            },
          },
        };
      }),
    });

    const queue = await client.approvalPending();
    assert.equal(queue.pending[0]?.run_id, "sp-ABC");
    assert.equal(queue.pending[1]?.run_id, null);
    assert.equal(queue.pending[2]?.run_id, undefined);
    assert.equal("run_id" in (queue.pending[2] ?? {}), false);
  });

  it("types the real system_doctor payload without a phantom status", async () => {
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        assert.equal(body.params?.name, "system_doctor");
        return {
          payload: {
            jsonrpc: "2.0",
            id: body.id,
            result: {
              structuredContent: {
                as_of: "2026-09-11T02:00:00+00:00",
                host: "example-host.local",
                hosts: [],
                checks: [
                  {
                    capability: "hub_auth",
                    status: "LIVE",
                    detail: "ok",
                    gate: "UA_ENFORCE_AUTH",
                  },
                ],
                summary: {
                  live: 1,
                  dormant: 0,
                  degraded: 1,
                  misconfigured: 0,
                  unknown: 0,
                },
              },
            },
          },
        };
      }),
    });

    const report = await client.systemDoctor();
    assert.equal(report.as_of, "2026-09-11T02:00:00+00:00");
    assert.equal(report.host, "example-host.local");
    assert.equal(report.summary.live, 1);
    assert.equal(report.summary.degraded, 1);
    assert.equal(report.checks[0]?.capability, "hub_auth");
    assert.equal("status" in report, false);
  });

  it("surfaces content[].text on HubRpcError.message and keeps the envelope on .rpc", () => {
    assert.throws(
      () =>
        unwrapToolResult({
          isError: true,
          content: [{ type: "text", text: "task 999 not found" }],
        }),
      (err: unknown) => {
        assert.ok(err instanceof HubRpcError);
        assert.equal(err.message, "task 999 not found");
        assert.equal(
          (err.rpc as { content?: Array<{ text?: string }> }).content?.[0]?.text,
          "task 999 not found",
        );
        return true;
      },
    );
  });

  it("decodes multi-frame and multi-line SSE responses", () => {
    const multiFrame = decodeRpc(
      { get: () => "text/event-stream" },
      [
        'event: message',
        'data: {"jsonrpc":"2.0","id":1,"result":{"partial":true}}',
        "",
        'event: message',
        'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}',
        "",
      ].join("\n"),
    );
    assert.deepEqual(multiFrame.result, { ok: true });

    const multiLine = decodeRpc(
      { get: () => "text/event-stream" },
      [
        "event: message",
        'data: {"jsonrpc":"2.0","id":2,',
        'data: "result":{"ok":true}}',
        "",
      ].join("\n"),
    );
    assert.deepEqual(multiLine.result, { ok: true });
  });

  it("single-flights concurrent ensureSession and records protocolVersion", async () => {
    let initializes = 0;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        const body = init?.body ? JSON.parse(init.body) : {};
        if (body.method === "initialize") {
          initializes += 1;
          await new Promise((r) => setTimeout(r, 20));
          return {
            ok: true,
            status: 200,
            headers: {
              get: (name: string) =>
                name.toLowerCase() === "mcp-session-id" ? "sess-once" : null,
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
        throw new Error(`unexpected ${body.method}`);
      },
    });

    const [a, b] = await Promise.all([
      transport.ensureSession(),
      transport.ensureSession(),
    ]);
    assert.equal(a, "sess-once");
    assert.equal(b, "sess-once");
    assert.equal(initializes, 1);
    assert.equal(transport.protocolVersion, "2025-06-18");
  });
});
