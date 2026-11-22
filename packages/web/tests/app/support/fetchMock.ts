import { vi } from "vitest";

export type MockReply = { status?: number; body?: unknown; text?: string };
export type MockHandler =
  | MockReply
  | ((request: { method: string; url: string; body: unknown; headers: Record<string, string> }) => MockReply | Promise<MockReply>);

export type RecordedCall = { method: string; path: string; body: unknown; headers: Record<string, string> };

/**
 * Stub global fetch with handlers keyed "METHOD /path" (exact path, no query)
 * or "/path" for any method. Unmatched requests answer 404. Returns the
 * recorded calls, newest last.
 */
export function mockFetch(handlers: Record<string, MockHandler>) {
  const calls: RecordedCall[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = new URL(url, "http://localhost").pathname;
    const method = (init.method ?? "GET").toUpperCase();
    const headers = Object.fromEntries(
      Object.entries((init.headers ?? {}) as Record<string, string>).map(([k, v]) => [k.toLowerCase(), v]),
    );
    let body: unknown = undefined;
    if (typeof init.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    calls.push({ method, path, body, headers });
    const handler = handlers[`${method} ${path}`] ?? handlers[path];
    const reply = await (typeof handler === "function" ? handler({ method, url, body, headers }) : handler);
    if (!reply) return new Response(JSON.stringify({ error: `not mocked: ${method} ${path}` }), { status: 404 });
    const status = reply.status ?? 200;
    const text = reply.text ?? (reply.body === undefined ? "" : JSON.stringify(reply.body));
    return new Response(status === 204 ? null : text, { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}
