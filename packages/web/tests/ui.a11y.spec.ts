import { expect, test, settleTransitions } from "./a11y.fixture";

const THEMES = ["light", "dark", "high-contrast"] as const;

for (const theme of THEMES) {
  test.describe(`UI Primitives E2E & A11y Certification (${theme})`, () => {
    test.beforeEach(async ({ page }) => {
      // Initialize theme before loading DOM
      await page.addInitScript((t) => {
        localStorage.setItem("ua-web-theme", t);
      }, theme);
      // Navigate to dedicated UI Primitives Showcase
      await page.goto("./?showcase=primitives");
      // Wait for showcase container to mount
      await expect(page.getByTestId("ui-showcase")).toBeVisible();
      // The v2 primitives load lazily; audit them only once they are mounted.
      await expect(page.getByTestId("showcase-v2")).toBeVisible();
      await expect(page.getByRole("list", { name: "Task dependencies: legend" })).toBeVisible();
      await settleTransitions(page);
    });

    test(`E2E-A11Y-01: All UI primitives meet WCAG 2.2 AA standards with zero violations (${theme})`, async ({
      page,
      makeAxeBuilder,
    }) => {
      // Run axe builder across the entire mounted showcase
      const results = await makeAxeBuilder().analyze();
      const blocking = results.violations.filter((v) =>
        ["critical", "serious", "moderate"].includes(v.impact ?? "")
      );

      expect(
        blocking,
        `Violations in ${theme} theme:\n${JSON.stringify(blocking, null, 2)}`
      ).toEqual([]);
    });

    test(`E2E-A11Y-02: Danger button hover state maintains >= 4.5:1 contrast under axe audit (${theme})`, async ({
      page,
      makeAxeBuilder,
    }) => {
      const dangerBtn = page.getByTestId("btn-danger-md");
      await expect(dangerBtn).toBeVisible();

      // Trigger real browser hover state
      await dangerBtn.hover();

      // Run axe-core specifically against the hovered danger button
      const results = await makeAxeBuilder()
        .include('[data-testid="btn-danger-md"]')
        .analyze();

      const contrastViolations = results.violations.filter(
        (v) => v.id === "color-contrast"
      );

      expect(
        contrastViolations,
        `Danger button hover contrast failed in ${theme} theme:\n${JSON.stringify(contrastViolations, null, 2)}`
      ).toEqual([]);
    });

    test(`E2E-A11Y-03: Removable Chip renders accessible sibling controls with zero nested-interactive violations (${theme})`, async ({
      page,
      makeAxeBuilder,
    }) => {
      const chipContainer = page.getByTestId("chip-removable");
      await expect(chipContainer).toBeVisible();

      const mainBtn = chipContainer.getByRole("button", { name: /active filter/i });
      const removeBtn = chipContainer.getByRole("button", { name: /remove/i });

      // Both buttons must be rendered as accessible siblings
      await expect(mainBtn).toBeVisible();
      await expect(removeBtn).toBeVisible();

      // Verify keyboard tab navigation can independently focus both buttons
      await mainBtn.focus();
      await expect(mainBtn).toBeFocused();

      await page.keyboard.press("Tab");
      await expect(removeBtn).toBeFocused();

      // Axe verification on the chip container
      const results = await makeAxeBuilder()
        .include('[data-testid="chip-removable"]')
        .analyze();

      expect(results.violations).toEqual([]);
    });

    test(`E2E-A11Y-04: Card and DataTable isolate keyboard events from child inputs and buttons (${theme})`, async ({
      page,
    }) => {
      // 1. Test Card event isolation
      const cardInput = page.getByTestId("card-nested-input");
      const cardCounter = page.getByTestId("card-click-count");

      await cardInput.focus();
      await page.keyboard.press("Space");
      await page.keyboard.press("Enter");

      // Typing inside input must NOT increment card click counter
      await expect(cardCounter).toHaveText("Card clicks: 0");

      // 2. Test DataTable event isolation
      const cellBtn = page.getByTestId("row-action-btn-1");
      const rowCounter = page.getByTestId("row-click-count");

      await cellBtn.focus();
      await page.keyboard.press("Enter");

      // Pressing Enter on cell button must NOT trigger row click
      await expect(rowCounter).toHaveText("Row clicks: 0");
    });

    test(`E2E-A11Y-05: DataTable implements true WAI-ARIA roving tabindex (${theme})`, async ({
      page,
    }) => {
      const table = page.getByTestId("interactive-datatable");
      await expect(table).toBeVisible();

      const rows = table.locator("tbody tr");
      const rowCount = await rows.count();
      expect(rowCount).toBeGreaterThan(1);

      // Verify exactly one row has tabIndex="0" initially
      const row0TabIndex = await rows.nth(0).getAttribute("tabindex");
      const row1TabIndex = await rows.nth(1).getAttribute("tabindex");

      expect(row0TabIndex).toBe("0");
      expect(row1TabIndex).toBe("-1");

      // Focus first row and press ArrowDown
      await rows.nth(0).focus();
      await page.keyboard.press("ArrowDown");

      // Verify roving focus moved to row 1
      await expect(rows.nth(1)).toBeFocused();
      expect(await rows.nth(0).getAttribute("tabindex")).toBe("-1");
      expect(await rows.nth(1).getAttribute("tabindex")).toBe("0");

      // Press ArrowUp to move focus back to row 0
      await page.keyboard.press("ArrowUp");
      await expect(rows.nth(0)).toBeFocused();
      expect(await rows.nth(0).getAttribute("tabindex")).toBe("0");
      expect(await rows.nth(1).getAttribute("tabindex")).toBe("-1");
    });

    test(`E2E-A11Y-06: Interactive primitives display 3px focus ring on keyboard focus (${theme})`, async ({
      page,
    }) => {
      const btn = page.getByTestId("btn-primary-md");
      await btn.focus();

      const outlineWidth = await btn.evaluate((el) => {
        return window.getComputedStyle(el).outlineWidth;
      });
      expect(outlineWidth).toBe("3px");
    });
  });
}

test.describe("Dynamic Theme Switching & Contrast Persistence", () => {
  test("E2E-A11Y-07: Switching themes dynamically preserves zero axe violations", async ({
    page,
    makeAxeBuilder,
  }) => {
    await page.goto("./?showcase=primitives");
    await expect(page.getByTestId("ui-showcase")).toBeVisible();

    // Verify initial theme
    const html = page.locator("html");

    // The ThemeToggle radios are REQUIRED, not optional. This test previously wrapped
    // its entire body in `if (await darkRadio.isVisible())`, so any run where the
    // control was absent passed with zero assertions — a silent false green. Assert
    // the control exists instead of skipping past it.
    const darkRadio = page.getByRole("radio", { name: /dark/i });
    await expect(darkRadio).toBeVisible();

    await darkRadio.click();
    await expect(html).toHaveAttribute("data-theme", "dark");
    await settleTransitions(page);
    let results = await makeAxeBuilder().analyze();
    expect(results.violations.filter((v) => ["critical", "serious"].includes(v.impact ?? ""))).toEqual([]);

    // Click high-contrast radio
    const hcRadio = page.getByRole("radio", { name: /high[- ]contrast/i });
    await expect(hcRadio).toBeVisible();
    await hcRadio.click();
    await expect(html).toHaveAttribute("data-theme", "high-contrast");
    await settleTransitions(page);
    results = await makeAxeBuilder().analyze();
    expect(results.violations.filter((v) => ["critical", "serious"].includes(v.impact ?? ""))).toEqual([]);
  });
});
