import { expect, test, type Page, type Route } from "@playwright/test";
import { mockHub, type HubOverride } from "./support/mockHub";
import { cycle, CYCLE_DETAIL, REALISTIC, RUN_ID, RUNS, runRow } from "./runs/hubData";

/**
 * Spec §9.6 flows against a mocked hub, each driven by the keyboard at least
 * once (spec §7), plus the §12 defects this area owns (7 and 10).
 */
test.use({ viewport: { width: 1440, height: 900 } });

const heading = (page: Page, name: string) => page.getByRole("main").getByRole("heading", { level: 1, name, exact: true });

type Seen = { body: unknown; headers: Record<string, string> };

function capture(into: Seen[], reply: (n: number) => { status?: number; json: unknown } | "abort"): HubOverride {
  return async (route: Route) => {
    into.push({ body: route.request().postDataJSON(), headers: route.request().headers() });
    const r = reply(into.length);
    if (r === "abort") {
      await route.abort("connectionreset");
      return;
    }
    await route.fulfill({ status: r.status ?? 200, contentType: "application/json", body: JSON.stringify(r.json) });
  };
}

test("overview: running now and recent runs; older runs load from the keyboard", async ({ page }) => {
  await mockHub(page, {
    ...REALISTIC,
    "GET /api/runs": async (route) => {
      const cursor = new URL(route.request().url()).searchParams.get("cursor");
      const json = cursor
        ? { runs: [runRow({ run_id: "run-old-live", status: "running", outcome: null, ended_at: null })], count: 1, truncated: false, next_cursor: null, limit: 50 }
        : { ...RUNS, truncated: true, next_cursor: "2026-11-01T00:00:00Z|run-x" };
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
    },
  });
  await page.goto("/ui/runs");
  await expect(heading(page, "Runs")).toBeVisible();
  await expect(page.getByText(/Older runs were not read/)).toBeVisible();
  await expect(page.getByRole("table", { name: "Running now, at least 2" })).toBeVisible();
  await page.getByRole("button", { name: "Load older runs" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("table", { name: "Running now, 3" })).toBeVisible();
});

test("workflows: pick one, fill its inputs and run it with the keyboard; see each step", async ({ page }) => {
  const seen: Seen[] = [];
  await mockHub(page, {
    ...REALISTIC,
    "POST /api/action/workflow": capture(seen, () => ({ json: { workflow: "smoke-echo", status: "done", steps: [{ name: "echo", tool: "format_render", status: "done", attempts: 1, error: null }], outputs: { echo: { text: "hi" } } } })),
  });
  await page.goto("/ui/runs/workflows");
  await page.getByRole("link", { name: "smoke-echo" }).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/workflow=smoke-echo/);
  const input = page.getByRole("textbox", { name: /message/ });
  await input.focus();
  await page.keyboard.type("hello");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("table", { name: "Step results" })).toContainText("Done");
  expect(seen[0].body).toEqual({ name: "smoke-echo", inputs: { message: "hello" } });
});

test("workflows: a lost answer holds the next start until the person decides, with a duplicate warning", async ({ page }) => {
  const seen: Seen[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/workflow": capture(seen, (n) => (n === 1 ? "abort" : { json: { workflow: "smoke-echo", status: "done", steps: [], outputs: {} } })) });
  await page.goto("/ui/runs/workflows?workflow=smoke-echo");
  await page.getByRole("textbox", { name: /message/ }).fill("hello");
  await page.getByRole("button", { name: "Run workflow" }).click();
  const check = page.getByRole("region", { name: "Start not confirmed" });
  await expect(check).toContainText("We could not confirm whether this started.");
  await expect(page.getByRole("button", { name: "Run workflow" })).toBeDisabled();
  // The hold survives a reload: the attempt was written before the request went out.
  await page.reload();
  await expect(page.getByRole("region", { name: "Start not confirmed" })).toBeVisible();
  await page.getByRole("button", { name: "I have checked and it did not start. Start again" }).focus();
  await page.keyboard.press("Enter");
  const confirm = page.getByRole("dialog", { name: "Start it again?" });
  await expect(confirm).toContainText("duplicate");
  await expect(confirm.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect.poll(() => seen.length).toBe(2);
  await expect(page.getByRole("region", { name: "Start not confirmed" })).toHaveCount(0);
});

test("workflows: a second tab sees the first tab's unconfirmed start and cannot start again (item 1)", async ({ page, context }) => {
  const seen: Seen[] = [];
  const wire = async (p: Page) => mockHub(p, { ...REALISTIC, "POST /api/action/workflow": capture(seen, () => "abort") });
  await wire(page);
  await page.goto("/ui/runs/workflows?workflow=smoke-echo");
  await page.getByRole("textbox", { name: /message/ }).fill("hello");
  await page.getByRole("button", { name: "Run workflow" }).click();
  await expect(page.getByRole("region", { name: "Start not confirmed" })).toBeVisible();
  const second = await context.newPage();
  await wire(second);
  await second.goto("/ui/runs/workflows?workflow=smoke-echo");
  await expect(second.getByRole("region", { name: "Start not confirmed" })).toContainText("This hold covers this browser only");
  await expect(second.getByRole("button", { name: "Run workflow" })).toBeDisabled();
  expect(seen).toHaveLength(1);
  await second.close();
});

test("graphs: an approval pause waits with the Inbox link; Continue resumes it with the run id", async ({ page }) => {
  const seen: Seen[] = [];
  const paused = {
    status: "approval_pending",
    code: "approval_required",
    approval_id: 55,
    run_status: "interrupted",
    run_id: RUN_ID,
    pending_hop: "merge",
    interrupted_by: { approval_id: 55, hop: "merge", node_key: "merge", tool_name: "task_update", run_id: RUN_ID, task_id: 42, claimed_by: "operator" },
    tool: "langgraph_workflow_run",
    session_id: "s1",
  };
  await mockHub(page, {
    ...REALISTIC,
    "POST /api/action/langgraph/run": capture(seen, (n) => (n === 1 ? { status: 202, json: paused } : { json: { status: "completed", run_id: RUN_ID, pending_hop: null, output: { task_id: 42 }, interrupted_by: null, reason: null } })),
  });
  await page.goto("/ui/runs/graphs");
  const start = page.getByRole("form", { name: "Start a graph run" });
  await start.getByRole("textbox", { name: /Task number/ }).fill("42");
  await start.getByRole("textbox", { name: /What should it do/ }).fill("Ship the login fix");
  await start.getByRole("button", { name: "Start graph run" }).click();
  await expect(page).toHaveURL(new RegExp(`run=${RUN_ID}`));
  await expect(page.getByText(/Waiting for approval before the merge step/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the approval in the Inbox" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=55");
  await expect(page.getByRole("table", { name: `Checkpoints of run ${RUN_ID}, newest first` })).toBeVisible();
  // A reload (or a shared link in this tab) still knows the run is waiting and how to continue it.
  await page.reload();
  await expect(page.getByText(/Waiting for approval before the merge step/)).toBeVisible();
  await expect(page.getByRole("form", { name: `Continue graph run ${RUN_ID}` }).getByRole("textbox", { name: /Task number/ })).toHaveValue("42");
  await page.getByRole("form", { name: `Continue graph run ${RUN_ID}` }).getByRole("button", { name: "Continue" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText(/^Completed: every step ran/)).toBeVisible();
  expect(seen[1].body).toEqual({ task_id: 42, query: "Ship the login fix", run_id: RUN_ID, engine: "workflow" });
});

test("routines: create with a checked schedule, entirely from the keyboard; the draft survives leaving", async ({ page }) => {
  const seen: Seen[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/routine/add": capture(seen, () => ({ json: { id: 9 } })) });
  await page.goto("/ui/runs/routines");
  await expect(page.getByRole("heading", { name: "Weekly dependency check" })).toBeVisible();
  const create = page.getByRole("main").getByRole("button", { name: "New routine" }).first();
  await create.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "New routine" });
  await page.keyboard.type("Nightly check");
  await dialog.getByRole("textbox", { name: /Schedule/ }).focus();
  await page.keyboard.type("0 99 * * *");
  await dialog.getByRole("button", { name: "Create routine" }).focus();
  await page.keyboard.press("Enter");
  await expect(dialog.getByText("The hour must be between 0 and 23.")).toBeVisible();
  await expect(dialog.getByRole("textbox", { name: /Schedule/ })).toBeFocused();
  expect(seen).toHaveLength(0);
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("15 2 * * *");
  await expect(dialog.getByText("The next three runs would be:")).toBeVisible();
  await expect(dialog.getByRole("listitem")).toHaveCount(3);
  // Leave and come back: the draft is restored.
  await page.keyboard.press("Escape");
  await page.getByRole("navigation", { name: "Runs sections" }).getByRole("link", { name: "Overview" }).click();
  await expect(heading(page, "Runs")).toBeVisible();
  await page.goBack();
  await page.getByRole("main").getByRole("button", { name: "New routine" }).first().click();
  await expect(page.getByRole("dialog", { name: "New routine" }).getByRole("textbox", { name: /Schedule/ })).toHaveValue("15 2 * * *");
  await page.getByRole("dialog", { name: "New routine" }).getByRole("button", { name: "Create routine" }).click();
  await expect.poll(() => seen.length).toBe(1);
  expect(seen[0].body).toMatchObject({ title: "Nightly check", trigger: "cron", schedule: "15 2 * * *" });
  await expect(create).toBeFocused();
});

test("routines: Run now sends an Idempotency-Key and explains a queued answer", async ({ page }) => {
  const seen: Seen[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/routine/run": capture(seen, () => ({ json: { status: "queued", task_id: 88 } })) });
  await page.goto("/ui/runs/routines");
  const card = page.getByRole("article", { name: "Weekly dependency check" });
  await card.getByRole("button", { name: "Run now" }).focus();
  await page.keyboard.press("Enter");
  const confirm = page.getByRole("dialog", { name: "Run this routine now?" });
  await confirm.getByRole("button", { name: "Run now" }).focus();
  await page.keyboard.press("Enter");
  await expect(card.getByText(/queued as task #88/)).toBeVisible();
  expect(seen[0].headers["idempotency-key"]).toMatch(/^cc2-routine-run-/);
  expect(seen[0].body).toEqual({ routine_id: 7 });
});

test("routines: pause asks first; resume is direct", async ({ page }) => {
  const paused: Seen[] = [];
  const resumed: Seen[] = [];
  await mockHub(page, {
    ...REALISTIC,
    "POST /api/action/routine/pause": capture(paused, () => ({ json: { id: 7, paused: 1 } })),
    "POST /api/action/routine/resume": capture(resumed, () => ({ json: { id: 8, paused: 0 } })),
  });
  await page.goto("/ui/runs/routines");
  await page.getByRole("article", { name: "Weekly dependency check" }).getByRole("button", { name: "Pause" }).click();
  await page.getByRole("dialog", { name: "Pause this routine?" }).getByRole("button", { name: "Pause routine" }).click();
  await expect.poll(() => paused.map((s) => s.body)).toEqual([{ routine_id: 7 }]);
  await page.getByRole("article", { name: "Triage the inbox" }).getByRole("button", { name: "Resume" }).click();
  await expect.poll(() => resumed.map((s) => s.body)).toEqual([{ routine_id: 8 }]);
});

test("cycles: start one from a form, never without a task (defect 7)", async ({ page }) => {
  const seen: Seen[] = [];
  let prompts = 0;
  page.on("dialog", async (d) => {
    prompts += 1;
    await d.dismiss();
  });
  await mockHub(page, { ...REALISTIC, "POST /api/action/cycle": capture(seen, () => ({ status: 201, json: { cycle: cycle({ id: "newcycle0001", task_id: 77, state: "plan" }), created: true } })) });
  await page.goto("/ui/runs/cycles");
  await page.getByRole("button", { name: "Start a cycle" }).focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Start a cycle" });
  await dialog.getByRole("button", { name: "Start cycle" }).focus();
  await page.keyboard.press("Enter");
  await expect(dialog.getByText("Enter the number of the task this cycle works on.")).toBeVisible();
  await expect(dialog.getByRole("textbox", { name: /Task number/ })).toBeFocused();
  expect(seen).toHaveLength(0);
  await page.keyboard.type("77");
  await dialog.getByRole("button", { name: "Start cycle" }).click();
  await expect(dialog.getByRole("link", { name: "Open cycle newcycle0001" })).toBeVisible();
  expect(seen[0].body).toEqual({ operation: "start", task_id: 77, max_rework: 2 });
  expect(prompts).toBe(0);
});

test("cycles: stop is a danger confirm with the full consequence, driven by the keyboard; focus returns", async ({ page }) => {
  const seen: Seen[] = [];
  await mockHub(page, {
    ...REALISTIC,
    "POST /api/action/cycle": capture(seen, () => ({ json: { cycle: cycle({ state: "killed" }), killed_from: "executing" } })),
  });
  await page.goto("/ui/runs/cycles/c0ffee123456");
  const stop = page.getByRole("button", { name: "Stop" });
  await stop.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Stop this cycle for good?" });
  await expect(dialog).toContainText("This cannot be undone");
  await expect(dialog).toContainText("the task itself are kept");
  await expect(dialog.getByRole("textbox", { name: /Reason/ })).toBeFocused();
  await page.keyboard.type("Wrong task");
  await dialog.getByRole("button", { name: "Stop cycle" }).focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => seen.map((s) => s.body)).toEqual([{ operation: "kill", cycle_id: "c0ffee123456", reason: "Wrong task" }]);
  await expect(dialog).toHaveCount(0);
  await expect(stop).toBeFocused();
});

test("cycles: a refused action never shows raw transport text in the headline (defect 10)", async ({ page }) => {
  await mockHub(page, {
    ...REALISTIC,
    "GET /api/cycles/c0ffee123456": { json: { ...CYCLE_DETAIL, cycle: cycle({ state: "blocked", version: 5 }) } },
    "POST /api/action/cycle": { status: 500, json: { error: "Traceback (most recent call last): sqlite3.OperationalError" } },
  });
  await page.goto("/ui/runs/cycles/c0ffee123456");
  await page.getByRole("button", { name: "Resume" }).click();
  const dialog = page.getByRole("dialog", { name: "Resume this cycle?" });
  await dialog.getByRole("button", { name: "Resume cycle" }).click();
  const alert = dialog.getByRole("alert");
  await expect(alert.locator("p").first()).toHaveText("The hub had a problem completing this. Try again in a moment.");
  await expect(alert.getByText(/Traceback/)).toBeHidden();
});

test("insights: choose a task with the keyboard to see how it was carried out", async ({ page }) => {
  await mockHub(page, REALISTIC);
  await page.goto("/ui/runs/insights");
  const select = page.getByRole("combobox", { name: "Task" });
  await select.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page).toHaveURL(/task=42/);
  await expect(page.getByRole("img", { name: /How task #42 was carried out/ }).or(page.getByText("How task #42 was carried out").first())).toBeVisible();
});

test("telemetry: escalate an exception to its task", async ({ page }) => {
  const seen: Seen[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/task": capture(seen, () => ({ json: { id: 42 } })) });
  await page.goto("/ui/runs/telemetry");
  await page.getByRole("button", { name: "Escalate to #42" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Escalated to #42" })).toBeDisabled();
  expect(seen[0].body).toMatchObject({ id: 42, append_note: expect.stringContaining("exception-escalated") });
});
