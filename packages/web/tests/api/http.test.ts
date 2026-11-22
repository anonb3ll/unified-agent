import { afterEach, describe, expect, it, vi } from "vitest";
import { postCsrfJson } from "../../src/lib/api/http";

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
    clone() {
      return this;
    },
  };
}

describe("postCsrfJson CSRF refresh-and-retry (C4)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("refreshes whoami CSRF once on 403 then retries the mutation", async () => {
    let posts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/whoami")) {
          return jsonResponse(200, { csrf: posts === 0 ? "stale" : "fresh" });
        }
        if (url === "/api/action/cycle" && init?.method === "POST") {
          posts += 1;
          if (posts === 1) {
            return jsonResponse(403, { error: "invalid or missing CSRF token" });
          }
          return jsonResponse(200, { ok: true, operation: "start" });
        }
        return jsonResponse(404, {});
      }),
    );

    await expect(
      postCsrfJson("/api/action/cycle", { operation: "start" }),
    ).resolves.toMatchObject({ ok: true, operation: "start" });

    const headers = vi
      .mocked(fetch)
      .mock.calls.filter(([url]) => String(url) === "/api/action/cycle")
      .map(([, init]) => (init?.headers as Record<string, string>)["x-ua-csrf"]);
    expect(headers).toEqual(["stale", "fresh"]);
  });

  it("does not retry forever on repeated CSRF failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/whoami")) {
          return jsonResponse(200, { csrf: "tok" });
        }
        return jsonResponse(403, { error: "CSRF validation failed" });
      }),
    );

    await expect(postCsrfJson("/api/action/fleet", { action: "restart_service", host: "desktop-host" })).rejects.toThrow(/CSRF/i);
    const fleetPosts = vi
      .mocked(fetch)
      .mock.calls.filter(([url]) => String(url) === "/api/action/fleet");
    expect(fleetPosts).toHaveLength(2);
  });
});
