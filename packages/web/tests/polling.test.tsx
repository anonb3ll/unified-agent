/**
 * Calm polling across the Control Center (task #1850).
 *
 * After a hub restart one tab drove ~1,000 gateway calls in ten minutes: panes
 * refetched on every hub event, polls kept full rate while the hub failed, and
 * the doctor pane re-ran system_doctor on each event. These tests pin the
 * shared rules: backoff on failure, coalesced and visibility-gated stream
 * refetches, and one attention poll shared by every reader.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ATTENTION_POLL_MS, attentionQueryOptions } from "../src/lib/attention/useAttention";
import {
  CALM_QUERY_DEFAULTS,
  POLL_BACKOFF_MAX_MS,
  STREAM_MIN_GAP_MS,
  STREAM_RECONNECT_MS,
  backoffRefetchInterval,
  openEventStream,
  pollDelay,
  queryRetryDelay,
  useStreamInvalidation,
} from "../src/lib/polling";

class MockEventSource {
  static instances: MockEventSource[] = [];
  listeners = new Map<string, Set<(e: Event) => void>>();
  closed = false;
  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (e: Event) => void) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(listener);
  }
  emit(type: string, data?: string) {
    const event =
      type === "message" ? new MessageEvent("message", { data: data ?? "{}" }) : new Event(type);
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
  close() {
    this.closed = true;
  }
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  setHidden(false);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("poll backoff", () => {
  it("doubles per consecutive failure and caps at the ceiling", () => {
    expect(pollDelay(15_000, 0)).toBe(15_000);
    expect(pollDelay(15_000, 1)).toBe(30_000);
    expect(pollDelay(15_000, 3)).toBe(120_000);
    expect(pollDelay(15_000, 40)).toBe(POLL_BACKOFF_MAX_MS);
    expect(pollDelay(15_000, Number.NaN)).toBe(15_000);
    // A steady interval above the ceiling is never shortened by an error.
    expect(pollDelay(10 * 60_000, 2)).toBe(10 * 60_000);
  });

  it("backs off while reads fail and returns to steady after a success", () => {
    const interval = backoffRefetchInterval(30_000);
    const query = { state: { status: "success", errorUpdateCount: 0 } } as Parameters<
      typeof interval
    >[0];
    const step = (status: string, errorUpdateCount: number) => {
      (query as { state: unknown }).state = { status, errorUpdateCount };
      return interval(query);
    };
    expect(step("success", 0)).toBe(30_000);
    expect(step("error", 1)).toBe(60_000);
    expect(step("error", 2)).toBe(120_000);
    expect(step("success", 2)).toBe(30_000);
    expect(step("error", 3)).toBe(60_000);
  });

  it("retries a failed read once, with growing delays, and never polls a hidden tab", () => {
    expect(CALM_QUERY_DEFAULTS.retry).toBe(1);
    expect(CALM_QUERY_DEFAULTS.refetchIntervalInBackground).toBe(false);
    expect(CALM_QUERY_DEFAULTS.refetchOnWindowFocus).toBe(false);
    expect(queryRetryDelay(0)).toBe(2_000);
    expect(queryRetryDelay(1)).toBe(4_000);
    expect(queryRetryDelay(20)).toBe(30_000);
  });
});

describe("useStreamInvalidation", () => {
  it("folds a burst of hub events into one immediate and one trailing refetch", () => {
    vi.useFakeTimers();
    const invalidate = vi.fn();
    const { unmount } = renderHook(() => useStreamInvalidation(invalidate));
    const stream = MockEventSource.instances[0];
    expect(stream.url).toBe("/api/events/stream");

    for (let i = 0; i < 200; i += 1) stream.emit("message");
    expect(invalidate).toHaveBeenCalledTimes(1);
    act(() => {
      vi.advanceTimersByTime(STREAM_MIN_GAP_MS);
    });
    expect(invalidate).toHaveBeenCalledTimes(2);
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(invalidate).toHaveBeenCalledTimes(2);

    unmount();
    expect(stream.closed).toBe(true);
  });

  it("holds events while the tab is hidden and replays one when it is shown", () => {
    const invalidate = vi.fn();
    renderHook(() => useStreamInvalidation(invalidate));
    const stream = MockEventSource.instances[0];

    act(() => setHidden(true));
    for (let i = 0; i < 50; i += 1) stream.emit("message");
    expect(invalidate).not.toHaveBeenCalled();

    act(() => setHidden(false));
    expect(invalidate).toHaveBeenCalledTimes(1);
  });

  it("ignores frames the pane does not care about", () => {
    const invalidate = vi.fn();
    renderHook(() =>
      useStreamInvalidation(invalidate, {
        accept: (event) => String(event.data).includes("agent.task."),
      }),
    );
    const stream = MockEventSource.instances[0];
    stream.emit("message", JSON.stringify({ channel: "agent.node.heartbeat" }));
    expect(invalidate).not.toHaveBeenCalled();
    stream.emit("message", JSON.stringify({ channel: "agent.task.updated" }));
    expect(invalidate).toHaveBeenCalledTimes(1);
  });

  it("refetches on a reconnect only when asked to", () => {
    const invalidate = vi.fn();
    renderHook(() => useStreamInvalidation(invalidate, { onReconnect: true }));
    const stream = MockEventSource.instances[0];
    stream.emit("open");
    expect(invalidate).not.toHaveBeenCalled();
    stream.emit("open");
    expect(invalidate).toHaveBeenCalledTimes(1);
  });
});

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("attention polling", () => {
  it("is one shared, calm definition", () => {
    const options = attentionQueryOptions();
    expect(ATTENTION_POLL_MS).toBeGreaterThanOrEqual(60_000);
    expect(options.refetchIntervalInBackground).toBe(false);
    const healthy = { state: { status: "success", errorUpdateCount: 0 } } as never;
    expect((options.refetchInterval as (q: never) => number)(healthy)).toBe(ATTENTION_POLL_MS);
  });

  it("refetches attention once for a burst of hub events", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        calls += 1;
        return {
          ok: true,
          status: 200,
          json: async () => ({ items: [], informational: [], counts: {}, total: 0, errors: [] }),
          text: async () => "",
        };
      }),
    );
    const { useAttention } = await import("../src/lib/attention/useAttention");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function Probe() {
      useAttention();
      return null;
    }
    render(<Probe />, { wrapper: wrapper(client) });
    await vi.waitFor(() =>
      expect(client.getQueryState(["attention"])?.fetchStatus).toBe("idle"),
    );
    expect(calls).toBe(1);

    const stream = MockEventSource.instances[0];
    for (let i = 0; i < 100; i += 1) stream.emit("message", JSON.stringify({ channel: "agent.task.updated" }));
    await vi.waitFor(() => expect(calls).toBe(2));
    await new Promise((r) => setTimeout(r, 20));
    expect(calls).toBe(2);
  });
});

describe("openEventStream (hub refusal, task #1811)", () => {
  /** An EventSource whose connection state the test drives, as a browser does. */
  class StatefulEventSource {
    static instances: StatefulEventSource[] = [];
    readyState = 0;
    closed = false;
    listeners = new Map<string, Set<(e: Event) => void>>();
    constructor(public url: string) {
      StatefulEventSource.instances.push(this);
    }
    addEventListener(type: string, listener: (e: Event) => void) {
      if (!this.listeners.has(type)) this.listeners.set(type, new Set());
      this.listeners.get(type)!.add(listener);
    }
    close() {
      this.closed = true;
      this.readyState = 2;
    }
    fire(event: Event) {
      for (const listener of this.listeners.get(event.type) ?? []) listener(event);
    }
    /** A non-200 reply (the hub's 429): the browser fails the connection for good. */
    refuse() {
      this.readyState = 2;
      this.fire(new Event("error"));
    }
    open() {
      this.readyState = 1;
      this.fire(new Event("open"));
    }
  }

  beforeEach(() => {
    StatefulEventSource.instances = [];
    vi.stubGlobal("EventSource", StatefulEventSource);
    vi.useFakeTimers();
  });

  const latest = () => StatefulEventSource.instances[StatefulEventSource.instances.length - 1];

  it("re-opens a refused stream after a delay that doubles, never hot-looping", () => {
    const stop = openEventStream("/api/events/stream", () => {});
    expect(StatefulEventSource.instances).toHaveLength(1);

    latest().refuse();
    vi.advanceTimersByTime(STREAM_RECONNECT_MS - 1);
    expect(StatefulEventSource.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(StatefulEventSource.instances).toHaveLength(2);

    latest().refuse();
    vi.advanceTimersByTime(2 * STREAM_RECONNECT_MS - 1);
    expect(StatefulEventSource.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(StatefulEventSource.instances).toHaveLength(3);

    for (let i = 0; i < 10; i++) {
      latest().refuse();
      vi.advanceTimersByTime(POLL_BACKOFF_MAX_MS);
    }
    // Capped at the ceiling: a refusal every five minutes at most.
    const before = StatefulEventSource.instances.length;
    latest().refuse();
    vi.advanceTimersByTime(POLL_BACKOFF_MAX_MS - 1);
    expect(StatefulEventSource.instances).toHaveLength(before);
    stop();
  });

  it("resets the backoff once a stream opens", () => {
    const stop = openEventStream("/api/events/stream", () => {});
    latest().refuse();
    vi.advanceTimersByTime(STREAM_RECONNECT_MS);
    latest().refuse();
    vi.advanceTimersByTime(2 * STREAM_RECONNECT_MS);
    latest().open();
    latest().refuse();
    vi.advanceTimersByTime(STREAM_RECONNECT_MS);
    expect(StatefulEventSource.instances).toHaveLength(4);
    stop();
  });

  it("leaves an ordinary drop to the browser's own reconnect", () => {
    const stop = openEventStream("/api/events/stream", () => {});
    // CONNECTING again after a drop: the browser retries by itself.
    latest().readyState = 0;
    latest().fire(new Event("error"));
    vi.advanceTimersByTime(10 * POLL_BACKOFF_MAX_MS);
    expect(StatefulEventSource.instances).toHaveLength(1);
    stop();
  });

  it("resumes a re-opened stream from the last event id it saw", () => {
    const seen: string[] = [];
    const stop = openEventStream("/api/events/stream?channels=agent.task.*", (stream) => {
      stream.addEventListener("message", (e) => seen.push((e as MessageEvent).data));
    });
    latest().fire(new MessageEvent("message", { data: "a", lastEventId: "01J8ZQ4T7X9K2M5N7P8R0S3T6W" }));
    latest().refuse();
    vi.advanceTimersByTime(STREAM_RECONNECT_MS);
    expect(latest().url).toBe(
      "/api/events/stream?channels=agent.task.*&since=01J8ZQ4T7X9K2M5N7P8R0S3T6W",
    );
    // The caller's listeners are attached to the re-opened stream too.
    latest().fire(new MessageEvent("message", { data: "b" }));
    expect(seen).toEqual(["a", "b"]);

    // An opening-frame id resumes by seq.
    latest().fire(new MessageEvent("cursor", { lastEventId: "seq:42" }));
    latest().refuse();
    vi.advanceTimersByTime(2 * STREAM_RECONNECT_MS);
    expect(latest().url).toBe("/api/events/stream?channels=agent.task.*&after_seq=42");
    stop();
  });

  it("replaces a cursor already in the caller's URL instead of adding a second", () => {
    const stop = openEventStream("/api/events/stream?since=OLD&channels=agent.node", () => {});
    latest().fire(new MessageEvent("message", { data: "a", lastEventId: "seq:7" }));
    latest().refuse();
    vi.advanceTimersByTime(STREAM_RECONNECT_MS);
    expect(latest().url).toBe("/api/events/stream?channels=agent.node&after_seq=7");

    latest().fire(new MessageEvent("message", { data: "b", lastEventId: "01J8ZQ4T7X9K2M5N7P8R0S3T6W" }));
    latest().refuse();
    vi.advanceTimersByTime(2 * STREAM_RECONNECT_MS);
    expect(latest().url).toBe(
      "/api/events/stream?channels=agent.node&since=01J8ZQ4T7X9K2M5N7P8R0S3T6W",
    );
    stop();
  });

  it("stops for good on cleanup, including a pending re-open", () => {
    const stop = openEventStream("/api/events/stream", () => {});
    latest().refuse();
    stop();
    vi.advanceTimersByTime(10 * POLL_BACKOFF_MAX_MS);
    expect(StatefulEventSource.instances).toHaveLength(1);
    expect(StatefulEventSource.instances[0].closed).toBe(true);
  });

  it("useStreamInvalidation survives a refusal and keeps invalidating after the re-open", () => {
    const invalidate = vi.fn();
    renderHook(() => useStreamInvalidation(invalidate, { minGapMs: 1 }));
    latest().refuse();
    vi.advanceTimersByTime(STREAM_RECONNECT_MS);
    expect(StatefulEventSource.instances).toHaveLength(2);
    act(() => {
      latest().fire(new MessageEvent("message", { data: "{}" }));
      vi.advanceTimersByTime(10);
    });
    expect(invalidate).toHaveBeenCalled();
  });
});
