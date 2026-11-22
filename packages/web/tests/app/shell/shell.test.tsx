import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { saveDraft, readDraft } from "../../../src/app/lib/drafts";
import { addParkedWrite, listParkedWrites } from "../../../src/app/lib/parkedWrites";
import { WHOAMI_QUERY_KEY } from "../../../src/lib/api/auth";
import { attentionFixture, whoamiFixture } from "../support/fixtures";
import { epochSetup } from "../support/epoch";
import { malformedAttentionWithoutOperatorErrors } from "../support/malformed";
import { findPageHeading, renderRoute } from "../support/render";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

function press(key: string, init: KeyboardEventInit = {}) {
  fireEvent.keyDown(document.body, { key, ...init });
}

describe("shell", () => {
  it("renders the eight destinations in order, with the current one marked", async () => {
    renderRoute("/work/goals");
    await findPageHeading("Goals");
    const nav = screen.getAllByRole("navigation", { name: "Primary" })[0];
    const links = within(nav).getAllByRole("link");
    expect(links.map((l) => l.textContent?.replace(/[–\d,].*$/, "").trim())).toEqual([
      "Home", "Inbox", "Work", "Agents", "Sessions", "Knowledge", "Runs", "Settings",
    ]);
    expect(within(nav).getByRole("link", { name: /^Work/ })).toHaveAttribute("aria-current", "page");
  });

  it("builds the breadcrumb from route handles and sets the document title", async () => {
    renderRoute("/work/tasks/42");
    await findPageHeading("Task 42");
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Work" })).toHaveAttribute("href", "/ui/work");
    expect(within(crumbs).getByText("Task 42")).toHaveAttribute("aria-current", "page");
    await waitFor(() => expect(document.title).toBe("Task 42 · Control Center"));
  });

  it("moves focus to the new page's h1 after navigation, not on first load", async () => {
    renderRoute("/home");
    const home = await findPageHeading("Home");
    expect(home).not.toHaveFocus();
    fireEvent.click(screen.getAllByRole("link", { name: /^Runs/ })[0]);
    const runs = await findPageHeading("Runs");
    await waitFor(() => expect(runs).toHaveFocus());
  });

  it("does not take focus to the h1 from a field focused after the page appeared", async () => {
    // Frames run only when flushed, as on a busy machine where the next frame
    // comes after the person has already clicked into a field.
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => frames.push(cb));
    vi.stubGlobal("cancelAnimationFrame", () => {});
    const flushFrames = () => act(() => frames.splice(0).forEach((cb) => cb(performance.now())));
    renderRoute("/home");
    await findPageHeading("Home");
    press("c");
    const heading = await findPageHeading("New task");
    const title = screen.getByRole("textbox", { name: /^Title/ });
    title.focus();
    flushFrames();
    expect(title).toHaveFocus();
    expect(heading).not.toHaveFocus();
    // With focus left where the navigation put it, the next page's h1 still gets it.
    title.blur();
    fireEvent.click(screen.getAllByRole("link", { name: /^Runs/ })[0]);
    const runs = await findPageHeading("Runs");
    flushFrames();
    expect(runs).toHaveFocus();
  });

  it("keyboard: g + letter, c, ? and ⌘K; nothing fires while typing", async () => {
    const { router } = renderRoute("/home");
    await findPageHeading("Home");
    press("g");
    press("k");
    await waitFor(() => expect(router.state.location.pathname).toBe("/ui/knowledge/memory"));
    press("c");
    await waitFor(() => expect(router.state.location.pathname).toBe("/ui/work/tasks/new"));
    press("?");
    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("dialog", { name: "Keyboard shortcuts" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument());
    press("k", { ctrlKey: true });
    const search = await screen.findByRole("combobox", { name: "Search pages and commands" });
    fireEvent.keyDown(search, { key: "g" });
    fireEvent.keyDown(search, { key: "h" });
    expect(router.state.location.pathname).toBe("/ui/work/tasks/new");
  });

  it("the palette filters and navigates with Enter", async () => {
    const { router } = renderRoute("/home");
    await findPageHeading("Home");
    fireEvent.click(screen.getByRole("button", { name: /^Search or jump to/ }));
    const input = await screen.findByRole("combobox", { name: "Search pages and commands" });
    fireEvent.change(input, { target: { value: "routines" } });
    expect(screen.getByRole("option", { name: /Routines/ })).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(router.state.location.pathname).toBe("/ui/runs/routines"));
  });

  it("shows the inbox count from a complete read and – when unknown", async () => {
    renderRoute("/home", { hub: { "GET /api/attention": { body: attentionFixture(3) } } });
    await findPageHeading("Home");
    const nav = screen.getAllByRole("navigation", { name: "Primary" })[0];
    await waitFor(() => expect(within(nav).getByRole("link", { name: /Inbox/ })).toHaveTextContent("3"));
  });

  it("shows – for the inbox count when the read fails", async () => {
    renderRoute("/home", { hub: { "GET /api/attention": { status: 503, body: { error: "down" } } } });
    await findPageHeading("Home");
    const nav = screen.getAllByRole("navigation", { name: "Primary" })[0];
    await waitFor(() => expect(within(nav).getByRole("link", { name: /Inbox/ })).toHaveTextContent("–"));
    expect(within(nav).getByRole("link", { name: /Inbox/ })).not.toHaveTextContent("0");
  });

  it.each([
    ["a failed producer in needs_operator.errors", attentionFixture(0, { operatorErrors: ["approvals: database is locked"] })],
    ["no needs_operator.errors list at all", malformedAttentionWithoutOperatorErrors(4)],
  ])("shows – for the inbox count with %s (round 4, item 6)", async (_name, body) => {
    renderRoute("/home", { hub: { "GET /api/attention": { body } } });
    await findPageHeading("Home");
    const nav = screen.getAllByRole("navigation", { name: "Primary" })[0];
    await waitFor(() => expect(within(nav).getByRole("link", { name: /Inbox/ })).toHaveTextContent("–"));
    expect(within(nav).getByRole("link", { name: /Inbox/ })).not.toHaveTextContent(/\d/);
  });

  it("on 401 shows sign-in, then returns to the requested page", async () => {
    let signedIn = false;
    const { router } = renderRoute("/work/goals", {
      hub: {
        "GET /api/whoami": () => (signedIn ? { body: whoamiFixture() } : { status: 401, body: { error: "unauthorized" } }),
        "POST /api/session": () => {
          signedIn = true;
          return { body: { agent: "operator", csrf: "c" } };
        },
        "GET /api/session/options": { body: { workos: false } },
      },
    });
    const token = await screen.findByLabelText(/Bearer Token/i);
    expect(document.title).toBe("Sign in · Control Center");
    fireEvent.change(token, { target: { value: "t0ken" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));
    expect(await findPageHeading("Goals")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/ui/work/goals");
  });

  it("sign out clears drafts and parked writes", async () => {
    const operator = () => epochSetup(whoamiFixture(), undefined, { connect: false }).epoch;
    saveDraft(operator(), "operator|task.create|", { title: "x" });
    addParkedWrite(operator(), {
      path: "/api/tasks",
      method: "POST",
      body: {},
      approvalId: "1",
      label: "Create task",
      context: { hubOrigin: window.location.origin, organization: null, identity: "operator" },
      invalidates: [],
    });
    renderRoute("/home", { hub: { "DELETE /api/session": { status: 204 }, "GET /api/approvals/1": { body: { status: "pending" } } } });
    await findPageHeading("Home");
    fireEvent.click(await screen.findByRole("button", { name: "Account: operator" }));
    await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" })));
    await waitFor(() => expect(readDraft(operator(), "operator|task.create|")).toBeNull());
    expect(listParkedWrites(operator())).toEqual([]);
  });

  it("sign-out drops drafts, stored writes and the return path before its request goes out (F)", async () => {
    const { rememberReturnPath, takeReturnPath } = await import("../../../src/app/lib/returnTo");
    const operator = () => epochSetup(whoamiFixture(), undefined, { connect: false }).epoch;
    saveDraft(operator(), "operator|task.create|", { title: "x" });
    addParkedWrite(operator(), {
      path: "/api/tasks",
      method: "POST",
      body: {},
      approvalId: "1",
      label: "Create task",
      context: { hubOrigin: window.location.origin, organization: null, identity: "operator" },
      invalidates: [],
    });
    rememberReturnPath("/ui/agents/hosts");
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    let sawStorage: { drafts: unknown; writes: number; returnTo: string | null } | null = null;
    renderRoute("/home", {
      hub: {
        "DELETE /api/session": async () => {
          // Observed when the sign-out request reaches the hub.
          sawStorage = {
            drafts: readDraft(operator(), "operator|task.create|"),
            writes: listParkedWrites(operator()).length,
            returnTo: sessionStorage.getItem("ua-cc2:return-to"),
          };
          await gate;
          return { status: 204 };
        },
        "GET /api/approvals/1": { body: { status: "pending" } },
      },
    });
    await findPageHeading("Home");
    fireEvent.click(await screen.findByRole("button", { name: "Account: operator" }));
    await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" })));
    await waitFor(() => expect(sawStorage).not.toBeNull());
    expect(sawStorage).toEqual({ drafts: null, writes: 0, returnTo: null });
    release();
    expect(takeReturnPath("/ui/")).toBeNull();
  });

  it("round 6 item 5: if B becomes the verified owner while A's sign-out waits, B's session is left alone", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    let deleteSeen = false;
    const { queryClient } = renderRoute("/home", {
      hub: {
        "DELETE /api/session": async () => {
          deleteSeen = true;
          await gate;
          return { status: 204 };
        },
      },
    });
    await findPageHeading("Home");
    fireEvent.click(await screen.findByRole("button", { name: "Account: operator" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
    await waitFor(() => expect(deleteSeen).toBe(true));
    const b = whoamiFixture({ agent: "B" });
    queryClient.setQueryData(WHOAMI_QUERY_KEY, b);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    await act(async () => {
      release();
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(queryClient.getQueryData(WHOAMI_QUERY_KEY)).toEqual(b);
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: WHOAMI_QUERY_KEY });
  });

  it("the sidebar's collapsed state is kept per owner, in owner-scoped storage", async () => {
    const first = renderRoute("/home");
    await findPageHeading("Home");
    fireEvent.click(screen.getAllByRole("button", { name: "Collapse sidebar" })[0]);
    expect(screen.getAllByRole("button", { name: "Expand sidebar" }).length).toBeGreaterThan(0);
    // Nothing under the old unscoped key; the owner's scoped key holds it.
    expect(localStorage.getItem("ua-cc2-sidebar")).toBeNull();
    expect(Object.keys(localStorage).some((k) => k.startsWith("ua-cc2:o:") && k.endsWith(":sidebar"))).toBe(true);
    first.unmount();
    // The same owner gets it back.
    const again = renderRoute("/home");
    await findPageHeading("Home");
    expect(screen.getAllByRole("button", { name: "Expand sidebar" }).length).toBeGreaterThan(0);
    // Another owner does not.
    act(() => void again.queryClient.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent: "B" })));
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Collapse sidebar" }).length).toBeGreaterThan(0));
    localStorage.clear();
  });

  it("the account menu offers the theme and no way back to the deleted classic app (spec §10)", async () => {
    renderRoute("/home");
    await findPageHeading("Home");
    fireEvent.click(screen.getByRole("button", { name: /Account/ }));
    expect(screen.getByRole("menuitemradio", { name: "Dark" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /classic/i })).not.toBeInTheDocument();
  });
});
