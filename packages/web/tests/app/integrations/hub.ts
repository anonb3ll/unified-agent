import type { MockHandler } from "../support/fetchMock";
import { INTEGRATION_READS } from "./fixtures";

/** Fetch handlers for every integrations read (realistic data), overridable per test. */
export function integrationsHub(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  const reads = Object.fromEntries(Object.entries(INTEGRATION_READS).map(([key, body]) => [key, { body } as MockHandler]));
  return { ...reads, ...overrides };
}

export const posts = <T extends { method: string; path: string }>(calls: T[], path: string) =>
  calls.filter((c) => c.method === "POST" && c.path === path);

/** A handler whose request goes out and whose answer is lost: the outcome is unknown. */
export const LOST: MockHandler = () => Promise.reject(new TypeError("network connection was lost"));
