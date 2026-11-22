import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { StatusIndicator } from "../../src/lib/ui/StatusIndicator";
import { expectNoAxeViolations } from "./a11y-helper";

describe("StatusIndicator primitive", () => {
  it("TC-STS-01: maps online/running to ok token and solid dot glyph", () => {
    const { container } = render(<StatusIndicator status="running" />);
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
    const dot = container.querySelector("[aria-hidden='true']");
    expect(dot?.className).toContain("bg-[var(--ua-ok)]");
    expect(dot?.textContent).toBe("●");
  });

  it("TC-STS-02: maps in-progress states to the live token and half-circle glyph", () => {
    const { container } = render(<StatusIndicator status="doing" />);
    expect(screen.getByText("DOING")).toBeInTheDocument();
    const dot = container.querySelector("[aria-hidden='true']");
    expect(dot?.className).toContain("bg-[var(--ua-live)]");
    expect(dot?.textContent).toBe("◐");
  });

  it("TC-STS-02b: waiting for input is a warn state, not live work (design protocol §3.1)", () => {
    const { container } = render(<StatusIndicator status="waiting" />);
    expect(screen.getByText("WAITING")).toBeInTheDocument();
    const dot = container.querySelector("[aria-hidden='true']");
    expect(dot?.className).toContain("bg-[var(--ua-warn)]");
    expect(dot?.className).not.toContain("ua-live");
  });

  it("TC-STS-03: maps failure states to the danger token and triangle glyph", () => {
    const { container } = render(<StatusIndicator status="error" />);
    expect(screen.getByText("ERROR")).toBeInTheDocument();
    const dot = container.querySelector("[aria-hidden='true']");
    expect(dot?.className).toContain("bg-[var(--ua-danger)]");
    expect(dot?.textContent).toBe("▲");

    for (const status of ["blocked", "failed", "denied", "misconfigured"]) {
      const { container: c } = render(<StatusIndicator status={status} />);
      expect(
        c.querySelector("[aria-hidden='true']")?.className,
        `Failed for status: ${status}`,
      ).toContain("bg-[var(--ua-danger)]");
    }
  });

  it("TC-STS-03b: keeps advisory states on the warn token", () => {
    for (const status of ["warning", "warn", "pending"]) {
      const { container: c } = render(<StatusIndicator status={status} />);
      expect(
        c.querySelector("[aria-hidden='true']")?.className,
        `Failed for status: ${status}`,
      ).toContain("bg-[var(--ua-warn)]");
    }
  });

  it("TC-STS-04 & 05: maps idle to muted token with hollow circle and done to ok token with check glyph", () => {
    const { container: c1 } = render(<StatusIndicator status="idle" />);
    expect(screen.getByText("IDLE")).toBeInTheDocument();
    expect(c1.querySelector("[aria-hidden='true']")?.textContent).toBe("○");
    expect(c1.querySelector("[aria-hidden='true']")?.className).toContain("bg-[var(--ua-muted)]");

    const { container: c2 } = render(<StatusIndicator status="done" />);
    expect(screen.getByText("DONE")).toBeInTheDocument();
    expect(c2.querySelector("[aria-hidden='true']")?.textContent).toBe("✓");
    expect(c2.querySelector("[aria-hidden='true']")?.className).toContain("bg-[var(--ua-ok)]");
  });

  it("TC-STS-06: respects custom label override", () => {
    render(<StatusIndicator status="online" label="Desktop_Agent1 (online)" />);
    expect(screen.getByText("Desktop_Agent1 (online)")).toBeInTheDocument();
  });

  it("TC-STS-07: complies with WCAG 1.4.1 (not relying on color alone)", () => {
    const { container } = render(<StatusIndicator status="error" />);
    // Dot is hidden from assistive tech
    const dot = container.querySelector("[aria-hidden='true']");
    expect(dot).toBeInTheDocument();
    // Screen reader receives textual equivalent
    expect(screen.getByText("ERROR")).toBeInTheDocument();
  });

  it("TC-STS-08: respects reduced motion preferences when pulsing", () => {
    const { container } = render(<StatusIndicator status="running" pulse />);
    const dot = container.querySelector("[aria-hidden='true']");
    expect(dot?.className).toContain("animate-pulse");
    expect(dot?.className).toContain("motion-reduce:animate-none");
  });

  it("TC-STS-09: passes axe-core validation across all statuses", async () => {
    const { container } = render(
      <div>
        <StatusIndicator status="online" />
        <StatusIndicator status="waiting" />
        <StatusIndicator status="error" />
        <StatusIndicator status="idle" />
        <StatusIndicator status="done" />
        <StatusIndicator status="LIVE" />
        <StatusIndicator status="MISCONFIGURED" />
      </div>
    );
    await expectNoAxeViolations(container);
  });

  it("TC-STS-10: handles null, undefined, and empty status strings without crashing", () => {
    expect(() => {
      render(<StatusIndicator status={null as any} />);
    }).not.toThrow();

    expect(() => {
      render(<StatusIndicator status={undefined as any} />);
    }).not.toThrow();

    const { container } = render(<StatusIndicator status="" />);
    const dot = container.querySelector("[aria-hidden='true']");
    expect(dot?.className).toContain("bg-[var(--ua-muted)]");
  });

  it("TC-STS-11: maps done, approved, completed, and ok to success green token (var(--ua-ok))", () => {
    const successStatuses = ["done", "approved", "completed", "ok"];

    for (const s of successStatuses) {
      const { container } = render(<StatusIndicator status={s} />);
      const dot = container.querySelector("[aria-hidden='true']");
      expect(dot?.className, `Failed for status: ${s}`).toContain("bg-[var(--ua-ok)]");
    }
  });
});
