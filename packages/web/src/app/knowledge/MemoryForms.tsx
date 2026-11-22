/**
 * Memory forms: create a note, edit one (replace or append), and promote one
 * to a Wiki page. Every write goes through `useHubWrite`; a parked approval
 * keeps the form and its draft (spec §6, §12 defects 1, 8 and 9).
 *
 * Replay: `memory_save` and `memory_promote` build their approval payload
 * from the body plus the signed-in identity (`author` / `agent`), and the
 * handler leaves absent fields out, so repeating the identical request after
 * approval rebuilds the identical payload (`docs/control-center-api.md`,
 * "Replaying a parked write"). They stay resumable. A promotion preview
 * (`dry_run`) is not: replaying a preview later writes nothing anyone sees.
 */
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { Button, Checkbox, ConfirmDialog, DescriptionList, Dialog, Field, Input, LinkButton, Markdown, RadioGroup, Select, Textarea, UiLink } from "../../lib/ui";
import { authGeneration, isCurrentGeneration, onAuthGenerationChange } from "../lib/authGeneration";
import { useIdentity as useIdentityState } from "../lib/identity";
import { useDraft } from "../lib/drafts";
import { useHubWrite } from "../lib/mutations";
import {
  KNOWLEDGE_KEYS,
  KNOWLEDGE_WRITES,
  MEMORY_LIMITS,
  formatWhen,
  memoryNoteHref,
  memoryTagsProblem,
  noteTags,
  parseTags,
  vaultNoteHref,
  DELETED_TAG,
  fetchMemoryNote,
  differingFields,
  lineDiff,
  noteChangedSince,
  snapshotOf,
  type EditFields,
  type MemoryNote,
  type NoteSnapshot,
} from "./api";
import { attemptKey, Notice, requestFocus, Section, unchangedSince, useIdentity, useLatest, useParkedDraft, WriteFeedback } from "./shared";

type SaveBody = {
  content: string;
  title?: string;
  tags?: string[];
  key?: string;
  append?: boolean;
  overwrite?: boolean;
  idempotency_key?: string;
};
type SaveResult = { id?: string; created?: boolean; similar?: unknown[] };

const INVALIDATES = [KNOWLEDGE_KEYS.memoryAll];

/** True when the hub refused a replace because another author wrote the note. */
function needsOverwrite(message: string | undefined): boolean {
  return /is owned by .+; you are /.test(message ?? "");
}

const CREATE_FIELDS = ["title", "content", "tags", "confidence"] as const;
const EDIT_FIELDS = ["mode", "title", "content", "tags", "addition", "confidence"] as const;

export function MemoryCreateForm({ cancelHref, keep }: { cancelHref: string; keep: URLSearchParams }) {
  const identity = useIdentity();
  const navigate = useNavigate();
  const draft = useDraft({ identity, operation: "memory.create", object: "", initial: { title: "", content: "", tags: "", confidence: "", attemptKey: "", attemptFor: "", parkedApprovalId: "" } });
  const parked = useParkedDraft(draft, CREATE_FIELDS, (data, cleared) => {
    const id = (data as SaveResult | null)?.id;
    if (cleared && typeof id === "string") navigate(memoryNoteHref(id, keep));
  });
  const latest = useLatest(draft.value);
  const [keptNewer, setKeptNewer] = useState(false);
  const [errors, setErrors] = useState<{ content?: string; title?: string; tags?: string }>({});
  const save = useHubWrite<SaveBody, SaveResult>({
    path: KNOWLEDGE_WRITES.memorySave,
    invalidates: INVALIDATES,
    label: "Saving the new note",
    successMessage: "Note saved.",
  });
  const { value, setValue } = draft;

  const submit = async (event: FormEvent, options: { replaceAnyway?: boolean } = {}) => {
    event.preventDefault();
    const tags = parseTags(value.tags);
    const next = {
      content: value.content.trim() ? (value.content.length > MEMORY_LIMITS.content ? `Keep the text under ${MEMORY_LIMITS.content.toLocaleString()} characters.` : undefined) : "Write the note's text.",
      title: value.title.length > MEMORY_LIMITS.title ? `Keep the title under ${MEMORY_LIMITS.title} characters.` : undefined,
      tags: memoryTagsProblem(tags) ?? undefined,
    };
    setErrors(next);
    if (next.content || next.title || next.tags) return;
    const fields = {
      content: value.content,
      ...(value.title.trim() ? { title: value.title.trim() } : {}),
      ...(tags.length ? { tags } : {}),
      ...(value.confidence ? { confidence: Number(value.confidence) } : {}),
    };
    const attempt = attemptKey("memory-create", value, fields);
    const submitted = { ...value, ...attempt };
    setValue(submitted);
    setKeptNewer(false);
    const outcome = await save.run({ ...fields, idempotency_key: attempt.attemptKey }, parked.bind(submitted));
    if (outcome.kind === "done") {
      if (!unchangedSince(latest.current, submitted, CREATE_FIELDS)) {
        setValue((v) => ({ ...v, attemptKey: "", attemptFor: "" }));
        setKeptNewer(true);
        return;
      }
      draft.clear();
      if (typeof outcome.data.id === "string") navigate(memoryNoteHref(outcome.data.id, keep));
      else navigate(cancelHref);
    } else if (outcome.kind === "pending_approval") {
      parked.park(outcome.approvalId, submitted, outcome.parkedWriteId);
    }
  };

  return (
    <Section title="New memory note" id="memory-new-heading">
      <form className="flex flex-col gap-4" onSubmit={(e) => void submit(e)} noValidate>
        {draft.restored && <Notice role="status">Your unsaved draft was restored.</Notice>}
        <p className="m-0 text-sm text-[var(--ua-muted)]">
          The note is saved under your name{identity ? ` (${identity})` : ""}. Agents find it by meaning and by words when they search memory.
        </p>
        <Field label="Title" hint="A short line that says what the note is about." error={errors.title}>
          <Input value={value.title} maxLength={MEMORY_LIMITS.title} onChange={(e) => setValue((v) => ({ ...v, title: e.target.value }))} />
        </Field>
        <Field label="Text" required hint="Markdown works: headings, lists, links and code." error={errors.content}>
          <Textarea rows={10} value={value.content} onChange={(e) => setValue((v) => ({ ...v, content: e.target.value }))} />
        </Field>
        <Field label="Tags" hint="Separate tags with commas. Use project:<name> to file the note under a project." error={errors.tags}>
          <Input value={value.tags} onChange={(e) => setValue((v) => ({ ...v, tags: e.target.value }))} />
        </Field>
        <ConfidenceField value={value.confidence} onChange={(confidence) => setValue((v) => ({ ...v, confidence }))} />
        {parked.notice}
        {parked.keptNotice}
        <WriteFeedback state={save.state} what="Saving the note" hidePending />
        {keptNewer && (
          <Notice role="status">
            The note was saved as you submitted it. You changed the text afterwards, so your newer text is still here; save again to keep it as another note, or
            discard it.
          </Notice>
        )}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" loading={save.isRunning}>
            Save note
          </Button>
          <LinkButton href={cancelHref} variant="secondary">
            Cancel
          </LinkButton>
          <Button type="button" variant="ghost" onClick={() => draft.clear()}>
            Discard draft
          </Button>
        </div>
      </form>
    </Section>
  );
}

/** A version of the note shown in the conflict panels: the fields plus who changed it and when. */
type ConflictVersion = EditFields & { updatedAt: string; by: string };

function parseJson<T>(text: string): T | null {
  if (!text) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

/**
 * Edit a note: replace it, or add to the end.
 *
 * A replace re-reads the note first (the hub has no compare-and-set on
 * `memory/save`). If anything changed since the editor opened, nothing is
 * saved; the person sees the note now beside their edit and chooses "Keep
 * editing from the latest version" or "Replace with my version" (a second
 * confirm). Nothing is merged for them. Their unresolved earlier edit is kept
 * with the draft until they dismiss it.
 *
 * The read and the save belong to the sign-in that started them: a sign-out
 * or another identity during the read cancels it, and nothing is written to
 * the draft or sent afterwards.
 *
 * A replace is not resumable: after an approval the person submits again,
 * and the editor re-reads before saving. An append stays resumable.
 */
export function MemoryEditForm({ note, keep }: { note: MemoryNote; keep: URLSearchParams }) {
  const identityState = useIdentityState();
  const identity = identityState.identity;
  const latestIdentity = useLatest(identity);
  const navigate = useNavigate();
  const deleted = Boolean(note.deleted) || noteTags(note).includes(DELETED_TAG);
  const opened = snapshotOf(note);
  const draft = useDraft({
    identity: identityState,
    operation: "memory.edit",
    object: { note: note.id },
    initial: {
      mode: "replace",
      title: opened.title,
      content: opened.content,
      tags: opened.tags,
      addition: "",
      confidence: "",
      overwrite: false,
      attemptKey: "",
      attemptFor: "",
      // The note as the editor opened it: a replace checks the hub still has exactly this.
      baseContent: opened.content,
      baseTitle: opened.title,
      baseTags: opened.tags,
      baseConfidence: opened.confidence,
      baseUpdatedAt: opened.updatedAt,
      /** The note as the hub had it when a replace found it changed (JSON), until the person chooses. */
      conflict: "",
      /** The person's edit set aside by "Keep editing from the latest version" (JSON), until dismissed. */
      earlier: "",
      parkedApprovalId: "",
    },
    omit: ["overwrite"],
  });
  const { value, setValue } = draft;
  const latest = useLatest(value);
  const [keptNewer, setKeptNewer] = useState(false);
  const [errors, setErrors] = useState<{ content?: string; addition?: string; tags?: string; overwrite?: string; title?: string }>({});
  const [checking, setChecking] = useState(false);
  const [checkFailed, setCheckFailed] = useState<string | null>(null);
  const [confirmReplace, setConfirmReplace] = useState(false);
  const readAbort = useRef<AbortController | null>(null);
  const latestNote = useLatest(note.id);
  const latestDraftKey = useLatest(draft.key);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  // A read belongs to one note and one draft: a change of either (or leaving) cancels it.
  useEffect(() => () => readAbort.current?.abort(), [note.id, draft.key]);
  const viewHref = memoryNoteHref(note.id, keep);
  const parked = useParkedDraft(draft, EDIT_FIELDS, (_data, cleared) => {
    if (cleared) {
      requestFocus("memory-edit-link");
      navigate(viewHref);
    }
  });
  const saveReplace = useHubWrite<SaveBody, SaveResult>({
    path: KNOWLEDGE_WRITES.memorySave,
    invalidates: INVALIDATES,
    label: "Saving the note",
    successMessage: "Note saved.",
    // Never repeated from the store after approval: the editor must re-read first.
    resumable: false,
  });
  const saveAppend = useHubWrite<SaveBody, SaveResult>({
    path: KNOWLEDGE_WRITES.memorySave,
    invalidates: INVALIDATES,
    label: "Adding to the note",
    successMessage: "Text added to the note.",
  });
  const save = value.mode === "append" ? saveAppend : saveReplace;
  const otherAuthor = Boolean(note.author && identity && note.author !== identity);
  const conflict = parseJson<ConflictVersion>(value.conflict);
  const earlier = earlierList(value.earlier);

  if (deleted) {
    return (
      <Section title="This note cannot be edited" id="memory-edit-heading">
        <p className="m-0 text-sm">It was deleted. Open it to restore a copy as a new note.</p>
        <LinkButton href={viewHref}>Open the note</LinkButton>
      </Section>
    );
  }

  // Exactly the fields a replace sends: an unchosen confidence is "" (not sent, so the hub keeps its own).
  const mine = (v: typeof value): EditFields => ({ content: v.content, title: v.title, tags: v.tags, confidence: v.confidence });

  const submit = async (event: { preventDefault: () => void } | null, options: { base?: NoteSnapshot } = {}) => {
    event?.preventDefault();
    const tags = parseTags(value.tags);
    const replace = value.mode !== "append";
    const next = replace
      ? {
          content: value.content.trim() ? (value.content.length > MEMORY_LIMITS.content ? `Keep the text under ${MEMORY_LIMITS.content.toLocaleString()} characters.` : undefined) : "The note needs some text.",
          title: value.title.length > MEMORY_LIMITS.title ? `Keep the title under ${MEMORY_LIMITS.title} characters.` : undefined,
          tags: memoryTagsProblem(tags) ?? undefined,
          overwrite: otherAuthor && !value.overwrite ? `Tick the box to confirm you are replacing ${note.author}'s note.` : undefined,
        }
      : { addition: value.addition.trim() ? undefined : "Write the text to add." };
    setErrors(next);
    if (Object.values(next).some(Boolean)) return;
    const body: SaveBody = replace
      ? {
          key: note.id,
          content: value.content,
          // Always sent: the hub reads an absent title as "keep", so a cleared one must be "".
          title: value.title.trim(),
          tags,
          ...(value.confidence ? { confidence: Number(value.confidence) } : {}),
          ...(otherAuthor && value.overwrite ? { overwrite: true } : {}),
        }
      : { key: note.id, content: value.addition, append: true };

    // Everything below belongs to the sign-in, the note and the draft that pressed the button.
    const startedAs = identity;
    const generation = authGeneration();
    const forNote = note.id;
    const forDraft = draft.key;
    const stillOwned = () =>
      mounted.current &&
      startedAs !== undefined &&
      isCurrentGeneration(generation) &&
      latestIdentity.current === startedAs &&
      latestNote.current === forNote &&
      latestDraftKey.current === forDraft;

    if (replace) {
      const base: NoteSnapshot = options.base ?? {
        content: value.baseContent,
        title: value.baseTitle,
        tags: value.baseTags,
        confidence: value.baseConfidence,
        updatedAt: value.baseUpdatedAt,
      };
      const controller = new AbortController();
      readAbort.current?.abort();
      readAbort.current = controller;
      const stopOnSignOut = onAuthGenerationChange(() => controller.abort());
      setChecking(true);
      setCheckFailed(null);
      let fresh: MemoryNote;
      try {
        fresh = await fetchMemoryNote(note.id, controller.signal);
      } catch (error) {
        if (!controller.signal.aborted && stillOwned()) setCheckFailed(error instanceof Error ? error.message : String(error));
        return;
      } finally {
        stopOnSignOut();
        if (readAbort.current === controller) readAbort.current = null;
        setChecking(false);
      }
      if (controller.signal.aborted || !stillOwned()) return;
      const now = snapshotOf(fresh);
      if (noteChangedSince(base, now)) {
        const version: ConflictVersion = { ...now, by: fresh.updated_by ?? fresh.author ?? "" };
        setValue((v) => ({ ...v, conflict: JSON.stringify(version) }));
        return;
      }
    }
    if (!stillOwned()) return;
    // One key per attempt, kept in the draft: a retry or a run after approval
    // reuses it, so the hub applies an append once even if a response was lost.
    const attempt = attemptKey("memory-edit", value, body);
    // What was submitted, to compare against later. The draft itself gets only
    // the attempt metadata: text typed while the read waited stays.
    const submitted = { ...value, ...attempt, conflict: "" };
    setValue((v) => ({ ...v, ...attempt, conflict: "" }));
    setKeptNewer(false);
    const outcome = await (replace ? saveReplace : saveAppend).run({ ...body, idempotency_key: attempt.attemptKey }, parked.bind(submitted));
    // The answer belongs to the note and draft that sent it; another editor is not touched.
    if (!stillOwned()) return;
    if (outcome.kind === "done") {
      if (!unchangedSince(latest.current, submitted, EDIT_FIELDS)) {
        // The submitted change is confirmed: retire its key, keep the newer text.
        setValue((v) => ({ ...v, attemptKey: "", attemptFor: "" }));
        setKeptNewer(true);
        return;
      }
      draft.clear();
      requestFocus("memory-edit-link");
      navigate(viewHref);
    } else if (outcome.kind === "pending_approval") {
      parked.park(outcome.approvalId, submitted, outcome.parkedWriteId);
    }
  };

  const refusedOverwrite = save.state.status === "error" && needsOverwrite(save.state.detail);

  /** Load the note as the hub has it now into every field; set my edit aside, visible. */
  const keepEditingFromLatest = () => {
    if (!conflict) return;
    const set: EditFields = mine(value);
    setValue((v) => ({
      ...v,
      title: conflict.title,
      content: conflict.content,
      tags: conflict.tags,
      confidence: conflict.confidence,
      baseContent: conflict.content,
      baseTitle: conflict.title,
      baseTags: conflict.tags,
      baseConfidence: conflict.confidence,
      baseUpdatedAt: conflict.updatedAt,
      conflict: "",
      // Newest first; an undismissed earlier edit is never dropped by a later conflict.
      earlier: JSON.stringify([{ ...set, savedAt: new Date().toISOString() }, ...earlierList(v.earlier)].slice(0, EARLIER_MAX)),
    }));
  };

  /** Second confirm given: replace what the hub had at the conflict read, re-reading once more first. */
  const replaceWithMine = () => {
    if (!conflict) return;
    setConfirmReplace(false);
    const base: NoteSnapshot = { content: conflict.content, title: conflict.title, tags: conflict.tags, confidence: conflict.confidence, updatedAt: conflict.updatedAt };
    setValue((v) => ({ ...v, baseContent: base.content, baseTitle: base.title, baseTags: base.tags, baseConfidence: base.confidence, baseUpdatedAt: base.updatedAt }));
    void submit(null, { base });
  };

  return (
    <Section title={`Edit “${note.title || "Untitled note"}”`} id="memory-edit-heading">
      <form className="flex flex-col gap-4" onSubmit={(e: FormEvent) => void submit(e)} noValidate>
        {draft.restored && <Notice role="status">Your unsaved changes were restored.</Notice>}
        <RadioGroup
          legend="How to change the note"
          value={value.mode}
          onChange={(mode) => setValue((v) => ({ ...v, mode }))}
          options={[
            { value: "replace", label: "Replace the note", description: "Change the title, text and tags. The old version is kept in the note's history." },
            { value: "append", label: "Add to the end", description: "Keep the note as it is and write new text to go below it." },
          ]}
        />
        {value.mode === "append" ? (
          <Field label="Text to add" required hint="Added below the current text, with your name and the time." error={errors.addition}>
            <Textarea rows={6} value={value.addition} onChange={(e) => setValue((v) => ({ ...v, addition: e.target.value }))} />
          </Field>
        ) : (
          <>
            {otherAuthor && (
              <div role="note" className="rounded-md border border-[var(--ua-warn)] bg-[var(--ua-warn-surface)] px-3 py-2 text-sm">
                <p className="m-0 font-medium">{note.author} wrote this note, not you.</p>
                <p className="m-0 mt-0.5">
                  Replacing it changes what every agent reads. The hub keeps their text under a marker line at the end of the note, so it can be recovered. To add
                  to it instead, choose “Add to the end”.
                </p>
                <Checkbox
                  className="mt-2"
                  label={`Replace the note ${note.author} wrote`}
                  checked={value.overwrite}
                  aria-invalid={errors.overwrite ? true : undefined}
                  aria-describedby={errors.overwrite ? "memory-overwrite-error" : undefined}
                  onChange={(e) => setValue((v) => ({ ...v, overwrite: e.target.checked }))}
                />
                {errors.overwrite && (
                  <p id="memory-overwrite-error" className="m-0 mt-1 text-xs font-medium text-[var(--ua-danger)]">
                    {errors.overwrite}
                  </p>
                )}
              </div>
            )}
            <Field label="Title" error={errors.title}>
              <Input value={value.title} maxLength={MEMORY_LIMITS.title} onChange={(e) => setValue((v) => ({ ...v, title: e.target.value }))} />
            </Field>
            <Field label="Text" required hint="Markdown works: headings, lists, links and code." error={errors.content}>
              <Textarea rows={12} value={value.content} onChange={(e) => setValue((v) => ({ ...v, content: e.target.value }))} />
            </Field>
            <Field label="Tags" hint="Separate tags with commas." error={errors.tags}>
              <Input value={value.tags} onChange={(e) => setValue((v) => ({ ...v, tags: e.target.value }))} />
            </Field>
            <ConfidenceField
              value={value.confidence}
              current={value.baseConfidence ? Number(value.baseConfidence) : note.confidence}
              onChange={(confidence) => setValue((v) => ({ ...v, confidence }))}
            />
          </>
        )}
        {earlier.map((edit, index) => (
          <EarlierEdit
            key={edit.savedAt ?? index}
            edit={edit}
            index={index}
            count={earlier.length}
            onDismiss={() => setValue((v) => ({ ...v, earlier: JSON.stringify(earlierList(v.earlier).filter((_, i) => i !== index)) }))}
          />
        ))}
        {checkFailed && (
          <div role="alert" className="rounded-md border border-[var(--ua-danger)] px-3 py-2 text-sm">
            <p className="m-0">Nothing was saved: the note could not be re-read to check that nobody changed it. Try again.</p>
            <details className="mt-1 text-xs">
              <summary className="cursor-pointer">Technical detail</summary>
              <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{checkFailed}</pre>
            </details>
          </div>
        )}
        {conflict && value.mode !== "append" && (
          <ConflictPanel now={conflict} mine={mine(value)} onKeepEditing={keepEditingFromLatest} onReplace={() => setConfirmReplace(true)} />
        )}
        {keptNewer && (
          <Notice role="status">
            Your change was saved as you submitted it. You kept typing afterwards, so the newer text is still here and not saved yet.
          </Notice>
        )}
        {parked.notice}
        {parked.keptNotice}
        <WriteFeedback
          hidePending
          state={save.state}
          what={value.mode === "append" ? "Adding the text" : "Saving the note"}
          message={refusedOverwrite ? "Someone else wrote this note. To replace it, tick the box that confirms you mean to replace their note, then save again." : null}
        />
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" loading={save.isRunning || checking} disabled={conflict !== null && value.mode !== "append"}>
            {value.mode === "append" ? "Add text" : "Save changes"}
          </Button>
          <LinkButton href={viewHref} variant="secondary" onClick={() => requestFocus("memory-edit-link")}>
            Cancel
          </LinkButton>
          <Button type="button" variant="ghost" onClick={() => draft.clear()}>
            Discard changes
          </Button>
        </div>
      </form>
      {conflict && (
        <ConfirmDialog
          open={confirmReplace}
          title="Replace the note with your version?"
          objectName={note.title || note.id}
          consequence={`This overwrites the note as it is now${conflict.by ? `, including ${conflict.by}'s change` : ""}${
            conflict.updatedAt ? ` of ${formatWhen(conflict.updatedAt)}` : ""
          }. Its ${listWords(overwritten(conflict, mine(value)))} will be replaced by yours. The replaced version stays in the note's history.`}
          confirmLabel="Replace with my version"
          variant="danger"
          busy={saveReplace.isRunning || checking}
          onCancel={() => setConfirmReplace(false)}
          onConfirm={replaceWithMine}
        />
      )}
    </Section>
  );
}

/** The fields a replace with `mine` would change on `now`; an unchosen confidence is not sent, so it never counts. */
function overwritten(now: EditFields, mine: EditFields): string[] {
  return differingFields(now, mine).filter((field) => field !== "confidence" || mine.confidence !== "");
}

function listWords(words: readonly string[]): string {
  if (words.length === 0) return "change time";
  if (words.length === 1) return words[0];
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/** One version of the note, read only, in a scrollable panel. */
function VersionPanel({ label, version, keptConfidence = false }: { label: string; version: EditFields; keptConfidence?: boolean }) {
  const id = `memory-version-${useId()}`;
  return (
    <section aria-labelledby={id} className="flex min-w-0 flex-col gap-1 rounded-md border border-[var(--ua-border)] bg-[var(--ua-bg)] p-2">
      <h4 id={id} className="m-0 text-xs font-semibold text-[var(--ua-muted)]">
        {label}
      </h4>
      <DescriptionList
        items={[
          { term: "Title", value: version.title },
          { term: "Tags", value: version.tags },
          { term: "Confidence", value: version.confidence || (keptConfidence ? "Not changed: the note keeps its own" : "") },
        ]}
      />
      <pre tabIndex={0} aria-label={`${label}: text`} className="m-0 max-h-72 overflow-auto whitespace-pre-wrap break-words font-mono text-xs">
        {version.content}
      </pre>
    </section>
  );
}

/**
 * The note changed after the editor opened. Nothing was saved. Shows the note
 * now beside the person's edit, what differs, and exactly two choices.
 */
function ConflictPanel({ now, mine, onKeepEditing, onReplace }: { now: ConflictVersion; mine: EditFields; onKeepEditing: () => void; onReplace: () => void }) {
  const differs = overwritten(now, mine);
  const diff = now.content !== mine.content ? lineDiff(now.content, mine.content) : [];
  return (
    <section aria-labelledby="memory-conflict-heading" className="flex flex-col gap-2 rounded-md border border-[var(--ua-warn)] bg-[var(--ua-warn-surface)] px-3 py-2 text-sm">
      <h3 id="memory-conflict-heading" className="m-0 text-sm font-semibold">
        The note changed after you opened it
      </h3>
      <p role="alert" className="m-0">
        {now.by ? `${now.by} changed it` : "Someone changed it"}
        {now.updatedAt ? ` at ${formatWhen(now.updatedAt)}` : ""}. Nothing was saved. Compare the two versions and choose.
      </p>
      {differs.length > 0 && <p className="m-0">{differs.map((f) => `${f[0].toUpperCase()}${f.slice(1)} differs`).join(". ")}.</p>}
      <div className="grid gap-2 md:grid-cols-2">
        <VersionPanel label="The note now" version={now} />
        <VersionPanel label="Your edit" version={mine} keptConfidence />
      </div>
      {diff === null ? (
        <p className="m-0 text-xs text-[var(--ua-muted)]">The text is too long to compare line by line here; compare the two panels.</p>
      ) : diff.length > 0 ? (
        <section aria-labelledby="memory-diff-heading">
          <h4 id="memory-diff-heading" className="m-0 text-xs font-semibold">
            Lines that differ
          </h4>
          <pre
            tabIndex={0}
            aria-label="Lines that differ, the note now against your edit"
            className="m-0 mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded border border-[var(--ua-border)] bg-[var(--ua-bg)] p-2 font-mono text-xs"
          >
            {diff.map((line, i) => (
              <span key={i} className="block">
                <span className="sr-only">{line.kind === "added" ? "Only in your edit: " : "Only in the note now: "}</span>
                <span aria-hidden="true">{line.kind === "added" ? "+ " : "− "}</span>
                {line.text}
              </span>
            ))}
          </pre>
        </section>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="primary" onClick={onKeepEditing}>
          Keep editing from the latest version
        </Button>
        <Button type="button" size="sm" variant="danger" onClick={onReplace}>
          Replace with my version
        </Button>
      </div>
    </section>
  );
}

/** The person's edit set aside after a conflict, kept with the draft until dismissed. */
type EarlierVersion = EditFields & { savedAt?: string };

/** At most this many set-aside edits are kept; the person dismisses them one by one. */
const EARLIER_MAX = 5;

/** The set-aside edits in the draft, newest first (an older draft held one object). */
function earlierList(text: string): EarlierVersion[] {
  const parsed = parseJson<EarlierVersion | EarlierVersion[]>(text);
  if (!parsed) return [];
  return Array.isArray(parsed) ? parsed : [parsed];
}

function EarlierEdit({ edit, index, count, onDismiss }: { edit: EarlierVersion; index: number; count: number; onDismiss: () => void }) {
  const id = `memory-earlier-heading-${index}`;
  const name = count > 1 ? `Your earlier edit ${index + 1} of ${count}` : "Your earlier edit";
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2 rounded-md border border-[var(--ua-border-strong)] px-3 py-2 text-sm">
      <h3 id={id} className="m-0 text-sm font-semibold">
        {name}
        {edit.savedAt ? <span className="font-normal text-[var(--ua-muted)]"> · set aside {formatWhen(edit.savedAt)}</span> : null}
      </h3>
      <p className="m-0 text-[var(--ua-muted)]">The editor holds a later version. Copy what you still want from here, then save. It stays until you dismiss it.</p>
      <VersionPanel label="Read-only copy" version={edit} />
      <div>
        <Button type="button" size="sm" variant="ghost" onClick={onDismiss} aria-label={`Dismiss ${name.toLowerCase()}`}>
          Dismiss
        </Button>
      </div>
    </section>
  );
}

const CONFIDENCE_CHOICES = [
  { value: "0.5", label: "0.50 · a guess or unconfirmed" },
  { value: "0.7", label: "0.70 · likely, seen once" },
  { value: "0.8", label: "0.80 · confirmed" },
  { value: "0.9", label: "0.90 · confirmed more than once" },
  { value: "1", label: "1.00 · certain" },
];

/**
 * The author-stated confidence `memory/save` accepts (a number, 0 to 1 by
 * convention). Never set for the person: "Not set" sends nothing, so a new
 * note has none and an edited note keeps what it had.
 */
function ConfidenceField({ value, onChange, current }: { value: string; onChange: (value: string) => void; current?: number | null }) {
  const options = [...CONFIDENCE_CHOICES];
  if (typeof current === "number" && !options.some((o) => Number(o.value) === current)) {
    options.unshift({ value: String(current), label: `${current.toFixed(2)} · as it is now` });
  }
  return (
    <Field
      label="How sure is this note?"
      hint="Optional. Search ranks surer notes higher, and promotion to the Wiki needs at least 0.70 (the hub's default). Leave it unset if you do not know."
    >
      <Select value={value} placeholder={typeof current === "number" ? "Keep as it is" : "Not set"} options={options} onChange={(e) => onChange(e.target.value)} />
    </Field>
  );
}

const PROMOTE_FIELDS = ["slug", "summary"] as const;

type PromoteBody = { key: string; slug?: string; summary?: string; dry_run?: boolean };
type PromoteResult = { promoted?: boolean; path?: string; markdown?: string; index_line?: string; wiki_link?: string };

export function PromoteDialog({ open, note, onClose }: { open: boolean; note: MemoryNote; onClose: () => void }) {
  const identity = useIdentity();
  const draft = useDraft({
    identity,
    operation: "memory.promote",
    object: { note: note.id },
    initial: { slug: "", summary: "", parkedApprovalId: "" },
  });
  // A parked promotion run later (here or from the Inbox) clears the fields it sent, keeping newer ones.
  const parked = useParkedDraft(draft, PROMOTE_FIELDS);
  const { slug, summary } = draft.value;
  const latestPromote = useLatest(draft.value);
  const setSlug = (v: string) => draft.setValue((d) => ({ ...d, slug: v }));
  const setSummary = (v: string) => draft.setValue((d) => ({ ...d, summary: v }));
  const preview = useHubWrite<PromoteBody, PromoteResult>({
    path: KNOWLEDGE_WRITES.memoryPromote,
    label: "The promotion preview",
    resumable: false,
  });
  const promote = useHubWrite<PromoteBody, PromoteResult>({
    path: KNOWLEDGE_WRITES.memoryPromote,
    invalidates: [KNOWLEDGE_KEYS.memoryAll, KNOWLEDGE_KEYS.vaultAll],
    label: "Promoting the note to the Wiki",
    successMessage: (d) => (d.path ? `Promoted to ${d.path}.` : "Promoted to the Wiki."),
  });
  const body = (): PromoteBody => ({
    key: note.id,
    ...(slug.trim() ? { slug: slug.trim() } : {}),
    ...(summary.trim() ? { summary: summary.trim() } : {}),
  });
  const lowConfidence = typeof note.confidence !== "number" || note.confidence < 0.7;
  const done = promote.state.status === "done" ? promote.state.data : null;
  const previewed = preview.state.status === "done" ? preview.state.data : null;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      size="lg"
      title="Promote this note to the Wiki"
      description="Promotion writes the note as a page under Wiki/Learnings and lists it in the Wiki index, where people and agents read settled knowledge."
      footer={
        done ? (
          <Button variant="primary" onClick={onClose}>
            Close
          </Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="secondary" loading={preview.isRunning} onClick={() => void preview.run({ ...body(), dry_run: true })}>
              Preview
            </Button>
            <Button
              variant="primary"
              loading={promote.isRunning}
              onClick={async () => {
                const sent = body();
                const submitted = draft.value;
                const outcome = await promote.run(sent, parked.bind(submitted));
                if (outcome.kind === "done" && (latestPromote.current.slug.trim() || undefined) === sent.slug && (latestPromote.current.summary.trim() || undefined) === sent.summary) draft.clear();
                else if (outcome.kind === "pending_approval") parked.park(outcome.approvalId, submitted, outcome.parkedWriteId);
              }}
            >
              Promote
            </Button>
          </>
        )
      }
    >
      <div className="flex flex-col gap-3">
        {lowConfidence && (
          <Notice tone="warn" role="note">
            The hub promotes only notes with a confidence of at least 0.70 (unless it is configured otherwise). This note’s confidence is{" "}
            {typeof note.confidence === "number" ? note.confidence.toFixed(2) : "not set"}, so the hub will refuse. To raise it, edit the note and
            choose “How sure is this note?”, then promote it.
          </Notice>
        )}
        <Field label="Page name" hint="Optional. Letters, numbers and dashes; the note's title is used when empty.">
          <Input value={slug} onChange={(e) => setSlug(e.target.value)} maxLength={200} />
        </Field>
        <Field label="One-line summary for the index" hint="Optional. The note's first line is used when empty.">
          <Input value={summary} onChange={(e) => setSummary(e.target.value)} maxLength={4000} />
        </Field>
        <WriteFeedback state={preview.state} what="The preview" />
        {parked.notice}
        {parked.keptNotice}
        <WriteFeedback hidePending state={promote.state} what="The promotion" />
        {done && (
          <p role="status" className="m-0 text-sm">
            Promoted.{" "}
            {done.path && (
              <UiLink href={vaultNoteHref(done.path)} className="text-[var(--ua-link)] underline">
                Open {done.path} in the vault
              </UiLink>
            )}
          </p>
        )}
        {previewed && !done && (
          <div>
            <h3 className="m-0 mb-1 text-sm font-semibold">Preview of {previewed.path ?? "the page"}</h3>
            <div tabIndex={0} role="region" aria-label="Page preview" className="max-h-80 overflow-auto rounded-md border border-[var(--ua-border)] bg-[var(--ua-bg)] p-3">
              <Markdown source={previewed.markdown ?? ""} headingOffset={3} frontmatter="show" />
            </div>
          </div>
        )}
      </div>
    </Dialog>
  );
}
