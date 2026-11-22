import { useCallback, useEffect, useRef, useState } from "react";
import { useEpoch, type OwnerEpoch } from "./epoch";
import type { IdentityState } from "./identity";

/**
 * Form drafts (spec §6). A draft is keyed by the signed-in identity, the
 * form's operation and the object's identity, including the query
 * parameters that select the object, for example
 * `["Desktop_Agent1","memory.edit",[["note","<key>"]]]`. It is restored only on an exact
 * key match and cleared on success and on sign-out.
 *
 * Drafts live in the owner epoch's storage (`epoch.storage`), so they are
 * scoped to the verified owner as well: nothing is read or written without
 * one, or once the epoch is revoked (a form unmounting after sign-out or an
 * owner change writes nothing back).
 *
 * Never put a token, password or credential in a draft: pass those fields in
 * `omit` so they are dropped before anything is written.
 */
const PREFIX = "draft:";

export type DraftKeyParts = {
  identity: string | null | undefined;
  /** What the form does: "task.create", "memory.edit". */
  operation: string;
  /** Which object: "", "id=42", "note=projects/alpha". Include every selecting query param. */
  object?: string | Record<string, string | number | null | undefined>;
};

/**
 * The storage key: a canonical JSON tuple `[identity, operation, object]`,
 * so no value can be crafted to collide with another (`{note: "a&tag=b"}`
 * and `{note: "a", tag: "b"}` differ). Object params are sorted, and empty
 * ones are dropped, so the same object always gives the same key.
 */
export function draftKey({ identity, operation, object }: DraftKeyParts): string {
  let objectPart: string | [string, string][] = "";
  if (typeof object === "string") objectPart = object;
  else if (object) {
    objectPart = Object.keys(object)
      .sort()
      .filter((k) => object[k] !== null && object[k] !== undefined && object[k] !== "")
      .map((k) => [k, String(object[k])] as [string, string]);
  }
  return JSON.stringify([identity ?? null, operation, objectPart]);
}

type Stored<T> = { key: string; value: T; savedAt: number };

export function readDraft<T>(epoch: OwnerEpoch, key: string): T | null {
  const stored = epoch.storage.read<Stored<T> | null>(PREFIX + key, null);
  // Exact match only: the stored key must equal the requested one.
  return stored && stored.key === key ? stored.value : null;
}

/** True only when storage confirmed it (false when revoked, without an owner, or refused). */
export function saveDraft<T>(epoch: OwnerEpoch, key: string, value: T): boolean {
  return epoch.storage.write(PREFIX + key, { key, value, savedAt: Date.now() } satisfies Stored<T>);
}

export function clearDraft(epoch: OwnerEpoch, key: string): boolean {
  return epoch.storage.remove(PREFIX + key);
}

/** JSON with sorted object keys, so two equal draft values always compare equal. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "undefined";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const obj = value as Record<string, unknown>;
  return `{${Object.keys(obj)
    .sort()
    .map((k) => `${JSON.stringify(k)}:${canonicalJson(obj[k])}`)
    .join(",")}}`;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/**
 * Whether a draft value still holds what was submitted from it. An object
 * snapshot is compared field by field over its own fields only, so a form
 * may keep bookkeeping beside the submitted fields (an approval id, an
 * attempt key) without counting as an edit; anything else is compared whole.
 */
export function draftHoldsSnapshot(draft: unknown, snapshot: unknown): boolean {
  if (isPlainObject(snapshot) && isPlainObject(draft)) {
    return Object.keys(snapshot).every((k) => k in draft && canonicalJson(draft[k]) === canonicalJson(snapshot[k]));
  }
  return canonicalJson(draft) === canonicalJson(snapshot);
}

/**
 * Clear the draft under `key` only while its stored value still holds
 * `snapshot` (`draftHoldsSnapshot`): text typed after the submit is never
 * deleted. "cleared" when it was removed, "kept" when it holds newer text,
 * "none" when nothing is stored (or the epoch is revoked).
 */
export function clearDraftIfUnchanged(epoch: OwnerEpoch, key: string, snapshot: unknown): "cleared" | "kept" | "none" {
  if (epoch.revoked || !key) return "none";
  const stored = readDraft<unknown>(epoch, key);
  if (stored === null) return "none";
  if (!draftHoldsSnapshot(stored, snapshot)) return "kept";
  return clearDraft(epoch, key) ? "cleared" : "kept";
}

/** Bumped by `clearAllDrafts`, so a form unmounting after sign-out does not write its draft back. */
let draftEpoch = 0;

/** Mounted `useDraft` hooks, told to drop their text when all drafts are cleared. */
const clearListeners = new Set<() => void>();

/**
 * Sign-out: drop every draft in this tab, and the text and pending saves of
 * every mounted form, so nothing typed under this identity is written back.
 */
export function clearAllDrafts(epoch: OwnerEpoch): void {
  draftEpoch += 1;
  for (const key of epoch.storage.keys(PREFIX)) epoch.storage.remove(key);
  for (const listener of clearListeners) listener();
}

export type UseDraftOptions<T> = Omit<DraftKeyParts, "identity"> & {
  /**
   * The signed-in identity: pass `useIdentity()` (preferred), or the label
   * (`null` for a genuinely anonymous form, `undefined` while it is not known
   * yet). Nothing is read or written while it is not known.
   */
  identity: string | null | undefined | IdentityState;
  initial: T;
  /** Field names never written to storage (tokens, credentials). */
  omit?: readonly (keyof T)[];
  /** Write delay after the last change, in ms. */
  debounceMs?: number;
};

type Phase =
  /** Mounted before the identity was known: the only state text may be adopted from. */
  | "initial-unresolved"
  | "resolved"
  /** Unknown again, or unknown after a reset: text typed now is never adopted. */
  | "unresolved";

type DraftState<T> = { key: string | null; form: string; epoch: number; phase: Phase; value: T; restored: boolean };

/**
 * Form state that survives navigation (spec §6). Returns the value, a
 * setter, a `clear` to call after the write is done, whether a draft was
 * restored, and the identity state.
 *
 * A draft belongs to one identity, one operation and one object. Text typed
 * before the identity is known is adopted under the real key only if the
 * form mounted that way and nothing changed since (same operation and
 * object, no sign-out). Any later change of identity, operation, object or
 * sign-out resets the text: the previous identity's draft stays under its
 * own key, and the new one's is restored.
 */
export function useDraft<T extends Record<string, unknown>>(options: UseDraftOptions<T>) {
  const epoch = useEpoch();
  const { initial, omit = [], debounceMs = 300, operation, object } = options;
  const who: IdentityState =
    options.identity !== null && typeof options.identity === "object"
      ? options.identity
      : { status: options.identity === undefined ? "loading" : "ready", identity: options.identity, retry: () => {} };
  const known = who.status === "ready";
  const key: string | null = known ? draftKey({ identity: who.identity ?? null, operation, object }) : null;
  // The form's identity without the person: operation and object.
  const form = draftKey({ identity: "", operation, object });

  const fresh = (phase: Phase): DraftState<T> => {
    const restored = key === null ? null : readDraft<Partial<T>>(epoch, key);
    return { key, form, epoch: draftEpoch, phase, value: restored ? { ...initial, ...restored } : initial, restored: Boolean(restored) };
  };
  const [state, setState] = useState<DraftState<T>>(() =>
    fresh(key !== null ? "resolved" : who.status === "failed" ? "unresolved" : "initial-unresolved"),
  );
  const omitRef = useRef(omit);
  omitRef.current = omit;
  const dirty = useRef(false);
  /** The latest unsaved value, so leaving the form (unmount or another key) can write it at once. */
  const pending = useRef<{ key: string; value: Record<string, unknown>; epoch: number } | null>(null);

  if (who.status === "failed" && state.phase === "initial-unresolved") {
    // A failed identity read ends the chance to adopt text typed so far: it
    // could be adopted under whoever signs in next. Reset the buffer.
    dirty.current = false;
    setState({ ...state, phase: "unresolved", value: initial, restored: false });
  } else if (state.key !== key || state.form !== form || state.epoch !== draftEpoch) {
    const sameForm = state.form === form && state.epoch === draftEpoch;
    if (sameForm && state.key === null && key !== null && state.phase === "initial-unresolved" && dirty.current) {
      // The identity arrived after the person started typing on a fresh form:
      // keep the text and save it under the real key.
      setState({ ...state, key, phase: "resolved", restored: false });
    } else {
      dirty.current = false;
      if (state.epoch !== draftEpoch) pending.current = null;
      setState(fresh(key === null ? "unresolved" : "resolved"));
    }
  }

  // Sign-out while mounted: drop the text and anything not yet saved.
  useEffect(() => {
    const onClear = () => {
      dirty.current = false;
      pending.current = null;
      setState((prev) => ({ ...prev, epoch: draftEpoch, value: initial, restored: false }));
    };
    clearListeners.add(onClear);
    return () => {
      clearListeners.delete(onClear);
    };
    // `initial` is a fresh object per render in most callers.
  }, []);

  useEffect(() => {
    if (!dirty.current || key === null || state.key !== key || state.epoch !== draftEpoch) return;
    const copy: Record<string, unknown> = { ...state.value };
    for (const field of omitRef.current) delete copy[field as string];
    pending.current = { key, value: copy, epoch: draftEpoch };
    const timer = setTimeout(() => {
      const last = pending.current;
      pending.current = null;
      if (last && last.epoch === draftEpoch) saveDraft(epoch, last.key, last.value);
    }, debounceMs);
    return () => clearTimeout(timer);
  }, [state, key, debounceMs]);

  // A full reload or tab close within the save delay would lose the text:
  // write it synchronously when the page is being hidden or unloaded.
  useEffect(() => {
    const flushNow = () => {
      const last = pending.current;
      if (last && last.epoch === draftEpoch) saveDraft(epoch, last.key, last.value);
    };
    window.addEventListener("pagehide", flushNow);
    window.addEventListener("beforeunload", flushNow);
    return () => {
      window.removeEventListener("pagehide", flushNow);
      window.removeEventListener("beforeunload", flushNow);
    };
  }, []);

  // Flush on unmount and when the key changes, unless cleared or signed out since.
  useEffect(
    () => () => {
      const last = pending.current;
      pending.current = null;
      if (last && last.epoch === draftEpoch) saveDraft(epoch, last.key, last.value);
    },
    [key],
  );

  const setValue = useCallback((next: T | ((prev: T) => T)) => {
    dirty.current = true;
    setState((prev) => ({
      ...prev,
      value: typeof next === "function" ? (next as (p: T) => T)(prev.value) : next,
    }));
  }, []);

  const clear = useCallback(() => {
    dirty.current = false;
    pending.current = null;
    if (key !== null) clearDraft(epoch, key);
    setState((prev) => ({ ...prev, value: initial, restored: false }));
    // `initial` is a fresh object per render in most callers; the key is what matters.
  }, [key]);

  return {
    value: state.value,
    setValue,
    clear,
    restored: state.restored,
    /** The storage key; "" while the identity is not known. */
    key: key ?? "",
    /** True while the identity is loading: typed text is kept but not stored yet. */
    identityPending: who.status === "loading",
    /** True when the identity read failed: nothing is stored; show it and offer `retryIdentity`. */
    identityFailed: who.status === "failed",
    retryIdentity: who.retry,
  };
}
