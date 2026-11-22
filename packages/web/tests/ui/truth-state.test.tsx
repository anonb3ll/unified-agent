import { render, screen, fireEvent, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  countLabel,
  deriveTruthState,
  formatAge,
  httpStatusOf,
  type QueryLike,
} from "../../src/lib/ui/truthState";
import { FreshnessStamp } from "../../src/lib/ui/FreshnessStamp";
import { Region } from "../../src/lib/ui/Region";
import { StatePill } from "../../src/lib/ui/StatePill";

const NOW = 1_800_000_000_000;

function ok(data: unknown, updatedAt = NOW - 2_000): QueryLike {
  return { status: "success", data, error: null, dataUpdatedAt: updatedAt };
}
function failed(message: string, data?: unknown, updatedAt = 0): QueryLike {
  return { status: "error", data, error: new Error(message), dataUpdatedAt: updatedAt };
}
const pending: QueryLike = { status: "pending", data: undefined, error: null, dataUpdatedAt: 0 };

describe("httpStatusOf", () => {
  it("reads a status property or the 'HTTP nnn' the hub fetchers throw", () => {
    expect(httpStatusOf(Object.assign(new Error("x"), { status: 403 }))).toBe(403);
    expect(httpStatusOf(new Error("approvals HTTP 401: invalid token"))).toBe(401);
    expect(httpStatusOf(new Error("Failed to fetch"))).toBeNull();
    expect(httpStatusOf(null)).toBeNull();
  });
});

describe("deriveTruthState (design protocol §5)", () => {
  it("is loading while the first read is in flight", () => {
    expect(deriveTruthState([pending], { now: NOW }).state).toBe("loading");
  });

  it("is ready or empty only after every read succeeded", () => {
    expect(deriveTruthState([ok([1])], { now: NOW, isEmpty: () => false }).state).toBe("ready");
    expect(deriveTruthState([ok([])], { now: NOW, isEmpty: () => true }).state).toBe("empty");
    expect(deriveTruthState([ok([]), pending], { now: NOW, isEmpty: () => true }).state).toBe(
      "loading",
    );
  });

  it("reports denied for 401/403 and never empty", () => {
    const r = deriveTruthState([failed("attention HTTP 401")], { now: NOW, isEmpty: () => true });
    expect(r.state).toBe("denied");
    expect(r.failed).toBe(1);
  });

  it("reports unavailable for a server or network failure with no data", () => {
    expect(deriveTruthState([failed("agents HTTP 503")], { now: NOW }).state).toBe("unavailable");
    expect(deriveTruthState([failed("Failed to fetch")], { now: NOW }).state).toBe("unavailable");
  });

  it("does not expose children when one first read fails and another is pending", () => {
    const children = vi.fn(() => <p>rows</p>);
    expect(deriveTruthState([failed("HTTP 503"), pending]).state).toBe("loading");
    render(<Region title="Fleet" queries={[failed("HTTP 503"), pending]}>{children}</Region>);
    expect(children).not.toHaveBeenCalled();
  });

  it("marks successful HTTP responses with failed subreads partial", () => {
    const truth = deriveTruthState([ok({ items: [], errors: ["task_list failed"] }), ok([])], { isEmpty: () => true });
    expect(truth.state).toBe("partial");
    expect(truth.failed).toBe(1);
  });

  it("is partial when one source answered and another failed", () => {
    const r = deriveTruthState([ok([]), failed("attention HTTP 500")], {
      now: NOW,
      isEmpty: () => true,
    });
    expect(r.state).toBe("partial");
    expect(r.failed).toBe(1);
  });

  it("is stale when a refetch failed but the last good data is kept", () => {
    const r = deriveTruthState([failed("agents HTTP 502", [1], NOW - 90_000)], { now: NOW });
    expect(r.state).toBe("stale");
    expect(r.updatedAt).toBe(NOW - 90_000);
  });

  it("is stale when the newest success is older than the freshness budget", () => {
    const r = deriveTruthState([ok([1], NOW - 120_000)], { now: NOW, staleAfterMs: 60_000 });
    expect(r.state).toBe("stale");
  });

  it("reports the oldest success as the region's age", () => {
    const r = deriveTruthState([ok([1], NOW - 5_000), ok([1], NOW - 50_000)], { now: NOW });
    expect(r.updatedAt).toBe(NOW - 50_000);
  });
});

describe("countLabel", () => {
  it("shows a count only for a complete read; otherwise an em dash", () => {
    expect(countLabel("ready", 3)).toBe("3");
    expect(countLabel("empty", 0)).toBe("0");
    for (const s of ["loading", "partial", "stale", "denied", "unavailable"] as const) {
      expect(countLabel(s, 0)).toBe("—");
    }
  });
});

describe("formatAge", () => {
  it("steps seconds, minutes, hours and days", () => {
    expect(formatAge(4_000)).toBe("4s");
    expect(formatAge(125_000)).toBe("2m");
    expect(formatAge(3 * 3_600_000)).toBe("3h");
    expect(formatAge(50 * 3_600_000)).toBe("2d");
    expect(formatAge(-10)).toBe("0s");
  });
});

describe("StatePill", () => {
  it("pairs every state colour with a glyph and an uppercase word", () => {
    render(<StatePill kind="decision" />);
    const pill = screen.getByText("Decision").closest("[data-state]");
    expect(pill).toHaveAttribute("data-state", "decision");
    expect(pill?.className).toContain("text-[var(--ua-decision)]");
    expect(pill?.className).toContain("uppercase");
    expect(pill?.querySelector("[aria-hidden='true']")?.textContent).toBe("◆");
  });

  it("renders unknown hatched, distinct from idle", () => {
    render(<StatePill kind="unknown" label="No receipt" />);
    const pill = screen.getByText("No receipt").closest("[data-state]") as HTMLElement;
    expect(pill.style.backgroundImage).toContain("repeating-linear-gradient");
  });
});

describe("FreshnessStamp", () => {
  it("updates age without a parent render and cleans up its clock", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    try {
      const { unmount } = render(<FreshnessStamp state="ready" updatedAt={NOW - 4_000} />);
      expect(screen.getByText("updated 4s ago")).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(60_000));
      expect(screen.getByText("updated 1m ago")).toBeInTheDocument();
      unmount();
      expect(vi.getTimerCount()).toBe(0);
    } finally { vi.useRealTimers(); }
  });
  it("says how old a ready region is, with the exact time in the title", () => {
    render(<FreshnessStamp state="ready" updatedAt={NOW - 4_000} now={NOW} />);
    const stamp = screen.getByText("updated 4s ago");
    expect(stamp.closest("[title]")?.getAttribute("title")).toMatch(/^Last updated /);
  });

  it("flags stale and partial in the warn colour", () => {
    const { rerender } = render(<FreshnessStamp state="stale" updatedAt={NOW - 26 * 3_600_000} now={NOW} />);
    expect(screen.getByText("stale · 1d").className).toContain("text-[var(--ua-warn)]");
    rerender(<FreshnessStamp state="partial" updatedAt={NOW} now={NOW} />);
    expect(screen.getByText("partial").className).toContain("text-[var(--ua-warn)]");
  });
});

describe("Region", () => {
  const base = { title: "Fleet", now: NOW, emptyText: "No agents registered." };

  it("never renders the empty sentence after a failed read", () => {
    render(
      <Region {...base} queries={[failed("agents HTTP 401")]} isEmpty={() => true}>
        {() => <p>rows</p>}
      </Region>,
    );
    expect(screen.queryByText("No agents registered.")).not.toBeInTheDocument();
    expect(screen.getByText("Your profile can't read Fleet.")).toBeInTheDocument();
  });

  it("shows a hatched unavailable block with the failure and a retry", () => {
    const refetch = vi.fn();
    render(
      <Region {...base} queries={[{ ...failed("agents HTTP 503"), refetch }]}>
        {() => <p>rows</p>}
      </Region>,
    );
    expect(screen.getByText("Couldn't load Fleet.")).toBeInTheDocument();
    expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry Fleet" }));
    expect(refetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("rows")).not.toBeInTheDocument();
  });

  it("renders the empty sentence only when every read succeeded", () => {
    render(
      <Region {...base} queries={[ok([])]} isEmpty={() => true}>
        {() => <p>rows</p>}
      </Region>,
    );
    expect(screen.getByText("No agents registered.")).toBeInTheDocument();
    expect(screen.getByText("updated 2s ago")).toBeInTheDocument();
  });

  it("keeps last known rows when stale and says so", () => {
    render(
      <Region {...base} queries={[failed("agents HTTP 502", [1], NOW - 600_000)]}>
        {() => <p>rows</p>}
      </Region>,
    );
    expect(screen.getByText("rows")).toBeInTheDocument();
    expect(screen.getByText("stale · 10m")).toBeInTheDocument();
  });

  it("marks partial data and keeps it visible", () => {
    render(
      <Region {...base} queries={[ok([1]), failed("attention HTTP 500")]}>
        {() => <p>rows</p>}
      </Region>,
    );
    expect(screen.getByText("rows")).toBeInTheDocument();
    expect(screen.getByText(/1 of 2 sources failed/)).toBeInTheDocument();
  });

  it("is busy with skeleton rows while loading", () => {
    render(
      <Region {...base} queries={[pending]}>
        {() => <p>rows</p>}
      </Region>,
    );
    expect(screen.getByRole("region", { name: "Fleet" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("Loading Fleet…")).toBeInTheDocument();
  });
});
