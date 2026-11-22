import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { deriveState, enrolmentRefusal, mergeRoster } from "../../../src/app/agents/model";
import { findPageHeading, renderRoute } from "../support/render";
import { agentsHub, FLEET, LIFECYCLE, OPEN_SESSION, OPERATOR_ONLY, ROSTER } from "./fixtures";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("agents: roster model", () => {
  it("derives the spec's states from lifecycle, heartbeat and work", () => {
    expect(deriveState({ lifecycle: "retired", rosterState: null, heartbeat: "online", openSessions: 1, activeTasks: 0 })).toBe("retired");
    expect(deriveState({ lifecycle: "paused", rosterState: null, heartbeat: "online", openSessions: 0, activeTasks: 0 })).toBe("paused");
    expect(deriveState({ lifecycle: "terminated", rosterState: null, heartbeat: "idle", openSessions: 0, activeTasks: 0 })).toBe("stopped");
    expect(deriveState({ lifecycle: null, rosterState: "active", heartbeat: "stale", openSessions: 0, activeTasks: 2 })).toBe("offline");
    expect(deriveState({ lifecycle: null, rosterState: null, heartbeat: "online", openSessions: 0, activeTasks: 1 })).toBe("working");
    expect(deriveState({ lifecycle: null, rosterState: null, heartbeat: "idle", openSessions: 0, activeTasks: 0 })).toBe("idle");
  });

  it("merges every read into one row per label and never guesses a missing read", () => {
    const rows = mergeRoster({ fleet: FLEET.agents, roster: ROSTER.agents, lifecycle: LIFECYCLE.agents, sessions: [OPEN_SESSION] });
    expect(rows.map((r) => r.label)).toEqual(["cloud_helper", "Desktop_Agent1", "Desktop_Agent2", "Laptop_Old"]);
    const claude = rows.find((r) => r.label === "Desktop_Agent1")!;
    expect(claude).toMatchObject({ state: "working", office: "steward", kind: "local", kindKnown: true, currentTask: { id: 2473, title: "Agents area" } });
    expect(rows.find((r) => r.label === "Laptop_Old")!.state).toBe("retired");
    expect(rows.find((r) => r.label === "cloud_helper")!).toMatchObject({ kind: "cloud", state: "offline", openSessions: null });
    // Without the operator's roster, seat and token stay unknown, and the kind comes from the label.
    const anon = mergeRoster({ fleet: FLEET.agents });
    expect(anon.find((r) => r.label === "Desktop_Agent2")!).toMatchObject({ office: null, token: null, added: null, kindKnown: false });
  });

  it("explains every enrolment refusal code", () => {
    for (const code of ["already_added", "retired", "sealed_store_required", "operator_required", "claims_held", "not_found", "unavailable", "policy_denied", "rate_limited"]) {
      expect(enrolmentRefusal(code, "X")).toMatch(/\w/);
    }
    expect(enrolmentRefusal("something_else", "X")).toBeNull();
  });
});

describe("agents: roster page", () => {
  it("lists every agent with state, seat, host, current task and token metadata, never a bearer", async () => {
    const { hub } = renderRoute("/agents", { hub: agentsHub() });
    await findPageHeading("Agents");
    const table = await screen.findByRole("table", { name: "Agents in the organization" });
    const claude = within(table).getByRole("link", { name: /Desktop_Agent1/ });
    expect(claude).toHaveAttribute("href", "/ui/agents/profile/Desktop_Agent1");
    const row = claude.closest("tr")!;
    expect(within(row).getByText("Working")).toBeInTheDocument();
    expect(within(row).getByText("steward")).toBeInTheDocument();
    expect(within(row).getByText(/#2473 Agents area/)).toBeInTheDocument();
    expect(within(row).getByText("Managed token, active")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: /Laptop_Old/ }).closest("tr")!).toHaveTextContent("Retired");
    expect(screen.getByText(/4 agents · 1 working · 1 retired · 4 shown/)).toBeInTheDocument();
    expect(hub.calls.some((c) => c.path === "/api/agent-roster")).toBe(true);
  });

  it("filters by host kind and state through the URL", async () => {
    const { router } = renderRoute("/agents", { hub: agentsHub() });
    await findPageHeading("Agents");
    await screen.findByRole("table", { name: "Agents in the organization" });
    fireEvent.change(screen.getByLabelText("Runs"), { target: { value: "cloud" } });
    await waitFor(() => expect(router.state.location.search).toBe("?kind=cloud"));
    const table = screen.getByRole("table", { name: "Agents in the organization" });
    expect(within(table).getByRole("link", { name: /cloud_helper/ })).toBeInTheDocument();
    expect(within(table).queryByRole("link", { name: /Desktop_Agent1/ })).toBeNull();
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "retired" } });
    await waitFor(() => expect(router.state.location.search).toContain("state=retired"));
    expect(screen.getByText(/No agent matches these filters/)).toBeInTheDocument();
  });

  it("for a non-operator, says seats and tokens are the operator's and still lists the fleet", async () => {
    renderRoute("/agents", { hub: agentsHub({ "GET /api/agent-roster": OPERATOR_ONLY }) });
    await findPageHeading("Agents");
    expect(await screen.findByText(/shown to the human operator only/)).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Agents in the organization" });
    const row = within(table).getByRole("link", { name: /Desktop_Agent2/ }).closest("tr")!;
    expect(within(row).getByText("Only the operator can see token details")).toBeInTheDocument();
  });

  it("shows no count while loading and explains a failed read without raw text", async () => {
    let release: (() => void) | null = null;
    renderRoute("/agents", {
      hub: agentsHub({
        "GET /api/agents": () =>
          new Promise((resolve) => {
            release = () => resolve({ status: 500, body: { error: "sqlite3.OperationalError: database is locked" } });
          }),
      }),
    });
    await findPageHeading("Agents");
    expect(await screen.findByText(/Loading agents/)).toBeInTheDocument();
    expect(screen.queryByText(/\d+ agents/)).toBeNull();
    await act(async () => release?.());
    expect(await screen.findByRole("heading", { name: "Agents could not be loaded" })).toBeInTheDocument();
    expect(screen.getByText(/database is locked/)).not.toBeVisible();
  });
});
