import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import type { MockHandler } from "../support/fetchMock";
import { findPageHeading, renderRoute } from "../support/render";
import { forVitest, REALISTIC, RUN_ID } from "./hubData";
import { foldAnswer, hopNodes, readKnownRun } from "../../../src/app/runs/GraphsPage";
import { installLocks } from "./locks";
import { fixtureEpoch } from "../support/epoch";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  installLocks();
  clearParkedWrites(fixtureEpoch());
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function open(overrides: Record<string, MockHandler> = {}, path = "/runs/graphs") {
  const r = renderRoute(path, { hub: { ...forVitest(REALISTIC), ...overrides } });
  await findPageHeading("Graphs");
  return r;
}

async function fillStart() {
  const form = await screen.findByRole("form", { name: "Start a graph run" });
  fireEvent.change(within(form).getByRole("textbox", { name: /Task number/ }), { target: { value: "42" } });
  fireEvent.change(within(form).getByRole("textbox", { name: /What should it do/ }), { target: { value: "Fix the login form" } });
  await waitFor(() => expect(within(form).getByRole("button", { name: "Start graph run" })).toBeEnabled());
  fireEvent.click(within(form).getByRole("button", { name: "Start graph run" }));
  return form;
}

/** A native graph approval (webui/cc_runs.langgraph_run 202). */
const PAUSED = {
  status: "approval_pending",
  code: "approval_required",
  approval_id: 55,
  run_status: "interrupted",
  run_id: RUN_ID,
  thread_id: `ua-lg:${RUN_ID}`,
  graph_name: "governed_workflow",
  graph_version: 1,
  claimed_by: "operator",
  pending_hop: "build",
  output: null,
  interrupted_by: { run_id: RUN_ID, task_id: 42, claimed_by: "operator", node_key: "build", tool_name: "task_update", approval_id: 55, hop: "build" },
  reason: null,
  tool: "langgraph_workflow_run",
  session_id: "01JD7ZSESSION0000000000001",
};

describe("/runs/graphs", { timeout: 40_000 }, () => {
  it("explains checkpoints, lists recent runs and offers the research engine only as not available yet", async () => {
    await open();
    expect(screen.getByText(/A checkpoint is the run's saved state after a step/)).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: RUN_ID })).toHaveAttribute("href", `/ui/runs/graphs?run=${RUN_ID}`);
    const engine = screen.getByRole("combobox", { name: /Engine/ });
    expect(within(engine).getByRole("option", { name: "Research (not available on this hub yet)" })).toBeDisabled();
  });

  it("an approval pause is shown as waiting with the Inbox link and Continue, not as an error; Continue sends the run id", async () => {
    const sent: unknown[] = [];
    await open({
      "POST /api/action/langgraph/run": ({ body }) => {
        sent.push(body);
        return sent.length === 1
          ? { status: 202, body: PAUSED }
          : { body: { ...PAUSED, status: "completed", code: undefined, approval_id: undefined, pending_hop: null, interrupted_by: null, output: { task_id: 42, build: {}, merge: {} } } };
      },
    });
    await fillStart();
    const watch = await screen.findByRole("region", { name: `Graph run ${RUN_ID}` }).catch(() => screen.findByRole("heading", { name: `Graph run ${RUN_ID}` }));
    expect(watch).toBeTruthy();
    expect(await screen.findByText(/Waiting for approval before the build step/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the approval in the Inbox" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=55");
    expect(screen.queryByRole("alert")).toBeNull();
    // The canvas's table alternative shows each step's state.
    expect(screen.getAllByText(/Waiting for approval/).length).toBeGreaterThan(0);
    const cont = screen.getByRole("form", { name: `Continue graph run ${RUN_ID}` });
    expect(within(cont).getByRole("textbox", { name: /Task number/ })).toHaveValue("42");
    fireEvent.click(within(cont).getByRole("button", { name: "Continue" }));
    expect(await screen.findByText(/^Completed: every step ran/)).toBeInTheDocument();
    expect(sent[0]).toEqual({ task_id: 42, query: "Fix the login form", engine: "workflow" });
    expect(sent[1]).toEqual({ task_id: 42, query: "Fix the login form", run_id: RUN_ID, engine: "workflow" });
  });

  it("a gate that parked the call before any run started goes through the shared parked-write notice", async () => {
    await open({ "POST /api/action/langgraph/run": { status: 202, body: { status: "approval_pending", approval_id: 77, code: "approval_required", tool: "langgraph_workflow_run" } } });
    await fillStart();
    const notice = await screen.findByRole("region", { name: "Approval status" });
    expect(notice).toHaveTextContent("Waiting for approval.");
    expect(notice).toHaveTextContent("submit this form again");
  });

  it("a refusal (task not claimed) is a plain message, raw text in the detail", async () => {
    await open({ "POST /api/action/langgraph/run": { status: 400, body: { error: "governed workflow requires a claimed task in doing status" } } });
    await fillStart();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/^The hub did not accept this/);
  });

  it("a lost answer holds a new start until the person decides; matching sessions are candidates", async () => {
    let posts = 0;
    await open({
      "POST /api/action/langgraph/run": () => {
        posts += 1;
        throw new TypeError("network down");
      },
      "GET /api/runs": {
        body: {
          runs: [{ run_id: "01JD7ZSESSION0000000000009", kind: "session", task_id: 42, agent: "operator", status: "completed", outcome: "completed", started_at: new Date().toISOString(), ended_at: null, session_kind: "control_center", links: {} }],
          count: 1,
          truncated: false,
          next_cursor: null,
          limit: 50,
        },
      },
    });
    const form = await fillStart();
    const check = await screen.findByRole("region", { name: "Start not confirmed" });
    expect(await within(check).findByText(/Request 01JD7ZSESSION0000000000009 for task #42/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Start graph run" })).toBeDisabled();
    expect(posts).toBe(1);
  });

  it("a denied merge keeps the steps already confirmed and says earlier effects stay (item 11)", () => {
    const base = { runId: RUN_ID, taskId: 42, query: "q", approvalId: null, at: 1 };
    const paused = foldAnswer(null, { ...base, result: { ...PAUSED, pending_hop: "merge", interrupted_by: { ...PAUSED.interrupted_by, hop: "merge" } } });
    expect(paused.confirmedHops).toEqual(["plan", "build", "verify"]);
    const denied = foldAnswer(paused, { ...base, at: 2, result: { status: "denied", run_id: RUN_ID, pending_hop: null, output: null, interrupted_by: null, reason: "merge approval denied" } });
    const states = hopNodes(denied).map((n) => `${n.id}:${n.description?.split(". ")[1]}`);
    expect(states).toEqual(["plan:Done.", "build:Done.", "verify:Done.", "merge:Approval denied."]);
    // With no history, nothing is invented.
    const cold = foldAnswer(null, { ...base, result: { status: "denied", run_id: RUN_ID, pending_hop: null, output: null, interrupted_by: null, reason: null } });
    expect(hopNodes(cold).every((n) => n.description?.endsWith("Not known."))).toBe(true);
  });

  it("the canvas is labelled as the last answer with its time, and says when checkpoints show the run moved on (item 10)", async () => {
    let head = "1ef-0002";
    let runReads = 0;
    const { appClient } = await open({
      "POST /api/action/langgraph/run": { status: 202, body: PAUSED },
      "GET /api/langgraph/checkpoints": async ({ url }) => {
        // The run's first read is slow, as on a loaded machine (task #2519):
        // the page records its baseline only once that read lands.
        if (url.includes("run_id") && ++runReads === 1) await new Promise((r) => setTimeout(r, 100));
        return { body: { checkpoint_db: "x", exists: true, count: 1, checkpoints: [{ thread_id: `ua-lg:${RUN_ID}`, checkpoint_ns: "", checkpoint_id: head, parent_checkpoint_id: null, type: "msgpack" }] } };
      },
    });
    await fillStart();
    expect(await screen.findByText(/From the hub's last answer at .*This is not live progress/)).toBeInTheDocument();
    // Settled: the first read after the answer is recorded as the baseline.
    // Moving the head before that (the old fixed 50 ms wait) made the moved
    // head the baseline, so "moved on" never showed (task #2519).
    await waitFor(() => expect(readKnownRun(fixtureEpoch(), "operator", RUN_ID)?.checkpointHead).toBe("1ef-0002"));
    expect(screen.queryByText(/saved a new checkpoint since that answer/)).toBeNull();
    head = "1ef-0003";
    await appClient().refetchQueries({ queryKey: ["cc2", "runs", "checkpoints", RUN_ID] });
    expect(await screen.findByText(/saved a new checkpoint since that answer/)).toBeInTheDocument();
  });

  it("instructions typed while a start is in flight survive its answer (sweep b)", async () => {
    let release: () => void = () => {};
    await open({ "POST /api/action/langgraph/run": () => new Promise((r) => (release = () => r({ status: 202, body: PAUSED }))) });
    const form = await fillStart();
    await waitFor(() => expect(within(form).getByRole("button", { name: "Start graph run" })).toBeDisabled());
    const query = within(form).getByRole("textbox", { name: /What should it do/ });
    fireEvent.change(query, { target: { value: "Fix the login form, then the signup form" } });
    release();
    expect(await screen.findByText(/Waiting for approval before the build step/)).toBeInTheDocument();
    expect(query).toHaveValue("Fix the login form, then the signup form");
  });
});
