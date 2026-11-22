/**
 * Session lifecycle hardening for real (long-lived) consumers (task #939).
 *
 * Complements the existing single-flight test in fidelity.test.ts (which
 * exercises `ensureSession()` called directly) with the paths a real worker
 * actually hits: concurrent `callTool()`/`listTools()` calls before any
 * session exists, a session going stale mid-flight under concurrent load,
 * the hub rejecting `initialize` itself, request timeouts, and close/reuse.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { HubAuthError, HubRpcError, HubTransportError } from "../src/errors.ts";
import { HubTransport, type FetchLike } from "../src/hub.ts";

type JsonRpcBody = {
  method?: string;
  id?: number;
  params?: { name?: string; arguments?: Record<string, unknown> };
};

function okInitialize(sessionId: string, body: JsonRpcBody) {
  return {
    ok: true,
    status: 200,
    headers: {
      get: (name: string) => (name.toLowerCase() === "mcp-session-id" ? sessionId : null),
    },
    text: async () =>
      JSON.stringify({ jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2025-06-18" } }),
  };
}

const okAck = { ok: true, status: 202, headers: { get: () => null }, text: async () => "" };

describe("session lifecycle: concurrent real callers (task #939)", () => {
  it("single-flights concurrent callTool() calls through the public path, not just ensureSession()", async () => {
    let initializes = 0;
    let toolCalls = 0;
    const fetch: FetchLike = async (_url, init) => {
      const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
      if (body.method === "initialize") {
        initializes += 1;
        await new Promise((r) => setTimeout(r, 15));
        return okInitialize("sess-concurrent", body);
      }
      if (body.method === "notifications/initialized") {
        return okAck;
      }
      if (body.method === "tools/call") {
        toolCalls += 1;
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
    };
    const transport = new HubTransport({ url: "http://hub/mcp", bearerToken: "tok", fetch });

    const results = await Promise.all([
      transport.callTool("t1"),
      transport.callTool("t2"),
      transport.callTool("t3"),
    ]);

    assert.equal(initializes, 1, "concurrent callers must share one handshake");
    assert.equal(toolCalls, 3);
    assert.deepEqual(results, [{ ok: true }, { ok: true }, { ok: true }]);
  });

  it("recovers a session that goes stale mid-flight under concurrent load", async () => {
    // First round of tool calls all use "sess-1". The hub then invalidates it
    // (as if it expired between requests) — every in-flight call sees a 404
    // and must converge on exactly one re-handshake, not one each.
    let initializes = 0;
    let currentSession = "sess-1";
    let invalidated = false;
    const fetch: FetchLike = async (_url, init) => {
      const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
      const sessionHeader = init?.headers?.["mcp-session-id"];
      if (body.method === "initialize") {
        initializes += 1;
        currentSession = initializes === 1 ? "sess-1" : "sess-2";
        return okInitialize(currentSession, body);
      }
      if (body.method === "notifications/initialized") {
        return okAck;
      }
      if (body.method === "tools/call") {
        if (invalidated && sessionHeader === "sess-1") {
          return {
            ok: false,
            status: 404,
            headers: { get: () => "application/json" },
            text: async () => "session expired",
          };
        }
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          text: async () =>
            JSON.stringify({
              jsonrpc: "2.0",
              id: body.id,
              result: { structuredContent: { session: sessionHeader } },
            }),
        };
      }
      throw new Error(`unexpected ${body.method}`);
    };
    const transport = new HubTransport({ url: "http://hub/mcp", bearerToken: "tok", fetch });

    await transport.callTool("warm_up");
    assert.equal(transport.mcpSessionId, "sess-1");
    invalidated = true;

    const results = await Promise.all([
      transport.callTool("t1"),
      transport.callTool("t2"),
    ]);

    assert.equal(initializes, 2, "exactly one re-handshake for the whole batch of stale calls");
    assert.deepEqual(results, [{ session: "sess-2" }, { session: "sess-2" }]);
  });
});

describe("session lifecycle: server rejection during initialize (task #939)", () => {
  it("propagates a JSON-RPC error from initialize as HubRpcError without poisoning future attempts", async () => {
    let attempt = 0;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") {
          attempt += 1;
          if (attempt === 1) {
            return {
              ok: true,
              status: 200,
              headers: { get: () => "application/json" },
              text: async () =>
                JSON.stringify({
                  jsonrpc: "2.0",
                  id: body.id,
                  error: { code: -32600, message: "hub is in maintenance mode" },
                }),
            };
          }
          return okInitialize("sess-after-rejection", body);
        }
        return okAck;
      },
    });

    await assert.rejects(
      () => transport.ensureSession(),
      (err: unknown) => {
        assert.ok(err instanceof HubRpcError);
        assert.match(err.message, /maintenance mode/);
        return true;
      },
    );
    assert.equal(transport.mcpSessionId, null);

    // A later attempt is not stuck behind the failed one.
    const sessionId = await transport.ensureSession();
    assert.equal(sessionId, "sess-after-rejection");
  });

  it("propagates a 401 during initialize as HubAuthError", async () => {
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "bad-token",
      fetch: async () => ({
        ok: false,
        status: 401,
        headers: { get: () => "application/json" },
        text: async () => "invalid token",
      }),
    });
    await assert.rejects(() => transport.ensureSession(), HubAuthError);
    assert.equal(transport.mcpSessionId, null);
  });
});

describe("session lifecycle: timeout (task #939)", () => {
  it("aborts a request that never resolves after requestTimeoutMs and reports it clearly", async () => {
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      requestTimeoutMs: 25,
      fetch: (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(init.signal!.reason);
          });
        }),
    });

    await assert.rejects(
      () => transport.ensureSession(),
      (err: unknown) => {
        assert.ok(err instanceof HubTransportError);
        assert.match(err.message, /timed out after 25ms/);
        assert.equal(err.status, undefined, "a timeout is in the same bucket as a network failure");
        return true;
      },
    );
  });

  it("requestTimeoutMs: 0 disables the internal timeout and leaves a caller AbortSignal unwrapped", async () => {
    const controller = new AbortController();
    // isAbortError() unwraps by name, matching what a real fetch rejects
    // with on a plain (reasonless) controller.abort() — the same convention
    // the existing v0.2 cancellation test uses.
    const reason = new DOMException("cancelled by caller", "AbortError");
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      requestTimeoutMs: 0,
      fetch: async (_url, init) => {
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") return okInitialize("sess-slow", body);
        if (body.method === "notifications/initialized") return okAck;
        // Only the actual tools/call hangs, awaiting the caller's own abort.
        return new Promise((_resolve, reject) => {
          if (init?.signal?.aborted) {
            reject(init.signal.reason);
            return;
          }
          init?.signal?.addEventListener("abort", () => reject(init.signal!.reason));
        });
      },
    });

    queueMicrotask(() => controller.abort(reason));
    await assert.rejects(
      () => transport.callTool("slow", {}, { signal: controller.signal }),
      (err: unknown) => {
        assert.equal(err, reason, "caller's own abort reason propagates unwrapped");
        return true;
      },
    );
  });

  it("a fast, healthy call is unaffected by the default timeout", async () => {
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") return okInitialize("sess-fast", body);
        return okAck;
      },
    });
    await transport.ensureSession();
    assert.equal(transport.mcpSessionId, "sess-fast");
  });
});

describe("session lifecycle: close and reuse (task #939)", () => {
  it("close() sends a DELETE, resets state, and a later call re-handshakes cleanly", async () => {
    let deletes = 0;
    let initializes = 0;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        if (init?.method === "DELETE") {
          deletes += 1;
          return { ok: true, status: 204, headers: { get: () => null }, text: async () => "" };
        }
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") {
          initializes += 1;
          return okInitialize(`sess-${initializes}`, body);
        }
        return okAck;
      },
    });

    await transport.ensureSession();
    assert.equal(transport.mcpSessionId, "sess-1");

    await transport.close();
    assert.equal(deletes, 1);
    assert.equal(transport.mcpSessionId, null);
    assert.equal(transport.protocolVersion, null);

    const secondSessionId = await transport.ensureSession();
    assert.equal(secondSessionId, "sess-2");
    assert.equal(initializes, 2);
  });

  it("close() is best-effort: a failing DELETE still resets local state", async () => {
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        if (init?.method === "DELETE") {
          throw new Error("network blip");
        }
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") return okInitialize("sess-x", body);
        return okAck;
      },
    });
    await transport.ensureSession();
    await assert.doesNotReject(() => transport.close());
    assert.equal(transport.mcpSessionId, null);
  });

  it("close() with a hung DELETE is still bounded by the request timeout", async () => {
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      requestTimeoutMs: 20,
      fetch: async (_url, init) => {
        if (init?.method === "DELETE") {
          // A realistic hang: never resolves on its own, but does honor the
          // abort signal boundedSignal() attaches — exactly like real fetch.
          return new Promise((_resolve, reject) => {
            if (init.signal?.aborted) {
              reject(init.signal.reason);
              return;
            }
            init.signal?.addEventListener("abort", () => reject(init.signal!.reason));
          });
        }
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") return okInitialize("sess-hang", body);
        return okAck;
      },
    });
    await transport.ensureSession();
    const start = Date.now();
    await assert.doesNotReject(() => transport.close());
    assert.ok(Date.now() - start < 5000, "close() must not hang on an unresponsive DELETE");
    assert.equal(transport.mcpSessionId, null);
  });

  it("close() on a transport with no session is a no-op", async () => {
    let calls = 0;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async () => {
        calls += 1;
        throw new Error("should not be called");
      },
    });
    await transport.close();
    assert.equal(calls, 0);
  });
});
