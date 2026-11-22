import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Badge, Chip } from "../../src/lib/ui/Badge";
import { expectNoAxeViolations } from "./a11y-helper";

describe("Badge & Chip primitives", () => {
  it("TC-BDG-01: renders neutral badge with muted tokens", () => {
    render(<Badge variant="neutral">Draft</Badge>);
    const badge = screen.getByText("Draft");
    expect(badge.className).toContain("text-[var(--ua-muted)]");
    expect(badge.className).toContain("border-[var(--ua-border)]");
  });

  it("TC-BDG-02: renders success badge with ok tokens", () => {
    render(<Badge variant="success">Active</Badge>);
    const badge = screen.getByText("Active");
    expect(badge.className).toContain("text-[var(--ua-ok)]");
    expect(badge.className).toContain("border-[var(--ua-ok)]");
  });

  it("TC-BDG-03 & 04: renders warning badge with warn tokens and danger badge with danger tokens", () => {
    const { rerender } = render(<Badge variant="warning">Attention</Badge>);
    expect(screen.getByText("Attention").className).toContain("text-[var(--ua-warn)]");

    rerender(<Badge variant="danger">Critical Error</Badge>);
    expect(screen.getByText("Critical Error").className).toContain("text-[var(--ua-danger)]");
    expect(screen.getByText("Critical Error").className).toContain("border-[var(--ua-danger)]");
  });

  it("TC-CHP-01 & 02: renders Chip with correct aria-pressed and active styles", () => {
    const { rerender } = render(<Chip pressed={false}>Scope</Chip>);
    const chip = screen.getByRole("button", { name: /scope/i });
    expect(chip).toHaveAttribute("aria-pressed", "false");
    expect(chip.className).toContain("bg-[var(--ua-surface)]");

    rerender(<Chip pressed={true}>Scope</Chip>);
    expect(chip).toHaveAttribute("aria-pressed", "true");
    expect(chip.className).toContain("bg-[var(--ua-accent)]");
    expect(chip.className).toContain("text-[var(--ua-accent-fg)]");
  });

  it("TC-CHP-03: renders tabular count within chip", () => {
    render(<Chip count={12}>Approvals</Chip>);
    const chip = screen.getByRole("button", { name: /approvals/i });
    expect(chip).toHaveTextContent("12");
  });

  it("TC-CHP-04: fires onClick when clicked or triggered with keyboard", () => {
    const handleClick = vi.fn();
    render(<Chip onClick={handleClick}>Toggle</Chip>);
    const chip = screen.getByRole("button", { name: /toggle/i });

    fireEvent.click(chip);
    expect(handleClick).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(chip, { key: "Enter" });
    expect(handleClick).toHaveBeenCalledTimes(2);

    fireEvent.keyDown(chip, { key: " " });
    expect(handleClick).toHaveBeenCalledTimes(3);
  });

  it("TC-CHP-05: has 3px focus ring on focus-visible", () => {
    render(<Chip>Focusable Chip</Chip>);
    const chip = screen.getByRole("button", { name: /focusable chip/i });
    expect(chip.className).toContain("focus-visible:outline-3");
    expect(chip.className).toContain("focus-visible:outline-[var(--ua-accent)]");
  });

  it("TC-BDG-06: passes axe-core accessibility validation", async () => {
    const { container } = render(
      <div>
        <Badge variant="neutral">Status: Neutral</Badge>
        <Badge variant="info">Status: Info</Badge>
        <Badge variant="success">Status: Success</Badge>
        <Badge variant="warning">Status: Warning</Badge>
        <Chip pressed={false} count={3}>Filter A</Chip>
        <Chip pressed={true} count={7}>Filter B</Chip>
      </div>
    );
    await expectNoAxeViolations(container);
  });

  it("TC-CHP-06: Chip with onRemove renders accessible sibling buttons with zero axe violations", async () => {
    const handleRemove = vi.fn();
    const handleClick = vi.fn();
    const { container } = render(
      <Chip onClick={handleClick} onRemove={handleRemove}>
        Category: Agents
      </Chip>
    );

    const buttons = screen.getAllByRole("button");
    // Main button and remove button are siblings
    expect(buttons).toHaveLength(2);
    expect(buttons[0]).toHaveTextContent("Category: Agents");
    expect(buttons[1]).toHaveAttribute("aria-label", "Remove");

    // Remove button must be naturally reachable
    expect(buttons[1]).not.toHaveAttribute("tabindex", "-1");

    // Clicking remove button does not trigger main button click
    fireEvent.click(buttons[1]);
    expect(handleRemove).toHaveBeenCalledTimes(1);
    expect(handleClick).not.toHaveBeenCalled();

    // Verify keyboard Backspace and Delete on main button trigger onRemove
    fireEvent.keyDown(buttons[0], { key: "Backspace" });
    expect(handleRemove).toHaveBeenCalledTimes(2);

    fireEvent.keyDown(buttons[0], { key: "Delete" });
    expect(handleRemove).toHaveBeenCalledTimes(3);

    // Verify axe-core nested-interactive rule passes
    await expectNoAxeViolations(container);
  });
});
