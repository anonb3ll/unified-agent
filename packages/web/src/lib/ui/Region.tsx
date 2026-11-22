import React, { useId } from "react";
import { FreshnessStamp } from "./FreshnessStamp";
import { deriveTruthState, httpStatusOf, type QueryLike } from "./truthState";

export interface RegionProps {
  /** Region heading; also names the region and its messages ("Couldn't load Fleet."). */
  title: string;
  /** Every source the region needs. One failed source makes it partial, not empty. */
  queries: readonly QueryLike[];
  /** True when the successful reads hold no rows. Only consulted when every read succeeded. */
  isEmpty?: () => boolean;
  /** Fact sentence for a verified empty region, ending in a full stop. */
  emptyText?: string;
  /** Older than this since the oldest success reads as stale. */
  staleAfterMs?: number;
  /** Rendered for ready, partial and stale. A function so it never runs without data. */
  children: () => React.ReactNode;
  /** Right-hand header controls. */
  actions?: React.ReactNode;
  /** Number of skeleton rows while loading. */
  skeletonRows?: number;
  /** Injected clock for tests. */
  now?: number;
  className?: string;
}

const HATCH: React.CSSProperties = {
  backgroundImage:
    "repeating-linear-gradient(135deg, transparent 0 6px, color-mix(in oklab, var(--ua-unknown) 9%, transparent) 6px 7px)",
};

function errorText(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return message.slice(0, 160) || "The read failed.";
}

/**
 * The one wrapper for hub data (design protocol §5). It owns loading, empty,
 * partial, stale, denied and unavailable, so a pane cannot turn a failed read
 * into "Nothing waiting" or "No agents registered".
 */
export function Region({
  title,
  queries,
  isEmpty,
  emptyText,
  staleAfterMs,
  children,
  actions,
  skeletonRows = 3,
  now,
  className = "",
}: RegionProps) {
  const headingId = useId();
  const truth = deriveTruthState(queries, { isEmpty, staleAfterMs, now });
  const retry = () => {
    for (const q of queries) if (q.status === "error") void q.refetch?.();
  };

  let body: React.ReactNode;
  switch (truth.state) {
    case "loading":
      body = (
        <div className="space-y-2 p-4">
          <span className="sr-only">Loading {title}…</span>
          {Array.from({ length: skeletonRows }, (_, i) => (
            <div
              key={i}
              aria-hidden="true"
              className="h-4 animate-pulse rounded bg-[var(--ua-hover)] motion-reduce:animate-none"
              style={{ width: `${88 - i * 14}%` }}
            />
          ))}
        </div>
      );
      break;
    case "denied":
      body = (
        <p className="m-0 p-4 text-sm text-[var(--ua-muted)]">
          Your profile can&apos;t read {title}.
        </p>
      );
      break;
    case "unavailable":
      body = (
        <div
          role="status"
          className="m-3 flex items-start justify-between gap-3 rounded-md border border-dashed border-[var(--ua-border-strong)] p-3 text-sm"
          style={HATCH}
        >
          <div className="min-w-0">
            <p className="m-0 font-medium">Couldn&apos;t load {title}.</p>
            <p className="m-0 mt-0.5 break-words text-xs text-[var(--ua-muted)]">
              {errorText(truth.error)}. Not shown as zero.
            </p>
          </div>
          <button
            type="button"
            onClick={retry}
            aria-label={`Retry ${title}`}
            className="shrink-0 cursor-pointer rounded-md border border-[var(--ua-border-strong)] bg-transparent px-2.5 py-1 text-xs font-medium text-[var(--ua-fg)] hover:bg-[var(--ua-hover)] focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] focus-visible:outline-offset-2"
          >
            Retry
          </button>
        </div>
      );
      break;
    case "empty":
      body = <p className="m-0 p-4 text-sm text-[var(--ua-muted)]">{emptyText ?? "Nothing here."}</p>;
      break;
    default:
      body = (
        <>
          {truth.state === "partial" && (
            <p
              role="status"
              className="m-0 border-b border-[var(--ua-border)] bg-[var(--ua-warn-surface)] px-4 py-2 text-xs text-[var(--ua-warn)]"
            >
              {truth.failed} of {queries.length} sources failed ({errorText(truth.error)}). Totals
              may be incomplete.
            </p>
          )}
          {children()}
        </>
      );
  }

  const status = httpStatusOf(truth.error);
  return (
    <section
      aria-labelledby={headingId}
      aria-busy={truth.state === "loading" ? "true" : undefined}
      data-truth-state={truth.state}
      data-http-status={status ?? undefined}
      className={`overflow-hidden rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] ${className}`}
    >
      <header className="flex min-h-11 items-center gap-2.5 border-b border-[var(--ua-border)] px-4">
        <h2 id={headingId} className="m-0 text-sm font-semibold">
          {title}
        </h2>
        <div className="ml-auto flex items-center gap-2">
          <FreshnessStamp state={truth.state} updatedAt={truth.updatedAt} now={now} />
          {actions}
        </div>
      </header>
      {body}
    </section>
  );
}
