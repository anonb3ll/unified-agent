import { expect, settleTransitions, test } from "../a11y.fixture";
import { NOTE_KEY } from "./knowledge/fixtures";
import { emptyReads, failingReads, mockKnowledge } from "./knowledge/pw";

/**
 * Spec §7: every Knowledge page passes axe (WCAG 2.2 AA) in dark, light and
 * high contrast, at 1440px and 390px, with realistic data, with empty data
 * and with failing reads.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;

type State = "data" | "empty" | "failing";

const note = encodeURIComponent(NOTE_KEY);

const ROUTES: { path: string; title: string; ready: Record<State, string | RegExp> }[] = [
  { path: "/knowledge/memory", title: "Memory", ready: { data: "Deploy steward owns hub restarts", empty: "No memory notes yet", failing: "Memory notes could not be loaded" } },
  { path: `/knowledge/memory?note=${note}`, title: "Memory", ready: { data: "Who restarts the hub", empty: "This note does not exist", failing: "The note could not be loaded" } },
  { path: "/knowledge/memory?new=1", title: "Memory", ready: { data: "New memory note", empty: "New memory note", failing: "New memory note" } },
  { path: `/knowledge/memory?note=${note}&edit=1`, title: "Memory", ready: { data: /Desktop_Agent1 wrote this note/, empty: "This note does not exist", failing: "The note could not be loaded" } },
  { path: "/knowledge/vault", title: "Vault", ready: { data: "Opened PR #1510 for the kernel batch.", empty: "No journal entries today", failing: "The journal could not be loaded" } },
  { path: "/knowledge/vault?path=Wiki%2FDeploys.v2.md&write=append", title: "Vault", ready: { data: "Text to add", empty: "This note does not exist", failing: "The note could not be loaded" } },
  { path: "/knowledge/vault?q=steward", title: "Vault", ready: { data: "2 matching lines", empty: "No notes match", failing: "The search could not run" } },
  { path: "/knowledge/vault?write=new", title: "Vault", ready: { data: "New vault note", empty: "New vault note", failing: "New vault note" } },
  { path: "/knowledge/vault?write=journal", title: "Vault", ready: { data: /of 240 characters/, empty: /of 240 characters/, failing: /of 240 characters/ } },
];

test.describe.configure({ timeout: 150_000 });

for (const state of ["data", "empty", "failing"] as const) {
  for (const width of WIDTHS) {
    for (const route of ROUTES) {
      test(`${route.path} with ${state} reads at ${width}px passes axe in every theme`, async ({ page, makeAxeBuilder }) => {
        await page.setViewportSize({ width, height: 900 });
        await mockKnowledge(page, state === "data" ? {} : state === "empty" ? emptyReads() : failingReads());
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
