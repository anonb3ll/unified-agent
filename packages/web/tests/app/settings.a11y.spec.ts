import { expect, settleTransitions, test } from "../a11y.fixture";
import { emptyReads, failingReads, mockSettings } from "./settings/pw";

/**
 * Spec §7: every Settings page passes axe (WCAG 2.2 AA) in dark, light and
 * high contrast, at 1440px and 390px, with realistic data, with empty data
 * and with failing reads.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;

type State = "data" | "empty" | "failing";

const ROUTES: { path: string; title: string; ready: Record<State, string | RegExp> }[] = [
  { path: "/settings/health", title: "Health", ready: { data: "Meaning-based memory search", empty: /reported none/, failing: "The health report could not be loaded" } },
  { path: "/settings/platform", title: "Platform", ready: { data: "open → doing → review → done", empty: "No object kinds", failing: "The object kinds could not be loaded" } },
  { path: "/settings/cloud", title: "Cloud", ready: { data: "This is a partial reading.", empty: "Could not read", failing: "The cloud project could not be loaded" } },
  { path: "/settings/usage", title: "Usage", ready: { data: "monthly-api", empty: "No model lanes", failing: "Model availability could not be loaded" } },
  { path: "/settings/credentials", title: "Credentials", ready: { data: "Present", empty: "No provider credentials", failing: "Provider credentials could not be loaded" } },
  { path: "/settings/audit", title: "Audit", ready: { data: "The ledger is intact", empty: "The ledger has no entries yet", failing: "The evidence ledger could not be loaded" } },
  { path: "/settings/audit/provenance", title: "Provenance", ready: { data: "4 items and 3 links", empty: "Nothing matches", failing: "The provenance graph could not be loaded" } },
  { path: "/settings/appearance", title: "Appearance", ready: { data: "Same as this device", empty: "Same as this device", failing: "Same as this device" } },
];

test.describe.configure({ timeout: 150_000 });

for (const state of ["data", "empty", "failing"] as const) {
  for (const width of WIDTHS) {
    for (const route of ROUTES) {
      test(`${route.path} with ${state} reads at ${width}px passes axe in every theme`, async ({ page, makeAxeBuilder }) => {
        await page.setViewportSize({ width, height: 900 });
        await mockSettings(page, state === "data" ? {} : state === "empty" ? emptyReads() : failingReads());
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

test("the revoke confirm dialog passes axe in every theme", async ({ page, makeAxeBuilder }) => {
  await mockSettings(page);
  for (const theme of THEMES) {
    await page.addInitScript((t) => localStorage.setItem("ua-web-theme", t), theme);
    await page.goto("/ui/settings/credentials");
    const main = page.getByRole("main");
    await main.getByLabel(/Credential id/).fill("0123456789abcdef");
    await main.getByRole("button", { name: "Revoke…" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await settleTransitions(page);
    const results = await makeAxeBuilder().analyze();
    expect(results.violations, `${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual([]);
  }
});
