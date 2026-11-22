import { expect, test, type Locator, type Page } from "@playwright/test";
import { CREDENTIAL_ID, EVIDENCE_TAMPERED, HEALTH, manyRecords } from "./settings/fixtures";
import { mockSettings } from "./settings/pw";

/**
 * Spec §9.7 flows against a mocked hub (response shapes from
 * `webui/routes.py` and `webui/api.py`). Each flow is driven by the
 * keyboard: focus moves directly or with Tab, and actions run on Enter or
 * Space, never a pointer click.
 */
test.use({ viewport: { width: 1440, height: 900 } });
test.describe.configure({ timeout: 90_000 });
expect.configure({ timeout: 20_000 });

const main = (page: Page) => page.getByRole("main");
const h1 = (page: Page, name: string) => main(page).getByRole("heading", { level: 1, name, exact: true });

async function press(page: Page, locator: Locator, key = "Enter") {
  await locator.focus();
  await page.keyboard.press(key);
}

async function typeInto(page: Page, locator: Locator, text: string) {
  await locator.focus();
  await page.keyboard.insertText(text);
}

const REVOKE = "/api/action/session-credential-revoke";

test("settings opens Health; the section tabs reach every page by keyboard", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/ui/settings");
  await expect(page).toHaveURL(/\/ui\/settings\/health$/);
  await expect(h1(page, "Health")).toBeVisible();
  const tabs = main(page).getByRole("navigation", { name: "Settings sections" });
  for (const [label, path] of [
    ["Platform", "/settings/platform"],
    ["Cloud", "/settings/cloud"],
    ["Usage", "/settings/usage"],
    ["Credentials", "/settings/credentials"],
    ["Audit", "/settings/audit"],
    ["Appearance", "/settings/appearance"],
    // Last: the integrations area draws its own section tabs.
    ["Integrations", "/settings/integrations"],
  ] as const) {
    await press(page, tabs.getByRole("link", { name: label }));
    await expect(page).toHaveURL(new RegExp(`/ui${path}$`));
    await expect(main(page).getByRole("heading", { level: 1 })).toHaveText(label === "Integrations" ? /Integrations/ : label);
  }
});

test("health: groups in plain words and runs the checks again by keyboard", async ({ page }) => {
  let forced = 0;
  await mockSettings(page, {
    "GET /api/health": async (route) => {
      if (route.request().url().includes("force=1")) forced += 1;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(HEALTH) });
    },
  });
  await page.goto("/ui/settings/health");
  const attention = main(page).getByRole("region", { name: "Needs attention" });
  await expect(attention.getByText("Workflow definitions")).toBeVisible();
  await expect(attention.locator('[data-status="MISCONFIGURED"]').getByText(/Set up wrong/)).toBeVisible();
  await expect(main(page).getByRole("region", { name: "Off" }).getByText("Zulip chat")).toBeVisible();
  await press(page, main(page).getByRole("button", { name: "Run the checks now" }));
  await expect.poll(() => forced).toBe(1);
});

test("credentials: revoke by keyboard names the object and the consequence; Cancel returns focus", async ({ page }) => {
  const { requests } = await mockSettings(page, { [`POST ${REVOKE}`]: { json: { revoked: [CREDENTIAL_ID], count: 1 } } });
  await page.goto("/ui/settings/credentials");
  await expect(h1(page, "Credentials")).toBeVisible();
  await expect(main(page).getByText("Present")).toBeVisible();
  // Invalid first: focus lands on the field with the error.
  const field = main(page).getByLabel(/Credential id/);
  await typeInto(page, field, "abc");
  const trigger = main(page).getByRole("button", { name: "Revoke…" });
  await press(page, trigger);
  await expect(main(page).getByText(/This is not a credential id/)).toBeVisible();
  await expect(field).toBeFocused();
  await field.fill("");
  await typeInto(page, field, CREDENTIAL_ID);
  await press(page, trigger);
  const dialog = page.getByRole("dialog", { name: "Revoke this session's access?" });
  await expect(dialog.getByText(`Session credential ${CREDENTIAL_ID}`)).toBeVisible();
  await expect(dialog.getByText(/cannot be undone/)).toBeVisible();
  // A danger confirm opens on Cancel.
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
  expect(requests.filter((r) => r.path === REVOKE)).toHaveLength(0);
  // Now confirm.
  await press(page, trigger);
  await press(page, page.getByRole("dialog").getByRole("button", { name: "Revoke", exact: true }));
  await expect(main(page).getByText(/1 credential revoked/)).toBeVisible();
  const sent = requests.filter((r) => r.path === REVOKE);
  expect(sent).toHaveLength(1);
  expect(sent[0].body).toEqual({ credential_id: CREDENTIAL_ID });
  expect(sent[0].headers["x-ua-csrf"]).toBe("csrf-1");
  await expect(field).toHaveValue("");
});

test("credentials: nothing typed is stored as a secret; a refused revoke keeps the text", async ({ page }) => {
  await mockSettings(page, { [`POST ${REVOKE}`]: { status: 403, json: { error: "revoking session credentials is an operator action" } } });
  await page.goto("/ui/settings/credentials");
  const field = main(page).getByLabel(/Credential id/);
  await typeInto(page, field, CREDENTIAL_ID);
  await press(page, main(page).getByRole("button", { name: "Revoke…" }));
  await press(page, page.getByRole("dialog").getByRole("button", { name: "Revoke", exact: true }));
  await expect(main(page).getByText(/The signed-in identity may not do this/)).toBeVisible();
  await expect(main(page).getByText(/operator action/)).toBeHidden();
  await expect(field).toHaveValue(CREDENTIAL_ID);
  expect(page.url()).not.toContain(CREDENTIAL_ID);
});

test("audit: filters in the URL, chain check and CSV export by keyboard", async ({ page }) => {
  let reads = 0;
  const { requests } = await mockSettings(page, {
    "GET /api/evidence": async (route) => {
      const url = route.request().url();
      if (url.includes("format=csv")) {
        await route.fulfill({ status: 200, contentType: "text/csv", headers: { "content-disposition": 'attachment; filename="evidence-report.csv"' }, body: "task_id\n2493\n" });
        return;
      }
      reads += 1;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(reads >= 3 ? EVIDENCE_TAMPERED : manyRecords(2)) });
    },
  });
  await page.goto("/ui/settings/audit");
  await expect(main(page).getByText("The ledger is intact")).toBeVisible();
  await typeInto(page, main(page).getByLabel("Agent"), "Desktop_Agent1");
  await press(page, main(page).getByRole("button", { name: "Apply filters" }));
  await expect(page).toHaveURL(/\/ui\/settings\/audit\?agent=Desktop_Agent1$/);
  await expect(main(page).getByText("2 records")).toBeVisible();
  // Back and forward restore the filter.
  await page.goBack();
  await expect(page).toHaveURL(/\/ui\/settings\/audit$/);
  await page.goForward();
  await expect(page).toHaveURL(/agent=Desktop_Agent1/);
  await press(page, main(page).getByRole("button", { name: "Check the ledger now" }));
  await expect(main(page).getByText("The ledger has been changed")).toBeVisible();
  const download = page.waitForEvent("download");
  await press(page, main(page).getByRole("button", { name: "Export as CSV" }));
  expect((await download).suggestedFilename()).toBe("evidence-report.csv");
  const exportCall = requests.find((r) => r.search.includes("format=csv"));
  expect(exportCall?.search).toBe("?agent=Desktop_Agent1&format=csv");
});

test("provenance: query by person by keyboard; table view of the graph", async ({ page }) => {
  const { requests } = await mockSettings(page);
  await page.goto("/ui/settings/audit/provenance");
  await expect(main(page).getByText("4 items and 3 links")).toBeVisible();
  const show = main(page).getByLabel("Show");
  await show.focus();
  await show.selectOption("by_human");
  await typeInto(page, main(page).getByLabel("Person"), "operator");
  await press(page, main(page).getByRole("button", { name: "Show", exact: true }));
  await expect(page).toHaveURL(/query=by_human&human=operator/);
  await expect.poll(() => requests.some((r) => r.path === "/api/platform/provenance-graph" && r.search.includes("human=operator"))).toBe(true);
});

test("appearance: theme and density by keyboard, and the theme persists across a reload", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/ui/settings/appearance");
  const light = main(page).getByLabel("Light", { exact: true });
  await light.focus();
  await page.keyboard.press("Space");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.keyboard.press("ArrowDown");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.keyboard.press("ArrowDown");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "high-contrast");
  await press(page, main(page).getByLabel("Compact"), "Space");
  await expect(page.locator("html")).toHaveAttribute("data-density", "compact");
  await page.reload();
  await expect(h1(page, "Appearance")).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "high-contrast");
  await expect(page.locator("html")).toHaveAttribute("data-density", "compact");
  await expect(main(page).getByLabel("High contrast")).toBeChecked();
});
