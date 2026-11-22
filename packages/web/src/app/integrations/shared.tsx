/**
 * Pieces the integrations pages share: the page frame with section tabs, a
 * read region that is always exactly one of loading / error with Retry /
 * empty / ready (and says when a refresh failed over data still shown), a
 * status label that never relies on colour alone.
 */
import type { ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { EmptyState, ErrorState, FreshnessStamp, LinkTabs, PageHeader, Skeleton, cx } from "../../lib/ui";
import { useAreaTabs, type AreaTab } from "../lib/navigation";
import { describeIntegrationReadError } from "./api";

export const TABS: AreaTab[] = [
  { href: "/settings/integrations", label: "Connectors", end: true },
  { href: "/settings/integrations/slack", label: "Slack" },
  { href: "/settings/integrations/zulip", label: "Zulip" },
  { href: "/settings/integrations/notifications", label: "Notifications" },
];

export function IntegrationsPage({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const tabs = useAreaTabs(TABS);
  return (
    <>
      <PageHeader
        title={title}
        description={description}
        actions={actions}
        tabs={<LinkTabs label="Integrations sections" items={tabs} />}
      />
      <div className="flex flex-col gap-6">{children}</div>
    </>
  );
}

export type Tone = "ok" | "warn" | "off";

const TONE_CLASS: Record<Tone, string> = {
  ok: "border-[var(--ua-ok)] text-[var(--ua-ok)]",
  warn: "border-[var(--ua-warn)] text-[var(--ua-warn)]",
  off: "border-[var(--ua-border-strong)] text-[var(--ua-muted)]",
};

const TONE_GLYPH: Record<Tone, string> = { ok: "●", warn: "▲", off: "○" };

/**
 * A status in words, with a shape as well as a colour (WCAG 1.4.1). With
 * `stale` (the latest read failed) it never passes an old result off as
 * current: it reads "Last known: …" in the warning tone.
 */
export function StatusLabel({
  tone: givenTone,
  label: givenLabel,
  stale = false,
  className,
  testId,
}: {
  tone: Tone;
  label: string;
  stale?: boolean;
  className?: string;
  testId?: string;
}) {
  const tone: Tone = stale ? "warn" : givenTone;
  const label = stale ? `Last known: ${givenLabel}` : givenLabel;
  return (
    <span
      data-testid={testId}
      data-tone={tone}
      data-stale={stale ? "true" : undefined}
      className={cx(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded border bg-[var(--ua-surface)] px-2 py-0.5 text-sm font-medium",
        TONE_CLASS[tone],
        className,
      )}
    >
      <span aria-hidden="true">{TONE_GLYPH[tone]}</span>
      {label}
    </span>
  );
}

export function Section({
  title,
  description,
  actions,
  children,
  id,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  id: string;
}) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1">
          <h2 id={id} className="m-0 text-base font-semibold text-[var(--ua-fg)]">
            {title}
          </h2>
          {description && <p className="m-0 mt-1 max-w-[70ch] text-sm text-[var(--ua-muted)]">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

export type ReadRegionProps<T> = {
  query: UseQueryResult<T>;
  /** What this region shows, for messages: "the Slack status". */
  what: string;
  loadingLabel: string;
  isEmpty?: (data: T) => boolean;
  empty?: { title: ReactNode; description?: ReactNode; action?: ReactNode };
  headingLevel?: 2 | 3;
  children: (data: T) => ReactNode;
};

/**
 * Exactly one of loading, error with Retry, empty or ready (spec §5). When a
 * refresh fails over data already shown, the data stays, with the error,
 * Retry and how old the data is.
 */
export function ReadRegion<T>({ query, what, loadingLabel, isEmpty, empty, headingLevel = 2, children }: ReadRegionProps<T>) {
  const retry = () => void query.refetch();
  if (query.data === undefined) {
    if (query.isError) {
      const { message, detail } = describeIntegrationReadError(query.error, what);
      return <ErrorState title={`Could not load ${what}`} message={message} detail={detail} onRetry={retry} headingLevel={headingLevel} />;
    }
    return <Skeleton label={loadingLabel} />;
  }
  const data = query.data;
  const refreshFailed = query.isError;
  return (
    <>
      {refreshFailed && <RefreshFailed query={query} what={what} headingLevel={headingLevel} />}
      {empty && isEmpty?.(data) ? (
        <EmptyState headingLevel={headingLevel} title={empty.title} description={empty.description} action={empty.action} />
      ) : (
        children(data)
      )}
    </>
  );
}

function RefreshFailed<T>({ query, what, headingLevel }: { query: UseQueryResult<T>; what: string; headingLevel: 2 | 3 }) {
  const { message, detail } = describeIntegrationReadError(query.error, what);
  return (
    <ErrorState
      title={`Could not refresh ${what}`}
      message={
        <>
          {message} What is shown below was read earlier{" "}
          (<FreshnessStamp state="stale" updatedAt={query.dataUpdatedAt || null} />) and may be out of date.
        </>
      }
      detail={detail}
      onRetry={() => void query.refetch()}
      headingLevel={headingLevel}
    />
  );
}

/** True when data is shown but the latest read of it failed: nothing on screen is confirmed current. */
export function isStale(query: UseQueryResult<unknown>): boolean {
  return query.isError && query.data !== undefined;
}

/** The explanation beside a status, replaced while the status is not confirmed. */
export function explainOrStale(explain: string, stale: boolean): string {
  return stale ? "The latest check failed, so this is what was true earlier and may have changed. Retry above." : explain;
}

/** A count from a complete read, else "–" (spec §5). */
export function countOf(query: UseQueryResult<unknown>, n: number | null | undefined): string {
  return query.isSuccess && typeof n === "number" ? String(n) : "–";
}

/** A small key-value grid for status facts. */
export function Facts({ items }: { items: { label: ReactNode; value: ReactNode; testId?: string }[] }) {
  return (
    <dl className="m-0 grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
      {items.map((item, i) => (
        <div key={i} className="contents" data-testid={item.testId}>
          <dt className="text-[var(--ua-muted)]">{item.label}</dt>
          <dd className="m-0 mb-1 break-words text-[var(--ua-fg)] sm:mb-0">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A note box for guidance ("what to do"). */
export function Advice({ title, children, tone = "info" }: { title: string; children: ReactNode; tone?: "info" | "warn" }) {
  return (
    <div
      className={cx(
        "rounded-md border border-l-4 bg-[var(--ua-surface)] px-3 py-2 text-sm text-[var(--ua-fg)]",
        tone === "warn" ? "border-[var(--ua-warn)]" : "border-[var(--ua-border-strong)]",
      )}
    >
      <p className="m-0 font-medium">{title}</p>
      <div className="mt-1 flex flex-col gap-1">{children}</div>
    </div>
  );
}
