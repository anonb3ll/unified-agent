import { expect as baseExpect, settleTransitions, test } from "../a11y.fixture";

const expect = baseExpect.configure({ timeout: 15_000 });
// Three themes and an axe pass per test: allow for a loaded machine.
test.describe.configure({ timeout: 180_000 });
import { emptyHub, failingHub, fullHub, type HubTable } from "./inbox/fixtures";
import { hub } from "./inbox/pw";

/**
 * Spec §7: every Inbox page and item kind passes axe (WCAG 2.2 AA) in dark,
 * light and high contrast, at 1440px and 390px, with realistic data, empty
 * data and failing reads.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;

type Case = { path: string; title: string; ready: string };
const REALISTIC: Case[] = [
  { path: "/inbox", title: "Inbox", ready: "list:Items waiting on you" },
  { path: "/inbox?kind=approval&item=12", title: "Inbox", ready: "What will run" },
  { path: "/inbox?kind=approval&item=wake%3Asess-1", title: "Inbox", ready: "Session" },
  { path: "/inbox?kind=message&item=reply%3Amsg-1", title: "Inbox", ready: "Desktop_Agent2 asks" },
  { path: "/inbox?kind=hold&item=red_tier_hold%3A77", title: "Inbox", ready: "Note" },
  { path: "/inbox?kind=proposal", title: "Inbox", ready: "list:Items waiting on you" },
  { path: "/inbox?kind=proposal&item=org%3Aorgprop-1", title: "Inbox", ready: "Proposed changes" },
  { path: "/inbox?kind=proposal&item=memory%3Aprop-1", title: "Inbox", ready: "What it says" },
  { path: "/inbox?kind=anomaly&item=alert%3Astale_claim%3A55", title: "Inbox", ready: "task 55: Claim has not moved for 3 hours" },
  { path: "/inbox/messages?conversation=conv-1", title: "Messages", ready: "Hand off to a task" },
  { path: "/inbox/messages?compose=1", title: "Messages", ready: "New message" },
];
const EMPTY: Case[] = [
  { path: "/inbox", title: "Inbox", ready: "Nothing is waiting on you" },
  { path: "/inbox?kind=approval", title: "Inbox", ready: "No approvals are waiting" },
  { path: "/inbox/messages", title: "Messages", ready: "No conversations" },
];
const FAILING: Case[] = [
  { path: "/inbox", title: "Inbox", ready: "The Inbox could not be loaded" },
  { path: "/inbox?kind=approval&item=12", title: "Inbox", ready: "The approval could not be loaded" },
  { path: "/inbox/messages", title: "Messages", ready: "Conversations could not be loaded" },
];
const SETS: [string, () => HubTable, Case[]][] = [
  ["realistic", fullHub, REALISTIC],
  ["empty", emptyHub, EMPTY],
  ["failing", failingHub, FAILING],
];

for (const width of WIDTHS) {
  for (const [name, table, cases] of SETS) {
    for (const c of cases) {
      test(`${c.path} with ${name} data at ${width}px passes axe in every theme`, async ({ page, makeAxeBuilder }) => {
        await page.setViewportSize({ width, height: 900 });
        await hub(page, table());
        for (const theme of THEMES) {
          await page.addInitScript((t) => localStorage.setItem("ua-web-theme", t), theme);
          await page.goto(`/ui${c.path}`);
          await expect(page.getByRole("main").getByRole("heading", { level: 1, name: c.title, exact: true })).toBeVisible();
          const ready = c.ready.startsWith("list:")
            ? page.getByRole("main").getByRole("list", { name: c.ready.slice(5) })
            : page.getByRole("main").getByRole("heading", { name: c.ready }).first();
          await expect(ready).toBeVisible();
          await expect(page.locator("main [aria-busy=true]")).toHaveCount(0);
          await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
          await settleTransitions(page);
          const results = await makeAxeBuilder().analyze();
          expect(results.violations, `${c.path} ${name} ${width}px ${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual([]);
        }
      });
    }
  }
}
