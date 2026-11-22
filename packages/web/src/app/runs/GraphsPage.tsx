import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, CodeBlock, DataTable, Field, GraphCanvas, Input, LinkButton, Select, StatePill, Textarea, UiLink, type Column, type GraphEdge, type GraphNode } from "../../lib/ui";
import { draftKey, readDraft, saveDraft, useDraft } from "../lib/drafts";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import { useIdentity } from "../lib/identity";
import { approvalInboxHref } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { useUrlParam } from "../lib/urlState";
import {
  CHECKPOINT_LIMIT,
  fetchCheckpoints,
  fetchRuns,
  GRAPH_HOPS,
  RUN_ID_RE,
  runsKeys,
  type GraphRunResult,
  type LangGraphCheckpoint,
} from "./api";
import { defaultSettle, startedSince, useStartGuard, type GuardRefusal, type Settle, type StartAttempt } from "./attempt";
import { useGraphRun, type GraphOutcome } from "./graphWrite";
import { formatWhen } from "./model";
import { AttemptCheck, Explainer, focusFirstInvalid, GuardRefused, RunsHeader, RunsRegion, Section, WriteFailure, useWriteContext, type CandidateRead } from "./parts";

const MAX_QUERY = 4_000;
const CHECKPOINT_POLL_MS = 30_000;

/**
 * What this browser knows about a graph run it started or continued, keyed
 * by run id: the hub's last answer, when it came, and the steps the hub has
 * confirmed across every answer so far (a later denial does not undo them).
 */
export type KnownGraphRun = {
  runId: string;
  taskId: number;
  query: string;
  result: GraphRunResult;
  approvalId: string | null;
  at: number;
  /** Steps confirmed done by any answer so far. */
  confirmedHops?: string[];
  /** The step the run last waited at, from an earlier answer. */
  lastPendingHop?: string | null;
  /** The newest checkpoint id read after this answer, to notice the run moving on. */
  checkpointHead?: string | null;
};

function knownKey(identity: string | null | undefined, runId: string) {
  return draftKey({ identity, operation: "runs.graph.known", object: { run: runId } });
}

export function readKnownRun(epoch: OwnerEpoch, identity: string | null | undefined, runId: string): KnownGraphRun | null {
  return runId ? readDraft<KnownGraphRun>(epoch, knownKey(identity, runId)) : null;
}

function rememberRun(epoch: OwnerEpoch, identity: string | null | undefined, run: KnownGraphRun) {
  saveDraft(epoch, knownKey(identity, run.runId), run);
}

function pendingHopOf(result: GraphRunResult): string | null {
  return result.pending_hop ?? result.interrupted_by?.hop ?? null;
}

export function isPaused(result: GraphRunResult | null): boolean {
  return Boolean(result && (result.status === "approval_pending" || result.run_status === "interrupted" || result.status === "interrupted"));
}

/** Fold a new answer into what is known: confirmed steps only ever grow. */
export function foldAnswer(previous: KnownGraphRun | null, next: Omit<KnownGraphRun, "confirmedHops" | "lastPendingHop" | "checkpointHead">): KnownGraphRun {
  const confirmed = new Set(previous?.confirmedHops ?? []);
  const pending = pendingHopOf(next.result);
  if (next.result.status === "completed") for (const h of GRAPH_HOPS) confirmed.add(h);
  else if (isPaused(next.result) && pending) {
    for (const h of GRAPH_HOPS.slice(0, Math.max(0, GRAPH_HOPS.indexOf(pending as (typeof GRAPH_HOPS)[number])))) confirmed.add(h);
  }
  return {
    ...next,
    confirmedHops: GRAPH_HOPS.filter((h) => confirmed.has(h)),
    lastPendingHop: isPaused(next.result) ? pending : (previous?.lastPendingHop ?? null),
    checkpointHead: null,
  };
}

const HOP_WORDS: Record<string, string> = {
  plan: "Plan the change",
  build: "Build: record the build receipt",
  verify: "Verify the build",
  merge: "Merge: record the merge receipt",
};

/**
 * Graph state per step for `GraphCanvas`, from the last answer and the steps
 * confirmed across answers. Anything the hub has not said is "Not known".
 */
export function hopNodes(known: KnownGraphRun | null): GraphNode[] {
  const result = known?.result ?? null;
  const confirmed = new Set(known?.confirmedHops ?? []);
  const paused = isPaused(result);
  const pending = result ? (paused ? pendingHopOf(result) : result.status === "denied" ? (pendingHopOf(result) ?? known?.lastPendingHop ?? null) : null) : null;
  const pendingIndex = pending ? GRAPH_HOPS.indexOf(pending as (typeof GRAPH_HOPS)[number]) : -1;
  return GRAPH_HOPS.map((hop, i) => {
    let tone: GraphNode["tone"] = "default";
    let state = "Not known";
    if (confirmed.has(hop)) [tone, state] = ["ok", "Done"];
    else if (paused && i === pendingIndex) [tone, state] = ["warn", "Waiting for approval"];
    else if (paused && pendingIndex >= 0 && i > pendingIndex) state = "Not started";
    else if (result?.status === "denied") {
      if (i === pendingIndex) [tone, state] = ["danger", "Approval denied"];
      else if (pendingIndex >= 0 && i > pendingIndex) state = "Not run";
    }
    return { id: hop, label: hop[0].toUpperCase() + hop.slice(1), description: `${HOP_WORDS[hop]}. ${state}.`, tone };
  });
}

const HOP_EDGES: GraphEdge[] = GRAPH_HOPS.slice(1).map((hop, i) => ({ id: `${GRAPH_HOPS[i]}-${hop}`, source: GRAPH_HOPS[i], target: hop, label: "then" }));

const CHECKPOINT_COLUMNS: Column<LangGraphCheckpoint>[] = [
  { id: "id", header: "Checkpoint", cell: (c) => <span className="font-mono text-xs break-all">{c.checkpoint_id ?? "Not recorded"}</span> },
  { id: "parent", header: "Saved after", cell: (c) => <span className="font-mono text-xs break-all">{c.parent_checkpoint_id ?? "The start"}</span> },
  { id: "ns", header: "Part", cell: (c) => c.checkpoint_ns || "Whole graph" },
  { id: "type", header: "Format", cell: (c) => c.type ?? "Not recorded" },
];

function runIdOfThread(thread: string | null | undefined): string | null {
  const m = /^ua-lg:([0-9A-HJKMNP-TV-Z]{26})$/.exec(thread ?? "");
  return m ? m[1] : null;
}

/** `/runs/graphs`: start a governed graph run, watch it, see checkpoints, continue it (spec §9.6 Graphs). */
export function GraphsPage() {
  const [runParam, setRun] = useUrlParam("run");
  const runId = RUN_ID_RE.test(runParam) ? runParam : "";
  return (
    <>
      <RunsHeader title="Graphs" description="Graph runs: start one, watch its progress, see its checkpoints and continue it after an approval." />
      <Explainer>
        A graph run takes one claimed task through four steps: plan, build, verify and merge. Build and merge each need an approval. A checkpoint is the
        run's saved state after a step, which is what lets a paused run continue where it stopped.
      </Explainer>
      <StartGraphRun onWatch={(id) => setRun(id, { push: true })} />
      {runId ? <WatchRun key={runId} runId={runId} onClose={() => setRun(null, { push: true })} /> : <RecentCheckpoints />}
      {runParam && !runId ? (
        <p role="alert" className="text-sm">
          “{runParam}” is not a graph run id. A run id has 26 letters and digits.
        </p>
      ) : null}
    </>
  );
}

type StartDraft = { taskId: string; query: string; engine: string };

function StartGraphRun({ onWatch }: { onWatch: (runId: string) => void }) {
  const identity = useIdentity();
  const epoch = useEpoch();
  const context = useWriteContext();
  const formRef = useRef<HTMLFormElement>(null);
  const draft = useDraft<StartDraft>({ identity, operation: "runs.graph.start", initial: { taskId: "", query: "", engine: "workflow" } });
  const latest = useRef(draft.value);
  latest.current = draft.value;
  const [submitted, setSubmitted] = useState(false);
  const [outcome, setOutcome] = useState<GraphOutcome | null>(null);
  const [refusal, setRefusal] = useState<GuardRefusal | null>(null);
  const guard = useStartGuard(context, "graph");
  const graph = useGraphRun([runsKeys.all]);
  const attempt = guard.attempt;

  const problemsOf = (v: StartDraft) => {
    const out: Partial<Record<keyof StartDraft, string>> = {};
    const t = Number(v.taskId);
    if (!Number.isSafeInteger(t) || t <= 0) out.taskId = "Enter the number of a task you have claimed and are working on.";
    if (!v.query.trim()) out.query = "Say what the run should do.";
    else if (v.query.length > MAX_QUERY) out.query = `Keep it under ${MAX_QUERY} characters.`;
    if (v.engine !== "workflow") out.engine = "The research engine is not available on this hub yet.";
    return out;
  };
  const problems = problemsOf(draft.value);

  /** The one way to start: validate, then send under the guard. */
  const dispatch = async (options: { replaceHeld?: string } = {}) => {
    setSubmitted(true);
    if (Object.keys(problemsOf(latest.current)).length) {
      focusFirstInvalid(formRef.current);
      return;
    }
    if (graph.running) return;
    const sentRevision = JSON.stringify(latest.current);
    const taskId = Number(latest.current.taskId);
    const query = latest.current.query.trim();
    setRefusal(null);
    const r = await guard.run({ summary: `A graph run for task #${taskId}`, match: { taskId } }, () => graph.run({ task_id: taskId, query, engine: "workflow" }), graphSettle, options);
    if (!r.sent) {
      setRefusal(r.refusal);
      return;
    }
    const result = r.outcome;
    if (!result) return;
    setOutcome(result);
    if (result.kind === "graph_paused" || result.kind === "done" || result.kind === "accepted") {
      const data = result.kind === "graph_paused" ? result.result : result.data;
      if (data.run_id) {
        rememberRun(epoch, identity.identity, foldAnswer(null, { runId: data.run_id, taskId, query, result: data, approvalId: result.kind === "graph_paused" ? result.approvalId : null, at: Date.now() }));
        onWatch(data.run_id);
      }
      if (JSON.stringify(latest.current) === sentRevision) {
        draft.clear();
        setSubmitted(false);
      }
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (guard.held) return;
    void dispatch();
  };

  return (
    <Section title="Start a graph run" description="You must have claimed the task, and it must be in progress.">
      {guard.held && attempt ? (
        <GraphAttemptCheck attempt={attempt} pendingElsewhere={guard.pendingElsewhere} onFound={() => void guard.resolve(attempt.id)} onRestart={() => void dispatch({ replaceHeld: attempt.id })} />
      ) : null}
      {draft.restored ? <p className="m-0 mb-2 text-sm text-[var(--ua-muted)]">Your unfinished form was restored.</p> : null}
      <form ref={formRef} onSubmit={submit} noValidate className="grid max-w-2xl gap-4" aria-label="Start a graph run">
        <Field label="Task number" required error={submitted ? problems.taskId : undefined}>
          <Input inputMode="numeric" value={draft.value.taskId} onChange={(e) => draft.setValue((p) => ({ ...p, taskId: e.target.value }))} />
        </Field>
        <Field label="What should it do?" required hint={`Up to ${MAX_QUERY} characters.`} error={submitted ? problems.query : undefined}>
          <Textarea value={draft.value.query} onChange={(e) => draft.setValue((p) => ({ ...p, query: e.target.value }))} />
        </Field>
        <Field label="Engine" hint="The research engine is not available on this hub yet: a run it pauses for approval could not be continued." error={submitted ? problems.engine : undefined}>
          <Select
            value={draft.value.engine}
            onChange={(e) => draft.setValue((p) => ({ ...p, engine: e.target.value }))}
            options={[
              { value: "workflow", label: "Governed workflow: plan, build, verify, merge" },
              { value: "research", label: "Research (not available on this hub yet)", disabled: true },
            ]}
          />
        </Field>
        <div>
          <Button type="submit" variant="primary" loading={graph.running} disabled={guard.held || !guard.key}>
            Start graph run
          </Button>
        </div>
      </form>
      <div className="mt-3 flex flex-col gap-2">
        <GuardRefused refusal={refusal} />
        <OutcomeView outcome={outcome} />
      </div>
    </Section>
  );
}

/** A graph answer with a run id is authoritative; everything else follows the shared rule. */
function graphSettle(outcome: GraphOutcome | null): Settle {
  return defaultSettle(outcome);
}

function OutcomeView({ outcome }: { outcome: GraphOutcome | null }) {
  if (!outcome) return null;
  if (outcome.kind === "error") return <WriteFailure message={outcome.message} detail={outcome.detail} />;
  if (outcome.kind === "pending_approval") return <ParkedWriteNotice approvalId={outcome.approvalId} label="The graph run" />;
  return null;
}

/** Candidates for a lost graph start or continue: the hub registers one session per request, linked to the task. */
function GraphAttemptCheck({ attempt, pendingElsewhere, onFound, onRestart }: { attempt: StartAttempt; pendingElsewhere: boolean; onFound: () => void; onRestart: () => void }) {
  const taskId = attempt.match.taskId ?? 0;
  const q = useQuery({ queryKey: [...runsKeys.runCandidates(taskId), attempt.id], queryFn: ({ signal }) => fetchRuns({ limit: 50, kind: "session", taskId }, signal) });
  const retry = () => void q.refetch();
  const read: CandidateRead = q.isPending
    ? { state: "loading" }
    : q.isError
      ? { state: "failed", message: "The task's sessions could not be read, so it is not known whether the run started.", retry }
      : {
          state: "ready",
          capped: q.data.truncated,
          retry,
          items: q.data.runs
            .filter((r) => r.session_kind === "control_center" && startedSince(r.started_at, attempt.sentAt))
            .map((r) => ({ id: r.run_id, label: `Request ${r.run_id} for task #${taskId} at ${formatWhen(r.started_at)}, ${r.status}` })),
        };
  return (
    <AttemptCheck attempt={attempt} what="graph run requests" candidates={read} pendingElsewhere={pendingElsewhere} onFound={onFound} onRestart={onRestart}>
      <p className="m-0 text-xs text-[var(--ua-muted)]">
        The hub does not accept an idempotency key for graph runs yet. Each request it received appears as a session on the task; the run id itself is
        in the <UiLink href="/runs/graphs">recent checkpoints</UiLink>.
      </p>
    </AttemptCheck>
  );
}

function RecentCheckpoints() {
  const q = useQuery({ queryKey: runsKeys.checkpoints(""), queryFn: ({ signal }) => fetchCheckpoints("", signal), refetchInterval: CHECKPOINT_POLL_MS });
  return (
    <Section title="Recent graph runs" description="Runs found in the checkpoint store, newest first. Open one to watch it.">
      <RunsRegion
        query={q}
        loadingLabel="Loading checkpoints"
        errorTitle="Checkpoints could not be loaded"
        isEmpty={(d) => d.checkpoints.length === 0}
        empty={{ title: "No graph runs yet", description: "Start one above. Its checkpoints appear here." }}
      >
        {(d) => {
          const runs = new Map<string, number>();
          for (const c of d.checkpoints) {
            const id = runIdOfThread(c.thread_id);
            if (id) runs.set(id, (runs.get(id) ?? 0) + 1);
          }
          const rows = [...runs.entries()].map(([id, n]) => ({ id, n }));
          return (
            <>
              {d.count >= CHECKPOINT_LIMIT ? (
                <p className="m-0 mb-2 text-sm text-[var(--ua-warn)]">Only the newest {CHECKPOINT_LIMIT} checkpoints were read, so older runs are not listed.</p>
              ) : null}
              <DataTable
                caption="Graph runs with saved checkpoints"
                data={rows}
                getRowKey={(r) => r.id}
                columns={[
                  { id: "run", header: "Run", cell: (r) => <UiLink href={`/runs/graphs?run=${r.id}`} className="font-mono text-xs">{r.id}</UiLink> },
                  { id: "n", header: "Checkpoints read", cell: (r) => String(r.n) },
                ]}
              />
            </>
          );
        }}
      </RunsRegion>
    </Section>
  );
}

type ContinueDraft = { taskId: string; query: string };

/** One run, keyed by its id so nothing from another run shows here. */
function WatchRun({ runId, onClose }: { runId: string; onClose: () => void }) {
  const identity = useIdentity();
  const epoch = useEpoch();
  const context = useWriteContext();
  const queryClient = useQueryClient();
  // Read once the identity is known (a direct link loads it after the page).
  const stored = useMemo(() => (identity.status === "ready" ? readKnownRun(epoch, identity.identity, runId) : null), [epoch, identity.status, identity.identity, runId]);
  // This tab's newest answer, tagged with the identity it was made under, so a
  // later sign-in as someone else never sees it.
  const [latest, setLatest] = useState<{ who: string | null | undefined; run: KnownGraphRun } | null>(null);
  const setKnown = (run: KnownGraphRun) => setLatest({ who: identity.identity, run });
  const known = (latest && latest.who === identity.identity ? latest.run : null) ?? stored;
  const checkpoints = useQuery({
    queryKey: runsKeys.checkpoints(runId),
    queryFn: ({ signal }) => fetchCheckpoints(runId, signal),
    refetchInterval: CHECKPOINT_POLL_MS,
  });
  const result = known?.result ?? null;
  const paused = isPaused(result);
  const nodes = useMemo(() => hopNodes(known), [known]);
  // The newest checkpoint read after the last answer: when a later read shows
  // another, the run has moved on and the answer below is out of date.
  const head = checkpoints.data?.checkpoints[0]?.checkpoint_id ?? null;
  useEffect(() => {
    if (!known || !checkpoints.data || checkpoints.dataUpdatedAt <= known.at) return;
    if (known.checkpointHead === null || known.checkpointHead === undefined) {
      const next = { ...known, checkpointHead: head };
      rememberRun(epoch, identity.identity, next);
      setKnown(next);
    }
  }, [known, head, checkpoints.data, checkpoints.dataUpdatedAt, identity.identity]);
  const movedOn = Boolean(known && known.checkpointHead && head && head !== known.checkpointHead);
  const formRef = useRef<HTMLFormElement>(null);
  const draft = useDraft<ContinueDraft>({
    identity,
    operation: "runs.graph.continue",
    object: { run: runId },
    initial: { taskId: known ? String(known.taskId) : "", query: known?.query ?? "" },
  });
  const latestDraft = useRef(draft.value);
  latestDraft.current = draft.value;
  const [submitted, setSubmitted] = useState(false);
  const [outcome, setOutcome] = useState<GraphOutcome | null>(null);
  const [refusal, setRefusal] = useState<GuardRefusal | null>(null);
  const guard = useStartGuard(context, "graph-continue", runId);
  const graph = useGraphRun([runsKeys.checkpoints(runId)]);
  const attempt = guard.attempt;
  const problemsOf = (v: ContinueDraft) => {
    const out: Partial<Record<keyof ContinueDraft, string>> = {};
    const t = Number(v.taskId);
    if (!Number.isSafeInteger(t) || t <= 0) out.taskId = "Enter the task number the run was started for.";
    if (!v.query.trim()) out.query = "Enter exactly what the run was started with.";
    return out;
  };
  const problems = problemsOf(draft.value);

  const dispatch = async (options: { replaceHeld?: string } = {}) => {
    setSubmitted(true);
    if (Object.keys(problemsOf(latestDraft.current)).length) {
      focusFirstInvalid(formRef.current);
      return;
    }
    if (graph.running) return;
    const taskId = Number(latestDraft.current.taskId);
    const query = latestDraft.current.query.trim();
    setRefusal(null);
    const r = await guard.run(
      { summary: `Continuing graph run ${runId}`, match: { taskId, runId } },
      () => graph.run({ task_id: taskId, query, run_id: runId, engine: "workflow" }),
      graphSettle,
      options,
    );
    if (!r.sent) {
      setRefusal(r.refusal);
      return;
    }
    const answer = r.outcome;
    if (!answer) return;
    setOutcome(answer);
    if (answer.kind === "graph_paused" || answer.kind === "done" || answer.kind === "accepted") {
      const data = answer.kind === "graph_paused" ? answer.result : answer.data;
      const next = foldAnswer(known, { runId, taskId, query, result: data, approvalId: answer.kind === "graph_paused" ? answer.approvalId : null, at: Date.now() });
      rememberRun(epoch, identity.identity, next);
      setKnown(next);
      setSubmitted(false);
      void queryClient.invalidateQueries({ queryKey: runsKeys.checkpoints(runId) });
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (guard.held) return;
    void dispatch();
  };

  const confirmed = known?.confirmedHops ?? [];
  const statusText = !result
    ? "This browser did not start or continue this run, so its current step is not known here. Its checkpoints are below."
    : result.status === "completed"
      ? "Completed: every step ran and both receipts were recorded. This does not merge code or start an agent."
      : result.status === "denied"
        ? `An approval was denied, so the remaining steps did not run${result.reason ? ` (${result.reason})` : ""}. ${
            confirmed.length ? `Steps that had already finished (${confirmed.join(", ")}) keep their effects, such as a recorded receipt.` : "Any step that had already finished keeps its effects."
          }`
        : paused
          ? `Waiting for approval before the ${pendingHopOf(result) ?? "next"} step. A different person must approve it in the Inbox; then continue the run here.`
          : `Last answer: ${result.status}.`;

  return (
    <Section
      title={`Graph run ${runId}`}
      actions={
        <Button size="sm" variant="ghost" onClick={onClose}>
          All graph runs
        </Button>
      }
    >
      <p role="status" aria-live="polite" className="m-0 mb-3 flex flex-wrap items-center gap-2 text-sm">
        {paused ? <StatePill kind="warn" label="Waiting for approval" /> : result?.status === "completed" ? <StatePill kind="ok" label="Completed" /> : result?.status === "denied" ? <StatePill kind="danger" label="Denied" /> : <StatePill kind="unknown" label="Not known" />}
        <span>{statusText}</span>
      </p>
      {known ? (
        <p className="m-0 mb-3 text-xs text-[var(--ua-muted)]">
          From the hub's last answer at {new Date(known.at).toLocaleString()}, as this browser recorded it. This is not live progress: the hub reports
          a graph run's step only when it is started or continued. The checkpoints below refresh every 30 seconds.
        </p>
      ) : null}
      {movedOn ? (
        <p role="note" className="m-0 mb-3 text-sm text-[var(--ua-warn)]">
          The run has saved a new checkpoint since that answer, so its current step may have changed. Continue it to get a fresh answer.
        </p>
      ) : null}
      {paused ? (
        <div className="mb-3 flex flex-wrap gap-2">
          <LinkButton href={approvalInboxHref(known?.approvalId ?? (result?.approval_id != null ? String(result.approval_id) : null))} size="sm">
            Open the approval in the Inbox
          </LinkButton>
        </div>
      ) : null}
      <GraphCanvas label={`Steps of graph run ${runId}`} nodes={nodes} edges={HOP_EDGES} height={220} legend={[
        { tone: "ok", label: "Done" },
        { tone: "warn", label: "Waiting for approval" },
        { tone: "danger", label: "Approval denied" },
        { tone: "default", label: "Not started or not known" },
      ]} />
      {result?.output ? <CodeBlock className="mt-3" label="Receipts the run recorded" code={JSON.stringify(result.output, null, 2)} language="json" /> : null}

      {result?.status !== "completed" && result?.status !== "denied" ? (
        <div className="mt-4">
          <h3 className="m-0 mb-1 text-sm font-semibold">Continue this run</h3>
          <p className="m-0 mb-3 max-w-[70ch] text-sm text-[var(--ua-muted)]">
            After the approval is decided, continue the run. It picks up from its last checkpoint; it does not start a new run. Use the same task and
            instructions it was started with.
          </p>
          {guard.held && attempt ? (
            <GraphAttemptCheck attempt={attempt} pendingElsewhere={guard.pendingElsewhere} onFound={() => void guard.resolve(attempt.id)} onRestart={() => void dispatch({ replaceHeld: attempt.id })} />
          ) : null}
          <form ref={formRef} onSubmit={submit} noValidate className="grid max-w-2xl gap-4" aria-label={`Continue graph run ${runId}`}>
            <Field label="Task number" required error={submitted ? problems.taskId : undefined}>
              <Input inputMode="numeric" value={draft.value.taskId} onChange={(e) => draft.setValue((p) => ({ ...p, taskId: e.target.value }))} />
            </Field>
            <Field label="What it was started with" required error={submitted ? problems.query : undefined}>
              <Textarea value={draft.value.query} onChange={(e) => draft.setValue((p) => ({ ...p, query: e.target.value }))} />
            </Field>
            <div>
              <Button type="submit" variant="primary" loading={graph.running} disabled={guard.held || !guard.key}>
                Continue
              </Button>
            </div>
          </form>
          <div className="mt-3 flex flex-col gap-2">
            <GuardRefused refusal={refusal} />
            <OutcomeView outcome={outcome} />
          </div>
        </div>
      ) : null}

      <h3 className="m-0 mt-6 mb-2 text-sm font-semibold">Checkpoints</h3>
      <RunsRegion
        query={checkpoints}
        headingLevel={3}
        loadingLabel="Loading checkpoints"
        errorTitle="Checkpoints could not be loaded"
        isEmpty={(d) => d.checkpoints.length === 0}
        empty={{ title: "No checkpoints for this run", description: "A run saves a checkpoint after each step. None is stored for this run id." }}
      >
        {(d) => (
          <>
            {d.count >= CHECKPOINT_LIMIT ? <p className="m-0 mb-2 text-sm text-[var(--ua-warn)]">Only the newest {CHECKPOINT_LIMIT} checkpoints are shown.</p> : null}
            <DataTable caption={`Checkpoints of run ${runId}, newest first`} columns={CHECKPOINT_COLUMNS} data={d.checkpoints} getRowKey={(c, i) => `${c.checkpoint_ns ?? ""}:${c.checkpoint_id ?? i}`} />
          </>
        )}
      </RunsRegion>
    </Section>
  );
}
