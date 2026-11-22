import { act, renderHook, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WHOAMI_QUERY_KEY } from "../../../src/lib/api/auth";
import { useIdentity } from "../../../src/app/lib/identity";
import { executeHubWrite, isParkedApproval, isParkedResponse, useHubWrite } from "../../../src/app/lib/mutations";
import { listParkedWrites } from "../../../src/app/lib/parkedWrites";
import { ToastProvider } from "../../../src/lib/ui";
import { epochSetup } from "../support/epoch";
import { mockFetch } from "../support/fetchMock";
import { whoamiFixture } from "../support/fixtures";
import { testQueryClient } from "../support/render";

const WHOAMI = { body: whoamiFixture() };

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

describe("isParkedApproval mirrors routes._is_parked_approval", () => {
  it.each([
    [{ code: "approval_required" }, false],
    [{ approval_id: 5 }, true],
    [{ approval_id: null }, true],
    [{ global_approval_id: 7 }, true],
    [{ pending: true }, true],
    [{ global_approval_status: "pending" }, true],
    [{ autonomy_review: { reason: "x" } }, true],
    [{ status: "pending" }, false],
    [{ status: "approval_pending" }, false],
    [{ status: "running" }, false],
    [{ pending: "yes" }, false],
    [{ autonomy_review: "x" }, false],
    [null, false],
  ])("%j -> %s", (body, parked) => {
    expect(isParkedApproval(body)).toBe(parked);
  });
});

describe("parked detection mirrors routes._json (finding 5)", () => {
  it("a 200 that carries approval_id (work-product release) is done, not parked", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/work-products/4/release": { status: 200, body: { id: 4, approval_id: 9, status: "released" } } });
    expect(await executeHubWrite({ path: "/api/work-products/4/release", body: {} })).toMatchObject({ kind: "done" });
  });

  it("the explicit approval request (202, markers, no code) is accepted, not parked", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/approvals/request": { status: 202, body: { approval_id: 5, status: "pending" } } });
    expect(await executeHubWrite({ path: "/api/approvals/request", body: {} })).toMatchObject({ kind: "accepted" });
  });

  it("isParkedResponse needs 202, the hub markers and code approval_required", () => {
    expect(isParkedResponse(202, { approval_id: 1, status: "pending", code: "approval_required" })).toBe(true);
    expect(isParkedResponse(202, { autonomy_review: {}, code: "approval_required" })).toBe(true);
    expect(isParkedResponse(200, { approval_id: 1, code: "approval_required" })).toBe(false);
    expect(isParkedResponse(202, { approval_id: 1 })).toBe(false);
    expect(isParkedResponse(202, { code: "approval_required" })).toBe(false);
  });
});

describe("replay context check (finding 1)", () => {
  const CTX = { hubOrigin: window.location.origin, organization: "org-1", identity: "operator" };

  it("refuses to send when the fresh whoami is another identity", async () => {
    const hub = mockFetch({ "GET /api/whoami": { body: whoamiFixture({ agent: "someone_else", org: "org-1" } as never) } });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {}, expectedContext: CTX });
    expect(outcome).toMatchObject({ kind: "error", contextMismatch: true });
    expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("re-checks the identity before the CSRF retry", async () => {
    let who = 0;
    const hub = mockFetch({
      "GET /api/whoami": () => ({ body: whoamiFixture({ agent: ++who === 1 ? "operator" : "intruder", org: "org-1", csrf: `c${who}` } as never) }),
      "POST /api/tasks": { status: 403, body: { error: "csrf mismatch" } },
    });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {}, expectedContext: CTX });
    expect(outcome).toMatchObject({ kind: "error", contextMismatch: true });
    expect(hub.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("an aborted signal prevents the send (#2490)", async () => {
    const hub = mockFetch({ "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as never) }, "POST /api/tasks": { body: {} } });
    const controller = new AbortController();
    controller.abort();
    expect(await executeHubWrite({ path: "/api/tasks", body: {}, expectedContext: CTX, signal: controller.signal })).toMatchObject({
      kind: "error",
      aborted: true,
    });
    expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("an unavailable or unreadable whoami is unverified, not a mismatch (#2504)", async () => {
    const hub = mockFetch({ "GET /api/whoami": { status: 503 }, "POST /api/tasks": { body: {} } });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {}, expectedContext: CTX });
    expect(outcome).toMatchObject({ kind: "error", contextUnverified: true });
    expect(outcome).not.toMatchObject({ contextMismatch: true });
    expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it.each([
    ["names nobody", { agent: null, org: "org-1" }],
    ["omits the organization", { org: undefined }],
  ])("a readable whoami that %s is unverified, not a mismatch (#2504)", async (_case, fields) => {
    const hub = mockFetch({ "GET /api/whoami": { body: { ...whoamiFixture(), ...fields } }, "POST /api/tasks": { body: {} } });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {}, expectedContext: CTX });
    expect(outcome).toMatchObject({ kind: "error", contextUnverified: true });
    expect(outcome).not.toMatchObject({ contextMismatch: true });
    expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("the same for the CSRF retry's read: refused as unverified, not as a mismatch (#2504)", async () => {
    let who = 0;
    const hub = mockFetch({
      "GET /api/whoami": () => ({ body: whoamiFixture({ agent: ++who === 1 ? "operator" : null, org: "org-1", csrf: `c${who}` } as never) }),
      "POST /api/tasks": { status: 403, body: { error: "csrf mismatch" } },
    });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {}, expectedContext: CTX });
    expect(outcome).toMatchObject({ kind: "error", contextUnverified: true });
    expect(outcome).not.toMatchObject({ contextMismatch: true });
    expect(hub.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("stillValid() false after the whoami read prevents the send (#2504)", async () => {
    const hub = mockFetch({ "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as never) }, "POST /api/tasks": { body: {} } });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {}, expectedContext: CTX, stillValid: () => false });
    expect(outcome).toMatchObject({ kind: "error", aborted: true });
    expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("sends when the fresh context matches", async () => {
    mockFetch({ "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as never) }, "POST /api/tasks": { body: { id: 1 } } });
    expect(await executeHubWrite({ path: "/api/tasks", body: {}, expectedContext: CTX })).toMatchObject({ kind: "done" });
  });
});

describe("round 2: sent context and dispatched-but-unknown outcomes (C, D)", () => {
  it("pending_approval carries the context of the whoami read made for the send", async () => {
    mockFetch({
      "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as never) },
      "POST /api/tasks": { status: 202, body: { approval_id: 4, code: "approval_required" } },
    });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {} });
    expect(outcome).toMatchObject({
      kind: "pending_approval",
      sentContext: { hubOrigin: window.location.origin, organization: "org-1", identity: "operator" },
    });
  });

  it("after a CSRF retry by the same session, the sent context is the retry's read", async () => {
    let who = 0;
    let posts = 0;
    mockFetch({
      "GET /api/whoami": () => {
        who += 1;
        return { body: whoamiFixture({ org: "org-1", csrf: `c${who}` } as never) };
      },
      "POST /api/tasks": () => (++posts === 1 ? { status: 403, body: { error: "csrf mismatch" } } : { status: 202, body: { approval_id: 9, code: "approval_required" } }),
    });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {} });
    expect(outcome).toMatchObject({ kind: "pending_approval", sentContext: { identity: "operator", organization: "org-1" } });
    expect(who).toBe(2);
  });

  it("a CSRF retry whose read names another identity is refused (round 4, item 1)", async () => {
    let who = 0;
    let posts = 0;
    const hub = mockFetch({
      "GET /api/whoami": () => {
        who += 1;
        return { body: whoamiFixture({ agent: who === 1 ? "first" : "second", org: "org-1", csrf: `c${who}` } as never) };
      },
      "POST /api/tasks": () => (++posts === 1 ? { status: 403, body: { error: "csrf mismatch" } } : { body: {} }),
    });
    expect(await executeHubWrite({ path: "/api/tasks", body: {} })).toMatchObject({ kind: "error", contextMismatch: true });
    expect(hub.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  function stubFetch(response: () => Promise<Response>) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
        if (url.includes("/api/whoami")) return new Response(JSON.stringify(whoamiFixture()), { status: 200 });
        return response();
      }),
    );
  }

  it("a 202 whose body cannot be read is outcome unknown, never accepted", async () => {
    stubFetch(async () => {
      const r = new Response("{}", { status: 202 });
      Object.defineProperty(r, "text", { value: () => Promise.reject(new DOMException("The operation was aborted.", "AbortError")) });
      return r;
    });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {} });
    expect(outcome).toMatchObject({ kind: "error", outcomeUnknown: true });
    expect(outcome).not.toMatchObject({ aborted: true });
  });

  it("an abort after the request started is outcome unknown, not 'cancelled before it was sent'", async () => {
    const controller = new AbortController();
    stubFetch(async () => {
      controller.abort();
      throw new DOMException("The operation was aborted.", "AbortError");
    });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {}, signal: controller.signal });
    expect(outcome).toMatchObject({ kind: "error", outcomeUnknown: true });
    expect(outcome).not.toMatchObject({ aborted: true });
  });

  it("a successful response with truncated JSON is outcome unknown, not accepted (round 3, item 2)", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/tasks": { status: 202, text: '{"code":"approval_required","approval_id":31' } });
    expect(await executeHubWrite({ path: "/api/tasks", body: {} })).toMatchObject({ kind: "error", outcomeUnknown: true });
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/tasks": { status: 200, text: "not json" } });
    expect(await executeHubWrite({ path: "/api/tasks", body: {} })).toMatchObject({ kind: "error", outcomeUnknown: true });
  });

  it("an empty success body is still done, and an error body keeps its text (round 3, item 2)", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/tasks": { status: 200, text: "" } });
    expect(await executeHubWrite({ path: "/api/tasks", body: {} })).toMatchObject({ kind: "done" });
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/tasks": { status: 502, text: "<html>Bad gateway</html>" } });
    expect(await executeHubWrite({ path: "/api/tasks", body: {} })).toMatchObject({ kind: "error", status: 502, detail: "<html>Bad gateway</html>" });
  });

  it("a network failure during the send is outcome unknown", async () => {
    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {} });
    expect(outcome).toMatchObject({ kind: "error", status: null, outcomeUnknown: true });
    if (outcome.kind === "error") expect(outcome.message).toMatch(/not known|could not confirm/i);
  });
});

describe("the hub's machine error code is passed on (Runs slice)", () => {
  it("a 409 with code in_flight surfaces it, message unchanged", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status: 409, body: { error: "a run is active", code: "in_flight" } } });
    const outcome = await executeHubWrite({ path: "/api/x", body: {} });
    expect(outcome).toMatchObject({ kind: "error", status: 409, code: "in_flight" });
    if (outcome.kind === "error") expect(outcome.message).toMatch(/changed since you opened it/);
  });

  it("a 409 without a code leaves it undefined", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status: 409, body: { error: "stale" } } });
    const outcome = await executeHubWrite({ path: "/api/x", body: {} });
    expect(outcome.kind === "error" && outcome.code).toBeUndefined();
  });

  it("a non-string or empty code is ignored", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status: 409, body: { error: "x", code: 409 } } });
    const numeric = await executeHubWrite({ path: "/api/x", body: {} });
    expect(numeric.kind === "error" && numeric.code).toBeUndefined();
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status: 409, body: { error: "x", code: "" } } });
    const empty = await executeHubWrite({ path: "/api/x", body: {} });
    expect(empty.kind === "error" && empty.code).toBeUndefined();
  });

  it.each([401, 403, 404, 400, 422, 500])("HTTP %s carries the code too", async (status) => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status, body: { error: "x", code: "conflict" } } });
    expect(await executeHubWrite({ path: "/api/x", body: {} })).toMatchObject({ kind: "error", status, code: "conflict" });
  });
});

describe("executeHubWrite", () => {
  it("done: sends JSON with the CSRF header and returns the body", async () => {
    const hub = mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/tasks": { body: { id: 9 } } });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: { title: "x" } });
    expect(outcome).toEqual({ kind: "done", data: { id: 9 } });
    const post = hub.calls.find((c) => c.method === "POST");
    expect(post?.headers["x-ua-csrf"]).toBe("csrf-1");
    expect(post?.headers["content-type"]).toBe("application/json");
    expect(post?.body).toEqual({ title: "x" });
  });

  it("accepts an empty success body and other methods", async () => {
    const hub = mockFetch({ "GET /api/whoami": WHOAMI, "DELETE /api/session": { status: 204 }, "PUT /api/graph-canvas": { text: "" } });
    expect(await executeHubWrite({ path: "/api/session", method: "DELETE" })).toEqual({ kind: "done", data: {} });
    expect(await executeHubWrite({ path: "/api/graph-canvas", method: "PUT", body: { a: 1 } })).toEqual({ kind: "done", data: {} });
    const del = hub.calls.find((c) => c.method === "DELETE");
    expect(del?.headers["content-type"]).toBeUndefined();
  });

  it("202 without an approval marker is accepted work, not an approval", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/action/dispatch": { status: 202, body: { status: "pending", run_id: "r1" } } });
    expect(await executeHubWrite({ path: "/api/action/dispatch", body: {} })).toEqual({
      kind: "accepted",
      data: { status: "pending", run_id: "r1" },
    });
  });

  it("202 with code approval_required is pending_approval with the id and tool", async () => {
    mockFetch({
      "GET /api/whoami": WHOAMI,
      "POST /api/tasks": { status: 202, body: { status: "approval_pending", approval_id: 31, code: "approval_required", tool: "task_add" } },
    });
    expect(await executeHubWrite({ path: "/api/tasks", body: {} })).toMatchObject({ kind: "pending_approval", approvalId: "31", tool: "task_add" });
  });

  it("refreshes CSRF once on a CSRF 403 and keeps the idempotency key", async () => {
    let posts = 0;
    let who = 0;
    const hub = mockFetch({
      "GET /api/whoami": () => ({ body: whoamiFixture({ csrf: `csrf-${++who}` }) }),
      "POST /api/tasks": () => (++posts === 1 ? { status: 403, body: { error: "csrf token mismatch" } } : { body: { ok: true } }),
    });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: { a: 1 }, idempotencyKey: "key-1" });
    expect(outcome.kind).toBe("done");
    const postsMade = hub.calls.filter((c) => c.method === "POST");
    expect(postsMade.map((c) => c.headers["x-ua-csrf"])).toEqual(["csrf-1", "csrf-2"]);
    expect(postsMade.map((c) => c.headers["idempotency-key"])).toEqual(["key-1", "key-1"]);
    expect(postsMade[1].body).toEqual({ a: 1 });
  });

  it("401 from whoami or the write says to sign in", async () => {
    mockFetch({ "GET /api/whoami": { status: 401 } });
    expect(await executeHubWrite({ path: "/api/tasks", body: {} })).toMatchObject({ kind: "error", status: 401 });
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/tasks": { status: 401, body: { error: "expired" } } });
    expect(await executeHubWrite({ path: "/api/tasks", body: {} })).toMatchObject({ kind: "error", status: 401, message: expect.stringMatching(/sign in/i) });
  });

  it("403 names the missing capability; a non-CSRF 403 is not retried", async () => {
    const hub = mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/tasks": { status: 403, body: { error: "denied", capability: "task_add" } } });
    const outcome = await executeHubWrite({ path: "/api/tasks", body: {} });
    expect(outcome).toMatchObject({ kind: "error", status: 403, capability: "task_add", detail: "denied" });
    if (outcome.kind === "error") expect(outcome.message).toContain("task_add");
    expect(hub.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("409 offers reload; 5xx and network failures keep the raw text in detail", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/a": { status: 409, body: { error: "stale" } }, "POST /api/b": { status: 503, body: { error: "db locked" } } });
    expect(await executeHubWrite({ path: "/api/a", body: {} })).toMatchObject({ status: 409, message: expect.stringMatching(/reload/i) });
    const server = await executeHubWrite({ path: "/api/b", body: {} });
    expect(server).toMatchObject({ status: 503, detail: "db locked" });
    if (server.kind === "error") expect(server.message).not.toContain("db locked");
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    expect(await executeHubWrite({ path: "/api/a", body: {} })).toMatchObject({ kind: "error", status: null, detail: expect.stringContaining("Failed") });
  });
});

describe("useHubWrite", () => {
  /**
   * The page's whoami (the root client); keep it the same session the mocked
   * hub answers with. `queryClient` is the epoch's client at setup (app data);
   * `root` holds whoami. `signOutBegins` revokes the epoch as the shell does.
   */
  let current: ReturnType<typeof epochSetup> | null = null;
  function setup(who: Record<string, unknown> | null = {}) {
    const s = epochSetup(who === null ? null : { ...whoamiFixture(), ...who });
    current = s;
    return { s, root: s.root, queryClient: s.epoch.queryClient, wrapper: s.wrapper, signOutBegins: () => s.host.beginSignOut(s.host.current) };
  }
  /** The stored writes of the current owner. */
  const storedWrites = () => (current ? listParkedWrites(current.epoch) : []);

  it("done invalidates the listed queries", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/tasks": { body: { id: 1 } } });
    const { queryClient, wrapper } = setup();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useHubWrite({ path: "/api/tasks", invalidates: [["tasks"]] }), { wrapper });
    await act(() => result.current.run({ title: "a" }));
    expect(result.current.state).toEqual({ status: "done", data: { id: 1 } });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["tasks"] });
  });

  it("pending approval stores the identical write with its key and context, never optimistic", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/tasks": { status: 202, body: { approval_id: 12, code: "approval_required" } } });
    const { queryClient, wrapper } = setup();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(
      () => useHubWrite({ path: "/api/tasks", label: "Create task", invalidates: [["tasks"]], idempotencyKey: () => "idem-7" }),
      { wrapper },
    );
    await act(() => result.current.run({ title: "a" }));
    expect(result.current.state).toMatchObject({ status: "pending_approval", approvalId: "12", resumable: true });
    expect(spy).not.toHaveBeenCalledWith({ queryKey: ["tasks"] });
    expect(storedWrites()).toEqual([
      expect.objectContaining({
        path: "/api/tasks",
        method: "POST",
        body: { title: "a" },
        approvalId: "12",
        label: "Create task",
        idempotencyKey: "idem-7",
        invalidates: [["tasks"]],
        context: { hubOrigin: window.location.origin, organization: null, identity: "operator" },
        formHref: expect.any(String),
      }),
    ]);
  });

  it("never promises an automatic replay; without an organization it cannot run from the store (E)", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status: 202, body: { approval_id: 3, code: "approval_required" } } });
    const { wrapper } = setup();
    const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
    let outcome: Awaited<ReturnType<typeof result.current.run>> | undefined;
    await act(async () => {
      outcome = await result.current.run({});
    });
    expect(outcome).toMatchObject({ kind: "pending_approval", willReplay: false, canRunFromStore: false, replayBlockedReason: "organization_unknown" });
    expect(result.current.state).toMatchObject({ willReplay: false, canRunFromStore: false, replayBlockedReason: "organization_unknown" });
    expect(await screen.findByText(/After it is approved, submit this form again/)).toBeInTheDocument();
  });

  it("can run from the store when the sent context has an organization; not_resumable when opted out (E)", async () => {
    mockFetch({
      "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as never) },
      "POST /api/x": { status: 202, body: { approval_id: 3, code: "approval_required" } },
    });
    const { wrapper } = setup({ org: "org-1" });
    const resumable = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
    await act(() => resumable.result.current.run({}));
    expect(resumable.result.current.state).toMatchObject({ willReplay: false, canRunFromStore: true, replayBlockedReason: null });
    expect(resumable.result.current.state).toHaveProperty("parkedWriteId");
    const optedOut = renderHook(() => useHubWrite({ path: "/api/x", resumable: false }), { wrapper });
    await act(() => optedOut.result.current.run({}));
    expect(optedOut.result.current.state).toMatchObject({ willReplay: false, canRunFromStore: false, replayBlockedReason: "not_resumable" });
  });

  it("a parked answer that arrives after another person signed in is not stored (C, round 5 item 2)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    let posted = false;
    mockFetch({
      "GET /api/whoami": { body: whoamiFixture({ agent: "sender", org: "org-1" } as never) },
      "POST /api/x": async () => {
        posted = true;
        await gate;
        return { status: 202, body: { approval_id: 5, code: "approval_required" } };
      },
    });
    const { root, wrapper } = setup({ agent: "sender", org: "org-1" });
    const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
    let outcome: unknown;
    let running: Promise<unknown> = Promise.resolve();
    act(() => {
      running = result.current.run({}).then((o) => (outcome = o));
    });
    await waitFor(() => expect(posted).toBe(true));
    // After the send, another tab signs in as someone else.
    root.setQueryData(WHOAMI_QUERY_KEY, { ...whoamiFixture({ agent: "cached" }), org: "org-2" });
    release();
    await act(async () => {
      await running;
    });
    expect(outcome).toMatchObject({ kind: "cancelled", reason: "owner_changed", dispatched: true });
    expect(storedWrites()).toEqual([]);
  });



  it.each([
    ["approval_id null", { approval_id: null, code: "approval_required" }],
    ["autonomy review with a nested reference", { autonomy_review: { approval_id: 8 }, code: "approval_required" }],
  ])("%s: nothing is stored and nothing is promised (finding 9)", async (_name, body) => {
    mockFetch({ "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as never) }, "POST /api/x": { status: 202, body } });
    const { wrapper } = setup({ org: "org-1" });
    const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
    await act(() => result.current.run({}));
    expect(result.current.state).toMatchObject({ status: "pending_approval", canRunFromStore: false, replayBlockedReason: "no_approval_reference" });
    expect(storedWrites()).toEqual([]);
  });

  it("a parked response that completes after sign-out began stores nothing and says nothing (round 3, item 3)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    mockFetch({
      "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as never) },
      "POST /api/x": async () => {
        await gate;
        return { status: 202, body: { approval_id: 3, code: "approval_required" } };
      },
    });
    const { wrapper, signOutBegins, s: started } = setup({ org: "org-1" });
    const a = started.epoch;
    const { result } = renderHook(() => useHubWrite({ path: "/api/x", label: "Create task" }), { wrapper });
    let running: Promise<unknown> = Promise.resolve();
    act(() => {
      running = result.current.run({ secret: "A's body" });
    });
    await new Promise((r) => setTimeout(r, 20));
    signOutBegins();
    release();
    await act(async () => {
      await running;
    });
    expect(storedWrites()).toEqual([]);
    expect(sessionStorage.getItem(`ua-cc2:o:${a.ownerKey}:parked-writes`)).toBeNull();
    expect(screen.queryByText(/waiting for approval/i)).not.toBeInTheDocument();
  });

  it("the parked 202 body is on the outcome and kept on the stored entry (round 3, Work slice)", async () => {
    const parked = { approval_id: 6, code: "approval_required", work_product_id: "wp-17" };
    mockFetch({ "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as never) }, "POST /api/work-products": { status: 202, body: parked } });
    const { wrapper } = setup({ org: "org-1" });
    const { result } = renderHook(() => useHubWrite({ path: "/api/work-products" }), { wrapper });
    let outcome: Awaited<ReturnType<typeof result.current.run>> | undefined;
    await act(async () => {
      outcome = await result.current.run({ file: "x" });
    });
    expect(outcome).toMatchObject({ kind: "pending_approval", body: parked });
    expect(storedWrites()[0].responseBody).toEqual(parked);
    expect(await executeHubWrite({ path: "/api/work-products", body: {} })).toMatchObject({ body: parked });
  });

  describe("an ordinary write never crosses a sign-in (round 4, item 1)", () => {
    function gate() {
      let release: () => void = () => {};
      const promise = new Promise<void>((r) => (release = r));
      return { promise, release: () => release() };
    }

    it("sign-out while the first whoami read waits: nothing is sent", async () => {
      const g = gate();
      let reads = 0;
      const hub = mockFetch({
        "GET /api/whoami": async () => {
          reads += 1;
          await g.promise;
          return { body: whoamiFixture() };
        },
        "POST /api/x": { body: {} },
      });
      const { wrapper, signOutBegins } = setup();
      const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
      let running: Promise<unknown> = Promise.resolve();
      act(() => {
        running = result.current.run({ secret: "A" });
      });
      await waitFor(() => expect(reads).toBe(1));
      signOutBegins();
      g.release();
      await act(async () => {
        await running;
      });
      expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
    });

    it("sign-out and sign-in as B while the CSRF refresh waits: A's body is not retried", async () => {
      const g = gate();
      let reads = 0;
      let posts = 0;
      const hub = mockFetch({
        "GET /api/whoami": async () => {
          reads += 1;
          if (reads === 2) await g.promise;
          return { body: whoamiFixture({ agent: reads === 1 ? "operator" : "B", csrf: `c${reads}` }) };
        },
        "POST /api/x": () => (++posts === 1 ? { status: 403, body: { error: "csrf mismatch" } } : { body: {} }),
      });
      const { root, wrapper, signOutBegins } = setup();
      const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
      let running: Promise<unknown> = Promise.resolve();
      act(() => {
        running = result.current.run({ secret: "A" });
      });
      await waitFor(() => expect(reads).toBe(2));
      signOutBegins();
      root.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent: "B" }));
      g.release();
      await act(async () => {
        await running;
      });
      expect(hub.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    });

    it("a CSRF retry whose fresh read names someone else is not sent, even without a sign-out here", async () => {
      let reads = 0;
      let posts = 0;
      const hub = mockFetch({
        "GET /api/whoami": () => {
          reads += 1;
          return { body: whoamiFixture({ agent: reads === 1 ? "operator" : "B", csrf: `c${reads}` }) };
        },
        "POST /api/x": () => (++posts === 1 ? { status: 403, body: { error: "csrf mismatch" } } : { body: {} }),
      });
      const { wrapper } = setup();
      const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
      let outcome: unknown;
      await act(async () => {
        outcome = await result.current.run({ secret: "A" });
      });
      expect(hub.calls.filter((c) => c.method === "POST")).toHaveLength(1);
      // Round 6, item 3: a fresh read naming someone else is a cancellation, not an error.
      expect(outcome).toMatchObject({ kind: "cancelled", reason: "owner_changed", dispatched: true });
      expect(result.current.state).toMatchObject({ status: "cancelled", reason: "owner_changed", dispatched: true });
    });
  });

  describe("round 6: every result path re-checks the owner", () => {
    it("item 1: after a 401 (a new auth generation), A's error is returned to nobody: cancelled, even if the refresh names B", async () => {
      let posted = false;
      mockFetch({
        // A until the write has been answered; the refresh after the 401 names B.
        "GET /api/whoami": () => ({ body: whoamiFixture({ agent: posted ? "B" : "operator" }) }),
        "POST /api/x": () => {
          posted = true;
          return { status: 401, body: { error: "session expired", code: "session_expired" } };
        },
      });
      const { wrapper } = setup();
      // The shell keeps the whoami query active; mirror that with an observer.
      const { result } = renderHook(
        () => {
          useIdentity();
          return useHubWrite({ path: "/api/x" });
        },
        { wrapper },
      );
      let outcome: unknown;
      await act(async () => {
        outcome = await result.current.run({});
      });
      expect(outcome).toMatchObject({ kind: "cancelled", reason: "signed_out", dispatched: true });
      expect(outcome).not.toMatchObject({ code: "session_expired" });
    });

    it("item 3: a fresh whoami naming B before the first send is a cancellation (owner_changed, not dispatched)", async () => {
      const hub = mockFetch({ "GET /api/whoami": { body: whoamiFixture({ agent: "B" }) }, "POST /api/x": { body: {} } });
      const { wrapper } = setup();
      const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
      let outcome: unknown;
      await act(async () => {
        outcome = await result.current.run({});
      });
      expect(outcome).toMatchObject({ kind: "cancelled", reason: "owner_changed", dispatched: false });
      expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
    });

    it("item 3: a failed fresh whoami read is a cancellation (unverified, not dispatched)", async () => {
      const hub = mockFetch({ "GET /api/whoami": { status: 503 }, "POST /api/x": { body: {} } });
      const { wrapper } = setup();
      const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
      let outcome: unknown;
      await act(async () => {
        outcome = await result.current.run({});
      });
      expect(outcome).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: false });
      expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
    });

    it.each([
      ["missing", {}],
      ["null", { agent: null }],
      ["empty", { agent: "" }],
    ])("item 6: a verified whoami with a %s identity is not an owner", async (_name, who) => {
      const hub = mockFetch({ "GET /api/whoami": { body: { ...whoamiFixture(), ...who } }, "POST /api/x": { body: {} } });
      const base = whoamiFixture() as Record<string, unknown>;
      delete base.agent;
      const { wrapper } = epochSetup({ ...base, ...who });
      const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
      let outcome: unknown;
      await act(async () => {
        outcome = await result.current.run({});
      });
      expect(outcome).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: false });
      expect(hub.calls).toEqual([]);
    });
  });

  it("a success whose invalidation finishes after sign-out publishes nothing and returns a cancelled result (round 4, item 2)", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { body: { id: 1 } } });
    const { queryClient, wrapper, signOutBegins } = setup();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    vi.spyOn(queryClient, "invalidateQueries").mockImplementation(async () => {
      await gate;
    });
    const { result } = renderHook(() => useHubWrite({ path: "/api/x", invalidates: [["x"]], successMessage: "Saved it." }), { wrapper });
    let outcome: unknown;
    let running: Promise<unknown> = Promise.resolve();
    act(() => {
      running = result.current.run({}).then((o) => (outcome = o));
    });
    await waitFor(() => expect(queryClient.invalidateQueries).toHaveBeenCalled());
    signOutBegins();
    release();
    await act(async () => {
      await running;
    });
    expect(outcome).toMatchObject({ kind: "cancelled", reason: "signed_out", dispatched: true });
    expect(screen.queryByText("Saved it.")).not.toBeInTheDocument();
    // The epoch was revoked: the hook lives in a new tree, with nothing of A's write.
    expect(result.current.state).toEqual({ status: "idle" });
  });

  it("a response that arrives after sign-out returns the cancelled result, not the hub's outcome (round 4, item 2)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    let posted = false;
    mockFetch({
      "GET /api/whoami": WHOAMI,
      "POST /api/x": async () => {
        posted = true;
        await gate;
        return { body: { id: 1 } };
      },
    });
    const { wrapper, signOutBegins } = setup();
    const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
    let outcome: unknown;
    let running: Promise<unknown> = Promise.resolve();
    act(() => {
      running = result.current.run({}).then((o) => (outcome = o));
    });
    await waitFor(() => expect(posted).toBe(true));
    signOutBegins();
    release();
    await act(async () => {
      await running;
    });
    expect(outcome).toMatchObject({ kind: "cancelled", reason: "signed_out", dispatched: true });
  });

  describe("an ordinary write needs a verified owner at submit (round 5, item 1)", () => {
    it("after a failed whoami refresh (A still cached), a submit is refused even if the hub then says B", async () => {
      let fail = true;
      const hub = mockFetch({
        "GET /api/whoami": () => (fail ? { status: 503 } : { body: whoamiFixture({ agent: "B" }) }),
        "POST /api/x": { body: {} },
      });
      const { root, wrapper } = setup();
      await act(async () => {
        await root.refetchQueries({ queryKey: WHOAMI_QUERY_KEY }).catch(() => {});
      });
      expect(root.getQueryState(WHOAMI_QUERY_KEY)?.status).toBe("error");
      fail = false;
      const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
      let outcome: unknown;
      await act(async () => {
        outcome = await result.current.run({ secret: "A" });
      });
      expect(outcome).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: false });
      expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
      expect(result.current.state).toMatchObject({ status: "cancelled", reason: "unverified" });
    });

    it("before the first whoami read has settled, a submit is refused and no later read is adopted", async () => {
      const hub = mockFetch({ "GET /api/whoami": { body: whoamiFixture({ agent: "B" }) }, "POST /api/x": { body: {} } });
      const { wrapper } = epochSetup(null);
      const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
      let outcome: unknown;
      await act(async () => {
        outcome = await result.current.run({ secret: "A" });
      });
      expect(outcome).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: false });
      expect(hub.calls).toEqual([]);
    });

    it("sign-out may opt out (requireVerifiedOwner: false), since it must work when whoami fails", async () => {
      const hub = mockFetch({ "GET /api/whoami": { status: 503 }, "DELETE /api/session": { status: 204 } });
      const { wrapper } = epochSetup(null);
      const { result } = renderHook(() => useHubWrite({ path: "/api/session", method: "DELETE", requireVerifiedOwner: false }), { wrapper });
      await act(async () => {
        await result.current.run(undefined);
      });
      expect(hub.calls.some((c) => c.method === "DELETE")).toBe(true);
    });
  });

  describe("nothing is published under a different or unverified owner (round 5, item 2)", () => {
    async function sendThenChange(change: (qc: QueryClient) => Promise<void> | void) {
      let release: () => void = () => {};
      const gate = new Promise<void>((r) => (release = r));
      let posted = false;
      const hub = mockFetch({
        "GET /api/whoami": () => ({ body: whoamiFixture() }),
        "POST /api/x": async () => {
          posted = true;
          await gate;
          return { body: { id: 1 } };
        },
      });
      const { root, wrapper } = setup();
      const { result } = renderHook(() => useHubWrite({ path: "/api/x", successMessage: "Saved it." }), { wrapper });
      let outcome: unknown;
      let running: Promise<unknown> = Promise.resolve();
      act(() => {
        running = result.current.run({}).then((o) => (outcome = o));
      });
      await waitFor(() => expect(posted).toBe(true));
      await act(async () => {
        await change(root);
      });
      release();
      await act(async () => {
        await running;
      });
      return { outcome, result, hub };
    }

    it("another tab signs in as B before A's response arrives: cancelled owner_changed, nothing shown", async () => {
      const { outcome, result } = await sendThenChange((qc) => {
        qc.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent: "B" }));
      });
      expect(outcome).toMatchObject({ kind: "cancelled", reason: "owner_changed", dispatched: true });
      // B's tree is new: it holds nothing of A's write.
      expect(result.current.state).toEqual({ status: "idle" });
      expect(screen.queryByText("Saved it.")).not.toBeInTheDocument();
    });

    it("the identity can no longer be verified before A's response arrives: cancelled unverified", async () => {
      const { outcome } = await sendThenChange(async (qc) => {
        mockFetch({ "GET /api/whoami": { status: 503 } });
        await qc.refetchQueries({ queryKey: WHOAMI_QUERY_KEY }).catch(() => {});
      });
      expect(outcome).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: true });
      expect(screen.queryByText("Saved it.")).not.toBeInTheDocument();
    });
  });

  it.each([
    ["a string", "Waiting for approval. The work product is created; release it once approved.", "The work product is created"],
    ["a function", (o: { approvalId: string | null }) => `Waiting on approval ${o.approvalId}: release it then.`, "Waiting on approval 3: release it then."],
  ])("round 6 item C: pendingMessage as %s replaces the parked toast", async (_name, pendingMessage, expected) => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status: 202, body: { approval_id: 3, code: "approval_required" } } });
    const { wrapper } = setup();
    const { result } = renderHook(() => useHubWrite({ path: "/api/x", resumable: false, pendingMessage }), { wrapper });
    await act(() => result.current.run({}));
    expect(await screen.findByText(new RegExp(expected.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))).toBeInTheDocument();
    expect(screen.queryByText(/submit this form again/)).not.toBeInTheDocument();
  });

  it("round 6 item C: pendingMessage false suppresses the parked toast", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status: 202, body: { approval_id: 3, code: "approval_required" } } });
    const { wrapper } = setup();
    const { result } = renderHook(() => useHubWrite({ path: "/api/x", resumable: false, pendingMessage: false }), { wrapper });
    await act(() => result.current.run({}));
    expect(result.current.state).toMatchObject({ status: "pending_approval" });
    expect(screen.queryByText(/waiting for approval/i)).not.toBeInTheDocument();
  });

  it("resumable: false parks nothing", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status: 202, body: { approval_id: 3, code: "approval_required" } } });
    const { wrapper } = setup();
    const { result } = renderHook(() => useHubWrite({ path: "/api/x", resumable: false }), { wrapper });
    await act(() => result.current.run({ secret: "s" }));
    expect(result.current.state).toMatchObject({ status: "pending_approval", resumable: false });
    expect(storedWrites()).toEqual([]);
  });

  it("401 starts a new auth generation: the epoch is revoked and whoami is read afresh (the shell shows sign-in)", async () => {
    mockFetch({ "GET /api/whoami": WHOAMI, "POST /api/x": { status: 401 } });
    const { root, wrapper, s } = setup();
    const before = s.epoch;
    const spy = vi.spyOn(root, "resetQueries");
    const { result } = renderHook(() => useHubWrite({ path: "/api/x" }), { wrapper });
    let outcome: unknown;
    await act(async () => {
      outcome = await result.current.run({});
    });
    expect(outcome).toMatchObject({ kind: "cancelled", reason: "signed_out", dispatched: true });
    expect(before.revoked).toBe(true);
    expect(spy).toHaveBeenCalledWith({ queryKey: WHOAMI_QUERY_KEY });
  });
});
