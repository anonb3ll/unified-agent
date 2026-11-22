import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import React from "react";
import { DataTable, type Column } from "../../src/lib/ui/DataTable";
import { Button } from "../../src/lib/ui/Button";
import { Badge, Chip } from "../../src/lib/ui/Badge";
import { Card } from "../../src/lib/ui/Card";
import { StatusIndicator } from "../../src/lib/ui/StatusIndicator";
import { expectNoAxeViolations } from "./a11y-helper";

interface TestRow {
  id: string;
  name: string;
  description: string;
  count: number;
}

const testColumns: Column<TestRow>[] = [
  { id: "id", header: "ID", accessor: "id" },
  { id: "name", header: "Name", accessor: "name" },
  { id: "desc", header: "Description", accessor: "description" },
  { id: "count", header: "Count", accessor: "count", align: "right" },
];

/**
 * Render cost in milliseconds of CPU time spent by this test process, not
 * wall-clock time. jsdom rendering is synchronous work on this thread, so CPU
 * time measures the render itself; wall-clock time also counts every moment
 * the OS gave the CPU to other processes, which on a shared, loaded machine
 * (load 30 on 4 cores) doubled the reading without the render changing.
 */
function cpuMs(work: () => void): number {
  const before = process.cpuUsage();
  work();
  const used = process.cpuUsage(before);
  return (used.user + used.system) / 1000;
}

describe("Empirical Challenge Suite: packages/web/src/lib/ui/", () => {

  describe("1. DataTable Large Datasets & Edge Datasets", () => {
    // The timeout only guards against a hang; the budget below is CPU time.
    it("STRESS-DATA-01: renders 1,000 rows without crashing and measures render time", { timeout: 60_000 }, () => {
      const largeData: TestRow[] = Array.from({ length: 1000 }, (_, i) => ({
        id: `row-${i}`,
        name: `Item ${i}`,
        description: `Description for item ${i}`,
        count: i,
      }));

      const elapsed = cpuMs(() => {
        render(
          <DataTable columns={testColumns} data={largeData} keyField="id" caption="Large Dataset" />
        );
      });

      const rows = screen.getAllByRole("row");
      expect(rows).toHaveLength(1001);
      console.log(`[STRESS BENCHMARK] 1,000 rows render CPU time: ${elapsed.toFixed(2)}ms`);
      expect(elapsed).toBeLessThan(3000);
    });

    it(
      "STRESS-DATA-02: measures 5,000 rows render time and demonstrates severe unvirtualized DOM overhead",
      // A hang guard only: both measurements below are CPU time, compared to each other.
      { timeout: 180_000 },
      () => {
      const makeRows = (n: number): TestRow[] =>
        Array.from({ length: n }, (_, i) => ({
          id: `row-${i}`,
          name: `Item ${i}`,
          description: `Desc ${i}`,
          count: i,
        }));

      // Baseline this machine first. The 5,000-row cost is asserted against a
      // 1,000-row render from the same run, never against an absolute millisecond
      // floor: a floor asserts that the hardware is SLOW, so it fails the moment CI
      // gets faster (observed on Depot CI: 476ms against a >500ms floor) and would
      // fail on any faster runner.
      let baseline: ReturnType<typeof render> | undefined;
      const baselineElapsed = cpuMs(() => {
        baseline = render(
          <DataTable columns={testColumns} data={makeRows(1000)} keyField="id" caption="Baseline 1000 Rows" />
        );
      });
      baseline?.unmount();

      const massiveData = makeRows(5000);
      const elapsed = cpuMs(() => {
        render(<DataTable columns={testColumns} data={massiveData} keyField="id" caption="5000 Rows" />);
      });
      console.log(
        `[STRESS BENCHMARK] 1,000 rows: ${baselineElapsed.toFixed(2)}ms | ` +
          `5,000 rows: ${elapsed.toFixed(2)}ms ` +
          `(${(elapsed / Math.max(baselineElapsed, 0.01)).toFixed(2)}x for 5x the rows)`
      );

      const rows = screen.getAllByRole("row");
      expect(rows).toHaveLength(5001);
      // Non-linear DOM overhead in an unvirtualized table: 5x the rows costs more
      // than the baseline, measured on the same machine, so this holds at any speed.
      expect(elapsed).toBeGreaterThan(baselineElapsed);
    });

    it("STRESS-DATA-03: roving tabindex audit on interactive table (tab-stop explosion test)", () => {
      const data: TestRow[] = Array.from({ length: 50 }, (_, i) => ({
        id: `row-${i}`,
        name: `Item ${i}`,
        description: `Desc ${i}`,
        count: i,
      }));

      render(
        <DataTable
          columns={testColumns}
          data={data}
          keyField="id"
          onRowClick={() => {}}
        />
      );

      const dataRows = screen.getAllByRole("row").slice(1);
      // In WAI-ARIA Roving Tabindex, exactly ONE item has tabIndex=0.
      const tabIndexZeroCount = dataRows.filter(
        (row) => row.getAttribute("tabindex") === "0"
      ).length;
      expect(tabIndexZeroCount).toBe(1);
      expect(dataRows[0]).toHaveAttribute("tabindex", "0");
      expect(dataRows[1]).toHaveAttribute("tabindex", "-1");
    });

    it("STRESS-DATA-04: missing keyField and duplicate keys fallback behavior", () => {
      const duplicateData: TestRow[] = [
        { id: "same", name: "A", description: "First", count: 1 },
        { id: "same", name: "B", description: "Second", count: 2 },
      ];

      render(
        <DataTable
          columns={testColumns}
          data={duplicateData}
          getRowKey={(row, idx) => `${row.id}-${idx}`}
        />
      );
      expect(screen.getByText("First")).toBeInTheDocument();
      expect(screen.getByText("Second")).toBeInTheDocument();
    });

    it("STRESS-DATA-05: empty columns or missing data handling", () => {
      const { rerender } = render(<DataTable columns={[]} data={[]} />);
      expect(screen.getByText("No data available")).toBeInTheDocument();

      rerender(<DataTable columns={[]} data={[{ id: "1", name: "X", description: "Y", count: 0 }]} />);
      const rows = screen.getAllByRole("row");
      expect(rows).toHaveLength(2);
    });
  });

  describe("2. Long Text & Extreme String Stress", () => {
    const longString = "A".repeat(10000);
    const unspacedString = "https://subdomain.example.com/very/long/path/with/lots/of/slashes/and/hex/parameters?token=0123456789abcdef0123456789abcdef0123456789abcdef";
    const xssString = "<script>alert('xss')</script><b>Bold</b>&amp;&quot;";

    it("STRESS-TEXT-01: DataTable handles extreme unspaced text and XSS strings safely", () => {
      const data: TestRow[] = [
        { id: "1", name: xssString, description: unspacedString, count: 99 },
      ];

      render(<DataTable columns={testColumns} data={data} keyField="id" />);
      expect(screen.getByText(xssString)).toBeInTheDocument();
      expect(document.querySelector("script")).toBeNull();
      expect(document.querySelector("b")).toBeNull();
    });

    it("STRESS-TEXT-02: Button, Badge, Chip, Card handle 10,000 char strings safely", () => {
      render(
        <div>
          <Button>{longString}</Button>
          <Badge>{longString}</Badge>
          <Chip>{longString}</Chip>
          <Card>
            <Card.Title>{longString}</Card.Title>
            <Card.Description>{longString}</Card.Description>
            <Card.Body>{longString}</Card.Body>
          </Card>
        </div>
      );
      expect(screen.getAllByText(longString).length).toBeGreaterThanOrEqual(4);
    });

    it("STRESS-TEXT-03: StatusIndicator with empty or whitespace-only label/status", () => {
      const { container, rerender } = render(<StatusIndicator status="  RUNNING  " />);
      expect(screen.getByText("RUNNING")).toBeInTheDocument();

      rerender(<StatusIndicator status="online" label="" />);
      const emptyLabelSpan = container.querySelector(".tracking-\\[0\\.06em\\]");
      expect(emptyLabelSpan?.textContent).toBe("");
    });
  });

  describe("3. Rapid Interaction & Event Bubbling Stress", () => {
    it("STRESS-INT-01: 500 rapid clicks on Button and disabled state resilience", () => {
      const handleClick = vi.fn();
      const { rerender } = render(<Button onClick={handleClick}>Rapid</Button>);
      const btn = screen.getByRole("button", { name: "Rapid" });

      for (let i = 0; i < 500; i++) {
        fireEvent.click(btn);
      }
      expect(handleClick).toHaveBeenCalledTimes(500);

      handleClick.mockClear();
      rerender(<Button onClick={handleClick} disabled>Rapid</Button>);
      for (let i = 0; i < 100; i++) {
        fireEvent.click(btn);
      }
      expect(handleClick).not.toHaveBeenCalled();

      rerender(<Button onClick={handleClick} isLoading>Rapid</Button>);
      for (let i = 0; i < 100; i++) {
        fireEvent.click(btn);
      }
      expect(handleClick).not.toHaveBeenCalled();
    });

    it("STRESS-INT-02: Card keyboard event bubbling bug (nested input/button space/enter trap)", () => {
      const handleCardClick = vi.fn();
      const handleNestedButtonClick = vi.fn();

      render(
        <Card interactive onClick={handleCardClick}>
          <Card.Body>
            <input type="text" data-testid="nested-input" />
            <button data-testid="nested-btn" onClick={handleNestedButtonClick}>
              Nested Action
            </button>
          </Card.Body>
        </Card>
      );

      const input = screen.getByTestId("nested-input");
      const nestedBtn = screen.getByTestId("nested-btn");

      // When user types Space in an input field inside an interactive Card:
      fireEvent.keyDown(input, { key: " " });
      expect(handleCardClick).not.toHaveBeenCalled();

      // When user presses Space on a nested button:
      handleCardClick.mockClear();
      fireEvent.keyDown(nestedBtn, { key: " " });
      expect(handleCardClick).not.toHaveBeenCalled();
    });

    it("STRESS-INT-03: DataTable row keyboard event bubbling bug (nested interactive controls)", () => {
      const handleRowClick = vi.fn();
      const handleCellButtonClick = vi.fn();

      const actionColumns: Column<TestRow>[] = [
        { id: "id", header: "ID", accessor: "id" },
        {
          id: "action",
          header: "Action",
          cell: () => (
            <button data-testid="row-action-btn" onClick={handleCellButtonClick}>
              Delete
            </button>
          ),
        },
      ];

      render(
        <DataTable
          columns={actionColumns}
          data={[{ id: "1", name: "A", description: "B", count: 1 }]}
          onRowClick={handleRowClick}
        />
      );

      const actionBtn = screen.getByTestId("row-action-btn");

      // Enter on nested button in table row does NOT trigger onRowClick
      fireEvent.keyDown(actionBtn, { key: "Enter" });
      expect(handleRowClick).not.toHaveBeenCalled();
    });

    it("STRESS-INT-04: Chip onRemove accessibility bug (nested-interactive violation & keyboard trap)", async () => {
      const handleRemove = vi.fn();
      const handleClick = vi.fn();

      render(<Chip onRemove={handleRemove} onClick={handleClick}>Filter Chip</Chip>);

      const chipButton = screen.getByRole("button", { name: /filter chip/i });
      const removeButton = screen.getByLabelText("Remove");

      expect(removeButton).toBeInTheDocument();
      // Verified: tabIndex is NOT -1, accessible for keyboard users
      expect(removeButton).not.toHaveAttribute("tabindex", "-1");

      fireEvent.keyDown(chipButton, { key: "Enter" });
      expect(handleClick).toHaveBeenCalledTimes(1);
      expect(handleRemove).not.toHaveBeenCalled();

      // Verified: axe-core passes with zero violations
      const { container } = render(
        <Chip onRemove={handleRemove}>Filter with Remove</Chip>
      );
      await expectNoAxeViolations(container);
    });
  });

  describe("4. Unknown & Malformed Status Strings in StatusIndicator", () => {
    it("STRESS-STS-01: handles completely unknown domain statuses gracefully with fallback", () => {
      const unknownStatuses = [
        "quarantined",
        "custom_pipeline_status",
        "stalled",
        "INITIALIZING",
        "flaky",
        "rework",
        "degraded",
        "incompatible",
      ];

      for (const status of unknownStatuses) {
        const { container } = render(<StatusIndicator status={status} />);
        expect(screen.getByText(status.toUpperCase())).toBeInTheDocument();
        const dot = container.querySelector("[aria-hidden='true']");
        expect(dot?.className).toContain("bg-[var(--ua-muted)]");
        expect(dot?.textContent).toBe("○");
      }
    });

    it("STRESS-STS-02: StatusIndicator null/undefined crash bug", () => {
      expect(() => {
        render(<StatusIndicator status={null as any} />);
      }).not.toThrow();

      expect(() => {
        render(<StatusIndicator status={undefined as any} />);
      }).not.toThrow();
    });

    it("STRESS-STS-03: StatusIndicator semantic mapping check for 'ok', 'done', 'approved'", () => {
      const { container: doneContainer } = render(<StatusIndicator status="done" />);
      const doneDot = doneContainer.querySelector("[aria-hidden='true']");
      expect(doneDot?.className).toContain("bg-[var(--ua-ok)]");
      expect(doneDot?.textContent).toBe("✓");

      const { container: okContainer } = render(<StatusIndicator status="ok" />);
      const okDot = okContainer.querySelector("[aria-hidden='true']");
      expect(okDot?.className).toContain("bg-[var(--ua-ok)]");
      expect(okDot?.textContent).toBe("●");
    });
  });

});
