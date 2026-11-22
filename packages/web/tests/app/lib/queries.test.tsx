import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QueryRegion } from "../../../src/app/lib/queries";

describe("QueryRegion", () => {
  it("a failed refetch over retained data shows the error with Retry and the data's age (slices)", () => {
    const refetch = vi.fn();
    render(
      <QueryRegion
        query={{ status: "error", data: ["Task 1"], error: new Error("tasks HTTP 503"), dataUpdatedAt: Date.now() - 5 * 60_000, refetch }}
        loadingLabel="Loading tasks"
        empty={{ title: "No tasks" }}
      >
        {(rows) => <p>{rows.join(",")}</p>}
      </QueryRegion>,
    );
    expect(screen.getByText("Task 1")).toBeInTheDocument();
    expect(screen.getByText(/The hub had a problem answering/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalled();
    expect(screen.getByText(/5m/)).toBeInTheDocument();
  });

  it("loading, error, empty and ready are exclusive", () => {
    const { rerender } = render(
      <QueryRegion query={{ status: "pending" }} loadingLabel="Loading tasks" empty={{ title: "No tasks" }}>
        {() => <p>ready</p>}
      </QueryRegion>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Loading tasks");
    rerender(
      <QueryRegion query={{ status: "error", error: new Error("tasks HTTP 503"), refetch: () => {} }} loadingLabel="Loading tasks" empty={{ title: "No tasks" }}>
        {() => <p>ready</p>}
      </QueryRegion>,
    );
    expect(screen.queryByText("ready")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    rerender(
      <QueryRegion query={{ status: "success", data: [], dataUpdatedAt: Date.now() }} isEmpty={(d: unknown[]) => d.length === 0} loadingLabel="Loading tasks" empty={{ title: "No tasks" }}>
        {() => <p>ready</p>}
      </QueryRegion>,
    );
    expect(screen.getByRole("heading", { name: "No tasks" })).toBeInTheDocument();
  });

  it("round 6 item 7: Retry refetches every failed source, including a secondary one", () => {
    const primary = vi.fn();
    const secondary = vi.fn();
    const healthy = vi.fn();
    render(
      <QueryRegion
        query={{ status: "success", data: ["Task 1"], dataUpdatedAt: Date.now(), refetch: primary }}
        also={[
          { status: "error", data: ["agents"], error: new Error("agents HTTP 503"), dataUpdatedAt: Date.now() - 60_000, refetch: secondary },
          { status: "success", data: ["ok"], dataUpdatedAt: Date.now(), refetch: healthy },
        ]}
        loadingLabel="Loading tasks"
        empty={{ title: "No tasks" }}
      >
        {(rows) => <p>{rows.join(",")}</p>}
      </QueryRegion>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(secondary).toHaveBeenCalledTimes(1);
    expect(healthy).not.toHaveBeenCalled();
  });
});
