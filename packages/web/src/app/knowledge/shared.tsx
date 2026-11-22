/**
 * Shared pieces of the Knowledge area: section tabs, the signed-in
 * identity, the inline result of a write (a parked approval is never shown
 * as success: spec §6, §12 defects 1 and 9) and small layout helpers.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Button, ConfirmDialog, EmptyState, ErrorState, LinkTabs, Skeleton, UiLink } from "../../lib/ui";
import { FreshnessStamp } from "../../lib/ui/FreshnessStamp";
import { deriveTruthState, type QueryLike, type TruthResult } from "../../lib/ui/truthState";
import { draftHoldsSnapshot } from "../lib/drafts";
import type { HubWriteState, RunOverrides } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { useEpoch } from "../lib/epoch";
import { getParkedWriteResult, useParkedWrites, useParkedWriteStore, type RunParkedWriteResult } from "../lib/parkedWrites";
import { useAreaTabs, type AreaTab } from "../lib/navigation";
import { describeReadError } from "../lib/queries";
import { HubReadError, VaultNotConfiguredError } from "./api";

export const KNOWLEDGE_TABS: AreaTab[] = [
  { href: "/knowledge/memory", label: "Memory" },
  { href: "/knowledge/vault", label: "Vault" },
];

export function KnowledgeTabs() {
  const items = useAreaTabs(KNOWLEDGE_TABS);
  return <LinkTabs label="Knowledge sections" items={items} />;
}

/** The signed-in identity: `undefined` while unknown, `null` for none (foundation `useSignedInIdentity`). */
export { useSignedInIdentity as useIdentity } from "../lib/identity";

/**
 * The inline outcome of a write, next to the form that made it. Success is
 * announced by the toast; this keeps "waiting for approval" and errors on
 * screen, with what to do next. The raw transport text stays in the
 * collapsed detail (spec §12 defect 10).
 */
export function WriteFeedback<T>({
  state,
  what,
  message,
  className,
  hidePending = false,
  onRun,
}: {
  state: HubWriteState<T>;
  /** "The note" — what has not happened yet while it waits. */
  what?: string;
  /** Replaces the error message with a more specific one for this form. */
  message?: string | null;
  className?: string;
  /** The form shows the parked notice itself, from its draft. */
  hidePending?: boolean;
  /** Called after the parked write is run from this notice. */
  onRun?: (result: RunParkedWriteResult) => void;
}) {
  if (state.status === "pending_approval") {
    // Forms that keep the parked attempt with their draft show the notice themselves (`useParkedDraft`).
    if (hidePending) return null;
    return <ParkedWriteNotice approvalId={state.approvalId} label={what} className={className} onRun={onRun} />;
  }
  if (state.status === "error") {
    return (
      <div role="alert" className={`rounded-md border border-[var(--ua-danger)] bg-[var(--ua-danger-surface)] px-3 py-2 text-sm ${className ?? ""}`}>
        <p className="m-0">{message ?? state.message}</p>
        {state.detail && (
          <details className="mt-1 text-xs">
            <summary className="cursor-pointer">Technical detail</summary>
            <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{state.detail}</pre>
          </details>
        )}
      </div>
    );
  }
  return null;
}

/** A labelled section of a page. */
export function Section({
  title,
  id,
  actions,
  children,
  className,
}: {
  title: ReactNode;
  id: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section aria-labelledby={id} className={`flex min-w-0 flex-col gap-3 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4 ${className ?? ""}`}>
      <div className="flex flex-wrap items-center gap-2">
        <h2 id={id} className="m-0 min-w-0 flex-1 break-words text-base font-semibold">
          {title}
        </h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

/** A plain-text note that a region is waiting on a form. */
export function Notice({ tone = "info", children, role }: { tone?: "info" | "warn" | "danger"; children: ReactNode; role?: "status" | "alert" | "note" }) {
  const border = tone === "danger" ? "border-[var(--ua-danger)]" : tone === "warn" ? "border-[var(--ua-warn)]" : "border-[var(--ua-border-strong)]";
  return (
    <div role={role} className={`rounded-md border ${border} bg-[var(--ua-surface)] px-3 py-2 text-sm`}>
      {children}
    </div>
  );
}

/** New unique idempotency key for one attempt of a write. */
export function newKey(prefix: string): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`;
}

/**
 * `describeReadError` plus the answers only this area gets: a hub without a
 * vault, and a read the hub refused with a reason (400).
 */
export function describeKnowledgeError(error: unknown): { message: string; detail: string | null } {
  const detail = error instanceof Error ? error.message : error ? String(error) : null;
  const disabled = (text: string | null | undefined) => /vault tools are disabled|UA_VAULT_PATH|vault not configured/i.test(text ?? "");
  if (error instanceof VaultNotConfiguredError || (error instanceof HubReadError && disabled(error.hubError))) {
    return {
      message: "This hub has no vault connected. An operator connects the Obsidian vault with the UA_VAULT_PATH setting on the hub.",
      detail,
    };
  }
  if (error instanceof HubReadError && error.status === 400) {
    return { message: "The hub could not read this. The path may be wrong, or it may lie outside the vault.", detail };
  }
  return describeReadError(error);
}

export type KnowledgeRegionProps<T> = {
  query: QueryLike & { data?: T };
  isEmpty?: (data: T) => boolean;
  loadingLabel: string;
  empty: { title: ReactNode; description?: ReactNode; action?: ReactNode };
  errorTitle?: ReactNode;
  headingLevel?: 2 | 3 | 4;
  children: (data: T, truth: TruthResult) => ReactNode;
};

/**
 * Exactly one of loading, error with Retry, empty or ready (spec §5), like
 * the foundation's `QueryRegion`, with this area's error wording.
 */
export function KnowledgeRegion<T>({ query, isEmpty, loadingLabel, empty, errorTitle, headingLevel = 2, children }: KnowledgeRegionProps<T>) {
  const truth = deriveTruthState([query], { isEmpty: () => query.data !== undefined && Boolean(isEmpty?.(query.data as T)) });
  if (truth.state === "loading") return <Skeleton label={loadingLabel} />;
  if (truth.state === "denied" || truth.state === "unavailable" || query.data === undefined) {
    const { message, detail } = describeKnowledgeError(truth.error);
    return (
      <ErrorState
        title={errorTitle}
        message={message}
        detail={detail}
        headingLevel={headingLevel}
        onRetry={query.refetch ? () => void query.refetch?.() : undefined}
      />
    );
  }
  if (truth.state === "empty") {
    return <EmptyState headingLevel={headingLevel} title={empty.title} description={empty.description} action={empty.action} />;
  }
  return (
    <>
      {(truth.state === "stale" || truth.state === "partial") && (
        <FreshnessStamp state={truth.state} updatedAt={truth.updatedAt} className="mb-2 block text-xs" />
      )}
      {truth.failed > 0 && (
        <div role="alert" className="mb-2 flex flex-wrap items-center gap-2 rounded-md border border-[var(--ua-warn)] px-3 py-2 text-sm">
          <span className="min-w-0 flex-1">{describeKnowledgeError(truth.error).message} What you see may be out of date.</span>
          {query.refetch && (
            <Button size="sm" variant="secondary" onClick={() => void query.refetch?.()}>
              Retry
            </Button>
          )}
        </div>
      )}
      {children(query.data as T, truth)}
    </>
  );
}

// --- Focus return -----------------------------------------------------------

let pendingFocus: string | null = null;

/**
 * Ask for the element with this id to take focus once it appears: an inline
 * form that closes by changing only the query string (no new page, so the
 * shell does not focus the `h1`) hands focus back to the control that opened it.
 */
export function requestFocus(id: string): void {
  pendingFocus = id;
}

/** Give focus to `id` when it was requested and the element is mounted. */
export function useFocusRequest(id: string, ready = true): void {
  useEffect(() => {
    if (!ready || pendingFocus !== id) return;
    const timer = setTimeout(() => {
      const el = document.getElementById(id);
      if (el) {
        pendingFocus = null;
        el.focus();
      }
    }, 0);
    return () => clearTimeout(timer);
  });
}

// --- Drafts and attempts ------------------------------------------------------

/** A ref that always holds the latest value (read after an await). */
export function useLatest<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

/**
 * True when the form still holds exactly what was submitted, so a success
 * may clear it. Text typed after the submit is kept (and stays in the draft).
 */
export function unchangedSince<T extends Record<string, unknown>>(current: T, submitted: T, fields: readonly (keyof T)[]): boolean {
  return fields.every((f) => current[f] === submitted[f]);
}

/**
 * One idempotency key per attempt, kept in the draft: the same body keeps
 * its key across clicks, reloads and approval; a changed body gets a new one.
 */
export function attemptKey(prefix: string, stored: { attemptKey?: unknown; attemptFor?: unknown }, body: unknown): { attemptKey: string; attemptFor: string } {
  const attemptFor = JSON.stringify(body);
  if (stored.attemptFor === attemptFor && typeof stored.attemptKey === "string" && stored.attemptKey) return { attemptKey: stored.attemptKey, attemptFor };
  return { attemptKey: newKey(prefix), attemptFor };
}

// --- Parked writes kept with the draft ------------------------------------------

type ParkedFields = { parkedApprovalId: string };
/** Kept beside the parked fields: the stored write's id, so the form can follow it to a new approval. */
type ParkedRef = { parkedWriteId?: string };

/**
 * A write parked behind an approval, remembered with the form's draft: the
 * approval id, so the form shows the shared `ParkedWriteNotice` (its state,
 * and "Run it now" once approved; spec §6: never automatic).
 *
 * The submitted snapshot is not kept in the draft: the run binds the parked
 * write to this draft (`bind`, foundation `ParkedDraftBinding`, task #2525),
 * so when the write runs, from here or from the Inbox with this form away,
 * the stored draft is cleared if it still holds what was submitted, and text
 * typed after the submit is kept. While the form is on screen it does the
 * same with the text it shows.
 */
export function useParkedDraft<T extends Record<string, unknown> & ParkedFields>(
  draft: { key: string; value: T; setValue: (next: T | ((prev: T) => T)) => void; clear: () => void },
  fields: readonly (keyof T)[],
  onApplied?: (data: unknown, cleared: boolean) => void,
  options: { checkHref?: string; checkLabel?: string } = {},
) {
  const store = useParkedWriteStore(); // re-render when a stored write runs anywhere in this tab
  const latest = useLatest(draft.value);
  const epoch = useEpoch();
  const [keptNewer, setKeptNewer] = useState(false);
  const snapshotOf = useCallback((v: T) => Object.fromEntries(fields.map((f) => [f, v[f]])), [fields]);
  /** The submitted fields per approval: from this form's own park, or from the stored write while it exists. */
  const snapshots = useRef(new Map<string, unknown>());

  /** Run options that bind a write parked from `submitted` to this draft (nothing while the draft has no key). */
  const bind = (submitted: T): RunOverrides =>
    draft.key ? { draft: { draftKey: draft.key, submittedSnapshot: snapshotOf(submitted) } } : {};

  const park = (approvalId: string | null, submitted: T, parkedWriteId?: string) => {
    if (!approvalId) return;
    snapshots.current.set(approvalId, snapshotOf(submitted));
    draft.setValue((v) => ({ ...v, parkedApprovalId: approvalId, parkedWriteId: parkedWriteId ?? "" }) as T);
  };

  /** True from a click on this form's own "Run it now" until its answer arrives. */
  const runningHere = useRef(false);
  /** A run that parked again: the stored write now waits on a new approval, and so does the form. */
  const follow = (approvalId: string) => {
    const before = snapshots.current.get(latest.current.parkedApprovalId);
    if (before !== undefined) snapshots.current.set(approvalId, before);
    draft.setValue((v) => ({ ...v, parkedApprovalId: approvalId }));
  };
  const writeId = (draft.value as T & ParkedRef).parkedWriteId ?? "";
  const moved = writeId ? store.find((w) => w.id === writeId && w.approvalId !== draft.value.parkedApprovalId) : undefined;
  useEffect(() => {
    // The run happened from the Inbox or another view and parked again there.
    if (moved && !runningHere.current) follow(moved.approvalId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [moved?.approvalId]);

  /** Approvals already reconciled here, so the store event and the run's own answer do not both act. */
  const handled = useRef(new Set<string>());
  const reconcile = (data: unknown) => {
    const now = latest.current;
    if (!now.parkedApprovalId || handled.current.has(now.parkedApprovalId)) return;
    handled.current.add(now.parkedApprovalId);
    // Unknown snapshot (a run while this form was away kept newer text, or a
    // write stored without a draft binding): keep the text, the safe side.
    const snapshot = snapshots.current.get(now.parkedApprovalId);
    if (snapshot !== undefined && draftHoldsSnapshot(now, snapshot)) {
      draft.clear();
      onApplied?.(data, true);
    } else {
      // The approved change is made: retire its attempt key too, so the same
      // text sent later is a new attempt, not a duplicate the hub drops.
      draft.setValue((v) => ({ ...v, parkedApprovalId: "", ...("attemptKey" in v ? { attemptKey: "", attemptFor: "" } : {}) }));
      setKeptNewer(true);
      onApplied?.(data, false);
    }
  };

  const id = draft.value.parkedApprovalId;
  const result = id ? getParkedWriteResult(epoch, id) : null;
  // A run of the stored write whose answer was lost (or that was cancelled
  // after it left) is kept by the store as outcome unknown: the form must not
  // offer another send until the person has checked (the notice asks).
  const [entry] = useParkedWrites({ approvalId: id || null });
  // The stored write carries the snapshot it was bound to: kept here, so it outlives the run that removes the entry.
  if (entry?.draft) snapshots.current.set(entry.approvalId, entry.draft.submittedSnapshot);
  const unknown = Boolean(id) && (entry?.outcome === "unknown" || result?.kind === "outcome_unknown");
  const ran = result !== null && (result.kind === "done" || result.kind === "accepted");
  const ended = result !== null && result.kind === "discarded";
  useEffect(() => {
    // It ran from the Inbox or another view while this form was away. A run
    // started from this form's own notice reconciles with its answer instead
    // (that answer carries the result, such as the new note's id).
    if ((!ran && !ended) || runningHere.current) return;
    if (ran) reconcile(null);
    else draft.setValue((v) => ({ ...v, parkedApprovalId: "" }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ran, ended, id]);

  const notice =
    id && (!ran || runningHere.current) ? (
      // The notice runs the write itself; a click on its run button marks the
      // run as this form's, so the store event does not reconcile it first.
      <div
        onClickCapture={(event) => {
          const button = (event.target as HTMLElement).closest("button");
          if (button && /^Run it now$/.test(button.textContent?.trim() ?? "")) runningHere.current = true;
        }}
      >
        <ParkedWriteNotice
          approvalId={id}
          checkHref={options.checkHref}
          checkLabel={options.checkLabel}
          onRun={(r) => {
            runningHere.current = false;
            if (r.kind === "done" || r.kind === "accepted") reconcile(r.data);
            else if (r.kind === "pending_approval" && r.approvalId) follow(r.approvalId);
          }}
        />
      </div>
    ) : null;
  const keptNotice = keptNewer ? (
    <Notice role="status">The approved change was made as you submitted it. You changed the text afterwards, so your newer text is still here and not saved yet.</Notice>
  ) : null;
  return { bind, park, notice, keptNotice, waiting: Boolean(id && !ran), unknown };
}

// --- Unknown outcomes -------------------------------------------------------------

/**
 * A write was sent but its answer was lost (or it was cancelled after it
 * left), so nobody knows whether it happened. The page never guesses: it
 * says so, links to where the person can look, and asks them to choose.
 * Sending again sits behind a confirm that says a duplicate is possible.
 */
export function UnknownOutcome({
  what,
  checkHref,
  checkLabel,
  onThere,
  onResend,
  resendLabel = "It is not there, send it again",
}: {
  /** "The text" / "The journal entry". */
  what: string;
  checkHref: string;
  checkLabel: string;
  onThere: () => void;
  onResend: () => void;
  resendLabel?: string;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <section aria-labelledby="knowledge-unknown-heading" className="flex flex-col gap-2 rounded-md border border-[var(--ua-warn)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
      <h3 id="knowledge-unknown-heading" className="m-0 text-sm font-semibold">
        Not known whether it was saved
      </h3>
      <p role="alert" className="m-0">
        {what} was sent, but the answer was lost, so it is not known whether it was saved. Look before you send it again.
      </p>
      <p className="m-0">
        <UiLink href={checkHref} className="text-[var(--ua-link)] underline">
          {checkLabel}
        </UiLink>
      </p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="primary" onClick={onThere}>
          It is there
        </Button>
        <Button type="button" size="sm" variant="secondary" onClick={() => setConfirming(true)}>
          {resendLabel}
        </Button>
      </div>
      <ConfirmDialog
        open={confirming}
        title="Send it again?"
        consequence={`If the first send did arrive, it will be there twice. Send it again only if you looked and it is not there.`}
        confirmLabel="Send it again"
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          onResend();
        }}
      />
    </section>
  );
}
