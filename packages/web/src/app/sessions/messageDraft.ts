import { useEffect, useRef } from "react";
import { useDraft } from "../lib/drafts";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import { getParkedWriteResult, useParkedWriteStore } from "../lib/parkedWrites";
import { newKey } from "./model";

/** The attempt a message was last sent as: its dedupe key and exactly what was sent. */
export type SentAttempt = {
  /** `idempotency_key` (device follow-up) or `message_id` (crew message). */
  key: string;
  mode: string;
  body: string;
  /** Item id or session id the message went to. */
  target: string;
  /** Set when the hub parked it behind an approval. */
  approvalId?: string | null;
};

export type MessageDraft = { text: string; sent: SentAttempt | null };

// Under the drafts prefix ("draft:") in the owner epoch's storage, so sign-out (`clearAllDrafts`) removes it too.
const ATTEMPT_PREFIX = "draft:attempt|";

function readAttempt(epoch: OwnerEpoch, key: string | null): SentAttempt | null {
  return key ? epoch.storage.read<SentAttempt | null>(key, null) : null;
}

function writeAttempt(epoch: OwnerEpoch, key: string | null, attempt: SentAttempt | null): void {
  if (!key) return;
  if (attempt) epoch.storage.write(key, attempt);
  else epoch.storage.remove(key);
}

/**
 * A message box whose draft survives navigation and remembers the attempt it
 * was sent as (review findings 4, 5). The attempt is stored with the draft
 * BEFORE the request goes out, so after a lost answer, a navigation or a
 * reload the same text is sent again under the same key and the hub dedupes
 * it. When the person runs a parked message from the store ("Run it now")
 * and it succeeds, the draft is cleared here.
 */
export function useMessageDraft(options: { identity: string | null | undefined; operation: string; sessionId: string; prefix: string }) {
  const draft = useDraft<MessageDraft>({
    identity: options.identity,
    operation: options.operation,
    object: { id: options.sessionId },
    initial: { text: "", sent: null },
  });
  // The latest value, so a send that resolves later compares against what is in the box now.
  const latest = useRef(draft.value);
  latest.current = draft.value;
  const parked = useParkedWriteStore();
  const epoch = useEpoch();
  // The attempt's own synchronous record, keyed like the draft; under the
  // draft prefix, so sign-out (`clearAllDrafts`) removes it too.
  const attemptKey = draft.key ? `${ATTEMPT_PREFIX}${draft.key}` : null;
  const approvalId = draft.value.sent?.approvalId ?? null;
  const replayed = approvalId ? getParkedWriteResult(epoch, approvalId) : null;

  useEffect(() => {
    if (!replayed || (replayed.kind !== "done" && replayed.kind !== "accepted")) return;
    const sent = latest.current.sent;
    writeAttempt(epoch, attemptKey, null);
    if (sent && latest.current.text.trim() === sent.body) draft.clear();
    else draft.setValue((v) => ({ ...v, sent: null }));
    // `parked` re-renders this hook when the runner records a result.
  }, [replayed?.kind, replayed?.at, parked]);

  /** The attempt to send `body` as: the stored one when it is the same message, else a new key. */
  const prepare = (body: string, mode: string, target: string): SentAttempt => {
    const prev = readAttempt(epoch, attemptKey) ?? draft.value.sent;
    const attempt =
      prev && prev.body === body && prev.mode === mode && prev.target === target ? { ...prev, approvalId: null } : { key: newKey(options.prefix), mode, body, target };
    // Stored synchronously before the request goes out (review 2, finding 5):
    // the debounced draft alone could lose the key to a reload.
    writeAttempt(epoch, attemptKey, attempt);
    draft.setValue((v) => ({ ...v, sent: attempt }));
    return attempt;
  };

  /** After the hub answered: clear what was sent on success, remember the approval when parked. */
  /**
   * `cancelled` (foundation contract), errors and anything else: keep the
   * text and the attempt, so a later send reuses the same key. A cancel after
   * the request left may have delivered it; the hub dedupes on that key.
   */
  const settle = (attempt: SentAttempt, outcome: { kind: string; approvalId?: string | null }) => {
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      writeAttempt(epoch, attemptKey, null);
      // Clear only the revision that was sent (review 13): text typed while
      // it was sending stays, as a new message.
      if (latest.current.text.trim() === attempt.body) draft.clear();
      else draft.setValue((v) => ({ ...v, sent: null }));
    } else if (outcome.kind === "pending_approval") {
      writeAttempt(epoch, attemptKey, { ...attempt, approvalId: outcome.approvalId ?? null });
      draft.setValue((v) => ({ ...v, sent: { ...attempt, approvalId: outcome.approvalId ?? null } }));
    }
  };

  return { draft, text: draft.value.text, setText: (text: string) => draft.setValue((v) => ({ ...v, text })), prepare, settle };
}
