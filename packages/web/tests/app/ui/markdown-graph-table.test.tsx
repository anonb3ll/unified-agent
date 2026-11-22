import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DataTable, GraphCanvas, layeredLayout, Markdown, safeHref } from "../../../src/lib/ui";

describe("Markdown", () => {
  it("renders structure with headings shifted one level", () => {
    render(
      <Markdown
        source={"---\ntags: [a]\n---\n# Title\n\nSome **bold**, *em*, `code` and ~~gone~~.\n\n- one\n  - nested\n- [x] done\n\n1. first\n\n> quoted\n\n```ts\nconst a = 1;\n```\n\n| A | B |\n|---|--:|\n| 1 | 2 |\n\n---"}
      />,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Title" })).toBeInTheDocument();
    expect(screen.queryByText(/tags:/)).not.toBeInTheDocument();
    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.getByText("em").tagName).toBe("EM");
    expect(screen.getByText("code").tagName).toBe("CODE");
    expect(screen.getByText("gone").tagName).toBe("DEL");
    expect(screen.getByText("nested").closest("ul")?.parentElement?.tagName).toBe("LI");
    expect(screen.getByText(/Done:/)).toBeInTheDocument();
    expect(screen.getByText("first").closest("ol")).toBeInTheDocument();
    expect(screen.getByText("quoted").closest("blockquote")).toBeInTheDocument();
    expect(screen.getByLabelText("Code (ts)")).toHaveTextContent("const a = 1;");
    expect(within(screen.getByRole("table")).getAllByRole("columnheader")).toHaveLength(2);
    expect(screen.getByRole("separator")).toBeInTheDocument();
  });

  it("never renders raw HTML or unsafe links", () => {
    const { container } = render(
      <Markdown source={'<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1)) [y](java\tscript:1) ![p](data:image/png;base64,AA) [ok](https://example.com)'} />,
    );
    expect(container.querySelector("script, img")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "https://example.com");
    expect(links[0]).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("safeHref allows http(s), mailto and relative only", () => {
    expect(safeHref("https://a.b")).toBe("https://a.b");
    expect(safeHref("mailto:a@b.c")).toBe("mailto:a@b.c");
    expect(safeHref("/ui/work")).toBe("/ui/work");
    expect(safeHref(" JavaScript:alert(1)")).toBeNull();
    expect(safeHref("vbscript:x")).toBeNull();
    expect(safeHref("data:text/html,x")).toBeNull();
  });

  it("wikilinks use the renderer", () => {
    render(<Markdown source="See [[Projects/Alpha|Alpha]]." renderWikilink={(t, l) => <a href={`/v?path=${t}`}>{l}</a>} />);
    expect(screen.getByRole("link", { name: "Alpha" })).toHaveAttribute("href", "/v?path=Projects/Alpha");
  });
});

describe("GraphCanvas", () => {
  const nodes = [
    { id: "a", label: "Spec" },
    { id: "b", label: "Foundation", description: "doing" },
    { id: "c", label: "Work area" },
  ];
  const edges = [
    { id: "e1", source: "a", target: "b", label: "blocks" },
    { id: "e2", source: "b", target: "c", label: "blocks" },
  ];

  it("layers nodes from the edges and survives cycles", () => {
    const pos = layeredLayout(nodes, edges);
    expect(pos.get("a")!.x).toBeLessThan(pos.get("b")!.x);
    expect(pos.get("b")!.x).toBeLessThan(pos.get("c")!.x);
    const cyclic = layeredLayout(nodes, [...edges, { id: "e3", source: "c", target: "a", label: "blocks" }]);
    expect(cyclic.size).toBe(3);
  });

  it("table view lists nodes with their labelled links; selecting calls back", () => {
    const onNodeSelect = vi.fn();
    render(<GraphCanvas label="Dependencies" nodes={nodes} edges={edges} defaultView="table" onNodeSelect={onNodeSelect} legend={[{ tone: "ok", label: "Done" }]} />);
    expect(screen.getByRole("button", { name: "Table" })).toHaveAttribute("aria-pressed", "true");
    const row = screen.getByRole("rowheader", { name: "Spec" }).closest("tr")!;
    expect(row).toHaveTextContent("blocks Foundation");
    fireEvent.click(screen.getByRole("button", { name: "Foundation" }));
    expect(onNodeSelect).toHaveBeenCalledWith("b");
    expect(screen.getByRole("list", { name: "Dependencies: legend" })).toHaveTextContent("Done");
  });
});

describe("GraphCanvas graph view keyboard (finding 9)", () => {
  it("Enter on a focused node calls onNodeSelect", async () => {
    class RO {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", RO);
    const onNodeSelect = vi.fn();
    render(
      <GraphCanvas
        label="Dependencies"
        nodes={[{ id: "a", label: "Spec" }, { id: "b", label: "Foundation" }]}
        edges={[{ id: "e", source: "a", target: "b", label: "blocks" }]}
        onNodeSelect={onNodeSelect}
      />,
    );
    const node = await screen.findByLabelText("Foundation");
    node.focus();
    fireEvent.keyDown(node, { key: "Enter" });
    expect(onNodeSelect).toHaveBeenCalledWith("b");
    // Enter again on the selected node reopens it, exactly once per press.
    fireEvent.keyDown(node, { key: "Enter" });
    expect(onNodeSelect).toHaveBeenCalledTimes(2);
    vi.unstubAllGlobals();
  });
});

describe("DataTable extensions", () => {
  type Row = { id: string; title: string };
  const rows: Row[] = [
    { id: "1", title: "Alpha" },
    { id: "2", title: "Beta" },
  ];
  const columns = [
    { id: "id", header: "ID", accessor: "id" as const },
    { id: "title", header: "Title", accessor: "title" as const, sortable: true, rowLink: true },
  ];

  it("sortable headers report aria-sort and toggle direction", () => {
    const onSortChange = vi.fn();
    render(<DataTable caption="Tasks" columns={columns} data={rows} sort={{ columnId: "title", direction: "ascending" }} onSortChange={onSortChange} />);
    expect(screen.getByRole("columnheader", { name: /Title/ })).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(screen.getByRole("button", { name: /Title/ }));
    expect(onSortChange).toHaveBeenCalledWith({ columnId: "title", direction: "descending" });
    expect(screen.getByRole("columnheader", { name: "ID" })).not.toHaveAttribute("aria-sort");
  });

  it("row links are real anchors in the link column", () => {
    render(<DataTable caption="Tasks" columns={columns} data={rows} getRowHref={(r) => `/work/tasks/${r.id}`} />);
    expect(screen.getByRole("link", { name: "Alpha" })).toHaveAttribute("href", "/work/tasks/1");
  });

  it("loading rows announce loading, never a count", () => {
    render(<DataTable caption="Tasks" columns={columns} data={[]} isLoading />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading…");
    expect(screen.queryByText("No data available")).not.toBeInTheDocument();
  });

  it("selection: per-row and select-all checkboxes", () => {
    const onChange = vi.fn();
    render(<DataTable caption="Tasks" columns={columns} data={rows} getRowKey={(r) => r.id} selection={{ selected: new Set(["1"]), onChange, rowLabel: (r) => r.title }} />);
    expect(screen.getByRole("checkbox", { name: "Select Alpha" })).toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Beta" }));
    expect(onChange).toHaveBeenLastCalledWith(new Set(["1", "2"]));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all rows" }));
    expect(onChange).toHaveBeenLastCalledWith(new Set(["1", "2"]));
  });

  it("with selection and onRowClick, a checkbox click does not activate the row (finding 8)", () => {
    const onRowClick = vi.fn();
    const onChange = vi.fn();
    render(
      <DataTable
        caption="Tasks"
        columns={[{ id: "t", header: "Title", accessor: "title" as const, cell: (r: Row) => <label>{r.title} <input type="checkbox" aria-label={`Flag ${r.title}`} /></label> }]}
        data={rows}
        getRowKey={(r) => r.id}
        onRowClick={onRowClick}
        selection={{ selected: new Set(), onChange, rowLabel: (r) => r.title }}
      />,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha" }));
    expect(onChange).toHaveBeenCalled();
    fireEvent.click(screen.getByText("Beta"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Flag Alpha" }));
    expect(onRowClick).toHaveBeenCalledTimes(0);
    fireEvent.click(screen.getAllByRole("row")[1].querySelector("td:last-child")!);
    expect(onRowClick).toHaveBeenCalledWith(rows[0]);
  });

  it("sticky header and max height are opt in", () => {
    const { container } = render(<DataTable caption="Tasks" columns={columns} data={rows} stickyHeader maxHeight="40vh" />);
    expect(container.querySelector("thead")?.className).toContain("sticky");
    expect((container.firstElementChild as HTMLElement).style.maxHeight).toBe("40vh");
  });
});
