import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { commands, routes } from "../../../src/app/runs/routes";
import type { MockHandler } from "../support/fetchMock";
import { findPageHeading, renderRoute } from "../support/render";
import { EXECUTOR, forVitest, LINEAGE_42, REALISTIC, TELEMETRY } from "./hubData";
import { installLocks } from "./locks";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  installLocks();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function open(path: string, title: string, overrides: Record<string, MockHandler> = {}) {
  const r = renderRoute(path, { hub: { ...forVitest(REALISTIC), ...overrides } });
  await findPageHeading(title);
  return r;
}

describe("Runs history pages", { timeout: 40_000 }, () => {
  it("receipts: every receipt with its outcome, evidence and failure reason", async () => {
    await open("/runs/receipts", "Receipts");
    const table = await screen.findByRole("table", { name: "Recent dispatch delivery receipts" });
    expect(within(table).getByText("ev-01JD7Z")).toBeInTheDocument();
    expect(within(table).getByText(/tests failed \(quality\)/)).toBeInTheDocument();
  });

  it("receipts: the hub's named unavailable answer is a failed read, not an empty list", async () => {
    await open("/runs/receipts", "Receipts", {
      "GET /api/dispatch_receipts": { body: { error: "dispatch delivery receipts unavailable", code: "dispatch_receipts_unavailable", receipts: [] } },
    });
    expect(await screen.findByRole("heading", { name: /Delivery receipts could not be loaded/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "No delivery receipts yet" })).toBeNull();
  });

  it("telemetry: summary, spans, exceptions with Escalate, and scorecards", async () => {
    let sent: unknown = null;
    await open("/runs/telemetry", "Telemetry", {
      "POST /api/action/task": ({ body }) => {
        sent = body;
        return { body: { id: 42, updated: true } };
      },
    });
    const summary = await screen.findByRole("group", { name: "Telemetry summary" });
    expect(summary).toHaveTextContent("$0.0123");
    expect(screen.getByRole("table", { name: /Telemetry spans/ })).toHaveTextContent("1200 tokens · $0.0123");
    const exceptions = screen.getByRole("table", { name: /Telemetry exceptions/ });
    fireEvent.click(within(exceptions).getByRole("button", { name: "Escalate to #42" }));
    await waitFor(() => expect(sent).toEqual({ id: 42, append_note: "[exception-escalated: 01JD7ZEVIDENCE00000000002] model_error: the model refused the request" }));
    expect(await within(exceptions).findByRole("button", { name: "Escalated to #42" })).toBeDisabled();
    expect(await screen.findByRole("table", { name: "Agent scorecards" })).toHaveTextContent("83%");
    expect(screen.getByRole("table", { name: "Agent scorecards" })).toHaveTextContent("75% (only 4)");
  });

  it("telemetry shows the newest spans and exceptions first, from a read larger than the shown cap (item 13)", async () => {
    // The hub returns the window oldest first; 60 spans is more than the 50 shown.
    const spans = Array.from({ length: 60 }, (_, i) => ({ ...TELEMETRY.waterfall.spans[0], span_id: `span-${String(i).padStart(2, "0")}`, trace_id: `trace-${String(i).padStart(2, "0")}` }));
    const exceptions = TELEMETRY.exception_digest.exceptions;
    await open("/runs/telemetry", "Telemetry", {
      "GET /api/telemetry": { body: { ...TELEMETRY, waterfall: { ...TELEMETRY.waterfall, spans }, exception_digest: { ...TELEMETRY.exception_digest, exceptions } } },
    });
    const table = await screen.findByRole("table", { name: /Telemetry spans/ });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(50);
    expect(rows[0]).toHaveTextContent("trace-59");
    expect(rows[49]).toHaveTextContent("trace-10");
    expect(table).not.toHaveTextContent("trace-09");
  });

  it("the exception digest is sorted by evidence time, not by the hub's grouping (item 4)", async () => {
    // The hub's real grouped order: ordinary exceptions in span order, then
    // repeated-failure and fallback-storm aggregates, then missing telemetry.
    const old = (i: number) => new Date(Date.UTC(2026, 10, 1, 0, i)).toISOString();
    const fresh = { trace_id: "new-failure", kind: "failure", severity: "high", message: "the newest failure", task_id: null, evidence_ref: { ulid: "01JDZZZZZZZZZZZZZZZZZZZZZZ", created_at: new Date().toISOString() } };
    const aggregates = Array.from({ length: 31 }, (_, i) => ({
      trace_id: `agg-${String(i).padStart(2, "0")}`,
      kind: i % 2 ? "fallback_storm" : "repeated_failure",
      severity: "high",
      count: 3,
      message: "3 failures",
      task_id: null,
      evidence_ref: { ulid: `01JD7Z${String(i).padStart(20, "0")}`, created_at: old(i) },
    }));
    const missing = { trace_id: "missing-1", kind: "missing_telemetry", severity: "high", message: "no telemetry received for trace missing-1" };
    await open("/runs/telemetry", "Telemetry", {
      "GET /api/telemetry": { body: { ...TELEMETRY, exception_digest: { exceptions: [fresh, ...aggregates, missing], total: 33 } } },
    });
    const rows = within(await screen.findByRole("table", { name: /Telemetry exceptions/ })).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(30);
    expect(rows[0]).toHaveTextContent("the newest failure");
    expect(rows[1]).toHaveTextContent("agg-30");
    // 32 dated entries fill the cap of 30, so the undated one is not among them.
    expect(rows.some((r) => r.textContent?.includes("missing-1"))).toBe(false);
  });

  it("an exception with no evidence time is shown last and labelled (item 4)", async () => {
    const missing = { trace_id: "missing-1", kind: "missing_telemetry", severity: "high", message: "no telemetry received" };
    await open("/runs/telemetry", "Telemetry", {
      "GET /api/telemetry": { body: { ...TELEMETRY, exception_digest: { exceptions: [missing, ...TELEMETRY.exception_digest.exceptions], total: 2 } } },
    });
    const rows = within(await screen.findByRole("table", { name: /Telemetry exceptions/ })).getAllByRole("row").slice(1);
    expect(rows[1]).toHaveTextContent("missing-1");
    expect(rows[1]).toHaveTextContent("No time recorded");
  });

  it("executor: a failed work-folder survey is unavailable with Retry, never 'no stale folders' (item 14)", async () => {
    await open("/runs/executor", "Executor", {
      "GET /api/executor-delegation": { body: { ...EXECUTOR, stale_worktrees: [], worktree_errors: ["git worktree list failed: timeout"], worktree_error: "git worktree list failed: timeout" } },
    });
    expect(await screen.findByRole("heading", { name: /The work folders could not be surveyed/ })).toBeInTheDocument();
    expect(screen.queryByText("No stale or kept work folders.")).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry" }).length).toBeGreaterThan(0);
  });

  it("executor: health, counts from a complete read only, and every table", async () => {
    await open("/runs/executor", "Executor");
    expect(await screen.findByText(/active_executor_service: heartbeat 12s ago/)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Delegations by status" })).toHaveTextContent("Completed14");
    for (const caption of ["Delegations between agents", "Recent delegation failures", "Recent executor sessions", "Stale and kept work folders"]) {
      expect(screen.getByRole("table", { name: caption })).toBeInTheDocument();
    }
  });

  it("executor: a partial read says so and shows no counts", async () => {
    await open("/runs/executor", "Executor", { "GET /api/executor-delegation": { body: { ...EXECUTOR, errors: ["session_list failed: boom"] } } });
    expect(await screen.findByText(/Part of this status could not be read/)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Delegations by status" })).toHaveTextContent("Completed–");
  });

  it("insights: flow measures and a task's lineage on the graph with its table alternative", async () => {
    await open("/runs/insights?task=42", "Insights", { "GET /api/execution-graph": ({ url }) => ({ body: url.includes("task=42") ? LINEAGE_42 : REALISTIC["GET /api/execution-graph"].json }) });
    expect(await screen.findByRole("group", { name: "Flow measures" })).toHaveTextContent("1h 30m");
    expect(await screen.findByText("task/42-login")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://github.com/example/repo/pull/12" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Work graph" })).toHaveAttribute("href", "/ui/work?view=graph");
  });

  it("exports every Runs route and palette commands that point at them", () => {
    const paths = routes.map((r) => (r.index ? "" : r.path));
    expect(paths).toEqual(["", "workflows", "graphs", "routines", "cycles", "cycles/:id", "receipts", "telemetry", "insights", "executor"]);
    for (const c of commands) expect(c.to).toMatch(/^\/runs(\/(workflows|graphs|routines|cycles|receipts|telemetry|insights|executor))?$/);
    // The shell lists every page by title; an area command must not repeat one.
    const titles = ["Runs", "Workflows", "Graphs", "Routines", "Cycles", "Receipts", "Telemetry", "Insights", "Executor"];
    for (const c of commands) for (const t of titles) expect(c.label).not.toMatch(new RegExp(`\\b${t}\\b`));
  });
});
