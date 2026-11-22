/**
 * Settings, Integrations, Notifications (spec §9.8): what the classic
 * `?view=notify_delivery` pane showed (the last send, delivered and failed
 * counts, recent outcomes), with what to do when alerts are not arriving.
 * Outcomes only: the message text is never read, and nothing is sent from
 * here (sending stays with the approval-gated `notify` tools).
 */
import { DataTable, type Column } from "../../lib/ui";
import type { NotifyDeliveryRow } from "../../lib/hub";
import { failingStreak, NOTIFY_CAP, notifyCounts, when } from "./api";
import { useNotifyStatus } from "./queries";
import { Advice, Facts, IntegrationsPage, isStale, ReadRegion, Section, StatusLabel } from "./shared";

export function NotificationsPage() {
  const query = useNotifyStatus();
  const stale = isStale(query);
  const columns: Column<NotifyDeliveryRow>[] = [
    { id: "when", header: "Sent", cell: (row) => when(row.sent_at) },
    { id: "agent", header: "From", cell: (row) => row.agent || "–" },
    {
      id: "outcome",
      header: "Outcome",
      cell: (row) => (
        <div className="flex flex-col gap-1">
          <StatusLabel tone={row.delivered ? "ok" : "warn"} label={row.delivered ? "Delivered" : "Not delivered"} />
          {row.error && (
            <details className="text-xs text-[var(--ua-muted)]">
              <summary className="cursor-pointer">Reason recorded</summary>
              <span className="block break-words font-mono">{row.error}</span>
            </details>
          )}
        </div>
      ),
    },
  ];
  return (
    <IntegrationsPage
      title="Notifications"
      description="Whether alerts from agents reached you (by iMessage). This page shows outcomes only: never the message text, and it sends nothing."
    >
      <ReadRegion
        query={query}
        what="notification delivery"
        loadingLabel="Loading notification delivery"
        isEmpty={(d) => d.deliveries.length === 0}
        empty={{
          title: "No notifications sent yet",
          description: "Nothing has tried to send a notification from this hub, so there is nothing to check.",
        }}
      >
        {(data) => {
          const counts = notifyCounts(data);
          const streak = failingStreak(data.deliveries);
          const last = data.last;
          return (
            <>
              <Section id="notify-summary-heading" title="Latest">
                <div className="flex flex-wrap items-center gap-3" data-testid="notify-last">
                  {last ? (
                    <>
                      <StatusLabel tone={last.delivered ? "ok" : "warn"} label={last.delivered ? "Last one delivered" : "Last one not delivered"} stale={stale} />
                      <span className="text-sm text-[var(--ua-muted)]">Sent {when(last.sent_at)}{last.agent ? ` by ${last.agent}` : ""}.</span>
                    </>
                  ) : (
                    <span className="text-sm text-[var(--ua-muted)]">No send recorded.</span>
                  )}
                </div>
                {streak > 0 && (
                  <Advice title={streak === 1 ? "The last notification did not arrive" : `The last ${streak} notifications did not arrive`} tone="warn">
                    <p className="m-0">
                      Notifications go out as iMessages through the hub's iMessage bridge. Check that the bridge is installed and
                      can send (a timed-out or failed send is usually the bridge or its connection), then open "Reason
                      recorded" on a failed send below for the hub's note.
                    </p>
                  </Advice>
                )}
                <Facts
                  items={[
                    {
                      label: counts.capped ? `Delivered, of the last ${counts.total}` : "Delivered",
                      value: counts.delivered,
                      testId: "notify-delivered",
                    },
                    {
                      label: counts.capped ? `Not delivered, of the last ${counts.total}` : "Not delivered",
                      value: counts.failed,
                      testId: "notify-failed",
                    },
                  ]}
                />
              </Section>
              <Section
                id="notify-history-heading"
                title="Recent sends"
                description={`Newest first. Only the last ${NOTIFY_CAP} are read.`}
              >
                <DataTable
                  columns={columns}
                  data={data.deliveries}
                  getRowKey={(row, i) => `${row.sent_at}-${i}`}
                  caption="Recent notification sends"
                  scrollLabel="Recent notification sends"
                />
              </Section>
            </>
          );
        }}
      </ReadRegion>
    </IntegrationsPage>
  );
}
