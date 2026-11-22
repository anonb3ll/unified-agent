import { useEffect, useId, useState } from "react";
import { useUnknownAttempt, withAttempt } from "./attempts";
import { previewFingerprint, previewLimitReasons, previewSaysComplete } from "./previewLimits";
import { useQuery } from "@tanstack/react-query";
import {
  APPROVALS_QUERY_KEY,
  approvalDetailQueryKey,
  fetchApprovalDetail,
  type ApprovalDetail as ApprovalDetailData,
} from "../../lib/api/approvals";
import { ATTENTION_QUERY_KEY } from "../../lib/api/attention";
import { approvalsRefetchInterval } from "../../lib/approvals/useApprovals";
import { Badge, Button, Checkbox, CodeBlock, DescriptionList, Field, LinkButton, Textarea, UiLink, type BadgeVariant } from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import { approvalStatusQueryKey, approverRelation, discardParkedWrite, useParkedWrites } from "../lib/parkedWrites";
import { useSignedInIdentity } from "../lib/identity";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { PATHS } from "./api";
import { DetailHeader, DetailRead, DetailSection, summarizeValue } from "./DetailParts";
import { ago, toolName } from "./model";
import { WriteStatus } from "./WriteStatus";

/** Messages the hub raises when the row moved under the operator (approval CAS, not pending). */
/** The hub's separation-of-duties refusal (`approvals.approval_resolve`). */
const SEPARATION = /approver must be a different person|separation of duties/i;

/** Approvals the hub refused to let this person approve (separation of duties): theirs for the rest of the tab session. */
const REFUSED_KEY = "ua-cc2:refused-as-requester";

function refusedFor(epoch: OwnerEpoch, identity: string | null | undefined): number[] {
  if (!identity) return [];
  return epoch.storage.read<Record<string, number[]>>(REFUSED_KEY, {})[identity] ?? [];
}

function rememberRefused(epoch: OwnerEpoch, identity: string | null | undefined, approvalId: number): void {
  if (!identity || refusedFor(epoch, identity).includes(approvalId)) return;
  const all = epoch.storage.read<Record<string, number[]>>(REFUSED_KEY, {});
  epoch.storage.write(REFUSED_KEY, { ...all, [identity]: [...(all[identity] ?? []), approvalId] });
}

/**
 * Whether the signed-in person asked for this approval. The hub decides
 * "same person" with aliases no read exposes (`approvals._same_person`), so
 * the shared `approverRelation` can prove only "self"; a different label is
 * "unknown", never "someone else". The page also knows it is "mine" when a
 * stored write of this person waits on it (stored writes are listed only for
 * their owner) or the hub already refused this person's approval as the
 * requester. "unknown" never means permission: the hub decides.
 */
export type Requester = "mine" | "unknown";

export function requesterOf(epoch: OwnerEpoch, input: {
  approvalId: number;
  requestedBy: string | null | undefined;
  identity: string | null | undefined;
  hasOwnStoredWrite: boolean;
}): Requester {
  if (refusedFor(epoch, input.identity).includes(input.approvalId) || input.hasOwnStoredWrite) return "mine";
  return approverRelation(input.requestedBy, input.identity) === "self" ? "mine" : "unknown";
}
const CONCURRENCY = /changed concurrently|not 'pending'|no approval request with id/i;

type StatusText = { label: string; variant: BadgeVariant; sentence: string };

/** What the approvals list says about a pass: the detail read carries no expiry (hub task #2498). */
export type PassListing = { bucket: "pending" | "approved_unconsumed" | "expired"; expiresAt: string | null; expired: boolean | null } | null;

/**
 * The approval's state in words. `consumed` means the gate let the call
 * through (it consumes the pass before the call runs), not that the call
 * finished. An `approved` pass may have expired unused: that is known only
 * from the approvals list; from a direct link it is not claimed either way.
 */
export function statusText(status: string | null | undefined, listing: PassListing, now = Date.now()): StatusText {
  switch (status) {
    case "pending":
      return { label: "Waiting", variant: "warning", sentence: "Waiting for your decision." };
    case "approved": {
      const expired =
        listing?.bucket === "expired" ||
        listing?.expired === true ||
        (listing?.expiresAt ? Date.parse(listing.expiresAt) <= now : false);
      if (expired) {
        return {
          label: "Expired unused",
          variant: "neutral",
          sentence: "Approved, but the pass expired before it was used. The call cannot run on it; it has to be requested again.",
        };
      }
      if (listing?.bucket === "approved_unconsumed" || listing?.bucket === "pending") {
        const until = listing.expiresAt ? ` until ${new Date(listing.expiresAt).toLocaleString()}` : " until the pass expires";
        return { label: "Approved", variant: "info", sentence: `Approved, not used yet. The requester may repeat exactly this call once${until}.` };
      }
      return {
        label: "Approved",
        variant: "info",
        sentence: "Approved. This page cannot tell whether the pass is still valid: an unused pass expires after the hub's pass lifetime.",
      };
    }
    case "consumed":
      return {
        label: "Approved and used",
        variant: "success",
        sentence:
          "The approval was used: the call was let through. Whether the call finished is not recorded on the approval; check the task or run it belongs to.",
      };
    case "denied":
      return { label: "Denied", variant: "danger", sentence: "Denied. The call did not run." };
    case "revoked":
      return { label: "Revoked", variant: "danger", sentence: "The approval was revoked before it was used." };
    case "expired":
      return { label: "Expired", variant: "neutral", sentence: "The approval expired before it was used." };
    default:
      return { label: status ?? "Unknown", variant: "neutral", sentence: `Status: ${status ?? "unknown"}.` };
  }
}


function field(payload: Record<string, unknown> | undefined, key: string): string | null {
  const value = payload?.[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : null;
}

function needsAttestation(data: ApprovalDetailData): boolean {
  return (
    data.tool === "github_app.process_pr_event" &&
    (data.review_display_complete === false ||
      (!!data.payload && Object.prototype.hasOwnProperty.call(data.payload, "independent_review")))
  );
}

/** What will run: tool, requester, where, and the arguments in plain rows. */
function WhatWillRun({ data }: { data: ApprovalDetailData }) {
  const payload = data.payload ?? {};
  const entries = Object.entries(payload);
  const taskId = data.links?.task?.id;
  return (
    <DetailSection title="What will run">
      <DescriptionList
        items={[
          { term: "Tool", value: <span className="font-mono">{toolName(data)}</span> },
          { term: "Requested by", value: data.requested_by ? <UiLink className="underline" href={`/agents/profile/${encodeURIComponent(data.requested_by)}`}>{data.requested_by}</UiLink> : null },
          {
            term: "Device",
            value: data.device ? (
              <span>
                {data.device}{" "}
                <Badge variant={data.device_verified ? "success" : "neutral"}>{data.device_verified ? "Seen by the hub" : "Reported by the agent"}</Badge>
              </span>
            ) : null,
          },
          { term: "Requested", value: data.created_at ? `${ago(data.created_at)} (${data.created_at})` : null },
          { term: "Why it needs approval", value: data.gate?.label ?? null },
          { term: "Task", value: taskId ? <UiLink className="underline" href={`/work/tasks/${encodeURIComponent(taskId)}`}>Task {taskId}</UiLink> : null },
          { term: "Run", value: data.run_id ? <span className="font-mono">{data.run_id}</span> : null },
        ]}
      />
      {entries.length > 0 ? (
        <div className="flex flex-col gap-1">
          <h4 className="m-0 text-xs font-semibold text-[var(--ua-muted)]">Arguments shown by the hub</h4>
          <ul aria-label="Arguments summary" className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
            {entries.slice(0, 12).map(([key, value]) => (
              <li key={key} className="break-words">
                <span className="font-mono text-[var(--ua-muted)]">{key}</span>: {summarizeValue(value)}
              </li>
            ))}
            {entries.length > 12 && <li className="text-[var(--ua-muted)]">and {entries.length - 12} more, below</li>}
          </ul>
          <details className="text-sm">
            <summary className="cursor-pointer">Arguments shown by the hub, in full</summary>
            <CodeBlock label="Arguments shown by the hub" code={JSON.stringify(payload, null, 2)} language="json" className="mt-2" />
          </details>
          <p className="m-0 text-xs text-[var(--ua-muted)]">Values that look like secrets are hidden by the hub before they reach this page.</p>
        </div>
      ) : (
        <p className="m-0 text-sm text-[var(--ua-muted)]">This call has no arguments.</p>
      )}
      {data.tool === "work_product_release" && (
        <DescriptionList
          items={[
            { term: "Work product", value: field(payload, "title") ?? field(payload, "work_product_id") },
            { term: "Content hash", value: field(payload, "content_sha256") },
            { term: "Reviewer role", value: field(payload, "reviewer_role") },
            { term: "Previous version", value: field(payload, "previous_version_id") ?? "None" },
          ]}
        />
      )}
      {data.tool === "acp_permission" && (
        <DescriptionList
          items={[
            { term: "Machine", value: field(payload, "node") ?? data.device ?? null },
            { term: "Agent session", value: field(payload, "session_id") },
            {
              term: "Action",
              value:
                payload.tool_call && typeof payload.tool_call === "object"
                  ? field(payload.tool_call as Record<string, unknown>, "title") ?? field(payload.tool_call as Record<string, unknown>, "kind")
                  : null,
            },
          ]}
        />
      )}
      {(data.pr_url || data.commit_sha) && (
        <DescriptionList
          items={[
            { term: "Pull request", value: data.pr_url ? <a className="break-all underline" href={data.pr_url} target="_blank" rel="noreferrer">{data.pr_url}</a> : null },
            { term: "Commit", value: data.commit_sha ? <span className="break-all font-mono">{data.commit_sha}</span> : null },
          ]}
        />
      )}
    </DetailSection>
  );
}

/**
 * The standing acknowledgement (review 2.1): the hub may shorten the preview
 * and does not say when, so every approval asks for it until the read says
 * its preview is complete. The heuristics only add a stronger line.
 */
function PreviewAcknowledgement({
  reasons,
  acknowledged,
  changed,
  onAcknowledge,
}: {
  reasons: string[];
  acknowledged: boolean;
  changed: boolean;
  onAcknowledge: (v: boolean) => void;
}) {
  const titleId = useId();
  return (
    <div
      role="group"
      aria-labelledby={titleId}
      className={`flex flex-col gap-2 rounded border-2 px-3 py-2 text-sm ${reasons.length || changed ? "border-[var(--ua-warn)]" : "border-[var(--ua-border-strong)]"}`}
    >
      <p id={titleId} className="m-0 font-semibold">
        Before you approve
      </p>
      {changed && (
        <p role="alert" className="m-0 font-semibold text-[var(--ua-warn)]">
          The request changed; review it again.
        </p>
      )}
      <p className="m-0">
        The hub may shorten what it shows here (long lists, nested fields and large arguments), and does not yet say when
        it has. Anything not shown would also be approved.
      </p>
      {reasons.length > 0 && (
        <>
          <p className="m-0 font-semibold">This preview looks shortened:</p>
          <ul className="m-0 pl-5">
            {reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </>
      )}
      <Checkbox
        label="I understand that anything not shown here would also be approved"
        checked={acknowledged}
        onChange={(e) => onAcknowledge(e.target.checked)}
      />
    </div>
  );
}

export function ApprovalDetail({ approvalId, listing = null }: { approvalId: number; listing?: PassListing }) {
  const epoch = useEpoch();
  const query = useQuery({
    queryKey: approvalDetailQueryKey(approvalId),
    queryFn: ({ signal }) => fetchApprovalDetail(approvalId, signal),
    refetchInterval: approvalsRefetchInterval,
    refetchIntervalInBackground: false,
    retry: 1,
  });
  const decide = useHubWrite<{ id: number; decision: "approve" | "deny"; reason?: string }>({
    path: PATHS.approval,
    label: "Your decision",
    resumable: false,
    // The stored-write notice reads the approval too; a decision refreshes it at once.
    invalidates: [APPROVALS_QUERY_KEY, ATTENTION_QUERY_KEY, approvalDetailQueryKey(approvalId), approvalStatusQueryKey(String(approvalId), epoch.ownerKey)],
  });
  const [denying, setDenying] = useState(false);
  // What the Deny form was opened on: the preview and the status. A refetch that changes either withdraws it.
  const [denyBasis, setDenyBasis] = useState<string | null>(null);
  const [denyChanged, setDenyChanged] = useState(false);
  const [reason, setReason] = useState("");
  const [decision, setDecision] = useState<"approve" | "deny" | null>(null);
  // The acknowledgement is bound to what was reviewed: the approval id plus a fingerprint of the preview (review 2.2).
  const [ackFingerprint, setAckFingerprint] = useState<string | null>(null);
  const [changedSinceAck, setChangedSinceAck] = useState(false);
  // The signed-in person; an approval they requested needs a different person to approve it.
  const identity = useSignedInIdentity();
  const attemptKey = identity ? `approval:${approvalId}|${identity}` : null;
  const { attempt: unknownAttempt, dismiss: dismissAttempt } = useUnknownAttempt(attemptKey, decide.isRunning);
  // A stored write of the signed-in person waiting on this approval (owner-filtered by the foundation).
  const [ownStored] = useParkedWrites({ approvalId: String(approvalId) });
  // True while the decision is in flight.
  const [busy, setBusy] = useState(false);
  // The decision's answer was lost: the state shown is the one read again afterwards.
  const [lost, setLost] = useState(false);

  useEffect(() => {
    setDenying(false);
    setReason("");
    setDecision(null);
    setAckFingerprint(null);
    setChangedSinceAck(false);
    setLost(false);
    decide.reset();
    // A new approval starts clean; `decide` is stable per mount.
  }, [approvalId]);

  const currentFingerprint = query.data ? previewFingerprint(approvalId, query.data.payload) : null;
  const currentBasis = query.data ? `${currentFingerprint}|${query.data.status ?? ""}` : null;
  useEffect(() => {
    if (denying && denyBasis && currentBasis && denyBasis !== currentBasis) {
      setDenying(false);
      setDenyBasis(null);
      setDenyChanged(true);
    }
  }, [denying, denyBasis, currentBasis]);
  useEffect(() => {
    if (ackFingerprint && currentFingerprint && ackFingerprint !== currentFingerprint) {
      setAckFingerprint(null);
      setChangedSinceAck(true);
    }
  }, [ackFingerprint, currentFingerprint]);

  const run = async (choice: "approve" | "deny") => {
    // Approve only what was reviewed: the preview must still be the one acknowledged.
    if (choice === "approve" && query.data && !previewSaysComplete(query.data) && ackFingerprint !== previewFingerprint(approvalId, query.data.payload)) {
      setAckFingerprint(null);
      setChangedSinceAck(true);
      return;
    }
    setDecision(choice);
    setBusy(true);
    setLost(false);
    try {
      const body = choice === "deny" && reason.trim() ? { id: approvalId, decision: choice, reason: reason.trim() } : { id: approvalId, decision: choice };
      // Stored before it is sent: leaving mid-request shows that the answer did not arrive (sweep c).
      const outcome = attemptKey ? await withAttempt(epoch, attemptKey, "Your decision", () => decide.run(body)) : await decide.run(body);
      if (outcome.kind === "error" || (outcome.kind === "cancelled" && outcome.dispatched)) {
        // The decision may have been recorded even if its answer was lost: read the approval again first.
        await query.refetch();
        if (outcome.kind === "cancelled" || outcome.outcomeUnknown) setLost(true);
        return;
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <DetailRead query={query} label="approval">
        {(data) => {
          const status = statusText(data.status, listing);
          const pending = data.status === "pending";
          const attest = needsAttestation(data);
          const attestBlocked = attest && data.review_display_complete !== true;
          const shortened = previewLimitReasons(data.payload);
          const needsAck = !previewSaysComplete(data);
          const fingerprint = previewFingerprint(data.id, data.payload);
          const acknowledged = ackFingerprint === fingerprint;
          const ackBlocked = needsAck && !acknowledged;
          // Separation of duties (approvals.approval_resolve): nobody approves a request they made.
          // The requester may still deny it, which withdraws it.
          const separation = decide.state.status === "error" && SEPARATION.test(`${decide.state.detail ?? ""} ${decide.state.message}`);
          if (separation) rememberRefused(epoch, identity, data.id);
          const requester = requesterOf(epoch, { approvalId: data.id, requestedBy: data.requested_by, identity, hasOwnStoredWrite: ownStored !== undefined });
          const mine = requester === "mine";
          // Known only from the approvals list row (the detail read carries no expiry).
          const passExpired = status.label === "Expired unused";
          const concurrency = decide.state.status === "error" && CONCURRENCY.test(`${decide.state.detail ?? ""} ${decide.state.message}`);
          return (
            <>
              <DetailHeader kind={`Approval ${data.id}`} title={`${toolName(data)} wants to run`} status={status.label} statusVariant={status.variant} />
              <WhatWillRun data={data} />
              {needsAck && pending && (
                <PreviewAcknowledgement
                  reasons={shortened}
                  acknowledged={acknowledged}
                  changed={changedSinceAck}
                  onAcknowledge={(v) => {
                    setAckFingerprint(v ? fingerprint : null);
                    if (v) setChangedSinceAck(false);
                  }}
                />
              )}
              {attest && (
                <DetailSection title="Release and review statement">
                  <p className="m-0 text-sm">
                    Approving this release also confirms the review statement below for the exact commit shown. These are claims a
                    person made; check them first.
                  </p>
                  <CodeBlock label="Review statement" code={JSON.stringify(data.payload?.independent_review ?? null, null, 2)} />
                  {attestBlocked && (
                    <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">
                      The review evidence is incomplete or hidden in this view, so it cannot be approved here.
                    </p>
                  )}
                </DetailSection>
              )}
              {/* A change of yours, stored in this tab, waits only on your own request (spec §6). */}
              {mine && (
                <DetailSection title="Your change that waits on this">
                  {passExpired && ownStored ? (
                    // The detail read says "approved", but the approvals list knows the pass expired:
                    // the shared notice cannot take that state yet (Needs from foundation), so it is said here.
                    <section aria-label="Approval status" className="flex flex-col gap-2 rounded-md border border-l-4 border-[var(--ua-danger)] bg-[var(--ua-surface)] px-3 py-2 text-sm">
                      <p className="m-0">
                        {ownStored.label}: the approval expired before it was used, so it cannot run on it. Ask again from its
                        form, or discard it.
                      </p>
                      <div className="flex flex-wrap items-center gap-2">
                        {ownStored.formHref && (
                          <LinkButton size="sm" href={ownStored.formHref}>
                            Open the form to ask again
                          </LinkButton>
                        )}
                        <Button size="sm" variant="ghost" onClick={() => discardParkedWrite(epoch, ownStored.id)}>
                          Discard
                        </Button>
                      </div>
                    </section>
                  ) : (
                    <ParkedWriteNotice approvalId={String(data.id)} inInbox />
                  )}
                </DetailSection>
              )}
              <DetailSection title={pending ? "Decide" : "Result"}>
                {!pending && (
                  <p role="status" className="m-0 text-sm">
                    {status.sentence}
                    {data.resolved_by ? ` Decided by ${data.resolved_by}${data.resolved_at ? ` ${ago(data.resolved_at)}` : ""}.` : ""}
                    {data.reason ? ` Reason: ${data.reason}` : ""}
                  </p>
                )}
                {pending && (
                  <div className="flex flex-col gap-3">
                    {denying ? (
                      <>
                        <Field label="Reason for denying" hint="Optional. The requesting agent sees it.">
                          <Textarea rows={2} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
                        </Field>
                        <div className="flex flex-wrap gap-2">
                          <Button variant="danger" loading={busy && decision === "deny"} disabled={busy} onClick={() => void run("deny")}>
                            Deny this call
                          </Button>
                          <Button variant="ghost" disabled={busy} onClick={() => setDenying(false)}>
                            Cancel
                          </Button>
                        </div>
                      </>
                    ) : (
                      <div className="flex flex-col gap-2">
                        {requester === "unknown" && (
                          <p className="m-0 text-sm text-[var(--ua-muted)]">
                            {data.requested_by && identity
                              ? "This page cannot confirm that someone other than you asked for this: the hub treats some sign-in names as the same person. If it was you, the hub will refuse your approval."
                              : "Who asked for this is not known here, so this page cannot tell whether you may approve it. If you asked for it yourself, the hub will refuse your approval."}
                          </p>
                        )}
                        {mine && (
                          <p className="m-0 rounded border border-[var(--ua-border-strong)] px-3 py-2 text-sm">
                            You asked for this, so someone else has to approve it. If you are the only person who can
                            approve on this hub, it cannot be approved. You can still withdraw it by denying it.
                          </p>
                        )}
                        <div className="flex flex-wrap gap-2">
                          {!mine && (
                            <Button
                              variant="primary"
                              disabled={attestBlocked || ackBlocked || busy}
                              loading={busy && decision === "approve"}
                              onClick={() => void run("approve")}
                            >
                              {attest ? "Approve release and confirm review" : "Approve"}
                            </Button>
                          )}
                          <Button
                            variant="danger"
                            disabled={busy}
                            onClick={() => {
                              setDenyBasis(currentBasis);
                              setDenyChanged(false);
                              setDenying(true);
                            }}
                          >
                            {mine ? "Withdraw (deny)…" : "Deny…"}
                          </Button>
                        </div>
                      </div>
                    )}
                    <p className="m-0 text-xs text-[var(--ua-muted)]">
                      {mine
                        ? "Denying withdraws the request; the call does not run."
                        : "Approving lets the requester repeat exactly this call once. It does not run it for them."}
                    </p>
                  </div>
                )}
                {denyChanged && (
                  <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">
                    The request changed while you were deciding; review it again.
                  </p>
                )}
                {unknownAttempt && (
                  <div role="alert" className="flex flex-wrap items-center gap-2 rounded border border-[var(--ua-warn)] px-3 py-2 text-sm">
                    <span>
                      Your decision was sent, but its answer did not arrive here. The approval was read again: its current
                      state is shown on this page.
                    </span>
                    <Button size="sm" variant="ghost" onClick={dismissAttempt}>
                      Dismiss
                    </Button>
                  </div>
                )}
                {separation ? (
                  <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">
                    This needs a different person. The hub does not let anyone approve a request they made, and it counts
                    your sign-in as the requester of this one. Someone else has to approve it.
                  </p>
                ) : lost ? (
                  <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">
                    The answer to your decision was lost, so it is not known from that answer whether it was recorded. The
                    approval was read again: its current state is shown here.
                  </p>
                ) : concurrency ? (
                  <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">
                    This approval changed since you opened it: someone decided it, or it expired. Its current state is shown above.
                  </p>
                ) : (
                  <WriteStatus state={decide.state} doneText={decision === "deny" ? "Denied. The call will not run." : "Approved."} />
                )}
              </DetailSection>
            </>
          );
        }}
      </DetailRead>
      <LinkButton size="sm" variant="ghost" href="/inbox?kind=approval">
        All approvals
      </LinkButton>
    </div>
  );
}
