/**
 * Attempt markers for the writes that must not be repeated blindly
 * (connector register, authorize, revoke). A marker is written, and read
 * back to verify it was stored, BEFORE the request is sent, keyed by the
 * signed-in identity, the operation and the object. It is cleared only when
 * the outcome is known: the hub answered (done, parked, refused) or nothing
 * was sent. If the person leaves mid-request, the answer is lost, or the
 * request is cancelled after it left, the marker survives, and the form says
 * the outcome is unknown until the person settles it ("It is there" / "It is
 * not there").
 *
 * Markers live in the owner epoch's storage (`epoch.storage`): nothing is
 * read or written without a verified owner, or once the epoch is revoked, so
 * a marker made by one owner is never shown to, or cleared by, another.
 *
 * A marker holds the request body for the person to check. The bodies of
 * these writes never carry a secret (names, scopes, references only).
 */
import { useCallback, useSyncExternalStore } from "react";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import type { WriteOutcome } from "../lib/mutations";

const PREFIX = "integrations-attempt:";

export type Attempt = { key: string; label: string; body: Record<string, unknown>; startedAt: number };

const listeners = new Set<() => void>();
const emit = () => {
  for (const listener of [...listeners]) listener();
};

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function attemptKey(identity: string, operation: string, object: string): string {
  return JSON.stringify([identity, operation, object]);
}

export function readAttempt(epoch: OwnerEpoch, key: string): Attempt | null {
  const stored = epoch.storage.read<Attempt | null>(PREFIX + key, null);
  return stored && stored.key === key ? stored : null;
}

/** Record the attempt and confirm it was stored. False: nothing may be sent. */
export function recordAttempt(epoch: OwnerEpoch, attempt: Attempt): boolean {
  const written = epoch.storage.write(PREFIX + attempt.key, attempt);
  emit();
  const back = written ? readAttempt(epoch, attempt.key) : null;
  return back !== null && back.startedAt === attempt.startedAt;
}

/** Remove the marker, only while the epoch that recorded it is live. */
export function clearAttempt(epoch: OwnerEpoch, key: string): void {
  epoch.storage.remove(PREFIX + key);
  emit();
}

/** The hub answered (or nothing was sent), so the attempt's outcome is known. */
export function outcomeKnown(outcome: WriteOutcome<unknown>): boolean {
  if (outcome.kind === "cancelled") return !outcome.dispatched;
  if (outcome.kind === "error") return !outcome.outcomeUnknown;
  return true;
}

/**
 * The attempt for one identity, operation and object. `key` is null while
 * the identity is not known: then nothing may be sent. A late identity or
 * another object shows its own marker, never this one's.
 */
export function useAttempt(identity: string | null | undefined, operation: string, object: string) {
  const epoch = useEpoch();
  const key = typeof identity === "string" && identity ? attemptKey(identity, operation, object) : null;
  const raw = useSyncExternalStore(
    subscribe,
    () => (key ? JSON.stringify(readAttempt(epoch, key)) : "null"),
    () => "null",
  );
  const current = JSON.parse(raw) as Attempt | null;
  const record = useCallback(
    (label: string, body: Record<string, unknown>): Attempt | null => {
      if (!key) return null;
      const attempt: Attempt = { key, label, body, startedAt: Date.now() };
      return recordAttempt(epoch, attempt) ? attempt : null;
    },
    [epoch, key],
  );
  const settle = useCallback((which: string) => clearAttempt(epoch, which), [epoch]);
  return { key, current, record, settle };
}
