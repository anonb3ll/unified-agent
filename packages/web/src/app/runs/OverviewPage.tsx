import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useStreamInvalidation } from "../../lib/polling";
import { Button, DataTable, StatePill, UiLink, type Column } from "../../lib/ui";
import { fetchRuns, runsKeys, type RunRow, type RunsPage } from "./api";
import { formatWhen, isRunning, runDuration, runPill, RUN_KIND_TEXT } from "./model";
import { Explainer, RunsHeader, RunsRegion, Section } from "./parts";

export const OVERVIEW_PAGE_SIZE = 50;
export const RUN_CHANNELS = ["agent.run*", "agent.dispatch.*", "agent.session*", "agent.lifecycle", "agent.lifecycle.*"];

function useNow(intervalMs = 15_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

function columns(now: number): Column<RunRow>[] {
  return [
    {
      id: "run",
      header: "Run",
      cell: (r) => (
        <span className="flex flex-col">
          <span className="font-mono text-xs break-all">{r.run_id}</span>
          <span className="text-xs text-[var(--ua-muted)]">{RUN_KIND_TEXT[r.kind] ?? r.kind}</span>
        </span>
      ),
    },
    {
      id: "task",
      header: "Task",
      cell: (r) => (r.task_id != null ? <UiLink href={`/work/tasks/${r.task_id}`}>#{r.task_id}</UiLink> : <span className="text-[var(--ua-muted)]">No task</span>),
    },
    { id: "agent", header: "Agent", cell: (r) => r.agent ?? <span className="text-[var(--ua-muted)]">Not recorded</span> },
    {
      id: "status",
      header: "Status",
      cell: (r) => {
        const pill = runPill(r);
        return (
          <span className="flex flex-col gap-0.5">
            <StatePill kind={pill.kind} label={pill.label} />
            {r.failure_reason ? <span className="text-xs text-[var(--ua-muted)]">{r.failure_reason}</span> : null}
          </span>
        );
      },
    },
    { id: "started", header: "Started", cell: (r) => formatWhen(r.started_at) },
    {
      id: "duration",
      header: "Duration",
      cell: (r) => {
        const d = runDuration(r, now);
        if (!d) return <span className="text-[var(--ua-muted)]">Not recorded</span>;
        return d.running ? `${d.text} so far` : d.text;
      },
    },
    { id: "cost", header: "Cost", cell: () => <span className="text-[var(--ua-muted)]">Not reported</span> },
  ];
}

/**
 * Announce, politely and by id, runs that started or finished since the last
 * read. Never moves focus.
 */
function useRunAnnouncements(rows: readonly RunRow[] | undefined): string {
  const previous = useRef<Map<string, boolean> | null>(null);
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (!rows) return;
    const current = new Map(rows.map((r) => [r.run_id, isRunning(r)]));
    const before = previous.current;
    previous.current = current;
    if (!before) return;
    const parts: string[] = [];
    for (const [id, running] of current) {
      const was = before.get(id);
      if (was === undefined && running) parts.push(`Run ${id} started.`);
      else if (was === true && !running) parts.push(`Run ${id} ended.`);
    }
    if (parts.length) setMessage(parts.slice(0, 5).join(" "));
  }, [rows]);
  return message;
}

/** `/runs`: what is running now and the newest runs (spec §9.6 Overview). */
export function OverviewPage() {
  const queryClient = useQueryClient();
  const now = useNow();
  const runs = useInfiniteQuery<RunsPage, Error, InfiniteData<RunsPage>, readonly unknown[], string | null>({
    queryKey: runsKeys.runs(OVERVIEW_PAGE_SIZE),
    queryFn: ({ signal, pageParam }) => fetchRuns({ limit: OVERVIEW_PAGE_SIZE, cursor: pageParam }, signal),
    initialPageParam: null,
    getNextPageParam: (last) => (last.truncated && last.next_cursor ? last.next_cursor : undefined),
  });
  useStreamInvalidation(() => void queryClient.invalidateQueries({ queryKey: runsKeys.runs(OVERVIEW_PAGE_SIZE) }), {
    channels: RUN_CHANNELS,
    onReconnect: true,
  });
  const rows = useMemo(() => runs.data?.pages.flatMap((p) => p.runs), [runs.data]);
  const announcement = useRunAnnouncements(rows);
  const cols = useMemo(() => columns(now), [now]);
  const lastPage = runs.data?.pages[runs.data.pages.length - 1];
  const incomplete = Boolean(lastPage?.truncated);

  return (
    <>
      <RunsHeader title="Runs" description="What is running now and what ran recently, across every agent." />
      <Explainer>
        A run is one attempt by an agent at a task: work the hub dispatched, a task an agent picked up itself, or an agent session. Open the other
        sections to start workflows, graph runs, routines and cycles.
      </Explainer>
      <p role="status" aria-live="polite" className="sr-only">
        {announcement}
      </p>
      <RunsRegion
        query={runs}
        loadingLabel="Loading runs"
        errorTitle="Runs could not be loaded"
        isEmpty={(d) => d.pages.every((p) => p.runs.length === 0)}
        empty={{ title: "No runs yet", description: "When an agent starts work on a task, the run appears here." }}
      >
        {(data, truth) => {
          const all = data.pages.flatMap((p) => p.runs);
          const running = all.filter(isRunning);
          // After a failed refresh the rows are what was seen before: no present-tense all-clear, no current counts.
          const current = truth.state === "ready";
          return (
            <>
              <Section
                title={current ? "Running now" : "Running at the last successful read"}
                description={
                  !current
                    ? "Previously observed: the latest refresh failed, so this may have changed."
                    : incomplete
                    ? `Among the newest ${all.length} runs. Older runs were not read, so a long-running run that started earlier may be missing. Load older runs to check.`
                    : undefined
                }
              >
                {running.length === 0 ? (
                  <p className="m-0 text-sm text-[var(--ua-muted)]">
                    {!current
                      ? "Nothing was running at the last successful read. That may have changed since."
                      : incomplete
                        ? `Nothing is running among the newest ${all.length} runs. Older runs were not checked.`
                        : "Nothing is running right now."}
                  </p>
                ) : (
                  <DataTable
                    caption={!current ? "Running at the last successful read (previously observed)" : incomplete ? `Running now, at least ${running.length}` : `Running now, ${running.length}`}
                    columns={cols}
                    data={running}
                    getRowKey={(r) => r.run_id}
                  />
                )}
              </Section>
              <Section
                title="Recent runs"
                description={
                  !current
                    ? "Previously observed runs, as of the last successful read."
                    : incomplete
                      ? `Showing the newest ${all.length}. More runs exist.`
                      : `All ${all.length} runs the hub has recorded.`
                }
                actions={null}
              >
                <DataTable caption={current ? "Recent runs, newest first" : "Recent runs as previously observed, newest first"} columns={cols} data={all} getRowKey={(r) => r.run_id} />
                <p className="m-0 mt-2 text-xs text-[var(--ua-muted)]">
                  Cost per run is not reported by this hub yet. Span costs are on the <UiLink href="/runs/telemetry">Telemetry</UiLink> page.
                </p>
                {runs.hasNextPage ? (
                  <div className="mt-3">
                    <Button onClick={() => void runs.fetchNextPage()} loading={runs.isFetchingNextPage}>
                      Load older runs
                    </Button>
                  </div>
                ) : null}
                {runs.isFetchNextPageError ? <p role="alert" className="m-0 mt-2 text-sm text-[var(--ua-danger)]">Older runs could not be loaded. Try again.</p> : null}
              </Section>
            </>
          );
        }}
      </RunsRegion>
    </>
  );
}
