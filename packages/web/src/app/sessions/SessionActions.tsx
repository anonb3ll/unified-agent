import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AGENTS_QUERY_KEY, fetchAgents } from "../../lib/api/sessions";
import { NODES_QUERY_KEY, type NodeDispatchItem } from "../../lib/api/nodes";
import type { SessionRow } from "../../lib/sessions/types";
import { Button, Combobox, ConfirmDialog, Dialog, Field, RadioGroup, Textarea, UiLink } from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useHubWrite } from "../lib/mutations";
import { sessionQueryKeys, WRITE_PATHS, type NodeStartResult } from "./api";
import { isOpen } from "./model";
import { KeptNewerText, useLatest, useWhoami, WriteStatus } from "./parts";
import { blockedReason, requestFingerprint, resolvableOnStart, useLaunchGuard, type LaunchSpec } from "./launchGuard";
import { LaunchBlockedNote } from "./LaunchResolution";

type Dialogs = "stop" | "resume" | "restart" | "handoff" | "end" | null;

export type ActionContext = {
  session: SessionRow;
  item: NodeDispatchItem | null;
  identity: string | null;
  /** The identity drafts are keyed by: undefined while whoami loads. */
  draftIdentity?: string | null | undefined;
  isOperator: boolean | null;
  hasCrew: boolean;
  onOpenCrew: () => void;
};

/** Why each action is unavailable, in plain words; null when it is available. */
export function actionAvailability({ session, item, identity, isOperator, hasCrew }: Omit<ActionContext, "onOpenCrew" | "draftIdentity">) {
  const open = isOpen(session.status);
  const own = identity !== null && identity === session.agent;
  const stoppable = item !== null && (item.status === "queued" || item.status === "claimed") && !item.cancel_requested;
  const wake = session.wake;
  return {
    stop: stoppable
      ? null
      : item?.cancel_requested
        ? "Stop was already requested; the device stops the agent at its next check."
        : item
          ? "The agent on this device is not running, so there is nothing to stop."
          : "Stop works for sessions started on a device. To close this one, use End session.",
    resume: item?.resume?.available
      ? null
      : item?.restartable
        ? null
        : open
          ? "The session is still open. Send a follow-up instead."
          : item?.resume?.reason
            ? `Resume is not available: ${item.resume.reason}.`
            : "Only sessions that ran on a device can be resumed from here.",
    handoff: null,
    end: !open
      ? "The session has already ended."
      : own
        ? null
        : isOperator
          ? null
          : isOperator === null
            ? "Checking whether you may end this session…"
            : `Only ${session.agent} or the operator can end this session.`,
    wake:
      !wake || (!wake.requested && !wake.granted)
        ? "This session has not asked to be woken when idle."
        : isOperator
          ? null
          : "Only the operator can grant idle wakes.",
    crew: hasCrew ? null : "This session has no crew conversation.",
  };
}

/** The session page actions (spec §9.4): each available one, or why it is not. */
export function SessionActions(ctx: ActionContext) {
  const { session, item, identity, isOperator } = ctx;
  const [dialog, setDialog] = useState<Dialogs>(null);
  const [ended, setEnded] = useState<OperatorEndResult | null>(null);
  const why = actionAvailability(ctx);
  const own = identity !== null && identity === session.agent;
  const invalidates = [sessionQueryKeys.all, NODES_QUERY_KEY];

  const stop = useHubWrite<{ action: "cancel"; item_id: string }>({
    path: WRITE_PATHS.nodeSession,
    label: "Stopping the session",
    invalidates,
    successMessage: "Stop requested. The device stops the agent at its next check.",
  });
  // "Start again" creates a new job: it goes through the launch guard.
  const guard = useLaunchGuard(useWhoami().data);
  const restartKey = useRef("");
  const restart = useHubWrite<{ action: "restart"; item_id: string }, NodeStartResult>({
    path: WRITE_PATHS.nodeSession,
    idempotencyKey: () => restartKey.current,
    label: "Starting the session again",
    invalidates,
    // A restart creates a new job and does not deduplicate (hub task #2498).
    resumable: false,
    successMessage: "Started again as a fresh session.",
  });
  const wake = useHubWrite<{ grant: boolean }>({
    path: WRITE_PATHS.wakeGrant(session.id),
    label: "Changing idle wakes",
    invalidates,
    successMessage: (_d) => (session.wake?.granted ? "Idle wakes revoked." : "Idle wakes granted."),
  });

  const unavailable = (Object.entries(why) as [string, string | null][]).filter(([key, reason]) => reason && key !== "crew" && key !== "wake");
  const wakeVisible = Boolean(session.wake && (session.wake.requested || session.wake.granted));

  const isRestart = item !== null && !item.resume?.available && item.restartable === true;
  const restartSpec: LaunchSpec | undefined = item
    ? {
        op: "restart",
        target: item.node,
        taskIds: item.task_id != null ? [item.task_id] : [],
        label: `Start again: ${session.agent} session ${session.id}`,
        fingerprint: requestFingerprint("restart", { item_id: item.id }),
      }
    : undefined;
  return (
    <div className="flex flex-col gap-2">
      <div role="group" aria-label="Session actions" className="flex flex-wrap gap-2">
        <Button variant="danger" disabled={why.stop !== null} onClick={() => setDialog("stop")}>
          Stop
        </Button>
        <Button
          disabled={why.resume !== null || (isRestart && blockedReason(guard.status, restartSpec) !== null)}
          onClick={() => setDialog(item?.resume?.available ? "resume" : "restart")}
        >
          {item && !item.resume?.available && item.restartable ? "Start again" : "Resume"}
        </Button>
        <Button onClick={() => setDialog("handoff")}>Hand off</Button>
        <Button disabled={why.end !== null} onClick={() => setDialog("end")}>
          End session
        </Button>
        {wakeVisible ? (
          <Button
            disabled={why.wake !== null}
            loading={wake.isRunning}
            onClick={() => void wake.run({ grant: !session.wake?.granted })}
          >
            {session.wake?.granted ? "Revoke idle wakes" : "Grant idle wakes"}
          </Button>
        ) : null}
        {ctx.hasCrew ? <Button onClick={ctx.onOpenCrew}>Open crew view</Button> : null}
      </div>
      {unavailable.length || (wakeVisible && why.wake) ? (
        <ul className="m-0 list-none p-0 text-xs text-[var(--ua-muted)]" aria-label="Why some actions are unavailable">
          {unavailable.map(([key, reason]) => (
            <li key={key}>
              <span className="font-medium">{LABEL[key]}:</span> {reason}
            </li>
          ))}
          {wakeVisible && why.wake ? (
            <li>
              <span className="font-medium">Idle wakes:</span> {why.wake}
            </li>
          ) : null}
        </ul>
      ) : null}
      {session.wake?.requested || session.wake?.granted ? (
        <p className="m-0 text-xs text-[var(--ua-muted)]">
          Idle wakes: {session.wake.granted ? "granted" : "requested, not granted yet"}. Automatic wakes are paused on this hub; inbox notifications
          still reach the agent.
        </p>
      ) : null}
      {isRestart ? (
        <LaunchBlockedNote reason={blockedReason(guard.status, restartSpec)} resolvable={resolvableOnStart(guard.status)} />
      ) : null}
      <WriteStatus state={stop.state} label="Stopping the session" />
      <WriteStatus state={restart.state} label="Starting again" />
      <WriteStatus state={wake.state} label="The idle-wake change" />

      <ConfirmDialog
        open={dialog === "stop"}
        variant="danger"
        title="Stop this session?"
        objectName={`${session.agent} · ${session.id}`}
        consequence="The agent stops at its next check. Work it has not saved may be lost. You can start it again afterwards."
        confirmLabel="Stop session"
        busy={stop.isRunning}
        onCancel={() => setDialog(null)}
        onConfirm={() => {
          setDialog(null);
          if (item) void stop.run({ action: "cancel", item_id: item.id });
        }}
      />
      <ConfirmDialog
        open={dialog === "restart"}
        title="Start this session again?"
        objectName={`${session.agent} · ${session.id}`}
        consequence="This device cannot reopen the old conversation, so the agent starts a fresh session with the same prompt, without its earlier history."
        confirmLabel="Start again"
        busy={restart.isRunning}
        onCancel={() => setDialog(null)}
        onConfirm={() => {
          setDialog(null);
          if (!item || !restartSpec) return;
          const key = guard.begin(restartSpec);
          if (!key) return;
          restartKey.current = key;
          void restart.run({ action: "restart", item_id: item.id }).then((o) => guard.settle(key, o));
        }}
      />
      {dialog === "resume" && item ? <ResumeDialog item={item} sessionId={session.id} identity={ctx.draftIdentity} onClose={() => setDialog(null)} /> : null}
      {dialog === "handoff" ? <HandoffDialog session={session} identity={ctx.draftIdentity} onClose={() => setDialog(null)} /> : null}
      {dialog === "end" ? <EndDialog session={session} own={own} onClose={() => setDialog(null)} onEnded={(r) => setEnded(r)} /> : null}
      {ended?.outstanding ? <StillHeld owner={ended.owner ?? session.agent} outstanding={ended.outstanding} /> : null}
    </div>
  );
}

const LABEL: Record<string, string> = { stop: "Stop", resume: "Resume", end: "End session", handoff: "Hand off" };

function ResumeDialog({ item, sessionId, identity, onClose }: { item: NodeDispatchItem; sessionId: string; identity: string | null | undefined; onClose: () => void }) {
  const draft = useDraft<{ text: string }>({ identity, operation: "session.resume", object: { id: sessionId }, initial: { text: "" } });
  // Clear and close only if the text is still what was sent (review 2, finding 10).
  const latest = useLatest(draft.value);
  const [kept, setKept] = useState(false);
  const write = useHubWrite<{ item_id: string; prompt: string }, NodeStartResult>({
    path: WRITE_PATHS.nodeResume,
    label: "Resuming the session",
    invalidates: [sessionQueryKeys.all, NODES_QUERY_KEY],
    successMessage: "Resumed. Your message is queued.",
  });
  const prompt = draft.value.text.trim();
  return (
    <Dialog
      open
      onClose={onClose}
      title="Resume this session"
      description={`The agent continues the earlier conversation${item.resume?.label ? ` (${item.resume.label})` : ""} with your message.`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!prompt}
            loading={write.isRunning}
            onClick={() =>
              {
                const sent = JSON.stringify(draft.value);
                setKept(false);
                void write.run({ item_id: item.id, prompt }).then((o) => {
                  if (o.kind !== "done" && o.kind !== "accepted") return;
                  if (JSON.stringify(latest.current) === sent) {
                    draft.clear();
                    onClose();
                  } else setKept(true);
                });
              }
            }
          >
            Resume
          </Button>
        </>
      }
    >
      <Field label="Next message" required>
        <Textarea value={draft.value.text} rows={4} onChange={(e) => draft.setValue({ text: e.target.value })} />
      </Field>
      <KeptNewerText shown={kept} />
      <WriteStatus state={write.state} label="Resuming" className="mt-2" />
    </Dialog>
  );
}

/** Hand the session's work to another agent: a delegated task with a note (legacy Dispatch "Agent handoff"). */
export function HandoffDialog({
  session,
  identity,
  onClose,
}: {
  session: SessionRow | null;
  identity: string | null | undefined;
  onClose: () => void;
}) {
  const agents = useQuery({ queryKey: AGENTS_QUERY_KEY, queryFn: ({ signal }) => fetchAgents(signal) });
  const draft = useDraft<{ agent: string; note: string }>({
    identity,
    operation: "session.handoff",
    object: { id: session?.id ?? "" },
    initial: { agent: "", note: "" },
  });
  const latest = useLatest(draft.value);
  const [kept, setKept] = useState(false);
  const write = useHubWrite<{ task_spec: string; to_agent: string; parent_task_id: number | null }, { task_id?: number }>({
    path: WRITE_PATHS.handoff,
    label: "The handoff",
    invalidates: [sessionQueryKeys.all],
    successMessage: (data) => `Handed off to ${draft.value.agent}${data?.task_id ? ` as task #${data.task_id}` : ""}.`,
  });
  const options = (agents.data?.agents ?? [])
    .filter((a) => a.label !== identity && a.label !== session?.agent)
    .map((a) => ({ value: a.label, label: a.label, description: a.status }));
  const note = draft.value.note.trim();
  const spec = session
    ? `${note}\n\nHanded off from session ${session.id} (${session.agent})${session.task_id != null ? `, task #${session.task_id}` : ""}.`
    : note;
  return (
    <Dialog
      open
      onClose={onClose}
      title="Hand off to another agent"
      description="The agent you pick gets a new task with your note. The current session is not stopped."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!note || !draft.value.agent}
            loading={write.isRunning}
            onClick={() =>
              {
                const sent = JSON.stringify(draft.value);
                setKept(false);
                void write.run({ task_spec: spec, to_agent: draft.value.agent, parent_task_id: session?.task_id ?? null }).then((o) => {
                  if (o.kind !== "done" && o.kind !== "accepted") return;
                  if (JSON.stringify(latest.current) === sent) {
                    draft.clear();
                    onClose();
                  } else setKept(true);
                });
              }
            }
          >
            Hand off
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field
          label="Agent"
          required
          hint={agents.isError ? "The agent list could not be loaded. Close this and try again." : agents.isLoading ? "Loading agents…" : undefined}
        >
          <Combobox
            options={options}
            value={draft.value.agent || null}
            onChange={(v) => draft.setValue((prev) => ({ ...prev, agent: v ?? "" }))}
            placeholder="Pick an agent"
            emptyText="No other agent matches"
          />
        </Field>
        <Field label="Note for the agent" required hint="Say what is done, what is left and anything it needs to know.">
          <Textarea value={draft.value.note} rows={5} onChange={(e) => draft.setValue((prev) => ({ ...prev, note: e.target.value }))} />
        </Field>
        <KeptNewerText shown={kept} />
        <WriteStatus state={write.state} label="The handoff" />
      </div>
    </Dialog>
  );
}

const END_OPTIONS = [
  { value: "completed", label: "Finished", description: "The work is done." },
  { value: "failed", label: "Failed", description: "The work could not be done." },
  { value: "abandoned", label: "Abandoned", description: "The work was given up or is no longer needed." },
];

/** What an operator end leaves with the owner (task/2480 contract `outstanding`). */
export type Outstanding = { claimed_task_ids?: number[]; open_delegation_ids?: number[]; note?: string | null };
type OperatorEndResult = { session_id?: string; owner?: string; outstanding?: Outstanding };

function EndDialog({
  session,
  own,
  onClose,
  onEnded,
}: {
  session: SessionRow;
  own: boolean;
  onClose: () => void;
  onEnded: (result: OperatorEndResult) => void;
}) {
  // The operator route defaults to "abandoned" (task/2480 contract); a caller's own end to "completed".
  const [status, setStatus] = useState(own ? "completed" : "abandoned");
  const [summary, setSummary] = useState("");
  const [reason, setReason] = useState("");
  const write = useHubWrite<Record<string, string>, OperatorEndResult>({
    path: own ? WRITE_PATHS.sessionEnd : WRITE_PATHS.sessionOperatorEnd,
    label: "Ending the session",
    invalidates: [sessionQueryKeys.all],
    successMessage: "Session ended.",
    // session/end carries no identity-derived argument (control-center-api.md),
    // so the exact body is the approved call. operator-end adds `operator`
    // from the verified identity, which the stored context pins.
    resumable: true,
  });
  const missingRoute =
    !own && write.state.status === "error" && write.state.httpStatus === 404 && !/no such session/i.test(write.state.detail ?? "");
  const body: Record<string, string> = own
    ? { session_id: session.id, status, ...(summary.trim() ? { summary: summary.trim() } : {}) }
    : { session_id: session.id, reason: reason.trim(), status };
  return (
    <ConfirmDialog
      open
      variant="danger"
      title="End this session?"
      objectName={`${session.agent} · ${session.id}`}
      consequence={
        own
          ? "This ends your session record and its hub credential. A running device job or process is not stopped; stop it first if it should not continue."
          : `As the operator, this ends the session record and its hub credential for ${session.agent}, and records your reason in the audit log. A running device job or process is not stopped, and the agent keeps its task claims and delegations: stop or reassign those separately.`
      }
      confirmLabel="End session"
      confirmDisabled={(!own && !reason.trim()) || missingRoute}
      busy={write.isRunning}
      onCancel={onClose}
      onConfirm={() =>
        void write.run(body).then((o) => {
          if (o.kind === "done" || o.kind === "accepted") {
            if (!own) onEnded(o.data ?? {});
            onClose();
          }
        })
      }
    >
      <div className="flex flex-col gap-3">
        <RadioGroup legend="How did it end?" options={END_OPTIONS} value={status} onChange={setStatus} />
        {own ? (
          <Field label="Summary (optional)" hint="Recorded with the session.">
            <Textarea value={summary} rows={2} maxLength={8000} onChange={(e) => setSummary(e.target.value)} />
          </Field>
        ) : (
          <Field label="Reason" required hint="Recorded in the audit log and on the session.">
            <Textarea value={reason} rows={2} maxLength={2000} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
        {missingRoute ? (
          <p role="alert" className="m-0 text-sm">
            Not available on this hub yet: this hub cannot end another identity's session. Ask {session.agent} to end it, or update the hub.
          </p>
        ) : (
          <WriteStatus state={write.state} label="Ending the session" />
        )}
      </div>
    </ConfirmDialog>
  );
}

/** After an operator end: what the owner still holds, with links (review 2, finding 11). */
function StillHeld({ owner, outstanding }: { owner: string; outstanding: Outstanding }) {
  const tasks = outstanding.claimed_task_ids ?? [];
  const delegations = outstanding.open_delegation_ids ?? [];
  if (!tasks.length && !delegations.length) return null;
  return (
    <section aria-label={`Still held by ${owner}`} className="rounded-md border border-[var(--ua-border)] p-3 text-sm">
      <p className="m-0 font-medium">The session is ended, but {owner} still holds:</p>
      <ul className="m-0 mt-1 list-disc pl-5">
        {tasks.map((id) => (
          <li key={`t-${id}`}>
            <UiLink href={`/work/tasks/${id}`} className="underline">
              Task #{id}
            </UiLink>{" "}
            (claimed)
          </li>
        ))}
        {delegations.map((id) => (
          <li key={`d-${id}`}>Delegation #{id} (open)</li>
        ))}
      </ul>
      <p className="m-0 mt-1 text-[var(--ua-muted)]">Release or reassign them from the task if the work should move on.</p>
    </section>
  );
}
