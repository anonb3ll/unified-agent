import { useCallback, useEffect, useRef } from "react";
import { useDraft } from "../lib/drafts";
import { useEpoch } from "../lib/epoch";
import { useSignedInIdentity } from "../lib/identity";
import { useHubWrite, type HubWriteOptions, type WriteOutcome } from "../lib/mutations";
import { useUnknownAttempt, withAttempt } from "./attempts";
import { discardParkedWrite, getParkedWriteResult, useParkedWrites, type RunBlockedReason } from "../lib/parkedWrites";

type Stored<TForm, TBody> = {
  form: TForm;
  /** The exact request last sent, kept until it is done so a repeat is the identical call. */
  pending: TBody | null;
  /** The approval that request waits on, when it parked. */
  approvalId: string | null;
};

/**
 * A form whose fields and whose last request live in a draft (spec §6),
 * keyed by identity, operation and object.
 *
 * Submitting builds the request once; while it waits for approval or fails,
 * every repeat sends that same request (an approved pass only matches the
 * identical call). Changing a field starts a new request. Done or accepted
 * clears the draft.
 *
 * A stored write is either run from the store or sent from this form, never
 * both: while the parked-write store holds the request for its approval,
 * `blockedByStore` is true and `submit` sends nothing (the page shows
 * `ParkedWriteNotice` instead). When the store's run succeeds, the draft is
 * cleared; when the store discards it, the form can send it again.
 */
export function useDraftedWrite<TForm extends Record<string, unknown>, TBody, TData = unknown>(options: {
  operation: string;
  object: Record<string, string | number | null | undefined>;
  initial: TForm;
  write: HubWriteOptions<TData>;
  /**
   * Set for a write that must not be repeated blindly: the attempt is stored
   * before it is sent, so leaving mid-request shows "outcome unknown" on return.
   */
  attemptLabel?: string;
}) {
  const identity = useSignedInIdentity();
  const epoch = useEpoch();
  const draft = useDraft<Stored<TForm, TBody>>({
    identity,
    operation: options.operation,
    object: options.object,
    initial: { form: options.initial, pending: null, approvalId: null },
  });
  const hub = useHubWrite<TBody, TData>(options.write);
  const attemptKey = options.attemptLabel && !draft.identityPending ? draft.key : null;
  const { attempt: unknownAttempt, dismiss: dismissAttempt } = useUnknownAttempt(attemptKey, hub.isRunning);
  // The draft as it is now, read when a request settles: the person may have kept typing.
  const latest = useRef(draft.value);
  latest.current = draft.value;
  const approvalId = draft.value.approvalId;
  const [stored] = useParkedWrites({ approvalId: approvalId ?? null });
  // The store can run it (now, or once approved), or it may already have been sent: the form must not send it too.
  // An entry the store can never run here (no organization, other identity or hub) is carried by the form instead.
  const carriedByForm: readonly (RunBlockedReason | null)[] = ["organization_unknown", "context_mismatch", "signed_out"];
  const blockedByStore = approvalId !== null && stored !== undefined && !carriedByForm.includes(stored.blockedReason);
  const storeResult = approvalId ? getParkedWriteResult(epoch, approvalId) : null;
  const storeDone = !stored && (storeResult?.kind === "done" || storeResult?.kind === "accepted");

  useEffect(() => {
    // The store ran the identical request: the form's copy is spent.
    if (storeDone) draft.clear();
  }, [storeDone, draft.clear]);

  const setForm = useCallback(
    (change: Partial<TForm>) => draft.setValue((v) => ({ form: { ...v.form, ...change }, pending: null, approvalId: null })),
    [draft.setValue],
  );

  /**
   * Send the kept request again, or build a new one. `sameRequest` says
   * whether the kept request is the one this button sends; it is required,
   * with no permissive default, so a form with several actions never re-sends
   * one action's kept request when the person pressed another (a parked note
   * sent again as a close, or a close sent when only a note was asked for).
   */
  const submit = useCallback(
    async (
      build: (form: TForm) => TBody,
      sameRequest: (pending: TBody) => boolean,
    ): Promise<(WriteOutcome<TData> & { draftUnchanged: boolean }) | null> => {
      if (blockedByStore || unknownAttempt) return null;
      // Reuse the kept request only when it is the same operation (a different button builds a new one).
      const kept = draft.value.pending !== null && sameRequest(draft.value.pending) ? draft.value.pending : null;
      const body = kept ?? build(draft.value.form);
      const submitted = JSON.stringify(draft.value.form);
      draft.setValue((v) => ({ ...v, pending: body }));
      const carried = stored && !blockedByStore ? stored.id : null;
      const outcome =
        attemptKey && options.attemptLabel ? await withAttempt(epoch, attemptKey, options.attemptLabel, () => hub.run(body)) : await hub.run(body);
      // The form sent the request itself: its stored copy is spent, never to be run as well.
      if (carried && (outcome.kind === "done" || outcome.kind === "accepted")) discardParkedWrite(epoch, carried);
      // Edited while the request was in flight: the draft now holds a different request; keep it.
      const unchanged = JSON.stringify(latest.current.form) === submitted;
      if (outcome.kind === "done" || outcome.kind === "accepted") {
        if (unchanged) draft.clear();
      } else if (outcome.kind === "pending_approval" && unchanged) {
        draft.setValue((v) => ({ ...v, pending: body, approvalId: outcome.approvalId }));
      }
      // Callers move on (navigate, close a reply) only when the form still holds what was sent.
      return { ...outcome, draftUnchanged: unchanged };
    },
    [epoch, blockedByStore, unknownAttempt, attemptKey, options.attemptLabel, stored, draft.value, draft.setValue, draft.clear, hub.run],
  );

  return {
    form: draft.value.form,
    setForm,
    submit,
    /** A request is kept from an earlier submit (waiting for approval, or failed). */
    hasPending: draft.value.pending !== null,
    pendingBody: draft.value.pending,
    pendingApprovalId: approvalId,
    /** The parked-write store holds this request: run it from there, not from the form. */
    blockedByStore,
    /** The store holds it but cannot run it here: the form shows the notice and still sends it. */
    storedForForm: stored !== undefined && !blockedByStore,
    restored: draft.restored,
    /** A request sent from this draft earlier whose answer never arrived: check before sending again. */
    unknownAttempt,
    /** The person checked: it was made, so the draft is spent. */
    attemptWasMade: () => {
      dismissAttempt();
      draft.clear();
    },
    /** The person checked: it was not made, so the form may send again. */
    attemptNotMade: dismissAttempt,
    clear: draft.clear,
    state: hub.state,
    isRunning: hub.isRunning,
    reset: hub.reset,
  };
}
