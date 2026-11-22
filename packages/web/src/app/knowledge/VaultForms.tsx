/**
 * Vault writes: a new note or an addition to one, in the folders the hub
 * writes to only, and a journal entry. Every write goes through
 * `useHubWrite`; a parked approval keeps the form and its draft.
 *
 * Replay: `vault_write` builds its approval payload from the body alone and
 * `vault_journal` from the body plus the signed-in identity (`agent`), with
 * absent fields left out, so the identical request rebuilds the identical
 * payload (`docs/control-center-api.md`). Both stay resumable. The journal
 * form always sends its `date`, so a replay the next day still writes to
 * the day the entry was meant for, and a new note is sent with
 * `expected_revision: "absent"`, so a replay can never replace a note that
 * appeared in the meantime.
 */
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { Button, Field, Input, LinkButton, Select, Textarea } from "../../lib/ui";
import { saveDraft, useDraft } from "../lib/drafts";
import { useEpoch } from "../lib/epoch";
import { useHubWrite, type HubWriteState } from "../lib/mutations";
import {
  isExistsRefusal,
  isLockedRefusal,
  journalLength,
  journalProblem,
  JOURNAL_MAX_CHARS,
  KNOWLEDGE_KEYS,
  KNOWLEDGE_WRITES,
  newNoteNameProblem,
  newNotePath,
  parseTags,
  todayIso,
  vaultNoteHref,
  writeFoldersFromRefusal,
} from "./api";
import { Notice, requestFocus, Section, UnknownOutcome, unchangedSince, useIdentity, useLatest, useParkedDraft, WriteFeedback } from "./shared";

type VaultWriteBody = { path: string; content: string; mode: "overwrite" | "append"; expected_revision?: string };
type VaultWriteResult = { path?: string; bytes_written?: number; revision?: string | null };

/** A plain message for the vault's own refusals; null keeps the generic one. */
function vaultRefusalMessage(state: HubWriteState<unknown>, path: string): string | null {
  if (state.status !== "error") return null;
  const text = state.detail ?? state.message;
  if (isLockedRefusal(text)) return "This note is locked (its front matter says locked: true), so the hub will not change it. Only the vault’s owner can unlock it.";
  const folders = writeFoldersFromRefusal(text);
  if (folders) {
    return folders.length
      ? `The hub writes only to these folders: ${folders.join(", ")}. Pick one of them.`
      : "This hub writes to no vault folders, so notes cannot be written here.";
  }
  if (isExistsRefusal(text)) return `A note named ${path} already exists. Pick another name, or open that note and add to it.`;
  return null;
}

export function VaultNewNoteForm({ folders, initialFolder, onFoldersLearned }: { folders: readonly string[]; initialFolder: string; onFoldersLearned: (folders: string[]) => void }) {
  const identity = useIdentity();
  const navigate = useNavigate();
  const startFolder = folders.includes(initialFolder) ? initialFolder : (folders[0] ?? "");
  const draft = useDraft({ identity, operation: "vault.new", object: "", initial: { folder: startFolder, name: "", content: "", parkedApprovalId: "" } });
  const parked = useParkedDraft(draft, NEW_FIELDS, (data, cleared) => {
    const written = (data as VaultWriteResult | null)?.path;
    if (cleared && written) navigate(vaultNoteHref(written));
  });
  const { value, setValue } = draft;
  const latest = useLatest(value);
  const [keptNewer, setKeptNewer] = useState(false);
  const [errors, setErrors] = useState<{ folder?: string; name?: string; content?: string }>({});
  const [sentPath, setSentPath] = useState("");
  const write = useHubWrite<VaultWriteBody, VaultWriteResult>({
    path: KNOWLEDGE_WRITES.vaultWrite,
    invalidates: [KNOWLEDGE_KEYS.vaultAll],
    label: "Writing the vault note",
    successMessage: (d) => `Saved ${d.path ?? "the note"}.`,
  });
  const folder = folders.includes(value.folder) ? value.folder : (folders[0] ?? "");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const next = {
      folder: folder ? undefined : "The hub writes to no folders, so there is nowhere to put the note.",
      name: newNoteNameProblem(value.name) ?? undefined,
      content: value.content.trim() ? undefined : "Write the note's text.",
    };
    setErrors(next);
    if (next.folder || next.name || next.content) return;
    const path = newNotePath(folder, value.name);
    setSentPath(path);
    const submitted = value;
    setKeptNewer(false);
    const outcome = await write.run({ path, content: value.content, mode: "overwrite", expected_revision: "absent" }, parked.bind(submitted));
    if (outcome.kind === "done") {
      if (!unchangedSince(latest.current, submitted, NEW_FIELDS)) {
        setKeptNewer(true);
        return;
      }
      draft.clear();
      navigate(vaultNoteHref(outcome.data.path ?? path));
    } else if (outcome.kind === "pending_approval") {
      parked.park(outcome.approvalId, submitted, outcome.parkedWriteId);
    } else if (outcome.kind === "error") {
      const learned = writeFoldersFromRefusal(outcome.detail ?? outcome.message);
      if (learned) onFoldersLearned(learned);
    }
  };

  return (
    <Section title="New vault note" id="vault-new-heading">
      <form className="flex flex-col gap-4" onSubmit={(e) => void submit(e)} noValidate>
        {draft.restored && <Notice role="status">Your unsaved draft was restored.</Notice>}
        <p className="m-0 text-sm text-[var(--ua-muted)]">
          The hub writes only to these folders: <strong>{folders.length ? folders.join(", ") : "none"}</strong>. Everything else in the vault is read only here.
        </p>
        <Field label="Folder" required hint="Notes go straight into this folder." error={errors.folder}>
          <Select value={folder} options={folders.map((f) => ({ value: f, label: f }))} onChange={(e) => setValue((v) => ({ ...v, folder: e.target.value }))} />
        </Field>
        <Field label="Note name" required hint="For example “Meeting notes 2026-11-10”. It is saved as a .md file; a name like “Ideas/next” makes a subfolder." error={errors.name}>
          <Input value={value.name} onChange={(e) => setValue((v) => ({ ...v, name: e.target.value }))} />
        </Field>
        <Field label="Text" required hint="Markdown, as in Obsidian. [[Wikilinks]] work." error={errors.content}>
          <Textarea rows={12} value={value.content} onChange={(e) => setValue((v) => ({ ...v, content: e.target.value }))} />
        </Field>
        {keptNewer && <Notice role="status">That was saved as you submitted it. You changed the text afterwards, so your newer text is still here and not saved yet.</Notice>}
        {parked.notice}
        {parked.keptNotice}
        <WriteFeedback hidePending state={write.state} what="Writing the note" message={vaultRefusalMessage(write.state, sentPath)} />
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" loading={write.isRunning}>
            Save note
          </Button>
          <LinkButton href="/knowledge/vault" variant="secondary">
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

const NEW_FIELDS = ["folder", "name", "content"] as const;
const APPEND_FIELDS = ["content"] as const;
const JOURNAL_FIELDS = ["entry", "date", "tags"] as const;

/**
 * What a send's outcome means for the attempt marker kept with the draft:
 * `keep` while it may have happened (sent, answer lost; or cancelled after it
 * left), `clear` when it surely did not (refused, parked, cancelled before
 * it left).
 */
function attemptAfter(outcome: { kind: string; outcomeUnknown?: boolean; dispatched?: boolean }): "keep" | "clear" {
  if (outcome.kind === "error") return outcome.outcomeUnknown ? "keep" : "clear";
  if (outcome.kind === "cancelled") return outcome.dispatched ? "keep" : "clear";
  return "clear";
}

/**
 * Add to a note. `vault_write` append does not deduplicate, so the form
 * records what it is about to send with the draft before it sends it. If the
 * answer is lost (here, after navigating away, or for an approved run from
 * the store) the outcome stays unknown: the page never infers it from the
 * note's text. The person looks and chooses "It is there" or "It is not
 * there, send it again" (behind a confirm).
 */
export function VaultAppendForm({
  path,
  current,
  onFoldersLearned,
}: {
  path: string;
  current: string;
  revision?: string;
  onFoldersLearned: (folders: string[]) => void;
}) {
  const identity = useIdentity();
  const navigate = useNavigate();
  const formHref = `${vaultNoteHref(path)}&write=append`;
  const draft = useDraft({
    identity,
    operation: "vault.append",
    object: { path },
    // `sentAttempt`: the exact text of a send whose outcome is not known yet.
    initial: { content: "", sentAttempt: "", parkedApprovalId: "" },
  });
  const parked = useParkedDraft(
    draft,
    APPEND_FIELDS,
    (_data, cleared) => {
      if (cleared) {
        requestFocus("vault-append-link");
        navigate(vaultNoteHref(path));
      }
    },
    { checkHref: vaultNoteHref(path), checkLabel: "Open the note and look" },
  );
  const { value, setValue } = draft;
  const latest = useLatest(value);
  const [keptNewer, setKeptNewer] = useState(false);
  const epoch = useEpoch();
  const [error, setError] = useState<string | undefined>();
  const write = useHubWrite<VaultWriteBody, VaultWriteResult>({
    path: KNOWLEDGE_WRITES.vaultWrite,
    invalidates: [KNOWLEDGE_KEYS.vaultAll],
    label: "Adding to the vault note",
    successMessage: "Added to the note.",
    formHref,
  });

  const unknown = (Boolean(value.sentAttempt) && !write.isRunning) || parked.unknown;

  const send = async (resend = false) => {
    if (!resend && unknown) return;
    if (!latest.current.content.trim()) {
      setError("Write the text to add.");
      return;
    }
    setError(undefined);
    const submitted = latest.current;
    // The hub appends bytes as sent; start the addition on a new paragraph.
    const separator = current === "" ? "" : current.endsWith("\n\n") ? "" : current.endsWith("\n") ? "\n" : "\n\n";
    // Recorded before the send, so a lost answer is known even if the person leaves first:
    // stored synchronously, since the owner epoch may end (and refuse the debounced save) mid-request.
    if (draft.key) saveDraft(epoch, draft.key, { ...submitted, sentAttempt: submitted.content });
    setValue((v) => ({ ...v, sentAttempt: submitted.content }));
    setKeptNewer(false);
    const outcome = await write.run({ path, content: `${separator}${submitted.content}`, mode: "append" }, parked.bind(submitted));
    if (outcome.kind === "done") {
      if (!unchangedSince(latest.current, submitted, APPEND_FIELDS)) {
        setValue((v) => ({ ...v, sentAttempt: "" }));
        setKeptNewer(true);
        return;
      }
      draft.clear();
      requestFocus("vault-append-link");
      navigate(vaultNoteHref(path));
      return;
    }
    if (attemptAfter(outcome) === "clear") setValue((v) => ({ ...v, sentAttempt: "" }));
    if (outcome.kind === "pending_approval") parked.park(outcome.approvalId, submitted, outcome.parkedWriteId);
    else if (outcome.kind === "error" && !outcome.outcomeUnknown) {
      const learned = writeFoldersFromRefusal(outcome.detail ?? outcome.message);
      if (learned) onFoldersLearned(learned);
    }
  };

  return (
    <form
      aria-labelledby="vault-append-heading"
      className="flex flex-col gap-3 rounded-md border border-[var(--ua-border-strong)] p-3"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        void send();
      }}
      noValidate
    >
      <h3 id="vault-append-heading" className="m-0 text-sm font-semibold">
        Add to this note
      </h3>
      {draft.restored && <Notice role="status">Your unsaved text was restored.</Notice>}
      <Field label="Text to add" required hint="Added at the end of the note, as you type it." error={error}>
        <Textarea rows={6} value={value.content} onChange={(e) => setValue((v) => ({ ...v, content: e.target.value }))} />
      </Field>
      {keptNewer && <Notice role="status">That was saved as you submitted it. You changed the text afterwards, so your newer text is still here and not saved yet.</Notice>}
      {parked.notice}
      {parked.keptNotice}
      {Boolean(value.sentAttempt) && !write.isRunning && (
        <UnknownOutcome
          what="The text"
          checkHref={vaultNoteHref(path)}
          checkLabel="Open the note and look for your text"
          onThere={() => {
            // The attempt arrived. Clear only what it sent: text typed since stays.
            if (latest.current.content === latest.current.sentAttempt) draft.clear();
            else {
              setValue((v) => ({ ...v, sentAttempt: "" }));
              setKeptNewer(true);
            }
          }}
          onResend={() => void send(true)}
        />
      )}
      {!(write.state.status === "error" && value.sentAttempt) && (
        <WriteFeedback hidePending state={write.state} what="Adding the text" message={vaultRefusalMessage(write.state, path)} />
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" loading={write.isRunning} disabled={unknown}>
          Add text
        </Button>
        <LinkButton href={vaultNoteHref(path)} variant="secondary" onClick={() => requestFocus("vault-append-link")}>
          Cancel
        </LinkButton>
        <Button type="button" variant="ghost" onClick={() => draft.clear()}>
          Discard draft
        </Button>
      </div>
    </form>
  );
}

type JournalBody = { entry: string; date: string; tags?: string[] };
type JournalResult = { path?: string; entry?: string; created_note?: boolean };

/**
 * A journal entry. `vault_journal` does not deduplicate either: the same
 * attempt marker and the same two choices as an addition to a note.
 */
export function VaultJournalForm() {
  const identity = useIdentity();
  const navigate = useNavigate();
  const draft = useDraft({
    identity,
    operation: "vault.journal",
    object: "",
    // `sentDate`, `sentEntry`, `sentTags`: what a send whose outcome is not known yet carried.
    initial: { entry: "", date: todayIso(), tags: "", sentDate: "", sentEntry: "", sentTags: "", parkedApprovalId: "" },
  });
  const { value, setValue } = draft;
  const journalHref = (day: string) => vaultNoteHref(`Logs/${day}.md`);
  const parked = useParkedDraft(draft, JOURNAL_FIELDS, undefined, {
    checkHref: journalHref(value.date),
    checkLabel: "Open the day's journal and look",
  });
  const latest = useLatest(value);
  const [keptNewer, setKeptNewer] = useState(false);
  const [errors, setErrors] = useState<{ entry?: string; date?: string }>({});
  const write = useHubWrite<JournalBody, JournalResult>({
    path: KNOWLEDGE_WRITES.vaultJournal,
    invalidates: [KNOWLEDGE_KEYS.vaultAll],
    label: "Adding the journal entry",
    successMessage: (d) => `Added to the journal${d.path ? ` (${d.path})` : ""}.`,
    formHref: "/knowledge/vault?write=journal",
  });
  const length = journalLength(value.entry);
  const over = length > JOURNAL_MAX_CHARS;
  const unknown = (Boolean(value.sentDate) && !write.isRunning) || parked.unknown;

  const send = async (resend = false) => {
    if (!resend && unknown) return;
    const now = latest.current;
    const next = {
      entry: journalProblem(now.entry) ?? undefined,
      date: /^\d{4}-\d{2}-\d{2}$/.test(now.date) ? undefined : "Pick a date.",
    };
    setErrors(next);
    if (next.entry || next.date) return;
    const tags = parseTags(now.tags).slice(0, 20);
    const submitted = now;
    // Recorded before the send, so a lost answer is known even if the person leaves first.
    setValue((v) => ({ ...v, sentDate: submitted.date, sentEntry: submitted.entry, sentTags: submitted.tags }));
    setKeptNewer(false);
    const outcome = await write.run({ entry: submitted.entry, date: submitted.date, ...(tags.length ? { tags } : {}) }, parked.bind(submitted));
    if (outcome.kind === "done") {
      if (!unchangedSince(latest.current, submitted, JOURNAL_FIELDS)) {
        setValue((v) => ({ ...v, sentDate: "", sentEntry: "", sentTags: "" }));
        setKeptNewer(true);
        return;
      }
      draft.clear();
      navigate("/knowledge/vault");
      return;
    }
    if (attemptAfter(outcome) === "clear") setValue((v) => ({ ...v, sentDate: "", sentEntry: "", sentTags: "" }));
    if (outcome.kind === "pending_approval") parked.park(outcome.approvalId, submitted, outcome.parkedWriteId);
  };

  const journalLocked = write.state.status === "error" && /journal note .* is locked/.test(write.state.detail ?? "");

  return (
    <Section title="Add a journal entry" id="vault-journal-form-heading">
      <form
        className="flex flex-col gap-4"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void send();
        }}
        noValidate
      >
        {draft.restored && <Notice role="status">Your unsaved entry was restored.</Notice>}
        <p className="m-0 text-sm text-[var(--ua-muted)]">
          One line in the day’s log under Logs, signed with your name{identity ? ` (${identity})` : ""}. Keep it short: put the detail in a memory note or task and
          point to it here.
        </p>
        <Field
          label="Entry"
          required
          hint={`${length} of ${JOURNAL_MAX_CHARS} characters, counting runs of spaces and line breaks as one space.`}
          error={errors.entry ?? (over ? `${length - JOURNAL_MAX_CHARS} characters over the limit.` : undefined)}
        >
          <Textarea rows={3} value={value.entry} onChange={(e) => setValue((v) => ({ ...v, entry: e.target.value }))} />
        </Field>
        <p role="status" className="sr-only">
          {over ? `${length - JOURNAL_MAX_CHARS} characters over the ${JOURNAL_MAX_CHARS} character limit.` : ""}
        </p>
        <Field label="Day" required error={errors.date}>
          <Input type="date" value={value.date} onChange={(e) => setValue((v) => ({ ...v, date: e.target.value }))} />
        </Field>
        <Field label="Tags" hint="Optional. Separate tags with commas.">
          <Input value={value.tags} onChange={(e) => setValue((v) => ({ ...v, tags: e.target.value }))} />
        </Field>
        {keptNewer && <Notice role="status">That was saved as you submitted it. You changed the text afterwards, so your newer text is still here and not saved yet.</Notice>}
        {parked.notice}
        {parked.keptNotice}
        {Boolean(value.sentDate) && !write.isRunning && (
          <UnknownOutcome
            what="The journal entry"
            checkHref={journalHref(value.sentDate)}
            checkLabel={`Open the journal for ${value.sentDate} and look for your entry`}
            onThere={() => {
              // The attempt arrived. Clear only if nothing changed since it was sent.
              const now = latest.current;
              if (now.entry === now.sentEntry && now.date === now.sentDate && now.tags === now.sentTags) draft.clear();
              else {
                setValue((v) => ({ ...v, sentDate: "", sentEntry: "", sentTags: "" }));
                setKeptNewer(true);
              }
            }}
            onResend={() => void send(true)}
          />
        )}
        {!(write.state.status === "error" && value.sentDate) && (
          <WriteFeedback
            hidePending
            state={write.state}
            what="Adding the entry"
            message={journalLocked ? "That day’s journal note is locked (locked: true), so the hub will not add to it. Pick another day, or ask the vault’s owner." : null}
          />
        )}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" loading={write.isRunning} disabled={unknown}>
            Add entry
          </Button>
          <LinkButton href="/knowledge/vault" variant="secondary">
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
