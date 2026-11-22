import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites, listParkedWrites, recordParkedWriteResult } from "../../../src/app/lib/parkedWrites";
import type { MockHandler } from "../support/fetchMock";
import { whoamiFixture } from "../support/fixtures";
import { contextFromWhoami } from "../../../src/app/lib/writeContext";
import { resetLaunchGuardCache } from "../../../src/app/sessions/launchGuard";
import { findPageHeading, renderRoute } from "../support/render";
import { AGENTS, CREW, eventsPayload, nodesPayload, sessionDetail, sessionRow, sessionsPayload } from "./fixtures";
import { fixtureEpoch } from "../support/epoch";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  resetLaunchGuardCache();
  clearParkedWrites(fixtureEpoch());
});
afterEach(() => vi.unstubAllGlobals());

function hub(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  return {
    "GET /api/sessions/s-live": { body: sessionDetail() },
    "GET /api/sessions": { body: sessionsPayload() },
    "GET /api/nodes": { body: nodesPayload() },
    "GET /api/events": { body: eventsPayload() },
    "GET /api/sessions/s-live/crew": { status: 404, body: { error: "unknown session" } },
    "GET /api/agents": { body: AGENTS },
    ...overrides,
  };
}

async function openSession(overrides: Record<string, MockHandler> = {}, path = "/sessions/s-live") {
  const result = renderRoute(path, { hub: hub(overrides) });
  await findPageHeading(`Session ${path.split("/").pop()}`);
  // The page is ready once the device item behind the session has loaded.
  await screen.findByText("evidence:item-1", undefined, { timeout: 10_000 });
  return result;
}

describe("/sessions/:id", () => {
  it("shows the conversation chat-style with tool activity collapsed", async () => {
    await openSession();
    const log = await screen.findByRole("list", { name: "Conversation, oldest first" });
    expect(within(log).getByRole("article", { name: /Message from the agent/ })).toHaveTextContent("Looking at the login form now.");
    expect(within(log).getByText("Fix the login form validation")).toBeInTheDocument();
    expect(within(log).getByText("Also add a test please")).toBeInTheDocument();
    const tool = within(log).getByText(/Tool: Read src\/login.tsx/).closest("details");
    expect(tool).not.toHaveAttribute("open");
    // The live state is visible (jsdom has no EventSource, so it says so).
    expect(screen.getByText(/Live updates are not available/)).toBeInTheDocument();
  });

  it("shows task, agent, host, cost, tokens, files and pull request in the side panel", async () => {
    await openSession();
    const panel = await screen.findByRole("complementary", { name: "About this session" });
    expect(within(panel).getByRole("link", { name: "#42 Fix the login form" })).toHaveAttribute("href", "/ui/work/tasks/42");
    expect(within(panel).getByRole("link", { name: "Desktop_Agent2" })).toHaveAttribute("href", "/ui/agents/profile/Desktop_Agent2");
    expect(within(panel).getByText("desktop")).toBeInTheDocument();
    expect(within(panel).getByText("$0.0421")).toBeInTheDocument();
    expect(within(panel).getByText("1,500 (1,200 in, 300 out)")).toBeInTheDocument();
    expect(await within(panel).findByText("modified: src/login.tsx")).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "github.com/acme/app/pull/7" })).toHaveAttribute("href", "https://github.com/acme/app/pull/7");
  });

  it("Ctrl/Cmd+Enter sends nothing while Send is unavailable (the same gate as the button)", async () => {
    const nodes = nodesPayload() as { nodes: { controls: Record<string, { capabilities: Record<string, { state: string; detail: string }> }> }[] };
    nodes.nodes[0].controls["acp:codex"].capabilities.queue = { state: "unsupported", detail: "no queue on this backend" };
    let posts = 0;
    await openSession({
      "GET /api/nodes": { body: nodes },
      "POST /api/action/node-followup": () => {
        posts += 1;
        return { status: 201, body: { seq: 3, mode: "queue", duplicate: false } };
      },
    });
    const box = await screen.findByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "Queue this please" } });
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    fireEvent.keyDown(box, { key: "Enter", metaKey: true });
    // Give a send that slipped through time to reach the hub.
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(posts).toBe(0);
    expect(box).toHaveValue("Queue this please");
  });

  it("Ctrl/Cmd+Enter sends through the gate when Send is available", async () => {
    let posts = 0;
    await openSession({
      "POST /api/action/node-followup": () => {
        posts += 1;
        return { status: 201, body: { seq: 3, mode: "queue", duplicate: false } };
      },
    });
    const box = await screen.findByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "Queue this please" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeEnabled());
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(posts).toBe(1));
  });

  it("keeps an unsent follow-up across navigation (defect 8) and sends it with one idempotency key", async () => {
    const sent: unknown[] = [];
    const first = await openSession({
      "POST /api/action/node-followup": ({ body }) => {
        sent.push(body);
        return { status: 201, body: { seq: 3, mode: "queue", duplicate: false } };
      },
    });
    const box = await screen.findByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "Please also check the mobile layout" } });
    // Debounced draft write, then leave the page.
    await waitFor(() => expect(JSON.stringify(sessionStorage)).toContain("mobile layout"));
    first.unmount();
    cleanup();

    await openSession({
      "POST /api/action/node-followup": ({ body }) => {
        sent.push(body);
        return { status: 201, body: { seq: 3, mode: "queue", duplicate: false } };
      },
    });
    const restored = await screen.findByRole("textbox", { name: "Message" });
    expect(restored).toHaveValue("Please also check the mobile layout");
    expect(screen.getByText("Your unsent message was restored.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({ item_id: "item-1", mode: "queue", body: "Please also check the mobile layout" });
    expect(String((sent[0] as { idempotency_key: string }).idempotency_key)).toMatch(/^fu-/);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(""));
  });

  it("a follow-up that needs approval says so and is followed, not resubmitted (defect 9)", async () => {
    await openSession({
      "POST /api/action/node-followup": { status: 202, body: { status: "approval_pending", approval_id: 77, code: "approval_required", tool: "node_dispatch" } },
      "GET /api/approvals/77": { body: { id: 77, tool: "node_dispatch", status: "pending", outcome: null } },
    });
    fireEvent.change(await screen.findByRole("textbox", { name: "Message" }), { target: { value: "steer left" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText(/Waiting for approval\./)).toBeInTheDocument();
    // Nothing runs by itself: the stored follow-up waits for a person's "Run it now" after approval, and the text is kept.
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("steer left");
    expect(screen.getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=77");
    const parked = listParkedWrites(fixtureEpoch());
    expect(parked).toHaveLength(1);
    expect(parked[0]).toMatchObject({ path: "/api/action/node-followup", approvalId: "77", body: { item_id: "item-1", mode: "queue", body: "steer left" } });
  });

  it("stop asks for confirmation, then cancels the device work", async () => {
    let cancelled: unknown = null;
    await openSession({
      "POST /api/action/node-session": ({ body }) => {
        cancelled = body;
        return { body: { cancel_requested: true } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog", { name: "Stop this session?" });
    expect(cancelled).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Stop session" }));
    await waitFor(() => expect(cancelled).toEqual({ action: "cancel", item_id: "item-1" }));
  });

  it("the operator ends another identity's session with a required reason (review 9, task/2480 contract)", async () => {
    const posts: unknown[] = [];
    await openSession({
      // Response shape from cc_work.session_operator_end on task/2480-cc2-hub-operations.
      "POST /api/action/session/operator-end": ({ body }) => {
        posts.push(body);
        return { body: { session_id: "s-live", owner: "Desktop_Agent2", status: "abandoned", ended_by: "operator", closed: true, event_ulid: "01OPEND", outstanding: { claimed_task_ids: [42], open_delegation_ids: [7], note: "the owner still holds these" } } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "End session" }));
    const dialog = await screen.findByRole("dialog", { name: "End this session?" });
    expect(within(dialog).getByText(/As the operator/)).toBeInTheDocument();
    // A reason is required by the hub; without one nothing is sent.
    expect(within(dialog).getByRole("button", { name: "End session" })).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Reason/ }), { target: { value: "Stuck for two hours" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "End session" }));
    await waitFor(() => expect(posts).toEqual([{ session_id: "s-live", reason: "Stuck for two hours", status: "abandoned" }]));
  });

  it("the operator-end confirmation says running work is not stopped, and the result lists what the owner still holds (review 2: 11)", async () => {
    await openSession({
      // task/2480 contract: the row is ended; process, claims and delegations are not touched.
      "POST /api/action/session/operator-end": {
        body: { session_id: "s-live", owner: "Desktop_Agent2", status: "abandoned", ended_by: "operator", closed: true, event_ulid: "01OPEND", outstanding: { claimed_task_ids: [42], open_delegation_ids: [7], note: "the owner still holds these" } },
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "End session" }));
    const dialog = await screen.findByRole("dialog", { name: "End this session?" });
    expect(within(dialog).getByText(/ends the session record and its hub credential/)).toBeInTheDocument();
    expect(within(dialog).getByText(/A running device job or process is not stopped/)).toBeInTheDocument();
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Reason/ }), { target: { value: "Stuck" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "End session" }));
    const held = await screen.findByRole("region", { name: "Still held by Desktop_Agent2" });
    expect(within(held).getByRole("link", { name: "Task #42" })).toHaveAttribute("href", "/ui/work/tasks/42");
    expect(within(held).getByText(/Delegation #7/)).toBeInTheDocument();
  });

  it("says plainly when this hub does not offer ending another identity's session yet", async () => {
    await openSession({
      "POST /api/action/session/operator-end": { status: 404, text: "Not Found" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "End session" }));
    const dialog = await screen.findByRole("dialog", { name: "End this session?" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Reason/ }), { target: { value: "Stuck" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "End session" }));
    expect(await screen.findByText(/Not available on this hub yet/)).toBeInTheDocument();
  });

  it("the caller ends its own session through session/end", async () => {
    const posts: unknown[] = [];
    await openSession({
      "GET /api/whoami": { body: whoamiFixture({ agent: "Desktop_Agent2" }) },
      "GET /api/sessions": { body: sessionsPayload(undefined, false) },
      // Response shape from docs/control-center-api.md (session_end adapter).
      "POST /api/action/session/end": ({ body }) => {
        posts.push(body);
        return { body: { session_id: "s-live", status: "completed", closed: true } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "End session" }));
    const own = await screen.findByRole("dialog", { name: "End this session?" });
    expect(within(own).getByText(/ends your session record/)).toBeInTheDocument();
    fireEvent.click(within(own).getByRole("button", { name: "End session" }));
    await waitFor(() => expect(posts).toEqual([{ session_id: "s-live", status: "completed" }]));
  });

  it("explains a disabled action instead of hiding it", async () => {
    await openSession({
      "GET /api/whoami": { body: whoamiFixture({ agent: "Mac_Hermes" }) },
      "GET /api/sessions": { body: sessionsPayload(undefined, false) },
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "End session" })).toBeDisabled());
    const reasons = screen.getByRole("list", { name: "Why some actions are unavailable" });
    expect(within(reasons).getByText(/Only Desktop_Agent2 or the operator can end this session/)).toBeInTheDocument();
    expect(within(reasons).getByText(/still open. Send a follow-up instead/)).toBeInTheDocument();
  });

  it("hands off to another agent with a note", async () => {
    let handoff: unknown = null;
    await openSession({
      "POST /api/action/handoff": ({ body }) => {
        handoff = body;
        return { body: { task_id: 99 } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Hand off" }));
    const dialog = await screen.findByRole("dialog", { name: "Hand off to another agent" });
    const agent = within(dialog).getByRole("combobox", { name: /Agent/ });
    fireEvent.change(agent, { target: { value: "Mac" } });
    fireEvent.mouseDown(await within(dialog).findByRole("option", { name: /Mac_Hermes/ }));
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Note for the agent/ }), { target: { value: "Finish the tests" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Hand off" }));
    await waitFor(() => expect(handoff).not.toBeNull());
    expect(handoff).toMatchObject({ to_agent: "Mac_Hermes", parent_task_id: 42 });
    expect((handoff as { task_spec: string }).task_spec).toMatch(/^Finish the tests\n\nHanded off from session s-live \(Desktop_Agent2\), task #42\./);
  });

  it("grants idle wakes for the operator, reading the wake state from the session list (review 10)", async () => {
    let grant: unknown = null;
    await openSession({
      // The detail has no wake state; the list read is where the hub reports it.
      "GET /api/sessions/s-live": { body: sessionDetail() },
      "GET /api/sessions": { body: sessionsPayload([sessionRow({ wake: { requested: true, granted: false } })]) },
      "POST /api/sessions/s-live/wake-grant": ({ body }) => {
        grant = body;
        return { body: { session_id: "s-live", granted: true, requested: true, changed: true } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Grant idle wakes" }));
    await waitFor(() => expect(grant).toEqual({ grant: true }));
  });


  it("opens the crew view when the session has one", async () => {
    await openSession({ "GET /api/sessions/s-live/crew": { body: CREW } });
    fireEvent.click(await screen.findByRole("button", { name: "Open crew view" }));
    const drawer = await screen.findByRole("dialog", { name: "Crew view" });
    expect(within(drawer).getByText("Review the form copy too.")).toBeInTheDocument();
  });

  it("resumes a finished device session with a message", async () => {
    let resumed: unknown = null;
    await openSession({
      "GET /api/sessions/s-live": { body: sessionDetail({ status: "completed", ended_at: "2026-11-10T11:59:00Z" }) },
      "GET /api/nodes": { body: nodesPayload({ status: "completed", resume: { available: true, mode: "acp_resume", label: "native resume", reason: null } }) },
      "POST /api/action/node-resume": ({ body }) => {
        resumed = body;
        return { status: 201, body: { item_id: "item-2", status: "queued" } };
      },
    });
    expect(await screen.findByText(/This session has ended, so it cannot take a follow-up/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    const dialog = await screen.findByRole("dialog", { name: "Resume this session" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Next message/ }), { target: { value: "Carry on" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(resumed).toEqual({ item_id: "item-1", prompt: "Carry on" }));
  });

  it("a crew message carries one message_id, reused when it is sent again (review 4)", async () => {
    const sent: { message_id?: string; body?: string }[] = [];
    let fail = true;
    renderRoute("/sessions/s-crew", {
      hub: hub({
        "GET /api/sessions/s-crew": { body: sessionDetail({ id: "s-crew", meta: {} }) },
        "GET /api/sessions/s-crew/crew": { body: CREW },
        "POST /api/action/crew-intervene": ({ body }) => {
          sent.push(body as { message_id?: string; body?: string });
          return fail ? { status: 502, text: "bad gateway" } : { body: { accepted: true, session_id: "s-crew", to_agent: "Desktop_Agent2" } };
        },
      }),
    });
    await findPageHeading("Session s-crew");
    const box = await screen.findByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "Please pause" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/The hub had a problem/);
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[0].message_id).toMatch(/^crew-/);
    expect(sent[1].message_id).toBe(sent[0].message_id);
    // A new message gets a new id.
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(""));
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Carry on" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(sent).toHaveLength(3));
    expect(sent[2].message_id).not.toBe(sent[0].message_id);
  });

  it("the crew drawer reuses its message_id across a failed send and keeps the text after closing", async () => {
    const sent: { message_id?: string }[] = [];
    let fail = true;
    await openSession({
      "GET /api/sessions/s-live/crew": { body: CREW },
      "POST /api/action/crew-intervene": ({ body }) => {
        sent.push(body as { message_id?: string });
        return fail ? { status: 503, text: "unavailable" } : { body: { accepted: true, session_id: "s-live", to_agent: "Desktop_Agent2" } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Open crew view" }));
    let drawer = await screen.findByRole("dialog", { name: "Crew view" });
    fireEvent.change(within(drawer).getByRole("textbox", { name: "Message to the crew" }), { target: { value: "Stop for review" } });
    fireEvent.click(within(drawer).getByRole("button", { name: "Send to crew" }));
    await within(drawer).findByText(/The hub had a problem/);
    fireEvent.click(within(drawer).getByRole("button", { name: /Close/ }));
    fireEvent.click(screen.getByRole("button", { name: "Open crew view" }));
    drawer = await screen.findByRole("dialog", { name: "Crew view" });
    expect(within(drawer).getByRole("textbox", { name: "Message to the crew" })).toHaveValue("Stop for review");
    fail = false;
    fireEvent.click(within(drawer).getByRole("button", { name: "Send to crew" }));
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[0].message_id).toMatch(/^crew-/);
    expect(sent[1].message_id).toBe(sent[0].message_id);
  });

  it("a follow-up whose answer was lost is sent again under the same key after navigating away (review 5)", async () => {
    const keys: string[] = [];
    let fail = true;
    const handlers = {
      "POST /api/action/node-followup": ({ body }: { body: unknown }) => {
        keys.push((body as { idempotency_key: string }).idempotency_key);
        if (fail) throw new TypeError("Failed to fetch");
        return { status: 201, body: { seq: 5, mode: "queue", duplicate: true } };
      },
    };
    const first = await openSession(handlers);
    fireEvent.change(await screen.findByRole("textbox", { name: "Message" }), { target: { value: "Rebase first" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/result could not be confirmed/);
    first.unmount();
    cleanup();
    fail = false;
    await openSession(handlers);
    expect(await screen.findByRole("textbox", { name: "Message" })).toHaveValue("Rebase first");
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[1]).toBe(keys[0]);
  });

  it("a parked follow-up run from the store (\"Run it now\") clears the draft once it succeeds", async () => {
    await openSession({
      "POST /api/action/node-followup": { status: 202, body: { status: "approval_pending", approval_id: 78, code: "approval_required", tool: "node_dispatch" } },
      "GET /api/approvals/78": { body: { id: 78, tool: "node_dispatch", status: "pending", outcome: null } },
    });
    fireEvent.change(await screen.findByRole("textbox", { name: "Message" }), { target: { value: "Wait for CI" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/Waiting for approval\./);
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Wait for CI");
    act(() => recordParkedWriteResult(fixtureEpoch(), { approvalId: "78", kind: "done", message: "Your follow-up: approved and done.", at: Date.now(), owner: contextFromWhoami(whoamiFixture()) }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(""));
  });

  it("text typed while a follow-up is sending survives that send's success (review 13)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    await openSession({ "POST /api/action/node-followup": { status: 201, body: { seq: 6, mode: "queue", duplicate: false } } });
    const real = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/api/action/node-followup")) await gate;
      return real(input, init);
    });
    const box = await screen.findByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "First" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Sending…");
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Second, typed while sending" } });
    release();
    await waitFor(() => expect(screen.queryByText("Sending…")).toBeNull());
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Second, typed while sending");
  });

  it("a handoff note cannot be typed before the identity is known, and is stored once it is (review 14)", async () => {
    let releaseWhoami: () => void = () => {};
    const whoamiGate = new Promise<void>((r) => (releaseWhoami = r));
    renderRoute("/sessions/s-live", { hub: hub() });
    const real = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/api/whoami")) await whoamiGate;
      return real(input, init);
    });
    // The v2 shell mounts no page until whoami answers (owner epoch), so no text
    // can be typed under an identity that is not known yet.
    expect(await screen.findByText("Checking who is signed in…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Hand off" })).toBeNull();
    releaseWhoami();
    await findPageHeading("Session s-live");
    fireEvent.click(await screen.findByRole("button", { name: "Hand off" }));
    const dialog = await screen.findByRole("dialog", { name: "Hand off to another agent" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Note for the agent/ }), { target: { value: "Typed early" } });
    await waitFor(() => expect(JSON.stringify(sessionStorage)).toContain("Typed early"));
    expect(within(dialog).getByRole("textbox", { name: /Note for the agent/ })).toHaveValue("Typed early");
  });

  it("\"Start again\" on the session page goes through the launch guard (review 2: A)", async () => {
    let key: string | undefined;
    await openSession({
      "GET /api/sessions/s-live": { body: sessionDetail({ status: "failed", ended_at: "2026-11-10T11:59:00Z" }) },
      "GET /api/nodes": { body: nodesPayload({ status: "failed", restartable: true }) },
      "POST /api/action/node-session": ({ headers }) => {
        key = headers["idempotency-key"];
        return { status: 201, body: { item_id: "item-2", node: "desktop", status: "queued" } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Start again" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Start this session again?" })).getByRole("button", { name: "Start again" }));
    await waitFor(() => expect(key).toMatch(/^launch-/));
  });

  it("a follow-up's attempt is stored before the POST, not only with the debounced draft (review 2: 5)", async () => {
    let storedAtPost = "";
    let key = "";
    await openSession({
      "POST /api/action/node-followup": ({ body }) => {
        key = (body as { idempotency_key: string }).idempotency_key;
        storedAtPost = JSON.stringify({ ...sessionStorage });
        throw new TypeError("Failed to fetch");
      },
    });
    fireEvent.change(await screen.findByRole("textbox", { name: "Message" }), { target: { value: "Ship it" } });
    // The text is in the draft already; the send happens at once after.
    await waitFor(() => expect(JSON.stringify({ ...sessionStorage })).toContain("Ship it"));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(key).not.toBe(""));
    expect(storedAtPost).toContain(key);
  });

  it("a crew message's attempt is stored before the POST (review 2: 5)", async () => {
    let storedAtPost = "";
    let key = "";
    renderRoute("/sessions/s-crew", {
      hub: hub({
        "GET /api/sessions/s-crew": { body: sessionDetail({ id: "s-crew", meta: {} }) },
        "GET /api/sessions/s-crew/crew": { body: CREW },
        "POST /api/action/crew-intervene": ({ body }) => {
          key = (body as { message_id: string }).message_id;
          storedAtPost = JSON.stringify({ ...sessionStorage });
          return { body: { accepted: true, session_id: "s-crew", to_agent: "Desktop_Agent2" } };
        },
      }),
    });
    await findPageHeading("Session s-crew");
    fireEvent.change(await screen.findByRole("textbox", { name: "Message" }), { target: { value: "Pause" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(key).not.toBe(""));
    expect(storedAtPost).toContain(key);
  });

  it("hand off and resume keep text edited while sending, and stay open (review 2: 10)", async () => {
    let release: () => void = () => {};
    let gate = new Promise<void>((r) => (release = r));
    await openSession({
      "GET /api/sessions/s-live": { body: sessionDetail({ status: "completed", ended_at: "2026-11-10T11:59:00Z" }) },
      "GET /api/nodes": { body: nodesPayload({ status: "completed", resume: { available: true, mode: "acp_resume", label: "native resume", reason: null } }) },
      "POST /api/action/handoff": { body: { task_id: 99 } },
      "POST /api/action/node-resume": { status: 201, body: { item_id: "item-2", status: "queued" } },
    });
    const real = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      if (/\/api\/action\/(handoff|node-resume)/.test(String(input))) await gate;
      return real(input, init);
    });
    // Hand off
    fireEvent.click(await screen.findByRole("button", { name: "Hand off" }));
    let dialog = await screen.findByRole("dialog", { name: "Hand off to another agent" });
    fireEvent.change(within(dialog).getByRole("combobox", { name: /Agent/ }), { target: { value: "Mac" } });
    fireEvent.mouseDown(await within(dialog).findByRole("option", { name: /Mac_Hermes/ }));
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Note for the agent/ }), { target: { value: "First note" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Hand off" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Hand off" })).toHaveAttribute("aria-busy", "true"));
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Note for the agent/ }), { target: { value: "A second note" } });
    release();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Hand off" })).not.toHaveAttribute("aria-busy"));
    expect(screen.getByRole("dialog", { name: "Hand off to another agent" })).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: /Note for the agent/ })).toHaveValue("A second note");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    // Resume
    gate = new Promise<void>((r) => (release = r));
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    dialog = await screen.findByRole("dialog", { name: "Resume this session" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Next message/ }), { target: { value: "Go on" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Resume" })).toHaveAttribute("aria-busy", "true"));
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Next message/ }), { target: { value: "Go on, then stop" } });
    release();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Resume" })).not.toHaveAttribute("aria-busy"));
    expect(screen.getByRole("dialog", { name: "Resume this session" })).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: /Next message/ })).toHaveValue("Go on, then stop");
  }, 40_000);

  it("says when the session does not exist", async () => {
    renderRoute("/sessions/nope", { hub: hub({ "GET /api/sessions/nope": { status: 404, body: { error: "unknown session" } } }) });
    await findPageHeading("Session nope");
    expect(await screen.findByRole("heading", { name: "No session with this id" })).toBeInTheDocument();
  });

  it("keeps the page usable when the history read fails, without raw errors (defect 10)", async () => {
    await openSession({ "GET /api/events": { status: 502, text: "upstream connect error" } });
    const heading = await screen.findByRole("heading", { name: "Earlier messages could not be loaded" });
    const region = heading.closest("section") as HTMLElement;
    expect(within(region).getByText(/New messages still appear below/)).toBeVisible();
    expect(within(region).getByText(/upstream connect error/).closest("details")).not.toHaveAttribute("open");
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
  });
});
