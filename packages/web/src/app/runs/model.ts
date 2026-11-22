/**
 * Pure helpers for the Runs area: run states in plain words, durations,
 * the cycle state machine (`tools/cycles.TRANSITIONS`) and routine receipts.
 */
import type { StateKind } from "../../lib/ui";
import type { Cycle, RunRow } from "./api";

export type Pill = { kind: StateKind; label: string };

/** Live states per store: `dispatch_states.LIVE_STATES`, an open self-pickup, an open session. */
const LIVE = new Set(["claimed", "launched", "running", "open", "starting", "waiting"]);

export function isRunning(run: RunRow): boolean {
  return !run.abandoned && !run.ended_at && LIVE.has(run.status);
}

export function runPill(run: RunRow): Pill {
  if (run.abandoned || run.status === "abandoned" || run.status === "expired") return { kind: "warn", label: "Abandoned" };
  if (isRunning(run)) return { kind: "live", label: run.status === "waiting" ? "Waiting" : "Running" };
  const s = (run.outcome ?? run.status ?? "").toLowerCase();
  if (["finished", "completed", "done", "success", "succeeded"].includes(s)) return { kind: "ok", label: "Finished" };
  if (["failed", "error", "denied", "refused"].includes(s)) return { kind: "danger", label: s === "denied" || s === "refused" ? "Refused" : "Failed" };
  if (["cancelled", "canceled", "killed"].includes(s)) return { kind: "unknown", label: "Cancelled" };
  if (s === "blocked") return { kind: "warn", label: "Blocked" };
  return { kind: "unknown", label: s ? s.replace(/_/g, " ") : "Unknown" };
}

export const RUN_KIND_TEXT: Record<string, string> = {
  dispatch: "Dispatched",
  selfpickup: "Picked up",
  session: "Session",
};

/** "1h 4m", "35s"; null when unknown (never a fake zero). */
export function formatDuration(ms: number | null | undefined): string | null {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return null;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

export function runDuration(run: RunRow, now: number): { text: string; running: boolean } | null {
  const start = run.started_at ? Date.parse(run.started_at) : Number.NaN;
  if (!Number.isFinite(start)) return null;
  const end = run.ended_at ? Date.parse(run.ended_at) : isRunning(run) ? now : Number.NaN;
  const text = formatDuration(end - start);
  return text ? { text, running: !run.ended_at && isRunning(run) } : null;
}

const WHEN = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

export function formatWhen(value: string | null | undefined): string {
  if (!value) return "Not recorded";
  const t = Date.parse(value);
  return Number.isFinite(t) ? WHEN.format(new Date(t)) : value;
}

// ---- cycles ---------------------------------------------------------------

export const CYCLE_TERMINAL = new Set(["completed", "failed", "killed"]);

/** `tools/cycles.TRANSITIONS`, minus the stop states the page offers separately. */
export const CYCLE_NEXT: Record<string, string[]> = {
  plan: ["delegated"],
  delegated: ["executing"],
  executing: ["validating", "rework"],
  validating: ["reviewing", "rework"],
  reviewing: ["pr", "rework"],
  rework: ["delegated", "executing"],
  pr: ["completed"],
  blocked: [],
};

export const CYCLE_STATE_TEXT: Record<string, string> = {
  plan: "Planning",
  delegated: "Handed to an agent",
  executing: "Doing the work",
  validating: "Checking the work",
  reviewing: "In review",
  rework: "Being reworked",
  pr: "Pull request open",
  blocked: "Paused",
  completed: "Completed",
  failed: "Failed",
  killed: "Stopped",
};

/** States that need human approval to enter (`tools/cycles.APPROVAL_ACTIONS`). */
export const CYCLE_APPROVAL_STATES = new Set(["pr", "completed", "blocked", "failed"]);

export function cyclePill(cycle: Cycle): Pill {
  const label = CYCLE_STATE_TEXT[cycle.state] ?? cycle.state;
  if (cycle.state === "completed") return { kind: "ok", label };
  if (cycle.state === "failed") return { kind: "danger", label };
  if (cycle.state === "killed") return { kind: "unknown", label };
  if (cycle.state === "blocked") return { kind: "warn", label };
  return { kind: "live", label };
}

export function canPause(cycle: Cycle): boolean {
  return !CYCLE_TERMINAL.has(cycle.state) && cycle.state !== "blocked";
}

export function canResume(cycle: Cycle): boolean {
  return cycle.state === "blocked";
}

export function canStop(cycle: Cycle): boolean {
  return !CYCLE_TERMINAL.has(cycle.state);
}

export type BudgetLine = { label: string; used: number; max: number | null; unit: string; over: boolean };

export function cycleBudgets(cycle: Cycle): BudgetLine[] {
  const line = (label: string, used: number | undefined, max: number | undefined, unit: string): BudgetLine => ({
    label,
    used: Number(used ?? 0),
    max: max === undefined || max === null ? null : Number(max),
    unit,
    over: max !== undefined && max !== null && Number(used ?? 0) > Number(max),
  });
  return [
    line("Time", cycle.used_time_s, cycle.max_time_s, "s"),
    line("Tokens", cycle.used_tokens, cycle.max_tokens, ""),
    line("Retries", cycle.used_retries, cycle.max_retries, ""),
    line("Cost", cycle.used_cost_cents, cycle.max_cost_cents, "¢"),
  ];
}

export function budgetText(b: BudgetLine): string {
  const n = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));
  return `${n(b.used)}${b.unit} of ${b.max === null ? "no limit" : `${n(b.max)}${b.unit}`}`;
}

/** A PR link or a commit SHA (`tools/cycles._looks_like_evidence`). */
export function looksLikeEvidence(value: string): boolean {
  return /^https?:\/\/\S+$/.test(value) || /^[0-9a-fA-F]{7,40}$/.test(value);
}

// ---- routines -------------------------------------------------------------

export const ROUTINE_RUN_TEXT: Record<string, string> = {
  created: "Created a task",
  queued: "Queued behind an earlier run",
  skipped: "Skipped",
  suppressed: "Skipped: nothing happened since the last run",
};

export const ROUTINE_REASON_TEXT: Record<string, string> = {
  overlap: "the previous run's task was still open",
  no_signal: "there was no activity since the last run",
  self_echo: "the only activity was the routine's own bookkeeping",
  catch_up_none: "the routine does not catch up on missed times",
};

export function routinePill(status: string): Pill {
  if (status === "created") return { kind: "ok", label: "Created" };
  if (status === "queued") return { kind: "warn", label: "Queued" };
  if (status === "skipped" || status === "suppressed") return { kind: "unknown", label: "Skipped" };
  return { kind: "unknown", label: status };
}

/** Plain words for a Run now receipt. */
export function fireResultText(result: { status: string; task_id: number | null }): string {
  switch (result.status) {
    case "created":
      return `It ran now and created task #${result.task_id}.`;
    case "queued":
      return `An earlier run is still active, so this run was queued as task #${result.task_id}. It starts when the earlier one finishes.`;
    case "skipped":
      return "Nothing was started: an earlier run of this routine is still active, and the routine is set to skip a run when that happens.";
    case "suppressed":
      return "Nothing was started: this routine only runs when there was activity since its last run, and there was none.";
    default:
      return `The hub answered "${result.status}".`;
  }
}
