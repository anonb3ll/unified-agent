import type { Page, Route } from "@playwright/test";
import { mockHub, type HubOverride } from "../support/mockHub";
import { EMPTY_READS, KNOWLEDGE_READS } from "./fixtures";
import { vaultListReply, vaultPageReply } from "./hub";

export type Captured = { method: string; path: string; body: unknown };

async function fulfil(route: Route, reply: { status?: number; body?: unknown }) {
  await route.fulfill({ status: reply.status ?? 200, contentType: "application/json", body: JSON.stringify(reply.body ?? {}) });
}

/**
 * The foundation's `mockHub` with every Knowledge read answered by the
 * realistic fixtures (vault list and page answer by their query), plus a log
 * of the writes the page sent.
 */
export async function mockKnowledge(page: Page, overrides: Record<string, HubOverride> = {}) {
  const writes: Captured[] = [];
  const reads: Record<string, HubOverride> = Object.fromEntries(Object.entries(KNOWLEDGE_READS).map(([k, json]) => [k, { json }]));
  const table: Record<string, HubOverride> = {
    ...reads,
    "GET /api/vault/list": (route) => fulfil(route, vaultListReply(route.request().url())),
    "GET /api/vault/page": (route) => fulfil(route, vaultPageReply(route.request().url())),
    ...overrides,
  };
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

/** Every Knowledge read failing with a 500 the person must never see raw. */
export function failingReads(): Record<string, HubOverride> {
  const keys = [...Object.keys(KNOWLEDGE_READS), "GET /api/vault/list", "GET /api/vault/page"];
  return Object.fromEntries(keys.map((k) => [k, { status: 500, json: { error: "sqlite3.OperationalError: database is locked" } }]));
}

/** Every Knowledge read answering, but with nothing in it. */
export function emptyReads(): Record<string, HubOverride> {
  return {
    ...Object.fromEntries(Object.entries(EMPTY_READS).map(([k, json]) => [k, { json }])),
    "GET /api/memory/note": { status: 404, json: { error: "no memory note with id 'x'" } },
    "GET /api/vault/page": { status: 404, json: { error: "no such note: x" } },
    "GET /api/vault/links": { status: 404, json: { error: "no such note: x" } },
    "GET /api/vault/ingest-status": { status: 404, json: { error: "no such note: x" } },
  };
}

/** A write that the hub answers once and records. */
export function reply(json: unknown, status = 200): HubOverride {
  return { status, json };
}
