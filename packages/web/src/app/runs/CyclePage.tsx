import { useEffect, useRef, useState, type RefObject } from "react";
import { useParams } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useStreamInvalidation } from "../../lib/polling";
import { Button, ConfirmDialog, DataTable, DescriptionList, Field, Input, LinkButton, RadioGroup, StatePill, Textarea, UiLink, type Column } from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useIdentity } from "../lib/identity";
import { useHubWrite } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { fetchCycle, runsKeys, WRITE_PATHS, type Cycle, type CycleTransition } from "./api";
import { newAttemptId } from "./attempt";
import { CYCLE_CHANNELS, CYCLE_EXPLAINER } from "./CyclesPage";
import {
  budgetText,
  canPause,
  canResume,
  canStop,
  CYCLE_APPROVAL_STATES,
  CYCLE_NEXT,
  CYCLE_STATE_TEXT,
  cycleBudgets,
  cyclePill,
  formatWhen,
  looksLikeEvidence,
} from "./model";
import { Explainer, RunsHeader, RunsRegion, Section, WriteFailure } from "./parts";

const TRANSITION_COLUMNS: Column<CycleTransition>[] = [
  { id: "when", header: "When", cell: (t) => formatWhen(t.created_at) },
  { id: "from", header: "From", cell: (t) => (t.from_state ? CYCLE_STATE_TEXT[t.from_state] ?? t.from_state : "Start") },
  { id: "to", header: "To", cell: (t) => CYCLE_STATE_TEXT[t.to_state] ?? t.to_state },
  { id: "by", header: "By", cell: (t) => t.actor ?? "Not recorded" },
  { id: "why", header: "Reason", cell: (t) => t.reason ?? "" },
];

/** `/runs/cycles/:id`: one cycle, what it did, and its actions. */
export function CyclePage() {
  const { id = "" } = useParams();
  const queryClient = useQueryClient();
  const cycle = useQuery({ queryKey: runsKeys.cycle(id), queryFn: ({ signal }) => fetchCycle(id, signal), enabled: id !== "" });
  useStreamInvalidation(() => void queryClient.invalidateQueries({ queryKey: runsKeys.cycle(id) }), { channels: CYCLE_CHANNELS, onReconnect: true });
  return (
    <>
      <RunsHeader title={`Cycle ${id}`} description="One cycle: where it is, what it has used, and every step it took." actions={<LinkButton href="/runs/cycles">All cycles</LinkButton>} />
      <Explainer>{CYCLE_EXPLAINER}</Explainer>
      <RunsRegion
        query={cycle}
        loadingLabel="Loading the cycle"
        errorTitle="This cycle could not be loaded"
        empty={{ title: "Not found" }}
      >
        {(d) => <CycleDetail key={d.cycle.id} cycle={d.cycle} transitions={d.transitions} />}
      </RunsRegion>
    </>
  );
}

type ActionKind = "advance" | "pause" | "resume" | "stop";

function CycleDetail({ cycle, transitions }: { cycle: Cycle; transitions: CycleTransition[] }) {
  const pill = cyclePill(cycle);
  const [open, setOpen] = useState<ActionKind | null>(null);
  const triggers = {
    advance: useRef<HTMLButtonElement>(null),
    pause: useRef<HTMLButtonElement>(null),
    resume: useRef<HTMLButtonElement>(null),
    stop: useRef<HTMLButtonElement>(null),
  } satisfies Record<ActionKind, RefObject<HTMLButtonElement | null>>;
  const close = () => {
    const which = open;
    setOpen(null);
    if (which) setTimeout(() => triggers[which].current?.focus(), 0);
  };
  const next = CYCLE_NEXT[cycle.state] ?? [];
  return (
    <>
      <Section title="Where it is">
        <p role="status" aria-live="polite" aria-label={`Cycle ${cycle.id}: ${pill.label}`} className="m-0 mb-3 flex flex-wrap items-center gap-2 text-sm">
          <StatePill kind={pill.kind} label={pill.label} />
          {cycle.stop_reason ? <span>{cycle.stop_reason}</span> : null}
        </p>
        <DescriptionList
          columns={2}
          items={[
            { term: "Task", value: cycle.task_id != null ? <UiLink href={`/work/tasks/${cycle.task_id}`}>#{cycle.task_id}</UiLink> : "No task" },
            { term: "Plan", value: cycle.plan_id ?? "" },
            { term: "Started by", value: cycle.created_by ?? "" },
            { term: "Started", value: formatWhen(cycle.created_at) },
            { term: "Last change", value: formatWhen(cycle.updated_at) },
            { term: "Rework rounds", value: `${cycle.rework_count ?? 0} of ${cycle.max_rework ?? 0}` },
            { term: "Pull request or commit", value: cycle.pr_url ? (/^https?:\/\//.test(cycle.pr_url) ? <UiLink href={cycle.pr_url}>{cycle.pr_url}</UiLink> : cycle.pr_url) : "" },
            { term: "Allowed tools", value: cycle.allowed_capabilities?.length ? cycle.allowed_capabilities.join(", ") : "" },
          ]}
        />
      </Section>
      <Section title="Limits used">
        <ul className="m-0 list-none p-0 text-sm">
          {cycleBudgets(cycle).map((b) => (
            <li key={b.label} className={b.over ? "font-semibold text-[var(--ua-warn)]" : undefined}>
              {b.label}: {budgetText(b)}
              {b.over ? " (over the limit)" : ""}
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Actions" description="Moving a cycle to a pull request, completing it, pausing it or failing it may need someone's approval first.">
        <div className="flex flex-wrap gap-2">
          {next.length ? (
            <Button ref={triggers.advance} variant="primary" onClick={() => setOpen("advance")}>
              Move to the next step
            </Button>
          ) : null}
          {canPause(cycle) ? (
            <Button ref={triggers.pause} onClick={() => setOpen("pause")}>
              Pause
            </Button>
          ) : null}
          {canResume(cycle) ? (
            <Button ref={triggers.resume} onClick={() => setOpen("resume")}>
              Resume
            </Button>
          ) : null}
          {canStop(cycle) ? (
            <Button ref={triggers.stop} variant="danger" onClick={() => setOpen("stop")}>
              Stop
            </Button>
          ) : null}
          {!next.length && !canPause(cycle) && !canResume(cycle) && !canStop(cycle) ? <p className="m-0 text-sm text-[var(--ua-muted)]">This cycle has ended; nothing more can be done with it.</p> : null}
        </div>
        {/* Keyed by action and cycle: one dialog's typed state never shows in another. */}
        <CycleAction key={`${cycle.id}:${open ?? "none"}`} cycle={cycle} kind={open} onClose={close} />
      </Section>
      <Section title="History">
        {transitions.length ? (
          <DataTable caption={`Steps of cycle ${cycle.id}, oldest first`} columns={TRANSITION_COLUMNS} data={transitions} getRowKey={(t) => t.id} />
        ) : (
          <p className="m-0 text-sm text-[var(--ua-muted)]">It has not changed step since it was started.</p>
        )}
      </Section>
    </>
  );
}

type ActionDraft = { reason: string; prUrl: string; toState: string; key: string };

/**
 * One dialog for the four actions. Each has a draft (its text survives
 * navigation) and, for transitions, an idempotency key kept for the attempt
 * so a resend after a lost answer cannot apply it twice.
 */
function CycleAction({ cycle, kind, onClose }: { cycle: Cycle; kind: ActionKind | null; onClose: () => void }) {
  const identity = useIdentity();
  const next = CYCLE_NEXT[cycle.state] ?? [];
  const draft = useDraft<ActionDraft>({
    identity,
    operation: `runs.cycle.${kind ?? "none"}`,
    object: { cycle: cycle.id },
    initial: { reason: "", prUrl: "", toState: next[0] ?? "", key: "" },
  });
  const latest = useRef(draft.value);
  latest.current = draft.value;
  const [submitted, setSubmitted] = useState(false);
  // A transition's idempotency key is made when the dialog opens and kept in
  // the draft, so it exists before anything is sent and a resend after a lost
  // answer uses the same key (the hub applies one key once).
  const needsKey = kind !== null && kind !== "stop" && !draft.identityPending && !draft.value.key;
  useEffect(() => {
    if (needsKey) draft.setValue((p) => (p.key ? p : { ...p, key: newAttemptId("cycle") }));
  }, [needsKey, draft.setValue]);
  const write = useHubWrite<Record<string, unknown>, { cycle: Cycle }>({
    path: WRITE_PATHS.cycle,
    label: kind === "stop" ? `Stopping cycle ${cycle.id}` : kind === "pause" ? `Pausing cycle ${cycle.id}` : kind === "resume" ? `Resuming cycle ${cycle.id}` : `Moving cycle ${cycle.id} on`,
    invalidates: [runsKeys.cycle(cycle.id), runsKeys.cycles],
    // The body carries the idempotency key, so the identical repeat after
    // approval is the approved call; but the version check may refuse it
    // once the cycle has moved. The person submits again after approval.
    resumable: false,
    successMessage: (d) => `Cycle ${cycle.id} is now: ${CYCLE_STATE_TEXT[d.cycle.state] ?? d.cycle.state}.`,
  });
  const v = draft.value;
  const toState = kind === "advance" ? (next.includes(v.toState) ? v.toState : next[0] ?? "") : kind === "pause" ? "blocked" : kind === "resume" ? "plan" : "";
  const problems: { reason?: string; prUrl?: string } = {};
  if ((kind === "pause" || kind === "stop") && !v.reason.trim()) problems.reason = kind === "stop" ? "Say why it is being stopped. It is kept in the cycle's history." : "Say why it is being paused.";
  if (kind === "advance" && toState === "pr") {
    if (!v.prUrl.trim()) problems.prUrl = "Enter the pull request link or the commit it produced.";
    else if (!looksLikeEvidence(v.prUrl.trim())) problems.prUrl = "Enter a link starting with http:// or https://, or a commit id of 7 to 40 letters and digits.";
  }
  const valid = Object.keys(problems).length === 0;

  const submit = async () => {
    setSubmitted(true);
    if (!valid || !kind) {
      setTimeout(() => document.querySelector<HTMLElement>('dialog[open] [aria-invalid="true"]')?.focus(), 0);
      return;
    }
    // Never post without the cycle (spec §12 defect 7).
    if (!cycle.id) return;
    const sent = latest.current;
    const sentRevision = JSON.stringify(sent);
    let body: Record<string, unknown>;
    if (kind === "stop") {
      body = { operation: "kill", cycle_id: cycle.id, reason: sent.reason.trim() };
    } else {
      const key = sent.key;
      // The key is made on open; without it nothing is sent.
      if (!key) return;
      body = {
        operation: "transition",
        cycle_id: cycle.id,
        to_state: toState,
        idempotency_key: key.slice(0, 128),
        expected_version: cycle.version ?? null,
        ...(sent.reason.trim() ? { reason: sent.reason.trim() } : {}),
        ...(toState === "pr" ? { pr_url: sent.prUrl.trim() } : {}),
      };
    }
    const outcome = await write.run(body);
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      // Clear only the text that was sent; anything typed since stays.
      if (JSON.stringify(latest.current) === sentRevision) draft.clear();
      setSubmitted(false);
      onClose();
    }
  };

  if (!kind) return null;
  const approval = kind !== "stop" && CYCLE_APPROVAL_STATES.has(toState);
  const consequences: Record<ActionKind, string> = {
    advance: `It moves from “${CYCLE_STATE_TEXT[cycle.state] ?? cycle.state}” to “${CYCLE_STATE_TEXT[toState] ?? toState}”.${approval ? " This step needs approval from a different person before it takes effect." : ""}`,
    pause:
      "No new work starts for this cycle. Work already running is not cut off: it stops on its own within its remaining time limit. Its history and what it used are kept, and you can resume it to the planning step later. Pausing needs approval from a different person.",
    resume: "It goes back to the planning step, and work can start again from there. Its history and what it used so far are kept.",
    stop: "This cannot be undone. No new work starts for this cycle, ever, and it cannot be resumed. Work already running is not cut off: it stops on its own within its remaining time limit (at most five minutes). Its history, the limits it used and the task itself are kept; the task stays open for other work.",
  };
  const titles: Record<ActionKind, string> = {
    advance: "Move this cycle to the next step?",
    pause: "Pause this cycle?",
    resume: "Resume this cycle?",
    stop: "Stop this cycle for good?",
  };
  const labels: Record<ActionKind, string> = { advance: "Move it on", pause: "Pause cycle", resume: "Resume cycle", stop: "Stop cycle" };

  return (
    <ConfirmDialog
      open
      title={titles[kind]}
      objectName={`Cycle ${cycle.id}${cycle.task_id != null ? ` for task #${cycle.task_id}` : ""}`}
      consequence={consequences[kind]}
      confirmLabel={labels[kind]}
      variant={kind === "stop" ? "danger" : "default"}
      busy={write.isRunning}
      confirmDisabled={kind !== "stop" && !v.key}
      submitOnEnter={false}
      onCancel={() => {
        write.reset();
        onClose();
      }}
      onConfirm={() => void submit()}
    >
      {kind === "advance" && next.length > 1 ? (
        <RadioGroup
          legend="Next step"
          value={toState}
          onChange={(value) => draft.setValue((p) => ({ ...p, toState: value }))}
          options={next.map((s) => ({ value: s, label: CYCLE_STATE_TEXT[s] ?? s }))}
        />
      ) : null}
      {kind === "advance" && toState === "pr" ? (
        <Field label="Pull request link or commit" required error={submitted ? problems.prUrl : undefined}>
          <Input value={v.prUrl} onChange={(e) => draft.setValue((p) => ({ ...p, prUrl: e.target.value }))} />
        </Field>
      ) : null}
      {kind !== "resume" ? (
        <Field label="Reason" required={kind === "pause" || kind === "stop"} error={submitted ? problems.reason : undefined} hint="Kept in the cycle's history.">
          <Textarea rows={3} value={v.reason} onChange={(e) => draft.setValue((p) => ({ ...p, reason: e.target.value }))} />
        </Field>
      ) : null}
      {write.state.status === "error" ? <WriteFailure message={write.state.message} detail={write.state.detail} /> : null}
      {write.state.status === "pending_approval" ? <ParkedWriteNotice approvalId={write.state.approvalId} label={labels[kind]} /> : null}
    </ConfirmDialog>
  );
}
