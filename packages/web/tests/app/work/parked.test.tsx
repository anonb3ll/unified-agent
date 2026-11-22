import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites, listParkedWrites, recordParkedWriteResult, removeParkedWrite } from "../../../src/app/lib/parkedWrites";
import { findPageHeading, renderRoute } from "../support/render";
import { TASK_102 } from "./fixtures";
import { PARKED, workHub } from "./hub";
import { fixtureEpoch } from "../support/epoch";

vi.setConfig({ testTimeout: 60_000 });

afterEach(() => {
  // Unmount first: a form flushes its draft on unmount, which must not outlive the clear below.
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

// Foundation round 2 (6d5cc5e8), review round 1 finding 8: a stored write run from the
// Inbox must not leave the form holding the submitted draft.
describe("work: parked writes and the form", () => {
  it("clears the create-task draft when the stored write is run elsewhere and succeeds", async () => {
    renderRoute("/work/tasks/new", { hub: workHub({ "POST /api/tasks": PARKED("task_add", 40) }) });
    await findPageHeading("New task");
    fireEvent.change(await screen.findByLabelText(/^Title/), { target: { value: "Gated" } });
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    expect(await screen.findByText(/Waiting for approval/)).toBeInTheDocument();
    const [entry] = listParkedWrites(fixtureEpoch());
    expect(entry.approvalId).toBe("40");
    // What a run from the Inbox does on success.
    act(() => {
      recordParkedWriteResult(fixtureEpoch(), { approvalId: "40", kind: "done", message: "The task was created.", at: Date.now(), owner: entry.context });
      removeParkedWrite(fixtureEpoch(), entry.id);
    });
    await waitFor(() => expect(screen.getByLabelText(/^Title/)).toHaveValue(""));
    expect(screen.getByText(/was created from the Inbox/)).toBeInTheDocument();
  });

  it("an outcome-unknown failure says the change may have been made and to check first", async () => {
    renderRoute("/work/tasks/new", {
      hub: workHub({
        "POST /api/tasks": () => {
          throw new TypeError("Failed to fetch");
        },
      }),
    });
    await findPageHeading("New task");
    fireEvent.change(await screen.findByLabelText(/^Title/), { target: { value: "Maybe" } });
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/may have been made/);
    expect(alert).toHaveTextContent(/Check/);
  });
});

// Review round 2, blocker 6: plan create has no idempotency key on the hub, so an
// unconfirmed or parked attempt blocks another create until the person reconciles it.
describe("work: plan create attempts", () => {
  const fill = async () => {
    fireEvent.change(await screen.findByLabelText(/What the plan achieves/), { target: { value: "Launch" } });
    fireEvent.change(screen.getByLabelText("Step 1", { exact: true }), { target: { value: "Prepare" } });
  };

  it("a lost response blocks Create until the person says whether the plan was created", async () => {
    let calls = 0;
    const { hub } = renderRoute("/work/plans/new", {
      hub: workHub({
        "POST /api/action/plan/create": () => {
          calls += 1;
          throw new TypeError("Failed to fetch");
        },
      }),
    });
    await findPageHeading("New plan");
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));
    expect(await screen.findByText(/could not confirm whether the plan was created/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create plan" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Check the plans list" })).toHaveAttribute("href", "/ui/work/plans");
    fireEvent.click(screen.getByRole("button", { name: "It was not created, create it" }));
    expect(screen.getByRole("button", { name: "Create plan" })).toBeEnabled();
    expect(calls).toBe(1);
    expect(hub.calls.filter((c) => c.path === "/api/action/plan/create")).toHaveLength(1);
  });

  // Review round 3, blocker 3: the attempt is recorded before the send.
  it("leaving while the create is still pending leaves an attempt that blocks another create", async () => {
    renderRoute("/work/plans/new", {
      hub: workHub({ "POST /api/action/plan/create": () => new Promise<never>(() => undefined) as never }),
    });
    await findPageHeading("New plan");
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create plan" })).toHaveAttribute("aria-busy", "true"));
    cleanup();
    vi.unstubAllGlobals();
    renderRoute("/work/plans/new", { hub: workHub() });
    await findPageHeading("New plan");
    expect(await screen.findByText(/could not confirm whether the plan was created/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create plan" })).toBeDisabled();
  });

  it("a parked create keeps Create disabled, also after leaving and coming back", async () => {
    renderRoute("/work/plans/new", { hub: workHub({ "POST /api/action/plan/create": PARKED("plan_manage", 40) }) });
    await findPageHeading("New plan");
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));
    expect(await screen.findByText(/Waiting for approval/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create plan" })).toBeDisabled();
    cleanup();
    vi.unstubAllGlobals();
    renderRoute("/work/plans/new", { hub: workHub() });
    await findPageHeading("New plan");
    expect(await screen.findByText(/This plan is waiting for approval/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create plan" })).toBeDisabled();
  });
});

// Review round 2, item 12: notes and comments keep one key per submitted draft and reconcile a stored run.
describe("work: notes and comments", () => {
  it.each([
    ["note", "/api/action/task", "Add a note", "Add note"],
    ["comment", "/api/tasks/102/comments", "Comment", "Post comment"],
  ] as const)("a %s resubmitted after a lost response reuses its idempotency key", async (_kind, path, field, button) => {
    const { hub } = renderRoute("/work/tasks/102", {
      hub: workHub({
        [`POST ${path}`]: () => {
          throw new TypeError("Failed to fetch");
        },
      }),
    });
    await findPageHeading("Ship the Work area");
    fireEvent.change(screen.getByRole("textbox", { name: field }), { target: { value: "Progress" } });
    fireEvent.click(screen.getByRole("button", { name: button }));
    await waitFor(() => expect(hub.calls.filter((c) => c.method === "POST" && c.path === path)).toHaveLength(1));
    await screen.findAllByText(/not known whether the change was made/);
    fireEvent.click(screen.getByRole("button", { name: button }));
    await waitFor(() => expect(hub.calls.filter((c) => c.method === "POST" && c.path === path)).toHaveLength(2));
    const keys = hub.calls.filter((c) => c.method === "POST" && c.path === path).map((c) => (c.body as { idempotency_key: string }).idempotency_key);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  // Review round 3, blocker 4: success clears only the text that was sent; a parked one records that text.
  it.each([
    ["comment", "/api/tasks/102/comments", "Comment", "Post comment", { status: 201, body: { id: 3 } }],
    ["note", "/api/action/task", "Add a note", "Add note", { body: TASK_102.task }],
  ] as const)("text typed while a %s is being sent survives its success", async (_kind, path, field, button, reply) => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    renderRoute("/work/tasks/102", {
      hub: workHub({
        [`POST ${path}`]: async () => {
          await gate;
          return reply;
        },
      }),
    });
    await findPageHeading("Ship the Work area");
    const box = () => screen.getByRole("textbox", { name: field });
    fireEvent.change(box(), { target: { value: "First" } });
    fireEvent.click(screen.getByRole("button", { name: button }));
    await waitFor(() => expect(screen.getByRole("button", { name: button })).toHaveAttribute("aria-busy", "true"));
    fireEvent.change(box(), { target: { value: "Second thought" } });
    await act(async () => {
      release();
      await gate;
    });
    await waitFor(() => expect(screen.getByRole("button", { name: button })).not.toHaveAttribute("aria-busy"));
    expect(box()).toHaveValue("Second thought");
  });

  it("a parked comment records the sent text, so an Inbox run does not clear newer text", async () => {
    renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/tasks/102/comments": PARKED("task_comment", 40) }) });
    await findPageHeading("Ship the Work area");
    fireEvent.change(screen.getByRole("textbox", { name: "Comment" }), { target: { value: "Sent text" } });
    fireEvent.click(screen.getByRole("button", { name: "Post comment" }));
    expect(await screen.findAllByText(/Waiting for approval/)).not.toHaveLength(0);
    fireEvent.change(screen.getByRole("textbox", { name: "Comment" }), { target: { value: "Newer text" } });
    const [entry] = listParkedWrites(fixtureEpoch());
    act(() => {
      recordParkedWriteResult(fixtureEpoch(), { approvalId: "40", kind: "done", message: "Comment posted.", at: Date.now(), owner: entry.context });
      removeParkedWrite(fixtureEpoch(), entry.id);
    });
    await new Promise((r) => setTimeout(r, 300));
    expect(screen.getByRole("textbox", { name: "Comment" })).toHaveValue("Newer text");
  });

  it("a parked comment run from the Inbox clears the comment draft", async () => {
    renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/tasks/102/comments": PARKED("task_comment", 40) }) });
    await findPageHeading("Ship the Work area");
    fireEvent.change(screen.getByRole("textbox", { name: "Comment" }), { target: { value: "Gated comment" } });
    fireEvent.click(screen.getByRole("button", { name: "Post comment" }));
    expect(await screen.findAllByText(/Waiting for approval/)).not.toHaveLength(0);
    const [entry] = listParkedWrites(fixtureEpoch());
    act(() => {
      recordParkedWriteResult(fixtureEpoch(), { approvalId: "40", kind: "done", message: "Comment posted.", at: Date.now(), owner: entry.context });
      removeParkedWrite(fixtureEpoch(), entry.id);
    });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Comment" })).toHaveValue(""));
  });
});
