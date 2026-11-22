/**
 * Knowledge area reads, write paths and the pure rules the pages share
 * (spec §7: new fetchers live in the area).
 *
 * Reads that already exist and fit are imported from `src/api/*` by the
 * hooks (`fetchVaultBriefing`, `fetchProposals`). This file adds the reads
 * the classic UI never made (`/api/memory/note`, `/api/vault/list`,
 * `/api/vault/links`, `/api/vault/ingest-status`) and a memory list read
 * with a limit, and keeps the HTTP status of a failed read so the page can
 * explain it (`describeReadError`). Request and response shapes follow
 * `docs/control-center-api.md` (hub task #2469) and the handlers in
 * `webui/cc_knowledge.py` and `webui/routes.py`.
 */

/** A failed read that keeps its HTTP status and the hub's `error` text. */
export class HubReadError extends Error {
  readonly status: number;
  readonly hubError: string | null;
  constructor(status: number, what: string, detail = "") {
    super(`${what} HTTP ${status}${detail ? `: ${detail.slice(0, 200)}` : ""}`);
    this.name = "HubReadError";
    this.status = status;
    let hubError: string | null = null;
    try {
      const parsed = JSON.parse(detail) as { error?: unknown };
      if (typeof parsed.error === "string") hubError = parsed.error;
    } catch {
      hubError = null;
    }
    this.hubError = hubError;
  }
}

export async function getJson<T>(path: string, what: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new HubReadError(response.status, what, detail);
  }
  return (await response.json()) as T;
}

// --- Query keys --------------------------------------------------------------

export const KNOWLEDGE_KEYS = {
  memoryAll: ["knowledge", "memory"] as const,
  memoryList: (q: string, tag: string) => ["knowledge", "memory", "list", q, tag] as const,
  memoryNote: (key: string) => ["knowledge", "memory", "note", key] as const,
  proposals: ["knowledge", "proposals"] as const,
  vaultAll: ["knowledge", "vault"] as const,
  vaultDir: (dir: string) => ["knowledge", "vault", "dir", dir] as const,
  vaultPage: (path: string) => ["knowledge", "vault", "page", path] as const,
  /** Keyed by the note's revision too, so a changed note re-reads its Wiki status. */
  vaultIngest: (path: string, revision = "") => ["knowledge", "vault", "ingest", path, revision] as const,
  vaultLinks: (path: string, revision = "") => ["knowledge", "vault", "links", path, revision] as const,
  vaultSearch: (q: string) => ["knowledge", "vault", "search", q] as const,
  vaultBriefing: ["knowledge", "vault", "briefing"] as const,
  vaultResolve: (target: string, from: string) => ["knowledge", "vault", "resolve", target, from] as const,
};

/** Write paths (hub task #2469, `docs/control-center-api.md` knowledge section). */
export const KNOWLEDGE_WRITES = {
  memorySave: "/api/action/memory/save",
  memoryDelete: "/api/action/memory/delete",
  memoryRestore: "/api/action/memory/restore",
  memoryPromote: "/api/action/memory/promote",
  memoryFeedback: "/api/action/memory/feedback",
  vaultWrite: "/api/action/vault/write",
  vaultJournal: "/api/action/vault/journal",
} as const;

// --- Memory ------------------------------------------------------------------

/** One row of `GET /api/memory` (`memory_list` item or `memory_search` result). */
export type MemoryRow = {
  id: string;
  title?: string | null;
  tags?: string[] | null;
  author?: string | null;
  updated_at?: string | null;
  snippet?: string | null;
  source_host?: string | null;
  superseded_by?: string | null;
  [key: string]: unknown;
};

export type MemoryListPayload = {
  mode?: "browse" | "search";
  results: MemoryRow[];
  total?: number;
  limit?: number;
  error?: string;
};

/** How many notes one list read asks for: the HTTP handler's cap (`webui/api.memory_payload`). */
export const MEMORY_LIST_LIMIT = 200;
/**
 * What the hub really returns at most: `memory_search` keeps the top 50
 * (`tools/memory.py`) whatever limit is asked, `memory_list` up to 200.
 * The payload's `limit` echoes the request, so it cannot tell the cap.
 */
export const MEMORY_SEARCH_CAP = 50;
export const MEMORY_BROWSE_CAP = 200;

export function memoryCap(mode: MemoryListPayload["mode"]): number {
  return mode === "search" ? MEMORY_SEARCH_CAP : MEMORY_BROWSE_CAP;
}

/** Filters the hub's read cannot take, applied here to the rows it returned. */
export function hasLocalFilters(filters: MemoryFilters): boolean {
  return Boolean(filters.author) || Boolean(filters.tag && filters.project);
}

export async function fetchMemoryList(filter: { q: string; tag: string }, signal?: AbortSignal): Promise<MemoryListPayload> {
  const qs = new URLSearchParams({ limit: String(MEMORY_LIST_LIMIT) });
  if (filter.q.trim()) qs.set("q", filter.q.trim());
  if (filter.tag.trim()) qs.set("tag", filter.tag.trim());
  const payload = await getJson<{ mode?: "browse" | "search"; results?: unknown; items?: unknown; total?: number; limit?: number; error?: string }>(
    `/api/memory?${qs}`,
    "memory",
    signal,
  );
  const raw = Array.isArray(payload.results) ? payload.results : Array.isArray(payload.items) ? payload.items : [];
  const results = (raw as MemoryRow[]).filter((row) => row && typeof row.id === "string");
  return { mode: payload.mode, results, total: payload.total, limit: payload.limit, error: payload.error };
}

export type MemoryLinkRow = { id: string; rel?: string | null; title?: string | null; actor?: string | null; created_at?: string | null };

export type MemoryVersion = {
  version: number;
  content: string;
  tags?: string[];
  updated_at?: string | null;
  actor?: string | null;
  recorded_at?: string | null;
};

/** `GET /api/memory/note?key=` (`memory_get`). */
export type MemoryNote = MemoryRow & {
  content: string;
  created_at?: string | null;
  updated_by?: string | null;
  salience?: number | null;
  confidence?: number | null;
  expires_at?: string | null;
  last_accessed?: string | null;
  access_count?: number | null;
  feedback_helpful?: number;
  feedback_not_helpful?: number;
  feedback_stale?: number;
  feedback_wrong?: number;
  deleted?: boolean;
  supersedes?: string[];
  epistemic?: string | null;
  evidence?: unknown[];
  links?: { out?: MemoryLinkRow[]; in?: MemoryLinkRow[] };
  confidence_posterior?: { posterior?: number; effective?: number; open_contradictions?: number } | null;
  versions?: MemoryVersion[];
  versions_total?: number;
};

export async function fetchMemoryNote(key: string, signal?: AbortSignal): Promise<MemoryNote> {
  const qs = new URLSearchParams({ key, include_versions: "true" });
  return getJson<MemoryNote>(`/api/memory/note?${qs}`, "memory note", signal);
}

/** The tag a soft-deleted note carries (`tools/memory.py` DELETED_TAG). */
export const DELETED_TAG = "meta:deleted";

export function noteTags(row: { tags?: unknown }): string[] {
  if (Array.isArray(row.tags)) return row.tags.filter((t): t is string => typeof t === "string");
  if (typeof row.tags === "string") return row.tags.split(",").map((t) => t.trim()).filter(Boolean);
  return [];
}

/** Projects come from `project:<name>` tags, the fleet's tagging convention. */
export function projectsOf(row: { tags?: unknown }): string[] {
  return noteTags(row)
    .filter((t) => t.startsWith("project:"))
    .map((t) => t.slice("project:".length))
    .filter(Boolean);
}

export type MemoryFilters = { q: string; tag: string; author: string; project: string };

/**
 * The hub's list read takes one tag. The tag filter goes to the hub; a
 * project filter goes too when no tag is set (as `project:<name>`). Author,
 * and a project alongside a tag, are applied to the rows the hub returned.
 */
export function serverTagFor(filters: MemoryFilters): string {
  if (filters.tag) return filters.tag;
  return filters.project ? `project:${filters.project}` : "";
}

export function applyLocalFilters(rows: readonly MemoryRow[], filters: MemoryFilters): MemoryRow[] {
  return rows.filter((row) => {
    if (filters.author && (row.author ?? "") !== filters.author) return false;
    if (filters.project && !projectsOf(row).includes(filters.project)) return false;
    if (filters.tag && !noteTags(row).includes(filters.tag)) return false;
    return true;
  });
}

/** Distinct sorted values, always including the current one so a selected filter stays visible. */
export function optionValues(values: Iterable<string>, current: string): string[] {
  const set = new Set<string>();
  for (const v of values) if (v) set.add(v);
  if (current) set.add(current);
  return [...set].sort((a, b) => a.localeCompare(b));
}

/** Tags typed as "a, b, c". */
export function parseTags(text: string): string[] {
  const out: string[] = [];
  for (const part of text.split(/[,\n]/)) {
    const tag = part.trim();
    if (tag && !out.includes(tag)) out.push(tag);
  }
  return out;
}

/** The hub's limits for one memory save (`docs/control-center-api.md`). */
export const MEMORY_LIMITS = { content: 200_000, title: 500, tags: 50, tag: 100 } as const;

export function memoryTagsProblem(tags: readonly string[]): string | null {
  if (tags.length > MEMORY_LIMITS.tags) return `Use at most ${MEMORY_LIMITS.tags} tags.`;
  const long = tags.find((t) => t.length > MEMORY_LIMITS.tag);
  if (long) return `Each tag must be at most ${MEMORY_LIMITS.tag} characters.`;
  return null;
}

// --- Proposals ---------------------------------------------------------------

/** Memory proposals are the learning and skill-cluster proposals in the vault Inbox (`GET /api/proposals`). */
export const PROPOSALS_INBOX_HREF = "/inbox?kind=proposal";

// --- Vault -------------------------------------------------------------------

export type VaultEntry = { name: string; type: "file" | "dir"; size?: number | null };
export type VaultListPayload = { entries: VaultEntry[]; truncated?: boolean };

export async function fetchVaultDir(dir: string, signal?: AbortSignal): Promise<VaultListPayload> {
  const qs = new URLSearchParams({ max_entries: "500" });
  if (dir) qs.set("dir", dir);
  const payload = await getJson<{ entries?: unknown; truncated?: boolean }>(`/api/vault/list?${qs}`, "vault list", signal);
  const entries = (Array.isArray(payload.entries) ? payload.entries : []).filter(
    (e): e is VaultEntry => Boolean(e) && typeof (e as VaultEntry).name === "string",
  );
  return { entries, truncated: Boolean(payload.truncated) };
}

export type VaultPage = {
  path: string;
  content: string;
  total_lines?: number;
  truncated?: boolean;
  revision?: string | null;
  outgoing: string[];
  backlinks: string[];
};

/** The hub answers a disabled vault with 200 and this code (`api._vault_not_configured`). */
export class VaultNotConfiguredError extends Error {
  readonly status = 503;
  constructor() {
    super("vault not configured");
    this.name = "VaultNotConfiguredError";
  }
}

type MaybeNotConfigured = { error?: string; code?: string };

function notConfigured(payload: MaybeNotConfigured): boolean {
  return payload.code === "vault_not_configured" || payload.error === "vault not configured";
}

export async function fetchVaultPage(path: string, signal?: AbortSignal): Promise<VaultPage> {
  const payload = await getJson<Partial<VaultPage> & MaybeNotConfigured>(`/api/vault/page?${new URLSearchParams({ path })}`, "vault page", signal);
  if (notConfigured(payload)) throw new VaultNotConfiguredError();
  return {
    path: typeof payload.path === "string" ? payload.path : path,
    content: typeof payload.content === "string" ? payload.content : "",
    total_lines: payload.total_lines,
    truncated: payload.truncated,
    revision: payload.revision ?? null,
    outgoing: Array.isArray(payload.outgoing) ? payload.outgoing : [],
    backlinks: Array.isArray(payload.backlinks) ? payload.backlinks : [],
  };
}

export type VaultLinks = { path: string; outgoing: string[]; backlinks: string[] };

export async function fetchVaultLinks(path: string, signal?: AbortSignal): Promise<VaultLinks> {
  const payload = await getJson<Partial<VaultLinks>>(`/api/vault/links?${new URLSearchParams({ path })}`, "vault links", signal);
  return {
    path: payload.path ?? path,
    outgoing: Array.isArray(payload.outgoing) ? payload.outgoing : [],
    backlinks: Array.isArray(payload.backlinks) ? payload.backlinks : [],
  };
}

export type VaultIngest = {
  path?: string;
  status?: string | null;
  content_hash?: string;
  last_ingested_at?: string | null;
  agent?: string | null;
  pages?: string[];
};

export async function fetchVaultIngest(path: string, signal?: AbortSignal): Promise<VaultIngest> {
  const payload = await getJson<VaultIngest & MaybeNotConfigured>(`/api/vault/ingest-status?${new URLSearchParams({ path })}`, "vault ingest status", signal);
  if (notConfigured(payload)) throw new VaultNotConfiguredError();
  return payload;
}

export type VaultSearchMatch = { path: string; line_no?: number; line?: string };

export async function fetchVaultSearch(q: string, signal?: AbortSignal): Promise<{ matches: VaultSearchMatch[]; truncated: boolean }> {
  const payload = await getJson<{ matches?: unknown; truncated?: boolean } & MaybeNotConfigured>(
    `/api/vault/search?${new URLSearchParams({ q, max_results: "100" })}`,
    "vault search",
    signal,
  );
  if (notConfigured(payload)) throw new VaultNotConfiguredError();
  const matches = (Array.isArray(payload.matches) ? payload.matches : []).filter(
    (m): m is VaultSearchMatch => Boolean(m) && typeof (m as VaultSearchMatch).path === "string",
  );
  return { matches, truncated: Boolean(payload.truncated) };
}

/** Plain-language meaning of each ingest status (`vault_ingest_status`). */
export const INGEST_STATUS_TEXT: Record<string, { label: string; meaning: string; tone: "neutral" | "info" | "success" | "warning" }> = {
  new: { label: "Not yet in the Wiki", meaning: "No one has folded this note into the Wiki yet.", tone: "info" },
  changed: { label: "Changed since it went into the Wiki", meaning: "The note changed after it was last folded into the Wiki.", tone: "warning" },
  unchanged: { label: "In the Wiki", meaning: "The Wiki already has this version of the note.", tone: "success" },
  empty: { label: "Empty", meaning: "The note has no text, so it will not go into the Wiki.", tone: "neutral" },
};

/** The default write folders (`UA_VAULT_WRITE_DIRS`) when the hub has not said otherwise. */
export const DEFAULT_WRITE_FOLDERS = ["00 - Inbox", "Logs", "Wiki"] as const;

/**
 * The folders named in a write-folder refusal
 * ("vault writes are restricted to 00 - Inbox, Logs; 'x' is outside. ..."),
 * or null when the message is something else.
 */
export function writeFoldersFromRefusal(message: string | null | undefined): string[] | null {
  const m = /vault writes are restricted to (.+?); /.exec(message ?? "");
  if (!m) return null;
  if (m[1] === "no directories") return [];
  return m[1].split(",").map((s) => s.trim()).filter(Boolean);
}

export function isLockedRefusal(message: string | null | undefined): boolean {
  return /is locked \(frontmatter `?locked: true`?\)/.test(message ?? "");
}

export function isExistsRefusal(message: string | null | undefined): boolean {
  return /already exists/i.test(message ?? "");
}

export type LockState = "locked" | "unlocked" | "uncertain";

/** Values the hub reads as "not locked" (`safety._LOCK_FALSE_VALUES`). */
const LOCK_FALSE = new Set(["false", "no", "off", "0"]);
const LOCKED_KEY = /^(\s*)(['"]?)locked\2\s*:(.*)$/i;

/**
 * The note's lock as the hub decides it (`safety.vault_lock_state`), read
 * conservatively: a top-level `locked` key, quoted or not, whose value is not
 * false, no, off or 0 locks the note (`true # curated`, `"locked": true`,
 * `yes`, a list). Front matter this cannot read with certainty (no closing
 * line, a nested or flow-style `locked`) is "uncertain": the editor is still
 * offered, with a warning. The hub's read does not report the lock (listed
 * under Needs from hub).
 */
export function lockState(content: string): LockState {
  const lines = content.replace(/^\ufeff/, "").replace(/\r\n?/g, "\n").split("\n");
  if (lines[0]?.trimEnd() !== "---") return "unlocked";
  // The hub runs two scans (`safety.vault_lock_state`): the YAML block, which
  // ends at "---" or "...", and the legacy block (`\A---\n(.*?)\n---`), which
  // reads past a "..." to the first line starting with "---". A lock in
  // either counts, so scan the longer of the two.
  const yamlEnd = lines.findIndex((line, i) => i > 0 && (line.trimEnd() === "---" || line.trimEnd() === "..."));
  const legacyEnd = lines.findIndex((line, i) => i > 0 && line.startsWith("---"));
  const end = yamlEnd < 0 ? -1 : Math.max(yamlEnd, legacyEnd);
  const block = end < 0 ? lines.slice(1) : lines.slice(1, end);
  const mentions = block.some((line) => /locked/i.test(line));
  if (end < 0) return mentions ? "uncertain" : "unlocked";
  let verdict: LockState = "unlocked";
  for (const line of block) {
    const m = LOCKED_KEY.exec(line);
    if (!m) {
      if (/(^|[\s{,'"])locked(['"]?\s*:)/i.test(line)) verdict = "uncertain";
      continue;
    }
    if (m[1].length > 0) {
      verdict = "uncertain";
      continue;
    }
    const value = m[3].replace(/\s+#.*$/, "").trim().replace(/^(['"])(.*)\1$/, "$2").trim().toLowerCase();
    if (!LOCK_FALSE.has(value)) return "locked";
  }
  return verdict;
}

/** True when the hub would refuse a write to this note for its lock. */
export function isLockedNote(content: string): boolean {
  return lockState(content) === "locked";
}

export function isInFolders(path: string, folders: readonly string[]): boolean {
  return folders.some((f) => path === f || path.startsWith(`${f}/`));
}

export function parentDir(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

export function baseName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

export function noteTitle(path: string): string {
  return baseName(path).replace(/\.(md|txt)$/i, "");
}

/** Every folder above a path, root first: "a/b/c.md" → ["a", "a/b"]. */
export function ancestorDirs(path: string): string[] {
  const parts = path.split("/").slice(0, -1);
  return parts.map((_, i) => parts.slice(0, i + 1).join("/"));
}

/** A note name typed by a person, as a vault path in `folder`. */
export function newNotePath(folder: string, name: string): string {
  const clean = name.trim().replace(/^\/+/, "");
  const withExt = /\.(md|txt)$/i.test(clean) ? clean : `${clean}.md`;
  return folder ? `${folder}/${withExt}` : withExt;
}

export function newNoteNameProblem(name: string): string | null {
  const clean = name.trim();
  if (!clean) return "Give the note a name.";
  if (clean.split("/").some((part) => part === ".." || part.startsWith("."))) return "A note name cannot start with a dot or contain “..”.";
  if (/\.[a-z0-9]+$/i.test(clean) && !/\.(md|txt)$/i.test(clean)) return "Notes end in .md or .txt.";
  return null;
}

/**
 * Where an Obsidian `[[wikilink]]` may point, most likely first: the target
 * as a path when it names a folder, else beside the note that links to it,
 * else at the vault root. `#heading` and `^block` parts are dropped.
 */
export function wikilinkCandidates(target: string, from: string): string[] {
  const bare = target.split(/[#^]/)[0].trim();
  if (!bare) return [];
  const file = /\.(md|txt)$/i.test(bare) ? bare : `${bare}.md`;
  if (bare.includes("/")) return [file.replace(/^\/+/, "")];
  const dir = parentDir(from);
  const out = dir ? [`${dir}/${file}`, file] : [file];
  return [...new Set(out)];
}

/** Find the first candidate the vault has; null when none. */
export async function resolveWikilink(target: string, from: string, signal?: AbortSignal): Promise<string | null> {
  for (const candidate of wikilinkCandidates(target, from)) {
    try {
      const page = await fetchVaultPage(candidate, signal);
      return page.path;
    } catch (error) {
      if (error instanceof HubReadError && (error.status === 404 || error.status === 400)) continue;
      throw error;
    }
  }
  return null;
}

/** The app URL of a vault note. */
export function vaultNoteHref(path: string): string {
  return `/knowledge/vault?${new URLSearchParams({ path })}`;
}

export function wikilinkHref(target: string, from: string): string {
  return `/knowledge/vault?${new URLSearchParams({ link: target, from })}`;
}

export function memoryNoteHref(key: string, keep?: URLSearchParams): string {
  const params = new URLSearchParams(keep);
  params.delete("new");
  params.delete("edit");
  params.set("note", key);
  return `/knowledge/memory?${params}`;
}

export type VaultBriefing = {
  date?: string | null;
  entries: { time?: string; agent?: string; text?: string; tags?: string[]; machine?: string }[];
  inbox: VaultIngest[];
  inbox_pending_count?: number;
};

/** `GET /api/vault/briefing`: today's journal digest and the Inbox captures' ingest state. */
export async function fetchVaultBriefing(signal?: AbortSignal): Promise<VaultBriefing> {
  const payload = await getJson<Partial<VaultBriefing> & MaybeNotConfigured>("/api/vault/briefing", "vault briefing", signal);
  if (notConfigured(payload)) throw new VaultNotConfiguredError();
  return {
    date: payload.date ?? null,
    entries: Array.isArray(payload.entries) ? payload.entries : [],
    inbox: Array.isArray(payload.inbox) ? payload.inbox : [],
    inbox_pending_count: payload.inbox_pending_count,
  };
}

// --- Journal -----------------------------------------------------------------

/** `vault_journal` collapses runs of whitespace and caps the entry (task #379). */
export const JOURNAL_MAX_CHARS = 240;
/** The hub refuses a raw entry longer than this before collapsing. */
export const JOURNAL_MAX_RAW = 4_000;

export function journalLength(entry: string): number {
  return entry.split(/\s+/).filter(Boolean).join(" ").length;
}

export function journalProblem(entry: string): string | null {
  const length = journalLength(entry);
  if (length === 0) return "Write the entry first.";
  if (length > JOURNAL_MAX_CHARS) return `The entry is ${length} characters; the journal takes at most ${JOURNAL_MAX_CHARS}. Put the detail in a memory note and journal a pointer to it.`;
  if (entry.length > JOURNAL_MAX_RAW) return `The entry is too long as typed (more than ${JOURNAL_MAX_RAW} characters with spacing).`;
  return null;
}

export function todayIso(now = new Date()): string {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** Short local date and time for a hub timestamp; the raw value when it does not parse. */
export function formatWhen(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// --- Edit conflicts ------------------------------------------------------------

/** The editable fields of a note, as the editor holds them. */
export type NoteSnapshot = { content: string; title: string; tags: string; confidence: string; updatedAt: string };

export function snapshotOf(note: {
  content: string;
  title?: string | null;
  tags?: unknown;
  confidence?: number | null;
  updated_at?: string | null;
}): NoteSnapshot {
  return {
    content: note.content,
    title: note.title ?? "",
    tags: noteTags(note).join(", "),
    confidence: typeof note.confidence === "number" ? String(note.confidence) : "",
    updatedAt: note.updated_at ?? "",
  };
}

/**
 * True when the note on the hub is not the one the editor opened. The hub
 * takes no revision on `memory/save`, so the editor re-reads the note and
 * compares the text, title, tags, confidence and change time before a replace.
 */
export function noteChangedSince(base: NoteSnapshot, current: NoteSnapshot): boolean {
  return (
    base.content !== current.content ||
    base.title !== current.title ||
    base.tags !== current.tags ||
    base.confidence !== current.confidence ||
    base.updatedAt !== current.updatedAt
  );
}

export type EditFields = Pick<NoteSnapshot, "content" | "title" | "tags" | "confidence">;

/** Which editable fields differ between two versions, in plain words. */
export function differingFields(a: EditFields, b: EditFields): string[] {
  const out: string[] = [];
  if (a.title !== b.title) out.push("title");
  if (a.content !== b.content) out.push("text");
  if (a.tags !== b.tags) out.push("tags");
  if (a.confidence !== b.confidence) out.push("confidence");
  return out;
}

export type DiffLine = { kind: "added" | "removed"; text: string };

/** Above this, the panels show the two versions without a line diff. */
export const DIFF_MAX_CHARS = 20_000;
export const DIFF_MAX_CELLS = 1_000_000;

/**
 * Lines removed from `before` and added in `after` (longest common
 * subsequence, line by line), or null when the texts are too long to compare
 * here.
 */
export function lineDiff(before: string, after: string): DiffLine[] | null {
  if (before.length > DIFF_MAX_CHARS || after.length > DIFF_MAX_CHARS) return null;
  const a = before.split("\n");
  const b = after.split("\n");
  const n = a.length;
  const m = b.length;
  if (n * m > DIFF_MAX_CELLS) return null;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) out.push({ kind: "removed", text: a[i++] });
    else out.push({ kind: "added", text: b[j++] });
  }
  while (i < n) out.push({ kind: "removed", text: a[i++] });
  while (j < m) out.push({ kind: "added", text: b[j++] });
  return out;
}
