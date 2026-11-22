/**
 * Usage (spec §9.7): quota and spend. Retains the legacy Quota pane and its
 * three sections: model availability per lane (`/api/quota`), costs and
 * budgets (`/api/costs`), delivery targets (`/api/delivery_slo`) and the
 * per-tool call allowance (`/api/tool-allowance`). Unknown cost is a count,
 * never $0; a missing measurement is "no data", never a number.
 */
import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, StatePill, type Column, type StateKind } from "../../lib/ui";
import type { BudgetRow, CostGroupRow, DeliverySloMetric, QuotaLaneRow, ToolAllowanceRow } from "../../lib/api/quota";
import { backoffRefetchInterval } from "../../lib/polling";
import { fetchAllowance, fetchCostReport, fetchQuotaReport, fetchSloReport, SETTINGS_KEYS, type CostsPayload, type DeliverySloPayload, type QuotaPayload, type ToolAllowancePayload } from "./api";
import { humanize, Notice, plural, Section, SettingsHeader, SettingsRegion, usd } from "./shared";

const POLL = backoffRefetchInterval(60_000);

export function UsagePage() {
  const quota = useQuery({ queryKey: SETTINGS_KEYS.quota, queryFn: ({ signal }) => fetchQuotaReport(signal), refetchInterval: POLL });
  const costs = useQuery({ queryKey: SETTINGS_KEYS.costs, queryFn: ({ signal }) => fetchCostReport(signal), refetchInterval: POLL });
  const slo = useQuery({ queryKey: SETTINGS_KEYS.slo, queryFn: ({ signal }) => fetchSloReport(signal), refetchInterval: POLL });
  const allowance = useQuery({ queryKey: SETTINGS_KEYS.allowance, queryFn: ({ signal }) => fetchAllowance(signal), refetchInterval: POLL });
  return (
    <>
      <SettingsHeader title="Usage" description="Which models can take work now, what work has cost, how delivery is going, and how many tool calls are left." />
      <div className="flex flex-col gap-4">
        <Section id="usage-quota" title="Model availability" description="Whether each model lane can take work now, from what the hub has seen. A lane with no source is unknown, never assumed available.">
          <SettingsRegion
            query={quota}
            what="model availability"
            loadingLabel="Loading model availability"
            errorTitle="Model availability could not be loaded"
            headingLevel={3}
            isEmpty={(d) => d.quotas.length === 0}
            empty={{ title: "No model lanes", description: "The hub has no lanes to report yet." }}
          >
            {(data) => <QuotaLanes data={data} />}
          </SettingsRegion>
        </Section>
        <Section id="usage-costs" title="Spend and budgets" description="What finished runs cost this month, money spent through the hub, and each budget's state.">
          <SettingsRegion query={costs} what="spend" loadingLabel="Loading spend" errorTitle="Spend could not be loaded" headingLevel={3}>
            {(data) => <Costs data={data} />}
          </SettingsRegion>
        </Section>
        <Section id="usage-slo" title="Delivery targets" description="How reliably work is delivered over the last days, against the targets.">
          <SettingsRegion query={slo} what="delivery targets" loadingLabel="Loading delivery targets" errorTitle="Delivery targets could not be loaded" headingLevel={3}>
            {(data) => <Slo data={data} />}
          </SettingsRegion>
        </Section>
        <Section id="usage-allowance" title="Tool call allowance" description="Calls left per tool under the hub's rate limits. When a limit is reached, the tool is refused until it resets.">
          <SettingsRegion query={allowance} what="the tool allowance" loadingLabel="Loading the tool allowance" errorTitle="The tool allowance could not be loaded" headingLevel={3}>
            {(data) => <Allowance data={data} />}
          </SettingsRegion>
        </Section>
      </div>
    </>
  );
}

const AVAILABILITY: Record<string, { word: string; kind: StateKind }> = {
  available: { word: "Available", kind: "ok" },
  unavailable: { word: "Used up", kind: "danger" },
  unknown: { word: "Unknown", kind: "unknown" },
};

function meter(row: QuotaLaneRow): string {
  const parts: string[] = [];
  if (row.used_percent != null) parts.push(`${row.used_percent}% used`);
  if (row.window_minutes != null) parts.push(`${row.window_minutes}-minute window`);
  if (row.resets_in_seconds != null) parts.push(`resets in ${Math.round(row.resets_in_seconds / 60)} min`);
  if (row.failure_class) parts.push(`last failure: ${humanize(row.failure_class)}`);
  if (row.availability === "unknown" && parts.length === 0) parts.push("No source has reported on this lane");
  return parts.join(" · ") || row.evidence || "–";
}

const LANE_COLUMNS: Column<QuotaLaneRow>[] = [
  {
    id: "lane",
    header: "Lane",
    cell: (row) => (
      <span>
        <span className="font-medium">{row.lane}</span>
        {row.provider ? <span className="block text-xs text-[var(--ua-muted)]">{row.provider}</span> : null}
      </span>
    ),
  },
  {
    id: "availability",
    header: "Can take work",
    cell: (row) => {
      const a = AVAILABILITY[row.availability] ?? { word: humanize(row.availability), kind: "unknown" as StateKind };
      return <StatePill kind={a.kind} label={a.word} data-availability={row.availability} />;
    },
  },
  { id: "meter", header: "Measured", cell: (row) => <span className="text-xs">{meter(row)}</span> },
  {
    id: "source",
    header: "Source",
    cell: (row) => (
      <span className="break-all text-xs">
        {row.source || "None"}
        {row.observed_at ? <span className="block text-[var(--ua-muted)]">seen {row.observed_at}</span> : null}
      </span>
    ),
  },
];

function QuotaLanes({ data }: { data: QuotaPayload }) {
  const failures = Object.entries(data.failure_class_counts ?? {}).filter(([, n]) => n > 0);
  return (
    <div className="flex flex-col gap-3">
      {data.credential_pool_error && (
        <Notice tone="warn" role="status">
          The registered accounts could not be read, so this list may be missing lanes. What the hub has seen is still shown.
        </Notice>
      )}
      <DataTable columns={LANE_COLUMNS} data={data.quotas} getRowKey={(r) => `${r.lane}:${r.provider ?? ""}:${r.label ?? ""}`} caption="Model lanes and whether each can take work" scrollLabel="Model lanes (scrolls sideways)" />
      {failures.length > 0 && (
        <p className="m-0 text-sm">
          <span className="font-medium">Recent failures by kind: </span>
          {failures.map(([cls, n]) => `${humanize(cls)} ${n}`).join(", ")}
        </p>
      )}
      {data.checked_at && <p className="m-0 text-xs text-[var(--ua-muted)]">Measured at {data.checked_at}</p>}
    </div>
  );
}

const COST_COLUMNS: Column<CostGroupRow>[] = [
  { id: "agent", header: "Agent", cell: (r) => r.agent },
  { id: "runs", header: "Runs", align: "right", cell: (r) => r.runs },
  { id: "cost", header: "Priced cost", align: "right", cell: (r) => (r.priced_runs ? usd(r.api_cost_usd) : "No priced runs") },
  { id: "unknown", header: "Runs of unknown cost", align: "right", cell: (r) => r.unknown_cost_runs },
  { id: "subscription", header: "On a subscription", align: "right", cell: (r) => r.subscription_runs },
  { id: "contended", header: "Waited for a lane", align: "right", cell: (r) => r.contended_runs },
];

function Costs({ data }: { data: CostsPayload }) {
  const rollup = data.rollup;
  const budgets = data.budgets;
  return (
    <div className="flex flex-col gap-3">
      {rollup ? (
        <>
          <p className="m-0 text-sm">
            {plural(rollup.totals.runs, "run")} since {rollup.since.slice(0, 10)}: {usd(rollup.totals.api_cost_usd)} priced
            {rollup.totals.unknown_cost_runs ? `, ${plural(rollup.totals.unknown_cost_runs, "run")} of unknown cost` : ""}
            {rollup.totals.subscription_runs ? `, ${plural(rollup.totals.subscription_runs, "run")} on a subscription` : ""}
            {rollup.totals.broker_usd ? `, ${usd(rollup.totals.broker_usd)} spent through the hub` : ""}.
          </p>
          {rollup.groups.length > 0 ? (
            <DataTable columns={COST_COLUMNS} data={rollup.groups} getRowKey={(r) => r.agent} caption="Cost per agent this period" scrollLabel="Cost per agent (scrolls sideways)" />
          ) : (
            <p className="m-0 text-sm text-[var(--ua-muted)]">No finished runs recorded this period.</p>
          )}
          {rollup.broker.length > 0 && (
            <p className="m-0 text-sm">
              <span className="font-medium">Model calls made through the hub: </span>
              {rollup.broker
                .map((b) => `${b.agent} ${plural(b.calls, "call")} ${usd(b.actual_usd)}${b.unreconciled_reserve_usd ? ` plus ${usd(b.unreconciled_reserve_usd)} held` : ""}`)
                .join("; ")}
            </p>
          )}
        </>
      ) : (
        <p className="m-0 text-sm text-[var(--ua-muted)]">The hub reported no cost totals.</p>
      )}
      <h3 className="m-0 text-sm font-semibold">Budgets</h3>
      {budgets?.error ? (
        <Notice tone="danger" role="alert">
          The budget settings could not be read, so the hub refuses work that a budget would cover until they are fixed.
          <details className="mt-1 text-xs">
            <summary className="cursor-pointer">Technical detail</summary>
            <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{budgets.error}</pre>
          </details>
        </Notice>
      ) : budgets && budgets.budgets.length > 0 ? (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {budgets.budgets.map((b) => (
            <BudgetItem key={b.name} budget={b} />
          ))}
        </ul>
      ) : (
        <p className="m-0 text-sm text-[var(--ua-muted)]">No budgets are set up. Budgets are set in the hub's policy file.</p>
      )}
    </div>
  );
}

const BUDGET_STATE: Record<string, { word: string; kind: StateKind }> = {
  ok: { word: "Within budget", kind: "ok" },
  warning: { word: "Close to the limit", kind: "warn" },
  exhausted: { word: "Used up", kind: "danger" },
};

function BudgetItem({ budget }: { budget: BudgetRow }) {
  return (
    <li className="rounded-md border border-[var(--ua-border)] p-3 text-sm">
      <p className="m-0 font-medium">{budget.name}</p>
      <p className="m-0 text-xs text-[var(--ua-muted)]">
        {usd(budget.limit_usd)} per {budget.period}, warns at {budget.warn_pct}%, {budget.action === "block" ? "stops work at the limit" : "only warns at the limit"}
      </p>
      {budget.agents_status.length > 0 ? (
        <ul className="m-0 mt-2 flex list-none flex-col gap-1 p-0">
          {budget.agents_status.map((s) => {
            const state = BUDGET_STATE[s.state] ?? { word: humanize(s.state), kind: "unknown" as StateKind };
            return (
              <li key={s.agent} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{s.agent}</span>
                <StatePill kind={state.kind} label={state.word} />
                <span>
                  {usd(s.committed_usd)} spent or held, {usd(s.remaining_usd)} left
                  {s.unknown_cost_runs ? `, ${plural(s.unknown_cost_runs, "run")} of unknown cost` : ""}
                  {s.subscription_runs ? `, subscription lane ${s.subscription_quota?.lane ?? "unknown"}: ${s.subscription_quota?.availability ?? "unknown"}` : ""}
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="m-0 mt-1 text-xs text-[var(--ua-muted)]">No agent under this budget has done work this period.</p>
      )}
    </li>
  );
}

const SLO_TILES: { key: keyof Pick<DeliverySloPayload, "dispatch_success_rate" | "p90_cycle_time_hours" | "unattributed_completions" | "ci_green_rate">; label: string; meaning: string; format: (v: number) => string }[] = [
  { key: "dispatch_success_rate", label: "Sessions that finished their work", meaning: "Share of started sessions that delivered.", format: (v) => `${Math.round(v * 100)}%` },
  { key: "p90_cycle_time_hours", label: "Time to finish (slowest 10%)", meaning: "Nine in ten tasks finished faster than this.", format: (v) => `${v.toFixed(1)} h` },
  { key: "unattributed_completions", label: "Finished work with no owner", meaning: "Tasks closed without a recorded agent.", format: (v) => String(Math.round(v)) },
  { key: "ci_green_rate", label: "Checks passing", meaning: "Share of pull request check runs that passed.", format: (v) => `${Math.round(v * 100)}%` },
];

function Slo({ data }: { data: DeliverySloPayload }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="m-0 text-sm text-[var(--ua-muted)]">Over the last {plural(data.window_days ?? 7, "day")}.</p>
      <dl className="m-0 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {SLO_TILES.map((tile) => (
          <SloTile key={tile.key} label={tile.label} meaning={tile.meaning} metric={data[tile.key]} format={tile.format} />
        ))}
      </dl>
    </div>
  );
}

function SloTile({ label, meaning, metric, format }: { label: string; meaning: string; metric: DeliverySloMetric | undefined; format: (v: number) => string }) {
  const status = metric?.status ?? "unavailable";
  const value = metric && metric.value !== null && metric.value !== undefined ? format(metric.value) : "No data";
  return (
    <div className="rounded-md border border-[var(--ua-border)] p-3" data-status={status}>
      <dt className="text-sm">{label}</dt>
      <dd className="m-0 mt-1 flex flex-col gap-1">
        <span className="text-lg font-semibold">{value}</span>
        {status === "breach" && <StatePill kind="danger" label="Misses the target" />}
        {status === "ok" && <StatePill kind="ok" label="Meets the target" />}
        <span className="text-xs text-[var(--ua-muted)]">{status === "unavailable" && metric?.detail ? metric.detail : meaning}</span>
      </dd>
    </div>
  );
}

function Allowance({ data }: { data: ToolAllowancePayload }) {
  const rows = data.tools ?? [];
  const fleet = data.agent == null;
  const full = data.capacity && data.capacity.buckets >= data.capacity.max_buckets;
  return (
    <div className="flex flex-col gap-3">
      <p className="m-0 text-sm text-[var(--ua-muted)]">
        {fleet ? "Every agent." : `Calls charged to ${data.agent}.`}
        {data.today ? ` Calls today counts from midnight UTC on ${data.today}.` : ""}
      </p>
      {full && (
        <Notice tone="warn" role="status">
          The hub's table of counters is full, so calls to tools it has not counted yet are refused.
        </Notice>
      )}
      {data.ledger_error && (
        <Notice tone="warn" role="status">
          Calls today could not be counted; the limits below are still enforced.
          <details className="mt-1 text-xs">
            <summary className="cursor-pointer">Technical detail</summary>
            <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{data.ledger_error}</pre>
          </details>
        </Notice>
      )}
      {rows.length === 0 ? (
        <p className="m-0 text-sm text-[var(--ua-muted)]">No rate limits are set and no calls were recorded today.</p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {rows.map((r, i) => (
            <AllowanceRow key={`${r.agent ?? ""}:${r.tool}:${r.rule ?? ""}:${i}`} row={r} showAgent={fleet} />
          ))}
        </ul>
      )}
    </div>
  );
}

function AllowanceRow({ row, showAgent }: { row: ToolAllowanceRow; showAgent: boolean }) {
  // A row for a retired rule generation is never "stopped".
  const stopped = row.stopped && row.current !== false;
  const limited = row.limit != null;
  return (
    <li className="rounded-md border border-[var(--ua-border)] p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {showAgent && row.agent ? <span className="text-xs text-[var(--ua-muted)]">{row.agent}</span> : null}
        <code className="font-mono font-medium">{row.tool}</code>
        {row.rule ? <span className="text-xs text-[var(--ua-muted)]">rule {row.rule}</span> : null}
        {stopped ? <StatePill kind="danger" label="Stopped" /> : null}
        {row.current === false ? <Badge>Old rule</Badge> : null}
      </div>
      {limited ? (
        <>
          <progress className="mt-2 h-2 w-full" max={row.limit ?? 0} value={Math.min(row.used, row.limit ?? 0)} aria-label={`${row.tool}: ${row.used} of ${row.limit} calls used`} />
          <p className="m-0 mt-1 text-xs text-[var(--ua-muted)]">
            {row.used} of {row.limit} used
            {row.remaining != null ? `, ${row.remaining} left` : ""}
            {row.window_seconds != null ? ` per ${Math.round(row.window_seconds)} seconds` : ""}
            {stopped && row.resets_at ? `. Stopped until ${row.resets_at.slice(11, 19)} UTC` : ""}
            {row.applies_reason ? `. ${row.applies_reason}` : ""}
          </p>
        </>
      ) : (
        <p className="m-0 mt-1 text-xs text-[var(--ua-muted)]">No limit set.</p>
      )}
      <p className="m-0 mt-1 text-xs text-[var(--ua-muted)]">
        {row.used_today === null ? `Calls today unknown${row.used_today_reason ? `: ${row.used_today_reason}` : ""}` : `${plural(row.used_today, "call")} today`}
      </p>
    </li>
  );
}
