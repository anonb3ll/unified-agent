import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { findPageHeading, renderRoute } from "../support/render";
import { forVitest, REALISTIC, RUNS, runRow } from "./hubData";
import { installLocks } from "./locks";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  installLocks();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("/runs overview", () => {
  it("shows what is running now and the recent runs with status, duration, agent and cost", async () => {
    const { hub } = renderRoute("/runs", { hub: forVitest(REALISTIC) });
    await findPageHeading("Runs");
    const running = await screen.findByRole("table", { name: "Running now, 2" });
    expect(within(running).getByText("run-43-live")).toBeInTheDocument();
    const recent = screen.getByRole("table", { name: "Recent runs, newest first" });
    expect(within(recent).getAllByRole("row")).toHaveLength(RUNS.runs.length + 1);
    expect(within(recent).getByText("tests failed")).toBeInTheDocument();
    expect(within(recent).getAllByText("Desktop_Agent2").length).toBeGreaterThan(0);
    expect(within(recent).getAllByText("30m 0s").length).toBeGreaterThan(0);
    expect(within(recent).getAllByText("Not reported").length).toBe(RUNS.runs.length);
    expect(screen.getByText(/All 5 runs the hub has recorded/)).toBeInTheDocument();
    expect(hub.calls.find((c) => c.path === "/api/runs")).toBeTruthy();
  });

  it("a truncated read is incomplete, never 'nothing running', and loads the rest from next_cursor", async () => {
    const finished = [runRow({ run_id: "run-1" }), runRow({ run_id: "run-2" })];
    const older = [runRow({ run_id: "run-old-live", status: "running", outcome: null, ended_at: null })];
    const urls: string[] = [];
    renderRoute("/runs", {
      hub: {
        "GET /api/runs": ({ url }) => {
          urls.push(url);
          return new URL(url, "http://x").searchParams.get("cursor")
            ? { body: { runs: older, count: 1, truncated: false, next_cursor: null, limit: 50 } }
            : { body: { runs: finished, count: 2, truncated: true, next_cursor: "2026-11-10T00:00:00Z|run-2", limit: 50 } };
        },
      },
    });
    await findPageHeading("Runs");
    expect(await screen.findByText("Nothing is running among the newest 2 runs. Older runs were not checked.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing is running right now.")).toBeNull();
    expect(screen.getByText("Showing the newest 2. More runs exist.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load older runs" }));
    expect(await screen.findByRole("table", { name: "Running now, 1" })).toBeInTheDocument();
    expect(urls[1]).toContain("cursor=2026-11-10T00%3A00%3A00Z%7Crun-2");
    expect(urls[0]).toContain("limit=50");
  });

  it("a failed read shows the error with Retry and no zero counts; raw text only in the detail (defect 10)", async () => {
    renderRoute("/runs", { hub: { "GET /api/runs": { status: 500, body: { error: "sqlite3.OperationalError: database is locked" } } } });
    await findPageHeading("Runs");
    expect(await screen.findByRole("heading", { name: /Runs could not be loaded/ })).toBeInTheDocument();
    expect(screen.getByText("The hub had a problem answering. Try again in a moment.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText("Nothing is running right now.")).toBeNull();
    const detail = screen.getByText(/database is locked/);
    expect(detail.closest("details")).not.toHaveAttribute("open");
  });

  it("after a failed refetch the rows stay with the error, Retry and their age", async () => {
    let fail = false;
    const { appClient } = renderRoute("/runs", {
      hub: { "GET /api/runs": () => (fail ? { status: 503, body: { error: "down" } } : { body: RUNS }) },
    });
    await screen.findByRole("table", { name: "Recent runs, newest first" });
    fail = true;
    await appClient().refetchQueries({ queryKey: ["cc2", "runs"] });
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("The latest refresh failed.");
    expect(banner).toHaveTextContent(/stale/);
    expect(within(banner).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // Retained rows are labelled as previously observed, with no current count or all-clear (item 15).
    expect(screen.getByRole("table", { name: "Recent runs as previously observed, newest first" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Running at the last successful read (previously observed)" })).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: /Running now, \d/ })).toBeNull();
    expect(screen.queryByText(/All \d+ runs the hub has recorded/)).toBeNull();
  });

  it("a failed refresh over an all-clear never says nothing is running now (item 15)", async () => {
    let fail = false;
    const { appClient } = renderRoute("/runs", {
      hub: { "GET /api/runs": () => (fail ? { status: 503, body: { error: "down" } } : { body: { runs: [runRow()], count: 1, truncated: false, next_cursor: null, limit: 50 } }) },
    });
    expect(await screen.findByText("Nothing is running right now.")).toBeInTheDocument();
    fail = true;
    await appClient().refetchQueries({ queryKey: ["cc2", "runs"] });
    expect(await screen.findByText("Nothing was running at the last successful read. That may have changed since.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing is running right now.")).toBeNull();
  });

  it("an empty hub says there are no runs yet", async () => {
    renderRoute("/runs", { hub: { "GET /api/runs": { body: { runs: [], count: 0, truncated: false, next_cursor: null, limit: 50 } } } });
    expect(await screen.findByRole("heading", { name: "No runs yet" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("table")).toBeNull());
  });
});
