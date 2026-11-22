import { useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { formatDuration, taskNodesFromGraph, type LineagePayload } from "../../lib/flow/types";
import { useStreamInvalidation } from "../../lib/polling";
import { DescriptionList, Field, GraphCanvas, LinkButton, Select, UiLink, type GraphEdge, type GraphNode } from "../../lib/ui";
import { useUrlParam } from "../lib/urlState";
import { fetchExecutionGraph, fetchFlowMetrics, fetchLineage, runsKeys } from "./api";
import { Explainer, RunsHeader, RunsRegion, Section, Stat } from "./parts";

const FLOW_CHANNELS = ["agent.task.*", "agent.plan*", "agent.dependency*", "agent.delegation.*", "agent.run*", "agent.cycle.*", "agent.lifecycle", "agent.lifecycle.*", "agent.approval.*", "agent.evidence.*", "agent.node"];

function dur(seconds: number | null | undefined): string {
  return formatDuration(seconds) ?? "Not enough data";
}

/** Task lineage as graph nodes: the task, then its delegations, dispatch runs and cycles. */
export function lineageGraph(taskId: number, title: string, lineage: LineagePayload["lineage"]): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const root = `task:${taskId}`;
  const nodes: GraphNode[] = [{ id: root, label: `#${taskId} ${title}`.trim(), description: "The task", tone: "decision" }];
  const edges: GraphEdge[] = [];
  const tone = (status: string | null | undefined): GraphNode["tone"] => {
    const s = (status ?? "").toLowerCase();
    if (["finished", "completed", "done", "merged"].includes(s)) return "ok";
    if (["failed", "error", "denied", "killed", "cancelled"].includes(s)) return "danger";
    if (["blocked", "expired"].includes(s)) return "warn";
    return s ? "live" : "default";
  };
  (lineage?.delegations ?? []).forEach((d, i) => {
    const id = `delegation:${d.id ?? i}`;
    nodes.push({ id, label: `Delegation #${d.id ?? "?"}`, description: `${d.from_agent ?? "?"} to ${d.to_agent ?? "?"}, ${d.status ?? "status not recorded"}`, tone: tone(d.status) });
    edges.push({ id: `${root}->${id}`, source: root, target: id, label: "handed off as" });
  });
  (lineage?.dispatch_runs ?? []).forEach((r, i) => {
    const id = `run:${r.id ?? i}`;
    nodes.push({ id, label: `Run ${r.id ?? i}`, description: r.status ?? "status not recorded", tone: tone(r.status) });
    edges.push({ id: `${root}->${id}`, source: root, target: id, label: "ran as" });
  });
  (lineage?.autonomous_cycles ?? []).forEach((c, i) => {
    const id = `cycle:${c.id ?? i}`;
    nodes.push({ id, label: `Cycle ${c.id ?? i}`, description: c.status ?? "status not recorded", tone: tone(c.status) });
    edges.push({ id: `${root}->${id}`, source: root, target: id, label: "supervised by" });
  });
  return { nodes, edges };
}

/** `/runs/insights`: the legacy Flow analytics (spec §11 `flow`). */
export function InsightsPage() {
  const queryClient = useQueryClient();
  const [taskParam, setTask] = useUrlParam("task");
  const taskId = /^\d+$/.test(taskParam) && Number(taskParam) > 0 ? Number(taskParam) : null;
  const metrics = useQuery({ queryKey: runsKeys.flowMetrics, queryFn: ({ signal }) => fetchFlowMetrics(signal) });
  const graph = useQuery({ queryKey: runsKeys.executionGraph, queryFn: ({ signal }) => fetchExecutionGraph(signal) });
  const lineage = useQuery({ queryKey: runsKeys.lineage(taskId ?? 0), queryFn: ({ signal }) => fetchLineage(taskId ?? 0, signal), enabled: taskId !== null });
  useStreamInvalidation(
    () => {
      void queryClient.invalidateQueries({ queryKey: runsKeys.flowMetrics });
      void queryClient.invalidateQueries({ queryKey: runsKeys.executionGraph });
      if (taskId !== null) void queryClient.invalidateQueries({ queryKey: runsKeys.lineage(taskId) });
    },
    { channels: FLOW_CHANNELS, onReconnect: true },
  );
  const tasks = useMemo(() => (graph.data ? taskNodesFromGraph(graph.data) : []), [graph.data]);

  return (
    <>
      <RunsHeader title="Insights" description="How work flows: how long tasks take, what is waiting, and how each task was carried out." />
      <Explainer>
        Cycle time is how long a task takes from start to done; lead time is from when it was created. The 50th percentile is the typical task, the
        90th the slow ones. The graph of tasks and plans is now on the <UiLink href="/work?view=graph">Work graph</UiLink>.
      </Explainer>
      <Section title="Flow">
        <RunsRegion query={metrics} loadingLabel="Loading flow measures" errorTitle="Flow measures could not be loaded" empty={{ title: "No measures" }}>
          {(m) => (
            <>
              <div className="flex flex-wrap gap-2" role="group" aria-label="Flow measures">
                <Stat label="Cycle time, typical (p50)" value={dur(m.cycle_time.p50_s)} />
                <Stat label="Cycle time, slow (p90)" value={dur(m.cycle_time.p90_s)} />
                <Stat label="Lead time, typical (p50)" value={dur(m.lead_time.p50_s)} />
                <Stat label="Lead time, slow (p90)" value={dur(m.lead_time.p90_s)} />
                <Stat label="Oldest work in progress" value={dur(m.wip_age.oldest_s)} />
                {m.attribution_coverage?.coverage_ratio != null ? (
                  <Stat
                    label="Finished tasks with a known agent"
                    value={`${Math.round(m.attribution_coverage.coverage_ratio * 100)}% (${m.attribution_coverage.attributed_done ?? 0} of ${m.attribution_coverage.total_done ?? 0})`}
                  />
                ) : null}
              </div>
              <h3 className="m-0 mt-4 mb-1 text-sm font-semibold">Work in progress by age</h3>
              {m.wip_age.buckets.length ? (
                <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
                  {m.wip_age.buckets.map((b) => (
                    <li key={b.label} className="rounded-md border border-[var(--ua-border)] px-2 py-1 text-sm">
                      <span className="font-mono tabular-nums">{b.count}</span> {b.label || "in progress"}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="m-0 text-sm text-[var(--ua-muted)]">Nothing in progress.</p>
              )}
              <p className="m-0 mt-2 text-xs text-[var(--ua-muted)]">
                Over the last {m.window_days} days{m.as_of ? `, as of ${new Date(m.as_of).toLocaleString()}` : ""}.
              </p>
            </>
          )}
        </RunsRegion>
      </Section>
      <Section title="How a task was carried out" description="Pick a task to see its hand-offs, runs and cycles.">
        <RunsRegion
          query={graph}
          loadingLabel="Loading tasks"
          errorTitle="The task list could not be loaded"
          isEmpty={() => tasks.length === 0}
          empty={{ title: "No tasks with recorded work", description: "Tasks appear here once agents hand them off or run them." }}
        >
          {(g) => (
            <>
              <p className="m-0 mb-2 text-sm text-[var(--ua-muted)]">
                {g.counts.tasks ?? tasks.length} tasks, {g.counts.delegations ?? 0} hand-offs and {g.counts.runs ?? 0} runs recorded.
              </p>
              <Field label="Task" className="max-w-xl">
                <Select
                  value={taskId === null ? "" : String(taskId)}
                  onChange={(e) => setTask(e.target.value || null, { push: true })}
                  placeholder="Choose a task"
                  options={tasks.map((t) => ({ value: String(t.task_id), label: `#${t.task_id} ${t.label ?? ""}${t.state ? ` (${t.state})` : ""}` }))}
                />
              </Field>
            </>
          )}
        </RunsRegion>
        {taskId !== null ? (
          <div className="mt-4">
            <RunsRegion
              query={lineage}
              headingLevel={3}
              loadingLabel={`Loading task #${taskId}`}
              errorTitle={`Task #${taskId}'s history could not be loaded`}
              isEmpty={(l) => Boolean(l.empty)}
              empty={{ title: `Task #${taskId} has no recorded hand-offs, runs or cycles` }}
            >
              {(l) => {
                const { nodes, edges } = lineageGraph(taskId, l.task?.title ?? "", l.lineage);
                const refs = l.lineage?.refs ?? {};
                return (
                  <>
                    <GraphCanvas label={`How task #${taskId} was carried out`} nodes={nodes} edges={edges} height={320} />
                    <DescriptionList
                      className="mt-3"
                      items={[
                        { term: "Pull request", value: refs.pr_url ? <UiLink href={refs.pr_url}>{refs.pr_url}</UiLink> : "" },
                        { term: "Branch", value: refs.branch ?? "" },
                        { term: "Work folder", value: refs.worktree_path ?? "" },
                      ]}
                    />
                    <div className="mt-3">
                      <LinkButton size="sm" href={`/work/tasks/${taskId}`}>
                        Open task #{taskId}
                      </LinkButton>
                    </div>
                  </>
                );
              }}
            </RunsRegion>
          </div>
        ) : null}
      </Section>
    </>
  );
}
