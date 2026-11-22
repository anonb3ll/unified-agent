import { Children, isValidElement, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, ConfirmDialog, LinkButton, UiLink } from "../../lib/ui";
import { useEpoch } from "./epoch";
import { useIdentity } from "./identity";
import { approvalInboxHref } from "./mutations";
import {
  approvalStatusQueryKey,
  confirmApplied,
  confirmNotApplied,
  discardParkedWrite,
  approverRelation,
  fetchApprovalStatus,
  needsOtherApprover,
  PARKED_POLL_MS,
  getParkedWriteResult,
  runParkedWrite,
  useParkedWrites,
  type ParkedWriteEntry,
  type RunParkedWriteResult,
} from "./parkedWrites";

export type ParkedWriteNoticeProps = {
  /** The approval the write waits on (from `useHubWrite` state, or the Inbox item). */
  approvalId: string | null;
  /** Name of the change when nothing was stored ("Create task"). */
  label?: string;
  /** Where to check whether an unknown outcome happened (the object's page). */
  checkHref?: string;
  checkLabel?: string;
  /** Hide the "Open approval" link (on the Inbox item itself). */
  inInbox?: boolean;
  /** Called after a run, with its result. */
  onRun?: (result: RunParkedWriteResult) => void;
  /**
   * What really happens next for a write that is not stored for a later run
   * (`resumable: false`), replacing the default "submit this form again".
   * For example a work-product submission whose 202 already created the
   * product: what continues after approval is a release, not a second
   * submission. `description` replaces the instruction while waiting; once
   * approved, `label` is offered as a link (`href`) or a button (`onAction`).
   */
  continuation?: ParkedWriteContinuation;
  className?: string;
};

export type ParkedWriteContinuation = {
  /** The action's name ("Release it"). */
  label: string;
  /** Replaces "After it is approved, submit this form again." */
  description?: string;
  href?: string;
  onAction?: () => void;
};

/**
 * The notice frame. Its text sits in one persistent polite, atomic status
 * region (the same node across states), so a change the poll brings, such
 * as waiting to approved or denied, is announced without moving focus.
 * Buttons and links (`Actions`) stay outside the region.
 */
function Box({ children, tone = "info", className }: { children: ReactNode; tone?: "info" | "warn" | "danger"; className?: string }) {
  const border =
    tone === "danger" ? "border-[var(--ua-danger)]" : tone === "warn" ? "border-[var(--ua-warn)]" : "border-[var(--ua-border-strong)]";
  const parts = Children.toArray(children);
  const actions = parts.filter((child) => isValidElement(child) && child.type === Actions);
  const text = parts.filter((child) => !(isValidElement(child) && child.type === Actions));
  return (
    <section
      aria-label="Approval status"
      className={`flex flex-col gap-2 rounded-md border border-l-4 ${border} bg-[var(--ua-surface)] px-3 py-2 text-sm text-[var(--ua-fg)] ${className ?? ""}`}
    >
      <div role="status" aria-live="polite" aria-atomic="true" className="flex flex-col gap-2">
        {text}
      </div>
      {actions}
    </section>
  );
}

/** Separation of duties: the hub never lets the requester approve their own request. */
function OtherApproverText() {
  return (
    <>
      <p className="m-0">
        <strong>This needs approval from a different person.</strong>
      </p>
      <p className="m-0 text-[var(--ua-muted)]">
        If you are the only person who can approve on this hub, it cannot be approved; discard it or ask for the rule to
        be changed.
      </p>
    </>
  );
}

/** The requester could not be matched to the signed-in person; this is not permission to approve. */
function UnknownApproverText() {
  return (
    <p className="m-0 text-[var(--ua-muted)]">
      It is not known whether you may approve this yourself: the hub requires an approver other than the person who
      asked, and it can treat different names as the same person.
    </p>
  );
}

function Actions({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center gap-2">{children}</div>;
}

const ENDED_TEXT: Partial<Record<ParkedWriteEntry["approval"], string>> = {
  denied: "was denied, so it did not run",
  revoked: "had its approval revoked, so it did not run",
  expired: "was not run before its approval expired",
  consumed: "had its approval used by another request; check the result before submitting again",
  missing: "has no approval any more, so it did not run",
};

/**
 * One notice for a write parked behind an approval, so every page says the
 * same thing (spec §6): waiting (with the Inbox link), approved ("Run it
 * now"), outcome unknown (a check link and "It did not happen, let me run
 * it again"), denied, revoked or expired ("Discard", "Open the form"), and
 * "Open the form to run it" when the write cannot be run from the store,
 * including while the hub reports no organization. Nothing runs without
 * the person's click.
 */
export function ParkedWriteNotice({
  approvalId,
  label = "This change",
  checkHref,
  checkLabel = "Check it",
  inInbox = false,
  onRun,
  continuation,
  className,
}: ParkedWriteNoticeProps) {
  const [entry] = useParkedWrites({ approvalId: approvalId ?? null });
  // Every handler below captures this epoch when it starts and acts only
  // through it: once it is revoked, no toast, callback, cache or storage
  // effect reaches whoever is signed in next.
  const epoch = useEpoch();
  const next = continuation?.description ?? (continuation ? `After it is approved: ${continuation.label}.` : "After it is approved, submit this form again.");
  const continueAction = continuation ? (
    continuation.href ? (
      <LinkButton href={continuation.href} size="sm">
        {continuation.label}
      </LinkButton>
    ) : continuation.onAction ? (
      <Button size="sm" onClick={epoch.wrap(continuation.onAction)}>
        {continuation.label}
      </Button>
    ) : null
  ) : null;
  const [busy, setBusy] = useState(false);
  const [resendOpen, setResendOpen] = useState(false);
  const me = useIdentity().identity;
  const inboxLink =
    approvalId && !inInbox ? (
      <UiLink href={approvalInboxHref(approvalId)} className="text-[var(--ua-link)] underline">
        Open the approval
      </UiLink>
    ) : null;

  // The approval is read even when nothing is stored (`resumable: false`,
  // or after Discard), so the page shows its real state.
  const direct = useQuery({
    queryKey: approvalStatusQueryKey(approvalId ?? "", epoch.ownerKey),
    queryFn: ({ signal }) => fetchApprovalStatus(approvalId as string, signal),
    enabled: Boolean(approvalId) && !entry,
    refetchInterval: PARKED_POLL_MS,
    refetchIntervalInBackground: false,
    retry: 1,
  });

  if (!entry) {
    const result = approvalId ? getParkedWriteResult(epoch, approvalId) : null;
    if (result) {
      return (
        <Box className={className} tone={result.kind === "error" || result.kind === "outcome_unknown" ? "warn" : "info"}>
          <p className="m-0">{result.message}</p>
        </Box>
      );
    }
    if (!approvalId) {
      return (
        <Box className={className}>
          <p className="m-0">
            <strong>Waiting for approval.</strong> {label} has no approval reference that can be followed from here.
            {next}
          </p>
        </Box>
      );
    }
    const state = direct.status === "error" ? "unverified" : direct.data?.resolution;
    const ended: Record<string, string> = {
      denied: `${label} was denied, so it did not run.`,
      revoked: `${label}: the approval was revoked, so it did not run.`,
      expired: `${label}: the approval expired before it was used.`,
      consumed: `${label}: the approval was already used by another request. Check the result before submitting again.`,
      missing: `${label}: the approval no longer exists.`,
    };
    return (
      <Box className={className} tone={state && ended[state] ? "danger" : state === "unverified" ? "warn" : "info"}>
        <p className="m-0">
          {state === "approved" ? (
            <>
              <strong>Approved.</strong>{" "}
              {continuation
                ? continueAction
                  ? null
                  : (continuation.description ?? `Next: ${continuation.label}.`)
                : "Submit this form again to make the change."}
            </>
          ) : state && ended[state] ? (
            ended[state]
          ) : state === "unverified" ? (
            <>
              <strong>The approval could not be checked.</strong> {next}
            </>
          ) : needsOtherApprover(direct.data?.requestedBy, me) ? null : (
            <>
              <strong>Waiting for approval.</strong> {next}
            </>
          )}
        </p>
        {(state === "pending" || state === undefined) && needsOtherApprover(direct.data?.requestedBy, me) && <OtherApproverText />}
        {state === "pending" && direct.data?.requestedBy && approverRelation(direct.data.requestedBy, me) === "unknown" && (
          <UnknownApproverText />
        )}
        {(inboxLink || (state === "approved" && continueAction)) && (
          <Actions>
            {state === "approved" && continueAction}
            {inboxLink}
          </Actions>
        )}
      </Box>
    );
  }

  if ((entry.approval === "waiting" || entry.approval === "loading") && entry.needsOtherApprover) {
    return (
      <Box className={className}>
        <OtherApproverText />
        <Actions>
          {inboxLink}
          <Button size="sm" variant="ghost" onClick={() => discardParkedWrite(epoch, entry.id)}>
            Discard
          </Button>
        </Actions>
      </Box>
    );
  }

  const name = entry.label;
  const formLink = entry.formHref ? (
    <LinkButton href={entry.formHref} size="sm">
      Open the form to run it
    </LinkButton>
  ) : null;
  const discard = (
    <Button size="sm" variant="ghost" onClick={() => discardParkedWrite(epoch, entry.id)}>
      Discard
    </Button>
  );

  const run = async () => {
    const bound = epoch;
    const report = bound.wrap(onRun);
    bound.run(() => setBusy(true));
    try {
      const result = await runParkedWrite(entry.id, { epoch: bound });
      // A cancelled run is never shown, reported or retried here (whoever is
      // signed in now may not be its owner). For its owner, the entry itself
      // carries what happened (outcome unknown or the last error).
      if (result.kind === "cancelled") return;
      const tone = result.kind === "done" || result.kind === "accepted" ? "success" : result.kind === "error" || result.kind === "outcome_unknown" ? "error" : "info";
      bound.toast({ message: result.message, tone });
      report(result);
    } finally {
      bound.run(() => setBusy(false));
    }
  };

  if (entry.outcome === "unknown") {
    // The same recovery the pages use after a lost answer: the person checks
    // (the slice supplies where, as checkHref) and says which it is.
    const itIsThere = () => {
      const bound = epoch;
      const result = confirmApplied(bound, entry.id);
      if (result.kind === "not_run" && result.reason === "not_recorded") bound.toast({ message: result.message, tone: "error" });
      if (result.kind !== "done") return;
      bound.toast({ message: result.message, tone: "success" });
      bound.wrap(onRun)(result);
    };
    const sendAgain = () => {
      const bound = epoch;
      bound.run(() => setResendOpen(false));
      if (!confirmNotApplied(bound, entry.id)) return;
      void run();
    };
    return (
      <>
        <Box className={className} tone="warn">
          <p className="m-0">
            <strong>Outcome unknown.</strong> {name} was sent, but its result could not be confirmed, so it is not
            known whether the change was made. Check whether it is there.
          </p>
          <Actions>
            {checkHref && (
              <LinkButton href={checkHref} size="sm">
                {checkLabel}
              </LinkButton>
            )}
            <Button size="sm" onClick={itIsThere}>
              It is there
            </Button>
            <Button size="sm" onClick={() => setResendOpen(true)}>
              It is not there, send it again
            </Button>
            {discard}
          </Actions>
        </Box>
        <ConfirmDialog
          open={resendOpen}
          title="Send it again?"
          objectName={name}
          consequence="If the first one did go through, this makes a second one. Check first if you can."
          confirmLabel="Send it again"
          variant="danger"
          onConfirm={sendAgain}
          onCancel={() => setResendOpen(false)}
        />
      </>
    );
  }

  const ended = ENDED_TEXT[entry.approval];
  if (ended) {
    return (
      <Box className={className} tone="danger">
        <p className="m-0">
          {name} {ended}.
        </p>
        <Actions>
          {discard}
          {formLink}
        </Actions>
      </Box>
    );
  }

  if (entry.blockedReason === "organization_unknown" || entry.blockedReason === "context_mismatch" || entry.blockedReason === "signed_out") {
    const why =
      entry.blockedReason === "organization_unknown"
        ? "It cannot be run from here yet, because the hub does not report the organization it was made in."
        : entry.blockedReason === "signed_out"
          ? "Sign in again to run it."
          : "It was made by another identity, organization or hub than the one signed in now.";
    return (
      <Box className={className}>
        <p className="m-0">
          <strong>{entry.approval === "approved" ? "Approved." : "Waiting for approval."}</strong> {why} After it is
          approved, open the form and run it again.
        </p>
        <Actions>
          {formLink}
          {inboxLink}
          {discard}
        </Actions>
      </Box>
    );
  }

  if (entry.approval === "unverified") {
    return (
      <Box className={className} tone="warn">
        <p className="m-0">
          <strong>The approval could not be checked.</strong> {name} will not run until the hub confirms it is approved.
        </p>
        <Actions>
          <Button
            size="sm"
            onClick={() =>
              void epoch.run(() =>
                epoch.queryClient.refetchQueries({ queryKey: approvalStatusQueryKey(entry.approvalId, epoch.ownerKey) }),
              )
            }
          >
            Check again
          </Button>
          {inboxLink}
          {discard}
        </Actions>
      </Box>
    );
  }

  if (entry.approval === "approved") {
    return (
      <Box className={className}>
        <p className="m-0">
          <strong>Approved.</strong> {name} has not run yet. Run it now to make the change.
        </p>
        {entry.lastError && <p className="m-0 text-[var(--ua-danger)]">The last attempt failed: {entry.lastError}</p>}
        <Actions>
          <Button size="sm" variant="primary" loading={busy || entry.running} onClick={run}>
            {entry.lastError ? "Try again" : "Run it now"}
          </Button>
          {discard}
        </Actions>
      </Box>
    );
  }

  return (
    <Box className={className}>
      <p className="m-0">
        <strong>{entry.approval === "loading" ? "Checking the approval…" : "Waiting for approval."}</strong> When it is
        approved, run it from here or from the Inbox.
      </p>
      {entry.approval === "waiting" && entry.approver === "unknown" && <UnknownApproverText />}
      <Actions>
        {inboxLink}
        {discard}
      </Actions>
    </Box>
  );
}
