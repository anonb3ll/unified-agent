import type { WhoamiPayload } from "../../lib/api/auth";

/**
 * The context a write is made under (spec §6): the hub origin that answered
 * whoami, the organization and the verified identity. An approval replay is
 * sent only when the fresh context equals the stored one.
 */
export type WriteContext = {
  hubOrigin: string;
  /** Verified organization identifier from whoami; null while the hub does not report one. */
  organization: string | null;
  identity: string | null;
};

/**
 * The organization the signed-in session belongs to, from `/api/whoami`.
 *
 * THE ONLY PLACE TO CHANGE when the hub's `org` field lands (hub task #2469,
 * which adds `org` with an explicit identifier for unpartitioned mode).
 * Until then the hub sends none, this returns null, and replay stays
 * suspended (`replaySuspendedReason` → "organization_unknown").
 */
export function organizationFromWhoami(whoami: WhoamiPayload | null | undefined): string | null {
  const org = (whoami as Record<string, unknown> | null | undefined)?.org;
  if (typeof org === "string" && org) return org;
  if (org && typeof org === "object") {
    const id = (org as Record<string, unknown>).id;
    if (typeof id === "string" && id) return id;
  }
  return null;
}

export function contextFromWhoami(whoami: WhoamiPayload | null | undefined): WriteContext {
  return {
    hubOrigin: typeof window === "undefined" ? "" : window.location.origin,
    organization: organizationFromWhoami(whoami),
    identity: whoami?.agent ?? null,
  };
}

export type ReplaySuspendedReason = "signed_out" | "context_mismatch" | "organization_unknown";

/**
 * Why a stored write may not be replayed under `current`, or null when it
 * may. Origin, identity and a verified organization must be present on both
 * sides and equal; a missing context never matches.
 */
export function replaySuspendedReason(
  stored: WriteContext | null | undefined,
  current: WriteContext | null | undefined,
): ReplaySuspendedReason | null {
  if (!current) return "signed_out";
  if (!stored) return "context_mismatch";
  if (!stored.hubOrigin || !current.hubOrigin || stored.hubOrigin !== current.hubOrigin) return "context_mismatch";
  if (!stored.identity || !current.identity || stored.identity !== current.identity) return "context_mismatch";
  if (!stored.organization || !current.organization) return "organization_unknown";
  return stored.organization === current.organization ? null : "context_mismatch";
}

/**
 * The same signed-in session, for ordinary writes: same hub origin, same
 * identity and the same organization, where "no organization reported" on
 * both sides counts as the same (unlike `sameContext`, which needs a
 * verified organization before a stored write may run).
 */
export function sameSession(a: WriteContext | null | undefined, b: WriteContext | null | undefined): boolean {
  if (!a || !b) return false;
  return a.hubOrigin === b.hubOrigin && a.identity === b.identity && a.organization === b.organization;
}

export function sameContext(stored: WriteContext | null | undefined, current: WriteContext | null | undefined): boolean {
  return replaySuspendedReason(stored, current) === null;
}

/**
 * Why a parked write cannot be run again from storage (spec §6: after
 * approval the repeat is always a person's click), or null when it can.
 * - `not_resumable`: the page opted out (`resumable: false`); nothing is stored.
 * - `not_stored`: browser storage refused the entry (confirmed by a
 *   read-back), so it cannot be run from the store.
 * - `no_approval_reference`: the hub parked it without a usable top-level
 *   approval id (for example an autonomy-review pause), so nothing can be
 *   tracked or promised.
 * - the `ReplaySuspendedReason` values: the stored context cannot be matched.
 * A page shows "Run it now" (from the store or the Inbox) for null, else
 * "After it is approved, submit this form again".
 */
export type ReplayBlockedReason = "not_resumable" | "no_approval_reference" | "not_stored" | ReplaySuspendedReason;

export type ReplayVerdict = {
  /** Always false: nothing is ever re-sent without a person's click. Kept for callers that read it. */
  willReplay: false;
  /** True when the stored write can be run from the store (or the Inbox) once approved. */
  canRunFromStore: boolean;
  reason: ReplayBlockedReason | null;
};

export function replayVerdict(
  resumable: boolean,
  stored: WriteContext | null,
  current: WriteContext | null,
  approvalId: string | null = "known",
): ReplayVerdict {
  if (!resumable) return { willReplay: false, canRunFromStore: false, reason: "not_resumable" };
  if (approvalId === null) return { willReplay: false, canRunFromStore: false, reason: "no_approval_reference" };
  const reason = replaySuspendedReason(stored, current);
  return { willReplay: false, canRunFromStore: reason === null, reason };
}
