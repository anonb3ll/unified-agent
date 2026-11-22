/**
 * `/work/tasks/:id`: one task, edited in place, with its subtasks,
 * dependencies, activity, comments, evidence and work products (spec §9.2).
 * Every `POST /api/action/task` field is reachable from this page.
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router";
import {
  Badge,
  Button,
  Checkbox,
  Combobox,
  ConfirmDialog,
  DescriptionList,
  Dialog,
  Field,
  Input,
  LinkButton,
  Markdown,
  Menu,
  PageHeader,
  Select,
  Switch,
  Textarea,
  UiLink,
} from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useHubWrite } from "../lib/mutations";
import { describeReadError, QueryRegion } from "../lib/queries";
import { ErrorState, Skeleton } from "../../lib/ui";
import { fetchTask, WORK_KEYS, WRITE_PATHS, type TaskRow } from "./api";
import { EditableField, useTaskUpdate } from "./EditableField";
import {
  ago,
  dateInputValue,
  dueIso,
  evidenceUrl,
  PRIORITY_LABEL,
  priorityLabel,
  queueLabel,
  STATUS_ORDER,
  statusLabel,
  tasksFromBoard,
} from "./model";
import { isGone, newKey, RetainedReadNotice, updateDraftAt, useLatest, useReconcileParkedDraft, Section, useBoard, useIdentity, WorkTabs, WriteFeedback } from "./shared";
import { useEpoch } from "../lib/epoch";
import { ActivitySection, CommentsSection, DependenciesSection, EvidenceSection, GoalSection, HoldSection, SubtasksSection } from "./TaskSections";

export function TaskPage() {
  const { id = "" } = useParams();
  const query = useQuery({ queryKey: WORK_KEYS.task(id), queryFn: ({ signal }) => fetchTask(id, signal), staleTime: 5_000 });
  const task = query.data?.task;

  if (!task) {
    const { message, detail } = describeReadError(query.error);
    const notFound = (query.error as { status?: number } | null)?.status === 404;
    return (
      <>
        <PageHeader title={`Task ${id}`} tabs={<WorkTabs />} />
        {query.status === "pending" ? (
          <Skeleton label="Loading the task" lines={6} />
        ) : (
          <ErrorState
            title={notFound ? "This task does not exist" : "The task could not be loaded"}
            message={notFound ? "No task has this number, or it was deleted. Go back to the task list to find it." : message}
            detail={detail}
            onRetry={() => void query.refetch()}
          />
        )}
      </>
    );
  }
  if (isGone(query)) {
    return (
      <>
        <PageHeader title={task.title} meta={`Task #${task.id}`} tabs={<WorkTabs />} />
        <ErrorState
          title="This task no longer exists"
          message="The hub no longer has this task. It was deleted, perhaps by an approved delete. Its subtasks went with it."
          retryLabel="Check again"
          onRetry={() => void query.refetch()}
        />
        <p className="mt-3 text-sm">
          <UiLink href="/work" className="text-[var(--ua-link)] underline">
            Back to all tasks
          </UiLink>
        </p>
      </>
    );
  }
  return (
    <>
      <RetainedReadNotice query={query} what="This task" />
      <TaskDetail task={task} uri={query.data?.uri ?? ""} />
    </>
  );
}

function TaskDetail({ task, uri }: { task: TaskRow; uri: string }) {
  const identity = useIdentity();
  const navigate = useNavigate();
  const board = useBoard();
  const allTasks = useMemo(() => (board.data ? tasksFromBoard(board.data) : []), [board.data]);
  const [confirm, setConfirm] = useState<null | "delete" | "archive">(null);
  const [releaseOpen, setReleaseOpen] = useState(false);
  const archived = Boolean(task.archived_at);
  const [deleteCheck, setDeleteCheck] = useState("");
  const descendants = useMemo(() => {
    const out: typeof allTasks = [];
    const visit = (id: number) => {
      for (const child of allTasks.filter((t) => t.parentId === id)) {
        out.push(child);
        visit(child.id);
      }
    };
    visit(task.id);
    return out;
  }, [allTasks, task.id]);

  const [heldBy, setHeldBy] = useState<SafeWait | null>(null);
  const claim = useHubWrite<{ id: number }, ClaimResult>({
    path: WRITE_PATHS.taskClaim,
    invalidates: [["work"]],
    label: `Claiming task #${task.id}`,
    // A claim that loses answers 200 with claimed: false (control-center-api.md): no success toast.
    successMessage: (data) => (data && data.claimed === false ? "" : `You claimed task #${task.id}.`),
  });
  const runClaim = async () => {
    setHeldBy(null);
    const outcome = await claim.run({ id: task.id });
    if (outcome.kind === "done" && outcome.data?.claimed === false) setHeldBy(outcome.data.safe_wait ?? { owner: null });
  };
  const archive = useTaskUpdate(task, archived ? `Restoring task #${task.id}` : `Archiving task #${task.id}`, archived ? "Task restored." : "Task archived.");
  const remove = useHubWrite<{ id: number }, unknown>({
    path: WRITE_PATHS.taskDelete,
    invalidates: [["work"]],
    label: `Deleting task #${task.id}`,
    successMessage: `Task #${task.id} deleted.`,
  });

  const mine = identity !== null && task.claimed_by === identity;

  return (
    <>
      <PageHeader
        meta={
          <span className="flex flex-wrap items-center gap-2">
            <span>Task #{task.id}</span>
            <Badge size="sm" variant="info">
              {statusLabel(task.status)}
            </Badge>
            {archived && (
              <Badge size="sm" variant="warning">
                Archived
              </Badge>
            )}
          </span>
        }
        title={task.title}
        description={task.claimed_by ? `Claimed by ${task.claimed_by}${task.claimed_at ? `, ${ago(task.claimed_at)}` : ""}.` : "Nobody has claimed this task yet."}
        actions={
          <>
            {!task.claimed_by && (
              <Button variant="primary" loading={claim.isRunning} onClick={() => void runClaim()}>
                Claim
              </Button>
            )}
            {task.claimed_by && (
              <Button onClick={() => setReleaseOpen(true)}>{mine ? "Release my claim" : "Release claim"}</Button>
            )}
            <LinkButton href={`/sessions/new?task=${task.id}`}>Send to an agent</LinkButton>
            <Menu
              label="More"
              aria-label="More task actions"
              align="end"
              items={[
                { id: "subtask", label: "Add a subtask", href: `/work/tasks/new?parent=${task.id}` },
                { id: "archive", label: archived ? "Restore from archive" : "Archive", onSelect: () => setConfirm("archive") },
                { kind: "separator", id: "sep" },
                { id: "delete", label: "Delete", onSelect: () => setConfirm("delete") },
              ]}
            />
          </>
        }
        tabs={<WorkTabs />}
      />
      <div className="flex flex-col gap-2">
        <WriteFeedback
          state={claim.state}
          what="The claim"
          onRun={(result) => {
            // A claim run from the notice answers like a direct one: 200 with claimed: false when another agent holds it.
            const data = result.kind === "done" ? (result.data as ClaimResult | undefined) : undefined;
            if (data?.claimed === false) setHeldBy(data.safe_wait ?? { owner: null });
          }}
        />
        {heldBy && <HeldNotice wait={heldBy} />}
        <WriteFeedback state={archive.state} what={archived ? "Restoring the task" : "Archiving the task"} />
        <WriteFeedback state={remove.state} what="Deleting the task" />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-4">
          <Section title="Description" id="task-description">
            <EditableField<string>
              task={task}
              name="title"
              label="Title"
              display={<p className="m-0 font-medium">{task.title}</p>}
              initial={task.title}
              editor={({ value, onChange, id }) => <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} />}
              toBody={(value) => ({ title: value.trim() })}
            />
            <EditableField<string>
              task={task}
              name="notes"
              label="Description"
              display={task.notes ? <Markdown source={task.notes} /> : <p className="m-0 text-sm text-[var(--ua-muted)]">No description yet.</p>}
              initial={task.notes ?? ""}
              editor={({ value, onChange, id }) => <Textarea id={id} rows={10} value={value} onChange={(e) => onChange(e.target.value)} />}
              toBody={(value) => ({ notes: value })}
            />
            <AddNote task={task} identity={identity} />
          </Section>
          <SubtasksSection task={task} tasks={allTasks} />
          <DependenciesSection task={task} tasks={allTasks} />
          <ActivitySection task={task} />
          <EvidenceSection task={task} identity={identity} />
          <CommentsSection task={task} identity={identity} />
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <StatusSection task={task} />
          <Section title="Details" id="task-details">
            <DetailFields task={task} tasks={allTasks} />
          </Section>
          <GoalSection task={task} />
          <HoldSection task={task} />
          <Section title="Record" id="task-record">
            <DescriptionList
              items={[
                { term: "Created", value: task.created_at ? ago(task.created_at) : null },
                { term: "Updated", value: task.updated_at ? ago(task.updated_at) : null },
                { term: "Completed", value: task.completed_at ? ago(task.completed_at) : null },
                { term: "Branch", value: task.branch },
                { term: "Lock ends", value: task.lock_expires_at ? ago(task.lock_expires_at) : null },
                { term: "Platform address", value: uri ? <code className="break-all text-xs">{uri}</code> : null },
              ]}
            />
          </Section>
        </div>
      </div>
      <ReleaseClaimDialog open={releaseOpen} task={task} onClose={() => setReleaseOpen(false)} />
      <ConfirmDialog
        open={confirm === "archive"}
        title={archived ? "Restore this task from the archive?" : "Archive this task?"}
        objectName={`#${task.id} ${task.title}`}
        consequence={archived ? "It comes back into lists and views." : "It leaves the lists and views but is kept, and you can restore it here."}
        confirmLabel={archived ? "Restore" : "Archive"}
        busy={archive.isRunning}
        onConfirm={async () => {
          const body = { id: task.id, archived: !archived };
          const outcome = await archive.run(body);
          // A cancelled write shows nothing and changes nothing: the dialog stays as it was.
          if (outcome.kind !== "error" && outcome.kind !== "cancelled") setConfirm(null);
          else setConfirm(null);
        }}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm === "delete"}
        variant="danger"
        // No hub read lists a task's whole subtree (the board omits archived and older done tasks,
        // and can be partial), and task_delete removes every descendant: the person types the
        // task number to confirm (Needs from hub: a complete subtree read).
        title="Delete this task and every task under it?"
        objectName={`#${task.id} ${task.title}`}
        consequence="The task and every task under it, with notes and links, are removed for everyone, and agent sessions working on them lose their credentials. This cannot be undone. Archive it instead if you may need it later."
        confirmLabel="Delete the task and its subtree"
        confirmDisabled={deleteCheck.trim() !== String(task.id)}
        busy={remove.isRunning}
        onConfirm={async () => {
          if (deleteCheck.trim() !== String(task.id)) return;
          const outcome = await remove.run({ id: task.id });
          if (outcome.kind === "cancelled") return;
          setConfirm(null);
          setDeleteCheck("");
          if (outcome.kind === "done") navigate("/work");
        }}
        onCancel={() => {
          setConfirm(null);
          setDeleteCheck("");
        }}
      >
        <div className="mt-2 flex flex-col gap-2 text-sm">
          <p className="m-0">
            {board.status === "success"
              ? `At least ${descendants.length} subtask${descendants.length === 1 ? "" : "s"} known from the current work. This list may not list every task: archived tasks and tasks done more than 30 days ago are not shown, and all of them are deleted too.`
              : "The subtasks could not be read, so this may not list every task under it. All of them are deleted."}
          </p>
          {descendants.length > 0 && (
            <ul aria-label="Subtasks that are deleted too" className="m-0 flex max-h-48 list-none flex-col gap-0.5 overflow-auto p-0">
              {descendants.map((d) => (
                <li key={d.id}>
                  #{d.id} {d.title}
                </li>
              ))}
            </ul>
          )}
          <Field label={`Type ${task.id} to confirm`}>
            <Input inputMode="numeric" autoComplete="off" value={deleteCheck} onChange={(e) => setDeleteCheck(e.target.value)} />
          </Field>
        </div>
      </ConfirmDialog>
    </>
  );
}

type SafeWait = { owner?: string | null; waiting_on?: string | null; until?: string | null; generation?: number; claimed_by?: string | null };
type ClaimResult = { id?: number; claimed?: boolean; claimed_by?: string | null; safe_wait?: SafeWait };

/** The claim did not happen: someone else holds the task (task_claim's safe_wait). */
function HeldNotice({ wait }: { wait: SafeWait }) {
  const who = wait.claimed_by ?? wait.owner ?? "Another agent";
  return (
    <div role="alert" className="rounded-md border border-[var(--ua-warn)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
      <p className="m-0 font-medium">You did not get this task. {who} holds this task.</p>
      <p className="m-0 mt-0.5 text-[var(--ua-muted)]">
        {wait.waiting_on ? `It is waiting on ${wait.waiting_on}. ` : ""}
        {wait.until ? `The hold lasts until ${new Date(wait.until).toISOString().replace("T", " ").slice(0, 16)} UTC (${ago(wait.until)}). ` : ""}
        Nothing was changed. Try again after the hold ends, or ask {who} to release it.
      </p>
    </div>
  );
}

function StatusSection({ task }: { task: TaskRow }) {
  // The chosen status and the task version it was chosen against are one task-scoped draft
  // (review round 3, item 5): a refresh never moves the base under the choice.
  const draft = useDraft<{ status: string; worktree: boolean; basedOn: string | null; seenStatus: string }>({
    identity: useIdentity(),
    operation: "task.status",
    object: { id: task.id },
    initial: { status: String(task.status), worktree: true, basedOn: task.updated_at ?? null, seenStatus: String(task.status) },
  });
  const d = draft.value;
  const write = useTaskUpdate(task, `Changing the status of task #${task.id}`, "Status changed.");
  const status = d.status;
  const changed = status !== task.status;
  const choose = (next: string) =>
    next === task.status ? draft.clear() : draft.setValue((p) => ({ ...p, status: next, ...(p.status === String(task.status) ? { basedOn: task.updated_at ?? null, seenStatus: String(task.status) } : {}) }));
  // Someone else changed the task after the choice was made.
  const movedUnderneath = changed && d.basedOn !== null && task.updated_at !== undefined && task.updated_at !== null && d.basedOn !== task.updated_at;
  return (
    <Section title="Status" id="task-status">
      <Field label="Status" hint={`Now: ${statusLabel(task.status)}`}>
        <Select value={status} onChange={(e) => choose(e.target.value)} options={STATUS_ORDER.map((s) => ({ value: s, label: statusLabel(s) }))} />
      </Field>
      {movedUnderneath && (
        <div role="alert" className="flex flex-col gap-2 rounded-md border border-[var(--ua-warn)] px-3 py-2 text-sm">
          <p className="m-0">
            <strong>This task changed since you chose {statusLabel(status)}.</strong> It was {statusLabel(d.seenStatus)}, now {statusLabel(task.status)}. Saving
            now is refused until you choose.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => draft.setValue((p) => ({ ...p, basedOn: task.updated_at ?? null, seenStatus: String(task.status) }))}>
              Keep {statusLabel(status)} over the change
            </Button>
            <Button size="sm" variant="ghost" onClick={() => draft.clear()}>
              Keep it {statusLabel(task.status)}
            </Button>
          </div>
        </div>
      )}
      {status === "doing" && changed && (
        <Checkbox
          label="Create a worktree for this task"
          description="A separate checkout under .worktrees, so work on it does not touch other tasks."
          checked={d.worktree}
          onChange={(e) => draft.setValue((p) => ({ ...p, worktree: e.target.checked }))}
        />
      )}
      <div>
        <Button
          variant="primary"
          disabled={!changed}
          loading={write.isRunning}
          onClick={async () => {
            const body = { id: task.id, status, ...(status === "doing" ? { worktree: d.worktree } : {}), ...(d.basedOn ? { expected_updated_at: d.basedOn } : {}) };
            const outcome = await write.run(body);
            if (outcome.kind === "done" || outcome.kind === "accepted") draft.clear();
          }}
        >
          Change status
        </Button>
      </div>
      <WriteFeedback state={write.state} what="The status change" />
    </Section>
  );
}

function DetailFields({ task, tasks }: { task: TaskRow; tasks: ReturnType<typeof tasksFromBoard> }) {
  const parentOptions = tasks.filter((t) => t.id !== task.id).map((t) => ({ value: String(t.id), label: `#${t.id} ${t.title}` }));
  const parent = tasks.find((t) => t.id === task.parent_id);
  const untrusted = useTaskUpdate(task, `Trust setting of task #${task.id}`, "Trust setting saved.");
  const dueClear = useTaskUpdate(task, `Due date of task #${task.id}`, "Due date cleared.");
  return (
    <div className="flex flex-col gap-3">
      <FieldRow label="Priority">
        <EditableField<string>
          task={task}
          name="priority"
          label="Priority"
          display={priorityLabel(task.priority)}
          initial={String(task.priority ?? "med")}
          editor={({ value, onChange, id }) => (
            <Select id={id} value={value} onChange={(e) => onChange(e.target.value)} options={Object.entries(PRIORITY_LABEL).map(([v, l]) => ({ value: v, label: l }))} />
          )}
          toBody={(value) => ({ priority: value })}
        />
      </FieldRow>
      <FieldRow label="Owner">
        <p className="m-0 text-sm">{task.claimed_by ?? "Unclaimed"}</p>
        <p className="m-0 text-xs text-[var(--ua-muted)]">The owner is whoever claims the task. Use Claim, Release claim or Send to an agent.</p>
      </FieldRow>
      <FieldRow label="Parent task">
        <EditableField<string | null>
          task={task}
          name="parent"
          label="Parent task"
          display={
            task.parent_id ? (
              <UiLink href={`/work/tasks/${task.parent_id}`} className="text-[var(--ua-link)] underline">
                #{task.parent_id} {parent?.title ?? ""}
              </UiLink>
            ) : (
              "None"
            )
          }
          initial={task.parent_id ? String(task.parent_id) : null}
          editor={({ value, onChange, id }) => <Combobox id={id} aria-label="Parent task" value={value} onChange={onChange} options={parentOptions} placeholder="No parent" />}
          // task_update: parent_id null leaves the parent unchanged; 0 detaches the task.
          toBody={(value) => ({ parent_id: value ? Number(value) : 0 })}
        />
      </FieldRow>
      <FieldRow label="Project">
        <EditableField<string>
          task={task}
          name="project"
          label="Project"
          display={task.project ?? "Not set"}
          initial={task.project ?? ""}
          editor={({ value, onChange, id }) => <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} />}
          toBody={(value) => ({ project: value.trim(), ...(task.project ? { expected_project: task.project } : {}) })}
        />
      </FieldRow>
      <FieldRow label="Domain">
        <EditableField<string>
          task={task}
          name="domain"
          label="Domain"
          display={task.domain || "Not set"}
          initial={task.domain ?? ""}
          editor={({ value, onChange, id }) => <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} />}
          toBody={(value) => ({ domain: value.trim() })}
        />
      </FieldRow>
      <FieldRow label="Queue">
        <EditableField<string>
          task={task}
          name="lane"
          label="Queue"
          display={queueLabel(task.lane)}
          initial={task.lane ?? ""}
          editor={({ value, onChange, id }) => <Input id={id} value={value} placeholder="default" onChange={(e) => onChange(e.target.value)} />}
          toBody={(value) => ({ lane: value.trim() || "default" })}
        />
      </FieldRow>
      <FieldRow label="Due date">
        <EditableField<string>
          task={task}
          name="due"
          label="Due date"
          display={task.due_at ? dateInputValue(task.due_at) : "No due date"}
          initial={dateInputValue(task.due_at)}
          editor={({ value, onChange, id }) => <Input id={id} type="date" value={value} onChange={(e) => onChange(e.target.value)} />}
          toBody={(value) => (value ? { due_at: dueIso(value) } : { clear_due_at: true })}
        />
        {task.due_at && (
          <Button size="sm" variant="ghost" loading={dueClear.isRunning} onClick={() => void dueClear.run({ id: task.id, clear_due_at: true })}>
            Clear due date
          </Button>
        )}
        <WriteFeedback state={dueClear.state} what="Clearing the due date" />
      </FieldRow>
      <FieldRow label="Evidence">
        <EditableField<string>
          task={task}
          name="evidence"
          label="Evidence link"
          display={
            evidenceUrl(task.evidence_ref) ? (
              <UiLink href={evidenceUrl(task.evidence_ref)!} className="break-all text-[var(--ua-link)] underline">
                {task.evidence_ref}
              </UiLink>
            ) : (
              task.evidence_ref || "None"
            )
          }
          initial={task.evidence_ref ?? ""}
          editor={({ value, onChange, id }) => <Input id={id} value={value} placeholder="https://github.com/…/pull/123" onChange={(e) => onChange(e.target.value)} />}
          toBody={(value) => ({ evidence_ref: value.trim() })}
        />
      </FieldRow>
      <Switch
        label="Treat the description as untrusted"
        description="Agents will not follow instructions written in it."
        checked={Boolean(task.untrusted)}
        disabled={untrusted.isRunning}
        onChange={(e) => void untrusted.run({ id: task.id, untrusted: e.target.checked })}
      />
      <WriteFeedback state={untrusted.state} what="The trust setting" />
    </div>
  );
}

function FieldRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 border-b border-[var(--ua-border)] pb-3 last:border-b-0">
      <span className="text-xs font-medium text-[var(--ua-muted)]">{label}</span>
      {children}
    </div>
  );
}

function AddNote({ task, identity }: { task: TaskRow; identity: string | null | undefined }) {
  // One idempotency key per submitted text (kept until the text changes or is cleared), and the
  // approval it parked behind, so a resend or an Inbox run never appends the note twice.
  const draft = useDraft<{ note: string; key: string; parkedApproval: string; submitted: string }>({
    identity,
    operation: "task.note",
    object: { id: task.id },
    initial: { note: "", key: "", parkedApproval: "", submitted: "" },
  });
  const write = useTaskUpdate(task, `Adding a note to task #${task.id}`, "Note added.");
  const note = draft.value.note;
  useReconcileParkedDraft(draft.value.parkedApproval, draft.value.submitted, note, draft.clear);
  const latestDraft = useLatest(draft);
  const epoch = useEpoch();
  return (
    <form
      aria-label="Add a note"
      className="flex flex-col gap-2 border-t border-[var(--ua-border)] pt-3"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!note.trim()) return;
        // Captured before the request (review round 3, blocker 4): see CommentsSection.
        const draftAt = draft.key;
        const sent = note;
        const key = draft.value.key || newKey(`task-${task.id}-note`);
        if (!draft.value.key) draft.setValue((p) => ({ ...p, key }));
        const outcome = await write.run({ id: task.id, append_note: note.trim(), idempotency_key: key });
        if (outcome.kind === "done" || outcome.kind === "accepted") updateDraftAt(epoch, latestDraft.current, draftAt, (p) => (p.note === sent ? null : { ...p, key: "", parkedApproval: "", submitted: "" }));
        else if (outcome.kind === "pending_approval") updateDraftAt(epoch, latestDraft.current, draftAt, (p) => ({ ...p, parkedApproval: outcome.approvalId ?? "", submitted: sent }));
      }}
    >
      <Field label="Add a note" hint="Appended to the description with your name and the time. Up to 2,000 characters.">
        <Textarea rows={3} maxLength={2000} value={note} onChange={(e) => draft.setValue((p) => ({ ...p, note: e.target.value, key: "" }))} />
      </Field>
      <div>
        <Button type="submit" size="sm" disabled={!note.trim()} loading={write.isRunning}>
          Add note
        </Button>
      </div>
      <WriteFeedback state={write.state} what="The note" />
    </form>
  );
}

type ClaimFence = { claimant: string; runId: string | null; claimedAt: string | null };
const fenceOf = (t: TaskRow): ClaimFence => ({ claimant: t.claimed_by ?? "", runId: t.current_run_id ?? null, claimedAt: t.claimed_at ?? null });
const sameFence = (a: ClaimFence | null, b: ClaimFence) => a !== null && a.claimant === b.claimant && a.runId === b.runId && a.claimedAt === b.claimedAt;

function ReleaseClaimDialog({ open, task, onClose }: { open: boolean; task: TaskRow; onClose: () => void }) {
  // The reason and the claim seen when the dialog opened are one draft (review round 3, item 6): the
  // release is fenced on that claim, and a claim that changes needs a new confirmation.
  const draft = useDraft<{ reason: string; fence: ClaimFence | null }>({
    identity: useIdentity(),
    operation: "task.claim-release",
    object: { id: task.id },
    initial: { reason: "", fence: null },
  });
  const reason = draft.value.reason;
  const current = fenceOf(task);
  const fence = draft.value.fence ?? current;
  const changed = draft.value.fence !== null && !sameFence(draft.value.fence, current);
  useEffect(() => {
    // Capture the claim when the dialog opens (once; a restored draft keeps its own).
    if (open && draft.value.fence === null) draft.setValue((p) => ({ ...p, fence: fenceOf(task) }));
  }, [open, draft.value.fence]); // eslint-disable-line react-hooks/exhaustive-deps
  const setReason = (v: string) => draft.setValue((p) => ({ ...p, reason: v, fence: p.fence ?? fenceOf(task) }));
  const write = useHubWrite<Record<string, string | null>, unknown>({
    path: WRITE_PATHS.claimRelease(task.id),
    invalidates: [["work"]],
    label: `Releasing the claim on task #${task.id}`,
    successMessage: "Claim released.",
    resumable: false,
  });
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Release the claim?"
      description={`${fence.claimant || "The current owner"} stops holding task #${task.id}, and anyone can claim it again.`}
      footer={
        <>
          <Button onClick={onClose} disabled={write.isRunning}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!reason.trim() || changed}
            loading={write.isRunning}
            onClick={async () => {
              const body: Record<string, string | null> = {
                expected_claimant: fence.claimant,
                reason: reason.trim(),
                expected_run_id: fence.runId,
              };
              if (!fence.runId) body.expected_claimed_at = fence.claimedAt ?? "";
              const outcome = await write.run(body);
              if (outcome.kind === "done" || outcome.kind === "accepted") {
                draft.clear();
                onClose();
              }
            }}
          >
            Release claim
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {changed && (
          <div role="alert" className="flex flex-col gap-2 rounded-md border border-[var(--ua-warn)] px-3 py-2 text-sm">
            <p className="m-0">
              <strong>The claim changed while this was open.</strong> You were releasing {draft.value.fence?.claimant || "a claim"}
              {draft.value.fence?.runId ? ` (run ${draft.value.fence.runId})` : ""}; it is now held by {current.claimant || "nobody"}
              {current.runId ? ` (run ${current.runId})` : ""}. Nothing was released.
            </p>
            <div>
              <Button size="sm" onClick={() => draft.setValue((p) => ({ ...p, fence: fenceOf(task) }))}>
                Review the new claim
              </Button>
            </div>
          </div>
        )}
        <Field label="Why" required hint="Recorded with the release, so the owner knows what happened.">
          <Textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <WriteFeedback state={write.state} what="The release" />
      </div>
    </Dialog>
  );
}
