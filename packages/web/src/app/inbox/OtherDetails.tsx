import { useEffect, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ATTENTION_QUERY_KEY } from "../../lib/api/attention";
import { PROPOSALS_QUERY_KEY, type ProposalRow } from "../../lib/api/proposals";
import type { AttentionItem } from "../../lib/attention/types";
import { fetchThread } from "../../lib/messages/api";
import {
  Badge,
  Button,
  CodeBlock,
  ConfirmDialog,
  DescriptionList,
  Field,
  Input,
  LinkButton,
  RadioGroup,
  Select,
  Textarea,
  UiLink,
} from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import {
  fetchOrgProposal,
  fetchSessionDetail,
  ORG_PROPOSALS_QUERY_KEY,
  orgProposalQueryKey,
  PATHS,
  sessionDetailQueryKey,
} from "./api";
import { DetailHeader, DetailRead, DetailSection, PlainText } from "./DetailParts";
import { ago, elapsed, humanizeKind, KIND_LABEL, type InboxItem } from "./model";
import { ReplyForm } from "./ReplyForm";
import { useDraftedWrite } from "./draftedWrite";
import { AttemptNotice } from "./AttemptNotice";
import { WriteStatus } from "./WriteStatus";

const taskHref = (id: number | string) => `/work/tasks/${encodeURIComponent(String(id))}`;
const sessionHref = (id: string) => `/sessions/${encodeURIComponent(id)}`;
const agentHref = (label: string) => `/agents/profile/${encodeURIComponent(label)}`;

/** A session asking to be woken when mail arrives while it is idle (task #2050). */
export function WakeDetail({ item }: { item: Extract<InboxItem, { kind: "wake_grant" }> }) {
  const refuseReasonId = useId();
  const session = useQuery({
    queryKey: sessionDetailQueryKey(item.sessionId),
    queryFn: ({ signal }) => fetchSessionDetail(item.sessionId, signal),
    retry: 1,
  });
  const grant = useHubWrite<{ grant: boolean }>({
    path: PATHS.wakeGrant(item.sessionId),
    label: "Grant wakes",
    resumable: false,
    invalidates: [ATTENTION_QUERY_KEY, ["sessions"], sessionDetailQueryKey(item.sessionId)],
  });
  return (
    <div className="flex flex-col gap-4">
      <DetailHeader kind={KIND_LABEL.wake_grant} title={item.title} status="Waiting" statusVariant="warning" />
      <p className="m-0 text-sm">
        The session asked to be woken when a message arrives while it is idle. Only you can allow that, because the agent
        could otherwise wake itself.
      </p>
      <DetailSection title="Session">
        <DetailRead query={session} label="session">
          {(s) => (
            <DescriptionList
              items={[
                { term: "Agent", value: <UiLink className="underline" href={agentHref(s.agent)}>{s.agent}</UiLink> },
                { term: "Running for", value: elapsed(s.started_at) || null },
                { term: "Task", value: s.task_id ? <UiLink className="underline" href={taskHref(s.task_id)}>{s.task_title || `Task ${s.task_id}`}</UiLink> : null },
                { term: "Machine", value: s.host ?? null },
              ]}
            />
          )}
        </DetailRead>
      </DetailSection>
      <DetailSection title="Decide">
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" loading={grant.isRunning} onClick={() => void grant.run({ grant: true })}>
            Grant wakes
          </Button>
          <Button variant="secondary" disabled aria-describedby={refuseReasonId}>
            Refuse
          </Button>
          <LinkButton href={sessionHref(item.sessionId)}>Open the session</LinkButton>
        </div>
        <p id={refuseReasonId} className="m-0 text-sm">
          Refuse: not available on this hub yet. The hub cannot decline a wake request. An ungranted request simply stays
          here, and the session still gets its mail the next time it checks.
        </p>
        <p className="m-0 text-xs text-[var(--ua-muted)]">
          Automatic wakes stay off on this hub until they can keep an unsent draft safe; inbox checks still run.
        </p>
        <WriteStatus state={grant.state} doneText="Wakes granted for this session." />
      </DetailSection>
    </div>
  );
}

/** An agent's question that waits for your answer. The body is agent text: plain text only. */
export function ReplyDetail({ item }: { item: Extract<InboxItem, { kind: "operator_reply" }> }) {
  const conversationId = item.conversationId;
  const thread = useQuery({
    queryKey: ["cc2", "thread-head", conversationId ?? ""],
    queryFn: ({ signal }) => fetchThread(conversationId as string, null, signal),
    enabled: Boolean(conversationId),
    retry: 1,
  });
  return (
    <div className="flex flex-col gap-4">
      <DetailHeader kind={KIND_LABEL.operator_reply} title={item.title} status="Waiting" statusVariant="warning" meta={item.since ? <span>{ago(item.since)}</span> : null} />
      {!conversationId ? (
        <p className="m-0 text-sm">The hub did not say which conversation this is in. Open Messages to find it.</p>
      ) : (
        <DetailRead query={thread} label="question">
          {(page) => {
            const message = page.messages.find((m) => m.message_id === item.messageId);
            if (!message) {
              return <p className="m-0 text-sm">The question is older than the newest page of this conversation. Open the conversation to read it.</p>;
            }
            return (
              <>
                <DetailSection title={`${message.from_agent} asks`}>
                  <div className="rounded border border-[var(--ua-border)] bg-[var(--ua-surface)] p-3">
                    {message.body_pruned ? <p className="m-0 text-sm text-[var(--ua-muted)]">The text was removed by the hub's retention rules.</p> : <PlainText text={message.body ?? ""} />}
                  </div>
                  <p className="m-0 text-xs text-[var(--ua-muted)]">
                    {humanizeKind(message.act)} · {ago(message.created_at)}
                    {message.from_session ? ` · from session ${message.from_session.slice(0, 8)}` : ""}
                  </p>
                </DetailSection>
                <DetailSection title="Reply">
                  <ReplyForm messageId={message.message_id} toAgent={message.from_agent} />
                </DetailSection>
              </>
            );
          }}
        </DetailRead>
      )}
      {conversationId && (
        <LinkButton size="sm" href={`/inbox/messages?conversation=${encodeURIComponent(conversationId)}`}>
          Open the whole conversation
        </LinkButton>
      )}
    </div>
  );
}

const HOLD_TEXT: Record<"operator_task" | "red_tier_hold" | "held_pr", string> = {
  operator_task: "This task is in the queue for a person only. Agents do not pick it up.",
  red_tier_hold:
    "The work is finished and its checks passed, but it changes protected files (the server, safety rules, deploy scripts and similar) that a person must look at before it merges.",
  held_pr: "A pull request is ready and waits for its merge decision.",
};

type TaskWrite = { id: number; append_note: string; status?: "done" };
type HoldForm = { note: string };

/**
 * A hold or a task only a person can do. The hub has no operation that
 * releases a hold (hub task #2498), so this page offers what exists: a note
 * on the task for whoever merges it, and closing an operator task. Each
 * request is built once and kept in a draft, so a repeat after an approval
 * is the identical call.
 */
export function HoldDetail({ item }: { item: Extract<InboxItem, { kind: "operator_task" | "red_tier_hold" | "held_pr" }> }) {
  const isTask = item.kind === "operator_task";
  const write = useDraftedWrite<HoldForm, TaskWrite>({
    operation: "inbox.hold",
    object: { kind: item.kind, task: item.taskId },
    initial: { note: "" },
    write: {
      path: PATHS.task,
      label: isTask ? "Close task" : "Note for the merge actor",
      resumable: false,
      invalidates: [ATTENTION_QUERY_KEY, ["queue-health"], ["tasks"]],
    },
  });
  const [confirmDone, setConfirmDone] = useState(false);
  const [last, setLast] = useState<{ kind: "note" | "done"; note: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const note = write.form.note.trim();
  // A kept request is either a note or a close; each button repeats only its own.
  const keptNote = write.pendingBody !== null && write.pendingBody.status === undefined;

  const addNote = async () => {
    if (!keptNote && !note) return setError("Write the note first.");
    setError(null);
    setLast({ kind: "note", note });
    await write.submit(
      () => ({ id: item.taskId, append_note: `[operator-note @ ${new Date().toISOString()}] ${note}` }),
      // A kept close is never sent by "Add a note": only a kept note is repeated.
      (kept) => kept.id === item.taskId && kept.status === undefined,
    );
  };
  const markDone = async () => {
    setLast({ kind: "done", note });
    const outcome = await write.submit(
      () => ({
        id: item.taskId,
        status: "done",
        append_note: `[operator-done @ ${new Date().toISOString()}] Closed by the operator from the Inbox.${note ? ` Note: ${note}` : ""}`,
      }),
      // A kept note is never sent by "Mark done": only a kept close is repeated.
      (kept) => kept.id === item.taskId && kept.status === "done",
    );
    if (outcome && outcome.kind !== "error") setConfirmDone(false);
  };

  return (
    <div className="flex flex-col gap-4">
      <DetailHeader kind={KIND_LABEL[item.kind]} title={item.title} status="Waiting" statusVariant="warning" meta={<span>Task {item.taskId}</span>} />
      <p className="m-0 text-sm">{HOLD_TEXT[item.kind]}</p>
      <div className="flex flex-wrap gap-2">
        <LinkButton href={taskHref(item.taskId)}>Open task {item.taskId}</LinkButton>
      </div>
      {!isTask && (
        <p className="m-0 rounded border border-[var(--ua-border-strong)] px-3 py-2 text-sm">
          This hub cannot release a hold yet. You can add a note to the task for whoever merges it. A note does not
          release the hold, does not start a merge, and the item stays in the Inbox.
        </p>
      )}
      <DetailSection title={isTask ? "Close it" : "Note"}>
        <Field
          label={isTask ? "Note for the task" : "Note for the merge actor"}
          hint={keptNote ? "This is the note waiting to be added. Change it to write a new one." : isTask ? "Optional. Recorded on the task." : "Recorded on the task, with the time."}
          error={error ?? undefined}
        >
          <Textarea rows={2} value={write.form.note} maxLength={1000} onChange={(e) => write.setForm({ note: e.target.value })} />
        </Field>
        <div className="flex flex-wrap gap-2">
          {isTask ? (
            <Button variant="primary" onClick={() => setConfirmDone(true)}>
              Mark done…
            </Button>
          ) : (
            <Button variant="primary" loading={write.isRunning} onClick={() => void addNote()}>
              Add a note for the merge actor
            </Button>
          )}
        </div>
        <WriteStatus
          state={write.state}
          doneText={
            last?.kind === "done"
              ? "The task is closed."
              : `Note added to task ${item.taskId}: “${last?.note ?? ""}”. The hold stays in the Inbox until the hub reports it resolved.`
          }
        />
      </DetailSection>
      <ConfirmDialog
        open={confirmDone}
        title="Mark this task done?"
        objectName={`Task ${item.taskId}: ${item.title}`}
        consequence="The task closes. If the hub needs evidence before closing, it says so and nothing changes."
        confirmLabel="Mark done"
        busy={write.isRunning}
        onConfirm={() => void markDone()}
        onCancel={() => setConfirmDone(false)}
      />
    </div>
  );
}

/**
 * A proposed change to the organization (teams, roles, routing), read from
 * `GET /api/org-proposals/{id}`. The hub has no route to review, apply or
 * reject one from the browser yet (hub task #2498), so the decisions are
 * shown disabled with that reason instead of being offered.
 */
export function OrgProposalDetail({ item }: { item: Extract<InboxItem, { kind: "org_proposal" }> }) {
  const query = useQuery({
    queryKey: orgProposalQueryKey(item.proposal.id),
    queryFn: ({ signal }) => fetchOrgProposal(item.proposal.id, signal),
    initialData: item.proposal,
    retry: 1,
  });
  const reasonId = useId();
  const p = query.data ?? item.proposal;
  const open = p.status === "proposed" || p.status === "reviewed";
  return (
    <div className="flex flex-col gap-4">
      <DetailHeader kind={KIND_LABEL.org_proposal} title={p.title} status={humanizeKind(p.status)} meta={<span>from {p.author}</span>} />
      <DescriptionList
        items={[
          { term: "Kind of change", value: humanizeKind(p.proposal_type) },
          { term: "Applies to", value: p.scope },
          { term: "Proposed", value: ago(p.created_at) || null },
          { term: "Reviewed", value: p.reviewed_by ? `by ${p.reviewed_by} ${ago(p.reviewed_at)}` : null },
        ]}
      />
      <DetailSection title="Why">
        <PlainText text={p.rationale || "No reason given."} />
      </DetailSection>
      <DetailSection title="Proposed changes">
        <CodeBlock label="Proposed changes" code={JSON.stringify(p.proposed_changes, null, 2)} language="json" />
      </DetailSection>
      {open && (
        <DetailSection title="Decide">
          <div className="flex flex-wrap gap-2">
            {p.status === "proposed" && (
              <Button variant="primary" disabled aria-describedby={reasonId}>
                Mark reviewed
              </Button>
            )}
            {p.status === "reviewed" && (
              <Button variant="primary" disabled aria-describedby={reasonId}>
                Apply
              </Button>
            )}
            <Button variant="danger" disabled aria-describedby={reasonId}>
              Reject
            </Button>
          </div>
          <p id={reasonId} className="m-0 text-sm">
            Not available on this hub yet: the hub cannot take an organization decision from the Control Center. It is
            listed here so you can read it; nothing you do here changes it.
          </p>
        </DetailSection>
      )}
    </div>
  );
}

/** A session-learning or skill-cluster note proposed for memory: file it as a task or dismiss it. */
export function MemoryProposalDetail({ item }: { item: Extract<InboxItem, { kind: "memory_proposal" }> }) {
  const row: ProposalRow = item.proposal;
  const [confirm, setConfirm] = useState(false);
  const [last, setLast] = useState<"file_task" | "dismiss" | null>(null);
  // What the Dismiss confirmation was opened on; a refetch that changes the proposal withdraws it.
  const snapshot = JSON.stringify(row);
  const [confirmBasis, setConfirmBasis] = useState<string | null>(null);
  const [changedWhileOpen, setChangedWhileOpen] = useState(false);
  useEffect(() => {
    if (confirm && confirmBasis !== null && confirmBasis !== snapshot) {
      setConfirm(false);
      setConfirmBasis(null);
      setChangedWhileOpen(true);
    }
  }, [confirm, confirmBasis, snapshot]);
  // The reason and the exact request live in a draft (spec §6), kept across "Open approval" and back.
  const act = useDraftedWrite<{ reason: string }, { action: "file_task" | "dismiss"; proposal_id: string; reason?: string }, { task_id?: number }>({
    operation: "inbox.memory-proposal",
    object: { proposal: String(row.id) },
    initial: { reason: "" },
    write: { path: PATHS.memoryProposal, label: "Memory proposal decision", resumable: false, invalidates: [PROPOSALS_QUERY_KEY] },
    // Filing creates a task: never resent blindly after a lost answer.
    attemptLabel: "Your decision on this proposal",
  });
  const reason = act.form.reason;
  const setReason = (value: string) => act.setForm({ reason: value });
  const run = async (action: "file_task" | "dismiss") => {
    setLast(action);
    await act.submit(
      (form) => ({ action, proposal_id: String(row.id), ...(form.reason.trim() ? { reason: form.reason.trim() } : {}) }),
      (pending) => pending.action === action,
    );
    setConfirm(false);
  };
  return (
    <div className="flex flex-col gap-4">
      <DetailHeader kind={KIND_LABEL.memory_proposal} title={item.title} meta={row.kind ? <span>{humanizeKind(row.kind)}</span> : null} />
      <DescriptionList
        items={[
          { term: "Source note", value: row.path ? <span className="break-all font-mono">{row.path}</span> : null },
          { term: "Written", value: row.source_date ?? null },
          { term: "Read into memory", value: row.ingest_status ? humanizeKind(row.ingest_status) : null },
        ]}
      />
      {row.error ? (
        <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">This proposal could not be read: {row.error}. Fix the source note before filing it.</p>
      ) : (
        <DetailSection title="What it says">
          <div className="max-h-80 overflow-auto rounded border border-[var(--ua-border)] p-3">
            <PlainText text={(row.content ?? "").trim() || "No text."} max={6000} />
          </div>
        </DetailSection>
      )}
      {row.filing_preview && (
        <DetailSection title="The task it would create">
          <p className="m-0 font-medium">{row.filing_preview.task_title}</p>
          {row.filing_preview.task_notes && <PlainText text={row.filing_preview.task_notes} max={2000} />}
        </DetailSection>
      )}
      <DetailSection title="Decide">
        <Field label="Reason" hint="Optional. Recorded with your decision.">
          <Input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
        </Field>
        {changedWhileOpen && (
          <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">
            The proposal changed while you were deciding; review it again.
          </p>
        )}
        {act.unknownAttempt ? (
          <AttemptNotice attempt={act.unknownAttempt} onMade={act.attemptWasMade} onNotMade={act.attemptNotMade} />
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" disabled={Boolean(row.error)} loading={act.isRunning && last === "file_task"} onClick={() => void run("file_task")}>
              File as a task
            </Button>
            <Button
              onClick={() => {
                setConfirmBasis(snapshot);
                setChangedWhileOpen(false);
                setConfirm(true);
              }}
            >
              Dismiss…
            </Button>
          </div>
        )}
        <WriteStatus state={act.state} doneText={(d) => (last === "dismiss" ? "Dismissed." : d.task_id ? `Filed as task ${d.task_id}.` : "Filed as a task.")} />
      </DetailSection>
      <ConfirmDialog
        open={confirm}
        title="Dismiss this proposal?"
        objectName={item.title}
        consequence="It leaves the Inbox. The source note stays in the vault, unchanged."
        confirmLabel="Dismiss"
        busy={act.isRunning}
        onConfirm={() => void run("dismiss")}
        onCancel={() => setConfirm(false)}
      />
    </div>
  );
}

const SEVERITY: Record<string, "danger" | "warning" | "info" | "neutral"> = {
  critical: "danger",
  high: "danger",
  medium: "warning",
  low: "info",
  info: "neutral",
};

/** Something the hub noticed. No write here: each alert links to the place that fixes it. */
export function AlertDetail({ item }: { item: Extract<InboxItem, { kind: "alert" }> }) {
  const alert: AttentionItem = item.alert;
  const source = alert.source ?? {};
  const taskId = typeof source.task_id === "number" ? source.task_id : null;
  const approvalId = typeof source.approval_id === "number" ? source.approval_id : null;
  const agent = typeof source.agent_label === "string" ? source.agent_label : null;
  const sessionId = typeof source.session_id === "string" ? source.session_id : null;
  const age = typeof alert.age_s === "number" ? elapsed(new Date(Date.now() - alert.age_s * 1000).toISOString()) : "";
  return (
    <div className="flex flex-col gap-4">
      <DetailHeader
        kind={item.waiting ? KIND_LABEL.alert : "For your information"}
        title={item.title}
        status={humanizeKind(String(alert.severity || "info"))}
        statusVariant={SEVERITY[String(alert.severity)] ?? "neutral"}
      />
      <DescriptionList
        items={[
          { term: "What", value: humanizeKind(alert.kind) },
          { term: "Why", value: alert.reason ?? null },
          { term: "For", value: age || null },
          { term: "Machine", value: alert.source_host ?? null },
          { term: "Worktree", value: typeof source.worktree === "string" ? <span className="break-all font-mono">{source.worktree}</span> : null },
        ]}
      />
      <div className="flex flex-wrap gap-2">
        {taskId && <LinkButton href={taskHref(taskId)}>Open task {taskId}</LinkButton>}
        {approvalId && <LinkButton href={`/inbox?kind=approval&item=${approvalId}`}>Open approval {approvalId}</LinkButton>}
        {sessionId && <LinkButton href={sessionHref(sessionId)}>Open the session</LinkButton>}
        {agent && <LinkButton href={agentHref(agent)}>Open {agent}</LinkButton>}
        {!taskId && !approvalId && !agent && !sessionId && <LinkButton href="/settings/health">Open Health</LinkButton>}
      </div>
      {!item.waiting && <p className="m-0 text-sm text-[var(--ua-muted)]">This does not wait on you; it is listed so you know.</p>}
    </div>
  );
}
