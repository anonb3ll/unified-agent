import type { Page, Route } from "@playwright/test";
import { mockHub, type HubOverride } from "../support/mockHub";
import { AGENTS_READS, EMPTY_READS } from "./fixtures";

export type Captured = { method: string; path: string; body: unknown };

/**
 * The foundation's `mockHub` with every Agents read answered by the fixtures,
 * plus a log of the writes the page sent.
 */
export async function mockAgents(page: Page, overrides: Record<string, HubOverride> = {}) {
  const writes: Captured[] = [];
  const reads: Record<string, HubOverride> = Object.fromEntries(Object.entries(AGENTS_READS).map(([k, json]) => [k, { json }]));
  const table: Record<string, HubOverride> = { ...reads, ...overrides };
  const wrapped: Record<string, HubOverride> = {};
  for (const [key, entry] of Object.entries(table)) {
    const [method] = key.split(" ");
    wrapped[key] =
      method === "GET"
        ? entry
        : async (route: Route) => {
            const request = route.request();
            const raw = request.postData();
            let body: unknown;
            try {
              body = raw ? JSON.parse(raw) : undefined;
            } catch {
              body = raw;
            }
            writes.push({ method: request.method(), path: new URL(request.url()).pathname, body });
            if (typeof entry === "function") return entry(route);
            await route.fulfill({ status: entry.status ?? 200, contentType: "application/json", body: entry.body ?? JSON.stringify(entry.json ?? {}) });
          };
  }
  await mockHub(page, wrapped);
  return { writes };
}

/** Every Agents read failing with a 500 the person must never see raw. */
export function failingReads(): Record<string, HubOverride> {
  return Object.fromEntries(Object.keys(AGENTS_READS).map((k) => [k, { status: 500, json: { error: "sqlite3.OperationalError: database is locked" } }]));
}

/** Every Agents read answering, but with nothing in it. */
export function emptyReads(): Record<string, HubOverride> {
  return {
    ...Object.fromEntries(Object.entries(EMPTY_READS).map(([k, json]) => [k, { json }])),
    "GET /api/agents/Desktop_Agent1": { status: 404, json: { error: "unknown agent" } },
  };
}

export function reply(json: unknown, status = 200): HubOverride {
  return { status, json };
}
