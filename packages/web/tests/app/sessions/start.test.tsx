import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites, listParkedWrites } from "../../../src/app/lib/parkedWrites";
import type { MockHandler } from "../support/fetchMock";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { resetLaunchGuardCache } from "../../../src/app/sessions/launchGuard";
import { AGENTS, nodesPayload, OVERVIEW, ROUTES, sessionRow, sessionsPayload } from "./fixtures";
import { fixtureEpoch } from "../support/epoch";

beforeEach(() => {
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
  localStorage.clear();
  resetLaunchGuardCache();
});
afterEach(() => vi.unstubAllGlobals());

function hub(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  return {
    "GET /api/overview": { body: OVERVIEW },
    "GET /api/dispatch-routes": { body: ROUTES },
    "GET /api/nodes": { body: nodesPayload() },
    "GET /api/agents": { body: AGENTS },
    "GET /api/dispatch-stack": { body: { next_task_id: 42, items: [{ task_id: 42, eligible: true, title: "Fix the login form" }, { task_id: 43, eligible: false, reason: "waits on #42" }] } },
    ...overrides,
  };
}

async function openForm(overrides: Record<string, MockHandler> = {}, path = "/sessions/new") {
  const result = renderRoute(path, { hub: hub(overrides) });
  await findPageHeading("Start a session");
  await screen.findByRole("button", { name: /Start (session|the next ready task)/ }, { timeout: 10_000 });
  return result;
}

async function pickAgent(label: string) {
  const agent = screen.getByRole("combobox", { name: /^Agent/ });
  fireEvent.change(agent, { target: { value: label.slice(0, 7) } });
  fireEvent.mouseDown(await screen.findByRole("option", { name: new RegExp(`^${label}`) }));
}

function fillDispatch() {
  fireEvent.change(screen.getByRole("textbox", { name: /Instructions/ }), { target: { value: "Tidy the README" } });
  fireEvent.change(screen.getByRole("combobox", { name: /Project/ }), { target: { value: "unified-agent" } });
}

describe("/sessions/new", () => {
  it("shows guardrails from the hub's answer, never a constant (defect 3)", async () => {
    await openForm();
    const rails = () => screen.getByTestId("guardrails").textContent ?? "";
    expect(rails()).not.toMatch(/guardrails on/i);
    await pickAgent("Desktop_Agent2");
    await waitFor(() => expect(rails()).toContain('"developer" permission profile'));
    expect(rails()).toContain("/home/dev/agent");
    await pickAgent("Desktop_Agent1");
    await waitFor(() => expect(rails()).toContain("No workspace is bound for this agent"));
    expect(rails()).toContain('"steward"');
    expect(screen.getByRole("region", { name: "Preview" })).toHaveTextContent("Cannot start: no workspace is bound");
  });

  it("says when the hub turned control off", async () => {
    await openForm({ "GET /api/whoami": { body: whoamiFixture({ control_enabled: false, control_disabled_reason: "maintenance window" }) } });
    await waitFor(() => expect(screen.getByTestId("guardrails")).toHaveTextContent("Starting work is turned off on this hub: maintenance window"));
  });

  it("accepted work (202, no approval markers) says the session started and links to it", async () => {
    let sent: unknown = null;
    await openForm({
      "POST /api/action/dispatch": ({ body }) => {
        sent = body;
        return { status: 202, body: { status: "pending", task_id: 501, run_id: "r1", spawned: true } };
      },
    });
    await pickAgent("Desktop_Agent2");
    fillDispatch();
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    expect(await screen.findByText("The session has started.")).toBeInTheDocument();
    expect(screen.queryByText(/Waiting for approval\./)).toBeNull();
    expect(screen.getByRole("link", { name: "Open the session" })).toHaveAttribute("href", "/ui/sessions?q=%23501");
    expect(sent).toEqual({ agent: "Desktop_Agent2", project: "unified-agent", prompt: "Tidy the README", task_id: null, context: ["task_notes", "recent_validation"] });
  });

  it("a parked launch is never replayed: approval can arrive after the hub already created the delegation (blocker 2, defect 9)", async () => {
    // dispatch_prompt() creates the delegation, then task_update, then starts
    // work. When the later task_update parks, the 202 looks exactly like this,
    // but a delegation already exists: repeating the request would create a
    // second one. With an organization in whoami, a resumable write WOULD be
    // replayed by the shell, so this proves the launch is not resumable.
    let posts = 0;
    await openForm({
      "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as Partial<ReturnType<typeof whoamiFixture>>) },
      "POST /api/action/dispatch": () => {
        posts += 1;
        return { status: 202, body: { approval_id: 31, status: "pending", code: "approval_required" } };
      },
      "GET /api/approvals/31": { body: { id: 31, tool: "task_update", status: "approved", outcome: null } },
    });
    await pickAgent("Desktop_Agent2");
    fillDispatch();
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    expect(await screen.findByText(/Waiting for approval\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=31");
    expect(listParkedWrites(fixtureEpoch())).toEqual([]);
    expect(screen.getByText(/the hub may already have created a task for it/i)).toBeInTheDocument();
    expect(screen.getAllByText(/After it is approved, submit this form again/).length).toBeGreaterThan(0);
    // The approval is granted; nothing repeats the launch on its own.
    await new Promise((r) => setTimeout(r, 1500));
    expect(posts).toBe(1);
    expect(screen.getByRole("textbox", { name: /Instructions/ })).toHaveValue("Tidy the README");
  });

  it("a parked launch is shown once, by the launch guard, not twice", async () => {
    await openForm({
      "POST /api/action/dispatch": { status: 202, body: { approval_id: 31, status: "pending", code: "approval_required" } },
      "GET /api/approvals/31": { body: { id: 31, tool: "delegate", status: "pending", outcome: null } },
    });
    await pickAgent("Desktop_Agent2");
    fillDispatch();
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    // Once the launch guard's panel is up (its own "Launch it again"), only one approval notice remains.
    await screen.findByRole("button", { name: "Launch it again" });
    expect(screen.getAllByRole("link", { name: "Open the approval" })).toHaveLength(1);
  });

  it("a parked device launch is not stored for replay either", async () => {
    let posts = 0;
    await openForm({
      "GET /api/whoami": { body: whoamiFixture({ org: "org-1" } as Partial<ReturnType<typeof whoamiFixture>>) },
      "POST /api/action/node-session": () => {
        posts += 1;
        return { status: 202, body: { status: "approval_pending", approval_id: 9, code: "approval_required", tool: "node_dispatch" } };
      },
      "GET /api/approvals/9": { body: { id: 9, tool: "node_dispatch", status: "pending", outcome: null } },
    });
    fireEvent.change(screen.getByRole("combobox", { name: /Where should it run/ }), { target: { value: "desktop" } });
    fireEvent.change(await screen.findByRole("textbox", { name: /Instructions/ }), { target: { value: "Run the tests" } });
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    expect(await screen.findByText(/Waiting for approval\./)).toBeInTheDocument();
    expect(posts).toBe(1);
    expect(listParkedWrites(fixtureEpoch())).toEqual([]);
  });

  it("edits made while a launch is sending are kept when it succeeds (review 13)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    await openForm({ "POST /api/action/dispatch": { status: 202, body: { status: "running", task_id: 800, spawned: true } } });
    const real = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/api/action/dispatch")) await gate;
      return real(input, init);
    });
    await pickAgent("Desktop_Agent2");
    fillDispatch();
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Start session/ })).toHaveAttribute("aria-busy", "true"));
    fireEvent.change(screen.getByRole("textbox", { name: /Instructions/ }), { target: { value: "A second job, typed meanwhile" } });
    release();
    expect(await screen.findByText("The session has started.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /Instructions/ })).toHaveValue("A second job, typed meanwhile");
  });

  it("keeps a half-filled form across navigation (defect 8)", async () => {
    const first = await openForm();
    await pickAgent("Desktop_Agent2");
    fireEvent.change(screen.getByRole("textbox", { name: /Instructions/ }), { target: { value: "Draft that must survive" } });
    await waitFor(() => expect(JSON.stringify(sessionStorage)).toContain("Draft that must survive"));
    first.unmount();
    cleanup();
    await openForm();
    expect(screen.getByRole("textbox", { name: /Instructions/ })).toHaveValue("Draft that must survive");
    expect(screen.getByText("Your unfinished form was restored.")).toBeInTheDocument();
  });

  it("?agent= preselects the agent, and the form launches to it (#2523)", async () => {
    let launched: Record<string, unknown> | null = null;
    await openForm(
      {
        "POST /api/action/dispatch": ({ body }) => {
          launched = body as Record<string, unknown>;
          return { status: 202, body: { status: "running", task_id: 9, spawned: true } };
        },
      },
      "/sessions/new?agent=Desktop_Agent2",
    );
    expect(screen.getByRole("combobox", { name: /^Agent/ })).toHaveValue("Desktop_Agent2");
    fireEvent.change(screen.getByRole("textbox", { name: /Instructions/ }), { target: { value: "Tidy the README" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Project/ }), { target: { value: "unified-agent" } });
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(launched).toMatchObject({ agent: "Desktop_Agent2", prompt: "Tidy the README", project: "unified-agent" }));
  });

  it("?task= preselects the task, and letting the hub choose previews and launches the next ready task", async () => {
    let launched: unknown = null;
    await openForm(
      {
        "POST /api/action/dispatch-stack": ({ body }) => {
          launched = body;
          return { status: 202, body: { next_task_id: 42, dispatch: { launched: true, run_id: "r9" } } };
        },
      },
      "/sessions/new?task=42",
    );
    expect(screen.getByRole("radio", { name: "A task" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /#42 Fix the login form/ })).toBeChecked();
    fireEvent.change(screen.getByRole("combobox", { name: /Project/ }), { target: { value: "unified-agent" } });
    const preview = await screen.findByRole("region", { name: "Preview" });
    await waitFor(() => expect(preview).toHaveTextContent("Task #42 starts next."));
    fireEvent.click(screen.getByRole("button", { name: "Start the next ready task" }));
    await waitFor(() => expect(launched).toEqual({ task_ids: [42], project: "unified-agent" }));
    expect(await screen.findByText("The session has started.")).toBeInTheDocument();
  });

  it("starts a session on a device with its agent program and folder", async () => {
    let started: unknown = null;
    await openForm({
      "POST /api/action/node-session": ({ body }) => {
        started = body;
        return { status: 201, body: { item_id: "item-9", node: "desktop", status: "queued" } };
      },
    });
    fireEvent.change(screen.getByRole("combobox", { name: /Where should it run/ }), { target: { value: "desktop" } });
    expect(await screen.findByText(/the agent connected from this device/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: /Instructions/ }), { target: { value: "Run the tests" } });
    fireEvent.click(screen.getByRole("radio", { name: /Edit files without asking/ }));
    expect(screen.getByRole("radio", { name: /Full access/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(started).not.toBeNull());
    expect(started).toEqual({ action: "start", node: "desktop", backend: "acp:codex", cwd: "/home/dev/app", prompt: "Run the tests", permission_mode: "auto-accept-edits" });
    expect(await screen.findByText("The session is queued on the device.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the session" })).toHaveAttribute("href", "/ui/sessions?tab=work&item=item-9");
  });

  it("an unclassified native permission policy is never shown as a safe default (blocker 3)", async () => {
    const nodes = nodesPayload();
    nodes.nodes[0].session_settings["acp:codex"] = {
      ...nodes.nodes[0].session_settings["acp:codex"],
      available_modes: [{ id: "default", name: "Default", permission_mode: "supervised" }],
      mode_control: "native_default_unclassified",
    } as (typeof nodes.nodes)[0]["session_settings"]["acp:codex"];
    await openForm({ "GET /api/nodes": { body: nodes } });
    fireEvent.change(screen.getByRole("combobox", { name: /Where should it run/ }), { target: { value: "desktop" } });
    const rails = await screen.findByTestId("guardrails");
    expect(rails).not.toHaveTextContent(/On: Ask before changes/);
    expect(rails).toHaveTextContent(/Check: .*native permission policy is not classified/);
    expect(screen.getByRole("radio", { name: /Edit files without asking/ })).toBeDisabled();
  });

  it("a device that has not reported its settings yet is not described as asking before changes", async () => {
    const nodes = nodesPayload();
    delete (nodes.nodes[0] as { session_settings?: unknown }).session_settings;
    await openForm({ "GET /api/nodes": { body: nodes } });
    fireEvent.change(screen.getByRole("combobox", { name: /Where should it run/ }), { target: { value: "desktop" } });
    const rails = await screen.findByTestId("guardrails");
    expect(rails).not.toHaveTextContent(/On: Ask before changes/);
    expect(rails).toHaveTextContent(/has not reported its permission settings yet/);
  });

  it("an unreported policy is described neutrally everywhere, never as \"Ask before changes\" (review 2: 9)", async () => {
    const nodes = nodesPayload();
    delete (nodes.nodes[0] as { session_settings?: unknown }).session_settings;
    await openForm({ "GET /api/nodes": { body: nodes } });
    fireEvent.change(screen.getByRole("combobox", { name: /Where should it run/ }), { target: { value: "desktop" } });
    expect(await screen.findByRole("radio", { name: /Supervised; this device has not reported its policy yet/ })).toBeChecked();
    expect(screen.queryByText(/Ask before changes/)).toBeNull();
  });

  it("a negotiated supervised mode is named from the hub's settings", async () => {
    await openForm();
    fireEvent.change(screen.getByRole("combobox", { name: /Where should it run/ }), { target: { value: "desktop" } });
    expect(await screen.findByTestId("guardrails")).toHaveTextContent(/On: Ask before changes: .*device reports this mode as "Ask"/);
  });

  it("the Hermes profile shown is the one sent, and its settings are the ones used (review 11)", async () => {
    let started: Record<string, unknown> | null = null;
    const nodes = nodesPayload();
    const node = nodes.nodes[0] as Record<string, unknown>;
    node.backends = ["acp:hermes"];
    // HermesProfile rows as node_queue reports them.
    node.hermes_profiles = [
      { kind: "hermes", name: "scratch", agent: null, mapping: "unmapped", launchable: false, reason: "no canonical agent mapping" },
      { kind: "hermes", name: "ops", agent: "Mac_Hermes", mapping: "canonical", launchable: true, reason: null },
    ];
    node.session_settings = {
      "acp:hermes:ops": {
        available_modes: [
          { id: "ask", name: "Ask", permission_mode: "supervised" },
          { id: "edit", name: "Edit", permission_mode: "auto-accept-edits" },
        ],
        available_models: [],
        current_mode_id: "ask",
        current_model_id: null,
        permission_mode: "supervised",
        mode_path: null,
        model_path: null,
      },
    };
    await openForm({
      "GET /api/nodes": { body: nodes },
      "POST /api/action/node-session": ({ body }) => {
        started = body as Record<string, unknown>;
        return { status: 201, body: { item_id: "item-h", node: "desktop", status: "queued" } };
      },
    });
    fireEvent.change(screen.getByRole("combobox", { name: /Where should it run/ }), { target: { value: "desktop" } });
    expect(await screen.findByRole("combobox", { name: /Hermes profile/ })).toHaveValue("ops");
    // The settings for the displayed profile are used: edit mode is offered.
    expect(screen.getByRole("radio", { name: /Edit files without asking/ })).toBeEnabled();
    fireEvent.change(screen.getByRole("textbox", { name: /Instructions/ }), { target: { value: "Check the logs" } });
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(started).not.toBeNull());
    expect(started).toMatchObject({ backend: "acp:hermes", hermes_profile: "ops" });
  });

  it("a device with no launchable Hermes profile is refused before sending", async () => {
    const nodes = nodesPayload();
    const node = nodes.nodes[0] as Record<string, unknown>;
    node.backends = ["acp:hermes"];
    node.hermes_profiles = [{ kind: "hermes", name: "scratch", agent: null, mapping: "unmapped", launchable: false, reason: "no canonical agent mapping" }];
    const { hub: calls } = await openForm({ "GET /api/nodes": { body: nodes } });
    fireEvent.change(screen.getByRole("combobox", { name: /Where should it run/ }), { target: { value: "desktop" } });
    fireEvent.change(await screen.findByRole("textbox", { name: /Instructions/ }), { target: { value: "Check the logs" } });
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    expect(await screen.findByText(/No Hermes profile on this device can be started/)).toBeInTheDocument();
    expect(calls.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("choosing \"A task\" requires a task with a chosen agent or a device too (review 15)", async () => {
    const { hub: calls } = await openForm();
    await pickAgent("Desktop_Agent2");
    fireEvent.click(screen.getByRole("radio", { name: "A task" }));
    fireEvent.change(screen.getByRole("textbox", { name: /Instructions/ }), { target: { value: "Do it" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Project/ }), { target: { value: "unified-agent" } });
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    expect(await screen.findByText(/Pick the task to work on, or choose "Instructions I write"/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: /Where should it run/ }), { target: { value: "desktop" } });
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    expect(await screen.findByText(/Pick the task to work on, or choose "Instructions I write"/)).toBeInTheDocument();
    expect(calls.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("a failed validation moves focus to the first invalid field (review 17)", async () => {
    await openForm();
    // Let the hub choose, with no task: the first problem is what to work on.
    fireEvent.click(screen.getByRole("button", { name: "Start the next ready task" }));
    await waitFor(() => expect(screen.getByRole("radio", { name: "A task" })).toHaveFocus());
    // A chosen agent with nothing filled in: the instructions come first.
    await pickAgent("Desktop_Agent2");
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: /Instructions/ })).toHaveFocus());
    fireEvent.change(screen.getByRole("textbox", { name: /Instructions/ }), { target: { value: "Do it" } });
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: /Project/ })).toHaveFocus());
  });

  it("explains what is missing instead of posting an incomplete launch", async () => {
    const { hub: calls } = await openForm();
    fireEvent.click(screen.getByRole("button", { name: "Start the next ready task" }));
    expect(await screen.findByText(/To let the hub choose, pick one or more tasks/)).toBeInTheDocument();
    expect(screen.getByText("Fix the fields marked above, then start again.")).toBeInTheDocument();
    expect(calls.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("a failed read says what happened, with Retry and the raw text collapsed", async () => {
    await openForm({ "GET /api/overview": { status: 503, text: "gateway timeout from upstream" } });
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText(/Could not load the agents, projects and tasks/)).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(within(alert).getByText(/gateway timeout/).closest("details")).not.toHaveAttribute("open");
  });
});
