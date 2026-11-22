import { useEffect, useState } from "react";
import { subscribeEventStream } from "../../lib/eventStream";
import { EVENTS_STREAM_URL } from "../../lib/eventStreamTransport";

type StreamState = "connecting" | "live" | "reconnecting" | "off";

/** Live-update connection, from the shared event stream (one per tab). */
export function useStreamState(): StreamState {
  const [state, setState] = useState<StreamState>(() => (typeof EventSource === "undefined" ? "off" : "connecting"));
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    return subscribeEventStream(EVENTS_STREAM_URL, (stream) => {
      stream.addEventListener("open", () => setState("live"));
      stream.addEventListener("cursor", () => setState("live"));
      stream.addEventListener("error", () => setState("reconnecting"));
    });
  }, []);
  return state;
}

const STREAM_TEXT: Record<StreamState, { glyph: string; text: string; tone: string }> = {
  connecting: { glyph: "○", text: "Connecting to live updates", tone: "text-[var(--ua-muted)]" },
  live: { glyph: "●", text: "Live updates on", tone: "text-[var(--ua-ok)]" },
  reconnecting: { glyph: "◷", text: "Live updates reconnecting", tone: "text-[var(--ua-warn)]" },
  off: { glyph: "○", text: "Live updates off", tone: "text-[var(--ua-muted)]" },
};

/** Hub connection, last refresh and version (spec §4). */
export function StatusBar({ hubVersion, refreshedAt }: { hubVersion: string | null; refreshedAt: number }) {
  const stream = STREAM_TEXT[useStreamState()];
  return (
    <footer
      aria-label="Connection status"
      className="flex h-7 shrink-0 items-center gap-4 overflow-hidden whitespace-nowrap border-t border-[var(--ua-border)] bg-[var(--ua-surface)] px-3.5 font-mono text-[11px] text-[var(--ua-muted)] max-md:mb-14"
    >
      <span role="status" className={stream.tone}>
        <span aria-hidden="true">{stream.glyph} </span>
        {stream.text}
      </span>
      {refreshedAt > 0 && <span className="max-md:hidden">Checked in at {new Date(refreshedAt).toLocaleTimeString()}</span>}
      <span className="ml-auto truncate">{hubVersion ? `Hub ${hubVersion}` : "Hub version unknown"}</span>
    </footer>
  );
}
