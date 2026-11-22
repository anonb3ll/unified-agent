/**
 * Health (spec §9.7): the `system_doctor` report in plain words, grouped
 * "Needs attention", "Working" and "Off", each row saying what it is and
 * what to do. The raw state name and the setting behind it stay available
 * but do not lead. Retains the legacy Doctor pane: every check, its detail,
 * its gate, the summary counts, the report's age and an on-demand run.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, StatePill, type StateKind } from "../../lib/ui";
import { backoffRefetchInterval } from "../../lib/polling";
import type { DoctorCheck } from "../../lib/api/health";
import { UNKNOWN_COUNT } from "../lib/queries";
import { fetchHealthReport, SETTINGS_KEYS, type HealthPayload } from "./api";
import { agoText, describeSettingsError, exactTime, humanize, Notice, SettingsHeader, SettingsRegion, useNow } from "./shared";

/** The doctor runs every probe on the hub: poll once a minute, never on stream events (legacy #1850). */
export const HEALTH_POLL_MS = 60_000;

export type HealthGroup = "attention" | "working" | "off";

type StatusInfo = { group: HealthGroup; word: string; kind: StateKind; todo: (check: DoctorCheck) => string };

const settingText = (check: DoctorCheck) => (check.gate ? ` (${String(check.gate)})` : "");

/** Each raw doctor state, in plain words, with what to do about it. */
export const STATUS_INFO: Record<string, StatusInfo> = {
  MISCONFIGURED: {
    group: "attention",
    word: "Set up wrong",
    kind: "danger",
    todo: (c) => `It is turned on but its setup is wrong. Fix the setting${settingText(c)} on the hub as the detail says, then run the checks again.`,
  },
  DEGRADED: {
    group: "attention",
    word: "Partly working",
    kind: "warn",
    todo: () => "It runs, but not fully. Fix what the detail names, then run the checks again.",
  },
  UNKNOWN: {
    group: "attention",
    word: "Could not check",
    kind: "unknown",
    todo: () => "The check could not tell whether this works. Run the checks again; if it stays this way, look at the hub's log.",
  },
  LIVE: { group: "working", word: "Working", kind: "ok", todo: () => "Nothing to do." },
  DORMANT: {
    group: "off",
    word: "Off",
    kind: "unknown",
    todo: (c) => `It is not turned on. Leave it off unless you need it; to turn it on, set it up on the hub${settingText(c)}.`,
  },
};

export function statusOf(check: DoctorCheck): string {
  const raw = String(check.status || "UNKNOWN").toUpperCase();
  return raw in STATUS_INFO ? raw : "UNKNOWN";
}

/** Plain names for the checks people meet most; others are the system name in sentence case. */
const CHECK_NAMES: Record<string, string> = {
  hub_auth: "Sign-in and access tokens",
  operator_identity: "Operator identity",
  event_bus: "Live event stream",
  semantic_search: "Meaning-based memory search",
  symbol_embeddings: "Code symbol search",
  vault_sync: "Vault sync",
  fleet_vault_sync: "Vault sync on other machines",
  web_ui: "Control Center web app",
  llm_provider_chain: "Language model providers",
  dispatch_scheduler: "Session scheduler",
  approval_push: "Approval notifications",
  approval_slack_cards: "Approval cards in Slack",
  stale_worktrees: "Leftover task worktrees",
  required_settings: "Required hub settings",
  workflow_definitions: "Workflow definitions",
  fleet_nodes: "Registered machines",
  fleet_node_versions: "Software version on machines",
  fleet_node_acceptance: "Machine sign-up",
  queue_backpressure: "Work queue load",
  zulip_chat: "Zulip chat",
  zulip_relay: "Zulip relay",
  ts_worker: "TypeScript worker",
  pr_watch_job: "Pull request watcher",
  org_registry: "Organization roster",
  trust_presets: "Trust presets",
  agent_wake: "Waking agents",
};

export function checkName(check: DoctorCheck): string {
  return String(check.capability || check.check || "unknown");
}

export function plainName(check: DoctorCheck): string {
  const name = checkName(check);
  return CHECK_NAMES[name] ?? humanize(name);
}

export function groupChecks(checks: readonly DoctorCheck[]): Record<HealthGroup, DoctorCheck[]> {
  const groups: Record<HealthGroup, DoctorCheck[]> = { attention: [], working: [], off: [] };
  const order = ["MISCONFIGURED", "DEGRADED", "UNKNOWN"];
  for (const check of checks) groups[STATUS_INFO[statusOf(check)].group].push(check);
  groups.attention.sort((a, b) => order.indexOf(statusOf(a)) - order.indexOf(statusOf(b)));
  for (const list of Object.values(groups)) list.sort((a, b) => (statusOf(a) === statusOf(b) ? plainName(a).localeCompare(plainName(b)) : 0));
  return groups;
}

function healthOptions() {
  return {
    queryKey: SETTINGS_KEYS.health,
    queryFn: ({ signal }: { signal: AbortSignal }) => fetchHealthReport(false, signal),
    staleTime: 30_000,
    refetchInterval: backoffRefetchInterval(HEALTH_POLL_MS),
    refetchIntervalInBackground: false,
  } as const;
}

const GROUPS: { id: HealthGroup; title: string; description: string; emptyText: string }[] = [
  { id: "attention", title: "Needs attention", description: "Set up wrong, partly working, or the check could not tell.", emptyText: "Nothing needs attention." },
  { id: "working", title: "Working", description: "Turned on and answering.", emptyText: "No part reported as working." },
  { id: "off", title: "Off", description: "Not turned on on this hub. That is fine unless you need it.", emptyText: "Nothing is off." },
];

export function HealthPage() {
  const query = useQuery(healthOptions());
  const queryClient = useQueryClient();
  const [refreshError, setRefreshError] = useState<unknown>(null);
  // A forced run waits for every probe; cancel a poll in flight so its older
  // reading cannot land after the forced one (legacy useDoctorRefresh).
  const refresh = useMutation({
    mutationFn: async () => {
      setRefreshError(null);
      await queryClient.cancelQueries({ queryKey: SETTINGS_KEYS.health });
      return queryClient.fetchQuery({ queryKey: SETTINGS_KEYS.health, queryFn: ({ signal }) => fetchHealthReport(true, signal), staleTime: 0 });
    },
    onError: (error) => setRefreshError(error),
  });
  const summary = query.data?.summary;
  const loaded = query.data !== undefined;

  return (
    <>
      <SettingsHeader
        title="Health"
        description="Which parts of the hub work, which need attention, and what to do about each."
        actions={
          <Button variant="secondary" onClick={() => refresh.mutate()} loading={refresh.isPending} disabled={refresh.isPending}>
            Run the checks now
          </Button>
        }
      />
      <div className="flex flex-col gap-4">
        {refreshError !== null && (
          <Notice tone="danger" role="alert">
            <p className="m-0">
              The checks could not be run now. {describeSettingsError(refreshError, "the health report").message}
              {loaded ? " The report below is the last one the hub gave." : ""}
            </p>
          </Notice>
        )}
        <SettingsRegion query={query} what="the health report" loadingLabel="Loading the health report" errorTitle="The health report could not be loaded" hideFreshness>
          {(data) => <HealthReport data={data} receivedAt={query.dataUpdatedAt} fetching={query.isFetching || refresh.isPending} />}
        </SettingsRegion>
        {loaded && summary && (
          <p className="m-0 text-xs text-[var(--ua-muted)]">
            Hub totals: {summary.live ?? UNKNOWN_COUNT} working (LIVE), {summary.degraded ?? UNKNOWN_COUNT} partly working (DEGRADED), {summary.misconfigured ?? UNKNOWN_COUNT} set up wrong (MISCONFIGURED),{" "}
            {summary.unknown ?? UNKNOWN_COUNT} could not check (UNKNOWN), {summary.dormant ?? UNKNOWN_COUNT} off (DORMANT).
          </p>
        )}
      </div>
    </>
  );
}

function HealthReport({ data, receivedAt, fetching }: { data: HealthPayload; receivedAt: number; fetching: boolean }) {
  const now = useNow(15_000);
  // The hub's age is as of receipt; keep counting from there (legacy #1044).
  const ageMs = typeof data.cached_age_s === "number" ? data.cached_age_s * 1000 + Math.max(0, now - receivedAt) : Math.max(0, now - receivedAt);
  const groups = groupChecks(data.checks);
  return (
    <div className="flex flex-col gap-4">
      <p className="m-0 text-sm text-[var(--ua-muted)]">
        <span title={data.cached_at ? `The checks ran at ${data.cached_at}` : exactTime(receivedAt)}>Checked {agoText(ageMs)}</span>
        {data.stale ? ". This report is older than usual; a new one is being made" : ""}
        {data.refreshing && !data.stale ? ". A new report is being made in the background" : ""}.{" "}
        <span role="status">{fetching ? "Refreshing…" : ""}</span>
      </p>
      {data.error && (
        <Notice tone="warn" role="status">
          The latest run of the checks did not finish in time, so this is the last report the hub has.{" "}
          <details className="mt-1 text-xs">
            <summary className="cursor-pointer">Technical detail</summary>
            <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{data.error}</pre>
          </details>
        </Notice>
      )}
      {data.refresh_error && (
        <Notice tone="warn" role="status">
          The last background run of the checks failed{data.refresh_error.at ? ` at ${data.refresh_error.at}` : ""}. This is the last good report.
          {data.refresh_error.error && (
            <details className="mt-1 text-xs">
              <summary className="cursor-pointer">Technical detail</summary>
              <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{data.refresh_error.error}</pre>
            </details>
          )}
        </Notice>
      )}
      {data.checks.length === 0 ? (
        <Notice role="status">The hub ran its checks but reported none. Run the checks again; if it stays empty, the doctor on the hub is not set up.</Notice>
      ) : (
        GROUPS.map((group) => (
          <section key={group.id} aria-labelledby={`health-${group.id}`} className="flex flex-col gap-2 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4">
            <div className="flex flex-wrap items-baseline gap-2">
              <h2 id={`health-${group.id}`} className="m-0 text-base font-semibold">
                {group.title}
              </h2>
              <Badge variant={group.id === "attention" && groups.attention.length > 0 ? "warning" : "neutral"}>{groups[group.id].length}</Badge>
              <p className="m-0 basis-full text-sm text-[var(--ua-muted)]">{group.description}</p>
            </div>
            {groups[group.id].length === 0 ? (
              <p className="m-0 text-sm text-[var(--ua-muted)]">{group.emptyText}</p>
            ) : (
              <ul className="m-0 flex list-none flex-col divide-y divide-[var(--ua-border)] p-0">
                {groups[group.id].map((check, index) => (
                  <HealthRow key={`${checkName(check)}-${index}`} check={check} />
                ))}
              </ul>
            )}
          </section>
        ))
      )}
    </div>
  );
}

function HealthRow({ check }: { check: DoctorCheck }) {
  const status = statusOf(check);
  const info = STATUS_INFO[status];
  const raw = String(check.status || "UNKNOWN").toUpperCase();
  return (
    <li className="flex flex-col gap-1 py-3 first:pt-0 last:pb-0" data-check={checkName(check)} data-status={raw}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="m-0 min-w-0 flex-1 break-words text-sm font-semibold">{plainName(check)}</h3>
        <StatePill kind={info.kind} label={info.word} />
      </div>
      {check.detail ? <p className="m-0 break-words text-sm">{String(check.detail)}</p> : null}
      <p className="m-0 text-sm text-[var(--ua-muted)]">
        <span className="font-medium text-[var(--ua-fg)]">What to do: </span>
        {info.todo(check)}
      </p>
      <p className="m-0 break-words text-xs text-[var(--ua-muted)]">
        System name <code className="font-mono">{checkName(check)}</code> · state <code className="font-mono">{raw}</code>
        {check.gate ? (
          <>
            {" "}· setting <code className="font-mono">{String(check.gate)}</code>
          </>
        ) : null}
      </p>
    </li>
  );
}
