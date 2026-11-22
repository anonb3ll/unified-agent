import { describe, expect, it } from "vitest";
import type { ActivityEvent, DispatchRouteRow } from "../../../src/lib/sessions/types";
import {
  buildTranscript,
  filesChanged,
  filterSessions,
  guardrailsFor,
  orderSessions,
  pullRequestOf,
  sessionState,
  usageOf,
} from "../../../src/app/sessions/model";
import { ROUTES, SESSIONS, nodeItem, sessionRow, transcriptEvents } from "./fixtures";

describe("session list helpers", () => {
  it("orders open sessions first, then by last activity", () => {
    expect(orderSessions(SESSIONS).map((s) => s.id)).toEqual(["s-live", "s-wait", "s-old"]);
  });

  it("filters by status, agent, host and search text", () => {
    const base = { status: "all" as const, agent: "", host: "", q: "" };
    expect(filterSessions(SESSIONS, { ...base, status: "ended" }).map((s) => s.id)).toEqual(["s-old"]);
    expect(filterSessions(SESSIONS, { ...base, status: "open" }).map((s) => s.id).sort()).toEqual(["s-live", "s-wait"]);
    expect(filterSessions(SESSIONS, { ...base, agent: "Desktop_Agent1", host: "laptop" }).map((s) => s.id).sort()).toEqual(["s-old", "s-wait"]);
    expect(filterSessions(SESSIONS, { ...base, q: "#42" }).map((s) => s.id)).toEqual(["s-live"]);
    expect(filterSessions(SESSIONS, { ...base, q: "item-1" }).map((s) => s.id)).toEqual(["s-live"]);
  });

  it("says states in plain words", () => {
    expect(sessionState("running")).toEqual({ label: "Running", kind: "live" });
    expect(sessionState("completed").label).toBe("Finished");
    expect(sessionState("abandoned").kind).toBe("danger");
    expect(sessionState(null).kind).toBe("unknown");
  });

  it("reports cost, tokens and the pull request only when the hub recorded them", () => {
    expect(usageOf(sessionRow())).toEqual({ cost: "$0.0421", tokens: "1,500 (1,200 in, 300 out)" });
    expect(usageOf(sessionRow({ meta: {} }))).toEqual({ cost: null, tokens: null });
    expect(pullRequestOf(sessionRow())).toBe("https://github.com/acme/app/pull/7");
    expect(pullRequestOf(sessionRow({ meta: { pr_url: "javascript:alert(1)" } }))).toBeNull();
  });
});

describe("buildTranscript", () => {
  const entries = buildTranscript({ events: transcriptEvents() as ActivityEvent[], item: nodeItem(), agent: "Desktop_Agent2" });

  it("starts with the prompt, joins message chunks and keeps tool activity separate", () => {
    expect(entries.map((e) => e.role)).toEqual(["operator", "agent", "tool", "operator", "changes"]);
    expect(entries[0]).toMatchObject({ role: "operator", text: "Fix the login form validation", note: "Starting prompt" });
    expect(entries[1]).toMatchObject({ role: "agent", text: "Looking at the login form now." });
    expect(entries[2]).toMatchObject({ role: "tool", title: "Tool: Read src/login.tsx (completed)" });
    expect(entries[3]).toMatchObject({ role: "operator", text: "Also add a test please", note: "Follow-up" });
  });

  it("lists the files a session changed", () => {
    expect(filesChanged(entries)).toEqual([{ status: "M", path: "src/login.tsx", withheld: false }]);
  });

  it("adds crew messages by author", () => {
    const crew = buildTranscript({
      events: [],
      crew: [
        { message_id: "a", from_agent: "Desktop_Agent2", body: "done", created_at: "2026-11-10T11:00:00Z" },
        { message_id: "b", from_agent: "operator", body: "thanks", created_at: "2026-11-10T11:01:00Z" },
      ],
      agent: "Desktop_Agent2",
    });
    expect(crew.map((e) => e.role)).toEqual(["agent", "operator"]);
  });
});

describe("guardrailsFor (defect 3: never a constant)", () => {
  const routes = ROUTES.routes as DispatchRouteRow[];
  const enabled = { enabled: true, reason: null };

  it("names the chosen agent's real profile and workspace", () => {
    const text = guardrailsFor({ control: enabled, route: routes[0], routesState: "ready", agent: "Desktop_Agent2", hubChooses: false })
      .map((g) => g.text)
      .join(" ");
    expect(text).toContain('"developer" permission profile');
    expect(text).toContain("/home/dev/agent");
  });

  it("changes with the hub's answer: an unbound workspace and a missing program are flagged", () => {
    const rails = guardrailsFor({ control: enabled, route: routes[1], routesState: "ready", agent: "Desktop_Agent1", hubChooses: false });
    expect(rails.map((g) => g.tone)).toEqual(["ok", "ok", "danger", "warn"]);
    expect(rails[1].text).toContain('"steward"');
  });

  it("says when control is off, and is unknown until the hub has answered", () => {
    expect(guardrailsFor({ control: { enabled: false, reason: "paused by operator" }, route: null, routesState: "ready", agent: "", hubChooses: false })[0]).toEqual({
      tone: "danger",
      text: "Starting work is turned off on this hub: paused by operator",
    });
    const unknown = guardrailsFor({ control: null, route: null, routesState: "loading", agent: "Desktop_Agent2", hubChooses: false });
    expect(unknown.every((g) => g.tone === "unknown")).toBe(true);
    expect(unknown.map((g) => g.text).join(" ")).not.toMatch(/guardrails on/i);
  });
});
