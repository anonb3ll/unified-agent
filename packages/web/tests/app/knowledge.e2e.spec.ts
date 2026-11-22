import { expect, test, type Page } from "@playwright/test";
import { memoryNote, MEMORY_ROWS, NOTE_KEY, parkedBody } from "./knowledge/fixtures";
import { mockKnowledge, reply } from "./knowledge/pw";

/**
 * Spec §9.5 flows against a mocked hub (response shapes from
 * `webui/cc_knowledge.py` and `webui/routes.py`). Each flow is driven by the
 * keyboard: focus moves with Tab or a direct focus, and actions run on Enter
 * or Space, never a pointer click.
 */
test.use({ viewport: { width: 1440, height: 900 } });
test.describe.configure({ timeout: 90_000 });
expect.configure({ timeout: 20_000 });

const main = (page: Page) => page.getByRole("main");
const h1 = (page: Page, name: string) => main(page).getByRole("heading", { level: 1, name, exact: true });
const noteUrl = (key = NOTE_KEY, extra = "") => `/ui/knowledge/memory?note=${encodeURIComponent(key)}${extra}`;

async function press(page: Page, locator: ReturnType<Page["locator"]>, key = "Enter") {
  await locator.focus();
  await page.keyboard.press(key);
}

async function typeInto(page: Page, locator: ReturnType<Page["locator"]>, text: string) {
  await locator.focus();
  await page.keyboard.insertText(text);
}

test("memory: search and filters live in the URL; a note opens from the list by keyboard", async ({ page }) => {
  await mockKnowledge(page);
  await page.goto("/ui/knowledge");
  await expect(page).toHaveURL(/\/ui\/knowledge\/memory$/);
  await expect(h1(page, "Memory")).toBeVisible();
  await typeInto(page, main(page).getByRole("searchbox", { name: "Search memory" }), "steward");
  await expect(page).toHaveURL(/q=steward/);
  const author = main(page).getByLabel("Author");
  await author.focus();
  await author.selectOption("Desktop_Agent1");
  await expect(page).toHaveURL(/author=Desktop_Agent1/);
  const link = main(page).getByRole("link", { name: /Deploy steward owns hub restarts/ });
  await press(page, link);
  await expect(page).toHaveURL(new RegExp(`note=${encodeURIComponent(NOTE_KEY).replace(/[.]/g, "\\.")}`));
  await expect(page).toHaveURL(/author=Desktop_Agent1/);
  await expect(main(page).getByRole("heading", { level: 2, name: "Deploy steward owns hub restarts" })).toBeVisible();
  await expect(main(page).getByRole("heading", { level: 3, name: "Who restarts the hub" })).toBeVisible();
  await expect(main(page).locator("script, img")).toHaveCount(0);
  // Back returns to the list with the filters.
  await page.goBack();
  await expect(page).not.toHaveURL(/note=/);
  await expect(page).toHaveURL(/author=Desktop_Agent1/);
});

test("memory: New → Memory note in the top bar opens the form; saving opens the new note", async ({ page }) => {
  const { writes } = await mockKnowledge(page, {
    "POST /api/action/memory/save": reply({ id: "a1b2c3d4e5f6", created: true, similar: [] }),
    "GET /api/memory/note": reply(memoryNote({ id: "a1b2c3d4e5f6", title: "Hub restarts", content: "Only the steward restarts it." })),
  });
  await page.goto("/ui/home");
  await expect(h1(page, "Home")).toBeVisible();
  await press(page, page.getByRole("button", { name: "New", exact: true }));
  const item = page.getByRole("menuitem", { name: "Memory note" });
  await expect(item).toBeVisible();
  await press(page, item);
  await expect(page).toHaveURL(/\/ui\/knowledge\/memory\?new=1$/);
  const form = main(page).getByRole("region", { name: "New memory note" });
  await typeInto(page, form.getByLabel("Title"), "Hub restarts");
  await typeInto(page, form.getByLabel(/^Text/), "Only the steward restarts it.");
  await typeInto(page, form.getByLabel("Tags"), "decision");
  await press(page, form.getByRole("button", { name: "Save note" }));
  await expect(page).toHaveURL(/note=a1b2c3d4e5f6/);
  expect(writes[0]).toMatchObject({ path: "/api/action/memory/save", body: { content: "Only the steward restarts it.", title: "Hub restarts", tags: ["decision"] } });
  await expect(page.getByRole("status").filter({ hasText: "Note saved." })).toBeVisible();
});

test("memory: a parked save waits for approval with the Inbox link, keeps the form, and the draft survives navigation", async ({ page }) => {
  await mockKnowledge(page, {
    "POST /api/action/memory/save": reply(parkedBody("memory_save", 77), 202),
    "GET /api/approvals/77": reply({ id: 77, tool: "memory_save", status: "pending" }),
  });
  await page.goto("/ui/knowledge/memory?new=1");
  const form = main(page).getByRole("region", { name: "New memory note" });
  await typeInto(page, form.getByLabel(/^Text/), "Parked thought");
  await press(page, form.getByRole("button", { name: "Save note" }));
  const notice = form.getByRole("region", { name: "Approval status" });
  await expect(notice.getByText(/Waiting for approval/)).toBeVisible();
  await expect(notice.getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=77");
  await expect(form.getByLabel(/^Text/)).toHaveValue("Parked thought");
  await expect(page.getByText("Note saved.")).toHaveCount(0);
  // Leave and come back: the draft is still there.
  await press(page, main(page).getByRole("link", { name: "Vault", exact: true }));
  await expect(h1(page, "Vault")).toBeVisible();
  await page.goBack();
  await expect(main(page).getByRole("region", { name: "New memory note" }).getByLabel(/^Text/)).toHaveValue("Parked thought");
});

test("memory: an approved parked save runs on the person's click from the form", async ({ page }) => {
  let saves = 0;
  const { writes } = await mockKnowledge(page, {
    "GET /api/whoami": reply({ hub_version: "9.9.9", api_version: 1, agent: "operator", via: "cookie", csrf: "csrf-1", enforce_auth: true, tokens_configured: true, control_enabled: true, control_disabled_reason: null, org: "org_7f3", org_mode: "partitioned" }),
    "POST /api/action/memory/save": async (route) => {
      saves += 1;
      await route.fulfill(
        saves === 1
          ? { status: 202, contentType: "application/json", body: JSON.stringify(parkedBody("memory_save", 77)) }
          : { status: 200, contentType: "application/json", body: JSON.stringify({ id: "ran0001", created: true, similar: [] }) },
      );
    },
    "GET /api/approvals/77": reply({ id: 77, tool: "memory_save", status: "approved" }),
    "GET /api/memory/note": reply(memoryNote({ id: "ran0001", title: "Approved", content: "Approved text" })),
  });
  await page.goto("/ui/knowledge/memory?new=1");
  const form = main(page).getByRole("region", { name: "New memory note" });
  await typeInto(page, form.getByLabel(/^Text/), "Approved text");
  await press(page, form.getByRole("button", { name: "Save note" }));
  await press(page, form.getByRole("button", { name: "Run it now" }));
  await expect(page).toHaveURL(/note=ran0001/);
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toEqual(writes[0].body);
});

test("memory: replacing another author's note needs the explicit overwrite choice", async ({ page }) => {
  const { writes } = await mockKnowledge(page, { "POST /api/action/memory/save": reply({ id: NOTE_KEY, created: false }) });
  await page.goto(noteUrl());
  await press(page, main(page).getByRole("link", { name: "Edit", exact: true }));
  const form = main(page).getByRole("region", { name: /^Edit/ });
  await expect(form.getByText("Desktop_Agent1 wrote this note, not you.")).toBeVisible();
  const text = form.getByLabel(/^Text/);
  await text.focus();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.insertText("New text");
  await press(page, form.getByRole("button", { name: "Save changes" }));
  await expect(form.getByText(/Tick the box to confirm/)).toBeVisible();
  expect(writes).toHaveLength(0);
  await press(page, form.getByRole("checkbox", { name: "Replace the note Desktop_Agent1 wrote" }), "Space");
  await press(page, form.getByRole("button", { name: "Save changes" }));
  await expect(page).not.toHaveURL(/edit=1/);
  expect(writes[0].body).toMatchObject({ key: NOTE_KEY, content: "New text", overwrite: true });
});

test("memory: delete confirms first; feedback and promotion run from the note", async ({ page }) => {
  const { writes } = await mockKnowledge(page, {
    "POST /api/action/memory/delete": reply({ deleted: true }),
    "POST /api/action/memory/feedback": reply({ signal: "helpful", ...MEMORY_ROWS[0], feedback_helpful: 3 }),
    "POST /api/action/memory/promote": reply({ promoted: true, path: "Wiki/Learnings/deploy-steward.md", wiki_link: "Wiki/Learnings/deploy-steward", index_updated: true }),
  });
  await page.goto(noteUrl());
  await press(page, main(page).getByRole("button", { name: "Helpful (2)" }));
  await expect.poll(() => writes.find((w) => w.path === "/api/action/memory/feedback")?.body).toEqual({ key: NOTE_KEY, signal: "helpful" });

  await press(page, main(page).getByRole("button", { name: "Promote to Wiki" }));
  const promote = page.getByRole("dialog", { name: "Promote this note to the Wiki" });
  await press(page, promote.getByRole("button", { name: "Promote", exact: true }));
  await expect(promote.getByRole("link", { name: "Open Wiki/Learnings/deploy-steward.md in the vault" })).toBeVisible();
  await press(page, promote.getByRole("button", { name: "Close", exact: true }));

  await press(page, main(page).getByRole("button", { name: "Delete", exact: true }));
  const confirm = page.getByRole("dialog", { name: "Delete this memory note?" });
  await expect(confirm.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(confirm).toBeHidden();
  expect(writes.some((w) => w.path === "/api/action/memory/delete")).toBe(false);
  await press(page, main(page).getByRole("button", { name: "Delete", exact: true }));
  await press(page, confirm.getByRole("button", { name: "Delete note" }));
  await expect.poll(() => writes.find((w) => w.path === "/api/action/memory/delete")?.body).toEqual({ key: NOTE_KEY });
});

test("memory: a deleted note restores as a copy", async ({ page }) => {
  const { writes } = await mockKnowledge(page, {
    "GET /api/memory/note": reply(memoryNote({ deleted: true, tags: ["decision", "meta:deleted"], content: "tombstone" })),
    "POST /api/action/memory/save": reply({ id: "copy01", created: true }),
  });
  await page.goto(noteUrl());
  await expect(main(page).getByText(/This note was deleted/)).toBeVisible();
  await press(page, main(page).getByRole("button", { name: "Restore a copy" }));
  await expect(page).toHaveURL(/note=copy01/);
  expect(writes[0].body).toEqual({
    content: "Anyone may restart the hub.",
    title: "Deploy steward owns hub restarts",
    tags: ["decision"],
    idempotency_key: expect.stringMatching(/^memory-restore-copy-/),
  });
});

test("vault: the folder tree opens with the keyboard; a note shows Markdown, wikilinks, backlinks and its Wiki status", async ({ page }) => {
  await mockKnowledge(page);
  await page.goto("/ui/knowledge/vault");
  await expect(h1(page, "Vault")).toBeVisible();
  await expect(main(page).getByText("Opened PR #1510 for the kernel batch.")).toBeVisible();
  const tree = main(page).getByRole("navigation", { name: "Vault folders" });
  const wiki = tree.getByRole("button", { name: /Wiki/ });
  await press(page, wiki);
  await expect(wiki).toHaveAttribute("aria-expanded", "true");
  await expect(tree.getByRole("link", { name: "Deploys.v2.md" })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(tree.getByRole("link", { name: "Deploys.v2.md" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/path=Wiki%2FDeploys\.v2\.md/);
  const note = main(page).getByRole("region", { name: "Deploys.v2" });
  await expect(note.getByText("Changed since it went into the Wiki")).toBeVisible();
  await expect(note.locator("b")).toHaveCount(0);
  await expect(main(page).getByRole("region", { name: "Links" }).getByRole("link", { name: "Wiki/Index.md" })).toBeVisible();
  await press(page, note.getByRole("link", { name: "Runbook" }));
  await expect(page).toHaveURL(/path=Wiki%2FRunbook\.md/);
  await expect(main(page).getByRole("heading", { level: 2, name: "Runbook" })).toBeVisible();
});

test("vault: search finds notes", async ({ page }) => {
  await mockKnowledge(page);
  await page.goto("/ui/knowledge/vault");
  await typeInto(page, main(page).getByRole("searchbox", { name: "Search the vault" }), "steward");
  await expect(page).toHaveURL(/q=steward/);
  const results = main(page).getByRole("region", { name: "Search results for “steward”" });
  await expect(results.getByText("2 matching lines")).toBeVisible();
  await press(page, results.getByRole("link", { name: "Wiki/Deploys.v2.md" }));
  await expect(main(page).getByRole("heading", { level: 2, name: "Deploys.v2" })).toBeVisible();
});

test("vault: a new note goes only to a write folder; the hub's folder refusal is explained", async ({ page }) => {
  let refuse = true;
  const { writes } = await mockKnowledge(page, {
    "POST /api/action/vault/write": async (route) => {
      if (refuse) {
        refuse = false;
        await route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ error: "vault writes are restricted to 00 - Inbox, Logs; 'Wiki/Plan.md' is outside. Extend UA_VAULT_WRITE_DIRS (colon-separated) to widen it." }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ path: "00 - Inbox/Plan.md", bytes_written: 6, mode: "overwrite", append: false, total_lines_after: 1, revision: "sha256:1" }) });
    },
    "GET /api/vault/page": reply({ path: "00 - Inbox/Plan.md", content: "# Plan", total_lines: 1, truncated: false, revision: "sha256:1", outgoing: [], backlinks: [] }),
  });
  await page.goto("/ui/knowledge/vault");
  await press(page, main(page).getByRole("link", { name: "New note" }));
  const form = main(page).getByRole("region", { name: "New vault note" });
  const folder = form.getByLabel(/^Folder/);
  await folder.focus();
  await folder.selectOption("Wiki");
  await typeInto(page, form.getByLabel(/^Note name/), "Plan");
  await typeInto(page, form.getByLabel(/^Text/), "# Plan");
  await press(page, form.getByRole("button", { name: "Save note" }));
  await expect(form.getByRole("alert")).toContainText("The hub writes only to these folders: 00 - Inbox, Logs. Pick one of them.");
  await expect(form.getByLabel(/^Text/)).toHaveValue("# Plan");
  await folder.selectOption("00 - Inbox");
  await press(page, form.getByRole("button", { name: "Save note" }));
  await expect(page).toHaveURL(/path=00\+-\+Inbox%2FPlan\.md/);
  expect(writes[1].body).toEqual({ path: "00 - Inbox/Plan.md", content: "# Plan", mode: "overwrite", expected_revision: "absent" });
});

test("vault: add to a note; a locked note says so and offers no write", async ({ page }) => {
  const { writes } = await mockKnowledge(page, { "POST /api/action/vault/write": reply({ path: "Wiki/Deploys.v2.md", bytes_written: 9, mode: "append", append: true, total_lines_after: 8, revision: "sha256:2" }) });
  await page.goto("/ui/knowledge/vault?path=Wiki%2FDeploys.v2.md");
  await press(page, main(page).getByRole("link", { name: "Add to this note" }));
  const form = main(page).getByRole("form", { name: "Add to this note" });
  await typeInto(page, form.getByLabel(/^Text to add/), "- one more step");
  await press(page, form.getByRole("button", { name: "Add text" }));
  await expect.poll(() => writes[0]?.body).toEqual({ path: "Wiki/Deploys.v2.md", content: "\n- one more step", mode: "append" });
  await expect(page).not.toHaveURL(/write=append/);

  await page.goto("/ui/knowledge/vault?path=Wiki%2FLocked%20page.md");
  await expect(main(page).getByText(/This note is locked/)).toBeVisible();
  await expect(main(page).getByRole("link", { name: "Add to this note" })).toHaveCount(0);
});

test("vault: a journal entry counts characters live and refuses more than 240", async ({ page }) => {
  const { writes } = await mockKnowledge(page, { "POST /api/action/vault/journal": reply({ path: "Logs/2026-11-10.md", entry: "x", created_note: false }) });
  await page.goto("/ui/knowledge/vault");
  await press(page, main(page).getByRole("link", { name: "Add journal entry" }));
  const form = main(page).getByRole("region", { name: "Add a journal entry" });
  const entry = form.getByLabel(/^Entry/);
  await typeInto(page, entry, "x".repeat(245));
  await expect(form.getByText("5 characters over the limit.")).toBeVisible();
  await press(page, form.getByRole("button", { name: "Add entry" }));
  await expect(form.getByText(/The entry is 245 characters/)).toBeVisible();
  expect(writes).toHaveLength(0);
  await entry.focus();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.insertText("Shipped   the vault page");
  await expect(form.getByText(/^22 of 240 characters/)).toBeVisible();
  await press(page, form.getByRole("button", { name: "Add entry" }));
  await expect.poll(() => (writes[0]?.body as { entry?: string } | undefined)?.entry).toBe("Shipped   the vault page");
  expect((writes[0].body as { date: string }).date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
});
