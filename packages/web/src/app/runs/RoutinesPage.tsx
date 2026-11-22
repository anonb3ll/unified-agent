import { useMemo, useRef, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, ConfirmDialog, DataTable, Dialog, Field, Input, Select, StatePill, Textarea, UiLink, type Column } from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useIdentity } from "../lib/identity";
import { useHubWrite, type WriteOutcome } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { fetchRoutines, ROUTINE_HISTORY, runsKeys, WRITE_PATHS, type Routine, type RoutineFireResult, type RoutineRun } from "./api";
import { defaultSettle, startedSince, useStartGuard, type GuardRefusal, type Settle, type StartAttempt } from "./attempt";
import { bothDayFieldsRestricted, describeRunTime, nextRuns, parseCron } from "./cron";
import { fireResultText, formatWhen, ROUTINE_REASON_TEXT, ROUTINE_RUN_TEXT, routinePill } from "./model";
import { AttemptCheck, Explainer, GuardRefused, RunsHeader, RunsRegion, Section, WriteFailure, useWriteContext, type CandidateRead } from "./parts";

/** Routines change only by these writes or the scheduler; no stream event exists, so poll calmly. */
const ROUTINES_POLL_MS = 60_000;

function scheduleText(r: Routine): string {
  if (r.trigger === "cron") return `${r.schedule ?? ""} (UTC)`;
  return r.trigger === "api" ? "Started through the API only" : "Started by a web hook only";
}

function nextText(r: Routine): string {
  if (r.paused) return "Paused";
  if (r.trigger !== "cron") return "Not scheduled";
  if (!r.next_run_at) return "Not known";
  const t = Date.parse(r.next_run_at);
  if (Number.isFinite(t) && t < Date.now()) return "Due now: runs at the next scheduler tick";
  return formatWhen(r.next_run_at);
}

function runReason(run: RoutineRun): string {
  const base = ROUTINE_RUN_TEXT[run.status] ?? run.status;
  const why = run.reason ? ROUTINE_REASON_TEXT[run.reason] ?? run.reason : null;
  return why && run.status !== "suppressed" ? `${base}: ${why}` : base;
}

/** `/runs/routines`: scheduled work (spec §9.6 Routines). */
export function RoutinesPage() {
  const routines = useQuery({ queryKey: runsKeys.routines, queryFn: ({ signal }) => fetchRoutines(signal), refetchInterval: ROUTINES_POLL_MS });
  const [creating, setCreating] = useState(false);
  const createRef = useRef<HTMLButtonElement>(null);
  const available = !(routines.isError && !routines.data);
  return (
    <>
      <RunsHeader
        title="Routines"
        description="Scheduled work: when each routine runs next and how its last runs went."
        actions={
          <Button ref={createRef} variant="primary" onClick={() => setCreating(true)} disabled={!available}>
            New routine
          </Button>
        }
      />
      <Explainer>
        A routine creates a task on a schedule (or when the API or a web hook calls it), so recurring work is never forgotten. Times are in UTC.
      </Explainer>
      <RunsRegion
        query={routines}
        loadingLabel="Loading routines"
        errorTitle="Routines could not be loaded"
        notAvailable="This hub does not list routines yet. It needs the routines routes from hub task #2480."
        isEmpty={(d) => d.routines.length === 0}
        empty={{
          title: "No routines yet",
          description: "Create one to have the hub open a task on a schedule.",
          action: (
            <Button variant="primary" onClick={() => setCreating(true)}>
              New routine
            </Button>
          ),
        }}
      >
        {(d) => (
          <>
            {!d.routines_enabled ? (
              <p role="note" className="m-0 mb-3 rounded-md border border-l-4 border-[var(--ua-warn)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
                <strong>Routines are switched off on this hub.</strong> Nothing runs on schedule, and Run now is refused, until the operator turns
                routines on (<code>UA_ROUTINES_ENABLED=1</code>).
              </p>
            ) : null}
            <ul className="m-0 flex list-none flex-col gap-4 p-0" aria-label="Routines">
              {d.routines.map((r) => (
                <li key={r.id}>
                  <RoutineCard routine={r} enabled={d.routines_enabled} />
                </li>
              ))}
            </ul>
          </>
        )}
      </RunsRegion>
      <CreateRoutine
        open={creating}
        onClose={() => {
          setCreating(false);
          setTimeout(() => createRef.current?.focus(), 0);
        }}
      />
    </>
  );
}

const HISTORY_COLUMNS: Column<RoutineRun>[] = [
  { id: "when", header: "When", cell: (r) => formatWhen(r.scheduled_for ?? r.created_at) },
  {
    id: "result",
    header: "Result",
    cell: (r) => {
      const p = routinePill(r.status);
      return <StatePill kind={p.kind} label={p.label} />;
    },
  },
  { id: "why", header: "What happened", cell: (r) => runReason(r) },
  { id: "task", header: "Task", cell: (r) => (r.task_id != null ? <UiLink href={`/work/tasks/${r.task_id}`}>#{r.task_id}</UiLink> : "None") },
];

function RoutineCard({ routine, enabled }: { routine: Routine; enabled: boolean }) {
  const paused = Boolean(routine.paused);
  const pause = useHubWrite<{ routine_id: number }, { id: number; paused: number }>({
    path: paused ? WRITE_PATHS.routineResume : WRITE_PATHS.routinePause,
    label: paused ? `Resuming “${routine.title}”` : `Pausing “${routine.title}”`,
    invalidates: [runsKeys.routines],
    successMessage: paused ? `“${routine.title}” resumed.` : `“${routine.title}” paused.`,
  });
  const [confirmPause, setConfirmPause] = useState(false);
  const pauseRef = useRef<HTMLButtonElement>(null);
  const history = routine.recent_runs ?? [];
  return (
    <article aria-labelledby={`routine-${routine.id}`} className="rounded-md border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 id={`routine-${routine.id}`} className="m-0 text-base font-semibold">
            {routine.title}
          </h2>
          <p className="m-0 text-sm text-[var(--ua-muted)]">
            {scheduleText(routine)} · Next run: {nextText(routine)}
          </p>
        </div>
        <div className="flex items-center gap-2">{paused ? <StatePill kind="warn" label="Paused" /> : <StatePill kind="live" label="Active" />}</div>
      </div>
      <p className="m-0 mt-1 text-xs text-[var(--ua-muted)]">
        If a run is still open when the next one is due: {routine.concurrency === "queue" ? "queue the new run behind it" : "skip the new run"}. Missed
        times: {routine.catch_up === "none" ? "skipped" : routine.catch_up === "all" ? "all run" : "the latest one runs"}.
        {routine.activity_gate === "require_activity" ? " Runs only when something happened since the last run." : ""}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          ref={pauseRef}
          size="sm"
          loading={pause.isRunning}
          onClick={() => (paused ? void pause.run({ routine_id: routine.id }) : setConfirmPause(true))}
        >
          {paused ? "Resume" : "Pause"}
        </Button>
        <RunNow routine={routine} enabled={enabled} />
      </div>
      {pause.state.status === "error" ? <div className="mt-2"><WriteFailure message={pause.state.message} detail={pause.state.detail} /></div> : null}
      {pause.state.status === "pending_approval" ? <ParkedWriteNotice className="mt-2" approvalId={pause.state.approvalId} label={paused ? "Resuming the routine" : "Pausing the routine"} /> : null}
      <ConfirmDialog
        open={confirmPause}
        title="Pause this routine?"
        objectName={routine.title}
        consequence="It stops creating tasks on its schedule until you resume it. Tasks it already created are kept. When resumed, it starts again from the next scheduled time."
        confirmLabel="Pause routine"
        busy={pause.isRunning}
        onCancel={() => {
          setConfirmPause(false);
          setTimeout(() => pauseRef.current?.focus(), 0);
        }}
        onConfirm={async () => {
          await pause.run({ routine_id: routine.id });
          setConfirmPause(false);
          setTimeout(() => pauseRef.current?.focus(), 0);
        }}
      />
      <details className="mt-3">
        <summary className="cursor-pointer text-sm">Last {Math.min(history.length, ROUTINE_HISTORY)} runs</summary>
        {history.length ? (
          <DataTable className="mt-2" caption={`Recent runs of ${routine.title}, newest first`} columns={HISTORY_COLUMNS} data={history} getRowKey={(r) => r.id} />
        ) : (
          <p className="m-0 mt-2 text-sm text-[var(--ua-muted)]">It has not run yet.</p>
        )}
        {history.length >= ROUTINE_HISTORY ? <p className="m-0 mt-1 text-xs text-[var(--ua-muted)]">Only the newest {ROUTINE_HISTORY} runs are shown.</p> : null}
      </details>
    </article>
  );
}

const RUN_CONFLICT_TEXT: Record<string, string> = {
  in_flight: "The hub is still running the first request with this key. Ask again shortly with the same key to read its answer.",
  conflict: "The hub says this key was already used for a different request, so this request fired nothing. Whether the first one ran is not known from here.",
  ambiguous_receipt: "The hub cannot tell whether an earlier run under this key belongs to this request. Check the routine's history; a new start needs a new key.",
};

/**
 * What a Run now answer means for its attempt (`docs/control-center-api.md`,
 * Run now): success settles it; a 202 parks it under the same key; a 409
 * `in_flight`, `conflict` or `ambiguous_receipt` keeps the key and the hold.
 * Only a 409 the hub explains as paused or scheduler-only is a plain refusal.
 */
export function routineRunSettle(outcome: WriteOutcome<RoutineFireResult>): Settle {
  if (outcome.kind === "pending_approval") return { record: "parked", approvalId: outcome.approvalId };
  if (outcome.kind === "error" && outcome.status === 409) {
    const code = outcome.code;
    if (code && RUN_CONFLICT_TEXT[code]) return { record: "unknown", note: RUN_CONFLICT_TEXT[code] };
    if (/paused|fired by the scheduler/i.test(outcome.detail ?? "")) return { record: "clear" };
    return { record: "unknown", note: "The hub refused with a conflict and did not say why; the first request may still be running. Ask again with the same key." };
  }
  return defaultSettle(outcome);
}

/**
 * Run now. One attempt id per press, sent as `Idempotency-Key`. The same
 * key is used to ask again after a lost answer or an `in_flight` refusal,
 * and to send it again after approval (the parked write stored by
 * `useHubWrite` carries the same key), so every path is one fire.
 */
function RunNow({ routine, enabled }: { routine: Routine; enabled: boolean }) {
  const context = useWriteContext();
  const guard = useStartGuard(context, "routine-run", String(routine.id));
  const keyRef = useRef("");
  const [confirm, setConfirm] = useState(false);
  const [answer, setAnswer] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<GuardRefusal | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const write = useHubWrite<{ routine_id: number }, RoutineFireResult>({
    path: WRITE_PATHS.routineRun,
    label: `Run now of “${routine.title}”`,
    invalidates: [runsKeys.routines],
    idempotencyKey: () => keyRef.current,
    // Not stored for a replay from the Inbox or the notice: after approval the
    // run is sent again only through this page's guarded dispatch, with the
    // same key, so no second path can settle or release the attempt.
    resumable: false,
    pendingMessage: `Run now of “${routine.title}” is waiting for approval. After it is approved, send it again here with the same key.`,
  });
  const attempt = guard.attempt;

  const send = async (options: { reuseId?: string; replaceHeld?: string } = {}) => {
    setAnswer(null);
    setRefusal(null);
    const r = await guard.run(
      { summary: `Run now of “${routine.title}”`, match: { routineId: routine.id } },
      (id) => {
        keyRef.current = id;
        return write.run({ routine_id: routine.id });
      },
      routineRunSettle,
      options,
    );
    if (!r.sent) setRefusal(r.refusal);
    else if (r.outcome.kind === "done" || r.outcome.kind === "accepted") setAnswer(fireResultText(r.outcome.data));
  };

  const disabledReason = routine.paused ? "Resume the routine to run it." : !enabled ? "Routines are switched off on this hub." : null;

  return (
    <>
      <Button
        ref={triggerRef}
        size="sm"
        variant="secondary"
        loading={write.isRunning}
        disabled={Boolean(disabledReason) || guard.held || !guard.key}
        aria-describedby={disabledReason ? `runnow-why-${routine.id}` : undefined}
        onClick={() => setConfirm(true)}
      >
        Run now
      </Button>
      {disabledReason ? (
        <span id={`runnow-why-${routine.id}`} className="self-center text-xs text-[var(--ua-muted)]">
          {disabledReason}
        </span>
      ) : null}
      <ConfirmDialog
        open={confirm}
        title="Run this routine now?"
        objectName={routine.title}
        consequence={`It runs once now, as if it were due, and its schedule is unchanged. If its last run is still open, it ${routine.concurrency === "queue" ? "queues this run behind it" : "skips this run"}.`}
        confirmLabel="Run now"
        busy={write.isRunning}
        onCancel={() => {
          setConfirm(false);
          setTimeout(() => triggerRef.current?.focus(), 0);
        }}
        onConfirm={() => {
          setConfirm(false);
          setTimeout(() => triggerRef.current?.focus(), 0);
          void send();
        }}
      />
      <div className="basis-full">
        {answer ? (
          <p role="status" className="m-0 mt-2 text-sm">
            {answer}
          </p>
        ) : null}
        <GuardRefused refusal={refusal} />
        {write.state.status === "error" && !guard.held ? (
          <div className="mt-2">
            <WriteFailure message={write.state.message} detail={write.state.detail} />
          </div>
        ) : null}
        {guard.held && attempt?.state === "parked" ? (
          <div className="mt-2 flex flex-col gap-2">
            <ParkedWriteNotice
              approvalId={attempt.approvalId ?? null}
              label={`Run now of “${routine.title}”`}
              continuation={{
                label: "Send it again with the same key",
                description: "After it is approved, send it again here. It keeps its key, so it is the same single run, never a second one.",
                onAction: () => void send({ reuseId: attempt.id }),
              }}
            />
          </div>
        ) : null}
        {guard.held && attempt && attempt.state !== "parked" ? (
          <div className="mt-2">
            <AttemptCheck
              attempt={attempt}
              what="runs"
              pendingElsewhere={guard.pendingElsewhere}
              onFound={() => void guard.resolve(attempt.id)}
              onRestart={() => void send({ replaceHeld: attempt.id })}
            >
              <p className="m-0">
                Run now carries a key the hub remembers. Asking again with the same key is safe: if the first request ran, the hub answers with its
                first answer and fires nothing new.
              </p>
              <div>
                <Button size="sm" variant="primary" loading={write.isRunning} onClick={() => void send({ reuseId: attempt.id })}>
                  Ask the hub with the same key
                </Button>
              </div>
            </AttemptCheck>
          </div>
        ) : null}
      </div>
    </>
  );
}

type RoutineDraft = {
  title: string;
  trigger: string;
  schedule: string;
  notes: string;
  project: string;
  concurrency: string;
  catch_up: string;
  activity_gate: string;
};

const INITIAL: RoutineDraft = { title: "", trigger: "cron", schedule: "", notes: "", project: "", concurrency: "skip", catch_up: "last", activity_gate: "always" };

function CreateRoutine({ open, onClose }: { open: boolean; onClose: () => void }) {
  const identity = useIdentity();
  const context = useWriteContext();
  const draft = useDraft<RoutineDraft>({ identity, operation: "runs.routine.add", initial: INITIAL });
  const latest = useRef(draft.value);
  latest.current = draft.value;
  const [submitted, setSubmitted] = useState(false);
  const [refusal, setRefusal] = useState<GuardRefusal | null>(null);
  const formRef = useRef<HTMLDivElement>(null);
  const guard = useStartGuard(context, "routine-add");
  const write = useHubWrite<Record<string, string>, { id: number }>({
    path: WRITE_PATHS.routineAdd,
    label: "Creating the routine",
    invalidates: [runsKeys.routines],
    successMessage: "Routine created.",
    // A repeat would create a second schedule; the person submits again after approval.
    resumable: false,
  });
  const v = draft.value;
  const cron = useMemo(() => (v.trigger === "cron" && v.schedule.trim() ? parseCron(v.schedule) : null), [v.trigger, v.schedule]);
  const upcoming = useMemo(() => (cron?.ok ? nextRuns(cron.fields, new Date(), 3) : []), [cron]);
  const problemsOf = (value: RoutineDraft) => {
    const out: Partial<Record<keyof RoutineDraft, string>> = {};
    if (!value.title.trim()) out.title = "Give the routine a name.";
    else if (value.title.includes("\n") || value.title.length > 200) out.title = "Keep the name to one line of at most 200 characters.";
    if (value.trigger === "cron") {
      const parsed = value.schedule.trim() ? parseCron(value.schedule) : null;
      if (!parsed) out.schedule = "Enter when it runs, for example 0 9 * * 1 for every Monday at 09:00 UTC.";
      else if (!parsed.ok) out.schedule = parsed.error;
      else if (nextRuns(parsed.fields, new Date(), 1).length === 0) out.schedule = "This schedule never runs.";
    }
    return out;
  };
  const problems = problemsOf(v);

  /** The one way to create: validate, then send under the guard. */
  const dispatch = async (options: { replaceHeld?: string } = {}) => {
    setSubmitted(true);
    const value = latest.current;
    if (Object.keys(problemsOf(value)).length) {
      setTimeout(() => formRef.current?.closest("form")?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus(), 0);
      return;
    }
    if (write.isRunning) return;
    const sentRevision = JSON.stringify(value);
    const body: Record<string, string> = { title: value.title.trim(), trigger: value.trigger, concurrency: value.concurrency, catch_up: value.catch_up, activity_gate: value.activity_gate };
    if (value.trigger === "cron") body.schedule = value.schedule.trim().split(/\s+/).join(" ");
    if (value.notes.trim()) body.notes = value.notes.trim();
    if (value.project.trim()) body.project = value.project.trim();
    setRefusal(null);
    const r = await guard.run({ summary: `Creating the routine “${body.title}”`, match: { title: body.title } }, () => write.run(body), defaultSettle, options);
    if (!r.sent) {
      setRefusal(r.refusal);
      return;
    }
    if (r.outcome.kind === "done") {
      if (JSON.stringify(latest.current) === sentRevision) {
        draft.clear();
        setSubmitted(false);
        onClose();
      }
    }
  };

  const submit = async () => {
    if (guard.held) return;
    await dispatch();
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New routine"
      description="It creates a task each time it runs. Nothing runs until routines are switched on for this hub."
      size="md"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        void submit();
      }}
      submitOnEnter={false}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={write.isRunning}>
            Create routine
          </Button>
        </>
      }
    >
      <div ref={formRef} className="flex flex-col gap-4">
        {draft.restored ? <p className="m-0 text-sm text-[var(--ua-muted)]">Your unfinished routine was restored.</p> : null}
        <Field label="Name" required error={submitted ? problems.title : undefined} hint="Also the title of each task it creates.">
          <Input value={v.title} onChange={(e) => draft.setValue((p) => ({ ...p, title: e.target.value }))} />
        </Field>
        <Field label="How it starts">
          <Select
            value={v.trigger}
            onChange={(e) => draft.setValue((p) => ({ ...p, trigger: e.target.value }))}
            options={[
              { value: "cron", label: "On a schedule" },
              { value: "api", label: "Only when called through the API" },
              { value: "webhook", label: "Only when a web hook calls it" },
            ]}
          />
        </Field>
        {v.trigger === "cron" ? (
          <Field
            label="Schedule (UTC)"
            required
            error={submitted ? problems.schedule : undefined}
            hint="Five parts: minute, hour, day of the month, month, day of the week. 0 9 * * 1 means every Monday at 09:00 UTC."
          >
            <Input className="font-mono" value={v.schedule} onChange={(e) => draft.setValue((p) => ({ ...p, schedule: e.target.value }))} />
          </Field>
        ) : null}
        {v.trigger === "cron" && cron ? (
          <div role="status" aria-live="polite" className="text-sm">
            {cron.ok && upcoming.length ? (
              <>
                <p className="m-0 font-medium">The next three runs would be:</p>
                <ul className="m-0 pl-5">
                  {upcoming.map((d) => (
                    <li key={d.toISOString()}>{describeRunTime(d)}</li>
                  ))}
                </ul>
                {bothDayFieldsRestricted(v.schedule) ? (
                  <p className="m-0 mt-1 text-xs text-[var(--ua-muted)]">
                    This hub runs a routine only on days that match both the day of the month and the day of the week.
                  </p>
                ) : null}
              </>
            ) : (
              <p className="m-0 text-[var(--ua-muted)]">Fix the schedule to see when it would run.</p>
            )}
          </div>
        ) : null}
        <Field label="If the previous run is still open">
          <Select
            value={v.concurrency}
            onChange={(e) => draft.setValue((p) => ({ ...p, concurrency: e.target.value }))}
            options={[
              { value: "skip", label: "Skip the new run" },
              { value: "queue", label: "Queue the new run behind it" },
            ]}
          />
        </Field>
        <Field label="Missed times (the hub was off)">
          <Select
            value={v.catch_up}
            onChange={(e) => draft.setValue((p) => ({ ...p, catch_up: e.target.value }))}
            options={[
              { value: "last", label: "Run the latest missed time once" },
              { value: "all", label: "Run every missed time" },
              { value: "none", label: "Skip them" },
            ]}
          />
        </Field>
        <Field label="Run only after activity">
          <Select
            value={v.activity_gate}
            onChange={(e) => draft.setValue((p) => ({ ...p, activity_gate: e.target.value }))}
            options={[
              { value: "always", label: "Always run" },
              { value: "require_activity", label: "Only when something happened since the last run" },
            ]}
          />
        </Field>
        <Field label="Project" hint="Leave empty for the hub's default project.">
          <Input value={v.project} onChange={(e) => draft.setValue((p) => ({ ...p, project: e.target.value }))} />
        </Field>
        <Field label="Notes" hint="Copied into each task it creates.">
          <Textarea value={v.notes} onChange={(e) => draft.setValue((p) => ({ ...p, notes: e.target.value }))} />
        </Field>
        {guard.held && guard.attempt ? (
          <RoutineAddCheck
            attempt={guard.attempt}
            pendingElsewhere={guard.pendingElsewhere}
            onFound={() => guard.attempt && void guard.resolve(guard.attempt.id)}
            onRestart={() => void dispatch({ replaceHeld: guard.attempt?.id })}
          />
        ) : null}
        <GuardRefused refusal={refusal} />
        {write.state.status === "error" && !guard.held ? <WriteFailure message={write.state.message} detail={write.state.detail} /> : null}
        {write.state.status === "pending_approval" ? <ParkedWriteNotice approvalId={write.state.approvalId} label="Creating the routine" /> : null}
      </div>
    </Dialog>
  );
}

/** Candidates for a lost routine create: routines with the same name created since it was sent. */
function RoutineAddCheck({ attempt, pendingElsewhere, onFound, onRestart }: { attempt: StartAttempt; pendingElsewhere: boolean; onFound: () => void; onRestart: () => void }) {
  const q = useQuery({ queryKey: [...runsKeys.routines, "attempt", attempt.id], queryFn: ({ signal }) => fetchRoutines(signal) });
  const retry = () => void q.refetch();
  const read: CandidateRead = q.isPending
    ? { state: "loading" }
    : q.isError
      ? { state: "failed", message: "The routine list could not be read, so it is not known whether the routine was created.", retry }
      : {
          state: "ready",
          capped: false,
          retry,
          items: q.data.routines
            .filter((r) => r.title === attempt.match.title && startedSince(r.created_at, attempt.sentAt))
            .map((r) => ({ id: String(r.id), label: `Routine ${r.id}, “${r.title}”, created ${formatWhen(r.created_at)}` })),
        };
  return <AttemptCheck attempt={attempt} what="routines" candidates={read} pendingElsewhere={pendingElsewhere} onFound={onFound} onRestart={onRestart} />;
}
