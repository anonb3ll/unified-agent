import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites, listParkedWrites } from "../../../src/app/lib/parkedWrites";
import type { MockHandler } from "../support/fetchMock";
import { findPageHeading, renderRoute } from "../support/render";
import { forVitest, REALISTIC, ROUTINES } from "./hubData";
import { routineRunSettle } from "../../../src/app/runs/RoutinesPage";
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

function open(overrides: Record<string, MockHandler> = {}) {
  return renderRoute("/runs/routines", { hub: { ...forVitest(REALISTIC), ...overrides } });
}

async function card(title: string) {
  return (await screen.findByRole("heading", { name: title })).closest("article") as HTMLElement;
}

describe("/runs/routines", () => {
  it("lists routines with their schedule, next run and history, and explains what a routine is", async () => {
    open();
    await findPageHeading("Routines");
    expect(screen.getByText(/A routine creates a task on a schedule/)).toBeInTheDocument();
    const weekly = await card("Weekly dependency check");
    expect(weekly).toHaveTextContent("0 9 * * 1 (UTC)");
    expect(weekly).toHaveTextContent(/Next run: \d+ \w+/);
    const history = within(weekly).getByRole("table", { name: /Recent runs of Weekly dependency check/ });
    expect(history).toHaveTextContent("Skipped: the previous run's task was still open");
    const triage = await card("Triage the inbox");
    expect(triage).toHaveTextContent("Next run: Paused");
    expect(within(triage).getByRole("button", { name: "Run now" })).toBeDisabled();
    expect(triage).toHaveTextContent("Resume the routine to run it.");
  });

  it("Run now sends one Idempotency-Key per attempt and explains a skipped answer", async () => {
    const keys: string[] = [];
    const { hub } = open({
      "POST /api/action/routine/run": ({ headers, body }) => {
        keys.push(headers["idempotency-key"]);
        expect(body).toEqual({ routine_id: 7 });
        return { body: keys.length === 1 ? { status: "skipped", task_id: null } : { status: "queued", task_id: 88 } };
      },
    });
    const weekly = await card("Weekly dependency check");
    fireEvent.click(within(weekly).getByRole("button", { name: "Run now" }));
    const confirm = await screen.findByRole("dialog", { name: "Run this routine now?" });
    expect(confirm).toHaveTextContent("its schedule is unchanged");
    fireEvent.click(within(confirm).getByRole("button", { name: "Run now" }));
    expect(await within(weekly).findByText(/Nothing was started: an earlier run of this routine is still active/)).toBeInTheDocument();
    fireEvent.click(within(weekly).getByRole("button", { name: "Run now" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Run this routine now?" })).getByRole("button", { name: "Run now" }));
    expect(await within(weekly).findByText(/queued as task #88/)).toBeInTheDocument();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toMatch(/^cc2-routine-run-/);
    expect(keys[0]).not.toBe(keys[1]);
    expect(hub.calls.filter((c) => c.path === "/api/action/routine/run")).toHaveLength(2);
  });

  it("after a lost answer, asking again sends the same key (safe), and a new start needs the duplicate warning", async () => {
    const keys: string[] = [];
    open({
      "POST /api/action/routine/run": ({ headers }) => {
        keys.push(headers["idempotency-key"]);
        if (keys.length === 1) throw new TypeError("connection reset");
        return { body: { status: "created", task_id: 90 } };
      },
    });
    const weekly = await card("Weekly dependency check");
    fireEvent.click(within(weekly).getByRole("button", { name: "Run now" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Run this routine now?" })).getByRole("button", { name: "Run now" }));
    const check = await within(weekly).findByRole("region", { name: "Start not confirmed" });
    expect(check).toHaveTextContent("We could not confirm whether this started.");
    expect(within(weekly).getByRole("button", { name: "Run now" })).toBeDisabled();
    fireEvent.click(within(check).getByRole("button", { name: "Ask the hub with the same key" }));
    expect(await within(weekly).findByText(/created task #90/)).toBeInTheDocument();
    expect(keys[1]).toBe(keys[0]);
  });

  it("create validates the schedule and shows the next three run times before saving", async () => {
    let sent: unknown = null;
    open({
      "POST /api/action/routine/add": ({ body }) => {
        sent = body;
        return { body: { id: 9 } };
      },
    });
    await card("Weekly dependency check");
    fireEvent.click(screen.getAllByRole("button", { name: "New routine" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "New routine" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Name/ }), { target: { value: "Nightly backup check" } });
    const schedule = within(dialog).getByRole("textbox", { name: /Schedule/ });
    fireEvent.change(schedule, { target: { value: "0 25 * * *" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create routine" }));
    expect(await within(dialog).findByText("The hour must be between 0 and 23.")).toBeInTheDocument();
    await waitFor(() => expect(schedule).toHaveFocus());
    expect(sent).toBeNull();
    fireEvent.change(schedule, { target: { value: "30  2 * * *" } });
    const preview = await within(dialog).findByText("The next three runs would be:");
    expect(preview.parentElement?.querySelectorAll("li")).toHaveLength(3);
    expect(preview.parentElement).toHaveTextContent(/02:30 UTC/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Create routine" }));
    await waitFor(() => expect(sent).toEqual({ title: "Nightly backup check", trigger: "cron", schedule: "30 2 * * *", concurrency: "skip", catch_up: "last", activity_gate: "always" }));
  });

  it("pause asks first and says what is kept", async () => {
    let sent: unknown = null;
    open({
      "POST /api/action/routine/pause": ({ body }) => {
        sent = body;
        return { body: { id: 7, paused: 1 } };
      },
    });
    const weekly = await card("Weekly dependency check");
    fireEvent.click(within(weekly).getByRole("button", { name: "Pause" }));
    const confirm = await screen.findByRole("dialog", { name: "Pause this routine?" });
    expect(confirm).toHaveTextContent("Tasks it already created are kept");
    fireEvent.click(within(confirm).getByRole("button", { name: "Pause routine" }));
    await waitFor(() => expect(sent).toEqual({ routine_id: 7 }));
  });

  it("a hub without the routines route says it is not available yet", async () => {
    open({ "GET /api/routines": { status: 404, body: { error: "Not Found" } } });
    expect(await screen.findByRole("heading", { name: "Not available on this hub yet" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "New routine" })[0]).toBeDisabled();
  });

  it("says when routines are switched off on the hub", async () => {
    open({ "GET /api/routines": { body: { ...ROUTINES, routines_enabled: false } } });
    expect(await screen.findByRole("note")).toHaveTextContent("Routines are switched off on this hub.");
    const weekly = await card("Weekly dependency check");
    expect(within(weekly).getByRole("button", { name: "Run now" })).toBeDisabled();
  });

  async function runNow(weekly: HTMLElement) {
    fireEvent.click(within(weekly).getByRole("button", { name: "Run now" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Run this routine now?" })).getByRole("button", { name: "Run now" }));
  }

  it("a 409 in_flight keeps the attempt and its key; asking again sends the same key (item 2)", async () => {
    const keys: string[] = [];
    open({
      "POST /api/action/routine/run": ({ headers }) => {
        keys.push(headers["idempotency-key"]);
        return keys.length === 1 ? { status: 409, body: { error: "a request with this key is still running", code: "in_flight" } } : { body: { status: "created", task_id: 91 } };
      },
    });
    const weekly = await card("Weekly dependency check");
    await runNow(weekly);
    const check = await within(weekly).findByRole("region", { name: "Start not confirmed" });
    expect(within(weekly).getByRole("button", { name: "Run now" })).toBeDisabled();
    fireEvent.click(within(check).getByRole("button", { name: "Ask the hub with the same key" }));
    expect(await within(weekly).findByText(/created task #91/)).toBeInTheDocument();
    expect(keys[1]).toBe(keys[0]);
  });

  it("explains each 409 code, and only a paused or scheduler-only refusal ends the attempt (item 2)", () => {
    const err = (code: string | undefined, detail: string) => ({ kind: "error" as const, status: 409, message: "x", detail, ...(code ? { code } : {}) });
    expect(routineRunSettle(err("in_flight", "busy"))).toMatchObject({ record: "unknown", note: expect.stringMatching(/still running the first request/) });
    expect(routineRunSettle(err("conflict", "x"))).toMatchObject({ record: "unknown", note: expect.stringMatching(/already used for a different request/) });
    expect(routineRunSettle(err("ambiguous_receipt", "x"))).toMatchObject({ record: "unknown", note: expect.stringMatching(/new start needs a new key/) });
    expect(routineRunSettle(err(undefined, "routine is paused"))).toEqual({ record: "clear" });
    expect(routineRunSettle(err(undefined, "cron routines are fired by the scheduler"))).toEqual({ record: "clear" });
    expect(routineRunSettle(err(undefined, "something else"))).toMatchObject({ record: "unknown" });
  });

  it("a fire parked for approval keeps one key; sending it again uses that key (item 4)", async () => {
    const keys: string[] = [];
    open({
      "POST /api/action/routine/run": ({ headers }) => {
        keys.push(headers["idempotency-key"]);
        return keys.length === 1
          ? { status: 202, body: { status: "approval_pending", approval_id: 61, code: "approval_required", tool: "routine_fire" } }
          : { body: { status: "created", task_id: 92 } };
      },
      "GET /api/approvals/61": { body: { id: 61, status: "approved", requested_by: "someone-else" } },
    });
    const weekly = await card("Weekly dependency check");
    await runNow(weekly);
    // The parked fire is not stored for a replay elsewhere: the only way to send it again is this page's guarded dispatch (item 2).
    expect(await within(weekly).findByRole("region", { name: "Approval status" })).toBeInTheDocument();
    expect(within(weekly).queryByRole("button", { name: "Run it now" })).toBeNull();
    expect(listParkedWrites(fixtureEpoch())).toHaveLength(0);
    fireEvent.click(await within(weekly).findByRole("button", { name: "Send it again with the same key" }));
    expect(await within(weekly).findByText(/created task #92/)).toBeInTheDocument();
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
    expect(within(weekly).getByRole("button", { name: "Run now" })).toBeEnabled();
  });

  it("a lost create is held until a matching routine is picked or a duplicate is confirmed (item 5)", async () => {
    let posts = 0;
    open({
      "POST /api/action/routine/add": () => {
        posts += 1;
        throw new TypeError("connection reset");
      },
    });
    await card("Weekly dependency check");
    fireEvent.click(screen.getAllByRole("button", { name: "New routine" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "New routine" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Name/ }), { target: { value: "Nightly check" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Schedule/ }), { target: { value: "0 2 * * *" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create routine" }));
    const check = await within(dialog).findByRole("region", { name: "Start not confirmed" });
    expect(await within(check).findByText(/Inconclusive/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Create routine" }));
    expect(posts).toBe(1);
  });

  it("notes typed while a create is in flight survive its answer (sweep b)", async () => {
    let release: () => void = () => {};
    open({ "POST /api/action/routine/add": () => new Promise((r) => (release = () => r({ body: { id: 9 } }))) });
    await card("Weekly dependency check");
    fireEvent.click(screen.getAllByRole("button", { name: "New routine" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "New routine" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Name/ }), { target: { value: "Nightly check" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Schedule/ }), { target: { value: "0 2 * * *" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create routine" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Create routine" })).toBeDisabled());
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Notes/ }), { target: { value: "added while sending" } });
    release();
    await waitFor(() => expect(screen.queryByRole("region", { name: "Start not confirmed" })).toBeNull());
    // The dialog stays open with the newer text rather than clearing it.
    expect(within(screen.getByRole("dialog", { name: "New routine" })).getByRole("textbox", { name: /Notes/ })).toHaveValue("added while sending");
  });
});
