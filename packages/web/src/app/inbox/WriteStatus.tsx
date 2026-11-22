import { LinkButton } from "../../lib/ui";
import { approvalInboxHref, type HubWriteState } from "../lib/mutations";

/**
 * The outcome of one write, said in place next to the button that made it
 * (the toast says it too, but leaves). A parked approval is never shown as
 * success: it says the change waits, links the approval, and tells the person
 * whether it will run on its own once approved.
 */
export function WriteStatus<T>({
  state,
  doneText,
  acceptedText,
}: {
  state: HubWriteState<T>;
  doneText?: string | ((data: T) => string);
  acceptedText?: string;
}) {
  // Nothing runs by itself (spec §6): a stored write is run by a click, else the form is submitted again.
  const fromStore = state.status === "pending_approval" && state.canRunFromStore;
  if (state.status === "idle" || state.status === "running") {
    return <p role="status" className="sr-only" aria-live="polite">{state.status === "running" ? "Working…" : ""}</p>;
  }
  if (state.status === "done") {
    const text = typeof doneText === "function" ? doneText(state.data) : doneText;
    return text ? (
      <p role="status" className="m-0 text-sm text-[var(--ua-fg)]">
        <span aria-hidden="true" className="mr-1 text-[var(--ua-ok)]">✓</span>
        {text}
      </p>
    ) : null;
  }
  if (state.status === "accepted") {
    return (
      <p role="status" className="m-0 text-sm text-[var(--ua-fg)]">
        {acceptedText ?? "The hub accepted this and has started it. It has not finished yet."}
      </p>
    );
  }
  if (state.status === "cancelled") {
    // Not sent: say nothing (the shell explains a sign-in change). Sent: the change may have been made.
    return state.dispatched ? (
      <p role="alert" className="m-0 rounded border border-[var(--ua-warn)] px-3 py-2 text-sm">
        Outcome unknown: the request had already been sent when the sign-in changed, so the change may have been made.
        Check before trying again.
      </p>
    ) : null;
  }
  if (state.status === "pending_approval") {
    return (
      <div role="status" className="flex flex-wrap items-center gap-2 rounded border border-[var(--ua-warn)] px-3 py-2 text-sm">
        <span>
          Waiting for approval. Nothing has changed yet.{" "}
          {fromStore
            ? "When it is approved, run it from the approval in the Inbox."
            : "After it is approved, submit this form again."}
        </span>
        <LinkButton size="sm" variant="secondary" href={approvalInboxHref(state.approvalId)}>
          Open the approval
        </LinkButton>
      </div>
    );
  }
  return (
    <div role="alert" className="flex flex-col gap-1 rounded border border-[var(--ua-danger)] bg-[var(--ua-danger-surface)] px-3 py-2 text-sm">
      <span>{state.message}</span>
      {state.detail && (
        <details className="text-xs">
          <summary className="cursor-pointer">Technical detail</summary>
          <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{state.detail}</pre>
        </details>
      )}
    </div>
  );
}
