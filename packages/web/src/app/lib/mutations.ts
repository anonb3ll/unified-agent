import { useCallback, useRef, useState } from "react";
import type { QueryKey } from "@tanstack/react-query";
import type { WhoamiPayload } from "../../lib/api/auth";
import { authGeneration, bumpAuthGeneration, isCurrentGeneration } from "./authGeneration";
import { useEpoch, type CancelReason, type OwnerEpoch } from "./epoch";
import { addParkedWrite, type ParkedDraftBinding } from "./parkedWrites";
import { contextFromWhoami, replayVerdict, sameContext, sameSession, type ReplayBlockedReason, type WriteContext } from "./writeContext";

/**
 * Why a write was cancelled by the client:
 * - `signed_out`: the sign-in that started it ended (sign-out, a 401,
 *   another tab's sign-in or sign-out: its epoch was revoked);
 * - `owner_changed`: a different, verified person is signed in now;
 * - `unverified`: who is signed in could not be confirmed.
 */
export type { CancelReason } from "./epoch";

/** The result of one write, as the UI must present it (spec §6). */
export type WriteOutcome<T> =
  | { kind: "done"; data: T }
  /**
   * The client stopped the write because it no longer belongs to the
   * signed-in person. Show nothing, do not retry, do not clear the draft.
   * `dispatched: true` means the request had already left: the change may
   * have been made.
   */
  | { kind: "cancelled"; reason: CancelReason; dispatched: boolean; message: string }
  /** 202 for work that has started (a dispatch), not finished. */
  | { kind: "accepted"; data: T }
  | {
      kind: "pending_approval";
      approvalId: string | null;
      tool?: string;
      /**
       * Hub origin, organization and identity of the whoami read the parked
       * request was actually sent with (after a CSRF retry, the retry's read).
       * This, never a cache read afterwards, is what a stored write records.
       */
      sentContext?: WriteContext | null;
      /** The hub's parsed 202 answer (for example a parked work product's `work_product_id`). */
      body?: unknown;
      /**
       * Set by `useHubWrite`. `willReplay` is always false: nothing is re-sent
       * without a person's click (spec §6). `canRunFromStore` says whether the
       * write was stored and can be run with "Run it now" once approved;
       * otherwise `replayBlockedReason` says why and the form must be
       * submitted again. Absent from a bare `executeHubWrite`.
       */
      willReplay?: false;
      canRunFromStore?: boolean;
      replayBlockedReason?: ReplayBlockedReason | null;
      /** The stored write's id, when one was stored (for `ParkedWriteNotice` and `runParkedWrite`). */
      parkedWriteId?: string;
    }
  | {
      kind: "error";
      status: number | null;
      message: string;
      detail?: string;
      capability?: string;
      /**
       * The hub's machine error code from the error body (for example
       * "in_flight", "conflict", "idempotency_conflict"), when it is a
       * non-empty string. Set on every HTTP error status; branch on it
       * rather than on `message`, which is for people.
       */
      code?: string;
      /** Set when `expectedContext` did not match the signed-in session: nothing was sent. */
      contextMismatch?: boolean;
      /**
       * Set when `expectedContext` was given but who is signed in could not be
       * read (whoami unavailable or unreadable, or naming no identity or no
       * organization where the write is bound to one): nothing was sent, and
       * nothing is known to differ.
       */
      contextUnverified?: boolean;
      /** Set when `signal` was aborted before the request went out: nothing was sent. */
      aborted?: boolean;

      /**
       * Set when the request was sent but its result is not known: the
       * response was lost, cut off or unreadable. The change may or may not
       * have been made; the UI must say so and not offer a blind retry.
       */
      outcomeUnknown?: boolean;
    };

export type HubWriteMethod = "POST" | "PUT" | "DELETE";

export type HubWriteRequest = {
  path: string;
  method?: HubWriteMethod;
  /** JSON body; omitted from the request when undefined. */
  body?: unknown;
  /** Sent as `Idempotency-Key` where the hub route honours one. */
  idempotencyKey?: string;
  /**
   * The hub origin, organization and identity the request must be sent
   * under. Checked against the fresh whoami before every send, including
   * the CSRF retry; on a mismatch nothing is sent (approval replay).
   */
  expectedContext?: WriteContext;
  signal?: AbortSignal;
  /** Called just before the request is handed to fetch (from then on, its outcome may be unknown). */
  onDispatch?: () => void;
  /**
   * Checked after each whoami read, as the last check right before each send
   * (also before the CSRF retry): returning false cancels with nothing sent;
   * returning true means the request leaves now. Callers tie it to
   * their auth generation, so a whoami answer issued before a sign-out in
   * this tab is never used to send.
   */
  stillValid?: () => boolean;
  /**
   * The session an ordinary write belongs to (the identity that started it).
   * Every whoami read made for a send, including the CSRF retry's, must be
   * the same session (`sameSession`); otherwise nothing is sent.
   */
  sessionContext?: WriteContext;
};

type JsonObject = Record<string, unknown>;

function asObject(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : null;
}

/**
 * Exact mirror of the hub's `routes._is_parked_approval`: the body names an
 * approval, says the call is pending, or carries an `autonomy_review` pause.
 * On its own this is not a verdict; use `isParkedResponse`, which also
 * needs the status and the code the hub's `_json` adds.
 */
export function isParkedApproval(body: unknown): boolean {
  const obj = asObject(body);
  if (!obj) return false;
  if ("approval_id" in obj || "global_approval_id" in obj) return true;
  if (obj.global_approval_status === "pending" || obj.pending === true) return true;
  return asObject(obj.autonomy_review) !== null;
}

/**
 * Whether a response is a call parked behind a human approval, the way the
 * hub marks one: `routes._json` adds `code: "approval_required"` to every
 * 202 that `_is_parked_approval` accepts. Anything else is a result:
 * - a 200 that carries `approval_id` (a released work product) is done;
 * - `/api/approvals/request` answers 202 with the markers but without the
 *   code (`approval_code=False`), because creating the request succeeded;
 * - a dispatch answers 202 with `status: "pending"` for accepted work.
 */
export function isParkedResponse(status: number, body: unknown): boolean {
  const obj = asObject(body);
  return status === 202 && obj !== null && obj.code === "approval_required" && isParkedApproval(obj);
}

function approvalIdOf(body: JsonObject): string | null {
  const id = body.approval_id ?? body.global_approval_id;
  return id === null || id === undefined || id === "" ? null : String(id);
}

/** CSRF token from GET /api/whoami; `status` reports why there is none. */
async function readCsrf(
  signal?: AbortSignal,
): Promise<{ token: string | null; status: number | null; whoami: WhoamiPayload | null }> {
  try {
    const response = await fetch("/api/whoami", {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) return { token: null, status: response.status, whoami: null };
    let me: WhoamiPayload & { csrf_token?: string | null };
    try {
      me = (await response.json()) as WhoamiPayload & { csrf_token?: string | null };
    } catch {
      // Unreadable (a proxy page, a cut-off body): who is signed in is unknown.
      return { token: null, status: response.status, whoami: null };
    }
    if (!me || typeof me !== "object") return { token: null, status: response.status, whoami: null };
    return { token: me.csrf || me.csrf_token || null, status: response.status, whoami: me };
  } catch {
    return { token: null, status: null, whoami: null };
  }
}

/**
 * The response body as JSON. Rejects if the body cannot be read (cut off,
 * aborted), and, for a successful response, if a non-empty body is not
 * valid JSON (truncated): a success we cannot read is not a success. An
 * error response keeps its text (`{ raw }`) for the technical detail.
 */
async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text) as unknown;
  } catch {
    if (response.ok) throw new Error(`unreadable ${response.status} response body: ${text.slice(0, 120)}`);
    return { raw: text };
  }
}

function errorText(body: unknown): string | undefined {
  const obj = asObject(body);
  if (!obj) return undefined;
  for (const key of ["error", "detail", "message", "raw"]) {
    if (typeof obj[key] === "string" && obj[key]) return (obj[key] as string).slice(0, 500);
  }
  return undefined;
}

/** The hub's machine error code (`code`), when it is a non-empty string. */
function codeOf(body: unknown): string | undefined {
  const obj = asObject(body);
  return obj && typeof obj.code === "string" && obj.code ? obj.code : undefined;
}

function capabilityOf(body: unknown): string | undefined {
  const obj = asObject(body);
  if (!obj) return undefined;
  for (const key of ["capability", "required_capability", "missing_capability", "tool"]) {
    if (typeof obj[key] === "string" && obj[key]) return obj[key] as string;
  }
  return undefined;
}

const OUTCOME_UNKNOWN = {
  kind: "error" as const,
  status: null,
  outcomeUnknown: true,
  message:
    "The request was sent, but its result could not be confirmed, so it is not known whether the change was made. Check before trying again.",
};

const ABORTED = {
  kind: "error" as const,
  status: null,
  aborted: true,
  message: "This was cancelled before it was sent.",
};

/** User-facing text for each cancellation reason. */
export const CANCEL_MESSAGES: Record<CancelReason, string> = {
  signed_out: "Cancelled: you signed out.",
  owner_changed: "Cancelled: a different person is signed in now.",
  unverified: "We could not confirm who is signed in, so this was not sent. Try again.",
};

const CONTEXT_UNVERIFIED = {
  kind: "error" as const,
  status: null,
  contextUnverified: true,
  message: "We could not check who is signed in, so this was not run.",
};

const CONTEXT_MISMATCH = {
  kind: "error" as const,
  status: null,
  contextMismatch: true,
  message:
    "This was not sent: the hub, organization or signed-in identity differs from the one the change was made under.",
};

/**
 * One write to the hub, classified for the UI. Never throws: every failure
 * comes back as an `error` outcome with a plain message, and the raw
 * transport text in `detail`.
 */
export async function executeHubWrite<T = unknown>(request: HubWriteRequest): Promise<WriteOutcome<T>> {
  const method = request.method ?? "POST";
  const csrf = await readCsrf(request.signal);
  if (csrf.status === 401) {
    return { kind: "error", status: 401, message: "Your session has ended. Sign in again to continue." };
  }
  // Replay guard: the credentials this request will carry must belong to the
  // same hub, organization and identity the write was made under.
  // "unverified": the read failed, or was readable but incomplete (it names
  // nobody, or omits an organization the write is bound to), so nothing is
  // known to differ.
  // "mismatch": a confirmed different hub origin, organization or identity.
  // The session of the first read: a CSRF retry must be made by the same one,
  // or a body started by A could go out with B's token.
  const firstSession = csrf.whoami ? contextFromWhoami(csrf.whoami) : null;
  const contextCheck = (fresh: { whoami: WhoamiPayload | null }, retry: boolean): null | "unverified" | "mismatch" => {
    const bound = request.expectedContext !== undefined || request.sessionContext !== undefined || retry;
    if (!bound) return null;
    if (fresh.whoami === null) return "unverified";
    const now = contextFromWhoami(fresh.whoami);
    const boundTo = [request.expectedContext, request.sessionContext, retry ? firstSession : null];
    if (!now.identity && boundTo.some((context) => context?.identity)) return "unverified";
    if (!now.organization && boundTo.some((context) => context?.organization)) return "unverified";
    if (request.expectedContext !== undefined && !sameContext(request.expectedContext, now)) return "mismatch";
    if (request.sessionContext !== undefined && !sameSession(request.sessionContext, now)) return "mismatch";
    if (retry && firstSession && !sameSession(firstSession, now)) return "mismatch";
    return null;
  };
  const refuse = (fresh: { whoami: WhoamiPayload | null }, retry = false) => {
    if (request.signal?.aborted) return ABORTED;
    const check = contextCheck(fresh, retry);
    if (check === "mismatch") return CONTEXT_MISMATCH;
    if (check === "unverified") return CONTEXT_UNVERIFIED;
    // Last, right before the send: a caller's stillValid may record that the
    // request is about to leave, so nothing may refuse the send after it.
    if (request.stillValid && !request.stillValid()) return ABORTED;
    return null;
  };
  const refusedFirst = refuse(csrf);
  if (refusedFirst) return refusedFirst;
  // The whoami read that the request about to go out was authorized by.
  let sentWith: WhoamiPayload | null = csrf.whoami;
  // From the first fetch call on, a failure no longer means "not sent".
  let dispatched = false;

  const send = async (token: string | null): Promise<Response> => {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (request.body !== undefined) headers["Content-Type"] = "application/json";
    if (token) headers["x-ua-csrf"] = token;
    if (request.idempotencyKey) headers["Idempotency-Key"] = request.idempotencyKey;
    dispatched = true;
    request.onDispatch?.();
    return fetch(request.path, {
      method,
      credentials: "same-origin",
      headers,
      body: request.body === undefined ? undefined : JSON.stringify(request.body),
      signal: request.signal,
    });
  };

  let response: Response;
  let body: unknown;
  try {
    response = await send(csrf.token);
    body = await readBody(response);
    if (response.status === 403 && /csrf/i.test(errorText(body) ?? "")) {
      // The token rotated: refresh once and retry, as postCsrfJson does.
      const fresh = await readCsrf(request.signal);
      if (fresh.status === 401) {
        return { kind: "error", status: 401, message: "Your session has ended. Sign in again to continue." };
      }
      const refusedRetry = refuse(fresh, true);
      if (refusedRetry) return refusedRetry;
      sentWith = fresh.whoami;
      response = await send(fresh.token);
      body = await readBody(response);
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    if (!dispatched) {
      if (request.signal?.aborted) return ABORTED;
      return { kind: "error", status: null, message: "The hub could not be reached. Nothing was sent; try again.", detail };
    }
    // Sent, then lost, cut off or unreadable: the hub may have applied it.
    return { ...OUTCOME_UNKNOWN, detail };
  }

  const detail = errorText(body);
  if (response.ok) {
    const obj = asObject(body);
    if (obj && isParkedResponse(response.status, obj)) {
      const tool = typeof obj.tool === "string" ? obj.tool : undefined;
      return {
        kind: "pending_approval",
        approvalId: approvalIdOf(obj),
        sentContext: sentWith ? contextFromWhoami(sentWith) : null,
        body: obj,
        ...(tool ? { tool } : {}),
      };
    }
    return { kind: response.status === 202 ? "accepted" : "done", data: body as T };
  }
  const code = codeOf(body);
  const withCode = <O extends WriteOutcome<T>>(outcome: O): O => (code ? { ...outcome, code } : outcome);
  switch (response.status) {
    case 401:
      return withCode({ kind: "error", status: 401, message: "Your session has ended. Sign in again to continue.", detail });
    case 403: {
      const capability = capabilityOf(body);
      return withCode({
        kind: "error",
        status: 403,
        message: capability
          ? `The signed-in identity may not do this: it needs the "${capability}" permission. Ask the operator, or sign in as an identity that has it.`
          : "The signed-in identity may not do this. Ask the operator, or sign in as an identity that may.",
        detail,
        ...(capability ? { capability } : {}),
      });
    }
    case 404:
      return withCode({ kind: "error", status: 404, message: "This item no longer exists, or this hub does not offer this action.", detail });
    case 409:
      return withCode({
        kind: "error",
        status: 409,
        message: "This item changed since you opened it. Reload it to see the latest version, then try again.",
        detail,
      });
    case 400:
    case 422:
      return withCode({
        kind: "error",
        status: response.status,
        message: detail ? `The hub did not accept this: ${detail}` : "The hub did not accept this. Check the fields and try again.",
        detail,
      });
    default:
      return withCode({
        kind: "error",
        status: response.status,
        message: "The hub had a problem completing this. Try again in a moment.",
        detail: detail ?? `${request.path} HTTP ${response.status}`,
      });
  }
}

/** Link to the Inbox item for a parked approval. */
export function approvalInboxHref(approvalId: string | null): string {
  return approvalId ? `/inbox?kind=approval&item=${encodeURIComponent(approvalId)}` : "/inbox?kind=approval";
}

export type HubWriteOptions<TData> = {
  path: string;
  method?: HubWriteMethod;
  /** Refetched on `done` or `accepted`, and after a parked write is repeated. */
  invalidates?: QueryKey[];
  successMessage?: string | ((data: TData) => string);
  /** Shown while waiting for approval and when the repeated call reports back. */
  label?: string;
  /**
   * The toast shown when the write is parked for approval. Default: the
   * generic "waiting for approval ... submit this form again" (or "run it
   * from here" when it is stored). A string or a function replaces the
   * message, so a slice can say what really happens next; `false` shows no
   * toast (the page shows its own notice). The state is set either way.
   */
  pendingMessage?: string | false | ((parked: { approvalId: string | null; canRunFromStore: boolean }) => string);
  /**
   * Default true: a parked write is stored and the shell's runner repeats the
   * identical request once when its approval is approved.
   *
   * A write is resumable ONLY when repeating it is proven, with a test, to
   * rebuild the identical approval payload: an approved row is a one-shot
   * pass for exactly that call. Slices opt out with `resumable: false` for
   * anything else, and always for a body that carries a secret (it would be
   * written to sessionStorage) and for a work-product submission (resubmitting
   * re-hashes the file and can create a second submission). When opted out,
   * the person reruns the form after approval.
   */
  resumable?: boolean;
  /**
   * Called once per `run`; the value is sent as `Idempotency-Key`, kept
   * across the CSRF retry, and stored with a parked write so the approval
   * replay carries the same key.
   */
  idempotencyKey?: () => string;
  /** App path of the form, for "Open the form to run it" (default: the current page). */
  formHref?: string;
  /**
   * Default true: refuse the write (`cancelled`, reason `unverified`) unless
   * a whoami read has succeeded and names who is signed in at submit. Only
   * sign-out sets false: it must work when whoami fails.
   */
  requireVerifiedOwner?: boolean;
};

export type HubWriteState<TData> =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; data: TData }
  | { status: "accepted"; data: TData }
  | {
      status: "pending_approval";
      approvalId: string | null;
      tool?: string;
      resumable: boolean;
      /** Always false: the repeat is always a person's click. */
      willReplay: false;
      /** True when the write was stored and "Run it now" can be offered once approved. */
      canRunFromStore: boolean;
      /** Why it cannot be run from the store, or null when it can. */
      replayBlockedReason: ReplayBlockedReason | null;
      parkedWriteId?: string;
    }
  | { status: "error"; httpStatus: number | null; message: string; detail?: string; capability?: string }
  /** See `WriteOutcome` `cancelled`: show nothing, do not retry, keep the draft. */
  | { status: "cancelled"; reason: CancelReason; dispatched: boolean };

/** The current page as an app path ("/work/tasks/new?x=1"), without the /ui basename. */
function currentAppPath(): string | undefined {
  if (typeof window === "undefined") return undefined;
  const { pathname, search } = window.location;
  return `${pathname.replace(/^\/ui(?=\/)/, "")}${search}`;
}

export type RunOverrides = {
  path?: string;
  signal?: AbortSignal;
  /**
   * The form draft this request is submitted from. Stored with the write if
   * it parks, so a later successful run from anywhere clears that draft
   * while it still holds the snapshot (`ParkedDraftBinding`, task #2525).
   */
  draft?: ParkedDraftBinding;
};

/**
 * The one way a page writes to the hub (spec §6). Classifies the result,
 * invalidates queries on success, toasts, parks an approval-gated write for
 * the shell's runner to repeat, and sends a 401 back to sign-in.
 *
 * Each `run` captures the owner epoch it starts in and uses only that
 * epoch afterwards: its signal for the requests, its QueryClient for the
 * invalidations, its storage for a parked write, its toasts, and the slice's
 * callbacks (`successMessage`, `pendingMessage`) wrapped by it. Once the
 * epoch is revoked, none of those has any effect.
 */
export function useHubWrite<TBody = unknown, TData = unknown>(options: HubWriteOptions<TData>) {
  const currentEpoch = useEpoch();
  const [state, setStateNow] = useState<HubWriteState<TData>>({ status: "idle" });
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const epochRef = useRef(currentEpoch);
  epochRef.current = currentEpoch;

  const run = useCallback(
    async (body: TBody, overrides: RunOverrides = {}): Promise<WriteOutcome<TData>> => {
      const epoch: OwnerEpoch = epochRef.current;
      const opts = optionsRef.current;
      const path = overrides.path ?? opts.path;
      const method = opts.method ?? "POST";
      const setState = (next: HubWriteState<TData>) => epoch.run(() => setStateNow(next));
      // The slice's callbacks, bound to this epoch now.
      const successMessage =
        typeof opts.successMessage === "function" ? epoch.wrap(opts.successMessage) : opts.successMessage;
      const pendingMessage =
        typeof opts.pendingMessage === "function" ? epoch.wrap(opts.pendingMessage) : opts.pendingMessage;
      setState({ status: "running" });
      const idempotencyKey = opts.idempotencyKey?.();
      // Bound to the sign-in that started it: the epoch, the generation, and
      // the session the page observed (when known). Every send (first and
      // CSRF retry) re-checks them.
      const generation = authGeneration();
      /**
       * The owner the page observes now: a successful whoami read that names
       * a non-empty identity. `{ agent: null }`, `{}` or "" is nobody, never
       * an owner (two empty identities must not count as the same person).
       */
      const observed = () => epoch.observed();
      const initiating = observed();
      let dispatched = false;
      const cancel = (reason: CancelReason): WriteOutcome<TData> => {
        setState({ status: "cancelled", reason, dispatched });
        return { kind: "cancelled", reason, dispatched, message: CANCEL_MESSAGES[reason] };
      };
      /** Sign-out began since the write started: store, show and return nothing actionable. */
      const signedOut = () => !isCurrentGeneration(generation);
      /**
       * Whether the write still belongs to the signed-in person: its epoch is
       * live, same sign-in generation, and (when it had a verified owner) the
       * page still observes that owner through a successful whoami read.
       * Checked after every await, before anything is stored, shown,
       * invalidated or returned.
       */
      const ownerChange = (): CancelReason | null => {
        if (signedOut()) return "signed_out";
        const now = observed();
        if (epoch.revoked) {
          if (initiating === null || now === null) return now === null ? "unverified" : "owner_changed";
          return sameSession(initiating, now) ? "signed_out" : "owner_changed";
        }
        if (initiating === null) return null;
        if (now === null) return "unverified";
        return sameSession(initiating, now) ? null : "owner_changed";
      };
      if (epoch.revoked) return cancel(ownerChange() ?? "signed_out");
      // A write needs a verified owner at submit: without a successful whoami
      // read now, it is refused, and no later read is adopted as its owner.
      if (opts.requireVerifiedOwner !== false && initiating === null) return cancel("unverified");
      const controller = new AbortController();
      const forwardAbort = () => controller.abort();
      epoch.signal.addEventListener("abort", forwardAbort);
      overrides.signal?.addEventListener("abort", forwardAbort);
      if (overrides.signal?.aborted || epoch.signal.aborted) controller.abort();
      let outcome: WriteOutcome<TData>;
      try {
        outcome = await executeHubWrite<TData>({
          path,
          method,
          body,
          idempotencyKey,
          signal: controller.signal,
          ...(initiating ? { sessionContext: initiating } : {}),
          stillValid: () =>
            !epoch.revoked && isCurrentGeneration(generation) && (initiating === null || sameSession(initiating, observed())),
          onDispatch: () => {
            dispatched = true;
          },
        });
      } finally {
        epoch.signal.removeEventListener("abort", forwardAbort);
        overrides.signal?.removeEventListener("abort", forwardAbort);
      }
      let outcomeForCaller: WriteOutcome<TData> = outcome;
      // First whether this write still belongs to the current owner: a late
      // 401 for A, after B became the owner, must not end B's sign-in.
      const changedAfterSend = ownerChange();
      if (changedAfterSend) return cancel(changedAfterSend);
      if (outcome.kind === "error" && outcome.status === 401) {
        // A 401 ends the sign-in this write belongs to (still the current
        // one): a new auth generation revokes the epoch and reads whoami
        // afresh (the sign-in page if it is really over). A's error, detail
        // and code are shown to nobody.
        bumpAuthGeneration();
        return cancel(ownerChange() ?? "signed_out");
      }
      // A fresh whoami read made for the send that named someone else, or
      // that failed, is a refusal to send on this owner's behalf: a
      // cancellation, never an ordinary error.
      if (outcome.kind === "error" && outcome.contextMismatch) return cancel("owner_changed");
      if (outcome.kind === "error" && outcome.contextUnverified) return cancel("unverified");
      const label = opts.label ?? "This change";
      switch (outcome.kind) {
        case "done":
        case "accepted": {
          await Promise.all(
            (opts.invalidates ?? []).map((queryKey) => epoch.run(() => epoch.queryClient.invalidateQueries({ queryKey }))),
          );
          const changedAfterInvalidate = ownerChange();
          if (changedAfterInvalidate) return cancel(changedAfterInvalidate);
          const message = typeof successMessage === "function" ? successMessage(outcome.data) : successMessage;
          if (message) epoch.toast({ message, tone: "success" });
          else if (outcome.kind === "accepted") epoch.toast({ message: `${label} has started.`, tone: "info" });
          setState({ status: outcome.kind, data: outcome.data });
          break;
        }
        case "pending_approval": {
          const resumable = opts.resumable !== false;
          // The context the request was actually sent under (spec §6), never a
          // cache read made afterwards.
          const sent = outcome.sentContext ?? null;
          const verdict = replayVerdict(resumable, sent, sent, outcome.approvalId);
          let parkedWriteId: string | undefined;
          let notStored = false;
          if (resumable && outcome.approvalId !== null && sent !== null) {
            // Advertised as runnable only once storage confirmed the entry.
            parkedWriteId = addParkedWrite(epoch, {
              path,
              method,
              body,
              approvalId: outcome.approvalId,
              label,
              context: sent,
              invalidates: (opts.invalidates ?? []) as unknown[][],
              formHref: opts.formHref ?? currentAppPath(),
              // The hub's own answer to this person; only stored with a resumable write.
              responseBody: outcome.body,
              ...(idempotencyKey ? { idempotencyKey } : {}),
              ...(overrides.draft?.draftKey ? { draft: overrides.draft } : {}),
            })?.id;
            notStored = parkedWriteId === undefined;
          }
          const canRunFromStore = parkedWriteId !== undefined && verdict.canRunFromStore;
          const replayBlockedReason: ReplayBlockedReason | null = notStored
            ? "not_stored"
            : parkedWriteId === undefined && verdict.reason === null
              ? "no_approval_reference"
              : verdict.reason;
          const custom = pendingMessage;
          if (custom !== false) {
            epoch.toast({
              message:
                typeof custom === "function"
                  ? custom({ approvalId: outcome.approvalId, canRunFromStore })
                  : typeof custom === "string"
                    ? custom
                    : canRunFromStore
                      ? `${label} is waiting for approval. When it is approved, run it from here or from the Inbox.`
                      : `${label} is waiting for approval. After it is approved, submit this form again.`,
              tone: "info",
              action: { label: "Open approval", href: approvalInboxHref(outcome.approvalId) },
            });
          }
          setState({
            status: "pending_approval",
            approvalId: outcome.approvalId,
            resumable,
            willReplay: false,
            canRunFromStore,
            replayBlockedReason,
            ...(parkedWriteId ? { parkedWriteId } : {}),
            ...(outcome.tool ? { tool: outcome.tool } : {}),
          });
          outcomeForCaller = {
            ...outcome,
            willReplay: false,
            canRunFromStore,
            replayBlockedReason,
            ...(parkedWriteId ? { parkedWriteId } : {}),
          };
          break;
        }
        case "cancelled":
          return cancel(outcome.reason);
        case "error":
          setState({
            status: "error",
            httpStatus: outcome.status,
            message: outcome.message,
            ...(outcome.detail ? { detail: outcome.detail } : {}),
            ...(outcome.capability ? { capability: outcome.capability } : {}),
          });
          break;
      }
      // The toast and the state above can outlive the owner (B arrives while
      // A's toast shows): the caller's continuation (navigate, clear a
      // draft, open the next form) must not run under whoever is next.
      const changedAtEnd = ownerChange();
      if (changedAtEnd) return cancel(changedAtEnd);
      return outcomeForCaller;
    },
    [],
  );

  const reset = useCallback(() => setStateNow({ status: "idle" }), []);

  return { run, state, reset, isRunning: state.status === "running" };
}
