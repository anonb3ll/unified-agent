import { expect, settleTransitions, test } from "../a11y.fixture";
import { emptyReads, failingReads, mockIntegrations } from "./integrations/pw";

/**
 * Spec §7: every integrations page passes axe (WCAG 2.2 AA) in dark, light
 * and high contrast, at 1440px and 390px, with realistic data, with empty
 * data and with failing reads.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;

type State = "data" | "empty" | "failing";

const ROUTES: { path: string; title: string; ready: Record<State, string | RegExp> }[] = [
  {
    path: "/settings/integrations",
    title: "Integrations",
    ready: { data: "Missing credentials: GCP_SERVICE_ACCOUNT", empty: "No connectors yet", failing: "Could not load the connector registry" },
  },
  { path: "/settings/integrations/slack", title: "Slack", ready: { data: "Reply failed", empty: "How to turn it on", failing: "Could not load the Slack status" } },
  {
    path: "/settings/integrations/zulip",
    title: "Zulip",
    ready: { data: "Task 2478", empty: "No Zulip messages have been received.", failing: "Could not load the Zulip status" },
  },
  {
    path: "/settings/integrations/notifications",
    title: "Notifications",
    ready: { data: "The last 2 notifications did not arrive", empty: "No notifications sent yet", failing: "Could not load notification delivery" },
  },
];

test.describe.configure({ timeout: 150_000 });

for (const state of ["data", "empty", "failing"] as const) {
  for (const width of WIDTHS) {
    for (const route of ROUTES) {
      test(`${route.path} with ${state} reads at ${width}px passes axe in every theme`, async ({ page, makeAxeBuilder }) => {
        await page.setViewportSize({ width, height: 900 });
        await mockIntegrations(page, state === "data" ? {} : state === "empty" ? emptyReads() : failingReads());
        for (const theme of THEMES) {
          await page.addInitScript((t) => localStorage.setItem("ua-web-theme", t), theme);
          await page.goto(`/ui${route.path}`);
          const main = page.getByRole("main");
          await expect(main.getByRole("heading", { level: 1, name: route.title, exact: true })).toBeVisible({ timeout: 20_000 });
          await expect(main.getByText(route.ready[state]).first()).toBeVisible({ timeout: 20_000 });
          await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
          await settleTransitions(page);
          const results = await makeAxeBuilder().analyze();
          expect(results.violations, `${route.path} ${state} ${width}px ${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual([]);
        }
      });
    }
  }
}

test("the disconnect, connect, register, authorize and revoke dialogs pass axe", async ({ page, makeAxeBuilder }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await mockIntegrations(page);
  await page.goto("/ui/settings/integrations");
  const main = page.getByRole("main");
  await main.getByRole("button", { name: "Disconnect GitHub" }).click();
  await expect(page.getByRole("dialog", { name: "Disconnect GitHub?" })).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
  await page.keyboard.press("Escape");
  await main.getByRole("button", { name: "Reconnect GitHub" }).click();
  await expect(page.getByRole("dialog", { name: "Reconnect GitHub?" })).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
  await page.keyboard.press("Escape");
  await main.getByRole("button", { name: "Register a connector" }).click();
  const register = page.getByRole("dialog", { name: "Register a connector" });
  await expect(register).toBeVisible();
  // With its validation errors shown.
  await register.getByRole("button", { name: "Register" }).click();
  await expect(register.getByText("Choose the package to register.")).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
  await page.keyboard.press("Escape");
  await main.getByRole("button", { name: "Authorize github-main" }).click();
  await expect(page.getByRole("dialog", { name: "Authorize github-main" })).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
  await page.keyboard.press("Escape");
  await main.getByRole("button", { name: "Revoke github-main" }).click();
  await expect(page.getByRole("dialog", { name: "Revoke github-main?" })).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
});
