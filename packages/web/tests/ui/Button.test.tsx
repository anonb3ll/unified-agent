import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Button } from "../../src/lib/ui/Button";
import { expectNoAxeViolations } from "./a11y-helper";

describe("Button primitive", () => {
  it("TC-BTN-01: renders default button as type='button' with secondary styling", () => {
    render(<Button>Click me</Button>);
    const btn = screen.getByRole("button", { name: "Click me" });
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveAttribute("type", "button");
    expect(btn.className).toContain("bg-[var(--ua-surface)]");
    expect(btn.className).toContain("text-[var(--ua-fg)]");
    // Control boundary clears 3:1 (WCAG 1.4.11): the strong border, not the hairline.
    expect(btn.className).toContain("border-[var(--ua-border-strong)]");
  });

  it("TC-BTN-02: renders primary variant with accent background and accent-fg text", () => {
    render(<Button variant="primary">Primary Action</Button>);
    const btn = screen.getByRole("button", { name: "Primary Action" });
    expect(btn.className).toContain("bg-[var(--ua-accent)]");
    expect(btn.className).toContain("text-[var(--ua-accent-fg)]");
  });

  it("TC-BTN-03: renders danger variant with danger tokens", () => {
    render(<Button variant="danger">Deny Tool</Button>);
    const btn = screen.getByRole("button", { name: "Deny Tool" });
    expect(btn.className).toContain("text-[var(--ua-danger)]");
    expect(btn.className).toContain("border-[var(--ua-danger)]");
    expect(btn.className).toContain("hover:bg-[var(--ua-danger-surface)]");
  });

  it("TC-BTN-04: renders ghost variant with transparent background", () => {
    render(<Button variant="ghost">Dismiss</Button>);
    const btn = screen.getByRole("button", { name: "Dismiss" });
    expect(btn.className).toContain("bg-transparent");
  });

  it("TC-BTN-05: applies size classes correctly", () => {
    const { rerender } = render(<Button size="sm">Small</Button>);
    expect(screen.getByRole("button").className).toContain("text-xs");

    rerender(<Button size="md">Medium</Button>);
    expect(screen.getByRole("button").className).toContain("text-sm");

    rerender(<Button size="lg">Large</Button>);
    expect(screen.getByRole("button").className).toContain("text-base");
  });

  it("TC-BTN-06: fires onClick on mouse click", () => {
    const handleClick = vi.fn();
    render(<Button onClick={handleClick}>Action</Button>);
    fireEvent.click(screen.getByRole("button", { name: "Action" }));
    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  it("TC-BTN-07 & 08: leaves Enter and Space to the native button (they become a click)", () => {
    const handleClick = vi.fn();
    render(<Button onClick={handleClick}>Key Action</Button>);
    const btn = screen.getByRole("button", { name: "Key Action" });
    btn.focus();

    // Not cancelled, so the browser activates the button itself (one click event).
    expect(fireEvent.keyDown(btn, { key: "Enter", code: "Enter" })).toBe(true);
    expect(fireEvent.keyDown(btn, { key: " ", code: "Space" })).toBe(true);
    fireEvent.click(btn);
    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  it("TC-BTN-09 & 10: respects disabled state and blocks click / key events", () => {
    const handleClick = vi.fn();
    render(<Button disabled onClick={handleClick}>Disabled Action</Button>);
    const btn = screen.getByRole("button", { name: "Disabled Action" });

    expect(btn).toBeDisabled();
    expect(btn.className).toContain("disabled:opacity-50");

    fireEvent.click(btn);
    expect(handleClick).not.toHaveBeenCalled();

    fireEvent.keyDown(btn, { key: "Enter" });
    expect(handleClick).not.toHaveBeenCalled();
  });

  it("TC-BTN-11: sets aria-busy and disables button when loading", () => {
    render(<Button loading>Processing</Button>);
    const btn = screen.getByRole("button", { name: /processing/i });
    expect(btn).toHaveAttribute("aria-busy", "true");
    expect(btn).toBeDisabled();
  });

  it("TC-BTN-12: contains 3px focus ring classes", () => {
    render(<Button>Focus Ring</Button>);
    const btn = screen.getByRole("button", { name: "Focus Ring" });
    expect(btn.className).toContain("focus-visible:outline-3");
    expect(btn.className).toContain("focus-visible:outline-[var(--ua-accent)]");
  });

  it("TC-BTN-14: passes axe-core accessibility audit with zero violations", async () => {
    const { container } = render(
      <div>
        <Button variant="primary">Submit</Button>
        <Button variant="secondary">Cancel</Button>
        <Button variant="danger">Delete</Button>
        <Button variant="ghost">Options</Button>
        <Button disabled>Inactive</Button>
      </div>
    );
    await expectNoAxeViolations(container);
  });
});
