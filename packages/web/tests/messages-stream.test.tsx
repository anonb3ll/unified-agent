import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MESSAGES_QUERY_KEY, handoffsQueryKey } from "../src/lib/messages/api";
import { useMessagesStream } from "../src/lib/messages/useMessages";

afterEach(() => {
  vi.useRealTimers();
  focusManager.setFocused(undefined);
  vi.unstubAllGlobals();
});

class Source {
  static instances: Source[] = [];
  readyState = 0;
  target = new EventTarget();
  constructor(public url: string) {
    Source.instances.push(this);
  }
  addEventListener(type: string, listener: EventListener) {
    this.target.addEventListener(type, listener);
  }
  close() {
    this.readyState = 2;
  }
  emit(channel: string) {
    this.target.dispatchEvent(
      new MessageEvent("message", { data: JSON.stringify({ channel }) }),
    );
  }
}

describe("Messages stream (task #1426)", () => {
  it("refreshes the handoff list when another caller hands the thread off", () => {
    Source.instances = [];
    vi.stubGlobal("EventSource", Source);
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { unmount } = renderHook(() => useMessagesStream(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    const [stream] = Source.instances;
    stream.emit("agent.task.updated");
    expect(invalidate).not.toHaveBeenCalled();
    stream.emit("agent.conversation.handoff");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: MESSAGES_QUERY_KEY });
    // The handoff list lives under the Messages key, so this refreshes it.
    expect(
      handoffsQueryKey("conv-1").slice(0, MESSAGES_QUERY_KEY.length),
    ).toEqual([...MESSAGES_QUERY_KEY]);
    unmount();
  });
});
