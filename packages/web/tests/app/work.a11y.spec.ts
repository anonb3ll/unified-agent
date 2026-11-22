import { expect, settleTransitions, test } from "../a11y.fixture";
import { EMPTY_BOARD } from "./work/fixtures";
import { failingReads, mockWork } from "./work/pw";

/**
 * Spec §7: every Work page passes axe (WCAG 2.2 AA) in dark, light and high
 * contrast, at 1440px and 390px, with realistic data, with empty data and
 * with failing reads.
 */
const THEMES = ["dark", "light", "high-contrast"] as const;
const WIDTHS = [1440, 390] as const;

type State = "data" | "empty" | "failing";

const ROUTES: { path: string; title: Record<State, string>; ready: Record<State, string | RegExp> }[] = [
  { path: "/work", title: { data: "Work", empty: "Work", failing: "Work" }, ready: { data: "Ship the Work area", empty: "No tasks yet", failing: "Tasks could not be loaded" } },
  { path: "/work?view=board", title: { data: "Work", empty: "Work", failing: "Work" }, ready: { data: "Ship the Work area", empty: "No tasks yet", failing: "Tasks could not be loaded" } },
  { path: "/work?view=tree", title: { data: "Work", empty: "Work", failing: "Work" }, ready: { data: "Ship the Work area", empty: "No tasks yet", failing: "Tasks could not be loaded" } },
  { path: "/work?view=graph", title: { data: "Work", empty: "Work", failing: "Work" }, ready: { data: "What the lines mean", empty: "No tasks yet", failing: "Tasks could not be loaded" } },
  { path: "/work/tasks/new", title: { data: "New task", empty: "New task", failing: "New task" }, ready: { data: "Create task", empty: "Create task", failing: "Create task" } },
  { path: "/work/tasks/102", title: { data: "Ship the Work area", empty: "Task 102", failing: "Task 102" }, ready: { data: "Looks good so far.", empty: "This task does not exist", failing: "The task could not be loaded" } },
  { path: "/work/goals", title: { data: "Goals", empty: "Goals", failing: "Goals" }, ready: { data: "Work area complete", empty: "No goals yet", failing: "Goals could not be loaded" } },
  { path: "/work/goals/7", title: { data: "A usable Control Center", empty: "Goal 7", failing: "Goal 7" }, ready: { data: /tasks done/, empty: "This goal does not exist", failing: "The goal could not be loaded" } },
  { path: "/work/plans", title: { data: "Plans", empty: "Plans", failing: "Plans" }, ready: { data: "Roll out Control Center v2", empty: "No plans yet", failing: "Plans could not be loaded" } },
  { path: "/work/plans/new", title: { data: "New plan", empty: "New plan", failing: "New plan" }, ready: { data: "Create plan", empty: "Create plan", failing: "Create plan" } },
  { path: "/work/plans/3", title: { data: "Roll out Control Center v2", empty: "Plan 3", failing: "Plan 3" }, ready: { data: "Checkpoint 1", empty: "The plan could not be loaded", failing: "The plan could not be loaded" } },
  { path: "/work/canvas", title: { data: "Planning canvas", empty: "Planning canvas", failing: "Planning canvas" }, ready: { data: /Board "default"/, empty: /is empty/, failing: "The board could not be loaded" } },
];

const EMPTY = {
  "GET /api/board": { json: EMPTY_BOARD },
  "GET /api/goals": { json: { tree: [] } },
  "GET /api/plans": { json: { plans: [] } },
  "GET /api/overview": { json: { task_graph: { nodes: [], links: [] } } },
  "GET /api/platform/objects/task/102": { status: 404, json: { error: "unknown task: 102" } },
  "GET /api/goals/7": { status: 404, json: { error: "unknown goal" } },
  "GET /api/plans/3": { status: 404, json: { error: "no plan with id 3" } },
  "GET /api/graph-canvas": { json: { board_id: "default", exists: false, updated_by: null, updated_at: null, scene: {} } },
};

test.describe.configure({ timeout: 120_000 });

for (const state of ["data", "empty", "failing"] as const) {
  for (const width of WIDTHS) {
    for (const route of ROUTES) {
      test(`${route.path} with ${state} reads at ${width}px passes axe in every theme`, async ({ page, makeAxeBuilder }) => {
        await page.setViewportSize({ width, height: 900 });
        await mockWork(page, state === "data" ? {} : state === "empty" ? EMPTY : failingReads());
        for (const theme of THEMES) {
          await page.addInitScript((t) => localStorage.setItem("ua-web-theme", t), theme);
          await page.goto(`/ui${route.path}`);
          const main = page.getByRole("main");
          await expect(main.getByRole("heading", { level: 1, name: route.title[state], exact: true })).toBeVisible({ timeout: 20_000 });
          const ready = route.ready[state];
          await expect(main.getByText(ready).first()).toBeVisible({ timeout: 20_000 });
          await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
          await settleTransitions(page);
          const results = await makeAxeBuilder().analyze();
          expect(results.violations, `${route.path} ${state} ${width}px ${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual([]);
        }
      });
    }
  }
}
