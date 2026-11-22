import { expect, settleTransitions, test } from "../a11y.fixture";
import { mockHub } from "./support/mockHub";
import { EMPTY, FAILING, REALISTIC, RUN_ID } from "./runs/hubData";

/**
 * Spec §7 / §13: every Runs page passes axe (WCAG 2.2 AA) in dark, light and
 * high contrast, at 1440px and 390px, with realistic data, empty data and
 * failing reads.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;
const PAGES = [
  { path: "/runs", title: "Runs" },
  { path: "/runs/workflows?workflow=smoke-echo", title: "Workflows" },
  { path: "/runs/graphs", title: "Graphs" },
  { path: `/runs/graphs?run=${RUN_ID}`, title: "Graphs" },
  { path: "/runs/routines", title: "Routines" },
  { path: "/runs/cycles", title: "Cycles" },
  { path: "/runs/cycles/c0ffee123456", title: "Cycle c0ffee123456" },
  { path: "/runs/receipts", title: "Receipts" },
  { path: "/runs/telemetry", title: "Telemetry" },
  { path: "/runs/insights?task=42", title: "Insights" },
  { path: "/runs/executor", title: "Executor" },
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
          await expect(browser.getByRole("main").locator('[aria-busy="true"]')).toHaveCount(0, { timeout: 15_000 });
          await settleTransitions(browser);
          const results = await makeAxeBuilder().analyze();
          expect(results.violations, `${page.path} ${dataName} ${width}px ${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual([]);
        }
      });
    }
  }
}

test("the Runs dialogs pass axe while open", async ({ page, makeAxeBuilder }) => {
  test.slow();
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockHub(page, REALISTIC);
  await page.goto("/ui/runs/routines");
  await page.getByRole("main").getByRole("button", { name: "New routine" }).first().click();
  await page.getByRole("textbox", { name: /Schedule/ }).fill("0 9 * * 1");
  await expect(page.getByText("The next three runs would be:")).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
  await page.keyboard.press("Escape");
  await page.goto("/ui/runs/cycles/c0ffee123456");
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(page.getByRole("dialog", { name: "Stop this cycle for good?" })).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
  await page.keyboard.press("Escape");
  await page.goto("/ui/runs/cycles");
  await page.getByRole("button", { name: "Start a cycle" }).click();
  await expect(page.getByRole("dialog", { name: "Start a cycle" })).toBeVisible();
  await settleTransitions(page);
  expect((await makeAxeBuilder().analyze()).violations).toEqual([]);
});
