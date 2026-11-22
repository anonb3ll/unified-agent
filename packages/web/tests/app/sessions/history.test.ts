/**
 * Durable history of one device work item (tasks #1992, #2010), moved from the
 * classic session transcript's tests when its pane was deleted (spec §10).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { fetchEvents } from "../../../src/lib/api/nodes";
import { loadItemHistory, scanItemHistory, transcriptWindow } from "../../../src/app/sessions/nodeHistory";

describe("transcript history reload (task #1992)", () => {
  afterEach(() => vi.unstubAllGlobals());

  const row = (seq: number, itemId: string) => ({
    seq,
    ulid: `01J${String(seq).padStart(23, "0")}`,
    channel: "agent.node",
    payload: {
      kind: "acp_event",
      acp_kind: "turn_checkpoint",
      item_id: itemId,
    },
    originator: "node",
    created_at: null,
  });

  it("asks the hub for the item's own events by item_id", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ events: [row(7, "nd-1")], seq_cursor: 7 }),
    }));
    vi.stubGlobal("fetch", fetchMock);
    const events = await loadItemHistory("nd-1");
    const url = new URL(
      String((fetchMock.mock.calls[0] as unknown[])[0]),
      "http://hub",
    );
    expect(url.pathname).toBe("/api/events");
    expect(url.searchParams.get("item_id")).toBe("nd-1");
    expect(url.searchParams.get("channels")).toBe("agent.node");
    expect(url.searchParams.get("after_seq")).toBe("0");
    expect(events.map((e) => e.ulid)).toEqual([row(7, "nd-1").ulid]);
  });

  it("pages by seq until the item is covered", async () => {
    const pages = [
      Array.from({ length: 500 }, (_, i) => row(i + 1, "nd-1")),
      [row(900, "nd-1")],
    ];
    const fetcher = vi.fn(async (opts: { afterSeq?: number }) => {
      const page = opts.afterSeq === 0 ? pages[0] : pages[1];
      return {
        events: page as Awaited<ReturnType<typeof fetchEvents>>["events"],
        seqCursor: page[page.length - 1].seq,
      };
    });
    const events = await loadItemHistory(
      "nd-1",
      undefined,
      fetcher as unknown as typeof fetchEvents,
    );
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls[1][0]).toMatchObject({
      afterSeq: 500,
      itemId: "nd-1",
    });
    expect(events).toHaveLength(501);
  });
});

describe("long transcript recovery (#2010)", () => {
  const row = (seq: number) => ({
    seq,
    ulid: `01J${String(seq).padStart(23, "0")}`,
    channel: "agent.node",
    payload: {
      item_id: "nd-1",
      kind: "acp_event",
      acp_kind: seq === 10001 ? "turn_checkpoint" : "agent_message_chunk",
    },
    originator: "node",
    created_at: null,
  });
  it.each([5001, 10001])(
    "keeps the latest checkpoint in %i events with bounded retained history",
    async (total) => {
      const fetcher = vi.fn(async (opts: { afterSeq?: number }) => {
        const start = opts.afterSeq ?? 0;
        const events = Array.from(
          { length: Math.min(500, total - start) },
          (_, i) => ({
            ...row(start + i + 1),
            payload: {
              ...row(start + i + 1).payload,
              acp_kind:
                start + i + 1 === total
                  ? "turn_checkpoint"
                  : "agent_message_chunk",
            },
          }),
        );
        return { events, seqCursor: events.at(-1)?.seq ?? null };
      });
      const events = await loadItemHistory(
        "nd-1",
        undefined,
        fetcher as unknown as typeof fetchEvents,
      );
      expect(fetcher).toHaveBeenCalledTimes(Math.ceil(total / 500));
      expect(events).toHaveLength(5000);
      expect(events.at(-1)?.payload).toMatchObject({
        acp_kind: "turn_checkpoint",
      });
      expect(events[0].ulid).toBe(row(total - 4999).ulid);
    },
  );
  it("retains the last checkpoint when a new turn streams past the window", async () => {
    const fetcher = vi.fn(async (opts: { afterSeq?: number }) => {
      const start = opts.afterSeq ?? 0;
      const events = Array.from(
        { length: Math.min(500, 6001 - start) },
        (_, i) => ({
          ...row(start + i + 1),
          payload: {
            ...row(start + i + 1).payload,
            acp_kind:
              start + i + 1 === 1 ? "turn_checkpoint" : "agent_message_chunk",
          },
        }),
      );
      return { events, seqCursor: events.at(-1)?.seq ?? null };
    });
    const events = await loadItemHistory(
      "nd-1",
      undefined,
      fetcher as unknown as typeof fetchEvents,
    );
    expect(events).toHaveLength(5001);
    expect(events[0].payload).toMatchObject({ acp_kind: "turn_checkpoint" });
    expect(events.at(-1)?.ulid).toBe(row(6001).ulid);
  });
  it("refuses a stalled cursor rather than reporting incomplete history", async () => {
    const fetcher = vi.fn(async () => ({
      events: Array.from({ length: 500 }, (_, i) => row(i + 1)),
      seqCursor: 0,
    }));
    await expect(
      loadItemHistory(
        "nd-1",
        undefined,
        fetcher as unknown as typeof fetchEvents,
      ),
    ).rejects.toThrow(/cursor/);
  });
  it("stops pagination when the query is aborted", async () => {
    const controller = new AbortController();
    const fetcher = vi.fn(async () => {
      controller.abort();
      return {
        events: Array.from({ length: 500 }, (_, i) => row(i + 1)),
        seqCursor: 500,
      };
    });
    await expect(
      loadItemHistory(
        "nd-1",
        controller.signal,
        fetcher as unknown as typeof fetchEvents,
      ),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

describe("incremental transcript refresh and window (#2010 review)", () => {
  const row = (seq: number, acpKind = "agent_message_chunk") => ({
    seq,
    ulid: `01J${String(seq).padStart(23, "0")}`,
    channel: "agent.node",
    payload: { item_id: "nd-1", kind: "acp_event", acp_kind: acpKind },
    originator: "node",
    created_at: null,
  });
  const pagedFetcher = (total: number, checkpointAt: number[] = []) =>
    vi.fn(async (opts: { afterSeq?: number }) => {
      const start = opts.afterSeq ?? 0;
      const events = Array.from(
        { length: Math.max(0, Math.min(500, total - start)) },
        (_, i) =>
          row(
            start + i + 1,
            checkpointAt.includes(start + i + 1)
              ? "turn_checkpoint"
              : "agent_message_chunk",
          ),
      );
      return { events, seqCursor: events.at(-1)?.seq ?? null };
    });

  it("refreshes from the stored durable cursor, not from sequence zero", async () => {
    const first = pagedFetcher(10001, [10001]);
    const initial = await scanItemHistory(
      "nd-1",
      undefined,
      first as unknown as typeof fetchEvents,
    );
    expect(first).toHaveBeenCalledTimes(21);
    expect(initial.seqCursor).toBe(10001);

    const next = pagedFetcher(10002);
    const refreshed = await scanItemHistory(
      "nd-1",
      undefined,
      next as unknown as typeof fetchEvents,
      initial,
    );
    expect(next).toHaveBeenCalledTimes(1);
    expect(next.mock.calls[0][0]).toMatchObject({ afterSeq: 10001 });
    expect(refreshed.seqCursor).toBe(10002);
    expect(refreshed.window).toHaveLength(5000);
    expect(refreshed.window.at(-1)?.ulid).toBe(row(10002).ulid);
    expect(refreshed.latestCheckpoint?.ulid).toBe(row(10001).ulid);
    expect(refreshed.truncated).toBe(true);
  });

  it("keeps the cursor when a refresh finds nothing new", async () => {
    const initial = await scanItemHistory(
      "nd-1",
      undefined,
      pagedFetcher(3) as unknown as typeof fetchEvents,
    );
    const idle = vi.fn(async () => ({ events: [], seqCursor: null }));
    const refreshed = await scanItemHistory(
      "nd-1",
      undefined,
      idle as unknown as typeof fetchEvents,
      initial,
    );
    expect(refreshed.seqCursor).toBe(3);
    expect(refreshed.window).toHaveLength(3);
  });

  it("does not call exactly 5,000 events a window", async () => {
    const history = await scanItemHistory(
      "nd-1",
      undefined,
      pagedFetcher(5000) as unknown as typeof fetchEvents,
    );
    expect(history.window).toHaveLength(5000);
    expect(history.truncated).toBe(false);
    expect(transcriptWindow(history, []).truncated).toBe(false);
  });

  it("bounds history plus live events to the window, keeping the checkpoint", async () => {
    const history = await scanItemHistory(
      "nd-1",
      undefined,
      pagedFetcher(5000, [1]) as unknown as typeof fetchEvents,
    );
    const live = Array.from({ length: 200 }, (_, i) => row(5001 + i));
    const { events, truncated } = transcriptWindow(history, live);
    // Newest first: 5,000 newest rows, then the checkpoint they pushed out.
    expect(events).toHaveLength(5001);
    expect(events[0].ulid).toBe(row(5200).ulid);
    expect(events[4999].ulid).toBe(row(201).ulid);
    expect(events[5000].payload).toMatchObject({ acp_kind: "turn_checkpoint" });
    expect(truncated).toBe(true);
  });

  it("prefers a newer live checkpoint over the durable one", async () => {
    const history = await scanItemHistory(
      "nd-1",
      undefined,
      pagedFetcher(5000, [1]) as unknown as typeof fetchEvents,
    );
    const live = [row(5001, "turn_checkpoint")];
    const { events } = transcriptWindow(history, live);
    expect(events).toHaveLength(5000);
    expect(events[0].ulid).toBe(row(5001).ulid);
  });
});

describe("transcript ordering and checkpoint choice (#2010 independent review)", () => {
  const at = "2026-10-12T00:00:00+00:00";
  const row = (seq: number, acpKind = "agent_message_chunk") => ({
    seq,
    ulid: `01J${String(seq).padStart(23, "0")}`,
    channel: "agent.node",
    payload: { item_id: "nd-1", kind: "acp_event", acp_kind: acpKind },
    originator: "node",
    created_at: at,
  });
  const fetcherOf = (rows: ReturnType<typeof row>[]) =>
    vi.fn(async (opts: { afterSeq?: number }) => {
      const start = opts.afterSeq ?? 0;
      const events = rows.filter((r) => r.seq > start).slice(0, 500);
      return { events, seqCursor: events.at(-1)?.seq ?? null };
    }) as unknown as typeof fetchEvents;

  it("keeps the newest durable checkpoint over a stale live one", async () => {
    const rows = Array.from({ length: 6500 }, (_, i) =>
      row(i + 1, i + 1 === 1000 ? "turn_checkpoint" : "agent_message_chunk"),
    );
    const history = await scanItemHistory("nd-1", undefined, fetcherOf(rows));
    // A missed SSE left the older checkpoint (seq 1) in the live buffer.
    const live = [{ ...row(1, "turn_checkpoint") }];
    const { events } = transcriptWindow(history, live);
    expect(events.at(-1)?.ulid).toBe(row(1000).ulid);
    expect(events.filter((e) => e.ulid === row(1).ulid)).toHaveLength(0);
  });

  it("breaks equal timestamps by sequence when cutting the window", async () => {
    const rows = Array.from({ length: 5000 }, (_, i) => row(i + 1));
    const history = await scanItemHistory("nd-1", undefined, fetcherOf(rows));
    const { events } = transcriptWindow(history, [row(5001)]);
    expect(events).toHaveLength(5000);
    expect(events[0].ulid).toBe(row(5001).ulid);
    expect(events.at(-1)?.ulid).toBe(row(2).ulid);
  });
});
