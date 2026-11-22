import { act, fireEvent, screen, waitFor, within, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import { jointPermissions } from "../../../src/app/work/JointPlanPanel";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { GOAL_7, JOINT_3, PLAN_3, PLAN_3_RESULT, WORK_GRAPH_7 } from "./fixtures";
import { workHub } from "./hub";
import { fixtureEpoch } from "../support/epoch";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  // Unmount first: a form flushes its draft on unmount, which must not outlive the clear below.
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

const posts = (calls: { method: string; path: string; body: unknown }[], path: string) => calls.filter((c) => c.method === "POST" && c.path === path);

describe("work: goals", () => {
  it("lists goals and creates one", async () => {
    const { hub } = renderRoute("/work/goals", { hub: workHub({ "POST /api/goals": { status: 201, body: { goal: { id: 9 } } } }) });
    await findPageHeading("Goals");
    expect(await screen.findByRole("link", { name: "Work area complete" })).toHaveAttribute("href", "/ui/work/goals/8");
    fireEvent.click(screen.getAllByRole("button", { name: "New goal" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "New goal" });
    fireEvent.change(within(dialog).getByLabelText(/^Title/), { target: { value: "Faster reviews" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create goal" }));
    await waitFor(() => expect(posts(hub.calls, "/api/goals")[0]?.body).toEqual({ action: "add", title: "Faster reviews" }));
  });

  // Review round 1, blocker 1: goal writes use #2480's POST /api/action/goal/<action>.
  it("goal page: unlink, edit and close go to the goal routes with the revision read", async () => {
    const goal = { goal: { id: 7, title: "A usable Control Center", status: "open", updated_at: "2026-11-10T12:00:00+00:00" }, edge: "goal_ancestry", meaning: "" };
    const { hub } = renderRoute("/work/goals/7", {
      hub: workHub({
        "POST /api/action/goal/unlink": { body: { task_id: 102, goal_id: 7, edge: "goal_task", unlinked: true } },
        "POST /api/action/goal/edit": { body: goal },
        "POST /api/action/goal/close": { body: { ...goal, goal: { ...goal.goal, status: "achieved" } } },
      }),
    });
    await findPageHeading("A usable Control Center");
    expect(await screen.findByText(/1 of 2 tasks done/)).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Tasks linked to this goal" });
    fireEvent.click(within(list).getByRole("button", { name: "Unlink task #102" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Unlink this task?" })).getByRole("button", { name: "Unlink" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/goal/unlink")[0]?.body).toEqual({ task_id: 102, goal_id: 7 }));

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const edit = await screen.findByRole("dialog", { name: "Edit goal" });
    fireEvent.change(within(edit).getByLabelText(/^Title/), { target: { value: "A usable new Control Center" } });
    fireEvent.click(within(edit).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/action/goal/edit")[0]?.body).toEqual({
        goal_id: 7,
        title: "A usable new Control Center",
        statement: "One person can run the fleet from the browser.",
        expected_revision: 4,
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Close goal" }));
    const close = await screen.findByRole("dialog", { name: "Close this goal?" });
    fireEvent.click(within(close).getByRole("radio", { name: /Abandoned/ }));
    fireEvent.change(within(close).getByLabelText(/Why/), { target: { value: "Superseded" } });
    fireEvent.click(within(close).getByRole("button", { name: "Close goal" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/action/goal/close")[0]?.body).toEqual({ goal_id: 7, status: "abandoned", reason: "Superseded", expected_revision: 4 }),
    );
  });

  // Review round 3, item 7: a goal edit refused with 409 reloads, keeps the text and retries with the new revision.
  it("a goal edit conflict reloads to the new revision and retries with it", async () => {
    let fresh = false;
    let writes = 0;
    const { hub } = renderRoute("/work/goals/7", {
      hub: workHub({
        "GET /api/goals/7": () => ({ body: fresh ? { ...GOAL_7, goal: { ...GOAL_7.goal, revision: 5 } } : GOAL_7 }),
        "POST /api/action/goal/edit": () => (writes++ === 0 ? { status: 409, body: { error: "goal 7 changed", code: "conflict" } } : { body: GOAL_7 }),
      }),
    });
    await findPageHeading("A usable Control Center");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const edit = await screen.findByRole("dialog", { name: "Edit goal" });
    fireEvent.change(within(edit).getByLabelText(/^Title/), { target: { value: "Renamed" } });
    fresh = true;
    fireEvent.click(within(edit).getByRole("button", { name: "Save" }));
    fireEvent.click(await within(edit).findByRole("button", { name: "Reload" }));
    const retry = await within(edit).findByRole("button", { name: "Retry" });
    await waitFor(() => expect(retry).toBeEnabled());
    expect(within(edit).getByLabelText(/^Title/)).toHaveValue("Renamed");
    fireEvent.click(retry);
    await waitFor(() => expect(posts(hub.calls, "/api/action/goal/edit")).toHaveLength(2));
    expect(posts(hub.calls, "/api/action/goal/edit")[1].body).toMatchObject({ goal_id: 7, title: "Renamed", expected_revision: 5 });
  });

  // Review round 1, finding 11: progress follows the work graph's next_cursor.
  it("goal progress counts every page of the work graph", async () => {
    const page = (nodes: unknown[], edges: unknown[], next: string | null) => ({ ...WORK_GRAPH_7, nodes, edges, next_cursor: next });
    const goal = { id: "goal:7", kind: "goal", label: "A usable Control Center", goal_id: 7, status: "open" };
    renderRoute("/work/goals/7", {
      hub: workHub({
        "GET /api/work-graph": ({ url }) => {
          const cursor = new URL(url, "http://x").searchParams.get("cursor");
          return cursor === "p2"
            ? { body: page([{ id: "task:110", kind: "task", label: "Second page task", task_id: 110, status: "done" }], [{ kind: "goal_task", from: "goal:7", to: "task:110" }], null) }
            : { body: page([goal, ...WORK_GRAPH_7.nodes.slice(1)], WORK_GRAPH_7.edges, "p2") };
        },
      }),
    });
    await findPageHeading("A usable Control Center");
    expect(await screen.findByText(/2 of 3 tasks done/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /#110 Second page task/ })).toBeInTheDocument();
  });

  it("goal page links a task through the goal link route", async () => {
    const { hub } = renderRoute("/work/goals/7", { hub: workHub({ "POST /api/action/goal/link": { body: { task_id: 106, goal_id: 7 } } }) });
    await findPageHeading("A usable Control Center");
    const form = await screen.findByRole("form", { name: "Link a task" });
    const box = within(form).getByRole("combobox");
    fireEvent.change(box, { target: { value: "design" } });
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.click(within(form).getByRole("button", { name: "Link task" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/goal/link")[0]?.body).toEqual({ task_id: 106, goal_id: 7 }));
  });
});

describe("work: plans", () => {
  it("lists plans with a row link", async () => {
    renderRoute("/work/plans", { hub: workHub() });
    await findPageHeading("Plans");
    expect(await screen.findByRole("link", { name: "Roll out Control Center v2" })).toHaveAttribute("href", "/ui/work/plans/3");
  });

  it("plan page shows ordered steps, checkpoints, and hands a step to an agent", async () => {
    const { hub } = renderRoute("/work/plans/3", { hub: workHub({ "POST /api/action/plan/update": { body: { step: PLAN_3.steps[2], plan: PLAN_3.plan, next_step_idx: 2 } } }) });
    await findPageHeading("Roll out Control Center v2");
    const steps = screen.getByRole("region", { name: "Steps" });
    // Only the steps (an outputs list nests inside a step).
    expect(within(steps).getAllByRole("listitem").filter((li) => !li.closest("ul")).map((li) => li.textContent)).toEqual([
      expect.stringContaining("Build the foundation"),
      expect.stringContaining("Build the Work area"),
      expect.stringContaining("Cut over"),
      expect.stringContaining("Write the changelog"),
    ]);
    expect(screen.getByText("Checkpoint 1")).toBeInTheDocument();
    fireEvent.click(within(steps).getAllByRole("button", { name: "Hand to an agent" })[2]);
    const dialog = await screen.findByRole("dialog", { name: "Hand this step to an agent" });
    const agent = within(dialog).getByRole("combobox", { name: /Agent/ });
    fireEvent.change(agent, { target: { value: "Desktop_Agent2" } });
    fireEvent.keyDown(agent, { key: "Enter" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Hand over" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/plan/update")[0]?.body).toEqual({ plan_id: "3", step_idx: 2, owner: "Desktop_Agent2", status: "open", expected_step_id: 32 }));
  });

  // Review round 1, blocker 1: plan structure uses #2480's routes with expected_revision.
  it("plan structure: reorder, add, edit, remove and edit the title go to the #2480 routes", async () => {
    const { hub } = renderRoute("/work/plans/3", {
      hub: workHub({
        "POST /api/action/plan/reorder": { body: PLAN_3_RESULT },
        "POST /api/action/plan/step-add": { body: { ...PLAN_3_RESULT, step: PLAN_3.steps[3] } },
        "POST /api/action/plan/step-edit": { body: { ...PLAN_3_RESULT, step: PLAN_3.steps[3] } },
        "POST /api/action/plan/step-remove": { body: { ...PLAN_3_RESULT, removed: PLAN_3.steps[3] } },
        "POST /api/action/plan/edit": { body: PLAN_3_RESULT },
      }),
    });
    await findPageHeading("Roll out Control Center v2");
    const rev = 12;

    fireEvent.click(screen.getByRole("button", { name: "Actions for step 4" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Move up" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/plan/reorder")[0]?.body).toEqual({ plan_id: "3", order: [0, 1, 3, 2], expected_revision: rev }));

    // A claimed or done step cannot be renumbered (control-center-api.md), so it cannot move.
    fireEvent.click(screen.getByRole("button", { name: "Actions for step 2" }));
    expect(await screen.findByRole("menuitem", { name: "Move down" })).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

    fireEvent.click(screen.getAllByRole("button", { name: "Add a step" })[0]);
    let dialog = await screen.findByRole("dialog", { name: "Add a step" });
    fireEvent.change(within(dialog).getByLabelText("Step"), { target: { value: "Announce it" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/plan/step-add")[0]?.body).toEqual({ plan_id: "3", description: "Announce it", expected_revision: rev }));

    fireEvent.click(screen.getByRole("button", { name: "Actions for step 4" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Edit the text" }));
    dialog = await screen.findByRole("dialog", { name: "Edit the step" });
    fireEvent.change(within(dialog).getByLabelText("Step"), { target: { value: "Write the release notes" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/plan/step-edit")[0]?.body).toEqual({ plan_id: "3", step_idx: 3, description: "Write the release notes", expected_revision: rev, expected_step_id: 33 }));

    fireEvent.click(screen.getByRole("button", { name: "Actions for step 4" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remove" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Remove this step?" })).getByRole("button", { name: "Remove step" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/plan/step-remove")[0]?.body).toEqual({ plan_id: "3", step_idx: 3, expected_revision: rev, expected_step_id: 33 }));

    fireEvent.click(screen.getByRole("button", { name: "More plan actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Edit title and description" }));
    dialog = await screen.findByRole("dialog", { name: "Edit the plan" });
    fireEvent.change(within(dialog).getByLabelText(/^Title/), { target: { value: "Roll out v2" } });
    fireEvent.change(within(dialog).getByLabelText("Description"), { target: { value: "Every area, then cutover." } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/action/plan/edit")[0]?.body).toEqual({ plan_id: "3", title: "Roll out v2", description: "Every area, then cutover.", expected_revision: rev }),
    );
  });

  // Review round 2, blocker 3: step writes carry the step's immutable id; a dialog follows its step through a reorder.
  it("step writes send expected_step_id, and an open dialog follows its step after a reorder", async () => {
    let reorderNow = false;
    const reordered = {
      ...PLAN_3,
      plan: { ...PLAN_3.plan, revision: 13 },
      steps: [PLAN_3.steps[0], PLAN_3.steps[1], { ...PLAN_3.steps[3], step_idx: 2 }, { ...PLAN_3.steps[2], step_idx: 3 }],
    };
    const { hub, appClient } = renderRoute("/work/plans/3", {
      hub: workHub({
        "GET /api/plans/3": () => ({ body: reorderNow ? reordered : PLAN_3 }),
        "POST /api/action/plan/update": { body: { step: PLAN_3.steps[2], plan: PLAN_3.plan, next_step_idx: 2 } },
        "POST /api/action/plan/retry": { body: { step: PLAN_3.steps[2], retries: 1, plan: PLAN_3.plan } },
      }),
    });
    await findPageHeading("Roll out Control Center v2");
    fireEvent.click(screen.getByRole("button", { name: "Set the status of step 3" }));
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "Blocked" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/plan/update")[0]?.body).toEqual({ plan_id: "3", step_idx: 2, status: "blocked", expected_step_id: 32 }));

    fireEvent.click(screen.getByRole("button", { name: "Actions for step 3" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Record a failed attempt" }));
    const dialog = await screen.findByRole("dialog", { name: "Record a failed attempt" });
    fireEvent.change(within(dialog).getByLabelText("What went wrong"), { target: { value: "Flaky" } });
    // Someone reorders: "Cut over" (id 32) moves from index 2 to 3.
    reorderNow = true;
    await act(async () => {
      await appClient().invalidateQueries({ queryKey: ["work", "plan", "3"] });
    });
    await waitFor(() => expect(screen.getByRole("region", { name: "Steps" })).toHaveTextContent(/Write the changelog.*Cut over/));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Record a failed attempt" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/plan/retry")[0]?.body).toEqual({ plan_id: "3", step_idx: 3, reason: "Flaky", expected_step_id: 32 }));
  });

  // Review round 2, item 5: a typed 409 reloads, keeps the edit, re-checks the action and retries.
  it("a plan conflict keeps the dialog and text, reloads to the new revision and retries", async () => {
    let fresh = false;
    let writes = 0;
    const { hub } = renderRoute("/work/plans/3", {
      hub: workHub({
        "GET /api/plans/3": () => ({ body: fresh ? { ...PLAN_3, plan: { ...PLAN_3.plan, revision: 15 } } : PLAN_3 }),
        "POST /api/action/plan/step-edit": () =>
          writes++ === 0 ? { status: 409, body: { error: "plan '3' changed since it was read (revision 15, expected 12); reload it and try again", code: "conflict" } } : { body: PLAN_3_RESULT },
        "POST /api/action/plan/step-remove": { status: 409, body: { error: "step 3 changed", code: "conflict" } },
      }),
    });
    await findPageHeading("Roll out Control Center v2");
    fireEvent.click(screen.getByRole("button", { name: "Actions for step 4" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Edit the text" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit the step" });
    fireEvent.change(within(dialog).getByLabelText("Step"), { target: { value: "Write the release notes" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByText(/changed since you opened it/i)).toBeInTheDocument();
    fresh = true;
    fireEvent.click(within(dialog).getByRole("button", { name: "Reload" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Retry" })).toBeEnabled());
    expect(within(dialog).getByLabelText("Step")).toHaveValue("Write the release notes");
    fireEvent.click(within(dialog).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/plan/step-edit")).toHaveLength(2));
    expect(posts(hub.calls, "/api/action/plan/step-edit")[1].body).toMatchObject({ expected_revision: 15, description: "Write the release notes" });

    fireEvent.click(screen.getByRole("button", { name: "Actions for step 4" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remove" }));
    const remove = await screen.findByRole("dialog", { name: "Remove this step?" });
    fireEvent.click(within(remove).getByRole("button", { name: "Remove step" }));
    expect(await within(remove).findByText(/changed since you opened it/i)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Remove this step?" })).toBeInTheDocument();
  });

  // Hub #2480: a workflow-bound plan (goal "governed workflow <run ULID>") refuses title, description
  // and step-structure edits, so those controls are hidden and the page says why.
  it("a workflow-bound plan hides structure and title edits and explains", async () => {
    const bound = "governed workflow 01ARZ3NDEKTSV4RRFFQ69G5FAV";
    renderRoute("/work/plans/3", {
      hub: workHub({ "GET /api/plans/3": { body: { ...PLAN_3, plan: { ...PLAN_3.plan, goal: bound, title: bound, description: "" } } } }),
    });
    await findPageHeading(bound);
    expect(screen.getByText(/belongs to a workflow run/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add a step" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "More plan actions" }));
    expect(screen.queryByRole("menuitem", { name: "Edit title and description" })).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Actions for step 3" }));
    expect(screen.queryByRole("menuitem", { name: "Edit the text" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Move up" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Remove" })).not.toBeInTheDocument();
  });

  // Review round 2, item 16: a step's outputs (from plan_handoff) are shown.
  it("shows a step's outputs", async () => {
    renderRoute("/work/plans/3", { hub: workHub() });
    await findPageHeading("Roll out Control Center v2");
    const outputs = screen.getByRole("list", { name: "Outputs of step 1" });
    expect(outputs).toHaveTextContent("summary");
    expect(outputs).toHaveTextContent("Foundation merged");
    expect(within(outputs).getByRole("link", { name: "https://github.com/example/repo/pull/1503" })).toBeInTheDocument();
  });

  it("creates a plan", async () => {
    const { hub, router } = renderRoute("/work/plans/new", { hub: workHub({ "POST /api/action/plan/create": { body: { plan_id: "11", steps: [] } } }) });
    await findPageHeading("New plan");
    fireEvent.change(await screen.findByLabelText(/What the plan achieves/), { target: { value: "Launch" } });
    fireEvent.change(screen.getByLabelText("Step 1"), { target: { value: "Prepare" } });
    fireEvent.click(screen.getByRole("button", { name: "Add a step" }));
    fireEvent.change(screen.getByLabelText("Step 2"), { target: { value: "Go" } });
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/ui/work/plans/11"));
    expect(posts(hub.calls, "/api/action/plan/create")[0].body).toEqual({ goal: "Launch", steps: ["Prepare", "Go"] });
  });

  it("shared plan actions are disabled with the reason for a non-member, the operator included", async () => {
    renderRoute("/work/plans/3", { hub: workHub() });
    await findPageHeading("Roll out Control Center v2");
    const panel = screen.getByRole("region", { name: "Shared plan" });
    expect(await within(panel).findByRole("note")).toHaveTextContent(/operator is not a member of org unit web-office/);
    expect(within(panel).getByRole("button", { name: "Propose new steps" })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Record a decision" })).toBeDisabled();
  });

  it("a member can propose with the revision it read", async () => {
    const { hub } = renderRoute("/work/plans/3", {
      hub: workHub({
        "GET /api/whoami": { body: whoamiFixture({ agent: "Desktop_Agent1" }) },
        "GET /api/plans/3/joint": { body: JOINT_3 },
        "POST /api/action/plan/joint/propose": { body: { ...JOINT_3, revision: 3 } },
      }),
    });
    await findPageHeading("Roll out Control Center v2");
    const panel = screen.getByRole("region", { name: "Shared plan" });
    fireEvent.click(await within(panel).findByRole("button", { name: "Propose new steps" }));
    const dialog = await screen.findByRole("dialog", { name: "Propose new steps" });
    fireEvent.change(within(dialog).getByLabelText(/Comment number/), { target: { value: "12" } });
    fireEvent.change(within(dialog).getByLabelText(/New steps/), { target: { value: "One\nTwo" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/action/plan/joint/propose")[0]?.body).toEqual({ plan_id: "3", expected_revision: 2, comment_id: 12, steps: ["One", "Two"] }),
    );
  });

  it("a plan that is not shared offers to share it", async () => {
    renderRoute("/work/plans/3", { hub: workHub({ "GET /api/plans/3/joint": { status: 400, body: { error: "plan 3 is not a joint plan" } } }) });
    await findPageHeading("Roll out Control Center v2");
    expect(await screen.findByText("This plan is not shared with a team yet.")).toBeInTheDocument();
  });

  it("permission helper reads the hub's caller entry", () => {
    expect(jointPermissions({ ...JOINT_3, caller: null }, "Desktop_Agent1").allowed).toBe(false);
    expect(jointPermissions(JOINT_3, "Desktop_Agent1").allowed).toBe(true);
    const { caller: _caller, ...withoutCaller } = JOINT_3;
    void _caller;
    expect(jointPermissions(withoutCaller, "Desktop_Agent2").allowed).toBe(true);
    expect(jointPermissions(JOINT_3, null).allowed).toBe(false);
  });
});
