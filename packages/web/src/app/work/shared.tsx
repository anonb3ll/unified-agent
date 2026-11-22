/**
 * Shared pieces of the Work area: data hooks over the hub reads, the section
 * tabs, the signed-in identity, and the inline result of a write (a parked
 * approval is never shown as success: spec §6, §12 defects 1 and 9).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { TASK_HOLDS_QUERY_KEY } from "../../lib/api/tasks";
import { useStreamInvalidation } from "../../lib/polling";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useSignedInIdentity } from "../lib/identity";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import { fetchProjects } from "../../lib/api/overview";
import { fetchAgents } from "../../lib/api/sessions";
import { fetchTaskGraph } from "../../lib/api/graph";
import { fetchPlans } from "../../lib/api/plans";
import { Button, LinkTabs, UiLink } from "../../lib/ui";
import { clearDraft, readDraft, saveDraft } from "../lib/drafts";
import { describeReadError } from "../lib/queries";
import { type HubWriteState } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { getParkedWriteResult, useParkedWriteStore, type RunParkedWriteResult } from "../lib/parkedWrites";
import { useAreaTabs, type AreaTab } from "../lib/navigation";
import { fetchBoardAll, fetchGoals, WORK_KEYS } from "./api";

export const WORK_TABS: AreaTab[] = [
  { href: "/work", label: "Tasks", end: true },
  { href: "/work/goals", label: "Goals" },
  { href: "/work/plans", label: "Plans" },
  { href: "/work/canvas", label: "Canvas" },
];

/** Hub event channels that can change what the Work pages show. */
export const WORK_CHANNELS = [
  "agent.task.*",
  "agent.plan*",
  "agent.goal*",
  "agent.dependency*",
  "agent.comment*",
  "agent.evidence.*",
  "agent.delegation.*",
  "agent.run*",
  "agent.session*",
] as const;

/**
 * Live updates for the Work area: a matching frame on the shared, coalesced
 * hub stream refetches the Work queries (and the holds read), as do
 * reconnects, so the pages track the hub while the shell says "live".
 */
export function useWorkLiveUpdates() {
  const queryClient = useQueryClient();
  useStreamInvalidation(
    () => {
      void queryClient.invalidateQueries({ queryKey: ["work"] });
      void queryClient.invalidateQueries({ queryKey: TASK_HOLDS_QUERY_KEY });
    },
    { channels: WORK_CHANNELS, onReconnect: true },
  );
}

/** The Work section tabs. Every Work page renders them, so they also own the live-update subscription. */
export function WorkTabs() {
  useWorkLiveUpdates();
  const items = useAreaTabs(WORK_TABS);
  return <LinkTabs label="Work sections" items={items} />;
}

/**
 * The verified identity: a label, `null` when signed out or anonymous, and
 * `undefined` while `/api/whoami` has not answered. Pass it to `useDraft` as
 * is, so text typed before it is known is saved under the real key.
 */
export function useIdentity(): string | null | undefined {
  return useSignedInIdentity();
}


export function useBoard() {
  return useQuery({ queryKey: WORK_KEYS.board, queryFn: ({ signal }) => fetchBoardAll(signal), staleTime: 10_000 });
}

export function useTaskGraphQuery(enabled = true) {
  return useQuery({ queryKey: WORK_KEYS.taskGraph, queryFn: ({ signal }) => fetchTaskGraph(signal), staleTime: 15_000, enabled });
}

export function useGoalsQuery() {
  return useQuery({ queryKey: WORK_KEYS.goals, queryFn: ({ signal }) => fetchGoals(signal), staleTime: 15_000 });
}

export function usePlansQuery() {
  return useQuery({ queryKey: WORK_KEYS.plans, queryFn: ({ signal }) => fetchPlans(signal), staleTime: 15_000 });
}

export function useAgentsQuery() {
  return useQuery({ queryKey: WORK_KEYS.agents, queryFn: ({ signal }) => fetchAgents(signal), staleTime: 30_000 });
}

export function useProjectsQuery() {
  return useQuery({ queryKey: WORK_KEYS.projects, queryFn: ({ signal }) => fetchProjects(signal), staleTime: 60_000 });
}

/** Agent labels for owner pickers; empty while the roster is unknown. */
export function useAgentOptions() {
  const agents = useAgentsQuery();
  return (agents.data?.agents ?? []).map((a) => ({ value: a.label, label: a.label, description: a.host ?? undefined }));
}

/**
 * The inline outcome of a write, next to the control that made it. Success
 * is announced by the toast; this keeps "waiting for approval" and errors on
 * screen, with what to do next.
 */
export function WriteFeedback<T>({
  state,
  what,
  className,
  onRun,
  outcomeUnknown = false,
  checkHref,
  checkLabel,
  onReload,
  onRetry,
  retryBlocked,
}: {
  state: HubWriteState<T>;
  what?: string;
  className?: string;
  /** Called when the person runs the approved write from the notice. */
  onRun?: (result: RunParkedWriteResult) => void;
  /** On a 409: reload the authoritative data (default: refetch every Work read). */
  onReload?: () => Promise<unknown> | unknown;
  /** On a 409: run the same action again, after a reload. */
  onRetry?: () => unknown;
  /** Why the action may no longer be retried after the reload (a step that became claimed or done). */
  retryBlocked?: string | null;
  /** The last outcome was `error` with `outcomeUnknown`: sent, result lost (foundation 6d5cc5e8). */
  outcomeUnknown?: boolean;
  /** Where to check whether an unknown outcome happened. */
  checkHref?: string;
  checkLabel?: string;
}) {
  if (state.status === "pending_approval") {
    // The shared notice (spec §6): waiting, approved with "Run it now", outcome
    // unknown, or ended. Nothing runs without the person's click.
    return <ParkedWriteNotice approvalId={state.approvalId} label={what} onRun={onRun} className={className} />;
  }
  if (state.status === "error" && state.httpStatus === 409) {
    // A typed conflict (HTTP 409, whatever its wording): the object changed since it was read.
    return <ConflictFeedback message={state.message} detail={state.detail} onReload={onReload} onRetry={onRetry} retryBlocked={retryBlocked} className={className} />;
  }
  if (state.status === "error") {
    return (
      <div role="alert" className={`rounded-md border border-[var(--ua-danger)] bg-[var(--ua-danger-surface)] px-3 py-2 text-sm ${className ?? ""}`}>
        <p className="m-0">{state.message}</p>
        {outcomeUnknown && (
          <p className="m-0 mt-1">
            The change may have been made. Check before submitting again
            {checkHref ? (
              <>
                {": "}
                <UiLink href={checkHref} className="text-[var(--ua-link)] underline">
                  {checkLabel ?? "check it"}
                </UiLink>
              </>
            ) : null}
            .
          </p>
        )}
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

/** A labelled section of a detail page. */
export function Section({ title, id, actions, children }: { title: string; id: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id={id} className="m-0 flex-1 text-base font-semibold">
          {title}
        </h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

/** New unique idempotency key for one attempt of a write. */
export function newKey(prefix: string): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`;
}

type RetainedQuery = { isError: boolean; error: unknown; dataUpdatedAt: number; refetch: () => unknown };

/** True when a read that had data now answers 404: the object was deleted (an approved delete ran). */
export function isGone(query: RetainedQuery): boolean {
  return query.isError && (query.error as { status?: number } | null)?.status === 404;
}

/**
 * A detail page keeps its last good copy when a refresh fails (spec §5), but
 * says so: what failed, when the copy is from, and Retry.
 */
export function RetainedReadNotice({ query, what }: { query: RetainedQuery; what: string }) {
  if (!query.isError) return null;
  const { message, detail } = describeReadError(query.error);
  const asOf = query.dataUpdatedAt ? new Date(query.dataUpdatedAt).toLocaleTimeString() : "an earlier read";
  return (
    <div role="alert" className="mb-3 flex flex-col gap-1 rounded-md border border-[var(--ua-warn)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
      <p className="m-0">
        <strong>{what} could not be refreshed.</strong> {message} What you see is as of {asOf}, and may be out of date.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Button size="sm" onClick={() => void query.refetch()}>
          Retry
        </Button>
        {detail && (
          <details className="text-xs">
            <summary className="cursor-pointer">Technical detail</summary>
            <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{detail}</pre>
          </details>
        )}
      </div>
    </div>
  );
}

/**
 * A form whose write parked: when the stored write is later run from the
 * Inbox (or another tab) and succeeds, the form must not keep the submitted
 * draft. Clears it when what is on screen still equals what was submitted.
 * Returns true once that happened, so the form can say so.
 */
export function useReconcileParkedDraft(approvalId: string, submitted: string, current: string, clear: () => void): boolean {
  const epoch = useEpoch();
  useParkedWriteStore(); // re-render when the store or its results change
  const [reconciled, setReconciled] = useState(false);
  const result = approvalId ? getParkedWriteResult(epoch, approvalId) : null;
  const ran = result !== null && (result.kind === "done" || result.kind === "accepted");
  useEffect(() => {
    if (!ran) return;
    if (submitted && submitted === current) {
      clear();
      setReconciled(true);
    }
  }, [ran, submitted, current, clear]);
  return reconciled;
}

/** Shown by a form whose draft cannot be saved because the identity read failed (foundation 6d5cc5e8). */
export function DraftIdentityNotice({ failed, retry }: { failed: boolean; retry: () => void }) {
  if (!failed) return null;
  return (
    <div role="alert" className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--ua-warn)] px-3 py-2 text-sm">
      <span>The hub could not confirm who is signed in, so this draft is not saved if you leave the page.</span>
      <Button size="sm" onClick={retry}>
        Retry
      </Button>
    </div>
  );
}

/** A 409 explained: nothing was saved, what you typed is kept; reload, then retry if still allowed. */
function ConflictFeedback({
  message,
  detail,
  onReload,
  onRetry,
  retryBlocked,
  className,
}: {
  message: string;
  detail?: string;
  onReload?: () => Promise<unknown> | unknown;
  onRetry?: () => unknown;
  retryBlocked?: string | null;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const [reloaded, setReloaded] = useState(false);
  const [reloading, setReloading] = useState(false);
  return (
    <div role="alert" className={`flex flex-col gap-2 rounded-md border border-[var(--ua-warn)] px-3 py-2 text-sm ${className ?? ""}`}>
      <p className="m-0">
        <strong>This changed since you opened it.</strong> Nothing was saved, and what you entered is kept. Reload to see the latest version, then try again.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          loading={reloading}
          onClick={async () => {
            setReloading(true);
            try {
              await (onReload ? onReload() : queryClient.invalidateQueries({ queryKey: ["work"] }));
              setReloaded(true);
            } finally {
              setReloading(false);
            }
          }}
        >
          Reload
        </Button>
        {onRetry && (
          <Button size="sm" variant="primary" disabled={!reloaded || Boolean(retryBlocked)} onClick={() => void onRetry()}>
            Retry
          </Button>
        )}
        {reloaded && retryBlocked && <span>{retryBlocked}</span>}
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer">Technical detail</summary>
        <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{detail ?? message}</pre>
      </details>
    </div>
  );
}

type DraftHandle<T> = { key: string; value: T; setValue: (next: T | ((prev: T) => T)) => void; clear: () => void };

/**
 * Change the draft a write was made from, after the write answers (review round 3). `key` is the
 * draft key captured before the request was sent: if the person has since moved to another object
 * or identity, the mounted draft is a different one, so the stored copy under `key` is changed
 * instead. `change` returns the next value, or null to clear it. `epoch` is the owner epoch the
 * write started in: once it is revoked, the stored copy is left alone.
 */
export function updateDraftAt<T>(epoch: OwnerEpoch, draft: DraftHandle<T>, key: string, change: (prev: T) => T | null): void {
  if (!key) return;
  if (draft.key === key) {
    const next = change(draft.value);
    if (next === null) draft.clear();
    else draft.setValue(next);
    return;
  }
  const stored = readDraft<T>(epoch, key);
  if (stored === null) return;
  const next = change(stored);
  if (next === null) clearDraft(epoch, key);
  else saveDraft(epoch, key, next);
}

/**
 * Record an attempt in the draft before the request leaves, and check it was stored (review round 3:
 * an attempt is recorded before the send, never after the answer). False when storage refused it.
 */
export function recordBeforeSend<T>(epoch: OwnerEpoch, draft: DraftHandle<T>, next: T): boolean {
  if (!draft.key) return false;
  saveDraft(epoch, draft.key, next);
  draft.setValue(next);
  return readDraft<T>(epoch, draft.key) !== null;
}

/** An outcome whose result is not known: sent and lost, or cancelled after it had left. */
export function isUnknownOutcome(outcome: { kind: string; outcomeUnknown?: boolean; dispatched?: boolean }): boolean {
  return (outcome.kind === "error" && Boolean(outcome.outcomeUnknown)) || (outcome.kind === "cancelled" && Boolean(outcome.dispatched));
}

/** The latest value of something a later callback reads (a draft handle after an await). */
export function useLatest<T>(value: T): { readonly current: T } {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}
