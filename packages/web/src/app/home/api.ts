/**
 * Home fetchers the legacy data layer does not have: the uniform run rows
 * (`GET /api/runs`), the daily digest (`GET /api/digest`) and a one-row
 * memory read for the first-run checklist. Sessions, agents, projects and
 * queue health come read-only from `src/api/*`.
 */
import { getJson } from "../inbox/api";

export type RunRow = {
  run_id: string;
  kind: "dispatch" | "selfpickup" | "session" | string;
  task_id: number | null;
  agent: string | null;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  abandoned?: boolean;
};

export type RunsPayload = { runs: RunRow[]; truncated?: boolean; next_cursor?: string | null };

/** Pages of `GET /api/runs` (100 rows each, newest first) read before giving up. */
export const RUNS_MAX_PAGES = 5;

export const RUNS_QUERY_KEY = ["cc2", "runs", "recent"] as const;

/**
 * Recent runs, following `next_cursor` so an older run that is still active
 * is not lost behind newer finished ones. After `RUNS_MAX_PAGES` pages the
 * result says `truncated`, and the caller must treat it as incomplete.
 */
export async function fetchRecentRuns(signal?: AbortSignal): Promise<RunsPayload> {
  const runs: RunRow[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < RUNS_MAX_PAGES; page++) {
    const qs = new URLSearchParams({ limit: "100" });
    if (cursor) qs.set("cursor", cursor);
    const body: RunsPayload = await getJson<RunsPayload>(`/api/runs?${qs}`, "runs", signal);
    runs.push(...(Array.isArray(body.runs) ? body.runs : []));
    cursor = body.truncated && body.next_cursor ? body.next_cursor : null;
    if (!cursor) return { runs, truncated: false };
  }
  return { runs, truncated: true };
}

export type DigestTask = {
  id: number;
  title: string | null;
  completed_at?: string | null;
  claimed_by?: string | null;
  project?: string | null;
};

export type DigestPayload = {
  status?: string;
  changed_today?: { tasks_completed?: DigestTask[]; total_completed?: number };
  errors?: string[];
};

export const DIGEST_QUERY_KEY = ["cc2", "digest"] as const;

export async function fetchDigest(signal?: AbortSignal): Promise<DigestPayload> {
  const body = await getJson<DigestPayload>("/api/digest", "digest", signal);
  return {
    status: body.status,
    changed_today: {
      tasks_completed: Array.isArray(body.changed_today?.tasks_completed) ? body.changed_today.tasks_completed : [],
      total_completed: body.changed_today?.total_completed,
    },
    errors: Array.isArray(body.errors) ? body.errors : [],
  };
}

export type MemoryProbe = { total: number; error?: string };

export const MEMORY_PROBE_QUERY_KEY = ["cc2", "memory-probe"] as const;

/** Whether any memory note exists (first-run checklist): one row is enough. */
export async function fetchMemoryProbe(signal?: AbortSignal): Promise<MemoryProbe> {
  const body = await getJson<{ total?: number; error?: string }>("/api/memory?limit=1", "memory", signal);
  return { total: typeof body.total === "number" ? body.total : 0, ...(body.error ? { error: body.error } : {}) };
}

/**
 * A dispatch or self-pickup run still going: not ended and not abandoned.
 * Session runs are left out here because Running now reads open sessions
 * directly (with their task title and start time).
 */
export function isActiveRun(run: RunRow): boolean {
  return run.kind !== "session" && !run.ended_at && !run.abandoned;
}
