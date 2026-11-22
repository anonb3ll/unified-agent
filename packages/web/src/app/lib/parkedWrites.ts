import { useSyncExternalStore } from "react";
import { useQueries } from "@tanstack/react-query";
import type { WhoamiPayload } from "../../lib/api/auth";
import { authGeneration, bumpAuthGeneration, isCurrentGeneration } from "./authGeneration";
import { clearDraftIfUnchanged } from "./drafts";
import { useEpoch, type CancelReason, type OwnerEpoch } from "./epoch";
import { useWhoami } from "./identity";
import { CANCEL_MESSAGES, executeHubWrite, type HubWriteMethod } from "./mutations";
import {
  contextFromWhoami,
  replaySuspendedReason,
  replayVerdict,
  sameContext,
  type ReplaySuspendedReason,
  type ReplayVerdict,
  type WriteContext,
} from "./writeContext";

export {
  replayVerdict,
  type ReplayBlockedReason,
  type ReplayVerdict,
  contextFromWhoami,
  organizationFromWhoami,
  replaySuspendedReason,
  sameContext,
  type ReplaySuspendedReason,
  type WriteContext,
} from "./writeContext";

/**
 * Writes parked behind a human approval (spec §6).
 *
 * Everything here is bound to an owner epoch (`epoch.tsx`): entries and
 * results live in `epoch.storage`, approval reads in `epoch.queryClient`
 * under keys that name the owner, and a run uses only the epoch it was
 * started with. A revoked epoch reads nothing and writes nothing.
 *
 * Cancellation contract of a manual run (`runParkedWrite`):
 * - It belongs to this tab, not to a component: it survives navigation and
 *   the unmount of the page that started it.
 * - It is cancelled by Discard (`discardParkedWrite`), by revocation of its
 *   epoch (sign-out, an owner change, a 401, another tab's sign-in or
 *   sign-out) and by a context change (the fresh whoami read before the
 *   send, and before a CSRF retry, must match the stored hub origin,
 *   organization and identity; a confirmed difference discards the entry).
 *   A read that cannot confirm who is signed in (whoami failed, unreadable,
 *   or naming no identity or no organization) is not a difference: nothing
 *   is sent, the entry is kept with an honest message, and it can be retried.
 * - Every await boundary re-checks: the entry still exists, the run was not
 *   cancelled, and the auth generation is the one it started under.
 * - A run that no longer belongs to the signed-in person returns
 *   `{ kind: "cancelled", reason, dispatched }` (`signed_out`,
 *   `owner_changed`, `unverified`): no result, approval cache update or
 *   notification is published for a new person. For the same person, a
 *   run cancelled after its request left keeps the entry as outcome
 *   unknown.
 * - Before the request leaves, cancelling means nothing was sent. After it
 *   left, a cancelled run cannot be taken back: the entry is kept with
 *   `outcome: "unknown"` (unless the hub's answer still arrives, which is
 *   then recorded as the result).
 *
 * Approval does not run the write: an approved row is a one-shot pass for
 * the identical call. The call is stored here so a person can run it again
 * with one click (`runParkedWrite`, from the form or the Inbox item). Nothing
 * here ever sends by itself: there is no runner, no polling-triggered send
 * and no cross-tab replay. Every entry records the hub origin, organization
 * and identity of the whoami read the original request was sent under, and
 * a run is sent only when a fresh whoami read matches all three.
 *
 * A write may carry the form draft it was submitted from (`draft`, task
 * #2525). When it later runs successfully, from anywhere (the form, the
 * Inbox, with the form unmounted), that draft is cleared, but only while
 * its stored value still holds the submitted snapshot: newer text survives.
 */
export type ParkedDraftBinding = {
  /** The draft's storage key (`useDraft().key`), in the same owner's storage. */
  draftKey: string;
  /**
   * What was submitted from it. An object is compared over its own fields
   * (`draftHoldsSnapshot`). Stored with the write: never include a field the
   * draft omits (a token or credential).
   */
  submittedSnapshot: unknown;
};

export type ParkedWrite = {
  id: string;
  path: string;
  method: HubWriteMethod;
  /** The exact body that was parked; the pass only matches the identical call. */
  body: unknown;
  /** The approval the write waits on. Writes without one are never stored. */
  approvalId: string;
  /** User-facing name of the write: "Create task". */
  label: string;
  /** The context the request was actually sent under. */
  context: WriteContext;
  invalidates: unknown[][];
  /** Sent again unchanged on the run. */
  idempotencyKey?: string;
  /** App path of the form that parked it ("Open the form to run it"). */
  formHref?: string;
  /** The hub's parsed 202 answer that parked it (for example `work_product_id`). */
  responseBody?: unknown;
  /** The form draft it was submitted from, cleared after a successful run if unchanged. */
  draft?: ParkedDraftBinding;
  createdAt: number;
  /**
   * "unknown": a run was sent but its result was lost. The change may have
   * been made, so the entry cannot run again until the person says it did
   * not happen (`confirmNotApplied`).
   */
  outcome?: "unknown";
  /** The last run's error, shown next to the entry. */
  lastError?: string;
  /**
   * Set, and read back from storage, right before a run's request is sent.
   * A page that reloads or closes before the answer cannot know what
   * happened: on load an entry with this marker and no run in flight in
   * this page is outcome unknown.
   */
  attemptStartedAt?: number;
  /**
   * When a run's own approval read failed. Until a successful read of the
   * approval newer than this, the entry is "unverified" and cannot run.
   */
  verifyFailedAt?: number;
};

export type ParkedWriteResult = {
  approvalId: string;
  kind: "done" | "accepted" | "error" | "outcome_unknown" | "discarded";
  message: string;
  at: number;
  /** The context of the write the result belongs to; results are only read back by that owner. */
  owner?: WriteContext;
};

/** Whether `current` is the verified owner of something made under `stored` (no organization equals no organization). */
export function isOwnedBy(stored: WriteContext | null | undefined, current: WriteContext | null | undefined): boolean {
  if (!stored || !current) return false;
  if (!stored.hubOrigin || stored.hubOrigin !== current.hubOrigin) return false;
  if (!stored.identity || stored.identity !== current.identity) return false;
  return stored.organization === current.organization;
}

/** Keys under the owner's scope (`epoch.storage`). */
const WRITES_KEY = "parked-writes";
const RESULTS_KEY = "parked-write-results";
/** How often a stored write's approval is re-read while a page shows it (a read, never a send). */
export const PARKED_POLL_MS = 10_000;

const listeners = new Set<() => void>();
const EMPTY: ParkedWrite[] = [];
/**
 * Last listing per epoch, reused while the stored text and the runs in
 * flight are unchanged. It holds request bodies, so it lives no longer than
 * its epoch: dropped on revocation and on `clearParkedWrites`.
 */
const cache = new Map<OwnerEpoch, { signature: string; list: ParkedWrite[] }>();
/** Epochs whose revocation already drops their cache entry. */
const evictOnRevoke = new WeakSet<OwnerEpoch>();

/** The listings held in memory now (for tests and diagnostics). */
export function parkedWritesInMemory(): ParkedWrite[][] {
  return [...cache.values()].map((entry) => entry.list);
}
/** Runs in flight in this tab, by entry id: the double-click guard and the cancel handle. */
const running = new Map<string, { controller: AbortController; dispatched: boolean; ownerKey: string | null }>();

function emit() {
  for (const listener of [...listeners]) listener();
}

/** Shown for an entry whose send was interrupted by a reload, a closed page or an owner change. */
const INTERRUPTED =
  "This was being sent when the page was reloaded or closed, or the signed-in person changed, so it is not known whether the change was made. Check before running it again.";

const TERMINAL: ParkedWriteResult["kind"][] = ["done", "accepted", "discarded"];

function readResults(epoch: OwnerEpoch): Record<string, ParkedWriteResult> {
  const stored = epoch.storage.read<unknown>(RESULTS_KEY, {});
  return stored && typeof stored === "object" && !Array.isArray(stored) ? (stored as Record<string, ParkedWriteResult>) : {};
}

/**
 * This owner's stored writes (none without an owner, or once revoked).
 * On every read:
 * - an entry whose approval has a recorded terminal result newer than the
 *   entry was settled (a retirement whose removal did not land) and is left out;
 * - an entry with an attempt marker and no run in flight in this page was
 *   interrupted after its request left: its outcome is unknown.
 */
export function listParkedWrites(epoch: OwnerEpoch): ParkedWrite[] {
  if (epoch.ownerKey === null || epoch.revoked) {
    cache.delete(epoch);
    return EMPTY;
  }
  const raw = epoch.storage.readText(WRITES_KEY);
  const rawResults = epoch.storage.readText(RESULTS_KEY);
  const signature = JSON.stringify([raw, rawResults, [...running.keys()]]);
  const hit = cache.get(epoch);
  if (hit && hit.signature === signature) return hit.list;
  const stored = epoch.storage.read<unknown>(WRITES_KEY, []);
  const results = readResults(epoch);
  const writes = Array.isArray(stored)
    ? (stored as ParkedWrite[]).filter((w) => typeof w?.approvalId === "string" && isOwnedBy(w.context, epoch.context))
    : [];
  const list = writes
    .filter((w) => {
      const result = results[w.approvalId];
      return !(result && TERMINAL.includes(result.kind) && result.at >= w.createdAt);
    })
    .map((w) =>
      w.attemptStartedAt !== undefined && !running.has(w.id) && w.outcome !== "unknown"
        ? { ...w, outcome: "unknown" as const, lastError: INTERRUPTED }
        : w,
    );
  if (!evictOnRevoke.has(epoch)) {
    evictOnRevoke.add(epoch);
    epoch.signal.addEventListener("abort", () => cache.delete(epoch), { once: true });
  }
  cache.set(epoch, { signature, list });
  return list;
}

/** Write the list (read back by `epoch.storage`); false when it did not land. */
function save(epoch: OwnerEpoch, writes: ParkedWrite[]): boolean {
  const ok = writes.length === 0 ? epoch.storage.remove(WRITES_KEY) : epoch.storage.write(WRITES_KEY, writes);
  emit();
  return ok;
}

function find(epoch: OwnerEpoch, id: string): ParkedWrite | undefined {
  return listParkedWrites(epoch).find((w) => w.id === id);
}

/**
 * Store a parked write under the epoch's owner. Returns the entry only when
 * storage confirmed it, else null (nothing can be run from the store then).
 * Refused when the epoch is revoked, has no owner, or the write was made
 * under someone else.
 */
export function addParkedWrite(epoch: OwnerEpoch, write: Omit<ParkedWrite, "id" | "createdAt">): ParkedWrite | null {
  if (epoch.revoked || !isOwnedBy(write.context, epoch.context)) return null;
  const entry: ParkedWrite = {
    ...write,
    id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    createdAt: Date.now(),
  };
  const rest = listParkedWrites(epoch).filter((w) => w.approvalId !== write.approvalId);
  if (!save(epoch, [...rest, entry])) return null;
  return find(epoch, entry.id) ? entry : null;
}

/** Change an entry; true only when storage confirmed it. */
export function updateParkedWrite(epoch: OwnerEpoch, id: string, change: Partial<Omit<ParkedWrite, "id">>): boolean {
  const list = listParkedWrites(epoch);
  if (!list.some((w) => w.id === id)) return false;
  return save(
    epoch,
    list.map((w) => {
      if (w.id !== id) return w;
      const next: ParkedWrite = { ...w, ...change };
      // An explicit `undefined` removes the field (so it does not linger in storage).
      for (const [key, value] of Object.entries(change)) {
        if (value === undefined) delete (next as Record<string, unknown>)[key];
      }
      return next;
    }),
  );
}

/** After the write was made: clear the draft it came from, if the draft still holds what was submitted. */
function settleDraft(epoch: OwnerEpoch, write: ParkedWrite): void {
  if (write.draft) clearDraftIfUnchanged(epoch, write.draft.draftKey, write.draft.submittedSnapshot);
}

/** Remove an entry; true only when storage confirmed it. */
export function removeParkedWrite(epoch: OwnerEpoch, id: string): boolean {
  return save(
    epoch,
    listParkedWrites(epoch).filter((w) => w.id !== id),
  );
}

/**
 * The person dismisses a stored write ("Discard"). A run of it that has not
 * been sent yet is cancelled and nothing is sent. A run already sent cannot
 * be taken back, so the entry is kept as "outcome unknown" instead of
 * vanishing. True when storage confirmed the change.
 */
export function discardParkedWrite(epoch: OwnerEpoch, id: string): boolean {
  if (epoch.revoked) return false;
  const run = running.get(id);
  if (run) {
    run.controller.abort();
    if (run.dispatched) {
      return updateParkedWrite(epoch, id, {
        outcome: "unknown",
        lastError: "Discarded after it was sent, so it is not known whether the change was made.",
      });
    }
  }
  const write = find(epoch, id);
  if (!write) return false;
  recordParkedWriteResult(epoch, {
    owner: write.context,
    approvalId: write.approvalId,
    kind: "discarded",
    message: `${write.label} was discarded. Submit the form again if it is still needed.`,
    at: Date.now(),
  });
  return removeParkedWrite(epoch, id);
}

/**
 * Sign-out: drop this owner's stored writes and results, and cancel any of
 * their runs that has not been sent yet. Call it under the epoch the
 * sign-out started in, before `beginSignOut` revokes it.
 */
export function clearParkedWrites(epoch: OwnerEpoch): boolean {
  for (const run of running.values()) if (run.ownerKey === epoch.ownerKey) run.controller.abort();
  for (const held of [...cache.keys()]) if (held === epoch || held.ownerKey === epoch.ownerKey) cache.delete(held);
  const ok = epoch.storage.remove(WRITES_KEY) && epoch.storage.remove(RESULTS_KEY);
  emit();
  return ok;
}

/** Record what happened to a parked write; true when storage confirmed it. Refused for another owner. */
export function recordParkedWriteResult(epoch: OwnerEpoch, result: ParkedWriteResult): boolean {
  if (!isOwnedBy(result.owner, epoch.context)) return false;
  const ok = epoch.storage.write(RESULTS_KEY, { ...readResults(epoch), [result.approvalId]: result });
  emit();
  return ok;
}

/** What happened to the write parked behind this approval, for this epoch's owner (the Inbox item). */
export function getParkedWriteResult(epoch: OwnerEpoch, approvalId: string): ParkedWriteResult | null {
  const result = readResults(epoch)[approvalId];
  return result && isOwnedBy(result.owner, epoch.context) ? result : null;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The raw store for this epoch's owner (no approval reads). */
export function useParkedWriteStore(): ParkedWrite[] {
  const epoch = useEpoch();
  return useSyncExternalStore(
    subscribe,
    () => listParkedWrites(epoch),
    () => EMPTY,
  );
}

/**
 * Whether this stored write can be run from the store under the signed-in
 * session, and if not, why. `willReplay` is always false.
 */
export function parkedWriteReplay(write: ParkedWrite, whoami: WhoamiPayload | null | undefined): ReplayVerdict {
  return replayVerdict(true, write.context, whoami ? contextFromWhoami(whoami) : null, write.approvalId);
}

// ---------------------------------------------------------------- approval state

/** The approval's state, from `GET /api/approvals/{id}` (`{ id, tool, status, outcome, ... }`). */
export type ApprovalResolution = "pending" | "approved" | "consumed" | "denied" | "revoked" | "expired" | "missing" | "unknown";

export type ApprovalStatusRead = {
  resolution: ApprovalResolution;
  /** Who asked for the approval (`requested_by`). The hub never lets them approve it themselves. */
  requestedBy?: string | null;
};

export async function fetchApprovalStatus(approvalId: string, signal?: AbortSignal): Promise<ApprovalStatusRead> {
  const response = await fetch(`/api/approvals/${encodeURIComponent(approvalId)}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (response.status === 404) return { resolution: "missing" };
  if (!response.ok) throw new Error(`approval detail HTTP ${response.status}`);
  const body = (await response.json().catch(() => ({}))) as { status?: unknown; requested_by?: unknown };
  const status = typeof body.status === "string" ? body.status.toLowerCase() : "";
  const known: ApprovalResolution[] = ["pending", "approved", "consumed", "denied", "revoked", "expired"];
  return {
    resolution: (known as string[]).includes(status) ? (status as ApprovalResolution) : "unknown",
    requestedBy: typeof body.requested_by === "string" && body.requested_by ? body.requested_by : null,
  };
}

/** The approval read's key in an epoch's client; it names the owner, so no read is shared between owners. */
export function approvalStatusQueryKey(approvalId: string, ownerKey: string | null) {
  return ["cc2", "parked-approval", ownerKey, approvalId] as const;
}

/** What a page shows for the approval a stored write waits on. */
export type ParkedApprovalState =
  | "loading"
  /** The latest read failed: the state is not verified, whatever was read before. */
  | "unverified"
  | "waiting"
  | "approved"
  | "denied"
  | "revoked"
  | "expired"
  | "consumed"
  | "missing"
  | "unknown";

/** Why "Run it now" is not offered for an entry, or null when it is. */
export type RunBlockedReason =
  | ReplaySuspendedReason
  | "not_approved"
  | "approval_unverified"
  | "outcome_unknown"
  | "running";

export type ParkedWriteEntry = ParkedWrite & {
  approval: ParkedApprovalState;
  /** True when "Run it now" may be offered. */
  canRun: boolean;
  blockedReason: RunBlockedReason | null;
  /** A run of this entry is in flight in this tab. */
  running: boolean;
  /**
   * Whether the signed-in person is the one who asked for this approval
   * (see `ApproverRelation`). Never read "other" or "unknown" as permission
   * to approve: the hub decides that.
   */
  approver: ApproverRelation;
  /** `approver === "self"`: the hub will not let this person approve it, so the Inbox must not offer Approve. */
  needsOtherApprover: boolean;
};

/**
 * The signed-in person's relation to an approval's requester. The hub lets
 * only a different person approve, and decides "same person" itself with
 * aliases from its own configuration (`_same_person`), which whoami does
 * not expose. So the client can prove "self" (identical labels) but never
 * "other": two different labels may be one person.
 * - "self": the same label: this person cannot approve it.
 * - "unknown": no requester, no signed-in identity, or a different label.
 * - "other": reserved for when the hub reports that the caller may approve;
 *   not produced today.
 * A consumer must not treat "other" or "unknown" as permission to approve.
 */
export type ApproverRelation = "self" | "other" | "unknown";

export function approverRelation(requestedBy: string | null | undefined, identity: string | null | undefined): ApproverRelation {
  if (!requestedBy || !identity) return "unknown";
  return requestedBy === identity ? "self" : "unknown";
}

/** True only when the approval was requested by the signed-in identity, who therefore cannot approve it. */
export function needsOtherApprover(requestedBy: string | null | undefined, identity: string | null | undefined): boolean {
  return approverRelation(requestedBy, identity) === "self";
}

function approvalState(resolution: ApprovalResolution | undefined, failed: boolean): ParkedApprovalState {
  // A failed latest read overrides anything cached: an approval read earlier
  // and not confirmed since is never presented as approved.
  if (failed) return "unverified";
  if (resolution === undefined) return "loading";
  return resolution === "pending" ? "waiting" : resolution;
}

function blockedReasonFor(
  write: ParkedWrite,
  approval: ParkedApprovalState,
  current: WriteContext | null,
): RunBlockedReason | null {
  if (running.has(write.id)) return "running";
  if (write.outcome === "unknown") return "outcome_unknown";
  const context = replaySuspendedReason(write.context, current);
  if (context) return context;
  if (approval === "unverified") return "approval_unverified";
  return approval === "approved" ? null : "not_approved";
}

/**
 * Stored writes with the state of their approvals, read through
 * `GET /api/approvals/{id}` and refreshed every `PARKED_POLL_MS` while a page
 * shows them. Reading never sends anything. Pass `approvalId` for the one
 * entry a form or an Inbox item shows.
 */
export function useParkedWrites(filter: { approvalId?: string | null } = {}): ParkedWriteEntry[] {
  const epoch = useEpoch();
  const store = useParkedWriteStore();
  const whoami = useWhoami();
  // Verified only by a successful latest read: TanStack keeps the old data
  // after a failed refresh (or a 403), and that must not count as verified.
  const current = whoami.status === "success" && whoami.data ? contextFromWhoami(whoami.data) : null;
  // Only the verified owner's writes are listed: another tab signing in as
  // someone else must not expose this one's bodies (round 3, item 4).
  const writes = store.filter(
    (w) => isOwnedBy(w.context, current) && (filter.approvalId === undefined || w.approvalId === filter.approvalId),
  );
  const statuses = useQueries(
    {
      queries: writes.map((w) => ({
        queryKey: approvalStatusQueryKey(w.approvalId, epoch.ownerKey),
        queryFn: ({ signal }: { signal: AbortSignal }) => fetchApprovalStatus(w.approvalId, signal),
        refetchInterval: PARKED_POLL_MS,
        refetchIntervalInBackground: false,
        retry: 1,
      })),
    },
    epoch.queryClient,
  );
  return writes.map((write, index) => {
    const read = statuses[index];
    // A failed run-time check blocks until a successful read newer than it.
    const verifiedSince = !write.verifyFailedAt || (read?.status === "success" && read.dataUpdatedAt > write.verifyFailedAt);
    const approval = approvalState(read?.data?.resolution, read?.status === "error" || !verifiedSince);
    const blockedReason = blockedReasonFor(write, approval, current);
    return {
      ...write,
      approval,
      canRun: blockedReason === null,
      blockedReason,
      running: running.has(write.id),
      approver: approverRelation(read?.data?.requestedBy, current?.identity),
      needsOtherApprover: needsOtherApprover(read?.data?.requestedBy, current?.identity),
    };
  });
}

// ---------------------------------------------------------------- run

export type RunParkedWriteResult =
  | {
      kind: "done" | "accepted";
      data: unknown;
      message: string;
      /**
       * True when the person said the change is there (`confirmApplied`)
       * after an outcome unknown: the hub did not confirm it, and `data` is
       * null. Run the page's success path, but do not read hub data from it.
       */
      confirmedByPerson?: true;
    }
  /** The repeat parked again; the entry now waits on `approvalId`. */
  | { kind: "pending_approval"; approvalId: string | null; message: string }
  /** Sent, result lost: the entry is kept and blocked until `confirmNotApplied`. */
  | { kind: "outcome_unknown"; message: string }
  /** The hub answered with an error; the entry is kept. */
  | { kind: "error"; status: number | null; message: string }
  /**
   * Stopped by the client because the run no longer belongs to the signed-in
   * person (see `CancelReason`). Show nothing to a new person; for the same
   * person, `dispatched: true` left the entry as outcome unknown.
   */
  | { kind: "cancelled"; reason: CancelReason; dispatched: boolean; message: string }
  /** Not sent; the entry was discarded because the signed-in context changed. */
  | { kind: "discarded"; reason: "context_mismatch" | "signed_out"; message: string }
  /** Not sent; the entry stays as it is. */
  | {
      kind: "not_run";
      reason:
        | "busy"
        | "missing"
        | "outcome_unknown"
        | "organization_unknown"
        | "not_approved"
        /** Discarded, or aborted, before the request left. */
        | "cancelled"
        /** The attempt could not be recorded in storage before sending, so it was not sent. */
        | "not_recorded";
      message: string;
    };

export type RunParkedWriteOptions = {
  /**
   * The epoch the run belongs to (`useEpoch()` where the person clicked).
   * The run reads and writes only through it: its storage, its QueryClient
   * (the run's approval read and the invalidations on success) and its
   * signal. The root whoami it observes is compared with the stored context
   * right before each send.
   */
  epoch: OwnerEpoch;
};

/** The reason a run stops because its epoch or observed owner no longer holds, or null. */
function ownerReason(epoch: OwnerEpoch, generation: number, stored: WriteContext): CancelReason | null {
  if (!isCurrentGeneration(generation)) return "signed_out";
  const now = epoch.observed();
  if (epoch.revoked) return now === null ? "unverified" : sameContext(stored, now) ? "signed_out" : "owner_changed";
  if (now === null) return "unverified";
  return sameContext(stored, now) ? null : "owner_changed";
}

const SENT_UNVERIFIED =
  "This was sent, but who is signed in could not be confirmed before the answer arrived, so it is not known whether the change was made.";

/**
 * Run a stored write once, on a person's click ("Run it now").
 *
 * Checks, in order: the epoch is live, no run of this entry in flight in
 * this tab (a second call is a no-op), the entry exists, its outcome is not
 * unknown, the stored context has an organization, and a fresh
 * `GET /api/approvals/{id}` says approved. `executeHubWrite` then reads
 * whoami afresh and sends only if hub origin, organization and identity
 * equal the stored ones (checked again before a CSRF retry); a confirmed
 * difference discards the entry, an unconfirmable read keeps it. It sends
 * the identical body and idempotency key.
 */
export async function runParkedWrite(id: string, options: RunParkedWriteOptions): Promise<RunParkedWriteResult> {
  const { epoch } = options;
  const generation = authGeneration();
  if (epoch.revoked) {
    const reason = epoch.observed() === null ? "unverified" : "owner_changed";
    return { kind: "cancelled", reason, dispatched: false, message: CANCEL_MESSAGES[reason] };
  }
  if (running.has(id)) return { kind: "not_run", reason: "busy", message: "This is already running." };
  const write = find(epoch, id);
  if (!write) return { kind: "not_run", reason: "missing", message: "This stored change no longer exists." };
  if (write.outcome === "unknown") {
    return {
      kind: "not_run",
      reason: "outcome_unknown",
      message: `It is not known whether ${write.label} was made. Check first, then confirm it did not happen.`,
    };
  }
  if (!write.context.organization) {
    return {
      kind: "not_run",
      reason: "organization_unknown",
      message: `${write.label} cannot be run from here yet. Open the form and run it again.`,
    };
  }
  const controller = new AbortController();
  const forwardAbort = () => controller.abort();
  const run = { controller, dispatched: false, ownerKey: epoch.ownerKey };
  const CANCELLED: RunParkedWriteResult = { kind: "not_run", reason: "cancelled", message: "Cancelled before it was sent." };
  /** Discarded, or its approval changed, while we waited. */
  const gone = () => controller.signal.aborted || find(epoch, id)?.approvalId !== write.approvalId;
  const observedReason = () => ownerReason(epoch, generation, write.context);
  /**
   * The run no longer belongs to the signed-in person. If its request had
   * already left and the epoch is still live (the same person, unverified
   * for now), the entry records that the change may have been made. After a
   * revocation the stored attempt marker already says so to its owner.
   */
  const cancelled = (reason: CancelReason): RunParkedWriteResult => {
    if (run.dispatched && find(epoch, id)) {
      updateParkedWrite(epoch, id, {
        outcome: "unknown",
        lastError:
          "This was sent, but the signed-in person changed or could not be confirmed before the answer arrived, so it is not known whether the change was made.",
      });
    }
    const message = run.dispatched && reason === "unverified" ? SENT_UNVERIFIED : CANCEL_MESSAGES[reason];
    return { kind: "cancelled", reason, dispatched: run.dispatched, message };
  };
  let notRecorded = false;
  /** Persist the attempt marker (read back by `epoch.storage`); false (do not send) if storage refused it. */
  const recordAttempt = () => {
    const ok = updateParkedWrite(epoch, id, { attemptStartedAt: Date.now() });
    if (!ok) notRecorded = true;
    return ok;
  };
  const before = observedReason();
  if (before) return cancelled(before);
  // Registered only now, and always removed in `finally` below.
  epoch.signal.addEventListener("abort", forwardAbort);
  running.set(id, run);
  emit();
  try {
    let read: ApprovalStatusRead | null = null;
    try {
      read = await fetchApprovalStatus(write.approvalId, controller.signal);
    } catch {
      read = null;
    }
    // Owner check right after the await, before anything is published or
    // returned, whether the read succeeded or failed.
    const changedDuringRead = observedReason();
    if (changedDuringRead) return cancelled(changedDuringRead);
    if (read === null) {
      // The check failed: block the entry (no cached "approved" may stand in
      // for it) until a later successful read.
      if (!gone()) updateParkedWrite(epoch, id, { verifyFailedAt: Date.now() });
      return { kind: "not_run", reason: "not_approved", message: "The approval could not be checked. Try again." };
    }
    const approval: ApprovalResolution = read.resolution;
    if (gone()) return CANCELLED;
    // What the run just read is the freshest state: show it, in this owner's client only.
    epoch.run(() => epoch.queryClient.setQueryData(approvalStatusQueryKey(write.approvalId, epoch.ownerKey), read));
    if (write.verifyFailedAt) updateParkedWrite(epoch, id, { verifyFailedAt: undefined });
    if (approval !== "approved") {
      return { kind: "not_run", reason: "not_approved", message: `${write.label} is not approved (${approval}).` };
    }
    const outcome = await executeHubWrite({
      path: write.path,
      method: write.method,
      body: write.body,
      idempotencyKey: write.idempotencyKey,
      expectedContext: write.context,
      signal: controller.signal,
      onDispatch: () => {
        run.dispatched = true;
      },
      // The live context, checked after each whoami read right before each
      // send (also the CSRF retry): the epoch is live, the entry was not
      // discarded, the page still observes the stored owner, and the attempt
      // is recorded in storage (so a reload cannot forget the request left).
      stillValid: () => !epoch.revoked && !gone() && observedReason() === null && recordAttempt(),
    });
    if (outcome.kind === "error" && outcome.aborted) {
      // The owner check first: a revoked epoch also aborts this run's signal.
      const reason = observedReason();
      if (reason) return cancelled(reason);
      if (notRecorded) {
        if (!gone()) updateParkedWrite(epoch, id, { attemptStartedAt: undefined });
        return {
          kind: "not_run",
          reason: "not_recorded",
          message:
            "The attempt could not be recorded in this browser's storage before sending, so it was not sent. Free some storage or use another tab, then try again.",
        };
      }
      return CANCELLED;
    }
    // The response can arrive after the page has started to observe someone
    // else (another tab) or lost its verification: publish nothing then.
    const changedAfterSend = observedReason();
    if (changedAfterSend) return cancelled(changedAfterSend);
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      const message = outcome.kind === "done" ? `${write.label}: done.` : `${write.label}: started.`;
      // Result first, then the removal: if the removal does not land, the
      // recorded result still retires the entry on the next read.
      recordParkedWriteResult(epoch, { owner: write.context, approvalId: write.approvalId, kind: outcome.kind, message, at: Date.now() });
      removeParkedWrite(epoch, id);
      settleDraft(epoch, write);
      await Promise.all(write.invalidates.map((queryKey) => epoch.run(() => epoch.queryClient.invalidateQueries({ queryKey }))));
      // The await above can outlive the epoch or the observed owner: report
      // nothing actionable then.
      const changedAfterInvalidate = observedReason();
      if (changedAfterInvalidate) return cancelled(changedAfterInvalidate);
      return { kind: outcome.kind, data: outcome.data, message };
    }
    if (outcome.kind === "pending_approval") {
      if (outcome.approvalId) {
        updateParkedWrite(epoch, id, {
          approvalId: outcome.approvalId,
          context: outcome.sentContext ?? write.context,
          responseBody: outcome.body,
          lastError: undefined,
          attemptStartedAt: undefined,
          // A definitive answer: even after a Discard during the send, the outcome is known.
          outcome: undefined,
        });
      } else {
        removeParkedWrite(epoch, id);
      }
      return {
        kind: "pending_approval",
        approvalId: outcome.approvalId,
        message: `${write.label} needs another approval before it can run.`,
      };
    }
    if (outcome.kind === "error" && outcome.contextUnverified) {
      // Not a mismatch: nothing is known to differ, so the entry is kept. A
      // refusal after the first request left (the CSRF retry's whoami read
      // failed) was dispatched: it records outcome unknown, never "not run".
      if (run.dispatched) return cancelled("unverified");
      updateParkedWrite(epoch, id, { lastError: outcome.message, attemptStartedAt: undefined });
      return { kind: "cancelled", reason: "unverified", dispatched: false, message: outcome.message };
    }
    if (outcome.kind === "cancelled") return cancelled(outcome.reason);
    if (outcome.contextMismatch || outcome.status === 401) {
      const reason = outcome.status === 401 ? "signed_out" : "context_mismatch";
      const message =
        reason === "signed_out"
          ? `${write.label} was discarded: the session has ended. Sign in and submit the form again.`
          : `${write.label} was discarded: it was made by another identity, organization or hub than the one signed in now.`;
      recordParkedWriteResult(epoch, { owner: write.context, approvalId: write.approvalId, kind: "discarded", message, at: Date.now() });
      removeParkedWrite(epoch, id);
      // A 401 ends the sign-in: after this owner's own bookkeeping, a new auth
      // generation revokes the epoch and whoami is read afresh.
      if (outcome.status === 401 && !epoch.revoked) bumpAuthGeneration();
      return { kind: "discarded", reason, message };
    }
    if (outcome.aborted) {
      return { kind: "not_run", reason: "cancelled", message: "Cancelled before it was sent." };
    }
    if (outcome.outcomeUnknown) {
      updateParkedWrite(epoch, id, { outcome: "unknown", lastError: outcome.message });
      recordParkedWriteResult(epoch, { owner: write.context, approvalId: write.approvalId, kind: "outcome_unknown", message: outcome.message, at: Date.now() });
      return { kind: "outcome_unknown", message: outcome.message };
    }
    updateParkedWrite(epoch, id, { lastError: outcome.message, attemptStartedAt: undefined });
    recordParkedWriteResult(epoch, { owner: write.context, approvalId: write.approvalId, kind: "error", message: outcome.message, at: Date.now() });
    return { kind: "error", status: outcome.status, message: outcome.message };
  } finally {
    epoch.signal.removeEventListener("abort", forwardAbort);
    running.delete(id);
    emit();
  }
}

/** The entry, if the epoch is live and it belongs to the epoch's owner (checked synchronously). */
function ownEntry(epoch: OwnerEpoch, id: string): ParkedWrite | undefined {
  if (epoch.revoked || epoch.context === null) return undefined;
  const write = find(epoch, id);
  return write && isOwnedBy(write.context, epoch.context) ? write : undefined;
}

/**
 * After an outcome unknown, the person checked and the change is there
 * ("It is there"): the entry is retired and recorded as done, confirmed by
 * the person, not by the hub. Nothing is sent. Refused while a run of the
 * entry is in flight in this page, for a revoked epoch, and for an entry of
 * another owner. `done` is returned only once the retirement is verified in
 * storage; otherwise `not_run`/`not_recorded`.
 */
export function confirmApplied(epoch: OwnerEpoch, id: string): RunParkedWriteResult {
  if (epoch.revoked) return { kind: "not_run", reason: "cancelled", message: CANCEL_MESSAGES.owner_changed };
  if (running.has(id)) return { kind: "not_run", reason: "busy", message: "This is still being sent." };
  const write = ownEntry(epoch, id);
  if (!write) return { kind: "not_run", reason: "missing", message: "This stored change no longer exists." };
  const message = `${write.label}: you confirmed it was made.`;
  recordParkedWriteResult(epoch, { owner: write.context, approvalId: write.approvalId, kind: "done", message, at: Date.now() });
  removeParkedWrite(epoch, id);
  settleDraft(epoch, write);
  if (listParkedWrites(epoch).some((w) => w.id === id)) {
    return {
      kind: "not_run",
      reason: "not_recorded",
      message: "This could not be recorded in this browser's storage, so it is still listed. Free some storage, then try again.",
    };
  }
  return { kind: "done", data: null, message, confirmedByPerson: true };
}

/**
 * The person checked and the change was not made: the entry may run again.
 * True only when the epoch is live, the entry is its owner's and storage
 * confirmed the change.
 */
export function confirmNotApplied(epoch: OwnerEpoch, id: string): boolean {
  if (!ownEntry(epoch, id)) return false;
  return updateParkedWrite(epoch, id, { outcome: undefined, lastError: undefined, attemptStartedAt: undefined });
}
