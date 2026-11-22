import type { ReactNode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { Transcript } from "../../../src/app/sessions/Transcript";
import type { TranscriptEntry } from "../../../src/app/sessions/model";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useItemTranscript } from "../../../src/app/sessions/itemTranscript";
import type { ActivityEvent } from "../../../src/lib/sessions/types";
import { mockFetch } from "../support/fetchMock";
import { testQueryClient } from "../support/render";
import { acpEvent, eventsPayload } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={testQueryClient()}>{children}</QueryClientProvider>;
}

function liveEvent(n: number, itemId = "item-1"): ActivityEvent {
  const e = acpEvent("agent_message_chunk", { text: `chunk ${n}`, item_id: itemId });
  return { ...e, seq: 10_000 + n, ulid: `01LIVE${String(n).padStart(8, "0")}` } as ActivityEvent;
}

describe("useItemTranscript (review 7, 8)", () => {
  it("live events and a (re)connection refresh durable history, so nothing beyond the live buffer is lost", async () => {
    const reads: string[] = [];
    let durable = eventsPayload();
    mockFetch({
      // Pages like the hub: rows after `after_seq`, at most `limit`, with the last seq as the cursor.
      "GET /api/events": ({ url }) => {
        const params = new URL(url, "http://x").searchParams;
        const after = Number(params.get("after_seq") ?? 0);
        const limit = Number(params.get("limit") ?? 500);
        reads.push(String(after));
        const rows = durable.events.filter((e) => e.seq > after).slice(0, limit);
        return { body: { events: rows, seq_cursor: rows.length ? rows[rows.length - 1].seq : after } };
      },
    });
    const { result } = renderHook(() => useItemTranscript("item-1", { minGapMs: 0 }), { wrapper });
    await waitFor(() => expect(result.current.history.isSuccess).toBe(true));
    expect(reads).toHaveLength(1);

    // 600 live events: more than the live buffer keeps. Each one asks for a refresh.
    const burst = Array.from({ length: 600 }, (_, i) => liveEvent(i));
    durable = { events: burst.map((e) => ({ ...e })) as typeof durable.events, seq_cursor: 10_599 };
    act(() => {
      for (const e of burst) result.current.push(e);
    });
    await waitFor(() => expect(reads.length).toBeGreaterThan(1));
    // The refresh reads after the stored cursor, not from zero.
    expect(Number(reads[1])).toBeGreaterThan(0);
    await waitFor(() => expect(result.current.events.some((e) => (e.payload as { text?: string }).text === "chunk 0")).toBe(true));
    expect(result.current.truncated).toBe(false);

    // A reconnection asks for the events missed while disconnected.
    const before = reads.length;
    act(() => result.current.connected());
    await waitFor(() => expect(reads.length).toBe(before + 1));
  });

  it("ignores events for another item", async () => {
    mockFetch({ "GET /api/events": { body: { events: [], seq_cursor: 0 } } });
    const { result } = renderHook(() => useItemTranscript("item-1", { minGapMs: 0 }), { wrapper });
    await waitFor(() => expect(result.current.history.isSuccess).toBe(true));
    act(() => {
      expect(result.current.push(liveEvent(1, "item-2"))).toBe(false);
    });
    expect(result.current.events).toEqual([]);
  });

  it("moving to another session's item drops the previous item's live messages and files (review 8)", async () => {
    mockFetch({ "GET /api/events": { body: { events: [], seq_cursor: 0 } } });
    const { result, rerender } = renderHook(({ id }: { id: string }) => useItemTranscript(id, { minGapMs: 0 }), {
      wrapper,
      initialProps: { id: "item-1" },
    });
    await waitFor(() => expect(result.current.history.isSuccess).toBe(true));
    act(() => {
      result.current.push(liveEvent(1, "item-1"));
    });
    expect(result.current.events).toHaveLength(1);
    rerender({ id: "item-2" });
    await waitFor(() => expect(result.current.history.isSuccess).toBe(true));
    expect(result.current.events).toEqual([]);
  });

  it("shows only the current item's events even when the history read returns others", async () => {
    const own = acpEvent("agent_message_chunk", { text: "mine", item_id: "item-1" });
    const other = acpEvent("agent_message_chunk", { text: "theirs", item_id: "item-9" });
    mockFetch({ "GET /api/events": { body: { events: [own, other], seq_cursor: other.seq } } });
    const { result } = renderHook(() => useItemTranscript("item-1", { minGapMs: 0 }), { wrapper });
    await waitFor(() => expect(result.current.events.length).toBeGreaterThan(0));
    expect(result.current.events.map((e) => (e.payload as { text: string }).text)).toEqual(["mine"]);
  });
});

describe("Transcript announcements (review 16)", () => {
  const msg = (id: string, text: string): TranscriptEntry => ({ id, role: "agent", author: "Desktop_Agent2", text, at: "2026-11-10T11:00:00Z" });

  it("announces each new message separately, even from the same agent, and when an old one is dropped at the same time", () => {
    const { rerender } = render(<Transcript entries={[msg("m1", "one")]} agent="Desktop_Agent2" />);
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent("");
    rerender(<Transcript entries={[msg("m1", "one"), msg("m2", "two")]} agent="Desktop_Agent2" />);
    rerender(<Transcript entries={[msg("m1", "one"), msg("m2", "two"), msg("m3", "three")]} agent="Desktop_Agent2" />);
    // Retention drops m1 as m4 arrives: the count stays the same.
    rerender(<Transcript entries={[msg("m2", "two"), msg("m3", "three"), msg("m4", "four")]} agent="Desktop_Agent2" />);
    const items = within(region).getAllByText(/New message from Desktop_Agent2/);
    expect(items.map((i) => i.textContent)).toEqual([
      "New message from Desktop_Agent2: two",
      "New message from Desktop_Agent2: three",
      "New message from Desktop_Agent2: four",
    ]);
  });
});
