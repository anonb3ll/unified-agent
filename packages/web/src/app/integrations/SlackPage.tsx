/**
 * Settings, Integrations, Slack (spec §9.8): everything the classic
 * `?view=slack_gateway` pane showed (status, what is missing, allowlists,
 * credential presence, recent activity, where the settings live), in plain
 * words with what to do. Status only: this page never sends to Slack and
 * never shows a secret, only whether one is present.
 */
import { DataTable, type Column } from "../../lib/ui";
import type { SlackGatewayEventRow } from "../../lib/hub";
import { SLACK_EVENT_CAP, slackEventText, slackReasonAdvice, slackStatusText, when } from "./api";
import { useSlackStatus } from "./queries";
import { Advice, explainOrStale, Facts, IntegrationsPage, isStale, ReadRegion, Section, StatusLabel } from "./shared";

const present = (value: boolean | null) => (value === null ? "Unknown" : value ? "Present" : "Missing");

export function SlackPage() {
  const query = useSlackStatus();
  const stale = isStale(query);
  const columns: Column<SlackGatewayEventRow>[] = [
    {
      id: "what",
      header: "What happened",
      cell: (row) => {
        const e = slackEventText(row.channel);
        return <StatusLabel tone={e.failed ? "warn" : "ok"} label={e.label} />;
      },
    },
    { id: "when", header: "When", cell: (row) => when(row.created_at) },
  ];
  return (
    <IntegrationsPage
      title="Slack"
      description="Whether people can reach agents from Slack. This page shows status only; it never sends to Slack and never shows a secret."
    >
      <ReadRegion query={query} what="the Slack status" loadingLabel="Loading the Slack status">
        {(data) => {
          const status = slackStatusText(data.status);
          const failures = data.events.filter((e) => slackEventText(e.channel).failed).length;
          return (
            <>
              <Section id="slack-status-heading" title="Status">
                <div className="flex flex-wrap items-center gap-3">
                  <StatusLabel tone={status.tone} label={status.label} stale={stale} testId="slack-status" />
                  <span className="text-sm text-[var(--ua-muted)]">{explainOrStale(status.explain, stale)}</span>
                </div>
                {data.reasons.length > 0 && (
                  <Advice title={data.status === "DORMANT" ? "How to turn it on" : "What to fix"} tone="warn">
                    <ul className="m-0 flex list-disc flex-col gap-1 pl-5" data-testid="slack-reasons">
                      {data.reasons.map((reason) => (
                        <li key={reason}>{slackReasonAdvice(reason)}</li>
                      ))}
                    </ul>
                  </Advice>
                )}
                {data.status === "LIVE" && failures > 0 && (
                  <Advice title="Some recent messages did not get through" tone="warn">
                    <p className="m-0">
                      {failures} of the last {data.events.length} events failed. If replies are failing, check that the bot
                      token is still valid and the bot is in the channel. If handing messages to an agent fails, check that
                      the coordinating agent is running.
                    </p>
                  </Advice>
                )}
                <Facts
                  items={[
                    { label: "Allowed channels", value: data.channelCount ?? "–", testId: "slack-channels" },
                    { label: "Allowed people", value: data.userCount ?? "–" },
                    { label: "Signing secret (checks messages come from Slack)", value: present(data.signingSecretPresent), testId: "slack-signing" },
                    { label: "Bot token (lets the hub reply)", value: present(data.botTokenPresent), testId: "slack-bot" },
                    { label: "Agent that receives messages", value: data.coordinator ?? "–" },
                    { label: "Last activity", value: when(data.lastEventAt) },
                  ]}
                />
                {data.configPointer && (
                  <p className="m-0 text-sm text-[var(--ua-muted)]" data-testid="slack-config-pointer">
                    These come from the hub's environment settings: {data.configPointer}. Change them on the hub and restart it.
                  </p>
                )}
              </Section>
              <Section
                id="slack-activity-heading"
                title="Recent activity"
                description={`The latest Slack events the hub recorded, newest first. Only the last ${SLACK_EVENT_CAP} are read.`}
              >
                <DataTable
                  columns={columns}
                  data={data.events}
                  getRowKey={(row, i) => row.ulid ?? `${row.channel}-${row.created_at}-${i}`}
                  caption="Recent Slack activity"
                  scrollLabel="Recent Slack activity"
                  emptyText="No Slack activity has been recorded."
                />
              </Section>
            </>
          );
        }}
      </ReadRegion>
    </IntegrationsPage>
  );
}
