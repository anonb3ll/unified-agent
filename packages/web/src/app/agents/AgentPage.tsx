/**
 * `/agents/profile/:label` (spec §9.3 Agent page): what the agent is doing
 * now (open sessions, claimed tasks), its seat and what it may do, where it
 * runs (host, harness, the hosts of its sessions), its credentials (present
 * or missing, never a value), recent sessions and activity. Actions: start a
 * session, send a message, pause, resume, stop, retire (confirm) and remove
 * (confirm, operator only, `POST /api/action/agent/remove`).
 *
 * The page component is reused by the router when the label changes, so all
 * local state lives in `AgentPageBody`, keyed by the signed-in identity and
 * the label: opening another agent never shows this one's dialog, reason or
 * write result.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router";
import {
  Button,
  Checkbox,
  ConfirmDialog,
  DataTable,
  DescriptionList,
  Field,
  Input,
  LinkButton,
  PageHeader,
  StatePill,
  Textarea,
  UiLink,
  type Column,
} from "../../lib/ui";
import { summarizePayload, truncate } from "../../lib/sessions/types";
import { useDraft } from "../lib/drafts";
import { useEpoch } from "../lib/epoch";
import { useIdentity } from "../lib/identity";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { messageAgentHref, startSessionHref } from "../lib/urlState";
import {
  AGENTS_KEYS,
  fetchAgentDetail,
  fetchClaimedTasks,
  fetchLifecycle,
  fetchOrg,
  fetchRoster,
  isOperatorOnly,
  WRITE_PATHS,
  type AgentDetail,
  type AgentRemoveBody,
  type AgentRemoveResult,
  type ClaimedTask,
  type SessionRow,
} from "./api";
import { deriveState, enrolmentRefusal, formatWhen, kindFromLabel, lifecycleRefusal, STATE_WORDS, tokenText, type LifecycleAction } from "./model";
import {
  AgentsTabs,
  Notice,
  recordBeforeSend,
  Section,
  shownFrom,
  settleDraftAt,
  updateDraftAt,
  useAgentsLiveUpdates,
  useLiveDraft,
  WriteFeedback,
  type ShownOutcome,
} from "./shared";

export function AgentPage() {
  const { label = "" } = useParams();
  const identity = useIdentity();
  // Keyed by identity and object: a reused route element never carries one
  // agent's dialogs, reasons or results over to another (review rule).
  return <AgentPageBody key={JSON.stringify([identity.identity ?? null, label])} label={label} />;
}

const SESSION_COLUMNS: Column<SessionRow>[] = [
  { id: "session", header: "Session", rowLink: true, cell: (s) => <span className="font-mono text-xs">{s.id}</span> },
  { id: "task", header: "Task", cell: (s) => (s.task_id != null ? `#${s.task_id}${s.task_title ? ` ${s.task_title}` : ""}` : "No task") },
  { id: "host", header: "Host", cell: (s) => hostOf(s) ?? "Not reported" },
  { id: "started", header: "Started", cell: (s) => formatWhen(s.started_at) },
  { id: "status", header: "State", cell: (s) => s.status },
];

const TASK_COLUMNS: Column<ClaimedTask>[] = [
  { id: "task", header: "Task", rowLink: true, cell: (t) => `#${t.id}${t.title ? ` ${t.title}` : ""}` },
  { id: "status", header: "Status", cell: (t) => t.status ?? "Not reported" },
  { id: "priority", header: "Priority", cell: (t) => t.priority ?? "Not set" },
  { id: "project", header: "Project", cell: (t) => t.project ?? "Not set" },
];

function hostOf(session: SessionRow): string | null {
  const node = session.meta?.node;
  return session.host || (typeof node === "string" && node ? node : null);
}

const OPEN = new Set(["starting", "running", "waiting"]);

function AgentPageBody({ label }: { label: string }) {
  useAgentsLiveUpdates();
  const detail = useQuery({ queryKey: AGENTS_KEYS.detail(label), queryFn: ({ signal }) => fetchAgentDetail(label, signal) });
  const tasks = useQuery({ queryKey: AGENTS_KEYS.tasks(label), queryFn: ({ signal }) => fetchClaimedTasks(label, signal) });
  const roster = useQuery({ queryKey: AGENTS_KEYS.roster, queryFn: ({ signal }) => fetchRoster(signal), retry: false });
  const lifecycle = useQuery({ queryKey: AGENTS_KEYS.lifecycle, queryFn: ({ signal }) => fetchLifecycle(signal) });
  const org = useQuery({ queryKey: AGENTS_KEYS.org, queryFn: ({ signal }) => fetchOrg(signal) });

  const seat = roster.data?.agents.find((a) => a.agent === label) ?? null;
  const life = useMemo(() => {
    const rows = [...(lifecycle.data?.registered ?? []), ...(lifecycle.data?.agents ?? [])];
    return rows.filter((r) => r.agent === label).pop() ?? null;
  }, [lifecycle.data, label]);
  const office = org.data?.offices.find((o) => o.members.some((m) => m.label === label)) ?? null;
  const member = office?.members.find((m) => m.label === label) ?? null;
  const agent = detail.data?.agent ?? null;
  const sessions = detail.data?.sessions ?? [];
  const openSessions = sessions.filter((s) => OPEN.has(s.status));
  const state = deriveState({
    lifecycle: life?.state,
    rosterState: seat?.state,
    heartbeat: agent?.status ?? null,
    openSessions: openSessions.length,
    activeTasks: tasks.data?.tasks.length ?? 0,
  });
  const known = detail.data != null || seat !== null || member !== null;
  const loading = detail.isPending || roster.isPending || org.isPending;
  const hosts = [...new Set([agent?.host, ...sessions.map(hostOf)].filter((h): h is string => Boolean(h)))];
  const operatorOnly = roster.isError && isOperatorOnly(roster.error);

  return (
    <>
      <PageHeader
        title={label}
        meta={
          known || !loading ? (
            <StatePill kind={STATE_WORDS[state].kind} label={STATE_WORDS[state].label} title={STATE_WORDS[state].meaning} />
          ) : undefined
        }
        description={
          agent?.display_name && agent.display_name !== label
            ? `${agent.display_name}. What this agent is doing, what it may do and where it runs.`
            : "What this agent is doing, what it may do and where it runs."
        }
        tabs={<AgentsTabs />}
        actions={
          <>
            <LinkButton href={startSessionHref(label)}>Start a session</LinkButton>
            <LinkButton href={messageAgentHref(label)}>Send a message</LinkButton>
          </>
        }
      />
      <div className="flex flex-col gap-4">
        {!loading && !known && !detail.isError && (
          <Notice role="status" tone="warn">
            The hub does not know an agent called {label}: it has no seat, no token and has never connected.{" "}
            <UiLink href="/agents" className="text-[var(--ua-link)] underline">
              Back to the roster
            </UiLink>
          </Notice>
        )}

        <Section title="What it is doing now" id="agent-now">
          <QueryRegion
            query={detail}
            loadingLabel="Loading sessions"
            errorTitle="Sessions could not be loaded"
            headingLevel={3}
            isEmpty={(d: AgentDetail | null) => !d || d.sessions.filter((s) => OPEN.has(s.status)).length === 0}
            empty={{ title: "No open session", description: `${label} is not running a session now.` }}
          >
            {() => (
              <DataTable
                columns={SESSION_COLUMNS}
                data={openSessions}
                getRowKey={(s) => s.id}
                getRowHref={(s) => `/sessions/${encodeURIComponent(s.id)}`}
                caption={`${label}'s open sessions`}
                captionSrOnly
                scrollLabel="Open sessions"
              />
            )}
          </QueryRegion>
          <h3 className="m-0 text-sm font-semibold">Claimed tasks</h3>
          <QueryRegion
            query={tasks}
            loadingLabel="Loading claimed tasks"
            errorTitle="Claimed tasks could not be loaded"
            headingLevel={4}
            isEmpty={(d) => d.tasks.length === 0}
            empty={{ title: "No claimed tasks", description: `${label} holds no task claims.` }}
          >
            {(d) => (
              <DataTable
                columns={TASK_COLUMNS}
                data={d.tasks}
                getRowKey={(t) => String(t.id)}
                getRowHref={(t) => `/work/tasks/${encodeURIComponent(String(t.id))}`}
                caption={`Tasks ${label} has claimed`}
                captionSrOnly
                scrollLabel="Claimed tasks"
              />
            )}
          </QueryRegion>
        </Section>

        <Section title="Seat and what it may do" id="agent-seat">
          <DescriptionList
            columns={2}
            items={[
              { id: "office", term: "Office seat", value: seat?.office ?? office?.name ?? (org.isPending ? "Loading" : "No seat") },
              { id: "lead", term: "Office lead", value: office ? (office.lead === label ? "This agent" : office.lead) : "Not in an office" },
              { id: "profile", term: "Profile", value: seat?.profile ?? member?.profile ?? "Not known" },
              {
                id: "rights",
                term: "Decision rights",
                value: office && office.decision_rights.length > 0 ? office.decision_rights.join(", ") : "None recorded",
              },
              {
                id: "lifecycle",
                term: "Lifecycle",
                value: life ? `${life.state}${life.reason ? `: ${life.reason}` : ""}${life.actor ? ` (by ${life.actor})` : ""}` : "Active",
              },
              { id: "owner", term: "Owner of record", value: life?.owner_of_record ?? "Not recorded" },
            ]}
          />
        </Section>

        <Section title="Where it runs" id="agent-where">
          <DescriptionList
            columns={2}
            items={[
              { id: "kind", term: "Runs", value: seat?.kind === "cloud" || (!seat && kindFromLabel(label) === "cloud") ? "Cloud" : "Local" },
              { id: "harness", term: "Harness", value: agent?.kind ?? "Not reported" },
              { id: "host", term: "Host", value: agent?.host ?? "Not reported" },
              { id: "hosts", term: "Hosts of its sessions", value: hosts.length > 0 ? hosts.join(", ") : "None reported" },
              { id: "heartbeat", term: "Heartbeat", value: agent ? `${agent.status}, last ${formatWhen(agent.last_seen)}` : "Never connected" },
              { id: "version", term: "Version", value: agent?.version ?? "Not reported" },
            ]}
          />
          <p className="m-0 text-sm">
            <UiLink href="/agents/hosts" className="text-[var(--ua-link)] underline">
              Open hosts and devices
            </UiLink>
          </p>
        </Section>

        <Section title="Credentials" id="agent-credentials" description="Whether it has a token, never the token itself.">
          <p className="m-0 text-sm">{roster.isPending ? "Loading" : tokenText(seat ? { source: seat.token_source, status: seat.token_status, issuedAt: seat.token_issued_at, expiresAt: seat.token_expires_at } : operatorOnly ? null : { source: null, status: null, issuedAt: null, expiresAt: null })}</p>
        </Section>

        <Section title="Recent sessions" id="agent-sessions">
          <QueryRegion
            query={detail}
            loadingLabel="Loading recent sessions"
            errorTitle="Recent sessions could not be loaded"
            headingLevel={3}
            isEmpty={(d: AgentDetail | null) => !d || d.sessions.length === 0}
            empty={{ title: "No sessions yet", description: "Sessions appear here once the agent starts work." }}
          >
            {(d: AgentDetail | null) => (
              <DataTable
                columns={SESSION_COLUMNS}
                data={d?.sessions ?? []}
                getRowKey={(s) => s.id}
                getRowHref={(s) => `/sessions/${encodeURIComponent(s.id)}`}
                caption={`${label}'s recent sessions`}
                captionSrOnly
                scrollLabel="Recent sessions"
              />
            )}
          </QueryRegion>
        </Section>

        <Section title="Recent activity" id="agent-activity">
          <QueryRegion
            query={detail}
            loadingLabel="Loading activity"
            errorTitle="Activity could not be loaded"
            headingLevel={3}
            isEmpty={(d: AgentDetail | null) => !d || d.activity.length === 0}
            empty={{ title: "No recent activity", description: "Events this agent records appear here." }}
          >
            {(d: AgentDetail | null) => (
              <ol aria-label="Recent activity, newest first" className="m-0 list-none p-0 text-sm">
                {(d?.activity ?? []).slice(0, 50).map((event) => (
                  <li key={event.ulid} className="flex flex-wrap gap-x-2 border-t border-[var(--ua-border)] py-1.5 first:border-t-0">
                    <span className="font-mono text-xs text-[var(--ua-muted)]">{formatWhen(event.created_at)}</span>
                    <span className="text-xs">{event.channel}</span>
                    <span className="min-w-0 grow break-words">{truncate(summarizePayload(event.payload), 160)}</span>
                  </li>
                ))}
              </ol>
            )}
          </QueryRegion>
        </Section>

        <LifecycleSection
          label={label}
          state={life?.state ?? seat?.state ?? "active"}
          current={life ? { state: life.state, actor: life.actor } : null}
          operators={lifecycle.isSuccess ? lifecycle.data.operators : null}
        />
        <RemoveSection label={label} operatorOnly={operatorOnly} rosterReady={roster.isSuccess} retired={state === "retired"} />
      </div>
    </>
  );
}

// --- Lifecycle ---------------------------------------------------------------

const LIFECYCLE_TEXT: Record<LifecycleAction, { button: string; title: (l: string) => string; consequence: string; reason: boolean; danger: boolean }> = {
  pause: { button: "Pause", title: (l) => `Pause ${l}?`, consequence: "It takes no new work and its workers are asked to pause.", reason: true, danger: false },
  resume: { button: "Resume", title: (l) => `Resume ${l}?`, consequence: "It takes work again.", reason: false, danger: false },
  terminate: {
    button: "Stop",
    title: (l) => `Stop ${l}?`,
    consequence: "Its active claims are released and its workers are asked to stop. Some execution paths may need a separate stop.",
    reason: true,
    danger: true,
  },
  retire: {
    button: "Retire",
    title: (l) => `Retire ${l}?`,
    consequence: "Its tokens and session credentials are revoked and the hub refuses it until an operator unretires it. Refused while it holds claims. Operator only.",
    reason: true,
    danger: true,
  },
  unretire: {
    button: "Unretire",
    title: (l) => `Unretire ${l}?`,
    consequence: "The retirement is cleared. Revoked credentials stay revoked; issue new ones separately. Operator only.",
    reason: false,
    danger: false,
  },
};

function LifecycleSection({
  label,
  state,
  current,
  operators,
}: {
  label: string;
  state: string;
  current: { state: string; actor: string | null } | null;
  /** From the lifecycle read; null while it is loading or failed. */
  operators: readonly string[] | null;
}) {
  const epoch = useEpoch();
  const identity = useIdentity();
  const [action, setAction] = useState<LifecycleAction | null>(null);
  const [reason, setReason] = useState("");
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const write = useHubWrite<{ agent: string; action: LifecycleAction; reason: string }, { stops?: { path: string; ref: string; control: string }[] }>({
    path: WRITE_PATHS.lifecycle,
    resumable: false,
    invalidates: [AGENTS_KEYS.all],
    label: "The lifecycle change",
    successMessage: (data) => (data.stops && data.stops.length > 0 ? `Done. ${data.stops.map((s) => `${s.path} ${s.ref}: ${s.control}`).join("; ")}` : "Done."),
  });
  const actions: LifecycleAction[] =
    state === "retired" ? ["unretire"] : state === "paused" || state === "terminated" ? ["resume", "terminate", "retire"] : ["pause", "terminate", "retire"];
  const refusal = (a: LifecycleAction) =>
    lifecycleRefusal(a, { identity: identity.identity, label, operators, current: state === "active" ? null : current });
  const reasons = [...new Set(actions.map(refusal).filter((r): r is string => r !== null))];
  const text = action ? LIFECYCLE_TEXT[action] : null;
  // Shown inside the open dialog (the page behind a modal is inert), else on the page.
  const feedback = <WriteFeedback state={write.state} shown={shown} what="The lifecycle change" checkHref={`/agents/profile/${encodeURIComponent(label)}`} checkLabel="reload this page" />;
  const confirm = async () => {
    if (!action || write.isRunning || refusal(action) !== null) return;
    setShown({ kind: "none" });
    const submittedReason = reason;
    const outcome = await write.run({ agent: label, action, reason: submittedReason.trim() });
    epoch.run(() => {
      setShown(shownFrom(outcome));
      if (outcome.kind === "done" || outcome.kind === "pending_approval") {
        setAction(null);
        // Cleared against the submitted text, never the latest.
        setReason((now) => (now === submittedReason ? "" : now));
      }
    });
  };
  return (
    <Section title="Pause, stop or retire" id="agent-lifecycle" description="Who may do each: an operator, any of them; the agent itself, pause, stop and resume from its own pause.">
      <div className="flex flex-wrap gap-2">
        {actions.map((a) => (
          <Button key={a} onClick={() => setAction(a)} disabled={write.isRunning || refusal(a) !== null} aria-describedby={refusal(a) ? "agent-lifecycle-why" : undefined}>
            {LIFECYCLE_TEXT[a].button}
          </Button>
        ))}
      </div>
      {reasons.length > 0 && (
        <p id="agent-lifecycle-why" className="m-0 text-sm text-[var(--ua-muted)]">
          {reasons.join(" ")}
        </p>
      )}
      {!action && feedback}
      {text && action && (
        <ConfirmDialog
          open
          title={text.title(label)}
          objectName={label}
          consequence={text.consequence}
          confirmLabel={text.button}
          variant={text.danger ? "danger" : "default"}
          busy={write.isRunning}
          confirmDisabled={text.reason && !reason.trim()}
          onCancel={() => {
            setAction(null);
            setReason("");
          }}
          onConfirm={() => void confirm()}
        >
          {text.reason && (
            <Field label="Reason" required hint="Recorded with the change.">
              <Input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
            </Field>
          )}
          {feedback}
        </ConfirmDialog>
      )}
    </Section>
  );
}

// --- Remove ------------------------------------------------------------------

type RemoveForm = { reason: string; forceRelease: boolean; attempt: { at: string } | null };
const REMOVE_INITIAL: RemoveForm = { reason: "", forceRelease: false, attempt: null };
const REMOVE_FIELDS = ["reason", "forceRelease"] as const;

function RemoveSection({ label, operatorOnly, rosterReady, retired }: { label: string; operatorOnly: boolean; rosterReady: boolean; retired: boolean }) {
  const epoch = useEpoch();
  const identity = useIdentity();
  const draft = useDraft<RemoveForm>({ identity, operation: "agent.remove", object: { label }, initial: REMOVE_INITIAL });
  const live = useLiveDraft(draft);
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const [result, setResult] = useState<AgentRemoveResult | null>(null);
  const write = useHubWrite<AgentRemoveBody, AgentRemoveResult>({
    path: WRITE_PATHS.agentRemove,
    invalidates: [AGENTS_KEYS.all],
    label: `Removing ${label}`,
    pendingMessage: false,
    successMessage: (data) => `${data.agent} was removed.`,
  });
  const form = draft.value;
  const set = (patch: Partial<RemoveForm>) => draft.setValue((prev) => ({ ...prev, ...patch }));
  // Shown inside the open dialog (the page behind a modal is inert), else on the page.
  const feedback = <WriteFeedback state={write.state} shown={shown} what={`Removing ${label}`} checkHref={`/agents/profile/${encodeURIComponent(label)}`} checkLabel="reload this page" />;

  const remove = async () => {
    if (write.isRunning || !form.reason.trim()) return;
    setShown({ kind: "none" });
    const keyAtSubmit = draft.key;
    const submitted: RemoveForm = { ...form };
    recordBeforeSend(epoch, draft, { ...submitted, attempt: { at: new Date().toISOString() } });
    const outcome = await write.run({ label, reason: submitted.reason.trim(), ...(submitted.forceRelease ? { force_release: true } : {}) });
    if (outcome.kind === "done") {
      epoch.run(() => {
        setResult(outcome.data);
        setOpen(false);
      });
      settleDraftAt(epoch, live.current, keyAtSubmit, submitted, REMOVE_FIELDS, (prev) => ({ ...prev, attempt: null }));
      return;
    }
    const unknown = (outcome.kind === "error" && outcome.outcomeUnknown) || (outcome.kind === "cancelled" && outcome.dispatched);
    if (!unknown) updateDraftAt(epoch, live.current, keyAtSubmit, (prev) => ({ ...prev, attempt: null }));
    epoch.run(() => {
      setShown(shownFrom(outcome, (code) => enrolmentRefusal(code, label)));
      if (outcome.kind === "pending_approval") setOpen(false);
    });
  };

  return (
    <Section title="Remove this agent" id="agent-remove" description="Retires the label, revokes its tokens, session credentials and device sign-ins, and removes its seat. Its history is kept.">
      {form.attempt && !write.isRunning && !result && (
        <Notice role="status" tone="warn">
          An earlier request to remove {label} was sent, but its answer was never seen. Check its state above before trying again.
        </Notice>
      )}
      {result ? (
        <div role="status" className="flex flex-col gap-1 text-sm">
          <p className="m-0">
            {result.changed ? `${result.agent} was removed.` : `${result.agent} was already removed; nothing changed.`} {result.revoked_tokens ?? 0} tokens and{" "}
            {result.revoked_session_credentials ?? 0} session credentials revoked
            {result.released && result.released.length > 0 ? `; released tasks ${result.released.map((r) => `#${r.task_id}`).join(", ")}` : ""}.
          </p>
          {(result.warnings ?? []).map((w) => (
            <p key={w} className="m-0 text-[var(--ua-warn)]">
              {w}
            </p>
          ))}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="danger" onClick={() => setOpen(true)} disabled={operatorOnly || !rosterReady || retired || write.isRunning} aria-describedby={operatorOnly ? "agent-remove-why" : undefined}>
            Remove {label}
          </Button>
          {operatorOnly && (
            <span id="agent-remove-why" className="text-sm text-[var(--ua-muted)]">
              Only the human operator can remove agents.
            </span>
          )}
          {retired && !operatorOnly && <span className="text-sm text-[var(--ua-muted)]">{label} is already retired.</span>}
        </div>
      )}
      {!open && feedback}
      {open && (
        <ConfirmDialog
          open
          title={`Remove ${label}?`}
          objectName={label}
          consequence="Its tokens, session credentials and device sign-ins stop working at once, and it loses its seat. This cannot be undone from here."
          confirmLabel="Remove"
          variant="danger"
          busy={write.isRunning}
          confirmDisabled={!form.reason.trim()}
          onCancel={() => setOpen(false)}
          onConfirm={() => void remove()}
        >
          <Field label="Reason" required hint="Recorded on the retirement and on each released claim.">
            <Textarea value={form.reason} rows={2} maxLength={2000} onChange={(e) => set({ reason: e.target.value })} />
          </Field>
          <Checkbox
            label="Release its claimed tasks"
            description="Without this, the hub refuses while it holds claimed tasks."
            checked={form.forceRelease}
            onChange={(e) => set({ forceRelease: e.target.checked })}
          />
          {feedback}
        </ConfirmDialog>
      )}
    </Section>
  );
}
