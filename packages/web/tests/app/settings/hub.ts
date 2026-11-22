import type { MockHandler } from "../support/fetchMock";
import { SETTINGS_READS } from "./fixtures";

/** Fetch handlers for every Settings read, overridable per test. */
export function settingsHub(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  const reads = Object.fromEntries(Object.entries(SETTINGS_READS).map(([key, body]) => [key, { body } as MockHandler]));
  return { ...reads, ...overrides };
}

export const posts = <T extends { method: string; path: string }>(calls: T[], path: string) => calls.filter((c) => c.method === "POST" && c.path === path);
