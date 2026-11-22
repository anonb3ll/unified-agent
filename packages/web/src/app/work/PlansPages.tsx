/**
 * Plans (spec §9.2): list, create, and the plan page with ordered steps,
 * step status, checkpoints, structure edits, handing a step to an agent,
 * the review gate the classic pane offered, and shared (joint) plans.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router";
import { fetchPlanDetail, planReviewIntent, type PlanRow, type PlanStepRow } from "../../lib/api/plans";
import {
  Badge,
  Button,
  Checkbox,
  Combobox,
  ConfirmDialog,
  DataTable,
  DescriptionList,
  Dialog,
  ErrorState,
  Field,
  IconButton,
  Input,
  LinkButton,
  Menu,
  PageHeader,
  Skeleton,
  Textarea,
  UiLink,
  type Column,
} from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { useParkedWrites } from "../lib/parkedWrites";
import { useHubWrite } from "../lib/mutations";
import { describeReadError, QueryRegion } from "../lib/queries";
import { WORK_KEYS, WRITE_PATHS, type PlanAction } from "./api";
import { ago } from "./model";
import { JointPlanPanel } from "./JointPlanPanel";
import { useIdentity as useIdentityState } from "../lib/identity";
import { DraftIdentityNotice, isGone, isUnknownOutcome, recordBeforeSend, RetainedReadNotice, updateDraftAt, useLatest, Section, useAgentOptions, useIdentity, usePlansQuery, useReconcileParkedDraft, WorkTabs, WriteFeedback } from "./shared";
import { useEpoch } from "../lib/epoch";

export const STEP_STATUSES = ["open", "doing", "done", "failed", "blocked"] as const;
const STEP_LABEL: Record<string, string> = { open: "Not started", doing: "In progress", done: "Done", failed: "Failed", blocked: "Blocked" };
const stepLabel = (s: string | null | undefined) => STEP_LABEL[s ?? ""] ?? s ?? "Not started";
const PLAN_LABEL: Record<string, string> = { active: "Active", done: "Done", blocked: "Blocked", abandoned: "Closed" };
/** #2480: plan reads report `title` beside `goal` (its first line). */
/** A step's immutable row id (#2480); null on a hub without it. */
const stepIdOf = (st: PlanStepRow): number | null => (typeof st.id === "number" ? st.id : null);
/**
 * How a step write addresses its step: the current index plus the immutable id, so a write that
 * lost a race with a reorder answers 409 instead of changing another step (#2480 expected_step_id).
 */
const stepRef = (st: PlanStepRow) => {
  const id = stepIdOf(st);
  return { step_idx: st.step_idx, ...(id === null ? {} : { expected_step_id: id }) };
};
/** plans.revision (#2480): advanced by every write to the plan; null on a hub without it. */
const planRevision = (p: PlanRow): number | null => (typeof p.revision === "number" ? p.revision : null);
const planTitle = (p: PlanRow) => (typeof p.title === "string" && p.title ? p.title : p.goal?.split("\n")[0] || `Plan ${p.id}`);
const planLabel = (s: string | null | undefined) => PLAN_LABEL[s ?? ""] ?? s ?? "Unknown";

export function PlansPage() {
  const plans = usePlansQuery();
  const columns: Column<PlanRow>[] = [
    { id: "goal", header: "Plan", rowLink: true, cell: (p) => planTitle(p) },
    { id: "status", header: "Status", cell: (p) => planLabel(p.status) },
    { id: "review", header: "Needs a person", cell: (p) => (p.needs_human ? <Badge size="sm" variant="warning">Needs review</Badge> : "No") },
    { id: "author", header: "Author", cell: (p) => p.author ?? "Unknown" },
    { id: "updated", header: "Updated", cell: (p) => ago(p.updated_at ?? p.created_at) },
  ];
  return (
    <>
      <PageHeader
        title="Plans"
        description="A plan is an ordered list of steps toward a result. Agents claim steps, hand them on, and save checkpoints so work can resume."
        actions={
          <LinkButton href="/work/plans/new" variant="primary">
            New plan
          </LinkButton>
        }
        tabs={<WorkTabs />}
      />
      <QueryRegion
        query={plans}
        loadingLabel="Loading plans"
        errorTitle="Plans could not be loaded"
        isEmpty={(d) => d.plans.length === 0}
        empty={{
          title: "No plans yet",
          description: "Write the steps for a piece of work, then hand each step to an agent.",
          action: (
            <LinkButton href="/work/plans/new" variant="primary">
              New plan
            </LinkButton>
          ),
        }}
      >
        {(d) => <DataTable caption="Plans" columns={columns} data={d.plans} getRowKey={(p) => p.id} getRowHref={(p) => `/work/plans/${encodeURIComponent(p.id)}`} />}
      </QueryRegion>
    </>
  );
}

type StepDraft = { description: string; owner: string };

export function PlanNewPage() {
  const identity = useIdentityState();
  const navigate = useNavigate();
  const agents = useAgentOptions();
  // `attempt` records a create whose result is not settled: parked behind an approval, or sent
  // with its response lost. plan_create has no idempotency key (Needs from hub), so while an
  // attempt is open the form never submits again; the person settles it first.
  // "sending" is recorded before the request leaves; found on return, it reads as unknown.
  const draft = useDraft<{ goal: string; scope: string; steps: StepDraft[]; parkedApproval: string; submitted: string; attempt: "" | "sending" | "parked" | "unknown" }>({
    identity,
    operation: "plan.create",
    initial: { goal: "", scope: "", steps: [{ description: "", owner: "" }], parkedApproval: "", submitted: "", attempt: "" },
  });
  const write = useHubWrite<Record<string, unknown>, { plan?: { id?: string }; plan_id?: string; id?: string }>({
    path: WRITE_PATHS.planAction("create"),
    invalidates: [WORK_KEYS.plans],
    label: "Creating the plan",
    successMessage: "Plan created.",
  });
  const v = draft.value;
  const fieldsOf = (d: typeof v) => JSON.stringify([d.goal, d.scope, d.steps]);
  const [notRecorded, setNotRecorded] = useState(false);
  // An attempt still "sending" when no request is running was left mid-flight: its result is unknown.
  const attempt = v.attempt === "sending" && !write.isRunning ? "unknown" : v.attempt;
  const stored = useParkedWrites({ approvalId: v.parkedApproval || null });
  const latestDraft = useLatest(draft);
  const epoch = useEpoch();
  const ranElsewhere = useReconcileParkedDraft(v.parkedApproval, v.submitted, fieldsOf(v), draft.clear);
  const setStep = (i: number, patch: Partial<StepDraft>) => draft.setValue((p) => ({ ...p, steps: p.steps.map((s, j) => (j === i ? { ...s, ...patch } : s)) }));
  const filled = v.steps.filter((s) => s.description.trim());
  return (
    <>
      <PageHeader title="New plan" description="Write the result you want and the steps to get there, in order." tabs={<WorkTabs />} />
      <form
        aria-label="New plan"
        className="flex max-w-3xl flex-col gap-4"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!v.goal.trim() || filled.length === 0 || v.attempt) return;
          // Record the attempt before the request leaves (and check it was stored), so leaving the
          // page mid-request can never allow a second create while the first may have committed.
          const key = draft.key;
          const submitted = fieldsOf(v);
          if (!recordBeforeSend(epoch, draft, { ...v, attempt: "sending", submitted, parkedApproval: "" })) {
            setNotRecorded(true);
            return;
          }
          setNotRecorded(false);
          const body: Record<string, unknown> = {
            goal: v.goal.trim(),
            steps: filled.map((s) => (s.owner ? { description: s.description.trim(), owner: s.owner } : s.description.trim())),
          };
          if (v.scope.trim()) body.scope = v.scope.trim();
          const outcome = await write.run(body);
          // Settle the draft this attempt was made from (it may no longer be the mounted one).
          if (isUnknownOutcome(outcome)) updateDraftAt(epoch, latestDraft.current, key, (p) => ({ ...p, attempt: "unknown" as const }));
          else if (outcome.kind === "pending_approval") updateDraftAt(epoch, latestDraft.current, key, (p) => ({ ...p, attempt: "parked" as const, parkedApproval: outcome.approvalId ?? "" }));
          else if (outcome.kind === "error" || outcome.kind === "cancelled") updateDraftAt(epoch, latestDraft.current, key, (p) => ({ ...p, attempt: "" as const }));
          if (outcome.kind === "done") {
            updateDraftAt(epoch, latestDraft.current, key, (p) => (fieldsOf(p) === submitted ? null : { ...p, attempt: "" as const, submitted: "" }));
            const id = outcome.data.plan_id ?? outcome.data.plan?.id ?? outcome.data.id;
            navigate(id ? `/work/plans/${encodeURIComponent(id)}` : "/work/plans");
          }
        }}
      >
        <DraftIdentityNotice failed={draft.identityFailed} retry={draft.retryIdentity} />
        {notRecorded && (
          <p role="alert" className="m-0 text-sm">
            This browser could not record the attempt, so the plan was not sent. Check that site storage is allowed, then try again.
          </p>
        )}
        <Field label="What the plan achieves" required>
          <Input value={v.goal} onChange={(e) => draft.setValue((p) => ({ ...p, goal: e.target.value }))} />
        </Field>
        <Field label="Group" hint="Optional. Plans in the same group are listed together.">
          <Input value={v.scope} onChange={(e) => draft.setValue((p) => ({ ...p, scope: e.target.value }))} />
        </Field>
        <fieldset className="m-0 flex flex-col gap-3 border-0 p-0">
          <legend className="mb-2 text-sm font-medium">Steps, in order</legend>
          <ol className="m-0 flex list-none flex-col gap-3 p-0">
            {v.steps.map((step, i) => (
              <li key={i} className="grid items-end gap-2 md:grid-cols-[1fr_14rem_auto]">
                <Field label={`Step ${i + 1}`}>
                  <Input value={step.description} onChange={(e) => setStep(i, { description: e.target.value })} />
                </Field>
                <Field label={`Owner of step ${i + 1}`}>
                  <Combobox value={step.owner || null} onChange={(o) => setStep(i, { owner: o ?? "" })} options={agents} placeholder="Anyone" />
                </Field>
                <IconButton
                  aria-label={`Remove step ${i + 1}`}
                  icon={<span>✕</span>}
                  disabled={v.steps.length === 1}
                  onClick={() => draft.setValue((p) => ({ ...p, steps: p.steps.filter((_, j) => j !== i) }))}
                />
              </li>
            ))}
          </ol>
          <div>
            <Button size="sm" onClick={() => draft.setValue((p) => ({ ...p, steps: [...p.steps, { description: "", owner: "" }] }))}>
              Add a step
            </Button>
          </div>
        </fieldset>
        {ranElsewhere && (
          <p role="status" className="m-0 text-sm">
            The plan you submitted was created from the Inbox after approval. Find it in the plans list.
          </p>
        )}
        {attempt === "unknown" && (
          <div role="alert" className="flex flex-col gap-2 rounded-md border border-[var(--ua-warn)] px-3 py-2 text-sm">
            <p className="m-0">
              <strong>We could not confirm whether the plan was created.</strong> The request was sent but its answer was lost.{" "}
              <UiLink href="/work/plans" className="text-[var(--ua-link)] underline">
                Check the plans list
              </UiLink>
              , then choose:
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                onClick={() => {
                  draft.clear();
                  navigate("/work/plans");
                }}
              >
                It was created
              </Button>
              <Button size="sm" onClick={() => draft.setValue((p) => ({ ...p, attempt: "" }))}>
                It was not created, create it
              </Button>
            </div>
          </div>
        )}
        {v.attempt === "parked" && write.state.status !== "pending_approval" && (
          <div className="flex flex-col gap-2">
            <p className="m-0 text-sm">
              This plan is waiting for approval. Once it is approved, run it from the notice below or from the Inbox; this form does not submit it a second time.
            </p>
            <ParkedWriteNotice approvalId={v.parkedApproval || null} label="This plan" />
            {stored.length === 0 && (
              <div>
                <Button size="sm" variant="ghost" onClick={() => draft.setValue((p) => ({ ...p, attempt: "", parkedApproval: "" }))}>
                  The parked plan was discarded, let me create it again
                </Button>
              </div>
            )}
          </div>
        )}
        <WriteFeedback
          state={write.state}
          what="The plan"
          onRun={(result) => {
            // Run from the notice after approval: the plan exists now, so finish the form.
            if (result.kind === "done" || result.kind === "accepted") {
              const data = (result.data ?? {}) as { plan_id?: string; plan?: { id?: string } };
              draft.clear();
              const id = data.plan_id ?? data.plan?.id;
              navigate(id ? `/work/plans/${encodeURIComponent(id)}` : "/work/plans");
            }
          }}
        />
        <div className="flex gap-2">
          <Button type="submit" variant="primary" disabled={!v.goal.trim() || filled.length === 0 || (v.attempt !== "" && !(v.attempt === "sending" && write.isRunning))} loading={write.isRunning}>
            Create plan
          </Button>
          <LinkButton href="/work/plans">Cancel</LinkButton>
        </div>
      </form>
    </>
  );
}

export function PlanPage() {
  const { id = "" } = useParams();
  const query = useQuery({ queryKey: WORK_KEYS.plan(id), queryFn: ({ signal }) => fetchPlanDetail(id, signal), staleTime: 5_000 });
  if (!query.data?.plan) {
    const { message, detail } = describeReadError(query.error);
    return (
      <>
        <PageHeader title={`Plan ${id}`} tabs={<WorkTabs />} />
        {query.status === "pending" ? (
          <Skeleton label="Loading the plan" lines={6} />
        ) : (
          <ErrorState title="The plan could not be loaded" message={/no plan|unknown plan|404/i.test(detail ?? "") ? "No plan has this id, or it was deleted." : message} detail={detail} onRetry={() => void query.refetch()} />
        )}
      </>
    );
  }
  if (isGone(query) || (query.isError && /no plan with id|unknown plan/i.test(String((query.error as Error | null)?.message ?? "")))) {
    return (
      <>
        <PageHeader title={query.data.plan.goal || `Plan ${id}`} tabs={<WorkTabs />} />
        <ErrorState title="This plan no longer exists" message="The hub no longer has this plan. It was deleted. Go back to the plans list." retryLabel="Check again" onRetry={() => void query.refetch()} />
      </>
    );
  }
  return (
    <>
      <RetainedReadNotice query={query} what="This plan" />
      <PlanDetail plan={query.data.plan} steps={query.data.steps} checkpoints={(query.data.checkpoints ?? []) as Checkpoint[]} nextStep={query.data.next_step_idx ?? null} />
    </>
  );
}

type Checkpoint = { seq?: number; note?: string | null; created_at?: string | null; [key: string]: unknown };

function usePlanAction(planId: string, label: string, success: string) {
  return useHubWrite<Record<string, unknown>, unknown>({
    path: WRITE_PATHS.planAction("update"),
    invalidates: [WORK_KEYS.plan(planId), WORK_KEYS.plans],
    label,
    successMessage: success,
    // Every plan route builds its arguments from the body and the verified
    // identity only, so repeating it after approval is exact (control-center-api.md).
  });
}

function PlanDetail({ plan, steps, checkpoints, nextStep }: { plan: PlanRow; steps: PlanStepRow[]; checkpoints: Checkpoint[]; nextStep: number | null }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const identity = useIdentity();
  const agents = useAgentOptions();
  const write = usePlanAction(plan.id, "Changing the plan", "Plan updated.");
  const run = (action: PlanAction, body: Record<string, unknown>) => write.run({ plan_id: plan.id, ...body }, { path: WRITE_PATHS.planAction(action) });
  // #2480 structure writes send the plan revision they were based on (expected_revision), so a plan
  // changed since answers 409. A dialog keeps the revision it opened with in its draft.
  const structure = (action: "step-add" | "step-edit" | "step-remove" | "reorder" | "edit", body: Record<string, unknown>, basedOn: number | null = planRevision(plan)) =>
    run(action, { ...body, ...(basedOn === null ? {} : { expected_revision: basedOn }) });
  const [removing, setRemoving] = useState<PlanStepRow | null>(null);
  const review = useHubWrite<{ intent: string }, unknown>({ path: WRITE_PATHS.planReview, invalidates: [WORK_KEYS.plan(plan.id), WORK_KEYS.plans], label: "Reviewing the plan", successMessage: "Review recorded.", resumable: false });

  type DialogKind = "hand" | "handoff" | "retry" | "edit-step" | "add-step" | "edit-plan" | "checkpoint" | "close" | "delete";
  type DialogState = null | { kind: "hand" | "handoff" | "retry" | "edit-step"; step: PlanStepRow } | { kind: Exclude<DialogKind, "hand" | "handoff" | "retry" | "edit-step"> };
  // The open dialog and what was typed in it are a draft (spec §6): kept through
  // errors, parked approvals and navigation, cleared when the action is done or cancelled.
  const draft = useDraft<{ kind: DialogKind | ""; stepIdx: number | null; stepId: number | null; text: string; owner: string | null; notify: boolean; title: string; description: string; revision: number | null }>({
    identity,
    operation: "plan.action",
    object: { plan: plan.id },
    initial: { kind: "", stepIdx: null, stepId: null, text: "", owner: null, notify: false, title: "", description: "", revision: null },
  });
  const d = draft.value;
  // A dialog follows its step by the step's immutable id, so a reorder never retargets it.
  const draftStep =
    d.stepId !== null ? steps.find((st) => st.id === d.stepId) : d.stepIdx === null ? undefined : steps.find((st) => st.step_idx === d.stepIdx);
  const dialog: DialogState =
    d.kind === ""
      ? null
      : d.kind === "hand" || d.kind === "handoff" || d.kind === "retry" || d.kind === "edit-step"
        ? draftStep
          ? { kind: d.kind, step: draftStep }
          : null
        : { kind: d.kind };
  const setDialog = (next: DialogState) =>
    next === null ? draft.clear() : draft.setValue((p) => ({ ...p, kind: next.kind, stepIdx: "step" in next ? next.step.step_idx : null, stepId: "step" in next ? stepIdOf(next.step) : null }));
  const text = d.text;
  const owner = d.owner;
  const notify = d.notify;
  const title = d.title;
  const description = d.description;
  const setText = (v: string) => draft.setValue((p) => ({ ...p, text: v }));
  const setOwner = (v: string | null) => draft.setValue((p) => ({ ...p, owner: v }));
  const setNotify = (v: boolean) => draft.setValue((p) => ({ ...p, notify: v }));
  const setTitle = (v: string) => draft.setValue((p) => ({ ...p, title: v }));
  const setDescription = (v: string) => draft.setValue((p) => ({ ...p, description: v }));
  const [resumed, setResumed] = useState<string | null>(null);
  const open = (next: NonNullable<DialogState>, initial = "", fields: { title?: string; description?: string } = {}) => {
    write.reset();
    // Reopening the same dialog keeps what was typed; another dialog starts fresh.
    const same = d.kind === next.kind && (d.stepId ?? d.stepIdx) === ("step" in next ? (stepIdOf(next.step) ?? next.step.step_idx) : null);
    if (same) return;
    draft.setValue({ kind: next.kind, stepIdx: "step" in next ? next.step.step_idx : null, stepId: "step" in next ? stepIdOf(next.step) : null, text: initial, owner: null, notify: false, title: fields.title ?? "", description: fields.description ?? "", revision: planRevision(plan) });
  };
  const finish = async (p: Promise<{ kind: string }>) => {
    const outcome = await p;
    if (outcome.kind === "done" || outcome.kind === "accepted") setDialog(null);
  };
  // The dialog's action, also run by Retry after a conflict reload.
  const submitDialog = () => {
                if (!dialog) return;
                switch (dialog.kind) {
                  case "hand":
                    void finish(run("update", { ...stepRef(dialog.step), owner, status: "open" }));
                    break;
                  case "handoff":
                    void finish(run("handoff", { ...stepRef(dialog.step), ...(text.trim() ? { note: text.trim() } : {}), ...(notify ? { notify_owner: true } : {}) }));
                    break;
                  case "retry":
                    void finish(run("retry", { ...stepRef(dialog.step), reason: text.trim() }));
                    break;
                  case "edit-step":
                    void finish(structure("step-edit", { ...stepRef(dialog.step), description: text.trim() }, d.revision));
                    break;
                  case "add-step":
                    void finish(structure("step-add", { description: text.trim(), ...(owner ? { owner } : {}) }, d.revision));
                    break;
                  case "edit-plan":
                    void finish(structure("edit", { title: title.trim(), description: description.trim() }, d.revision));
                    break;
                  case "checkpoint":
                    void finish(run("checkpoint", text.trim() ? { note: text.trim() } : {}));
                    break;
                  case "close":
                    void finish(run("close", { reason: text.trim() }));
                    break;
                }
  };
  // After a 409: reload the plan and rebase the open dialog on the revision just read.
  const reloadPlan = async () => {
    await queryClient.invalidateQueries({ queryKey: WORK_KEYS.plan(plan.id) });
    const fresh = queryClient.getQueryData<{ plan?: PlanRow }>(WORK_KEYS.plan(plan.id));
    if (fresh?.plan) draft.setValue((p) => ({ ...p, revision: planRevision(fresh.plan as PlanRow) }));
  };
  // A step that became claimed or done can no longer be edited or removed.
  const stepNowFixed = (st: PlanStepRow | undefined) => (st && fixed(steps.find((x) => x.id === st.id) ?? st) ? "That step is now claimed or done, so it can no longer be changed." : null);
  const ordered = [...steps].sort((a, b) => a.step_idx - b.step_idx);
  const move = (idx: number, delta: -1 | 1) => {
    const order = ordered.map((s) => s.step_idx);
    const at = order.indexOf(idx);
    const to = at + delta;
    if (to < 0 || to >= order.length) return;
    [order[at], order[to]] = [order[to], order[at]];
    void structure("reorder", { order });
  };
  /** A claimed (doing) or done step is never renumbered (control-center-api.md). */
  const fixed = (st: PlanStepRow | undefined) => !st || st.status === "doing" || st.status === "done";
  const terminal = plan.status === "done" || plan.status === "abandoned";
  // tools/plan.py _WORKFLOW_PLAN_GOAL_RE: a LangGraph run's plan; its title, description and steps are the run's binding.
  const workflowBound = /^governed workflow [0-9A-HJKMNP-TV-Z]{26}$/.test(plan.goal ?? "");

  return (
    <>
      <PageHeader
        meta={
          <span className="flex flex-wrap items-center gap-2">
            Plan {plan.id}
            <Badge size="sm" variant={plan.needs_human ? "warning" : "info"}>
              {plan.needs_human ? "Needs a person" : planLabel(plan.status)}
            </Badge>
          </span>
        }
        title={planTitle(plan)}
        description={typeof plan.description === "string" && plan.description ? plan.description : `By ${plan.author ?? "unknown"}, updated ${ago(plan.updated_at ?? plan.created_at)}.`}
        actions={
          <>
            <Button onClick={() => open({ kind: "checkpoint" })}>Save a checkpoint</Button>
            <Menu
              label="More"
              aria-label="More plan actions"
              align="end"
              items={[
                ...(workflowBound
                  ? []
                  : [
                      {
                        id: "edit",
                        label: "Edit title and description",
                        onSelect: () => open({ kind: "edit-plan" }, "", { title: planTitle(plan), description: typeof plan.description === "string" ? plan.description : "" }),
                      },
                      { id: "add", label: "Add a step", onSelect: () => open({ kind: "add-step" }) },
                    ]),
                { id: "close", label: "Close plan", disabled: terminal, onSelect: () => open({ kind: "close" }) },
                { kind: "separator", id: "sep" },
                { id: "delete", label: "Delete plan", onSelect: () => setDialog({ kind: "delete" }) },
              ]}
            />
          </>
        }
        tabs={<WorkTabs />}
      />
      <div className="flex flex-col gap-4">
        <WriteFeedback state={write.state} what="The plan change" />
        {plan.needs_human && (
          <Section title="Review" id="plan-review">
            <p className="m-0 text-sm">This plan is waiting for a person to approve it before agents continue.</p>
            <div className="flex gap-2">
              <Button variant="primary" loading={review.isRunning} onClick={() => void review.run({ intent: planReviewIntent("approve", plan.id) })}>
                Approve plan
              </Button>
              <Button loading={review.isRunning} onClick={() => void review.run({ intent: planReviewIntent("changes", plan.id) })}>
                Request changes
              </Button>
            </div>
            <WriteFeedback state={review.state} what="The review" />
          </Section>
        )}
        <Section
          title="Steps"
          id="plan-steps"
          actions={workflowBound ? undefined : <Button size="sm" onClick={() => open({ kind: "add-step" })}>Add a step</Button>}
        >
          {workflowBound && (
            <p className="m-0 text-sm text-[var(--ua-muted)]">
              This plan belongs to a workflow run: its title, description and steps are the run's binding, so they cannot be edited here. Step status,
              hand-offs and checkpoints still work.
            </p>
          )}
          {ordered.length === 0 ? (
            <p className="m-0 text-sm text-[var(--ua-muted)]">This plan has no steps yet.</p>
          ) : (
            <ol className="m-0 flex list-none flex-col gap-2 p-0">
              {ordered.map((step, i) => (
                <li key={step.step_idx} className={`flex flex-col gap-2 rounded-md border p-3 ${step.step_idx === nextStep ? "border-[var(--ua-accent)]" : "border-[var(--ua-border)]"}`}>
                  <div className="flex flex-wrap items-start gap-2">
                    <span className="font-mono text-xs text-[var(--ua-muted)]">{i + 1}.</span>
                    <p className="m-0 min-w-0 flex-1 text-sm font-medium">{step.description}</p>
                    <Badge size="sm" variant={step.status === "done" ? "success" : step.status === "failed" || step.status === "blocked" ? "danger" : "neutral"}>
                      {stepLabel(step.status)}
                    </Badge>
                    {step.step_idx === nextStep && <Badge size="sm" variant="info">Next</Badge>}
                  </div>
                  <p className="m-0 text-xs text-[var(--ua-muted)]">
                    {step.owner ? `Owner: ${step.owner}` : "No owner"}
                    {step.note ? ` · ${step.note}` : ""}
                    {step.retries ? ` · ${step.retries} failed attempt${step.retries === 1 ? "" : "s"}` : ""}
                  </p>
                  {step.outputs && Object.keys(step.outputs).length > 0 && (
                    // What the step handed on (plan_handoff outputs), read by the next step's owner.
                    <ul aria-label={`Outputs of step ${i + 1}`} className="m-0 flex list-none flex-col gap-0.5 p-0 text-xs">
                      {Object.entries(step.outputs).map(([k, val]) => {
                        const text = typeof val === "string" ? val : JSON.stringify(val);
                        return (
                          <li key={k}>
                            <span className="text-[var(--ua-muted)]">{k}: </span>
                            {/^https?:\/\//i.test(text) ? (
                              <UiLink href={text} className="break-all text-[var(--ua-link)] underline">
                                {text}
                              </UiLink>
                            ) : (
                              <span className="break-words">{text}</span>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  )}
                  <div className="flex flex-wrap items-center gap-2">
                    <Menu
                      label="Set status"
                      aria-label={`Set the status of step ${i + 1}`}
                      size="sm"
                      items={STEP_STATUSES.map((s) => ({
                        id: s,
                        label: stepLabel(s),
                        checked: (step.status ?? "open") === s,
                        onSelect: () => void run("update", { ...stepRef(step), status: s }),
                      }))}
                    />
                    <Button size="sm" onClick={() => open({ kind: "hand", step })}>
                      Hand to an agent
                    </Button>
                    <Menu
                      label="Step actions"
                      aria-label={`Actions for step ${i + 1}`}
                      size="sm"
                      items={[
                        { id: "claim", label: "Claim it myself", disabled: !identity, onSelect: () => void run("claim", { ...stepRef(step) }) },
                        { id: "handoff", label: "Mark done and hand on", onSelect: () => open({ kind: "handoff", step }) },
                        { id: "retry", label: "Record a failed attempt", onSelect: () => open({ kind: "retry", step }) },
                        ...(workflowBound
                          ? []
                          : [
                              { id: "edit", label: "Edit the text", disabled: fixed(step), onSelect: () => open({ kind: "edit-step", step }, step.description ?? "") },
                              { id: "up", label: "Move up", disabled: i === 0 || fixed(step) || fixed(ordered[i - 1]), onSelect: () => move(step.step_idx, -1) },
                              { id: "down", label: "Move down", disabled: i === ordered.length - 1 || fixed(step) || fixed(ordered[i + 1]), onSelect: () => move(step.step_idx, 1) },
                              { id: "remove", label: "Remove", disabled: fixed(step) || ordered.length === 1, onSelect: () => setRemoving(step) },
                            ]),
                      ]}
                    />
                  </div>
                </li>
              ))}
            </ol>
          )}
        </Section>
        <Section title="Checkpoints" id="plan-checkpoints">
          <p className="m-0 text-xs text-[var(--ua-muted)]">A checkpoint saves where the plan stands, so an agent can pick the work back up later.</p>
          {checkpoints.length === 0 ? (
            <p className="m-0 text-sm text-[var(--ua-muted)]">No checkpoints yet.</p>
          ) : (
            <ol className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
              {checkpoints.map((c, i) => (
                <li key={c.seq ?? i} className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">Checkpoint {c.seq ?? i + 1}</span>
                  <span className="text-[var(--ua-muted)]">{c.note ?? ""}</span>
                  <span className="text-xs text-[var(--ua-muted)]">{ago(c.created_at)}</span>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={async () => {
                      const outcome = await run("resume", { from_checkpoint: c.seq ?? i + 1 });
                      if (outcome.kind === "done") setResumed(`Resumed from checkpoint ${c.seq ?? i + 1}.`);
                    }}
                  >
                    Resume from here
                  </Button>
                </li>
              ))}
            </ol>
          )}
          {resumed && (
            <p role="status" className="m-0 text-sm">
              {resumed}
            </p>
          )}
        </Section>
        <JointPlanPanel planId={plan.id} identity={identity} />
        <Section title="About" id="plan-about">
          <DescriptionList
            items={[
              { term: "Status", value: planLabel(plan.status) },
              { term: "Group", value: plan.scope },
              { term: "Author", value: plan.author },
              { term: "Created", value: plan.created_at ? ago(plan.created_at) : null },
            ]}
          />
        </Section>
      </div>

      <Dialog
        open={dialog !== null && dialog.kind !== "delete"}
        onClose={() => setDialog(null)}
        title={
          dialog?.kind === "hand"
            ? "Hand this step to an agent"
            : dialog?.kind === "handoff"
              ? "Mark the step done and hand on"
              : dialog?.kind === "retry"
                ? "Record a failed attempt"
                : dialog?.kind === "edit-step"
                  ? "Edit the step"
                  : dialog?.kind === "add-step"
                    ? "Add a step"
                    : dialog?.kind === "edit-plan"
                      ? "Edit the plan"
                      : dialog?.kind === "checkpoint"
                        ? "Save a checkpoint"
                        : "Close the plan"
        }
        description={
          dialog?.kind === "hand"
            ? "The agent becomes the step's owner and picks it up next."
            : dialog?.kind === "retry"
              ? "After three failed attempts the plan stops and asks a person."
              : dialog?.kind === "close"
                ? "Closing keeps every step as it is and records why the plan stopped."
                : undefined
        }
        footer={
          <>
            <Button onClick={() => setDialog(null)}>Cancel</Button>
            <Button
              variant="primary"
              loading={write.isRunning}
              disabled={
                (dialog?.kind === "hand" && !owner) ||
                (((dialog?.kind === "retry" || dialog?.kind === "edit-step" || dialog?.kind === "add-step" || dialog?.kind === "close") && !text.trim()) ||
                (dialog?.kind === "edit-plan" && !title.trim()))
              }
              onClick={submitDialog}
            >
              {dialog?.kind === "hand" ? "Hand over" : dialog?.kind === "close" ? "Close plan" : "Save"}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          {dialog?.kind === "handoff" && (
            <Checkbox label="Tell the next step's owner" checked={notify} onChange={(e) => setNotify(e.target.checked)} />
          )}
          {(dialog?.kind === "hand" || dialog?.kind === "add-step") && (
            <Field label="Agent">
              <Combobox value={owner} onChange={setOwner} options={agents} placeholder="Choose an agent" />
            </Field>
          )}
          {dialog?.kind === "edit-plan" && (
            <>
              <Field label="Title" required hint="One line.">
                <Input value={title} onChange={(e) => setTitle(e.target.value.replace(/\n/g, " "))} />
              </Field>
              <Field label="Description">
                <Textarea rows={4} value={description} onChange={(e) => setDescription(e.target.value)} />
              </Field>
            </>
          )}
          {dialog && dialog.kind !== "hand" && dialog.kind !== "edit-plan" && (
            <Field
              label={
                dialog.kind === "retry"
                  ? "What went wrong"
                  : dialog.kind === "edit-step" || dialog.kind === "add-step"
                    ? "Step"
                    : dialog.kind === "close"
                        ? "Why it is closing"
                        : "Note"
              }
            >
              <Textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} />
            </Field>
          )}
          <WriteFeedback
            state={write.state}
            what="The change"
            onReload={reloadPlan}
            onRetry={submitDialog}
            retryBlocked={dialog && "step" in dialog && (dialog.kind === "edit-step") ? stepNowFixed(dialog.step) : null}
          />
        </div>
      </Dialog>
      <ConfirmDialog
        open={removing !== null}
        variant="danger"
        title="Remove this step?"
        objectName={removing?.description ?? undefined}
        consequence="The steps after it move up one place, and their dependencies are renumbered with them."
        confirmLabel="Remove step"
        busy={write.isRunning}
        onConfirm={async () => {
          if (!removing) return;
          const outcome = await structure("step-remove", { ...stepRef(removing) });
          // On a failure the confirmation stays open with the reason, Reload and Retry.
          if (outcome.kind !== "error" && outcome.kind !== "cancelled") setRemoving(null);
        }}
        onCancel={() => {
          setRemoving(null);
          write.reset();
        }}
      >
        <WriteFeedback
          state={write.state}
          what="Removing the step"
          onReload={() => queryClient.invalidateQueries({ queryKey: WORK_KEYS.plan(plan.id) })}
          onRetry={() => removing && structure("step-remove", { ...stepRef(removing) }, planRevision(queryClient.getQueryData<{ plan?: PlanRow }>(WORK_KEYS.plan(plan.id))?.plan ?? plan))}
          retryBlocked={stepNowFixed(removing ?? undefined)}
          className="mt-2"
        />
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog?.kind === "delete"}
        variant="danger"
        title="Delete this plan?"
        objectName={planTitle(plan)}
        consequence="The plan, its steps and its checkpoints are removed for everyone. This cannot be undone. Close the plan instead to keep its history."
        confirmLabel="Delete plan"
        busy={write.isRunning}
        onConfirm={async () => {
          const outcome = await run("delete", {});
          setDialog(null);
          if (outcome.kind === "done") navigate("/work/plans");
        }}
        onCancel={() => setDialog(null)}
      />
    </>
  );
}
