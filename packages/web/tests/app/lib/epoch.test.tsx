import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HttpAuthError, WHOAMI_QUERY_KEY } from "../../../src/lib/api/auth";
import { authGeneration, bumpAuthGeneration } from "../../../src/app/lib/authGeneration";
import { AUTH_CHANNEL, createEpochHost, OwnerEpochProvider, useEpoch, type OwnerEpoch } from "../../../src/app/lib/epoch";
import { epochSetup } from "../support/epoch";
import { whoamiFixture } from "../support/fixtures";
import { testQueryClient } from "../support/render";

const A = whoamiFixture({ agent: "A" });
const B = whoamiFixture({ agent: "B" });

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

/** Let queued microtasks and channel messages land. */
const settle = () => act(async () => new Promise((r) => setTimeout(r, 20)));

describe("owner epoch: what revokes it (spec §6, Owner epoch)", () => {
  it("an identity change revokes: signal aborted, client cleared, storage and toasts refused", () => {
    const s = epochSetup(A);
    const a = s.epoch;
    a.queryClient.setQueryData(["x"], 1);
    expect(a.storage.write("k", 1)).toBe(true);
    s.root.setQueryData(WHOAMI_QUERY_KEY, B);
    expect(a.revoked).toBe(true);
    expect(a.signal.aborted).toBe(true);
    expect(a.queryClient.getQueryData(["x"])).toBeUndefined();
    expect(a.storage.write("k", 2)).toBe(false);
    expect(a.storage.read("k", "none")).toBe("none");
    expect(a.toast({ message: "late" })).toBeUndefined();
    expect(s.epoch.owner?.identity).toBe("B");
    // B's storage is its own: A's key is not visible under B.
    expect(s.epoch.storage.read("k", "none")).toBe("none");
  });

  it("a 401 revokes; a transient whoami failure does not (departure 1)", () => {
    const s = epochSetup(A);
    const a = s.epoch;
    act(() => {
      s.root.getQueryCache().find({ queryKey: WHOAMI_QUERY_KEY })!.setState({
        status: "error",
        error: new Error("503"),
        errorUpdatedAt: Date.now(),
        fetchStatus: "idle",
      });
    });
    expect(a.revoked).toBe(false);
    expect(s.epoch).toBe(a);
    // The per-send checks still refuse: nothing observed while the read failed.
    expect(a.observed()).toBeNull();
    act(() => {
      s.root.getQueryCache().find({ queryKey: WHOAMI_QUERY_KEY })!.setState({
        status: "error",
        error: new HttpAuthError(401, "unauthorized"),
        errorUpdatedAt: Date.now(),
        fetchStatus: "idle",
      });
    });
    expect(a.revoked).toBe(true);
    expect(s.epoch.owner).toBeNull();
  });

  it("an auth generation bump (a 401 seen by a write) revokes at once", () => {
    const s = epochSetup(A);
    const a = s.epoch;
    bumpAuthGeneration();
    expect(a.revoked).toBe(true);
  });

  it("the routed tree mounts under the epoch and remounts when it changes", async () => {
    const s = epochSetup(A);
    const seen: number[] = [];
    function Probe() {
      const epoch = useEpoch();
      seen.push(epoch.id);
      return <p>owner {epoch.owner?.identity ?? "none"}</p>;
    }
    render(<Probe />, { wrapper: s.wrapper });
    expect(screen.getByText("owner A")).toBeInTheDocument();
    act(() => void s.root.setQueryData(WHOAMI_QUERY_KEY, B));
    expect(await screen.findByText("owner B")).toBeInTheDocument();
    expect(new Set(seen).size).toBe(2);
  });
});

describe("sign-out is bound to the epoch it started in (departure 2)", () => {
  it("a sign-out from a revoked epoch does nothing to the current owner", () => {
    const s = epochSetup(A);
    const a = s.epoch;
    s.root.setQueryData(WHOAMI_QUERY_KEY, B);
    const b = s.epoch;
    const generation = authGeneration();
    expect(s.host.beginSignOut(a)).toBeNull();
    expect(b.revoked).toBe(false);
    expect(authGeneration()).toBe(generation);
  });

  it("revokes the epoch it started in at once; finish clears whoami while the lease holds", () => {
    const s = epochSetup(A);
    const a = s.epoch;
    const signOut = s.host.beginSignOut(a)!;
    expect(a.revoked).toBe(true);
    expect(signOut.from).toBe(a);
    expect(signOut.live).toBe(true);
    // A answering again while the request is in flight keeps the lease.
    s.root.setQueryData(WHOAMI_QUERY_KEY, { ...A });
    expect(signOut.live).toBe(true);
    signOut.finish(true);
    expect(s.root.getQueryData(WHOAMI_QUERY_KEY)).toBeNull();
  });

  it.each([
    ["B becomes the owner", (s: ReturnType<typeof epochSetup>) => s.root.setQueryData(WHOAMI_QUERY_KEY, B)],
    ["a sign-in finishes in this tab", (s: ReturnType<typeof epochSetup>) => s.host.signedIn(s.host.current)],
    ["a 401 is seen", () => bumpAuthGeneration()],
  ])("after %s, a late completion touches nothing", (_name, next) => {
    const s = epochSetup(A);
    const signOut = s.host.beginSignOut(s.epoch)!;
    next(s);
    const after = s.epoch;
    const whoami = s.root.getQueryData(WHOAMI_QUERY_KEY);
    const invalidate = vi.spyOn(s.root, "invalidateQueries");
    expect(signOut.live).toBe(false);
    expect(signOut.signal.aborted).toBe(true);
    signOut.finish(true);
    expect(s.root.getQueryData(WHOAMI_QUERY_KEY)).toBe(whoami);
    expect(invalidate).not.toHaveBeenCalled();
    expect(after.revoked).toBe(false);
  });
});

describe("routed tree mounts only after whoami first answers (departure 3)", () => {
  it("the first epoch has no owner until whoami answers, then the owner's epoch replaces it", () => {
    const root = testQueryClient();
    const host = createEpochHost(root);
    const stop = host.connect();
    const first = host.current;
    expect(first.owner).toBeNull();
    root.setQueryData(WHOAMI_QUERY_KEY, A);
    expect(first.revoked).toBe(true);
    expect(host.current.owner?.identity).toBe("A");
    stop();
  });
});

describe("another tab's sign-out revokes this tab's epoch (BroadcastChannel)", () => {
  it("a second tab signing out revokes the first tab's epoch; the tab that signed out keeps its lease", async () => {
    const tab1 = epochSetup(A);
    const tab2 = epochSetup(A);
    const inTab1 = tab1.epoch;
    const inTab2 = tab2.epoch;
    const refetch = vi.spyOn(tab1.root, "resetQueries");
    render(<p>tab 1</p>, { wrapper: tab1.wrapper });
    const signOut = tab2.host.beginSignOut(inTab2)!;
    await settle();
    await waitFor(() => expect(inTab1.revoked).toBe(true));
    expect(inTab1.signal.aborted).toBe(true);
    expect(inTab1.storage.write("k", 1)).toBe(false);
    expect(inTab1.toast({ message: "late" })).toBeUndefined();
    // Only a whoami answer from after the message names the next owner.
    expect(tab1.epoch.owner).toBeNull();
    expect(refetch).toHaveBeenCalledWith({ queryKey: WHOAMI_QUERY_KEY });
    // The tab that signed out does not revoke the epoch it made for itself.
    expect(signOut.live).toBe(true);
  });

  it("any message on the channel revokes, whatever it says", async () => {
    const s = epochSetup(A);
    const a = s.epoch;
    const other = new BroadcastChannel(AUTH_CHANNEL);
    other.postMessage({ kind: "sign-in", at: Date.now() });
    other.close();
    await settle();
    await waitFor(() => expect(a.revoked).toBe(true));
  });

  it("a disconnected host no longer listens", async () => {
    const root = testQueryClient();
    root.setQueryData(WHOAMI_QUERY_KEY, A);
    const host = createEpochHost(root);
    const stop = host.connect();
    stop();
    const a = host.peek();
    const other = new BroadcastChannel(AUTH_CHANNEL);
    other.postMessage({ kind: "sign-out", at: Date.now() });
    other.close();
    await settle();
    expect(a.revoked).toBe(false);
  });
});

describe("the provider", () => {
  it("renders the routed tree under the epoch's own QueryClient, not the root one", () => {
    const s = epochSetup(A);
    let seen: OwnerEpoch | null = null;
    function Probe() {
      seen = useEpoch();
      return null;
    }
    render(
      <QueryClientProvider client={s.root}>
        <OwnerEpochProvider host={s.host}>
          <Probe />
        </OwnerEpochProvider>
      </QueryClientProvider>,
    );
    expect(seen).toBe(s.epoch);
    expect(s.epoch.queryClient).not.toBe(s.root);
  });
});
