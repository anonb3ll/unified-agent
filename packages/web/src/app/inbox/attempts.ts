import { useCallback, useSyncExternalStore } from "react";
import type { WriteOutcome } from "../lib/mutations";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";

/**
 * Attempts of writes that must not be repeated blindly (one that creates a
 * task, files or dismisses a proposal, decides an approval). The attempt is
 * stored before the request is sent and removed only when the hub's answer
 * arrives. If the person leaves (or the tab reloads) while the request is in
 * flight and the answer never arrives here, the stored attempt says so on
 * return: the outcome is unknown, and the page asks them to check before
 * trying again instead of offering a silent resend.
 *
 * Attempts live in the owner epoch's storage: nothing is read or written
 * without a verified owner, or once the epoch is revoked.
 */
export type Attempt = { label: string; at: number };

const PREFIX = "ua-cc2:attempt:";
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function readAttempt(epoch: OwnerEpoch, key: string): Attempt | null {
  return epoch.storage.read<Attempt | null>(PREFIX + key, null);
}

export function recordAttempt(epoch: OwnerEpoch, key: string, label: string): void {
  epoch.storage.write(PREFIX + key, { label, at: Date.now() } satisfies Attempt);
  emit();
}

export function clearAttempt(epoch: OwnerEpoch, key: string): void {
  epoch.storage.remove(PREFIX + key);
  emit();
}

/** The hub answered (or nothing was sent), so the attempt's outcome is known. */
export function outcomeKnown(outcome: WriteOutcome<unknown>): boolean {
  if (outcome.kind === "cancelled") return !outcome.dispatched;
  if (outcome.kind === "error") return !("outcomeUnknown" in outcome && outcome.outcomeUnknown);
  return true;
}

/**
 * Run `send` with its attempt recorded first. The attempt is cleared when the
 * outcome is known, and kept (outcome unknown) when it is not.
 */
export async function withAttempt<T>(epoch: OwnerEpoch, key: string, label: string, send: () => Promise<WriteOutcome<T>>): Promise<WriteOutcome<T>> {
  recordAttempt(epoch, key, label);
  const outcome = await send();
  if (outcomeKnown(outcome as WriteOutcome<unknown>)) clearAttempt(epoch, key);
  return outcome;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * The stored attempt for `key` that this page did not start (an attempt
 * whose request is in flight here is not "unknown"). `running` is whether
 * this page has a request for it in flight now.
 */
export function useUnknownAttempt(key: string | null, running: boolean) {
  const epoch = useEpoch();
  const raw = useSyncExternalStore(
    subscribe,
    () => (key ? JSON.stringify(readAttempt(epoch, key)) : "null"),
    () => "null",
  );
  const attempt = running ? null : (JSON.parse(raw) as Attempt | null);
  const dismiss = useCallback(() => {
    if (key) clearAttempt(epoch, key);
  }, [epoch, key]);
  return { attempt, dismiss };
}
