import { expect, test } from "@playwright/test";
import { DESTINATIONS } from "../../src/app/routes";
import { HUB_DEFAULTS, mockHub } from "./support/mockHub";

test.use({ viewport: { width: 1440, height: 900 } });
// Areas load lazily; on a loaded desktop the first paint of a page can exceed the 5 s default.
test.describe.configure({ timeout: 60_000 });
expect.configure({ timeout: 15_000 });

const heading = (page: import("@playwright/test").Page, name: string) =>
  page.getByRole("main").getByRole("heading", { level: 1, name, exact: true });

const TITLES: Record<string, string> = { "/knowledge": "Memory", "/settings": "Health" };

test.beforeEach(async ({ page }) => {
  await mockHub(page);
});

test("/ui/ opens Home in the new app (spec §10)", async ({ page }) => {
  await page.goto("/ui/");
  await expect(page).toHaveURL(/\/ui\/home$/);
  await expect(heading(page, "Home")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /^Home/ })).toHaveAttribute("aria-current", "page");
});

test("a classic ?view= link opens its new page, keeping what it pointed at (spec §11)", async ({ page }) => {
  await page.goto("/ui/?view=approvals&approval=42");
  await expect(page).toHaveURL(/\/ui\/inbox\?item=42&kind=approval$/);
  await expect(heading(page, "Inbox")).toBeVisible();
  // The redirect replaces the classic URL: Back does not bounce through it.
  await page.goto("/ui/?view=kanban");
  await expect(page).toHaveURL(/\/ui\/work\?view=board$/);
  await expect(heading(page, "Work")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/ui\/inbox\?item=42&kind=approval$/);
});

test("a classic #view link and an unknown ?view= id both land on a page (spec §11)", async ({ page }) => {
  await page.goto("/ui/#credential_pool");
  await expect(page).toHaveURL(/\/ui\/settings\/credentials$/);
  await expect(heading(page, "Credentials")).toBeVisible();
  await page.goto("/ui/?view=no-such-view");
  await expect(page).toHaveURL(/\/ui\/home$/);
  await expect(heading(page, "Home")).toBeVisible();
});

test("navigates the eight destinations by click", async ({ page }) => {
  await page.goto("/ui/home");
  const nav = page.getByRole("navigation", { name: "Primary" });
  for (const d of DESTINATIONS) {
    await nav.getByRole("link", { name: new RegExp(`^${d.label}`) }).click();
    await expect(heading(page, TITLES[d.path] ?? d.label)).toBeVisible();
    await expect(nav.getByRole("link", { name: new RegExp(`^${d.label}`) })).toHaveAttribute("aria-current", "page");
    await expect(page).toHaveTitle(`${TITLES[d.path] ?? d.label} · Control Center`);
  }
});

test("navigates the eight destinations by keyboard shortcut and focuses the h1", async ({ page }) => {
  await page.goto("/ui/home");
  await expect(heading(page, "Home")).toBeVisible();
  for (const d of DESTINATIONS) {
    await page.keyboard.press("g");
    await page.keyboard.press(d.shortcut);
    const h1 = heading(page, TITLES[d.path] ?? d.label);
    await expect(h1).toBeVisible();
    if (d.id !== "home") await expect(h1).toBeFocused();
  }
});

test("the skip link is first in the tab order", async ({ page }) => {
  await page.goto("/ui/work");
  await expect(heading(page, "Work")).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();
});

test("back and forward restore pages; a deep link survives reload", async ({ page }) => {
  await page.goto("/ui/work");
  await page.getByRole("navigation", { name: "Work sections" }).getByRole("link", { name: "Goals" }).click();
  await expect(heading(page, "Goals")).toBeVisible();
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /^Runs/ }).click();
  await expect(heading(page, "Runs")).toBeVisible();
  await page.goBack();
  await expect(heading(page, "Goals")).toBeVisible();
  await page.goBack();
  await expect(heading(page, "Work")).toBeVisible();
  await page.goForward();
  await expect(heading(page, "Goals")).toBeVisible();
  await page.reload();
  await expect(heading(page, "Goals")).toBeVisible();
  await expect(page).toHaveURL(/\/ui\/work\/goals$/);
});

test("unknown paths show not found with the destinations", async ({ page }) => {
  await page.goto("/ui/nowhere/at-all");
  await expect(heading(page, "Page not found")).toBeVisible();
  await page.getByRole("main").getByRole("link", { name: /^Agents/ }).click();
  await expect(heading(page, "Agents")).toBeVisible();
});

test("command palette: Ctrl+K, filter, Enter; Escape returns focus", async ({ page }) => {
  await page.goto("/ui/home");
  await expect(heading(page, "Home")).toBeVisible();
  await page.keyboard.press("Control+k");
  const input = page.getByRole("combobox", { name: "Search pages and commands" });
  await expect(input).toBeFocused();
  await input.fill("provenance");
  await page.keyboard.press("Enter");
  await expect(heading(page, "Provenance")).toBeVisible();
  await expect(page).toHaveURL(/\/ui\/settings\/audit\/provenance$/);

  const search = page.getByRole("button", { name: /^Search or jump to/ });
  await search.click();
  await expect(input).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(input).toBeHidden();
  await expect(search).toBeFocused();
});

test("the New menu works from the keyboard", async ({ page }) => {
  await page.goto("/ui/home");
  const trigger = page.getByRole("button", { name: "New" });
  await trigger.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("menuitem", { name: "Task" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(heading(page, "New task")).toBeVisible();
});

test("sign-in returns to the requested URL", async ({ page }) => {
  let signedIn = false;
  await mockHub(page, {
    "GET /api/whoami": async (route) => {
      await route.fulfill(
        signedIn
          ? { status: 200, contentType: "application/json", body: JSON.stringify(HUB_DEFAULTS["GET /api/whoami"].json) }
          : { status: 401, contentType: "application/json", body: JSON.stringify({ error: "unauthorized" }) },
      );
    },
    "POST /api/session": async (route) => {
      signedIn = true;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ agent: "operator", csrf: "c" }) });
    },
  });
  await page.goto("/ui/agents/hosts");
  await page.getByLabel(/Bearer Token/i).fill("t0ken");
  await page.getByRole("button", { name: "Sign In" }).click();
  await expect(heading(page, "Hosts")).toBeVisible();
  await expect(page).toHaveURL(/\/ui\/agents\/hosts$/);
});

test.describe("phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test("bottom tab bar and More menu", async ({ page }) => {
    await page.goto("/ui/home");
    const bar = page.getByRole("navigation", { name: "Primary" });
    await bar.getByRole("link", { name: /^Work/ }).click();
    await expect(heading(page, "Work")).toBeVisible();
    await bar.getByRole("button", { name: "More destinations" }).click();
    await page.getByRole("menuitem", { name: "Knowledge" }).click();
    await expect(heading(page, "Memory")).toBeVisible();
  });
});

test("an app chunk that fails to load shows Reload, not a blank page", async ({ page }) => {
  await page.route(/\/assets\/App-[^/]+\.js$/, (route) => route.abort());
  await page.goto("/ui/home");
  await expect(page.getByRole("heading", { level: 1, name: "The Control Center could not load" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reload" })).toBeVisible();
});

test("WorkOS sign-in returns to the requested URL, not the landing page", async ({ page }) => {
  let signedIn = false;
  await mockHub(page, {
    "GET /api/session/options": { json: { workos: true } },
    "GET /api/whoami": async (route) => {
      await route.fulfill(
        signedIn
          ? { status: 200, contentType: "application/json", body: JSON.stringify(HUB_DEFAULTS["GET /api/whoami"].json) }
          : { status: 401, contentType: "application/json", body: JSON.stringify({ error: "unauthorized" }) },
      );
    },
    // The hub's callback signs the browser in and lands on /ui/.
    "GET /api/session/workos/login": async (route) => {
      signedIn = true;
      await route.fulfill({ status: 302, headers: { Location: "/ui/" } });
    },
  });
  await page.goto("/ui/agents/hosts?tab=nodes");
  await page.getByRole("link", { name: /Sign in with WorkOS/i }).click();
  await expect(heading(page, "Hosts")).toBeVisible();
  await expect(page).toHaveURL(/\/ui\/agents\/hosts\?tab=nodes$/);
  // Consumed once: a later visit to /ui/ opens Home.
  await page.goto("/ui/");
  await expect(page).toHaveURL(/\/ui\/home$/);
  await expect(heading(page, "Home")).toBeVisible();
});

test("a danger ConfirmDialog with a reason field: Enter does not confirm, the button does (items 4, 5, finding 8)", async ({ page }) => {
  await page.goto("/ui/?showcase=primitives");
  await page.getByRole("button", { name: "Deny with a reason" }).click();
  const dialog = page.getByRole("dialog", { name: "Deny this approval?" });
  const reason = dialog.getByRole("textbox", { name: /Reason/ });
  await expect(reason).toBeFocused();
  await expect(dialog.getByRole("button", { name: "Deny" })).toBeDisabled();
  await reason.fill("Out of scope");
  await page.keyboard.press("Enter");
  await expect(dialog).toBeVisible();
  await expect(page.getByText("Denied: Out of scope")).toHaveCount(0);
  // The submit button itself submits (a native button, item 4).
  await dialog.getByRole("button", { name: "Deny" }).focus();
  await page.keyboard.press("Enter");
  await expect(dialog).toBeHidden();
  await expect(page.getByText("Denied: Out of scope")).toBeVisible();
});
