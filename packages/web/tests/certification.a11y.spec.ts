import { expect, settleTransitions, test } from "./a11y.fixture";
import { LEGACY_VIEW_ALIASES, LEGACY_VIEW_PATHS } from "../src/app/lib/legacyRedirect";
import { DESTINATIONS, ROUTE_REDIRECTS, ROUTE_SAMPLES, destinationFor } from "../src/app/routes";
import { mockHub } from "./app/support/mockHub";

/**
 * WCAG 2.2 AA certification of the Control Center after the cutover (spec
 * §10). `tests/nav.test.ts` checks these lists against the spec itself; this
 * proves them in a browser:
 *
 * - each of the eight destinations, in light, dark and high contrast, shows
 *   exactly the eight destinations in the sidebar, marks its own as current
 *   and has no axe violation;
 * - every route in spec §3 opens its page under its destination;
 * - every classic `?view=` id and alias (spec §11) opens its new page.
 *
 * Every route at both widths and in every theme is audited by
 * `tests/app/shell.a11y.spec.ts`; this file does not repeat that matrix.
 */
const THEMES = ["light", "dark", "high-contrast"] as const;
const TITLE_OF = new Map(ROUTE_SAMPLES.map((r) => [r.path, r.title]));

test.use({ viewport: { width: 1440, height: 900 } });
test.describe.configure({ timeout: 180_000 });
expect.configure({ timeout: 15_000 });

const heading = (page: import("@playwright/test").Page, name: string) =>
  page.getByRole("main").getByRole("heading", { level: 1, name, exact: true });

test.beforeEach(async ({ page }) => {
  await mockHub(page);
});

for (const theme of THEMES) {
  for (const d of DESTINATIONS) {
    test(`${d.label} is certified WCAG 2.2 AA and lists the eight destinations (${theme})`, async ({ page, makeAxeBuilder }) => {
      await page.addInitScript((t) => localStorage.setItem("ua-web-theme", t), theme);
      const target = ROUTE_REDIRECTS.find((r) => r.from === d.path)?.to ?? d.path;
      await page.goto(`/ui${d.path}`);
      await expect(heading(page, TITLE_OF.get(target)!)).toBeVisible();
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);

      const links = page.getByRole("navigation", { name: "Primary" }).getByRole("link");
      await expect(links).toHaveCount(DESTINATIONS.length);
      for (const [index, other] of DESTINATIONS.entries()) {
        await expect(links.nth(index)).toHaveAttribute("href", `/ui${other.path}`);
        await expect(links.nth(index)).toHaveAccessibleName(new RegExp(`^${other.label}`));
      }
      await expect(links.nth(DESTINATIONS.indexOf(d))).toHaveAttribute("aria-current", "page");

      await settleTransitions(page);
      const results = await makeAxeBuilder().analyze();
      expect(results.violations, `${d.path} ${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual([]);
    });
  }
}

for (const d of DESTINATIONS) {
  const routes = [
    ...ROUTE_SAMPLES.filter((r) => destinationFor(r.path)?.id === d.id).map((r) => ({ from: r.path, to: r.path })),
    ...ROUTE_REDIRECTS.filter((r) => destinationFor(r.from)?.id === d.id),
  ];
  test(`every ${d.label} route in spec §3 opens its page (${routes.length})`, async ({ page }) => {
    const current = page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: new RegExp(`^${d.label}`) });
    for (const { from, to } of routes) {
      await page.goto(`/ui${from}`);
      await expect(page, from).toHaveURL((url) => url.pathname === `/ui${to}`);
      await expect(heading(page, TITLE_OF.get(to)!), from).toBeVisible();
      await expect(current, from).toHaveAttribute("aria-current", "page");
    }
  });
}

test("every classic ?view= id and alias, and #id, opens its new page (spec §11)", async ({ page }) => {
  const entries = [
    ...Object.entries(LEGACY_VIEW_PATHS),
    ...Object.entries(LEGACY_VIEW_ALIASES).map(([alias, id]) => [alias, LEGACY_VIEW_PATHS[id]] as const),
  ];
  for (const [index, [id, target]] of entries.entries()) {
    // Alternate the two classic forms; legacy-redirect.test.ts covers both for every id.
    await page.goto(index % 2 === 0 ? `/ui/?view=${id}` : `/ui/#${id}`);
    const pathname = target.split("?")[0];
    await expect(page, id).toHaveURL((url) => `${url.pathname}${url.search}` === `/ui${target}`);
    await expect(heading(page, TITLE_OF.get(pathname)!), id).toBeVisible();
  }
});
