import { useEffect, useRef, type ReactNode } from "react";
import { EmptyState, ErrorState, LinkButton, Skeleton } from "../../lib/ui";
import { describeReadError } from "../lib/queries";
import { ApprovalDetail, type PassListing } from "./ApprovalDetail";
import { approvalIdFromKey, type InboxItem } from "./model";
import { AlertDetail, HoldDetail, MemoryProposalDetail, OrgProposalDetail, ReplyDetail, WakeDetail } from "./OtherDetails";
import type { InboxData, InboxSource } from "./useInbox";
import { useOwnerKey } from "./ownerKey";

/** Which read an `item=` value comes from, so its absence is judged by that read alone. */
function sourceOf(itemKey: string): InboxSource["id"] {
  if (itemKey.startsWith("memory:")) return "memory";
  if (itemKey.startsWith("org:")) return "org";
  return "attention";
}

/**
 * The detail pane for one Inbox item, with that kind's action inline. An
 * approval is read by id, so it stays on screen after it is decided (and
 * after it leaves the waiting list) to show the result.
 *
 * Any other item is looked up in the list. Its absence is judged by the one
 * read that would list it: loading while that read is, an error if it
 * failed, and "no longer waiting" only after it answered without the item.
 * An item that disappears because its action resolved it stays on screen
 * with its result, and focus moves to its heading, since the control that
 * had focus may be gone.
 */
export function ItemDetail({ itemKey, inbox }: { itemKey: string; inbox: InboxData }) {
  const items = inbox.items;
  // Per-item state (acknowledgement, decision in progress, write state) belongs to one person and one item:
  // every detail is keyed by both, so a reused pane never carries it to another identity or item.
  const owner = useOwnerKey();
  const lastSeen = useRef<{ key: string; item: InboxItem } | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const approvalId = approvalIdFromKey(itemKey);
  const found = approvalId === null ? items.find((i) => i.key === itemKey) : undefined;
  if (found) lastSeen.current = { key: itemKey, item: found };
  const readState = approvalId === null ? inbox.readStateFor(itemKey) : "complete";
  const previous = approvalId === null && !found && lastSeen.current?.key === itemKey ? lastSeen.current.item : null;
  // Absence means "resolved" only after a complete read of the item's kind.
  const resolved = previous && readState === "complete" ? previous : null;
  const unchecked = !found && (readState === "incomplete" || (readState === "failed" && previous !== null));

  useEffect(() => {
    if (!resolved) return;
    const heading = container.current?.querySelector<HTMLElement>("[data-detail-heading]");
    const active = document.activeElement;
    if (heading && (!active || active === document.body || !container.current?.contains(active))) heading.focus();
  }, [resolved]);

  if (approvalId !== null) {
    // Expiry is known only from the approvals list row the item came from.
    const row = items.find((i): i is Extract<InboxItem, { kind: "approval" }> => i.kind === "approval" && i.key === itemKey);
    const listing: PassListing = row
      ? { bucket: row.bucket, expiresAt: row.approval.expires_at ?? null, expired: row.approval.expired ?? null }
      : null;
    return <ApprovalDetail key={`${owner}|${approvalId}`} approvalId={approvalId} listing={listing} />;
  }
  if (unchecked) {
    return (
      <div ref={container} className="flex flex-col gap-3">
        <p role="status" className="m-0 rounded border border-[var(--ua-warn)] px-3 py-2 text-sm">
          We could not check whether this is still waiting: the hub's answer for this kind of item was incomplete. Try again
          in a moment, or open the related task or session.
        </p>
        {previous && detailFor(previous, owner)}
      </div>
    );
  }
  if (resolved) {
    return (
      <div ref={container} className="flex flex-col gap-3">
        <p role="status" className="m-0 rounded border border-[var(--ua-border-strong)] px-3 py-2 text-sm">
          This item is no longer waiting. Its result is shown below.
        </p>
        {detailFor(resolved, owner)}
      </div>
    );
  }
  if (!found) {
    const source = inbox.sources.find((s) => s.id === sourceOf(itemKey));
    if (!source || source.query.status === "pending") return <Skeleton label="Loading the item" />;
    if (source.query.status === "error") {
      const { message, detail } = describeReadError(source.query.error);
      return (
        <ErrorState
          title="This item could not be loaded"
          message={`${source.label} could not be read, so it is not known whether this item is still waiting. ${message}`}
          detail={detail}
          onRetry={() => void source.query.refetch()}
        />
      );
    }
    return (
      <EmptyState
        headingLevel={2}
        title="This item is no longer waiting"
        description="Someone already handled it, or the link is out of date. Pick another item from the list."
        action={<LinkButton href="/inbox">Back to the Inbox</LinkButton>}
      />
    );
  }
  return <div ref={container}>{detailFor(found, owner)}</div>;
}

function detailFor(item: InboxItem, owner: string): ReactNode {
  const key = `${owner}|${item.key}`;
  switch (item.kind) {
    case "approval":
      return <ApprovalDetail key={key} approvalId={item.approval.id} listing={{ bucket: item.bucket, expiresAt: item.approval.expires_at ?? null, expired: item.approval.expired ?? null }} />;
    case "wake_grant":
      return <WakeDetail key={key} item={item} />;
    case "operator_reply":
      return <ReplyDetail key={key} item={item} />;
    case "operator_task":
    case "red_tier_hold":
    case "held_pr":
      return <HoldDetail key={key} item={item} />;
    case "org_proposal":
      return <OrgProposalDetail key={key} item={item} />;
    case "memory_proposal":
      return <MemoryProposalDetail key={key} item={item} />;
    case "alert":
      return <AlertDetail key={key} item={item} />;
  }
}
