import { useRef, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchWhoami, WHOAMI_QUERY_KEY, type WhoamiPayload } from "../../lib/api/auth";
import { useRootQueryClient } from "../lib/epoch";
import { cx, UiLink } from "../../lib/ui";
import { type HubWriteState } from "../lib/mutations";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { fetchOperatorFlag, sessionQueryKeys } from "./api";
import { LIVE_TEXT, type LiveState } from "./live";

/** The signed-in identity; shares the shell's whoami cache entry. */
export function useWhoami() {
  // whoami is not owner data: it lives in the root client (spec §6, owner epoch).
  return useQuery<WhoamiPayload>(
    {
      queryKey: WHOAMI_QUERY_KEY,
      queryFn: ({ signal }) => fetchWhoami(signal),
      staleTime: 30_000,
    },
    useRootQueryClient(),
  );
}

/** True only when the hub says the caller is the verified operator. */
export function useIsOperator() {
  return useQuery({
    queryKey: sessionQueryKeys.operator,
    queryFn: ({ signal }) => fetchOperatorFlag(signal),
    staleTime: 60_000,
  });
}

/**
 * What happened to a write, said in place (spec §6): running, waiting for
 * approval with a link to it, or an error in plain words with the raw text
 * collapsed (defect 10). Success is announced by the caller, which knows
 * what to link to.
 */
export function WriteStatus<T>({
  state,
  label,
  className,
  formHref,
}: {
  state: HubWriteState<T>;
  /** "The session", "Your follow-up". */
  label: string;
  className?: string;
  /** Where the form lives, when the person may need to come back and submit it again. */
  formHref?: string;
}) {
  if (state.status === "running") {
    return (
      <p role="status" className={cx("m-0 text-sm text-[var(--ua-muted)]", className)}>
        Sending…
      </p>
    );
  }
  if (state.status === "pending_approval") {
    // The shared notice (spec §6): nothing runs by itself. A stored write
    // offers "Run it now" once approved; anything else is submitted again
    // from the form, which keeps what was entered.
    return (
      <div role="status" className={cx("flex flex-col gap-1", className)}>
        <ParkedWriteNotice approvalId={state.approvalId} label={label} />
        {!state.canRunFromStore && formHref ? (
          <UiLink href={formHref} className="text-sm underline">
            Back to the form
          </UiLink>
        ) : null}
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <div role="alert" className={cx("rounded-md border border-[var(--ua-danger)] p-3 text-sm", className)}>
        <p className="m-0">{state.message}</p>
        {state.detail ? (
          <details className="mt-1 text-xs text-[var(--ua-muted)]">
            <summary className="cursor-pointer">Technical detail</summary>
            <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{state.detail}</pre>
          </details>
        ) : null}
      </div>
    );
  }
  return null;
}

const LIVE_TONE: Record<LiveState, string> = {
  connecting: "text-[var(--ua-muted)]",
  live: "text-[var(--ua-ok)]",
  reconnecting: "text-[var(--ua-warn)]",
  off: "text-[var(--ua-muted)]",
};

const LIVE_GLYPH: Record<LiveState, string> = { connecting: "○", live: "●", reconnecting: "◷", off: "○" };

/** The visible connection state of a live region. Not itself a live region: the page announces messages. */
export function LiveIndicator({ state }: { state: LiveState }) {
  return (
    <p className={cx("m-0 text-xs", LIVE_TONE[state])} data-live-state={state}>
      <span aria-hidden="true">{LIVE_GLYPH[state]} </span>
      {LIVE_TEXT[state]}
    </p>
  );
}

export function SectionHeading({ id, children, actions }: { id: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
      <h2 id={id} className="m-0 text-base font-semibold text-[var(--ua-fg)]">
        {children}
      </h2>
      {actions}
    </div>
  );
}


/** The latest value, readable from a callback that resolves later. */
export function useLatest<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

/** Shown when a send succeeded but the text was changed meanwhile, so the dialog stays open. */
export function KeptNewerText({ shown }: { shown: boolean }) {
  return shown ? (
    <p role="status" className="m-0 text-sm">
      Sent. You changed the text while it was sending, so your newer text is kept here: send it too, or close.
    </p>
  ) : null;
}
