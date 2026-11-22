import { expect, settleTransitions, test } from "../a11y.fixture";
import { mockHub } from "./support/mockHub";
import { EMPTY, FAILING, REALISTIC } from "./sessions/hubData";

/**
 * Spec §7 / §13: every Sessions page passes axe (WCAG 2.2 AA) in dark, light
 * and high contrast, at 1440px and 390px, with realistic data, empty data
 * and failing reads.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;
const PAGES = [
  { path: "/sessions", title: "Sessions" },
  { path: "/sessions?tab=devices", title: "Sessions" },
  { path: "/sessions?tab=teams", title: "Sessions" },
  { path: "/sessions?tab=activity", title: "Sessions" },
  { path: "/sessions/s-live", title: "Session s-live" },
  { path: "/sessions/new", title: "Start a session" },
] as const;
const DATA = { realistic: REALISTIC, empty: EMPTY, failing: FAILING } as const;

for (const width of WIDTHS) {
  for (const [dataName, table] of Object.entries(DATA)) {
    for (const page of PAGES) {
      test(`${page.path} with ${dataName} data at ${width}px passes axe in every theme`, async ({ page: browser, makeAxeBuilder }) => {
        test.slow();
        await browser.setViewportSize({ width, height: 900 });
        await mockHub(browser, table);
        for (const theme of THEMES) {
          await browser.addInitScript((t) => localStorage.setItem("ua-web-theme", t), theme);
          await browser.goto(`/ui${page.path}`);
          await expect(browser.getByRole("main").getByRole("heading", { level: 1, name: page.title, exact: true })).toBeVisible();
          await expect(browser.locator("html")).toHaveAttribute("data-theme", theme);
          // Wait until every region has left its loading state.
          await expect(browser.getByRole("main").locator('[aria-busy="true"]')).toHaveCount(0, { timeout: 15_000 });
          await settleTransitions(browser);
          const results = await makeAxeBuilder().analyze();
          expect(results.violations, `${page.path} ${dataName} ${width}px ${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual([]);
        }
      });
    }
  }
}

test("the session page with its crew view and a dialog open passes axe", async ({ page, makeAxeBuilder }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockHub(page, REALISTIC);
  await page.goto("/ui/sessions/s-live");
  await page.getByRole("button", { name: "Open crew view" }).click();
  await expect(page.getByRole("dialog", { name: "Crew view" })).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Hand off" }).click();
  await expect(page.getByRole("dialog", { name: "Hand off to another agent" })).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
});
