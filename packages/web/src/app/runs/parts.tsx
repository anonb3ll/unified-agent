/**
 * Shared pieces of the Runs pages: the page header with section tabs, the
 * data-state region, write feedback and the unknown-outcome check.
 */
import { useId, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchWhoami, WHOAMI_QUERY_KEY } from "../../lib/api/auth";
import { useRootQueryClient } from "../lib/epoch";
import {
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  FreshnessStamp,
  LinkTabs,
  PageHeader,
  RadioGroup,
  Skeleton,
  UiLink,
  deriveTruthState,
  type QueryLike,
  type TruthResult,
} from "../../lib/ui";
import { describeReadError } from "../lib/queries";
import { useAreaTabs, type AreaTab } from "../lib/navigation";
import { contextFromWhoami, type WriteContext } from "../lib/writeContext";
import { HubReadError, isRouteMissing } from "./api";
import { REFUSAL_TEXT, type GuardRefusal, type StartAttempt } from "./attempt";

export const RUNS_TABS: AreaTab[] = [
  { href: "/runs", label: "Overview", end: true },
  { href: "/runs/workflows", label: "Workflows" },
  { href: "/runs/graphs", label: "Graphs" },
  { href: "/runs/routines", label: "Routines" },
  { href: "/runs/cycles", label: "Cycles" },
  { href: "/runs/receipts", label: "Receipts" },
  { href: "/runs/telemetry", label: "Telemetry" },
  { href: "/runs/insights", label: "Insights" },
  { href: "/runs/executor", label: "Executor" },
];

export function RunsHeader({ title, description, actions }: { title: string; description: ReactNode; actions?: ReactNode }) {
  const tabs = useAreaTabs(RUNS_TABS);
  return <PageHeader title={title} description={description} actions={actions} tabs={<LinkTabs label="Runs sections" items={tabs} />} />;
}

/** The hub origin, organization and verified identity of this tab, or null while unknown. */
export function useWriteContext(): WriteContext | null {
  // whoami is not owner data: it lives in the root client (spec §6, owner epoch).
  const whoami = useQuery({ queryKey: WHOAMI_QUERY_KEY, queryFn: ({ signal }) => fetchWhoami(signal) }, useRootQueryClient());
  return whoami.data ? contextFromWhoami(whoami.data) : null;
}

/** Plain words for a failed read, using the hub's own text for an in-band error. */
export function readProblem(error: unknown): { message: string; detail: string | null } {
  if (error instanceof HubReadError && error.status === 200) {
    return { message: "The hub answered, but reported that this information could not be read. Try again in a moment.", detail: error.message };
  }
  return describeReadError(error);
}

export type RunsRegionProps<T> = {
  query: QueryLike & { data?: T; isFetching?: boolean };
  loadingLabel: string;
  errorTitle: string;
  isEmpty?: (data: T) => boolean;
  empty: { title: ReactNode; description?: ReactNode; action?: ReactNode };
  /** Shown instead of an error when the hub has no such route (404). */
  notAvailable?: ReactNode;
  headingLevel?: 2 | 3;
  children: (data: T, truth: TruthResult) => ReactNode;
};

/**
 * Exactly one of loading, error with Retry, empty or ready (spec §5). A
 * refetch that failed over data still on screen shows the error, Retry and
 * how old the data is, above the data, so stale rows are never silently
 * actionable.
 */
export function RunsRegion<T>({ query, loadingLabel, errorTitle, isEmpty, empty, notAvailable, headingLevel = 2, children }: RunsRegionProps<T>) {
  const truth = deriveTruthState([query], { isEmpty: () => query.data !== undefined && Boolean(isEmpty?.(query.data as T)) });
  const retry = query.refetch ? () => void query.refetch?.() : undefined;
  if (truth.state === "loading") return <Skeleton label={loadingLabel} />;
  if (query.data === undefined) {
    if (notAvailable && isRouteMissing(query.error)) {
      return <EmptyState headingLevel={headingLevel} title="Not available on this hub yet" description={notAvailable} />;
    }
    const { message, detail } = readProblem(query.error);
    return <ErrorState title={errorTitle} message={message} detail={detail} headingLevel={headingLevel} onRetry={retry} />;
  }
  const failedRefetch = query.status === "error";
  return (
    <>
      {failedRefetch ? <StaleBanner error={query.error} updatedAt={truth.updatedAt} onRetry={retry} /> : null}
      {truth.state === "empty" ? (
        <EmptyState headingLevel={headingLevel} title={empty.title} description={empty.description} action={empty.action} />
      ) : (
        children(query.data as T, truth)
      )}
    </>
  );
}

/** A refetch failed: say so, say how old the shown data is, offer Retry. */
export function StaleBanner({ error, updatedAt, onRetry }: { error: unknown; updatedAt: number | null; onRetry?: () => void }) {
  const { message, detail } = readProblem(error);
  return (
    <div role="alert" className="mb-3 flex flex-col gap-1 rounded-md border border-l-4 border-[var(--ua-warn)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
      <p className="m-0">
        <strong>The latest refresh failed.</strong> {message} What is shown below may be out of date{" "}
        (<FreshnessStamp state="stale" updatedAt={updatedAt} />
        ). Check it before acting on it.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        {onRetry && (
          <Button size="sm" onClick={onRetry}>
            Retry
          </Button>
        )}
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

/** One explanatory sentence under a page header (spec §5 Copy). */
export function Explainer({ children }: { children: ReactNode }) {
  return <p className="m-0 mb-4 max-w-[70ch] text-sm text-[var(--ua-muted)]">{children}</p>;
}

export function Section({ title, children, actions, description }: { title: string; children: ReactNode; actions?: ReactNode; description?: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="mb-8">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 id={id} className="m-0 text-base font-semibold">
          {title}
        </h2>
        {actions}
      </div>
      {description ? <p className="m-0 mb-3 max-w-[70ch] text-sm text-[var(--ua-muted)]">{description}</p> : null}
      {children}
    </section>
  );
}

/** A write that failed: plain message, raw text collapsed. */
export function WriteFailure({ message, detail }: { message: string; detail?: string }) {
  return (
    <div role="alert" className="rounded-md border border-l-4 border-[var(--ua-danger)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
      <p className="m-0">{message}</p>
      {detail && (
        <details className="mt-1 text-xs">
          <summary className="cursor-pointer">Technical detail</summary>
          <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{detail}</pre>
        </details>
      )}
    </div>
  );
}

export type Candidate = { id: string; label: string; href?: string };

export type CandidateRead =
  | { state: "loading" }
  | { state: "failed"; message: string; detail?: string | null; retry: () => void }
  | { state: "ready"; items: Candidate[]; /** The read was capped or partial: absence proves nothing. */ capped: boolean; retry: () => void };

export type AttemptCheckProps = {
  attempt: StartAttempt;
  /** What a candidate is, plural: "workflow runs", "cycles". */
  what: string;
  /** Possible matches; omitted where the hub answers by key instead. */
  candidates?: CandidateRead;
  /** Another tab of this browser is still waiting for the answer. */
  pendingElsewhere?: boolean;
  /** The person picked the candidate that is this attempt: forget the hold. */
  onFound: (candidate: Candidate) => void;
  /** After the person confirmed: start again (the page validates first). */
  onRestart: () => void;
  restartBusy?: boolean;
  /** Extra guidance for this kind (a safe resend with the same key). */
  children?: ReactNode;
};

/**
 * An attempt whose outcome is unknown. Nothing here decides for the person:
 * matches are candidates the person must pick from, a failed or capped
 * check stays inconclusive, and a new start needs an explicit confirmation
 * that a duplicate may be created.
 */
export function AttemptCheck({ attempt, what, candidates, pendingElsewhere, onFound, onRestart, restartBusy, children }: AttemptCheckProps) {
  const [confirm, setConfirm] = useState(false);
  const [picked, setPicked] = useState("");
  const sent = new Date(attempt.sentAt);
  const items = candidates?.state === "ready" ? candidates.items : [];
  const chosen = items.find((c) => c.id === picked) ?? null;
  return (
    <section
      aria-label="Start not confirmed"
      className="mb-4 flex flex-col gap-2 rounded-md border border-l-4 border-[var(--ua-warn)] bg-[var(--ua-surface)] px-3 py-3 text-sm"
    >
      {pendingElsewhere ? (
        <p className="m-0">
          <strong>Another tab of this browser is starting this now.</strong> {attempt.summary} was sent at {sent.toLocaleTimeString()} and that
          tab is still waiting for the answer. Starting is held until it arrives.
        </p>
      ) : (
        <p className="m-0">
          <strong>We could not confirm whether this started.</strong> {attempt.summary} was sent at {sent.toLocaleTimeString()}
          {attempt.state === "parked" ? ", and the hub parked it for approval" : ", but its answer was lost or inconclusive"}. Starting again might
          run the same work twice, so starting is held until you decide.
        </p>
      )}
      {attempt.note ? <p className="m-0">{attempt.note}</p> : null}
      <p className="m-0 text-xs text-[var(--ua-muted)]">
        This hold covers this browser only, not other browsers or devices. Attempt {attempt.id}.
      </p>
      {children}
      {pendingElsewhere ? null : (
        <>
          {candidates?.state === "loading" ? <Skeleton label={`Looking for ${what} that may be this one`} lines={2} /> : null}
          {candidates?.state === "failed" ? (
            <div className="flex flex-col gap-1">
              <p className="m-0">
                <strong>Could not check.</strong> {candidates.message} Whether it started is still unknown.
              </p>
              <div>
                <Button size="sm" onClick={candidates.retry}>
                  Retry the check
                </Button>
              </div>
            </div>
          ) : null}
          {candidates?.state === "ready" ? (
            <div className="flex flex-col gap-2">
              {candidates.capped ? (
                <p role="note" className="m-0">
                  <strong>Inconclusive.</strong> The hub returned only part of the history (the list is capped), so newer or older matching activity may be
                  missing from what is shown here.
                </p>
              ) : null}
              {items.length > 0 ? (
                <>
                  <RadioGroup
                    legend={`These ${what} started around that time and may be this one. Pick the one that is, after checking it.`}
                    value={picked}
                    onChange={setPicked}
                    options={items.map((c) => ({ value: c.id, label: c.href ? <UiLink href={c.href}>{c.label}</UiLink> : c.label }))}
                  />
                  <div>
                    <Button size="sm" disabled={!chosen} onClick={() => chosen && onFound(chosen)}>
                      This one is it
                    </Button>
                  </div>
                </>
              ) : (
                <p className="m-0">
                  {candidates.capped ? null : <strong>Inconclusive. </strong>}No possible match was found{candidates.capped ? " in the part the hub returned" : ""}. That does not prove it did not start.
                </p>
              )}
              <div>
                <Button size="sm" variant="ghost" onClick={candidates.retry}>
                  Check again
                </Button>
              </div>
            </div>
          ) : null}
          <div>
            <Button size="sm" variant="secondary" onClick={() => setConfirm(true)}>
              I have checked and it did not start. Start again
            </Button>
          </div>
        </>
      )}
      <ConfirmDialog
        open={confirm}
        title="Start it again?"
        objectName={attempt.summary}
        consequence="If the first attempt did start, this creates a duplicate that runs the same work a second time. Only continue if you have checked that it did not start."
        confirmLabel="Start again"
        variant="danger"
        busy={restartBusy}
        onCancel={() => setConfirm(false)}
        onConfirm={() => {
          setConfirm(false);
          onRestart();
        }}
      />
    </section>
  );
}

/** Plain words for a start the guard refused to send. */
export function GuardRefused({ refusal }: { refusal: GuardRefusal | null }) {
  if (!refusal) return null;
  return (
    <p role="alert" className="m-0 rounded-md border border-l-4 border-[var(--ua-danger)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
      {REFUSAL_TEXT[refusal]}
    </p>
  );
}

/** A small "label: value" chip for summary rows. */
export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: "warn" }) {
  return (
    <div className="flex flex-col rounded-md border border-[var(--ua-border)] bg-[var(--ua-surface)] px-3 py-2">
      <span className="text-xs text-[var(--ua-muted)]">{label}</span>
      <span className={`font-mono text-base tabular-nums ${tone === "warn" ? "text-[var(--ua-warn)]" : ""}`}>{value}</span>
    </div>
  );
}

/** Move focus to the first invalid control after a failed submit. */
export function focusFirstInvalid(form: HTMLFormElement | null): void {
  if (!form) return;
  setTimeout(() => {
    const el = form.querySelector<HTMLElement>('[aria-invalid="true"]');
    el?.focus();
  }, 0);
}
