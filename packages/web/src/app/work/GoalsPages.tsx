/**
 * Goals (spec §9.2): `/work/goals` lists the why-tree and creates a goal;
 * `/work/goals/:id` shows a goal with its linked tasks and progress, and
 * edits, links and unlinks tasks, closes and reopens it.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useParams } from "react-router";
import {
  Badge,
  Button,
  Combobox,
  ConfirmDialog,
  DescriptionList,
  Dialog,
  ErrorState,
  Field,
  Input,
  Markdown,
  RadioGroup,
  PageHeader,
  Select,
  Skeleton,
  Textarea,
  UiLink,
} from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useHubWrite } from "../lib/mutations";
import { describeReadError, QueryRegion } from "../lib/queries";
import { fetchGoal, fetchWorkGraphAll, WORK_KEYS, WRITE_PATHS, type GoalAction, type GoalRow } from "./api";
import { ago, statusLabel, tasksFromBoard } from "./model";
import { isGone, recordBeforeSend, RetainedReadNotice, updateDraftAt, useLatest, useReconcileParkedDraft, Section, useBoard, useGoalsQuery, useIdentity, WorkTabs, WriteFeedback } from "./shared";
import { useEpoch } from "../lib/epoch";

function flatten(tree: readonly GoalRow[], depth = 0, out: { goal: GoalRow; depth: number }[] = []) {
  for (const g of tree) {
    out.push({ goal: g, depth });
    flatten(g.children ?? [], depth + 1, out);
  }
  return out;
}

/** goals.STATUSES: open | achieved | abandoned. */
const GOAL_STATUS_LABEL: Record<string, string> = { open: "Open", achieved: "Achieved", abandoned: "Abandoned" };
const goalStatus = (status: string | null | undefined) => GOAL_STATUS_LABEL[status ?? "open"] ?? status ?? "Open";
/** goals.revision (#2480), advanced by every goal update; null on a hub without it. */
const goalRevision = (g: GoalRow): number | null => (typeof g.revision === "number" ? g.revision : null);
const isClosedGoal = (status: string | null | undefined) => status === "achieved" || status === "abandoned";

export function GoalsPage() {
  const goals = useGoalsQuery();
  const [creating, setCreating] = useState(false);
  return (
    <>
      <PageHeader
        title="Goals"
        description="A goal is the outcome a group of tasks works toward. Linking a task to a goal records why the task matters."
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            New goal
          </Button>
        }
        tabs={<WorkTabs />}
      />
      <QueryRegion
        query={goals}
        loadingLabel="Loading goals"
        errorTitle="Goals could not be loaded"
        isEmpty={(d) => d.tree.length === 0}
        empty={{
          title: "No goals yet",
          description: "Create a goal, then link tasks to it from the goal page or the task page.",
          action: (
            <Button variant="primary" onClick={() => setCreating(true)}>
              New goal
            </Button>
          ),
        }}
      >
        {(d) => (
          <ul aria-label="Goals" className="m-0 flex list-none flex-col gap-1 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-3">
            {flatten(d.tree).map(({ goal, depth }) => (
              <li key={goal.id} className="flex flex-wrap items-center gap-2 py-1" style={{ paddingLeft: `${depth * 1.25}rem` }}>
                <UiLink href={`/work/goals/${goal.id}`} className="font-medium text-[var(--ua-link)] underline">
                  {goal.title}
                </UiLink>
                <Badge size="sm" variant={isClosedGoal(goal.status) ? "success" : "info"}>
                  {goalStatus(goal.status)}
                </Badge>
                {goal.project && <span className="text-xs text-[var(--ua-muted)]">{goal.project}</span>}
              </li>
            ))}
          </ul>
        )}
      </QueryRegion>
      <GoalCreateDialog open={creating} onClose={() => setCreating(false)} parents={goals.data ? flatten(goals.data.tree).map((f) => f.goal) : []} />
    </>
  );
}

function GoalCreateDialog({ open, onClose, parents }: { open: boolean; onClose: () => void; parents: GoalRow[] }) {
  const identity = useIdentity();
  const draft = useDraft<{ title: string; statement: string; parent: string; project: string }>({
    identity,
    operation: "goal.create",
    initial: { title: "", statement: "", parent: "", project: "" },
  });
  const write = useHubWrite<Record<string, unknown>, { goal?: GoalRow; id?: number }>({
    path: WRITE_PATHS.goals,
    invalidates: [WORK_KEYS.goals],
    label: "Creating the goal",
    successMessage: "Goal created.",
    resumable: false,
  });
  const v = draft.value;
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New goal"
      description="Name the outcome. Tasks that serve it are linked afterwards."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!v.title.trim()}
            loading={write.isRunning}
            onClick={async () => {
              const body: Record<string, unknown> = { action: "add", title: v.title.trim() };
              if (v.statement.trim()) body.statement = v.statement.trim();
              if (v.parent) body.parent_goal_id = Number(v.parent);
              if (v.project.trim()) body.project = v.project.trim();
              const outcome = await write.run(body);
              if (outcome.kind === "done") {
                draft.clear();
                onClose();
              }
            }}
          >
            Create goal
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label="Title" required>
          <Input value={v.title} onChange={(e) => draft.setValue((p) => ({ ...p, title: e.target.value }))} />
        </Field>
        <Field label="Statement" hint="What will be true when this goal is met.">
          <Textarea rows={3} value={v.statement} onChange={(e) => draft.setValue((p) => ({ ...p, statement: e.target.value }))} />
        </Field>
        <Field label="Part of goal">
          <Select value={v.parent} placeholder="None (a top-level goal)" onChange={(e) => draft.setValue((p) => ({ ...p, parent: e.target.value }))} options={parents.map((g) => ({ value: String(g.id), label: g.title }))} />
        </Field>
        <Field label="Project" hint="Tasks can only serve a goal in their own project.">
          <Input value={v.project} onChange={(e) => draft.setValue((p) => ({ ...p, project: e.target.value }))} />
        </Field>
        <WriteFeedback state={write.state} what="The goal" />
      </div>
    </Dialog>
  );
}

export function GoalPage() {
  const { id = "" } = useParams();
  const goal = useQuery({ queryKey: WORK_KEYS.goal(id), queryFn: ({ signal }) => fetchGoal(id, signal), staleTime: 10_000 });
  if (!goal.data) {
    const { message, detail } = describeReadError(goal.error);
    const notFound = (goal.error as { status?: number } | null)?.status === 404;
    return (
      <>
        <PageHeader title={`Goal ${id}`} tabs={<WorkTabs />} />
        {goal.status === "pending" ? (
          <Skeleton label="Loading the goal" lines={5} />
        ) : (
          <ErrorState
            title={notFound ? "This goal does not exist" : "The goal could not be loaded"}
            message={notFound ? "No goal has this number. Go back to the goals list." : message}
            detail={detail}
            onRetry={() => void goal.refetch()}
          />
        )}
      </>
    );
  }
  if (isGone(goal)) {
    return (
      <>
        <PageHeader title={goal.data.goal.title} tabs={<WorkTabs />} />
        <ErrorState title="This goal no longer exists" message="The hub no longer has this goal. Go back to the goals list." retryLabel="Check again" onRetry={() => void goal.refetch()} />
      </>
    );
  }
  return (
    <>
      <RetainedReadNotice query={goal} what="This goal" />
      <GoalDetail goal={goal.data.goal} ancestry={goal.data.ancestry ?? []} />
    </>
  );
}

function GoalDetail({ goal, ancestry }: { goal: GoalRow; ancestry: GoalRow[] }) {
  const id = String(goal.id);
  const graph = useQuery({ queryKey: WORK_KEYS.workGraph(id), queryFn: ({ signal }) => fetchWorkGraphAll(goal.id, signal), staleTime: 10_000 });
  const board = useBoard();
  const allTasks = useMemo(() => (board.data ? tasksFromBoard(board.data) : []), [board.data]);
  const identity = useIdentity();
  // The edit dialog's text is a draft (spec §6): kept through errors, parked approvals and navigation.
  // The revision the edit started from is kept with the draft, so a live refresh never moves it (#2480 expected_revision).
  const editDraft = useDraft<{ open: boolean; title: string; statement: string; revision: number | null }>({
    identity,
    operation: "goal.edit",
    object: { goal: goal.id },
    initial: { open: false, title: goal.title, statement: goal.statement ?? "", revision: goalRevision(goal) },
  });
  const editing = editDraft.value.open;
  const title = editDraft.value.title;
  const statement = editDraft.value.statement;
  const setEditing = (open: boolean) =>
    open ? editDraft.setValue((p) => (p.open ? p : { open: true, title: goal.title, statement: goal.statement ?? "", revision: goalRevision(goal) })) : editDraft.clear();
  const setTitle = (v: string) => editDraft.setValue((p) => ({ ...p, title: v }));
  const setStatement = (v: string) => editDraft.setValue((p) => ({ ...p, statement: v }));
  const [linkPick, setLinkPick] = useState<string | null>(null);
  const [unlinking, setUnlinking] = useState<{ id: number; title: string } | null>(null);
  // Close and reopen: the dialog, its choices and the revision it started from are a draft too.
  const closeDraft = useDraft<{ open: boolean; status: "achieved" | "abandoned"; reason: string; revision: number | null; parkedApproval: string; submitted: string }>({
    identity,
    operation: "goal.close",
    object: { goal: goal.id },
    initial: { open: false, status: "achieved", reason: "", revision: goalRevision(goal), parkedApproval: "", submitted: "" },
  });
  const closeFingerprint = (d: { status: string; reason: string }) => JSON.stringify([d.status, d.reason]);
  const latestClose = useLatest(closeDraft);
  const epoch = useEpoch();
  // A parked close run later from the Inbox ends the dialog only if it still holds what was sent.
  useReconcileParkedDraft(closeDraft.value.parkedApproval, closeDraft.value.submitted, closeFingerprint(closeDraft.value), closeDraft.clear);
  const closing = closeDraft.value.open;
  const setClosing = (open: boolean) =>
    open
      ? closeDraft.setValue((p) => (p.open ? p : { open: true, status: "achieved", reason: "", revision: goalRevision(goal), parkedApproval: "", submitted: "" }))
      : closeDraft.clear();
  const closed = isClosedGoal(goal.status);
  const closeStatus = closeDraft.value.status;
  const closeReason = closeDraft.value.reason;
  const setCloseStatus = (v: "achieved" | "abandoned") => closeDraft.setValue((p) => ({ ...p, status: v }));
  const setCloseReason = (v: string) => closeDraft.setValue((p) => ({ ...p, reason: v }));
  const queryClient = useQueryClient();
  const latestGoal = useLatest(goal);
  const reloadGoal = () => queryClient.refetchQueries({ queryKey: WORK_KEYS.goal(id) });
  const asExpected = (rev: number | null) => (rev === null ? {} : { expected_revision: rev });
  const invalidates = [WORK_KEYS.goal(id), WORK_KEYS.goals, ["work", "work-graph"]];
  // The goal routes rebuild their approval payload from the body and the verified author (control-center-api.md), so they keep the default.
  const action = useHubWrite<Record<string, unknown>, unknown>({ path: WRITE_PATHS.goalAction("edit"), invalidates, label: "Changing the goal", successMessage: "Goal updated." });
  const link = useHubWrite<Record<string, unknown>, unknown>({ path: WRITE_PATHS.goalAction("link"), invalidates, label: "Linking a task", successMessage: "Task linked." });
  const goalWrite = (name: GoalAction, body: Record<string, unknown>) => action.run({ goal_id: goal.id, ...body }, { path: WRITE_PATHS.goalAction(name) });
  const confirmClose = async (revision?: number | null) => {
    const reason = closeReason.trim() ? { reason: closeReason.trim() } : {};
    const rev = asExpected(revision === undefined ? closeDraft.value.revision : revision);
    const key = closeDraft.key;
    const submitted = closeFingerprint(closeDraft.value);
    // The dialog's choices are stored before the send: the owner epoch may end mid-request,
    // and an ended epoch refuses the debounced save (review round 3: record before the send).
    recordBeforeSend(epoch, closeDraft, closeDraft.value);
    const outcome = closed ? await goalWrite("reopen", { ...reason, ...rev }) : await goalWrite("close", { status: closeStatus, ...reason, ...rev });
    // Only a completed close ends the dialog; a parked one keeps the choices until its run is reconciled,
    // and a cancelled or failed one keeps them as they were (review round 3, items 1 and 9).
    if (outcome.kind === "done" || outcome.kind === "accepted") updateDraftAt(epoch, latestClose.current, key, () => null);
    else if (outcome.kind === "pending_approval") updateDraftAt(epoch, latestClose.current, key, (p) => ({ ...p, parkedApproval: outcome.approvalId ?? "", submitted }));
  };

  const linked = (graph.data?.nodes ?? []).filter(
    (n) => n.kind === "task" && !n.stub && (graph.data?.edges ?? []).some((e) => e.kind === "goal_task" && e.from === `goal:${goal.id}` && e.to === n.id),
  );
  const subtreeTasks = (graph.data?.nodes ?? []).filter((n) => n.kind === "task" && !n.stub);
  const done = subtreeTasks.filter((n) => n.status === "done").length;

  return (
    <>
      <PageHeader
        meta={
          <span className="flex flex-wrap items-center gap-2">
            Goal #{goal.id}
            <Badge size="sm" variant={closed ? "success" : "info"}>
              {goalStatus(goal.status)}
            </Badge>
          </span>
        }
        title={goal.title}
        description={goal.project ? `Project ${goal.project}` : undefined}
        actions={
          <>
            <Button onClick={() => setEditing(true)}>Edit</Button>
            <Button onClick={() => setClosing(true)}>{closed ? "Reopen" : "Close goal"}</Button>
          </>
        }
        tabs={<WorkTabs />}
      />
      <div className="flex flex-col gap-4">
        <WriteFeedback state={action.state} what="The goal change" />
        {ancestry.length > 1 && (
          <nav aria-label="Goal ancestry" className="text-sm">
            Part of{" "}
            {ancestry.slice(0, -1).map((g, i) => (
              <span key={g.id}>
                {i > 0 && " › "}
                <UiLink href={`/work/goals/${g.id}`} className="text-[var(--ua-link)] underline">
                  {g.title}
                </UiLink>
              </span>
            ))}
          </nav>
        )}
        <Section title="Statement" id="goal-statement">
          {goal.statement ? <Markdown source={goal.statement} /> : <p className="m-0 text-sm text-[var(--ua-muted)]">No statement yet. Edit the goal to say what will be true when it is met.</p>}
          <DescriptionList items={[{ term: "Created by", value: goal.created_by }, { term: "Created", value: goal.created_at ? ago(goal.created_at) : null }]} />
        </Section>
        <Section title="Tasks" id="goal-tasks">
          <QueryRegion query={graph} loadingLabel="Loading linked tasks" errorTitle="Linked tasks could not be loaded" headingLevel={3} empty={{ title: "" }}>
            {() => (
              <>
                <div>
                  <p className="m-0 text-sm">
                    {subtreeTasks.length === 0
                      ? graph.data?.complete === false
                        ? "No tasks found in the pages read so far; the goal's work graph is too large to read in full."
                        : "No tasks serve this goal yet."
                      : graph.data?.complete === false
                        ? `${done} of the first ${subtreeTasks.length} tasks read are done. The goal has more tasks than one view reads, so its total progress is not known.`
                        : `${done} of ${subtreeTasks.length} task${subtreeTasks.length === 1 ? "" : "s"} done, including tasks of the goals under it.`}
                  </p>
                  {subtreeTasks.length > 0 && (
                    <progress className="mt-1 h-2 w-full max-w-md accent-[var(--ua-accent)]" max={subtreeTasks.length} value={done} aria-label="Goal progress">
                      {done} of {subtreeTasks.length}
                    </progress>
                  )}
                </div>
                {linked.length > 0 && (
                  <ul aria-label="Tasks linked to this goal" className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
                    {linked.map((n) => (
                      <li key={n.id} className="flex flex-wrap items-center gap-2">
                        <UiLink href={`/work/tasks/${n.task_id}`} className="text-[var(--ua-link)] underline">
                          #{n.task_id} {n.label}
                        </UiLink>
                        <span className="text-[var(--ua-muted)]">{statusLabel(n.status)}</span>
                        <Button size="sm" variant="ghost" aria-label={`Unlink task #${n.task_id}`} onClick={() => setUnlinking({ id: Number(n.task_id), title: String(n.label ?? "") })}>
                          Unlink
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </QueryRegion>
          <form
            aria-label="Link a task"
            className="flex flex-wrap items-end gap-2"
            onSubmit={async (e) => {
              e.preventDefault();
              if (!linkPick) return;
              const outcome = await link.run({ task_id: Number(linkPick), goal_id: goal.id });
              if (outcome.kind === "done") setLinkPick(null);
            }}
          >
            <Field label="Link a task" className="min-w-60 flex-1" hint="Only tasks in the goal's project can be linked; a task serves one goal.">
              <Combobox
                value={linkPick}
                onChange={setLinkPick}
                placeholder="Choose a task"
                options={allTasks.filter((t) => !goal.project || t.project === goal.project).map((t) => ({ value: String(t.id), label: `#${t.id} ${t.title}` }))}
              />
            </Field>
            <Button type="submit" disabled={!linkPick} loading={link.isRunning}>
              Link task
            </Button>
          </form>
          <WriteFeedback state={link.state} what="The link" />
        </Section>
      </div>
      <Dialog
        open={editing}
        onClose={() => setEditing(false)}
        title="Edit goal"
        footer={
          <>
            <Button onClick={() => setEditing(false)}>Cancel</Button>
            <Button
              variant="primary"
              disabled={!title.trim()}
              loading={action.isRunning}
              onClick={async () => {
                const outcome = await goalWrite("edit", { title: title.trim(), statement: statement.trim(), ...asExpected(editDraft.value.revision) });
                if (outcome.kind === "done") setEditing(false);
              }}
            >
              Save
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <Field label="Title" required>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label="Statement">
            <Textarea rows={4} value={statement} onChange={(e) => setStatement(e.target.value)} />
          </Field>
          <WriteFeedback
            state={action.state}
            what="The edit"
            // A 409: reload the goal, then Retry sends this text against the reloaded revision (review round 3, item 7).
            onReload={() => reloadGoal()}
            onRetry={async () => {
              const revision = goalRevision(latestGoal.current);
              editDraft.setValue((p) => ({ ...p, revision }));
              const outcome = await goalWrite("edit", { title: title.trim(), statement: statement.trim(), ...asExpected(revision) });
              if (outcome.kind === "done") setEditing(false);
            }}
          />
        </div>
      </Dialog>
      <ConfirmDialog
        open={unlinking !== null}
        title="Unlink this task?"
        objectName={unlinking ? `#${unlinking.id} ${unlinking.title}` : undefined}
        consequence="The task stays as it is but no longer serves this goal."
        confirmLabel="Unlink"
        busy={action.isRunning}
        onConfirm={async () => {
          if (unlinking) await action.run({ task_id: unlinking.id, goal_id: goal.id }, { path: WRITE_PATHS.goalAction("unlink") });
          setUnlinking(null);
        }}
        onCancel={() => setUnlinking(null)}
      />
      <ConfirmDialog
        open={closing}
        title={closed ? "Reopen this goal?" : "Close this goal?"}
        objectName={goal.title}
        consequence={closed ? "The goal is open again and tasks can be linked to it." : "The goal is recorded as achieved or abandoned. Its tasks are not changed or unlinked, and you can reopen it."}
        confirmLabel={closed ? "Reopen" : "Close goal"}
        busy={action.isRunning}
        onConfirm={() => confirmClose()}
        onCancel={() => setClosing(false)}
      >
        <div className="mt-3 flex flex-col gap-3">
          {!closed && (
            <RadioGroup
              legend="How it ended"
              value={closeStatus}
              onChange={(v) => setCloseStatus(v === "abandoned" ? "abandoned" : "achieved")}
              options={[
                { value: "achieved", label: "Achieved", description: "The outcome is met." },
                { value: "abandoned", label: "Abandoned", description: "It is no longer pursued." },
              ]}
            />
          )}
          <Field label="Why" hint="Optional. Recorded with the change.">
            <Textarea rows={2} value={closeReason} onChange={(e) => setCloseReason(e.target.value)} />
          </Field>
          <WriteFeedback
            state={action.state}
            what="The change"
            onReload={() => reloadGoal()}
            onRetry={() => {
              // Retry against the reloaded revision (review round 3, item 7).
              const revision = goalRevision(latestGoal.current);
              closeDraft.setValue((p) => ({ ...p, revision }));
              return confirmClose(revision);
            }}
          />
        </div>
      </ConfirmDialog>
    </>
  );
}
