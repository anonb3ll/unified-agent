import { expect, test, type Locator, type Page } from "@playwright/test";
import { ADD_RESULT, BEARER, parked, REMOVE_RESULT } from "./agents/fixtures";
import { mockAgents, reply } from "./agents/pw";

/**
 * Spec §9.3 flows against a mocked hub (response shapes from
 * `webui/cc_agents.py` and the classic routes). Each flow is driven by the
 * keyboard: focus moves directly or with Tab, and actions run on Enter or
 * Space, never a pointer click.
 */
test.use({ viewport: { width: 1440, height: 900 } });
test.describe.configure({ timeout: 90_000 });
expect.configure({ timeout: 20_000 });

const main = (page: Page) => page.getByRole("main");
const h1 = (page: Page, name: string) => main(page).getByRole("heading", { level: 1, name, exact: true });

async function press(page: Page, locator: Locator, key = "Enter") {
  await locator.focus();
  await page.keyboard.press(key);
}

async function typeInto(page: Page, locator: Locator, text: string) {
  await locator.focus();
  await page.keyboard.insertText(text);
}

async function bearerInStorage(page: Page): Promise<boolean> {
  return page.evaluate((bearer) => {
    const all = [sessionStorage, localStorage].flatMap((area) => Object.keys(area).map((k) => area.getItem(k) ?? ""));
    return all.some((v) => v.includes(bearer));
  }, BEARER);
}

test("roster: filter from the keyboard and open an agent with Enter", async ({ page }) => {
  await mockAgents(page);
  await page.goto("/ui/agents");
  await expect(h1(page, "Agents")).toBeVisible();
  const table = main(page).getByRole("table", { name: "Agents in the organization" });
  await expect(table.getByRole("link", { name: /Desktop_Agent1/ })).toBeVisible();
  const runs = main(page).getByLabel("Runs");
  await runs.focus();
  await runs.selectOption("local");
  await expect(page).toHaveURL(/kind=local/);
  await expect(table.getByRole("link", { name: /cloud_helper/ })).toHaveCount(0);
  await press(page, table.getByRole("link", { name: /Desktop_Agent1/ }));
  await expect(page).toHaveURL(/\/ui\/agents\/profile\/Desktop_Agent1$/);
  await expect(h1(page, "Desktop_Agent1")).toBeVisible();
  await expect(main(page).getByRole("link", { name: "#2473 Agents area" })).toBeVisible();
});

test("add: three steps by keyboard; the bearer is shown once and is gone from the page and storage after close", async ({ page }) => {
  const { writes } = await mockAgents(page, { "POST /api/action/agent/add": reply(ADD_RESULT) });
  await page.goto("/ui/home");
  await expect(h1(page, "Home")).toBeVisible();
  await press(page, page.getByRole("button", { name: "New", exact: true }));
  await press(page, page.getByRole("menuitem", { name: "Agent" }));
  await expect(page).toHaveURL(/\/ui\/agents\/add$/);
  await expect(h1(page, "Add an agent")).toBeVisible();
  await press(page, main(page).getByLabel(/Another machine/), "Space");
  await press(page, main(page).getByRole("button", { name: "Next" }));
  await typeInto(page, main(page).getByLabel(/^Label/), "Laptop_New");
  const office = main(page).getByLabel(/^Office/);
  await office.focus();
  await office.selectOption("platform");
  await press(page, main(page).getByRole("button", { name: "Next" }));
  await press(page, main(page).getByRole("button", { name: "Add the agent" }));
  const dialog = page.getByRole("dialog", { name: "Laptop_New's token" });
  await expect(dialog.getByText(BEARER)).toBeVisible();
  expect(writes[0]).toMatchObject({ path: "/api/action/agent/add", body: { label: "Laptop_New", office: "platform", kind: "local" } });
  expect(await bearerInStorage(page)).toBe(false);
  await press(page, dialog.getByRole("button", { name: "I have copied it" }));
  await expect(dialog).toHaveCount(0);
  await expect(main(page).getByRole("heading", { name: "Laptop_New was added" })).toBeVisible();
  expect(await page.content()).not.toContain(BEARER);
  expect(await bearerInStorage(page)).toBe(false);
});

test("add: a parked add waits for a different person's approval and shows no token", async ({ page }) => {
  await mockAgents(page, {
    "POST /api/action/agent/add": reply(parked("agent_add", 91), 202),
    "GET /api/approvals/91": reply({ id: 91, tool: "agent_add", status: "pending", requested_by: "operator" }),
  });
  await page.goto("/ui/agents/add");
  await expect(h1(page, "Add an agent")).toBeVisible();
  await press(page, main(page).getByLabel(/This machine/), "Space");
  await press(page, main(page).getByRole("button", { name: "Next" }));
  await typeInto(page, main(page).getByLabel(/^Label/), "Laptop_New");
  await main(page).getByLabel(/^Office/).selectOption("platform");
  await press(page, main(page).getByRole("button", { name: "Next" }));
  await press(page, main(page).getByRole("button", { name: "Add the agent" }));
  // The operator asked, so the hub's separation of duties needs another person to approve.
  await expect(main(page).getByRole("region", { name: "Approval status" })).toContainText("This needs approval from a different person");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("add: a non-operator sees why the button is disabled", async ({ page }) => {
  await mockAgents(page, { "GET /api/agent-roster": { status: 403, json: { error: "operator only", code: "operator_required" } } });
  await page.goto("/ui/agents/add");
  await expect(main(page).getByRole("heading", { name: "Only the operator can add agents" })).toBeVisible();
  await expect(main(page).getByRole("button", { name: "Add the agent" })).toBeDisabled();
});

test("agent page: remove after a confirm with a reason, all by keyboard", async ({ page }) => {
  const { writes } = await mockAgents(page, { "POST /api/action/agent/remove": reply(REMOVE_RESULT) });
  await page.goto("/ui/agents/profile/Desktop_Agent1");
  await expect(h1(page, "Desktop_Agent1")).toBeVisible();
  const remove = main(page).getByRole("button", { name: "Remove Desktop_Agent1" });
  await expect(remove).toBeEnabled();
  await press(page, remove);
  const dialog = page.getByRole("dialog", { name: "Remove Desktop_Agent1?" });
  await typeInto(page, dialog.getByLabel(/^Reason/), "replaced");
  await press(page, dialog.getByLabel("Release its claimed tasks"), "Space");
  await press(page, dialog.getByRole("button", { name: "Remove", exact: true }));
  await expect(main(page).getByText(/Desktop_Agent1 was removed\. 1 tokens/)).toBeVisible();
  expect(writes.find((w) => w.path === "/api/action/agent/remove")?.body).toEqual({ label: "Desktop_Agent1", reason: "replaced", force_release: true });
});

test("hosts: retire a device after a confirm, by keyboard", async ({ page }) => {
  const { writes } = await mockAgents(page, { "POST /api/action/node-retire": reply({ node: "desktop-node", retired: true, changed: true }) });
  await page.goto("/ui/agents/hosts");
  await expect(h1(page, "Hosts")).toBeVisible();
  await press(page, main(page).getByRole("button", { name: "Retire desktop-node" }));
  const dialog = page.getByRole("dialog", { name: "Retire desktop-node?" });
  await typeInto(page, dialog.getByLabel(/^Reason/), "moved");
  await press(page, dialog.getByRole("button", { name: "Retire", exact: true }));
  await expect.poll(() => writes.find((w) => w.path === "/api/action/node-retire")?.body).toEqual({ action: "retire", node: "desktop-node", reason: "moved" });
});

test("teams: open a plan and start the team, by keyboard", async ({ page }) => {
  const { writes } = await mockAgents(page, { "POST /api/action/team-up": reply({ booted: true, seats: [{ label: "Desktop_Agent2", outcome: "started" }] }) });
  await page.goto("/ui/agents/teams");
  await expect(h1(page, "Teams")).toBeVisible();
  await press(page, main(page).getByRole("link", { name: "review-pair" }));
  await expect(page).toHaveURL(/team=review-pair/);
  await press(page, main(page).getByRole("button", { name: "Start this team" }));
  await press(page, page.getByRole("dialog").getByRole("button", { name: "Start the team" }));
  await expect(main(page).getByText("review-pair started.")).toBeVisible();
  expect(writes.find((w) => w.path === "/api/action/team-up")?.body).toEqual({ team: "review-pair", plan_sha256: "plan-sha-1" });
});

test("hosts: revoke a device's sign-in and attest a device's acceptance, by keyboard", async ({ page }) => {
  const pending = { state: "pending", run_id: "acc-0003", checks: { launch: "pass", receipt: "pass" } };
  const { writes } = await mockAgents(page, {
    "GET /api/nodes": { json: { nodes: [{ node: "desktop-node", agent: "Desktop_Agent2", backends: ["codex"], harnesses: {}, worktree_roots: [], last_heartbeat: null, acceptance: pending }], recent_items: [], retired_nodes: [], acceptance_required: true } },
    "POST /api/action/oauth-revoke": reply({ ok: true, revoked: 1 }),
    "POST /api/action/node-acceptance": reply({ state: "accepted", run_id: "acc-0003" }),
  });
  await page.goto("/ui/agents/hosts");
  await expect(h1(page, "Hosts")).toBeVisible();
  await press(page, main(page).getByRole("button", { name: "Revoke sign-in for laptop-node" }));
  await press(page, page.getByRole("dialog", { name: "Revoke the sign-in for laptop-node?" }).getByRole("button", { name: "Revoke", exact: true }));
  await expect.poll(() => writes.find((w) => w.path === "/api/action/oauth-revoke")?.body).toEqual({ device: "n123" });
  await press(page, main(page).getByRole("button", { name: "Attest acceptance on desktop-node" }));
  const dialog = page.getByRole("dialog", { name: "Attest desktop-node's acceptance?" });
  await typeInto(page, dialog.getByLabel(/^Independent evidence/), "checked by hand");
  await press(page, dialog.getByRole("button", { name: "Attest acceptance" }));
  await expect.poll(() => writes.find((w) => w.path === "/api/action/node-acceptance")?.body).toEqual({ action: "attest", node: "desktop-node", run_id: "acc-0003", evidence_ref: "checked by hand" });
});

test("operating: create a sprint and set a task's next run, by keyboard", async ({ page }) => {
  const { writes } = await mockAgents(page, { "POST /api/action/operating-sprint": reply({ id: 8 }), "POST /api/action/operating-run": reply({ ok: true }) });
  await page.goto("/ui/agents/teams?tab=operating");
  await expect(h1(page, "Teams")).toBeVisible();
  const form = main(page).getByRole("form", { name: "Create a sprint" });
  await typeInto(page, form.getByLabel(/^Sprint name/), "Next");
  await press(page, form.getByRole("button", { name: "Create sprint" }));
  await expect.poll(() => writes.find((w) => w.path === "/api/action/operating-sprint")?.body).toEqual({ op: "add", name: "Next" });
  await typeInto(page, main(page).getByLabel(/^Task id$/), "41");
  await press(page, main(page).getByRole("button", { name: "Show resolved settings" }));
  await expect(main(page).getByRole("table", { name: "Resolved run settings" })).toBeVisible();
  await expect(main(page).getByLabel(/^Run overrides/)).toHaveValue('{"planning":"P3"}');
  await press(page, main(page).getByRole("button", { name: "Set for next run" }));
  await expect.poll(() => writes.find((w) => w.path === "/api/action/operating-run")?.body).toEqual({ op: "set", task_id: 41, profile: "steady", overrides: { planning: "P3" } });
});
