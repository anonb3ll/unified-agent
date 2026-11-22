import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchNodes, NODES_QUERY_KEY, type NodeDispatchItem } from "../../lib/api/nodes";
import { Button, ConfirmDialog, DataTable, DescriptionList, Drawer, Skeleton, UiLink, type Column } from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import { fetchSessionList, sessionQueryKeys, WRITE_PATHS, type NodeStartResult } from "./api";
import type { SessionRow } from "../../lib/sessions/types";
import { useItemTranscript } from "./itemTranscript";
import { useLiveEvents } from "./live";
import { buildTranscript, formatWhen } from "./model";
import { LiveIndicator, SectionHeading, useWhoami, WriteStatus } from "./parts";
import { blockedReason, requestFingerprint, resolvableOnStart, useLaunchGuard, type LaunchSpec } from "./launchGuard";
import { DEVICE_WORK_CAP, LaunchBlockedNote } from "./LaunchResolution";
import { useStreamInvalidation } from "../../lib/polling";
import { Transcript } from "./Transcript";

const STATUS_TEXT: Record<string, string> = {
  queued: "Waiting for the device",
  claimed: "Running",
  completed: "Finished",
  failed: "Failed",
  cancelled: "Cancelled",
};

/** The hub session row a device job opened, or null when none is registered. */
export function resolveHubSession(item: NodeDispatchItem, sessions: readonly SessionRow[]): SessionRow | null {
  const onNode = (s: SessionRow) => s.meta?.node === item.node;
  return (
    // The row bound to this item (node_queue records `item_id` in its meta)...
    sessions.find((s) => onNode(s) && s.meta?.item_id === item.id && (!item.session_id || !s.external_session_id || s.external_session_id === item.session_id)) ??
    // ...or, for a resumed harness session bound to an earlier item, the row with its harness session id on the same node.
    (item.session_id ? sessions.find((s) => onNode(s) && s.external_session_id === item.session_id) : undefined) ??
    null
  );
}

function itemHref(id: string) {
  return `/sessions?tab=work&item=${encodeURIComponent(id)}`;
}

const COLUMNS: Column<NodeDispatchItem>[] = [
  {
    id: "job",
    header: "Job",
    rowLink: true,
    cell: (i) => (
      <span className="flex flex-col">
        <span className="font-mono text-xs">{i.id}</span>
        <span className="line-clamp-2 max-w-[22rem]">{i.prompt_preview || "No prompt recorded"}</span>
      </span>
    ),
  },
  { id: "device", header: "Device", cell: (i) => `${i.node} · ${i.backend}` },
  { id: "agent", header: "Agent", cell: (i) => i.profile_agent ?? i.agent },
  { id: "created", header: "Sent", cell: (i) => formatWhen(i.created_at) },
  {
    id: "status",
    header: "State",
    cell: (i) => `${STATUS_TEXT[i.status] ?? i.status}${i.cancel_requested ? " (stop requested)" : ""}${i.outcome_unknown ? " (outcome unknown)" : ""}`,
  },
];

/**
 * Work sent to devices (legacy "Node session transcript", review finding 6).
 * A device job has no agent session row until its agent opens one, and a job
 * that fails first never gets one, so this view reads `/api/nodes` recent
 * work directly: open a job for its transcript, cancel or start it again.
 */
export function DeviceWorkPanel() {
  const { values, update } = useUrlParams({ item: "" });
  const nodes = useQuery({ queryKey: NODES_QUERY_KEY, queryFn: ({ signal }) => fetchNodes(signal) });
  // Device work changes on node events; a reconnect may have missed some (review 2, finding 8).
  const queryClient = useQueryClient();
  useStreamInvalidation(() => void queryClient.invalidateQueries({ queryKey: NODES_QUERY_KEY }), { channels: ["agent.node"], onReconnect: true });
  const selected = values.item ? (nodes.data?.recent_items.find((i) => i.id === values.item) ?? null) : null;
  return (
    <section aria-labelledby="work-heading">
      <SectionHeading id="work-heading">Work sent to devices</SectionHeading>
      <p className="m-0 mb-3 text-sm text-[var(--ua-muted)]">
        Each job a device was asked to run, including jobs still waiting for the device and jobs that stopped before a session opened.
      </p>
      <QueryRegion
        query={nodes}
        loadingLabel="Loading device work"
        errorTitle="Device work could not be loaded"
        headingLevel={3}
        isEmpty={(data) => data.recent_items.length === 0}
        empty={{ title: "No work sent to devices yet", description: "Jobs appear here when a session is started on a device." }}
      >
        {(data) => (
          <>
          {data.recent_items.length >= DEVICE_WORK_CAP ? (
            <p className="m-0 mb-2 text-sm text-[var(--ua-muted)]">The hub lists only its latest {DEVICE_WORK_CAP} jobs; older ones are not shown here.</p>
          ) : null}
          <DataTable
            columns={COLUMNS}
            data={data.recent_items}
            getRowKey={(i) => i.id}
            getRowHref={(i) => itemHref(i.id)}
            caption="Work sent to devices, newest first"
            captionSrOnly
            scrollLabel="Work sent to devices"
          />
          </>
        )}
      </QueryRegion>
      {values.item ? (
        <DeviceJobDrawer
          itemId={values.item}
          item={selected}
          loading={nodes.isPending}
          onClose={() => update({ item: null }, { push: true })}
        />
      ) : null}
    </section>
  );
}

function DeviceJobDrawer({ itemId, item, loading, onClose }: { itemId: string; item: NodeDispatchItem | null; loading: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const transcript = useItemTranscript(itemId);
  // The item's `session_id` is the harness (ACP) session id, not the hub's
  // session row id. Resolve the row by that id plus its node and item
  // binding (review 2, finding 7); hub task #2501 adds the row id to items.
  const sessions = useQuery({
    queryKey: sessionQueryKeys.list({ agent: "", status: "all" }),
    queryFn: ({ signal }) => fetchSessionList({ agent: "", status: "all" }, signal),
  });
  const hubSession = item ? resolveHubSession(item, sessions.data?.sessions ?? []) : null;
  const live = useLiveEvents(
    ["agent.node"],
    (event) => {
      if (transcript.push(event)) void queryClient.invalidateQueries({ queryKey: NODES_QUERY_KEY });
    },
    transcript.connected,
  );
  const [confirm, setConfirm] = useState<"cancel" | "restart" | null>(null);
  const invalidates = [NODES_QUERY_KEY, sessionQueryKeys.all];
  const cancel = useHubWrite<{ action: "cancel"; item_id: string }>({
    path: WRITE_PATHS.nodeSession,
    label: "Cancelling the job",
    invalidates,
    successMessage: "Cancel requested. The device stops the job at its next check.",
  });
  // "Start again" creates a new job: it goes through the launch guard.
  const guard = useLaunchGuard(useWhoami().data);
  const keyRef = useRef("");
  const restart = useHubWrite<{ action: "restart"; item_id: string }, NodeStartResult>({
    path: WRITE_PATHS.nodeSession,
    idempotencyKey: () => keyRef.current,
    label: "Starting the job again",
    invalidates,
    successMessage: "Started again as a new job.",
    // A restart creates a new job and does not deduplicate (hub task #2498).
    resumable: false,
  });
  const entries = useMemo(
    () => buildTranscript({ events: transcript.events, item, agent: item?.profile_agent ?? item?.agent ?? "Agent" }),
    [transcript.events, item],
  );
  const restartSpec: LaunchSpec = {
    op: "restart",
    target: item?.node ?? "",
    taskIds: item?.task_id != null ? [item.task_id] : [],
    label: `Start again: job ${itemId}`,
    fingerprint: requestFingerprint("restart", { item_id: itemId }),
  };
  const cancellable = item !== null && (item.status === "queued" || item.status === "claimed") && !item.cancel_requested;
  const restartData = restart.state.status === "done" || restart.state.status === "accepted" ? restart.state.data : null;

  return (
    <Drawer open onClose={onClose} title={`Device job ${itemId}`} description={item ? `${item.node} · ${item.backend}` : undefined}>
      {loading ? (
        <Skeleton label="Loading the job" lines={4} />
      ) : !item ? (
        <p className="m-0 text-sm">This job is no longer in the device's recent work. It may have been cleared; the session list may still show it.</p>
      ) : (
        <div className="flex flex-col gap-3">
          <DescriptionList
            items={[
              { term: "State", value: `${STATUS_TEXT[item.status] ?? item.status}${item.cancel_requested ? " (stop requested)" : ""}` },
              { term: "Agent", value: item.profile_agent ?? item.agent },
              { term: "Task", value: item.task_id != null ? <UiLink href={`/work/tasks/${item.task_id}`} className="underline">#{item.task_id}</UiLink> : "No task" },
              { term: "Sent", value: formatWhen(item.created_at) },
              { term: "Folder", value: <span className="break-all font-mono text-xs">{item.cwd}</span> },
              {
                term: "Session",
                value: hubSession ? (
                  <UiLink href={`/sessions/${encodeURIComponent(hubSession.id)}`} className="underline">
                    {hubSession.id}
                  </UiLink>
                ) : sessions.isPending ? (
                  "Checking…"
                ) : (
                  "No hub session is registered for this job yet. Its transcript is below."
                ),
              },
            ]}
          />
          {item.outcome_unknown ? (
            <p className="m-0 text-sm text-[var(--ua-warn)]">The device's lease ran out before it confirmed the outcome. Check what it did before starting it again.</p>
          ) : null}
          <div role="group" aria-label="Job actions" className="flex flex-wrap gap-2">
            <Button variant="danger" disabled={!cancellable} onClick={() => setConfirm("cancel")}>
              Cancel this job
            </Button>
            <Button disabled={!item.restartable || blockedReason(guard.status, restartSpec) !== null} onClick={() => setConfirm("restart")}>
              Start again
            </Button>
          </div>
          {!cancellable || !item.restartable ? (
            <ul className="m-0 list-none p-0 text-xs text-[var(--ua-muted)]" aria-label="Why some job actions are unavailable">
              {!cancellable ? <li>Cancel: {item.cancel_requested ? "a stop was already requested." : "the job is not waiting or running."}</li> : null}
              {!item.restartable ? <li>Start again: only a job that has ended, and whose outcome is known, can be started again.</li> : null}
            </ul>
          ) : null}
          {item.restartable ? (
            <LaunchBlockedNote reason={blockedReason(guard.status, restartSpec)} resolvable={resolvableOnStart(guard.status)} />
          ) : null}
          <WriteStatus state={cancel.state} label="Cancelling the job" />
          <WriteStatus state={restart.state} label="Starting the job again" />
          {restartData?.item_id ? (
            <p role="status" className="m-0 text-sm">
              Started again as{" "}
              <UiLink href={itemHref(restartData.item_id)} className="underline">
                job {restartData.item_id}
              </UiLink>
              .
            </p>
          ) : null}
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="m-0 text-sm font-semibold">Conversation</h3>
            <LiveIndicator state={live} />
          </div>
          {transcript.truncated ? <p className="m-0 text-xs text-[var(--ua-muted)]">This is a long job: only the most recent part is shown.</p> : null}
          {transcript.history.isPending ? <Skeleton label="Loading the conversation" lines={3} /> : <Transcript entries={entries} agent={item.profile_agent ?? item.agent} />}
        </div>
      )}
      <ConfirmDialog
        open={confirm === "cancel"}
        variant="danger"
        title="Cancel this job?"
        objectName={`${itemId} on ${item?.node ?? "its device"}`}
        consequence="The device stops the job at its next check. Work it has not saved may be lost."
        confirmLabel="Cancel job"
        cancelLabel="Keep it"
        busy={cancel.isRunning}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null);
          void cancel.run({ action: "cancel", item_id: itemId });
        }}
      />
      <ConfirmDialog
        open={confirm === "restart"}
        title="Start this job again?"
        objectName={`${itemId} on ${item?.node ?? "its device"}`}
        consequence="A new job starts with the same prompt and folder, without the earlier conversation."
        confirmLabel="Start again"
        busy={restart.isRunning}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null);
          const key = guard.begin(restartSpec);
          if (!key) return;
          keyRef.current = key;
          void restart.run({ action: "restart", item_id: itemId }).then((o) => guard.settle(key, o));
        }}
      />
    </Drawer>
  );
}
