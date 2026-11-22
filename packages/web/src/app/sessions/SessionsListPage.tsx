import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AGENTS_QUERY_KEY, fetchAgents } from "../../lib/api/sessions";
import { channelShortLabel, summarizePayload, truncate, type ActivityEvent, type SessionRow } from "../../lib/sessions/types";
import { useStreamInvalidation } from "../../lib/polling";
import { DataTable, FilterBar, LinkButton, PageHeader, StatePill, Tabs, type Column } from "../../lib/ui";
import { countText, QueryRegion } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import { fetchSessionList, sessionQueryKeys } from "./api";
import { DevicesPanel } from "./DevicesPanel";
import { DeviceWorkPanel } from "./DeviceWorkPanel";
import { useLiveEvents } from "./live";
import { filterSessions, formatWhen, hostOf, isOpen, lastActivity, orderSessions, parseStatusFilter, sessionState } from "./model";
import { LiveIndicator, SectionHeading } from "./parts";
import { TeamsPanel } from "./TeamsPanel";

const DEFAULTS = { status: "all", agent: "", host: "", q: "", tab: "sessions" };

const COLUMNS: Column<SessionRow>[] = [
  {
    id: "session",
    header: "Session",
    rowLink: true,
    cell: (s) => (
      <span className="flex flex-col">
        <span className="font-medium">{s.agent}</span>
        <span className="font-mono text-xs text-[var(--ua-muted)]">{s.id}</span>
      </span>
    ),
  },
  {
    id: "task",
    header: "Task",
    cell: (s) =>
      s.task_id != null ? (
        <span className="line-clamp-2 max-w-[18rem]">
          #{s.task_id}
          {s.task_title ? ` ${s.task_title}` : ""}
        </span>
      ) : (
        <span className="text-[var(--ua-muted)]">No task</span>
      ),
  },
  { id: "host", header: "Host", cell: (s) => hostOf(s) ?? <span className="text-[var(--ua-muted)]">Not reported</span> },
  { id: "started", header: "Started", cell: (s) => formatWhen(s.started_at) },
  { id: "activity", header: "Last activity", cell: (s) => formatWhen(lastActivity(s)) },
  {
    id: "state",
    header: "State",
    cell: (s) => {
      const state = sessionState(s.status);
      return <StatePill kind={state.kind} label={state.label} />;
    },
  },
];

const STATUS_OPTIONS = [
  { value: "open", label: "Open" },
  { value: "ended", label: "Ended" },
];

/** `/sessions`: open sessions first, filtered from the URL (spec §9.4 List). */
export function SessionsListPage() {
  const { values, update, clear } = useUrlParams(DEFAULTS);
  const status = parseStatusFilter(values.status);
  const queryClient = useQueryClient();
  // The hub filters by agent and "open"; "ended" needs every recent row.
  const hubFilter = { agent: values.agent, status: status === "open" ? "open" : "all" };
  const sessions = useQuery({
    queryKey: sessionQueryKeys.list(hubFilter),
    queryFn: ({ signal }) => fetchSessionList(hubFilter, signal),
  });
  const agents = useQuery({ queryKey: AGENTS_QUERY_KEY, queryFn: ({ signal }) => fetchAgents(signal) });

  useStreamInvalidation(
    () => {
      void queryClient.invalidateQueries({ queryKey: sessionQueryKeys.all });
    },
    { channels: ["agent.session*", "agent.lifecycle", "agent.lifecycle.*", "agent.node", "agent.wake.*"], onReconnect: true },
  );

  const rows = useMemo(
    () => orderSessions(filterSessions(sessions.data?.sessions ?? [], { status, agent: values.agent, host: values.host, q: values.q })),
    [sessions.data, status, values.agent, values.host, values.q],
  );
  const agentOptions = useMemo(() => {
    const labels = new Set<string>();
    for (const a of agents.data?.agents ?? []) labels.add(a.label);
    for (const s of sessions.data?.sessions ?? []) labels.add(s.agent);
    if (values.agent) labels.add(values.agent);
    return [...labels].sort().map((label) => ({ value: label, label }));
  }, [agents.data, sessions.data, values.agent]);
  const hostOptions = useMemo(() => {
    const hosts = new Set<string>();
    for (const s of sessions.data?.sessions ?? []) {
      const host = hostOf(s);
      if (host) hosts.add(host);
    }
    if (values.host) hosts.add(values.host);
    return [...hosts].sort().map((h) => ({ value: h, label: h }));
  }, [sessions.data, values.host]);

  const tab = ["sessions", "work", "devices", "teams", "activity"].includes(values.tab) ? values.tab : "sessions";

  return (
    <>
      <PageHeader
        title="Sessions"
        description="Every agent session, live and past. Open one to read the conversation, send a follow-up or stop it."
        actions={
          <LinkButton href="/sessions/new" variant="primary">
            Start a session
          </LinkButton>
        }
      />
      <Tabs
        label="Sessions views"
        value={tab}
        onChange={(id) => update({ tab: id }, { push: true })}
        items={[
          {
            id: "sessions",
            label: "Sessions",
            panel:
              tab === "sessions" ? (
                <section aria-labelledby="sessions-list-heading">
                  <h2 id="sessions-list-heading" className="sr-only">
                    Session list
                  </h2>
                  <FilterBar
                    label="Session filters"
                    search={{
                      label: "Search sessions",
                      value: values.q,
                      onChange: (q) => update({ q }),
                      placeholder: "Search by agent, task, host or id",
                    }}
                    filters={[
                      { id: "status", label: "Status", value: status === "all" ? "" : status, options: STATUS_OPTIONS, anyLabel: "All", onChange: (v) => update({ status: v || null }) },
                      { id: "agent", label: "Agent", value: values.agent, options: agentOptions, anyLabel: "Any agent", onChange: (v) => update({ agent: v }) },
                      { id: "host", label: "Host", value: values.host, options: hostOptions, anyLabel: "Any host", onChange: (v) => update({ host: v }) },
                    ]}
                    onClearAll={clear}
                    className="mb-3"
                  />
                  <QueryRegion
                    query={sessions}
                    loadingLabel="Loading sessions"
                    errorTitle="Sessions could not be loaded"
                    isEmpty={(data) => data.sessions.length === 0}
                    empty={{
                      title: status === "open" ? "No open sessions" : "No sessions yet",
                      description: "A session appears here when an agent starts work: from a terminal, a device, or the Start a session page.",
                      action: (
                        <LinkButton href="/sessions/new" variant="primary">
                          Start a session
                        </LinkButton>
                      ),
                    }}
                  >
                    {(data, truth) => (
                      <>
                        <p className="m-0 mb-2 text-sm text-[var(--ua-muted)]" aria-live="polite">
                          {countText(truth, data.sessions.filter((s) => isOpen(s.status)).length)} open ·{" "}
                          {countText(truth, rows.length)} shown
                          {data.error ? ` · Some sources could not be read: ${data.error}` : ""}
                        </p>
                        <DataTable
                          columns={COLUMNS}
                          data={rows}
                          getRowKey={(s) => s.id}
                          getRowHref={(s) => `/sessions/${encodeURIComponent(s.id)}`}
                          caption="Agent sessions, open sessions first"
                          captionSrOnly
                          scrollLabel="Agent sessions"
                          emptyText="No session matches these filters. Clear the filters to see every session."
                        />
                      </>
                    )}
                  </QueryRegion>
                </section>
              ) : null,
          },
          { id: "work", label: "Device work", panel: tab === "work" ? <DeviceWorkPanel /> : null },
          { id: "devices", label: "On your devices", panel: tab === "devices" ? <DevicesPanel /> : null },
          { id: "teams", label: "Team snapshots", panel: tab === "teams" ? <TeamsPanel /> : null },
          { id: "activity", label: "Live activity", panel: tab === "activity" ? <ActivityPanel /> : null },
        ]}
      />
    </>
  );
}

const ACTIVITY_MAX = 100;

/** Every hub event as it happens (legacy Sessions "Live activity"). */
function ActivityPanel() {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const live = useLiveEvents([], (event) => setEvents((prev) => [event, ...prev].slice(0, ACTIVITY_MAX)));
  return (
    <section aria-labelledby="activity-heading">
      <SectionHeading id="activity-heading" actions={<LiveIndicator state={live} />}>
        Live activity
      </SectionHeading>
      <p className="m-0 mb-2 text-sm text-[var(--ua-muted)]">
        Everything the hub records, newest first, from the moment this page opened. The last {ACTIVITY_MAX} events are kept.
      </p>
      {events.length === 0 ? (
        <p className="m-0 rounded-md border border-[var(--ua-border)] px-3 py-4 text-sm text-[var(--ua-muted)]">
          Nothing has happened since this page opened. Events appear here as they arrive.
        </p>
      ) : (
        <ol aria-label="Live activity, newest first" tabIndex={0} className="m-0 max-h-[32rem] list-none overflow-y-auto rounded-md border border-[var(--ua-border)] p-0 text-sm">
          {events.map((event) => {
            const detail = summarizePayload(event.payload);
            return (
              <li key={event.ulid} className="flex flex-wrap items-baseline gap-x-2 border-t border-[var(--ua-border)] px-3 py-1.5 first:border-t-0">
                <span className="font-mono text-xs text-[var(--ua-muted)]">{event.created_at ? formatWhen(event.created_at) : "Time not reported"}</span>
                <span className="rounded border border-[var(--ua-border)] px-1 text-xs">{channelShortLabel(event.channel)}</span>
                <span className="text-xs text-[var(--ua-muted)]">{event.originator ?? "Unknown sender"}</span>
                <span className="min-w-0 grow break-words">{truncate(detail, 160)}</span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
