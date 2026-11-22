/**
 * Vault at `/knowledge/vault` (spec §9.5): a folder tree (a plain nested
 * list of links, with disclosure buttons for folders), a note reader at
 * `?path=<vault path>` (Markdown; `[[wikilinks]]` navigate inside the app;
 * backlinks from `GET /api/vault/links`), search at `?q=`, ingest status
 * per note, and the write forms: `?write=new`, `?path=…&write=append` and
 * `?write=journal`. With nothing selected it shows today's journal and the
 * Inbox captures' ingest state, as the classic Vault pane did.
 */
import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { Badge, Button, FilterBar, LinkButton, Markdown, PageHeader, UiLink } from "../../lib/ui";
import { useEpoch } from "../lib/epoch";
import { useUrlParams } from "../lib/urlState";
import {
  ancestorDirs,
  baseName,
  DEFAULT_WRITE_FOLDERS,
  fetchVaultBriefing,
  fetchVaultDir,
  fetchVaultIngest,
  fetchVaultLinks,
  fetchVaultPage,
  fetchVaultSearch,
  HubReadError,
  formatWhen,
  INGEST_STATUS_TEXT,
  isInFolders,
  lockState,
  KNOWLEDGE_KEYS,
  noteTitle,
  resolveWikilink,
  vaultNoteHref,
  wikilinkHref,
  type VaultEntry,
} from "./api";
import { KnowledgeRegion, KnowledgeTabs, Section, useFocusRequest } from "./shared";
import { VaultAppendForm, VaultJournalForm, VaultNewNoteForm } from "./VaultForms";

const DEFAULTS = { path: "", q: "", write: "", dir: "", link: "", from: "" };
/**
 * The hub publishes no vault events on its stream, and agents and Obsidian
 * Sync change the vault all the time, so the open views poll.
 */
const VAULT_POLL_MS = 60_000;
const FOLDERS_KEY = "ua-cc2:knowledge:write-folders";

/**
 * The folders the hub writes to. The hub has no read for them, so this
 * starts from the documented default and learns the real list from the
 * hub's refusal, which names it (kept for this tab).
 */
function useWriteFolders() {
  const epoch = useEpoch();
  const [folders, setFolders] = useState<string[]>(() => {
    const stored = epoch.storage.read<unknown>(FOLDERS_KEY, null);
    return Array.isArray(stored) && stored.every((s) => typeof s === "string") ? (stored as string[]) : [...DEFAULT_WRITE_FOLDERS];
  });
  const learned = (next: string[]) => {
    setFolders(next);
    epoch.storage.write(FOLDERS_KEY, next);
  };
  return { folders, learned };
}

export function VaultPage() {
  const { values, update } = useUrlParams(DEFAULTS);
  const { folders, learned } = useWriteFolders();
  const writeHref = (extra: Record<string, string>) => `/knowledge/vault?${new URLSearchParams(extra)}`;
  const detailOpen = Boolean(values.path || values.write || values.link || values.q);
  const onSearch = useCallback((q: string) => update({ q, path: null, write: null, link: null, from: null }), [update]);

  let main;
  if (values.write === "new") main = <VaultNewNoteForm folders={folders} initialFolder={values.dir} onFoldersLearned={learned} />;
  else if (values.write === "journal") main = <VaultJournalForm />;
  else if (values.link) main = <WikilinkResolver target={values.link} from={values.from} />;
  else if (values.path) main = <NoteReader path={values.path} appending={values.write === "append"} folders={folders} onFoldersLearned={learned} />;
  else if (values.q.trim()) main = <SearchResults q={values.q.trim()} />;
  else main = <Briefing />;

  return (
    <>
      <PageHeader
        title="Vault"
        description="The Obsidian vault the hub reads: browse folders, read notes and their links, and write to the folders agents may change."
        actions={
          <>
            <LinkButton href={writeHref({ write: "new" })} variant="primary">
              New note
            </LinkButton>
            <LinkButton href={writeHref({ write: "journal" })} variant="secondary">
              Add journal entry
            </LinkButton>
          </>
        }
        tabs={<KnowledgeTabs />}
      />
      <VaultSearchBar q={values.q} onChange={onSearch} />
      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(14rem,1fr)_minmax(0,3fr)]">
        <nav aria-label="Vault folders" className={`min-w-0 ${detailOpen ? "max-lg:hidden" : ""}`}>
          <h2 className="m-0 mb-2 text-sm font-semibold">Folders</h2>
          <FolderTree current={values.path} />
        </nav>
        <div className="min-w-0">
          {detailOpen && (
            <p className="m-0 mb-2 lg:hidden">
              <UiLink href="/knowledge/vault" className="text-sm text-[var(--ua-link)] underline">
                Back to the folders
              </UiLink>
            </p>
          )}
          {main}
        </div>
      </div>
    </>
  );
}

function VaultSearchBar({ q, onChange }: { q: string; onChange: (q: string) => void }) {
  const [text, setText] = useState(q);
  const pushed = useRef(q);
  useEffect(() => {
    if (q !== pushed.current) {
      pushed.current = q;
      setText(q);
    }
  }, [q]);
  useEffect(() => {
    if (text === pushed.current) return;
    const timer = setTimeout(() => {
      pushed.current = text;
      onChange(text);
    }, 400);
    return () => clearTimeout(timer);
  }, [text, onChange]);
  return (
    <FilterBar
      label="Vault search"
      search={{ label: "Search the vault", value: text, onChange: setText, placeholder: "Search note text (regular expressions work)" }}
      onClearAll={() => {
        pushed.current = "";
        setText("");
        onChange("");
      }}
    />
  );
}

// --- Folder tree --------------------------------------------------------------

function FolderTree({ current }: { current: string }) {
  const [open, setOpen] = useState<Set<string>>(() => new Set(ancestorDirs(current)));
  useEffect(() => {
    const needed = ancestorDirs(current);
    if (needed.some((d) => !open.has(d))) setOpen((prev) => new Set([...prev, ...needed]));
    // Only a new selection opens its folders; closing one by hand stays closed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);
  const toggle = (dir: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(dir)) next.delete(dir);
      else next.add(dir);
      return next;
    });
  const root = useQuery({ queryKey: KNOWLEDGE_KEYS.vaultDir(""), queryFn: ({ signal }) => fetchVaultDir("", signal), staleTime: 30_000, refetchInterval: VAULT_POLL_MS });
  return (
    <KnowledgeRegion
      query={root}
      loadingLabel="Loading the vault folders"
      errorTitle="The folders could not be loaded"
      headingLevel={3}
      isEmpty={(d) => d.entries.length === 0}
      empty={{ title: "The vault is empty", description: "Write a note into one of the folders agents may change to start it." }}
    >
      {(data) => <FolderEntries dir="" entries={data.entries} truncated={data.truncated} current={current} open={open} onToggle={toggle} />}
    </KnowledgeRegion>
  );
}

function FolderEntries({
  dir,
  entries,
  truncated,
  current,
  open,
  onToggle,
}: {
  dir: string;
  entries: readonly VaultEntry[];
  truncated?: boolean;
  current: string;
  open: ReadonlySet<string>;
  onToggle: (dir: string) => void;
}) {
  const sorted = [...entries].sort((a, b) => (a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1));
  return (
    <ul className={`m-0 list-none p-0 ${dir ? "ml-3 border-0 border-l border-[var(--ua-border)] pl-2" : ""}`}>
      {sorted.map((entry) => {
        const path = dir ? `${dir}/${entry.name}` : entry.name;
        if (entry.type === "dir") {
          const expanded = open.has(path);
          const listId = `vault-dir-${path.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
          return (
            <li key={path}>
              <Button
                variant="ghost"
                size="sm"
                aria-expanded={expanded}
                aria-controls={expanded ? listId : undefined}
                onClick={() => onToggle(path)}
                className="w-full justify-start text-left"
              >
                <span aria-hidden="true" className="inline-block w-3">
                  {expanded ? "▾" : "▸"}
                </span>
                <span className="sr-only">Folder </span>
                {entry.name}
              </Button>
              {expanded && (
                <div id={listId}>
                  <SubFolder dir={path} current={current} open={open} onToggle={onToggle} />
                </div>
              )}
            </li>
          );
        }
        const selected = path === current;
        return (
          <li key={path}>
            <UiLink
              href={vaultNoteHref(path)}
              aria-current={selected ? "page" : undefined}
              className={`block rounded px-2 py-1 text-sm no-underline focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] max-md:py-2.5 ${
                selected ? "bg-[var(--ua-selected)] font-medium text-[var(--ua-fg)]" : "text-[var(--ua-link)] hover:bg-[var(--ua-hover)]"
              }`}
            >
              {entry.name}
            </UiLink>
          </li>
        );
      })}
      {truncated && <li className="px-2 py-1 text-xs text-[var(--ua-muted)]">More notes are in this folder than the hub lists at once. Use search to find them.</li>}
    </ul>
  );
}

function SubFolder({ dir, current, open, onToggle }: { dir: string; current: string; open: ReadonlySet<string>; onToggle: (dir: string) => void }) {
  const query = useQuery({ queryKey: KNOWLEDGE_KEYS.vaultDir(dir), queryFn: ({ signal }) => fetchVaultDir(dir, signal), staleTime: 30_000 });
  if (query.status === "pending") {
    return (
      <p role="status" className="m-0 ml-5 py-1 text-xs text-[var(--ua-muted)]">
        Loading {baseName(dir)}…
      </p>
    );
  }
  if (query.status === "error") {
    return (
      <div className="ml-5 flex flex-wrap items-center gap-2 py-1 text-xs">
        <span>This folder could not be loaded.</span>
        <Button size="sm" variant="secondary" onClick={() => void query.refetch()}>
          Retry
        </Button>
      </div>
    );
  }
  if (query.data.entries.length === 0) return <p className="m-0 ml-5 py-1 text-xs text-[var(--ua-muted)]">Empty folder</p>;
  return <FolderEntries dir={dir} entries={query.data.entries} truncated={query.data.truncated} current={current} open={open} onToggle={onToggle} />;
}

// --- Reader ------------------------------------------------------------------

function NoteReader({
  path,
  appending,
  folders,
  onFoldersLearned,
}: {
  path: string;
  appending: boolean;
  folders: readonly string[];
  onFoldersLearned: (folders: string[]) => void;
}) {
  const page = useQuery({
    queryKey: KNOWLEDGE_KEYS.vaultPage(path),
    queryFn: ({ signal }) => fetchVaultPage(path, signal),
    staleTime: 15_000,
    refetchInterval: VAULT_POLL_MS,
  });
  useFocusRequest("vault-append-link", page.isSuccess);
  return (
    <KnowledgeRegion
      query={page}
      loadingLabel="Loading the note"
      errorTitle={page.error instanceof HubReadError && page.error.status === 404 ? "This note does not exist" : "The note could not be loaded"}
      empty={{ title: "Empty" }}
    >
      {(data) => {
        const lock = lockState(data.content);
        const locked = lock === "locked";
        const writable = isInFolders(data.path, folders);
        return (
          <div className="flex min-w-0 flex-col gap-4">
            <Section
              title={noteTitle(data.path)}
              id="vault-note-heading"
              actions={
                writable && !locked && !appending ? (
                  <LinkButton id="vault-append-link" href={`${vaultNoteHref(data.path)}&write=append`} size="sm" variant="primary">
                    Add to this note
                  </LinkButton>
                ) : undefined
              }
            >
              <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--ua-muted)]">
                <span className="break-all">{data.path}</span>
                {locked && <Badge variant="warning">Locked</Badge>}
                {lock === "uncertain" && <Badge variant="warning">May be locked</Badge>}
                <IngestBadge path={data.path} revision={data.revision ?? ""} />
              </div>
              {locked && (
                <p role="note" className="m-0 text-sm">
                  This note is locked: its front matter says <code>locked: true</code>, so the hub refuses changes to it. Only the vault’s owner can unlock it in Obsidian.
                </p>
              )}
              {lock === "uncertain" && (
                <p role="note" className="m-0 text-sm">
                  This note may be locked: its front matter mentions <code>locked</code> in a form this page cannot read for certain. You can still add to it,
                  but the hub may refuse.
                </p>
              )}
              {!locked && !writable && (
                <p className="m-0 text-xs text-[var(--ua-muted)]">
                  Read only here: the hub writes only to {folders.length ? folders.join(", ") : "no folders"}.
                </p>
              )}
              {appending && writable && !locked && <VaultAppendForm path={data.path} current={data.content} revision={data.revision ?? ""} onFoldersLearned={onFoldersLearned} />}
              {data.truncated && (
                <p className="m-0 text-xs text-[var(--ua-muted)]">
                  This is the start of a long note{typeof data.total_lines === "number" ? ` (${data.total_lines} lines)` : ""}. Open it in Obsidian to read the rest.
                </p>
              )}
              <div className="rounded-md border border-[var(--ua-border)] bg-[var(--ua-bg)] p-3">
                <Markdown
                  source={data.content}
                  headingOffset={2}
                  renderWikilink={(target, label) => (
                    <UiLink href={wikilinkHref(target, data.path)} className="text-[var(--ua-link)] underline">
                      {label}
                    </UiLink>
                  )}
                />
              </div>
            </Section>
            <NoteLinksSection path={data.path} revision={data.revision ?? ""} />
          </div>
        );
      }}
    </KnowledgeRegion>
  );
}

/**
 * The Wiki status of the note on screen. Re-read whenever the note's revision
 * changes, and on the reader's poll, so new text never sits beside an old
 * "In the Wiki" badge.
 */
function IngestBadge({ path, revision }: { path: string; revision: string }) {
  const ingest = useQuery({
    queryKey: KNOWLEDGE_KEYS.vaultIngest(path, revision),
    queryFn: ({ signal }) => fetchVaultIngest(path, signal),
    staleTime: 30_000,
    refetchInterval: VAULT_POLL_MS,
    // While the new revision loads, keep this note's last answer (never another note's).
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[3] === path ? previous : undefined),
  });
  if (ingest.status === "pending") return <span role="status">Checking Wiki status…</span>;
  if (ingest.status === "error")
    return (
      <span className="inline-flex items-center gap-1">
        Wiki status unknown
        <Button size="sm" variant="ghost" onClick={() => void ingest.refetch()} aria-label="Retry the Wiki status">
          Retry
        </Button>
      </span>
    );
  const info = INGEST_STATUS_TEXT[ingest.data.status ?? ""];
  if (!info) return <span>Wiki status: {ingest.data.status ?? "unknown"}</span>;
  return (
    <span className="inline-flex flex-wrap items-center gap-1" title={info.meaning}>
      <Badge variant={info.tone}>{info.label}</Badge>
      {ingest.data.last_ingested_at && <span>last folded in {formatWhen(ingest.data.last_ingested_at)}</span>}
      <span className="sr-only">{info.meaning}</span>
    </span>
  );
}

/** Links follow the note's revision; backlinks change with other notes, so they poll too. */
function NoteLinksSection({ path, revision }: { path: string; revision: string }) {
  const links = useQuery({
    queryKey: KNOWLEDGE_KEYS.vaultLinks(path, revision),
    queryFn: ({ signal }) => fetchVaultLinks(path, signal),
    staleTime: 30_000,
    refetchInterval: VAULT_POLL_MS,
    // While the new revision loads, keep this note's last answer (never another note's).
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[3] === path ? previous : undefined),
  });
  return (
    <Section title="Links" id="vault-links-heading">
      <KnowledgeRegion
        query={links}
        loadingLabel="Loading the links"
        errorTitle="The links could not be loaded"
        headingLevel={3}
        isEmpty={(d) => d.backlinks.length === 0 && d.outgoing.length === 0}
        empty={{ title: "No links", description: "This note links to no other notes, and no note links to it." }}
      >
        {(data) => (
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <h3 className="m-0 mb-1 text-sm font-semibold">Notes that link here ({data.backlinks.length})</h3>
              {data.backlinks.length === 0 ? (
                <p className="m-0 text-sm text-[var(--ua-muted)]">None.</p>
              ) : (
                <ul className="m-0 flex list-none flex-col gap-1 p-0">
                  {data.backlinks.map((b) => (
                    <li key={b} className="text-sm">
                      <UiLink href={vaultNoteHref(b)} className="break-all text-[var(--ua-link)] underline">
                        {b}
                      </UiLink>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <h3 className="m-0 mb-1 text-sm font-semibold">This note links to ({data.outgoing.length})</h3>
              {data.outgoing.length === 0 ? (
                <p className="m-0 text-sm text-[var(--ua-muted)]">None.</p>
              ) : (
                <ul className="m-0 flex list-none flex-col gap-1 p-0">
                  {data.outgoing.map((o) => (
                    <li key={o} className="text-sm">
                      <UiLink href={wikilinkHref(o, path)} className="break-all text-[var(--ua-link)] underline">
                        {o}
                      </UiLink>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </KnowledgeRegion>
    </Section>
  );
}

/** Finds the note a `[[wikilink]]` names and replaces the URL with its path. */
function WikilinkResolver({ target, from }: { target: string; from: string }) {
  const navigate = useNavigate();
  const resolved = useQuery({
    queryKey: KNOWLEDGE_KEYS.vaultResolve(target, from),
    queryFn: ({ signal }) => resolveWikilink(target, from, signal),
    staleTime: 30_000,
  });
  useEffect(() => {
    if (resolved.data) navigate(vaultNoteHref(resolved.data), { replace: true });
  }, [resolved.data, navigate]);
  return (
    <KnowledgeRegion query={resolved} loadingLabel={`Finding the note “${target}”`} errorTitle="The link could not be followed" isEmpty={(d) => d === null} empty={{
      title: `No note named “${target}”`,
      description: (
        <>
          The vault has no note at that name next to {from ? <UiLink href={vaultNoteHref(from)} className="text-[var(--ua-link)] underline">{noteTitle(from)}</UiLink> : "the linking note"} or at the top of the vault. Search for it instead.
        </>
      ),
      action: (
        <LinkButton href={`/knowledge/vault?${new URLSearchParams({ q: target.split(/[#^]/)[0] })}`} variant="secondary">
          Search for “{target.split(/[#^]/)[0]}”
        </LinkButton>
      ),
    }}>
      {() => (
        <p role="status" className="m-0 text-sm">
          Opening the note…
        </p>
      )}
    </KnowledgeRegion>
  );
}

// --- Search ------------------------------------------------------------------

function SearchResults({ q }: { q: string }) {
  const search = useQuery({ queryKey: KNOWLEDGE_KEYS.vaultSearch(q), queryFn: ({ signal }) => fetchVaultSearch(q, signal), staleTime: 15_000 });
  return (
    <Section title={`Search results for “${q}”`} id="vault-search-heading">
      <KnowledgeRegion
        query={search}
        loadingLabel="Searching the vault"
        errorTitle="The search could not run"
        headingLevel={3}
        isEmpty={(d) => d.matches.length === 0}
        empty={{ title: "No notes match", description: "Try fewer or different words. The search matches note text line by line." }}
      >
        {(data) => (
          <>
            <p className="m-0 text-xs text-[var(--ua-muted)]" aria-live="polite">
              {data.matches.length} matching {data.matches.length === 1 ? "line" : "lines"}
              {data.truncated ? " (the first 100; narrow the search to see others)" : ""}
            </p>
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {data.matches.map((m, i) => (
                <li key={`${m.path}:${m.line_no ?? i}`} className="rounded-md border border-[var(--ua-border)] px-3 py-2">
                  <UiLink href={vaultNoteHref(m.path)} className="break-all text-sm text-[var(--ua-link)] underline">
                    {m.path}
                  </UiLink>
                  {typeof m.line_no === "number" && <span className="text-xs text-[var(--ua-muted)]"> · line {m.line_no}</span>}
                  {m.line && <p className="m-0 mt-1 break-words font-mono text-xs text-[var(--ua-muted)]">{m.line}</p>}
                </li>
              ))}
            </ul>
          </>
        )}
      </KnowledgeRegion>
    </Section>
  );
}

// --- Landing: today's journal and the Inbox captures --------------------------

function Briefing() {
  const briefing = useQuery({
    queryKey: KNOWLEDGE_KEYS.vaultBriefing,
    queryFn: ({ signal }) => fetchVaultBriefing(signal),
    staleTime: 30_000,
    refetchInterval: VAULT_POLL_MS,
  });
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Section
        title={`Today’s journal${briefing.data?.date ? `, ${briefing.data.date}` : ""}`}
        id="vault-journal-heading"
        actions={
          <LinkButton href="/knowledge/vault?write=journal" size="sm">
            Add entry
          </LinkButton>
        }
      >
        <p className="m-0 text-sm text-[var(--ua-muted)]">Short entries agents and people add to the day’s log, merged from every machine.</p>
        <KnowledgeRegion
          query={briefing}
          loadingLabel="Loading today’s journal"
          errorTitle="The journal could not be loaded"
          headingLevel={3}
          isEmpty={(d) => d.entries.length === 0}
          empty={{ title: "No journal entries today", description: "Entries appear here as agents and people add them." }}
        >
          {(data) => (
            <ol className="m-0 flex list-none flex-col gap-2 p-0">
              {data.entries.map((e, i) => (
                <li key={i} className="text-sm">
                  <span className="text-xs text-[var(--ua-muted)]">
                    {[e.time, e.agent, e.machine].filter(Boolean).join(" · ")}
                  </span>
                  <p className="m-0 break-words">{e.text}</p>
                </li>
              ))}
            </ol>
          )}
        </KnowledgeRegion>
      </Section>
      <Section title="Inbox captures" id="vault-inbox-heading">
        <p className="m-0 text-sm text-[var(--ua-muted)]">Notes captured into the vault Inbox, and whether each one has been folded into the Wiki yet.</p>
        <KnowledgeRegion
          query={briefing}
          loadingLabel="Loading the Inbox captures"
          errorTitle="The Inbox captures could not be loaded"
          headingLevel={3}
          isEmpty={(d) => d.inbox.length === 0}
          empty={{ title: "The Inbox is clear", description: "No captures are waiting in the vault Inbox." }}
        >
          {(data) => (
            <ul className="m-0 flex list-none flex-col gap-1 p-0">
              {data.inbox.map((row, i) => {
                const info = INGEST_STATUS_TEXT[row.status ?? ""];
                return (
                  <li key={row.path ?? i} className="flex flex-wrap items-center gap-2 text-sm">
                    {row.path ? (
                      <UiLink href={vaultNoteHref(row.path)} className="break-all text-[var(--ua-link)] underline">
                        {row.path}
                      </UiLink>
                    ) : (
                      <span>Unnamed capture</span>
                    )}
                    <Badge variant={info?.tone ?? "neutral"}>{info?.label ?? row.status ?? "Unknown"}</Badge>
                  </li>
                );
              })}
            </ul>
          )}
        </KnowledgeRegion>
      </Section>
    </div>
  );
}
