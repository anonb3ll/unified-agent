/**
 * Memory at `/knowledge/memory` (spec §9.5): hybrid search and filters for
 * tag, author and project, all in the URL; the selected note in the URL as
 * `?note=<key>` (keys can hold dots and slashes, so never a path segment);
 * the create form at `?new=1` and the edit form at `?note=<key>&edit=1`.
 * Retains the classic Memory pane (search, tag filter, title, tags, author,
 * updated) and adds everything it could not do.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useStreamInvalidation } from "../../lib/polling";
import { useEffect, useRef, useState } from "react";
import { fetchProposals } from "../../lib/api/proposals";
import { Badge, FilterBar, LinkButton, PageHeader, Skeleton, UiLink } from "../../lib/ui";
import { countText, useTruth } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import {
  applyLocalFilters,
  fetchMemoryList,
  hasLocalFilters,
  memoryCap,
  formatWhen,
  KNOWLEDGE_KEYS,
  memoryNoteHref,
  noteTags,
  optionValues,
  projectsOf,
  PROPOSALS_INBOX_HREF,
  serverTagFor,
  type MemoryFilters,
  type MemoryRow,
} from "./api";
import { MemoryCreateForm } from "./MemoryForms";
import { MemoryNoteView } from "./MemoryNote";
import { KnowledgeRegion, KnowledgeTabs } from "./shared";

const DEFAULTS = { q: "", tag: "", author: "", project: "", note: "", new: "", edit: "" };

export function MemoryPage() {
  const { values, update } = useUrlParams(DEFAULTS);
  const filters: MemoryFilters = { q: values.q, tag: values.tag, author: values.author, project: values.project };
  const serverTag = serverTagFor(filters);
  const list = useQuery({
    queryKey: KNOWLEDGE_KEYS.memoryList(filters.q.trim(), serverTag),
    queryFn: ({ signal }) => fetchMemoryList({ q: filters.q, tag: serverTag }, signal),
    staleTime: 15_000,
    refetchInterval: 120_000,
  });
  // Agents save notes all the time: refetch when the hub stream reports a memory change.
  const queryClient = useQueryClient();
  useStreamInvalidation(() => void queryClient.invalidateQueries({ queryKey: [...KNOWLEDGE_KEYS.memoryAll, "list"] }), { channels: ["agent.memory*"] });
  const rows = list.data ? applyLocalFilters(list.data.results, filters) : [];
  const cap = memoryCap(list.data?.mode);
  // The hub stopped at its cap: more notes may match than the rows it sent.
  const capped = Boolean(list.data && list.data.results.length >= cap);
  const localFiltered = hasLocalFilters(filters);
  const detailOpen = Boolean(values.note || values.new);

  const keep = new URLSearchParams();
  for (const key of ["q", "tag", "author", "project"] as const) if (values[key]) keep.set(key, values[key]);
  const newHref = `/knowledge/memory?${new URLSearchParams([...keep, ["new", "1"]])}`;

  return (
    <>
      <PageHeader
        title="Memory"
        description="Notes that agents and people save so the next session does not start from zero. Search them, read one in full, and write or correct them."
        actions={
          <LinkButton href={newHref} variant="primary">
            New note
          </LinkButton>
        }
        tabs={<KnowledgeTabs />}
      />
      <ProposalsNotice />
      <MemoryFiltersBar filters={filters} rows={list.data?.results ?? []} onChange={update} />
      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <section aria-labelledby="memory-list-heading" className={`min-w-0 ${detailOpen ? "max-lg:hidden" : ""}`}>
          <h2 id="memory-list-heading" className="sr-only">
            Notes
          </h2>
          <KnowledgeRegion
            query={list}
            loadingLabel="Loading memory notes"
            errorTitle="Memory notes could not be loaded"
            isEmpty={() => rows.length === 0}
            empty={
              capped && localFiltered
                ? {
                    title: `None of the top ${cap} ${list.data?.mode === "search" ? "matches" : "notes"} fit these filters`,
                    description: `More notes may match. The hub returns only its top ${cap}, and the author filter (and a project together with a tag) is applied here to those. Refine the search or the tag to reach the others.`,
                  }
                : filters.q || filters.tag || filters.author || filters.project
                ? { title: "No notes match", description: "Change the search or remove a filter to see more notes." }
                : {
                    title: "No memory notes yet",
                    description: "Agents save notes here as they work. You can write the first one yourself.",
                    action: (
                      <LinkButton href={newHref} variant="primary">
                        Write a note
                      </LinkButton>
                    ),
                  }
            }
          >
            {(data, truth) => (
              <>
                <p className="m-0 mb-2 text-xs text-[var(--ua-muted)]" aria-live="polite">
                  {data.mode === "search" ? "Best matches first" : "Newest first"} ·{" "}
                  {capped
                    ? `${data.mode === "search" ? `Showing the top ${cap} matches` : `Showing the newest ${cap} notes`}; refine the search to see others.${
                        localFiltered ? ` ${rows.length} of them fit your other filters.` : ""
                      }`
                    : `${countText(truth, rows.length)} ${rows.length === 1 ? "note" : "notes"}`}
                </p>
                <MemoryList rows={rows} selected={values.note} keep={keep} />
              </>
            )}
          </KnowledgeRegion>
        </section>
        <div className={`min-w-0 ${detailOpen ? "" : "max-lg:hidden"}`}>
          {detailOpen && (
            <p className="m-0 mb-2 lg:hidden">
              <UiLink href={`/knowledge/memory${keep.size ? `?${keep}` : ""}`} className="text-sm text-[var(--ua-link)] underline">
                Back to the list
              </UiLink>
            </p>
          )}
          {values.new ? (
            <MemoryCreateForm cancelHref={`/knowledge/memory${keep.size ? `?${keep}` : ""}`} keep={keep} />
          ) : values.note ? (
            <MemoryNoteView noteKey={values.note} editing={values.edit === "1"} keep={keep} />
          ) : (
            <div className="rounded-lg border border-dashed border-[var(--ua-border-strong)] p-6 text-sm text-[var(--ua-muted)]">
              <h2 className="m-0 mb-1 text-base font-semibold text-[var(--ua-fg)]">No note open</h2>
              Pick a note from the list to read it in full, with its tags, author, history and links.
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function MemoryFiltersBar({
  filters,
  rows,
  onChange,
}: {
  filters: MemoryFilters;
  rows: readonly MemoryRow[];
  onChange: (changes: Partial<Record<keyof typeof DEFAULTS, string | null>>) => void;
}) {
  // Typing updates the URL after a pause, so each key press does not start a search.
  const [text, setText] = useState(filters.q);
  const pushed = useRef(filters.q);
  useEffect(() => {
    if (filters.q !== pushed.current) {
      pushed.current = filters.q;
      setText(filters.q);
    }
  }, [filters.q]);
  useEffect(() => {
    if (text === pushed.current) return;
    const timer = setTimeout(() => {
      pushed.current = text;
      onChange({ q: text });
    }, 350);
    return () => clearTimeout(timer);
  }, [text, onChange]);

  const tags = optionValues(
    rows.flatMap((r) => noteTags(r)).filter((t) => !t.startsWith("project:") && !t.startsWith("meta:")),
    filters.tag,
  );
  const authors = optionValues(
    rows.map((r) => r.author ?? ""),
    filters.author,
  );
  const projects = optionValues(
    rows.flatMap((r) => projectsOf(r)),
    filters.project,
  );
  return (
    <FilterBar
      label="Memory search and filters"
      search={{ label: "Search memory", value: text, onChange: setText, placeholder: "Search by meaning or words" }}
      filters={[
        { id: "tag", label: "Tag", value: filters.tag, anyLabel: "Any tag", options: tags.map((t) => ({ value: t, label: t })), onChange: (v) => onChange({ tag: v }) },
        { id: "author", label: "Author", value: filters.author, anyLabel: "Anyone", options: authors.map((a) => ({ value: a, label: a })), onChange: (v) => onChange({ author: v }) },
        { id: "project", label: "Project", value: filters.project, anyLabel: "Any project", options: projects.map((p) => ({ value: p, label: p })), onChange: (v) => onChange({ project: v }) },
      ]}
      onClearAll={() => {
        pushed.current = "";
        setText("");
        onChange({ q: null, tag: null, author: null, project: null });
      }}
    />
  );
}

function MemoryList({ rows, selected, keep }: { rows: readonly MemoryRow[]; selected: string; keep: URLSearchParams }) {
  return (
    <ul className="m-0 flex list-none flex-col gap-2 p-0">
      {rows.map((row) => {
        const current = row.id === selected;
        const tags = noteTags(row).filter((t) => !t.startsWith("meta:"));
        return (
          <li key={row.id}>
            <UiLink
              href={memoryNoteHref(row.id, keep)}
              aria-current={current ? "page" : undefined}
              className={`block rounded-md border px-3 py-2 no-underline focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] ${
                current ? "border-[var(--ua-accent)] bg-[var(--ua-selected)]" : "border-[var(--ua-border)] bg-[var(--ua-surface)] hover:bg-[var(--ua-hover)]"
              }`}
            >
              <span className="block break-words text-sm font-medium text-[var(--ua-fg)]">{row.title || "Untitled note"}</span>
              {row.snippet && <span className="mt-0.5 line-clamp-2 block break-words text-xs text-[var(--ua-muted)]">{row.snippet}</span>}
              <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-[var(--ua-muted)]">
                <span>{row.author ? `By ${row.author}` : "Author not recorded"}</span>
                {row.updated_at && <span>· Updated {formatWhen(row.updated_at)}</span>}
                {tags.slice(0, 4).map((t) => (
                  <Badge key={t} variant="neutral">
                    {t}
                  </Badge>
                ))}
                {tags.length > 4 && <span>+{tags.length - 4} more</span>}
              </span>
            </UiLink>
          </li>
        );
      })}
    </ul>
  );
}

/** Memory proposals wait in the Inbox (spec §9.5); the count shows only after a complete read. */
function ProposalsNotice() {
  const proposals = useQuery({ queryKey: KNOWLEDGE_KEYS.proposals, queryFn: ({ signal }) => fetchProposals(signal), staleTime: 30_000 });
  const truth = useTruth([proposals]);
  if (truth.state === "loading") return <Skeleton label="Checking for memory proposals" lines={1} className="mb-3" />;
  const failed = truth.state === "denied" || truth.state === "unavailable" || !proposals.data;
  const count = proposals.data?.proposals.length ?? 0;
  if (!failed && count === 0) return null;
  return (
    <p className="m-0 mb-3 rounded-md border border-[var(--ua-border)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
      {failed ? (
        <>
          Proposed memory and skill changes could not be checked.{" "}
          <UiLink href={PROPOSALS_INBOX_HREF} className="text-[var(--ua-link)] underline">
            Open proposals in the Inbox
          </UiLink>
        </>
      ) : (
        <>
          {countText(truth, count)} proposed {count === 1 ? "change to memory or skills is" : "changes to memory or skills are"} waiting for a decision.{" "}
          <UiLink href={PROPOSALS_INBOX_HREF} className="text-[var(--ua-link)] underline">
            Review in the Inbox
          </UiLink>
        </>
      )}
    </p>
  );
}
