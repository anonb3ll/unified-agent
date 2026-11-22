import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findPageHeading, renderRoute } from "../support/render";
import { approvalsPayload, digestPayload, emptyHub, failingHub, fullHub, openSessions, runsPayload, type HubTable } from "../inbox/fixtures";
import { vitestHub } from "../inbox/support";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

function render(path: string, table: HubTable = fullHub(), extra: Parameters<typeof vitestHub>[1] = {}) {
  return renderRoute(path, { hub: vitestHub(table, extra) });
}

const region = (name: string) => screen.getByRole("region", { name: new RegExp(`^${name}`) });
const T = { timeout: 8000 };

describe("Home", () => {
  it("shows Needs you, Running now, Just finished and the fleet from realistic reads", async () => {
    render("/home");
    await findPageHeading("Home");
    const needs = await screen.findByRole("list", { name: "Waiting on you" }, T);
    expect(within(needs).getAllByRole("listitem")).toHaveLength(5);
    expect(within(region("Needs you")).getByRole("link", { name: "See all" })).toHaveAttribute("href", "/ui/inbox");

    const running = await screen.findByRole("list", { name: "Running now" }, T);
    expect(running).toHaveTextContent("Ship the inbox");
    expect(running).toHaveTextContent("Beta research");
    expect(running).toHaveTextContent("Sent to the agent");
    expect(within(running).getByRole("link", { name: "Task 43" })).toHaveAttribute("href", "/ui/work/tasks/43");

    const finished = await screen.findByRole("list", { name: "Just finished" }, T);
    expect(finished).toHaveTextContent("Fix the flaky test");
    expect(screen.getByRole("list", { name: "In review" })).toHaveTextContent("Tidy the docs");
    expect(finished).toHaveTextContent("Session ended (ok)");

    const fleet = await screen.findByRole("list", { name: "Agents and their state" }, T);
    expect(within(fleet).getByRole("link", { name: "Desktop_Agent2" })).toHaveAttribute("href", "/ui/agents/profile/Desktop_Agent2");
    expect(fleet).toHaveTextContent("Offline");
    // The org is not empty, so there is no checklist.
    expect(screen.queryByRole("region", { name: "Get started" })).toBeNull();
  });

  it("never renders a count before its read completes (defect 4)", async () => {
    render("/home", fullHub(), {
      "GET /api/runs": () => new Promise<never>(() => {}),
    });
    await findPageHeading("Home");
    const running = region("Running now");
    expect(within(running).getByText("–")).toBeInTheDocument();
    expect(within(running).queryByText("0")).toBeNull();
    expect(within(running).getByRole("status")).toHaveTextContent("Loading what is running");
  });

  it("failed reads show an error with Retry per region, never a zero, and the raw text only in the detail (defects 4, 10)", async () => {
    render("/home", failingHub());
    await findPageHeading("Home");
    for (const title of ["What is running could not be loaded", "What just finished could not be loaded", "Agents could not be loaded", "What needs you could not be loaded"]) {
      const heading = await screen.findByRole("heading", { name: new RegExp(title) }, T);
      const box = heading.closest("section") as HTMLElement;
      expect(within(box).getByRole("button", { name: "Retry" })).toBeInTheDocument();
      expect(box).not.toHaveTextContent(/^upstream timeout/);
    }
    for (const name of ["Needs you", "Running now", "Just finished"]) {
      expect(within(region(name)).getAllByText("–").length).toBeGreaterThan(0);
      expect(within(region(name)).queryByText("0")).toBeNull();
    }
  });

  it("the project filter lives in the URL and scopes every region", async () => {
    const { router } = render("/home");
    await screen.findByRole("list", { name: "Running now" }, T);
    const select = screen.getByLabelText("Project");
    await waitFor(() => expect(within(select).getByRole("option", { name: "Beta" })).toBeInTheDocument());
    fireEvent.change(select, { target: { value: "beta" } });
    await waitFor(() => expect(router.state.location.search).toBe("?project=beta"));
    const running = await screen.findByRole("list", { name: "Running now" });
    await waitFor(() => expect(running).not.toHaveTextContent("Ship the inbox"));
    expect(running).toHaveTextContent("Beta research");
    expect(screen.getByRole("list", { name: "In review" })).toHaveTextContent("Tidy the docs");
    expect(region("Just finished")).not.toHaveTextContent("Fix the flaky test");
    const needs = screen.getByRole("list", { name: "Waiting on you" });
    expect(needs).not.toHaveTextContent("Rotate hub tokens");
    expect(needs).toHaveTextContent("Renew the domain");
  });

  it("Running now follows the runs pages, so an older active run is not lost (review 1.11)", async () => {
    const finished = (i: number) => ({ ...runsPayload.runs[1], run_id: `run-f${i}` });
    render("/home", fullHub(), {
      "GET /api/runs": ({ url }) => {
        const cursor = new URL(url, "http://localhost").searchParams.get("cursor");
        return cursor === "c1"
          ? { body: { runs: [{ ...runsPayload.runs[0], run_id: "run-old", task_id: 99, started_at: "2026-11-09T01:00:00+00:00" }], count: 1, truncated: false, next_cursor: null, limit: 100 } }
          : { body: { runs: Array.from({ length: 100 }, (_, i) => finished(i)), count: 100, truncated: true, next_cursor: "c1", limit: 100 } };
      },
    });
    const running = await screen.findByRole("list", { name: "Running now" }, T);
    await waitFor(() => expect(running).toHaveTextContent("Task 99"));
  });

  it("Running now says when the runs could not all be read, and shows no exact count (review 1.11)", async () => {
    render("/home", fullHub(), {
      "GET /api/runs": ({ url }) => {
        const n = Number(new URL(url, "http://localhost").searchParams.get("cursor") ?? "0");
        return { body: { runs: [{ ...runsPayload.runs[1], run_id: `run-${n}` }], count: 1, truncated: true, next_cursor: String(n + 1), limit: 100 } };
      },
    });
    await screen.findByRole("list", { name: "Running now" }, T);
    const running = region("Running now");
    expect(await within(running).findByText(/Not every run could be read/)).toBeInTheDocument();
    expect(within(running).getAllByText("–").length).toBeGreaterThan(0);
  });

  it("Just finished uses one rolling 24-hour window; work in review is listed without claiming it is recent (review 1.12)", async () => {
    render("/home", fullHub(), {
      "GET /api/digest": {
        body: {
          ...digestPayload,
          changed_today: {
            tasks_completed: [
              ...digestPayload.changed_today.tasks_completed,
              { id: 40, title: "Old completion", completed_at: new Date(Date.now() - 30 * 3600_000).toISOString(), claimed_by: "Mac_Claude", project: "unified-agent" },
            ],
            total_completed: 2,
          },
        },
      },
    });
    const finished = await screen.findByRole("list", { name: "Just finished" }, T);
    expect(finished).toHaveTextContent("Fix the flaky test");
    expect(finished).not.toHaveTextContent("Old completion");
    // A dispatch run that ended 4 hours ago is in the window.
    expect(finished).toHaveTextContent("Task 44");
    expect(finished).not.toHaveTextContent("Tidy the docs");
    const review = screen.getByRole("list", { name: "In review" });
    expect(review).toHaveTextContent("Tidy the docs");
    expect(region("Just finished")).toHaveTextContent(/in the last 24 hours/);
  });

  it("Just finished shows no exact count when the runs could not all be read (review 2.8)", async () => {
    render("/home", fullHub(), {
      "GET /api/runs": ({ url }) => {
        const n = Number(new URL(url, "http://localhost").searchParams.get("cursor") ?? "0");
        return { body: { runs: [{ ...runsPayload.runs[1], run_id: `run-${n}` }], count: 1, truncated: true, next_cursor: String(n + 1), limit: 100 } };
      },
    });
    await screen.findByRole("list", { name: "Just finished" }, T);
    const finished = region("Just finished");
    expect(within(finished).getAllByText("–").length).toBeGreaterThan(0);
    expect(finished).toHaveTextContent(/Not every run could be read/);
  });

  it("a full page of sessions makes Running now and Just finished incomplete (review 2.8)", async () => {
    const many = Array.from({ length: 100 }, (_, i) => ({ ...openSessions.sessions[0], id: `sess-${i}` }));
    render("/home", fullHub(), {
      "GET /api/sessions": () => ({ body: { ...openSessions, sessions: many, open: 100, total: 100 } }),
    });
    await screen.findByRole("list", { name: "Running now" }, T);
    const running = region("Running now");
    expect(within(running).getAllByText("–").length).toBeGreaterThan(0);
    expect(running).toHaveTextContent(/The hub returned 100 sessions, the most it returns at once/);
    expect(within(region("Just finished")).getAllByText("–").length).toBeGreaterThan(0);
  });

  it("the project-filtered Needs you count keeps the source's lower bound (review 2.8)", async () => {
    const pending = Array.from({ length: 100 }, (_, i) => ({ ...approvalsPayload.pending[0], id: 1000 + i }));
    render("/home?project=beta", { ...fullHub(), "GET /api/approvals": { body: { ...approvalsPayload, pending } } });
    await screen.findByRole("list", { name: "Waiting on you" }, T);
    await waitFor(() => expect(within(region("Needs you")).getByRole("heading", { level: 2 })).toHaveTextContent(/\d+\+/));
  });

  it("Needs you is qualified because work proposals cannot be listed (review 2.9)", async () => {
    render("/home", emptyHub());
    await findPageHeading("Home");
    const needs = region("Needs you");
    expect(await within(needs).findByText(/Work proposals cannot be listed on this hub yet, so this may not be everything/, {}, T)).toBeInTheDocument();
    expect(within(needs).getByRole("heading", { level: 2 })).toHaveTextContent("0+");
  });

  it("a reload with ?project= restores the filter", async () => {
    render("/home?project=beta");
    await screen.findByRole("list", { name: "Running now" }, T);
    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("beta"));
  });

  it("shows the first-run checklist for an empty organization, from hub reads", async () => {
    render("/home", emptyHub());
    const checklist = await screen.findByRole("region", { name: "Get started" }, T);
    await waitFor(() => expect(checklist).toHaveTextContent("0 of 4 done"));
    expect(within(checklist).getByRole("link", { name: "Add an agent" })).toHaveAttribute("href", "/ui/agents/add");
    expect(within(checklist).getByRole("link", { name: "Start a session" })).toHaveAttribute("href", "/ui/sessions/new");
    expect(within(checklist).getByRole("link", { name: "Create a task" })).toHaveAttribute("href", "/ui/work/tasks/new");
    expect(within(checklist).getByRole("link", { name: "Write a note" })).toHaveAttribute("href", "/ui/knowledge/memory?new=1");
    expect(await within(region("Needs you")).findByText("Nothing is waiting on you that this hub can list")).toBeInTheDocument();
    expect(within(region("Running now")).getByText("0")).toBeInTheDocument();
  });

  it("the checklist reads an error body as unknown and does not take no active tasks for no tasks (review 1.13)", async () => {
    const table = emptyHub();
    table["GET /api/agents"] = { body: { agents: [], counts: {}, error: "agent registry unavailable" } };
    table["GET /api/memory"] = { body: { total: 0, items: [], results: [] } };
    render("/home", table);
    const checklist = await screen.findByRole("region", { name: "Get started" }, T);
    const step = (name: RegExp) => within(checklist).getAllByRole("listitem").find((li) => name.test(li.textContent ?? "")) as HTMLElement;
    await waitFor(() => expect(step(/Connect an agent/)).toHaveTextContent("Could not check"));
    expect(step(/Create a task/)).toHaveTextContent("Could not check");
    expect(step(/Create a task/)).toHaveTextContent(/finished tasks/);
    expect(step(/Save a memory note/)).toHaveTextContent("not done yet");
  });

  it("Act opens the item with its action inline, without leaving Home", async () => {
    const { router } = render("/home");
    const needs = await screen.findByRole("list", { name: "Waiting on you" }, T);
    fireEvent.click(within(needs).getByRole("button", { name: /^Act on: task_delete wants to run/ }));
    const drawer = await screen.findByRole("dialog", { name: "Approval" });
    expect(await within(drawer).findByRole("button", { name: "Approve" }, T)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/ui/home");
  });
});
