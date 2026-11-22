import { expect as baseExpect, test, type Page } from "@playwright/test";
import { TASK_102 } from "./work/fixtures";
import { mockWork } from "./work/pw";

/**
 * Work area flows (spec §9.2), the hub mocked with `page.route`. Each flow is
 * driven by the keyboard at least once.
 */
test.use({ viewport: { width: 1440, height: 900 } });
// A shared 4-core desktop: give the lazy area chunks time to load.
const expect = baseExpect.configure({ timeout: 15_000 });

const h1 = (page: Page, name: string | RegExp) => page.getByRole("main").getByRole("heading", { level: 1, name });
const PARKED = (tool: string, id = 77) => ({ status: 202, json: { approval_id: id, code: "approval_required", status: "pending", tool } });

test("create a task from the keyboard: c, type, Enter", async ({ page }) => {
  const { writes } = await mockWork(page, { "POST /api/tasks": { status: 201, json: { id: 102, title: "Ship the Work area" } } });
  await page.goto("/ui/work");
  await expect(h1(page, "Work")).toBeVisible();
  await page.keyboard.press("c");
  await expect(h1(page, "New task")).toBeVisible();
  const title = page.getByLabel(/^Title/);
  await title.focus();
  await page.keyboard.type("Ship the Work area");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/ui\/work\/tasks\/102$/);
  await expect(h1(page, "Ship the Work area")).toBeVisible();
  expect(writes.find((w) => w.path === "/api/tasks")?.body).toMatchObject({ title: "Ship the Work area", priority: "med" });
});

test("filters live in the URL and survive a reload; views switch", async ({ page }) => {
  await mockWork(page);
  await page.goto("/ui/work");
  const filters = page.getByRole("group", { name: "Task filters" });
  await filters.getByLabel("Status").selectOption("doing");
  // The box is controlled by the URL: it shows checked once the router commits the navigation.
  const mine = page.getByRole("checkbox", { name: "Mine" });
  await mine.click();
  await expect(mine).toBeChecked();
  await expect(page).toHaveURL(/status=doing/);
  await expect(page).toHaveURL(/mine=1/);
  await page.reload();
  await expect(filters.getByLabel("Status")).toHaveValue("doing");
  await expect(page.getByText("No tasks match these filters")).toBeVisible();
  await page.getByRole("button", { name: "Clear all" }).click();
  await expect(page).not.toHaveURL(/mine=1/);
  await page.getByRole("button", { name: "Board" }).click();
  await expect(page).toHaveURL(/view=board/);
  await expect(page.getByRole("heading", { name: /Doing/, level: 2 })).toBeVisible();
  await page.getByRole("button", { name: "Tree" }).click();
  await expect(page.getByRole("navigation", { name: "Task tree" })).toBeVisible();
});

test("board: keyboard Move to opens a confirm, and the move is sent once", async ({ page }) => {
  const { writes } = await mockWork(page, { "POST /api/action/task": { json: { id: 101, status: "doing" } } });
  await page.goto("/ui/work?view=board");
  const menu = page.getByRole("button", { name: "Move task #101 to" });
  await menu.focus();
  for (const key of ["ArrowLeft", "ArrowRight"]) await page.keyboard.press(key);
  expect(writes).toHaveLength(0);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "Doing" })).toBeFocused();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Move this task?" });
  await expect(dialog).toBeVisible();
  expect(writes).toHaveLength(0);
  await page.keyboard.press("Enter");
  await expect(dialog).toBeHidden();
  await expect.poll(() => writes.filter((w) => w.path === "/api/action/task")).toHaveLength(1);
  expect(writes[0].body).toEqual({ id: 101, status: "doing", expected_updated_at: "2026-11-09T08:00:00+00:00" });
});

test("board: dragging a card to a column moves it", async ({ page }) => {
  const { writes } = await mockWork(page, { "POST /api/action/task": { json: { id: 101, status: "blocked" } } });
  await page.goto("/ui/work?view=board");
  await page.locator('[data-task-card="101"]').dragTo(page.locator('[data-column="blocked"]'));
  await expect.poll(() => writes.find((w) => w.path === "/api/action/task")?.body).toEqual({ id: 101, status: "blocked", expected_updated_at: "2026-11-09T08:00:00+00:00" });
});

test("board: a parked move waits for approval and links to the Inbox", async ({ page }) => {
  await mockWork(page, { "POST /api/action/task": PARKED("task_update") });
  await page.goto("/ui/work?view=board");
  await page.getByRole("button", { name: "Move task #101 to" }).click();
  await page.getByRole("menuitem", { name: "Done" }).click();
  await page.getByRole("dialog", { name: "Move this task?" }).getByRole("button", { name: "Move to Done" }).click();
  await expect(page.getByText(/Waiting for approval/).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=77");
});

test("graph: table fallback by keyboard opens the task drawer; Escape closes it", async ({ page }) => {
  await mockWork(page);
  await page.goto("/ui/work?view=graph");
  await expect(page.getByRole("list", { name: "Line labels" })).toBeVisible();
  await page.getByRole("button", { name: "Table" }).click();
  const node = page.getByRole("button", { name: "#102 Ship the Work area" });
  await node.focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByRole("dialog", { name: "#102 Ship the Work area" });
  await expect(drawer).toBeVisible();
  await expect(page).toHaveURL(/task=102/);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await page.getByRole("button", { name: "Goals to work" }).click();
  await expect(page).toHaveURL(/scope=work/);
  await expect(page.getByText("Goals, plans and tasks")).toBeVisible();
});

test("task page: edit the title from the keyboard, then delete with confirm", async ({ page }) => {
  const { writes } = await mockWork(page, {
    "POST /api/action/task": { json: TASK_102.task },
    "POST /api/action/task/delete": { json: { deleted: true } },
  });
  await page.goto("/ui/work/tasks/102");
  await expect(h1(page, "Ship the Work area")).toBeVisible();
  await page.getByRole("button", { name: "Edit title" }).focus();
  await page.keyboard.press("Enter");
  const input = page.getByLabel("Title", { exact: true });
  await input.fill("Ship it all");
  await page.getByRole("button", { name: "Save" }).focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => writes.find((w) => w.path === "/api/action/task")?.body).toEqual({ id: 102, title: "Ship it all", expected_updated_at: "2026-11-10T11:00:00Z" });
  await page.getByRole("button", { name: "More task actions" }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete this task and every task under it?" });
  await expect(dialog.getByLabel(/Type 102 to confirm/)).toBeFocused();
  await dialog.getByLabel(/Type 102 to confirm/).fill("102");
  await dialog.getByRole("button", { name: "Delete the task and its subtree" }).click();
  await expect(page).toHaveURL(/\/ui\/work$/);
  expect(writes.find((w) => w.path === "/api/action/task/delete")?.body).toEqual({ id: 102 });
});

test("task page: claim, comment and a parked status change", async ({ page }) => {
  const { writes } = await mockWork(page, {
    "GET /api/platform/objects/task/101": { json: { uri: "unified-agent://tasks/101", task: { ...TASK_102.task, id: 101, title: "Write the release notes", claimed_by: null, current_run_id: null } } },
    "POST /api/action/task/claim": { json: { id: 101, claimed_by: "operator" } },
    "POST /api/tasks/101/comments": { status: 201, json: { id: 2 } },
    "POST /api/action/task": PARKED("task_update", 31),
  });
  await page.goto("/ui/work/tasks/101");
  await page.getByRole("button", { name: "Claim" }).click();
  await expect.poll(() => writes.find((w) => w.path === "/api/action/task/claim")?.body).toEqual({ id: 101 });
  await page.getByRole("textbox", { name: "Comment" }).fill("On it.");
  await page.getByRole("button", { name: "Post comment" }).click();
  await expect.poll(() => writes.find((w) => w.path === "/api/tasks/101/comments")?.body).toMatchObject({ body: "On it." });
  const status = page.getByRole("region", { name: "Status" });
  await status.getByLabel("Status").selectOption("done");
  await status.getByRole("button", { name: "Change status" }).click();
  await expect(status.getByText(/Waiting for approval/)).toBeVisible();
});

test("task page: a parked work product continues through its release route", async ({ page }) => {
  const { writes } = await mockWork(page, {
    "POST /api/work-products": { status: 202, json: { status: "pending", approval_id: 12, code: "approval_required", work_product_id: 5 } },
    "GET /api/approvals/12": { json: { id: 12, status: "approved", payload: { work_product_id: 5 } } },
    "POST /api/work-products/5/release": { json: { id: 5, approval_id: 12, released: true } },
  });
  await page.goto("/ui/work/tasks/102");
  const form = page.getByRole("form", { name: "Submit a work product" });
  await form.getByLabel(/File in the workspace/).fill("reports/a.md");
  await form.getByLabel(/^Title/).fill("Report");
  await form.getByLabel(/^Template/).fill("memo");
  await form.getByRole("button", { name: "Submit for release" }).click();
  // The parked submission is followed as a release (review round 2, item 7); Submit stays disabled.
  const followUp = page.getByRole("group", { name: "Release follow-up" });
  await expect(followUp.getByText(/Approved. Continue the release of work product #5/)).toBeVisible();
  await expect(form.getByRole("button", { name: "Submit for release" })).toBeDisabled();
  await followUp.getByRole("button", { name: "Continue release" }).click();
  await expect.poll(() => writes.filter((w) => w.path === "/api/work-products/5/release")).toHaveLength(1);
  expect(writes.filter((w) => w.path === "/api/work-products")).toHaveLength(1);
});

test("goals: create a goal and close it", async ({ page }) => {
  const { writes } = await mockWork(page, {
    "POST /api/goals": { status: 201, json: { goal: { id: 9 } } },
    "POST /api/action/goal/close": { json: { goal: { id: 7, title: "A usable Control Center", status: "achieved" }, edge: "goal_ancestry", meaning: "" } },
  });
  await page.goto("/ui/work/goals");
  await page.getByRole("button", { name: "New goal" }).first().click();
  const dialog = page.getByRole("dialog", { name: "New goal" });
  await dialog.getByLabel(/^Title/).fill("Faster reviews");
  await dialog.getByRole("button", { name: "Create goal" }).click();
  await expect.poll(() => writes.find((w) => w.path === "/api/goals")?.body).toEqual({ action: "add", title: "Faster reviews" });
  await page.goto("/ui/work/goals/7");
  await expect(page.getByText(/1 of 2 tasks done/)).toBeVisible();
  await page.getByRole("button", { name: "Close goal" }).click();
  await page.getByRole("dialog", { name: "Close this goal?" }).getByRole("button", { name: "Close goal" }).click();
  await expect.poll(() => writes.find((w) => w.path === "/api/action/goal/close")?.body).toEqual({ goal_id: 7, status: "achieved", expected_revision: 4 });
});

test("plans: create, hand a step to an agent, and see shared-plan actions disabled for a non-member", async ({ page }) => {
  const { writes } = await mockWork(page, {
    "POST /api/action/plan/create": { json: { plan_id: "3", steps: [] } },
    "POST /api/action/plan/update": { json: { step: {}, plan: {}, next_step_idx: 2 } },
  });
  await page.goto("/ui/work/plans/new");
  await page.getByLabel(/What the plan achieves/).fill("Roll out");
  await page.getByLabel("Step 1", { exact: true }).fill("Prepare");
  await page.getByRole("button", { name: "Create plan" }).click();
  await expect(page).toHaveURL(/\/ui\/work\/plans\/3$/);
  await page.getByRole("button", { name: "Hand to an agent" }).nth(2).click();
  const dialog = page.getByRole("dialog", { name: "Hand this step to an agent" });
  const agent = dialog.getByRole("combobox", { name: /Agent/ });
  await agent.fill("Desktop_Agent2");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await dialog.getByRole("button", { name: "Hand over" }).click();
  await expect.poll(() => writes.find((w) => w.path === "/api/action/plan/update")?.body).toEqual({ plan_id: "3", step_idx: 2, owner: "Desktop_Agent2", status: "open", expected_step_id: 32 });
  const panel = page.getByRole("region", { name: "Shared plan" });
  await expect(panel.getByRole("note")).toContainText("operator is not a member of org unit web-office");
  await expect(panel.getByRole("button", { name: "Propose new steps" })).toBeDisabled();
});

test("canvas: open a board from the URL and save it with PUT", async ({ page }) => {
  const { writes } = await mockWork(page, {
    "PUT /api/graph-canvas": { json: { board_id: "default", saved: true, updated_by: "operator", updated_at: "2026-11-10T12:00:00Z" } },
  });
  await page.goto("/ui/work/canvas");
  await expect(h1(page, "Planning canvas")).toBeVisible();
  await expect(page.getByTestId("excalidraw-host")).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "Save board" }).click();
  await expect.poll(() => writes.find((w) => w.method === "PUT")?.body).toMatchObject({ type: "excalidraw", elements: [] });
});
