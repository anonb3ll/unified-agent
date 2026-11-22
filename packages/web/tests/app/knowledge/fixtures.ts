/**
 * Knowledge hub responses, shaped like the real handlers:
 * - `GET /api/memory`: `webui/api.memory_payload` over `memory_list` /
 *   `memory_search` rows (`tools/memory._row_summary` plus `snippet`).
 * - `GET /api/memory/note`: `memory_get` (`_row_summary` + content,
 *   updated_by, created_at, links, confidence_posterior, versions).
 * - vault reads: `vault_list`, `vault_read` + `vault_links`,
 *   `vault_ingest_status`, `vault_search`, `vault_briefing_digest`.
 * - writes: `webui/cc_knowledge.py` (200 tool result, 202 parked approval,
 *   400 `{error}` with the tool's own wording).
 */
export const NOTE_KEY = "proj.alpha/decisions.v2";

export const MEMORY_ROWS = [
  {
    id: NOTE_KEY,
    title: "Deploy steward owns hub restarts",
    tags: ["project:unified-agent", "decision"],
    author: "Desktop_Agent1",
    updated_at: "2026-11-09T18:02:11+00:00",
    salience: 0.7,
    confidence: 0.9,
    last_accessed: "2026-11-10T09:00:00+00:00",
    access_count: 4,
    superseded_by: null,
    source_host: "linux-desktop",
    expires_at: null,
    feedback_helpful: 2,
    feedback_not_helpful: 0,
    feedback_stale: 0,
    feedback_wrong: 0,
  },
  {
    id: "b41c09e2aa10",
    title: "Run targeted pytest only on the desktop",
    tags: ["project:unified-agent", "pref"],
    author: "Desktop_Agent2",
    updated_at: "2026-11-08T12:00:00+00:00",
    salience: 0.5,
    confidence: null,
    last_accessed: null,
    access_count: 0,
    superseded_by: null,
    source_host: "linux-desktop",
    expires_at: null,
    feedback_helpful: 0,
    feedback_not_helpful: 0,
    feedback_stale: 0,
    feedback_wrong: 0,
  },
  {
    id: "c99a01f3bb22",
    title: "Vault Inbox is synced by Obsidian Sync",
    tags: ["project:vault", "fact"],
    author: "Mac_Hermes",
    updated_at: "2026-11-01T08:30:00+00:00",
    salience: 0.5,
    confidence: 0.8,
    last_accessed: null,
    access_count: 1,
    superseded_by: null,
    source_host: "mac-hub",
    expires_at: null,
    feedback_helpful: 0,
    feedback_not_helpful: 0,
    feedback_stale: 0,
    feedback_wrong: 0,
  },
];

/**
 * `webui/api.memory_payload`: echoes the requested limit (capped at 200),
 * while `memory_search` itself returns at most 50 rows
 * (`tools/memory.py` `candidates[: max(1, min(limit, 50))]`) and
 * `memory_list` at most 200.
 */
export const HUB_SEARCH_CAP = 50;
export const HUB_BROWSE_CAP = 200;

export function memoryList(rows = MEMORY_ROWS, mode: "browse" | "search" = "browse", query: string | null = null) {
  const cap = mode === "search" ? HUB_SEARCH_CAP : HUB_BROWSE_CAP;
  const results = rows.slice(0, cap);
  return { mode, query, tag: null, limit: 200, results, items: results, total: results.length };
}

/** `n` distinct notes by one author, newest first, shaped like `memory_list` rows. */
export function manyRows(n: number, author = "Desktop_Agent2") {
  return Array.from({ length: n }, (_, i) => ({
    ...MEMORY_ROWS[1],
    id: `bulk${String(i).padStart(8, "0")}`,
    title: `Bulk note ${i}`,
    author,
    updated_at: new Date(Date.UTC(2026, 10, 9, 0, 0, 0) - i * 60_000).toISOString(),
  }));
}

/** A body that tries raw HTML: it must render as text, never as markup. */
export const NOTE_CONTENT = [
  "# Who restarts the hub",
  "",
  "Only the **deploy steward** restarts the hub. See [the runbook](https://example.com/runbook).",
  "",
  "- Merge lands",
  "- Steward deploys",
  "",
  '<img src=x onerror="alert(1)"> <script>alert(2)</script>',
].join("\n");

export function memoryNote(overrides: Record<string, unknown> = {}) {
  return {
    ...MEMORY_ROWS[0],
    content: NOTE_CONTENT,
    updated_by: "Desktop_Agent1",
    created_at: "2026-11-01T10:00:00+00:00",
    epistemic: "observed",
    evidence: ["PR #1495"],
    links: {
      out: [{ id: "b41c09e2aa10", rel: "relates_to", title: "Run targeted pytest only on the desktop", actor: "Desktop_Agent1", created_at: "2026-11-02T00:00:00+00:00" }],
      in: [],
    },
    confidence_posterior: { alpha: 3, beta: 1, posterior: 0.75, open_contradictions: 0, effective: 0.75 },
    versions: [
      { version: 1, content: "Anyone may restart the hub.", tags: ["decision"], updated_at: "2026-11-01T10:00:00+00:00", actor: "Desktop_Agent1", recorded_at: "2026-11-09T18:02:11+00:00" },
    ],
    versions_total: 1,
    trust: { level: "untrusted" },
    ...overrides,
  };
}

export const PROPOSALS = {
  proposals: [{ id: "p1", path: "00 - Inbox/proposal-1.md", kind: "learning", title: "Prefer Depot for CI", content: "…", source_date: "2026-11-09", ingest_status: "new" }],
};

export const VAULT_ROOT = {
  entries: [
    { name: "00 - Inbox", type: "dir", size: null },
    { name: "Logs", type: "dir", size: null },
    { name: "Wiki", type: "dir", size: null },
    { name: "README.md", type: "file", size: 120 },
  ],
  truncated: false,
};

export const VAULT_WIKI = {
  entries: [
    { name: "Index.md", type: "file", size: 400 },
    { name: "Deploys.v2.md", type: "file", size: 900 },
    { name: "Locked page.md", type: "file", size: 80 },
  ],
  truncated: false,
};

export const DEPLOYS_PATH = "Wiki/Deploys.v2.md";
export const DEPLOYS_CONTENT = "# Deploys\n\nThe steward runs [[Runbook]] and records in [[Logs/2026-11-10]].\n\n<b>not bold</b>\n";

export function vaultPage(path: string, content: string, links: { outgoing?: string[]; backlinks?: string[] } = {}) {
  return {
    path,
    content,
    total_lines: content.split("\n").length,
    truncated: false,
    revision: "sha256:abc",
    outgoing: links.outgoing ?? [],
    backlinks: links.backlinks ?? [],
  };
}

export const DEPLOYS_LINKS = { path: DEPLOYS_PATH, outgoing: ["Logs/2026-11-10", "Runbook"], backlinks: ["Wiki/Index.md"] };

export const LOCKED_PATH = "Wiki/Locked page.md";
export const LOCKED_CONTENT = "---\nlocked: true\n---\n# Locked\n\nDo not edit.\n";

export const INGEST = {
  path: DEPLOYS_PATH,
  content_hash: "abc",
  status: "changed",
  last_ingested_at: "2026-11-05T10:00:00+00:00",
  agent: "Desktop_Agent1",
  pages: ["Wiki/Deploys.v2.md"],
};

export const BRIEFING = {
  date: "2026-11-10",
  entries: [
    { time: "09:12", agent: "Desktop_Agent1", text: "Opened PR #1510 for the kernel batch.", tags: [], machine: "desktop" },
    { time: "10:40", agent: "Mac_Hermes", text: "Deployed df9abb10 to the hub.", tags: ["deploy"], machine: "laptop" },
  ],
  desktop_count: 1,
  laptop_count: 1,
  deduped_count: 0,
  inbox: [{ path: "00 - Inbox/idea.md", status: "new", content_hash: "x", last_ingested_at: null, agent: null, pages: [] }],
  inbox_pending_count: 1,
};

export const SEARCH = {
  matches: [
    { path: DEPLOYS_PATH, line_no: 3, line: "The steward runs [[Runbook]] and records in [[Logs/2026-11-10]]." },
    { path: "Wiki/Index.md", line_no: 5, line: "- [[Deploys.v2]] how deploys work" },
  ],
  truncated: false,
};

/** Every Knowledge read, keyed "METHOD /path" (exact path; queries vary per test). */
export const KNOWLEDGE_READS: Record<string, unknown> = {
  "GET /api/memory": memoryList(),
  "GET /api/memory/note": memoryNote(),
  "GET /api/proposals": PROPOSALS,
  "GET /api/vault/briefing": BRIEFING,
  "GET /api/vault/search": SEARCH,
  "GET /api/vault/links": DEPLOYS_LINKS,
  "GET /api/vault/ingest-status": INGEST,
};

/** The hub's parked-approval answer (`cc_knowledge.pending`). */
export function parkedBody(tool: string, approvalId = 77) {
  return { status: "approval_pending", approval_id: approvalId, code: "approval_required", tool };
}

export const EMPTY_READS: Record<string, unknown> = {
  "GET /api/memory": memoryList([]),
  "GET /api/proposals": { proposals: [] },
  "GET /api/vault/briefing": { ...BRIEFING, entries: [], inbox: [], inbox_pending_count: 0 },
  "GET /api/vault/search": { matches: [], truncated: false },
  "GET /api/vault/list": { entries: [], truncated: false },
};
