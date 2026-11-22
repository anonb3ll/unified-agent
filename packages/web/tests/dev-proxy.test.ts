// Dev-server proxy to an explicit hub (task #1579): `npm run dev` reads live hub
// data through a loopback-only Vite server, and never proxies hub cookies or
// OAuth to a network-exposed one.
import { describe, expect, it } from "vitest";

import {
  DEV_PROXY_PATHS,
  assertLoopbackDevServer,
  devHubProxy,
  isLoopbackHost,
} from "../dev-proxy";

describe("devHubProxy", () => {
  it("proxies nothing without an explicit UA_DEV_HUB_URL", () => {
    expect(devHubProxy(undefined)).toBeUndefined();
    expect(devHubProxy("")).toBeUndefined();
    expect(devHubProxy("   ")).toBeUndefined();
  });

  it("proxies /api, /healthz, /oauth and /mcp to the hub origin", () => {
    const proxy = devHubProxy("http://hub.example:8765/mcp");
    expect(DEV_PROXY_PATHS).toEqual(["/api", "/healthz", "/oauth", "/mcp"]);
    expect(Object.keys(proxy ?? {})).toEqual([...DEV_PROXY_PATHS]);
    for (const route of DEV_PROXY_PATHS) {
      expect(proxy?.[route]).toMatchObject({
        target: "http://hub.example:8765",
        changeOrigin: true,
      });
    }
  });

  it("refuses a URL that is not http(s)", () => {
    expect(() => devHubProxy("not a url")).toThrow(/UA_DEV_HUB_URL/);
    expect(() => devHubProxy("file:///etc/hosts")).toThrow(/http/);
  });

  it("refuses credentials embedded in the URL", () => {
    // Built with the URL API so no credential-shaped literal sits in the repo.
    const withUser = new URL("http://hub.example:8765");
    withUser.username = "operator";
    expect(() => devHubProxy(withUser.toString())).toThrow(/credentials/);
    const withPassword = new URL("http://hub.example:8765");
    withPassword.password = "placeholder";
    expect(() => devHubProxy(withPassword.toString())).toThrow(/credentials/);
  });
});

describe("loopback binding", () => {
  it("recognises loopback hosts only", () => {
    for (const host of ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"]) {
      expect(isLoopbackHost(host)).toBe(true);
    }
    for (const host of [true, "0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "example.com"]) {
      expect(isLoopbackHost(host)).toBe(false);
    }
  });

  it("refuses a network-exposed dev server while the hub proxy is on", () => {
    const proxy = devHubProxy("http://hub.example:8765");
    expect(() => assertLoopbackDevServer("0.0.0.0", proxy)).toThrow(/loopback/);
    expect(() => assertLoopbackDevServer(true, proxy)).toThrow(/loopback/);
    expect(() => assertLoopbackDevServer("127.0.0.1", proxy)).not.toThrow();
  });

  it("leaves the host alone when no hub proxy is configured", () => {
    expect(() => assertLoopbackDevServer("0.0.0.0", undefined)).not.toThrow();
  });
});
