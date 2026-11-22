/**
 * Register, authorize and revoke a connector server or package (spec §8,
 * §9.8; hub routes from task #2470: `POST /api/action/connector/register |
 * register-package | authorize | revoke`).
 *
 * Each write parks on its own one-shot approval that a different person
 * decides. The parked write is stored (it rebuilds the identical approval
 * payload; register and register-package are bound to the caller), so the
 * person runs it once approved from this form's notice or from the Inbox.
 *
 * Everything here runs in the owner epoch the form was opened in: attempt
 * markers and drafts live in `epoch.storage`, and nothing a write does after
 * its answer (a message, a draft clear, closing the dialog) reaches whoever
 * is signed in next. Results are kept with the identity and object they were
 * made for and shown only while the form still shows that identity and
 * object. Each form keeps a draft (no field here is a secret), moves focus to
 * the first invalid field, never shows a parked write as success, and says so
 * when the outcome is unknown.
 */
import { useEffect, useRef, useState, type RefObject } from "react";
import { ConfirmDialog, Dialog, Button, ErrorState, Field, Input, RadioGroup, Select } from "../../lib/ui";
import { clearDraft, draftHoldsSnapshot, readDraft, saveDraft, useDraft } from "../lib/drafts";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import { useIdentity } from "../lib/identity";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { getParkedWriteResult, useParkedWriteStore } from "../lib/parkedWrites";
import { useHubWrite, type WriteOutcome } from "../lib/mutations";
import { outcomeKnown, useAttempt } from "./attempts";
import {
  authorizeBody,
  connectorErrorText,
  CONNECTOR_ADMIN_PATHS,
  EMPTY_AUTHORIZE,
  EMPTY_REGISTER,
  INTEGRATION_KEYS,
  registerBody,
  registerPath,
  registerTarget,
  revokeBody,
  validateAuthorize,
  validateRegister,
  type AuthorizeForm,
  type FieldErrors,
  type RegisterForm,
} from "./api";

/**
 * Required fields keep the visible mark and `aria-required`, but not the
 * native `required` attribute: the form's own validation runs, names the
 * problem under the field and moves focus to the first one.
 */
const REQ = { required: false, "aria-required": true } as const;

type Problem = { message: string; detail: string | null; unknown: boolean };

function problemOf(outcome: Extract<WriteOutcome<unknown>, { kind: "error" }>, what: string, objectName: string): Problem {
  if (outcome.outcomeUnknown) {
    return {
      message: `The request was sent, but its answer was lost, so it is not known whether ${what}. Check the lists on this page (they refresh) before trying again.`,
      detail: outcome.detail ?? null,
      unknown: true,
    };
  }
  return {
    message: connectorErrorText(outcome.code, outcome.status, objectName, outcome.message),
    detail: outcome.detail ?? (outcome.code ? `${outcome.code}: ${outcome.message}` : outcome.message),
    unknown: false,
  };
}

/** What a send ended in, for the form that sent it. */
export type SendResult = { kind: "done"; data: unknown } | { kind: "parked"; approvalId: string | null } | { kind: "not_done" };

/** What the form shows about its last send, kept with the attempt key (identity, operation, object) it was made for. */
type Shown = { key: string; problem: Problem | null; parked: string | null | undefined };

/**
 * One non-repeatable write (register, authorize, revoke): the attempt is
 * recorded and read back BEFORE the send, cleared only when the outcome is
 * known (the hub answered, or nothing left), and kept (outcome unknown) when
 * the answer is lost, the request is cancelled after it left, or the person
 * leaves mid-request. While a marker is unsettled nothing more is sent for
 * this identity and object.
 */
function useGuardedWrite(opts: {
  operation: string;
  object: string;
  path: string;
  label: string;
  successMessage: string | ((data: unknown) => string);
  what: string;
  objectName: string;
}) {
  const epoch = useEpoch();
  const identity = useIdentity();
  const who = identity.status === "ready" ? identity.identity : undefined;
  const attempt = useAttempt(who, opts.operation, opts.object);
  const write = useHubWrite<Record<string, unknown>, unknown>({
    path: opts.path,
    label: opts.label,
    invalidates: [INTEGRATION_KEYS.inventory],
    successMessage: opts.successMessage,
    // The identical request rebuilds the identical approval payload, so the
    // parked write is stored and run once approved (docs: "Connectors").
    resumable: true,
  });
  const [shown, setShown] = useState<Shown | null>(null);
  /** The attempt key of the send in flight in this form, if any. */
  const [inFlight, setInFlight] = useState<string | null>(null);

  /**
   * `draft`: the form draft the body is submitted from. A parked write keeps
   * it, so a run from anywhere (the Inbox, with this form closed) clears that
   * draft while it still holds the snapshot (foundation, task #2525).
   */
  async function send(body: Record<string, unknown>, path?: string, draft?: Submitted<unknown> | null): Promise<SendResult> {
    // This send belongs to the epoch, identity and object it started under.
    const started: OwnerEpoch = epoch;
    const key = attempt.key;
    const show = (problem: Problem | null, parked?: string | null) =>
      started.run(() => setShown(key === null ? null : { key, problem, parked }));
    show(null);
    if (key === null) {
      started.run(() =>
        setShown({ key: "", problem: { message: "We are still checking who is signed in, so nothing was sent. Try again in a moment.", detail: null, unknown: false }, parked: undefined }),
      );
      return { kind: "not_done" };
    }
    if (attempt.current) return { kind: "not_done" };
    const recorded = attempt.record(opts.label, body);
    if (!recorded) {
      show({
        message: "This browser could not record the attempt, so nothing was sent. Check that this site may use storage, then try again.",
        detail: null,
        unknown: false,
      });
      return { kind: "not_done" };
    }
    setInFlight(key);
    let outcome: WriteOutcome<unknown>;
    try {
      outcome = await write.run(body, {
        ...(path ? { path } : {}),
        ...(draft ? { draft: { draftKey: draft.key, submittedSnapshot: draft.snapshot } } : {}),
      });
    } finally {
      started.run(() => setInFlight(null));
    }
    // Known outcome: the marker goes. Unknown (lost, or cancelled after it
    // left): it stays, and the form asks the person to check.
    if (outcomeKnown(outcome)) attempt.settle(recorded.key);
    switch (outcome.kind) {
      case "done":
      case "accepted":
        return { kind: "done", data: outcome.data };
      case "pending_approval":
        show(null, outcome.approvalId);
        return { kind: "parked", approvalId: outcome.approvalId };
      case "error":
        show(problemOf(outcome, opts.what, opts.objectName));
        return { kind: "not_done" };
      case "cancelled":
        // It no longer belongs to the signed-in person: show nothing, keep the
        // draft. Sent before it was cancelled: the marker says "unknown".
        return { kind: "not_done" };
    }
  }

  const mine = shown !== null && shown.key === (attempt.key ?? "") ? shown : null;
  // A marker with nothing in flight here: sent earlier (maybe in a page that
  // was left, or by a send cut off), never settled. The person must check.
  const unknown = attempt.current !== null && inFlight !== attempt.key ? attempt.current : null;
  return {
    send,
    running: write.isRunning,
    problem: unknown ? null : (mine?.problem ?? null),
    parked: mine?.parked,
    unknown,
    blocked: unknown !== null || mine?.problem?.unknown === true,
    settleUnknown: () => {
      if (unknown) attempt.settle(unknown.key);
      setShown(null);
    },
  };
}

/** The outcome of an earlier attempt is not known: ask the person to check before anything is sent again. */
function UnknownAttempt({ startedAt, what, onThere, onNotThere }: { startedAt: number; what: string; onThere: () => void; onNotThere: () => void }) {
  return (
    <div className="flex flex-col gap-2">
      <ErrorState
        headingLevel={3}
        title="It may have been done"
        message={`This was sent at ${new Date(startedAt).toLocaleTimeString()}, but its result was never confirmed, so it is not known whether ${what}. Check the connector registry on this page before sending it again.`}
      />
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="secondary" onClick={onThere}>
          It is there
        </Button>
        <Button size="sm" variant="secondary" onClick={onNotThere}>
          It is not there, send it again
        </Button>
      </div>
    </div>
  );
}

/** Focus the first field with an error, in form order. */
function focusFirstInvalid<K extends string>(errors: FieldErrors<K>, order: readonly K[], refs: Partial<Record<K, RefObject<HTMLElement | null>>>) {
  const first = order.find((k) => errors[k]);
  if (first) refs[first]?.current?.focus();
}

function Outcome({ problem, parked, label }: { problem: Problem | null; parked: string | null | undefined; label: string }) {
  if (!problem && parked === undefined) return null;
  return (
    <div className="flex flex-col gap-2">
      {problem && (
        <ErrorState headingLevel={3} title={problem.unknown ? "It may have been done" : "This was not done"} message={problem.message} detail={problem.detail} />
      )}
      {parked !== undefined && <ParkedWriteNotice approvalId={parked} label={label} />}
    </div>
  );
}

// ---------------------------------------------------------------- drafts

/** The draft a send was made from: its storage key and the exact value sent. */
type Submitted<T> = { key: string; snapshot: T };

type DraftHandle<T> = { key: string; value: T; clear: () => void };

/**
 * The form instance a send was made from, while it is mounted. A completion
 * reaches the form (reset its text, close it) only through this, and only
 * while the instance that sent it is still on screen: after Cancel and a
 * reopen, the open form is another instance, with its own text.
 */
type MountedForm<T> = { readonly alive: boolean; readonly draft: DraftHandle<T> };

function useMountedForm<T>(draft: DraftHandle<T>): MountedForm<T> {
  const latest = useRef(draft);
  latest.current = draft;
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [handle] = useState<MountedForm<T>>(() => ({
    get alive() {
      return alive.current;
    },
    get draft() {
      return latest.current;
    },
  }));
  return handle;
}

/**
 * Before the send: store the submitted value under the draft's key now
 * (synchronously, not on the form's save delay), so the stored draft is
 * exactly what was sent. Null while the identity, and so the key, is unknown.
 */
export function rememberSubmitted<T>(epoch: OwnerEpoch, key: string, snapshot: T): Submitted<T> | null {
  if (!key) return null;
  saveDraft(epoch, key, snapshot);
  return { key, snapshot };
}

/**
 * After the write is done, two separate steps, both gated on the snapshot:
 *
 * 1. Storage: read the stored draft back now and delete it only when it still
 *    is the submitted snapshot. Newer text, saved by this form or by another
 *    instance opened since, is never deleted.
 * 2. The form on screen: reset its text only when the instance that sent it
 *    is still mounted, still on the same draft and still shows exactly the
 *    snapshot, and storage holds nothing newer. A completion that belongs to
 *    a closed instance never touches the open one.
 *
 * Returns whether the stored draft was the snapshot (or gone). Nothing
 * happens once `epoch` is revoked.
 */
export function clearSubmitted<T>(epoch: OwnerEpoch, submitted: Submitted<T>, form: MountedForm<T> | null): boolean {
  if (epoch.revoked) return false;
  const stored = readDraft<T>(epoch, submitted.key);
  if (stored !== null && !draftHoldsSnapshot(stored, submitted.snapshot)) return false;
  if (stored !== null) clearDraft(epoch, submitted.key);
  if (form?.alive && form.draft.key === submitted.key && draftHoldsSnapshot(form.draft.value, submitted.snapshot)) {
    // Storage under this key was just checked and holds nothing newer, so
    // the draft's own clear (which also removes the key) only resets the text.
    form.draft.clear();
  }
  return true;
}

/**
 * A parked write from this form, run later while this form is still open
 * (from its notice, or the Inbox in another view): once its result says it
 * was done, reset the open form the same way. The stored draft itself is
 * cleared by the run (`ParkedDraftBinding`), wherever it runs.
 */
function useClearWhenParkedRuns<T>(parked: (Submitted<T> & { approvalId: string }) | null, form: MountedForm<T>) {
  const epoch = useEpoch();
  useParkedWriteStore(); // re-render when the store or its results change
  const result = parked ? getParkedWriteResult(epoch, parked.approvalId) : null;
  const ran = result !== null && (result.kind === "done" || result.kind === "accepted");
  useEffect(() => {
    if (ran && parked) clearSubmitted(epoch, parked, form);
  }, [ran, parked, epoch, form]);
}

const REGISTER_ORDER = ["packageName", "serverId", "connector", "provenance", "scopes", "reviewRef"] as const;

export function RegisterDialog({ onClose, packages }: { onClose: () => void; packages: readonly string[] }) {
  const epoch = useEpoch();
  const identity = useIdentity();
  const draft = useDraft<RegisterForm>({ identity, operation: "connector.register", initial: EMPTY_REGISTER });
  const mounted = useMountedForm(draft);
  const form = draft.value;
  const [errors, setErrors] = useState<FieldErrors<keyof RegisterForm>>({});
  const [parkedFrom, setParkedFrom] = useState<(Submitted<RegisterForm> & { approvalId: string }) | null>(null);
  useClearWhenParkedRuns(parkedFrom, mounted);
  const refs = {
    packageName: useRef<HTMLSelectElement>(null),
    serverId: useRef<HTMLInputElement>(null),
    connector: useRef<HTMLInputElement>(null),
    provenance: useRef<HTMLInputElement>(null),
    scopes: useRef<HTMLInputElement>(null),
    reviewRef: useRef<HTMLInputElement>(null),
  };
  const server = form.kind === "server";
  const name = server ? form.serverId.trim() || "This server" : form.packageName.trim() || "This connector";
  const guarded = useGuardedWrite({
    operation: "connector.register",
    // The target being registered (kind and identifiers): an unknown outcome
    // or a result for one server or package never shows on another.
    object: registerTarget(form),
    path: registerPath(form),
    label: "Register a connector",
    successMessage: "The connector is registered.",
    what: "the connector was registered",
    objectName: name,
  });
  const set = <K extends keyof RegisterForm>(key: K, value: RegisterForm[K]) => draft.setValue((prev) => ({ ...prev, [key]: value }));

  async function submit() {
    const found = validateRegister(form);
    setErrors(found);
    if (Object.keys(found).length > 0) {
      focusFirstInvalid(found, REGISTER_ORDER, refs);
      return;
    }
    // Bound to this epoch and this form instance now: nothing below reaches
    // whoever is signed in next, nor a form opened again since.
    const closeInEpoch = epoch.wrap(onClose);
    const close = () => {
      if (mounted.alive) closeInEpoch();
    };
    // The snapshot that is sent, and whose draft it is, stored before the send.
    const submitted = rememberSubmitted(epoch, draft.key, form);
    const result = await guarded.send(registerBody(form), registerPath(form), submitted);
    if (result.kind === "parked") {
      if (submitted && result.approvalId) {
        epoch.run(() => setParkedFrom({ ...submitted, approvalId: result.approvalId as string }));
      }
      return;
    }
    if (result.kind !== "done") return;
    if (submitted) clearSubmitted(epoch, submitted, mounted);
    close();
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="Register a connector"
      description="Registering records a reviewed connector in the hub's registry, with you as its reviewer. It needs an approval from someone else, and agents still cannot use it until it is authorized."
      size="md"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={guarded.running} disabled={guarded.blocked}>
            Register
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {draft.restored && <p className="m-0 text-sm text-[var(--ua-muted)]">Restored what you typed earlier.</p>}
        <RadioGroup
          legend="What are you registering?"
          name="register-kind"
          value={form.kind}
          onChange={(v) => set("kind", v === "server" ? "server" : "package")}
          options={[
            { value: "package", label: "A packaged connector (shipped with the hub)" },
            { value: "server", label: "A connector server" },
          ]}
        />
        {server ? (
          <>
            <Field label="Server id" required error={errors.serverId} hint="A short unique name, such as github-main.">
              <Input ref={refs.serverId} {...REQ} value={form.serverId} onChange={(e) => set("serverId", e.target.value)} autoComplete="off" />
            </Field>
            <Field label="Connector" required error={errors.connector} hint="The connector this server runs.">
              <Input ref={refs.connector} {...REQ} value={form.connector} onChange={(e) => set("connector", e.target.value)} autoComplete="off" />
            </Field>
          </>
        ) : (
          <>
            <Field label="Package" required error={errors.packageName}>
              <Select
                ref={refs.packageName}
                {...REQ}
                value={form.packageName}
                onChange={(e) => set("packageName", e.target.value)}
                placeholder="Choose a package"
                options={packages.map((p) => ({ value: p, label: p }))}
              />
            </Field>
            <Field label="Server id (optional)" error={errors.serverId} hint="Leave empty to use the package name.">
              <Input ref={refs.serverId} value={form.serverId} onChange={(e) => set("serverId", e.target.value)} autoComplete="off" />
            </Field>
          </>
        )}
        <Field label="Where it comes from" required error={errors.provenance} hint="The source the review covered, such as a repository and version.">
          <Input ref={refs.provenance} {...REQ} value={form.provenance} onChange={(e) => set("provenance", e.target.value)} autoComplete="off" />
        </Field>
        <Field
          label={server ? "Scopes" : "Scopes (optional)"}
          required={server}
          error={errors.scopes}
          hint="What it may do, separated by commas, such as repo:read, issues:write."
        >
          <Input ref={refs.scopes} required={false} aria-required={server} value={form.scopes} onChange={(e) => set("scopes", e.target.value)} autoComplete="off" />
        </Field>
        <Field label="Review reference" required error={errors.reviewRef} hint="The review that approved it: a task, pull request or note.">
          <Input ref={refs.reviewRef} {...REQ} value={form.reviewRef} onChange={(e) => set("reviewRef", e.target.value)} autoComplete="off" />
        </Field>
        {guarded.unknown && (
          <UnknownAttempt startedAt={guarded.unknown.startedAt} what="the connector was registered" onThere={() => { guarded.settleUnknown(); onClose(); }} onNotThere={guarded.settleUnknown} />
        )}
        <Outcome problem={guarded.problem} parked={guarded.parked} label="Register a connector" />
      </div>
    </Dialog>
  );
}

const AUTHORIZE_ORDER = ["serverId", "scope", "taskId", "traceId", "connectionRef"] as const;

export function AuthorizeDialog({ serverId, onClose }: { serverId: string; onClose: () => void }) {
  const epoch = useEpoch();
  const identity = useIdentity();
  const draft = useDraft<AuthorizeForm>({
    identity,
    operation: "connector.authorize",
    object: { server: serverId },
    initial: { ...EMPTY_AUTHORIZE, serverId },
  });
  const mounted = useMountedForm(draft);
  const form = { ...draft.value, serverId };
  const [errors, setErrors] = useState<FieldErrors<keyof AuthorizeForm>>({});
  const [parkedFrom, setParkedFrom] = useState<(Submitted<AuthorizeForm> & { approvalId: string }) | null>(null);
  // A parked send belongs to the server it was made for.
  useClearWhenParkedRuns(parkedFrom && parkedFrom.snapshot.serverId === serverId ? parkedFrom : null, mounted);
  const refs = {
    scope: useRef<HTMLInputElement>(null),
    taskId: useRef<HTMLInputElement>(null),
    traceId: useRef<HTMLInputElement>(null),
    connectionRef: useRef<HTMLInputElement>(null),
  };
  const guarded = useGuardedWrite({
    operation: "connector.authorize",
    object: serverId,
    path: CONNECTOR_ADMIN_PATHS.authorize,
    label: `Authorize ${serverId}`,
    successMessage: `${serverId} is authorized for this use.`,
    what: `${serverId} was authorized`,
    objectName: serverId,
  });
  const set = <K extends keyof AuthorizeForm>(key: K, value: AuthorizeForm[K]) => draft.setValue((prev) => ({ ...prev, [key]: value }));

  async function submit() {
    const found = validateAuthorize(form);
    setErrors(found);
    if (Object.keys(found).length > 0) {
      focusFirstInvalid(found, AUTHORIZE_ORDER, refs);
      return;
    }
    const closeInEpoch = epoch.wrap(onClose);
    const close = () => {
      if (mounted.alive) closeInEpoch();
    };
    const submitted = rememberSubmitted(epoch, draft.key, { ...draft.value, serverId });
    const result = await guarded.send(authorizeBody(form), undefined, submitted);
    if (result.kind === "parked") {
      if (submitted && result.approvalId) {
        epoch.run(() => setParkedFrom({ ...submitted, approvalId: result.approvalId as string }));
      }
      return;
    }
    if (result.kind !== "done") return;
    if (submitted) clearSubmitted(epoch, submitted, mounted);
    close();
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title={`Authorize ${serverId}`}
      description="Authorizing lets agents use this connector for one task and scope. It needs an approval from someone else."
      size="md"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={guarded.running} disabled={guarded.blocked}>
            Authorize
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {draft.restored && <p className="m-0 text-sm text-[var(--ua-muted)]">Restored what you typed earlier.</p>}
        <Field label="Scope" required error={errors.scope} hint="One of the scopes the server was registered with.">
          <Input ref={refs.scope} {...REQ} value={form.scope} onChange={(e) => set("scope", e.target.value)} autoComplete="off" />
        </Field>
        <Field label="Task" required error={errors.taskId} hint="The task number this use belongs to.">
          <Input ref={refs.taskId} {...REQ} inputMode="numeric" value={form.taskId} onChange={(e) => set("taskId", e.target.value)} autoComplete="off" />
        </Field>
        <Field label="Trace id" required error={errors.traceId} hint="Lets the use be followed in the evidence ledger.">
          <Input ref={refs.traceId} {...REQ} value={form.traceId} onChange={(e) => set("traceId", e.target.value)} autoComplete="off" />
        </Field>
        <Field label="Connection reference (optional)" error={errors.connectionRef}>
          <Input ref={refs.connectionRef} value={form.connectionRef} onChange={(e) => set("connectionRef", e.target.value)} autoComplete="off" />
        </Field>
        {guarded.unknown && (
          <UnknownAttempt startedAt={guarded.unknown.startedAt} what={`${serverId} was authorized`} onThere={() => { guarded.settleUnknown(); onClose(); }} onNotThere={guarded.settleUnknown} />
        )}
        <Outcome problem={guarded.problem} parked={guarded.parked} label={`Authorize ${serverId}`} />
      </div>
    </Dialog>
  );
}

/** Revoke answers 200 `{already_revoked: true}` when it was revoked already. */
function revokedText(serverId: string, data: unknown): string {
  const already = Boolean(data && typeof data === "object" && (data as Record<string, unknown>).already_revoked === true);
  return already ? `${serverId} was already revoked; nothing changed.` : `${serverId} is revoked.`;
}

export function RevokeDialog({ serverId, connector, onClose }: { serverId: string; connector: string; onClose: () => void }) {
  const epoch = useEpoch();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const guarded = useGuardedWrite({
    operation: "connector.revoke",
    object: serverId,
    path: CONNECTOR_ADMIN_PATHS.revoke,
    label: `Revoke ${serverId}`,
    successMessage: (data) => revokedText(serverId, data),
    what: `${serverId} was revoked`,
    objectName: serverId,
  });

  async function confirm() {
    const close = epoch.wrap(onClose);
    // A revoke confirmed in a dialog closed since never closes the one open now.
    if ((await guarded.send(revokeBody(serverId))).kind === "done" && alive.current) close();
  }

  return (
    <ConfirmDialog
      open
      variant="danger"
      title={`Revoke ${serverId}?`}
      objectName={`${serverId} (${connector || "connector server"})`}
      consequence={`Agents can no longer use ${serverId}: every authorization for it stops working, and the revocation is permanent. Using ${connector || "this connector"} again needs a new registration and review. It needs an approval from someone else.`}
      confirmLabel={`Revoke ${serverId}`}
      busy={guarded.running}
      confirmDisabled={guarded.blocked}
      onConfirm={() => void confirm()}
      onCancel={onClose}
    >
      {(guarded.unknown || guarded.problem || guarded.parked !== undefined) && (
        <>
          {guarded.unknown && (
            <UnknownAttempt startedAt={guarded.unknown.startedAt} what={`${serverId} was revoked`} onThere={() => { guarded.settleUnknown(); onClose(); }} onNotThere={guarded.settleUnknown} />
          )}
          <Outcome problem={guarded.problem} parked={guarded.parked} label={`Revoke ${serverId}`} />
        </>
      )}
    </ConfirmDialog>
  );
}
