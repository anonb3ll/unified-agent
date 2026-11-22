/**
 * `/work/tasks/new` (also `c` and "New → Task"): create a task through
 * `POST /api/tasks`. The form keeps a draft across navigation (spec §6, §12
 * defect 8) and a parked create shows "Waiting for approval", never success.
 */
import { useMemo, useRef, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { Button, Combobox, Field, Input, LinkButton, PageHeader, Select, Textarea, UiLink } from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useHubWrite } from "../lib/mutations";
import { WRITE_PATHS } from "./api";
import { distinct, dueIso, PRIORITY_LABEL, queueLabel, tasksFromBoard } from "./model";
import { useIdentity as useIdentityState } from "../lib/identity";
import { DraftIdentityNotice, isUnknownOutcome, newKey, useBoard, useReconcileParkedDraft, WorkTabs, WriteFeedback } from "./shared";

type TaskDraft = {
  title: string;
  notes: string;
  priority: string;
  project: string;
  parent: string;
  lane: string;
  due: string;
  domain: string;
  /** One idempotency key per draft, made on the first submit and kept until the draft is cleared, so a second submit never makes a second task. */
  key: string;
  /** The approval a submit parked behind, and the fields as submitted, to reconcile a later run from the Inbox. */
  parkedApproval: string;
  submitted: string;
};

type CreateBody = {
  title: string;
  notes?: string;
  priority: string;
  project?: string;
  parent_id?: number;
  lane?: string;
  due_at?: string;
  domain?: string;
  idempotency_key: string;
};

export function TaskNewPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const identity = useIdentityState();
  const presetParent = params.get("parent") ?? "";
  const draft = useDraft<TaskDraft>({
    identity,
    operation: "task.create",
    object: { parent: presetParent },
    initial: { title: "", notes: "", priority: "med", project: params.get("project") ?? "", parent: presetParent, lane: "", due: "", domain: "", key: "", parkedApproval: "", submitted: "" },
  });
  const board = useBoard();
  const tasks = useMemo(() => (board.data ? tasksFromBoard(board.data) : []), [board.data]);
  const titleRef = useRef<HTMLInputElement>(null);
  const write = useHubWrite<CreateBody, { id?: number; task?: { id?: number } }>({
    path: WRITE_PATHS.taskCreate,
    invalidates: [["work"]],
    label: "Creating the task",
    successMessage: (data) => `Task #${data.id ?? data.task?.id ?? ""} created.`,
  });
  const v = draft.value;
  const set = (patch: Partial<TaskDraft>) => draft.setValue((prev) => ({ ...prev, ...patch }));
  const [titleError, setTitleError] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const fieldsOf = (d: TaskDraft) => JSON.stringify([d.title, d.notes, d.priority, d.project, d.parent, d.lane, d.due, d.domain]);
  const clearDraft = draft.clear;
  const ranElsewhere = useReconcileParkedDraft(v.parkedApproval, v.submitted, fieldsOf(v), clearDraft);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!v.title.trim()) {
      setTitleError(true);
      titleRef.current?.focus();
      return;
    }
    setTitleError(false);
    const key = v.key || newKey("task-create");
    if (!v.key) set({ key });
    const body: CreateBody = { title: v.title.trim(), priority: v.priority, idempotency_key: key };
    if (v.notes.trim()) body.notes = v.notes.trim();
    if (v.project.trim()) body.project = v.project.trim();
    if (v.parent) body.parent_id = Number(v.parent);
    if (v.lane.trim()) body.lane = v.lane.trim();
    if (v.due) body.due_at = dueIso(v.due);
    if (v.domain.trim()) body.domain = v.domain.trim();
    const outcome = await write.run(body);
    // Sent and lost, or cancelled after it left: the task may exist (the draft keeps its key, so a resend is safe).
    setUnknown(isUnknownOutcome(outcome));
    if (outcome.kind === "pending_approval") set({ parkedApproval: outcome.approvalId ?? "", submitted: fieldsOf(v) });
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      draft.clear();
      const id = outcome.data.id ?? outcome.data.task?.id;
      navigate(id ? `/work/tasks/${id}` : "/work");
    }
  };

  const projects = distinct(tasks, (t) => t.project);
  const lanes = distinct(tasks, (t) => t.lane);

  return (
    <>
      <PageHeader title="New task" description="Describe the work. An agent or a person can claim it once it is saved." tabs={<WorkTabs />} />
      <form onSubmit={submit} noValidate aria-label="New task" className="flex max-w-2xl flex-col gap-4">
        <DraftIdentityNotice failed={draft.identityFailed} retry={draft.retryIdentity} />
        {draft.restored && (
          <p role="status" className="m-0 text-sm text-[var(--ua-muted)]">
            Your unsaved draft was restored.{" "}
            <Button size="sm" variant="ghost" onClick={() => draft.clear()}>
              Discard draft
            </Button>
          </p>
        )}
        <Field label="Title" required error={titleError && !v.title.trim() ? "Give the task a title." : undefined}>
          <Input ref={titleRef} value={v.title} onChange={(e) => set({ title: e.target.value })} autoComplete="off" />
        </Field>
        <Field label="Description" hint="What done looks like, links, and anything the person or agent needs to know.">
          <Textarea rows={6} value={v.notes} onChange={(e) => set({ notes: e.target.value })} />
        </Field>
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Priority">
            <Select value={v.priority} onChange={(e) => set({ priority: e.target.value })} options={Object.entries(PRIORITY_LABEL).map(([value, label]) => ({ value, label }))} />
          </Field>
          <Field label="Project" hint="Leave empty for the hub's default project.">
            <Input value={v.project} list="work-projects" onChange={(e) => set({ project: e.target.value })} />
          </Field>
          <Field label="Parent task" hint="Makes this a subtask.">
            <Combobox
              value={v.parent || null}
              onChange={(next) => set({ parent: next ?? "" })}
              placeholder="No parent"
              options={tasks.map((t) => ({ value: String(t.id), label: `#${t.id} ${t.title}` }))}
            />
          </Field>
          <Field label="Queue" hint="Which queue picks it up. Leave empty for the default queue.">
            <Input value={v.lane} list="work-queues" onChange={(e) => set({ lane: e.target.value })} />
          </Field>
          <Field label="Due date">
            <Input type="date" value={v.due} onChange={(e) => set({ due: e.target.value })} />
          </Field>
          <Field label="Domain" hint="Optional area within the project, such as web or docs.">
            <Input value={v.domain} onChange={(e) => set({ domain: e.target.value })} />
          </Field>
        </div>
        <datalist id="work-projects">
          {projects.map((p) => (
            <option key={p} value={p} />
          ))}
        </datalist>
        <datalist id="work-queues">
          {lanes.map((l) => (
            <option key={l} value={l}>
              {queueLabel(l)}
            </option>
          ))}
        </datalist>
        {ranElsewhere && (
          <p role="status" className="m-0 text-sm">
            The task you submitted was created from the Inbox after approval.{" "}
            <UiLink href="/work" className="text-[var(--ua-link)] underline">
              Find it in the task list
            </UiLink>
            .
          </p>
        )}
        <WriteFeedback
          state={write.state}
          what="The task"
          outcomeUnknown={unknown}
          checkHref="/work"
          checkLabel="look for it in the task list"
          onRun={(result) => {
            // Run from the notice after approval: the task exists now, so finish the form.
            if (result.kind === "done" || result.kind === "accepted") {
              const data = (result.data ?? {}) as { id?: number; task?: { id?: number } };
              draft.clear();
              const id = data.id ?? data.task?.id;
              navigate(id ? `/work/tasks/${id}` : "/work");
            }
          }}
        />
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" loading={write.isRunning}>
            Create task
          </Button>
          <LinkButton href="/work">Cancel</LinkButton>
        </div>
      </form>
    </>
  );
}
