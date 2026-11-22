import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchDispatchReceipts, type DispatchReceiptRow } from "../../lib/api/dispatch";
import { fetchNodes, NODES_QUERY_KEY, type NodeDispatchItem } from "../../lib/api/nodes";
import type { SessionRow } from "../../lib/sessions/types";
import { Button, ConfirmDialog, RadioGroup, Skeleton, UiLink } from "../../lib/ui";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { describeReadError } from "../lib/queries";
import { attemptHasEvidence, fetchLaunchAttempt, fetchSessionList, sessionQueryKeys, type LaunchAttemptRead } from "./api";
import { canResend, KEYED_OPS, type LaunchRecord } from "./launchGuard";
import { formatWhen } from "./model";

/** What the page shows once an attempt is resolved. */
export type Resolution =
  | { kind: "started"; label: string; href: string | null; by: "hub" | "person" }
  | { kind: "not_started"; reason: string };

/** Candidates are shown from a minute before the send to this long after it. */
export const WINDOW_BEFORE_MS = 60_000;
export const WINDOW_AFTER_MS = 10 * 60_000;
/** The hub's own caps: a read this long may have left older rows out. */
export const SESSIONS_CAP = 200;
export const DEVICE_WORK_CAP = 25;
export const RECEIPTS_CAP = 20;

export type Candidate = { id: string; label: string; href: string };

function inWindow(value: string | null | undefined, sentAt: number): boolean {
  if (!value) return false;
  const t = Date.parse(value);
  return Number.isFinite(t) && t >= sentAt - WINDOW_BEFORE_MS && t <= sentAt + WINDOW_AFTER_MS;
}

/**
 * Jobs that started around the same time as the attempt. For display only
 * (review round 2, C): they may be it; nothing is concluded from them.
 */
export function launchCandidates(
  record: LaunchRecord,
  reads: { sessions: readonly SessionRow[]; items: readonly NodeDispatchItem[]; receipts: readonly DispatchReceiptRow[] },
): { candidates: Candidate[]; compared: string } {
  const tasks = new Set(record.taskIds);
  const out: Candidate[] = [];
  const byTask = (taskId: number | null | undefined) => taskId != null && tasks.has(taskId);
  for (const s of reads.sessions) {
    if (!inWindow(s.started_at, record.sentAt)) continue;
    const hit =
      record.op === "dispatch"
        ? s.agent === record.target
        : record.op === "device" || record.op === "restart"
          ? s.meta?.node === record.target
          : byTask(s.task_id) || record.op === "restore";
    if (hit) out.push({ id: `session:${s.id}`, label: `${s.agent} session ${s.id}, started ${formatWhen(s.started_at)}`, href: `/sessions/${encodeURIComponent(s.id)}` });
  }
  for (const i of reads.items) {
    if (!inWindow(i.created_at, record.sentAt)) continue;
    const hit =
      record.op === "dispatch" ? i.agent === record.target : record.op === "device" || record.op === "restart" ? i.node === record.target : byTask(i.task_id) || record.op === "restore";
    if (hit) out.push({ id: `item:${i.id}`, label: `Device job ${i.id} on ${i.node}, sent ${formatWhen(i.created_at)}`, href: `/sessions?tab=work&item=${encodeURIComponent(i.id)}` });
  }
  for (const r of reads.receipts) {
    if (!inWindow(r.created_at, record.sentAt)) continue;
    const hit = record.op === "dispatch" ? r.agent_label === record.target : byTask(r.task_id);
    if (hit) out.push({ id: `run:${r.run_id}`, label: `Run ${r.run_id} for task #${r.task_id}, created ${formatWhen(r.created_at)}`, href: `/work/tasks/${r.task_id}` });
  }
  const from = formatWhen(new Date(record.sentAt - WINDOW_BEFORE_MS).toISOString());
  const to = formatWhen(new Date(record.sentAt + WINDOW_AFTER_MS).toISOString());
  const what =
    record.op === "dispatch"
      ? `sessions, device jobs and dispatch runs of ${record.target}`
      : record.op === "device" || record.op === "restart"
        ? `sessions and device jobs on ${record.target}`
        : record.op === "restore"
          ? "sessions and device jobs of any agent"
          : `sessions, device jobs and dispatch runs for task ${record.taskIds.map((t) => `#${t}`).join(", ")}`;
  return { candidates: out, compared: `Compared: ${what} that started between ${from} and ${to}.` };
}

/** Why a read cannot rule anything in or out, in plain words; empty when complete. */
export function incompleteReads(reads: {
  sessions: { sessions: SessionRow[]; error?: string } | undefined;
  nodes: { recent_items: NodeDispatchItem[]; error?: string } | undefined;
  receipts: { receipts: DispatchReceiptRow[]; error?: string; code?: string } | undefined;
}): string[] {
  const out: string[] = [];
  if (reads.sessions?.error) out.push(`the session list was incomplete (${reads.sessions.error})`);
  if ((reads.sessions?.sessions.length ?? 0) >= SESSIONS_CAP) out.push(`the session list stops at its newest ${SESSIONS_CAP}`);
  if (reads.nodes?.error) out.push(`device work was incomplete (${reads.nodes.error})`);
  if ((reads.nodes?.recent_items.length ?? 0) >= DEVICE_WORK_CAP) out.push(`the hub returns only its latest ${DEVICE_WORK_CAP} device jobs`);
  if (reads.receipts?.error || reads.receipts?.code) out.push(`dispatch runs were not available (${reads.receipts.error ?? reads.receipts.code})`);
  if ((reads.receipts?.receipts.length ?? 0) >= RECEIPTS_CAP) out.push(`dispatch runs stop at the newest ${RECEIPTS_CAP}`);
  return out;
}

function hrefFor(attempt: LaunchAttemptRead): string | null {
  if (attempt.session_id) return `/sessions/${encodeURIComponent(attempt.session_id)}`;
  if (attempt.item_id) return `/sessions?tab=work&item=${encodeURIComponent(attempt.item_id)}`;
  return null;
}

function failedText(attempt: LaunchAttemptRead): string {
  return attempt.reason ? `The hub says it failed: ${attempt.reason}` : "The hub says it failed.";
}

/**
 * Resolves an attempt whose outcome is not known (review round 2, B). The
 * hub's answer is the truth when it has the route; otherwise only the
 * person decides, with possible matches shown as candidates.
 *
 * Only a definite answer calls `onResolved` (task #2510): the hub says it
 * started, it has no record of a keyed launch, or it failed without
 * recording any step. A failed lookup, an unsettled start, or a failed
 * attempt that already created something keep the record and its key: the
 * person sends it again with the same key (`onRelaunch`) or explicitly
 * starts something new (`onStartNew`). A record stored before task #2510
 * does not say which request its key is bound to, so it is never sent again
 * (task #2532): only the lookup or "start something new" settles it.
 */
export function LaunchResolution({
  record,
  onResolved,
  onRelaunch,
  onStartNew,
}: {
  record: LaunchRecord;
  onResolved: (resolution: Resolution) => void;
  /** The person (or the hub) says it did not start, or an approval allows sending it again: launch with the same key. */
  onRelaunch: (key: string) => void;
  /** The person's explicit choice to set this launch aside and start something new under a new key. */
  onStartNew: () => void;
}) {
  const lookup = useQuery({
    queryKey: [...sessionQueryKeys.all, "launch-attempt", record.key],
    queryFn: ({ signal }) => fetchLaunchAttempt(record.key, signal),
    retry: false,
    // Ask again whenever the panel opens: a resend may have changed the attempt.
    staleTime: 0,
    refetchInterval: (q) => {
      const state = q.state.data && !q.state.data.routeAbsent ? q.state.data.attempt.state : null;
      return state === "in_progress" ? 3_000 : state === "unsettled" ? 15_000 : false;
    },
  });
  const attempt = lookup.data && !lookup.data.routeAbsent ? lookup.data.attempt : null;
  // The hub records only keyed launches: for any other, "unknown" proves nothing.
  const keyed = KEYED_OPS.has(record.op);
  const failedWithEvidence = attempt?.state === "failed" && attemptHasEvidence(attempt);
  const resendable = canResend(record);

  // The hub's definite answer resolves it.
  useEffect(() => {
    if (!attempt) return;
    if (attempt.state === "started") onResolved({ kind: "started", label: record.label, href: hrefFor(attempt), by: "hub" });
    else if (attempt.state === "failed" && !attemptHasEvidence(attempt)) onResolved({ kind: "not_started", reason: failedText(attempt) });
    else if (attempt.state === "unknown" && keyed) onResolved({ kind: "not_started", reason: "The hub never received it." });
    // `onResolved` is a fresh callback each render; the attempt is what matters.
  }, [attempt?.state, attempt?.key, failedWithEvidence]);

  return (
    <section aria-labelledby="launch-guard-heading" className="rounded-md border border-[var(--ua-warn)] p-3 text-sm">
      <h2 id="launch-guard-heading" className="m-0 text-sm font-semibold">
        Did the last launch start?
      </h2>
      <p className="m-0 mt-1">
        <span className="font-medium">{record.label}</span>, sent {formatWhen(new Date(record.sentAt).toISOString())}. No other launch starts until
        this one is settled, so the same work is not started twice.
      </p>
      {lookup.isPending ? (
        <Skeleton label="Asking the hub about the last launch" lines={1} />
      ) : lookup.isError ? (
        <div role="alert" className="mt-2">
          <p className="m-0">
            Could not ask the hub about it: {describeReadError(lookup.error).message}. Until it answers, this launch keeps its attempt id, so sending
            it again does not start it twice.
          </p>
          <div className="mt-1 flex flex-wrap gap-2">
            <Button size="sm" onClick={() => void lookup.refetch()}>
              Retry
            </Button>
            <StartNew record={record} onStartNew={onStartNew} />
          </div>
        </div>
      ) : attempt?.state === "in_progress" ? (
        <p role="status" className="m-0 mt-2">
          The hub is still starting it. This updates by itself.
        </p>
      ) : attempt?.state === "unsettled" ? (
        <div className="mt-2 flex flex-col gap-2">
          <p role="status" className="m-0">
            The hub cannot tell yet whether it started: the start of {attempt.run_id != null ? `run ${attempt.run_id}` : "its run"} has no recorded
            outcome, so the job may exist. An operator can settle it; this updates by itself.
          </p>
          <div>
            <StartNew record={record} onStartNew={onStartNew} />
          </div>
        </div>
      ) : failedWithEvidence && attempt ? (
        <div className="mt-2 flex flex-col gap-2">
          <p className="m-0">{failedText(attempt)}</p>
          {resendable ? (
            <p className="m-0">
              The hub had already begun this launch{attempt.task_id != null ? ` (task #${attempt.task_id})` : ""}. Launching it again with the same
              form continues it: the hub recognises this attempt and does not start it twice.
            </p>
          ) : (
            <NotResendable />
          )}
          <div className="flex flex-wrap gap-2">
            {resendable ? (
              <Button size="sm" variant="primary" onClick={() => onRelaunch(record.key)}>
                Launch it again
              </Button>
            ) : null}
            <StartNew record={record} onStartNew={onStartNew} />
          </div>
        </div>
      ) : attempt?.state === "parked" || (record.state === "parked" && !attempt) ? (
        <ParkedLaunch record={record} approvalId={attempt?.approval_id != null ? String(attempt.approval_id) : (record.approvalId ?? null)} hubKnows={attempt !== null} onRelaunch={onRelaunch} onResolved={onResolved} onStartNew={onStartNew} />
      ) : lookup.data?.routeAbsent || (attempt?.state === "unknown" && !keyed) ? (
        <PersonDecides record={record} onResolved={onResolved} onRelaunch={onRelaunch} onStartNew={onStartNew} />
      ) : null}
    </section>
  );
}

/** A record from before task #2510: its key is bound to a request this page no longer knows. */
function NotResendable() {
  return (
    <p className="m-0">
      This launch was recorded by an earlier version of this page, which did not keep the request it belongs to, so it cannot be sent again under
      its attempt id. Check the sessions and work it may have created, then start something new.
    </p>
  );
}

/** The person's explicit "start something new": confirmed, because the last launch may already have created work. */
function StartNew({ record, onStartNew }: { record: LaunchRecord; onStartNew: () => void }) {
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <Button size="sm" onClick={() => setConfirming(true)}>
        Start something new
      </Button>
      <ConfirmDialog
        open={confirming}
        title="Start something new?"
        objectName={record.label}
        consequence="The last launch may already have created a task or a session. A new launch gets a new attempt id, so the hub cannot recognise it as the same launch, and a second one may be created."
        confirmLabel="Start something new"
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          onStartNew();
        }}
      />
    </>
  );
}

/**
 * A launch the person chose to send again (task #2510). It keeps its key and
 * request across reloads; Start sends it again when the form is the same
 * request, and anything else waits for an explicit "start something new".
 */
export function ArmedLaunch({ record, matches, onStartNew }: { record: LaunchRecord; matches: boolean; onStartNew: () => void }) {
  return (
    <section aria-labelledby="launch-armed-heading" className="rounded-md border border-[var(--ua-warn)] p-3 text-sm">
      <h2 id="launch-armed-heading" className="m-0 text-sm font-semibold">
        Ready to send the last launch again
      </h2>
      <p className="m-0 mt-1">
        <span className="font-medium">{record.label}</span>, first sent {formatWhen(new Date(record.sentAt).toISOString())}. It keeps its attempt id,
        so the hub continues it and does not start it twice.{" "}
        {matches ? "Press Start to send it." : "The form no longer matches that launch: change it back to send it again, or start something new."}
      </p>
      <div className="mt-2">
        <StartNew record={record} onStartNew={onStartNew} />
      </div>
    </section>
  );
}

function ParkedLaunch({
  record,
  approvalId,
  hubKnows,
  onRelaunch,
  onResolved,
  onStartNew,
}: {
  record: LaunchRecord;
  approvalId: string | null;
  hubKnows: boolean;
  onRelaunch: (key: string) => void;
  onResolved: (resolution: Resolution) => void;
  onStartNew: () => void;
}) {
  const resendable = canResend(record);
  return (
    <div className="mt-2 flex flex-col gap-2">
      <ParkedWriteNotice approvalId={approvalId} label={record.label} />
      {resendable ? (
        <p className="m-0">
          {hubKnows
            ? "After it is approved, launch it again: the hub recognises this attempt and does not start it twice."
            : "The launch stopped part way, so the hub may already have created a task for it. After the approval, check sessions and work before launching again: launching again can start a second one."}
        </p>
      ) : (
        <NotResendable />
      )}
      <div className="flex flex-wrap gap-2">
        {resendable ? (
          <Button size="sm" variant="primary" onClick={() => onRelaunch(record.key)}>
            Launch it again
          </Button>
        ) : (
          <StartNew record={record} onStartNew={onStartNew} />
        )}
        <Button size="sm" onClick={() => onResolved({ kind: "not_started", reason: "You dropped the launch that was waiting for approval." })}>
          Drop it
        </Button>
      </div>
    </div>
  );
}

function PersonDecides({
  record,
  onResolved,
  onRelaunch,
  onStartNew,
}: {
  record: LaunchRecord;
  onResolved: (r: Resolution) => void;
  onRelaunch: (key: string) => void;
  onStartNew: () => void;
}) {
  const resendable = canResend(record);
  const sessions = useQuery({
    queryKey: [...sessionQueryKeys.all, "launch-guard", record.key, "sessions"],
    queryFn: ({ signal }) => fetchSessionList({ agent: "", status: "all" }, signal),
  });
  const nodes = useQuery({ queryKey: NODES_QUERY_KEY, queryFn: ({ signal }) => fetchNodes(signal), refetchOnMount: "always" });
  const receipts = useQuery({
    queryKey: [...sessionQueryKeys.all, "launch-guard", record.key, "receipts"],
    queryFn: ({ signal }) => fetchDispatchReceipts(signal),
    retry: false,
  });
  const [picked, setPicked] = useState("");
  const [confirming, setConfirming] = useState(false);
  const fetching = sessions.isFetching || nodes.isFetching || receipts.isFetching;
  const failed = [sessions, nodes, receipts].filter((q) => q.isError);
  const partial = incompleteReads({ sessions: sessions.data, nodes: nodes.data, receipts: receipts.data });
  const found =
    sessions.data && nodes.data
      ? launchCandidates(record, { sessions: sessions.data.sessions, items: nodes.data.recent_items, receipts: receipts.data?.receipts ?? [] })
      : null;
  const retry = () => {
    void sessions.refetch();
    void nodes.refetch();
    void receipts.refetch();
  };
  const choice = found?.candidates.find((c) => c.id === picked) ?? null;

  return (
    <div className="mt-2 flex flex-col gap-2">
      <p className="m-0 font-medium">We could not confirm whether this started.</p>
      <p className="m-0">This hub cannot report on a launch attempt yet, so only you can decide. Check the sessions and work it may have created.</p>
      {resendable ? null : <NotResendable />}
      {fetching ? (
        <Skeleton label="Looking for jobs that started around the same time" lines={2} />
      ) : (
        <>
          {failed.length || partial.length ? (
            <div role="alert">
              <p className="m-0">
                Could not check everything:{" "}
                {[...failed.map((q) => describeReadError(q.error).message), ...partial].join("; ")}. A job missing from these lists may still have
                started.
              </p>
              <Button size="sm" className="mt-1" onClick={retry}>
                Retry
              </Button>
            </div>
          ) : null}
          {found ? (
            <>
              <p className="m-0 text-xs text-[var(--ua-muted)]">{found.compared}</p>
              {found.candidates.length ? (
                <RadioGroup
                  legend="These started around the same time and may be it"
                  value={picked}
                  onChange={setPicked}
                  options={found.candidates.map((c) => ({
                    value: c.id,
                    label: (
                      <span>
                        {c.label} (
                        <UiLink href={c.href} className="underline">
                          open
                        </UiLink>
                        )
                      </span>
                    ),
                  }))}
                />
              ) : (
                <p className="m-0">Nothing that started around the same time was found. That does not prove it did not start.</p>
              )}
            </>
          ) : null}
        </>
      )}
      <div className="flex flex-wrap gap-2">
        {found?.candidates.length ? (
          <Button size="sm" disabled={!choice} onClick={() => choice && onResolved({ kind: "started", label: choice.label, href: choice.href, by: "person" })}>
            One of these is it
          </Button>
        ) : null}
        {resendable ? (
          <Button size="sm" onClick={() => setConfirming(true)}>
            I have checked and it did not start. Launch again
          </Button>
        ) : (
          <StartNew record={record} onStartNew={onStartNew} />
        )}
      </div>
      <ConfirmDialog
        open={confirming}
        title="Launch it again?"
        objectName={record.label}
        consequence="If the first launch did start, this starts a second one, and a duplicate session may be created. Launch again only if you have checked."
        confirmLabel="Launch again"
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          onRelaunch(record.key);
        }}
      />
    </div>
  );
}

/** Under a launch control that the guard holds: why, and where to settle it. */
export function LaunchBlockedNote({ reason, resolvable }: { reason: string | null; resolvable: boolean }) {
  if (!reason) return null;
  return (
    <p className="m-0 text-xs text-[var(--ua-muted)]">
      {reason}{" "}
      {resolvable ? (
        <UiLink href="/sessions/new" className="underline">
          Resolve it
        </UiLink>
      ) : null}
    </p>
  );
}
