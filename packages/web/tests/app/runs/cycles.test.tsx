import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import type { MockHandler } from "../support/fetchMock";
import { findPageHeading, renderRoute } from "../support/render";
import { cycle, CYCLE_DETAIL, forVitest, REALISTIC } from "./hubData";
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
  vi.restoreAllMocks();
});

async function open(path: string, overrides: Record<string, MockHandler> = {}) {
  const r = renderRoute(path, { hub: { ...forVitest(REALISTIC), ...overrides } });
  await findPageHeading(path === "/runs/cycles" ? "Cycles" : `Cycle ${path.split("/").pop()}`);
  await screen.findByRole(path === "/runs/cycles" ? "table" : "button", path === "/runs/cycles" ? undefined : { name: /Stop|Resume/ }, { timeout: 10_000 }).catch(() => undefined);
  return r;
}

describe("/runs/cycles", () => {
  it("lists cycles with state, limits used and a link to each, and says what a cycle is", async () => {
    await open("/runs/cycles");
    await findPageHeading("Cycles");
    expect(screen.getByText(/A cycle is a supervised loop/)).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: /Cycles, with the share of each limit used/ });
    expect(within(table).getByRole("link", { name: "c0ffee123456" })).toHaveAttribute("href", "/ui/runs/cycles/c0ffee123456");
    expect(within(table).getByText("Tokens: 120000 of 100000 (over the limit)")).toBeInTheDocument();
    expect(within(table).getByText("autonomy budget exhausted: tokens")).toBeInTheDocument();
  });

  it("never posts a start without a task, and never uses window.prompt (defect 7)", async () => {
    const prompt = vi.spyOn(window, "prompt");
    const { hub } = await open("/runs/cycles");
    fireEvent.click(await screen.findByRole("button", { name: "Start a cycle" }));
    const dialog = await screen.findByRole("dialog", { name: "Start a cycle" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Start cycle" }));
    expect(await within(dialog).findByText("Enter the number of the task this cycle works on.")).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole("textbox", { name: /Task number/ })).toHaveFocus());
    expect(hub.calls.filter((c) => c.method === "POST")).toHaveLength(0);
    expect(prompt).not.toHaveBeenCalled();
  });

  it("starts a cycle for the task with its limits", async () => {
    let sent: unknown = null;
    await open("/runs/cycles", {
      "POST /api/action/cycle": ({ body }) => {
        sent = body;
        return { status: 201, body: { cycle: cycle({ id: "newcycle0001", task_id: 77, state: "plan" }), created: true } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Start a cycle" }));
    const dialog = await screen.findByRole("dialog", { name: "Start a cycle" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Task number/ }), { target: { value: "77" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Token limit/ }), { target: { value: "5000" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Start cycle" }));
    expect(await within(dialog).findByRole("link", { name: "Open cycle newcycle0001" })).toBeInTheDocument();
    expect(sent).toEqual({ operation: "start", task_id: 77, max_rework: 2, budgets: { tokens: 5000 } });
  });
  it("after a lost answer, start again validates first: an emptied task number never posts task_id 0 (item 9)", async () => {
    const sent: unknown[] = [];
    await open("/runs/cycles", {
      "POST /api/action/cycle": ({ body }) => {
        sent.push(body);
        throw new TypeError("connection reset");
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Start a cycle" }));
    const dialog = await screen.findByRole("dialog", { name: "Start a cycle" });
    const task = within(dialog).getByRole("textbox", { name: /Task number/ });
    fireEvent.change(task, { target: { value: "77" } });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Start cycle" })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole("button", { name: "Start cycle" }));
    const check = await within(dialog).findByRole("region", { name: "Start not confirmed" });
    fireEvent.change(task, { target: { value: "" } });
    fireEvent.click(within(check).getByRole("button", { name: "I have checked and it did not start. Start again" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Start it again?" })).getByRole("button", { name: "Start again" }));
    expect(await within(dialog).findByText("Enter the number of the task this cycle works on.")).toBeInTheDocument();
    expect(sent).toEqual([{ operation: "start", task_id: 77, max_rework: 2 }]);
    expect(within(dialog).getByRole("region", { name: "Start not confirmed" })).toBeInTheDocument();
  });
});

describe("/runs/cycles sweep", () => {
  it("a task number typed while a start is in flight survives its answer (sweep b)", async () => {
    let release: () => void = () => {};
    await open("/runs/cycles", {
      "POST /api/action/cycle": () => new Promise((r) => (release = () => r({ status: 201, body: { cycle: cycle({ id: "newcycle0001", task_id: 77, state: "plan" }), created: true } }))),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Start a cycle" }));
    const dialog = await screen.findByRole("dialog", { name: "Start a cycle" });
    const task = within(dialog).getByRole("textbox", { name: /Task number/ });
    fireEvent.change(task, { target: { value: "77" } });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Start cycle" })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole("button", { name: "Start cycle" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Start cycle" })).toBeDisabled());
    fireEvent.change(task, { target: { value: "78" } });
    release();
    expect(await within(dialog).findByRole("link", { name: "Open cycle newcycle0001" })).toBeInTheDocument();
    expect(task).toHaveValue("78");
  });
});

describe("/runs/cycles/:id", () => {
  it("shows the cycle, its limits and its history", async () => {
    await open("/runs/cycles/c0ffee123456");
    await findPageHeading("Cycle c0ffee123456");
    expect(await screen.findByRole("table", { name: /Steps of cycle c0ffee123456/ })).toHaveTextContent("Handed to an agent");
    expect(screen.getByText("Tokens: 42000 of 100000")).toBeInTheDocument();
  });

  it("pause is a form in a confirm dialog that needs a reason and sends a keyed transition to blocked", async () => {
    const sent: unknown[] = [];
    await open("/runs/cycles/c0ffee123456", {
      "POST /api/action/cycle": ({ body }) => {
        sent.push(body);
        return { body: { cycle: cycle({ state: "blocked", version: 4 }), transition: {}, idempotent: false } };
      },
    });
    const pause = await screen.findByRole("button", { name: "Pause" });
    fireEvent.click(pause);
    const dialog = await screen.findByRole("dialog", { name: "Pause this cycle?" });
    expect(dialog).toHaveTextContent("Work already running is not cut off");
    fireEvent.click(within(dialog).getByRole("button", { name: "Pause cycle" }));
    expect(await within(dialog).findByText("Say why it is being paused.")).toBeInTheDocument();
    expect(sent).toHaveLength(0);
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Reason/ }), { target: { value: "waiting on the API key" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Pause cycle" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({ operation: "transition", cycle_id: "c0ffee123456", to_state: "blocked", expected_version: 3, reason: "waiting on the API key" });
    expect((sent[0] as { idempotency_key: string }).idempotency_key).toMatch(/^cc2-cycle-/);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Pause this cycle?" })).toBeNull());
    await waitFor(() => expect(pause).toHaveFocus());
  });

  it("stop states the full consequence, is a danger confirm, and sends kill with the reason", async () => {
    const sent: unknown[] = [];
    await open("/runs/cycles/c0ffee123456", {
      "POST /api/action/cycle": ({ body }) => {
        sent.push(body);
        return { body: { cycle: cycle({ state: "killed" }), killed_from: "executing" } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog", { name: "Stop this cycle for good?" });
    expect(dialog).toHaveTextContent("This cannot be undone");
    expect(dialog).toHaveTextContent("Its history, the limits it used and the task itself are kept");
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Reason/ }), { target: { value: "wrong task" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Stop cycle" }));
    await waitFor(() => expect(sent).toEqual([{ operation: "kill", cycle_id: "c0ffee123456", reason: "wrong task" }]));
  });

  it("moving to a pull request needs a link or commit, asked in the dialog", async () => {
    const sent: unknown[] = [];
    await open("/runs/cycles/c0ffee123456", {
      "GET /api/cycles/c0ffee123456": { body: { ...CYCLE_DETAIL, cycle: cycle({ state: "reviewing" }) } },
      "POST /api/action/cycle": ({ body }) => {
        sent.push(body);
        return { body: { cycle: cycle({ state: "pr" }) } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Move to the next step" }));
    const dialog = await screen.findByRole("dialog", { name: "Move this cycle to the next step?" });
    fireEvent.click(within(dialog).getByRole("radio", { name: "Pull request open" }));
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Pull request link or commit/ }), { target: { value: "not a link" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Move it on" }));
    expect(await within(dialog).findByText(/Enter a link starting with http/)).toBeInTheDocument();
    expect(sent).toHaveLength(0);
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Pull request link or commit/ }), { target: { value: "https://github.com/x/y/pull/3" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Move it on" }));
    await waitFor(() => expect(sent[0]).toMatchObject({ to_state: "pr", pr_url: "https://github.com/x/y/pull/3" }));
  });

  it("a refused action shows a plain message; the raw hub text is only in the collapsed detail (defect 10)", async () => {
    await open("/runs/cycles/c0ffee123456", {
      "POST /api/action/cycle": { status: 400, body: { error: "invalid cycle request: ToolError('stale version 3 != 4')" } },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Resume" }).catch(() => screen.findByRole("button", { name: "Pause" })));
    const dialog = await screen.findByRole("dialog");
    const reason = within(dialog).queryByRole("textbox", { name: /Reason/ });
    if (reason) fireEvent.change(reason, { target: { value: "r" } });
    fireEvent.click(within(dialog).getAllByRole("button").at(-1) as HTMLElement);
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/^The hub did not accept this/);
    expect(within(alert).getByText(/stale version/, { selector: "pre" }).closest("details")).not.toBeNull();
  });

  it("a transition's key exists before it is sent and is reused after a lost answer (sweep c)", async () => {
    const keys: string[] = [];
    await open("/runs/cycles/c0ffee123456", {
      "POST /api/action/cycle": ({ body }) => {
        keys.push((body as { idempotency_key: string }).idempotency_key);
        if (keys.length === 1) throw new TypeError("connection reset");
        return { body: { cycle: cycle({ state: "blocked" }) } };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    const dialog = await screen.findByRole("dialog", { name: "Pause this cycle?" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Reason/ }), { target: { value: "waiting" } });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Pause cycle" })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole("button", { name: "Pause cycle" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Pause cycle" }));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[0]).toMatch(/^cc2-cycle-/);
    expect(keys[1]).toBe(keys[0]);
  });

  it("one action's typed state never shows in another action's dialog (sweep a)", async () => {
    await open("/runs/cycles/c0ffee123456");
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    let dialog = await screen.findByRole("dialog", { name: "Pause this cycle?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Pause cycle" }));
    expect(await within(dialog).findByText("Say why it is being paused.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Pause this cycle?" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    dialog = await screen.findByRole("dialog", { name: "Stop this cycle for good?" });
    expect(within(dialog).queryByText(/Say why it is being stopped/)).toBeNull();
  });
});
