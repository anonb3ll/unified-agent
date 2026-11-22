import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WHOAMI_QUERY_KEY } from "../../../src/lib/api/auth";
import { clearParkedWrites, listParkedWrites } from "../../../src/app/lib/parkedWrites";
import { fixtureEpoch } from "../support/epoch";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { agentsHub, OPERATOR_ONLY, parked, REMOVE_RESULT } from "./fixtures";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

const REMOVE = "/api/action/agent/remove";
const removes = <T extends { method: string; path: string }>(calls: T[]) => calls.filter((c) => c.method === "POST" && c.path === REMOVE);

async function openRemove(label = "Desktop_Agent1") {
  const button = await screen.findByRole("button", { name: `Remove ${label}` });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  return screen.findByRole("dialog", { name: `Remove ${label}?` });
}

describe("agents: the agent page", () => {
  it("shows what it is doing, its seat, where it runs, credentials, sessions and activity", async () => {
    renderRoute("/agents/profile/Desktop_Agent1", { hub: agentsHub() });
    await findPageHeading("Desktop_Agent1");
    const now = screen.getByRole("region", { name: "What it is doing now" });
    expect(await within(now).findByRole("link", { name: "sess-open-1" })).toHaveAttribute("href", "/ui/sessions/sess-open-1");
    expect(await within(now).findByRole("link", { name: "#2473 Agents area" })).toHaveAttribute("href", "/ui/work/tasks/2473");
    expect(within(now).queryByText(/Someone else's/)).toBeNull();
    const seat = screen.getByRole("region", { name: "Seat and what it may do" });
    expect(await within(seat).findAllByText("steward")).toHaveLength(2);
    expect(within(seat).getByText("This agent")).toBeInTheDocument();
    expect(within(seat).getByText("review verdicts")).toBeInTheDocument();
    const where = screen.getByRole("region", { name: "Where it runs" });
    expect(within(where).getByText("claude-code")).toBeInTheDocument();
    expect(within(where).getByText("desktop, laptop")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Credentials" })).toHaveTextContent("Managed token, active");
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Recent activity" })).getByText("agent.task.claim")).toBeInTheDocument();
  });

  it("'Start a session' and 'Send a message' open those pages for this agent (#2523)", async () => {
    renderRoute("/agents/profile/Desktop_Agent1", { hub: agentsHub() });
    await findPageHeading("Desktop_Agent1");
    expect(screen.getByRole("link", { name: "Start a session" })).toHaveAttribute("href", "/ui/sessions/new?agent=Desktop_Agent1");
    expect(screen.getByRole("link", { name: "Send a message" })).toHaveAttribute("href", "/ui/inbox/messages?participant=Desktop_Agent1&compose=1&to=Desktop_Agent1");
  });

  it("an unknown label says so instead of an empty page", async () => {
    renderRoute("/agents/profile/Nobody_Here", { hub: agentsHub({ "GET /api/board": { body: { columns: [] } } }) });
    await findPageHeading("Nobody_Here");
    expect(await screen.findByText(/does not know an agent called Nobody_Here/)).toBeInTheDocument();
  });

  it("removes after a confirm with a reason, sends the body and shows what was revoked", async () => {
    const { hub } = renderRoute("/agents/profile/Desktop_Agent1", { hub: agentsHub({ [`POST ${REMOVE}`]: { body: REMOVE_RESULT } }) });
    await findPageHeading("Desktop_Agent1");
    const dialog = await openRemove();
    const confirm = within(dialog).getByRole("button", { name: "Remove" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "replaced by a new machine" } });
    fireEvent.click(within(dialog).getByLabelText("Release its claimed tasks"));
    fireEvent.click(confirm);
    expect(await screen.findByText(/Desktop_Agent1 was removed\. 1 tokens and 2 session credentials revoked; released tasks #2473/)).toBeInTheDocument();
    expect(removes(hub.calls)[0].body).toEqual({ label: "Desktop_Agent1", reason: "replaced by a new machine", force_release: true });
    expect(Object.keys(sessionStorage).filter((k) => k.includes("agent.remove"))).toEqual([]);
  });

  it("a 409 claims_held is explained and the draft keeps the reason", async () => {
    renderRoute("/agents/profile/Desktop_Agent1", {
      hub: agentsHub({ [`POST ${REMOVE}`]: { status: 409, body: { error: "agent holds claims", code: "claims_held" } } }),
    });
    await findPageHeading("Desktop_Agent1");
    const dialog = await openRemove();
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "done" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(await screen.findByText(/still holds claimed tasks/)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 400));
    await waitFor(() => expect(Object.values(sessionStorage).join("")).toContain('"reason":"done"'));
    expect(Object.values(sessionStorage).join("")).not.toContain('"attempt":{');
  });

  it("a parked remove waits for approval and is stored for Run it now", async () => {
    const result = renderRoute("/agents/profile/Desktop_Agent1", {
      hub: agentsHub({
        [`POST ${REMOVE}`]: { status: 202, body: parked("agent_remove", 92) },
        "GET /api/approvals/92": { body: { id: 92, tool: "agent_remove", status: "pending", requested_by: "operator" } },
      }),
    });
    await findPageHeading("Desktop_Agent1");
    const dialog = await openRemove();
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "retire it" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(await screen.findByRole("region", { name: "Approval status" })).toBeInTheDocument();
    expect(screen.queryByText(/was removed/)).toBeNull();
    const stored = listParkedWrites(result.epoch());
    expect(stored).toHaveLength(1);
    expect(stored[0]).toMatchObject({ path: REMOVE, approvalId: "92", body: { label: "Desktop_Agent1", reason: "retire it" } });
  });

  it("text typed while the remove is in flight is kept: the draft is cleared only against the submitted reason", async () => {
    let release: (() => void) | null = null;
    renderRoute("/agents/profile/Desktop_Agent1", {
      hub: agentsHub({
        [`POST ${REMOVE}`]: () =>
          new Promise((resolve) => {
            release = () => resolve({ body: REMOVE_RESULT });
          }),
      }),
    });
    await findPageHeading("Desktop_Agent1");
    const dialog = await openRemove();
    const reason = within(dialog).getByLabelText(/^Reason/);
    fireEvent.change(reason, { target: { value: "first" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(release).not.toBeNull());
    fireEvent.change(reason, { target: { value: "first, and more" } });
    await act(async () => release?.());
    expect(await screen.findByText(/Desktop_Agent1 was removed\. 1 tokens/)).toBeInTheDocument();
    await waitFor(() => expect(Object.values(sessionStorage).join("")).toContain("first, and more"));
  });

  it("a remove answer that arrives after the page was left keeps the reason a later visit typed (review of b4cc9988, item 1)", async () => {
    let release: (() => void) | null = null;
    const { router } = renderRoute("/agents/profile/Desktop_Agent1", {
      hub: agentsHub({
        [`POST ${REMOVE}`]: () =>
          new Promise((resolve) => {
            release = () => resolve({ body: REMOVE_RESULT });
          }),
      }),
    });
    await findPageHeading("Desktop_Agent1");
    const dialog = await openRemove();
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "first reason" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(release).not.toBeNull());
    await act(async () => {
      await router.navigate("/agents");
    });
    await findPageHeading("Agents");
    await act(async () => {
      await router.navigate("/agents/profile/Desktop_Agent1");
    });
    await findPageHeading("Desktop_Agent1");
    const again = await openRemove();
    fireEvent.change(within(again).getByLabelText(/^Reason/), { target: { value: "a newer reason" } });
    await waitFor(() => expect(Object.values(sessionStorage).join("")).toContain("a newer reason"));
    await act(async () => release?.());
    await new Promise((r) => setTimeout(r, 50));
    expect(Object.entries(sessionStorage).find(([k]) => k.includes("agent.remove"))?.[1] ?? "").toContain("a newer reason");
  });

  it("lifecycle controls follow the hub's rules: another agent sees them disabled with the reason (review of b4cc9988, item 3)", async () => {
    const { hub } = renderRoute("/agents/profile/Desktop_Agent1", {
      hub: agentsHub({ "GET /api/whoami": { body: whoamiFixture({ agent: "Desktop_Agent2" }) }, "GET /api/agent-roster": OPERATOR_ONLY }),
    });
    await findPageHeading("Desktop_Agent1");
    const section = await screen.findByRole("region", { name: "Pause, stop or retire" });
    for (const name of ["Pause", "Stop", "Retire"]) await waitFor(() => expect(within(section).getByRole("button", { name })).toBeDisabled());
    expect(within(section).getByText(/Only an operator or Desktop_Agent1 itself can change Desktop_Agent1's state\./)).toBeInTheDocument();
    expect(within(section).getByText(/Only an operator can retire or unretire an agent/)).toBeInTheDocument();
    fireEvent.click(within(section).getByRole("button", { name: "Retire" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(hub.calls.some((c) => c.method === "POST" && c.path === "/api/agent-lifecycle")).toBe(false);
  });

  it("an agent may pause and stop itself but not retire itself, and resumes only its own pause", async () => {
    renderRoute("/agents/profile/Desktop_Agent1", {
      hub: agentsHub({ "GET /api/whoami": { body: whoamiFixture({ agent: "Desktop_Agent1" }) }, "GET /api/agent-roster": OPERATOR_ONLY }),
    });
    await findPageHeading("Desktop_Agent1");
    const section = await screen.findByRole("region", { name: "Pause, stop or retire" });
    await waitFor(() => expect(within(section).getByRole("button", { name: "Pause" })).toBeEnabled());
    expect(within(section).getByRole("button", { name: "Stop" })).toBeEnabled();
    expect(within(section).getByRole("button", { name: "Retire" })).toBeDisabled();
    cleanup();
    renderRoute("/agents/profile/Desktop_Agent1", {
      hub: agentsHub({
        "GET /api/whoami": { body: whoamiFixture({ agent: "Desktop_Agent1" }) },
        "GET /api/agent-roster": OPERATOR_ONLY,
        "GET /api/agent-lifecycle": { body: { agents: [{ agent: "Desktop_Agent1", state: "paused", reason: "deploy", actor: "operator", since: null }], registered: [], operators: ["operator"] } },
      }),
    });
    await findPageHeading("Desktop_Agent1");
    const paused = await screen.findByRole("region", { name: "Pause, stop or retire" });
    await waitFor(() => expect(within(paused).getByRole("button", { name: "Resume" })).toBeDisabled());
    expect(within(paused).getByText(/was paused by operator; only an operator can resume it/)).toBeInTheDocument();
  });

  it("the operator gets every lifecycle action", async () => {
    renderRoute("/agents/profile/Desktop_Agent1", { hub: agentsHub() });
    await findPageHeading("Desktop_Agent1");
    const section = await screen.findByRole("region", { name: "Pause, stop or retire" });
    for (const name of ["Pause", "Stop", "Retire"]) await waitFor(() => expect(within(section).getByRole("button", { name })).toBeEnabled());
  });

  it("a cancelled remove (owner changed after the send) says the outcome is unknown and keeps the attempt", async () => {
    let release: (() => void) | null = null;
    const result = renderRoute("/agents/profile/Desktop_Agent1", {
      hub: agentsHub({
        [`POST ${REMOVE}`]: () =>
          new Promise((resolve) => {
            release = () => resolve({ body: REMOVE_RESULT });
          }),
      }),
    });
    await findPageHeading("Desktop_Agent1");
    const dialog = await openRemove();
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "gone" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(release).not.toBeNull());
    const before = fixtureEpoch();
    await act(async () => {
      result.queryClient.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent: "Mac_Hermes" }));
    });
    await act(async () => release?.());
    await new Promise((r) => setTimeout(r, 50));
    // Nothing of the operator's remove is shown to the next person.
    expect(screen.queryByText(/was removed/)).toBeNull();
    // The operator's attempt marker, recorded before the send, is still in their storage.
    expect(before.storage.keys("draft:").some((k) => k.includes("agent.remove"))).toBe(true);
  });

  it("a non-operator sees the remove disabled with the reason", async () => {
    const { hub } = renderRoute("/agents/profile/Desktop_Agent1", { hub: agentsHub({ "GET /api/agent-roster": OPERATOR_ONLY }) });
    await findPageHeading("Desktop_Agent1");
    expect(await screen.findByText("Only the human operator can remove agents.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove Desktop_Agent1" })).toBeDisabled();
    expect(screen.getByRole("region", { name: "Credentials" })).toHaveTextContent("Only the operator can see token details");
    expect(removes(hub.calls)).toHaveLength(0);
  });

  it("a 403 answer to the remove is explained", async () => {
    renderRoute("/agents/profile/Desktop_Agent1", { hub: agentsHub({ [`POST ${REMOVE}`]: OPERATOR_ONLY }) });
    await findPageHeading("Desktop_Agent1");
    const dialog = await openRemove();
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "x" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(await screen.findByText(/Only the human operator can add or remove agents/)).toBeInTheDocument();
  });

  it("state is keyed by agent: another agent's page never shows this one's dialog, reason or result", async () => {
    const { router } = renderRoute("/agents/profile/Desktop_Agent1", { hub: agentsHub({ [`POST ${REMOVE}`]: { body: REMOVE_RESULT } }) });
    await findPageHeading("Desktop_Agent1");
    const dialog = await openRemove();
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "claude only" } });
    await act(async () => {
      await router.navigate("/agents/profile/Desktop_Agent2");
    });
    await findPageHeading("Desktop_Agent2");
    expect(screen.queryByRole("dialog")).toBeNull();
    const other = await openRemove("Desktop_Agent2");
    expect(within(other).getByLabelText(/^Reason/)).toHaveValue("");
  });

  it("pauses through a confirm with a reason", async () => {
    const { hub } = renderRoute("/agents/profile/Desktop_Agent1", { hub: agentsHub({ "POST /api/agent-lifecycle": { body: { stops: [] } } }) });
    await findPageHeading("Desktop_Agent1");
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    const dialog = await screen.findByRole("dialog", { name: "Pause Desktop_Agent1?" });
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "deploy window" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(hub.calls.some((c) => c.method === "POST" && c.path === "/api/agent-lifecycle")).toBe(true));
    expect(hub.calls.find((c) => c.method === "POST" && c.path === "/api/agent-lifecycle")!.body).toEqual({ agent: "Desktop_Agent1", action: "pause", reason: "deploy window" });
  });
});
