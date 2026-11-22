import { useId, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchProjects, PROJECTS_QUERY_KEY } from "../../lib/api/overview";
import { fetchQueueHealth, QUEUE_HEALTH_QUERY_KEY, type QueueHealthWithPressure } from "../../lib/api/queue";
import { AGENTS_QUERY_KEY, fetchAgents, fetchSessions, sessionsQueryKey } from "../../lib/api/sessions";
import { backoffRefetchInterval, useStreamInvalidation } from "../../lib/polling";
import type { AgentRow, SessionRow } from "../../lib/sessions/types";
import {
  Button,
  Drawer,
  EmptyState,
  ErrorState,
  Field,
  FreshnessStamp,
  LinkButton,
  PageHeader,
  Select,
  Skeleton,
  StatusIndicator,
  UiLink,
  deriveTruthState,
  type QueryLike,
  type TruthResult,
} from "../../lib/ui";
import { describeReadError, UNKNOWN_COUNT } from "../lib/queries";
import { useUrlParam } from "../lib/urlState";
import { ItemDetail } from "../inbox/ItemDetail";
import { itemHref } from "../inbox/InboxPage";
import { ago, elapsed, KIND_LABEL, taskIdOf, type InboxItem } from "../inbox/model";
import { UNLISTABLE_NOTE, useInbox } from "../inbox/useInbox";
import {
  DIGEST_QUERY_KEY,
  fetchDigest,
  fetchMemoryProbe,
  fetchRecentRuns,
  isActiveRun,
  MEMORY_PROBE_QUERY_KEY,
  RUNS_QUERY_KEY,
  type RunRow,
} from "./api";

const DAY_MS = 24 * 60 * 60 * 1000;
/** `GET /api/sessions` returns at most this many rows by default (`_int_param(limit, 100)`): a full page may hide more. */
export const SESSIONS_PAGE = 100;
const SESSIONS_CAPPED_NOTE = `The hub returned ${SESSIONS_PAGE} sessions, the most it returns at once, so some may be missing.`;
const RUNS_INCOMPLETE_NOTE = "Not every run could be read: the hub has more than 500 recent runs, so some may be missing.";

/** A region whose read succeeded but may be missing rows is partial: no exact count, no "nothing here". */
function incompleteAs(base: TruthResult, incomplete: boolean): TruthResult {
  return incomplete && (base.state === "ready" || base.state === "empty") ? { ...base, state: "partial" } : base;
}
const POLL = backoffRefetchInterval(30_000);
const calm = { refetchInterval: POLL, refetchIntervalInBackground: false, retry: 1 } as const;

const taskHref = (id: number) => `/work/tasks/${id}`;
const agentHref = (label: string) => `/agents/profile/${encodeURIComponent(label)}`;

/** All active tasks the queue knows, for project scoping. */
function queueTasks(data: QueueHealthWithPressure | undefined) {
  if (!data) return [];
  return [
    ...data.desktop_open,
    ...data.dispatch_ready,
    ...data.interactive_only,
    ...data.operator_only,
    ...data.doing,
    ...data.waiting_review,
    ...data.stale_claims,
    ...data.laptop_lane,
    ...data.blocked_archived,
    ...data.inconsistent,
    ...data.other,
  ];
}

/**
 * Scoping by project. Items not tied to a task (approvals, replies,
 * proposals, sessions without a task) show under every project. A task whose
 * project cannot be read is hidden under a project filter and counted, so
 * the page can say how many it hid.
 */
type Scope = { project: string; projectOf: (taskId: number) => string | undefined };

function scopeFilter<T>(scope: Scope, rows: readonly T[], taskOf: (row: T) => number | null, projectHint?: (row: T) => string | null | undefined) {
  if (!scope.project) return { rows: [...rows], hidden: 0 };
  let hidden = 0;
  const kept = rows.filter((row) => {
    const hint = projectHint?.(row);
    if (hint) return (hint || "unified-agent") === scope.project;
    const id = taskOf(row);
    if (id === null) return true;
    const project = scope.projectOf(id);
    if (project === undefined) {
      hidden += 1;
      return false;
    }
    return project === scope.project;
  });
  return { rows: kept, hidden };
}

/** A Home region: its heading, its count (from a complete read only) and exactly one data state. */
function HomeRegion({
  title,
  description,
  truth,
  count,
  loadingLabel,
  empty,
  onRetry,
  errorTitle,
  action,
  hiddenNote,
  children,
}: {
  title: string;
  description: string;
  truth: TruthResult;
  count: number | string | null;
  loadingLabel: string;
  empty: { title: string; description?: string; action?: ReactNode };
  onRetry: () => void;
  errorTitle: string;
  action?: ReactNode;
  hiddenNote?: string | null;
  children: () => ReactNode;
}) {
  const headingId = useId();
  const complete = truth.state === "ready" || truth.state === "empty";
  const countText = complete && count !== null ? String(count) : UNKNOWN_COUNT;
  let body: ReactNode;
  if (truth.state === "loading") body = <Skeleton label={loadingLabel} />;
  else if (truth.state === "denied" || truth.state === "unavailable") {
    const { message, detail } = describeReadError(truth.error);
    body = <ErrorState headingLevel={3} title={errorTitle} message={message} detail={detail} onRetry={onRetry} />;
  } else if (truth.state === "empty") {
    body = <EmptyState headingLevel={3} title={empty.title} description={empty.description} action={empty.action} />;
  } else {
    body = (
      <>
        {(truth.state === "stale" || truth.state === "partial") && (
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
            <FreshnessStamp state={truth.state} updatedAt={truth.updatedAt} />
            {truth.state === "partial" && (
              <>
                <span>Part of this could not be read, so it may be incomplete.</span>
                <Button size="sm" variant="ghost" onClick={onRetry}>
                  Retry
                </Button>
              </>
            )}
          </div>
        )}
        {children()}
      </>
    );
  }
  return (
    <section aria-labelledby={headingId} className="flex min-w-0 flex-col gap-3 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <h2 id={headingId} className="m-0 flex items-center gap-2 text-base font-semibold">
            {title}
            <span className="rounded-full bg-[var(--ua-hover)] px-2 font-mono text-xs">
              <span aria-hidden="true">{countText}</span>
              <span className="sr-only">{complete && count !== null ? `, ${countText} items` : ", count not known yet"}</span>
            </span>
          </h2>
          <p className="m-0 mt-0.5 text-sm text-[var(--ua-muted)]">{description}</p>
        </div>
        {action}
      </div>
      {hiddenNote && <p className="m-0 text-xs text-[var(--ua-muted)]">{hiddenNote}</p>}
      {body}
    </section>
  );
}

function hiddenText(hidden: number): string | null {
  return hidden > 0 ? `${hidden} item${hidden === 1 ? " is" : "s are"} hidden because ${hidden === 1 ? "its" : "their"} project could not be read.` : null;
}

function NeedsYou({ scope }: { scope: Scope }) {
  const inbox = useInbox();
  const [open, setOpen] = useState<InboxItem | null>(null);
  const queries: QueryLike[] = inbox.sources.map((s) => s.query);
  const waiting = inbox.items.filter((i) => i.waiting);
  const scoped = scopeFilter(scope, waiting, taskIdOf);
  const anyData = queries.some((q) => q.data !== undefined);
  const truth: TruthResult = inbox.loading
    ? { state: "loading", failed: 0, updatedAt: null, error: null }
    : !anyData
      ? deriveTruthState(queries)
      : {
          state: inbox.countKnown("all") ? (scoped.rows.length === 0 ? "empty" : "ready") : "partial",
          failed: inbox.failed.length,
          updatedAt: null,
          error: inbox.failed[0]?.error ?? null,
        };
  const top = scoped.rows.slice(0, 5);
  return (
    <>
      <HomeRegion
        title="Needs you"
        description="The first five things waiting on a person, with the action one click away."
        truth={truth}
        count={
          inbox.countKnown("all")
            ? scope.project
              ? `${scoped.rows.length}${inbox.capped("all") || inbox.unlistable("all") ? "+" : ""}`
              : inbox.countText("all")
            : null
        }
        loadingLabel="Loading what needs you"
        errorTitle="What needs you could not be loaded"
        onRetry={inbox.refetchAll}
        empty={{
          title: "Nothing is waiting on you that this hub can list",
          description: `Approvals, questions from agents, holds and proposals show here when they arrive. ${UNLISTABLE_NOTE}`,
        }}
        hiddenNote={hiddenText(scoped.hidden)}
        action={<LinkButton size="sm" href="/inbox">See all</LinkButton>}
      >
        {() =>
          top.length === 0 ? (
            <p className="m-0 text-sm text-[var(--ua-muted)]">Nothing to show from the parts that could be read.</p>
          ) : (
            <ul aria-label="Waiting on you" className="m-0 flex list-none flex-col gap-2 p-0">
              {top.map((item) => (
                <li key={item.key} className="flex flex-wrap items-center gap-2 rounded border border-[var(--ua-border)] px-3 py-2">
                  <div className="min-w-0 flex-1">
                    <p className="m-0 text-xs text-[var(--ua-muted)]">
                      {KIND_LABEL[item.kind]}
                      {item.since ? ` · ${ago(item.since)}` : ""}
                    </p>
                    <UiLink className="break-words text-sm font-medium text-[var(--ua-fg)] underline-offset-2 hover:underline" href={itemHref(item.tab, item.key)}>
                      {item.title}
                    </UiLink>
                  </div>
                  <Button size="sm" variant="primary" onClick={() => setOpen(item)} aria-label={`Act on: ${item.title}`}>
                    Act
                  </Button>
                </li>
              ))}
            </ul>
          )
        }
      </HomeRegion>
      <Drawer open={open !== null} onClose={() => setOpen(null)} title={open ? KIND_LABEL[open.kind] : ""} description="Decide here without leaving Home.">
        {open && <ItemDetail itemKey={open.key} inbox={inbox} />}
      </Drawer>
    </>
  );
}

type RunningRow = { key: string; agent: string | null; taskId: number | null; what: string; startedAt: string | null; kind: string; sessionId?: string };

function RunningNow({ scope, sessions, runs }: { scope: Scope; sessions: ReturnType<typeof useSessionsOpen>; runs: ReturnType<typeof useRuns> }) {
  const rows: RunningRow[] = [
    ...(sessions.data?.sessions ?? [])
      .filter((s: SessionRow) => ["open", "starting", "running", "waiting"].includes(s.status))
      .map((s) => ({
        key: `s:${s.id}`,
        agent: s.agent,
        taskId: s.task_id,
        what: s.task_title ?? (s.task_id ? `Task ${s.task_id}` : `${s.kind || "Session"} without a task`),
        startedAt: s.started_at,
        kind: "Session",
        sessionId: s.id,
      })),
    ...(runs.data?.runs ?? []).filter(isActiveRun).map((r: RunRow) => ({
      key: `r:${r.run_id}`,
      agent: r.agent,
      taskId: r.task_id,
      what: r.task_id ? `Task ${r.task_id}` : "Run without a task",
      startedAt: r.started_at,
      kind: r.kind === "selfpickup" ? "Picked up by the agent" : "Sent to the agent",
    })),
  ];
  const scoped = scopeFilter(scope, rows, (r) => r.taskId);
  // Runs read only partly: an older active run may be missing, so neither "nothing is running" nor an exact count.
  const runsIncomplete = Boolean(runs.data?.truncated);
  const sessionsCapped = (sessions.data?.sessions.length ?? 0) >= SESSIONS_PAGE;
  const truth = incompleteAs(deriveTruthState([sessions, runs], { isEmpty: () => scoped.rows.length === 0 }), runsIncomplete || sessionsCapped);
  return (
    <HomeRegion
      title="Running now"
      description="Open sessions and active runs: who, on what, and for how long."
      truth={truth}
      count={scoped.rows.length}
      loadingLabel="Loading what is running"
      errorTitle="What is running could not be loaded"
      onRetry={() => {
        void sessions.refetch();
        void runs.refetch();
      }}
      empty={{ title: "Nothing is running", description: "Start a session to put an agent to work.", action: <LinkButton size="sm" href="/sessions/new">Start a session</LinkButton> }}
      hiddenNote={[hiddenText(scoped.hidden), runsIncomplete ? RUNS_INCOMPLETE_NOTE : null, sessionsCapped ? SESSIONS_CAPPED_NOTE : null].filter(Boolean).join(" ") || null}
      action={<LinkButton size="sm" href="/sessions">All sessions</LinkButton>}
    >
      {() => (
        <ul aria-label="Running now" className="m-0 flex list-none flex-col gap-2 p-0">
          {scoped.rows.map((r) => (
            <li key={r.key} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 rounded border border-[var(--ua-border)] px-3 py-2 text-sm">
              {r.agent ? <UiLink className="font-medium underline" href={agentHref(r.agent)}>{r.agent}</UiLink> : <span className="font-medium">Unknown agent</span>}
              {r.taskId ? <UiLink className="min-w-0 break-words underline" href={taskHref(r.taskId)}>{r.what}</UiLink> : r.sessionId ? <UiLink className="min-w-0 break-words underline" href={`/sessions/${encodeURIComponent(r.sessionId)}`}>{r.what}</UiLink> : <span>{r.what}</span>}
              <span className="text-xs text-[var(--ua-muted)]">
                {r.kind}
                {r.startedAt ? ` · for ${elapsed(r.startedAt)}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </HomeRegion>
  );
}

type FinishedRow = { key: string; title: string; href: string | null; taskId: number | null; project?: string | null; what: string; when: string | null; who: string | null };

/**
 * Just finished: everything with an end time in one rolling 24-hour window
 * (task completions, ended sessions, ended dispatch runs). The hub reports
 * task completions only since midnight UTC (`/api/digest`), so earlier ones
 * in the window are not available, and the page says so. Work waiting for
 * review has no "entered review" time on the hub, so it is listed as "In
 * review" without claiming it is recent.
 */
function JustFinished({
  scope,
  queue,
  sessionsAll,
  runs,
}: {
  scope: Scope;
  queue: ReturnType<typeof useQueue>;
  sessionsAll: ReturnType<typeof useSessionsAll>;
  runs: ReturnType<typeof useRuns>;
}) {
  const digest = useQuery({ queryKey: DIGEST_QUERY_KEY, queryFn: ({ signal }) => fetchDigest(signal), ...calm });
  const now = Date.now();
  const inWindow = (iso: string | null | undefined) => Boolean(iso) && now - Date.parse(iso as string) < DAY_MS;
  const finished: FinishedRow[] = [
    ...(digest.data?.changed_today?.tasks_completed ?? [])
      .filter((t) => inWindow(t.completed_at))
      .map((t) => ({
        key: `done:${t.id}`,
        title: t.title || `Task ${t.id}`,
        href: taskHref(t.id),
        taskId: t.id,
        project: t.project,
        what: "Done",
        when: t.completed_at ?? null,
        who: t.claimed_by ?? null,
      })),
    ...(sessionsAll.data?.sessions ?? [])
      .filter((s) => inWindow(s.ended_at))
      .map((s) => ({
        key: `ended:${s.id}`,
        title: s.task_title ?? (s.task_id ? `Task ${s.task_id}` : `${s.kind || "Session"} without a task`),
        href: `/sessions/${encodeURIComponent(s.id)}`,
        taskId: s.task_id,
        what: s.exit_status ? `Session ended (${s.exit_status})` : "Session ended",
        when: s.ended_at,
        who: s.agent,
      })),
    ...(runs.data?.runs ?? [])
      .filter((r) => r.kind !== "session" && inWindow(r.ended_at))
      .map((r) => ({
        key: `run:${r.run_id}`,
        title: r.task_id ? `Task ${r.task_id}` : "Run without a task",
        href: r.task_id ? taskHref(r.task_id) : null,
        taskId: r.task_id,
        what: `Run ${r.status === "succeeded" || r.status === "completed" ? "finished" : `ended (${r.status})`}`,
        when: r.ended_at,
        who: r.agent,
      })),
  ].sort((a, b) => Date.parse(b.when ?? "") - Date.parse(a.when ?? ""));
  const inReview: FinishedRow[] = (queue.data?.waiting_review ?? []).map((t) => ({
    key: `review:${t.id}`,
    title: t.title || `Task ${t.id}`,
    href: taskHref(t.id),
    taskId: t.id,
    project: t.project,
    what: "In review",
    when: null,
    who: t.claimed_by ?? null,
  }));
  const hint = (r: FinishedRow) => (r.project === undefined ? undefined : r.project ?? "unified-agent");
  const scoped = scopeFilter(scope, finished, (r) => r.taskId, hint);
  const scopedReview = scopeFilter(scope, inReview, (r) => r.taskId, hint);
  const runsIncomplete = Boolean(runs.data?.truncated);
  const sessionsCapped = (sessionsAll.data?.sessions.length ?? 0) >= SESSIONS_PAGE;
  const truth = incompleteAs(
    deriveTruthState([digest, queue, sessionsAll, runs], { isEmpty: () => scoped.rows.length === 0 && scopedReview.rows.length === 0 }),
    runsIncomplete || sessionsCapped,
  );
  const midnight = new Date();
  midnight.setUTCHours(0, 0, 0, 0);
  const gapNote =
    now - midnight.getTime() < DAY_MS
      ? `Tasks completed before midnight UTC (${new Date(midnight).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} here) are not shown: this hub reports completions since then only.`
      : null;
  const row = (r: FinishedRow) => (
    <li key={r.key} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 rounded border border-[var(--ua-border)] px-3 py-2 text-sm">
      {r.href ? <UiLink className="min-w-0 break-words font-medium underline" href={r.href}>{r.title}</UiLink> : <span className="font-medium">{r.title}</span>}
      <span className="text-xs text-[var(--ua-muted)]">
        {r.what}
        {r.who ? ` · ${r.who}` : ""}
        {r.when ? ` · ${ago(r.when)}` : ""}
      </span>
    </li>
  );
  return (
    <HomeRegion
      title="Just finished"
      description="Tasks, sessions and runs that ended in the last 24 hours, and work now in review."
      truth={truth}
      count={scoped.rows.length}
      loadingLabel="Loading what just finished"
      errorTitle="What just finished could not be loaded"
      onRetry={() => {
        void digest.refetch();
        void queue.refetch();
        void sessionsAll.refetch();
        void runs.refetch();
      }}
      empty={{ title: "Nothing finished in the last 24 hours", description: "Closed tasks, ended sessions and finished runs show here." }}
      hiddenNote={
        [hiddenText(scoped.hidden + scopedReview.hidden), gapNote, runsIncomplete ? RUNS_INCOMPLETE_NOTE : null, sessionsCapped ? SESSIONS_CAPPED_NOTE : null]
          .filter(Boolean)
          .join(" ") || null
      }
      action={<LinkButton size="sm" href="/work">Open Work</LinkButton>}
    >
      {() => (
        <>
          {scoped.rows.length > 0 ? (
            <ul aria-label="Just finished" className="m-0 flex list-none flex-col gap-2 p-0">
              {scoped.rows.map(row)}
            </ul>
          ) : (
            <p className="m-0 text-sm text-[var(--ua-muted)]">Nothing ended in the last 24 hours.</p>
          )}
          {scopedReview.rows.length > 0 && (
            <>
              <h3 className="m-0 mt-2 text-sm font-semibold">In review</h3>
              <ul aria-label="In review" className="m-0 flex list-none flex-col gap-2 p-0">
                {scopedReview.rows.map(row)}
              </ul>
            </>
          )}
        </>
      )}
    </HomeRegion>
  );
}

const AGENT_STATE: Record<string, { status: string; label: string }> = {
  online: { status: "online", label: "Working" },
  idle: { status: "idle", label: "Idle" },
  stale: { status: "warning", label: "Not heard from lately" },
  offline: { status: "offline", label: "Offline" },
};

function FleetStrip({ agents }: { agents: ReturnType<typeof useAgentsQuery> }) {
  const headingId = useId();
  const rows: AgentRow[] = agents.data?.agents ?? [];
  const truth = deriveTruthState([agents], { isEmpty: () => rows.length === 0 });
  let body: ReactNode;
  if (truth.state === "loading") body = <Skeleton label="Loading agents" lines={2} />;
  else if (truth.state === "denied" || truth.state === "unavailable") {
    const { message, detail } = describeReadError(truth.error);
    body = <ErrorState headingLevel={3} title="Agents could not be loaded" message={message} detail={detail} onRetry={() => void agents.refetch()} />;
  } else if (truth.state === "empty") {
    body = <EmptyState headingLevel={3} title="No agents yet" description="Connect an agent to start." action={<LinkButton size="sm" href="/agents/add">Add an agent</LinkButton>} />;
  } else {
    body = (
      <>
        {truth.state === "stale" && <FreshnessStamp state={truth.state} updatedAt={truth.updatedAt} className="text-xs" />}
        <ul aria-label="Agents and their state" className="m-0 flex list-none flex-col gap-1 p-0">
          {rows.map((a) => {
            const state = AGENT_STATE[a.status] ?? { status: "neutral", label: "State unknown" };
            return (
              <li key={a.label} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 border-b border-[var(--ua-border)] py-1.5 text-sm last:border-b-0">
                <UiLink className="min-w-[10rem] font-medium underline" href={agentHref(a.label)}>
                  {a.display_name || a.persona?.display_name || a.label}
                </UiLink>
                <StatusIndicator status={state.status} label={state.label} size="sm" />
                <span className="text-xs text-[var(--ua-muted)]">
                  {a.active_tasks > 0 ? `${a.active_tasks} active task${a.active_tasks === 1 ? "" : "s"}` : "No active task"}
                  {a.open_sessions > 0 ? ` · ${a.open_sessions} open session${a.open_sessions === 1 ? "" : "s"}` : ""}
                  {a.last_seen ? ` · seen ${ago(a.last_seen)}` : ""}
                  {a.host ? ` · ${a.host}` : ""}
                </span>
              </li>
            );
          })}
        </ul>
      </>
    );
  }
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-2 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id={headingId} className="m-0 flex-1 text-base font-semibold">
          Fleet
        </h2>
        <LinkButton size="sm" href="/agents">All agents</LinkButton>
      </div>
      {body}
    </section>
  );
}

type Step = { id: string; label: string; done: boolean | null; href: string; action: string; unknownWhy?: string };

/**
 * First-run checklist (spec §4): shown until each step is done, from hub
 * reads only. A read that failed, or answered 200 with an `error`, makes its
 * step unknown, never "not done". The hub has no read that lists finished
 * tasks, so "Create a task" is done only on evidence that a task exists
 * (an active task, a completion today, a session or run on a task) and is
 * otherwise unknown: an organization with only finished tasks is not told
 * to create its first.
 */
function FirstRun({
  agents,
  sessionsAll,
  queue,
  runs,
}: {
  agents: ReturnType<typeof useAgentsQuery>;
  sessionsAll: ReturnType<typeof useSessionsAll>;
  queue: ReturnType<typeof useQueue>;
  runs: ReturnType<typeof useRuns>;
}) {
  const memory = useQuery({ queryKey: MEMORY_PROBE_QUERY_KEY, queryFn: ({ signal }) => fetchMemoryProbe(signal), retry: 1, staleTime: 60_000 });
  const digest = useQuery({ queryKey: DIGEST_QUERY_KEY, queryFn: ({ signal }) => fetchDigest(signal), ...calm });
  const ok = (q: { status: string; data?: unknown }) =>
    q.status === "success" && !(q.data && typeof q.data === "object" && "error" in q.data && (q.data as { error?: unknown }).error);
  const known = (q: { status: string; data?: unknown }, done: () => boolean) => (ok(q) ? done() : null);
  const taskEvidence =
    (ok(queue) && (queue.data?.counts.total_active ?? 0) > 0) ||
    (ok(digest) && (digest.data?.changed_today?.tasks_completed?.length ?? 0) > 0) ||
    (ok(sessionsAll) && (sessionsAll.data?.sessions ?? []).some((x) => x.task_id !== null)) ||
    (ok(runs) && (runs.data?.runs ?? []).some((r) => r.task_id !== null));
  const steps: Step[] = [
    { id: "agent", label: "Connect an agent", done: known(agents, () => (agents.data?.agents.length ?? 0) > 0), href: "/agents/add", action: "Add an agent" },
    { id: "session", label: "Start a session", done: known(sessionsAll, () => (sessionsAll.data?.total ?? 0) > 0 || (sessionsAll.data?.sessions.length ?? 0) > 0), href: "/sessions/new", action: "Start a session" },
    {
      id: "task",
      label: "Create a task",
      done: taskEvidence ? true : null,
      href: "/work/tasks/new",
      action: "Create a task",
      unknownWhy: "This hub cannot list finished tasks yet, so it cannot tell whether any exist.",
    },
    { id: "note", label: "Save a memory note", done: known(memory, () => (memory.data?.total ?? 0) > 0), href: "/knowledge/memory?new=1", action: "Write a note" },
  ];
  const headingId = useId();
  if (steps.every((s) => s.done === true)) return null;
  if (!steps.some((s) => s.done === false)) return null;
  const doneCount = steps.filter((s) => s.done === true).length;
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-2 rounded-lg border border-[var(--ua-accent)] p-4">
      <h2 id={headingId} className="m-0 text-base font-semibold">
        Get started
      </h2>
      <p className="m-0 text-sm text-[var(--ua-muted)]">
        {doneCount} of {steps.length} done. This list goes away once each step is done.
      </p>
      <ol className="m-0 flex list-none flex-col gap-2 p-0">
        {steps.map((s) => (
          <li key={s.id} className="flex flex-wrap items-center gap-3 text-sm">
            <span aria-hidden="true" className="w-4 text-center">{s.done ? "✓" : s.done === null ? "?" : "○"}</span>
            <span className={s.done ? "text-[var(--ua-muted)] line-through" : ""}>{s.label}</span>
            <span className="sr-only">{s.done ? "done" : s.done === null ? "could not check" : "not done yet"}</span>
            {s.done === null && <span className="text-xs text-[var(--ua-muted)]">Could not check{s.unknownWhy ? `. ${s.unknownWhy}` : ""}</span>}
            {s.done !== true && (
              <LinkButton size="sm" href={s.href}>
                {s.action}
              </LinkButton>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

function useSessionsOpen() {
  return useQuery({ queryKey: sessionsQueryKey({ status: "open" }), queryFn: ({ signal }) => fetchSessions({ status: "open" }, signal), ...calm });
}
function useSessionsAll() {
  return useQuery({ queryKey: sessionsQueryKey({}), queryFn: ({ signal }) => fetchSessions({}, signal), ...calm });
}
function useRuns() {
  return useQuery({ queryKey: RUNS_QUERY_KEY, queryFn: ({ signal }) => fetchRecentRuns(signal), ...calm });
}
function useQueue() {
  return useQuery({ queryKey: QUEUE_HEALTH_QUERY_KEY, queryFn: ({ signal }) => fetchQueueHealth(signal), ...calm });
}
function useAgentsQuery() {
  return useQuery({ queryKey: AGENTS_QUERY_KEY, queryFn: ({ signal }) => fetchAgents(signal), ...calm });
}

/** Home (spec §9.1): what needs you, what is running, what just finished, scoped by project. */
export function HomePage() {
  const [project, setProject] = useUrlParam("project");
  const projects = useQuery({ queryKey: PROJECTS_QUERY_KEY, queryFn: ({ signal }) => fetchProjects(signal), ...calm });
  const queue = useQueue();
  const sessionsOpen = useSessionsOpen();
  const sessionsAll = useSessionsAll();
  const runs = useRuns();
  const agents = useAgentsQuery();

  useStreamInvalidation(
    () => {
      void sessionsOpen.refetch();
      void runs.refetch();
      void queue.refetch();
    },
    { channels: ["agent.session.*", "agent.task.*", "agent.dispatch.*"] },
  );

  const projectMap = useMemo(() => {
    const map = new Map<number, string>();
    for (const t of queueTasks(queue.data)) map.set(t.id, t.project || "unified-agent");
    return map;
  }, [queue.data]);
  const scope: Scope = { project, projectOf: (id) => projectMap.get(id) };

  const options = (projects.data?.projects ?? []).map((p) => ({ value: p.project, label: p.label || p.project }));
  if (project && !options.some((o) => o.value === project)) options.push({ value: project, label: project });

  return (
    <>
      <PageHeader
        title="Home"
        description="What needs you, what is running now and what just finished."
        actions={
          <Field
            label="Project"
            hint={projects.status === "error" ? "The project list could not be loaded." : undefined}
            className="min-w-48"
          >
            <Select value={project} placeholder="All projects" options={options} onChange={(e) => setProject(e.target.value || null, { push: true })} />
          </Field>
        }
      />
      <div className="flex flex-col gap-5">
        <FirstRun agents={agents} sessionsAll={sessionsAll} queue={queue} runs={runs} />
        <NeedsYou scope={scope} />
        <div className="grid gap-5 xl:grid-cols-2">
          <RunningNow scope={scope} sessions={sessionsOpen} runs={runs} />
          <JustFinished scope={scope} queue={queue} sessionsAll={sessionsAll} runs={runs} />
        </div>
        <FleetStrip agents={agents} />
      </div>
    </>
  );
}
