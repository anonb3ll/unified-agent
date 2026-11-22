/**
 * Shared fixture for the SDK conformance suite (task #937, sdk-lane).
 *
 * Builds a deterministic mocked MCP transport: `initialize` / `notifications/*`
 * are handled generically, and `tools/call` is dispatched to a per-tool table
 * of canned responses. This is what makes the mock conformance suite
 * hermetic — it never touches a real hub — while still exercising the same
 * JSON-RPC envelope, session lifecycle, and unwrap path a live hub would.
 */

import type { FetchLike } from "../../src/hub.ts";

type FetchResponse = Awaited<ReturnType<FetchLike>>;

export type JsonRpcBody = {
  jsonrpc?: string;
  method?: string;
  id?: number;
  params?: { name?: string; arguments?: Record<string, unknown> };
};

export type ToolResponder = (
  args: Record<string, unknown>,
  body: JsonRpcBody,
) => unknown;

export type MockHubOptions = {
  /** tool name -> structuredContent (or a function producing it) for tools/call. */
  tools: Record<string, unknown | ToolResponder>;
  /** Canned `tools/list` payload. Defaults to one entry per key in `tools`. */
  toolList?: Array<{ name: string; description?: string }>;
  /** Session id handed back on `initialize`. */
  sessionId?: string;
};

export type MockHttpWrite = { status: number; payload: unknown };
export type MockControlCenterOptions = {
  /** HTTP path (not origin) -> status and JSON result. */
  writes: Record<string, MockHttpWrite>;
  calls?: Array<{ url: string; method?: string; headers?: Record<string, string>; body?: string }>;
};

/** Small HTTP/JSON double for ControlCenterClient write conformance tests. */
export function mockControlCenter(options: MockControlCenterOptions): FetchLike {
  return async (url, init) => {
    options.calls?.push({
      url,
      method: init?.method,
      headers: init?.headers,
      body: init?.body,
    });
    const path = new URL(url).pathname;
    const result = options.writes[path];
    if (!result) return httpJsonResponse(404, { error: `unmatched fixture route: ${path}` });
    return httpJsonResponse(result.status, result.payload);
  };
}

/**
 * A minimal, spec-shaped MCP hub double.
 *
 * Deliberately narrow: this is a conformance fixture, not a hub reimplementation.
 * Anything beyond initialize/tools-list/tools-call is out of scope — the live
 * gated suite is what proves compatibility with the real Python hub.
 */
export function mockHub(options: MockHubOptions): FetchLike {
  const sessionId = options.sessionId ?? "sess-conformance";
  const toolList =
    options.toolList ??
    Object.keys(options.tools).map((name) => ({ name, description: "" }));
  let sessionOpen = false;

  return async (_url, init) => {
    const body = (init?.body ? JSON.parse(init.body) : {}) as JsonRpcBody;

    if (body.method === "initialize") {
      sessionOpen = true;
      return jsonResponse(
        { get: (name: string) => (name.toLowerCase() === "mcp-session-id" ? sessionId : null) },
        { jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2025-06-18" } },
      );
    }

    if (body.method === "notifications/initialized" || body.method === "notifications/cancelled") {
      return jsonResponse({ get: () => null }, "", 202);
    }

    if (body.method === "tools/list") {
      return jsonResponse({ get: () => "application/json" }, {
        jsonrpc: "2.0",
        id: body.id,
        result: { tools: toolList },
      });
    }

    if (body.method === "tools/call") {
      if (!sessionOpen) {
        return jsonResponse({ get: () => "application/json" }, "session expired", 404);
      }
      const name = body.params?.name ?? "";
      const args = body.params?.arguments ?? {};
      if (!(name in options.tools)) {
        return jsonResponse({ get: () => "application/json" }, {
          jsonrpc: "2.0",
          id: body.id,
          error: { code: -32601, message: `unknown conformance tool: ${name}` },
        });
      }
      const entry = options.tools[name];
      const structuredContent =
        typeof entry === "function" ? (entry as ToolResponder)(args, body) : entry;
      return jsonResponse({ get: () => "application/json" }, {
        jsonrpc: "2.0",
        id: body.id,
        result: { structuredContent },
      });
    }

    throw new Error(`mockHub: unhandled method ${body.method}`);
  };
}

async function jsonResponse(
  headers: { get(name: string): string | null },
  payload: unknown,
  status = 200,
): Promise<FetchResponse> {
  const text = typeof payload === "string" ? payload : JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers,
    text: async () => text,
  };
}

async function httpJsonResponse(
  status: number,
  payload: unknown,
): Promise<FetchResponse> {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(payload),
  };
}
