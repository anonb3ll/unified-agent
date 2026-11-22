import { expect, settleTransitions, test } from "../a11y.fixture";
import { ROUTE_SAMPLES } from "../../src/app/routes";
import { mockHub } from "./support/mockHub";

/**
 * Spec §7: every v2 route passes axe (WCAG 2.2 AA) in dark, light and high
 * contrast, at 1440px and 390px wide. Pages are stubs until their slice
 * lands; this proves the shell, primitives and stubs.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;

// Three page loads and three axe runs per test; allow for a loaded desktop.
test.describe.configure({ timeout: 120_000 });

for (const width of WIDTHS) {
  for (const route of ROUTE_SAMPLES) {
    test(`${route.path} at ${width}px passes axe in every theme`, async ({ page, makeAxeBuilder }) => {
      await page.setViewportSize({ width, height: 900 });
      await mockHub(page);
      for (const theme of THEMES) {
        await page.addInitScript((t) => localStorage.setItem("ua-web-theme", t), theme);
        await page.goto(`/ui${route.path}`);
        await expect(page.getByRole("main").getByRole("heading", { level: 1, name: route.title, exact: true })).toBeVisible({ timeout: 15_000 });
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        await settleTransitions(page);
        const results = await makeAxeBuilder().analyze();
        expect(results.violations, `${route.path} ${width}px ${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual([]);
      }
    });
  }
}
