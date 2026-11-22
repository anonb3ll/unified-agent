/**
 * The start guard: one record per attempt to start work, shared by every tab
 * of this browser.
 *
 * Running a workflow, starting or continuing a graph run, starting a cycle,
 * creating a routine and a routine's Run now launch real work. If the answer
 * to such a POST is lost, the browser cannot prove from lists whether it
 * started, and only routine Run now has a hub-side idempotency key. So:
 *
 * - the record lives in the owner epoch's shared (local) storage, keyed by
 *   hub origin, organization, verified identity, operation and object, so
 *   every tab of this browser sees the same hold (`storage` events tell
 *   other tabs at once); nothing is read or written without a verified
 *   owner or once the epoch is revoked;
 * - a start runs inside a Web Lock on that same key (`navigator.locks`), so
 *   two tabs cannot dispatch the same operation at once, and the record is
 *   written and read back BEFORE the POST. If it cannot be stored, or the
 *   browser has no Web Locks, nothing is sent;
 * - the attempt id is the `Idempotency-Key` where the hub accepts one;
 * - while an outcome is unknown, another start is refused until the person
 *   picks a matching run, or confirms a new start knowing it may duplicate;
 * - an explicit sign-out clears this owner's Runs attempt records: the
 *   shell calls `clearAllAttempts` under the epoch the sign-out started in,
 *   before its own request. Nothing secret is stored here.
 *
 * The hold covers this browser only, not other browsers or devices.
 */
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { authGeneration } from "../lib/authGeneration";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import type { WriteContext } from "../lib/writeContext";

export type AttemptKind = "workflow" | "graph" | "graph-continue" | "cycle" | "routine-run" | "routine-add";

export type StartAttempt = {
  /** Unique per attempt; also the `Idempotency-Key` where the hub honours one. */
  id: string;
  kind: AttemptKind;
  /** What it starts, in words: "Workflow smoke-echo". */
  summary: string;
  /** Browser clock, ms. */
  sentAt: number;
  /**
   * `sending`: on the wire (unknown once no tab holds its lock). `unknown`:
   * the answer was lost or inconclusive. `parked`: the hub parked it for
   * approval and the same key must be used again.
   */
  state: "sending" | "unknown" | "parked";
  /** What identifies the work it would create. */
  match: { name?: string; taskId?: number; routineId?: number; runId?: string; title?: string };
  approvalId?: string | null;
  /** Why the outcome is not known, in plain words, when the hub said so. */
  note?: string;
};

export const NO_GUARD_MESSAGE =
  "This browser cannot record the start, so a lost answer could not be told apart from a start that never happened. Nothing was sent. Use a browser with storage and Web Locks enabled.";

const PREFIX = "ua-cc2:runs-attempt:";
const listeners = new Set<() => void>();
const cache = new Map<string, { raw: string | null; value: StartAttempt | null }>();

function emit() {
  for (const l of listeners) l();
}

if (typeof window !== "undefined") {
  // The stored key carries the owner scope in front of the guard's own key.
  window.addEventListener("storage", (event) => {
    if (event.key === null || event.key.includes(PREFIX)) emit();
  });
}

/** Sign-out: clear this owner's Runs attempt records. Call it under the epoch the sign-out started in. */
export function clearAllAttempts(epoch: OwnerEpoch): void {
  for (const key of epoch.sharedStorage.keys(PREFIX)) epoch.sharedStorage.remove(key);
  emit();
}

/** The storage and lock key for one operation on one object, under one hub, organization and identity. */
export function guardKey(context: WriteContext, kind: AttemptKind, object = ""): string {
  return PREFIX + JSON.stringify([context.hubOrigin, context.organization, context.identity, kind, object]);
}

export function readAttempt(epoch: OwnerEpoch, key: string): StartAttempt | null {
  const raw = epoch.sharedStorage.readText(key);
  const cached = cache.get(key);
  if (cached && cached.raw === raw) return cached.value;
  let value: StartAttempt | null = null;
  try {
    value = raw ? (JSON.parse(raw) as StartAttempt) : null;
  } catch {
    value = null;
  }
  cache.set(key, { raw, value });
  return value;
}

/** Write and read back. False when the record could not be stored (or the epoch is revoked). */
export function writeAttempt(epoch: OwnerEpoch, key: string, attempt: StartAttempt): boolean {
  const ok = epoch.sharedStorage.write(key, attempt);
  emit();
  return ok;
}

/** A record that cannot be removed keeps holding: the safe side. */
export function removeAttempt(epoch: OwnerEpoch, key: string): void {
  epoch.sharedStorage.remove(key);
  emit();
}

export function newAttemptId(kind: AttemptKind): string {
  const random =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `cc2-${kind}-${random}`;
}

/** Allow for clock skew between this browser and the hub. */
export const SKEW_MS = 2 * 60_000;

export function startedSince(value: string | null | undefined, sentAt: number): boolean {
  if (!value) return false;
  const t = Date.parse(value);
  return Number.isFinite(t) && t >= sentAt - SKEW_MS;
}

/**
 * Whether an error outcome leaves it unknown if the work started: the
 * request went out and its answer was lost (`outcomeUnknown`), or the hub
 * failed part way (5xx).
 */
export function isAmbiguousFailure(outcome: { status: number | null; outcomeUnknown?: boolean }): boolean {
  return outcome.outcomeUnknown === true || (outcome.status !== null && outcome.status >= 500);
}

/** What to do with the record after an answer. */
export type Settle = { record: "clear" } | { record: "unknown"; note?: string } | { record: "parked"; approvalId: string | null; note?: string };

/**
 * The settle rule shared by every start: success and definite refusals
 * clear; a lost, 5xx or dispatched-then-cancelled answer stays unknown.
 * Pages refine it (an `indeterminate` workflow, a routine `in_flight`).
 */
export function defaultSettle(outcome: { kind: string; status?: number | null; outcomeUnknown?: boolean; dispatched?: boolean } | null): Settle {
  if (!outcome) return { record: "clear" };
  if (outcome.kind === "cancelled") return outcome.dispatched ? { record: "unknown", note: "The page stopped waiting after the request had left." } : { record: "clear" };
  if (outcome.kind === "error") return isAmbiguousFailure({ status: outcome.status ?? null, outcomeUnknown: outcome.outcomeUnknown }) ? { record: "unknown" } : { record: "clear" };
  return { record: "clear" };
}

export type GuardRefusal = "no-guard" | "busy" | "held" | "storage" | "no-identity";

export const REFUSAL_TEXT: Record<GuardRefusal, string> = {
  "no-guard": NO_GUARD_MESSAGE,
  storage: NO_GUARD_MESSAGE,
  busy: "Another tab of this browser is starting this right now. Wait for its answer.",
  held: "An earlier start is not confirmed yet. Decide about it before starting again.",
  "no-identity": "Checking who is signed in. Try again in a moment.",
};

type LockManagerLike = {
  request: (name: string, options: { ifAvailable?: boolean }, callback: (lock: unknown) => Promise<unknown>) => Promise<unknown>;
  query?: () => Promise<{ held?: { name?: string }[] }>;
};

function locks(): LockManagerLike | null {
  if (typeof navigator === "undefined") return null;
  const l = (navigator as Navigator & { locks?: LockManagerLike }).locks;
  return l && typeof l.request === "function" ? l : null;
}

export type GuardRun<O> = { sent: true; outcome: O; attempt: StartAttempt } | { sent: false; refusal: GuardRefusal };

/**
 * One operation's guard. `run` sends at most once per call, under the lock,
 * after the record is stored and verified.
 */
export function useStartGuard(context: WriteContext | null, kind: AttemptKind, object = "") {
  const epoch = useEpoch();
  const key = context && context.identity ? guardKey(context, kind, object) : null;
  const attempt = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => (key ? readAttempt(epoch, key) : null),
    () => null,
  );
  const [inFlight, setInFlight] = useState<string | null>(null);
  const [elsewhere, setElsewhere] = useState(false);

  // A `sending` record this tab is not waiting for: another tab may hold the
  // lock (still waiting), or nobody does (its answer is lost).
  const foreignSending = attempt?.state === "sending" && attempt.id !== inFlight;
  useEffect(() => {
    if (!foreignSending || !key) {
      setElsewhere(false);
      return;
    }
    const l = locks();
    let stop = false;
    const check = async () => {
      const state = l?.query ? await l.query().catch(() => null) : null;
      if (!stop) setElsewhere(Boolean(state?.held?.some((h) => h.name === key)));
    };
    void check();
    const t = setInterval(() => void check(), 2_000);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [foreignSending, key]);

  const run = useCallback(
    async <O>(
      draft: { summary: string; match: StartAttempt["match"] },
      send: (attemptId: string) => Promise<O>,
      settle: (outcome: O) => Settle,
      options: { replaceHeld?: string; reuseId?: string } = {},
    ): Promise<GuardRun<O>> => {
      if (!key) return { sent: false, refusal: "no-identity" };
      const l = locks();
      if (!l) return { sent: false, refusal: "no-guard" };
      const generation = authGeneration();
      let result: GuardRun<O> = { sent: false, refusal: "busy" };
      await l.request(key, { ifAvailable: true }, async (lock) => {
        if (!lock) return;
        const existing = readAttempt(epoch, key);
        const replacing = existing && (existing.id === options.replaceHeld || existing.id === options.reuseId);
        if (existing && !replacing) {
          result = { sent: false, refusal: "held" };
          return;
        }
        const attempt: StartAttempt = {
          id: options.reuseId ?? newAttemptId(kind),
          kind,
          summary: draft.summary,
          sentAt: Date.now(),
          state: "sending",
          match: draft.match,
        };
        if (!writeAttempt(epoch, key, attempt)) {
          // Leave whatever is stored untouched: an earlier attempt keeps holding
          // (removing it could release a start whose answer is still unknown).
          result = { sent: false, refusal: "storage" };
          return;
        }
        setInFlight(attempt.id);
        let outcome: O;
        try {
          outcome = await send(attempt.id);
        } finally {
          setInFlight(null);
        }
        result = { sent: true, outcome, attempt };
        // Sign-out began while it was in flight: the records are gone; write nothing back.
        if (authGeneration() !== generation) return;
        const s = settle(outcome);
        if (s.record === "clear") removeAttempt(epoch, key);
        else if (s.record === "unknown") writeAttempt(epoch, key, { ...attempt, state: "unknown", ...(s.note ? { note: s.note } : {}) });
        else writeAttempt(epoch, key, { ...attempt, state: "parked", approvalId: s.approvalId, ...(s.note ? { note: s.note } : {}) });
      });
      return result;
    },
    [epoch, key, kind],
  );

  /**
   * Forget the attempt `expectedId` (the person picked its run, or the hub
   * answered for it). Done under the same lock as a start, and only when the
   * stored record is still that attempt: a late answer for an older attempt
   * never releases a newer one.
   */
  const resolve = useCallback(
    async (expectedId: string): Promise<boolean> => {
      const l = locks();
      if (!key || !l) return false;
      let done = false;
      await l.request(key, { ifAvailable: false }, async () => {
        if (readAttempt(epoch, key)?.id === expectedId) {
          removeAttempt(epoch, key);
          done = true;
        }
      });
      return done;
    },
    [epoch, key],
  );

  /** The hold this tab must respect: a record, unless it is this tab's own request on the wire. */
  const held = attempt !== null && !(attempt.state === "sending" && attempt.id === inFlight);

  return {
    key,
    attempt,
    held,
    /** Another tab is waiting for this attempt's answer right now. */
    pendingElsewhere: elsewhere,
    inFlight: inFlight !== null,
    available: locks() !== null,
    run,
    resolve,
  };
}

export type StartGuard = ReturnType<typeof useStartGuard>;
