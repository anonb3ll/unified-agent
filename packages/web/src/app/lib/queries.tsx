import type { ReactNode } from "react";
import { EmptyState, ErrorState, Skeleton } from "../../lib/ui";
import { FreshnessStamp } from "../../lib/ui/FreshnessStamp";
import { deriveTruthState, httpStatusOf, type QueryLike, type TruthResult } from "../../lib/ui/truthState";

/** The dash shown for any count that does not come from a complete read (spec §4, §5). */
export const UNKNOWN_COUNT = "–";

export function useTruth(
  queries: readonly QueryLike[],
  options: { isEmpty?: () => boolean; staleAfterMs?: number } = {},
): TruthResult {
  return deriveTruthState(queries, options);
}

/** A count from a complete read, else "–". Never 0 for unknown. */
export function countText(truth: TruthResult, count: number | null | undefined): string {
  if ((truth.state === "ready" || truth.state === "empty") && typeof count === "number") return String(count);
  return UNKNOWN_COUNT;
}

/** Plain-language explanation of a failed read; the raw text goes to `detail`. */
export function describeReadError(error: unknown): { message: string; detail: string | null } {
  const status = httpStatusOf(error);
  const detail = error instanceof Error ? error.message : error ? String(error) : null;
  if (status === 401) return { message: "Your session has ended. Sign in again to see this.", detail };
  if (status === 403) return { message: "The signed-in identity may not see this.", detail };
  if (status === 404) return { message: "This hub does not offer this information.", detail };
  if (status !== null && status >= 500) return { message: "The hub had a problem answering. Try again in a moment.", detail };
  return { message: "The hub could not be reached. Check your connection and try again.", detail };
}

export type QueryRegionProps<T> = {
  /** The query (or queries) this region shows; the first one's data is passed to `children`. */
  query: QueryLike & { data?: T };
  also?: readonly QueryLike[];
  isEmpty?: (data: T) => boolean;
  /** Announced while loading: "Loading tasks". */
  loadingLabel: string;
  empty: { title: ReactNode; description?: ReactNode; action?: ReactNode };
  errorTitle?: ReactNode;
  staleAfterMs?: number;
  headingLevel?: 2 | 3 | 4;
  children: (data: T, truth: TruthResult) => ReactNode;
};

/**
 * Renders exactly one of loading, error, empty or ready for a query-backed
 * region (spec §5 data states). A stale or partial read still renders its
 * data, with a freshness stamp.
 */
export function QueryRegion<T>({
  query,
  also = [],
  isEmpty,
  loadingLabel,
  empty,
  errorTitle,
  staleAfterMs,
  headingLevel = 2,
  children,
}: QueryRegionProps<T>) {
  const truth = deriveTruthState([query, ...also], {
    isEmpty: () => query.data !== undefined && Boolean(isEmpty?.(query.data as T)),
    staleAfterMs,
  });
  if (truth.state === "loading") return <Skeleton label={loadingLabel} />;
  if (truth.state === "denied" || truth.state === "unavailable" || query.data === undefined) {
    const { message, detail } = describeReadError(truth.error);
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
  // A failed refresh keeps the last data: say so, with Retry and its age,
  // rather than silently showing it as current.
  const refreshFailed = (truth.state === "stale" || truth.state === "partial") && truth.failed > 0;
  // Retry every source whose latest read failed (the primary or any `also`),
  // each through its own refetch; healthy sources are left alone.
  // A partial read (HTTP 200 whose body lists failed sub-reads) has no
  // source in error: then every source is read again.
  const errored = [query, ...also].filter((q) => q.status === "error" && q.refetch);
  const failedSources = errored.length > 0 ? errored : [query, ...also].filter((q) => q.refetch);
  const retryFailed = failedSources.length > 0 ? () => failedSources.forEach((q) => void q.refetch?.()) : undefined;
  const failure = refreshFailed ? describeReadError(truth.error) : null;
  return (
    <>
      {failure && (
        <ErrorState
          className="mb-3"
          title="Showing the last data that loaded"
          message={failure.message}
          detail={failure.detail}
          headingLevel={headingLevel}
          onRetry={retryFailed}
        />
      )}
      {(truth.state === "stale" || truth.state === "partial") && (
        <FreshnessStamp state={truth.state} updatedAt={truth.updatedAt} className="mb-2 block text-xs" />
      )}
      {children(query.data as T, truth)}
    </>
  );
}
