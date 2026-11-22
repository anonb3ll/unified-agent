import type { Page, Route } from "@playwright/test";
import { mockHub, type HubOverride } from "../support/mockHub";
import { EMPTY_READS, SETTINGS_READS } from "./fixtures";

export type Captured = { method: string; path: string; search: string; body: unknown; headers: Record<string, string> };

/**
 * The foundation's `mockHub` with every Settings read answered by the
 * realistic fixtures, plus a log of every request the page sent to a
 * Settings route (writes and exports).
 */
export async function mockSettings(page: Page, overrides: Record<string, HubOverride> = {}) {
  const requests: Captured[] = [];
  const reads: Record<string, HubOverride> = Object.fromEntries(Object.entries(SETTINGS_READS).map(([k, json]) => [k, { json }]));
  const table: Record<string, HubOverride> = { ...reads, ...overrides };
  const wrapped: Record<string, HubOverride> = {};
  for (const [key, entry] of Object.entries(table)) {
    wrapped[key] = async (route: Route) => {
      const request = route.request();
      const raw = request.postData();
      let body: unknown;
      try {
        body = raw ? JSON.parse(raw) : undefined;
      } catch {
        body = raw;
      }
      const url = new URL(request.url());
      requests.push({ method: request.method(), path: url.pathname, search: url.search, body, headers: request.headers() });
      if (typeof entry === "function") return entry(route);
      await route.fulfill({ status: entry.status ?? 200, contentType: entry.contentType ?? "application/json", body: entry.body ?? JSON.stringify(entry.json ?? {}) });
    };
  }
  await mockHub(page, wrapped);
  return { requests };
}

/** Every Settings read failing with a 500 whose raw text the person must never see as the headline. */
export function failingReads(): Record<string, HubOverride> {
  return Object.fromEntries(Object.keys(SETTINGS_READS).map((k) => [k, { status: 500, json: { error: "sqlite3.OperationalError: database is locked" } }]));
}

/** Every Settings read answering with nothing in it. */
export function emptyReads(): Record<string, HubOverride> {
  return Object.fromEntries(Object.entries(EMPTY_READS).map(([k, json]) => [k, { json }]));
}
