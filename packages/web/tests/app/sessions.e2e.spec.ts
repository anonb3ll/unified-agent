import { expect, test, type Page, type Route } from "@playwright/test";
import { mockHub, type HubOverride } from "./support/mockHub";
import { REALISTIC } from "./sessions/hubData";

/**
 * Spec §9.4 flows against a mocked hub, each driven by the keyboard at least
 * once (spec §7).
 */
test.use({ viewport: { width: 1440, height: 900 } });

const heading = (page: Page, name: string) => page.getByRole("main").getByRole("heading", { level: 1, name, exact: true });

function capture(into: unknown[], reply: { status?: number; json: unknown }): HubOverride {
  return async (route: Route) => {
    into.push(route.request().postDataJSON());
    await route.fulfill({ status: reply.status ?? 200, contentType: "application/json", body: JSON.stringify(reply.json) });
  };
}

test("list: filter from the keyboard, then open a session with Enter", async ({ page }) => {
  await mockHub(page, REALISTIC);
  await page.goto("/ui/sessions");
  await expect(heading(page, "Sessions")).toBeVisible();
  const rows = page.getByRole("table", { name: /agent sessions/i }).getByRole("row");
  await expect(rows).toHaveCount(4);
  await page.getByRole("combobox", { name: "Status" }).focus();
  await page.keyboard.press("ArrowDown"); // Open
  await expect(page).toHaveURL(/status=open/);
  await expect(rows).toHaveCount(3);
  await page.getByRole("link", { name: /Desktop_Agent2\s*s-live/ }).focus();
  await page.keyboard.press("Enter");
  await expect(heading(page, "Session s-live")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/ui\/sessions\?status=open$/);
});

test("session: a follow-up typed and sent with the keyboard reaches the device", async ({ page }) => {
  const sent: unknown[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/node-followup": capture(sent, { status: 201, json: { seq: 4, mode: "queue", duplicate: false } }) });
  await page.goto("/ui/sessions/s-live");
  await expect(page.getByRole("list", { name: "Conversation, oldest first" })).toContainText("Looking at the login form now.");
  const box = page.getByRole("textbox", { name: "Message" });
  await box.focus();
  await page.keyboard.type("Check the mobile layout too");
  await page.keyboard.press("Control+Enter");
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0]).toMatchObject({ item_id: "item-1", mode: "queue", body: "Check the mobile layout too" });
  await expect(box).toHaveValue("");
});

test("session: an unsent follow-up survives leaving and coming back", async ({ page }) => {
  await mockHub(page, REALISTIC);
  await page.goto("/ui/sessions/s-live");
  await page.getByRole("textbox", { name: "Message" }).fill("half a thought");
  await page.getByRole("link", { name: "All sessions" }).click();
  await expect(heading(page, "Sessions")).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("textbox", { name: "Message" })).toHaveValue("half a thought");
});

test("session: stop is confirmed in a dialog driven by the keyboard", async ({ page }) => {
  const sent: unknown[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/node-session": capture(sent, { json: { cancel_requested: true } }) });
  await page.goto("/ui/sessions/s-live");
  const stop = page.getByRole("button", { name: "Stop", exact: true });
  await expect(stop).toBeEnabled();
  await stop.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Stop this session?" });
  await expect(dialog).toBeVisible();
  // Danger confirmations open on Cancel, so a stray Enter does nothing harmful.
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect.poll(() => sent).toEqual([{ action: "cancel", item_id: "item-1" }]);
});

test("session: the operator ends another identity's session and grants a wake", async ({ page }) => {
  const ended: unknown[] = [];
  const granted: unknown[] = [];
  await mockHub(page, {
    ...REALISTIC,
    "POST /api/action/session/operator-end": capture(ended, { json: { session_id: "s-live", owner: "Desktop_Agent2", status: "failed", ended_by: "operator", closed: true, event_ulid: "01OPEND", outstanding: { claimed_task_ids: [], open_delegation_ids: [], note: null } } }),
    "POST /api/sessions/s-live/wake-grant": capture(granted, { json: { granted: true } }),
  });
  await page.goto("/ui/sessions/s-live");
  await page.getByRole("button", { name: "Grant idle wakes" }).click();
  await expect.poll(() => granted).toEqual([{ grant: true }]);
  await page.getByRole("button", { name: "End session" }).click();
  const dialog = page.getByRole("dialog", { name: "End this session?" });
  await dialog.getByRole("radio", { name: /Failed/ }).check();
  await dialog.getByRole("textbox", { name: /Reason/ }).fill("Stuck for two hours");
  await dialog.getByRole("button", { name: "End session" }).click();
  await expect.poll(() => ended).toEqual([{ session_id: "s-live", reason: "Stuck for two hours", status: "failed" }]);
});

test("session: hand off with the keyboard", async ({ page }) => {
  const sent: unknown[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/handoff": capture(sent, { json: { task_id: 77 } }) });
  await page.goto("/ui/sessions/s-live");
  await page.getByRole("button", { name: "Hand off" }).focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Hand off to another agent" });
  await dialog.getByRole("combobox", { name: /Agent/ }).focus();
  await page.keyboard.type("Mac");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await dialog.getByRole("textbox", { name: /Note for the agent/ }).fill("Finish the tests");
  await dialog.getByRole("button", { name: "Hand off" }).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0]).toMatchObject({ to_agent: "Mac_Hermes", parent_task_id: 42 });
});

test("session: the crew view opens and a message reaches the crew", async ({ page }) => {
  const sent: unknown[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/crew-intervene": capture(sent, { json: { accepted: true, to_agent: "Desktop_Agent2" } }) });
  await page.goto("/ui/sessions/s-live");
  await page.getByRole("button", { name: "Open crew view" }).click();
  const drawer = page.getByRole("dialog", { name: "Crew view" });
  await expect(drawer).toContainText("Review the form copy too.");
  await drawer.getByRole("textbox", { name: "Message to the crew" }).fill("Ship it");
  await drawer.getByRole("button", { name: "Send to crew" }).click();
  await expect.poll(() => sent.length).toBe(1);
  // One message_id per message, kept with the draft so a retry is deduplicated (review 4).
  expect(sent[0]).toEqual({ session_id: "s-live", body: "Ship it", message_id: expect.stringMatching(/^crew-/) });
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(page.getByRole("button", { name: "Open crew view" })).toBeFocused();
});

test("session: a live message arrives through the event stream and is announced", async ({ page }) => {
  const frame = {
    seq: 5000,
    ulid: "01LIVE000001",
    channel: "agent.node",
    originator: "desktop",
    created_at: "2026-11-10T11:30:00Z",
    payload: { kind: "acp_event", item_id: "item-1", acp_kind: "agent_message_chunk", turn_id: "t9", text: "Tests are green now." },
  };
  let served = false;
  await mockHub(page, {
    ...REALISTIC,
    "GET /api/events/stream": async (route) => {
      // Deliver the frame once, a moment after the page has loaded its history.
      const body = served ? ": ok\n\n" : `event: cursor\ndata: {}\n\nevent: message\ndata: ${JSON.stringify(frame)}\n\n`;
      if (!served) await new Promise((r) => setTimeout(r, 1500));
      served = true;
      await route.fulfill({ status: 200, contentType: "text/event-stream", body });
    },
  });
  await page.goto("/ui/sessions/s-live");
  const log = page.getByRole("list", { name: "Conversation, oldest first" });
  await expect(log).toContainText("Looking at the login form now.");
  await expect(log).toContainText("Tests are green now.", { timeout: 15_000 });
  await expect(page.getByRole("main").getByRole("status").filter({ hasText: "New message from Desktop_Agent2" })).toHaveCount(1);
});

test("start: a launch by keyboard that the hub accepts says the session started and links to it", async ({ page }) => {
  const sent: unknown[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/dispatch": capture(sent, { status: 202, json: { status: "running", task_id: 600, spawned: true } }) });
  await page.goto("/ui/sessions/new");
  await expect(heading(page, "Start a session")).toBeVisible();
  await page.getByRole("combobox", { name: /^Agent/ }).focus();
  await page.keyboard.press("Control+a");
  await page.keyboard.type("Desktop_Agent2");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("guardrails")).toContainText('"developer" permission profile');
  await page.getByRole("textbox", { name: /Instructions/ }).focus();
  await page.keyboard.type("Tidy the README");
  await page.getByRole("combobox", { name: /Project/ }).selectOption("unified-agent");
  await page.getByRole("button", { name: "Start session" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("The session has started.")).toBeVisible();
  expect(sent[0]).toMatchObject({ agent: "Desktop_Agent2", project: "unified-agent", prompt: "Tidy the README" });
  await page.getByRole("link", { name: "Open the session" }).click();
  await expect(heading(page, "Sessions")).toBeVisible();
  await expect(page).toHaveURL(/q=%23600/);
});

test("start: a launch that needs approval waits for it and keeps the form", async ({ page }) => {
  await mockHub(page, {
    ...REALISTIC,
    "POST /api/action/dispatch": { status: 202, json: { approval_id: 12, status: "pending", code: "approval_required" } },
    // The approval still waits (GET /api/approvals/{id} shape).
    "GET /api/approvals/12": { json: { id: 12, tool: "delegate", status: "pending", outcome: null } },
  });
  await page.goto("/ui/sessions/new?task=42");
  await page.getByRole("combobox", { name: /^Agent/ }).fill("Desktop_Agent2");
  await page.getByRole("option", { name: /^Desktop_Agent2/ }).click();
  await page.getByRole("textbox", { name: /Instructions/ }).fill("Fix it");
  await page.getByRole("combobox", { name: /Project/ }).selectOption("unified-agent");
  await page.getByRole("button", { name: "Start session" }).click();
  await expect(page.getByText(/Waiting for approval\./).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=12");
  await expect(page.getByText("The session has started.")).toHaveCount(0);
  // Leave and come back: the form is still filled in.
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /^Inbox/ }).click();
  await expect(heading(page, "Inbox")).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("textbox", { name: /Instructions/ })).toHaveValue("Fix it");
});

test("start: on a device, from the New menu", async ({ page }) => {
  const sent: unknown[] = [];
  await mockHub(page, { ...REALISTIC, "POST /api/action/node-session": capture(sent, { status: 201, json: { item_id: "item-7", node: "desktop", status: "queued" } }) });
  await page.goto("/ui/sessions");
  await page.getByRole("button", { name: /^New/ }).click();
  await page.getByRole("menuitem", { name: "Session" }).click();
  await expect(heading(page, "Start a session")).toBeVisible();
  await page.getByRole("combobox", { name: /Where should it run/ }).selectOption("desktop");
  await page.getByRole("textbox", { name: /Instructions/ }).fill("Run the tests");
  await page.getByRole("button", { name: "Start session" }).click();
  await expect(page.getByText("The session is queued on the device.")).toBeVisible();
  expect(sent[0]).toMatchObject({ action: "start", node: "desktop", backend: "acp:codex", prompt: "Run the tests" });
});

test("devices and team snapshots: adopt a session and restore a team after confirming", async ({ page }) => {
  const adopted: unknown[] = [];
  const restored: unknown[] = [];
  await mockHub(page, {
    ...REALISTIC,
    "GET /api/teams/snapshots/snap-1/preview": { json: { seats: [{ label: "Lead", node: "desktop", backend: "acp:codex", task_id: 42, item_id: null, planned: "resumed", reason: null }] } },
    "POST /api/teams/snapshots/snap-1/restore": capture(restored, { json: { seats: [{ label: "Lead", node: "desktop", backend: "acp:codex", task_id: 42, item_id: "i", planned: "resumed", outcome: "resumed", reason: null }] } }),
    "POST /api/action/node-adopt": capture(adopted, { status: 201, json: { item_id: "item-8", status: "queued" } }),
  });
  await page.goto("/ui/sessions?tab=devices");
  await page.getByRole("button", { name: /Adopt claude session on desktop/ }).click();
  const dialog = page.getByRole("dialog", { name: "Adopt this session" });
  await dialog.getByRole("textbox", { name: /Message to continue with/ }).fill("Pick up where you left off");
  await dialog.getByRole("button", { name: "Adopt and send" }).click();
  await expect.poll(() => adopted.length).toBe(1);
  expect(adopted[0]).toMatchObject({ node: "desktop", harness: "claude", external_session_id: "abc", backend: "acp:claude" });

  await page.getByRole("tab", { name: "Team snapshots" }).click();
  await page.getByRole("button", { name: /Preview restoring Web team/ }).click();
  await page.getByRole("button", { name: "Restore this team" }).click();
  await page.getByRole("dialog", { name: "Restore this team?" }).getByRole("button", { name: "Restore team" }).click();
  await expect.poll(() => restored.length).toBe(1);
  await expect(page.getByRole("heading", { name: /Restore result/ })).toBeVisible();
});

test("the palette reaches Start a session", async ({ page }) => {
  await mockHub(page, REALISTIC);
  await page.goto("/ui/sessions");
  await expect(heading(page, "Sessions")).toBeVisible();
  await page.keyboard.press("Control+k");
  const input = page.getByRole("combobox", { name: "Search pages and commands" });
  await expect(input).toBeFocused();
  await input.fill("Start a session");
  // Area commands load with the palette; wait for this area's entry before choosing it.
  await expect(page.getByRole("option", { name: /^Start a session/ }).first()).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(heading(page, "Start a session")).toBeVisible();
});
