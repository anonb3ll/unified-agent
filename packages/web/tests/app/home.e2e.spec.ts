import { expect as baseExpect, test, type Page } from "@playwright/test";
import { APPROVAL_ID, approvalDetail, emptyHub, failingHub, fullHub } from "./inbox/fixtures";
import { hub } from "./inbox/pw";

// Home and the Inbox read many sources at once; allow for a loaded machine.
const expect = baseExpect.configure({ timeout: 15_000 });

test.use({ viewport: { width: 1440, height: 900 } });

const main = (page: Page) => page.getByRole("main");

test("the project filter scopes every region, lives in the URL and survives reload and back", async ({ page }) => {
  await hub(page, fullHub());
  await page.goto("/ui/home");
  const running = main(page).getByRole("list", { name: "Running now" });
  await expect(running).toContainText("Ship the inbox");
  const project = page.getByLabel("Project");
  await project.focus();
  await project.selectOption("beta");
  await expect(page).toHaveURL(/\/ui\/home\?project=beta$/);
  await expect(running).not.toContainText("Ship the inbox");
  await expect(running).toContainText("Beta research");
  await page.reload();
  await expect(page.getByLabel("Project")).toHaveValue("beta");
  await expect(main(page).getByRole("list", { name: "Running now" })).not.toContainText("Ship the inbox");
  await page.goBack();
  await expect(page).toHaveURL(/\/ui\/home$/);
  await expect(main(page).getByRole("list", { name: "Running now" })).toContainText("Ship the inbox");
});

test("Act by keyboard opens the item in a drawer and approves without leaving Home", async ({ page }) => {
  let status = "pending";
  const writes = await hub(page, fullHub(), {
    [`GET /api/approvals/${APPROVAL_ID}`]: () => ({ body: approvalDetail(status) }),
    "POST /api/action/approval": () => {
      status = "approved";
      return { body: { id: APPROVAL_ID, status: "approved" } };
    },
  });
  await page.goto("/ui/home");
  const act = main(page).getByRole("button", { name: /^Act on: task_delete wants to run/ });
  await act.focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByRole("dialog", { name: "Approval" });
  await expect(drawer.getByRole("list", { name: "Arguments summary" })).toContainText("task_id: 42");
  // The standing acknowledgement comes first (review 2.1), by keyboard.
  await drawer.getByLabel(/I understand that anything not shown here would also be approved/).focus();
  await page.keyboard.press("Space");
  await drawer.getByRole("button", { name: "Approve" }).focus();
  await page.keyboard.press("Enter");
  await expect(drawer.getByText(/Approved, not used yet\. The requester may repeat exactly this call once/)).toBeVisible();
  expect(writes[0].body).toEqual({ id: APPROVAL_ID, decision: "approve" });
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(act).toBeFocused();
  await expect(page).toHaveURL(/\/ui\/home$/);
});

test("See all and fleet links go to the Inbox and agent pages", async ({ page }) => {
  await hub(page, fullHub());
  await page.goto("/ui/home");
  await main(page).getByRole("list", { name: "Agents and their state" }).getByRole("link", { name: "Desktop_Agent5" }).click();
  await expect(page).toHaveURL(/\/ui\/agents\/profile\/Desktop_Agent5$/);
  await page.goBack();
  await main(page).getByRole("link", { name: "See all" }).click();
  await expect(page).toHaveURL(/\/ui\/inbox$/);
});

test("first run: the checklist links each step", async ({ page }) => {
  await hub(page, emptyHub());
  await page.goto("/ui/home");
  const checklist = main(page).getByRole("region", { name: "Get started" });
  await expect(checklist).toContainText("0 of 4 done");
  await checklist.getByRole("link", { name: "Create a task" }).click();
  await expect(page).toHaveURL(/\/ui\/work\/tasks\/new$/);
});

test("failing reads show errors with Retry, and Retry reads again", async ({ page }) => {
  let agentReads = 0;
  await hub(page, failingHub(), {
    "GET /api/agents": () => {
      agentReads += 1;
      return { status: 503, body: { error: "busy" } };
    },
  });
  await page.goto("/ui/home");
  const error = main(page).getByRole("region", { name: "Agents could not be loaded" });
  await expect(error).toBeVisible();
  const before = agentReads;
  await error.getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => agentReads).toBeGreaterThan(before);
  await expect(main(page).getByText("0", { exact: true })).toHaveCount(0);
});
