import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import type { MockHandler } from "../support/fetchMock";
import { findPageHeading, renderRoute } from "../support/render";
import { forVitest, REALISTIC } from "./hubData";
import { installLocks } from "./locks";
import { fixtureEpoch } from "../support/epoch";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  installLocks();
  clearParkedWrites(fixtureEpoch());
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function open(overrides: Record<string, MockHandler> = {}, path = "/runs/workflows?workflow=smoke-echo") {
  const r = renderRoute(path, { hub: { ...forVitest(REALISTIC), ...overrides } });
  return r;
}

const DONE = {
  workflow: "smoke-echo",
  status: "done",
  steps: [{ name: "echo", tool: "format_render", status: "done", attempts: 1, error: null, result: { text: "hello" } }],
  outputs: { echo: { text: "hello" } },
};

describe("/runs/workflows", () => {
  it("builds the form from the declared inputs and moves focus to the first missing one", async () => {
    const { hub } = open();
    await findPageHeading("Workflows");
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    const input = within(form).getByRole("textbox", { name: /message/ });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    expect(await screen.findByText("Enter a value for message.")).toBeInTheDocument();
    await waitFor(() => expect(input).toHaveFocus());
    expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("runs it with its inputs and shows each step's result", async () => {
    let sent: unknown = null;
    open({
      "POST /api/action/workflow": ({ body }) => {
        sent = body;
        return { body: DONE };
      },
    });
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    fireEvent.change(within(form).getByRole("textbox", { name: /message/ }), { target: { value: "hello" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    const steps = await screen.findByRole("table", { name: "Step results" });
    expect(within(steps).getByText("echo")).toBeInTheDocument();
    expect(within(steps).getByText("Done")).toBeInTheDocument();
    expect(sent).toEqual({ name: "smoke-echo", inputs: { message: "hello" } });
    expect(screen.getByText("Every step finished.")).toBeInTheDocument();
  });

  it("a step waiting for approval is shown as waiting, with the Inbox link, not as an error", async () => {
    open({
      "POST /api/action/workflow": {
        body: { workflow: "smoke-echo", status: "pending-approval", steps: [{ name: "echo", tool: "run_cmd", status: "pending-approval", attempts: 1, error: "approval 12 pending" }], outputs: {} },
      },
    });
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    fireEvent.change(within(form).getByRole("textbox", { name: /message/ }), { target: { value: "x" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    expect(await screen.findByText(/A step is waiting for approval/)).toBeInTheDocument();
    // The result says to run it again after approval, so the inputs stay (pass-2 item 6).
    expect(within(form).getByRole("textbox", { name: /message/ })).toHaveValue("x");
    expect(screen.getByRole("link", { name: "Open the approvals in the Inbox" })).toHaveAttribute("href", "/ui/inbox?kind=approval");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a lost answer holds a second start; candidates are only candidates; restarting needs a confirm that warns of a duplicate", async () => {
    let posts = 0;
    const fresh = { seq: 12, ulid: "01JD7Z0000000000000000000C", channel: "agent.workflow.updated", payload: { kind: "workflow", ref_id: "smoke-echo", summary: "run started (1 steps)" }, originator: "operator", created_at: new Date().toISOString() };
    const old = { ...fresh, ulid: "01JD7Z0000000000000000000D", created_at: new Date(Date.now() - 3_600_000).toISOString() };
    const other = { ...fresh, ulid: "01JD7Z0000000000000000000E", payload: { ...fresh.payload, ref_id: "weekly-export" } };
    open({
      "GET /api/events": { body: { events: [fresh, old, other], count: 3 } },
      "POST /api/action/workflow": () => {
        posts += 1;
        if (posts === 1) throw new TypeError("network connection lost");
        return { body: DONE };
      },
    });
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    fireEvent.change(within(form).getByRole("textbox", { name: /message/ }), { target: { value: "hello" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    const check = await screen.findByRole("region", { name: "Start not confirmed" });
    expect(check).toHaveTextContent("We could not confirm whether this started.");
    // The attempt was recorded before sending, so a reload still holds it.
    expect(Object.keys(localStorage).some((k) => k.includes("ua-cc2:runs-attempt:") && k.includes("smoke-echo"))).toBe(true);
    expect(within(form).getByRole("button", { name: "Run workflow" })).toBeDisabled();
    // Events the hub returned are offered as possible matches, never as success.
    await waitFor(() => expect(check.textContent).toMatch(/may be this one/), { timeout: 4000 }).catch(() => { throw new Error(check.textContent ?? ""); });
    // Only this workflow's runs, started since the attempt was sent; nothing is picked for the person.
    expect(within(check).getAllByRole("radio")).toHaveLength(1);
    expect(within(check).getByRole("button", { name: "This one is it" })).toBeDisabled();
    expect(screen.queryByText("Every step finished.")).toBeNull();
    fireEvent.click(within(check).getByRole("button", { name: "I have checked and it did not start. Start again" }));
    const confirm = await screen.findByRole("dialog", { name: "Start it again?" });
    expect(confirm).toHaveTextContent("this creates a duplicate");
    fireEvent.click(within(confirm).getByRole("button", { name: "Start again" }));
    expect(await screen.findByRole("table", { name: "Step results" })).toBeInTheDocument();
    expect(posts).toBe(2);
    expect(screen.queryByRole("region", { name: "Start not confirmed" })).toBeNull();
  });

  it("a failed check is 'could not check' with Retry, and never enables a restart by itself", async () => {
    open({
      "GET /api/events": { status: 503, body: { error: "event log unavailable" } },
      "POST /api/action/workflow": () => {
        throw new TypeError("network connection lost");
      },
    });
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    fireEvent.change(within(form).getByRole("textbox", { name: /message/ }), { target: { value: "hello" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    const check = await screen.findByRole("region", { name: "Start not confirmed" });
    expect(await within(check).findByText("Could not check.")).toBeInTheDocument();
    expect(within(check).getByRole("button", { name: "Retry the check" })).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Run workflow" })).toBeDisabled();
    // No candidate to pick: the only way on is the confirmed restart (item 6).
    expect(within(check).queryByRole("button", { name: /one is it/ })).toBeNull();
    expect(within(check).getByRole("button", { name: "I have checked and it did not start. Start again" })).toBeInTheDocument();
  });

  it("picking a candidate is what releases the hold", async () => {
    const fresh = { seq: 12, ulid: "01JD7Z0000000000000000000C", channel: "agent.workflow.updated", payload: { kind: "workflow", ref_id: "smoke-echo", summary: "run started (1 steps)" }, originator: "operator", created_at: new Date().toISOString() };
    open({
      "GET /api/events": { body: { events: [fresh], count: 1 } },
      "POST /api/action/workflow": () => {
        throw new TypeError("network connection lost");
      },
    });
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    fireEvent.change(within(form).getByRole("textbox", { name: /message/ }), { target: { value: "hello" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    const check = await screen.findByRole("region", { name: "Start not confirmed" });
    fireEvent.click(await within(check).findByRole("radio", { name: /run started/ }));
    fireEvent.click(within(check).getByRole("button", { name: "This one is it" }));
    await waitFor(() => expect(within(form).getByRole("button", { name: "Run workflow" })).toBeEnabled());
  });

  it("a capped check warns that matching activity may be missing even when it lists a candidate (pass-2 item 5)", async () => {
    const fresh = { seq: 12, ulid: "01JD7Z0000000000000000000C", channel: "agent.workflow.updated", payload: { kind: "workflow", ref_id: "smoke-echo", summary: "run started (1 steps)" }, originator: "operator", created_at: new Date().toISOString() };
    const filler = Array.from({ length: 199 }, (_, i) => ({ ...fresh, ulid: `01JD7Z${String(i).padStart(20, "0")}`, payload: { ...fresh.payload, ref_id: "weekly-export" } }));
    open({
      "GET /api/events": { body: { events: [fresh, ...filler], count: 200 } },
      "POST /api/action/workflow": () => {
        throw new TypeError("network connection lost");
      },
    });
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    fireEvent.change(within(form).getByRole("textbox", { name: /message/ }), { target: { value: "hello" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    const check = await screen.findByRole("region", { name: "Start not confirmed" });
    expect(await within(check).findByRole("radio", { name: /run started/ })).toBeInTheDocument();
    expect(within(check).getByRole("note")).toHaveTextContent("the list is capped");
    expect(within(check).getByRole("button", { name: "Check again" })).toBeInTheDocument();
    expect(within(check).getByRole("button", { name: "This one is it" })).toBeDisabled();
  });

  it("an indeterminate answer keeps the hold and the inputs (item 3)", async () => {
    open({
      "POST /api/action/workflow": { body: { workflow: "smoke-echo", status: "indeterminate", steps: [{ name: "echo", tool: "run_cmd", status: "indeterminate", attempts: 1, error: "LedgerWriteError: receipt not written" }], outputs: {} } },
    });
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    const input = within(form).getByRole("textbox", { name: /message/ });
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    const check = await screen.findByRole("region", { name: "Start not confirmed" });
    expect(check).toHaveTextContent("a step may have run but its record could not be written");
    expect(input).toHaveValue("hello");
    expect(within(form).getByRole("button", { name: "Run workflow" })).toBeDisabled();
  });

  it("start again validates first and never clears the hold for an invalid form (item 9)", async () => {
    let posts = 0;
    open({
      "POST /api/action/workflow": () => {
        posts += 1;
        throw new TypeError("network connection lost");
      },
    });
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    const input = within(form).getByRole("textbox", { name: /message/ });
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    const check = await screen.findByRole("region", { name: "Start not confirmed" });
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(within(check).getByRole("button", { name: "I have checked and it did not start. Start again" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Start it again?" })).getByRole("button", { name: "Start again" }));
    expect(await within(form).findByText("Enter a value for message.")).toBeInTheDocument();
    expect(posts).toBe(1);
    expect(screen.getByRole("region", { name: "Start not confirmed" })).toBeInTheDocument();
  });

  it("text typed while the run was in flight survives its answer (item 12)", async () => {
    let release: (v: unknown) => void = () => {};
    open({
      "POST /api/action/workflow": () => new Promise((r) => (release = () => r({ body: DONE }))),
    });
    const form = await screen.findByRole("form", { name: "Run smoke-echo" });
    const input = within(form).getByRole("textbox", { name: /message/ });
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(within(form).getByRole("button", { name: "Run workflow" })).toBeDisabled());
    fireEvent.change(input, { target: { value: "hello again" } });
    release(null);
    expect(await screen.findByRole("table", { name: "Step results" })).toBeInTheDocument();
    expect(input).toHaveValue("hello again");
  });

  it("a 200 whose body carries an error is a failed read", async () => {
    open({ "GET /api/overview": { body: { workflows: { workflows: [], error: "workflow_list failed: boom" } } } }, "/runs/workflows");
    expect(await screen.findByRole("heading", { name: /Workflows could not be loaded/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "No workflows" })).toBeNull();
  });
});
