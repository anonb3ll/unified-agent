import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WHOAMI_QUERY_KEY, type WhoamiPayload } from "../../../src/lib/api/auth";
import { bumpAuthGeneration } from "../../../src/app/lib/authGeneration";
import { clearDraftIfUnchanged, draftHoldsSnapshot, draftKey, readDraft, saveDraft } from "../../../src/app/lib/drafts";
import { createEpochHost, OwnerEpochProvider, type EpochHost, type OwnerEpoch } from "../../../src/app/lib/epoch";
import { useHubWrite } from "../../../src/app/lib/mutations";
import { ParkedWriteNotice } from "../../../src/app/lib/ParkedWriteNotice";
import {
  addParkedWrite,
  approvalStatusQueryKey,
  clearParkedWrites,
  confirmApplied,
  confirmNotApplied,
  discardParkedWrite,
  contextFromWhoami,
  getParkedWriteResult,
  listParkedWrites,
  organizationFromWhoami,
  approverRelation,
  needsOtherApprover,
  parkedWriteReplay,
  parkedWritesInMemory,
  recordParkedWriteResult,
  replaySuspendedReason,
  runParkedWrite,
  sameContext,
  updateParkedWrite,
  useParkedWrites,
  type ParkedWrite,
  type WriteContext,
} from "../../../src/app/lib/parkedWrites";
import { ToastProvider } from "../../../src/lib/ui";
import { mockFetch, type MockHandler } from "../support/fetchMock";
import { whoamiFixture } from "../support/fixtures";
import { testQueryClient } from "../support/render";

const ORIGIN = window.location.origin;
const CTX = { hubOrigin: ORIGIN, organization: "org-1", identity: "operator" };
const ME = { ...whoamiFixture(), org: "org-1" } as WhoamiPayload;

/** An epoch for the owner `whoami` names (null: no owner), over its own root client. */
function epochFor(whoami: WhoamiPayload | null = ME): OwnerEpoch {
  return hostOf(client(whoami)).current;
}

/** The epoch of the owner a stored context names. */
function epochForContext(context: WriteContext): OwnerEpoch {
  return epochFor({ ...whoamiFixture(), agent: context.identity, org: context.organization ?? undefined } as WhoamiPayload);
}

/** The signed-in owner's epoch (A, the default). */
const me = () => epochFor(ME);

/** A's approval read in the client of `qc`'s current epoch, under A's key. */
function approvalData(qc: QueryClient, approvalId = "31") {
  const epoch = hostOf(qc).current;
  return epoch.queryClient.getQueryData(approvalStatusQueryKey(approvalId, me().ownerKey));
}

function refetchApproval(qc: QueryClient, approvalId = "31") {
  const epoch = hostOf(qc).current;
  return epoch.queryClient.refetchQueries({ queryKey: approvalStatusQueryKey(approvalId, epoch.ownerKey) });
}

function park(overrides: Partial<Omit<ParkedWrite, "id" | "createdAt">> = {}) {
  const entry = addParkedWrite(epochForContext(overrides.context ?? CTX), {
    path: "/api/tasks",
    method: "POST",
    body: { title: "Ship it", priority: 2 },
    approvalId: "31",
    label: "Create task",
    context: CTX,
    invalidates: [["tasks"]],
    idempotencyKey: "idem-1",
    formHref: "/work/tasks/new",
    ...overrides,
  });
  if (!entry) throw new Error("park: storage refused the entry");
  return entry;
}

const posts = (calls: { method: string }[]) => calls.filter((c) => c.method === "POST");

function hub(handlers: Record<string, MockHandler>) {
  return mockFetch({ "GET /api/whoami": { body: ME }, ...handlers });
}

const hosts = new WeakMap<QueryClient, EpochHost>();

/** A root (identity) client with `whoami` seeded, and an epoch host over it. */
function client(whoami: WhoamiPayload | null = ME): QueryClient {
  const qc = testQueryClient();
  // Keep the seeded identity: these tests set who is signed in through the cache.
  qc.setDefaultOptions({ queries: { retry: false, gcTime: Infinity, staleTime: Infinity } });
  if (whoami) qc.setQueryData(WHOAMI_QUERY_KEY, whoami);
  hosts.set(qc, createEpochHost(qc));
  return qc;
}

function hostOf(qc: QueryClient): EpochHost {
  const host = hosts.get(qc);
  if (!host) throw new Error("not a test root client");
  return host;
}

/** The current epoch over a root client. */
const epochOf = (qc: QueryClient) => hostOf(qc).current;

/** A root client and its epoch provider, as the shell renders them. */
function TestRoot({ qc, children }: { qc: QueryClient; children: ReactNode }) {
  return (
    <QueryClientProvider client={qc}>
      <OwnerEpochProvider host={hostOf(qc)}>{children}</OwnerEpochProvider>
    </QueryClientProvider>
  );
}

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <OwnerEpochProvider host={hostOf(qc)}>
        <ToastProvider>{children}</ToastProvider>
      </OwnerEpochProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

describe("write context", () => {
  it("reads the organization through one function", () => {
    expect(organizationFromWhoami(whoamiFixture())).toBeNull();
    expect(organizationFromWhoami(ME)).toBe("org-1");
    expect(organizationFromWhoami({ ...whoamiFixture(), org: { id: "acme" } } as WhoamiPayload)).toBe("acme");
    expect(contextFromWhoami(ME)).toEqual(CTX);
  });

  it("requires origin, identity and a verified organization on both sides", () => {
    expect(sameContext(CTX, CTX)).toBe(true);
    expect(sameContext(CTX, { ...CTX, identity: "someone" })).toBe(false);
    const noOrg = { ...CTX, organization: null };
    expect(sameContext(noOrg, noOrg)).toBe(false);
    expect(replaySuspendedReason(noOrg, noOrg)).toBe("organization_unknown");
    expect(replaySuspendedReason(CTX, null)).toBe("signed_out");
  });

  it("parkedWriteReplay never promises a replay and says whether it can run from the store", () => {
    const entry = park();
    expect(parkedWriteReplay(entry, ME)).toEqual({ willReplay: false, canRunFromStore: true, reason: null });
    expect(parkedWriteReplay(entry, { ...ME, agent: "x" } as WhoamiPayload)).toMatchObject({ canRunFromStore: false, reason: "context_mismatch" });
  });
});

describe("nothing runs by itself (A)", () => {
  it("an approved stored write is only read, never sent, while a page shows it", async () => {
    park();
    const { calls } = hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(client()) });
    await waitFor(() => expect(result.current[0]?.approval).toBe("approved"));
    expect(result.current[0]).toMatchObject({ canRun: true, blockedReason: null, running: false });
    await new Promise((r) => setTimeout(r, 50));
    expect(posts(calls)).toEqual([]);
    expect(calls.some((c) => c.path === "/api/approvals")).toBe(false);
  });

  it.each([
    ["pending", "waiting"],
    ["denied", "denied"],
    ["revoked", "revoked"],
    ["expired", "expired"],
  ])("reads %s through GET /api/approvals/{id} as %s", async (status, state) => {
    park();
    hub({ "GET /api/approvals/31": { body: { status } } });
    const { result } = renderHook(() => useParkedWrites({ approvalId: "31" }), { wrapper: wrapper(client()) });
    await waitFor(() => expect(result.current[0]?.approval).toBe(state));
    expect(result.current[0].canRun).toBe(false);
  });

  it("an entry without a stored organization is blocked with organization_unknown", async () => {
    park({ context: { ...CTX, organization: null } });
    hub({ "GET /api/approvals/31": { body: { status: "approved" } } });
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(client(whoamiFixture())) });
    await waitFor(() => expect(result.current[0]?.approval).toBe("approved"));
    expect(result.current[0]).toMatchObject({ canRun: false, blockedReason: "organization_unknown" });
  });
});

describe("entries and results are scoped to the signed-in owner (round 3, item 4)", () => {
  it("another identity, organization or no verified session sees nothing of A's writes", async () => {
    park();
    hub({ "GET /api/approvals/31": { body: { status: "approved" } } });
    const asB = renderHook(() => useParkedWrites(), { wrapper: wrapper(client({ ...ME, agent: "B" } as WhoamiPayload)) });
    await new Promise((r) => setTimeout(r, 20));
    expect(asB.result.current).toEqual([]);
    const otherOrg = renderHook(() => useParkedWrites(), { wrapper: wrapper(client({ ...ME, org: "org-2" } as WhoamiPayload)) });
    expect(otherOrg.result.current).toEqual([]);
    const unknown = renderHook(() => useParkedWrites(), { wrapper: wrapper(client(null)) });
    expect(unknown.result.current).toEqual([]);
    const asA = renderHook(() => useParkedWrites(), { wrapper: wrapper(client()) });
    expect(asA.result.current).toHaveLength(1);
  });

  it("result records belong to the owner they were made under", async () => {
    park();
    hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) });
    expect(getParkedWriteResult(me(), "31")).toMatchObject({ kind: "done" });
    expect(getParkedWriteResult(epochFor({ ...ME, agent: "B" } as WhoamiPayload), "31")).toBeNull();
    expect(getParkedWriteResult(epochFor(null), "31")).toBeNull();
  });
});

describe("a failed approval read is never presented as a state (round 3, item 5)", () => {
  it("approved, then a failed refresh: unverified and not runnable", async () => {
    park();
    let reads = 0;
    hub({ "GET /api/approvals/31": () => (++reads === 1 ? { body: { status: "approved" } } : { status: 503 }) });
    const qc = client();
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current[0]?.approval).toBe("approved"));
    await refetchApproval(qc);
    await waitFor(() => expect(result.current[0]?.approval).toBe("unverified"));
    expect(result.current[0]).toMatchObject({ canRun: false, blockedReason: "approval_unverified" });
  });

  it("an initial failure is unverified, not unknown", async () => {
    park();
    hub({ "GET /api/approvals/31": { status: 503 } });
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(client()) });
    await waitFor(() => expect(result.current[0]?.approval).toBe("unverified"), { timeout: 8000 });
  });

  it("a failed approval read during a run blocks the entry until a successful check (round 4, item 3)", async () => {
    park();
    let reads = 0;
    let fail = false;
    hub({ "GET /api/approvals/31": () => (++reads > 1 && fail ? { status: 503 } : { body: { status: "approved" } }) });
    const qc = client();
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current[0]?.approval).toBe("approved"));
    fail = true;
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) })).toMatchObject({ kind: "not_run", reason: "not_approved" });
    await waitFor(() => expect(result.current[0]).toMatchObject({ approval: "unverified", canRun: false, blockedReason: "approval_unverified" }));
    fail = false;
    await act(() => refetchApproval(qc));
    await waitFor(() => expect(result.current[0]).toMatchObject({ approval: "approved", canRun: true }));
  });

  it("the run's own fresh approval read updates what the page shows", async () => {
    park();
    let reads = 0;
    hub({ "GET /api/approvals/31": () => ({ body: { status: ++reads === 1 ? "approved" : "revoked" } }) });
    const qc = client();
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current[0]?.approval).toBe("approved"));
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) })).toMatchObject({ kind: "not_run", reason: "not_approved" });
    await waitFor(() => expect(result.current[0]?.approval).toBe("revoked"));
  });
});

describe("separation of duties: a requester cannot approve their own write (round 3, item 10)", () => {
  it("needsOtherApprover is true when requested_by is the signed-in identity", async () => {
    park();
    hub({ "GET /api/approvals/31": { body: { status: "pending", requested_by: "operator" } } });
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(client()) });
    await waitFor(() => expect(result.current[0]?.approval).toBe("waiting"));
    expect(result.current[0].needsOtherApprover).toBe(true);
  });

  it("needsOtherApprover is false for someone else's request or an unknown requester", async () => {
    park();
    hub({ "GET /api/approvals/31": { body: { status: "pending", requested_by: "Desktop_Agent2" } } });
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(client()) });
    await waitFor(() => expect(result.current[0]?.approval).toBe("waiting"));
    expect(result.current[0].needsOtherApprover).toBe(false);
  });
});

describe("only a successful identity verification counts (round 4, item 4)", () => {
  it.each([503, 403])("a whoami refresh that fails with %s suspends listing and result access, though data is cached", async (status) => {
    park();
    recordParkedWriteResult(me(), { owner: CTX, approvalId: "77", kind: "done", message: "Earlier.", at: 1 });
    hub({ "GET /api/whoami": { status }, "GET /api/approvals/31": { body: { status: "approved" } } });
    const qc = client();
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current).toHaveLength(1));
    await act(async () => {
      await qc.refetchQueries({ queryKey: WHOAMI_QUERY_KEY }).catch(() => {});
    });
    expect(qc.getQueryState(WHOAMI_QUERY_KEY)?.status).toBe("error");
    expect(qc.getQueryData(WHOAMI_QUERY_KEY)).toBeTruthy();
    await waitFor(() => expect(result.current).toEqual([]));
  });

  it("runParkedWrite does not send while the identity query is in error, even with cached data", async () => {
    park();
    const { calls } = hub({ "GET /api/whoami": { status: 503 }, "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    const qc = client();
    await qc.refetchQueries({ queryKey: WHOAMI_QUERY_KEY }).catch(() => {});
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) })).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: false });
    expect(posts(calls)).toEqual([]);
    expect(calls.some((c) => c.path === "/api/approvals/31")).toBe(false);
  });

  it("round 8 item 4: a run refused for an unverified owner leaves no abort listener behind", async () => {
    park();
    hub({ "GET /api/whoami": { status: 503 } });
    const qc = client();
    await qc.refetchQueries({ queryKey: WHOAMI_QUERY_KEY }).catch(() => {});
    const epoch = epochOf(qc);
    // The listing cache hooks the epoch's revocation once per epoch; runs must add nothing on top.
    listParkedWrites(epoch);
    const added = vi.spyOn(epoch.signal, "addEventListener");
    const removed = vi.spyOn(epoch.signal, "removeEventListener");
    const id = listParkedWrites(me())[0].id;
    for (let i = 0; i < 3; i++) {
      expect(await runParkedWrite(id, { epoch })).toMatchObject({ kind: "cancelled", reason: "unverified" });
    }
    expect(added.mock.calls.length - removed.mock.calls.length).toBe(0);
  });
});

describe("round 8 item 5: stored bodies do not outlive the owner in memory", () => {
  const secret = { title: "body that must not linger" };
  const held = () => JSON.stringify(parkedWritesInMemory());

  it("sign-out (clear, then revocation) drops the cached listing, with no subscriber", () => {
    const qc = client();
    const epoch = epochOf(qc);
    park({ body: secret });
    expect(listParkedWrites(epoch)).toHaveLength(1);
    expect(held()).toContain(secret.title);
    clearParkedWrites(epoch);
    hostOf(qc).beginSignOut(epoch);
    expect(held()).not.toContain(secret.title);
  });

  it("revocation alone (another owner) drops the old owner's cached listing", () => {
    const qc = client();
    const epoch = epochOf(qc);
    addParkedWrite(epoch, { path: "/api/tasks", method: "POST", body: secret, approvalId: "31", label: "Create task", context: CTX, invalidates: [] });
    expect(listParkedWrites(epoch)).toHaveLength(1);
    qc.setQueryData(WHOAMI_QUERY_KEY, { ...ME, agent: "B" });
    expect(epoch.revoked).toBe(true);
    expect(held()).not.toContain(secret.title);
  });
});

describe("approver relation is three-state and never grants permission (round 4, item 5)", () => {
  it.each([
    ["the same label", "operator", "operator", "self"],
    ["no requested_by", null, "operator", "unknown"],
    ["no signed-in identity", "operator", null, "unknown"],
    // The hub resolves aliases (one person, several labels) from its own
    // config and whoami does not expose them: a different label may still be
    // the same person, so it is never reported as "other".
    ["a different label", "Operator", "operator", "unknown"],
  ] as const)("%s -> %s", (_name, requestedBy, identity, expected) => {
    expect(approverRelation(requestedBy, identity)).toBe(expected);
    expect(needsOtherApprover(requestedBy, identity)).toBe(expected === "self");
  });

  it("the entry carries approver; needsOtherApprover is derived and true only for self", async () => {
    park();
    hub({ "GET /api/approvals/31": { body: { status: "pending", requested_by: "Operator" } } });
    const { result } = renderHook(() => useParkedWrites(), { wrapper: wrapper(client()) });
    await waitFor(() => expect(result.current[0]?.approval).toBe("waiting"));
    expect(result.current[0]).toMatchObject({ approver: "unknown", needsOtherApprover: false });
  });
});

describe("runParkedWrite (B)", () => {
  it("sends the identical body and key once, clears the entry and invalidates", async () => {
    park();
    const { calls } = hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: { id: 77 } } });
    const qc = client();
    const spy = vi.spyOn(epochOf(qc).queryClient, "invalidateQueries");
    const id = listParkedWrites(me())[0].id;
    const result = await runParkedWrite(id, { epoch: epochOf(qc) });
    expect(result).toMatchObject({ kind: "done" });
    expect(posts(calls)).toHaveLength(1);
    expect(posts(calls)[0]).toMatchObject({ body: { title: "Ship it", priority: 2 } });
    expect(calls.find((c) => c.method === "POST")?.headers["idempotency-key"]).toBe("idem-1");
    expect(listParkedWrites(me())).toEqual([]);
    expect(getParkedWriteResult(me(), "31")).toMatchObject({ kind: "done" });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["tasks"] });
  });

  it("a second call while one is in flight is a no-op", async () => {
    park();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const { calls } = hub({
      "GET /api/approvals/31": { body: { status: "approved" } },
      "POST /api/tasks": async () => {
        await gate;
        return { body: {} };
      },
    });
    const id = listParkedWrites(me())[0].id;
    const first = runParkedWrite(id, { epoch: epochOf(client()) });
    expect(await runParkedWrite(id, { epoch: epochOf(client()) })).toMatchObject({ kind: "not_run", reason: "busy" });
    release();
    expect(await first).toMatchObject({ kind: "done" });
    expect(posts(calls)).toHaveLength(1);
  });

  it("a fresh whoami for another identity discards the entry without sending", async () => {
    park();
    const { calls } = hub({
      "GET /api/whoami": { body: { ...ME, agent: "intruder" } },
      "GET /api/approvals/31": { body: { status: "approved" } },
      "POST /api/tasks": { body: {} },
    });
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) })).toMatchObject({ kind: "discarded", reason: "context_mismatch" });
    expect(posts(calls)).toEqual([]);
    expect(listParkedWrites(me())).toEqual([]);
  });

  it("an unavailable whoami read keeps the entry and says it could not check (#2504)", async () => {
    park();
    const { calls } = hub({ "GET /api/whoami": { status: 503 }, "GET /api/approvals/31": { body: { status: "approved" } } });
    const result = await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) });
    expect(result).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: false });
    expect(result.message).toBe("We could not check who is signed in, so this was not run.");
    expect(posts(calls)).toEqual([]);
    expect(listParkedWrites(me())).toEqual([expect.objectContaining({ lastError: "We could not check who is signed in, so this was not run." })]);
  });

  it("an unreadable whoami body is the same: kept, not discarded (#2504)", async () => {
    park();
    hub({ "GET /api/whoami": { status: 200, text: "<html>proxy</html>" }, "GET /api/approvals/31": { body: { status: "approved" } } });
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) })).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: false });
    expect(listParkedWrites(me())).toHaveLength(1);
  });

  it("a whoami read that throws is the same: kept, no false attribution (#2504)", async () => {
    park();
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
        const url = String(input);
        calls.push(`${(init.method ?? "GET").toUpperCase()} ${url}`);
        if (url.includes("/api/approvals/31")) return new Response(JSON.stringify({ status: "approved" }), { status: 200 });
        throw new TypeError("network connection was lost");
      }),
    );
    const result = await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) });
    expect(result).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: false });
    expect(calls.filter((c) => c.startsWith("POST"))).toEqual([]);
    expect(listParkedWrites(me())).toHaveLength(1);
    expect(getParkedWriteResult(me(), "31")).toBeNull();
  });

  // A readable whoami that does not name who is signed in, or omits the
  // organization the entry was made under, proves nothing differs: kept.
  it.each([
    ["no identity", { ...ME, agent: null }],
    ["no organization", { ...ME, org: undefined }],
  ])("a whoami with %s is unverified, not a mismatch: kept, nothing sent (#2504)", async (_case, whoami) => {
    park();
    const { calls } = hub({ "GET /api/whoami": { body: whoami }, "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    const result = await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) });
    expect(result).toMatchObject({ kind: "cancelled", reason: "unverified", dispatched: false });
    expect(result.message).not.toMatch(/another identity/);
    expect(posts(calls)).toEqual([]);
    expect(listParkedWrites(me())).toHaveLength(1);
    expect(getParkedWriteResult(me(), "31")).toBeNull();
  });

  it("a fresh whoami for another organization still discards the entry without sending (#2504)", async () => {
    park();
    const { calls } = hub({ "GET /api/whoami": { body: { ...ME, org: "org-2" } }, "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) })).toMatchObject({ kind: "discarded", reason: "context_mismatch" });
    expect(posts(calls)).toEqual([]);
    expect(listParkedWrites(me())).toEqual([]);
    expect(getParkedWriteResult(me(), "31")).toMatchObject({ kind: "discarded" });
  });

  it("a whoami answer issued before a sign-out in this tab is not trusted (#2504)", async () => {
    park();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    let reads = 0;
    const { calls } = hub({
      "GET /api/whoami": async () => {
        reads += 1;
        await gate;
        return { body: ME };
      },
      "GET /api/approvals/31": { body: { status: "approved" } },
      "POST /api/tasks": { body: {} },
    });
    const run = runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) });
    await waitFor(() => expect(reads).toBe(1));
    // A new sign-in generation starts while the read is in flight (the
    // controller is deliberately not aborted here).
    bumpAuthGeneration();
    release();
    expect(await run).toMatchObject({ kind: "cancelled", reason: "signed_out", dispatched: false });
    expect(posts(calls)).toEqual([]);
  });

  describe("the observed context is checked right before each send (#2499 item 2)", () => {
    it("the observed owner changing during the approval read stops the run before whoami is read", async () => {
      park();
      const qc = client();
      let release: () => void = () => {};
      const gate = new Promise<void>((r) => (release = r));
      let approvalReads = 0;
      const { calls } = hub({
        "GET /api/approvals/31": async () => {
          approvalReads += 1;
          await gate;
          return { body: { status: "approved" } };
        },
        "POST /api/tasks": { body: {} },
      });
      const run = runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) });
      await waitFor(() => expect(approvalReads).toBe(1));
      qc.setQueryData(WHOAMI_QUERY_KEY, { ...ME, agent: "B" });
      release();
      expect(await run).toMatchObject({ kind: "cancelled", reason: "owner_changed", dispatched: false });
      expect(calls.filter((c) => c.path === "/api/whoami")).toEqual([]);
      expect(posts(calls)).toEqual([]);
    });

    it.each([
      ["identity", { agent: "B" }],
      ["organization", { org: "org-2" }],
    ])("a held whoami returning A while the page now observes another %s does not dispatch", async (_case, change) => {
      park();
      const qc = client();
      let release: () => void = () => {};
      const gate = new Promise<void>((r) => (release = r));
      let reads = 0;
      const { calls } = hub({
        "GET /api/whoami": async () => {
          reads += 1;
          await gate;
          return { body: ME };
        },
        "GET /api/approvals/31": { body: { status: "approved" } },
        "POST /api/tasks": { body: {} },
      });
      const run = runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) });
      await waitFor(() => expect(reads).toBe(1));
      qc.setQueryData(WHOAMI_QUERY_KEY, { ...ME, ...change });
      release();
      expect(await run).toMatchObject({ kind: "cancelled", reason: "owner_changed", dispatched: false });
      expect(posts(calls)).toEqual([]);
      expect(listParkedWrites(me())).toHaveLength(1);
    });

    it("the same through the CSRF retry", async () => {
      park();
      const qc = client();
      let reads = 0;
      const { calls } = hub({
        "GET /api/whoami": () => {
          reads += 1;
          // The page observes B by the time the retry's read answers (as A).
          if (reads === 2) qc.setQueryData(WHOAMI_QUERY_KEY, { ...ME, agent: "B" });
          return { body: { ...ME, csrf: `c${reads}` } };
        },
        "GET /api/approvals/31": { body: { status: "approved" } },
        "POST /api/tasks": { status: 403, body: { error: "csrf mismatch" } },
      });
      expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) })).toMatchObject({
        kind: "cancelled",
        reason: "owner_changed",
        dispatched: true,
      });
      expect(posts(calls)).toHaveLength(1);
      // The first send left: for the same person returning, the change may have been made.
      expect(listParkedWrites(me())[0]).toMatchObject({ outcome: "unknown" });
    });

    it("an unknown observed context blocks the send", async () => {
      park();
      const { calls } = hub({ "GET /api/whoami": { status: 503 }, "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
      // A's epoch, but the page can no longer verify who is signed in (a
      // transient failure does not end the epoch; the per-send check refuses).
      const qc = client();
      const a = epochOf(qc);
      await qc.refetchQueries({ queryKey: WHOAMI_QUERY_KEY }).catch(() => {});
      expect(a.revoked).toBe(false);
      expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: a })).toMatchObject({
        kind: "cancelled",
        reason: "unverified",
        dispatched: false,
      });
      expect(posts(calls)).toEqual([]);
    });
  });

  it("a signed-out session discards the entry without sending", async () => {
    park();
    const { calls } = hub({ "GET /api/whoami": { status: 401 }, "GET /api/approvals/31": { body: { status: "approved" } } });
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) })).toMatchObject({ kind: "discarded", reason: "signed_out" });
    expect(posts(calls)).toEqual([]);
  });

  it("does not send without a stored organization, or before approval", async () => {
    park({ context: { ...CTX, organization: null } });
    const { calls } = hub({ "GET /api/approvals/31": { body: { status: "approved" } } });
    const noOrg = epochForContext({ ...CTX, organization: null });
    expect(await runParkedWrite(listParkedWrites(noOrg)[0].id, { epoch: noOrg })).toMatchObject({ kind: "not_run", reason: "organization_unknown" });
    park();
    hub({ "GET /api/approvals/31": { body: { status: "pending" } } });
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) })).toMatchObject({ kind: "not_run", reason: "not_approved" });
    expect(posts(calls)).toEqual([]);
  });

  it("a repeat that parks again keeps the entry under the new approval", async () => {
    park();
    hub({
      "GET /api/approvals/31": { body: { status: "approved" } },
      "POST /api/tasks": { status: 202, body: { approval_id: 40, code: "approval_required" } },
    });
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) })).toMatchObject({ kind: "pending_approval", approvalId: "40" });
    expect(listParkedWrites(me())).toEqual([expect.objectContaining({ approvalId: "40", idempotencyKey: "idem-1" })]);
  });

  it("a lost response marks the outcome unknown; no second run until the person confirms it did not happen", async () => {
    park();
    const { calls } = hub({ "GET /api/approvals/31": { body: { status: "approved" } } });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
        const url = String(input);
        calls.push({ method: (init.method ?? "GET").toUpperCase(), path: url, body: undefined, headers: {} });
        if (url.includes("/api/whoami")) return new Response(JSON.stringify(ME), { status: 200 });
        if (url.includes("/api/approvals/31")) return new Response(JSON.stringify({ status: "approved" }), { status: 200 });
        throw new TypeError("network connection was lost");
      }),
    );
    const id = listParkedWrites(me())[0].id;
    expect(await runParkedWrite(id, { epoch: epochOf(client()) })).toMatchObject({ kind: "outcome_unknown" });
    expect(listParkedWrites(me())[0]).toMatchObject({ outcome: "unknown" });
    expect(await runParkedWrite(id, { epoch: epochOf(client()) })).toMatchObject({ kind: "not_run", reason: "outcome_unknown" });
    expect(posts(calls)).toHaveLength(1);
    confirmNotApplied(me(), id);
    expect(listParkedWrites(me())[0].outcome).toBeUndefined();
  });

  it("a run that completes after sign-out records nothing (round 3, item 3)", async () => {
    park();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    hub({
      "GET /api/approvals/31": { body: { status: "approved" } },
      "POST /api/tasks": async () => {
        await gate;
        return { body: { id: 1 } };
      },
    });
    const qc = client();
    const running = runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) });
    await new Promise((r) => setTimeout(r, 30));
    clearParkedWrites(epochOf(qc));
    hostOf(qc).beginSignOut(hostOf(qc).current);
    release();
    await running;
    expect(getParkedWriteResult(me(), "31")).toBeNull();
    expect(listParkedWrites(me())).toEqual([]);
  });

  describe("Discard cancels a run in flight (#2499)", () => {
    function gated() {
      let release: () => void = () => {};
      const gate = new Promise<void>((r) => (release = r));
      return { gate, release: () => release() };
    }

    it("during the approval read: nothing is sent and the entry is gone", async () => {
      park();
      const g = gated();
      let approvalReads = 0;
      const { calls } = hub({
        "GET /api/approvals/31": async () => {
          approvalReads += 1;
          await g.gate;
          return { body: { status: "approved" } };
        },
        "POST /api/tasks": { body: {} },
      });
      const id = listParkedWrites(me())[0].id;
      const run = runParkedWrite(id, { epoch: epochOf(client()) });
      await waitFor(() => expect(approvalReads).toBe(1));
      discardParkedWrite(me(), id);
      g.release();
      expect(await run).toMatchObject({ kind: "not_run" });
      expect(posts(calls)).toEqual([]);
      expect(listParkedWrites(me())).toEqual([]);
    });

    it("during the whoami read: nothing is sent", async () => {
      park();
      const g = gated();
      let whoamiReads = 0;
      const { calls } = hub({
        "GET /api/whoami": async () => {
          whoamiReads += 1;
          await g.gate;
          return { body: ME };
        },
        "GET /api/approvals/31": { body: { status: "approved" } },
        "POST /api/tasks": { body: {} },
      });
      const id = listParkedWrites(me())[0].id;
      const run = runParkedWrite(id, { epoch: epochOf(client()) });
      await waitFor(() => expect(whoamiReads).toBe(1));
      discardParkedWrite(me(), id);
      g.release();
      await run;
      expect(posts(calls)).toEqual([]);
      expect(listParkedWrites(me())).toEqual([]);
    });

    it("after the request left: the entry is kept as outcome unknown, not silently removed", async () => {
      park();
      const g = gated();
      const { calls } = hub({
        "GET /api/approvals/31": { body: { status: "approved" } },
        "POST /api/tasks": async () => {
          await g.gate;
          return { body: { id: 1 } };
        },
      });
      const id = listParkedWrites(me())[0].id;
      const run = runParkedWrite(id, { epoch: epochOf(client()) });
      await waitFor(() => expect(posts(calls)).toHaveLength(1));
      discardParkedWrite(me(), id);
      expect(listParkedWrites(me())).toEqual([expect.objectContaining({ id, outcome: "unknown" })]);
      g.release();
      // This mock answers despite the abort; a definitive answer is then recorded
      // as the result (a real browser cuts the response off and it stays unknown).
      await run;
      expect(getParkedWriteResult(me(), "31")).toMatchObject({ kind: "done" });
    });
  });

  it("a success whose invalidation finishes after sign-out is reported as signed out, not done (round 4, item 2)", async () => {
    park();
    hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    const qc = client();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    vi.spyOn(epochOf(qc).queryClient, "invalidateQueries").mockImplementation(async () => {
      await gate;
    });
    const run = runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) });
    const a = epochOf(qc);
    await waitFor(() => expect(a.queryClient.invalidateQueries).toHaveBeenCalled());
    clearParkedWrites(a);
    hostOf(qc).beginSignOut(hostOf(qc).current);
    release();
    expect(await run).toMatchObject({ kind: "cancelled", reason: "signed_out", dispatched: true });
    expect(getParkedWriteResult(me(), "31")).toBeNull();
  });

  it("an approval read that finishes after sign-out does not update the shared cache (round 4, item 2)", async () => {
    park();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    let reads = 0;
    hub({
      "GET /api/approvals/31": async () => {
        reads += 1;
        await gate;
        return { body: { status: "approved" } };
      },
    });
    const qc = client();
    const run = runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) });
    await waitFor(() => expect(reads).toBe(1));
    clearParkedWrites(epochOf(qc));
    hostOf(qc).beginSignOut(hostOf(qc).current);
    release();
    expect(await run).toMatchObject({ kind: "cancelled", reason: "signed_out", dispatched: false });
    expect(approvalData(qc)).toBeUndefined();
  });

  it("another tab signs in as B before the run's response arrives: cancelled, nothing published (round 5, item 2)", async () => {
    park();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const { calls } = hub({
      "GET /api/approvals/31": { body: { status: "approved" } },
      "POST /api/tasks": async () => {
        await gate;
        return { body: { id: 1 } };
      },
    });
    const qc = client();
    const run = runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) });
    await waitFor(() => expect(posts(calls)).toHaveLength(1));
    qc.setQueryData(WHOAMI_QUERY_KEY, { ...ME, agent: "B" });
    release();
    expect(await run).toMatchObject({ kind: "cancelled", reason: "owner_changed", dispatched: true });
    expect(getParkedWriteResult(me(), "31")).toBeNull();
    // For A returning: the request left, so the change may have been made.
    expect(listParkedWrites(me())[0]).toMatchObject({ outcome: "unknown" });
  });

  it.each([
    ["answers denied", { body: { status: "denied" } }],
    ["fails", { status: 503 }],
  ] as const)("round 6 item 2: B observed while A's approval read waits, then it %s: cancelled, nothing published", async (_name, reply) => {
    park();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    let reads = 0;
    hub({
      "GET /api/approvals/31": async () => {
        reads += 1;
        await gate;
        return reply;
      },
    });
    const qc = client();
    const run = runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(qc) });
    await waitFor(() => expect(reads).toBe(1));
    qc.setQueryData(WHOAMI_QUERY_KEY, { ...ME, agent: "B" });
    release();
    const result = await run;
    expect(result).toMatchObject({ kind: "cancelled", reason: "owner_changed", dispatched: false });
    expect(result.message).not.toContain("Create task");
    expect(approvalData(qc)).toBeUndefined();
    expect(listParkedWrites(me())[0].verifyFailedAt).toBeUndefined();
  });

  it("round 6 item A: a reload after the request left makes the entry outcome unknown until the person confirms", async () => {
    park();
    let posted = false;
    hub({
      "GET /api/approvals/31": { body: { status: "approved" } },
      // The answer never arrives before the reload.
      "POST /api/tasks": async () => {
        posted = true;
        await new Promise(() => {});
        return { body: {} };
      },
    });
    const id = listParkedWrites(me())[0].id;
    void runParkedWrite(id, { epoch: epochOf(client()) });
    await waitFor(() => expect(posted).toBe(true));
    // The attempt is on the stored entry before the request left.
    expect(JSON.parse(sessionStorage.getItem(`ua-cc2:o:${me().ownerKey}:parked-writes`) ?? "[]")[0].attemptStartedAt).toEqual(expect.any(Number));
    // Simulate a reload: a fresh copy of the module reads the same sessionStorage.
    vi.resetModules();
    const reloaded = await import("../../../src/app/lib/parkedWrites");
    expect(reloaded.listParkedWrites(me())[0]).toMatchObject({ id, outcome: "unknown" });
    hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    expect(await reloaded.runParkedWrite(id, { epoch: epochOf(client()) })).toMatchObject({ kind: "not_run", reason: "outcome_unknown" });
    reloaded.confirmNotApplied(me(), id);
    expect(reloaded.listParkedWrites(me())[0].outcome).toBeUndefined();
    expect(reloaded.listParkedWrites(me())[0].attemptStartedAt).toBeUndefined();
  });

  it("round 6 item A: if the attempt cannot be stored, nothing is sent and the run says why", async () => {
    park();
    const { calls } = hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    const result = await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) });
    setItem.mockRestore();
    expect(result).toMatchObject({ kind: "not_run", reason: "not_recorded" });
    expect(result.message).toMatch(/could not be recorded/);
    expect(posts(calls)).toEqual([]);
  });

  it("an error answer keeps the entry with the error", async () => {
    park();
    hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { status: 503, body: { error: "db locked" } } });
    expect(await runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) })).toMatchObject({ kind: "error", status: 503 });
    expect(listParkedWrites(me())[0].lastError).toMatch(/problem/);
  });
});

/**
 * Task #2525: a parked write carries the form draft it was submitted from.
 * A successful run, from anywhere (the form unmounted), clears that draft,
 * but only while it still holds the submitted snapshot.
 */
describe("a parked write carries the draft it came from (#2525)", () => {
  const KEY = draftKey({ identity: "operator", operation: "task.create" });
  const SNAPSHOT = { title: "Ship it", priority: "2" };
  const approvedHub = (post: MockHandler = { body: { id: 77 } }) => hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": post });
  const runIt = async () => runParkedWrite(listParkedWrites(me())[0].id, { epoch: epochOf(client()) });

  it("a successful run clears the draft while it still holds the snapshot, whatever bookkeeping sits beside it", async () => {
    expect(saveDraft(me(), KEY, { ...SNAPSHOT, parkedApprovalId: "31", attemptKey: "k-1" })).toBe(true);
    park({ draft: { draftKey: KEY, submittedSnapshot: SNAPSHOT } });
    expect(listParkedWrites(me())[0].draft).toEqual({ draftKey: KEY, submittedSnapshot: SNAPSHOT });
    approvedHub();
    expect(await runIt()).toMatchObject({ kind: "done" });
    expect(readDraft(me(), KEY)).toBeNull();
  });

  it("keeps a draft edited after the submit", async () => {
    saveDraft(me(), KEY, { ...SNAPSHOT, title: "Ship it today" });
    park({ draft: { draftKey: KEY, submittedSnapshot: SNAPSHOT } });
    approvedHub();
    expect(await runIt()).toMatchObject({ kind: "done" });
    expect(readDraft(me(), KEY)).toEqual({ ...SNAPSHOT, title: "Ship it today" });
  });

  it("a run that did not make the change leaves the draft alone", async () => {
    saveDraft(me(), KEY, SNAPSHOT);
    park({ draft: { draftKey: KEY, submittedSnapshot: SNAPSHOT } });
    approvedHub({ status: 400, body: { error: "title taken" } });
    expect(await runIt()).toMatchObject({ kind: "error" });
    expect(readDraft(me(), KEY)).toEqual(SNAPSHOT);
  });

  it("'It is there' after an unknown outcome clears it the same way", () => {
    saveDraft(me(), KEY, SNAPSHOT);
    const entry = park({ draft: { draftKey: KEY, submittedSnapshot: SNAPSHOT }, outcome: "unknown" });
    expect(confirmApplied(me(), entry.id)).toMatchObject({ kind: "done" });
    expect(readDraft(me(), KEY)).toBeNull();
  });

  it("only the run's owner's draft is touched; a revoked epoch touches none", async () => {
    const other = epochFor({ ...ME, agent: "B" } as WhoamiPayload);
    // The same draft key under another owner's scope.
    expect(saveDraft(other, KEY, SNAPSHOT)).toBe(true);
    saveDraft(me(), KEY, SNAPSHOT);
    park({ draft: { draftKey: KEY, submittedSnapshot: SNAPSHOT } });
    approvedHub();
    expect(await runIt()).toMatchObject({ kind: "done" });
    expect(readDraft(me(), KEY)).toBeNull();
    expect(readDraft(epochFor({ ...ME, agent: "B" } as WhoamiPayload), KEY)).toEqual(SNAPSHOT);
    // Revoked: nothing is read or removed.
    const qc = client();
    const epoch = epochOf(qc);
    saveDraft(epoch, KEY, SNAPSHOT);
    qc.setQueryData(WHOAMI_QUERY_KEY, { ...ME, agent: "C" });
    expect(epoch.revoked).toBe(true);
    expect(clearDraftIfUnchanged(epoch, KEY, SNAPSHOT)).toBe("none");
    expect(readDraft(me(), KEY)).toEqual(SNAPSHOT);
  });

  it("a write parked through useHubWrite with a draft stores the binding", async () => {
    hub({ "POST /api/tasks": { status: 202, body: { approval_id: 31, code: "approval_required", tool: "task_add" } } });
    const qc = client();
    const { result } = renderHook(() => useHubWrite<{ title: string }>({ path: "/api/tasks", label: "Create task" }), { wrapper: wrapper(qc) });
    let outcome: Awaited<ReturnType<typeof result.current.run>> | undefined;
    await act(async () => {
      outcome = await result.current.run({ title: "Ship it" }, { draft: { draftKey: KEY, submittedSnapshot: SNAPSHOT } });
    });
    expect(outcome).toMatchObject({ kind: "pending_approval" });
    expect(listParkedWrites(me())[0]).toMatchObject({ approvalId: "31", draft: { draftKey: KEY, submittedSnapshot: SNAPSHOT } });
  });

  it("draftHoldsSnapshot compares an object snapshot over its own fields, anything else whole", () => {
    expect(draftHoldsSnapshot({ a: 1, b: [1, 2], extra: "x" }, { b: [1, 2], a: 1 })).toBe(true);
    expect(draftHoldsSnapshot({ a: 1 }, { a: 1, b: 2 })).toBe(false);
    expect(draftHoldsSnapshot({ a: "1" }, { a: 1 })).toBe(false);
    expect(draftHoldsSnapshot({ anything: 1 }, {})).toBe(true);
    expect(draftHoldsSnapshot("text", "text")).toBe(true);
    expect(draftHoldsSnapshot(["a"], ["a", "b"])).toBe(false);
  });
});

describe("ParkedWriteNotice (E)", () => {
  function show(approvalId: string | null, handlers: Record<string, MockHandler>, whoami: WhoamiPayload | null = ME) {
    const recorded = hub(handlers);
    render(
      <TestRoot qc={client(whoami)}>
        <ToastProvider>
          <ParkedWriteNotice approvalId={approvalId} checkHref="/work/tasks/9" />
        </ToastProvider>
      </TestRoot>,
    );
    return recorded;
  }

  it("with nothing stored: waiting, submit the form again, and the Inbox link", async () => {
    show("12", { "GET /api/approvals/12": { body: { status: "pending" } } });
    expect(await screen.findByText(/After it is approved, submit this form again/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/inbox?kind=approval&item=12");
  });

  it.each([
    ["approved", /Submit this form again to make the change/],
    ["denied", /was denied/],
    ["revoked", /approval was revoked/],
    ["expired", /approval expired/],
  ])("nothing stored (resumable: false), approval %s: shows the real state (round 3, item 6)", async (status, text) => {
    show("12", { "GET /api/approvals/12": { body: { status } } });
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  describe("round 6 item C: a slice-supplied continuation replaces 'submit this form again'", () => {
    function showWith(continuation: Parameters<typeof ParkedWriteNotice>[0]["continuation"], status: string) {
      hub({ "GET /api/approvals/12": { body: { status } } });
      render(
        <TestRoot qc={client()}>
          <ToastProvider>
            <ParkedWriteNotice approvalId="12" continuation={continuation} />
          </ToastProvider>
        </TestRoot>,
      );
    }

    it("waiting: says what really happens next, not 'submit this form again'", async () => {
      showWith({ label: "Release it", description: "After it is approved, release the work product.", onAction: vi.fn() }, "pending");
      expect(await screen.findByText(/release the work product/)).toBeInTheDocument();
      expect(screen.queryByText(/submit this form again/)).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Release it" })).not.toBeInTheDocument();
    });

    it("approved: offers the continuation as an action", async () => {
      const onAction = vi.fn();
      showWith({ label: "Release it", onAction }, "approved");
      fireEvent.click(await screen.findByRole("button", { name: "Release it" }));
      expect(onAction).toHaveBeenCalledTimes(1);
      expect(screen.queryByText(/submit this form again/i)).not.toBeInTheDocument();
    });

    it("approved: or as a link when the slice gives an href", async () => {
      showWith({ label: "Open the release", href: "/work/products/wp-17" }, "approved");
      expect(await screen.findByRole("link", { name: "Open the release" })).toHaveAttribute("href", "/work/products/wp-17");
    });
  });

  it("no approval reference says so instead of waiting forever (round 3, item 6)", () => {
    show(null, {});
    expect(screen.getByText(/no approval reference/)).toBeInTheDocument();
  });

  it("after Discard the notice says it was discarded (round 3, item 6)", async () => {
    park();
    show("31", { "GET /api/approvals/31": { body: { status: "pending" } } });
    fireEvent.click(await screen.findByRole("button", { name: "Discard" }));
    expect(await screen.findByText(/was discarded/)).toBeInTheDocument();
  });

  it("own request, waiting: needs a different person, and why it may never be approved (item 10)", async () => {
    park();
    show("31", { "GET /api/approvals/31": { body: { status: "pending", requested_by: "operator" } } });
    expect(await screen.findByText("This needs approval from a different person.")).toBeInTheDocument();
    expect(
      screen.getByText(/If you are the only person who can approve on this hub, it cannot be approved; discard it or ask for the rule to be changed\./),
    ).toBeInTheDocument();
  });

  it("approver unknown, waiting: says that this is not known and is no permission (round 4, item 5)", async () => {
    park();
    show("31", { "GET /api/approvals/31": { body: { status: "pending", requested_by: "Operator" } } });
    expect(await screen.findByText(/It is not known whether you may approve this yourself/)).toBeInTheDocument();
  });

  it("own request with nothing stored says the same (item 10)", async () => {
    show("12", { "GET /api/approvals/12": { body: { status: "pending", requested_by: "operator" } } });
    expect(await screen.findByText("This needs approval from a different person.")).toBeInTheDocument();
  });

  it("announces state changes in one persistent polite, atomic status region (round 3, item 8)", async () => {
    park();
    let status = "pending";
    const qcHolder: { qc?: QueryClient } = {};
    hub({ "GET /api/approvals/31": () => ({ body: { status } }) });
    const qc = client();
    qcHolder.qc = qc;
    render(
      <TestRoot qc={qc}>
        <ToastProvider>
          <ParkedWriteNotice approvalId="31" />
        </ToastProvider>
      </TestRoot>,
    );
    await screen.findByText("Waiting for approval.");
    const region = screen.getByRole("region", { name: "Approval status" });
    const live = within(region).getByRole("status");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveAttribute("aria-atomic", "true");
    expect(within(live).queryByRole("button")).toBeNull();
    status = "approved";
    await refetchApproval(qc);
    await waitFor(() => expect(live).toHaveTextContent(/Approved\./));
    // The same node: a polite region that is replaced is not announced.
    expect(within(screen.getByRole("region", { name: "Approval status" })).getByRole("status")).toBe(live);
  });

  it("waiting: says so, with the Inbox link and no Run button", async () => {
    park();
    show("31", { "GET /api/approvals/31": { body: { status: "pending" } } });
    expect(await screen.findByText("Waiting for approval.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run it now" })).not.toBeInTheDocument();
  });

  it("approved: Run it now sends once on click", async () => {
    park();
    const { calls } = show("31", { "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    const run = await screen.findByRole("button", { name: "Run it now" });
    expect(posts(calls)).toEqual([]);
    fireEvent.click(run);
    await waitFor(() => expect(listParkedWrites(me())).toEqual([]));
    expect(posts(calls)).toHaveLength(1);
  });

  it("after an unverified identity the notice keeps the entry, says why and offers Try again (#2504)", async () => {
    park();
    show("31", { "GET /api/whoami": { status: 503 }, "GET /api/approvals/31": { body: { status: "approved" } } });
    fireEvent.click(await screen.findByRole("button", { name: "Run it now" }));
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getAllByText(/We could not check who is signed in, so this was not run\./).length).toBeGreaterThan(0);
    expect(listParkedWrites(me())).toHaveLength(1);
  });

  it("a run cancelled because someone else is signed in shows nothing (round 5, item 3)", async () => {
    park();
    const qc = client();
    hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
    const onRun = vi.fn();
    render(
      <TestRoot qc={qc}>
        <ToastProvider>
          <ParkedWriteNotice approvalId="31" onRun={onRun} />
        </ToastProvider>
      </TestRoot>,
    );
    const button = await screen.findByRole("button", { name: "Run it now" });
    qc.setQueryData(WHOAMI_QUERY_KEY, { ...ME, agent: "B" });
    fireEvent.click(button);
    await new Promise((r) => setTimeout(r, 100));
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.queryByText(/different person is signed in/)).not.toBeInTheDocument();
  });

  it("round 6 item 4: a run cancelled as unverified after dispatch shows no toast, calls no onRun, and never says it was not sent", async () => {
    park();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    let posted = false;
    hub({
      "GET /api/approvals/31": { body: { status: "approved" } },
      "POST /api/tasks": async () => {
        posted = true;
        await gate;
        return { body: {} };
      },
    });
    const qc = client();
    const onRun = vi.fn();
    render(
      <TestRoot qc={qc}>
        <ToastProvider>
          <ParkedWriteNotice approvalId="31" onRun={onRun} />
        </ToastProvider>
      </TestRoot>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Run it now" }));
    await waitFor(() => expect(posted).toBe(true));
    // Verification fails while the request is out.
    mockFetch({ "GET /api/whoami": { status: 503 } });
    await act(async () => {
      await qc.refetchQueries({ queryKey: WHOAMI_QUERY_KEY }).catch(() => {});
    });
    release();
    await new Promise((r) => setTimeout(r, 100));
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.queryByText(/not sent/)).not.toBeInTheDocument();
    expect(screen.queryByText(/so this was not run/)).not.toBeInTheDocument();
  });

  describe("round 6 item B: recovering from an outcome unknown", () => {
    function unknownEntry() {
      const entry = park();
      updateParkedWrite(me(), entry.id, { outcome: "unknown" });
      return entry;
    }

    it("offers the slice's check link, It is there, It is not there and Discard", async () => {
      unknownEntry();
      show("31", { "GET /api/approvals/31": { body: { status: "approved" } } });
      expect(await screen.findByText(/Outcome unknown\./)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Check it" })).toHaveAttribute("href", "/work/tasks/9");
      expect(screen.getByRole("button", { name: "It is there" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "It is not there, send it again" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
    });

    it("It is there: retires the entry and reports success confirmed by the person, with nothing sent", async () => {
      unknownEntry();
      const onRun = vi.fn();
      const { calls } = hub({ "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
      render(
        <TestRoot qc={client()}>
          <ToastProvider>
            <ParkedWriteNotice approvalId="31" onRun={onRun} />
          </ToastProvider>
        </TestRoot>,
      );
      fireEvent.click(await screen.findByRole("button", { name: "It is there" }));
      expect(onRun).toHaveBeenCalledWith(expect.objectContaining({ kind: "done", confirmedByPerson: true }));
      expect(listParkedWrites(me())).toEqual([]);
      expect(posts(calls)).toEqual([]);
    });

    it("It is not there: warns that a duplicate is possible, and sends once only after the confirmation", async () => {
      unknownEntry();
      const { calls } = show("31", { "GET /api/approvals/31": { body: { status: "approved" } }, "POST /api/tasks": { body: {} } });
      fireEvent.click(await screen.findByRole("button", { name: "It is not there, send it again" }));
      const dialog = await screen.findByRole("dialog", { name: /Send it again/ });
      expect(dialog).toHaveTextContent(/second/i);
      expect(posts(calls)).toEqual([]);
      fireEvent.click(within(dialog).getByRole("button", { name: "Send it again" }));
      await waitFor(() => expect(posts(calls)).toHaveLength(1));
      await waitFor(() => expect(listParkedWrites(me())).toEqual([]));
    });

    it("confirmApplied cannot retire an entry while it is running", async () => {
      park();
      let release: () => void = () => {};
      const gate = new Promise<void>((r) => (release = r));
      hub({
        "GET /api/approvals/31": { body: { status: "approved" } },
        "POST /api/tasks": async () => {
          await gate;
          return { body: {} };
        },
      });
      const id = listParkedWrites(me())[0].id;
      const run = runParkedWrite(id, { epoch: epochOf(client()) });
      await waitFor(() => expect(listParkedWrites(me())[0].attemptStartedAt).toEqual(expect.any(Number)));
      expect(confirmApplied(me(), id)).toMatchObject({ kind: "not_run", reason: "busy" });
      release();
      await run;
    });
  });


  it.each(["denied", "revoked", "expired"])("%s: says which, with Discard and Open the form", async (status) => {
    park();
    show("31", { "GET /api/approvals/31": { body: { status } } });
    expect(await screen.findByText(new RegExp(status === "expired" ? "approval expired" : status))).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the form to run it" })).toHaveAttribute("href", "/work/tasks/new");
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(listParkedWrites(me())).toEqual([]);
  });

  it("no organization reported: Open the form to run it, no Run button", async () => {
    park({ context: { ...CTX, organization: null } });
    show("31", { "GET /api/whoami": { body: whoamiFixture() }, "GET /api/approvals/31": { body: { status: "approved" } } }, whoamiFixture());
    expect(await screen.findByText(/does not report the organization/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the form to run it" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run it now" })).not.toBeInTheDocument();
  });
});
