import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import { contextFromWhoami } from "../../../src/app/lib/writeContext";
import { guardStatus, guardStorageKey, nextRecord, resetLaunchGuardCache, sameLaunchRequest, type LaunchRecord } from "../../../src/app/sessions/launchGuard";
import type { MockHandler, MockReply } from "../support/fetchMock";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { AGENTS, nodeItem, nodesPayload, OVERVIEW, ROUTES, sessionRow, sessionsPayload } from "./fixtures";
import { fixtureEpoch } from "../support/epoch";

/**
 * The launch guard (review round 2: A-E). `GET /api/launch-attempts/{key}`
 * is hub task #2501; these tests cover a hub with the route and one without.
 */
const STORE = guardStorageKey(contextFromWhoami(whoamiFixture()));
const RECEIPTS_OK = { body: { receipts: [], count: 0, terminal_count: 0 } };

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  clearParkedWrites(fixtureEpoch());
  resetLaunchGuardCache();
});
afterEach(() => vi.unstubAllGlobals());

/** The record as the signed-in owner's shared storage holds it. */
function stored(): LaunchRecord | null {
  return fixtureEpoch().sharedStorage.read<LaunchRecord | null>(STORE, null);
}

/** Another tab (or an earlier page load) of the same owner left this record. */
function leave(record: LaunchRecord): void {
  expect(fixtureEpoch().sharedStorage.write(STORE, record)).toBe(true);
}

function hub(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  return {
    "GET /api/overview": { body: OVERVIEW },
    "GET /api/dispatch-routes": { body: ROUTES },
    "GET /api/nodes": { body: nodesPayload() },
    "GET /api/agents": { body: AGENTS },
    "GET /api/sessions": { body: sessionsPayload([]) },
    "GET /api/dispatch_receipts": RECEIPTS_OK,
    ...overrides,
  };
}

/** Answer `GET /api/launch-attempts/{key}` (a dynamic path the table mock cannot key). */
function attemptRoute(reply: (key: string) => MockReply) {
  const real = globalThis.fetch;
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const m = url.match(/\/api\/launch-attempts\/([^?]+)/);
    if (m) {
      const key = decodeURIComponent(m[1]);
      calls.push(key);
      const r = reply(key);
      return new Response(r.text ?? JSON.stringify(r.body ?? {}), { status: r.status ?? 200, headers: { "Content-Type": "application/json" } });
    }
    return real(input, init);
  });
  return calls;
}

async function openForm(overrides: Record<string, MockHandler> = {}) {
  const result = renderRoute("/sessions/new", { hub: hub(overrides) });
  await findPageHeading("Start a session");
  await screen.findByRole("button", { name: /Start (session|the next ready task)/ }, { timeout: 10_000 });
  return result;
}

async function launchDispatch() {
  const agent = screen.getByRole("combobox", { name: /^Agent/ });
  fireEvent.change(agent, { target: { value: "Desktop_" } });
  fireEvent.mouseDown(await screen.findByRole("option", { name: /^Desktop_Agent2/ }));
  fireEvent.change(screen.getByRole("textbox", { name: /Instructions/ }), { target: { value: "Tidy the README" } });
  fireEvent.change(screen.getByRole("combobox", { name: /Project/ }), { target: { value: "unified-agent" } });
  fireEvent.click(screen.getByRole("button", { name: "Start session" }));
}

const lost = () => {
  throw new TypeError("Failed to fetch");
};

describe("launch guard", () => {
  it("stores the attempt before the POST and sends its id as Idempotency-Key", async () => {
    let seen: { header?: string; record: LaunchRecord | null } | null = null;
    await openForm({
      "POST /api/action/dispatch": ({ headers }) => {
        seen = { header: headers["idempotency-key"], record: stored() };
        return { status: 202, body: { status: "running", task_id: 9, spawned: true } };
      },
    });
    await launchDispatch();
    await waitFor(() => expect(seen).not.toBeNull());
    expect(seen!.record).toMatchObject({ op: "dispatch", target: "Desktop_Agent2", state: "sending" });
    expect(seen!.header).toBe(seen!.record!.key);
    // No prompt text in the shared record.
    expect(JSON.stringify(seen!.record)).not.toContain("Tidy the README");
    await screen.findByText("The session has started.");
    expect(stored()).toBeNull();
  });

  it("route present: the hub's 'started' resolves it and links to the session", async () => {
    await openForm({ "POST /api/action/dispatch": lost });
    const lookups = attemptRoute((key) => ({ body: { key, state: "started", session_id: "s-9" } }));
    await launchDispatch();
    expect(await screen.findByText(/The hub says the last launch started/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open it" })).toHaveAttribute("href", "/ui/sessions/s-9");
    expect(lookups).toHaveLength(1);
    expect(stored()).toBeNull();
    expect(screen.getByRole("button", { name: "Start session" })).toBeEnabled();
  });

  it("route present: 'unknown' (the hub never saw it) allows a new launch", async () => {
    await openForm({ "POST /api/action/dispatch": lost });
    attemptRoute((key) => ({ body: { key, state: "unknown" } }));
    await launchDispatch();
    expect(await screen.findByText(/The hub never received it/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start session" })).toBeEnabled();
  });

  it("route present: 'in_progress' keeps further launches blocked", async () => {
    await openForm({ "POST /api/action/dispatch": lost });
    attemptRoute((key) => ({ body: { key, state: "in_progress" } }));
    await launchDispatch();
    expect(await screen.findByText(/The hub is still starting it/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
  });

  it("route absent: possible matches are candidates only, never success, and never enable a relaunch by themselves", async () => {
    const posts: string[] = [];
    await openForm({
      "POST /api/action/dispatch": ({ headers }) => {
        posts.push(headers["idempotency-key"] ?? "");
        if (posts.length === 1) throw new TypeError("Failed to fetch");
        return { status: 202, body: { status: "running", task_id: 10, spawned: true } };
      },
      "GET /api/sessions": () => ({ body: sessionsPayload([sessionRow({ id: "s-new", agent: "Desktop_Agent2", started_at: new Date().toISOString(), meta: {} })]) }),
    });
    attemptRoute(() => ({ status: 404, text: "Not Found" }));
    await launchDispatch();
    const panel = await screen.findByRole("region", { name: "Did the last launch start?" });
    expect(await within(panel).findByText("We could not confirm whether this started.")).toBeInTheDocument();
    const group = await within(panel).findByRole("group", { name: "These started around the same time and may be it" });
    expect(within(group).getByText(/Desktop_Agent2 session s-new/)).toBeInTheDocument();
    expect(within(panel).getByText(/Compared: sessions, device jobs and dispatch runs of Desktop_Agent2 that started between/)).toBeInTheDocument();
    expect(screen.queryByText(/says the last launch started/)).toBeNull();
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
    // "One of these is it" needs an explicit pick.
    expect(within(panel).getByRole("button", { name: "One of these is it" })).toBeDisabled();
    // Relaunch is explicit and confirmed, and reuses the attempt id.
    fireEvent.click(within(panel).getByRole("button", { name: "I have checked and it did not start. Launch again" }));
    const confirm = await screen.findByRole("dialog", { name: "Launch it again?" });
    expect(within(confirm).getByText(/a duplicate session may be created/)).toBeInTheDocument();
    fireEvent.click(within(confirm).getByRole("button", { name: "Launch again" }));
    fireEvent.click(await screen.findByRole("button", { name: "Start session" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toBe(posts[0]);
  });

  it("route absent: picking a candidate resolves it as the person's choice", async () => {
    await openForm({
      "POST /api/action/dispatch": lost,
      "GET /api/sessions": () => ({ body: sessionsPayload([sessionRow({ id: "s-new", agent: "Desktop_Agent2", started_at: new Date().toISOString(), meta: {} })]) }),
    });
    attemptRoute(() => ({ status: 404, text: "Not Found" }));
    await launchDispatch();
    const group = await screen.findByRole("group", { name: "These started around the same time and may be it" });
    fireEvent.click(within(group).getByRole("radio"));
    fireEvent.click(screen.getByRole("button", { name: "One of these is it" }));
    expect(await screen.findByText(/You marked the last launch as started/)).toBeInTheDocument();
    expect(stored()).toBeNull();
  });

  it("route absent: failed, partial or capped reads are shown as 'could not check', with Retry", async () => {
    const capped = { ...nodesPayload(), recent_items: Array.from({ length: 25 }, (_, i) => nodeItem({ id: `old-${i}`, created_at: "2026-11-01T00:00:00Z" })) };
    await openForm({
      "POST /api/action/dispatch": lost,
      "GET /api/nodes": { body: capped },
      // dispatch_receipts names its failure in a 200 body.
      "GET /api/dispatch_receipts": { body: { receipts: [], error: "dispatch delivery receipts unavailable", code: "unavailable" } },
    });
    attemptRoute(() => ({ status: 404, text: "Not Found" }));
    await launchDispatch();
    const alert = await screen.findByText(/Could not check everything/);
    expect(alert).toHaveTextContent(/only its latest 25 device jobs/);
    expect(alert).toHaveTextContent(/dispatch runs were not available/);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
    // The explicit way out is always there.
    expect(screen.getByRole("button", { name: "I have checked and it did not start. Launch again" })).toBeInTheDocument();
  });

  it("an attempt left 'sending' by a closed tab or a reload is unresolved when the form opens", async () => {
    leave({ key: "launch-old", op: "device", target: "desktop", taskIds: [], label: "Desktop_Agent2 on desktop", sentAt: Date.now() - 5 * 60_000, state: "sending", tab: "tab-gone" });
    await openForm();
    attemptRoute(() => ({ status: 404, text: "Not Found" }));
    expect(await screen.findByRole("region", { name: "Did the last launch start?" })).toHaveTextContent("Desktop_Agent2 on desktop");
    expect(screen.getByRole("button", { name: /^Start (session|the next ready task)$/ })).toBeDisabled();
  });

  it("a launch being sent from another tab blocks this one until that tab settles it", async () => {
    leave({ key: "launch-x", op: "dispatch", target: "Desktop_Agent2", taskIds: [], label: "Desktop_Agent2", sentAt: Date.now(), state: "sending", tab: "tab-other" });
    await openForm();
    expect(await screen.findByText(/being sent from another tab/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start/ })).toBeDisabled();
    // The other tab settles it: storage changes and this tab is told.
    fixtureEpoch().sharedStorage.remove(STORE);
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: STORE, newValue: null }));
    });
    await waitFor(() => expect(screen.getByRole("button", { name: /Start/ })).toBeEnabled());
  });

  // Task #2510: the key stays until the launch is settled.
  it("a plain 400 keeps the key: a failed attempt with launch evidence is continued with the same key", async () => {
    const posts: string[] = [];
    await openForm({
      "POST /api/action/dispatch": ({ headers }) => {
        posts.push(headers["idempotency-key"] ?? "");
        return posts.length === 1 ? { status: 400, body: { error: "the agent refused the task" } } : { status: 202, body: { status: "running", task_id: 9, spawned: true } };
      },
    });
    attemptRoute((key) => ({ body: { key, state: "failed", delegation_id: 4, task_id: 9, reason: "the agent refused the task" } }));
    await launchDispatch();
    const panel = await screen.findByRole("region", { name: "Did the last launch start?" });
    expect(await within(panel).findByText(/The hub says it failed: the agent refused the task/)).toBeInTheDocument();
    // Not "not started": the hub already created a task for it.
    expect(screen.queryByText(/You can launch again/)).toBeNull();
    expect(stored()).toMatchObject({ key: posts[0], state: "unresolved" });
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
    // "Launch it again" keeps the key (stored, not only in this page's memory).
    fireEvent.click(within(panel).getByRole("button", { name: "Launch it again" }));
    await waitFor(() => expect(stored()).toMatchObject({ key: posts[0], state: "armed" }));
    fireEvent.click(await screen.findByRole("button", { name: "Start session" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toBe(posts[0]);
    await screen.findByText("The session has started.");
    expect(stored()).toBeNull();
  });

  it("a failed attempt with no recorded step is the hub's answer: nothing was created, a new launch may start", async () => {
    await openForm({ "POST /api/action/dispatch": { status: 400, body: { error: "no route for Desktop_Agent2" } } });
    attemptRoute((key) => ({ body: { key, state: "failed", reason: "no route for Desktop_Agent2" } }));
    await launchDispatch();
    expect(await screen.findByText(/The hub says it failed: no route for Desktop_Agent2/)).toBeInTheDocument();
    await waitFor(() => expect(stored()).toBeNull());
    expect(screen.getByRole("button", { name: "Start session" })).toBeEnabled();
  });

  it("a lookup that fails (network or 5xx) keeps the key; only an explicit 'start something new' gives a new one", async () => {
    const posts: string[] = [];
    await openForm({
      "POST /api/action/dispatch": ({ headers }) => {
        posts.push(headers["idempotency-key"] ?? "");
        return { status: 400, body: { error: "refused" } };
      },
    });
    let lookups = 0;
    attemptRoute(() => {
      lookups += 1;
      if (lookups === 1) throw new TypeError("Failed to fetch");
      return { status: 502, text: "bad gateway" };
    });
    await launchDispatch();
    expect(await screen.findByText(/Could not ask the hub about it/)).toBeInTheDocument();
    expect(stored()).toMatchObject({ key: posts[0] });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(lookups).toBe(2));
    expect(await screen.findByText(/Could not ask the hub about it/)).toBeInTheDocument();
    expect(stored()).toMatchObject({ key: posts[0] });
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
    // The explicit way out: confirmed, and only then a new key.
    fireEvent.click(screen.getByRole("button", { name: "Start something new" }));
    const confirm = await screen.findByRole("dialog", { name: "Start something new?" });
    expect(stored()).toMatchObject({ key: posts[0] });
    fireEvent.click(within(confirm).getByRole("button", { name: "Start something new" }));
    await waitFor(() => expect(stored()).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).not.toBe(posts[0]);
  });

  it("a reload keeps the key and the request fingerprint of a launch the person chose to send again", async () => {
    const posts: string[] = [];
    const first = await openForm({
      "POST /api/action/dispatch": ({ headers }) => {
        posts.push(headers["idempotency-key"] ?? "");
        return posts.length === 1 ? lost() : { status: 202, body: { status: "running", task_id: 9, spawned: true } };
      },
    });
    attemptRoute((key) => ({ body: { key, state: "failed", run_id: "r-1", reason: "the launch was interrupted before it started; repeat it with the same key to continue" } }));
    await launchDispatch();
    fireEvent.click(await screen.findByRole("button", { name: "Launch it again" }));
    await waitFor(() => expect(stored()).toMatchObject({ key: posts[0], state: "armed", fingerprint: expect.any(String) }));
    const before = stored()!;
    // A reload: a new page load with the same storage.
    first.unmount();
    resetLaunchGuardCache();
    await openForm({
      "POST /api/action/dispatch": ({ headers }) => {
        posts.push(headers["idempotency-key"] ?? "");
        return { status: 202, body: { status: "running", task_id: 9, spawned: true } };
      },
    });
    expect(await screen.findByText(/Ready to send the last launch again/)).toBeInTheDocument();
    expect(stored()).toEqual(before);
    fireEvent.click(await screen.findByRole("button", { name: "Start session" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toBe(posts[0]);
  });

  it("a key ready to be sent again is never sent with a different request", async () => {
    const posts: string[] = [];
    await openForm({
      "POST /api/action/dispatch": ({ headers }) => {
        posts.push(headers["idempotency-key"] ?? "");
        return { status: 400, body: { error: "refused" } };
      },
    });
    attemptRoute((key) => ({ body: { key, state: "failed", delegation_id: 4, reason: "refused" } }));
    await launchDispatch();
    fireEvent.click(await screen.findByRole("button", { name: "Launch it again" }));
    await waitFor(() => expect(stored()).toMatchObject({ state: "armed" }));
    const prompt = screen.getByRole("textbox", { name: /Instructions/ });
    fireEvent.change(prompt, { target: { value: "Tidy the README and the CHANGELOG" } });
    expect(await screen.findByText(/no longer matches that launch/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
    fireEvent.change(prompt, { target: { value: "Tidy the README" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Start session" })).toBeEnabled());
    expect(posts).toHaveLength(1);
  });

  it("the hub's 'unsettled' keeps the launch blocked: its start may still exist", async () => {
    await openForm({ "POST /api/action/dispatch": lost });
    attemptRoute((key) => ({ body: { key, state: "unsettled", run_id: "r-7" } }));
    await launchDispatch();
    expect(await screen.findByText(/The hub cannot tell yet whether it started/)).toBeInTheDocument();
    expect(stored()).not.toBeNull();
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
  });

  it("a team restore's 'unknown' proves nothing (the hub does not record restore attempts): the person decides", async () => {
    leave({ key: "launch-r", op: "restore", target: "snap-1", taskIds: [], label: "Restore team", sentAt: Date.now() - 600_000, state: "unresolved", tab: "tab-gone" });
    await openForm({ "GET /api/launch-attempts/launch-r": { body: { key: "launch-r", state: "unknown" } } });
    expect(await screen.findByText("We could not confirm whether this started.")).toBeInTheDocument();
    expect(screen.queryByText(/The hub never received it/)).toBeNull();
    expect(stored()).toMatchObject({ key: "launch-r" });
  });

  it("sign-out drops the record", async () => {
    leave({ key: "launch-y", op: "dispatch", target: "Desktop_Agent2", taskIds: [], label: "Desktop_Agent2", sentAt: Date.now() - 600_000, state: "unresolved", tab: "tab-gone" });
    await openForm({ "DELETE /api/session": { status: 204 } });
    attemptRoute(() => ({ status: 404, text: "Not Found" }));
    await screen.findByRole("region", { name: "Did the last launch start?" });
    // The shell's sign-out drops the record under the epoch it started in.
    fireEvent.click(await screen.findByRole("button", { name: "Account: operator" }));
    await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" })));
    await waitFor(() => expect(stored()).toBeNull());
  });
});

/**
 * Task #2532: a record stored before task #2510 has no request fingerprint,
 * so nothing says which request its key was bound to. Its key is never sent
 * again: the hub would answer 409 for a different request and the person
 * would be stuck. It is settled by the hub's lookup or by an explicit
 * "start something new".
 */
describe("a launch record stored before #2510 (no fingerprint)", () => {
  const legacy = (state: LaunchRecord["state"]): LaunchRecord => ({
    key: "launch-legacy",
    op: "dispatch",
    target: "Desktop_Agent2",
    taskIds: [],
    label: "Desktop_Agent2",
    sentAt: Date.now() - 600_000,
    state,
    tab: "tab-gone",
  });

  /** Every dispatch POST, with its key and body. */
  function recordPosts(posts: { key: string; body: unknown }[]): MockHandler {
    return ({ headers, body }) => {
      posts.push({ key: headers["idempotency-key"] ?? "", body });
      return { status: 202, body: { status: "running", task_id: 9, spawned: true } };
    };
  }

  it("is never armable: no 'Launch it again'; the lookup and 'Start something new' settle it", async () => {
    leave(legacy("unresolved"));
    const posts: { key: string; body: unknown }[] = [];
    await openForm({
      "POST /api/action/dispatch": recordPosts(posts),
      "GET /api/launch-attempts/launch-legacy": { body: { key: "launch-legacy", state: "failed", delegation_id: 4, task_id: 9, reason: "refused" } },
    });
    const panel = await screen.findByRole("region", { name: "Did the last launch start?" });
    expect(await within(panel).findByText(/The hub says it failed: refused/)).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Launch it again" })).toBeNull();
    expect(within(panel).getByText(/cannot be sent again under its attempt id/)).toBeInTheDocument();
    // A new request cannot ride on the old key.
    await launchDispatch();
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
    expect(posts).toEqual([]);
    expect(stored()).toMatchObject({ key: "launch-legacy", state: "unresolved" });
    // The way out: an explicit, confirmed "start something new", then a new key.
    fireEvent.click(within(panel).getByRole("button", { name: "Start something new" }));
    const confirm = await screen.findByRole("dialog", { name: "Start something new?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Start something new" }));
    await waitFor(() => expect(stored()).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].key).not.toBe("launch-legacy");
  });

  it("a legacy record already armed (armed before a reload) never sends its key with the form's request", async () => {
    leave(legacy("armed"));
    const posts: { key: string; body: unknown }[] = [];
    await openForm({
      "POST /api/action/dispatch": recordPosts(posts),
      "GET /api/launch-attempts/launch-legacy": { body: { key: "launch-legacy", state: "failed", delegation_id: 4, reason: "refused" } },
    });
    await launchDispatch();
    // Give a send the time to leave, then check none carried the old key.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(posts.map((p) => p.key)).not.toContain("launch-legacy");
    expect(await screen.findByRole("region", { name: "Did the last launch start?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
    // Nothing adopted the form's fingerprint onto the old key.
    expect(stored()?.fingerprint).toBeUndefined();
  });

  it("route absent: the person may mark a match or start something new, never 'Launch again' under the old key", async () => {
    leave(legacy("unresolved"));
    await openForm({ "GET /api/launch-attempts/launch-legacy": { status: 404, text: "Not Found" } });
    expect(await screen.findByText("We could not confirm whether this started.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Launch again/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Start something new" })).toBeInTheDocument();
  });

  it("the guard functions treat it as matching no request", () => {
    const fp = "fp-0123456789abcdef";
    expect(sameLaunchRequest(legacy("armed"), "dispatch", fp)).toBe(false);
    expect(sameLaunchRequest({ ...legacy("armed"), fingerprint: fp }, "dispatch", fp)).toBe(true);
    expect(guardStatus(legacy("armed"), Date.now()).kind).toBe("unresolved");
  });
});

describe("nextRecord: how a launch request ended", () => {
  const base: LaunchRecord = { key: "launch-1", op: "dispatch", target: "Desktop_Agent2", taskIds: [], label: "Desktop_Agent2", sentAt: 0, state: "sending", tab: "t" };
  it.each([
    ["started", { kind: "accepted" as const }, null],
    // #2510: a refusal of a keyed launch may come after the hub created a task; the hub's answer settles it.
    ["refused (400)", { kind: "error" as const, status: 400 }, "unresolved"],
    ["the hub could not be reached, nothing was sent", { kind: "error" as const, status: null }, null],
    ["the hub says the spawn is unsettled", { kind: "error" as const, status: 409, code: "launch_unsettled" }, "unresolved"],
    ["sent, answer lost", { kind: "error" as const, status: null, outcomeUnknown: true }, "unresolved"],
    ["hub failed part way", { kind: "error" as const, status: 502 }, "unresolved"],
    ["sign-out began meanwhile", { kind: "error" as const, status: null, signedOut: true }, null],
    ["cancelled before sending", { kind: "error" as const, status: null, aborted: true }, null],
    ["sign-in could not be verified, not sent", { kind: "error" as const, status: null, contextUnverified: true }, null],
    ["parked", { kind: "pending_approval" as const, approvalId: "9" }, "parked"],
    // hub #2501: a repeat while the first attempt runs answers 409 launch_in_progress; it may well start.
    ["the hub says this attempt is already in progress", { kind: "error" as const, status: 409, detail: "this launch is already in progress; look it up or retry shortly" }, "unresolved"],
    // Foundation contract: `cancelled` after the request left may have been applied; before, nothing was sent.
    ["cancelled after it was sent (sign-in changed)", { kind: "cancelled" as const, dispatched: true }, "unresolved"],
    ["cancelled before it was sent", { kind: "cancelled" as const, dispatched: false }, null],
    ["an ordinary conflict (no eligible task)", { kind: "error" as const, status: 409, detail: "dispatch stack has no eligible task" }, "unresolved"],
  ])("%s", (_name, outcome, state) => {
    const next = nextRecord(base, outcome);
    expect(next?.state ?? null).toBe(state);
    if (next) expect(next.key).toBe(base.key);
  });

  // A key that was sent before (the person chose to send it again): a send that never left keeps it ready, never drops it.
  const resent: LaunchRecord = { ...base, sentBefore: true, fingerprint: "fp-1" };
  it.each([
    ["cancelled before sending", { kind: "error" as const, status: null, aborted: true }, "armed"],
    ["sign-in could not be verified, not sent", { kind: "error" as const, status: null, contextUnverified: true }, "armed"],
    ["the hub could not be reached, nothing was sent", { kind: "error" as const, status: null }, "armed"],
    ["cancelled before it was sent", { kind: "cancelled" as const, dispatched: false }, "armed"],
    ["started", { kind: "accepted" as const }, null],
    ["sign-out began meanwhile", { kind: "error" as const, status: null, signedOut: true }, null],
  ])("sent before, then %s", (_name, outcome, state) => {
    const next = nextRecord(resent, outcome);
    expect(next?.state ?? null).toBe(state);
    if (next) expect(next).toMatchObject({ key: "launch-1", fingerprint: "fp-1" });
  });

  it("a refused team restore (the hub keeps no attempt for it) clears as before", () => {
    expect(nextRecord({ ...base, op: "restore" }, { kind: "error", status: 400 })).toBeNull();
  });
});
