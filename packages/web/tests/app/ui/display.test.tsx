import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  CodeBlock,
  DescriptionList,
  EmptyState,
  ErrorState,
  FilterBar,
  IconButton,
  LinkButton,
  LinkProvider,
  LinkTabs,
  PageHeader,
  Skeleton,
  Tabs,
  ToastProvider,
  Toolbar,
  useToast,
  type UiLinkProps,
} from "../../../src/lib/ui";

describe("links", () => {
  it("LinkButton is a real anchor and uses the provided router link", () => {
    const Custom = ({ href, reloadDocument: _r, ...rest }: UiLinkProps) => <a data-custom="yes" href={`/ui${href}`} {...rest} />;
    render(
      <LinkProvider component={Custom}>
        <LinkButton href="/work">Work</LinkButton>
      </LinkProvider>,
    );
    const link = screen.getByRole("link", { name: "Work" });
    expect(link).toHaveAttribute("href", "/ui/work");
    expect(link).toHaveAttribute("data-custom", "yes");
  });

  it("IconButton needs its label and shows it as the tooltip", () => {
    const onClick = vi.fn();
    render(<IconButton aria-label="Refresh" icon="↻" onClick={onClick} />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(onClick).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveAttribute("title", "Refresh");
  });
});

describe("Tabs", () => {
  it("LinkTabs are a nav of links with aria-current", () => {
    render(<LinkTabs label="Work sections" items={[{ href: "/work", label: "Tasks", current: true }, { href: "/work/goals", label: "Goals", count: "–" }]} />);
    const nav = screen.getByRole("navigation", { name: "Work sections" });
    expect(nav).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Tasks" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  });

  it("in-page Tabs move with arrows and show one panel", () => {
    const onChange = vi.fn();
    render(<Tabs label="Detail" onChange={onChange} items={[{ id: "a", label: "Activity", panel: "A panel" }, { id: "b", label: "Comments", panel: "B panel" }]} />);
    const first = screen.getByRole("tab", { name: "Activity" });
    expect(first).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("A panel");
    fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("b");
    expect(screen.getByRole("tab", { name: "Comments" })).toHaveFocus();
    expect(screen.getByRole("tabpanel")).toHaveTextContent("B panel");
    expect(screen.getByRole("tab", { name: "Activity" })).toHaveAttribute("tabindex", "-1");
  });
});

describe("page structure", () => {
  it("PageHeader renders one focusable h1, description and actions", () => {
    render(<PageHeader title="Work" description="Tasks." actions={<button>New</button>} />);
    const h1 = screen.getByRole("heading", { level: 1, name: "Work" });
    expect(h1).toHaveAttribute("tabindex", "-1");
    expect(screen.getByText("Tasks.")).toBeInTheDocument();
  });

  it("Toolbar is a labelled group; FilterBar binds values and removes chips", () => {
    const onStatus = vi.fn();
    const onSearch = vi.fn();
    const onClear = vi.fn();
    render(
      <>
        <Toolbar label="Controls">
          <button>One</button>
        </Toolbar>
        <FilterBar
          label="Task filters"
          search={{ label: "Search tasks", value: "auth", onChange: onSearch }}
          filters={[{ id: "status", label: "Status", value: "open", onChange: onStatus, options: [{ value: "open", label: "Open" }] }]}
          onClearAll={onClear}
        />
      </>,
    );
    expect(screen.getByRole("group", { name: "Controls" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search tasks" }), { target: { value: "x" } });
    expect(onSearch).toHaveBeenCalledWith("x");
    fireEvent.click(screen.getByRole("button", { name: "Remove filter Status: Open" }));
    expect(onStatus).toHaveBeenCalledWith("");
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
    expect(onClear).toHaveBeenCalled();
  });
});

describe("data states", () => {
  it("Skeleton announces loading and never shows a number", () => {
    render(<Skeleton label="Loading tasks" />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Loading tasks…");
    expect(status.textContent).not.toMatch(/\d/);
  });

  it("EmptyState and ErrorState: plain message, Retry, collapsed detail", () => {
    const onRetry = vi.fn();
    render(
      <>
        <EmptyState title="No tasks" description="Clear a filter." action={<button>Clear</button>} />
        <ErrorState message="The hub could not be reached." detail="tasks HTTP 503" onRetry={onRetry} />
      </>,
    );
    expect(screen.getByRole("heading", { name: "No tasks" })).toBeInTheDocument();
    expect(screen.getByText("The hub could not be reached.")).toBeInTheDocument();
    expect(screen.getByText("Technical detail").closest("details")).not.toHaveAttribute("open");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();
  });
});

describe("Toast", () => {
  function Trigger() {
    const toast = useToast();
    return (
      <>
        <button onClick={() => toast.show({ message: "Saved.", tone: "success", duration: 1000 })}>ok</button>
        <button onClick={() => toast.show({ message: "Failed.", tone: "error", action: { label: "Retry", onAction: () => {} } })}>bad</button>
      </>
    );
  }

  it("announces in a status region; success leaves, errors stay until dismissed", () => {
    vi.useFakeTimers();
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByText("ok"));
    fireEvent.click(screen.getByText("bad"));
    expect(screen.getByRole("status", { name: "Notifications" })).toHaveTextContent("Saved.");
    act(() => vi.advanceTimersByTime(60_000));
    expect(screen.queryByText("Saved.")).not.toBeInTheDocument();
    expect(screen.getByText("Failed.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss notification" }));
    expect(screen.queryByText("Failed.")).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("evicts transient toasts first and keeps an unresolved error with its Retry (finding 12)", () => {
    function Many() {
      const toast = useToast();
      return (
        <>
          <button onClick={() => toast.show({ message: "Broken.", tone: "error", action: { label: "Retry", onAction: () => {} } })}>err</button>
          <button onClick={() => toast.show({ message: `Info ${Math.random()}`, tone: "info", duration: 60_000 })}>info</button>
        </>
      );
    }
    render(
      <ToastProvider>
        <Many />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByText("err"));
    for (let i = 0; i < 6; i++) fireEvent.click(screen.getByText("info"));
    expect(screen.getByText("Broken.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getAllByText(/^Info /).length).toBeLessThanOrEqual(4);
  });

  it("useToast outside a provider is a no-op", () => {
    render(<Trigger />);
    expect(() => fireEvent.click(screen.getByText("ok"))).not.toThrow();
  });
});

describe("content", () => {
  it("DescriptionList shows pairs and 'Not set' for blanks", () => {
    render(<DescriptionList items={[{ term: "Author", value: "op" }, { term: "Project", value: "" }]} />);
    expect(screen.getByText("Author").tagName).toBe("DT");
    expect(screen.getByText("Not set")).toBeInTheDocument();
  });

  it("CodeBlock is a labelled focusable region with Copy", async () => {
    const writeText = vi.fn(async () => {});
    Object.assign(navigator, { clipboard: { writeText } });
    render(<CodeBlock label="Command to run" code="uv run x" />);
    expect(screen.getByLabelText("Command to run", { selector: "pre" })).toHaveAttribute("tabindex", "0");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Copy Command to run" })));
    expect(writeText).toHaveBeenCalledWith("uv run x");
    expect(screen.getByText("Copied")).toBeInTheDocument();
  });
});

describe("Button is a native button (item 4)", () => {
  it("Enter on a submit Button is not cancelled, and activating it submits the form", async () => {
    const { Button } = await import("../../../src/lib/ui");
    const onSubmit = vi.fn((event: Event) => event.preventDefault());
    render(
      <form aria-label="Reason form" onSubmit={(e) => onSubmit(e.nativeEvent)}>
        <Button type="submit">Save</Button>
      </form>,
    );
    const save = screen.getByRole("button", { name: "Save" });
    save.focus();
    expect(fireEvent.keyDown(save, { key: "Enter" })).toBe(true);
    fireEvent.click(save);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("LinkButton and IconButton do not cancel keys either", () => {
    render(
      <>
        <LinkButton href="/work">Work</LinkButton>
        <IconButton aria-label="Refresh" icon="↻" />
      </>,
    );
    expect(fireEvent.keyDown(screen.getByRole("link", { name: "Work" }), { key: "Enter" })).toBe(true);
    expect(fireEvent.keyDown(screen.getByRole("button", { name: "Refresh" }), { key: "Enter" })).toBe(true);
  });
});
