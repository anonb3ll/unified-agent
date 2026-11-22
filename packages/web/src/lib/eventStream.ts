/** One authenticated event connection per tab, with local channel subscriptions. */
import { EVENTS_STREAM_URL, openEventStream } from "./eventStreamTransport";

type Subscription = {
  channels: string[];
  target: EventTarget;
};
type SharedStream = {
  subscribers: Set<Subscription>;
  stop: () => void;
  connected: boolean;
  cursor: Event | null;
};

let shared: SharedStream | null = null;

function deliver(subscription: Subscription, type: string, event: Event) {
  // Clone: a browser event cannot be dispatched again while its native
  // EventSource dispatch is in progress. EventTarget also isolates listeners
  // (one throwing subscriber must not prevent another pane receiving a frame).
  const message = event as MessageEvent;
  subscription.target.dispatchEvent(
    type === "message" || type === "cursor"
      ? new MessageEvent(type, { data: message.data, lastEventId: message.lastEventId })
      : new Event(type),
  );
}

function matches(subscription: Subscription, event: Event): boolean {
  if (subscription.channels.length === 0) return true;
  try {
    const { channel } = JSON.parse(String((event as MessageEvent).data));
    if (typeof channel !== "string") return false;
    return subscription.channels.some((pattern) =>
      pattern.endsWith("*")
        ? channel.startsWith(pattern.slice(0, -1))
        : channel === pattern,
    );
  } catch {
    return false;
  }
}

/**
 * `channels` in the subscriber URL filter frames locally. The transport stays
 * unfiltered so mounting a new pane never reopens it or loses events between
 * two connections. Auth/profile filtering still runs on the hub. This is a
 * live subscription, not historical replay; historical queries keep their own
 * cursors. The transport retains its existing refusal backoff and resume cursor.
 *
 * Listeners attach once and survive reconnects. The last subscriber closes the
 * socket and cancels retries; removing one pane never disconnects another.
 */
export function subscribeEventStream(
  url: string,
  attach: (stream: Pick<EventTarget, "addEventListener">) => void,
): () => void {
  const params = new URLSearchParams(url.split("?", 2)[1] ?? "");
  const subscription: Subscription = {
    channels: (params.get("channels") ?? "")
      .split(",")
      .map((c) => c.trim())
      .filter(Boolean),
    target: new EventTarget(),
  };
  attach(subscription.target);

  if (!shared) {
    const state: SharedStream = {
      subscribers: new Set(),
      stop: () => {},
      connected: false,
      cursor: null,
    };
    shared = state;
    state.subscribers.add(subscription);
    state.stop = openEventStream(EVENTS_STREAM_URL, (stream) => {
      for (const type of ["open", "cursor", "message", "error"]) {
        stream.addEventListener(type, (event) => {
          if (type === "open" || type === "cursor") state.connected = true;
          if (type === "cursor") state.cursor = event;
          if (type === "error") {
            state.connected = false;
            state.cursor = null;
          }
          for (const subscriber of state.subscribers) {
            if (type !== "message" || matches(subscriber, event)) deliver(subscriber, type, event);
          }
        });
      }
    });
  } else {
    shared.subscribers.add(subscription);
    // A pane mounted after the opening frame should also show "connected".
    if (shared.connected) deliver(subscription, "open", new Event("open"));
    if (shared.cursor) deliver(subscription, "cursor", shared.cursor);
  }

  const state = shared;
  return () => {
    state.subscribers.delete(subscription);
    if (state.subscribers.size === 0 && shared === state) {
      state.stop();
      shared = null;
    }
  };
}
