import { expect, test, type Locator, type Page } from "@playwright/test";
import { OAUTH_BEGIN, OAUTH_CONNECTED, OAUTH_DISCONNECTED, OAUTH_STATE, parkedBody, SLACK_LIVE, SLACK_UNAVAILABLE } from "./integrations/fixtures";
import { mockIntegrations } from "./integrations/pw";

/**
 * Spec §9.8 flows against a mocked hub (shapes from `webui/routes.py`,
 * `webui/api.py` and `connector_oauth.py`). Each flow is driven by the
 * keyboard: focus moves with Tab or a direct focus, and actions run on
 * Enter or Space, never a pointer click.
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

test("connect GitHub: the dialog names github.com, then the tab leaves for the provider (keyboard)", async ({ page }) => {
  let reached: string | null = null;
  await page.route("https://github.com/**", async (route) => {
    reached = route.request().url();
    await route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html><title>GitHub</title><h1>Authorize</h1>" });
  });
  const { writes } = await mockIntegrations(page, {
    "GET /api/connectors/oauth/github": { json: OAUTH_DISCONNECTED },
    "POST /api/connectors/oauth/github/begin": { status: 201, json: OAUTH_BEGIN },
  });
  await page.goto("/ui/settings/integrations");
  await expect(h1(page, "Integrations")).toBeVisible();
  await expect(main(page).getByTestId("oauth-github-status")).toHaveText(/Not connected/);
  await press(page, main(page).getByRole("button", { name: "Connect GitHub" }));
  const dialog = page.getByRole("dialog", { name: "Connect GitHub?" });
  await expect(dialog.getByText("You will go to github.com")).toBeVisible();
  const go = dialog.getByRole("button", { name: "Continue to github.com" });
  await expect(go).toBeFocused();
  await expect(page.getByText(OAUTH_STATE)).toHaveCount(0);
  await page.keyboard.press("Enter");
  await page.waitForURL(/^https:\/\/github\.com\/login\/oauth\/authorize/);
  expect(reached).toBe(OAUTH_BEGIN.authorize_url);
  expect(writes.filter((w) => w.path === "/api/connectors/oauth/github/begin")).toHaveLength(1);
});

test("connect refuses a sign-in address on another host and stays on the page", async ({ page }) => {
  await mockIntegrations(page, {
    "POST /api/connectors/oauth/github/begin": { status: 201, json: { provider: "github", authorize_url: `http://github.com.evil.example/x?state=${OAUTH_STATE}` } },
  });
  await page.goto("/ui/settings/integrations");
  await press(page, main(page).getByRole("button", { name: "Reconnect GitHub" }));
  const dialog = page.getByRole("dialog", { name: "Reconnect GitHub?" });
  await press(page, dialog.getByRole("button", { name: "Continue to github.com" }));
  await expect(dialog.getByText(/not a secure \(https\) address\. Nothing was opened\./)).toBeVisible();
  await expect(page).toHaveURL(/\/ui\/settings\/integrations$/);
  await expect(page.getByText(OAUTH_STATE)).toHaveCount(0);
});

test("disconnect: Escape returns focus to the trigger; confirming disconnects and the status updates (keyboard)", async ({ page }) => {
  let status: unknown = OAUTH_CONNECTED;
  const { writes } = await mockIntegrations(page, {
    "GET /api/connectors/oauth/github": async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(status) });
    },
    "POST /api/connectors/oauth/github/disconnect": async (route) => {
      status = OAUTH_DISCONNECTED;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(OAUTH_DISCONNECTED) });
    },
  });
  await page.goto("/ui/settings/integrations");
  const trigger = main(page).getByRole("button", { name: "Disconnect GitHub" });
  await press(page, trigger);
  const dialog = page.getByRole("dialog", { name: "Disconnect GitHub?" });
  await expect(dialog.getByText(/stop working until someone connects/)).toBeVisible();
  // Danger: the safe button has focus first.
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Disconnect GitHub" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(dialog).toBeHidden();
  await expect(main(page).getByTestId("oauth-github-status")).toHaveText(/Not connected/);
  await expect(page.getByRole("status").filter({ hasText: "GitHub is disconnected." })).toBeVisible();
  expect(writes.filter((w) => w.path === "/api/connectors/oauth/github/disconnect")).toHaveLength(1);
});

test("a parked disconnect waits for approval with an Inbox link and is never shown as done", async ({ page }) => {
  await mockIntegrations(page, {
    "POST /api/connectors/oauth/github/disconnect": { status: 202, json: parkedBody("connector_oauth_disconnect", 91) },
    "GET /api/approvals/91": { json: { id: 91, status: "pending", tool: "connector_oauth_disconnect", requested_by: "someone-else" } },
  });
  await page.goto("/ui/settings/integrations");
  await press(page, main(page).getByRole("button", { name: "Disconnect GitHub" }));
  const dialog = page.getByRole("dialog", { name: "Disconnect GitHub?" });
  await press(page, dialog.getByRole("button", { name: "Disconnect GitHub" }));
  await expect(dialog.getByRole("region", { name: "Approval status" })).toBeVisible();
  await expect(dialog.getByRole("link", { name: /approval/i }).first()).toHaveAttribute("href", /\/ui\/inbox\?kind=approval&item=91/);
  await expect(main(page).getByTestId("oauth-github-status")).toHaveText(/Connected/);
});

test("register a packaged connector: errors focus the field, the 202 parks with an Inbox link and is never shown as done (keyboard)", async ({ page }) => {
  const { writes } = await mockIntegrations(page, {
    "POST /api/action/connector/register-package": {
      status: 202,
      json: { status: "approval_pending", approval_id: 81, code: "approval_required", tool: "horizon2_server_register" },
    },
    "GET /api/approvals/81": { json: { id: 81, status: "pending", tool: "horizon2_server_register", requested_by: "operator" } },
  });
  await page.goto("/ui/settings/integrations");
  await expect(main(page).getByTestId("package-status-github")).toHaveText(/Working/);
  await press(page, main(page).getByRole("button", { name: "Register a connector" }));
  const dialog = page.getByRole("dialog", { name: "Register a connector" });
  await expect(dialog).toBeVisible();
  await press(page, dialog.getByRole("button", { name: "Register" }));
  await expect(dialog.getByLabel(/^Package/)).toBeFocused();
  await dialog.getByLabel(/^Package/).selectOption("int-gcp-agent20");
  await dialog.getByLabel(/^Where it comes from/).fill("packaged:int-gcp-agent20@0.9.2");
  await dialog.getByLabel(/^Review reference/).fill("task-2478");
  await press(page, dialog.getByRole("button", { name: "Register" }));
  await expect(dialog.getByRole("region", { name: "Approval status" })).toBeVisible();
  await expect(dialog.getByRole("link", { name: /approval/i }).first()).toHaveAttribute("href", /\/ui\/inbox\?kind=approval&item=81/);
  await expect(page.getByText("The connector is registered.")).toHaveCount(0);
  expect(writes.filter((w) => w.path === "/api/action/connector/register-package").map((w) => w.body)).toEqual([
    { package: "int-gcp-agent20", provenance: "packaged:int-gcp-agent20@0.9.2", review_ref: "task-2478" },
  ]);
});

test("revoke a server: the confirm names it, a coded refusal is explained in plain words (keyboard)", async ({ page }) => {
  const { writes } = await mockIntegrations(page, {
    "POST /api/action/connector/revoke": { status: 404, json: { error: "unregistered horizon 2 server: github-main", code: "not_found" } },
  });
  await page.goto("/ui/settings/integrations");
  await press(page, main(page).getByRole("button", { name: "Revoke github-main" }));
  const dialog = page.getByRole("dialog", { name: "Revoke github-main?" });
  await expect(dialog.getByText("github-main (github)")).toBeVisible();
  await press(page, dialog.getByRole("button", { name: "Revoke github-main" }));
  await expect(dialog.getByText(/^github-main is not registered on this hub/)).toBeVisible();
  await expect(dialog.getByText("unregistered horizon 2 server: github-main", { exact: false })).toBeHidden();
  expect(writes.filter((w) => w.path === "/api/action/connector/revoke").map((w) => w.body)).toEqual([{ server_id: "github-main" }]);
  await page.keyboard.press("Escape");
  await expect(main(page).getByRole("button", { name: "Revoke github-main" })).toBeFocused();
});

test("integrations sections: tabs move between Connectors, Slack, Zulip and Notifications (keyboard)", async ({ page }) => {
  const { writes } = await mockIntegrations(page);
  await page.goto("/ui/settings/integrations");
  await expect(main(page).getByTestId("package-status-github")).toHaveText(/Working/);
  expect(writes).toHaveLength(0);

  const tabs = main(page).getByRole("navigation", { name: "Integrations sections" });
  await expect(tabs.getByRole("link", { name: "Connectors" })).toHaveAttribute("aria-current", "page");
  await press(page, tabs.getByRole("link", { name: "Slack" }));
  await expect(h1(page, "Slack")).toBeVisible();
  await expect(page).toHaveURL(/\/ui\/settings\/integrations\/slack$/);
  await press(page, main(page).getByRole("navigation", { name: "Integrations sections" }).getByRole("link", { name: "Zulip" }));
  await expect(h1(page, "Zulip")).toBeVisible();
  await expect(main(page).getByRole("link", { name: "Task 2478" })).toHaveAttribute("href", "/ui/work/tasks/2478");
  await press(page, main(page).getByRole("navigation", { name: "Integrations sections" }).getByRole("link", { name: "Notifications" }));
  await expect(h1(page, "Notifications")).toBeVisible();
  await expect(main(page).getByText("The last 2 notifications did not arrive")).toBeVisible();
});

test("slack: a failed read shows a plain error with Retry; Retry recovers (keyboard)", async ({ page }) => {
  let body: unknown = SLACK_UNAVAILABLE;
  await mockIntegrations(page, {
    "GET /api/slack_gateway": async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    },
  });
  await page.goto("/ui/settings/integrations/slack");
  await expect(main(page).getByRole("heading", { name: /Could not load the Slack status/ })).toBeVisible();
  await expect(main(page).getByText("slack gateway status unavailable")).toBeHidden();
  body = SLACK_LIVE;
  await press(page, main(page).getByRole("button", { name: "Retry" }));
  await expect(main(page).getByTestId("slack-status")).toHaveText(/Working/);
});

test("command palette: the integrations pages are reachable by name", async ({ page }) => {
  await mockIntegrations(page);
  await page.goto("/ui/home");
  await expect(h1(page, "Home")).toBeVisible();
  await page.keyboard.press("Control+k");
  const input = page.getByRole("combobox", { name: "Search pages and commands" });
  await expect(input).toBeFocused();
  await input.fill("notification delivery");
  await expect(page.getByRole("option", { name: /Notification delivery/ })).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(h1(page, "Notifications")).toBeVisible();
});
