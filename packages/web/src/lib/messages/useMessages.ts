import { subscribeEventStream } from "../eventStream";
import { useEffect } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createCoalescedInvalidator } from "../approvals/useApprovals";
import {
  LIVE_SESSIONS_QUERY_KEY,
  MESSAGES_QUERY_KEY,
  conversationsQueryKey,
  fetchConversations,
  fetchLiveSessions,
  fetchThread,
  sendMessage,
  threadQueryKey,
  type ConversationFilter,
  type SendMessageInput,
} from "./api";

/**
 * Fallback refresh while the tab is visible. Message events normally arrive over
 * the stream below; this only catches what it misses (a dropped stream, or a
 * profile without event_log_query). Deliberately calm: aggressive polling once
 * loaded the hub, so nothing here polls faster than this.
 */
export const MESSAGES_POLL_MS = 30_000;
/**
 * Message and handoff events only; the unfiltered stream carries every hub
 * event. The handoff channel refreshes the open thread's handoff list when
 * another caller hands it off.
 */
export const MESSAGES_STREAM_URL =
  "/api/events/stream?channels=agent.message.*,agent.conversation.handoff";

/**
 * Invalidate every Messages query (handoff lists included) when an
 * `agent.message.*` or `agent.conversation.handoff` event lands.
 * Bursts are coalesced and held while the tab is hidden (the approvals pattern).
 */
export function useMessagesStream() {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const invalidator = createCoalescedInvalidator(() => {
      void queryClient.invalidateQueries({ queryKey: MESSAGES_QUERY_KEY });
    });
    const onVisibilityChange = () => invalidator.onVisible();
    const closeStream = subscribeEventStream(MESSAGES_STREAM_URL, (stream) => {
      stream.addEventListener("message", () => invalidator.request());
      stream.addEventListener("error", () => {
        // EventSource reconnects on its own, or openEventStream re-opens it
        // after a refusal; the poll covers the gap.
      });
    });
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      invalidator.cancel();
      closeStream();
    };
  }, [queryClient]);
}

/** Conversations, newest activity first; `fetchNextPage` loads the next 50. */
export function useConversations(filter: ConversationFilter, participant: string) {
  return useInfiniteQuery({
    queryKey: conversationsQueryKey(filter, participant),
    queryFn: ({ pageParam, signal }) =>
      fetchConversations(filter, participant, pageParam, signal),
    initialPageParam: null as number | null,
    getNextPageParam: (page) => page.next_before_seq ?? undefined,
    refetchInterval: MESSAGES_POLL_MS,
    refetchIntervalInBackground: false,
  });
}

/**
 * One conversation, newest page first. `fetchNextPage` loads the page before the
 * oldest one shown ("Load earlier"); `hasNextPage` is the hub's `has_older`.
 */
export function useThread(conversationId: string | null) {
  return useInfiniteQuery({
    queryKey: threadQueryKey(conversationId ?? ""),
    queryFn: ({ pageParam, signal }) =>
      fetchThread(conversationId as string, pageParam, signal),
    initialPageParam: null as number | null,
    getNextPageParam: (page) => (page.has_older ? page.oldest_seq : undefined),
    enabled: Boolean(conversationId),
    refetchInterval: MESSAGES_POLL_MS,
    refetchIntervalInBackground: false,
  });
}

export function useLiveSessions() {
  return useQuery({
    queryKey: LIVE_SESSIONS_QUERY_KEY,
    queryFn: ({ signal }) => fetchLiveSessions(signal),
    staleTime: MESSAGES_POLL_MS,
    refetchInterval: 2 * MESSAGES_POLL_MS,
    refetchIntervalInBackground: false,
  });
}

export function useSendMessage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: SendMessageInput) => sendMessage(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: MESSAGES_QUERY_KEY });
    },
  });
}
