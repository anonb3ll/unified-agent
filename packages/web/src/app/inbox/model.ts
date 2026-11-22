import type { ApprovalsPayload, PublicApprovalRow } from "../../lib/api/approvals";
import type { ProposalRow, ProposalsPayload } from "../../lib/api/proposals";
import type { AttentionItem, AttentionPayload, NeedsOperatorRow } from "../../lib/attention/types";
import type { OrgProposal } from "./api";

/**
 * One thing waiting on a person, from any of the Inbox's sources (spec §9.1).
 * The tab a kind lives under is fixed here; the URL names the tab
 * (`?kind=`) and the item (`&item=`).
 */
export type InboxTab = "all" | "approval" | "message" | "hold" | "proposal" | "anomaly";

export const TABS: readonly { id: InboxTab; label: string; description: string }[] = [
  { id: "all", label: "All", description: "Everything waiting on a person." },
  {
    id: "approval",
    label: "Approvals",
    description: "Calls an agent made that wait for your yes or no, and sessions asking to be woken.",
  },
  { id: "message", label: "Replies", description: "Questions from agents that wait for your answer." },
  {
    id: "hold",
    label: "Tasks and holds",
    description: "Tasks only a person can do, and finished work held for your go before it merges.",
  },
  {
    id: "proposal",
    label: "Proposals",
    description: "Organization and memory changes that agents propose and you decide.",
  },
  { id: "anomaly", label: "Alerts", description: "Problems the hub noticed: stuck work, stale agents, failures." },
];

export function isTab(value: string): value is InboxTab {
  return TABS.some((t) => t.id === value);
}

export type InboxItem =
  | { key: string; tab: "approval"; kind: "approval"; title: string; since: string | null; approval: PublicApprovalRow; waiting: boolean; bucket: "pending" | "approved_unconsumed" | "expired" }
  | { key: string; tab: "approval"; kind: "wake_grant"; title: string; since: string | null; sessionId: string; waiting: true }
  | { key: string; tab: "message"; kind: "operator_reply"; title: string; since: string | null; messageId: string; conversationId: string | null; waiting: true }
  | { key: string; tab: "hold"; kind: "operator_task" | "red_tier_hold" | "held_pr"; title: string; since: string | null; taskId: number; waiting: true }
  | { key: string; tab: "proposal"; kind: "org_proposal"; title: string; since: string | null; proposal: OrgProposal; waiting: true }
  | { key: string; tab: "proposal"; kind: "memory_proposal"; title: string; since: string | null; proposal: ProposalRow; waiting: true }
  | { key: string; tab: "anomaly"; kind: "alert"; title: string; since: string | null; alert: AttentionItem; waiting: boolean };

export type InboxItemKind = InboxItem["kind"];

/** Short, plain name of a kind for the list and the detail header. */
export const KIND_LABEL: Record<InboxItemKind, string> = {
  approval: "Approval",
  wake_grant: "Wake request",
  operator_reply: "Reply needed",
  operator_task: "Your task",
  red_tier_hold: "Held for your go",
  held_pr: "Pull request held",
  org_proposal: "Organization proposal",
  memory_proposal: "Memory proposal",
  alert: "Alert",
};

/** The `item=` value. Approvals use the bare approval id, which is what parked writes link to. */
export const itemKey = {
  approval: (id: number | string) => String(id),
  wake: (sessionId: string) => `wake:${sessionId}`,
  reply: (messageId: string) => `reply:${messageId}`,
  task: (kind: string, taskId: number | string) => `${kind}:${taskId}`,
  org: (id: string) => `org:${id}`,
  memory: (id: string) => `memory:${id}`,
  alert: (index: number, item: AttentionItem) =>
    `alert:${item.kind}:${item.source?.task_id ?? item.source?.agent_label ?? item.source?.anomaly ?? index}`,
};

/** The approval id an `item=` value names, when it names one. */
export function approvalIdFromKey(key: string | null | undefined): number | null {
  if (!key || !/^\d+$/.test(key)) return null;
  const id = Number(key);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

function needsRows(attention: AttentionPayload | undefined): NeedsOperatorRow[] {
  return attention?.needs_operator?.rows ?? [];
}

const TASK_KINDS = new Set(["operator_task", "red_tier_hold", "held_pr"]);

/**
 * Attention kinds that describe the same wait as a typed-inbox row about the
 * same task: a held or red-tier task waits for a person's merge or review
 * decision, which the attention read also reports as a merge or review wait.
 * Anything else about the task (a stale claim, a block) is a separate alert.
 */
const SAME_WAIT: Record<string, ReadonlySet<string>> = {
  held_pr: new Set(["human_merge_wait", "review_wait", "awaiting_operator"]),
  red_tier_hold: new Set(["human_merge_wait", "review_wait", "awaiting_operator"]),
};

export type InboxSources = {
  attention?: AttentionPayload;
  approvals?: ApprovalsPayload;
  memoryProposals?: ProposalsPayload;
  orgProposals?: OrgProposal[];
};

/**
 * Merge the sources into one list, newest decisions first within a tab.
 * Approvals come from `/api/approvals` (the typed inbox's approval rows are
 * the same rows, so they are not listed twice). Items that do not wait on a
 * person (approved passes, expired passes, informational alerts) carry
 * `waiting: false` and are never counted.
 */
export function buildInbox(sources: InboxSources): InboxItem[] {
  const items: InboxItem[] = [];
  const approvals = sources.approvals;
  if (approvals) {
    for (const row of approvals.pending) {
      items.push({
        key: itemKey.approval(row.id),
        tab: "approval",
        kind: "approval",
        title: `${toolName(row)} wants to run`,
        since: row.created_at ?? row.requested_at ?? null,
        approval: row,
        waiting: true,
        bucket: "pending",
      });
    }
  }
  for (const row of needsRows(sources.attention)) {
    if (row.kind === "wake_grant") {
      items.push({ key: itemKey.wake(row.subject), tab: "approval", kind: "wake_grant", title: row.title, since: row.since ?? null, sessionId: row.subject, waiting: true });
    } else if (row.kind === "operator_reply") {
      items.push({
        key: itemKey.reply(row.subject),
        tab: "message",
        kind: "operator_reply",
        title: row.title,
        since: row.since ?? null,
        messageId: row.subject,
        conversationId: row.act?.params?.conversation ?? null,
        waiting: true,
      });
    } else if (TASK_KINDS.has(row.kind)) {
      const taskId = Number(row.subject);
      if (!Number.isInteger(taskId)) continue;
      items.push({
        key: itemKey.task(row.kind, taskId),
        tab: "hold",
        kind: row.kind as "operator_task" | "red_tier_hold" | "held_pr",
        title: row.title,
        since: row.since ?? null,
        taskId,
        waiting: true,
      });
    }
  }
  for (const proposal of sources.orgProposals ?? []) {
    items.push({ key: itemKey.org(proposal.id), tab: "proposal", kind: "org_proposal", title: proposal.title, since: proposal.created_at, proposal, waiting: true });
  }
  for (const proposal of sources.memoryProposals?.proposals ?? []) {
    items.push({
      key: itemKey.memory(String(proposal.id)),
      tab: "proposal",
      kind: "memory_proposal",
      title: proposal.title || proposal.path || "Untitled proposal",
      since: proposal.source_date ?? null,
      proposal,
      waiting: true,
    });
  }
  const attention = sources.attention;
  if (attention) {
    const seen = new Set<string>();
    const typedWaits = new Map<number, string[]>();
    for (const item of items) if ("taskId" in item) typedWaits.set(item.taskId, [...(typedWaits.get(item.taskId) ?? []), item.kind]);
    const pushAlert = (alert: AttentionItem, index: number, waiting: boolean) => {
      // Approvals are listed from the approvals queue.
      if (alert.kind === "approval") return;
      // The same wait as a typed row about the same task is listed once, as that row.
      const taskId = alert.source?.task_id;
      if (typeof taskId === "number" && (typedWaits.get(taskId) ?? []).some((kind) => SAME_WAIT[kind]?.has(alert.kind))) return;
      let key = itemKey.alert(index, alert);
      if (seen.has(key)) key = `${key}:${index}`;
      seen.add(key);
      items.push({ key, tab: "anomaly", kind: "alert", title: alertTitle(alert), since: null, alert, waiting });
    };
    attention.items.forEach((alert, index) => pushAlert(alert, index, true));
    attention.informational.forEach((alert, index) => pushAlert(alert, attention.items.length + index, false));
  }
  if (approvals) {
    for (const [rows, bucket] of [
      [approvals.approved_unconsumed, "approved_unconsumed"],
      [approvals.expired_passes, "expired"],
    ] as const) {
      for (const row of rows) {
        items.push({
          key: itemKey.approval(row.id),
          tab: "approval",
          kind: "approval",
          title: `${toolName(row)}: ${bucket === "expired" ? "approval expired unused" : "approved, not used yet"}`,
          since: row.updated_at ?? row.created_at ?? null,
          approval: row,
          waiting: false,
          bucket,
        });
      }
    }
  }
  return items;
}

export function toolName(row: { tool?: string | null; tool_name?: string | null }): string {
  return row.tool || row.tool_name || "A tool";
}

export function alertTitle(item: AttentionItem): string {
  const subject = item.source_subject || "";
  if (item.title && subject) return `${subject}: ${item.title}`;
  return item.title || subject || humanizeKind(item.kind);
}

/** "stale_claim" → "Stale claim". */
export function humanizeKind(kind: string): string {
  const text = kind.replace(/[_-]+/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "Alert";
}

export function itemsForTab(items: readonly InboxItem[], tab: InboxTab): InboxItem[] {
  return tab === "all" ? items.filter((i) => i.waiting) : items.filter((i) => i.tab === tab);
}

export function waitingCount(items: readonly InboxItem[], tab: InboxTab): number {
  return items.filter((i) => i.waiting && (tab === "all" || i.tab === tab)).length;
}

/** The task an item is about, for project scoping and the "Open task" link. */
export function taskIdOf(item: InboxItem): number | null {
  switch (item.kind) {
    case "operator_task":
    case "red_tier_hold":
    case "held_pr":
      return item.taskId;
    case "alert": {
      const id = item.alert.source?.task_id;
      return typeof id === "number" && Number.isInteger(id) && id > 0 ? id : null;
    }
    case "approval": {
      const id = Number(item.approval.links?.task?.id);
      return Number.isInteger(id) && id > 0 ? id : null;
    }
    default:
      return null;
  }
}

/** Compact relative time: "4m ago". Unknown or unparsable is an empty string, never "0". */
export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.round((now - then) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** Elapsed time since `iso`: "3h 12m". */
export function elapsed(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.round((now - then) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}
