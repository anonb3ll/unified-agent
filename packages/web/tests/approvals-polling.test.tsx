/**
 * Approvals polling discipline (task #1786).
 *
 * A Control Center tab once sent ~3,900 GET /api/approvals in a burst and held
 * the hub near 300% CPU: every hub event on the unfiltered SSE stream refetched
 * the queue, hidden tab or not. These tests pin the fixes: error backoff on
 * the poll, coalesced and visibility-gated SSE invalidation, and conditional
 * GETs that reuse the cached payload on 304.
 */
import { QueryClient, QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  APPROVALS_POLL_MAX_MS,
  APPROVALS_POLL_MS,
  approvalsPollDelay,
  approvalsQueryOptions,
  approvalsRefetchInterval,
  createCoalescedInvalidator,
  useApprovalDetail,
  useApprovals,
} from "../src/lib/approvals/useApprovals";
import {
  APPROVALS_QUERY_KEY,
  approvalDetailQueryKey,
  fetchApprovals,
  resetApprovalsValidator,
  type ApprovalsPayload,
} from "../src/lib/hub";

function payload(): ApprovalsPayload {
  return {
    pending: [{ id: 1, tool: "run_cmd", status: "pending" }],
    approved_unconsumed: [],
    expired_passes: [],
    gated_tools: ["run_cmd"],
    pass_ttl_hours: 24,
  };
}

describe("approvals poll backoff", () => {
  it("polls steadily when healthy and doubles per consecutive failure, capped", () => {
    expect(approvalsPollDelay(0)).toBe(APPROVALS_POLL_MS);
    expect(approvalsPollDelay(1)).toBe(APPROVALS_POLL_MS * 2);
    expect(approvalsPollDelay(2)).toBe(APPROVALS_POLL_MS * 4);
    expect(approvalsPollDelay(50)).toBe(APPROVALS_POLL_MAX_MS);
    expect(approvalsPollDelay(Number.NaN)).toBe(APPROVALS_POLL_MS);
  });

  it("counts failures since the last success, not since the query was created", () => {
    type QueryLike = Parameters<typeof approvalsRefetchInterval>[0];
    const query = { state: {} } as QueryLike;
    const step = (status: string, errorUpdateCount: number) => {
      (query as { state: unknown }).state = { status, errorUpdateCount };
      return approvalsRefetchInterval(query);
    };
    expect(step("success", 0)).toBe(APPROVALS_POLL_MS);
    expect(step("error", 1)).toBe(APPROVALS_POLL_MS * 2);
    expect(step("error", 2)).toBe(APPROVALS_POLL_MS * 4);
    expect(step("success", 2)).toBe(APPROVALS_POLL_MS);
    expect(step("error", 3)).toBe(APPROVALS_POLL_MS * 2);
  });

  it("does not poll a hidden tab", () => {
    const options = approvalsQueryOptions();
    expect(options.queryKey).toEqual(APPROVALS_QUERY_KEY);
    expect(options.refetchIntervalInBackground).toBe(false);
    expect(options.refetchInterval).toBe(approvalsRefetchInterval);
  });
});

describe("createCoalescedInvalidator", () => {
  function harness(hidden = { value: false }) {
    let now = 0;
    const pending: Array<{ at: number; fn: () => void; id: number }> = [];
    let nextId = 1;
    const runs: number[] = [];
    const timers = {
      now: () => now,
      setTimeout: (fn: () => void, ms: number) => {
        const id = nextId++;
        pending.push({ at: now + ms, fn, id });
        return id;
      },
      clearTimeout: (id: unknown) => {
        const index = pending.findIndex((t) => t.id === id);
        if (index >= 0) pending.splice(index, 1);
      },
    };
    const advance = (ms: number) => {
      now += ms;
      for (const t of [...pending].sort((a, b) => a.at - b.at)) {
        if (t.at <= now) {
          pending.splice(pending.indexOf(t), 1);
          t.fn();
        }
      }
    };
    const invalidator = createCoalescedInvalidator(() => runs.push(now), {
      minGapMs: 2_000,
      isHidden: () => hidden.value,
      timers,
    });
    return { invalidator, runs, advance, pending, hidden };
  }

  it("runs the first request at once and folds a burst into one trailing run", () => {
    const { invalidator, runs, advance } = harness();
    invalidator.request();
    expect(runs).toEqual([0]);
    for (let i = 0; i < 100; i += 1) invalidator.request();
    expect(runs).toEqual([0]);
    advance(1_999);
    expect(runs).toEqual([0]);
    advance(1);
    expect(runs).toEqual([0, 2_000]);
    advance(10_000);
    expect(runs).toEqual([0, 2_000]);
  });

  it("holds requests while hidden and replays one when visible again", () => {
    const hidden = { value: true };
    const { invalidator, runs, advance } = harness(hidden);
    for (let i = 0; i < 50; i += 1) invalidator.request();
    advance(60_000);
    expect(runs).toEqual([]);
    hidden.value = false;
    invalidator.onVisible();
    expect(runs).toEqual([60_000]);
    invalidator.onVisible();
    expect(runs).toEqual([60_000]);
  });

  it("defers a trailing run that comes due while hidden", () => {
    const hidden = { value: false };
    const { invalidator, runs, advance } = harness(hidden);
    invalidator.request();
    invalidator.request();
    hidden.value = true;
    advance(5_000);
    expect(runs).toEqual([0]);
    hidden.value = false;
    invalidator.onVisible();
    expect(runs).toEqual([0, 5_000]);
  });

  it("cancel drops a scheduled run", () => {
    const { invalidator, runs, advance, pending } = harness();
    invalidator.request();
    invalidator.request();
    expect(pending).toHaveLength(1);
    invalidator.cancel();
    advance(10_000);
    expect(runs).toEqual([0]);
  });
});

class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  closed = false;
  listeners = new Map<string, Set<(evt: Event) => void>>();
  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (evt: Event) => void) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(listener);
  }
  close() {
    this.closed = true;
  }
  emit(type: string) {
    for (const listener of this.listeners.get(type) ?? []) listener(new MessageEvent(type, { data: JSON.stringify({ channel: "agent.approval.requested" }) }));
  }
}

function Probe() {
  useApprovals();
  return null;
}

describe("useApprovals SSE invalidation", () => {
  let visibility: DocumentVisibilityState = "visible";

  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => payload(),
        text: async () => "",
      })),
    );
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility,
    });
    resetApprovalsValidator();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    // Drop the instance override so the prototype getter applies again.
    delete (document as { visibilityState?: unknown }).visibilityState;
  });

  it("does not refetch on stream events while the tab is hidden", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <Probe />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    const stream = MockEventSource.instances[0];
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    visibility = "hidden";
    for (let i = 0; i < 25; i += 1) stream.emit("message");
    expect(invalidateSpy).not.toHaveBeenCalled();

    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: APPROVALS_QUERY_KEY });

    // A burst right after waits out the gap instead of refetching per event.
    for (let i = 0; i < 25; i += 1) stream.emit("message");
    expect(invalidateSpy).toHaveBeenCalledTimes(1);

    unmount();
    expect(stream.closed).toBe(true);
  });
});

describe("fetchApprovals conditional GET", () => {
  beforeEach(() => {
    resetApprovalsValidator();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetApprovalsValidator();
  });

  it("sends the last ETag and reuses the cached payload on 304", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ etag: 'W/"v1"' }),
        json: async () => payload(),
        text: async () => "",
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 304,
        headers: new Headers({ etag: 'W/"v1"' }),
        json: async () => {
          throw new Error("a 304 has no body");
        },
        text: async () => "",
      });
    vi.stubGlobal("fetch", fetchMock);

    const first = await fetchApprovals();
    const second = await fetchApprovals();

    expect(second).toBe(first);
    expect(second.pending[0].id).toBe(1);
    const firstInit = fetchMock.mock.calls[0][1] as RequestInit;
    const secondInit = fetchMock.mock.calls[1][1] as RequestInit;
    expect(firstInit.cache).toBe("no-store");
    expect((firstInit.headers as Record<string, string>)["If-None-Match"]).toBeUndefined();
    expect((secondInit.headers as Record<string, string>)["If-None-Match"]).toBe('W/"v1"');
  });

  it("treats a 304 with nothing cached as an error rather than an empty queue", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 304,
        headers: new Headers(),
        json: async () => ({}),
        text: async () => "",
      })),
    );
    await expect(fetchApprovals()).rejects.toThrow(/304/);
  });

  it("an older response finishing last does not replace a newer validator", async () => {
    type Deferred = { resolve: (value: unknown) => void };
    const deferred: Deferred[] = [];
    const fetchMock = vi.fn(
      () => new Promise((resolve) => deferred.push({ resolve })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const response = (etag: string, id: number) => ({
      ok: true,
      status: 200,
      headers: new Headers({ etag }),
      json: async () => ({ ...payload(), pending: [{ id, tool: "run_cmd", status: "pending" }] }),
      text: async () => "",
    });

    const older = fetchApprovals();
    const newer = fetchApprovals();
    deferred[1].resolve(response('W/"new"', 2));
    expect((await newer).pending[0].id).toBe(2);
    deferred[0].resolve(response('W/"old"', 1));
    expect((await older).pending[0].id).toBe(1);

    const third = fetchApprovals();
    const thirdInit = (fetchMock.mock.calls[2] as unknown[])[1] as RequestInit;
    expect((thirdInit.headers as Record<string, string>)["If-None-Match"]).toBe('W/"new"');
    deferred[2].resolve({
      ok: false,
      status: 304,
      headers: new Headers({ etag: 'W/"new"' }),
      json: async () => ({}),
      text: async () => "",
    });
    expect((await third).pending[0].id).toBe(2);
  });

  it("carries the expired-pass total and truncation flag through", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          ...payload(),
          expired_passes_total: 120,
          expired_passes_truncated: true,
        }),
        text: async () => "",
      })),
    );
    const result = await fetchApprovals();
    expect(result.expired_passes_total).toBe(120);
    expect(result.expired_passes_truncated).toBe(true);
  });
});

describe("approvals list vs drawer detail keys", () => {
  it("invalidating the list does not refetch an open drawer's detail", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    const detailFn = vi.fn(async () => ({ id: 7 }));
    const listFn = vi.fn(async () => payload());
    await queryClient.fetchQuery({ queryKey: APPROVALS_QUERY_KEY, queryFn: listFn });
    await queryClient.fetchQuery({ queryKey: approvalDetailQueryKey(7), queryFn: detailFn });
    // Observe both queries so an invalidation would refetch either of them.
    const observers = [
      new QueryObserver(queryClient, { queryKey: APPROVALS_QUERY_KEY, queryFn: listFn }),
      new QueryObserver(queryClient, { queryKey: approvalDetailQueryKey(7), queryFn: detailFn }),
    ];
    const unsubs = observers.map((observer) => observer.subscribe(() => {}));
    listFn.mockClear();
    detailFn.mockClear();

    await queryClient.invalidateQueries({ queryKey: APPROVALS_QUERY_KEY });

    expect(listFn).toHaveBeenCalledTimes(1);
    expect(detailFn).not.toHaveBeenCalled();
    expect(queryClient.getQueryState(approvalDetailQueryKey(7))?.isInvalidated).toBe(false);
    unsubs.forEach((unsub) => unsub());
  });
});

describe("open drawer detail stays current", () => {
  let visibility: DocumentVisibilityState = "visible";

  function DrawerProbe() {
    useApprovals();
    useApprovalDetail(7);
    return null;
  }

  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility,
    });
    resetApprovalsValidator();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    delete (document as { visibilityState?: unknown }).visibilityState;
  });

  function stubFetch() {
    const detailCalls = { n: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).startsWith("/api/approvals/")) detailCalls.n += 1;
        return {
          ok: true,
          status: 200,
          headers: new Headers(),
          json: async () => (String(url).startsWith("/api/approvals/") ? { id: 7 } : payload()),
          text: async () => "",
        };
      }),
    );
    return detailCalls;
  }

  it("an approval event refetches the open drawer, not just the list", async () => {
    const detailCalls = stubFetch();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <DrawerProbe />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(detailCalls.n).toBe(1));
    MockEventSource.instances[0].emit("message");
    await waitFor(() => expect(detailCalls.n).toBe(2));
    unmount();
  });

  it("the open drawer polls on its own, so a TTL expiry with no event still shows", () => {
    const queryClient = new QueryClient();
    let observed: unknown;
    function OptionsProbe() {
      useApprovalDetail(7);
      observed = queryClient
        .getQueryCache()
        .find({ queryKey: approvalDetailQueryKey(7) })?.options;
      return null;
    }
    stubFetch();
    render(
      <QueryClientProvider client={queryClient}>
        <OptionsProbe />
      </QueryClientProvider>,
    );
    const options = observed as {
      refetchInterval?: unknown;
      refetchIntervalInBackground?: boolean;
    };
    expect(options.refetchInterval).toBeTruthy();
    expect(options.refetchIntervalInBackground).toBe(false);
  });
});
