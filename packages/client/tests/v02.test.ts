import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ZodError } from "zod";

import { isTaskSafeWait } from "@unified-agent/protocol";
import { UnifiedAgentClient } from "../src/client.ts";
import {
  HubAuthError,
  HubRpcError,
  HubTransportError,
  HubValidationError,
} from "../src/errors.ts";
import { HubTransport, type FetchLike } from "../src/hub.ts";

type JsonRpcBody = {
  method?: string;
  id?: number;
  params?: { name?: string; arguments?: Record<string, unknown>; requestId?: number };
};

function sessionAwareFetch(
  onCall: (body: JsonRpcBody, init?: Parameters<FetchLike>[1]) => {
    status?: number;
    payload?: unknown;
    headers?: Record<string, string | null>;
    throwNetwork?: boolean;
  },
): FetchLike {
  let session = false;
  return async (_url, init) => {
    const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
    if (body.method === "initialize") {
      session = true;
      return {
        ok: true,
        status: 200,
        headers: {
          get: (name: string) =>
            name.toLowerCase() === "mcp-session-id" ? "sess-v02" : null,
        },
        text: async () => JSON.stringify({ jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2025-06-18" } }),
      };
    }
    if (body.method === "notifications/initialized" || body.method === "notifications/cancelled") {
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
    const outcome = onCall(body, init);
    if (outcome.throwNetwork) {
      throw new TypeError("fetch failed");
    }
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

describe("v0.2 HubTransport reliability", () => {
  it("recovers from an expired MCP session (HTTP 404) exactly once", async () => {
    let toolAttempts = 0;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        if (body.method === "tools/call") {
          toolAttempts += 1;
          if (toolAttempts === 1) {
            return { status: 404, payload: "session expired" };
          }
          return {
            payload: {
              jsonrpc: "2.0",
              id: body.id,
              result: { structuredContent: { ok: true, attempt: toolAttempts } },
            },
          };
        }
        throw new Error(`unexpected ${body.method}`);
      }),
    });

    // Prime a session, then force the transport to believe it is still live.
    await transport.ensureSession();
    assert.equal(transport.mcpSessionId, "sess-v02");

    const result = await transport.callTool("task_list", { status: "open" });
    assert.deepEqual(result, { ok: true, attempt: 2 });
    assert.equal(toolAttempts, 2);
  });

  it("does not blind-retry a mutation without an idempotency key", async () => {
    let attempts = 0;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        if (body.method === "tools/call") {
          attempts += 1;
          return { throwNetwork: true };
        }
        throw new Error(`unexpected ${body.method}`);
      }),
    });

    await assert.rejects(
      () => transport.callTool("task_update", { id: 1, status: "done" }),
      HubTransportError,
    );
    assert.equal(attempts, 1);
  });

  it("bounds transport retries when an idempotency key is present", async () => {
    let attempts = 0;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        if (body.method === "tools/call") {
          attempts += 1;
          if (attempts === 1) {
            return { throwNetwork: true };
          }
          return {
            payload: {
              jsonrpc: "2.0",
              id: body.id,
              result: {
                structuredContent: {
                  id: body.params?.arguments?.id,
                  key: body.params?.arguments?.idempotency_key,
                },
              },
            },
          };
        }
        throw new Error(`unexpected ${body.method}`);
      }),
    });

    const result = await transport.callTool("task_update", {
      id: 42,
      status: "doing",
      idempotency_key: "sdk-v02-retry-1",
    });
    assert.deepEqual(result, { id: 42, key: "sdk-v02-retry-1" });
    assert.equal(attempts, 2);
  });

  it("never retries HubAuthError or HubRpcError", async () => {
    let attempts = 0;
    const auth = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        if (body.method === "tools/call") {
          attempts += 1;
          return { status: 401, payload: "nope" };
        }
        throw new Error(`unexpected ${body.method}`);
      }),
    });
    await assert.rejects(
      () =>
        auth.callTool("task_list", {}, { retryable: true, maxAttempts: 3 }),
      HubAuthError,
    );
    assert.equal(attempts, 1);

    attempts = 0;
    const rpc = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        if (body.method === "tools/call") {
          attempts += 1;
          return {
            payload: {
              jsonrpc: "2.0",
              id: body.id,
              error: { code: -32000, message: "tool failed" },
            },
          };
        }
        throw new Error(`unexpected ${body.method}`);
      }),
    });
    await assert.rejects(
      () =>
        rpc.callTool(
          "task_add",
          { title: "x", idempotency_key: "k" },
          { retryable: true, maxAttempts: 3 },
        ),
      HubRpcError,
    );
    assert.equal(attempts, 1);
  });

  it("maps malformed JSON-RPC bodies to HubTransportError", async () => {
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        if (body.method === "tools/call") {
          return { payload: "{not-json" };
        }
        throw new Error(`unexpected ${body.method}`);
      }),
    });
    await assert.rejects(() => transport.callTool("task_list", {}), HubTransportError);
  });

  it("forwards AbortSignal cancellation to the transport notification", async () => {
    const methods: string[] = [];
    let resolveCall: (() => void) | undefined;
    const callGate = new Promise<void>((resolve) => {
      resolveCall = resolve;
    });
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        methods.push(body.method ?? "");
        if (body.method === "initialize") {
          return {
            ok: true,
            status: 200,
            headers: {
              get: (name: string) =>
                name.toLowerCase() === "mcp-session-id" ? "sess-abort" : null,
            },
            text: async () => JSON.stringify({ result: { protocolVersion: "2025-06-18" } }),
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
        if (body.method === "tools/call") {
          await callGate;
          if (init?.signal?.aborted) {
            throw new DOMException("Aborted", "AbortError");
          }
          return {
            ok: true,
            status: 200,
            headers: { get: () => "application/json" },
            text: async () =>
              JSON.stringify({
                jsonrpc: "2.0",
                id: body.id,
                result: { structuredContent: { ok: true } },
              }),
          };
        }
        throw new Error(`unexpected ${body.method}`);
      },
    });

    const controller = new AbortController();
    const pending = transport.callTool("task_list", {}, { signal: controller.signal });
    // Let the tools/call request start, then abort.
    await new Promise((r) => setTimeout(r, 0));
    controller.abort();
    resolveCall?.();
    await assert.rejects(() => pending);
    assert.ok(methods.includes("notifications/cancelled"));
  });
});

describe("v0.2 UnifiedAgentClient typed surface", () => {
  it("exercises newly typed capabilities through the public entry point", async () => {
    const seen: Array<{ name: string; args: Record<string, unknown> }> = [];
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        if (body.method !== "tools/call") {
          throw new Error(`unexpected ${body.method}`);
        }
        const name = body.params?.name ?? "";
        const args = body.params?.arguments ?? {};
        seen.push({ name, args });
        const payloads: Record<string, unknown> = {
          task_claim: {
            id: args.id,
            status: "doing",
            claimed_by: args.agent,
            title: "SDK slice",
            priority: "high",
            project: "unified-agent",
            created_at: "2026-09-11T00:00:00Z",
            updated_at: "2026-09-11T00:00:00Z",
          },
          profile_status: { profile: "developer", identity: "Desktop_Agent4" },
          system_doctor: {
            as_of: "2026-09-11T00:00:00Z",
            host: "test-host",
            hosts: [],
            checks: [{ capability: "hub_auth", status: "LIVE" }],
            summary: { live: 1, dormant: 0, misconfigured: 0, unknown: 0 },
          },
          mcp_health_telemetry: {
            status: "healthy",
            protocol_version: "2025-06-18",
          },
          task_add: {
            id: 928,
            title: args.title,
            status: "open",
            priority: "high",
            project: "unified-agent",
            created_at: "2026-09-11T00:00:00Z",
            updated_at: "2026-09-11T00:00:00Z",
          },
        };
        return {
          payload: {
            jsonrpc: "2.0",
            id: body.id,
            result: { structuredContent: payloads[name] ?? {} },
          },
        };
      }),
    });

    const claimed = await client.taskClaim({
      id: 928,
      agent: "Desktop_Agent4",
      expected_lane: "Desktop_Agent4",
      expected_project: "unified-agent",
    });
    assert.ok(!isTaskSafeWait(claimed));
    assert.equal(claimed.status, "doing");
    assert.equal(claimed.claimed_by, "Desktop_Agent4");

    const profile = await client.profileStatus();
    assert.deepEqual(profile, {
      profile: "developer",
      identity: "Desktop_Agent4",
    });

    const doctor = await client.systemDoctor();
    assert.equal(doctor.summary.live, 1);
    assert.equal(doctor.host, "test-host");

    const telemetry = await client.mcpHealthTelemetry();
    assert.equal(telemetry.status, "healthy");

    await client.taskAdd({
      title: "typed add",
      priority: "high",
      idempotency_key: "sdk-v02-add-1",
    });

    assert.deepEqual(
      seen.map((s) => s.name),
      [
        "task_claim",
        "profile_status",
        "system_doctor",
        "mcp_health_telemetry",
        "task_add",
      ],
    );
    assert.equal(seen[4]?.args.idempotency_key, "sdk-v02-add-1");
  });

  it("returns safe_wait from taskClaim as a typed non-claim result", async () => {
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => ({
        payload: {
          jsonrpc: "2.0",
          id: body.id,
          result: {
            structuredContent: {
              id: 928,
              claimed: false,
              safe_wait: {
                owner: "Desktop_Agent2",
                waiting_on: "ci",
                until: "2026-09-11T00:00:00+00:00",
                generation: 2,
                claimed_by: "Desktop_Agent2",
              },
            },
          },
        },
      })),
    });

    const result = await client.taskClaim({ id: 928, agent: "Desktop_Agent4" });

    assert.ok(isTaskSafeWait(result));
    assert.equal(result.claimed, false);
    assert.equal(result.safe_wait.owner, "Desktop_Agent2");
    assert.equal(result.safe_wait.generation, 2);
  });

  it("raises HubValidationError for malformed typed responses", async () => {
    const client = new UnifiedAgentClient({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: sessionAwareFetch((body) => {
        if (body.method !== "tools/call") {
          throw new Error(`unexpected ${body.method}`);
        }
        return {
          payload: {
            jsonrpc: "2.0",
            id: body.id,
            result: {
              structuredContent: {
                tasks: [{ id: "not-a-number", title: "bad", status: "open" }],
              },
            },
          },
        };
      }),
    });

    await assert.rejects(async () => {
      try {
        await client.taskList({ status: "open" });
      } catch (err) {
        assert.ok(err instanceof HubValidationError);
        assert.ok(err.cause instanceof ZodError);
        throw err;
      }
    }, HubValidationError);
  });
});
