import { expect, settleTransitions, test } from "../a11y.fixture";
import { emptyReads, failingReads, mockAgents } from "./agents/pw";

/**
 * Spec §7: every Agents page passes axe (WCAG 2.2 AA) in dark, light and
 * high contrast, at 1440px and 390px, with realistic data, with empty data
 * and with failing reads.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;

type State = "data" | "empty" | "failing";

const ROUTES: { path: string; title: string; ready: Record<State, string | RegExp> }[] = [
  { path: "/agents", title: "Agents", ready: { data: "Managed token, active", empty: "No agents yet", failing: "Agents could not be loaded" } },
  { path: "/agents/add", title: "Add an agent", ready: { data: "Where does it run?", empty: "Where does it run?", failing: "The add form could not load" } },
  { path: "/agents/profile/Desktop_Agent1", title: "Desktop_Agent1", ready: { data: "#2473 Agents area", empty: "does not know an agent called", failing: "Sessions could not be loaded" } },
  { path: "/agents/teams", title: "Teams", ready: { data: "review-pair", empty: "No team specs", failing: "Teams could not be loaded" } },
  { path: "/agents/teams?tab=operating&run_task=41", title: "Teams", ready: { data: "Cutover", empty: "No sprints yet", failing: "Operating profiles could not be loaded" } },
  { path: "/agents/teams?tab=registry", title: "Teams", ready: { data: "Starter team templates", empty: "No teams in the registry", failing: "Registry teams could not be loaded" } },
  { path: "/agents/skills", title: "Skills", ready: { data: "v3 (latest)", empty: "No skills published", failing: "Skills could not be loaded" } },
  { path: "/agents/org", title: "Organization chart", ready: { data: "review verdicts", empty: "No offices configured", failing: "The org chart could not be loaded" } },
  { path: "/agents/topology", title: "Topology", ready: { data: "Agent topology", empty: "Nothing connected yet", failing: "The topology could not be loaded" } },
  { path: "/agents/hosts", title: "Hosts", ready: { data: "Accepted", empty: "No machine has reported", failing: "Machines could not be loaded" } },
  { path: "/agents/capabilities", title: "Capabilities", ready: { data: "Agent enrolment", empty: "No capability catalog", failing: "Capabilities could not be loaded" } },
];

test.describe.configure({ timeout: 150_000 });

for (const state of ["data", "empty", "failing"] as const) {
  for (const width of WIDTHS) {
    for (const route of ROUTES) {
      test(`${route.path} with ${state} reads at ${width}px passes axe in every theme`, async ({ page, makeAxeBuilder }) => {
        await page.setViewportSize({ width, height: 900 });
        await mockAgents(page, state === "data" ? {} : state === "empty" ? emptyReads() : failingReads());
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
