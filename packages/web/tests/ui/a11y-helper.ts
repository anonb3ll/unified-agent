import axe from "axe-core";
import { expect } from "vitest";

/**
 * Validates that a rendered HTML element container contains zero
 * critical or serious WCAG 2.2 Level AA accessibility violations.
 */
export async function expectNoAxeViolations(
  container: HTMLElement,
  options?: axe.RunOptions
): Promise<void> {
  const results = await axe.run(container, {
    runOnly: {
      type: "tag",
      values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"],
    },
    rules: {
      // JSDOM does not render layout geometry or compute CSS variable colors
      "color-contrast": { enabled: false },
      ...options?.rules,
    },
    ...options,
  });

  const blocking = results.violations.filter((v) =>
    ["critical", "serious"].includes(v.impact ?? "")
  );

  expect(
    blocking,
    `Found axe violations:\n${JSON.stringify(blocking, null, 2)}`
  ).toEqual([]);
}
