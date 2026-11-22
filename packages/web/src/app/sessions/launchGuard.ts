/**
 * The launch guard (review rounds 1 and 2): every operation that creates a
 * new job goes through it. That covers starting a session (hub chooses,
 * chosen agent, device), "Start again" on a device job, and a team restore
 * or its continuation.
 *
 * CONTRACT
 *
 * 1. One attempt id per attempt, generated once by `begin`, written to
 *    `localStorage` SYNCHRONOUSLY before the POST and sent as
 *    `Idempotency-Key`, together with a fingerprint of the request it is
 *    bound to (hub #2501 binds the key to the request's digest). A retry of
 *    the same attempt ("Launch it again" after an explicit choice) reuses the
 *    id, and only for the same request: the record is `armed`, stored, so a
 *    reload or navigation keeps it (task #2510).
 * 2. The record is keyed by hub origin, organization and verified identity
 *    (the foundation's `WriteContext`), so another identity or hub never
 *    sees it. It is shared by every tab of that identity (a `storage`
 *    listener keeps them in step), because a second tab launching the same
 *    thing is exactly the risk. It holds only the attempt id, the operation,
 *    the target, task ids, a short label (≤ 80 characters) and the time: no
 *    prompt text.
 * 3. States: `sending` (a request is on the wire; while this tab sends, or
 *    another tab sent less than `OTHER_TAB_GRACE_MS` ago), `parked` (waits
 *    for an approval), `unresolved` (the answer was lost: `outcomeUnknown`,
 *    a 5xx, a reload or a closed tab during the send, or a refusal of a
 *    keyed launch, which the hub may have refused after creating its task),
 *    `armed` (the person or the hub's answer says to send it again with the
 *    same key). Only a definite answer clears the record: the request
 *    started, the hub's attempt lookup settles it, it never left the browser
 *    on its first send, or the person explicitly starts something new.
 * 4. While a record exists, no other launch starts. An unresolved record is
 *    resolved by the hub when it can say (`GET /api/launch-attempts/{key}`,
 *    hub task #2501), else ONLY by the person's explicit choice ("One of
 *    these is it" or "I have checked and it did not start. Launch again",
 *    behind a confirmation). Nothing in the browser concludes "it did not
 *    start" by itself, and every record can always be resolved by those
 *    choices, so none blocks launches forever.
 * 5. Sign-out clears it: the shell's sign-out drops this owner's records
 *    (`clearLaunchRecords`) under the epoch it started in, before its own
 *    request, and a record of another identity is never read. Records live
 *    in the owner epoch's shared (local) storage, so nothing is read or
 *    written without a verified owner or once the epoch is revoked.
 */
import { useCallback, useSyncExternalStore } from "react";
import type { WhoamiPayload } from "../../lib/api/auth";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import { contextFromWhoami, type WriteContext } from "../lib/writeContext";
import { newKey } from "./model";

export type LaunchOp = "dispatch" | "stack" | "device" | "restart" | "restore";

export type LaunchRecord = {
  /** The attempt id, sent as `Idempotency-Key`. */
  key: string;
  op: LaunchOp;
  /** Agent label, node name, project, item id or snapshot id. */
  target: string;
  taskIds: number[];
  /** What was launched, in a few words (≤ 80 characters, no prompt text). */
  label: string;
  sentAt: number;
  state: "sending" | "parked" | "unresolved" | "armed";
  approvalId?: string | null;
  /** The tab that sent it. */
  tab: string;
  /**
   * The request the key is bound to (`requestFingerprint`): set when the
   * record is created and never changed, so the key is only ever sent with
   * that request. Absent on records stored before task #2510: such a
   * record matches no request, so its key is never sent again (task #2532);
   * the hub's lookup or "start something new" settles it.
   */
  fingerprint?: string;
  /** The key was sent before: a later send that never left the browser leaves it `armed`, never dropped. */
  sentBefore?: boolean;
};

/** Launches the hub records under their `Idempotency-Key` (docs/control-center-api.md, "Launches"). */
export const KEYED_OPS: ReadonlySet<LaunchOp> = new Set<LaunchOp>(["dispatch", "stack", "device", "restart"]);

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    return `{${Object.keys(obj)
      .filter((k) => obj[k] !== undefined)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonical(obj[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/**
 * A short, stable digest of a launch request (operation plus its body), so
 * the record can say which request its key belongs to without holding the
 * prompt. It only tells requests apart; the hub's own digest stays the
 * authority (a different request under a used key answers 409 `conflict`).
 */
export function requestFingerprint(op: LaunchOp, request: unknown): string {
  const text = canonical([op, request]);
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let i = 0; i < text.length; i++) {
    const ch = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return `fp-${(h2 >>> 0).toString(16).padStart(8, "0")}${(h1 >>> 0).toString(16).padStart(8, "0")}`;
}

/**
 * Whether a request may be sent under this record's key: the same operation
 * and the same request. A record without a fingerprint (stored before task
 * #2510) matches nothing: the request its key is bound to is unknown, and
 * the hub answers 409 for any other (task #2532).
 */
export function sameLaunchRequest(record: LaunchRecord, op: LaunchOp, fingerprint: string): boolean {
  return record.op === op && record.fingerprint !== undefined && record.fingerprint === fingerprint;
}

/** Whether this record's key may be sent again: only when it says which request it is bound to (task #2532). */
export function canResend(record: LaunchRecord): boolean {
  return record.fingerprint !== undefined;
}

const PREFIX = "ua-cc2:launch-guard|";
/** A send from another tab is trusted to be in flight this long, then treated as unresolved. */
export const OTHER_TAB_GRACE_MS = 60_000;

/** This page load. A record "sending" from a tab that is gone is unresolved. */
const TAB = newKey("tab");
/** Keys this tab has on the wire right now (survives in-app navigation). */
const inFlight = new Set<string>();

export function guardStorageKey(context: WriteContext): string {
  return PREFIX + JSON.stringify([context.hubOrigin, context.organization, context.identity]);
}

function read(epoch: OwnerEpoch, storageKey: string): LaunchRecord | null {
  return epoch.sharedStorage.read<LaunchRecord | null>(storageKey, null);
}

const listeners = new Set<() => void>();
const cache = new Map<string, LaunchRecord | null>();
function emit() {
  for (const l of listeners) l();
}

export function readLaunchRecord(epoch: OwnerEpoch, storageKey: string): LaunchRecord | null {
  if (!cache.has(storageKey)) cache.set(storageKey, read(epoch, storageKey));
  return cache.get(storageKey) ?? null;
}

/**
 * Written synchronously: callers rely on it being stored before the request
 * is sent. A revoked epoch writes nothing; the in-memory copy still guards
 * this tab (the safe side).
 */
export function writeLaunchRecord(epoch: OwnerEpoch, storageKey: string, record: LaunchRecord | null): void {
  if (record) epoch.sharedStorage.write(storageKey, record);
  else epoch.sharedStorage.remove(storageKey);
  cache.set(storageKey, record);
  emit();
}

/** Sign-out: drop this owner's launch records. Call it under the epoch the sign-out started in. */
export function clearLaunchRecords(epoch: OwnerEpoch): void {
  for (const key of epoch.sharedStorage.keys(PREFIX)) epoch.sharedStorage.remove(key);
  cache.clear();
  emit();
}

/** Tests: forget in-memory copies. */
export function resetLaunchGuardCache(): void {
  cache.clear();
  inFlight.clear();
  emit();
}

if (typeof window !== "undefined") {
  // Another tab of this identity started, settled or resolved a launch. The
  // stored key carries the owner scope in front of the guard's own key.
  window.addEventListener("storage", (event) => {
    if (event.key === null || event.key.includes(PREFIX)) {
      cache.clear();
      emit();
    }
  });
}

export type GuardStatus =
  | { kind: "free" }
  | { kind: "loading" }
  | { kind: "sending"; record: LaunchRecord; here: boolean }
  | { kind: "parked"; record: LaunchRecord }
  | { kind: "unresolved"; record: LaunchRecord }
  /** To be sent again with the same key and the same request; nothing else launches meanwhile. */
  | { kind: "armed"; record: LaunchRecord };

export function guardStatus(record: LaunchRecord | null, now: number): GuardStatus {
  if (!record) return { kind: "free" };
  if (record.state === "parked") return { kind: "parked", record };
  // An armed record that cannot be resent (no fingerprint) is unresolved: the lookup or "start something new" settles it.
  if (record.state === "armed" && canResend(record)) return { kind: "armed", record };
  if (record.state === "sending") {
    if (record.tab === TAB && inFlight.has(record.key)) return { kind: "sending", record, here: true };
    if (record.tab !== TAB && now - record.sentAt < OTHER_TAB_GRACE_MS) return { kind: "sending", record, here: false };
  }
  return { kind: "unresolved", record };
}

export type LaunchSpec = {
  op: LaunchOp;
  target: string;
  taskIds?: number[];
  label: string;
  /** `requestFingerprint` of the request body; defaults to one over the operation, target, tasks and label. */
  fingerprint?: string;
};

function specFingerprint(spec: LaunchSpec): string {
  return spec.fingerprint ?? requestFingerprint(spec.op, { target: spec.target, taskIds: spec.taskIds ?? [], label: spec.label });
}

/** How a write ended, as far as the guard cares. */
export type LaunchOutcome =
  | { kind: "done" | "accepted" }
  /** Foundation contract: cancelled by the client (sign-in changed, aborted); `dispatched` when the request had left. */
  | { kind: "cancelled"; dispatched: boolean }
  | { kind: "pending_approval"; approvalId: string | null }
  | {
      kind: "error";
      status: number | null;
      outcomeUnknown?: boolean;
      signedOut?: boolean;
      aborted?: boolean;
      contextMismatch?: boolean;
      contextUnverified?: boolean;
      /** The hub's error text; a launch already in progress for this key answers 409 (hub #2501). */
      detail?: string;
      /** The hub's machine error code (`launch_in_progress`, `launch_unsettled`, `conflict`, ...). */
      code?: string;
    };

/** hub #2501: `409 {code: "launch_in_progress"}` when the same attempt id is still running. */
const IN_PROGRESS = /already in progress/i;

const UNSETTLED_CODES = new Set(["launch_in_progress", "launch_unsettled"]);

/**
 * The record after a request ended. Started: cleared. Parked: waits for its
 * approval. Sent and lost (`outcomeUnknown`) or a 5xx: unresolved, as is a
 * 409 saying the same attempt is still in progress or its start is
 * unsettled (hub #2501). Refused (another 4xx) on a keyed launch:
 * unresolved too (task #2510), because the hub records a refusal as a
 * `failed` attempt that may already hold a delegation or a task, and a
 * repeat with the same key continues it; the attempt lookup settles it.
 * Sign-out began meanwhile (`signedOut`): cleared, never shown (sign-out
 * drops the record anyway). Not sent at all (`aborted`, `contextMismatch`,
 * `contextUnverified`, a network error before sending): cleared when this
 * was the key's first send, otherwise `armed` again, because the earlier
 * send may have created something.
 */
export function nextRecord(current: LaunchRecord, outcome: LaunchOutcome): LaunchRecord | null {
  const notSent = (): LaunchRecord | null => (current.sentBefore ? { ...current, state: "armed" } : null);
  const unresolved: LaunchRecord = { ...current, state: "unresolved" };
  switch (outcome.kind) {
    case "done":
    case "accepted":
      return null;
    case "pending_approval":
      return { ...current, state: "parked", approvalId: outcome.approvalId };
    case "cancelled":
      // Show nothing and do not retry. After the request left it may have
      // started, so it stays uncertain; before, nothing was sent.
      return outcome.dispatched ? unresolved : notSent();
    case "error":
      if (outcome.signedOut) return null;
      if (outcome.outcomeUnknown || (outcome.status !== null && outcome.status >= 500)) return unresolved;
      if (outcome.status === null) return notSent();
      // The hub is still running this attempt: it may start, so it is not settled.
      if (outcome.status === 409 && (UNSETTLED_CODES.has(outcome.code ?? "") || IN_PROGRESS.test(outcome.detail ?? ""))) return unresolved;
      // Refused: on a keyed launch the hub's attempt record decides.
      if (KEYED_OPS.has(current.op)) return unresolved;
      return notSent();
  }
}

/**
 * The guard for the signed-in context. `whoami` undefined means unknown: the
 * guard reports `loading` and refuses to begin.
 */
export function useLaunchGuard(whoami: WhoamiPayload | null | undefined) {
  const epoch = useEpoch();
  const context = whoami ? contextFromWhoami(whoami) : null;
  const storageKey = context?.identity ? guardStorageKey(context) : null;
  const record = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => (storageKey ? readLaunchRecord(epoch, storageKey) : null),
    () => null,
  );

  const status: GuardStatus = !storageKey ? { kind: "loading" } : guardStatus(record, Date.now());

  /**
   * Store the attempt and return its key, or null when a launch may not
   * begin. An `armed` record is sent again with its own key, and only for
   * the same request; any other record blocks.
   */
  const begin = useCallback(
    (spec: LaunchSpec): string | null => {
      if (!storageKey) return null;
      const current = guardStatus(readLaunchRecord(epoch, storageKey), Date.now());
      const fingerprint = specFingerprint(spec);
      if (current.kind === "armed") {
        if (!sameLaunchRequest(current.record, spec.op, fingerprint)) return null;
        const { key } = current.record;
        writeLaunchRecord(epoch, storageKey, {
          ...current.record,
          sentAt: Date.now(),
          state: "sending",
          tab: TAB,
          sentBefore: true,
        });
        inFlight.add(key);
        return key;
      }
      if (current.kind !== "free") return null;
      const key = newKey("launch");
      writeLaunchRecord(epoch, storageKey, {
        key,
        op: spec.op,
        target: spec.target,
        taskIds: spec.taskIds ?? [],
        label: spec.label.slice(0, 80),
        sentAt: Date.now(),
        state: "sending",
        tab: TAB,
        fingerprint,
      });
      inFlight.add(key);
      return key;
    },
    [epoch, storageKey],
  );

  /** Record how the request ended. Works after the page has unmounted (in-app navigation). */
  const settle = useCallback(
    (key: string, outcome: LaunchOutcome) => {
      inFlight.delete(key);
      if (!storageKey) return;
      const current = readLaunchRecord(epoch, storageKey);
      if (!current || current.key !== key) return;
      writeLaunchRecord(epoch, storageKey, nextRecord(current, outcome));
    },
    [epoch, storageKey],
  );

  /**
   * The person chose to send this attempt again, or an approval allows it:
   * keep its key and request, stored, until the send settles. A record that
   * does not say which request its key is bound to is never armed.
   */
  const arm = useCallback(
    (key: string) => {
      if (!storageKey) return;
      const current = readLaunchRecord(epoch, storageKey);
      if (!current || current.key !== key || !canResend(current)) return;
      writeLaunchRecord(epoch, storageKey, { ...current, state: "armed" });
    },
    [epoch, storageKey],
  );

  /**
   * Drop the record and its key. Only for a definite answer (the hub says
   * it started or never had it) or the person's explicit "start something
   * new"; never for a failed lookup.
   */
  const clear = useCallback(() => {
    if (storageKey) writeLaunchRecord(epoch, storageKey, null);
  }, [epoch, storageKey]);

  return { status, record, begin, settle, arm, clear };
}

/**
 * Whether a launch control may be used, and why not. With `spec`, an armed
 * record of that same request does not block it: the control sends it
 * again under its own key.
 */
export function blockedReason(status: GuardStatus, spec?: LaunchSpec): string | null {
  if (status.kind === "armed" && spec && sameLaunchRequest(status.record, spec.op, specFingerprint(spec))) return null;
  switch (status.kind) {
    case "free":
      return null;
    case "loading":
      return "Checking who is signed in…";
    case "sending":
      return status.here ? "A launch is being sent." : "A launch is being sent from another tab.";
    case "parked":
      return "The last launch is waiting for approval. Resolve it on Start a session first.";
    case "unresolved":
      return "It is not known whether the last launch started. Resolve it on Start a session first.";
    case "armed":
      return "The last launch is waiting to be sent again. Send it, or start something new, on Start a session first.";
  }
}

/** Whether "Resolve it" on Start a session applies to this status. */
export function resolvableOnStart(status: GuardStatus): boolean {
  return status.kind === "unresolved" || status.kind === "parked" || status.kind === "armed";
}
