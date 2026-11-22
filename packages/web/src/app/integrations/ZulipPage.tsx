/**
 * Settings, Integrations, Zulip (spec §9.8): what the classic `?view=zulip`
 * pane showed (worker health, per-agent channels, recent inbound messages,
 * the task-topic convention), in plain words with what to do. Read only.
 */
import { DataTable, UiLink, type Column } from "../../lib/ui";
import type { ZulipInboundRow } from "../../lib/hub";
import {
  channelStatusText,
  safeExternalUrl,
  seconds,
  when,
  ZULIP_INBOUND_CAP,
  zulipStatusText,
  type ZulipWorker,
} from "./api";
import { useZulipStatus } from "./queries";
import { Advice, explainOrStale, Facts, IntegrationsPage, isStale, ReadRegion, Section, StatusLabel } from "./shared";

type ChannelRow = ZulipWorker["channels"][number];

export function ZulipPage() {
  const query = useZulipStatus();
  const stale = isStale(query);

  const channelColumns: Column<ChannelRow>[] = [
    { id: "agent", header: "Agent", cell: (row) => row.agent_label || "–" },
    { id: "channel", header: "Channel", cell: (row) => <span className="font-mono text-xs">{row.channel || "–"}</span> },
    {
      id: "status",
      header: "Status",
      cell: (row) => (
        <div className="flex flex-col gap-1">
          <StatusLabel
            tone={row.status === "healthy" ? "ok" : row.status === "not_started" ? "off" : "warn"}
            label={channelStatusText(row.status)}
            stale={stale}
          />
          {row.last_error && (
            <details className="text-xs text-[var(--ua-muted)]">
              <summary className="cursor-pointer">Last error</summary>
              <span className="block break-words font-mono">{row.last_error}</span>
            </details>
          )}
        </div>
      ),
    },
    {
      id: "age",
      header: "Last read",
      cell: (row) => (
        <span>
          {row.cursor_age_seconds == null ? "Never" : `${seconds(row.cursor_age_seconds)} ago`}
          {row.cursor != null && <span className="block font-mono text-xs text-[var(--ua-muted)]">Up to message {String(row.cursor)}</span>}
        </span>
      ),
    },
    { id: "processed", header: "Messages handled", align: "right", cell: (row) => row.messages_processed ?? 0 },
  ];

  const inboundColumns: Column<ZulipInboundRow>[] = [
    { id: "when", header: "When", cell: (row) => when(row.created_at) },
    {
      id: "where",
      header: "Channel and topic",
      cell: (row) => (
        <span>
          <span className="font-mono text-xs">#{row.channel || "?"}</span>
          {row.topic ? ` / ${row.topic}` : ""}
          {typeof row.task_id === "number" && (
            <>
              {" "}
              <UiLink href={`/work/tasks/${row.task_id}`} className="text-[var(--ua-link)] underline">
                Task {row.task_id}
              </UiLink>
            </>
          )}
        </span>
      ),
    },
    { id: "sender", header: "From", cell: (row) => row.sender || "–" },
    {
      id: "for",
      header: "For",
      cell: (row) => (row.actionable_for && row.actionable_for.length ? row.actionable_for.join(", ") : row.is_bot ? "A bot (no action)" : "No one"),
    },
    {
      id: "link",
      header: "Open",
      cell: (row) => {
        const href = safeExternalUrl(row.zulip_url);
        return href ? (
          <a href={href} target="_blank" rel="noopener noreferrer" className="text-[var(--ua-link)] underline">
            In Zulip<span className="sr-only"> (opens in a new tab): #{row.channel} {row.topic}</span>
          </a>
        ) : (
          "–"
        );
      },
    },
  ];

  return (
    <IntegrationsPage
      title="Zulip"
      description="Agents and people coordinate in Zulip chat; the hub reads it so messages reach the right agent. Tasks and approvals still live here, not in Zulip."
    >
      <ReadRegion query={query} what="the Zulip status" loadingLabel="Loading the Zulip status">
        {(data) => {
          const status = zulipStatusText(data.worker.status);
          const site = safeExternalUrl(data.site);
          const queueProblems = (data.worker.queueSummary.error ?? 0) + (data.worker.queueSummary.reset ?? 0);
          return (
            <>
              <Section id="zulip-status-heading" title="Status">
                <div className="flex flex-wrap items-center gap-3">
                  <StatusLabel tone={status.tone} label={status.label} stale={stale} testId="zulip-status" />
                  <span className="text-sm text-[var(--ua-muted)]">{explainOrStale(status.explain, stale)}</span>
                </div>
                {data.configurationError && (
                  <Advice title="How to set it up" tone="warn">
                    <p className="m-0">
                      Set the Zulip site and bot credentials in the hub's environment, then restart the hub. The hub reported:
                    </p>
                    <details className="text-xs">
                      <summary className="cursor-pointer">What is missing</summary>
                      <span className="block break-words font-mono">{data.configurationError}</span>
                    </details>
                  </Advice>
                )}
                {!data.configurationError && queueProblems > 0 && (
                  <Advice title="Some message queues need attention" tone="warn">
                    <p className="m-0">
                      {queueProblems} of the hub's Zulip event queues failed or were reset, so messages can arrive late. They
                      recover on the next poll; if this stays, restart the hub's Zulip worker.
                    </p>
                  </Advice>
                )}
                {!data.configurationError && (
                  <Facts
                    items={[
                      {
                        label: "Zulip site",
                        value: site ? (
                          <a href={site} target="_blank" rel="noopener noreferrer" className="text-[var(--ua-link)] underline">
                            {new URL(site).host}
                            <span className="sr-only"> (opens in a new tab)</span>
                          </a>
                        ) : (
                          "–"
                        ),
                      },
                      { label: "Last successful read", value: when(data.worker.lastSuccessfulPoll) },
                      { label: "Reads every", value: seconds(data.worker.pollIntervalS) },
                      {
                        label: "Messages in the last 24 hours",
                        value: data.stats.inbound24h ?? "–",
                        testId: "zulip-inbound-24h",
                      },
                      {
                        label: "Coordination messages on a task topic (24 h)",
                        value:
                          data.stats.protocolTotal24h && data.stats.protocolTotal24h > 0
                            ? `${data.stats.protocolCompliant24h ?? 0} of ${data.stats.protocolTotal24h}`
                            : "–",
                      },
                    ]}
                  />
                )}
                {!data.configurationError && data.stats.protocolTotal24h ? (
                  <p className="m-0 text-sm text-[var(--ua-muted)]">
                    In the requests, coord and review channels, a topic named task-&lt;number&gt; links the conversation to
                    that task.
                  </p>
                ) : null}
              </Section>

              {!data.configurationError && (
                <Section id="zulip-channels-heading" title="Channels the hub reads" description="One row per agent and channel.">
                  <DataTable
                    columns={channelColumns}
                    data={data.worker.channels}
                    getRowKey={(row, i) => `${row.agent_label}-${row.channel}-${i}`}
                    caption="Zulip channels the hub reads"
                    scrollLabel="Zulip channels the hub reads"
                    emptyText="No agent is set to read any Zulip channel."
                  />
                </Section>
              )}

              {!data.configurationError && (
                <Section
                  id="zulip-inbound-heading"
                  title="Recent messages"
                  description={`Messages the hub received, newest first. Only the last ${ZULIP_INBOUND_CAP} are shown.`}
                >
                  {data.inbound === null ? (
                    <Advice title="Recent messages could not be read" tone="warn">
                      <p className="m-0">The hub could read the worker's status but not its message log. Retry in a moment.</p>
                      <details className="text-xs">
                        <summary className="cursor-pointer">Technical detail</summary>
                        <span className="block break-words font-mono">{data.inboundError}</span>
                      </details>
                    </Advice>
                  ) : (
                    <DataTable
                      columns={inboundColumns}
                      data={data.inbound}
                      getRowKey={(row, i) => `${row.created_at}-${i}`}
                      caption="Recent Zulip messages"
                      scrollLabel="Recent Zulip messages"
                      emptyText="No Zulip messages have been received."
                    />
                  )}
                </Section>
              )}
            </>
          );
        }}
      </ReadRegion>
    </IntegrationsPage>
  );
}
