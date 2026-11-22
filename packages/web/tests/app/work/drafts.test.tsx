import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { JOINT_3 } from "./fixtures";
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

/** Leave the page and come back, as navigation does. */
async function remount(path: string, hub: Parameters<typeof workHub>[0] = {}) {
  cleanup();
  vi.unstubAllGlobals();
  return renderRoute(path, { hub: workHub(hub) });
}

// Review round 1, finding 7: in-place edits and dialogs kept text only in component state.
describe("work: drafts survive leaving the page", () => {
  it("an in-place task edit keeps its text through an error and a revisit", async () => {
    renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/action/task": { status: 409, body: { error: "changed", code: "conflict" } } }) });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), { target: { value: "Half-written notes" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/changed since you opened it/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("Half-written notes");
    await remount("/work/tasks/102");
    await findPageHeading("Ship the Work area");
    expect(await screen.findByRole("textbox", { name: "Description" })).toHaveValue("Half-written notes");
  });

  it("a parked in-place edit keeps the editor and its text", async () => {
    renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/action/task": PARKED("task_update") }) });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Edit domain" }));
    fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "hobby" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Waiting for approval/)).toBeInTheDocument();
    expect(screen.getByLabelText("Domain")).toHaveValue("hobby");
  });

  it("the goal edit dialog keeps its text", async () => {
    renderRoute("/work/goals/7", { hub: workHub() });
    await findPageHeading("A usable Control Center");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(within(await screen.findByRole("dialog", { name: "Edit goal" })).getByLabelText(/^Statement/), { target: { value: "Draft statement" } });
    await remount("/work/goals/7");
    await findPageHeading("A usable Control Center");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(within(await screen.findByRole("dialog", { name: "Edit goal" })).getByLabelText(/^Statement/)).toHaveValue("Draft statement");
  });

  it("a plan action dialog keeps its text", async () => {
    renderRoute("/work/plans/3", { hub: workHub() });
    await findPageHeading("Roll out Control Center v2");
    fireEvent.click(screen.getByRole("button", { name: "Save a checkpoint" }));
    fireEvent.change(within(await screen.findByRole("dialog", { name: "Save a checkpoint" })).getByLabelText("Note"), { target: { value: "Half a checkpoint" } });
    await remount("/work/plans/3");
    await findPageHeading("Roll out Control Center v2");
    fireEvent.click(screen.getByRole("button", { name: "Save a checkpoint" }));
    expect(within(await screen.findByRole("dialog", { name: "Save a checkpoint" })).getByLabelText("Note")).toHaveValue("Half a checkpoint");
  });

  it("a shared-plan form keeps its text", async () => {
    const member = { "GET /api/whoami": { body: whoamiFixture({ agent: "Desktop_Agent1" }) }, "GET /api/plans/3/joint": { body: JOINT_3 } };
    renderRoute("/work/plans/3", { hub: workHub(member) });
    await findPageHeading("Roll out Control Center v2");
    fireEvent.click(await screen.findByRole("button", { name: "Record a decision" }));
    fireEvent.change(within(await screen.findByRole("dialog", { name: "Record a decision" })).getByLabelText(/^Decision/), { target: { value: "Ship on Friday" } });
    await remount("/work/plans/3", member);
    await findPageHeading("Roll out Control Center v2");
    fireEvent.click(await screen.findByRole("button", { name: "Record a decision" }));
    await waitFor(() => expect(within(screen.getByRole("dialog", { name: "Record a decision" })).getByLabelText(/^Decision/)).toHaveValue("Ship on Friday"));
  });

  // Review round 2 (first-pass 7): the claim-release and pause reasons are drafts too.
  it("the claim-release reason survives leaving the page", async () => {
    renderRoute("/work/tasks/102", { hub: workHub() });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Release claim" }));
    fireEvent.change(within(await screen.findByRole("dialog", { name: "Release the claim?" })).getByLabelText(/Why/), { target: { value: "Stalled for a day" } });
    await remount("/work/tasks/102");
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Release claim" }));
    expect(within(await screen.findByRole("dialog", { name: "Release the claim?" })).getByLabelText(/Why/)).toHaveValue("Stalled for a day");
  });

  it("the pause reason survives leaving the page", async () => {
    renderRoute("/work/tasks/102", { hub: workHub() });
    await findPageHeading("Ship the Work area");
    fireEvent.change(await screen.findByLabelText("Why pause"), { target: { value: "Waiting on design" } });
    await remount("/work/tasks/102");
    await findPageHeading("Ship the Work area");
    expect(await screen.findByLabelText("Why pause")).toHaveValue("Waiting on design");
  });
});
