/**
 * Shared (joint) plans on the plan page (spec §9.2): bind, propose, review,
 * decide and recover. These need membership of the plan's office; anyone
 * else, the operator included, sees each action disabled with the reason.
 */
import { useQuery } from "@tanstack/react-query";
import { Button, Dialog, Field, Input, Select, Textarea } from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useHubWrite } from "../lib/mutations";
import { describeReadError } from "../lib/queries";
import { Skeleton } from "../../lib/ui";
import { fetchJoint, WORK_KEYS, WRITE_PATHS, type HubReadError, type JointAction, type JointView } from "./api";
import { Section, WriteFeedback } from "./shared";

/**
 * Whether the signed-in identity may take the joint actions, and why not.
 * `caller` is the caller's own team entry (null when not on the team); the
 * tool decides each write when it is made, and its refusal is shown then.
 */
export function jointPermissions(view: JointView | null, identity: string | null | undefined): { allowed: boolean; reason: string } {
  if (!identity) return { allowed: false, reason: "Sign in to take part in a shared plan." };
  if (!view) return { allowed: true, reason: "" };
  const member = view.caller !== undefined ? Boolean(view.caller) : view.team.some((m) => m.identity === identity);
  return member
    ? { allowed: true, reason: "" }
    : { allowed: false, reason: `Only members of the ${view.org_unit_id} office may change this shared plan. ${identity} is not a member, and being the operator does not change that.` };
}

/** How the joint read failed: not a shared plan, a membership refusal (with the hub's reason), or something else. */
export function jointReadFailure(error: unknown): { kind: "not-shared" } | { kind: "refused"; reason: string } | { kind: "other" } {
  const status = (error as { status?: number } | null)?.status;
  const text = ((error as HubReadError | null)?.hubError ?? "").trim();
  if (status === 400 && /not a joint plan|not joint|no joint|is not bound/i.test(text)) return { kind: "not-shared" };
  if (status === 400 && text) return { kind: "refused", reason: text };
  return { kind: "other" };
}

function stepText(step: unknown): string {
  if (typeof step === "string") return step;
  if (step && typeof step === "object" && typeof (step as { description?: unknown }).description === "string") return (step as { description: string }).description;
  return JSON.stringify(step);
}

type Form = { action: JointAction; revision: string; comment: string; text: string; decision: string; goal: string; office: string; steps: string };

export function JointPlanPanel({ planId, identity }: { planId: string; identity: string | null | undefined }) {
  const query = useQuery({ queryKey: WORK_KEYS.joint(planId), queryFn: ({ signal }) => fetchJoint(planId, signal), staleTime: 10_000, retry: false });
  // The open form and its text are a draft (spec §6): kept through errors,
  // parked approvals, revision conflicts and navigation.
  const draft = useDraft<{ open: boolean } & Form>({
    identity,
    operation: "plan.joint",
    object: { plan: planId },
    initial: { open: false, action: "propose", revision: "", comment: "", text: "", decision: "accept", goal: "", office: "", steps: "" },
  });
  const form: Form | null = draft.value.open ? draft.value : null;
  const setForm = (next: Form | null) => (next === null ? draft.clear() : draft.setValue({ ...next, open: true }));
  const write = useHubWrite<Record<string, unknown>, unknown>({
    path: WRITE_PATHS.jointAction("propose"),
    invalidates: [WORK_KEYS.joint(planId), WORK_KEYS.plan(planId)],
    label: "Changing the shared plan",
    successMessage: "Shared plan updated.",
  });
  const failure = query.status === "error" ? jointReadFailure(query.error) : null;
  const notShared = failure?.kind === "not-shared";
  const view = query.data ?? null;
  const perms = jointPermissions(view, identity);

  const start = (action: JointAction) => {
    write.reset();
    // Reopening the same action keeps what was typed.
    if (draft.value.open && draft.value.action === action) return;
    setForm({ action, revision: view ? String(view.revision) : "", comment: "", text: "", decision: "accept", goal: "", office: "", steps: "" });
  };

  const submit = async () => {
    if (!form) return;
    const base = { plan_id: planId };
    const revision = Number(form.revision);
    const comment = Number(form.comment);
    let body: Record<string, unknown>;
    switch (form.action) {
      case "bind":
        body = { ...base, goal_id: Number(form.goal), org_unit_id: form.office.trim(), ...(form.comment ? { comment_id: comment } : {}) };
        break;
      case "propose":
        body = { ...base, expected_revision: revision, comment_id: comment, steps: form.steps.split("\n").map((s) => s.trim()).filter(Boolean) };
        break;
      case "review":
        body = { ...base, expected_revision: revision, comment_id: comment, decision: form.decision };
        break;
      case "decide":
        body = { ...base, expected_revision: revision, comment_id: comment, text: form.text.trim() };
        break;
      case "recover":
        body = base;
        break;
    }
    const outcome = await write.run(body, { path: WRITE_PATHS.jointAction(form.action) });
    if (outcome.kind === "done") setForm(null);
  };

  // joint_planning.review: only a proposal still "proposed", by another member.
  const pending = view?.pending ?? null;
  const reviewBlocked: string | null = !pending
    ? "Nothing to review: no proposal is waiting."
    : pending.status !== "proposed"
      ? `Nothing to review: the proposal was sent back for changes (${String(pending.status ?? "").replace(/_/g, " ")}). Its author proposes again.`
      : pending.author === identity
        ? "You cannot review your own proposal; another member of the office must."
        : null;
  // A 409 from a joint write carries expected_revision and current_revision (control-center-api.md).
  const conflict = write.state.status === "error" && write.state.httpStatus === 409;

  const ACTIONS: { action: JointAction; label: string; help: string }[] = [
    { action: "propose", label: "Propose new steps", help: "Stage a new list of steps for the team to review. The live steps do not change yet." },
    { action: "review", label: "Review the proposal", help: "Another member accepts the proposal or asks for changes." },
    { action: "decide", label: "Record a decision", help: "Write down a decision, linked to the goal and a comment." },
    { action: "recover", label: "Recover after a conflict", help: "Re-read the winning version after two people changed the plan at once." },
  ];

  return (
    <Section title="Shared plan" id="plan-joint">
      <p className="m-0 text-xs text-[var(--ua-muted)]">
        A shared plan belongs to an office (a team). Its members propose step changes, review each other's proposals and record decisions; every change names the revision it builds on, so two edits at once are caught.
      </p>
      {query.status === "pending" ? (
        <Skeleton label="Loading the shared plan" lines={2} />
      ) : notShared ? (
        <div className="flex flex-col gap-2">
          <p className="m-0 text-sm">This plan is not shared with a team yet.</p>
          <div>
            <Button size="sm" disabled={!perms.allowed} onClick={() => start("bind")}>
              Share with a team
            </Button>
          </div>
        </div>
      ) : failure?.kind === "refused" ? (
        <div className="flex flex-col gap-2 text-sm">
          <p id="joint-reason" role="note" className="m-0 rounded-md border border-[var(--ua-border-strong)] px-3 py-2">
            You cannot see or change this shared plan: {failure.reason}. Only members of its office can, and being the operator does not change that.
          </p>
          <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
            {ACTIONS.map((a) => (
              <li key={a.action}>
                <Button size="sm" disabled aria-describedby="joint-reason">
                  {a.label}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      ) : query.status === "error" ? (
        <div role="alert" className="text-sm">
          <p className="m-0">{describeReadError(query.error).message}</p>
          <Button size="sm" onClick={() => void query.refetch()}>
            Retry
          </Button>
        </div>
      ) : view ? (
        <div className="flex flex-col gap-3 text-sm">
          <p className="m-0">
            Office <strong>{view.org_unit_id}</strong>, revision {view.revision}
            {view.recovered ? " (recovered)" : ""}. Team: {view.team.map((m) => `${m.identity}${m.role === "lead" ? " (lead)" : ""}`).join(", ") || "nobody"}.
          </p>
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <h3 className="m-0 mb-1 text-sm font-semibold">Live steps</h3>
              <ol aria-label="Live steps" className="m-0 flex flex-col gap-0.5 pl-5">
                {(view.steps ?? []).map((st, i) => (
                  <li key={i}>{stepText(st)}</li>
                ))}
              </ol>
            </div>
            <div>
              <h3 className="m-0 mb-1 text-sm font-semibold">Proposal</h3>
              {pending ? (
                <>
                  <p className="m-0 mb-1 text-[var(--ua-muted)]">
                    From {String(pending.author ?? "a member")}
                    {pending.comment_id ? ` (comment ${pending.comment_id})` : ""}:{" "}
                    {pending.status === "proposed" ? "waiting for review." : "the reviewer asked for changes."}
                  </p>
                  <ol aria-label="Proposed steps" className="m-0 flex flex-col gap-0.5 pl-5">
                    {(pending.steps ?? []).map((st, i) => (
                      <li key={i}>{stepText(st)}</li>
                    ))}
                  </ol>
                </>
              ) : (
                <p className="m-0 text-[var(--ua-muted)]">No proposal is waiting.</p>
              )}
            </div>
          </div>
          {view.review && (
            <p className="m-0">
              Last review: {String(view.review.reviewer ?? view.review.author ?? "a member")} {view.review.decision === "accept" ? "accepted the proposal" : "asked for changes"}
              {view.review.comment_id ? ` (comment ${String(view.review.comment_id)})` : ""}.
            </p>
          )}
          <div>
            <h3 className="m-0 mb-1 text-sm font-semibold">Decisions</h3>
            {(view.decisions ?? []).length === 0 ? (
              <p className="m-0 text-[var(--ua-muted)]">No decisions recorded.</p>
            ) : (
              <ul aria-label="Decisions" className="m-0 flex flex-col gap-1 pl-5">
                {(view.decisions ?? []).map((d, i) => (
                  <li key={i}>
                    {d.text} <span className="text-[var(--ua-muted)]">({String(d.author ?? "a member")}, revision {String(d.revision ?? "?")})</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {!perms.allowed && (
            <p id="joint-reason" role="note" className="m-0 rounded-md border border-[var(--ua-border-strong)] px-3 py-2">
              {perms.reason}
            </p>
          )}
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {ACTIONS.map((a) => {
              const allowed = perms.allowed && (a.action !== "review" || reviewBlocked === null);
              return (
                <li key={a.action} className="flex flex-wrap items-center gap-2">
                  <Button size="sm" disabled={!allowed} aria-describedby={!perms.allowed ? "joint-reason" : undefined} onClick={() => start(a.action)}>
                    {a.label}
                  </Button>
                  <span className="text-xs text-[var(--ua-muted)]">
                    {a.action === "review" && reviewBlocked && perms.allowed ? reviewBlocked : a.help}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      <WriteFeedback state={write.state} what="The shared-plan change" />
      <Dialog
        open={form !== null}
        onClose={() => setForm(null)}
        title={form ? (form.action === "bind" ? "Share with a team" : (ACTIONS.find((a) => a.action === form.action)?.label ?? "")) : ""}
        footer={
          <>
            <Button onClick={() => setForm(null)}>Cancel</Button>
            <Button variant="primary" loading={write.isRunning} onClick={() => void submit()}>
              {form?.action === "recover" ? "Recover" : "Submit"}
            </Button>
          </>
        }
      >
        {form && (
          <div className="flex flex-col gap-3">
            {form.action === "bind" && (
              <>
                <Field label="Goal number" required>
                  <Input inputMode="numeric" value={form.goal} onChange={(e) => setForm({ ...form, goal: e.target.value })} />
                </Field>
                <Field label="Office" required hint="The team that owns the plan, as named in the organization.">
                  <Input value={form.office} onChange={(e) => setForm({ ...form, office: e.target.value })} />
                </Field>
              </>
            )}
            {form.action !== "bind" && form.action !== "recover" && (
              <Field label="Builds on revision" hint="The revision you read. If someone changed the plan since, the change is refused and you can recover.">
                <Input inputMode="numeric" value={form.revision} onChange={(e) => setForm({ ...form, revision: e.target.value })} />
              </Field>
            )}
            {form.action !== "recover" && (
              <Field label="Comment number" required={form.action !== "bind"} hint="The task comment that explains this change. Post it first, then enter its number.">
                <Input inputMode="numeric" value={form.comment} onChange={(e) => setForm({ ...form, comment: e.target.value })} />
              </Field>
            )}
            {form.action === "propose" && (
              <Field label="New steps, one per line" required>
                <Textarea rows={6} value={form.steps} onChange={(e) => setForm({ ...form, steps: e.target.value })} />
              </Field>
            )}
            {form.action === "review" && (
              <Field label="Decision">
                <Select
                  value={form.decision}
                  onChange={(e) => setForm({ ...form, decision: e.target.value })}
                  options={[
                    { value: "accept", label: "Accept the proposal" },
                    { value: "changes", label: "Ask for changes" },
                  ]}
                />
              </Field>
            )}
            {form.action === "decide" && (
              <Field label="Decision" required>
                <Textarea rows={3} value={form.text} onChange={(e) => setForm({ ...form, text: e.target.value })} />
              </Field>
            )}
            {form.action === "recover" && <p className="m-0">This re-reads the version of the plan that won and shows its current revision.</p>}
            {conflict ? (
              <div role="alert" className="flex flex-col gap-2 rounded-md border border-[var(--ua-warn)] px-3 py-2">
                <p className="m-0">
                  <strong>Someone changed this shared plan since you read it.</strong> Nothing was saved and your text is kept. Reload to see the
                  current revision and build on it, or recover the plan if it is stuck after a conflict.
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    onClick={async () => {
                      const fresh = await query.refetch();
                      if (fresh.data) setForm({ ...form, revision: String(fresh.data.revision) });
                      write.reset();
                    }}
                  >
                    Reload the shared plan
                  </Button>
                  <Button
                    size="sm"
                    onClick={async () => {
                      const outcome = await write.run({ plan_id: planId }, { path: WRITE_PATHS.jointAction("recover") });
                      if (outcome.kind === "done") {
                        const fresh = await query.refetch();
                        if (fresh.data) setForm({ ...form, revision: String(fresh.data.revision) });
                      }
                    }}
                  >
                    Recover
                  </Button>
                </div>
              </div>
            ) : (
              <WriteFeedback state={write.state} what="The change" />
            )}
          </div>
        )}
      </Dialog>
    </Section>
  );
}
