/**
 * `/agents`: every agent in the organization, local and cloud (spec §9.3
 * Roster). One row per label from the fleet heartbeat list, the operator's
 * roster (seat, kind, token metadata, never a bearer), lifecycle states and
 * open sessions. Filters (host kind, state, search) live in the URL. A read
 * that failed leaves its columns "unknown", never guessed; the operator-only
 * roster read says why it is missing for anyone else.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { DataTable, FilterBar, LinkButton, PageHeader, StatePill, type Column } from "../../lib/ui";
import { countText, QueryRegion, UNKNOWN_COUNT } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import { AGENTS_KEYS, fetchFleet, fetchLifecycle, fetchRoster, getJson, isOperatorOnly } from "./api";
import { filterRoster, formatWhen, mergeRoster, STATE_OPTIONS, STATE_WORDS, tokenText, type RosterEntry } from "./model";
import { AgentsTabs, Notice, useAgentsLiveUpdates } from "./shared";
import type { SessionRow } from "../../lib/sessions/types";

const DEFAULTS = { kind: "", state: "", q: "" };

const KIND_OPTIONS = [
  { value: "local", label: "Local" },
  { value: "cloud", label: "Cloud" },
];

const muted = (text: string) => <span className="text-[var(--ua-muted)]">{text}</span>;

const COLUMNS: Column<RosterEntry>[] = [
  {
    id: "agent",
    header: "Agent",
    rowLink: true,
    cell: (row) => (
      <span className="flex items-center gap-2">
        {row.avatar && (
          <span aria-hidden="true" className="text-base leading-none">
            {row.avatar}
          </span>
        )}
        <span className="flex flex-col">
          <span className="font-medium">{row.label}</span>
          {row.displayName && row.displayName !== row.label && <span className="text-xs text-[var(--ua-muted)]">{row.displayName}</span>}
        </span>
      </span>
    ),
  },
  {
    id: "state",
    header: "State",
    cell: (row) => <StatePill kind={STATE_WORDS[row.state].kind} label={STATE_WORDS[row.state].label} title={STATE_WORDS[row.state].meaning} />,
  },
  { id: "office", header: "Office seat", cell: (row) => row.office ?? muted(row.added === false ? "No seat" : "Not known") },
  { id: "kind", header: "Runs", cell: (row) => `${row.kind === "cloud" ? "Cloud" : "Local"}${row.kindKnown ? "" : " (from its label)"}${row.host ? ` · ${row.host}` : ""}` },
  {
    id: "task",
    header: "Current task",
    cell: (row) =>
      row.currentTask ? (
        <span className="line-clamp-2 max-w-[16rem]">
          #{row.currentTask.id}
          {row.currentTask.title ? ` ${row.currentTask.title}` : ""}
        </span>
      ) : row.activeTasks ? (
        `${row.activeTasks} claimed`
      ) : (
        muted("None")
      ),
  },
  { id: "seen", header: "Last seen", cell: (row) => formatWhen(row.lastSeen) },
  { id: "token", header: "Token", cell: (row) => <span className="text-xs">{tokenText(row.token)}</span> },
];

async function fetchOpenSessions(signal?: AbortSignal): Promise<SessionRow[]> {
  const body = await getJson<{ sessions?: unknown }>("/api/sessions?status=open&limit=200", "sessions", signal);
  return Array.isArray(body.sessions) ? (body.sessions as SessionRow[]) : [];
}

export function RosterPage() {
  useAgentsLiveUpdates();
  const { values, update, clear } = useUrlParams(DEFAULTS);
  const fleet = useQuery({ queryKey: AGENTS_KEYS.fleet, queryFn: ({ signal }) => fetchFleet(signal) });
  const roster = useQuery({ queryKey: AGENTS_KEYS.roster, queryFn: ({ signal }) => fetchRoster(signal), retry: false });
  const lifecycle = useQuery({ queryKey: AGENTS_KEYS.lifecycle, queryFn: ({ signal }) => fetchLifecycle(signal) });
  const sessions = useQuery({ queryKey: [...AGENTS_KEYS.all, "open-sessions"], queryFn: ({ signal }) => fetchOpenSessions(signal) });

  const rows = useMemo(
    () =>
      mergeRoster({
        fleet: fleet.data?.agents,
        roster: roster.data?.agents,
        lifecycle: lifecycle.data ? [...(lifecycle.data.registered ?? []), ...lifecycle.data.agents] : null,
        sessions: sessions.data,
      }),
    [fleet.data, roster.data, lifecycle.data, sessions.data],
  );
  const shown = useMemo(() => filterRoster(rows, values), [rows, values]);
  const operatorOnly = roster.isError && isOperatorOnly(roster.error);

  return (
    <>
      <PageHeader
        title="Agents"
        description="Every agent in the organization, local and cloud: what each is doing, where it runs and how it signs in."
        tabs={<AgentsTabs />}
        actions={
          <LinkButton href="/agents/add" variant="primary">
            Add an agent
          </LinkButton>
        }
      />
      <div className="flex flex-col gap-3">
        {operatorOnly && (
          <Notice role="note">
            Office seats, token details and agents that have never connected are shown to the human operator only. You see the agents the hub has heard from.
          </Notice>
        )}
        {roster.isError && !operatorOnly && (
          <Notice role="alert" tone="warn">
            The enrolment roster could not be read, so seats and token details are missing. The rest of the list is current.
          </Notice>
        )}
        <FilterBar
          label="Agent filters"
          search={{ label: "Search agents", value: values.q, onChange: (q) => update({ q }), placeholder: "Search by label, name, office or host" }}
          filters={[
            { id: "kind", label: "Runs", value: values.kind, options: KIND_OPTIONS, anyLabel: "Local or cloud", onChange: (v) => update({ kind: v }) },
            { id: "state", label: "State", value: values.state, options: STATE_OPTIONS, anyLabel: "Any state", onChange: (v) => update({ state: v }) },
          ]}
          onClearAll={clear}
        />
        <QueryRegion
          query={fleet}
          loadingLabel="Loading agents"
          errorTitle="Agents could not be loaded"
          isEmpty={(data) => data.agents.length === 0 && (roster.data?.agents.length ?? 0) === 0}
          empty={{
            title: "No agents yet",
            description: "An agent appears here once it is added or first connects to the hub.",
            action: (
              <LinkButton href="/agents/add" variant="primary">
                Add an agent
              </LinkButton>
            ),
          }}
        >
          {(data, truth) => (
            <>
              <p className="m-0 text-sm text-[var(--ua-muted)]" aria-live="polite">
                {countText(truth, rows.length)} agents · {countText(truth, rows.filter((r) => r.state === "working").length)} working ·{" "}
                {lifecycle.isSuccess ? countText(truth, rows.filter((r) => r.state === "retired").length) : UNKNOWN_COUNT} retired · {countText(truth, shown.length)} shown
                {data.error ? ` · Some counts may be incomplete: ${data.error}` : ""}
              </p>
              <DataTable
                columns={COLUMNS}
                data={shown}
                getRowKey={(row) => row.label}
                getRowHref={(row) => `/agents/profile/${encodeURIComponent(row.label)}`}
                caption="Agents in the organization"
                captionSrOnly
                scrollLabel="Agents"
                emptyText="No agent matches these filters. Clear the filters to see every agent."
              />
            </>
          )}
        </QueryRegion>
      </div>
    </>
  );
}
