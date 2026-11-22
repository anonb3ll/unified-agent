import type { Page, Route } from "@playwright/test";
import type { ApprovalsPayload } from "../../../src/lib/api/approvals";
import type { WhoamiPayload } from "../../../src/lib/api/auth";
import type { AttentionPayload } from "../../../src/lib/attention/types";

export type HubReply = { status?: number; json?: unknown; body?: string; contentType?: string };
export type HubOverride = HubReply | ((route: Route) => Promise<void> | void);

const WHOAMI = {
  hub_version: "9.9.9",
  api_version: 1,
  agent: "operator",
  via: "cookie",
  csrf: "csrf-1",
  enforce_auth: true,
  tokens_configured: true,
  control_enabled: true,
  control_disabled_reason: null,
} satisfies WhoamiPayload;

/** Defaults for the reads the v2 shell makes; keys are "METHOD /api/path". */
export const HUB_DEFAULTS: Record<string, HubReply> = {
  "GET /api/whoami": { json: WHOAMI },
  "GET /api/attention": {
    json: {
      items: [],
      informational: [],
      counts: {},
      total: 0,
      errors: [],
      needs_operator: { rows: [], counts: {}, total: 0, errors: [] },
    } satisfies AttentionPayload,
  },
  "GET /api/approvals": { json: { pending: [], approved_unconsumed: [], expired_passes: [] } satisfies ApprovalsPayload },
  "GET /api/session/options": { json: { workos: false } },
  "GET /api/events/stream": { status: 200, body: ": ok\n\n", contentType: "text/event-stream" },
};

/**
 * Answer the hub API with `page.route`: defaults above, overridden per test.
 * Anything not listed answers 404 with a JSON error, like a hub without the route.
 */
export async function mockHub(page: Page, overrides: Record<string, HubOverride> = {}) {
  const table: Record<string, HubOverride> = { ...HUB_DEFAULTS, ...overrides };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const key = `${request.method()} ${new URL(request.url()).pathname}`;
    const entry = table[key];
    if (typeof entry === "function") {
      await entry(route);
      return;
    }
    if (!entry) {
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: `not mocked: ${key}` }) });
      return;
    }
    await route.fulfill({
      status: entry.status ?? 200,
      contentType: entry.contentType ?? "application/json",
      body: entry.body ?? JSON.stringify(entry.json ?? {}),
    });
  });
}
