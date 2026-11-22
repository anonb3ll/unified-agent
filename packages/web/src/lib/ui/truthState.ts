/**
 * The truth-state contract (design protocol §5, wiring gap G1): a region of
 * hub data is in exactly one of these states, and only `ready` and `empty`
 * may show a count or an "all clear". A failed read is never a zero.
 */
export type TruthState =
  | "loading"
  | "ready"
  | "empty"
  | "partial"
  | "stale"
  | "denied"
  | "unavailable";

/** The parts of a TanStack Query result the contract reads. */
export interface QueryLike {
  status: "pending" | "error" | "success";
  data?: unknown;
  error?: unknown;
  dataUpdatedAt?: number;
  refetch?: () => unknown;
}

export interface TruthResult {
  state: TruthState;
  /** How many sources failed on their latest read. */
  failed: number;
  /** Age anchor: the oldest successful read among the sources, or null. */
  updatedAt: number | null;
  /** The first failure, for the unavailable/partial explanation. */
  error: unknown;
}

/** HTTP status from a `status` property or the "<thing> HTTP 503" message the hub fetchers throw. */
export function httpStatusOf(error: unknown): number | null {
  if (!error || typeof error !== "object") return null;
  const status = (error as { status?: unknown }).status;
  if (typeof status === "number") return status;
  const match = String((error as { message?: unknown }).message ?? "").match(/\bHTTP (\d{3})\b/);
  return match ? Number(match[1]) : null;
}

function isDenied(error: unknown): boolean {
  const status = httpStatusOf(error);
  return status === 401 || status === 403;
}

/** HTTP 200 may still carry failed subreads from the hub aggregator. */
function readErrors(data: unknown): string[] {
  if (!data || typeof data !== "object") return [];
  const payload = data as { errors?: unknown; error?: unknown };
  const errors = Array.isArray(payload.errors) ? payload.errors.map(String) : [];
  if (typeof payload.error === "string" && payload.error) errors.push(payload.error);
  return errors;
}

/**
 * Derive one region's state from its sources. Precedence: a source with no
 * data that failed makes the region denied/unavailable (all failed) or
 * partial (some answered); a failed refetch over kept data is stale; any
 * first read still in flight is loading; otherwise ready, empty or stale by
 * the freshness budget.
 */
export function deriveTruthState(
  queries: readonly QueryLike[],
  options: { isEmpty?: () => boolean; staleAfterMs?: number; now?: number } = {},
): TruthResult {
  const now = options.now ?? Date.now();
  const failedQueries = queries.filter((q) => q.status === "error" || readErrors(q.data).length > 0);
  const lost = failedQueries.filter((q) => q.data === undefined);
  const withData = queries.filter((q) => q.data !== undefined);
  const stamps = withData.map((q) => q.dataUpdatedAt ?? 0).filter((t) => t > 0);
  const updatedAt = stamps.length ? Math.min(...stamps) : null;
  const result = (state: TruthState): TruthResult => ({
    state,
    failed: failedQueries.length,
    updatedAt,
    error: failedQueries[0]?.error ?? readErrors(failedQueries[0]?.data)[0] ?? null,
  });

  if (lost.length > 0) {
    if (withData.length === 0) {
      if (queries.some((q) => q.status === "pending")) return result("loading");
      return result(lost.every((q) => isDenied(q.error)) ? "denied" : "unavailable");
    }
    return result("partial");
  }
  if (failedQueries.some((q) => readErrors(q.data).length > 0)) return result("partial");
  if (failedQueries.length > 0) return result("stale");
  if (queries.some((q) => q.status === "pending")) return result("loading");
  if (
    options.staleAfterMs !== undefined &&
    updatedAt !== null &&
    now - updatedAt > options.staleAfterMs
  ) {
    return result("stale");
  }
  return result(options.isEmpty?.() ? "empty" : "ready");
}

/** A count is shown only for a complete read; anything else is an em dash. */
export function countLabel(state: TruthState, count: number): string {
  return state === "ready" || state === "empty" ? String(count) : "—";
}

/** Compact age: 4s, 2m, 3h, 2d. */
export function formatAge(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}
