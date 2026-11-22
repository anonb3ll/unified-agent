import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Button, ConfirmDialog, Dialog, Drawer, Menu } from "../../../src/lib/ui";

function DialogHarness({ drawer = false }: { drawer?: boolean }) {
  const [open, setOpen] = useState(false);
  const Component = drawer ? Drawer : Dialog;
  return (
    <>
      <Button onClick={() => setOpen(true)}>Open</Button>
      <Component open={open} onClose={() => setOpen(false)} title="Rename plan" description="Give it a short name.">
        <input aria-label="Plan name" />
      </Component>
    </>
  );
}

describe("Dialog and Drawer", () => {
  it.each([false, true])("open, label, focus first field, Escape closes and returns focus (drawer=%s)", (drawer) => {
    render(<DialogHarness drawer={drawer} />);
    const opener = screen.getByRole("button", { name: "Open" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = screen.getByRole("dialog", { name: "Rename plan" });
    expect(dialog).toHaveAccessibleDescription("Give it a short name.");
    expect(screen.getByRole("textbox", { name: "Plan name" })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: "Plan name" })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("the close button closes", () => {
    render(<DialogHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    fireEvent.click(screen.getByRole("button", { name: "Close dialog" }));
    expect(screen.queryByRole("textbox", { name: "Plan name" })).not.toBeInTheDocument();
  });
});

describe("ConfirmDialog", () => {
  it("names object and consequence; danger focuses Cancel; confirm and cancel call back", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open
        title="Delete this note?"
        objectName="project:alpha"
        consequence="Agents stop finding it."
        confirmLabel="Delete note"
        variant="danger"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByText("project:alpha")).toBeInTheDocument();
    expect(screen.getByText("Agents stop finding it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Delete note" }));
    expect(onConfirm).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("busy blocks a second confirm", () => {
    render(<ConfirmDialog open busy title="Delete?" consequence="Gone." confirmLabel="Delete" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Delete/ })).toBeDisabled();
  });
});

describe("Menu", () => {
  function setup() {
    const onTask = vi.fn();
    const onAgent = vi.fn();
    render(
      <Menu
        label="New"
        items={[
          { id: "task", label: "Task", onSelect: onTask },
          { id: "plan", label: "Plan", disabled: true },
          { kind: "separator", id: "s" },
          { id: "agent", label: "Agent", onSelect: onAgent },
          { id: "classic", label: "Back to classic", href: "/ui/", reloadDocument: true },
        ]}
      />,
    );
    return { trigger: screen.getByRole("button", { name: "New" }), onTask, onAgent };
  }

  it("opens on ArrowDown at the first item, skips disabled items, Escape returns focus", () => {
    const { trigger } = setup();
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menuitem", { name: "Task" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(screen.getByRole("menuitem", { name: "Agent" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("type-ahead, End and selection", () => {
    const { trigger, onAgent } = setup();
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "b" });
    expect(screen.getByRole("menuitem", { name: "Back to classic" })).toHaveFocus();
    expect(screen.getByRole("menuitem", { name: "Back to classic" })).toHaveAttribute("href", "/ui/");
    fireEvent.keyDown(screen.getByRole("menu"), { key: "a" });
    fireEvent.click(screen.getByRole("menuitem", { name: "Agent" }));
    expect(onAgent).toHaveBeenCalledOnce();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("checked items are menuitemradio", () => {
    render(<Menu label="Theme" items={[{ id: "l", label: "Light", checked: true, onSelect: vi.fn() }, { id: "d", label: "Dark", checked: false, onSelect: vi.fn() }]} />);
    fireEvent.click(screen.getByRole("button", { name: "Theme" }));
    expect(screen.getByRole("menuitemradio", { name: "Light" })).toHaveAttribute("aria-checked", "true");
  });
});

describe("ConfirmDialog with form controls (item 5)", () => {
  function Harness({ onConfirm }: { onConfirm: (reason: string) => void }) {
    const [reason, setReason] = useState("");
    return (
      <ConfirmDialog
        open
        title="Deny this approval?"
        objectName="task_delete #42"
        consequence="The agent is told it was denied."
        confirmLabel="Deny"
        variant="danger"
        confirmDisabled={reason.trim() === ""}
        onConfirm={() => onConfirm(reason)}
        onCancel={() => {}}
      >
        <label>
          Reason
          <input value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
      </ConfirmDialog>
    );
  }

  it("renders the fields, focuses the first, and the caller can disable confirm", () => {
    const onConfirm = vi.fn();
    render(<Harness onConfirm={onConfirm} />);
    const field = screen.getByRole("textbox", { name: "Reason" });
    expect(field).toHaveFocus();
    expect(screen.getByRole("button", { name: "Deny" })).toBeDisabled();
    fireEvent.submit(field.closest("form")!);
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: "out of scope" } });
    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Deny" })).toHaveAttribute("type", "submit");
    fireEvent.submit(field.closest("form")!);
    expect(onConfirm).toHaveBeenCalledWith("out of scope");
  });

  it("danger: Enter in a field does not confirm unless the caller opts in (finding 8)", () => {
    const { unmount } = render(<Harness onConfirm={vi.fn()} />);
    const field = screen.getByRole("textbox", { name: "Reason" });
    fireEvent.change(field, { target: { value: "x" } });
    // Cancelled keydown: the browser's implicit submission does not happen.
    expect(fireEvent.keyDown(field, { key: "Enter" })).toBe(false);
    unmount();
    render(
      <ConfirmDialog open title="Rename?" consequence="c" confirmLabel="Rename" variant="danger" submitOnEnter onConfirm={vi.fn()} onCancel={vi.fn()}>
        <label>
          Name
          <input defaultValue="a" />
        </label>
      </ConfirmDialog>,
    );
    expect(fireEvent.keyDown(screen.getByRole("textbox", { name: "Name" }), { key: "Enter" })).toBe(true);
  });

  it("default variant: Enter in a field confirms (finding 8)", () => {
    render(
      <ConfirmDialog open title="Rename?" consequence="c" confirmLabel="Rename" onConfirm={vi.fn()} onCancel={vi.fn()}>
        <label>
          Name
          <input defaultValue="a" />
        </label>
      </ConfirmDialog>,
    );
    expect(fireEvent.keyDown(screen.getByRole("textbox", { name: "Name" }), { key: "Enter" })).toBe(true);
  });

  it("the dialog form is noValidate, so the form's own errors are shown, and fields keep required (slices)", () => {
    render(
      <ConfirmDialog open title="Deny?" consequence="c" confirmLabel="Deny" onConfirm={vi.fn()} onCancel={vi.fn()}>
        <label>
          Reason
          <input required />
        </label>
      </ConfirmDialog>,
    );
    const field = screen.getByRole("textbox", { name: "Reason" });
    expect((field.closest("form") as HTMLFormElement).noValidate).toBe(true);
    expect(field).toBeRequired();
  });

  it("without children it keeps working as before", () => {
    const onConfirm = vi.fn();
    render(<ConfirmDialog open title="Delete?" consequence="Gone." confirmLabel="Delete" onConfirm={onConfirm} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});
