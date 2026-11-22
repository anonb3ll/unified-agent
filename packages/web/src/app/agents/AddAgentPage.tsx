/**
 * `/agents/add` (spec §9.3 Add agent): a three-step form. 1. Where it runs.
 * 2. Harness, label, office seat and profile. 3. Connect: the command to
 * paste on that host, the one-time token, then a live check that turns green
 * when the agent first connects. The check holds one read open on the hub
 * (`?wait_for=connected`, `connectWait.ts`) rather than polling.
 *
 * The token (the bearer `POST /api/action/agent/add` returns once):
 * - lives only in this component's memory (`useBearer`), in a copy-once
 *   dialog that is unmounted when closed, so it leaves the DOM;
 * - is never in a draft (it is not a form field), never in a parked write
 *   (`resumable: false`), never in the query cache (no query reads or sets
 *   it; the write's own state is reset as soon as the answer is read), never
 *   in any storage;
 * - is dropped when the dialog closes, when the page unmounts and the moment
 *   the owner epoch is revoked (sign-out, another person signing in).
 *
 * The form's draft records the attempt before the request leaves, so a
 * result that is never seen (the tab closed, the answer was lost) is
 * explained on the next visit; the draft is cleared on success only when
 * what is on screen still equals what was submitted.
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Button,
  Checkbox,
  CodeBlock,
  Dialog,
  Field,
  Input,
  LinkButton,
  PageHeader,
  RadioGroup,
  Select,
  Skeleton,
  StatePill,
  Textarea,
  UiLink,
} from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import { useIdentity } from "../lib/identity";
import { useHubWrite } from "../lib/mutations";
import { describeReadError } from "../lib/queries";
import {
  AGENTS_KEYS,
  fetchRoster,
  isOperatorOnly,
  WRITE_PATHS,
  type AgentAddBody,
  type AgentAddResult,
  type RosterPayload,
} from "./api";
import { useAgentConnectWait } from "./connectWait";
import { connectSnippet, enrolmentRefusal, HARNESS_OPTIONS, LABEL_PATTERN, WHERE_OPTIONS, type Where } from "./model";
import {
  AgentsTabs,
  Notice,
  recordBeforeSend,
  Section,
  shownFrom,
  settleDraftAt,
  updateDraftAt,
  useLiveDraft,
  WriteFeedback,
  type ShownOutcome,
} from "./shared";

type Attempt = { label: string; at: string };

type AddForm = {
  step: number;
  where: Where | "";
  harness: string;
  label: string;
  office: string;
  profile: string;
  expiresDays: string;
  note: string;
  /** Recorded before the add is sent; cleared once its answer is known. */
  attempt: Attempt | null;
};

const INITIAL: AddForm = { step: 1, where: "", harness: "claude-code", label: "", office: "", profile: "", expiresDays: "", note: "", attempt: null };

/** The fields that make up the submitted request (the draft is cleared against these). */
const REQUEST_FIELDS = ["where", "harness", "label", "office", "profile", "expiresDays", "note"] as const;

type Issued = {
  agent: string;
  token: string;
  hashPrefix: string | null;
  /** The add answer's `issued_at`: only a connection at or after it counts. */
  issuedAt: string | null;
  expiresAt: string | null;
  warnings: string[];
  where: Where;
  harness: string;
};

/**
 * The bearer, held in memory by this component only. `hold` takes it from
 * the add answer inside the epoch the add started in; `drop` forgets it. It
 * is dropped when the epoch is revoked and when the component unmounts.
 */
function useBearer(epoch: OwnerEpoch) {
  const [issued, setIssued] = useState<Issued | null>(null);
  useEffect(() => {
    const drop = () => setIssued(null);
    epoch.signal.addEventListener("abort", drop);
    return () => {
      epoch.signal.removeEventListener("abort", drop);
      setIssued(null);
    };
  }, [epoch]);
  return {
    issued,
    hold: (next: Issued) => epoch.run(() => setIssued(next)),
    drop: () => setIssued(null),
  };
}

function validate(form: AddForm, roster: RosterPayload | undefined): Partial<Record<keyof AddForm, string>> {
  const errors: Partial<Record<keyof AddForm, string>> = {};
  if (!form.where) errors.where = "Choose where the agent runs.";
  if (!form.label.trim()) errors.label = "Enter a label.";
  else if (!LABEL_PATTERN.test(form.label.trim())) errors.label = "Start with a letter; then letters, digits, _ or -, at most 64 characters.";
  else if (roster?.agents.some((a) => a.agent === form.label.trim() && a.added)) errors.label = `${form.label.trim()} is already added. Choose another label.`;
  if (!form.office) errors.office = "Choose the office this agent sits in.";
  if (form.expiresDays.trim()) {
    const n = Number(form.expiresDays);
    if (!Number.isInteger(n) || n < 1 || n > 3650) errors.expiresDays = "A whole number of days from 1 to 3650, or leave it empty for no expiry.";
  }
  if (form.note.length > 500) errors.note = "At most 500 characters.";
  return errors;
}

function bodyOf(form: AddForm, allowPlaintext: boolean): AgentAddBody {
  return {
    label: form.label.trim(),
    office: form.office,
    ...(form.profile.trim() ? { profile: form.profile.trim() } : {}),
    kind: form.where === "cloud" ? "cloud" : "local",
    ...(form.expiresDays.trim() ? { expires_days: Number(form.expiresDays) } : {}),
    ...(form.note.trim() ? { note: form.note.trim() } : {}),
    ...(allowPlaintext ? { allow_plaintext_at_rest: true } : {}),
  };
}

function hubMcpUrl(): string {
  return typeof window === "undefined" ? "/mcp" : `${window.location.origin}/mcp`;
}

export function AddAgentPage() {
  const roster = useQuery({ queryKey: AGENTS_KEYS.roster, queryFn: ({ signal }) => fetchRoster(signal), retry: false });
  return (
    <>
      <PageHeader
        title="Add an agent"
        description="Connect an agent on this machine, another machine or a cloud host. It gets a seat in an office and a token that is shown once."
        tabs={<AgentsTabs />}
      />
      {roster.isPending ? (
        <Skeleton label="Checking who may add agents" />
      ) : roster.isError && isOperatorOnly(roster.error) ? (
        <Section title="Only the operator can add agents" id="add-denied">
          <p className="m-0 text-sm">
            Adding an agent issues a token that lets it act on this hub, so only the human operator may do it. You are signed in as someone else.
          </p>
          <div>
            <Button variant="primary" disabled aria-describedby="add-denied">
              Add the agent
            </Button>
          </div>
        </Section>
      ) : roster.isError ? (
        <RosterReadFailed error={roster.error} retry={() => void roster.refetch()} />
      ) : (
        <AddAgentForm roster={roster.data} />
      )}
    </>
  );
}

function RosterReadFailed({ error, retry }: { error: unknown; retry: () => void }) {
  const { message, detail } = describeReadError(error);
  return (
    <Section title="The add form could not load" id="add-unavailable">
      <p className="m-0 text-sm">{message} The form needs the list of offices and whether the hub can keep tokens encrypted.</p>
      {detail && (
        <details className="text-xs">
          <summary className="cursor-pointer">Technical detail</summary>
          <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{detail}</pre>
        </details>
      )}
      <div>
        <Button onClick={retry}>Retry</Button>
      </div>
    </Section>
  );
}

function AddAgentForm({ roster }: { roster: RosterPayload }) {
  const epoch = useEpoch();
  const identity = useIdentity();
  const draft = useDraft<AddForm>({ identity, operation: "agent.add", initial: INITIAL });
  const live = useLiveDraft(draft);
  const form = draft.value;
  const set = (patch: Partial<AddForm>) => draft.setValue((prev) => ({ ...prev, ...patch }));
  /** The explicit opt-in: never drafted, so it is chosen again on every visit. */
  const [allowPlaintext, setAllowPlaintext] = useState(false);
  const [sealRefused, setSealRefused] = useState(false);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const [showErrors, setShowErrors] = useState(false);
  /** The agent this visit added; its connect check stays after the token is gone. */
  const [added, setAdded] = useState<Omit<Issued, "token"> | null>(null);
  const bearer = useBearer(epoch);
  const write = useHubWrite<AgentAddBody, AgentAddResult>({
    path: WRITE_PATHS.agentAdd,
    // The answer carries the bearer: never stored for a later run. After
    // approval the operator submits the form again and sees the token then.
    resumable: false,
    invalidates: [AGENTS_KEYS.all],
    label: "Adding the agent",
    pendingMessage: false,
    successMessage: (data) => `${data.agent} was added.`,
  });
  const errors = validate(form, roster);
  const stepErrors = (step: number) =>
    step === 1 ? (["where"] as const).filter((k) => errors[k]) : (["label", "office", "expiresDays", "note"] as const).filter((k) => errors[k]);
  const needsOptIn = !roster.sealed_at_rest || sealRefused;
  const officeOptions = roster.offices.map((o) => ({ value: o.id, label: o.name === o.id ? o.name : `${o.name} (${o.id})` }));
  const busy = write.isRunning;

  const goTo = (step: number) => {
    if (step > form.step && stepErrors(form.step).length > 0) {
      setShowErrors(true);
      return;
    }
    setShowErrors(false);
    set({ step });
  };

  const submit = async () => {
    if (busy) return;
    if (Object.keys(errors).length > 0) {
      setShowErrors(true);
      return;
    }
    setShown({ kind: "none" });
    const keyAtSubmit = draft.key;
    const submitted: AddForm = { ...form };
    const body = bodyOf(submitted, allowPlaintext);
    // Recorded before the send: if the answer is never seen, the next visit
    // knows an add for this label may have happened.
    recordBeforeSend(epoch, draft, { ...submitted, attempt: { label: body.label, at: new Date().toISOString() } });
    const outcome = await write.run(body);
    if (outcome.kind === "done") {
      const data = outcome.data;
      // The hook's own state held the answer (and so the bearer): drop it now.
      write.reset();
      bearer.hold({
        agent: data.agent,
        token: data.token,
        hashPrefix: data.token_hash_prefix ?? null,
        issuedAt: data.issued_at ?? null,
        expiresAt: data.expires_at ?? null,
        warnings: Array.isArray(data.warnings) ? data.warnings : [],
        where: (submitted.where || "this") as Where,
        harness: submitted.harness,
      });
      epoch.run(() => {
        setAdded({
          agent: data.agent,
          hashPrefix: data.token_hash_prefix ?? null,
          issuedAt: data.issued_at ?? null,
          expiresAt: data.expires_at ?? null,
          warnings: Array.isArray(data.warnings) ? data.warnings : [],
          where: (submitted.where || "this") as Where,
          harness: submitted.harness,
        });
        setAllowPlaintext(false);
      });
      // Clear the draft only if what is there now (read back from storage,
      // and the form's text while it is still mounted) is what was submitted.
      settleDraftAt(epoch, live.current, keyAtSubmit, submitted, REQUEST_FIELDS, (prev) => ({ ...prev, attempt: null }));
      return;
    }
    if (outcome.kind === "cancelled") {
      // Another person or a sign-out: this page is being replaced. Nothing is
      // shown to the next owner; the attempt stays recorded when it had left.
      if (!outcome.dispatched) updateDraftAt(epoch, live.current, keyAtSubmit, (prev) => ({ ...prev, attempt: null }));
      epoch.run(() => setShown(shownFrom(outcome)));
      return;
    }
    const unknown = outcome.kind === "error" && outcome.outcomeUnknown;
    if (!unknown) {
      // The hub answered (parked, refused or failed): nothing was issued.
      updateDraftAt(epoch, live.current, keyAtSubmit, (prev) => ({ ...prev, attempt: null }));
    }
    if (outcome.kind === "error" && outcome.code === "sealed_store_required") epoch.run(() => setSealRefused(true));
    epoch.run(() => setShown(shownFrom(outcome, (code) => enrolmentRefusal(code, body.label))));
  };

  if (added) {
    return (
      <>
        {bearer.issued && <TokenDialog issued={bearer.issued} onClose={bearer.drop} />}
        <ConnectCheck added={added} tokenOpen={bearer.issued !== null} onAnother={() => setAdded(null)} />
      </>
    );
  }

  const attempt = form.attempt;
  const attemptLanded = attempt ? roster.agents.some((a) => a.agent === attempt.label && a.added) : false;

  return (
    <form
      noValidate
      aria-label="Add an agent"
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (form.step < 3) goTo(form.step + 1);
        else void submit();
      }}
    >
      {attempt && !busy && (
        <Notice role="status" tone="warn">
          <p className="m-0">
            An earlier request to add <strong>{attempt.label}</strong> was sent, but its answer was never seen.
          </p>
          <p className="m-0 mt-1">
            {attemptLanded
              ? `${attempt.label} is in the roster, so it was added. Its token was not shown and cannot be shown again: remove the agent and add it again to issue a new one.`
              : `${attempt.label} is not in the roster as added, so it most likely was not. You can submit again; the hub never issues a second token for a label it already added.`}
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <LinkButton size="sm" href={`/agents/profile/${encodeURIComponent(attempt.label)}`}>
              Open {attempt.label}
            </LinkButton>
            <Button size="sm" variant="ghost" onClick={() => set({ attempt: null })}>
              Dismiss
            </Button>
          </div>
        </Notice>
      )}
      <ol aria-label="Steps" className="m-0 flex list-none flex-wrap gap-4 p-0 text-sm">
        {["Where it runs", "Who it is", "Connect"].map((name, index) => (
          <li key={name} aria-current={form.step === index + 1 ? "step" : undefined} className={form.step === index + 1 ? "font-semibold" : "text-[var(--ua-muted)]"}>
            {index + 1}. {name}
          </li>
        ))}
      </ol>

      {form.step === 1 && (
        <Section title="1. Where does it run?" id="add-step-where">
          <RadioGroup
            legend="Where the agent runs"
            legendHidden
            name="where"
            value={form.where}
            onChange={(where) => set({ where: where as Where })}
            options={WHERE_OPTIONS.map((o) => ({ value: o.value, label: o.label, description: o.description }))}
          />
          {showErrors && errors.where && (
            <p role="alert" className="m-0 text-xs font-medium text-[var(--ua-danger)]">
              {errors.where}
            </p>
          )}
        </Section>
      )}

      {form.step === 2 && (
        <Section title="2. Which harness and label?" id="add-step-who" description="The label is how the agent signs its work. The office seat gives it its role and default permissions.">
          <Field label="Harness" hint="The program the agent runs in. It decides the connect instructions in step 3.">
            <Select value={form.harness} onChange={(e) => set({ harness: e.target.value })} options={[...HARNESS_OPTIONS]} />
          </Field>
          <Field label="Label" required hint="For example Laptop_Agent2. A letter first, then letters, digits, _ or -." error={showErrors ? errors.label : undefined}>
            <Input value={form.label} autoComplete="off" spellCheck={false} maxLength={64} onChange={(e) => set({ label: e.target.value })} />
          </Field>
          <Field label="Office" required hint="The office whose seat this agent takes." error={showErrors ? errors.office : undefined}>
            <Select value={form.office} onChange={(e) => set({ office: e.target.value })} options={officeOptions} placeholder="Choose an office" />
          </Field>
          <Field label="Profile" hint={`What it may do. Leave empty for the label's default (${roster.default_profile}); a profile can lower it, never raise it.`}>
            <Input value={form.profile} autoComplete="off" maxLength={64} placeholder={roster.default_profile} onChange={(e) => set({ profile: e.target.value })} />
          </Field>
          <Field label="Token expires after (days)" hint="Leave empty for a token that does not expire." error={showErrors ? errors.expiresDays : undefined}>
            <Input value={form.expiresDays} inputMode="numeric" onChange={(e) => set({ expiresDays: e.target.value })} />
          </Field>
          <Field label="Note" hint="Recorded with the token, for example which machine it is for." error={showErrors ? errors.note : undefined}>
            <Textarea value={form.note} rows={2} maxLength={500} onChange={(e) => set({ note: e.target.value })} />
          </Field>
        </Section>
      )}

      {form.step === 3 && (
        <Section title="3. Connect" id="add-step-connect" description="Adding issues the agent's token. It is shown once, with the command to paste on the agent's host, and then a check that turns green when the agent first connects.">
          <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-[var(--ua-muted)]">Runs</dt>
            <dd className="m-0">{WHERE_OPTIONS.find((o) => o.value === form.where)?.label ?? "Not chosen"}</dd>
            <dt className="text-[var(--ua-muted)]">Label</dt>
            <dd className="m-0">{form.label.trim() || "Not set"}</dd>
            <dt className="text-[var(--ua-muted)]">Office</dt>
            <dd className="m-0">{officeOptions.find((o) => o.value === form.office)?.label ?? "Not chosen"}</dd>
            <dt className="text-[var(--ua-muted)]">Profile</dt>
            <dd className="m-0">{form.profile.trim() || `${roster.default_profile} (default)`}</dd>
          </dl>
          {needsOptIn && (
            <Notice role="note" tone="warn">
              <p className="m-0">
                <strong>The hub's secret store is not encrypted.</strong> A token added now is kept on the hub protected only by file permissions. The
                hub refuses to issue one unless you accept that.
              </p>
              <Checkbox
                className="mt-2"
                label="Accept a token protected only by file permissions"
                description="Ask for the store to be sealed to avoid this."
                checked={allowPlaintext}
                onChange={(e) => setAllowPlaintext(e.target.checked)}
              />
            </Notice>
          )}
          {showErrors && Object.keys(errors).length > 0 && (
            <p role="alert" className="m-0 text-sm text-[var(--ua-danger)]">
              Some answers need fixing: {Object.values(errors).join(" ")}
            </p>
          )}
          <WriteFeedback
            state={write.state}
            shown={shown}
            what="Adding the agent"
            checkHref="/agents"
            checkLabel="check the roster"
            continuation={{
              label: "Submit the form again",
              description: "Nothing was issued yet. After a different person approves it, submit this form again: the token is shown then, once.",
            }}
          />
        </Section>
      )}

      <div className="flex flex-wrap gap-2">
        {form.step > 1 && (
          <Button type="button" onClick={() => goTo(form.step - 1)} disabled={busy}>
            Back
          </Button>
        )}
        {form.step < 3 ? (
          <Button type="submit" variant="primary">
            Next
          </Button>
        ) : (
          <Button type="submit" variant="primary" loading={busy} disabled={needsOptIn && !allowPlaintext}>
            Add the agent
          </Button>
        )}
      </div>
    </form>
  );
}

/** The copy-once panel. Unmounted when closed, so the token leaves the page. */
function TokenDialog({ issued, onClose }: { issued: Issued; onClose: () => void }) {
  const snippet = connectSnippet({ where: issued.where, harness: issued.harness, label: issued.agent, hubUrl: hubMcpUrl() });
  return (
    <Dialog
      open
      onClose={onClose}
      dismissOnBackdrop={false}
      size="lg"
      title={`${issued.agent}'s token`}
      description="This is the only time the token is shown. Copy it now and keep it where the agent runs. Once you close this, the Control Center cannot show it again."
      footer={
        <Button variant="primary" onClick={onClose}>
          I have copied it
        </Button>
      }
    >
      <div className="flex flex-col gap-3">
        <CodeBlock label="One-time token" code={issued.token} />
        <CodeBlock label="Paste this on the agent's host, with the token in UA_TOKEN" code={snippet} />
        {issued.warnings.length > 0 && (
          <div>
            <p className="m-0 text-sm font-medium">Still to do by hand</p>
            <ul className="m-0 pl-5 text-sm">
              {issued.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Dialog>
  );
}

/** After the token is gone: the connect instructions (no token) and the live check. */
function ConnectCheck({ added, tokenOpen, onAnother }: { added: Omit<Issued, "token">; tokenOpen: boolean; onAnother: () => void }) {
  const check = useAgentConnectWait(added.agent, added.issuedAt);
  const connected = check.status === "connected";
  const snippet = useMemo(
    () => connectSnippet({ where: added.where, harness: added.harness, label: added.agent, hubUrl: hubMcpUrl() }),
    [added],
  );
  return (
    <Section title={`${added.agent} was added`} id="add-connect">
      <p className="m-0 text-sm">
        {tokenOpen
          ? "Copy the token from the dialog before you close it."
          : "Its token was shown once and is no longer available here. If it was lost, remove the agent and add it again."}
        {added.hashPrefix ? ` The hub knows the token by the prefix ${added.hashPrefix}.` : ""}
      </p>
      <CodeBlock label="Paste this on the agent's host, with the token in UA_TOKEN" code={snippet} />
      <div role="status" aria-live="polite" className="flex items-center gap-2 text-sm">
        {connected ? (
          <>
            <StatePill kind="ok" label="Connected" /> {added.agent} has connected to the hub.
          </>
        ) : check.status === "unknown" ? (
          <>
            <StatePill kind="unknown" label="Not known" /> The hub could not be asked whether {added.agent} has connected; this page tries again.
          </>
        ) : (
          <>
            <StatePill kind="live" label="Waiting" /> Waiting for {added.agent} to connect to the hub. This turns green when it does.
          </>
        )}
      </div>
      <div className="flex flex-wrap gap-2">
        <LinkButton href={`/agents/profile/${encodeURIComponent(added.agent)}`}>Open {added.agent}</LinkButton>
        <Button variant="ghost" onClick={onAnother} disabled={tokenOpen}>
          Add another agent
        </Button>
        <UiLink href="/agents" className="self-center text-sm text-[var(--ua-link)] underline">
          Back to the roster
        </UiLink>
      </div>
    </Section>
  );
}
