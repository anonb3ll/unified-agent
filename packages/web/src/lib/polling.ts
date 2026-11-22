/**
 * Calm polling for the Control Center (tasks #1786, #1850).
 *
 * After a hub restart one Control Center tab drove ~1,000 gateway calls in ten
 * minutes: every pane that listened to the unfiltered event stream refetched
 * on every hub event, polls kept their full rate while the hub was failing,
 * and slow replies fed more refetches. The approvals pane fixed this first
 * (#1786); these are its rules, shared by every pane:
 *
 * - a poll backs off exponentially while its reads keep failing;
 * - a poll pauses while the tab is hidden (`refetchIntervalInBackground: false`);
 * - stream-driven refetches are coalesced to one per gap and held while hidden;
 * - failed reads are retried once, with backoff, not three times.
 */
import { useEffect, useRef } from "react";
import type { Query } from "@tanstack/react-query";
import { EVENTS_STREAM_URL, POLL_BACKOFF_MAX_MS } from "./eventStreamTransport";
import { subscribeEventStream } from "./eventStream";
export {
  EVENTS_STREAM_URL,
  POLL_BACKOFF_MAX_MS,
  STREAM_RECONNECT_MS,
  openEventStream,
} from "./eventStreamTransport";

/** Fewest milliseconds between two stream-driven refetches of one pane. */
export const STREAM_MIN_GAP_MS = 5_000;

/**
 * Poll delay after `consecutiveErrors` failed reads in a row: `steadyMs` when
 * healthy, doubling per failure up to `maxMs`, so a struggling hub is not
 * polled at full rate.
 */
export function pollDelay(
  steadyMs: number,
  consecutiveErrors: number,
  maxMs: number = POLL_BACKOFF_MAX_MS,
): number {
  if (!(consecutiveErrors > 0)) return steadyMs;
  return Math.min(steadyMs * 2 ** consecutiveErrors, Math.max(maxMs, steadyMs));
}

// errorUpdateCount never resets, so remember its value at the last success.
const errorCountAtLastSuccess = new WeakMap<object, number>();

/**
 * A `refetchInterval` that polls every `steadyMs` while reads succeed and backs
 * off (see {@link pollDelay}) while they fail. Pair it with
 * `refetchIntervalInBackground: false` (TanStack's default) so a hidden tab
 * does not poll at all.
 */
export function backoffRefetchInterval(steadyMs: number, maxMs: number = POLL_BACKOFF_MAX_MS) {
  return function refetchInterval<T>(query: Pick<Query<T>, "state">): number {
    const { status, errorUpdateCount } = query.state;
    if (status !== "error") {
      errorCountAtLastSuccess.set(query, errorUpdateCount);
      return steadyMs;
    }
    return pollDelay(
      steadyMs,
      errorUpdateCount - (errorCountAtLastSuccess.get(query) ?? 0),
      maxMs,
    );
  };
}

/** Retry a failed read once; the poll or the next stream event covers the rest. */
export const QUERY_RETRY = 1;

/** Backoff before a retry: 2 s, 4 s, ... capped at 30 s. */
export function queryRetryDelay(attemptIndex: number): number {
  return Math.min(2_000 * 2 ** Math.max(0, attemptIndex), 30_000);
}

/** QueryClient query defaults for the Control Center (see main.tsx). */
export const CALM_QUERY_DEFAULTS = {
  staleTime: 5_000,
  refetchOnWindowFocus: false,
  refetchIntervalInBackground: false,
  retry: QUERY_RETRY,
  retryDelay: queryRetryDelay,
} as const;

type Timers = {
  now: () => number;
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
};

const browserTimers: Timers = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

function documentHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

/**
 * Coalesce bursts of invalidation requests (task #1786).
 *
 * The first request runs at once; requests inside `minGapMs` of the last run
 * collapse into one trailing run. While `isHidden()` nothing runs: the request
 * is remembered and replayed once by `onVisible()`, so a background tab does
 * not keep the hub busy and still shows fresh data when it comes back.
 */
export function createCoalescedInvalidator(
  run: () => void,
  {
    minGapMs = STREAM_MIN_GAP_MS,
    isHidden = documentHidden,
    timers = browserTimers,
  }: { minGapMs?: number; isHidden?: () => boolean; timers?: Timers } = {},
) {
  let lastRun = Number.NEGATIVE_INFINITY;
  let timer: unknown = null;
  let deferredWhileHidden = false;

  const fire = () => {
    timer = null;
    if (isHidden()) {
      deferredWhileHidden = true;
      return;
    }
    lastRun = timers.now();
    run();
  };

  return {
    request() {
      if (isHidden()) {
        deferredWhileHidden = true;
        return;
      }
      if (timer !== null) return;
      const wait = lastRun + minGapMs - timers.now();
      if (wait <= 0) fire();
      else timer = timers.setTimeout(fire, wait);
    },
    onVisible() {
      if (!deferredWhileHidden || isHidden()) return;
      deferredWhileHidden = false;
      this.request();
    },
    cancel() {
      if (timer !== null) timers.clearTimeout(timer);
      timer = null;
      deferredWhileHidden = false;
    },
  };
}

export type StreamInvalidationOptions = {
  /** Subscriber URL; `?channels=` is applied locally on the shared stream. */
  url?: string;
  /** Channels that can change this pane's data. Omitted = every channel. */
  channels?: readonly string[];
  /** Return false to ignore a `message` frame (e.g. an unrelated channel). */
  accept?: (event: MessageEvent) => boolean;
  /** Also refetch when the stream reconnects after a drop. */
  onReconnect?: boolean;
  minGapMs?: number;
};

/**
 * Refetch a pane's queries when the hub event stream says something changed,
 * at most once per `minGapMs` and never while the tab is hidden (a hidden
 * tab's last request is replayed once when it becomes visible again).
 */
export function useStreamInvalidation(
  invalidate: () => void,
  {
    url = EVENTS_STREAM_URL,
    channels,
    accept,
    onReconnect = false,
    minGapMs,
  }: StreamInvalidationOptions = {},
): void {
  const subscriberUrl = channels
    ? `${EVENTS_STREAM_URL}?channels=${encodeURIComponent(channels.join(","))}`
    : url;
  const invalidateRef = useRef(invalidate);
  const acceptRef = useRef(accept);
  invalidateRef.current = invalidate;
  acceptRef.current = accept;

  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const invalidator = createCoalescedInvalidator(() => invalidateRef.current(), {
      ...(minGapMs === undefined ? {} : { minGapMs }),
    });
    let opened = false;
    const onVisibilityChange = () => invalidator.onVisible();

    const closeStream = subscribeEventStream(subscriberUrl, (stream) => {
      stream.addEventListener("open", () => {
        // EventSource fires `open` on first connect and on every reconnect.
        if (opened && onReconnect) invalidator.request();
        opened = true;
      });
      stream.addEventListener("message", (event: Event) => {
        const filter = acceptRef.current;
        if (filter && !filter(event as MessageEvent)) return;
        invalidator.request();
      });
      stream.addEventListener("error", () => {
        // EventSource reconnects on its own, or openEventStream re-opens it
        // after a refusal; leave the query cache intact.
      });
    });
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      invalidator.cancel();
      closeStream();
    };
  }, [subscriberUrl, onReconnect, minGapMs]);
}
