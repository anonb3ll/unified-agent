import { test as base, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/** Shared axe fixture: WCAG 2.2 AA tags from Wayfinder research #886. */
export const test = base.extend<{
  makeAxeBuilder: () => AxeBuilder;
}>({
  makeAxeBuilder: async ({ page }, use) => {
    await use(
      () =>
        new AxeBuilder({ page }).withTags([
          "wcag2a",
          "wcag2aa",
          "wcag21a",
          "wcag21aa",
          "wcag22aa",
        ]),
    );
  },
});

export { expect } from "@playwright/test";

/**
 * Wait for in-flight CSS transitions to finish before auditing.
 *
 * Theme switches animate through Tailwind's `transition-colors`. Auditing mid-flight
 * measures interpolated colours that belong to neither theme: on Depot CI the dark
 * switch reported `#f06666` on `#3d434a` at 3.23:1, where `#f06666` is not a token in
 * any theme (dark's `--ua-danger` is `#f87171`) and `#3d434a` sits roughly 80% of the
 * way from the light surface `#ffffff` to the dark one `#171e27`. Waiting for the
 * transitions to settle makes axe measure the real design instead of a transient frame.
 *
 * Only CSS *transitions* are awaited: a looping animation (a spinner) never settles and
 * must not stall the audit. The wait is also bounded, so a stuck transition can never
 * fail a test whose subject is contrast.
 */
export async function settleTransitions(page: Page): Promise<void> {
  await page
    .evaluate(
      () =>
        new Promise<void>((resolve) => {
          const settled = () =>
            document
              .getAnimations()
              .every((a) => !(a instanceof CSSTransition) || a.playState !== "running");

          // Two consecutive clear frames, not one: a transition begins on the style
          // recalc *after* the attribute change, so a single clear frame can be observed
          // in the gap before it starts.
          const deadline = performance.now() + 2000;
          let clearFrames = 0;
          const tick = () => {
            if (performance.now() > deadline) {
              resolve(); // bounded: a stuck or looping animation must not hang the audit
              return;
            }
            clearFrames = settled() ? clearFrames + 1 : 0;
            if (clearFrames >= 2) resolve();
            else requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
        }),
    )
    .catch(() => {
      /* bounded on purpose: an unsettled transition must not mask the contrast result */
    });
}
