import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchEvents } from "../../lib/api/nodes";
import { createCoalescedInvalidator } from "../../lib/polling";
import { scanItemHistory, transcriptWindow, type ItemHistory } from "./nodeHistory";
import type { ActivityEvent } from "../../lib/sessions/types";
import { sessionQueryKeys } from "./api";

/** Live events kept between two durable reads. */
export const LIVE_KEEP = 500;

/**
 * One device work item's events: durable history (`/api/events?item_id=`,
 * read incrementally from the stored cursor) merged with live stream events,
 * oldest first. Used by the session page and by the device work view, which
 * shows a job before (or without) any agent session row.
 *
 * Every live event for the item, and every (re)connection of the stream,
 * asks for a durable refresh through one coalesced invalidator (review
 * finding 7). The refresh reads only rows after the stored cursor, so it
 * fills in events the live buffer dropped or the stream missed while it was
 * disconnected. Live events already in durable history are released from
 * the buffer; truncation is reported from the merged window.
 */
export function useItemTranscript(itemId: string | null, options: { minGapMs?: number } = {}) {
  const queryClient = useQueryClient();
  const queryKey = sessionQueryKeys.transcript(itemId ?? "");
  const history = useQuery({
    queryKey,
    enabled: itemId !== null,
    queryFn: ({ signal }) =>
      scanItemHistory(itemId ?? "", signal, fetchEvents, queryClient.getQueryData<ItemHistory>(sessionQueryKeys.transcript(itemId ?? "")) ?? undefined),
  });

  // Keyed by item: another item never shows this item's buffer (review 8).
  const [live, setLive] = useState<{ itemId: string | null; events: ActivityEvent[]; dropped: boolean }>({ itemId, events: [], dropped: false });
  const buffer = live.itemId === itemId ? live : { itemId, events: [] as ActivityEvent[], dropped: false };

  const invalidator = useRef<ReturnType<typeof createCoalescedInvalidator> | null>(null);
  useEffect(() => {
    if (!itemId) return;
    const inv = createCoalescedInvalidator(
      () => void queryClient.invalidateQueries({ queryKey: sessionQueryKeys.transcript(itemId), exact: true }, { cancelRefetch: false }),
      options.minGapMs === undefined ? {} : { minGapMs: options.minGapMs },
    );
    invalidator.current = inv;
    const onVisible = () => inv.onVisible();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      inv.cancel();
      invalidator.current = null;
    };
  }, [itemId, queryClient, options.minGapMs]);

  // Once durable history holds them, live events are released from the buffer.
  useEffect(() => {
    const window = history.data?.window;
    if (!window || !itemId) return;
    const known = new Set(window.map((e) => e.ulid));
    setLive((prev) => (prev.itemId !== itemId ? prev : { itemId, events: prev.events.filter((e) => !known.has(e.ulid)), dropped: false }));
  }, [history.data, itemId]);

  const push = useCallback(
    (event: ActivityEvent) => {
      const payload = event.payload as { item_id?: unknown } | null;
      if (!itemId || event.channel !== "agent.node" || payload?.item_id !== itemId) return false;
      setLive((prev) => {
        const base = prev.itemId === itemId ? prev : { itemId, events: [] as ActivityEvent[], dropped: false };
        const events = [event, ...base.events];
        return { itemId, events: events.slice(0, LIVE_KEEP), dropped: base.dropped || events.length > LIVE_KEEP };
      });
      invalidator.current?.request();
      return true;
    },
    [itemId],
  );

  /** The stream opened or reopened: read what was missed while disconnected. */
  const connected = useCallback(() => invalidator.current?.request(), []);

  const merged = useMemo(() => {
    if (!itemId) return { events: [] as ActivityEvent[], truncated: false };
    // Only this item's events, from both sources.
    const mine = (e: ActivityEvent) => (e.payload as { item_id?: unknown } | null)?.item_id === itemId;
    const durable = history.data ? { ...history.data, window: history.data.window.filter(mine) } : undefined;
    return transcriptWindow(durable, buffer.events.filter(mine));
  }, [itemId, history.data, buffer.events]);
  const events = useMemo(() => merged.events.slice().reverse(), [merged]);
  return { history, events, truncated: merged.truncated || buffer.dropped, push, connected };
}
