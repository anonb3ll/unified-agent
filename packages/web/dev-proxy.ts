/**
 * Dev-server proxy to an explicit hub (task #1579).
 *
 * `npm run dev` serves the SPA from Vite. With `UA_DEV_HUB_URL` set, the
 * routes the SPA calls same-origin (`/api`, `/healthz`, `/oauth`, `/mcp`)
 * are proxied to that hub, so the dev server shows live hub data. Without it
 * nothing is proxied: there is no implicit target, and the desktop never
 * runs a hub of its own (a loopback hub is only an isolated test fixture).
 *
 * The proxy carries the hub's session cookies and OAuth flow, so the dev
 * server must stay on loopback while it is on: `assertLoopbackDevServer`
 * refuses `--host` / `server.host` values that expose it to the network.
 */
import type { ProxyOptions } from "vite";

export const DEV_PROXY_PATHS = ["/api", "/healthz", "/oauth", "/mcp"] as const;

/** The dev server's bind address. */
export const LOOPBACK_HOST = "127.0.0.1";

/** Proxy table for `server.proxy`, or `undefined` when no hub is configured. */
export function devHubProxy(
  hubUrl: string | undefined,
): Record<string, ProxyOptions> | undefined {
  const raw = hubUrl?.trim();
  if (!raw) return undefined;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`UA_DEV_HUB_URL is not a URL: ${JSON.stringify(raw)}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`UA_DEV_HUB_URL must be an http or https URL, got ${url.protocol}`);
  }
  if (url.username || url.password) {
    // Never put credentials in a URL that lands in shell history and logs.
    throw new Error("UA_DEV_HUB_URL must not carry credentials; sign in through the UI");
  }
  const target = url.origin;
  return Object.fromEntries(
    DEV_PROXY_PATHS.map((route) => [route, { target, changeOrigin: true }]),
  );
}

/** Whether a Vite `server.host` value binds only to the loopback interface. */
export function isLoopbackHost(host: string | boolean | undefined): boolean {
  // Vite's default (undefined) is `localhost`; `true` means every interface.
  if (host === undefined || host === false) return true;
  if (host === true) return false;
  const name = host.trim().toLowerCase().replace(/^\[(.*)\]$/, "$1");
  return name === "localhost" || name === "::1" || /^127(\.\d{1,3}){3}$/.test(name);
}

/** Refuse a network-exposed dev server while the hub proxy is on. */
export function assertLoopbackDevServer(
  host: string | boolean | undefined,
  proxy: Record<string, unknown> | undefined,
): void {
  if (proxy === undefined || isLoopbackHost(host)) return;
  throw new Error(
    `UA_DEV_HUB_URL proxies hub cookies and OAuth, so the dev server must bind to ` +
      `loopback (${LOOPBACK_HOST}), not ${JSON.stringify(host)}. Drop --host, or unset ` +
      `UA_DEV_HUB_URL.`,
  );
}
