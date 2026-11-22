import type { MockHandler, MockReply } from "../support/fetchMock";
import {
  DEPLOYS_CONTENT,
  DEPLOYS_LINKS,
  DEPLOYS_PATH,
  KNOWLEDGE_READS,
  LOCKED_CONTENT,
  LOCKED_PATH,
  parkedBody,
  VAULT_ROOT,
  VAULT_WIKI,
  vaultPage,
} from "./fixtures";

/** `GET /api/vault/list?dir=` answers by directory; unknown dirs are empty. */
export function vaultListReply(url: string): MockReply {
  const dir = new URL(url, "http://localhost").searchParams.get("dir") ?? "";
  if (dir === "") return { body: VAULT_ROOT };
  if (dir === "Wiki") return { body: VAULT_WIKI };
  return { body: { entries: [], truncated: false } };
}

const PAGES: Record<string, ReturnType<typeof vaultPage>> = {
  [DEPLOYS_PATH]: vaultPage(DEPLOYS_PATH, DEPLOYS_CONTENT, DEPLOYS_LINKS),
  [LOCKED_PATH]: vaultPage(LOCKED_PATH, LOCKED_CONTENT),
  "Wiki/Index.md": vaultPage("Wiki/Index.md", "# Index\n\n- [[Deploys.v2]]\n"),
  "README.md": vaultPage("README.md", "# Vault\n\nRead me first.\n"),
  "Wiki/Runbook.md": vaultPage("Wiki/Runbook.md", "# Runbook\n\nStep one.\n"),
  "Logs/2026-11-10.md": vaultPage("Logs/2026-11-10.md", "# 2026-11-10\n\n- 09:12 entry\n"),
};

/** `GET /api/vault/page?path=` answers from the pages above; others are 404 like `vault_read`. */
export function vaultPageReply(url: string): MockReply {
  const path = new URL(url, "http://localhost").searchParams.get("path") ?? "";
  const page = PAGES[path];
  return page ? { body: page } : { status: 404, body: { error: `no such note: ${path}` } };
}

/** Fetch handlers for every Knowledge read, overridable per test. */
export function knowledgeHub(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  const reads = Object.fromEntries(Object.entries(KNOWLEDGE_READS).map(([key, body]) => [key, { body } as MockHandler]));
  return {
    ...reads,
    "GET /api/vault/list": ({ url }) => vaultListReply(url),
    "GET /api/vault/page": ({ url }) => vaultPageReply(url),
    ...overrides,
  };
}

export const PARKED = (tool: string, approvalId = 77): MockReply => ({ status: 202, body: parkedBody(tool, approvalId) });

export const posts = <T extends { method: string; path: string }>(calls: T[], path: string) => calls.filter((c) => c.method === "POST" && c.path === path);
