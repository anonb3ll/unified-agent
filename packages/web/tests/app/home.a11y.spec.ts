import { expect as baseExpect, settleTransitions, test } from "../a11y.fixture";

const expect = baseExpect.configure({ timeout: 15_000 });
// Three themes and an axe pass per test: allow for a loaded machine.
test.describe.configure({ timeout: 180_000 });
import { emptyHub, failingHub, fullHub, type HubTable } from "./inbox/fixtures";
import { hub } from "./inbox/pw";

/**
 * Spec §7: Home passes axe (WCAG 2.2 AA) in dark, light and high contrast,
 * at 1440px and 390px, with realistic data, with an empty organization and
 * with every read failing.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;
const DATA: Record<string, { table: () => HubTable; ready: string }> = {
  realistic: { table: fullHub, ready: "Running now" },
  empty: { table: emptyHub, ready: "Get started" },
  failing: { table: failingHub, ready: "Agents could not be loaded" },
};

for (const width of WIDTHS) {
  for (const [name, data] of Object.entries(DATA)) {
    for (const path of ["/home", "/home?project=beta"]) {
      test(`${path} with ${name} data at ${width}px passes axe in every theme`, async ({ page, makeAxeBuilder }) => {
        await page.setViewportSize({ width, height: 900 });
        await hub(page, data.table());
        for (const theme of THEMES) {
          await page.addInitScript((t) => localStorage.setItem("ua-web-theme", t), theme);
          await page.goto(`/ui${path}`);
          await expect(page.getByRole("main").getByRole("heading", { level: 1, name: "Home", exact: true })).toBeVisible();
          await expect(page.getByRole("main").getByRole("heading", { name: data.ready }).first()).toBeVisible();
          await expect(page.locator("[aria-busy=true]")).toHaveCount(0);
          await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
          await settleTransitions(page);
          const results = await makeAxeBuilder().analyze();
          expect(results.violations, `${path} ${name} ${width}px ${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual([]);
        }
      });
    }
  }
}

test("the Act drawer passes axe", async ({ page, makeAxeBuilder }) => {
  await hub(page, fullHub());
  await page.goto("/ui/home");
  await page.getByRole("button", { name: /^Act on: task_delete/ }).click();
  await expect(page.getByRole("dialog", { name: "Approval" }).getByRole("button", { name: "Approve" })).toBeVisible();
  await settleTransitions(page);
  const results = await makeAxeBuilder().analyze();
  expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
});
