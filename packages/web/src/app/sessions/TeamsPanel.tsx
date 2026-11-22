import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge, Button, Checkbox, ConfirmDialog, DataTable, Field, Input, Skeleton, UiLink, type Column } from "../../lib/ui";
import { approvalInboxHref, useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { fetchTeamPreview, fetchTeamSnapshots, sessionQueryKeys, WRITE_PATHS, type SeatPlan, type TeamSnapshotRow } from "./api";
import { formatWhen } from "./model";
import { SectionHeading, useWhoami, WriteStatus } from "./parts";
import { blockedReason, resolvableOnStart, useLaunchGuard, type LaunchSpec } from "./launchGuard";
import { LaunchBlockedNote } from "./LaunchResolution";

const OUTCOME: Record<string, { text: string; variant: "success" | "warning" | "neutral" }> = {
  resumed: { text: "Resumed", variant: "success" },
  rebuilt: { text: "Rebuilt", variant: "success" },
  fresh: { text: "Started fresh", variant: "neutral" },
  live: { text: "Already running", variant: "neutral" },
  failed: { text: "Failed", variant: "warning" },
  awaiting_approval: { text: "Waiting for approval", variant: "warning" },
};

const SEAT_COLUMNS = (done: boolean): Column<SeatPlan>[] => [
  { id: "seat", header: "Seat", cell: (s) => s.label ?? "Unnamed seat" },
  { id: "where", header: "Where", cell: (s) => [s.node, s.backend].filter(Boolean).join(" · ") || "Not recorded" },
  { id: "task", header: "Task", cell: (s) => (s.task_id != null ? `#${s.task_id}` : "No task") },
  {
    id: "plan",
    header: done ? "Outcome" : "Would",
    cell: (s) => {
      const key = done ? (s.outcome ?? s.planned) : s.planned;
      const known = OUTCOME[key];
      return (
        <span className="flex flex-col gap-0.5">
          <Badge variant={known?.variant ?? "neutral"}>{known?.text ?? key}</Badge>
          {done && key === "awaiting_approval" && s.awaiting_approval != null ? (
            <UiLink href={approvalInboxHref(String(s.awaiting_approval))} className="text-xs underline">
              Open approval #{s.awaiting_approval}
            </UiLink>
          ) : null}
          {done && s.new_item_id ? (
            <UiLink href={`/sessions?tab=work&item=${encodeURIComponent(s.new_item_id)}`} className="text-xs underline">
              Device job {s.new_item_id}
            </UiLink>
          ) : null}
          {s.reason ? <span className="text-xs text-[var(--ua-muted)]">{s.reason}</span> : null}
        </span>
      );
    },
  },
];

/**
 * Team snapshot and restore (legacy Sessions "Team snapshot and restore",
 * task #1738): save which agents were working on what, preview what a
 * restore would do per seat, then restore with a confirmation.
 */
/**
 * What a restore left to do (review finding 12): the hub answers 200 even when
 * seats wait for approval. Each waiting seat links to its approval above;
 * "Continue the restore" runs the restore again once they are decided.
 */
function RestoreSummary({ seats, busy, onContinue }: { seats: SeatPlan[]; busy: boolean; onContinue: () => void }) {
  const waiting = seats.filter((s) => s.outcome === "awaiting_approval").length;
  const failed = seats.filter((s) => s.outcome === "failed").length;
  if (waiting === 0) {
    return (
      <p role="status" className="m-0 mt-2 text-sm">
        {failed ? `All other seats are restored or running; ${failed} could not be restored (see the reasons above).` : "All seats are restored or running."}
      </p>
    );
  }
  return (
    <div role="status" className="mt-2 flex flex-col gap-2 rounded-md border border-[var(--ua-warn)] p-3 text-sm">
      <p className="m-0">
        {waiting === 1 ? "1 seat is waiting for approval" : `${waiting} seats are waiting for approval`}. Nothing starts for {waiting === 1 ? "it" : "them"} by
        itself: open each approval above, and once it is decided, continue the restore.
      </p>
      <Button className="self-start" onClick={onContinue} loading={busy}>
        Continue the restore
      </Button>
    </div>
  );
}

export function TeamsPanel() {
  const snapshots = useQuery({ queryKey: sessionQueryKeys.teamSnapshots, queryFn: ({ signal }) => fetchTeamSnapshots(signal) });
  const [team, setTeam] = useState("");
  const [selected, setSelected] = useState<TeamSnapshotRow | null>(null);
  const [confirming, setConfirming] = useState(false);
  const preview = useQuery({
    queryKey: [...sessionQueryKeys.teamSnapshots, "preview", selected?.snapshot_id ?? ""],
    queryFn: ({ signal }) => fetchTeamPreview(selected?.snapshot_id ?? "", signal),
    enabled: selected !== null,
  });
  const take = useHubWrite<{ team: string }>({
    path: WRITE_PATHS.teamSnapshots,
    label: "Taking a team snapshot",
    invalidates: [sessionQueryKeys.teamSnapshots],
    successMessage: "Snapshot saved.",
  });
  // A restore (and its continuation) starts new jobs: it goes through the launch guard.
  const guard = useLaunchGuard(useWhoami().data);
  const keyRef = useRef("");
  const [acknowledged, setAcknowledged] = useState<Record<string, boolean>>({});
  const restore = useHubWrite<Record<string, never>, { seats?: SeatPlan[] }>({
    path: WRITE_PATHS.teamRestore(selected?.snapshot_id ?? ""),
    idempotencyKey: () => keyRef.current,
    label: "Restoring the team",
    invalidates: [sessionQueryKeys.all],
    // A restore starts seats from the snapshot's current state; repeating it
    // after an approval is not proven to rebuild the same request.
    resumable: false,
  });
  const restored = restore.state.status === "done" || restore.state.status === "accepted" ? (restore.state.data.seats ?? []) : null;

  // Continuing: the refreshed preview says which seats would start now.
  // A seat that already started in the last restore and would start again
  // is named and needs an explicit acknowledgement (review 2, finding 6).
  const startedBefore = new Set((restored ?? []).filter((r) => ["resumed", "rebuilt", "fresh"].includes(r.outcome ?? "")).map((r) => r.label ?? ""));
  const wouldStart = (restored && preview.data ? preview.data : []).filter((seat) => ["resumed", "rebuilt", "fresh"].includes(seat.planned));
  const again = wouldStart.filter((seat) => startedBefore.has(seat.label ?? ""));
  const continueReady = !restored || (!preview.isFetching && preview.isSuccess);
  const allAcknowledged = again.every((seat) => acknowledged[seat.label ?? ""]);

  const restoreSpec: LaunchSpec | undefined = selected
    ? {
        op: "restore",
        target: selected.snapshot_id,
        taskIds: (preview.data ?? []).map((seat) => seat.task_id).filter((t): t is number => t != null),
        label: `${restored ? "Continue restoring" : "Restore"} ${selected.team ?? "team"}`,
      }
    : undefined;
  const runRestore = () => {
    if (!restoreSpec) return;
    const key = guard.begin(restoreSpec);
    if (!key) return;
    keyRef.current = key;
    void restore.run({}).then((o) => guard.settle(key, o));
  };
  const openContinue = () => {
    setAcknowledged({});
    void preview.refetch();
    setConfirming(true);
  };

  return (
    <section aria-labelledby="teams-heading">
      <SectionHeading id="teams-heading">Team snapshots</SectionHeading>
      <p className="m-0 mb-3 text-sm text-[var(--ua-muted)]">
        A snapshot records which agent sat in each seat of a team and what it was working on, so the team can be brought back later.
      </p>
      <form
        className="mb-4 flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!team.trim()) return;
          void take.run({ team: team.trim() }).then((o) => {
            if (o.kind === "done") setTeam("");
          });
        }}
      >
        <Field label="Team name" className="min-w-48">
          <Input value={team} onChange={(e) => setTeam(e.target.value)} autoComplete="off" />
        </Field>
        <Button type="submit" disabled={!team.trim()} loading={take.isRunning}>
          Take snapshot
        </Button>
        <WriteStatus state={take.state} label="The snapshot" className="w-full" />
      </form>
      <QueryRegion
        query={snapshots}
        loadingLabel="Loading team snapshots"
        errorTitle="Team snapshots could not be loaded"
        headingLevel={3}
        isEmpty={(rows) => rows.length === 0}
        empty={{ title: "No snapshots yet", description: "Take a snapshot of a team above to be able to restore it later." }}
      >
        {(rows) => (
          <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Team snapshots">
            {rows.map((row) => (
              <li key={row.snapshot_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-[var(--ua-border)] px-3 py-2 text-sm">
                <span>
                  {row.team ?? "Unnamed team"} · {row.seats} seats · {formatWhen(row.taken_at)}
                  {row.by ? ` · by ${row.by}` : ""}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Preview restoring ${row.team ?? "unnamed team"} from ${formatWhen(row.taken_at)}`}
                  onClick={() => {
                    restore.reset();
                    setSelected(row);
                  }}
                >
                  Preview restore
                </Button>
              </li>
            ))}
          </ul>
        )}
      </QueryRegion>
      {selected ? (
        <section aria-labelledby="team-preview-heading" className="mt-4">
          <h3 id="team-preview-heading" className="m-0 mb-2 text-sm font-semibold">
            {restored ? "Restore result" : "What a restore would do"}: {selected.team ?? "Unnamed team"}
          </h3>
          {restored ? (
            <>
              <DataTable columns={SEAT_COLUMNS(true)} data={restored} caption="Restore result per seat" captionSrOnly getRowKey={(s, i) => `${s.label ?? ""}-${i}`} />
              <RestoreSummary seats={restored} busy={restore.isRunning} onContinue={openContinue} />
            </>
          ) : (
            <QueryRegion
              query={preview}
              loadingLabel="Working out what a restore would do"
              errorTitle="The restore preview could not be loaded"
              headingLevel={4}
              isEmpty={(seats) => seats.length === 0}
              empty={{ title: "This snapshot has no seats" }}
            >
              {(seats) => (
                <>
                  <DataTable columns={SEAT_COLUMNS(false)} data={seats} caption="Restore plan per seat" captionSrOnly getRowKey={(s, i) => `${s.label ?? ""}-${i}`} />
                  <Button className="mt-2" variant="primary" onClick={() => setConfirming(true)} loading={restore.isRunning}>
                    Restore this team
                  </Button>
                </>
              )}
            </QueryRegion>
          )}
          <WriteStatus state={restore.state} label="The restore" className="mt-2" />
        </section>
      ) : null}
      <ConfirmDialog
        open={confirming}
        title={restored ? "Continue the restore?" : "Restore this team?"}
        objectName={selected?.team ?? "Unnamed team"}
        consequence={
          restored
            ? "The restore runs again with the hub's current view of each seat. Seats still running are left alone; the seats below start now."
            : "Each seat is resumed, rebuilt or started fresh as the preview shows. Agents that start will begin working right away. A seat whose start needs approval waits for it."
        }
        confirmLabel={restored ? "Continue" : "Restore team"}
        confirmDisabled={!continueReady || !allAcknowledged || blockedReason(guard.status, restoreSpec) !== null}
        busy={restore.isRunning}
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          runRestore();
        }}
      >
        {restored ? (
          !continueReady ? (
            <Skeleton label="Checking each seat again" lines={2} />
          ) : (
            <div className="flex flex-col gap-2">
              {wouldStart.length ? (
                <ul className="m-0 list-disc pl-5" aria-label="Seats that would start">
                  {wouldStart.map((seat) => (
                    <li key={seat.label ?? seat.item_id ?? ""}>
                      {seat.label ?? "Unnamed seat"}:{" "}
                      {startedBefore.has(seat.label ?? "") ? "started in the last restore and would start again" : "would start"}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="m-0">No seat would start now.</p>
              )}
              {again.map((seat) => (
                <Checkbox
                  key={`ack-${seat.label ?? ""}`}
                  label={`Start ${seat.label ?? "this seat"} again`}
                  description="It already started once; continuing starts a second session for it."
                  checked={acknowledged[seat.label ?? ""] === true}
                  onChange={(e) => setAcknowledged((prev) => ({ ...prev, [seat.label ?? ""]: e.target.checked }))}
                />
              ))}
              <LaunchBlockedNote reason={blockedReason(guard.status, restoreSpec)} resolvable={resolvableOnStart(guard.status)} />
            </div>
          )
        ) : (
          <LaunchBlockedNote reason={blockedReason(guard.status, restoreSpec)} resolvable={resolvableOnStart(guard.status)} />
        )}
      </ConfirmDialog>
    </section>
  );
}
