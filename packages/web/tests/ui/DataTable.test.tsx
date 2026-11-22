import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { DataTable, type Column } from "../../src/lib/ui/DataTable";
import { expectNoAxeViolations } from "./a11y-helper";
import { MOCK_TASKS, type MockTask } from "./fixtures";

const taskColumns: Column<MockTask>[] = [
  { id: "id", header: "ID", accessor: "id" },
  { id: "title", header: "Title", accessor: "title" },
  { id: "status", header: "Status", cell: (row) => <span>{row.status.toUpperCase()}</span> },
  { id: "project", header: "Project", accessor: "project", align: "right" },
];

describe("DataTable primitive", () => {
  it("TC-TBL-01 & 02: renders semantic table, headers with scope='col', caption, and scroll container", () => {
    render(<DataTable columns={taskColumns} data={MOCK_TASKS} caption="Tasks Queue" keyField="id" />);

    const scrollRegion = screen.getByRole("region", { name: "Tasks Queue" });
    expect(scrollRegion).toHaveAttribute("tabindex", "0");
    expect(scrollRegion.className).toContain("overflow-x-auto");

    const table = screen.getByRole("table", { name: "Tasks Queue" });
    expect(table).toBeInTheDocument();

    const colHeaders = screen.getAllByRole("columnheader");
    expect(colHeaders).toHaveLength(4);
    colHeaders.forEach((th) => expect(th).toHaveAttribute("scope", "col"));

    const rows = screen.getAllByRole("row");
    // 1 header row + 4 data rows = 5
    expect(rows).toHaveLength(5);
  });

  it("TC-TBL-03: applies column alignment classes", () => {
    render(<DataTable columns={taskColumns} data={MOCK_TASKS} caption="Tasks" keyField="id" />);
    const projectHeader = screen.getByRole("columnheader", { name: "Project" });
    expect(projectHeader.className).toContain("text-right");
  });

  it("TC-TBL-04 & 05: extracts data via accessor and custom cell renderers", () => {
    render(<DataTable columns={taskColumns} data={MOCK_TASKS} caption="Tasks" keyField="id" />);
    expect(screen.getByText("Modernize Control Center shell")).toBeInTheDocument();
    expect(screen.getByText("DOING")).toBeInTheDocument();
  });

  it("TC-TBL-06 & 07: renders accessible empty state spanning all columns", () => {
    const { rerender } = render(
      <DataTable columns={taskColumns} data={[]} caption="Tasks" emptyText="No tasks registered" />
    );

    const emptyCell = screen.getByRole("cell");
    expect(emptyCell).toHaveAttribute("colspan", "4");
    expect(screen.getByRole("status")).toHaveTextContent("No tasks registered");

    rerender(<DataTable columns={taskColumns} data={[]} caption="Tasks" />);
    expect(screen.getByRole("status")).toHaveTextContent("No data available");
  });

  it("TC-TBL-08 & 09: interactive rows support click, Enter, and Space activation", () => {
    const handleRowClick = vi.fn();
    render(
      <DataTable
        columns={taskColumns}
        data={MOCK_TASKS}
        caption="Tasks"
        keyField="id"
        onRowClick={handleRowClick}
      />
    );

    const dataRows = screen.getAllByRole("row").slice(1);
    const firstRow = dataRows[0];
    expect(firstRow).toHaveAttribute("tabindex", "0");

    fireEvent.click(firstRow);
    expect(handleRowClick).toHaveBeenCalledWith(MOCK_TASKS[0]);

    fireEvent.keyDown(firstRow, { key: "Enter" });
    expect(handleRowClick).toHaveBeenCalledWith(MOCK_TASKS[0]);

    fireEvent.keyDown(firstRow, { key: " " });
    expect(handleRowClick).toHaveBeenCalledWith(MOCK_TASKS[0]);
  });

  it("TC-TBL-10: supports keyboard arrow navigation between interactive rows", () => {
    render(
      <DataTable
        columns={taskColumns}
        data={MOCK_TASKS}
        caption="Tasks"
        keyField="id"
        onRowClick={() => {}}
      />
    );

    const dataRows = screen.getAllByRole("row").slice(1);
    dataRows[0].focus();
    expect(document.activeElement).toBe(dataRows[0]);

    fireEvent.keyDown(dataRows[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(dataRows[1]);

    fireEvent.keyDown(dataRows[1], { key: "ArrowUp" });
    expect(document.activeElement).toBe(dataRows[0]);
  });

  it("TC-TBL-11: interactive rows display 3px focus ring", () => {
    render(
      <DataTable
        columns={taskColumns}
        data={MOCK_TASKS}
        caption="Tasks"
        keyField="id"
        onRowClick={() => {}}
      />
    );
    const firstDataRow = screen.getAllByRole("row")[1];
    expect(firstDataRow.className).toContain("focus-visible:outline-3");
    expect(firstDataRow.className).toContain("focus-visible:outline-[var(--ua-accent)]");
  });

  it("TC-TBL-12: passes axe-core accessibility audit on populated and empty tables", async () => {
    const { container, rerender } = render(
      <DataTable columns={taskColumns} data={MOCK_TASKS} caption="Tasks Queue" keyField="id" />
    );
    await expectNoAxeViolations(container);

    rerender(<DataTable columns={taskColumns} data={[]} caption="Empty Tasks Queue" />);
    await expectNoAxeViolations(container);
  });

  it("TC-TBL-13: cell action button keydown does not bubble to onRowClick", () => {
    const handleRowClick = vi.fn();
    const handleCellAction = vi.fn();

    const cols: Column<MockTask>[] = [
      { id: "id", header: "ID", accessor: "id" },
      {
        id: "action",
        header: "Action",
        cell: () => (
          <button
            data-testid="cell-action-btn"
            onClick={(e) => {
              e.stopPropagation();
              handleCellAction();
            }}
          >
            Claim
          </button>
        ),
      },
    ];

    render(
      <DataTable
        columns={cols}
        data={[MOCK_TASKS[0]]}
        keyField="id"
        onRowClick={handleRowClick}
      />
    );

    const cellBtn = screen.getByTestId("cell-action-btn");
    fireEvent.keyDown(cellBtn, { key: "Enter" });
    expect(handleRowClick).not.toHaveBeenCalled();

    fireEvent.keyDown(cellBtn, { key: " " });
    expect(handleRowClick).not.toHaveBeenCalled();
  });

  it("TC-TBL-14: maintains true WAI-ARIA roving tabindex (single tab stop across rows)", () => {
    render(
      <DataTable
        columns={taskColumns}
        data={MOCK_TASKS}
        keyField="id"
        onRowClick={() => {}}
      />
    );

    const dataRows = screen.getAllByRole("row").slice(1);
    // Exactly ONE row has tabIndex=0
    const tabIndexZeros = dataRows.filter((r) => r.getAttribute("tabindex") === "0");
    const tabIndexNegOnes = dataRows.filter((r) => r.getAttribute("tabindex") === "-1");

    expect(tabIndexZeros).toHaveLength(1);
    expect(tabIndexNegOnes).toHaveLength(dataRows.length - 1);
    expect(dataRows[0]).toHaveAttribute("tabindex", "0");

    // Navigate ArrowDown
    fireEvent.keyDown(dataRows[0], { key: "ArrowDown" });
    expect(dataRows[0]).toHaveAttribute("tabindex", "-1");
    expect(dataRows[1]).toHaveAttribute("tabindex", "0");

    // Navigate ArrowUp
    fireEvent.keyDown(dataRows[1], { key: "ArrowUp" });
    expect(dataRows[0]).toHaveAttribute("tabindex", "0");
    expect(dataRows[1]).toHaveAttribute("tabindex", "-1");

    // Navigate End
    fireEvent.keyDown(dataRows[0], { key: "End" });
    expect(dataRows[dataRows.length - 1]).toHaveAttribute("tabindex", "0");
    expect(dataRows[0]).toHaveAttribute("tabindex", "-1");

    // Navigate Home
    fireEvent.keyDown(dataRows[dataRows.length - 1], { key: "Home" });
    expect(dataRows[0]).toHaveAttribute("tabindex", "0");
    expect(dataRows[dataRows.length - 1]).toHaveAttribute("tabindex", "-1");
  });
});
