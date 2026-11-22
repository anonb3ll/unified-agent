import type { MockHandler } from "../support/fetchMock";
import { resolve, type HubTable } from "./fixtures";

/** Adapt a fixture table to the foundation's Vitest fetch mock. */
export function vitestHub(table: HubTable, extra: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  const out: Record<string, MockHandler> = {};
  for (const [key, responder] of Object.entries(table)) {
    out[key] = ({ url }) => resolve(responder, new URL(url, "http://localhost"));
  }
  return { ...out, ...extra };
}
