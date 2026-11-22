import { useMemo, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useStreamInvalidation } from "../../lib/polling";
import { Button, CodeBlock, DataTable, Field, Input, StatePill, UiLink, type Column } from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useIdentity } from "../lib/identity";
import { approvalInboxHref, useHubWrite, type WriteOutcome } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { useUrlParam } from "../lib/urlState";
import {
  fetchEvents,
  fetchWorkflows,
  runsKeys,
  ulidFloor,
  WORKFLOW_CHANNEL,
  WRITE_PATHS,
  type HubEvent,
  type WorkflowDef,
  type WorkflowRunResult,
  type WorkflowStepReport,
} from "./api";
import { defaultSettle, SKEW_MS, startedSince, useStartGuard, type GuardRefusal, type Settle, type StartAttempt } from "./attempt";
import { formatWhen } from "./model";
import { AttemptCheck, Explainer, focusFirstInvalid, GuardRefused, RunsHeader, RunsRegion, Section, WriteFailure, useWriteContext, type CandidateRead } from "./parts";

const HISTORY_DAYS = 7;
const HISTORY_LIMIT = 200;

const LIST_COLUMNS: Column<WorkflowDef>[] = [
  {
    id: "name",
    header: "Workflow",
    cell: (w) => (
      <UiLink href={`/runs/workflows?workflow=${encodeURIComponent(w.name)}`} className="font-medium">
        {w.name}
      </UiLink>
    ),
  },
  { id: "description", header: "What it does", cell: (w) => w.description ?? <span className="text-[var(--ua-muted)]">No description</span> },
  { id: "steps", header: "Steps", cell: (w) => (w.steps ? w.steps.join(" → ") : <span className="text-[var(--ua-muted)]">Not listed</span>) },
  { id: "inputs", header: "Inputs", cell: (w) => (w.inputs.length ? w.inputs.join(", ") : "None") },
];

const STEP_TEXT: Record<string, { kind: "ok" | "danger" | "warn" | "unknown"; label: string }> = {
  done: { kind: "ok", label: "Done" },
  failed: { kind: "danger", label: "Failed" },
  "pending-approval": { kind: "warn", label: "Waiting for approval" },
  denied: { kind: "danger", label: "Refused" },
  indeterminate: { kind: "warn", label: "Outcome unknown" },
};

const STEP_COLUMNS: Column<WorkflowStepReport>[] = [
  { id: "name", header: "Step", cell: (s) => <span className="font-medium">{s.name}</span> },
  { id: "tool", header: "Tool", cell: (s) => <span className="font-mono text-xs">{s.tool}</span> },
  {
    id: "status",
    header: "Result",
    cell: (s) => {
      const t = STEP_TEXT[s.status] ?? { kind: "unknown" as const, label: s.status };
      return <StatePill kind={t.kind} label={t.label} />;
    },
  },
  { id: "attempts", header: "Attempts", cell: (s) => String(s.attempts) },
  { id: "error", header: "Problem", cell: (s) => (s.error ? <span className="break-words text-xs">{s.error}</span> : <span className="text-[var(--ua-muted)]">None</span>) },
];

const RUN_STATUS_TEXT: Record<string, string> = {
  done: "Every step finished.",
  failed: "A step failed, so the workflow stopped there. Later steps did not run.",
  "pending-approval":
    "A step is waiting for approval, so the workflow stopped there. After a different person approves it in the Inbox, run the workflow again. Steps that already finished will run again.",
  denied: "A step was refused by policy, so the workflow stopped there.",
  indeterminate: "A step may have run, but its record could not be written. Check its effect before running the workflow again.",
};

function HistoryTable({ events }: { events: HubEvent[] }) {
  const rows = [...events].sort((a, b) => (a.ulid < b.ulid ? 1 : -1));
  const cols: Column<HubEvent>[] = [
    { id: "when", header: "When", cell: (e) => formatWhen(e.created_at) },
    { id: "workflow", header: "Workflow", cell: (e) => e.payload.ref_id ?? "Unknown" },
    { id: "who", header: "Run by", cell: (e) => e.originator ?? "Not recorded" },
    { id: "what", header: "What happened", cell: (e) => e.payload.summary ?? "" },
  ];
  return <DataTable caption="Workflow activity, newest first" columns={cols} data={rows} getRowKey={(e) => e.ulid} />;
}

/** `/runs/workflows`: reviewed workflows, run one with its inputs, see step results (spec §9.6). */
export function WorkflowsPage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useUrlParam("workflow");
  const workflows = useQuery({ queryKey: runsKeys.workflows, queryFn: ({ signal }) => fetchWorkflows(signal) });
  const historySince = useMemo(() => ulidFloor(Date.now() - HISTORY_DAYS * 86_400_000), []);
  const history = useQuery({
    queryKey: runsKeys.workflowEvents,
    queryFn: ({ signal }) => fetchEvents({ channels: [WORKFLOW_CHANNEL], since: historySince, limit: HISTORY_LIMIT }, signal),
  });
  useStreamInvalidation(() => void queryClient.invalidateQueries({ queryKey: runsKeys.workflowEvents }), { channels: [WORKFLOW_CHANNEL], onReconnect: true });
  const def = workflows.data?.find((w) => w.name === selected) ?? null;

  return (
    <>
      <RunsHeader title="Workflows" description="Reviewed workflows you can run with your own inputs." />
      <Explainer>
        A workflow is a reviewed list of steps kept in the hub's code. Each step calls one hub tool with the same checks and approvals as a direct
        call. Pick one to run it.
      </Explainer>
      <Section title="Available workflows">
        <RunsRegion
          query={workflows}
          loadingLabel="Loading workflows"
          errorTitle="Workflows could not be loaded"
          isEmpty={(d) => d.length === 0}
          empty={{ title: "No workflows", description: "This hub has no reviewed workflow definitions." }}
        >
          {(data) => <DataTable caption="Reviewed workflows" columns={LIST_COLUMNS} data={data} getRowKey={(w) => w.name} />}
        </RunsRegion>
      </Section>
      {selected ? (
        def ? (
          <RunWorkflow key={def.name} def={def} onClose={() => setSelected(null)} />
        ) : workflows.data ? (
          <p role="alert" className="text-sm">
            There is no workflow called “{selected}” on this hub. <UiLink href="/runs/workflows">Show all workflows</UiLink>
          </p>
        ) : null
      ) : null}
      <Section title={`Recent workflow activity (last ${HISTORY_DAYS} days)`}>
        <RunsRegion
          query={history}
          loadingLabel="Loading workflow activity"
          errorTitle="Workflow activity could not be loaded"
          isEmpty={(d) => d.events.length === 0}
          empty={{ title: "No workflow runs in this period" }}
        >
          {(d) => (
            <>
              {d.events.length >= HISTORY_LIMIT ? (
                <p className="m-0 mb-2 text-sm text-[var(--ua-warn)]">
                  The hub returned its limit of {HISTORY_LIMIT} entries, oldest first, so the newest activity may be missing.
                </p>
              ) : null}
              <HistoryTable events={d.events} />
            </>
          )}
        </RunsRegion>
      </Section>
    </>
  );
}

type Draft = { values: Record<string, string> };

/** A workflow answer that did not settle the run: a step may have taken effect without its record. */
export function workflowSettle(outcome: WriteOutcome<WorkflowRunResult>): Settle {
  if ((outcome.kind === "done" || outcome.kind === "accepted") && outcome.data.status === "indeterminate") {
    return {
      record: "unknown",
      note: "The hub answered that a step may have run but its record could not be written, so it is not known whether that step took effect.",
    };
  }
  return defaultSettle(outcome);
}

function RunWorkflow({ def, onClose }: { def: WorkflowDef; onClose: () => void }) {
  const identity = useIdentity();
  const context = useWriteContext();
  const formRef = useRef<HTMLFormElement>(null);
  const draft = useDraft<Draft>({ identity, operation: "runs.workflow.run", object: { workflow: def.name }, initial: { values: {} } });
  // The latest text, for "was anything typed after it was sent?" once the answer arrives.
  const latest = useRef(draft.value);
  latest.current = draft.value;
  const [submitted, setSubmitted] = useState(false);
  const [result, setResult] = useState<WorkflowRunResult | null>(null);
  const [refusal, setRefusal] = useState<GuardRefusal | null>(null);
  const guard = useStartGuard(context, "workflow", def.name);
  const write = useHubWrite<{ name: string; inputs: Record<string, string> }, WorkflowRunResult>({
    path: WRITE_PATHS.workflow,
    label: `Workflow ${def.name}`,
    invalidates: [runsKeys.workflowEvents],
    // Never replayed: a run stops at a step that waits for approval after
    // earlier steps took effect; repeating it runs those steps again.
    resumable: false,
  });
  const attempt = guard.attempt;

  const problemsOf = (value: Draft) => {
    const out: Record<string, string> = {};
    for (const name of def.inputs) if (!(value.values[name] ?? "").trim()) out[name] = `Enter a value for ${name}.`;
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
    if (write.isRunning) return;
    const sentRevision = JSON.stringify(latest.current);
    const inputs = Object.fromEntries(def.inputs.map((n) => [n, (latest.current.values[n] ?? "").trim()]));
    setRefusal(null);
    const r = await guard.run({ summary: `Workflow ${def.name}`, match: { name: def.name } }, () => write.run({ name: def.name, inputs }), workflowSettle, options);
    if (!r.sent) {
      setRefusal(r.refusal);
      return;
    }
    const outcome = r.outcome;
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      setResult(outcome.data);
      // Keep the inputs while the outcome is not settled or the person must run it
      // again after approval, and anything typed after it was sent.
      const finished = outcome.data.status !== "indeterminate" && outcome.data.status !== "pending-approval";
      if (finished && JSON.stringify(latest.current) === sentRevision) {
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
    <Section
      title={`Run ${def.name}`}
      description={def.description ?? undefined}
      actions={
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      }
    >
      {guard.held && attempt ? (
        <WorkflowAttemptCheck
          attempt={attempt}
          pendingElsewhere={guard.pendingElsewhere}
          identity={context?.identity ?? null}
          onFound={() => void guard.resolve(attempt.id)}
          onRestart={() => void dispatch({ replaceHeld: attempt.id })}
        />
      ) : null}
      {draft.restored ? <p className="m-0 mb-2 text-sm text-[var(--ua-muted)]">Your unfinished inputs were restored.</p> : null}
      <form ref={formRef} onSubmit={submit} noValidate className="grid max-w-2xl gap-4" aria-label={`Run ${def.name}`}>
        {def.inputs.length === 0 ? <p className="m-0 text-sm">This workflow takes no inputs.</p> : null}
        {def.inputs.map((name) => (
          <Field key={name} label={name} required error={submitted ? problems[name] : undefined} hint={name === "task_id" ? "The task number, for example 42." : undefined}>
            <Input
              value={draft.value.values[name] ?? ""}
              inputMode={name.endsWith("_id") ? "numeric" : undefined}
              onChange={(e) => {
                const v = e.target.value;
                draft.setValue((prev) => ({ values: { ...prev.values, [name]: v } }));
              }}
            />
          </Field>
        ))}
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" variant="primary" loading={write.isRunning} disabled={guard.held || !guard.key}>
            Run workflow
          </Button>
          {!guard.key ? <span className="text-sm text-[var(--ua-muted)]">Checking who is signed in…</span> : null}
        </div>
      </form>
      <div className="mt-4 flex flex-col gap-3">
        <GuardRefused refusal={refusal} />
        {write.state.status === "error" ? <WriteFailure message={write.state.message} detail={write.state.detail} /> : null}
        {write.state.status === "pending_approval" ? <ParkedWriteNotice approvalId={write.state.approvalId} label={`Workflow ${def.name}`} /> : null}
        {result ? <WorkflowResult result={result} /> : null}
      </div>
    </Section>
  );
}

function WorkflowResult({ result }: { result: WorkflowRunResult }) {
  const t = STEP_TEXT[result.status] ?? { kind: "unknown" as const, label: result.status };
  return (
    <section aria-label={`Result of ${result.workflow}`} className="flex flex-col gap-2">
      <p role="status" className="m-0 flex flex-wrap items-center gap-2 text-sm">
        <StatePill kind={t.kind} label={t.label} /> {RUN_STATUS_TEXT[result.status] ?? `The workflow ended with status ${result.status}.`}
      </p>
      {result.status === "pending-approval" ? (
        <p className="m-0 text-sm">
          <UiLink href={approvalInboxHref(null)}>Open the approvals in the Inbox</UiLink>
        </p>
      ) : null}
      {result.instance_id ? <p className="m-0 text-sm">Role workflow instance {result.instance_id} was created; its steps are tasks for the bound seats.</p> : null}
      {result.steps ? <DataTable caption="Step results" columns={STEP_COLUMNS} data={result.steps} getRowKey={(s) => s.name} /> : null}
      {result.outputs && Object.keys(result.outputs).length ? <CodeBlock label="Step outputs" code={JSON.stringify(result.outputs, null, 2)} language="json" /> : null}
    </section>
  );
}

/** Candidates for a lost workflow start: "run started" events for this workflow by this identity since it was sent. */
function WorkflowAttemptCheck({ attempt, pendingElsewhere, identity, onFound, onRestart }: { attempt: StartAttempt; pendingElsewhere: boolean; identity: string | null; onFound: () => void; onRestart: () => void }) {
  const since = ulidFloor(attempt.sentAt - SKEW_MS);
  const q = useQuery({
    queryKey: ["cc2", "runs", "attempt", attempt.id],
    queryFn: ({ signal }) => fetchEvents({ channels: [WORKFLOW_CHANNEL], since, limit: HISTORY_LIMIT }, signal),
  });
  const retry = () => void q.refetch();
  const read: CandidateRead = q.isPending
    ? { state: "loading" }
    : q.isError
      ? { state: "failed", message: "The workflow history could not be read, so it is not known whether the run started.", retry }
      : {
          state: "ready",
          capped: q.data.events.length >= HISTORY_LIMIT,
          retry,
          items: q.data.events
            .filter((e) => e.payload.ref_id === attempt.match.name && startedSince(e.created_at, attempt.sentAt) && (identity === null || e.originator === identity) && /^run started/.test(e.payload.summary ?? ""))
            .map((e) => ({ id: e.ulid, label: `${e.payload.ref_id}: ${e.payload.summary} at ${formatWhen(e.created_at)}` })),
        };
  return (
    <AttemptCheck attempt={attempt} what="workflow runs" candidates={read} pendingElsewhere={pendingElsewhere} onFound={onFound} onRestart={onRestart}>
      <p className="m-0 text-xs text-[var(--ua-muted)]">This hub does not accept an idempotency key for workflow runs yet, so it cannot tell a repeat from a new run.</p>
    </AttemptCheck>
  );
}
