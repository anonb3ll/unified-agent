import { useMemo } from "react";
import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { APPROVALS_QUERY_KEY } from "../../lib/api/approvals";
import { ATTENTION_QUERY_KEY } from "../../lib/api/attention";
import { fetchProposals, PROPOSALS_QUERY_KEY } from "../../lib/api/proposals";
import { approvalsQueryOptions } from "../../lib/approvals/useApprovals";
import { attentionQueryOptions } from "../../lib/attention/useAttention";
import { backoffRefetchInterval, useStreamInvalidation } from "../../lib/polling";
import { UNKNOWN_COUNT } from "../lib/queries";
import {
  fetchOpenOrgProposals,
  orgProposalsQueryKey,
  type OrgProposalFilter,
  ORG_PROPOSALS_QUERY_KEY,
} from "./api";
import { buildInbox, waitingCount, type InboxItem, type InboxTab } from "./model";

/** The hub caps each typed-inbox kind at this many rows (`needs_operator.LIMIT_PER_KIND`). */
export const KIND_LIMIT = 100;
/** `approval_pending` returns at most this many pending rows (its default `limit`). */
export const APPROVALS_PENDING_LIMIT = 100;

const NEEDS_KINDS: Record<Exclude<InboxTab, "all" | "proposal" | "anomaly">, readonly string[]> = {
  approval: ["wake_grant"],
  message: ["operator_reply"],
  hold: ["operator_task", "red_tier_hold", "held_pr"],
};

export type InboxSource = {
  id: "attention" | "approvals" | "org" | "memory";
  /** Plain name for the "could not be read" notice. */
  label: string;
  query: UseQueryResult<unknown>;
};

export type InboxData = {
  items: InboxItem[];
  /** True while no source has answered yet. */
  loading: boolean;
  sources: InboxSource[];
  /** Sources whose last read failed (no data, or a hub-reported partial read). */
  failed: { label: string; error: unknown; retry: () => void }[];
  /** Count for a tab from complete reads only; "–" otherwise. */
  countText: (tab: InboxTab) => string;
  countKnown: (tab: InboxTab) => boolean;
  /** The tab's sources returned a full page: its count is a lower bound. */
  capped: (tab: InboxTab) => boolean;
  /** A source that could hold items for this tab cannot be read at all (work proposals): the count is a lower bound. */
  unlistable: (tab: InboxTab) => boolean;
  /**
   * How completely the read that would list this `item=` value answered:
   * "complete" only for a successful read of that kind with no reported
   * error and no full page, so absence there means the item is resolved.
   */
  readStateFor: (itemKey: string) => "pending" | "failed" | "incomplete" | "complete";
  refetchAll: () => void;
};

/**
 * Every Inbox source, read in parallel and merged. Each source keeps its own
 * state: one failing source never empties the others, and a tab's count is
 * shown only when every source that feeds it answered completely.
 */
/** Work proposals have no read route on the hub yet (hub task #2498), so All and Proposals may not be everything. */
export const UNLISTABLE_NOTE = "Work proposals cannot be listed on this hub yet, so this may not be everything.";
const unlistable = (tab: InboxTab) => tab === "all" || tab === "proposal";

export function useInbox(options: { orgFilter?: OrgProposalFilter } = {}): InboxData {
  const orgFilter = options.orgFilter ?? {};
  const queryClient = useQueryClient();
  const attention = useQuery({ ...attentionQueryOptions(), retry: 1 });
  const approvals = useQuery({ ...approvalsQueryOptions(), retry: 1 });
  const memory = useQuery({
    queryKey: PROPOSALS_QUERY_KEY,
    queryFn: ({ signal }) => fetchProposals(signal),
    refetchInterval: backoffRefetchInterval(60_000),
    refetchIntervalInBackground: false,
    retry: 1,
  });
  const org = useQuery({
    queryKey: orgProposalsQueryKey(orgFilter),
    queryFn: ({ signal }) => fetchOpenOrgProposals(signal, orgFilter),
    refetchInterval: backoffRefetchInterval(60_000),
    refetchIntervalInBackground: false,
    retry: 1,
  });

  useStreamInvalidation(
    () => {
      for (const queryKey of [ATTENTION_QUERY_KEY, APPROVALS_QUERY_KEY, ORG_PROPOSALS_QUERY_KEY]) {
        void queryClient.invalidateQueries({ queryKey });
      }
    },
    {
      channels: [
        "agent.approval.*",
        "agent.task.*",
        "agent.message.*",
        "agent.org_proposal.*",
        "agent.session.*",
      ],
    },
  );

  const items = useMemo(
    () =>
      buildInbox({
        attention: attention.data,
        approvals: approvals.data,
        memoryProposals: memory.data,
        orgProposals: org.data?.proposals,
      }),
    [attention.data, approvals.data, memory.data, org.data],
  );

  const sources: InboxSource[] = [
    { id: "attention", label: "Tasks, replies, holds and alerts", query: attention },
    { id: "approvals", label: "Approvals", query: approvals },
    { id: "org", label: "Organization proposals", query: org },
    { id: "memory", label: "Memory proposals", query: memory },
  ];

  const memorySourceMissing = memory.data?.code === "no_proposals_source";
  const needsErrors = attention.data?.needs_operator?.errors ?? [];
  const kindsOk = (kinds: readonly string[]) =>
    attention.status === "success" &&
    attention.data?.needs_operator !== undefined &&
    !needsErrors.some((e) => kinds.some((k) => e.startsWith(`${k}:`)));
  const alertsOk =
    attention.status === "success" && !attention.data?.error && (attention.data?.errors?.length ?? 0) === 0;
  const memoryOk = memory.status === "success" && (!memory.data?.error || memorySourceMissing);

  const complete: Record<InboxTab, boolean> = {
    approval: approvals.status === "success" && !approvals.data?.error && kindsOk(NEEDS_KINDS.approval),
    message: kindsOk(NEEDS_KINDS.message),
    hold: kindsOk(NEEDS_KINDS.hold),
    proposal: org.status === "success" && memoryOk,
    anomaly: alertsOk,
    all: false,
  };
  complete.all = complete.approval && complete.message && complete.hold && complete.proposal && complete.anomaly;

  // A source that returned a full page may have more rows than were returned.
  const needsCapped = (kinds: readonly string[]) => {
    const counts = attention.data?.needs_operator?.counts ?? {};
    return kinds.some((k) => (counts[k] ?? 0) >= KIND_LIMIT);
  };
  const cappedBy: Record<Exclude<InboxTab, "all">, boolean> = {
    approval: (approvals.data?.pending.length ?? 0) >= APPROVALS_PENDING_LIMIT || needsCapped(NEEDS_KINDS.approval),
    message: needsCapped(NEEDS_KINDS.message),
    hold: needsCapped(NEEDS_KINDS.hold),
    proposal: Boolean(org.data?.capped),
    anomaly: false,
  };
  const capped = (tab: InboxTab) => (tab === "all" ? Object.values(cappedBy).some(Boolean) : cappedBy[tab]);

  const failed: InboxData["failed"] = [];
  for (const source of sources) {
    const data = source.query.data as { error?: unknown; errors?: unknown; code?: unknown } | undefined;
    const partial =
      source.id === "attention"
        ? needsErrors.length > 0 || !alertsOk
        : source.id === "memory"
          ? Boolean(data?.error) && !memorySourceMissing
          : Boolean(data?.error);
    if (source.query.status === "error" || (source.query.status === "success" && partial)) {
      failed.push({
        label: source.label,
        error: source.query.error ?? data?.error ?? needsErrors[0] ?? null,
        retry: () => void source.query.refetch(),
      });
    }
  }

  return {
    items,
    loading: sources.every((s) => s.query.status === "pending"),
    sources,
    failed,
    countKnown: (tab) => complete[tab],
    capped,
    readStateFor: (itemKey) => {
      const typed = /^(wake|reply|operator_task|red_tier_hold|held_pr):/.exec(itemKey)?.[1];
      if (typed) {
        const kind = typed === "wake" ? "wake_grant" : typed === "reply" ? "operator_reply" : typed;
        if (attention.status === "pending") return "pending";
        if (attention.status === "error") return "failed";
        return kindsOk([kind]) && !needsCapped([kind]) ? "complete" : "incomplete";
      }
      if (itemKey.startsWith("alert:")) {
        if (attention.status === "pending") return "pending";
        if (attention.status === "error") return "failed";
        return alertsOk ? "complete" : "incomplete";
      }
      if (itemKey.startsWith("memory:")) {
        if (memory.status === "pending") return "pending";
        if (memory.status === "error") return "failed";
        return memoryOk ? "complete" : "incomplete";
      }
      if (itemKey.startsWith("org:")) {
        if (org.status === "pending") return "pending";
        if (org.status === "error") return "failed";
        return org.data?.capped ? "incomplete" : "complete";
      }
      return "incomplete";
    },
    countText: (tab) => {
      if (!complete[tab]) return UNKNOWN_COUNT;
      const n = waitingCount(items, tab);
      return capped(tab) || unlistable(tab) ? `${n}+` : String(n);
    },
    unlistable,
    refetchAll: () => {
      for (const s of sources) void s.query.refetch();
    },
  };
}
