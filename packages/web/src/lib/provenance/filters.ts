import {
  EMPTY_PROVENANCE_GRAPH_FILTER,
  type ProvenanceGraphFilter,
} from "../hub";

const FILTER_KEYS = Object.keys(EMPTY_PROVENANCE_GRAPH_FILTER) as Array<
  keyof ProvenanceGraphFilter
>;

/** Convert a datetime-local value to an explicit UTC instant for API comparisons. */
export function localDateTimeToUtc(value: string | undefined): string {
  const trimmed = (value ?? "").trim();
  if (!trimmed) return "";
  const date = new Date(trimmed);
  return Number.isNaN(date.getTime()) ? trimmed : date.toISOString();
}

/** Render an absolute instant in the user's local datetime-local control. */
export function utcDateTimeToLocal(value: string | undefined): string {
  const trimmed = (value ?? "").trim();
  if (!trimmed) return "";
  const date = new Date(trimmed);
  if (Number.isNaN(date.getTime())) return trimmed;
  const pad = (part: number) => String(part).padStart(2, "0");
  const base = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  const millis = date.getMilliseconds();
  return millis ? `${base}.${String(millis).padStart(3, "0")}` : base;
}

/**
 * Read filters from a shareable URL. `since`/`until` are kept exactly as written
 * — absolute instants — so a shared window is reproduced without loss; the pane
 * converts them to local time for display only, never for the request.
 */
export function readProvenanceFilter(search: string): ProvenanceGraphFilter {
  const params = new URLSearchParams(search);
  const filter: ProvenanceGraphFilter = { ...EMPTY_PROVENANCE_GRAPH_FILTER };
  for (const key of FILTER_KEYS) {
    const value = params.get(key);
    if (value !== null) filter[key] = value as never;
  }
  return filter;
}

export function writeProvenanceFilter(url: URL, filter: ProvenanceGraphFilter): void {
  for (const key of FILTER_KEYS) {
    const value = key === "since" || key === "until"
      ? localDateTimeToUtc(filter[key])
      : (filter[key] ?? "").trim();
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
}

/**
 * Release queries address one release either by a single task id or by a list.
 * When both controls are populated the values are merged into `task_ids`
 * (deduped, order preserved) so neither visible value can be silently dropped;
 * a singular-only query keeps its own `task_id` parameter.
 */
export function normalizeProvenanceFilter(filter: ProvenanceGraphFilter): ProvenanceGraphFilter {
  const normalized = { ...filter };
  if (normalized.query === "by_release") {
    const singular = (normalized.task_id ?? "").trim();
    const list = (normalized.task_ids ?? "").trim();
    if (list) {
      // Both release controls are on screen, so a populated singular id must
      // contribute to the query instead of being silently dropped by the API's
      // task_ids preference. A singular-only query keeps its own parameter.
      const merged = [singular, list]
        .flatMap((value) => value.split(","))
        .map((value) => value.trim())
        .filter(Boolean);
      normalized.task_ids = [...new Set(merged)].join(",");
      normalized.task_id = "";
    }
  }
  normalized.since = localDateTimeToUtc(normalized.since);
  normalized.until = localDateTimeToUtc(normalized.until);
  return normalized;
}
