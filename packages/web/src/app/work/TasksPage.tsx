/**
 * `/work`: one task set in four views over the same URL-held filters
 * (spec §9.2). `?view=list|board|tree|graph`, `&scope=work` for the work
 * graph, `&task=<id>` for the graph's task drawer.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { fetchWorkGraph, type WorkGraphPayload } from "../../lib/api/graph";
import { Button, Checkbox, DescriptionList, Drawer, EmptyState, FilterBar, LinkButton, PageHeader, Skeleton, Toolbar, UiLink } from "../../lib/ui";
import { QueryRegion } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import { fetchWorkGraphAll, mergeWorkGraphs, WORK_KEYS } from "./api";
import {
  boardHasDomain,
  evidenceUrl,
  delegationsFromBoard,
  distinct,
  filterTasks,
  FILTER_DEFAULTS,
  priorityLabel,
  PRIORITY_LABEL,
  queueLabel,
  STATUS_ORDER,
  statusLabel,
  tasksFromBoard,
  type TaskFilters,
  type WorkTask,
} from "./model";
import { QueueHealthPanel, SearchResults } from "./QueuePanels";
import { useBoard, useIdentity, useTaskGraphQuery, WorkTabs } from "./shared";
import { buildGraph, TaskBoardView, TaskGraphView, TaskListView, TaskTreeView, ViewButton } from "./TaskViews";

/** The task domains the hub accepts (task_update `domain`). */
const DOMAINS = ["personal", "professional", "hobby"] as const;
const VIEWS = ["list", "board", "tree", "graph"] as const;
type View = (typeof VIEWS)[number];
const VIEW_LABEL: Record<View, string> = { list: "List", board: "Board", tree: "Tree", graph: "Graph" };

export function TasksPage() {
  const { values, update } = useUrlParams({ ...FILTER_DEFAULTS, view: "list", scope: "", task: "", goal: "" });
  const view: View = (VIEWS as readonly string[]).includes(values.view) ? (values.view as View) : "list";
  const me = useIdentity();
  const board = useBoard();
  const all = useMemo(() => (board.data ? tasksFromBoard(board.data) : []), [board.data]);
  const filters: TaskFilters = values;
  const tasks = useMemo(() => filterTasks(all, filters, me ?? null), [all, filters, me]);
  const clearFilters = () => update(Object.fromEntries(Object.keys(FILTER_DEFAULTS).map((k) => [k, null])));
  const anyFilter = Object.keys(FILTER_DEFAULTS).some((k) => values[k as keyof TaskFilters] !== "");

  const option = (v: string, label = v) => ({ value: v, label });
  const hasDomain = board.data ? boardHasDomain(board.data) : false;
  const filterDefs = [
    { id: "status", label: "Status", value: values.status, options: STATUS_ORDER.map((s) => option(s, statusLabel(s))), onChange: (v: string) => update({ status: v }) },
    { id: "project", label: "Project", value: values.project, options: distinct(all, (t) => t.project).map((p) => option(p)), onChange: (v: string) => update({ project: v }) },
    // Only a hub whose board cards carry domain (#2480) gets the filter; elsewhere it would match nothing.
    ...(hasDomain
      ? [{ id: "domain", label: "Domain", value: values.domain, options: DOMAINS.map((d) => option(d, d[0].toUpperCase() + d.slice(1))), onChange: (v: string) => update({ domain: v }) }]
      : []),
    { id: "priority", label: "Priority", value: values.priority, options: Object.entries(PRIORITY_LABEL).map(([v, l]) => option(v, l)), onChange: (v: string) => update({ priority: v }) },
    { id: "owner", label: "Owner", value: values.owner, options: distinct(all, (t) => t.owner).map((p) => option(p)), onChange: (v: string) => update({ owner: v }) },
    { id: "queue", label: "Queue", value: values.queue, options: distinct(all, (t) => t.lane).map((l) => option(l, queueLabel(l))), onChange: (v: string) => update({ queue: v }) },
  ];

  return (
    <>
      <PageHeader
        title="Work"
        description="Tasks, goals and plans. Every view below shows the same tasks with the same filters."
        actions={
          <LinkButton href="/work/tasks/new" variant="primary">
            New task
          </LinkButton>
        }
        tabs={<WorkTabs />}
      />
      <div className="flex flex-col gap-4">
        <FilterBar
          label="Task filters"
          search={{ label: "Search tasks", value: values.q, onChange: (v) => update({ q: v }), placeholder: "Search by title, id or owner" }}
          filters={filterDefs}
          onClearAll={anyFilter ? clearFilters : undefined}
        >
          <div className="flex flex-wrap items-center gap-4 pb-1.5">
            <Checkbox label="Mine" checked={values.mine === "1"} onChange={(e) => update({ mine: e.target.checked ? "1" : null })} />
            <Checkbox label="Blocked" checked={values.blocked === "1"} onChange={(e) => update({ blocked: e.target.checked ? "1" : null })} />
            <Checkbox label="Waiting for review" checked={values.review === "1"} onChange={(e) => update({ review: e.target.checked ? "1" : null })} />
          </div>
        </FilterBar>
        <Toolbar label="Task view">
          {VIEWS.map((v) => (
            <ViewButton key={v} pressed={view === v} onClick={() => update({ view: v === "list" ? null : v }, { push: true })}>
              {VIEW_LABEL[v]}
            </ViewButton>
          ))}
          {view === "graph" && (
            <>
              <span aria-hidden="true" className="mx-1 h-6 w-px bg-[var(--ua-border)]" />
              <ViewButton pressed={values.scope !== "work"} onClick={() => update({ scope: null })}>
                Task dependencies
              </ViewButton>
              <ViewButton pressed={values.scope === "work"} onClick={() => update({ scope: "work" })}>
                Goals to work
              </ViewButton>
            </>
          )}
        </Toolbar>
        <SearchResults q={values.q} />
        {view === "list" && <QueueHealthPanel />}
        {view === "graph" && values.scope === "work" ? (
          // The work graph has its own reads (goals, plans, tasks); the board only narrows its tasks.
          <WorkGraph
            goal={values.goal}
            onGoal={(g) => update({ goal: g }, { push: true })}
            taskIds={new Set(tasks.map((t) => t.id))}
            filtered={anyFilter && board.status === "success"}
            onSelect={(id) => update({ task: id }, { push: true })}
          />
        ) : (
        <QueryRegion
          query={board}
          loadingLabel="Loading tasks"
          errorTitle="Tasks could not be loaded"
          // A board with delegations only is not empty: it has cards to show.
          isEmpty={(data) => tasksFromBoard(data).length === 0 && delegationsFromBoard(data).length === 0}
          empty={{
            title: "No tasks yet",
            description: "Tasks are the units of work agents and people pick up. Create the first one.",
            action: (
              <LinkButton href="/work/tasks/new" variant="primary">
                New task
              </LinkButton>
            ),
          }}
        >
          {(boardData, truth) => {
            // Counts only from a complete, fresh read (spec §5); a partial board is said to be partial.
            const complete = truth.state === "ready" || truth.state === "empty";
            const noMatch = (
              <EmptyState
                title={all.length === 0 ? "No tasks yet" : "No tasks match these filters"}
                description={all.length === 0 ? "Only delegations are waiting right now." : "Change or clear the filters to see more tasks."}
                action={
                  all.length === 0 ? undefined : (
                    <Button variant="secondary" onClick={clearFilters}>
                      Clear filters
                    </Button>
                  )
                }
              />
            );
            // The dependency graph also shows plans and agents, and the board delegations: they stay explorable.
            const otherData = view === "graph" || (view === "board" && !anyFilter && delegationsFromBoard(boardData).length > 0);
            return tasks.length === 0 && !otherData ? (
              noMatch
            ) : (
              <>
                {tasks.length === 0 && view === "graph" && noMatch}
                <p className="m-0 text-sm text-[var(--ua-muted)]" aria-live="polite">
                  {complete
                    ? `${tasks.length} of ${all.length} task${all.length === 1 ? "" : "s"}. `
                    : "Some tasks could not be read, so this list may be incomplete and no counts are shown. "}
                  Done shows tasks finished in the last 30 days.
                </p>
                {view === "list" && <TaskListView tasks={tasks} />}
                {view === "board" && (
                  <TaskBoardView
                    tasks={tasks}
                    complete={complete}
                    // Delegations are not tasks: shown only when no task filter would hide them.
                    delegations={anyFilter ? [] : delegationsFromBoard(boardData)}
                    wip={boardData.wip}
                  />
                )}
                {view === "tree" && <TaskTreeView tasks={tasks} />}
                {view === "graph" && <DependencyGraph tasks={tasks} onSelect={(id) => update({ task: id }, { push: true })} />}
              </>
            );
          }}
        </QueryRegion>
        )}
      </div>
      <TaskDrawer taskId={values.task} goal={values.goal} tasks={all} onClose={() => update({ task: null })} />
    </>
  );
}

/**
 * The task graph from the overview (task_graph_payload): the filtered tasks,
 * plus the plans and agents bound to them, with claim, subtask, dependency,
 * delegation and authorship links, and each task's attribution (who did it,
 * whether proof is recorded). A task opens the drawer; a plan or an agent
 * opens its page.
 */
function DependencyGraph({ tasks, onSelect }: { tasks: readonly WorkTask[]; onSelect: (taskId: string) => void }) {
  const graph = useTaskGraphQuery();
  const navigate = useNavigate();
  const overview = useMemo(() => new Map((graph.data?.nodes ?? []).map((n) => [n.id, n])), [graph.data]);
  const built = useMemo(() => {
    const links = (graph.data?.links ?? []).map((l) => ({ kind: String(l.kind ?? ""), from: l.source, to: l.target }));
    const taskNodes = tasks.map((t) => {
      const o = overview.get(`task:${t.id}`);
      const proof = o?.audit?.has_evidence ? "evidence recorded" : o ? "no evidence yet" : null;
      const who = t.owner ? `by ${t.owner}` : "unclaimed";
      return { id: `task:${t.id}`, label: `#${t.id} ${t.title}`, status: t.status, kind: "task", description: [statusLabel(t.status), who, proof].filter(Boolean).join(" · ") };
    });
    const others = (graph.data?.nodes ?? [])
      .filter((n) => n.kind === "plan" || n.kind === "agent")
      .map((n) => ({
        id: n.id,
        label: String(n.label ?? n.id),
        status: n.status,
        kind: String(n.kind),
        description: n.kind === "plan" ? `Plan · ${n.needs_human ? "needs a person" : String(n.status ?? "")}` : "Agent",
      }));
    return buildGraph([...taskNodes, ...others], links);
  }, [graph.data, tasks, overview]);
  if (graph.status === "pending") return <Skeleton label="Loading dependencies" variant="block" />;
  return (
    <>
      {graph.status === "error" && (
        <p role="status" className="m-0 text-sm text-[var(--ua-muted)]">
          The relations could not be loaded, so only the tasks are shown.
        </p>
      )}
      <TaskGraphView
        label="Task dependencies"
        nodes={built.nodes}
        edges={built.edges}
        onSelect={(id) => {
          if (id.startsWith("task:")) onSelect(id.slice(5));
          else if (id.startsWith("plan:")) navigate(`/work/plans/${encodeURIComponent(id.slice(5))}`);
          else if (id.startsWith("agent:")) navigate(`/agents/profile/${encodeURIComponent(id.slice(6))}`);
        }}
      />
    </>
  );
}

/**
 * The work graph (goal → plan → task). Without a goal the hub answers root
 * goals only (work_graph._root_page), so choosing a goal drills into its
 * subtree, held in the URL as `goal`; the task filters apply to its tasks.
 */
/** A readable name for a work-graph node; sessions, runs and evidence carry no label. */
function workNodeLabel(n: WorkGraphPayload["nodes"][number]): string {
  if (n.label) return n.label;
  if (n.kind === "session" && n.session_id) return `Session ${n.session_id}`;
  if (n.kind === "run" && n.run_id) return `Run ${n.run_id}`;
  if (n.kind === "evidence" && n.task_id) return `Evidence for #${n.task_id}`;
  return n.id;
}

function WorkGraph({
  goal,
  onGoal,
  taskIds,
  filtered,
  onSelect,
}: {
  goal: string;
  onGoal: (goalId: string | null) => void;
  taskIds: ReadonlySet<number>;
  filtered: boolean;
  onSelect: (taskId: string) => void;
}) {
  const goalId = /^\d+$/.test(goal) ? Number(goal) : null;
  const navigate = useNavigate();
  const roots = useQuery({ queryKey: WORK_KEYS.workGraph(null), queryFn: ({ signal }) => fetchWorkGraph({ pageSize: 50 }, signal), staleTime: 15_000, enabled: goalId === null });
  // Later pages read on request: root goals past the first page, and a goal's work past the page cap.
  const [moreRoots, setMoreRoots] = useState<WorkGraphPayload | null>(null);
  const [moreSubtree, setMoreSubtree] = useState<{ goal: number; data: WorkGraphPayload & { complete: boolean } } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState(false);
  const readMore = async (read: () => Promise<void>) => {
    setLoadingMore(true);
    setMoreError(false);
    try {
      await read();
    } catch {
      setMoreError(true);
    } finally {
      setLoadingMore(false);
    }
  };
  const subtree = useQuery({
    queryKey: WORK_KEYS.workGraph(goalId === null ? "none" : String(goalId)),
    queryFn: ({ signal }) => fetchWorkGraphAll(goalId as number, signal),
    staleTime: 15_000,
    enabled: goalId !== null,
  });
  if (goalId === null) {
    return (
      <QueryRegion
        query={roots}
        loadingLabel="Loading goals"
        errorTitle="The work graph could not be loaded"
        isEmpty={(d) => d.nodes.length === 0}
        empty={{ title: "No goals yet", description: "The work graph starts from goals. Create a goal and link tasks to it to see the chain from goal to plan to task." }}
      >
        {(first) => {
          const data = moreRoots ? mergeWorkGraphs(first, moreRoots) : first;
          const built = buildGraph(
            data.nodes.map((n) => ({ id: n.id, label: n.label ?? n.id, status: n.status, kind: n.kind })),
            data.edges.map((e) => ({ kind: e.kind, from: e.from, to: e.to })),
          );
          const next = data.next_cursor;
          return (
            <>
              <p className="m-0 text-sm text-[var(--ua-muted)]">Choose a goal to see the plans and tasks that serve it.</p>
              {next && (
                <div className="flex items-center gap-2">
                  <Button
                    size="sm"
                    loading={loadingMore}
                    onClick={() => void readMore(async () => setMoreRoots(mergeWorkGraphs(data, await fetchWorkGraph({ pageSize: 50, cursor: next }))))}
                  >
                    Show more goals
                  </Button>
                  {moreError && <span role="alert" className="text-sm">More goals could not be loaded. Try again.</span>}
                </div>
              )}
              <TaskGraphView
                label="Goals, plans and tasks"
                nodes={built.nodes}
                edges={built.edges}
                onSelect={(id) => {
                  const node = data.nodes.find((n) => n.id === id);
                  if (node?.goal_id) onGoal(String(node.goal_id));
                }}
              />
            </>
          );
        }}
      </QueryRegion>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <p className="m-0 text-sm">
        <UiLink href="/work?view=graph&scope=work" onClick={(e) => { e.preventDefault(); onGoal(null); }} className="text-[var(--ua-link)] underline">
          All goals
        </UiLink>
      </p>
      <QueryRegion query={subtree} loadingLabel="Loading the goal's work" errorTitle="The goal's work could not be loaded" empty={{ title: "" }}>
        {(first) => {
          const data = moreSubtree && moreSubtree.goal === goalId ? moreSubtree.data : first;
          const goalNode = data.nodes.find((n) => n.kind === "goal" && n.goal_id === goalId);
          const nodes = data.nodes.filter((n) => !n.stub && (n.kind !== "task" || !filtered || (n.task_id !== undefined && taskIds.has(n.task_id))));
          const built = buildGraph(
            nodes.map((n) => ({ id: n.id, label: workNodeLabel(n), status: n.status, kind: n.kind })),
            data.edges.map((e) => ({ kind: e.kind, from: e.from, to: e.to })),
          );
          return (
            <>
              {!data.complete && (
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <p className="m-0">This goal has more work than one view reads; only the pages read so far are shown.</p>
                  <Button
                    size="sm"
                    loading={loadingMore}
                    onClick={() =>
                      void readMore(async () => {
                        const more = await fetchWorkGraphAll(goalId, undefined, data.next_cursor);
                        setMoreSubtree({ goal: goalId, data: { ...mergeWorkGraphs(data, more), complete: more.complete } });
                      })
                    }
                  >
                    Read more of this goal
                  </Button>
                  {moreError && <span role="alert">More could not be loaded. Try again.</span>}
                </div>
              )}
              <TaskGraphView
                label={goalNode?.label ?? `Goal ${goalId}`}
                nodes={built.nodes}
                edges={built.edges}
                onSelect={(id) => {
                  // Every node kind opens its detail: a task its drawer, a goal its subtree, the others their pages.
                  const node = data.nodes.find((n) => n.id === id);
                  if (!node) return;
                  if (node.kind === "task" && node.task_id) onSelect(String(node.task_id));
                  else if (node.kind === "goal" && node.goal_id && node.goal_id !== goalId) onGoal(String(node.goal_id));
                  else if (node.kind === "plan" && node.plan_id) navigate(`/work/plans/${encodeURIComponent(node.plan_id)}`);
                  else if (node.kind === "session" && node.session_id) navigate(`/sessions/${encodeURIComponent(node.session_id)}`);
                  else if (node.kind === "evidence" && node.task_id) navigate(`/settings/audit?task_id=${node.task_id}`);
                  else if (node.kind === "run" && node.run_id) navigate(`/runs?run=${encodeURIComponent(node.run_id)}`);
                  else if (node.kind === "agent") navigate(`/agents/profile/${encodeURIComponent(String(node.label ?? node.id.replace(/^agent:/, "")))}`);
                }}
              />
            </>
          );
        }}
      </QueryRegion>
    </div>
  );
}

function TaskDrawer({ taskId, goal, tasks, onClose }: { taskId: string; goal: string; tasks: readonly WorkTask[]; onClose: () => void }) {
  const task = tasks.find((t) => String(t.id) === taskId) ?? null;
  const queryClient = useQueryClient();
  // Attribution and proof from the overview's task node (task_graph_payload `audit`); readiness from
  // the work graph's task node (work_graph._ready_info) when the drawer was opened from a goal.
  const overview = useTaskGraphQuery(Boolean(taskId)).data?.nodes.find((n) => n.id === `task:${taskId}`);
  const workNode = (/^\d+$/.test(goal) ? queryClient.getQueryData<WorkGraphPayload>(WORK_KEYS.workGraph(goal)) : undefined)?.nodes.find(
    (n) => n.id === `task:${taskId}`,
  );
  const audit = overview?.audit;
  const evidence = audit?.evidence_ref ?? (typeof overview?.evidence_ref === "string" ? overview.evidence_ref : null) ?? task?.evidenceRef ?? null;
  const ready = workNode?.ready;
  return (
    <Drawer
      open={Boolean(taskId)}
      onClose={onClose}
      title={task ? `#${task.id} ${task.title}` : `Task ${taskId}`}
      description="A summary of the selected task."
      footer={
        <LinkButton href={`/work/tasks/${taskId}`} variant="primary">
          Open task
        </LinkButton>
      }
    >
      {task ? (
        <div className="flex flex-col gap-4">
          <DescriptionList
            items={[
              { term: "Status", value: statusLabel(task.status) },
              { term: "Priority", value: priorityLabel(task.priority) },
              { term: "Owner", value: task.owner ?? "Unclaimed" },
              { term: "Project", value: task.project },
              { term: "Queue", value: queueLabel(task.lane) },
              { term: "Waiting on other tasks", value: task.depCount > 0 ? `${task.depCount}` : "None" },
              { term: "Blocked because", value: task.blockedReason },
              {
                term: "Ready to work on",
                value: ready ? (
                  <span>
                    {`${ready.state[0].toUpperCase()}${ready.state.slice(1).replace(/_/g, " ")}: ${ready.reason}`}
                    {ready.unmet_dependency_ids.length > 0 && (
                      <>
                        {" (waits on "}
                        {ready.unmet_dependency_ids.map((d, i) => (
                          <span key={d}>
                            {i > 0 && ", "}
                            <UiLink href={`/work/tasks/${d}`} className="text-[var(--ua-link)] underline">
                              #{d}
                            </UiLink>
                          </span>
                        ))}
                        {")"}
                      </>
                    )}
                  </span>
                ) : null,
              },
              { term: "Who did it", value: audit?.chain ?? null },
              {
                term: "Proof",
                value: evidence ? (
                  evidenceUrl(evidence) ? (
                    <UiLink href={evidenceUrl(evidence)!} className="break-all text-[var(--ua-link)] underline">
                      {evidence}
                    </UiLink>
                  ) : (
                    evidence
                  )
                ) : audit ? (
                  "No evidence recorded yet"
                ) : null,
              },
              { term: "Approval", value: audit?.approval_status && audit.approval_status !== "none" ? audit.approval_status : null },
            ]}
          />
          <UiLink href={`/work/tasks/${taskId}#task-activity`} className="text-sm text-[var(--ua-link)] underline">
            Runs, evidence and comments
          </UiLink>
        </div>
      ) : (
        <p className="m-0 text-[var(--ua-muted)]">This task is not in the current list. Open it to see its details.</p>
      )}
    </Drawer>
  );
}
