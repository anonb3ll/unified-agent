import type { Page } from "@playwright/test";
import { mockHub, type HubOverride } from "../support/mockHub";
import { resolve, type HubTable, type Reply } from "./fixtures";

export type Recorded = { method: string; path: string; body: unknown };

/**
 * Answer the hub with a fixture table over the foundation's `mockHub`, plus
 * per-test overrides. Returns the writes the page made, newest last.
 */
export async function hub(page: Page, table: HubTable, extra: Record<string, Reply | ((url: URL, body: unknown) => Reply)> = {}) {
  const writes: Recorded[] = [];
  const overrides: Record<string, HubOverride> = {};
  const all: Record<string, Reply | ((url: URL, body: unknown) => Reply)> = { ...table, ...extra };
  for (const [key, responder] of Object.entries(all)) {
    overrides[key] = async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      let body: unknown = undefined;
      try {
        body = request.postDataJSON();
      } catch {
        body = request.postData();
      }
      if (request.method() !== "GET") writes.push({ method: request.method(), path: url.pathname, body });
      const reply = typeof responder === "function" ? (responder as (u: URL, b: unknown) => Reply)(url, body) : resolve(responder, url);
      await route.fulfill({ status: reply.status ?? 200, contentType: "application/json", body: JSON.stringify(reply.body ?? {}) });
    };
  }
  await mockHub(page, overrides);
  return writes;
}
