/**
 * Shared pieces of the Settings area: section tabs, one region wrapper that
 * renders exactly one of loading / error with Retry / empty / ready (spec
 * §5), the freshness line, plain-language read errors (spec §12 defect 10)
 * and the inline result of a write.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button, EmptyState, ErrorState, LinkTabs, PageHeader, Skeleton, UiLink } from "../../lib/ui";
import { formatAge, httpStatusOf } from "../../lib/ui/truthState";
import type { HubWriteState } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { useAreaTabs, type AreaTab } from "../lib/navigation";
import { describeReadError } from "../lib/queries";
import { SettingsReadError } from "./api";

export const SETTINGS_TABS: AreaTab[] = [
  { href: "/settings/health", label: "Health" },
  { href: "/settings/platform", label: "Platform" },
  { href: "/settings/cloud", label: "Cloud" },
  { href: "/settings/usage", label: "Usage" },
  { href: "/settings/credentials", label: "Credentials" },
  { href: "/settings/audit", label: "Audit" },
  { href: "/settings/integrations", label: "Integrations" },
  { href: "/settings/appearance", label: "Appearance" },
];

export const AUDIT_TABS: AreaTab[] = [
  { href: "/settings/audit", label: "Evidence ledger", end: true },
  { href: "/settings/audit/provenance", label: "Provenance" },
];

export function SettingsTabs() {
  const items = useAreaTabs(SETTINGS_TABS);
  return <LinkTabs label="Settings sections" items={items} />;
}

export function AuditTabs() {
  const items = useAreaTabs(AUDIT_TABS);
  return <LinkTabs label="Audit sections" items={items} />;
}

/** The page header every Settings page uses, with the section tabs. */
export function SettingsHeader({ title, description, actions, meta }: { title: string; description: ReactNode; actions?: ReactNode; meta?: ReactNode }) {
  return <PageHeader title={title} description={description} actions={actions} meta={meta} tabs={<SettingsTabs />} />;
}

/**
 * What went wrong with a read, in plain words; the hub's own text goes in
 * the collapsed detail. A 200 that named a failure arrives here as a
 * `SettingsReadError` with the hub's message.
 */
export function describeSettingsError(error: unknown, what = "this"): { message: string; detail: string | null } {
  const detail = error instanceof Error ? error.message : error ? String(error) : null;
  if (error instanceof SettingsReadError && error.status !== null && error.status >= 200 && error.status < 300) {
    return { message: `The hub answered, but could not read ${what}. Try again; if it keeps failing, check Health for the part that is off.`, detail };
  }
  const status = httpStatusOf(error);
  if (status === 403 && error instanceof SettingsReadError && /feature|edition/i.test(error.hubError ?? "")) {
    return { message: "This hub's edition does not include this feature.", detail };
  }
  if (status === 400) return { message: "The hub did not accept these filters. Check them and try again.", detail };
  return describeReadError(error);
}

/** Wall-clock milliseconds, re-read every `intervalMs` while mounted. */
export function useNow(intervalMs = 15_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** "just now", "4m ago". */
export function agoText(ms: number): string {
  return ms < 5_000 ? "just now" : `${formatAge(ms)} ago`;
}

/** The time a value was read, in the person's locale, for a title attribute. */
export function exactTime(ms: number): string {
  return new Date(ms).toLocaleString();
}

/** The parts of a TanStack Query result a region reads. */
export type RegionQuery<T> = {
  status: "pending" | "error" | "success";
  data: T | undefined;
  error: unknown;
  dataUpdatedAt: number;
  isFetching: boolean;
  refetch: () => unknown;
};

/** "Updated 4m ago", live, with the exact time in the title, and a polite "Refreshing" while a refetch runs. */
export function Freshness({ updatedAt, fetching, label = "Updated" }: { updatedAt: number; fetching?: boolean; label?: string }) {
  const now = useNow(10_000);
  if (!updatedAt) return null;
  return (
    <span className="text-xs text-[var(--ua-muted)]">
      <span title={exactTime(updatedAt)}>
        {label} {agoText(Math.max(0, now - updatedAt))}
      </span>
      <span role="status" className="ml-2">
        {fetching ? "Refreshing…" : ""}
      </span>
    </span>
  );
}

export type SettingsRegionProps<T> = {
  query: RegionQuery<T>;
  /** "the health report": used in error copy. */
  what: string;
  loadingLabel: string;
  errorTitle: string;
  isEmpty?: (data: T) => boolean;
  empty?: { title: ReactNode; description?: ReactNode; action?: ReactNode };
  headingLevel?: 2 | 3 | 4;
  /** Hide the "Updated N ago" line (the page shows its own). */
  hideFreshness?: boolean;
  children: (data: T) => ReactNode;
};

/**
 * Exactly one of loading, error with Retry, empty or ready (spec §5). When a
 * refetch fails over data already on screen, the data stays, with an alert
 * that says it could not be refreshed, how old it is, and Retry.
 */
export function SettingsRegion<T>({
  query,
  what,
  loadingLabel,
  errorTitle,
  isEmpty,
  empty,
  headingLevel = 2,
  hideFreshness,
  children,
}: SettingsRegionProps<T>) {
  if (query.data === undefined) {
    if (query.status === "pending") return <Skeleton label={loadingLabel} />;
    const { message, detail } = describeSettingsError(query.error, what);
    return <ErrorState title={errorTitle} message={message} detail={detail} headingLevel={headingLevel} onRetry={() => void query.refetch()} />;
  }
  const refetchFailed = query.status === "error";
  return (
    <div className="flex min-w-0 flex-col gap-3">
      {refetchFailed && <StaleAlert error={query.error} what={what} updatedAt={query.dataUpdatedAt} onRetry={() => void query.refetch()} />}
      {!hideFreshness && !refetchFailed && <Freshness updatedAt={query.dataUpdatedAt} fetching={query.isFetching} />}
      {empty && isEmpty?.(query.data) ? (
        <EmptyState headingLevel={headingLevel} title={empty.title} description={empty.description} action={empty.action} />
      ) : (
        children(query.data)
      )}
    </div>
  );
}

/** A failed refresh over data still on screen: says so, how old the data is, and offers Retry. */
export function StaleAlert({ error, what, updatedAt, onRetry }: { error: unknown; what: string; updatedAt: number; onRetry: () => void }) {
  const now = useNow(10_000);
  const { message, detail } = describeSettingsError(error, what);
  return (
    <div role="alert" className="flex flex-col gap-1 rounded-md border border-[var(--ua-warn)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
      <p className="m-0">
        <span className="font-medium">Could not refresh {what}.</span> {message} What you see was read{" "}
        <span title={exactTime(updatedAt)}>{agoText(Math.max(0, now - updatedAt))}</span> and may be out of date.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Button size="sm" variant="secondary" onClick={onRetry}>
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

/** A labelled section of a page. */
export function Section({
  title,
  id,
  description,
  actions,
  children,
  className,
}: {
  title: ReactNode;
  id: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section aria-labelledby={id} className={`flex min-w-0 flex-col gap-3 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4 ${className ?? ""}`}>
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <h2 id={id} className="m-0 break-words text-base font-semibold">
            {title}
          </h2>
          {description && <p className="m-0 mt-1 text-sm text-[var(--ua-muted)]">{description}</p>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

/** A short note in a box: info, a warning or a problem. */
export function Notice({ tone = "info", children, role }: { tone?: "info" | "warn" | "danger"; children: ReactNode; role?: "status" | "alert" | "note" }) {
  const border = tone === "danger" ? "border-[var(--ua-danger)]" : tone === "warn" ? "border-[var(--ua-warn)]" : "border-[var(--ua-border-strong)]";
  return <div role={role} className={`rounded-md border ${border} bg-[var(--ua-surface)] px-3 py-2 text-sm`}>{children}</div>;
}

/**
 * The inline outcome of a write, next to the form that made it. A parked
 * approval is shown only through the shared `ParkedWriteNotice` (spec §6,
 * §12 defects 1 and 9): nothing runs by itself after approval. An error
 * whose outcome is unknown says the change may have been made.
 */
export function WriteFeedback<T>({
  state,
  what,
  outcomeUnknown,
  checkHref,
  className,
}: {
  state: HubWriteState<T>;
  what: string;
  /** Set from the `run` outcome: the request went out and its result was lost. */
  outcomeUnknown?: boolean;
  checkHref?: string;
  className?: string;
}) {
  if (state.status === "pending_approval") {
    return <ParkedWriteNotice approvalId={state.approvalId} label={what} checkHref={checkHref} className={className} />;
  }
  if (state.status === "error") {
    return (
      <div role="alert" className={`rounded-md border border-[var(--ua-danger)] bg-[var(--ua-danger-surface)] px-3 py-2 text-sm ${className ?? ""}`}>
        {outcomeUnknown ? (
          <p className="m-0">
            {what} was sent, but its answer was lost, so it may or may not have been done. Check before trying again.
            {checkHref && (
              <>
                {" "}
                <UiLink href={checkHref} className="text-[var(--ua-link)] underline">
                  Check now
                </UiLink>
              </>
            )}
          </p>
        ) : (
          <p className="m-0">{state.message}</p>
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

/**
 * The foundation's `cancelled` write result (outcome `kind` or hook state
 * `status`): the client stopped a write that no longer belongs to the
 * signed-in person. Show nothing, do not retry, do not clear the draft.
 * `dispatched: true` means the request had left, so the change may have
 * been made: never "not sent". Read structurally so this area compiles
 * on either side of the foundation change that adds the kind.
 */
export function cancelledWrite(value: unknown): { dispatched: boolean } | null {
  if (!value || typeof value !== "object") return null;
  const v = value as { kind?: unknown; status?: unknown; dispatched?: unknown };
  if (v.kind !== "cancelled" && v.status !== "cancelled") return null;
  return { dispatched: v.dispatched === true };
}

/** A ref that always holds the latest value (read after an await). */
export function useLatest<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

/** New unique key for one attempt of a write. */
export function newKey(prefix: string): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`;
}

/** Sentence case from a snake_case or kebab-case name: "event_bus" → "Event bus". */
export function humanize(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : name;
}

/** "$12.30". */
export function usd(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `$${value.toFixed(2)}` : "–";
}

/** "1 record", "3 records". */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
