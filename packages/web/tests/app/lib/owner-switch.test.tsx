/**
 * Staged owner-switch harness over every write path (spec §6, "Owner
 * epoch"; review round 7, step 3). Each path is run as owner A; the owner
 * becomes B at one stage boundary (see `support/ownerSwitch.tsx`), and no
 * effect of A's operation may land after that: no storage write, cache
 * write, toast, navigation, slice callback or request.
 */
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WHOAMI_QUERY_KEY, type WhoamiPayload } from "../../../src/lib/api/auth";
import { useDraft } from "../../../src/app/lib/drafts";
import { useIdentity } from "../../../src/app/lib/identity";
import { useHubWrite, type WriteOutcome } from "../../../src/app/lib/mutations";
import type { OwnerEpoch } from "../../../src/app/lib/epoch";
import { ParkedWriteNotice } from "../../../src/app/lib/ParkedWriteNotice";
import { addParkedWrite } from "../../../src/app/lib/parkedWrites";
import { attentionFixture, approvalsFixture } from "../support/fixtures";
import {
  CONTEXT_A,
  OWNER_A,
  OWNER_B,
  ownerSwitchHarness,
  STAGES,
  visibleToasts,
  type OwnerSwitchHarness,
  type Stage,
} from "../support/ownerSwitch";
import { findPageHeading, renderRoute } from "../support/render";

const TASK = { "POST /api/tasks": { body: { id: 7 } } };
const PARKED = { "POST /api/tasks": { status: 202, body: { approval_id: 31, status: "pending", code: "approval_required" } } };
const APPROVED = { "GET /api/approvals/31": { body: { id: 31, status: "approved", requested_by: "C" } } };

let harness: OwnerSwitchHarness | null = null;
let startHref = "";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  startHref = window.location.href;
});
afterEach(() => {
  harness?.dispose();
  harness = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function make(stage: Stage, options: Parameters<typeof ownerSwitchHarness>[1]) {
  harness = ownerSwitchHarness(stage, options);
  return harness;
}

/** What every path must show after the switch. */
function expectNothingUnderB(h: OwnerSwitchHarness) {
  expect(h.switched).toBe(true);
  expect(h.effects).toEqual([]);
  expect(visibleToasts()).toBe("");
  expect(window.location.href).toBe(startHref);
}

/** A path with no toast: the during-toast boundary does not exist, and no toast is ever shown. */
function expectNoToastBoundary(h: OwnerSwitchHarness) {
  expect(h.toasts).toEqual([]);
  expect(h.switched).toBe(false);
  expect(h.effects).toEqual([]);
}

const parkedKeys = () => Object.keys(sessionStorage).filter((k) => k.endsWith(":parked-writes"));

describe.each(STAGES)("owner switches %s", (stage) => {
  it("useHubWrite, an ordinary write: cancelled, no cache, toast, callback or navigation under B", async () => {
    const h = make(stage, { before: "GET /api/whoami", primary: "POST /api/tasks", routes: TASK });
    const s = h.epochSetup();
    const successMessage = h.callback("successMessage", () => "Task created");
    const { result } = renderHook(() => useHubWrite({ path: "/api/tasks", invalidates: [["tasks"]], successMessage }), {
      wrapper: s.wrapper,
    });
    let outcome: WriteOutcome<unknown> | undefined;
    await act(async () => {
      outcome = await result.current.run({ title: "x" });
    });
    // The caller's continuation: it navigates only on done.
    if (outcome?.kind === "done") h.navigate("/work/tasks/7");
    await h.settle();
    expect(outcome?.kind).toBe("cancelled");
    if (stage === "before-send") expect(h.requests()).not.toContain("POST /api/tasks");
    expectNothingUnderB(h);
  });

  it("useHubWrite, a parked write: nothing stored after the switch, no pending message, toast or navigation", async () => {
    const h = make(stage, { before: "GET /api/whoami", primary: "POST /api/tasks", routes: PARKED });
    const s = h.epochSetup();
    const pendingMessage = h.callback("pendingMessage", () => "Waiting for approval");
    const { result } = renderHook(() => useHubWrite({ path: "/api/tasks", label: "Create task", pendingMessage }), {
      wrapper: s.wrapper,
    });
    let outcome: WriteOutcome<unknown> | undefined;
    await act(async () => {
      outcome = await result.current.run({ title: "x" });
    });
    if (outcome?.kind === "pending_approval") h.navigate("/inbox?kind=approval&item=31");
    await h.settle();
    expect(outcome?.kind).toBe("cancelled");
    // Stored only when A's toast was already shown (during-toast), and then under A.
    if (stage === "during-toast") expect(parkedKeys()).toEqual([expect.stringContaining('"A"]:')]);
    else expect(parkedKeys()).toEqual([]);
    expectNothingUnderB(h);
  });

  it("a parked write's replay (Run it now): no result, toast or onRun under B", async () => {
    const h = make(stage, { before: "GET /api/whoami", primary: "POST /api/tasks", routes: { ...APPROVED, ...TASK } });
    const s = h.epochSetup();
    addParkedWrite(s.epoch, {
      path: "/api/tasks",
      method: "POST",
      body: { title: "x" },
      approvalId: "31",
      label: "Create task",
      context: CONTEXT_A,
      invalidates: [["tasks"]],
      idempotencyKey: "idem-1",
    });
    const onRun = h.callback("onRun");
    render(<ParkedWriteNotice approvalId="31" onRun={onRun} />, { wrapper: s.wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Run it now" }));
    await waitFor(() => expect(h.switched).toBe(true));
    await h.settle();
    if (stage === "before-send") expect(h.requests()).not.toContain("POST /api/tasks");
    expectNothingUnderB(h);
  });

  it("a form draft (debounced save, flush, clear after the write): nothing written or cleared under B", async () => {
    const h = make(stage, { before: "GET /api/whoami", primary: "POST /api/tasks", routes: TASK });
    const s = h.epochSetup();
    const cleared = h.callback("draftCleared");
    function DraftForm() {
      const draft = useDraft({ identity: useIdentity(), operation: "task.create", initial: { title: "" }, debounceMs: 20 });
      const write = useHubWrite({ path: "/api/tasks", successMessage: "Task created" });
      return (
        <>
          <input aria-label="Title" value={draft.value.title} onChange={(e) => draft.setValue({ title: e.target.value })} />
          <button
            type="button"
            onClick={() =>
              void write.run(draft.value).then((outcome) => {
                if (outcome.kind !== "done") return;
                draft.clear();
                cleared();
              })
            }
          >
            Save
          </button>
        </>
      );
    }
    render(<DraftForm />, { wrapper: s.wrapper });
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "typed by A" } });
    if (stage === "before-send") {
      // B arrives while the debounced save is still pending.
      act(() => h.switchOwner());
    } else if (stage === "after-send") {
      // B arrives, then the page is hidden before the tree remounts: the flush runs.
      act(() => {
        h.switchOwner();
        window.dispatchEvent(new Event("pagehide"));
      });
    } else {
      // Saved under A, then the write: B arrives with its answer, or during its toast.
      await h.settle();
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(h.switched).toBe(true));
    }
    await h.settle();
    expectNothingUnderB(h);
  });

  it("ParkedWriteNotice's approval read: nothing published under B or into A's dead client", async () => {
    const h = make(stage, { primary: "GET /api/approvals/31", routes: APPROVED });
    const s = h.epochSetup();
    const a = s.epoch;
    render(<ParkedWriteNotice approvalId="31" />, { wrapper: s.wrapper });
    if (stage === "during-toast") {
      expect(await screen.findByText(/Submit this form again to make the change/)).toBeInTheDocument();
      expectNoToastBoundary(h);
      return;
    }
    await waitFor(() => expect(h.switched).toBe(true));
    await h.settle();
    expectNothingUnderB(h);
    expect(a.queryClient.getQueryCache().getAll()).toEqual([]);
    const b = s.epoch;
    expect(b.queryClient.getQueryCache().getAll().every((q) => JSON.stringify(q.queryKey).includes('\\"B\\"'))).toBe(true);
  });

  it("sign-out: a late completion leaves B's whoami alone; nothing sent after the switch", async () => {
    const { queryClient, router } = renderRoute("/home", { hub: { "GET /api/whoami": { body: OWNER_A } } });
    await findPageHeading("Home");
    const h = make(stage, {
      before: "GET /api/whoami",
      primary: "DELETE /api/session",
      routes: {
        "DELETE /api/session": { status: 204 },
        "GET /api/attention": { body: attentionFixture(0) },
        "GET /api/approvals": { body: approvalsFixture() },
      },
    });
    h.watch(queryClient);
    const at = router.state.location.pathname;
    fireEvent.click(await screen.findByRole("button", { name: "Account: A" }));
    await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" })));
    if (stage === "during-toast") {
      await waitFor(() => expect(h.requests()).toContain("DELETE /api/session"));
      await h.settle();
      expectNoToastBoundary(h);
      return;
    }
    await waitFor(() => expect(h.switched).toBe(true));
    await h.settle();
    expectNothingUnderB(h);
    expect((queryClient.getQueryData(WHOAMI_QUERY_KEY) as WhoamiPayload | null)?.agent).toBe("B");
    expect(router.state.location.pathname).toBe(at);
    if (stage === "before-send") expect(h.requests()).not.toContain("DELETE /api/session");
  });
});

/**
 * Review round 8: the cookie and the cached whoami move separately, and
 * completions arrive after B is verified (a 401, a sign-in confirmation).
 */
describe("round 8: the hub's cookie changes while the page still observes A", () => {
  async function signOutAsA(stage: Stage) {
    const { queryClient } = renderRoute("/home", { hub: { "GET /api/whoami": { body: OWNER_A } } });
    await findPageHeading("Home");
    const h = make(stage, {
      cookieOnly: true,
      before: "GET /api/whoami",
      primary: "DELETE /api/session",
      routes: {
        "DELETE /api/session": { status: 204 },
        "GET /api/attention": { body: attentionFixture(0) },
        "GET /api/approvals": { body: approvalsFixture() },
      },
    });
    h.watch(queryClient);
    fireEvent.click(await screen.findByRole("button", { name: "Account: A" }));
    await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" })));
    await waitFor(() => expect(h.switched).toBe(true));
    await h.settle();
    return { h, queryClient };
  }

  it.each(["before-send", "csrf-retry"] as const)(
    "item 1: A's sign-out never sends DELETE /api/session for B's cookie (%s)",
    async (stage) => {
      const { h } = await signOutAsA(stage);
      const deletes = h.requests().filter((r) => r === "DELETE /api/session");
      // Before the send: none. Through the CSRF retry: only A's first, refused attempt.
      expect(deletes).toHaveLength(stage === "before-send" ? 0 : 1);
      expect(h.effects).toEqual([]);
    },
  );

  it.each(["before-send", "csrf-retry"] as const)("useHubWrite and a replay send nothing with B's token (%s)", async (stage) => {
    const h = make(stage, { cookieOnly: true, before: "GET /api/whoami", primary: "POST /api/tasks", routes: { ...APPROVED, ...TASK } });
    const s = h.epochSetup();
    const { result } = renderHook(() => useHubWrite({ path: "/api/tasks" }), { wrapper: s.wrapper });
    let outcome: WriteOutcome<unknown> | undefined;
    await act(async () => {
      outcome = await result.current.run({ title: "x" });
    });
    expect(outcome?.kind).toBe("cancelled");
    await h.settle();
    expect(h.effects).toEqual([]);
  });
});

describe("round 8: completions that arrive after B is verified", () => {
  it.each(["after-send", "after-answer"] as const)(
    "item 2: A's late 401 does not revoke B's epoch or clear B's cache (%s)",
    async (stage) => {
      const h = make(stage, {
        before: "GET /api/whoami",
        primary: "POST /api/tasks",
        routes: { "POST /api/tasks": { status: 401, body: { error: "unauthorized" } } },
      });
      const s = h.epochSetup();
      const { result } = renderHook(() => useHubWrite({ path: "/api/tasks" }), { wrapper: s.wrapper });
      let b: OwnerEpoch | null = null;
      let outcome: WriteOutcome<unknown> | undefined;
      await act(async () => {
        const running = result.current.run({ title: "x" });
        await waitFor(() => expect(h.epochAfterSwitch).not.toBeNull());
        b = h.epochAfterSwitch;
        // B's own data, under a key that names B (not an effect of A's write).
        b!.queryClient.setQueryData(["b-data", b!.ownerKey], 1);
        outcome = await running;
      });
      await h.settle();
      expect(outcome?.kind).toBe("cancelled");
      expect(b!.owner?.identity).toBe("B");
      expect(b!.revoked).toBe(false);
      expect(s.epoch).toBe(b);
      expect(b!.queryClient.getQueryData(["b-data", b!.ownerKey])).toBe(1);
      expect(h.effects).toEqual([]);
    },
  );

  it("item 2: a replay's late 401 does not revoke B's epoch", async () => {
    const h = make("after-send", {
      before: "GET /api/whoami",
      primary: "POST /api/tasks",
      routes: { ...APPROVED, "POST /api/tasks": { status: 401, body: { error: "unauthorized" } } },
    });
    const s = h.epochSetup();
    addParkedWrite(s.epoch, {
      path: "/api/tasks",
      method: "POST",
      body: { title: "x" },
      approvalId: "31",
      label: "Create task",
      context: CONTEXT_A,
      invalidates: [],
    });
    render(<ParkedWriteNotice approvalId="31" />, { wrapper: s.wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Run it now" }));
    await waitFor(() => expect(h.epochAfterSwitch).not.toBeNull());
    const b = h.epochAfterSwitch!;
    await h.settle();
    expect(b.owner?.identity).toBe("B");
    expect(b.revoked).toBe(false);
    expect(s.epoch).toBe(b);
    expect(h.effects).toEqual([]);
  });

  it.each(["after-send", "after-answer"] as const)(
    "item 3: a sign-in confirmed after B is verified does not revoke B or touch the return path (%s)",
    async (stage) => {
      const { queryClient } = renderRoute("/home", { hub: { "GET /api/whoami": { status: 401, body: { error: "unauthorized" } } } });
      fireEvent.change(await screen.findByLabelText(/token/i), { target: { value: "t0ken" } });
      const h = make(stage, {
        primary: "POST /api/session",
        routes: {
          "POST /api/session": { body: { ok: true } },
          "GET /api/whoami": () => (h.switched ? { body: OWNER_B } : { status: 401, body: { error: "unauthorized" } }),
          "GET /api/attention": { body: attentionFixture(0) },
          "GET /api/approvals": { body: approvalsFixture() },
        },
      });
      h.watch(queryClient);
      fireEvent.click(screen.getByRole("button", { name: "Sign In" }));
      await waitFor(() => expect(h.switched).toBe(true));
      // B's shell is up: the sign-in form is gone.
      const main = await screen.findByRole("main");
      await h.settle();
      expect(screen.getByRole("main")).toBe(main);
      expect((queryClient.getQueryData(WHOAMI_QUERY_KEY) as WhoamiPayload | null)?.agent).toBe("B");
      expect(h.effects).toEqual([]);
    },
  );
});

describe("round 9: the cookie flips between the retry's whoami read and its DELETE", () => {
  it("the hub refuses A's token on B's cookie; the page re-reads whoami and never resets it", async () => {
    const { queryClient } = renderRoute("/home", { hub: { "GET /api/whoami": { body: OWNER_A } } });
    await findPageHeading("Home");
    const tokens: (string | undefined)[] = [];
    const h = make("retry-flip", {
      before: "GET /api/whoami",
      primary: "DELETE /api/session",
      routes: {
        // The hub's rule (webui/routes.py session_delete): the token must be
        // the CSRF token of the session the cookie names.
        "DELETE /api/session": ({ headers, cookie }) => {
          tokens.push(headers["x-ua-csrf"]);
          return headers["x-ua-csrf"] === cookie.csrf
            ? { status: 204 }
            : { status: 403, body: { error: "The CSRF token does not belong to the session this cookie names.", code: "csrf_session_mismatch" } };
        },
        "GET /api/attention": { body: attentionFixture(0) },
        "GET /api/approvals": { body: approvalsFixture() },
      },
    });
    h.watch(queryClient);
    fireEvent.click(await screen.findByRole("button", { name: "Account: A" }));
    await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" })));
    await waitFor(() => expect(h.switched).toBe(true));
    await h.settle();
    // Both attempts carried A's token; the second reached the hub on B's cookie and was refused.
    expect(h.requests().filter((r) => r === "DELETE /api/session")).toHaveLength(2);
    const sentTokens = h.fetch.mock.calls
      .filter(([, init]) => (init as RequestInit | undefined)?.method === "DELETE")
      .map(([, init]) => ((init as RequestInit).headers as Record<string, string>)["x-ua-csrf"]);
    expect(sentTokens).toEqual([OWNER_A.csrf, OWNER_A.csrf]);
    // The hub saw the retry, with A's token, on B's cookie.
    expect(tokens).toEqual([OWNER_A.csrf]);
    // Not signed out: whoami is read again (it now names B), never reset or nulled.
    expect(h.effects).toEqual(["root:whoami:invalidate"]);
    await waitFor(() => expect((queryClient.getQueryData(WHOAMI_QUERY_KEY) as WhoamiPayload | null)?.agent).toBe("B"));
  });
});
