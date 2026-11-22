/**
 * The Runs history and status pages that replace legacy panes: Receipts
 * (`dispatch_receipts`), Telemetry (`telemetry`, with agent scorecards) and
 * Executor (`executor`). Everything the legacy panes show is kept.
 */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useStreamInvalidation } from "../../lib/polling";
import { Button, DataTable, ErrorState, StatePill, UiLink, type Column } from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import {
  fetchExecutor,
  fetchReceipts,
  fetchScorecards,
  fetchTelemetry,
  runsKeys,
  WRITE_PATHS,
  type DelegationRow,
  type DispatchReceiptRow,
  type Metric,
  type Scorecard,
  type TelemetryException,
  type TelemetrySpan,
} from "./api";
import { formatWhen } from "./model";
import { Explainer, RunsHeader, RunsRegion, Section, Stat, WriteFailure } from "./parts";
import { RUN_CHANNELS } from "./OverviewPage";

const SPAN_LIMIT = 50;
const EXCEPTION_LIMIT = 30;

/** The hub returns telemetry oldest first (`telemetry/projection.py`): take the tail, newest first. */
export function newestFirst<T>(rows: readonly T[], limit: number): T[] {
  return rows.slice(-limit).reverse();
}

/** An exception's evidence time and id (`telemetry/projection.py` copies the span's `evidence_ref`). */
function evidenceOf(e: TelemetryException): { at: number | null; ulid: string } {
  const ref = (e.evidence_ref ?? null) as { ulid?: string | null; created_at?: string | null } | null;
  const t = ref?.created_at ? Date.parse(ref.created_at) : Number.NaN;
  return { at: Number.isFinite(t) ? t : null, ulid: ref?.ulid ?? "" };
}

/**
 * The exception digest is grouped, not chronological: ordinary exceptions
 * in span order, then repeated-failure and fallback-storm aggregates, then
 * missing-telemetry entries with no evidence at all. Sort by evidence time,
 * then ULID, newest first; entries with no time come last and are labelled.
 */
export function exceptionsNewestFirst(rows: readonly TelemetryException[], limit: number): TelemetryException[] {
  const dated = rows.filter((e) => evidenceOf(e).at !== null);
  const undated = rows.filter((e) => evidenceOf(e).at === null);
  dated.sort((a, b) => {
    const x = evidenceOf(a);
    const y = evidenceOf(b);
    return (y.at as number) - (x.at as number) || (y.ulid > x.ulid ? 1 : y.ulid < x.ulid ? -1 : 0);
  });
  return [...dated, ...undated].slice(0, limit);
}

function shortId(value: unknown, len = 12): string {
  const s = typeof value === "string" ? value : "";
  return s ? (s.length > len ? `${s.slice(0, len)}…` : s) : "None";
}

// ---- Receipts ---------------------------------------------------------------

const RECEIPT_COLUMNS: Column<DispatchReceiptRow>[] = [
  { id: "task", header: "Task", cell: (r) => <UiLink href={`/work/tasks/${r.task_id}`}>#{r.task_id}</UiLink> },
  { id: "agent", header: "Agent", cell: (r) => r.agent_label },
  {
    id: "state",
    header: "Outcome",
    cell: (r) => (
      <span className="flex flex-col gap-0.5">
        <StatePill
          kind={!r.terminal ? "live" : r.state === "finished" ? "ok" : r.state === "failed" || r.state === "denied" ? "danger" : "warn"}
          label={r.terminal ? r.state : `${r.state} (in progress)`}
        />
        {r.failure_reason ? <span className="text-xs text-[var(--ua-muted)]">{r.failure_reason}{r.failure_class ? ` (${r.failure_class})` : ""}</span> : null}
      </span>
    ),
  },
  { id: "evidence", header: "Evidence", cell: (r) => (r.has_evidence ? <span className="font-mono text-xs break-all">{r.evidence_ref}</span> : "None recorded") },
  { id: "run", header: "Run", cell: (r) => <span className="font-mono text-xs break-all">{r.run_id}</span> },
  { id: "updated", header: "Last change", cell: (r) => formatWhen(r.updated_at) },
];

export function ReceiptsPage() {
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: runsKeys.receipts, queryFn: ({ signal }) => fetchReceipts(signal) });
  useStreamInvalidation(() => void queryClient.invalidateQueries({ queryKey: runsKeys.receipts }), { channels: RUN_CHANNELS, onReconnect: true });
  return (
    <>
      <RunsHeader title="Receipts" description="Proof of what each dispatched run delivered." />
      <Explainer>
        A receipt is the hub's record of how a dispatched run ended: finished with evidence, failed and why, or still going. This page only reads;
        cancelling or resuming a run happens elsewhere. For one run's full receipt, an agent calls the dispatch_delivery_receipt tool with its run id.
      </Explainer>
      <RunsRegion
        query={q}
        loadingLabel="Loading delivery receipts"
        errorTitle="Delivery receipts could not be loaded"
        isEmpty={(d) => d.receipts.length === 0}
        empty={{ title: "No delivery receipts yet", description: "Receipts appear when the hub dispatches a run." }}
      >
        {(d) => (
          <>
            <div className="mb-3 flex flex-wrap gap-2">
              <Stat label="Recent receipts" value={d.count ?? d.receipts.length} />
              <Stat label="Ended" value={d.terminal_count ?? d.receipts.filter((r) => r.terminal).length} />
            </div>
            <p className="m-0 mb-2 text-xs text-[var(--ua-muted)]">The hub returns its most recent receipts only.</p>
            <DataTable caption="Recent dispatch delivery receipts" columns={RECEIPT_COLUMNS} data={d.receipts} getRowKey={(r) => r.run_id} />
          </>
        )}
      </RunsRegion>
    </>
  );
}

// ---- Telemetry --------------------------------------------------------------

const SPAN_COLUMNS: Column<TelemetrySpan>[] = [
  { id: "id", header: "Trace / span", cell: (s) => <span className="font-mono text-xs">{shortId(s.trace_id)} / {shortId(s.span_id)}</span> },
  { id: "kind", header: "Kind", cell: (s) => s.kind || "Not recorded" },
  {
    id: "outcome",
    header: "Outcome",
    cell: (s) => <StatePill kind={s.outcome === "success" ? "ok" : s.outcome ? "danger" : "unknown"} label={s.outcome || "unknown"} />,
  },
  { id: "duration", header: "Duration", cell: (s) => (s.duration_ms == null ? "Not recorded" : `${s.duration_ms} ms`) },
  {
    id: "usage",
    header: "Tokens and cost",
    cell: (s) => `${s.usage?.tokens_total ?? 0} tokens${s.usage?.cost_usd == null ? "" : ` · $${Number(s.usage.cost_usd).toFixed(4)}`}`,
  },
  { id: "agent", header: "Agent and task", cell: (s) => `${s.agent ?? "Unknown agent"}${typeof s.task_id === "number" ? ` · #${s.task_id}` : ""}` },
  { id: "evidence", header: "Evidence", cell: (s) => <span className="font-mono text-xs">{shortId(s.evidence_ref?.ulid, 14)}</span> },
];

function EscalateButton({ exc }: { exc: TelemetryException }) {
  const [done, setDone] = useState(false);
  const taskId = exc.task_id as number;
  const write = useHubWrite<{ id: number; append_note: string }, unknown>({
    path: WRITE_PATHS.task,
    label: `Escalating to task #${taskId}`,
    invalidates: [runsKeys.telemetry],
    successMessage: `Added a note about this exception to task #${taskId}.`,
    // Not proven to rebuild the identical approval payload; submit again after approval.
    resumable: false,
  });
  const evidenceId = exc.evidence_ref?.ulid ?? "unreferenced";
  return (
    <span className="mt-1 flex flex-col gap-1">
      <Button
        size="sm"
        disabled={done}
        loading={write.isRunning}
        onClick={async () => {
          const note = `[exception-escalated: ${evidenceId}] ${exc.kind || "exception"}: ${exc.message || "telemetry exception"}`;
          const outcome = await write.run({ id: taskId, append_note: note });
          if (outcome.kind === "done") setDone(true);
        }}
      >
        {done ? `Escalated to #${taskId}` : `Escalate to #${taskId}`}
      </Button>
      {write.state.status === "error" ? <WriteFailure message={write.state.message} detail={write.state.detail} /> : null}
      {write.state.status === "pending_approval" ? <ParkedWriteNotice approvalId={write.state.approvalId} label={`The note on task #${taskId}`} /> : null}
    </span>
  );
}

const EXCEPTION_COLUMNS: Column<TelemetryException>[] = [
  {
    id: "when",
    header: "When",
    cell: (e) => {
      const at = evidenceOf(e).at;
      return at === null ? <span className="text-[var(--ua-muted)]">No time recorded</span> : formatWhen(new Date(at).toISOString());
    },
  },
  { id: "trace", header: "Trace", cell: (e) => <span className="font-mono text-xs">{shortId(e.trace_id ?? e.evidence_ref?.ulid, 14)}</span> },
  { id: "kind", header: "Kind", cell: (e) => e.kind || "exception" },
  { id: "severity", header: "Severity", cell: (e) => e.severity || "medium" },
  { id: "count", header: "Count", cell: (e) => (e.count == null ? "Not counted" : String(e.count)) },
  { id: "message", header: "Message", cell: (e) => (e.message ? String(e.message).slice(0, 240) : "None") },
  {
    id: "act",
    header: "Evidence and action",
    cell: (e) => (
      <span className="flex flex-col">
        <span className="font-mono text-xs">{shortId(e.evidence_ref?.ulid, 14)}</span>
        {typeof e.task_id === "number" ? <EscalateButton exc={e} /> : null}
      </span>
    ),
  },
];

function rate(m: Metric | undefined): string {
  if (!m || m.value === null) return "Not enough data";
  const pct = `${Math.round(m.value * 100)}%`;
  return m.sample === "small" ? `${pct} (only ${m.denominator})` : pct;
}

const SCORECARD_COLUMNS: Column<Scorecard>[] = [
  { id: "agent", header: "Agent", cell: (a) => a.agent },
  { id: "runs", header: "Runs finished", cell: (a) => String(a.runs.finished) },
  { id: "delivered", header: "Delivered", cell: (a) => rate(a.delivery_rate) },
  { id: "abandoned", header: "Abandoned", cell: (a) => rate(a.abandon_rate) },
  { id: "approved", header: "Review approved", cell: (a) => rate(a.review_approval_rate) },
  { id: "reopened", header: "Reopened", cell: (a) => rate(a.reopen_rate) },
  { id: "harness", header: "Tool failures", cell: (a) => (a.harness.attempts ? `${a.harness.harness_failures} of ${a.harness.attempts}` : "None run") },
  { id: "quality", header: "Quality success", cell: (a) => rate(a.harness.quality_success_rate) },
  {
    id: "evals",
    header: "Evaluations",
    cell: (a) => (a.evals.length ? a.evals.map((e) => `${e.suite} v${e.suite_version}${e.model ? ` on ${e.model}` : ""}: ${rate(e.pass_rate)}`).join("; ") : "None"),
  },
];

export function TelemetryPage() {
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: runsKeys.telemetry, queryFn: ({ signal }) => fetchTelemetry(signal) });
  const scorecards = useQuery({ queryKey: runsKeys.scorecards, queryFn: ({ signal }) => fetchScorecards(signal), staleTime: 60_000 });
  useStreamInvalidation(
    () => {
      void queryClient.invalidateQueries({ queryKey: runsKeys.telemetry });
      void queryClient.invalidateQueries({ queryKey: runsKeys.scorecards });
    },
    { channels: ["agent.telemetry*", ...RUN_CHANNELS], onReconnect: true },
  );
  return (
    <>
      <RunsHeader title="Telemetry" description="Run timings, token use, costs, errors and agent scorecards." />
      <Explainer>
        A span is one timed piece of a run (a tool call, a model call); spans of one run share a trace. Only spans with a problem are kept in the
        exception digest.
      </Explainer>
      <RunsRegion
        query={q}
        loadingLabel="Loading telemetry"
        errorTitle="Telemetry could not be loaded"
        isEmpty={(d) => d.waterfall.spans.length === 0 && d.exception_digest.exceptions.length === 0}
        empty={{ title: "No telemetry yet", description: "Spans appear when agents report their runs." }}
      >
        {(d) => {
          const summary = d.waterfall.summary ?? {};
          const exceptions = d.exception_digest.exceptions;
          const total = d.exception_digest.total ?? exceptions.length;
          return (
            <>
              <div className="mb-4 flex flex-wrap gap-2" role="group" aria-label="Telemetry summary">
                <Stat label="Traces" value={summary.traces ?? 0} />
                <Stat label="Spans" value={summary.spans ?? 0} />
                <Stat label="Tokens" value={summary.tokens_total ?? 0} />
                <Stat label="Cost" value={summary.cost_usd == null ? "Not reported" : `$${Number(summary.cost_usd).toFixed(4)}`} />
                <Stat label="Exceptions" value={total} tone={total > 0 ? "warn" : undefined} />
              </div>
              {d.exception_digest.normal_spans_omitted != null ? (
                <p className="m-0 mb-3 text-xs text-[var(--ua-muted)]">{d.exception_digest.normal_spans_omitted} spans without problems are left out of the digest.</p>
              ) : null}
              <Section title="Spans" description={`The newest ${Math.min(SPAN_LIMIT, d.waterfall.spans.length)} of the ${d.waterfall.spans.length} spans read, newest first.`}>
                <DataTable caption={`Telemetry spans, newest ${SPAN_LIMIT}`} columns={SPAN_COLUMNS} data={newestFirst(d.waterfall.spans, SPAN_LIMIT)} getRowKey={(s, i) => `${s.span_id ?? "span"}-${i}`} emptyText="No spans in this window." />
              </Section>
              <Section title="Exception digest" description="Escalating adds a note with the evidence to the task the exception belongs to.">
                <DataTable caption={`Telemetry exceptions, newest ${EXCEPTION_LIMIT}`} columns={EXCEPTION_COLUMNS} data={exceptionsNewestFirst(exceptions, EXCEPTION_LIMIT)} getRowKey={(e, i) => `${e.trace_id ?? "exc"}-${i}`} emptyText="No exceptions in the current window." />
              </Section>
            </>
          );
        }}
      </RunsRegion>
      <Section title={`Agent scorecards${scorecards.data?.window_days ? ` (last ${scorecards.data.window_days} days)` : ""}`} description="Rates from each agent's finished runs and reviews. A rate over a small number of runs says how many.">
        <RunsRegion
          query={scorecards}
          loadingLabel="Loading scorecards"
          errorTitle="Scorecards could not be loaded"
          isEmpty={(d) => d.agents.length === 0}
          empty={{ title: "No scorecards yet", description: "Scorecards appear once agents finish runs." }}
        >
          {(d) => (
            <>
              <DataTable caption="Agent scorecards" columns={SCORECARD_COLUMNS} data={d.agents} getRowKey={(a) => a.agent} />
              <p className="m-0 mt-2 text-xs text-[var(--ua-muted)]">First-pass CI rate is not available yet.</p>
              {d.suite_errors?.length ? (
                <p role="note" className="m-0 mt-2 text-sm text-[var(--ua-warn)]">
                  Some evaluation suites could not be read: {d.suite_errors.map((e) => `${e.file}: ${e.error}`).join("; ")}
                </p>
              ) : null}
            </>
          )}
        </RunsRegion>
      </Section>
    </>
  );
}

// ---- Executor ---------------------------------------------------------------

const DELEGATION_COLUMNS: Column<DelegationRow>[] = [
  { id: "id", header: "Delegation", cell: (d) => `#${d.id ?? d.task_id ?? "?"}` },
  { id: "task", header: "Task", cell: (d) => (d.task_id != null ? <UiLink href={`/work/tasks/${d.task_id}`}>#{d.task_id}</UiLink> : "None") },
  { id: "from", header: "From", cell: (d) => d.from_agent || "Not recorded" },
  { id: "to", header: "To", cell: (d) => d.to_agent || "Not recorded" },
  { id: "status", header: "Status", cell: (d) => d.status || "Not recorded" },
  { id: "deadline", header: "Deadline", cell: (d) => (d.deadline ? formatWhen(d.deadline) : "None") },
];

type SessionRow = NonNullable<Awaited<ReturnType<typeof fetchExecutor>>["recent_sessions"]>[number];
const SESSION_COLUMNS: Column<SessionRow>[] = [
  { id: "agent", header: "Agent", cell: (s) => String(s.agent || "Not recorded") },
  { id: "kind", header: "Kind", cell: (s) => String(s.kind || "Not recorded") },
  { id: "status", header: "Status", cell: (s) => String(s.status || "Not recorded") },
  { id: "model", header: "Model", cell: (s) => String(s.model || "Not recorded") },
  { id: "task", header: "Task", cell: (s) => (s.task_id != null ? <UiLink href={`/work/tasks/${s.task_id}`}>#{s.task_id}</UiLink> : "None") },
];

type Worktree = Record<string, unknown>;
const WORKTREE_COLUMNS: Column<Worktree>[] = [
  { id: "path", header: "Folder", cell: (w) => <span className="font-mono text-xs break-all">{String(w.path || w.worktree_path || "Not recorded")}</span> },
  { id: "reason", header: "Why it is kept", cell: (w) => String(w.reason || w.status || "Not recorded") },
  { id: "detail", header: "Detail", cell: (w) => String(w.detail || w.last_modified || "") },
];

const COUNT_KEYS = ["pending", "running", "completed", "cancelled", "error"] as const;

export function ExecutorPage() {
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: runsKeys.executor, queryFn: ({ signal }) => fetchExecutor(signal) });
  useStreamInvalidation(() => void queryClient.invalidateQueries({ queryKey: runsKeys.executor }), { channels: ["agent.delegation.*", "agent.session*", "agent.health*"], onReconnect: true });
  return (
    <>
      <RunsHeader title="Executor" description="The background worker that runs approved work, and the work it holds." />
      <Explainer>
        The executor picks up work handed from one agent to another (a delegation) and runs it. This page shows its health, the delegations it holds,
        recent failures and sessions, and work folders it kept.
      </Explainer>
      <RunsRegion
        query={q}
        loadingLabel="Loading the executor status"
        errorTitle="The executor status could not be loaded"
        empty={{ title: "Nothing to show" }}
      >
        {(d) => {
          const check = d.health?.check;
          const partial = Array.isArray(d.errors) && d.errors.length > 0;
          return (
            <>
              {partial ? (
                <p role="alert" className="m-0 mb-3 rounded-md border border-l-4 border-[var(--ua-warn)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
                  Part of this status could not be read, so counts below may be incomplete. {d.errors?.join("; ")}
                </p>
              ) : null}
              <p className="m-0 mb-3 flex flex-wrap items-center gap-2 text-sm">
                {check ? (
                  <>
                    <StatePill kind={check.status === "LIVE" ? "ok" : check.status === "DEGRADED" || check.status === "MISCONFIGURED" ? "warn" : "unknown"} label={check.status || "UNKNOWN"} />
                    <span>
                      {check.capability || "active_executor_service"}
                      {check.detail ? `: ${check.detail}` : ""}
                    </span>
                  </>
                ) : (
                  "The hub did not report the executor's health."
                )}
              </p>
              <div className="mb-4 flex flex-wrap gap-2" role="group" aria-label="Delegations by status">
                {COUNT_KEYS.map((k) => (
                  <Stat key={k} label={k[0].toUpperCase() + k.slice(1)} value={partial || !d.counts ? "–" : (d.counts[k] ?? 0)} />
                ))}
              </div>
              <Section title="Delegations">
                <DataTable caption="Delegations between agents" columns={DELEGATION_COLUMNS} data={d.delegations} getRowKey={(r, i) => String(r.id ?? i)} emptyText="No delegations recorded." />
              </Section>
              <Section title="Recent failures">
                <DataTable caption="Recent delegation failures" columns={DELEGATION_COLUMNS} data={d.recent_failures} getRowKey={(r, i) => String(r.id ?? i)} emptyText="No recent failures." />
              </Section>
              <Section title="Recent sessions">
                <DataTable caption="Recent executor sessions" columns={SESSION_COLUMNS} data={d.recent_sessions ?? []} getRowKey={(s, i) => String(s.id ?? i)} emptyText="No recent executor sessions." />
              </Section>
              <Section title="Kept work folders">
                {d.worktree_error ? (
                  <ErrorState
                    headingLevel={3}
                    title="The work folders could not be surveyed"
                    message="The hub could not check its work folders, so it is not known whether any are stale or kept."
                    detail={d.worktree_error}
                    onRetry={() => void q.refetch()}
                  />
                ) : (
                  <DataTable caption="Stale and kept work folders" columns={WORKTREE_COLUMNS} data={d.stale_worktrees} getRowKey={(w, i) => String(w.path ?? w.worktree_path ?? i)} emptyText="No stale or kept work folders." />
                )}
              </Section>
            </>
          );
        }}
      </RunsRegion>
    </>
  );
}
