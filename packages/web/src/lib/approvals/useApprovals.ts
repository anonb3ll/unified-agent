import { subscribeEventStream } from "../eventStream";
import { useEffect } from "react";
import {
  queryOptions,
  useMutation,
  useQuery,
  useQueryClient,
  type Query,
} from "@tanstack/react-query";
import {
  APPROVALS_QUERY_KEY,
  APPROVAL_DETAIL_QUERY_PREFIX,
  approvalDetailQueryKey,
  fetchApprovalDetail,
  fetchApprovals,
  postApprovalDecision,
  type ApprovalDecision,
  type ApprovalsPayload,
} from "../hub";
import {
  backoffRefetchInterval,
  createCoalescedInvalidator,
  pollDelay,
} from "../polling";

/** Steady approvals poll while the last read succeeded. */
export const APPROVALS_POLL_MS = 15_000;
/** Ceiling for the poll interval while the hub keeps failing. */
export const APPROVALS_POLL_MAX_MS = 5 * 60_000;
/** Fewest milliseconds between two SSE-driven approvals refetches. */
export const APPROVALS_SSE_MIN_GAP_MS = 2_000;
/**
 * Approval events only. The unfiltered stream carries every hub event, and
 * refetching the queue on each one let a busy hub drive a tab to several
 * `/api/approvals` requests a second (task #1786). Changes that arrive on
 * other channels are still picked up by the poll.
 */
export const APPROVALS_STREAM_URL = "/api/events/stream?channels=agent.approval.*";

/**
 * Poll delay after `consecutiveErrors` failed reads in a row: the steady
 * interval when healthy, doubling per failure up to {@link APPROVALS_POLL_MAX_MS}
 * so a struggling hub is not polled at full rate.
 */
export function approvalsPollDelay(consecutiveErrors: number): number {
  return pollDelay(APPROVALS_POLL_MS, consecutiveErrors, APPROVALS_POLL_MAX_MS);
}

/** `refetchInterval` for the approvals queries: steady, or backed off on error. */
export const approvalsRefetchInterval: <T = ApprovalsPayload>(
  query: Pick<Query<T>, "state">,
) => number = backoffRefetchInterval(APPROVALS_POLL_MS, APPROVALS_POLL_MAX_MS);

/**
 * The one definition of the approvals list query, shared by every surface that
 * shows it (pane, shell badge, header pill, home). Polling stops while the tab
 * is hidden (`refetchIntervalInBackground: false`) and backs off on errors.
 */
export function approvalsQueryOptions() {
  return queryOptions({
    queryKey: APPROVALS_QUERY_KEY,
    queryFn: ({ signal }) => fetchApprovals(signal),
    refetchInterval: approvalsRefetchInterval,
    refetchIntervalInBackground: false,
  });
}

// Shared with every other pane since task #1850; re-exported for callers.
export { createCoalescedInvalidator };

/**
 * Approvals queue via TanStack Query + SSE invalidation on approval events.
 * Invalidations are coalesced and held while the tab is hidden.
 */
export function useApprovals() {
  const queryClient = useQueryClient();

  useEffect(() => {
    const invalidator = createCoalescedInvalidator(
      () => {
        void queryClient.invalidateQueries({ queryKey: APPROVALS_QUERY_KEY });
        // An approval event can change the row an open drawer shows (decided,
        // revoked elsewhere). Refetch only active, i.e. open, drawers.
        void queryClient.refetchQueries({
          queryKey: APPROVAL_DETAIL_QUERY_PREFIX,
          type: "active",
        });
      },
      { minGapMs: APPROVALS_SSE_MIN_GAP_MS },
    );
    const onVisibilityChange = () => invalidator.onVisible();

    const closeStream = subscribeEventStream(APPROVALS_STREAM_URL, (stream) => {
      stream.addEventListener("cursor", () => {
        // Connected / heartbeat — no invalidate needed.
      });

      stream.addEventListener("message", () => {
        invalidator.request();
      });

      stream.addEventListener("error", () => {
        // EventSource reconnects on its own, or openEventStream re-opens it
        // after a refusal; leave query cache intact.
      });
    });

    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      invalidator.cancel();
      closeStream();
    };
  }, [queryClient]);

  return useQuery(approvalsQueryOptions());
}

/**
 * One approval's full detail, for the drawer (task #1263). Disabled until an
 * id is selected — the drawer only fetches while it is actually open.
 *
 * Its key sits outside the list key (task #1786), so list refreshes do not
 * reach it. It polls on its own instead, at the list's interval and backoff
 * and only while visible: a pass that crosses its TTL emits no event, and the
 * drawer must not keep offering a decision on a row that has moved on.
 */
export function useApprovalDetail(id: number | null) {
  return useQuery({
    queryKey: approvalDetailQueryKey(id ?? -1),
    queryFn: ({ signal }) => fetchApprovalDetail(id as number, signal),
    enabled: id != null,
    refetchInterval: approvalsRefetchInterval,
    refetchIntervalInBackground: false,
  });
}

export function useApprovalDecision() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      id,
      decision,
      reason,
    }: {
      id: number;
      decision: ApprovalDecision;
      reason?: string;
    }) => postApprovalDecision(id, decision, undefined, reason),
    // Refresh on both outcomes: a failure here is most often the
    // optimistic-concurrency row changing under the operator (task #1263 /
    // approval CAS from #592) or the row leaving 'pending' — the queue and
    // any open drawer must reflect the current state either way, not just
    // on success. Never a silent failure.
    onSettled: (_data, _error, variables) => {
      void queryClient.invalidateQueries({ queryKey: APPROVALS_QUERY_KEY });
      if (variables) {
        void queryClient.invalidateQueries({
          queryKey: approvalDetailQueryKey(variables.id),
        });
      }
    },
  });
}
