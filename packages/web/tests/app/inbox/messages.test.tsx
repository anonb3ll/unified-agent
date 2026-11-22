import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { listParkedWrites } from "../../../src/app/lib/parkedWrites";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { agentsPayload, fullHub } from "./fixtures";
import { vitestHub } from "./support";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

function render(path: string, extra: Parameters<typeof vitestHub>[1] = {}) {
  return renderRoute(path, { hub: vitestHub(fullHub(), extra) });
}

async function writeMessage(to: string, text: string) {
  const form = await screen.findByRole("form", { name: "New message" }, { timeout: 8000 });
  await waitFor(() => expect(within(form).getByRole("option", { name: to })).toBeInTheDocument());
  fireEvent.change(within(form).getByLabelText(/^To/), { target: { value: to } });
  fireEvent.change(within(form).getByLabelText(/^Message/), { target: { value: text } });
  fireEvent.click(within(form).getByRole("button", { name: "Send" }));
}

describe("Messages", () => {
  it("lists conversations and opens one from the URL; agent text is plain text", async () => {
    const { router } = render("/inbox/messages");
    await findPageHeading("Messages");
    const list = await screen.findByRole("list", { name: "Conversations" }, { timeout: 8000 });
    fireEvent.click(within(list).getByRole("link", { name: /Desktop_Agent2 and operator/ }));
    await waitFor(() => expect(router.state.location.search).toBe("?conversation=conv-1"));
    const thread = await screen.findByRole("list", { name: "Messages in this conversation" });
    expect(thread).toHaveTextContent("Should I ship <b>now</b>?");
    expect(thread.querySelector("b")).toBeNull();
  });

  it("?to= opens a new message to that agent, with its own draft (#2523)", async () => {
    const { hub } = render("/inbox/messages?participant=Desktop_Agent5&compose=1&to=Desktop_Agent5", {
      "POST /api/messages": { body: { accepted: true, message_id: "m", conversation_id: "conv-9", target_live: true } },
    });
    const form = await screen.findByRole("form", { name: "New message" }, { timeout: 8000 });
    await waitFor(() => expect(within(form).getByLabelText(/^To/)).toHaveValue("Desktop_Agent5"));
    expect(screen.getByLabelText(/^With/)).toHaveValue("Desktop_Agent5");
    fireEvent.change(within(form).getByLabelText(/^Message/), { target: { value: "Your seat moved." } });
    fireEvent.click(within(form).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(hub.calls.some((c) => c.method === "POST" && c.path === "/api/messages")).toBe(true));
    expect(hub.calls.find((c) => c.path === "/api/messages")?.body).toMatchObject({ to_agent: "Desktop_Agent5", body: "Your seat moved." });
  });

  it("a ?to= that is not an agent label is ignored", async () => {
    render("/inbox/messages?compose=1&to=%3Cscript%3E");
    const form = await screen.findByRole("form", { name: "New message" }, { timeout: 8000 });
    expect(within(form).getByLabelText(/^To/)).toHaveValue("");
  });

  it("sends a new message with a client id and the chosen kind", async () => {
    const { hub } = render("/inbox/messages?compose=1", {
      "POST /api/messages": { body: { accepted: true, message_id: "m", conversation_id: "conv-9", target_live: true } },
    });
    await writeMessage(agentsPayload.agents[1].label, "Please rebase onto main.");
    await waitFor(() => expect(hub.calls.some((c) => c.method === "POST" && c.path === "/api/messages")).toBe(true));
    const body = hub.calls.find((c) => c.path === "/api/messages")?.body as Record<string, unknown>;
    expect(body).toMatchObject({ act: "inform", body: "Please rebase onto main.", to_agent: "Desktop_Agent5", expects_reply: false });
    expect(typeof body.message_id).toBe("string");
  });

  it("a parked send keeps the exact request in the draft and is run from the store or the form, never both (review 1.5)", async () => {
    let parked = true;
    const { hub, epoch } = render("/inbox/messages?compose=1", {
      "GET /api/whoami": { body: { ...whoamiFixture(), org: "org-1" } },
      "POST /api/messages": () =>
        parked
          ? { status: 202, body: { status: "pending", approval_id: 77, code: "approval_required" } }
          : { body: { accepted: true, message_id: "m", conversation_id: "conv-1" } },
      "GET /api/approvals/77": { body: { id: 77, status: "pending", tool: "message_send" } },
    });
    await writeMessage("Desktop_Agent2", "Deploy when green.");
    const form = screen.getByRole("form", { name: "New message" });
    await within(form).findByRole("region", { name: "Approval status" });
    const sent = hub.calls.find((c) => c.path === "/api/messages")?.body as Record<string, unknown>;
    const stored = listParkedWrites(epoch());
    expect(stored).toHaveLength(1);
    expect(stored[0].body).toEqual(sent);
    // The text stays in the form, and the form cannot send it while the store holds it.
    expect(within(form).getByLabelText(/^Message/)).toHaveValue("Deploy when green.");
    expect(within(form).queryByRole("button", { name: "Send" })).toBeNull();
    // Discarding the stored copy hands the request back to the form, unchanged.
    fireEvent.click(within(form).getByRole("button", { name: "Discard" }));
    parked = false;
    fireEvent.click(await within(form).findByRole("button", { name: "Send" }));
    await waitFor(() => expect(hub.calls.filter((c) => c.path === "/api/messages")).toHaveLength(2));
    expect(hub.calls.filter((c) => c.path === "/api/messages")[1].body).toEqual(sent);
    expect(sent.message_id).toBeTruthy();
  });

  it("a stored send the store cannot run (no organization) goes back through its form, and the stored copy goes only after that send succeeds", async () => {
    let parked = true;
    const { hub, epoch } = render("/inbox/messages?compose=1", {
      "POST /api/messages": () =>
        parked
          ? { status: 202, body: { status: "pending", approval_id: 77, code: "approval_required" } }
          : { body: { accepted: true, message_id: "m", conversation_id: "conv-1" } },
      "GET /api/approvals/77": { body: { id: 77, status: "approved", tool: "message_send" } },
    });
    await writeMessage("Desktop_Agent2", "Deploy when green.");
    const form = screen.getByRole("form", { name: "New message" });
    const notice = await within(form).findByRole("region", { name: "Approval status" });
    expect(within(notice).queryByRole("button", { name: "Run it now" })).toBeNull();
    expect(listParkedWrites(epoch())).toHaveLength(1);
    parked = false;
    fireEvent.click(within(form).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(hub.calls.filter((c) => c.path === "/api/messages")).toHaveLength(2));
    const [first, second] = hub.calls.filter((c) => c.path === "/api/messages").map((c) => c.body);
    expect(second).toEqual(first);
    await waitFor(() => expect(listParkedWrites(epoch())).toHaveLength(0));
  });

  it("a send that settles after more typing does not move to its conversation or drop the newer text (review 2.6)", async () => {
    let release: (reply: { body: unknown }) => void = () => {};
    const { hub, router } = render("/inbox/messages?compose=1", {
      "POST /api/messages": () => new Promise((resolve) => (release = resolve)),
    });
    await writeMessage("Desktop_Agent2", "First version.");
    const form = screen.getByRole("form", { name: "New message" });
    fireEvent.change(within(form).getByLabelText(/^Message/), { target: { value: "Second message, still typing" } });
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/messages")).toBe(true));
    release({ body: { accepted: true, duplicate: false, message_id: "m-1", conversation_id: "conv-9", from_agent: "operator", to_agent: "Desktop_Agent2", target_live: true } });
    await waitFor(() => expect(form).toHaveTextContent("Sent."));
    expect(router.state.location.search).toBe("?compose=1");
    expect(screen.getByRole("form", { name: "New message" })).toBe(form);
    expect(within(form).getByLabelText(/^Message/)).toHaveValue("Second message, still typing");
  });

  it("a reply that settles after more typing keeps the reply open with the newer text (review 2.6)", async () => {
    let release: (reply: { body: unknown }) => void = () => {};
    const { hub, router } = render("/inbox/messages?conversation=conv-1", {
      "POST /api/messages": () => new Promise((resolve) => (release = resolve)),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Reply to Desktop_Agent2" }, { timeout: 8000 }));
    const form = await screen.findByRole("form", { name: "Reply to Desktop_Agent2" });
    fireEvent.change(within(form).getByLabelText(/^Message/), { target: { value: "Not yet." } });
    fireEvent.click(within(form).getByRole("button", { name: "Send" }));
    fireEvent.change(within(form).getByLabelText(/^Message/), { target: { value: "Not yet. Also, wait for CI." } });
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/messages")).toBe(true));
    release({ body: { accepted: true, duplicate: false, message_id: "m-2", conversation_id: "conv-1", from_agent: "operator", to_agent: "Desktop_Agent2", target_live: true } });
    await waitFor(() => expect(form).toHaveTextContent("Sent."));
    expect(router.state.location.search).toContain("reply=msg-1");
    expect(within(screen.getByRole("form", { name: "Reply to Desktop_Agent2" })).getByLabelText(/^Message/)).toHaveValue("Not yet. Also, wait for CI.");
  });

  it("sweep (b): a send that parks after the person moved away still marks the draft it came from", async () => {
    let release: (reply: { status?: number; body: unknown }) => void = () => {};
    const { hub, router, epoch } = render("/inbox/messages?conversation=conv-1", {
      "GET /api/whoami": { body: { ...whoamiFixture(), org: "org-1" } },
      "POST /api/messages": () => new Promise((resolve) => (release = resolve)),
      "GET /api/approvals/77": { body: { id: 77, status: "pending", tool: "message_send", requested_by: "operator" } },
    });
    const form = await screen.findByRole("form", { name: "New message" }, { timeout: 8000 });
    await waitFor(() => expect(within(form).getByRole("option", { name: "Desktop_Agent2" })).toBeInTheDocument());
    fireEvent.change(within(form).getByLabelText(/^To/), { target: { value: "Desktop_Agent2" } });
    fireEvent.change(within(form).getByLabelText(/^Message/), { target: { value: "Deploy when green." } });
    fireEvent.click(within(form).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/messages")).toBe(true));
    await router.navigate("/inbox/messages?compose=1");
    release({ status: 202, body: { status: "pending", approval_id: 77, code: "approval_required" } });
    await waitFor(() => expect(listParkedWrites(epoch())).toHaveLength(1));
    await router.navigate("/inbox/messages?conversation=conv-1");
    const back = await screen.findByRole("form", { name: "New message" }, { timeout: 8000 });
    await within(back).findByRole("region", { name: "Approval status" }, { timeout: 8000 });
    expect(within(back).queryByRole("button", { name: "Send" })).toBeNull();
    expect(within(back).getByLabelText(/^Message/)).toHaveValue("Deploy when green.");
  });

  it("sweep (c): a hand-off left mid-request shows outcome unknown on return, not a fresh Create", async () => {
    const { router, hub } = render("/inbox/messages?conversation=conv-1", {
      "POST /api/messages/conversations/conv-1/handoff": () => new Promise<never>(() => {}),
    });
    const form = await screen.findByRole("form", { name: "Hand off this conversation" }, { timeout: 8000 });
    fireEvent.change(within(form).getByLabelText(/^Task title/), { target: { value: "Decide the ship date" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create the task" }));
    await waitFor(() => expect(hub.calls.some((c) => c.path.endsWith("/handoff"))).toBe(true));
    await router.navigate("/inbox/messages?compose=1");
    await waitFor(() => expect(screen.queryByRole("form", { name: "Hand off this conversation" })).toBeNull());
    await router.navigate("/inbox/messages?conversation=conv-1");
    const back = await screen.findByRole("form", { name: "Hand off this conversation" }, { timeout: 8000 });
    expect(await within(back).findByText(/Outcome unknown/)).toBeInTheDocument();
    expect(within(back).queryByRole("button", { name: "Create the task" })).toBeNull();
    fireEvent.click(within(back).getByRole("button", { name: "It did not happen" }));
    expect(await within(back).findByRole("button", { name: "Create the task" })).toBeInTheDocument();
  });

  it("text typed while a send is in flight survives its success (review 1.6)", async () => {
    let release: (reply: { body: unknown }) => void = () => {};
    const { hub } = render("/inbox/messages?compose=1", {
      "POST /api/messages": () => new Promise((resolve) => (release = resolve)),
    });
    await writeMessage("Desktop_Agent2", "First version.");
    const form = screen.getByRole("form", { name: "New message" });
    fireEvent.change(within(form).getByLabelText(/^Message/), { target: { value: "Second thought, still typing" } });
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/messages")).toBe(true));
    release({ body: { accepted: true, message_id: "m", conversation_id: null } });
    await waitFor(() => expect(form).toHaveTextContent("Sent."));
    expect(within(form).getByLabelText(/^Message/)).toHaveValue("Second thought, still typing");
  });

  it("replies from the thread to the other party", async () => {
    const { hub } = render("/inbox/messages?conversation=conv-1", {
      "POST /api/messages": { body: { accepted: true, conversation_id: "conv-1" } },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Reply to Desktop_Agent2" }, { timeout: 8000 }));
    const form = await screen.findByRole("form", { name: "Reply to Desktop_Agent2" });
    fireEvent.change(within(form).getByLabelText(/^Message/), { target: { value: "Not yet." } });
    fireEvent.click(within(form).getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(hub.calls.find((c) => c.path === "/api/messages")?.body).toMatchObject({ to_agent: "Desktop_Agent2", reply_to: "msg-1", body: "Not yet." }),
    );
  });

  it("hands the conversation off to a new task", async () => {
    const { hub } = render("/inbox/messages?conversation=conv-1", {
      "POST /api/messages/conversations/conv-1/handoff": { body: { conversation_id: "conv-1", task_id: 310, created: true, reference: "x" } },
    });
    const form = await screen.findByRole("form", { name: "Hand off this conversation" }, { timeout: 8000 });
    fireEvent.change(within(form).getByLabelText(/^Task title/), { target: { value: "Decide the ship date" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create the task" }));
    await waitFor(() => {
      expect(hub.calls.find((c) => c.path === "/api/messages/conversations/conv-1/handoff")?.body).toEqual({ title: "Decide the ship date" });
    });
    expect(await screen.findByText("Created task 310.")).toBeInTheDocument();
  });
});
