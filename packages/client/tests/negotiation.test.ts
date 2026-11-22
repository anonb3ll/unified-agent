/**
 * MCP negotiation, capability discovery, and progress support (task #938).
 *
 * The protocol-version constant here is not a free choice — see the comment
 * on `MCP_PROTOCOL_VERSION` in `hub.ts` and docs/compatibility-matrix.md for
 * why `2025-11-25`, not `2026-07-28`, is the correct target for this
 * transport, verified against the installed hub dependency's own
 * negotiation source.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { HubNegotiationError } from "../src/errors.ts";
import {
  HubTransport,
  KNOWN_HANDSHAKE_PROTOCOL_VERSIONS,
  MCP_PROTOCOL_VERSION,
  decodeSseFrames,
  type FetchLike,
} from "../src/hub.ts";

type JsonRpcBody = {
  method?: string;
  id?: number;
  params?: Record<string, unknown>;
};

function fixedVersionFetch(
  serverVersion: string,
  options: { capabilities?: unknown } = {},
): FetchLike {
  return async (_url, init) => {
    const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
    if (body.method === "initialize") {
      return {
        ok: true,
        status: 200,
        headers: {
          get: (name: string) =>
            name.toLowerCase() === "mcp-session-id" ? "sess-negotiation" : null,
        },
        text: async () =>
          JSON.stringify({
            jsonrpc: "2.0",
            id: body.id,
            result: {
              protocolVersion: serverVersion,
              capabilities: options.capabilities ?? {},
            },
          }),
      };
    }
    if (body.method === "notifications/initialized") {
      return { ok: true, status: 202, headers: { get: () => null }, text: async () => "" };
    }
    throw new Error(`unexpected ${body.method}`);
  };
}

describe("MCP negotiation (task #938)", () => {
  it("requests the handshake-reachable protocol version, not the modern-era one", () => {
    assert.equal(MCP_PROTOCOL_VERSION, "2025-11-25");
    assert.ok(!(KNOWN_HANDSHAKE_PROTOCOL_VERSIONS as readonly string[]).includes("2026-07-28"));
  });

  it("sends MCP_PROTOCOL_VERSION on initialize", async () => {
    let sentVersion: unknown;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") {
          sentVersion = (body.params as { protocolVersion?: unknown })?.protocolVersion;
          return {
            ok: true,
            status: 200,
            headers: { get: (n: string) => (n.toLowerCase() === "mcp-session-id" ? "s" : null) },
            text: async () =>
              JSON.stringify({
                jsonrpc: "2.0",
                id: body.id,
                result: { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {} },
              }),
          };
        }
        return { ok: true, status: 202, headers: { get: () => null }, text: async () => "" };
      },
    });
    await transport.ensureSession();
    assert.equal(sentVersion, MCP_PROTOCOL_VERSION);
  });

  it("accepts every known handshake version the hub might echo back", async () => {
    for (const version of KNOWN_HANDSHAKE_PROTOCOL_VERSIONS) {
      const transport = new HubTransport({
        url: "http://hub/mcp",
        bearerToken: "tok",
        fetch: fixedVersionFetch(version),
      });
      await transport.ensureSession();
      assert.equal(transport.protocolVersion, version);
    }
  });

  it("fails closed on a wholly unrecognized negotiated version", async () => {
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: fixedVersionFetch("1999-01-01"),
    });
    await assert.rejects(
      () => transport.ensureSession(),
      (err: unknown) => {
        assert.ok(err instanceof HubNegotiationError);
        assert.equal(err.negotiatedProtocolVersion, "1999-01-01");
        return true;
      },
    );
    assert.equal(transport.protocolVersion, null);
  });

  it("fails closed on 2026-07-28: a real revision, but not one a classic-handshake response can honestly claim", async () => {
    // A hub that returns this from a classic `initialize` response is
    // self-contradictory: 2026-07-28 uses a per-request envelope with no
    // `initialize` at all (see MCP_PROTOCOL_VERSION's doc comment). Accepting
    // this string here would let a broken or lying hub convince the SDK it
    // has capabilities (the modern envelope, no session concept) that this
    // transport does not implement.
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: fixedVersionFetch("2026-07-28"),
    });
    await assert.rejects(() => transport.ensureSession(), HubNegotiationError);
  });

  it("captures typed server capabilities from the initialize result", async () => {
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: fixedVersionFetch("2025-11-25", {
        capabilities: {
          tools: { listChanged: true },
          resources: { subscribe: true, listChanged: false },
          experimental: { unifiedAgent: { approvalGates: true } },
        },
      }),
    });
    await transport.ensureSession();
    assert.deepEqual(transport.serverCapabilities, {
      tools: { listChanged: true },
      resources: { subscribe: true, listChanged: false },
      experimental: { unifiedAgent: { approvalGates: true } },
    });
  });

  it("clears serverCapabilities on resetSession/close", async () => {
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: fixedVersionFetch("2025-11-25", { capabilities: { tools: {} } }),
    });
    await transport.ensureSession();
    assert.ok(transport.serverCapabilities);
    transport.resetSession();
    assert.equal(transport.serverCapabilities, null);
    assert.equal(transport.protocolVersion, null);
  });
});

describe("client capability declaration (task #938)", () => {
  it("declares no capabilities by default", async () => {
    let sentCapabilities: unknown;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") {
          sentCapabilities = (body.params as { capabilities?: unknown })?.capabilities;
        }
        return {
          ok: true,
          status: 200,
          headers: { get: (n: string) => (n.toLowerCase() === "mcp-session-id" ? "s" : null) },
          text: async () =>
            JSON.stringify({
              jsonrpc: "2.0",
              id: body.id,
              result: { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {} },
            }),
        };
      },
    });
    await transport.ensureSession();
    assert.deepEqual(sentCapabilities, {});
  });

  it("declares elicitation and roots when configured", async () => {
    let sentCapabilities: unknown;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      capabilities: { elicitation: true, roots: true },
      fetch: async (_url, init) => {
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") {
          sentCapabilities = (body.params as { capabilities?: unknown })?.capabilities;
        }
        return {
          ok: true,
          status: 200,
          headers: { get: (n: string) => (n.toLowerCase() === "mcp-session-id" ? "s" : null) },
          text: async () =>
            JSON.stringify({
              jsonrpc: "2.0",
              id: body.id,
              result: { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {} },
            }),
        };
      },
    });
    await transport.ensureSession();
    assert.deepEqual(sentCapabilities, { elicitation: {}, roots: { listChanged: false } });
  });
});

describe("progress notifications (task #938)", () => {
  it("routes notifications/progress frames to onProgress and still returns the final result", async () => {
    const events: Array<{ progress: number; total: number | null; message: string | null }> = [];
    let sentMeta: unknown;
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") {
          return {
            ok: true,
            status: 200,
            headers: { get: (n: string) => (n.toLowerCase() === "mcp-session-id" ? "s" : null) },
            text: async () =>
              JSON.stringify({
                jsonrpc: "2.0",
                id: body.id,
                result: { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {} },
              }),
          };
        }
        if (body.method === "notifications/initialized") {
          return { ok: true, status: 202, headers: { get: () => null }, text: async () => "" };
        }
        if (body.method === "tools/call") {
          sentMeta = body.params?._meta;
          const token = (body.params?._meta as { progressToken?: unknown } | undefined)
            ?.progressToken;
          const frames = [
            JSON.stringify({
              jsonrpc: "2.0",
              method: "notifications/progress",
              params: { progressToken: token, progress: 1, total: 3, message: "step 1" },
            }),
            JSON.stringify({
              jsonrpc: "2.0",
              method: "notifications/progress",
              params: { progressToken: token, progress: 2, total: 3 },
            }),
            JSON.stringify({
              jsonrpc: "2.0",
              id: body.id,
              result: { structuredContent: { done: true } },
            }),
          ];
          return {
            ok: true,
            status: 200,
            headers: { get: () => "text/event-stream" },
            text: async () => frames.map((f) => `data: ${f}\n`).join("\n"),
          };
        }
        throw new Error(`unexpected ${body.method}`);
      },
    });

    const result = await transport.callTool(
      "long_running_tool",
      {},
      { onProgress: (event) => events.push(event) },
    );

    assert.deepEqual(result, { done: true });
    assert.deepEqual(events, [
      { progress: 1, total: 3, message: "step 1" },
      { progress: 2, total: 3, message: null },
    ]);
    assert.ok(sentMeta && typeof (sentMeta as { progressToken?: unknown }).progressToken === "number");
  });

  it("does not attach a progress token when onProgress is not provided", async () => {
    let sentMeta: unknown = "unset";
    const transport = new HubTransport({
      url: "http://hub/mcp",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;
        if (body.method === "initialize") {
          return {
            ok: true,
            status: 200,
            headers: { get: (n: string) => (n.toLowerCase() === "mcp-session-id" ? "s" : null) },
            text: async () =>
              JSON.stringify({
                jsonrpc: "2.0",
                id: body.id,
                result: { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {} },
              }),
          };
        }
        if (body.method === "notifications/initialized") {
          return { ok: true, status: 202, headers: { get: () => null }, text: async () => "" };
        }
        if (body.method === "tools/call") {
          sentMeta = body.params?._meta;
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

    await transport.callTool("quick_tool");
    assert.equal(sentMeta, undefined);
  });

  it("decodeSseFrames returns every frame, notifications included", () => {
    const frames = decodeSseFrames(
      [
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}',
        "",
        'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}',
        "",
      ].join("\n"),
    );
    assert.equal(frames.length, 2);
    assert.equal(frames[0]?.method, "notifications/progress");
    assert.deepEqual(frames[1]?.result, { ok: true });
  });
});
