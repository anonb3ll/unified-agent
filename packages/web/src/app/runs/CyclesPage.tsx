import { useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useStreamInvalidation } from "../../lib/polling";
import { Button, DataTable, Dialog, Field, Input, LinkButton, StatePill, UiLink, type Column } from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useIdentity } from "../lib/identity";
import { useHubWrite } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { CYCLE_LIMIT, fetchCycles, runsKeys, WRITE_PATHS, type Cycle } from "./api";
import { defaultSettle, startedSince, useStartGuard, type GuardRefusal, type StartAttempt } from "./attempt";
import { budgetText, cycleBudgets, cyclePill, formatWhen } from "./model";
import { AttemptCheck, Explainer, GuardRefused, RunsHeader, RunsRegion, WriteFailure, useWriteContext, type CandidateRead } from "./parts";

export const CYCLE_CHANNELS = ["agent.cycle.*"];

export const CYCLE_EXPLAINER =
  "A cycle is a supervised loop that takes one task from plan to pull request, step by step, within limits on time, tokens, retries and cost. Starting one creates its record in the planning step; it does not start any work by itself.";

const COLUMNS: Column<Cycle>[] = [
  {
    id: "cycle",
    header: "Cycle",
    cell: (c) => (
      <UiLink href={`/runs/cycles/${encodeURIComponent(c.id)}`} className="font-mono text-xs">
        {c.id}
      </UiLink>
    ),
  },
  { id: "task", header: "Task", cell: (c) => (c.task_id != null ? <UiLink href={`/work/tasks/${c.task_id}`}>#{c.task_id}</UiLink> : "No task") },
  {
    id: "state",
    header: "State",
    cell: (c) => {
      const p = cyclePill(c);
      return (
        <span className="flex flex-col gap-0.5">
          <StatePill kind={p.kind} label={p.label} />
          {c.stop_reason ? <span className="text-xs text-[var(--ua-muted)]">{c.stop_reason}</span> : null}
        </span>
      );
    },
  },
  {
    id: "budget",
    header: "Limits used",
    cell: (c) => (
      <ul className="m-0 list-none p-0 text-xs">
        {cycleBudgets(c).map((b) => (
          <li key={b.label} className={b.over ? "font-semibold text-[var(--ua-warn)]" : undefined}>
            {b.label}: {budgetText(b)}
            {b.over ? " (over the limit)" : ""}
          </li>
        ))}
      </ul>
    ),
  },
  { id: "by", header: "Started by", cell: (c) => c.created_by ?? "Not recorded" },
  { id: "updated", header: "Last change", cell: (c) => formatWhen(c.updated_at) },
];

/** `/runs/cycles` (spec §9.6 Cycles). */
export function CyclesPage() {
  const queryClient = useQueryClient();
  const cycles = useQuery({ queryKey: runsKeys.cycles, queryFn: ({ signal }) => fetchCycles(signal) });
  useStreamInvalidation(() => void queryClient.invalidateQueries({ queryKey: runsKeys.cycles }), { channels: CYCLE_CHANNELS, onReconnect: true });
  const [starting, setStarting] = useState(false);
  const startRef = useRef<HTMLButtonElement>(null);
  return (
    <>
      <RunsHeader
        title="Cycles"
        description="Supervised work loops: start, pause and stop them."
        actions={
          <Button ref={startRef} variant="primary" onClick={() => setStarting(true)}>
            Start a cycle
          </Button>
        }
      />
      <Explainer>{CYCLE_EXPLAINER}</Explainer>
      <RunsRegion
        query={cycles}
        loadingLabel="Loading cycles"
        errorTitle="Cycles could not be loaded"
        isEmpty={(d) => d.cycles.length === 0}
        empty={{ title: "No cycles yet", description: "Start one for a task to run it through plan, build, review and pull request." }}
      >
        {(d) => (
          <>
            {d.capped ? <p className="m-0 mb-2 text-sm text-[var(--ua-warn)]">Showing the newest {CYCLE_LIMIT} cycles; older ones are not listed.</p> : null}
            <DataTable caption="Cycles, with the share of each limit used" columns={COLUMNS} data={d.cycles} getRowKey={(c) => c.id} />
          </>
        )}
      </RunsRegion>
      <StartCycle
        open={starting}
        onClose={() => {
          setStarting(false);
          setTimeout(() => startRef.current?.focus(), 0);
        }}
      />
    </>
  );
}

type StartDraft = { taskId: string; maxRework: string; timeS: string; tokens: string; retries: string; costCents: string };
const INITIAL: StartDraft = { taskId: "", maxRework: "2", timeS: "", tokens: "", retries: "", costCents: "" };

function positive(value: string): number | null {
  if (!value.trim()) return null;
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : Number.NaN;
}

/** Start a cycle. It never posts without a task (spec §12 defect 7). */
function StartCycle({ open, onClose }: { open: boolean; onClose: () => void }) {
  const identity = useIdentity();
  const context = useWriteContext();
  const draft = useDraft<StartDraft>({ identity, operation: "runs.cycle.start", initial: INITIAL });
  const latest = useRef(draft.value);
  latest.current = draft.value;
  const [submitted, setSubmitted] = useState(false);
  const [created, setCreated] = useState<Cycle | null>(null);
  const [refusal, setRefusal] = useState<GuardRefusal | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const guard = useStartGuard(context, "cycle");
  const write = useHubWrite<Record<string, unknown>, { cycle: Cycle; created?: boolean }>({
    path: WRITE_PATHS.cycle,
    label: "Starting the cycle",
    invalidates: [runsKeys.cycles],
    // No idempotency key on cycle start: a repeat would create a second cycle.
    resumable: false,
    successMessage: (d) => `Cycle ${d.cycle.id} started for task #${d.cycle.task_id}.`,
  });
  const attempt = guard.attempt;
  const v = draft.value;
  const problemsOf = (value: StartDraft) => {
    const out: Partial<Record<keyof StartDraft, string>> = {};
    const t = Number(value.taskId);
    const rw = Number(value.maxRework);
    if (!value.taskId.trim() || !Number.isSafeInteger(t) || t <= 0) out.taskId = "Enter the number of the task this cycle works on.";
    if (!Number.isInteger(rw) || rw < 0 || rw > 20) out.maxRework = "Enter a whole number from 0 to 20.";
    for (const k of ["timeS", "tokens", "retries", "costCents"] as const) if (Number.isNaN(positive(value[k]))) out[k] = "Enter a number above 0, or leave it empty for the hub's default.";
    return out;
  };
  const problems = problemsOf(v);

  /** The one way to start: validate (never without a task), then send under the guard. */
  const dispatch = async (options: { replaceHeld?: string } = {}) => {
    setSubmitted(true);
    const value = latest.current;
    if (Object.keys(problemsOf(value)).length) {
      setTimeout(() => bodyRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus(), 0);
      return;
    }
    if (write.isRunning) return;
    const taskId = Number(value.taskId);
    const budgets: Record<string, number> = {};
    const add = (key: string, raw: string, integer: boolean) => {
      const n = positive(raw);
      if (n !== null && !Number.isNaN(n)) budgets[key] = integer ? Math.round(n) : n;
    };
    add("time_s", value.timeS, false);
    add("tokens", value.tokens, true);
    add("retries", value.retries, true);
    add("cost_cents", value.costCents, false);
    const body = { operation: "start", task_id: taskId, max_rework: Number(value.maxRework), ...(Object.keys(budgets).length ? { budgets } : {}) };
    const sentRevision = JSON.stringify(value);
    setCreated(null);
    setRefusal(null);
    const r = await guard.run({ summary: `A cycle for task #${taskId}`, match: { taskId } }, () => write.run(body), defaultSettle, options);
    if (!r.sent) {
      setRefusal(r.refusal);
      return;
    }
    if (r.outcome.kind === "done" || r.outcome.kind === "accepted") {
      setCreated(r.outcome.data.cycle);
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

  const field = (key: keyof StartDraft, label: string, hint?: string, required = false) => (
    <Field label={label} hint={hint} required={required} error={submitted ? problems[key] : undefined}>
      <Input inputMode="numeric" value={v[key]} onChange={(e) => draft.setValue((p) => ({ ...p, [key]: e.target.value }))} />
    </Field>
  );

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Start a cycle"
      description="It is created in the planning step for the task. No work starts until an agent moves it on."
      onSubmit={submit}
      submitOnEnter={false}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {created ? "Close" : "Cancel"}
          </Button>
          <Button type="submit" variant="primary" loading={write.isRunning} disabled={guard.held || !guard.key}>
            Start cycle
          </Button>
        </>
      }
    >
      <div ref={bodyRef} className="flex flex-col gap-4">
        {guard.held && attempt ? (
          <CycleAttemptCheck attempt={attempt} pendingElsewhere={guard.pendingElsewhere} identity={context?.identity ?? null} onFound={() => void guard.resolve(attempt.id)} onRestart={() => void dispatch({ replaceHeld: attempt.id })} />
        ) : null}
        <GuardRefused refusal={refusal} />
        {draft.restored ? <p className="m-0 text-sm text-[var(--ua-muted)]">Your unfinished form was restored.</p> : null}
        {field("taskId", "Task number", "The task this cycle works on.", true)}
        {field("maxRework", "Rework rounds allowed", "How many times review may send the work back (0 to 20).")}
        <fieldset className="m-0 flex flex-col gap-3 border-0 p-0">
          <legend className="mb-1 text-sm font-medium">Limits (empty uses the hub's default)</legend>
          {field("timeS", "Time limit, seconds")}
          {field("tokens", "Token limit")}
          {field("retries", "Retry limit")}
          {field("costCents", "Cost limit, cents")}
        </fieldset>
        {write.state.status === "error" ? <WriteFailure message={write.state.message} detail={write.state.detail} /> : null}
        {write.state.status === "pending_approval" ? <ParkedWriteNotice approvalId={write.state.approvalId} label="Starting the cycle" /> : null}
        {created ? (
          <p role="status" className="m-0 text-sm">
            Cycle {created.id} was created{created.state === "blocked" ? `, but it is paused: ${created.stop_reason ?? "a permission it asked for was refused"}` : ""}.{" "}
            <LinkButton size="sm" href={`/runs/cycles/${encodeURIComponent(created.id)}`}>
              Open cycle {created.id}
            </LinkButton>
          </p>
        ) : null}
      </div>
    </Dialog>
  );
}

function CycleAttemptCheck({ attempt, pendingElsewhere, identity, onFound, onRestart }: { attempt: StartAttempt; pendingElsewhere: boolean; identity: string | null; onFound: () => void; onRestart: () => void }) {
  const q = useQuery({ queryKey: [...runsKeys.cycles, "attempt", attempt.id], queryFn: ({ signal }) => fetchCycles(signal) });
  const retry = () => void q.refetch();
  const read: CandidateRead = q.isPending
    ? { state: "loading" }
    : q.isError
      ? { state: "failed", message: "The cycle list could not be read, so it is not known whether the cycle was created.", retry }
      : {
          state: "ready",
          capped: q.data.capped,
          retry,
          items: q.data.cycles
            .filter((c) => c.task_id === attempt.match.taskId && (identity === null || c.created_by === identity) && startedSince(c.created_at, attempt.sentAt))
            .map((c) => ({ id: c.id, label: `Cycle ${c.id} for task #${c.task_id}, created ${formatWhen(c.created_at)}`, href: `/runs/cycles/${encodeURIComponent(c.id)}` })),
        };
  return (
    <AttemptCheck attempt={attempt} what="cycles" candidates={read} pendingElsewhere={pendingElsewhere} onFound={onFound} onRestart={onRestart}>
      <p className="m-0 text-xs text-[var(--ua-muted)]">This hub does not accept an idempotency key for starting a cycle yet.</p>
    </AttemptCheck>
  );
}
