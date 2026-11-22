/**
 * The task page's sections below the description: subtasks, dependencies,
 * activity (notes, runs, evidence, pull request), comments, the goal it
 * serves, the subtree hold, and evidence and work products.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { fetchEvidence } from "../../lib/api/evidence";
import { fetchWhyChain } from "../../lib/api/goals";
import { fetchTaskComments, fetchTaskHolds, heldTaskMap, TASK_HOLDS_QUERY_KEY } from "../../lib/api/tasks";
import {
  Button,
  Combobox,
  ConfirmDialog,
  DataTable,
  EmptyState,
  Field,
  Input,
  LinkButton,
  Markdown,
  Select,
  Skeleton,
  Textarea,
  UiLink,
  type Column,
} from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { approvalStatusQueryKey, fetchApprovalStatus } from "../lib/parkedWrites";
import { fetchTaskRuns, getJson, WORK_KEYS, WRITE_PATHS, type GoalRow, type RunRow, type TaskRow } from "./api";
import { ago, evidenceUrl, relationEdge, statusLabel, type WorkTask } from "./model";
import { isUnknownOutcome, newKey, recordBeforeSend, Section, updateDraftAt, useLatest, useBoard, useIdentity, useReconcileParkedDraft, useGoalsQuery, useTaskGraphQuery, WriteFeedback } from "./shared";
import { useEpoch } from "../lib/epoch";

// --- Subtasks -------------------------------------------------------------------

export function SubtasksSection({ task, tasks }: { task: TaskRow; tasks: readonly WorkTask[] }) {
  const board = useBoard();
  const subtasks = tasks.filter((t) => t.parentId === task.id);
  return (
    <Section
      title="Subtasks"
      id="task-subtasks"
      actions={
        <LinkButton size="sm" href={`/work/tasks/new?parent=${task.id}`}>
          Add a subtask
        </LinkButton>
      }
    >
      {/* The subtask list comes from the board read: "none" only after it succeeded. */}
      <QueryRegion query={board} loadingLabel="Loading subtasks" errorTitle="Subtasks could not be loaded" headingLevel={3} empty={{ title: "" }}>
        {() =>
          subtasks.length === 0 ? (
            <p className="m-0 text-sm text-[var(--ua-muted)]">No subtasks in the current work. Done subtasks older than 30 days are not listed.</p>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
              {subtasks.map((s) => (
                <li key={s.id}>
                  <UiLink href={`/work/tasks/${s.id}`} className="text-[var(--ua-link)] underline">
                    #{s.id} {s.title}
                  </UiLink>{" "}
                  <span className="text-[var(--ua-muted)]">· {statusLabel(s.status)}</span>
                </li>
              ))}
            </ul>
          )
        }
      </QueryRegion>
    </Section>
  );
}

// --- Dependencies -----------------------------------------------------------------

type DependencyRow = { otherId: number; relation: "blocked by" | "blocks"; dependencyId: number | null };

export function DependenciesSection({ task, tasks }: { task: TaskRow; tasks: readonly WorkTask[] }) {
  const graph = useTaskGraphQuery();
  const [pick, setPick] = useState<string | null>(null);
  const [removing, setRemoving] = useState<DependencyRow | null>(null);
  const write = useHubWrite<Record<string, unknown>, unknown>({
    path: WRITE_PATHS.dependency,
    invalidates: [["work"]],
    label: "Changing a dependency",
    successMessage: "Dependencies updated.",
    resumable: false,
  });
  const me = `task:${task.id}`;
  const rows: DependencyRow[] = [];
  for (const link of graph.data?.links ?? []) {
    const edge = relationEdge(String(link.kind ?? ""), link.source, link.target, "");
    if (!edge || edge.label !== "blocks") continue;
    const depId = typeof link.dependency_id === "number" ? link.dependency_id : null;
    if (edge.target === me) rows.push({ otherId: Number(edge.source.replace("task:", "")), relation: "blocked by", dependencyId: depId });
    else if (edge.source === me) rows.push({ otherId: Number(edge.target.replace("task:", "")), relation: "blocks", dependencyId: depId });
  }
  const titleOf = (id: number) => tasks.find((t) => t.id === id)?.title ?? "";
  return (
    <Section title="Dependencies" id="task-dependencies">
      <QueryRegion
        query={graph}
        loadingLabel="Loading dependencies"
        errorTitle="Dependencies could not be loaded"
        headingLevel={3}
        empty={{ title: "" }}
      >
        {() =>
          rows.length === 0 ? (
            <p className="m-0 text-sm text-[var(--ua-muted)]">This task does not wait for other tasks, and none wait for it.</p>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
              {rows.map((r) => (
                <li key={`${r.relation}-${r.otherId}`} className="flex flex-wrap items-center gap-2">
                  <span className="text-[var(--ua-muted)]">{r.relation === "blocked by" ? "Blocked by" : "Blocks"}</span>
                  <UiLink href={`/work/tasks/${r.otherId}`} className="text-[var(--ua-link)] underline">
                    #{r.otherId} {titleOf(r.otherId)}
                  </UiLink>
                  {r.dependencyId !== null ? (
                    <Button size="sm" variant="ghost" aria-label={`Remove dependency on #${r.otherId}`} onClick={() => setRemoving(r)}>
                      Remove
                    </Button>
                  ) : (
                    <span className="text-xs text-[var(--ua-muted)]">This hub does not report the link's id, so it cannot be removed here.</span>
                  )}
                </li>
              ))}
            </ul>
          )
        }
      </QueryRegion>
      <form
        aria-label="Add a dependency"
        className="flex flex-wrap items-end gap-2"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!pick) return;
          const outcome = await write.run({ operation: "add", task_id: task.id, depends_on_task_id: Number(pick) });
          if (outcome.kind === "done") setPick(null);
        }}
      >
        <Field label="This task is blocked by" className="min-w-60 flex-1">
          <Combobox
            value={pick}
            onChange={setPick}
            placeholder="Choose a task"
            options={tasks.filter((t) => t.id !== task.id).map((t) => ({ value: String(t.id), label: `#${t.id} ${t.title}` }))}
          />
        </Field>
        <Button type="submit" disabled={!pick} loading={write.isRunning}>
          Add dependency
        </Button>
      </form>
      <WriteFeedback state={write.state} what="The dependency change" />
      <ConfirmDialog
        open={removing !== null}
        title="Remove this dependency?"
        objectName={removing ? `${removing.relation === "blocked by" ? "Blocked by" : "Blocks"} #${removing.otherId} ${titleOf(removing.otherId)}` : undefined}
        consequence="The two tasks no longer wait for each other."
        confirmLabel="Remove"
        variant="danger"
        busy={write.isRunning}
        onConfirm={async () => {
          if (removing?.dependencyId != null) await write.run({ operation: "remove", dependency_id: removing.dependencyId });
          setRemoving(null);
        }}
        onCancel={() => setRemoving(null)}
      />
    </Section>
  );
}

// --- Activity ---------------------------------------------------------------------

export function ActivitySection({ task }: { task: TaskRow }) {
  const [older, setOlder] = useState<{ runs: RunRow[]; next: string | null; loaded: boolean; loading: boolean; error: boolean }>({ runs: [], next: null, loaded: false, loading: false, error: false });
  const loadOlder = async (cursor: string | null) => {
    if (!cursor) return;
    setOlder((o) => ({ ...o, loading: true, error: false }));
    try {
      const page = await fetchTaskRuns(String(task.id), undefined, cursor);
      setOlder((o) => ({ runs: [...o.runs, ...page.runs], next: page.next_cursor ?? null, loaded: true, loading: false, error: false }));
    } catch {
      setOlder((o) => ({ ...o, loading: false, error: true }));
    }
  };
  const runs = useQuery({ queryKey: WORK_KEYS.taskRuns(String(task.id)), queryFn: ({ signal }) => fetchTaskRuns(String(task.id), signal), staleTime: 15_000 });
  const evidence = useQuery({
    queryKey: WORK_KEYS.taskEvidence(String(task.id)),
    queryFn: ({ signal }) => fetchEvidence({ task_id: String(task.id) }, signal),
    staleTime: 15_000,
  });
  const record = evidence.data?.records?.find((r) => r.task_id === task.id) ?? evidence.data?.records?.[0];
  const pr = record?.proof.pr_url ?? evidenceUrl(task.evidence_ref);
  const columns: Column<RunRow>[] = [
    { id: "run", header: "Run", cell: (r) => <code className="text-xs">{r.run_id}</code> },
    { id: "agent", header: "Agent", cell: (r) => r.agent ?? "Unknown" },
    { id: "status", header: "Status", cell: (r) => r.outcome ?? r.status ?? "Unknown" },
    { id: "started", header: "Started", cell: (r) => ago(r.started_at) },
  ];
  return (
    <Section title="Activity" id="task-activity">
      <div className="flex flex-col gap-1">
        <h3 className="m-0 text-sm font-semibold">Pull request</h3>
        {pr ? (
          <UiLink href={pr} className="break-all text-sm text-[var(--ua-link)] underline">
            {pr}
          </UiLink>
        ) : evidence.status === "pending" ? (
          <Skeleton label="Loading the pull request" lines={1} />
        ) : evidence.status === "error" ? (
          <p className="m-0 text-sm text-[var(--ua-muted)]">The evidence could not be read, so it is not known whether a pull request is recorded.</p>
        ) : (
          <p className="m-0 text-sm text-[var(--ua-muted)]">No pull request is recorded for this task.</p>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <h3 className="m-0 text-sm font-semibold">Runs</h3>
        <QueryRegion query={runs} loadingLabel="Loading runs" errorTitle="Runs could not be loaded" headingLevel={4} isEmpty={(d) => d.runs.length === 0} empty={{ title: "No runs yet", description: "A run appears when an agent starts working on this task." }}>
          {(d) => (
            <>
              <DataTable caption="Runs for this task" columns={columns} data={[...d.runs, ...older.runs]} getRowKey={(r) => r.run_id} />
              {(older.loaded ? older.next : d.next_cursor) && (
                <div>
                  <Button size="sm" loading={older.loading} onClick={() => void loadOlder(older.loaded ? older.next : (d.next_cursor ?? null))}>
                    Show older runs
                  </Button>
                </div>
              )}
              {older.error && (
                <p role="alert" className="m-0 text-sm">
                  Older runs could not be loaded. Try again.
                </p>
              )}
            </>
          )}
        </QueryRegion>
      </div>
      <div className="flex flex-col gap-1">
        <h3 className="m-0 text-sm font-semibold">Evidence trail</h3>
        <QueryRegion
          query={evidence}
          loadingLabel="Loading evidence"
          errorTitle="Evidence could not be loaded"
          headingLevel={4}
          isEmpty={(d) => !d.records?.length}
          empty={{ title: "No evidence yet", description: "Evidence is the proof a task was done: a merged pull request, a commit or an attached record." }}
        >
          {() =>
            record ? (
              <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
                <li>Done by {record.agent || "an unknown agent"}{record.human ? ` for ${record.human}` : ""}</li>
                {record.proof.commit_sha && (
                  <li>
                    Commit <code>{record.proof.commit_sha.slice(0, 12)}</code>
                  </li>
                )}
                <li>{record.proof.merge_verified ? "Merge verified." : "Merge not verified yet."}</li>
                <li>{record.proof.events_count} recorded event{record.proof.events_count === 1 ? "" : "s"}</li>
              </ul>
            ) : null
          }
        </QueryRegion>
      </div>
    </Section>
  );
}

// --- Comments ---------------------------------------------------------------------

export function CommentsSection({ task, identity }: { task: TaskRow; identity: string | null | undefined }) {
  const id = String(task.id);
  const comments = useQuery({ queryKey: WORK_KEYS.taskComments(id), queryFn: ({ signal }) => fetchTaskComments(id, signal), staleTime: 10_000 });
  // One idempotency key per submitted text, and the approval it parked behind (see AddNote).
  const draft = useDraft<{ body: string; key: string; parkedApproval: string; submitted: string }>({
    identity,
    operation: "task.comment",
    object: { id },
    initial: { body: "", key: "", parkedApproval: "", submitted: "" },
  });
  useReconcileParkedDraft(draft.value.parkedApproval, draft.value.submitted, draft.value.body, draft.clear);
  const latestDraft = useLatest(draft);
  const epoch = useEpoch();
  const write = useHubWrite<{ body: string; idempotency_key: string }, unknown>({
    path: WRITE_PATHS.comments(id),
    invalidates: [WORK_KEYS.taskComments(id)],
    label: `Commenting on task #${id}`,
    successMessage: "Comment posted.",
  });
  return (
    <Section title="Comments" id="task-comments">
      <QueryRegion
        query={comments}
        loadingLabel="Loading comments"
        errorTitle="Comments could not be loaded"
        headingLevel={3}
        isEmpty={(d) => d.comments.length === 0}
        empty={{ title: "No comments yet", description: "Comments are a shared thread for people and agents working on this task." }}
      >
        {(d) => (
          <ol className="m-0 flex list-none flex-col gap-3 p-0">
            {d.comments.map((c) => (
              <li key={c.id} className="rounded-md border border-[var(--ua-border)] p-3">
                <p className="m-0 mb-1 text-xs text-[var(--ua-muted)]">
                  {c.author}
                  {c.parent_comment_id ? ` · reply to comment ${c.parent_comment_id}` : ""} · comment {c.id}
                </p>
                <Markdown source={c.body} />
              </li>
            ))}
          </ol>
        )}
      </QueryRegion>
      <form
        aria-label="Write a comment"
        className="flex flex-col gap-2"
        onSubmit={async (e) => {
          e.preventDefault();
          const body = draft.value.body.trim();
          if (!body) return;
          // The draft key and the text sent are captured before the request: the answer changes only
          // that draft, and only while it still holds that text (review round 3, blocker 4).
          const draftAt = draft.key;
          const sent = draft.value.body;
          const key = draft.value.key || newKey(`comment-${id}`);
          if (!draft.value.key) draft.setValue((p) => ({ ...p, key }));
          const outcome = await write.run({ body, idempotency_key: key });
          if (outcome.kind === "done" || outcome.kind === "accepted") updateDraftAt(epoch, latestDraft.current, draftAt, (p) => (p.body === sent ? null : { ...p, key: "", parkedApproval: "", submitted: "" }));
          else if (outcome.kind === "pending_approval") updateDraftAt(epoch, latestDraft.current, draftAt, (p) => ({ ...p, parkedApproval: outcome.approvalId ?? "", submitted: sent }));
        }}
      >
        <Field label="Comment">
          <Textarea rows={3} value={draft.value.body} onChange={(e) => draft.setValue((p) => ({ ...p, body: e.target.value, key: "" }))} />
        </Field>
        <div>
          <Button type="submit" size="sm" disabled={!draft.value.body.trim()} loading={write.isRunning}>
            Post comment
          </Button>
        </div>
        <WriteFeedback state={write.state} what="The comment" />
      </form>
    </Section>
  );
}

// --- Goal -------------------------------------------------------------------------

export function GoalSection({ task }: { task: TaskRow }) {
  const id = String(task.id);
  const why = useQuery({ queryKey: WORK_KEYS.taskWhy(id), queryFn: ({ signal }) => fetchWhyChain(id, signal), staleTime: 30_000 });
  const goals = useGoalsQuery();
  const [goalId, setGoalId] = useState("");
  const link = useHubWrite<{ task_id: number; goal_id: number }, unknown>({
    path: WRITE_PATHS.goalAction("link"),
    invalidates: [WORK_KEYS.taskWhy(id), ["work", "goal"], ["work", "work-graph"]],
    label: `Linking task #${id} to a goal`,
    successMessage: "Task linked to the goal.",
    resumable: false,
  });
  const flat: { id: number; title: string; project?: string | null }[] = [];
  const walk = (list: readonly GoalRow[]) => {
    for (const g of list ?? []) {
      flat.push({ id: g.id, title: g.title, project: g.project });
      walk(g.children ?? []);
    }
  };
  walk(goals.data?.tree ?? []);
  const sameProject = flat.filter((g) => !g.project || g.project === (task.project ?? "unified-agent"));
  return (
    <Section title="Goal" id="task-goal">
      <QueryRegion query={why} loadingLabel="Loading the goal" errorTitle="The goal could not be loaded" headingLevel={3} empty={{ title: "" }}>
        {(d) =>
          d.chain.length === 0 ? (
            <p className="m-0 text-sm text-[var(--ua-muted)]">This task does not serve a goal yet. A goal says why the work matters.</p>
          ) : (
            <ol aria-label="Why this task matters" className="m-0 flex list-none flex-col gap-0.5 p-0 text-sm">
              {d.chain.map((g, i) => (
                <li key={g.id} style={{ paddingLeft: `${i * 0.75}rem` }}>
                  <UiLink href={`/work/goals/${g.id}`} className="text-[var(--ua-link)] underline">
                    {g.title}
                  </UiLink>
                </li>
              ))}
            </ol>
          )
        }
      </QueryRegion>
      <form
        aria-label="Link to a goal"
        className="flex flex-wrap items-end gap-2"
        onSubmit={async (e) => {
          e.preventDefault();
          if (goalId) await link.run({ task_id: task.id, goal_id: Number(goalId) });
        }}
      >
        <Field label="Serves goal" className="min-w-48 flex-1" hint="Only goals in the task's project can be linked.">
          <Select value={goalId} onChange={(e) => setGoalId(e.target.value)} placeholder="Choose a goal" options={sameProject.map((g) => ({ value: String(g.id), label: g.title }))} />
        </Field>
        <Button type="submit" disabled={!goalId} loading={link.isRunning}>
          Link
        </Button>
      </form>
      <WriteFeedback state={link.state} what="The goal link" />
    </Section>
  );
}

// --- Hold -------------------------------------------------------------------------

export function HoldSection({ task }: { task: TaskRow }) {
  const holds = useQuery({ queryKey: TASK_HOLDS_QUERY_KEY, queryFn: ({ signal }) => fetchTaskHolds(signal), staleTime: 15_000 });
  const hold = holds.data ? heldTaskMap(holds.data).get(task.id) : undefined;
  // The reason is a draft (spec §6): kept through errors, parked approvals and leaving the page.
  const reasonDraft = useDraft<{ reason: string }>({ identity: useIdentity(), operation: "task.pause", object: { id: task.id }, initial: { reason: "" } });
  const reason = reasonDraft.value.reason;
  const setReason = (v: string) => (v === "" ? reasonDraft.clear() : reasonDraft.setValue({ reason: v }));
  const [confirmRelease, setConfirmRelease] = useState(false);
  const place = useHubWrite<{ root_task_id: number; mode: "pause"; reason: string }, unknown>({
    path: WRITE_PATHS.holds,
    invalidates: [TASK_HOLDS_QUERY_KEY, ["work"]],
    label: `Pausing task #${task.id} and its subtasks`,
    successMessage: "Task and subtasks paused.",
    resumable: false,
  });
  const release = useHubWrite<Record<string, never>, unknown>({
    path: hold ? WRITE_PATHS.holdRelease(hold.id) : WRITE_PATHS.holds,
    invalidates: [TASK_HOLDS_QUERY_KEY, ["work"]],
    label: "Resuming the paused tasks",
    successMessage: "Pause released.",
    resumable: false,
  });
  return (
    <Section title="Pause" id="task-hold">
      <p className="m-0 text-xs text-[var(--ua-muted)]">Pausing stops agents from picking up this task and every task under it until the pause is released.</p>
      <QueryRegion query={holds} loadingLabel="Loading pauses" errorTitle="Pauses could not be loaded" headingLevel={3} empty={{ title: "" }}>
        {() =>
          hold ? (
            <div className="flex flex-col gap-2 text-sm">
              <p className="m-0">
                Paused by {hold.created_by}
                {hold.root_task_id !== task.id ? ` through task #${hold.root_task_id}` : ""}
                {hold.reason ? `: ${hold.reason}` : "."}
              </p>
              <div>
                <Button size="sm" onClick={() => setConfirmRelease(true)}>
                  Release the pause
                </Button>
              </div>
            </div>
          ) : (
            <form
              aria-label="Pause this task and its subtasks"
              className="flex flex-col gap-2"
              onSubmit={async (e) => {
                e.preventDefault();
                if (!reason.trim()) return;
                const outcome = await place.run({ root_task_id: task.id, mode: "pause", reason: reason.trim() });
                if (outcome.kind === "done") setReason("");
              }}
            >
              <Field label="Why pause">
                <Input value={reason} onChange={(e) => setReason(e.target.value)} />
              </Field>
              <div>
                <Button type="submit" size="sm" disabled={!reason.trim()} loading={place.isRunning}>
                  Pause task and subtasks
                </Button>
              </div>
            </form>
          )
        }
      </QueryRegion>
      <WriteFeedback state={place.state} what="The pause" />
      <WriteFeedback state={release.state} what="Releasing the pause" />
      <ConfirmDialog
        open={confirmRelease}
        title="Release this pause?"
        objectName={hold ? `Pause on task #${hold.root_task_id}` : undefined}
        consequence="Agents may pick up the task and every task under it again."
        confirmLabel="Release"
        busy={release.isRunning}
        onConfirm={async () => {
          await release.run({});
          setConfirmRelease(false);
        }}
        onCancel={() => setConfirmRelease(false)}
      />
    </Section>
  );
}

// --- Evidence and work products ----------------------------------------------------

/**
 * A submission or release from this task whose result is not settled: parked behind an approval,
 * or sent with its response lost (`phase`). While one exists, no new submission is made.
 */
type ParkedRelease = { workProductId: number | null; approvalId: string | null; phase?: "submit_unknown" | "release_unknown" };
// Under the draft prefix, so sign-out clears it with the drafts.

export function EvidenceSection({ task, identity }: { task: TaskRow; identity: string | null | undefined }) {
  const evidenceDraft = useDraft<{ kind: string; ref: string; note: string }>({
    identity,
    operation: "task.evidence",
    object: { id: task.id },
    initial: { kind: "link", ref: "", note: "" },
  });
  const productDraft = useDraft<{ source_path: string; title: string; template_pack: string }>({
    identity,
    operation: "task.work-product",
    object: { id: task.id },
    initial: { source_path: "", title: "", template_pack: "" },
  });
  const attach = useHubWrite<{ channel: string; payload: Record<string, unknown>; dedupe_key: string }, { event_ulid?: string }>({
    path: WRITE_PATHS.evidence,
    invalidates: [WORK_KEYS.taskEvidence(String(task.id))],
    label: `Attaching evidence to task #${task.id}`,
    successMessage: "Evidence attached.",
    resumable: false,
  });
  const submit = useHubWrite<Record<string, unknown>, { id?: number; work_product_id?: number; status?: string }>({
    path: WRITE_PATHS.workProducts,
    invalidates: [["work"]],
    label: "Submitting the work product",
    successMessage: "Work product submitted for release.",
    // Never repeat a submission: it re-hashes the file and can create a second one (spec §6).
    resumable: false,
  });
  const release = useHubWrite<undefined, unknown>({
    path: WRITE_PATHS.workProducts,
    invalidates: [["work"]],
    label: "Releasing the work product",
    successMessage: "Work product released.",
    resumable: false,
  });
  // The release continuation is a draft keyed by identity and task (review round 3, blocker 2): a
  // cached page reused for another task, or an identity that resolves late, loads its own.
  const releaseDraft = useDraft<{ active: boolean; workProductId: number | null; approvalId: string | null; phase: "" | "submit_unknown" | "release_unknown" }>({
    identity,
    operation: "work-product.release",
    object: { id: task.id },
    initial: { active: false, workProductId: null, approvalId: null, phase: "" },
  });
  const rd = releaseDraft.value;
  const latestRelease = useLatest(releaseDraft);
  const epoch = useEpoch();
  const latestProduct = useLatest(productDraft);
  const parked: ParkedRelease | null = rd.active ? { workProductId: rd.workProductId, approvalId: rd.approvalId, ...(rd.phase ? { phase: rd.phase } : {}) } : null;
  const [manualId, setManualId] = useState("");
  const approvalRead = useQuery({
    queryKey: approvalStatusQueryKey(parked?.approvalId ?? "none", epoch.ownerKey),
    queryFn: ({ signal }) => fetchApprovalStatus(parked?.approvalId ?? "", signal),
    enabled: Boolean(parked?.approvalId),
    staleTime: 5_000,
    refetchInterval: 15_000,
  });
  const approvalState: string | null = !parked?.approvalId ? null : approvalRead.status === "error" ? "unverified" : (approvalRead.data?.resolution ?? "loading");
  /**
   * Record or clear the continuation. `key` is the draft key captured before a request was sent,
   * so an answer that arrives after the person moved to another task changes that task's record.
   */
  const remember = (next: ParkedRelease | null, key: string = latestRelease.current.key) => {
    const current = latestRelease.current;
    const value = next ? { active: true, workProductId: next.workProductId, approvalId: next.approvalId, phase: next.phase ?? ("" as const) } : null;
    if (value && key === current.key) recordBeforeSend(epoch, current, value);
    else updateDraftAt(epoch, current, key, () => value);
  };

  const [lookupFailed, setLookupFailed] = useState(false);
  const lookUp = async (approvalId: string | null, key: string = releaseDraft.key, productKey: string = productDraft.key, sent: string = "") => {
    if (!approvalId) {
      setLookupFailed(true);
      return;
    }
    try {
      const approval = await getJson<{ payload?: { work_product_id?: number } }>(`/api/approvals/${encodeURIComponent(approvalId)}`, "approval");
      const id = typeof approval.payload?.work_product_id === "number" ? approval.payload.work_product_id : null;
      if (id === null) throw new Error("no work_product_id in the approval payload");
      remember({ workProductId: id, approvalId }, key);
      setLookupFailed(false);
      updateDraftAt(epoch, latestProduct.current, productKey, (p) => (!sent || productFingerprint(p) === sent ? null : p));
    } catch {
      setLookupFailed(true);
    }
  };
  const ev = evidenceDraft.value;
  const wp = productDraft.value;
  const releaseId = parked?.workProductId ?? (manualId ? Number(manualId) : null);

  return (
    <Section title="Evidence and work products" id="task-evidence">
      <form
        aria-label="Attach evidence"
        className="flex flex-col gap-2"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!ev.ref.trim()) return;
          const outcome = await attach.run({
            channel: "sdk.task_evidence",
            payload: { task_id: task.id, kind: ev.kind, ref: ev.ref.trim(), ...(ev.note.trim() ? { note: ev.note.trim() } : {}) },
            dedupe_key: `task-${task.id}-${ev.kind}-${ev.ref.trim()}`.slice(0, 250),
          });
          if (outcome.kind === "done") evidenceDraft.clear();
        }}
      >
        <h3 className="m-0 text-sm font-semibold">Attach evidence</h3>
        <p className="m-0 text-xs text-[var(--ua-muted)]">Recorded in the tamper-evident evidence ledger with your name.</p>
        <div className="grid gap-2 md:grid-cols-[10rem_1fr]">
          <Field label="Kind">
            <Select
              value={ev.kind}
              onChange={(e) => evidenceDraft.setValue((p) => ({ ...p, kind: e.target.value }))}
              options={[
                { value: "link", label: "Link" },
                { value: "pull_request", label: "Pull request" },
                { value: "commit", label: "Commit" },
                { value: "test_run", label: "Test run" },
                { value: "note", label: "Note" },
              ]}
            />
          </Field>
          <Field label="Reference" hint="A URL, commit id or short identifier.">
            <Input value={ev.ref} onChange={(e) => evidenceDraft.setValue((p) => ({ ...p, ref: e.target.value }))} />
          </Field>
        </div>
        <Field label="Note">
          <Textarea rows={2} value={ev.note} onChange={(e) => evidenceDraft.setValue((p) => ({ ...p, note: e.target.value }))} />
        </Field>
        <div>
          <Button type="submit" size="sm" disabled={!ev.ref.trim()} loading={attach.isRunning}>
            Attach evidence
          </Button>
        </div>
        <WriteFeedback state={attach.state} what="The evidence" />
      </form>

      <form
        aria-label="Submit a work product"
        className="flex flex-col gap-2 border-t border-[var(--ua-border)] pt-3"
        onSubmit={async (e) => {
          e.preventDefault();
          if (parked !== null || !wp.source_path.trim() || !wp.title.trim() || !wp.template_pack.trim()) return;
          // Keys and the submitted fields are captured before the send; the attempt is recorded before
          // it leaves, so leaving mid-request blocks another submission until it is settled.
          const key = releaseDraft.key;
          const productKey = productDraft.key;
          const sent = productFingerprint(wp);
          remember({ workProductId: null, approvalId: null, phase: "submit_unknown" }, key);
          const outcome = await submit.run({
            source_path: wp.source_path.trim(),
            title: wp.title.trim(),
            template_pack: wp.template_pack.trim(),
            tags: { task: String(task.id) },
          });
          const clearSent = () => updateDraftAt(epoch, latestProduct.current, productKey, (p) => (productFingerprint(p) === sent ? null : p));
          if (outcome.kind === "done") {
            clearSent();
            remember(null, key);
          } else if (isUnknownOutcome(outcome)) {
            remember({ workProductId: null, approvalId: null, phase: "submit_unknown" }, key);
          } else if (outcome.kind === "pending_approval") {
            // The parked 202 carries work_product_id (routes._work_product_write). It continues
            // through the release route once approved; this form never submits it a second time.
            const fromBody = (outcome.body as { work_product_id?: unknown } | undefined)?.work_product_id;
            if (typeof fromBody === "number") {
              remember({ workProductId: fromBody, approvalId: outcome.approvalId }, key);
              clearSent();
            } else {
              remember({ workProductId: null, approvalId: outcome.approvalId }, key);
              await lookUp(outcome.approvalId, key, productKey, sent);
            }
          } else {
            // Refused, or cancelled before it left: nothing was submitted.
            remember(null, key);
          }
        }}
      >
        <h3 className="m-0 text-sm font-semibold">Submit a work product for release</h3>
        <p className="m-0 text-xs text-[var(--ua-muted)]">
          A work product is a finished file from the workspace (a report, a document) that is checked and released. Release may need a person's approval.
        </p>
        <Field label="File in the workspace" required>
          <Input value={wp.source_path} placeholder="reports/summary.md" onChange={(e) => productDraft.setValue((p) => ({ ...p, source_path: e.target.value }))} />
        </Field>
        <div className="grid gap-2 md:grid-cols-2">
          <Field label="Title" required>
            <Input value={wp.title} onChange={(e) => productDraft.setValue((p) => ({ ...p, title: e.target.value }))} />
          </Field>
          <Field label="Template" required hint="The template pack the file is checked against.">
            <Input value={wp.template_pack} onChange={(e) => productDraft.setValue((p) => ({ ...p, template_pack: e.target.value }))} />
          </Field>
        </div>
        <div>
          <Button
            type="submit"
            size="sm"
            // A submission that is waiting, being located or being released blocks another one.
            disabled={parked !== null || !wp.source_path.trim() || !wp.title.trim() || !wp.template_pack.trim()}
            loading={submit.isRunning}
          >
            Submit for release
          </Button>
        </div>
        {/* A parked submission is followed below as a release; only errors are shown here. */}
        {submit.state.status === "error" && <WriteFeedback state={submit.state} what="The submission" />}
        {parked !== null && <p className="m-0 text-xs text-[var(--ua-muted)]">A submission from this task is waiting below. Finish or forget it before submitting another.</p>}
      </form>

      <div className="flex flex-col gap-2 border-t border-[var(--ua-border)] pt-3" aria-label="Release follow-up" role="group">
        <h3 className="m-0 text-sm font-semibold">Continue a release</h3>
        {parked?.phase === "submit_unknown" && submit.isRunning ? (
          <p role="status" className="m-0 text-sm">Submitting the work product…</p>
        ) : parked?.phase === "submit_unknown" ? (
          <div role="alert" className="flex flex-col gap-2 text-sm">
            <p className="m-0">
              <strong>We could not confirm whether the work product was submitted.</strong> The request was sent but its answer was lost. If you
              find its number (in the approval or the evidence trail), enter it below to continue its release; otherwise choose:
            </p>
            <div>
              <Button size="sm" onClick={() => remember(null)}>
                It was not submitted, let me submit again
              </Button>
            </div>
          </div>
        ) : parked?.phase === "release_unknown" ? (
          <div role="alert" className="flex flex-col gap-2 text-sm">
            <p className="m-0">
              <strong>We could not confirm whether work product #{parked.workProductId} was released.</strong> Check the evidence trail, then choose:
            </p>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => remember(null)}>
                It was released
              </Button>
              <Button size="sm" onClick={() => remember({ workProductId: parked.workProductId, approvalId: parked.approvalId })}>
                It was not released, let me try again
              </Button>
            </div>
          </div>
        ) : parked ? (
          <ReleaseApprovalState parked={parked} state={approvalState} />
        ) : (
          <p className="m-0 text-sm text-[var(--ua-muted)]">If a release waited for approval earlier, continue it here with its work product number.</p>
        )}
        {parked && !parked.workProductId && lookupFailed && (
          <p role="alert" className="m-0 text-sm">
            The work product number could not be looked up yet, so the release cannot continue. Your submission is kept above.{" "}
            <Button size="sm" onClick={() => void lookUp(parked.approvalId)}>
              Look it up again
            </Button>
          </p>
        )}
        {!parked?.workProductId && (
          <Field label="Work product number" className="max-w-48">
            <Input inputMode="numeric" value={manualId} onChange={(e) => setManualId(e.target.value.replace(/\D/g, ""))} />
          </Field>
        )}
        <div className="flex gap-2">
          <Button
            size="sm"
            // A remembered release continues only once its approval reads as approved.
            disabled={!releaseId || parked?.phase === "release_unknown" || (parked?.phase === "submit_unknown" && !manualId) || (parked?.approvalId ? approvalState !== "approved" : false)}
            loading={release.isRunning}
            onClick={async () => {
              if (!releaseId) return;
              const outcome = await release.run(undefined, { path: WRITE_PATHS.workProductRelease(releaseId) });
              if (outcome.kind === "done") remember(null);
              else if (isUnknownOutcome(outcome)) remember({ workProductId: releaseId, approvalId: parked?.approvalId ?? null, phase: "release_unknown" });
            }}
          >
            Continue release
          </Button>
          {parked && (
            <Button size="sm" variant="ghost" onClick={() => remember(null)}>
              Forget
            </Button>
          )}
        </div>
        <WriteFeedback state={release.state} what="The release" />
      </div>
    </Section>
  );
}

/**
 * What a remembered release waits on, read from its approval (GET /api/approvals/{id}). The shared
 * ParkedWriteNotice would tell the person to submit the form again; a work product continues
 * through its release route instead (Needs from foundation: a continuation label for the notice).
 */
function ReleaseApprovalState({ parked, state }: { parked: ParkedRelease; state: string | null }) {
  const name = parked.workProductId ? `Work product #${parked.workProductId}` : "The work product";
  const link = parked.approvalId ? (
    <UiLink href={`/inbox?kind=approval&item=${encodeURIComponent(parked.approvalId)}`} className="text-[var(--ua-link)] underline">
      Open the approval
    </UiLink>
  ) : null;
  const text: Record<string, string> = {
    loading: `Checking the approval for ${name.toLowerCase()}…`,
    pending: `Waiting for approval. ${name} is released once the approval is granted and you continue here; it is not submitted again.`,
    approved: `Approved. Continue the release of ${name.toLowerCase()} here.`,
    denied: `${name}: the release was denied, so it was not released. Forget it, or submit a new one.`,
    revoked: `${name}: the approval was revoked, so it was not released.`,
    expired: `${name}: the approval expired before the release continued.`,
    consumed: `${name}: the approval was already used. Check whether it was released before continuing.`,
    missing: `${name}: the approval no longer exists.`,
    unverified: `The approval for ${name.toLowerCase()} could not be checked. Try again shortly.`,
    unknown: `The approval for ${name.toLowerCase()} is in a state this page does not know.`,
  };
  return (
    <p role="status" className="m-0 text-sm">
      {text[state ?? "pending"] ?? text.unknown} {link}
    </p>
  );
}

/** The fields a work-product submission sent, to clear only that submission's draft. */
function productFingerprint(p: { source_path: string; title: string; template_pack: string }): string {
  return JSON.stringify([p.source_path.trim(), p.title.trim(), p.template_pack.trim()]);
}

export function NoTask() {
  return <EmptyState title="Task not found" description="Go back to the task list." action={<LinkButton href="/work">All tasks</LinkButton>} />;
}
