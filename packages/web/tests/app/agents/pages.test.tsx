import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../../../src/app/agents/routes";
import { clearParkedWrites, listParkedWrites } from "../../../src/app/lib/parkedWrites";
import { fixtureEpoch } from "../support/epoch";
import { findPageHeading, renderRoute } from "../support/render";
import { agentsHub, parked } from "./fixtures";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

describe("agents: routes", () => {
  it("registers every page of spec §3, with the agent page under profile/", () => {
    expect(routes.map((r) => (r.index ? "" : r.path))).toEqual(["", "add", "teams", "skills", "org", "topology", "hosts", "capabilities", "profile/:label"]);
  });
});

describe("agents: teams", () => {
  it("lists team specs, opens a plan from the URL and starts the team after a confirm", async () => {
    const { hub, router } = renderRoute("/agents/teams", {
      hub: agentsHub({ "POST /api/action/team-up": { body: { booted: true, seats: [{ label: "Desktop_Agent2", outcome: "started" }] } } }),
    });
    await findPageHeading("Teams");
    fireEvent.click(await screen.findByRole("link", { name: "review-pair" }));
    await waitFor(() => expect(router.state.location.search).toBe("?team=review-pair"));
    const plan = await screen.findByRole("region", { name: "Plan for review-pair" });
    expect(await within(plan).findByText(/start Desktop_Agent2 on desktop/)).toBeInTheDocument();
    expect(within(plan).getByText(/already running a session/)).toBeInTheDocument();
    fireEvent.click(within(plan).getByRole("button", { name: "Start this team" }));
    const dialog = await screen.findByRole("dialog", { name: "Start review-pair?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Start the team" }));
    expect(await screen.findByText("review-pair started.")).toBeInTheDocument();
    expect(hub.calls.find((c) => c.path === "/api/action/team-up")!.body).toEqual({ team: "review-pair", plan_sha256: "plan-sha-1" });
  });

  it("a parked team start explains the approval and stores nothing to replay", async () => {
    const result = renderRoute("/agents/teams?team=review-pair", {
      hub: agentsHub({
        "POST /api/action/team-up": { status: 202, body: parked("team_up", 93) },
        "GET /api/approvals/93": { body: { id: 93, tool: "team_up", status: "pending", requested_by: "operator" } },
      }),
    });
    await findPageHeading("Teams");
    fireEvent.click(await screen.findByRole("button", { name: "Start this team" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Start the team" }));
    expect(await screen.findByRole("region", { name: "Approval status" })).toBeInTheDocument();
    expect(listParkedWrites(result.epoch())).toEqual([]);
  });

  it("shows registry teams and operating profiles in their tabs (legacy operating view)", async () => {
    renderRoute("/agents/teams?tab=operating", { hub: agentsHub() });
    await findPageHeading("Teams");
    expect(await screen.findByRole("table", { name: "Operating profile per office" })).toHaveTextContent("steady (default)");
    expect(screen.getByRole("region", { name: "Sprints" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Run settings" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Registry teams" }));
    expect(await screen.findByRole("region", { name: "Steward team" })).toHaveTextContent("review v3");
  });
});

describe("agents: hosts", () => {
  it("lists machines, devices with acceptance and retired devices, and retires one after a confirm with a reason", async () => {
    const { hub } = renderRoute("/agents/hosts", { hub: agentsHub({ "POST /api/action/node-retire": { body: { node: "desktop-node", retired: true, changed: true } } }) });
    await findPageHeading("Hosts");
    const machines = await screen.findByRole("table", { name: "Machines" });
    expect(machines).toHaveTextContent("Desktop_Agent1");
    expect(machines).toHaveTextContent("Desktop_Agent2");
    expect(machines).toHaveTextContent("active · task #2473");
    expect(screen.getByText(/spare-laptop/)).toBeInTheDocument();
    const devices = await screen.findByRole("table", { name: "Registered devices" });
    expect(within(devices).getByText("Accepted")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Retired devices" })).toHaveTextContent("replaced");
    fireEvent.click(within(devices).getByRole("button", { name: "Retire desktop-node" }));
    const dialog = await screen.findByRole("dialog", { name: "Retire desktop-node?" });
    expect(within(dialog).getByRole("button", { name: "Retire" })).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "moved" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Retire" }));
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/action/node-retire")).toBe(true));
    expect(hub.calls.find((c) => c.path === "/api/action/node-retire")!.body).toEqual({ action: "retire", node: "desktop-node", reason: "moved" });
  });

  it("a refused retire (409, work queued) is explained", async () => {
    renderRoute("/agents/hosts", { hub: agentsHub({ "POST /api/action/node-retire": { status: 409, body: { error: "node has queued work", code: "conflict" } } }) });
    await findPageHeading("Hosts");
    fireEvent.click(await screen.findByRole("button", { name: "Retire desktop-node" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), { target: { value: "moved" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Retire" }));
    expect(await screen.findByText(/desktop-node has queued or claimed work/)).toBeInTheDocument();
  });

  it("a bundle edited while Publish is pending is kept after success (review of b4cc9988, item 2)", async () => {
    let release: (() => void) | null = null;
    renderRoute("/agents/hosts", {
      hub: agentsHub({
        "POST /api/action/harness-config": () =>
          new Promise((resolve) => {
            release = () => resolve({ status: 201, body: { version: 5 } });
          }),
      }),
    });
    await findPageHeading("Hosts");
    const field = await screen.findByLabelText(/Bundle \(JSON\)/);
    fireEvent.change(field, { target: { value: '{"a": 1}' } });
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(release).not.toBeNull());
    fireEvent.change(field, { target: { value: '{"a": 2}' } });
    await act(async () => release?.());
    await new Promise((r) => setTimeout(r, 50));
    expect(field).toHaveValue('{"a": 2}');
  });

  it("an unchanged bundle is cleared after success", async () => {
    renderRoute("/agents/hosts", { hub: agentsHub({ "POST /api/action/harness-config": { status: 201, body: { version: 5 } } }) });
    await findPageHeading("Hosts");
    const field = await screen.findByLabelText(/Bundle \(JSON\)/);
    fireEvent.change(field, { target: { value: '{"a": 1}' } });
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(field).toHaveValue(""));
  });

  it("publishes a harness configuration bundle and refuses invalid JSON before sending", async () => {
    const { hub } = renderRoute("/agents/hosts", { hub: agentsHub({ "POST /api/action/harness-config": { status: 201, body: { version: 5 } } }) });
    await findPageHeading("Hosts");
    expect(await screen.findByText(/Published version 4 by operator/)).toBeInTheDocument();
    const field = screen.getByLabelText(/Bundle \(JSON\)/);
    fireEvent.change(field, { target: { value: "{not json" } });
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByText("The bundle is not valid JSON.")).toBeInTheDocument();
    expect(hub.calls.some((c) => c.path === "/api/action/harness-config")).toBe(false);
    fireEvent.change(field, { target: { value: '{"claude": {"model": "x"}}' } });
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/action/harness-config")).toBe(true));
    expect(hub.calls.find((c) => c.path === "/api/action/harness-config")!.body).toEqual({ action: "publish", bundle: { claude: { model: "x" } } });
  });
});

describe("agents: read-only pages", () => {
  const stubResize = () => vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  it("the org chart lists offices with members linked to their pages, with a graph", async () => {
    stubResize();
    renderRoute("/agents/org", { hub: agentsHub() });
    await findPageHeading("Organization chart");
    const office = await screen.findByRole("region", { name: "Senior engineer & steward" });
    expect(within(office).getByRole("link", { name: /Claude \(desktop\) \(Desktop_Agent1\)/ })).toHaveAttribute("href", "/ui/agents/profile/Desktop_Agent1");
    expect(within(office).getByText(/review verdicts/)).toBeInTheDocument();
  });

  it("the topology names edges in words", async () => {
    stubResize();
    renderRoute("/agents/topology", { hub: agentsHub() });
    await findPageHeading("Topology");
    fireEvent.click(await screen.findByRole("button", { name: "Table" }));
    expect(await screen.findByText("has claimed #2473 Agents area")).toBeInTheDocument();
  });

  it("skills list published versions and install drift", async () => {
    renderRoute("/agents/skills", { hub: agentsHub() });
    await findPageHeading("Skills");
    expect(await screen.findByRole("table", { name: "Published skills" })).toHaveTextContent("v3 (latest)");
    expect(screen.getByRole("region", { name: "Installed in ~/.claude/skills" })).toHaveTextContent("In sync: 1");
  });

  it("capabilities filter by family through the URL", async () => {
    const { router } = renderRoute("/agents/capabilities", { hub: agentsHub() });
    await findPageHeading("Capabilities");
    expect(await screen.findByRole("table", { name: "Capability catalog" })).toHaveTextContent("Agent enrolment");
    fireEvent.click(within(screen.getByRole("navigation", { name: "Capability families" })).getByRole("link", { name: "Agents" }));
    await waitFor(() => expect(router.state.location.search).toBe("?family=agents"));
    expect(screen.getByRole("table", { name: "Capability catalog" })).toHaveTextContent("Agent enrolment");
  });

  it.each([
    ["/agents/org", "The org chart could not be loaded", "GET /api/org"],
    ["/agents/hosts", "Machines could not be loaded", "GET /api/devices"],
    ["/agents/capabilities", "Capabilities could not be loaded", "GET /api/capability-catalog"],
  ])("%s explains a failed read", async (path, title, key) => {
    renderRoute(path, { hub: agentsHub({ [key]: { status: 500, body: { error: "boom" } } }) });
    expect(await screen.findByRole("heading", { name: title }, { timeout: 12_000 })).toBeInTheDocument();
  });
});
