/**
 * Shared pieces of the Agents area: section tabs, live updates, the inline
 * result of a write (a parked approval is never shown as success, a cancelled
 * write that had left says its outcome is unknown, a coded refusal says what
 * it means), and the draft helpers that keep the three rules of review:
 * state keyed by identity and object, a draft cleared only against the
 * submitted snapshot, and an attempt recorded before the send.
 */
import { useEffect, useRef, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useStreamInvalidation } from "../../lib/polling";
import { LinkTabs, UiLink } from "../../lib/ui";
import { clearDraft, readDraft, saveDraft } from "../lib/drafts";
import type { OwnerEpoch } from "../lib/epoch";
import type { HubWriteState, WriteOutcome } from "../lib/mutations";
import { CANCEL_MESSAGES } from "../lib/mutations";
import { useAreaTabs, type AreaTab } from "../lib/navigation";
import { ParkedWriteNotice, type ParkedWriteContinuation } from "../lib/ParkedWriteNotice";
import { AGENTS_KEYS } from "./api";

export const AGENTS_TABS: AreaTab[] = [
  { href: "/agents", label: "Roster", end: true },
  { href: "/agents/teams", label: "Teams" },
  { href: "/agents/skills", label: "Skills" },
  { href: "/agents/org", label: "Org chart" },
  { href: "/agents/topology", label: "Topology" },
  { href: "/agents/hosts", label: "Hosts" },
  { href: "/agents/capabilities", label: "Capabilities" },
];

export function AgentsTabs() {
  const items = useAreaTabs(AGENTS_TABS);
  return <LinkTabs label="Agents sections" items={items} />;
}

/** Refetch every Agents read when the hub reports agent, node or fleet activity. */
export function useAgentsLiveUpdates() {
  const queryClient = useQueryClient();
  useStreamInvalidation(
    () => {
      void queryClient.invalidateQueries({ queryKey: AGENTS_KEYS.all });
    },
    {
      channels: ["agent.session*", "agent.lifecycle", "agent.lifecycle.*", "agent.node", "agent.fleet.*", "agent.roster"],
      onReconnect: true,
    },
  );
}

/** A labelled section of a page. */
export function Section({ title, id, actions, children, description }: { title: string; id: string; actions?: ReactNode; children: ReactNode; description?: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id={id} className="m-0 flex-1 text-base font-semibold">
          {title}
        </h2>
        {actions}
      </div>
      {description && <p className="m-0 text-sm text-[var(--ua-muted)]">{description}</p>}
      {children}
    </section>
  );
}

export function Notice({ tone = "info", children, role }: { tone?: "info" | "warn" | "danger"; children: ReactNode; role?: "status" | "alert" | "note" }) {
  const border = tone === "danger" ? "border-[var(--ua-danger)]" : tone === "warn" ? "border-[var(--ua-warn)]" : "border-[var(--ua-border-strong)]";
  return (
    <div role={role} className={`rounded-md border border-l-4 ${border} bg-[var(--ua-surface)] px-3 py-2 text-sm text-[var(--ua-fg)]`}>
      {children}
    </div>
  );
}

/** The last outcome a form shows inline, recorded from `run`'s answer (the hook's state lacks `code`). */
export type ShownOutcome =
  | { kind: "none" }
  | { kind: "cancelled"; dispatched: boolean; message: string }
  | { kind: "error"; status: number | null; code?: string; message: string; detail?: string; outcomeUnknown?: boolean };

export function shownFrom<T>(outcome: WriteOutcome<T>, explain?: (code: string | undefined) => string | null): ShownOutcome {
  if (outcome.kind === "cancelled") return { kind: "cancelled", dispatched: outcome.dispatched, message: outcome.message };
  if (outcome.kind === "error") {
    return {
      kind: "error",
      status: outcome.status,
      ...(outcome.code ? { code: outcome.code } : {}),
      message: explain?.(outcome.code) ?? outcome.message,
      ...(outcome.detail ? { detail: outcome.detail } : {}),
      ...(outcome.outcomeUnknown ? { outcomeUnknown: true } : {}),
    };
  }
  return { kind: "none" };
}

/**
 * Inline result of a write next to its form. A parked write shows the shared
 * approval notice (waiting, a different person must approve, approved with
 * Run it now or "submit this form again"); an error shows its plain message
 * with the raw text collapsed; a write whose result is unknown says so and
 * where to check.
 */
export function WriteFeedback<T>({
  state,
  shown,
  what,
  checkHref,
  checkLabel,
  continuation,
  className,
}: {
  state: HubWriteState<T>;
  shown: ShownOutcome;
  what: string;
  checkHref?: string;
  checkLabel?: string;
  continuation?: ParkedWriteContinuation;
  className?: string;
}) {
  if (state.status === "pending_approval") {
    return (
      <ParkedWriteNotice
        approvalId={state.approvalId}
        label={what}
        checkHref={checkHref}
        checkLabel={checkLabel}
        continuation={continuation}
        className={className}
      />
    );
  }
  if (shown.kind === "cancelled") {
    return (
      <div role="alert" className={`rounded-md border border-[var(--ua-warn)] px-3 py-2 text-sm ${className ?? ""}`}>
        <p className="m-0">{shown.message}</p>
        {shown.dispatched && <UnknownText checkHref={checkHref} checkLabel={checkLabel} />}
      </div>
    );
  }
  if (shown.kind === "error") {
    return (
      <div role="alert" className={`rounded-md border border-[var(--ua-danger)] bg-[var(--ua-danger-surface)] px-3 py-2 text-sm ${className ?? ""}`}>
        <p className="m-0">{shown.message}</p>
        {shown.outcomeUnknown && <UnknownText checkHref={checkHref} checkLabel={checkLabel} />}
        {shown.detail && (
          <details className="mt-1 text-xs">
            <summary className="cursor-pointer">Technical detail</summary>
            <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{shown.detail}</pre>
          </details>
        )}
      </div>
    );
  }
  return null;
}

function UnknownText({ checkHref, checkLabel }: { checkHref?: string; checkLabel?: string }) {
  return (
    <p className="m-0 mt-1">
      The request had already been sent, so the change may have been made. Check before trying again
      {checkHref ? (
        <>
          {": "}
          <UiLink href={checkHref} className="text-[var(--ua-link)] underline">
            {checkLabel ?? "check it"}
          </UiLink>
        </>
      ) : null}
      .
    </p>
  );
}

export { CANCEL_MESSAGES };

// --- Draft helpers -----------------------------------------------------------

export type DraftHandle<T> = { key: string; value: T; setValue: (next: T | ((prev: T) => T)) => void; clear: () => void };

/**
 * Record an attempt in the draft before the request leaves (review rule: an
 * attempt is recorded before the send, never after the answer), and confirm
 * it was stored. False when storage refused it.
 */
export function recordBeforeSend<T>(epoch: OwnerEpoch, draft: DraftHandle<T>, next: T): boolean {
  if (!draft.key) return false;
  saveDraft(epoch, draft.key, next);
  draft.setValue(next);
  return readDraft<T>(epoch, draft.key) !== null;
}

/**
 * Change the draft a write was made from, after the write answers. `key` is
 * the draft key captured before the send. `live` is the form's handle only
 * while that form is still mounted (`useLiveDraft`), else null: an answer
 * that arrives after the form unmounted never reads or writes that form's
 * component state, only the copy in owner storage, which a newer visit to
 * the form may have changed. `change` returns the next value, or null to
 * clear it.
 */
export function updateDraftAt<T>(epoch: OwnerEpoch, live: DraftHandle<T> | null, key: string, change: (prev: T) => T | null): void {
  if (!key || epoch.revoked) return;
  if (live && live.key === key) {
    const next = change(live.value);
    if (next === null) live.clear();
    else {
      // Stored now, not after the draft's save delay: a reload right after
      // the answer must not find the attempt marker it just cleared.
      saveDraft(epoch, key, next);
      live.setValue(next);
    }
    return;
  }
  const stored = readDraft<T>(epoch, key);
  if (stored === null) return;
  const next = change(stored);
  if (next === null) clearDraft(epoch, key);
  else saveDraft(epoch, key, next);
}

/**
 * After a successful write: clear the draft under `key` only if what is
 * there now still equals the submitted snapshot (review rule: a draft is
 * cleared against the submitted snapshot, never the latest text). "Now" is
 * the stored copy read back from owner storage, and, while the submitting
 * form is still mounted, its current text as well. Otherwise the draft is
 * kept, with `keep` applied (for example dropping the attempt marker).
 */
export function settleDraftAt<T extends Record<string, unknown>>(
  epoch: OwnerEpoch,
  live: DraftHandle<T> | null,
  key: string,
  submitted: T,
  fields: readonly (keyof T)[],
  keep: (current: T) => T,
): void {
  if (!key || epoch.revoked) return;
  const stored = readDraft<T>(epoch, key);
  const storedUnchanged = stored === null || sameFields(stored, submitted, fields);
  if (live && live.key === key) {
    if (storedUnchanged && sameFields(live.value, submitted, fields)) live.clear();
    else updateDraftAt(epoch, live, key, keep);
    return;
  }
  if (stored === null) return;
  if (storedUnchanged) clearDraft(epoch, key);
  else saveDraft(epoch, key, keep(stored));
}

/**
 * The draft handle while the component that owns it is mounted, else null.
 * Read `.current` after an await: a form that has unmounted must not be used
 * to decide anything about the draft.
 */
export function useLiveDraft<T>(draft: DraftHandle<T>): { readonly current: DraftHandle<T> | null } {
  const handle = useRef(draft);
  handle.current = draft;
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  return useRef({
    get current() {
      return mounted.current ? handle.current : null;
    },
  }).current;
}

/** Whether the fields the person typed still equal the submitted snapshot. */
export function sameFields<T extends Record<string, unknown>>(current: T, submitted: T, fields: readonly (keyof T)[]): boolean {
  return fields.every((f) => JSON.stringify(current[f] ?? null) === JSON.stringify(submitted[f] ?? null));
}

/** The latest value of something a later callback reads (a draft handle after an await). */
export function useLatest<T>(value: T): { readonly current: T } {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}
