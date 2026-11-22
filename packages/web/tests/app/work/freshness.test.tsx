import { act, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import type { MockHandler } from "../support/fetchMock";
import { findPageHeading, renderRoute } from "../support/render";
import { GOAL_7, PLAN_3, TASK_102 } from "./fixtures";
import { workHub } from "./hub";
import { fixtureEpoch } from "../support/epoch";

vi.setConfig({ testTimeout: 60_000 });

afterEach(() => {
  // Unmount first: a form flushes its draft on unmount, which must not outlive the clear below.
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

/** Answers once with `first`, then with `after` (a refetch that fails). */
function thenFails(first: unknown, after: { status: number; body: unknown }): MockHandler {
  let n = 0;
  return () => (n++ === 0 ? { body: first } : after);
}

// Review round 1, finding 13: a failed refetch left the old object fully actionable with no sign.
describe("work: retained data after a failed refetch", () => {
  it("a task that is gone on refetch (an approved delete ran) says it no longer exists", async () => {
    const { appClient } = renderRoute("/work/tasks/102", {
      hub: workHub({ "GET /api/platform/objects/task/102": thenFails(TASK_102, { status: 404, body: { error: "unknown task: 102" } }) }),
    });
    await findPageHeading("Ship the Work area");
    await act(async () => {
      await appClient().invalidateQueries({ queryKey: ["work", "task", "102"] });
    });
    expect(await screen.findByText("This task no longer exists")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Release claim" })).not.toBeInTheDocument();
  });

  it.each([
    ["/work/tasks/102", "Ship the Work area", "GET /api/platform/objects/task/102", TASK_102, ["work", "task", "102"]],
    ["/work/goals/7", "A usable Control Center", "GET /api/goals/7", GOAL_7, ["work", "goal", "7"]],
    ["/work/plans/3", "Roll out Control Center v2", "GET /api/plans/3", PLAN_3, ["work", "plan", "3"]],
  ] as const)("%s keeps its data but says the refresh failed", async (path, title, key, body, queryKey) => {
    const { appClient } = renderRoute(path, { hub: workHub({ [key]: thenFails(body, { status: 500, body: { error: "database is locked" } }) }) });
    await findPageHeading(title);
    await act(async () => {
      await appClient().invalidateQueries({ queryKey: [...queryKey] });
    });
    const banner = await screen.findByRole("alert", {}, { timeout: 15_000 });
    expect(banner).toHaveTextContent(/could not be refreshed/);
    expect(banner).toHaveTextContent(/as of/);
    expect(screen.getByRole("heading", { level: 1, name: title })).toBeInTheDocument();
  });
});
