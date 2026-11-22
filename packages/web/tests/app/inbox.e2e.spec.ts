import { expect as baseExpect, test, type Page } from "@playwright/test";
import { APPROVAL_ID, approvalDetail, fullHub, taskRow } from "./inbox/fixtures";
import { hub } from "./inbox/pw";

// Home and the Inbox read many sources at once; allow for a loaded machine.
const expect = baseExpect.configure({ timeout: 15_000 });

test.use({ viewport: { width: 1440, height: 900 } });

const main = (page: Page) => page.getByRole("main");
const detailHeading = (page: Page, name: string | RegExp) => main(page).getByRole("heading", { level: 2, name });

test("j / k move through the list, Enter opens the item and focuses its heading; the URL keeps it", async ({ page }) => {
  await hub(page, fullHub());
  await page.goto("/ui/inbox?kind=hold");
  await expect(main(page).getByRole("list", { name: "Items waiting on you" }).getByRole("link")).toHaveCount(2);
  const list = main(page).getByRole("list", { name: "Items waiting on you" });
  await page.keyboard.press("j");
  await expect(page).toHaveURL(/item=red_tier_hold%3A77/);
  await expect(list.getByRole("link", { name: /Rotate hub tokens/ })).toBeFocused();
  await expect(detailHeading(page, "Rotate hub tokens")).toBeVisible();
  await page.keyboard.press("j");
  await expect(page).toHaveURL(/item=operator_task%3A78/);
  await expect(list.getByRole("link", { name: /Renew the domain/ })).toBeFocused();
  await expect(detailHeading(page, "Renew the domain")).toBeVisible();
  await page.keyboard.press("k");
  await expect(page).toHaveURL(/item=red_tier_hold%3A77/);
  await expect(list.getByRole("link", { name: /Rotate hub tokens/ })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(detailHeading(page, "Rotate hub tokens")).toBeFocused();
  await page.reload();
  await expect(detailHeading(page, "Rotate hub tokens")).toBeVisible();
});

test("a key pressed before the page re-renders still moves the selection from where the last key left it", async ({ page }) => {
  // Both keys land in one task, so React cannot re-render (or re-bind its
  // listener) between them: the second key must see the first key's
  // selection, not the one the page last rendered (task #2602).
  await hub(page, fullHub());
  await page.goto("/ui/inbox?kind=hold");
  const list = main(page).getByRole("list", { name: "Items waiting on you" });
  await expect(list.getByRole("link")).toHaveCount(2);
  await page.keyboard.press("j");
  await expect(page).toHaveURL(/item=red_tier_hold%3A77/);
  await expect(detailHeading(page, "Rotate hub tokens")).toBeVisible();
  await page.evaluate(() => {
    for (const key of ["j", "k"]) window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
  });
  await expect(list.getByRole("link", { name: /Rotate hub tokens/ })).toBeFocused();
  await expect(page).toHaveURL(/item=red_tier_hold%3A77/);
  await expect(detailHeading(page, "Rotate hub tokens")).toBeVisible();
});

test("approve by keyboard: the preview shows what will run first, then the result", async ({ page }) => {
  let status = "pending";
  const writes = await hub(page, fullHub(), {
    [`GET /api/approvals/${APPROVAL_ID}`]: () => ({ body: approvalDetail(status) }),
    "POST /api/action/approval": () => {
      status = "approved";
      return { body: { id: APPROVAL_ID, status: "approved" } };
    },
  });
  await page.goto(`/ui/inbox?kind=approval&item=${APPROVAL_ID}`);
  const args = main(page).getByRole("list", { name: "Arguments summary" });
  await expect(args).toContainText("task_id: 42");
  await expect(main(page).getByText("Desktop_Agent2").first()).toBeVisible();
  // The standing acknowledgement comes first (review 2.1), by keyboard.
  await main(page).getByLabel(/I understand that anything not shown here would also be approved/).focus();
  await page.keyboard.press("Space");
  await main(page).getByRole("button", { name: "Approve" }).focus();
  await page.keyboard.press("Enter");
  await expect(main(page).getByText(/Approved, not used yet\. The requester may repeat exactly this call once/)).toBeVisible();
  expect(writes).toEqual([{ method: "POST", path: "/api/action/approval", body: { id: APPROVAL_ID, decision: "approve" } }]);
});

test("deny with a reason, typed", async ({ page }) => {
  const writes = await hub(page, fullHub(), { "POST /api/action/approval": { body: { id: APPROVAL_ID, status: "denied" } } });
  await page.goto(`/ui/inbox?kind=approval&item=${APPROVAL_ID}`);
  await main(page).getByRole("button", { name: "Deny…" }).click();
  await page.getByLabel("Reason for denying").fill("Task 42 is still needed");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(main(page).getByText("Denied. The call will not run.")).toBeVisible();
  expect(writes[0].body).toEqual({ id: APPROVAL_ID, decision: "deny", reason: "Task 42 is still needed" });
});

test("reply to an agent's question from the item", async ({ page }) => {
  const writes = await hub(page, fullHub(), { "POST /api/messages": { body: { accepted: true, conversation_id: "conv-1" } } });
  await page.goto("/ui/inbox?kind=message&item=reply%3Amsg-1");
  await expect(main(page).getByText("Should I ship <b>now</b>?")).toBeVisible();
  await page.getByLabel("Your reply to Desktop_Agent2").fill("Yes, ship it.");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(main(page).getByRole("status").filter({ hasText: "Sent." })).toBeVisible();
  expect(writes[0]).toMatchObject({ path: "/api/messages", body: { reply_to: "msg-1", to_agent: "Desktop_Agent2", body: "Yes, ship it." } });
});

test("grant a wake", async ({ page }) => {
  const writes = await hub(page, fullHub(), { "POST /api/sessions/sess-1/wake-grant": { body: { granted: true } } });
  await page.goto("/ui/inbox?kind=approval&item=wake%3Asess-1");
  await main(page).getByRole("button", { name: "Grant wakes" }).focus();
  await page.keyboard.press("Enter");
  await expect(main(page).getByText("Wakes granted for this session.")).toBeVisible();
  expect(writes[0]).toEqual({ method: "POST", path: "/api/sessions/sess-1/wake-grant", body: { grant: true } });
});

test("hold: add a note for the merge actor by keyboard; the item stays listed (review 1.2)", async ({ page }) => {
  const writes = await hub(page, fullHub(), { "POST /api/action/task": { body: taskRow(77, "Rotate hub tokens") } });
  await page.goto("/ui/inbox?kind=hold&item=red_tier_hold%3A77");
  await expect(main(page).getByText("This hub cannot release a hold yet", { exact: false })).toBeVisible();
  await page.getByLabel("Note for the merge actor").fill("Checked; fine to merge.");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(main(page).getByText(/Note added to task 77/)).toBeVisible();
  expect((writes[0].body as { append_note: string }).append_note).toMatch(/^\[operator-note @ .+\] Checked; fine to merge\.$/);
  await expect(main(page).getByRole("list", { name: "Items waiting on you" })).toContainText("Rotate hub tokens");
});

test("file a memory proposal and dismiss through a confirm", async ({ page }) => {
  const writes = await hub(page, fullHub(), { "POST /api/action/proposal": { body: { ok: true } } });
  await page.goto("/ui/inbox?kind=proposal&item=memory%3Aprop-1");
  await main(page).getByRole("button", { name: "Dismiss…" }).click();
  await page.getByRole("dialog", { name: "Dismiss this proposal?" }).getByRole("button", { name: "Dismiss" }).click();
  await expect(main(page).getByText("Dismissed.")).toBeVisible();
  expect(writes[0].body).toEqual({ action: "dismiss", proposal_id: "prop-1" });
});

test("org proposal decisions are shown, disabled, until the hub offers them (review 1.4)", async ({ page }) => {
  const writes = await hub(page, fullHub());
  await page.goto("/ui/inbox?kind=proposal&item=org%3Aorgprop-1");
  await expect(main(page).getByRole("heading", { name: "Proposed changes" })).toBeVisible();
  await expect(main(page).getByRole("button", { name: "Apply" })).toBeDisabled();
  await expect(main(page).getByText("Not available on this hub yet", { exact: false }).first()).toBeVisible();
  expect(writes).toHaveLength(0);
});

test("messages: send by keyboard, open the conversation, hand it off", async ({ page }) => {
  const writes = await hub(page, fullHub(), {
    "POST /api/messages": { body: { accepted: true, conversation_id: "conv-1" } },
    "POST /api/messages/conversations/conv-1/handoff": { body: { conversation_id: "conv-1", task_id: 310, created: true, reference: "r" } },
  });
  await page.goto("/ui/inbox/messages?compose=1");
  const form = page.getByRole("form", { name: "New message" });
  await form.getByLabel(/^To/).selectOption("Desktop_Agent2");
  await form.getByLabel(/^Message/).fill("Please rebase onto main.");
  await form.getByRole("button", { name: "Send" }).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/conversation=conv-1/);
  expect(writes[0]).toMatchObject({ path: "/api/messages", body: { to_agent: "Desktop_Agent2", body: "Please rebase onto main.", act: "inform" } });
  await expect(main(page).getByRole("list", { name: "Messages in this conversation" })).toContainText("Should I ship");
  const handoff = page.getByRole("form", { name: "Hand off this conversation" });
  await handoff.getByLabel(/^Task title/).fill("Decide the ship date");
  await handoff.getByRole("button", { name: "Create the task" }).click();
  await expect(main(page).getByText("Created task 310.")).toBeVisible();
});

test.describe("phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test("list and detail stack; Back to the list returns", async ({ page }) => {
    await hub(page, fullHub());
    await page.goto("/ui/inbox?kind=hold");
    await main(page).getByRole("link", { name: /Rotate hub tokens/ }).click();
    await expect(detailHeading(page, "Rotate hub tokens")).toBeVisible();
    await expect(main(page).getByRole("list", { name: "Items waiting on you" })).toBeHidden();
    await main(page).getByRole("link", { name: "Back to the list" }).click();
    await expect(main(page).getByRole("list", { name: "Items waiting on you" })).toBeVisible();
  });
});
