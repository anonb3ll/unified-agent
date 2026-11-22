import type { Page, Route } from "@playwright/test";
import { mockHub, type HubOverride } from "../support/mockHub";
import { JOINT_REFUSED, WORK_GRAPH_7, WORK_GRAPH_ROOT, WORK_READS } from "./fixtures";

export type Captured = { method: string; path: string; body: unknown };

/**
 * The foundation's `mockHub` with every Work read answered by the realistic
 * fixtures, plus a log of the writes the page sent.
 */
export async function mockWork(page: Page, overrides: Record<string, HubOverride> = {}) {
  const writes: Captured[] = [];
  const reads: Record<string, HubOverride> = Object.fromEntries(Object.entries(WORK_READS).map(([k, json]) => [k, { json }]));
  const workGraph: HubOverride = async (route) => {
    const goal = new URL(route.request().url()).searchParams.get("goal_id");
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(goal === "7" ? WORK_GRAPH_7 : WORK_GRAPH_ROOT) });
  };
  const table: Record<string, HubOverride> = { ...reads, "GET /api/plans/3/joint": { status: 400, json: JOINT_REFUSED }, "GET /api/work-graph": workGraph, ...overrides };
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

/** Every Work read failing with a 500. */
export function failingReads(): Record<string, HubOverride> {
  return Object.fromEntries(Object.keys(WORK_READS).map((k) => [k, { status: 500, json: { error: "database is locked" } }]));
}
