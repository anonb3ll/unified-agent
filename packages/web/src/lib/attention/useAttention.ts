import { queryOptions, useQuery, useQueryClient } from "@tanstack/react-query";
import { ATTENTION_QUERY_KEY, fetchAttention } from "../hub";
import { backoffRefetchInterval, useStreamInvalidation } from "../polling";

/** Steady attention poll; the header pill and the pane share it. */
export const ATTENTION_POLL_MS = 60_000;

/** The one attention query definition: polled calmly, backed off on errors. */
export function attentionQueryOptions() {
  return queryOptions({
    queryKey: ATTENTION_QUERY_KEY,
    queryFn: ({ signal }) => fetchAttention(signal),
    refetchInterval: backoffRefetchInterval(ATTENTION_POLL_MS),
    refetchIntervalInBackground: false,
  });
}

/**
 * Attention data via TanStack Query + SSE invalidation on /api/events/stream.
 *
 * One attention read fans out to several hub tools (task_list, approvals,
 * delegations, the doctor), so stream-driven refetches are coalesced and held
 * while the tab is hidden (task #1850).
 */
export function useAttention() {
  const queryClient = useQueryClient();

  useStreamInvalidation(() => {
    void queryClient.invalidateQueries({ queryKey: ATTENTION_QUERY_KEY });
  }, { channels: ["agent.task.*", "agent.approval.*", "agent.delegation.*", "agent.health.*", "agent.executor*", "agent.cycle.*", "agent.supervisor.*"] });

  return useQuery(attentionQueryOptions());
}
