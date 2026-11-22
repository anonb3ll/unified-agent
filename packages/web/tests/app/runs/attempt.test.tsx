import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAllAttempts, defaultSettle, guardKey, readAttempt as readStored, useStartGuard, type StartAttempt } from "../../../src/app/runs/attempt";
import { epochSetup, type EpochSetup } from "../support/epoch";
import { whoamiFixture } from "../support/fixtures";
import { FakeLockManager, installLocks } from "./locks";

const CONTEXT = { hubOrigin: "http://localhost", organization: "org-1", identity: "operator" };
let locks: FakeLockManager;
/** The owner the records belong to: they live in this epoch's shared storage. */
let ep: EpochSetup;
const readAttempt = (key: string) => readStored(ep.epoch, key);
const guard = (kind: Parameters<typeof useStartGuard>[1], object?: string) =>
  renderHook(() => useStartGuard(CONTEXT, kind, object), { wrapper: ep.wrapper });

beforeEach(() => {
  localStorage.clear();
  locks = installLocks() as FakeLockManager;
  ep = epochSetup({ ...whoamiFixture(), org: "org-1" }, undefined, { connect: false });
});
afterEach(() => {
  vi.restoreAllMocks();
  installLocks();
});

const DRAFT = { summary: "Workflow smoke-echo", match: { name: "smoke-echo" } };

describe("start guard", () => {
  it("stores and verifies the record in the owner's shared storage before sending, keyed by hub, organization, identity, operation and object", async () => {
    const { result } = guard("workflow", "smoke-echo");
    const key = guardKey(CONTEXT, "workflow", "smoke-echo");
    expect(JSON.parse(key.slice("ua-cc2:runs-attempt:".length))).toEqual(["http://localhost", "org-1", "operator", "workflow", "smoke-echo"]);
    let seenBeforeSend: StartAttempt | null = null;
    await act(async () => {
      await result.current.run(
        DRAFT,
        async () => {
          seenBeforeSend = readAttempt(key);
          expect(locks.held.has(key)).toBe(true);
          return { kind: "error" as const, status: null, outcomeUnknown: true };
        },
        defaultSettle,
      );
    });
    expect(seenBeforeSend).toMatchObject({ state: "sending", kind: "workflow", summary: "Workflow smoke-echo" });
    expect(readAttempt(key)).toMatchObject({ state: "unknown" });
    expect(result.current.held).toBe(true);
  });

  it("another tab sees a hold appear and clear through the storage event, and cannot start while it holds", async () => {
    const tabA = guard("cycle");
    const key = guardKey(CONTEXT, "cycle");
    expect(tabA.result.current.held).toBe(false);
    // Another tab writes the record (same origin, so the same localStorage).
    act(() => {
      ep.epoch.sharedStorage.write(key, { id: "x", kind: "cycle", summary: "A cycle", sentAt: Date.now(), state: "unknown", match: {} });
      window.dispatchEvent(new StorageEvent("storage", { key }));
    });
    expect(tabA.result.current.held).toBe(true);
    const send = vi.fn();
    let refusal: unknown;
    await act(async () => {
      const r = await tabA.result.current.run(DRAFT, send, defaultSettle);
      refusal = r.sent ? null : r.refusal;
    });
    expect(refusal).toBe("held");
    expect(send).not.toHaveBeenCalled();
    act(() => {
      ep.epoch.sharedStorage.remove(key);
      window.dispatchEvent(new StorageEvent("storage", { key }));
    });
    expect(tabA.result.current.held).toBe(false);
  });

  it("refuses while another tab holds the lock", async () => {
    const { result } = guard("graph");
    locks.held.add(guardKey(CONTEXT, "graph"));
    const send = vi.fn();
    let r: Awaited<ReturnType<typeof result.current.run>> | undefined;
    await act(async () => {
      r = await result.current.run(DRAFT, send, defaultSettle);
    });
    expect(r).toEqual({ sent: false, refusal: "busy" });
    expect(send).not.toHaveBeenCalled();
  });

  it("refuses to send when the record cannot be stored, or the browser has no Web Locks", async () => {
    const { result } = guard("routine-add");
    const send = vi.fn();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    let r: Awaited<ReturnType<typeof result.current.run>> | undefined;
    await act(async () => {
      r = await result.current.run(DRAFT, send, defaultSettle);
    });
    expect(r).toEqual({ sent: false, refusal: "storage" });
    vi.restoreAllMocks();
    installLocks(null);
    await act(async () => {
      r = await result.current.run(DRAFT, send, defaultSettle);
    });
    expect(r).toEqual({ sent: false, refusal: "no-guard" });
    expect(send).not.toHaveBeenCalled();
  });

  it("a cancellation after the request left stays unknown; one before it left clears", () => {
    expect(defaultSettle({ kind: "cancelled", dispatched: true })).toMatchObject({ record: "unknown" });
    expect(defaultSettle({ kind: "cancelled", dispatched: false })).toEqual({ record: "clear" });
    expect(defaultSettle({ kind: "error", status: 400 })).toEqual({ record: "clear" });
    expect(defaultSettle({ kind: "error", status: 502 })).toMatchObject({ record: "unknown" });
  });

  it("a failed write leaves an earlier record untouched, so its hold stays (item 1)", async () => {
    const { result } = guard("routine-run", "7");
    const key = guardKey(CONTEXT, "routine-run", "7");
    let firstId = "";
    await act(async () => {
      const r = await result.current.run({ summary: "Run now", match: { routineId: 7 } }, async () => ({ kind: "error" as const, status: null, outcomeUnknown: true }), defaultSettle);
      if (r.sent) firstId = r.attempt.id;
    });
    expect(readAttempt(key)).toMatchObject({ id: firstId, state: "unknown" });
    // The same-key retry cannot be stored: nothing is sent and the original record survives.
    const send = vi.fn();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    let r: Awaited<ReturnType<typeof result.current.run>> | undefined;
    await act(async () => {
      r = await result.current.run({ summary: "Run now", match: { routineId: 7 } }, send, defaultSettle, { reuseId: firstId });
    });
    vi.restoreAllMocks();
    expect(r).toEqual({ sent: false, refusal: "storage" });
    expect(send).not.toHaveBeenCalled();
    expect(readAttempt(key)).toMatchObject({ id: firstId, state: "unknown" });
    expect(result.current.held).toBe(true);
  });

  it("resolve names the attempt it settles; a late answer for an older attempt never releases a newer one (item 2)", async () => {
    const { result } = guard("routine-run", "7");
    const key = guardKey(CONTEXT, "routine-run", "7");
    const lost = async () => ({ kind: "error" as const, status: null, outcomeUnknown: true });
    let a = "";
    let b = "";
    await act(async () => {
      const r = await result.current.run(DRAFT, lost, defaultSettle);
      if (r.sent) a = r.attempt.id;
    });
    await act(async () => {
      const r = await result.current.run(DRAFT, lost, defaultSettle, { replaceHeld: a });
      if (r.sent) b = r.attempt.id;
    });
    expect(b).not.toBe(a);
    let released = true;
    await act(async () => {
      released = await result.current.resolve(a);
    });
    expect(released).toBe(false);
    expect(readAttempt(key)?.id).toBe(b);
    await act(async () => {
      released = await result.current.resolve(b);
    });
    expect(released).toBe(true);
    expect(readAttempt(key)).toBeNull();
  });

  it("resolve waits for a start in flight under the same lock instead of racing it (item 2)", async () => {
    const { result } = guard("workflow", "smoke-echo");
    const key = guardKey(CONTEXT, "workflow", "smoke-echo");
    let finish: () => void = () => {};
    let started: Promise<unknown> = Promise.resolve();
    let first = "";
    act(() => {
      started = result.current.run(DRAFT, () => new Promise<{ kind: "error"; status: number }>((r) => (finish = () => r({ kind: "error", status: 503 }))), defaultSettle).then((r) => {
        if (r.sent) first = r.attempt.id;
      });
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    const sendingId = readAttempt(key)?.id ?? "";
    let resolved: Promise<boolean> = Promise.resolve(false);
    act(() => {
      resolved = result.current.resolve(sendingId);
    });
    finish();
    await act(async () => {
      await started;
    });
    // The resolve ran after the answer settled the record as unknown, and it names that attempt.
    expect(await resolved).toBe(true);
    expect(first).toBe(sendingId);
    expect(readAttempt(key)).toBeNull();
  });

  it("sign-out clears the owner's records even when no Runs page is rendered (item 3)", async () => {
    const { result, unmount } = guard("workflow", "smoke-echo");
    await act(async () => {
      await result.current.run(DRAFT, async () => ({ kind: "error" as const, status: 503 }), defaultSettle);
    });
    const key = guardKey(CONTEXT, "workflow", "smoke-echo");
    expect(readAttempt(key)).not.toBeNull();
    unmount();
    // The shell's sign-out calls this under the epoch it started in (AppLayout).
    clearAllAttempts(ep.epoch);
    expect(readAttempt(key)).toBeNull();
  });
});
