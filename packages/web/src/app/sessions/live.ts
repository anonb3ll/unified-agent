import { useEffect, useRef, useState } from "react";
import { subscribeEventStream } from "../../lib/eventStream";
import { EVENTS_STREAM_URL } from "../../lib/eventStreamTransport";
import { parseActivityEvent, type ActivityEvent } from "../../lib/sessions/types";

export type LiveState = "connecting" | "live" | "reconnecting" | "off";

export const LIVE_TEXT: Record<LiveState, string> = {
  connecting: "Connecting to live updates",
  live: "Live: new messages appear as they happen",
  reconnecting: "Live updates paused, reconnecting. Messages will catch up when the connection returns.",
  off: "Live updates are not available in this browser. Reload to see new messages.",
};

/**
 * One subscription to the shared event stream (one connection per tab,
 * `src/eventStream.ts`): the connection state for the page, and every parsed
 * event on `channels` handed to `onEvent`. `onEvent` is held in a ref, so a
 * new callback never reopens the subscription.
 */
export function useLiveEvents(
  channels: readonly string[],
  onEvent?: (event: ActivityEvent) => void,
  /** Called when the stream opens or reopens (catch up on what was missed). */
  onConnect?: () => void,
): LiveState {
  const [state, setState] = useState<LiveState>(() => (typeof EventSource === "undefined" ? "off" : "connecting"));
  const ref = useRef(onEvent);
  ref.current = onEvent;
  const connectRef = useRef(onConnect);
  connectRef.current = onConnect;
  const key = channels.join(",");
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const url = key ? `${EVENTS_STREAM_URL}?channels=${encodeURIComponent(key)}` : EVENTS_STREAM_URL;
    return subscribeEventStream(url, (stream) => {
      stream.addEventListener("open", () => {
        setState("live");
        connectRef.current?.();
      });
      stream.addEventListener("cursor", () => {
        setState("live");
        connectRef.current?.();
      });
      stream.addEventListener("error", () => setState("reconnecting"));
      stream.addEventListener("message", (raw) => {
        const event = parseActivityEvent((raw as MessageEvent).data);
        if (!event) return;
        setState("live");
        ref.current?.(event);
      });
    });
  }, [key]);
  return state;
}
