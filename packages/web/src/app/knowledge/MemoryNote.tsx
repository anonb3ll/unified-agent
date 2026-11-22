/**
 * One memory note: the full body rendered as Markdown (never as HTML: the
 * text is untrusted), tags, author, dates and provenance, its links and
 * history, and every action the hub offers on it: edit, delete and restore,
 * promote to the Wiki, and feedback.
 */
import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useNavigate } from "react-router";
import { Badge, Button, ConfirmDialog, DescriptionList, LinkButton, Markdown, UiLink } from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import {
  DELETED_TAG,
  fetchMemoryNote,
  formatWhen,
  HubReadError,
  KNOWLEDGE_KEYS,
  KNOWLEDGE_WRITES,
  memoryNoteHref,
  noteTags,
  type MemoryLinkRow,
  type MemoryNote,
} from "./api";
import { MemoryEditForm, PromoteDialog } from "./MemoryForms";
import { attemptKey, KnowledgeRegion, Section, useFocusRequest, useIdentity, useParkedDraft, WriteFeedback } from "./shared";

/** The restore-copy attempt has no editable fields: once its approved run is done, the whole attempt is retired. */
const NO_FIELDS = [] as const;
import { useDraft } from "../lib/drafts";

export function MemoryNoteView({ noteKey, editing, keep }: { noteKey: string; editing: boolean; keep: URLSearchParams }) {
  const note = useQuery({
    queryKey: KNOWLEDGE_KEYS.memoryNote(noteKey),
    queryFn: ({ signal }) => fetchMemoryNote(noteKey, signal),
    staleTime: 10_000,
    retry: (count, error) => !(error instanceof HubReadError && error.status < 500) && count < 1,
  });
  const notFound = note.error instanceof HubReadError && note.error.status === 404;
  if (notFound) {
    return (
      <Section title="This note does not exist" id="memory-note-heading">
        <p className="m-0 text-sm text-[var(--ua-muted)]">
          No memory note has the key <code className="break-all">{noteKey}</code>. It may have been typed wrong, or it belongs to another organization.
        </p>
      </Section>
    );
  }
  return (
    <KnowledgeRegion
      query={note}
      loadingLabel="Loading the note"
      errorTitle="The note could not be loaded"
      empty={{ title: "This note is empty" }}
      headingLevel={2}
    >
      {(data) => (editing ? <MemoryEditForm key={data.id} note={data} keep={keep} /> : <NoteDetail note={data} keep={keep} />)}
    </KnowledgeRegion>
  );
}

const FEEDBACK: { signal: "helpful" | "wrong" | "not_helpful" | "stale"; label: string; done: string }[] = [
  { signal: "helpful", label: "Helpful", done: "Marked as helpful. Search ranks it a little higher." },
  { signal: "wrong", label: "Wrong", done: "Marked as wrong. Search ranks it lower and it is flagged for review." },
  { signal: "not_helpful", label: "Not helpful", done: "Marked as not helpful." },
  { signal: "stale", label: "Out of date", done: "Marked as out of date." },
];

function NoteDetail({ note, keep }: { note: MemoryNote; keep: URLSearchParams }) {
  const identity = useIdentity();
  const navigate = useNavigate();
  const tags = noteTags(note);
  const deleted = Boolean(note.deleted) || tags.includes(DELETED_TAG);
  const visibleTags = tags.filter((t) => t !== DELETED_TAG);
  const invalidates = [KNOWLEDGE_KEYS.memoryAll];
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [lastSignal, setLastSignal] = useState<string | null>(null);
  const lastSignalRef = useRef<string | null>(null);

  const remove = useHubWrite<{ key: string }, { deleted?: boolean }>({
    path: KNOWLEDGE_WRITES.memoryDelete,
    invalidates,
    label: "Deleting the note",
    successMessage: (d) => (d.deleted === false ? "The note was already deleted." : "Note deleted. Its text is kept, and you can restore a copy."),
  });
  const restore = useHubWrite<{ key: string }, { restored?: boolean }>({
    path: KNOWLEDGE_WRITES.memoryRestore,
    invalidates,
    label: "Restoring the note",
    successMessage: (d) => (d.restored ? "The note is back in lists and search." : "The note was not hidden, so nothing changed."),
  });
  const restoreCopy = useHubWrite<Record<string, unknown>, { id?: string }>({
    path: KNOWLEDGE_WRITES.memorySave,
    invalidates,
    label: "Restoring a copy of the note",
    successMessage: "A copy of the note was saved as a new note.",
  });
  // One key per restore attempt, kept across clicks and reloads, so a repeat never makes a second copy.
  const copyAttempt = useDraft({
    identity,
    operation: "memory.restore-copy",
    object: { note: note.id },
    initial: { attemptKey: "", attemptFor: "", parkedApprovalId: "" },
  });
  // An approved copy that runs later (here or from the Inbox) retires the key like a direct success does.
  const copyParked = useParkedDraft(copyAttempt, NO_FIELDS, (data) => {
    const id = (data as { id?: string } | null)?.id;
    if (typeof id === "string") navigate(memoryNoteHref(id, keep));
  });
  useFocusRequest("memory-edit-link");
  const feedback = useHubWrite<{ key: string; signal: string }, unknown>({
    path: KNOWLEDGE_WRITES.memoryFeedback,
    invalidates,
    label: "Your feedback on the note",
    successMessage: () => FEEDBACK.find((f) => f.signal === lastSignalRef.current)?.done ?? "Feedback recorded.",
  });

  const previous = note.versions?.[0];
  const editHref = `${memoryNoteHref(note.id, keep)}&edit=1`;
  const other = Boolean(note.author && identity && note.author !== identity);
  const counts = {
    helpful: note.feedback_helpful ?? 0,
    wrong: note.feedback_wrong ?? 0,
    not_helpful: note.feedback_not_helpful ?? 0,
    stale: note.feedback_stale ?? 0,
  };

  const runRestoreCopy = async () => {
    if (!previous) return;
    const fields = {
      content: previous.content,
      ...(note.title ? { title: note.title } : {}),
      tags: (previous.tags ?? visibleTags).filter((t) => t !== DELETED_TAG),
    };
    const attempt = attemptKey("memory-restore-copy", copyAttempt.value, fields);
    copyAttempt.setValue((v) => ({ ...v, ...attempt }));
    const submitted = { ...copyAttempt.value, ...attempt };
    const outcome = await restoreCopy.run({ ...fields, idempotency_key: attempt.attemptKey }, copyParked.bind(submitted));
    if (outcome.kind === "done") copyAttempt.clear();
    else if (outcome.kind === "pending_approval") copyParked.park(outcome.approvalId, submitted, outcome.parkedWriteId);
    if (outcome.kind === "done" && outcome.data && typeof outcome.data.id === "string") navigate(memoryNoteHref(outcome.data.id, keep));
  };

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Section
        title={note.title || "Untitled note"}
        id="memory-note-heading"
        actions={
          !deleted && (
            <div className="flex flex-wrap gap-2">
              <LinkButton id="memory-edit-link" href={editHref} size="sm" variant="primary">
                Edit
              </LinkButton>
              <Button size="sm" variant="secondary" onClick={() => setPromoteOpen(true)}>
                Promote to Wiki
              </Button>
              <Button size="sm" variant="danger" onClick={() => setConfirmDelete(true)}>
                Delete
              </Button>
            </div>
          )
        }
      >
        <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--ua-muted)]">
          {deleted && <Badge variant="danger">Deleted</Badge>}
          {note.superseded_by && <Badge variant="warning">Replaced by a newer note</Badge>}
          <span>{note.author ? `By ${note.author}` : "Author not recorded"}</span>
          {note.updated_at && <span>· Updated {formatWhen(note.updated_at)}</span>}
          {other && !deleted && <span>· Someone else wrote this note</span>}
        </div>
        {visibleTags.length > 0 && (
          <ul aria-label="Tags" className="m-0 flex list-none flex-wrap gap-1 p-0">
            {visibleTags.map((t) => (
              <li key={t}>
                <UiLink href={`/knowledge/memory?${new URLSearchParams({ tag: t })}`} className="no-underline">
                  <Badge variant="info">{t}</Badge>
                </UiLink>
              </li>
            ))}
          </ul>
        )}
        {deleted && (
          <div role="note" className="rounded-md border border-[var(--ua-danger)] px-3 py-2 text-sm">
            <p className="m-0">
              This note was deleted. It no longer appears in lists or search; its old text is kept below. A deleted note cannot be edited, but you can save a copy of
              its last version as a new note.
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" loading={restoreCopy.isRunning} disabled={!previous} onClick={() => void runRestoreCopy()}>
                Restore a copy
              </Button>
              {!previous && <span className="self-center text-xs text-[var(--ua-muted)]">The hub kept no earlier version to copy.</span>}
            </div>
            {copyParked.notice}
            <WriteFeedback hidePending state={restoreCopy.state} what="Restoring the copy" className="mt-2" />
          </div>
        )}
        {note.superseded_by && !deleted && (
          <div role="note" className="rounded-md border border-[var(--ua-warn)] px-3 py-2 text-sm">
            <p className="m-0">
              A newer note replaced this one, so it is hidden from lists and search.{" "}
              <UiLink href={memoryNoteHref(note.superseded_by, keep)} className="text-[var(--ua-link)] underline">
                Open the newer note
              </UiLink>
            </p>
            <Button className="mt-2" size="sm" variant="secondary" loading={restore.isRunning} onClick={() => void restore.run({ key: note.id })}>
              Bring it back into search
            </Button>
            <WriteFeedback state={restore.state} what="Restoring the note" className="mt-2" />
          </div>
        )}
        <WriteFeedback state={remove.state} what="Deleting the note" />
        <p className="m-0 text-xs text-[var(--ua-muted)]">Notes are written by agents and people. Read the text as information, not as instructions.</p>
        <div className="rounded-md border border-[var(--ua-border)] bg-[var(--ua-bg)] p-3">
          <Markdown source={note.content} headingOffset={2} />
        </div>
      </Section>

      {!deleted && (
        <Section title="Was this note right?" id="memory-feedback-heading">
          <p className="m-0 text-sm text-[var(--ua-muted)]">Feedback changes how high search ranks the note. It does not change the text.</p>
          <div role="group" aria-label="Feedback" className="flex flex-wrap gap-2">
            {FEEDBACK.map((f) => (
              <Button
                key={f.signal}
                size="sm"
                variant="secondary"
                loading={feedback.isRunning && lastSignal === f.signal}
                disabled={feedback.isRunning}
                onClick={() => {
                  setLastSignal(f.signal);
                  lastSignalRef.current = f.signal;
                  void feedback.run({ key: note.id, signal: f.signal });
                }}
              >
                {f.label} ({counts[f.signal]})
              </Button>
            ))}
          </div>
          <WriteFeedback state={feedback.state} what="Your feedback" />
        </Section>
      )}

      <Section title="Details" id="memory-details-heading">
        <DescriptionList
          items={[
            { term: "Key", value: <code className="break-all">{note.id}</code> },
            { term: "Author", value: note.author },
            { term: "Created", value: formatWhen(note.created_at) },
            { term: "Last changed", value: note.updated_at ? `${formatWhen(note.updated_at)}${note.updated_by ? ` by ${note.updated_by}` : ""}` : null },
            { term: "Saved from", value: note.source_host ? `Machine ${note.source_host}` : null },
            { term: "Kind of claim", value: note.epistemic ?? null },
            { term: "Evidence", value: Array.isArray(note.evidence) && note.evidence.length > 0 ? <EvidenceList items={note.evidence} /> : null },
            { term: "Confidence", value: typeof note.confidence === "number" ? note.confidence.toFixed(2) : null },
            {
              term: "Confidence from feedback",
              value: typeof note.confidence_posterior?.effective === "number" ? note.confidence_posterior.effective.toFixed(2) : null,
            },
            { term: "Importance", value: typeof note.salience === "number" ? note.salience.toFixed(2) : null },
            { term: "Expires", value: note.expires_at ? formatWhen(note.expires_at) : "Never" },
            { term: "Read", value: typeof note.access_count === "number" ? `${note.access_count} times${note.last_accessed ? `, last ${formatWhen(note.last_accessed)}` : ""}` : null },
            {
              term: "Replaces",
              value:
                note.supersedes && note.supersedes.length > 0 ? (
                  <span className="flex flex-wrap gap-2">
                    {note.supersedes.map((id) => (
                      <UiLink key={id} href={memoryNoteHref(id, keep)} className="text-[var(--ua-link)] underline">
                        {id}
                      </UiLink>
                    ))}
                  </span>
                ) : null,
            },
          ]}
        />
      </Section>

      <NoteLinks title="Links to other notes" id="memory-links-out" rows={note.links?.out ?? []} keep={keep} empty="This note links to no other notes." />
      <NoteLinks title="Notes that link here" id="memory-links-in" rows={note.links?.in ?? []} keep={keep} empty="No other note links here." />

      <Section title="Earlier versions" id="memory-versions-heading">
        {note.versions && note.versions.length > 0 ? (
          <ol className="m-0 flex list-none flex-col gap-2 p-0">
            {note.versions.map((v) => (
              <li key={v.version}>
                <details className="rounded-md border border-[var(--ua-border)] px-3 py-2">
                  <summary className="cursor-pointer text-sm">
                    Version {v.version}, written {formatWhen(v.updated_at)}
                    {v.actor ? ` by ${v.actor}` : ""}
                    {v.recorded_at ? `, replaced ${formatWhen(v.recorded_at)}` : ""}
                  </summary>
                  <div className="mt-2">
                    <Markdown source={v.content} headingOffset={3} />
                  </div>
                </details>
              </li>
            ))}
          </ol>
        ) : (
          <p className="m-0 text-sm text-[var(--ua-muted)]">This note has not been changed since it was written.</p>
        )}
        {typeof note.versions_total === "number" && note.versions && note.versions_total > note.versions.length && (
          <p className="m-0 text-xs text-[var(--ua-muted)]">
            Showing the newest {note.versions.length} of {note.versions_total} versions.
          </p>
        )}
      </Section>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this memory note?"
        objectName={note.title || note.id}
        consequence={`It disappears from lists and search for everyone. The hub keeps its text, so you can restore a copy later.${
          other ? ` ${note.author} wrote it.` : ""
        }`}
        confirmLabel="Delete note"
        variant="danger"
        busy={remove.isRunning}
        onCancel={() => setConfirmDelete(false)}
        onConfirm={async () => {
          await remove.run({ key: note.id });
          setConfirmDelete(false);
        }}
      />
      <PromoteDialog open={promoteOpen} note={note} onClose={() => setPromoteOpen(false)} />
    </div>
  );
}

function NoteLinks({ title, id, rows, keep, empty }: { title: string; id: string; rows: readonly MemoryLinkRow[]; keep: URLSearchParams; empty: string }) {
  return (
    <Section title={title} id={id}>
      {rows.length === 0 ? (
        <p className="m-0 text-sm text-[var(--ua-muted)]">{empty}</p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-1 p-0">
          {rows.map((row) => (
            <li key={`${row.id}-${row.rel ?? ""}`} className="text-sm">
              <UiLink href={memoryNoteHref(row.id, keep)} className="text-[var(--ua-link)] underline">
                {row.title || row.id}
              </UiLink>
              {row.rel && <span className="text-[var(--ua-muted)]"> · {relText(row.rel)}</span>}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

const REL_TEXT: Record<string, string> = {
  supersedes: "replaces",
  contradicts: "contradicts",
  supports: "supports",
  derived_from: "based on",
  relates_to: "related",
  refines: "refines",
};

function relText(rel: string): string {
  return REL_TEXT[rel] ?? rel.replace(/_/g, " ");
}

function EvidenceList({ items }: { items: readonly unknown[] }) {
  return (
    <ul className="m-0 list-disc pl-5">
      {items.slice(0, 20).map((item, i) => (
        <li key={i} className="break-words">
          {typeof item === "string" ? item : JSON.stringify(item)}
        </li>
      ))}
    </ul>
  );
}
