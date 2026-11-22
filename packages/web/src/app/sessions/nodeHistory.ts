import { fetchEvents, type NodeDispatchItem } from "../../lib/api/nodes";
import type { ActivityEvent } from "../../lib/sessions/types";

/**
 * One device work item's durable history (`/api/events?item_id=`), kept from
 * the classic session transcript when the classic panes were deleted (spec
 * §10). `itemTranscript.ts` reads it incrementally and merges live events.
 */

function itemFromEvents(
  events: ActivityEvent[],
  itemId: string,
): ActivityEvent[] {
  return events.filter((event) => {
    if (event.channel !== "agent.node") return false;
    const payload = event.payload;
    if (!payload || typeof payload !== "object") return false;
    return (payload as { item_id?: unknown }).item_id === itemId;
  });
}

const HISTORY_PAGE = 500;
export const HISTORY_RETAIN_MAX = 5000;

function isCheckpoint(event: ActivityEvent): boolean {
  const payload = event.payload as Record<string, unknown> | null;
  return (
    !!payload &&
    payload.kind === "acp_event" &&
    payload.acp_kind === "turn_checkpoint"
  );
}

/** One item's retained durable history and where its scan stopped (#2010). */
export interface ItemHistory {
  /** The newest `HISTORY_RETAIN_MAX` durable events, oldest first. */
  window: ActivityEvent[];
  /** The newest checkpoint seen, even when it fell out of `window`. */
  latestCheckpoint?: ActivityEvent;
  /** The durable `after_seq` cursor: the last seq read for this item. */
  seqCursor: number;
  /** Retention discarded older events, so `window` is not the whole history. */
  truncated: boolean;
}

/**
 * Scan one item's durable history to its tail, retaining a bounded newest
 * window. The page cursor always advances even after retention fills, so a
 * long session never silently loses its latest checkpoint (#2010). With
 * `previous`, only rows after its cursor are read: a refresh costs the new
 * tail, not the whole history again.
 */
export async function scanItemHistory(
  itemId: string,
  signal?: AbortSignal,
  fetcher: typeof fetchEvents = fetchEvents,
  previous?: ItemHistory,
  onProgress?: (partial: ItemHistory) => void,
): Promise<ItemHistory> {
  const collected: ActivityEvent[] = previous ? [...previous.window] : [];
  let latestCheckpoint = previous?.latestCheckpoint;
  let truncated = previous?.truncated ?? false;
  let afterSeq = previous?.seqCursor ?? 0;
  while (true) {
    signal?.throwIfAborted();
    const { events, seqCursor, error } = await fetcher(
      {
        channels: ["agent.node"],
        itemId,
        afterSeq,
        limit: HISTORY_PAGE,
      },
      signal,
    );
    signal?.throwIfAborted();
    if (error) throw new Error(error);
    const itemEvents = itemFromEvents(events, itemId);
    for (const event of itemEvents) {
      if (isCheckpoint(event)) latestCheckpoint = event;
    }
    collected.push(...itemEvents);
    if (collected.length > HISTORY_RETAIN_MAX) {
      collected.splice(0, collected.length - HISTORY_RETAIN_MAX);
      truncated = true;
    }
    if (events.length < HISTORY_PAGE) {
      if (
        seqCursor != null &&
        Number.isSafeInteger(seqCursor) &&
        seqCursor > afterSeq
      ) {
        afterSeq = seqCursor;
      }
      break;
    }
    if (
      seqCursor == null ||
      !Number.isSafeInteger(seqCursor) ||
      seqCursor <= afterSeq
    ) {
      throw new Error(
        "Transcript history cursor did not advance; retry the history read.",
      );
    }
    afterSeq = seqCursor;
    // Each full page is a consistent resume point: a cancelled or failed
    // scan picks up here instead of at the previous refresh's cursor.
    onProgress?.({
      window: [...collected],
      latestCheckpoint,
      seqCursor: afterSeq,
      truncated,
    });
  }
  return {
    window: collected,
    latestCheckpoint,
    seqCursor: afterSeq,
    truncated,
  };
}

/**
 * `scanItemHistory` from sequence zero, as a flat oldest-first list: the
 * retained window, preceded by the latest checkpoint when a long in-progress
 * turn streamed it out of the window (at most one event beyond it).
 */
export async function loadItemHistory(
  itemId: string,
  signal?: AbortSignal,
  fetcher: typeof fetchEvents = fetchEvents,
): Promise<ActivityEvent[]> {
  const { window, latestCheckpoint } = await scanItemHistory(
    itemId,
    signal,
    fetcher,
  );
  if (
    latestCheckpoint &&
    !window.some((event) => event.ulid === latestCheckpoint.ulid)
  ) {
    return [latestCheckpoint, ...window];
  }
  return window;
}

/**
 * The transcript shown for one item: durable history merged with live
 * stream events, newest first, bounded to the newest `HISTORY_RETAIN_MAX`
 * events plus the latest checkpoint as one explicit extra row when it fell
 * outside them. `truncated` is true only when events were actually left out.
 */
export function transcriptWindow(
  history: ItemHistory | undefined,
  live: ActivityEvent[],
): { events: ActivityEvent[]; truncated: boolean } {
  const merged = mergeByUlid(history?.window ?? [], live);
  const shown = merged.slice(0, HISTORY_RETAIN_MAX);
  const truncated =
    (history?.truncated ?? false) || merged.length > HISTORY_RETAIN_MAX;
  // The newest checkpoint across both sources: a missed stream event can
  // leave an older checkpoint in the live buffer while the durable history
  // holds a newer one that fell out of the window.
  const checkpoint = [
    ...merged.filter(isCheckpoint),
    ...(history?.latestCheckpoint ? [history.latestCheckpoint] : []),
  ].sort(newerFirst)[0];
  if (checkpoint && !shown.some((event) => event.ulid === checkpoint.ulid)) {
    shown.push(checkpoint);
  }
  return { events: shown, truncated };
}

/**
 * Newest-first order. Commit order (`seq`) decides when both events carry it;
 * otherwise the timestamp, which has second precision, with `seq` and then
 * the ULID (minted in commit order) breaking ties, so a window cut through
 * equal timestamps keeps the newer rows.
 */
function newerFirst(left: ActivityEvent, right: ActivityEvent): number {
  const ls = left.seq;
  const rs = right.seq;
  if (ls != null && rs != null && ls !== rs) return rs - ls;
  const lt = left.created_at ?? left.ulid;
  const rt = right.created_at ?? right.ulid;
  if (lt !== rt) return rt.localeCompare(lt);
  if (ls != null && rs != null) return rs - ls;
  return right.ulid.localeCompare(left.ulid);
}

function mergeByUlid(a: ActivityEvent[], b: ActivityEvent[]): ActivityEvent[] {
  const map = new Map<string, ActivityEvent>();
  for (const event of [...a, ...b]) map.set(event.ulid, event);
  return [...map.values()].sort(newerFirst);
}

/**
 * Evidence deep-link for a dispatch: the Evidence pane filters by task, so a
 * session started for a task opens that task's chain (task #1332).

/** True for a dispatch that can take a follow-up at all (an ACP session not yet over). */
export function acceptsFollowUps(item: NodeDispatchItem): boolean {
  return (
    item.backend.startsWith("acp:") &&
    (item.status === "claimed" || item.status === "queued") &&
    !item.cancel_requested
  );
}
