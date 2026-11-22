import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Card } from "../../src/lib/ui/Card";
import { expectNoAxeViolations } from "./a11y-helper";

describe("Card primitive", () => {
  it("TC-CRD-01: renders default card container with surface tokens", () => {
    const { container } = render(<Card>Card Body</Card>);
    const cardEl = container.firstElementChild as HTMLElement;
    expect(cardEl.tagName).toBe("DIV");
    expect(cardEl.className).toContain("bg-[var(--ua-surface)]");
    expect(cardEl.className).toContain("border-[var(--ua-border)]");
  });

  it("TC-CRD-02: renders semantic HTML tags via as prop", () => {
    const { container: c1 } = render(<Card as="section">Section</Card>);
    expect(c1.firstElementChild?.tagName).toBe("SECTION");

    const { container: c2 } = render(<Card as="article">Article</Card>);
    expect(c2.firstElementChild?.tagName).toBe("ARTICLE");

    const { container: c3 } = render(<ul><Card as="li">Item</Card></ul>);
    expect(screen.getByRole("listitem")).toBeInTheDocument();
  });

  it("TC-CRD-03: renders sunken variant with bg token", () => {
    const { container } = render(<Card variant="sunken">Sunken</Card>);
    expect((container.firstElementChild as HTMLElement).className).toContain("bg-[var(--ua-bg)]");
  });

  it("TC-CRD-04 & 05: renders compound elements with accessible heading", () => {
    render(
      <Card as="section" aria-labelledby="task-title">
        <Card.Header>
          <Card.Title id="task-title">Task #949</Card.Title>
          <Card.Description>Control Center Information Architecture</Card.Description>
        </Card.Header>
        <Card.Body>Task body details</Card.Body>
        <Card.Footer>Actions</Card.Footer>
      </Card>
    );

    const heading = screen.getByRole("heading", { name: "Task #949" });
    expect(heading).toBeInTheDocument();
    expect(screen.getByText("Control Center Information Architecture")).toBeInTheDocument();
    expect(screen.getByText("Task body details")).toBeInTheDocument();
    expect(screen.getByText("Actions")).toBeInTheDocument();
  });

  it("TC-CRD-06, 07, 08: interactive card provides button role, tabindex, and keyboard activation", () => {
    const handleClick = vi.fn();
    render(
      <Card onClick={handleClick}>
        <Card.Title>Clickable Card</Card.Title>
      </Card>
    );

    const card = screen.getByRole("button", { name: /clickable card/i });
    expect(card).toHaveAttribute("tabindex", "0");

    fireEvent.click(card);
    expect(handleClick).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(card, { key: "Enter" });
    expect(handleClick).toHaveBeenCalledTimes(2);

    fireEvent.keyDown(card, { key: " " });
    expect(handleClick).toHaveBeenCalledTimes(3);
  });

  it("TC-CRD-09: interactive card has 3px focus ring classes", () => {
    render(<Card onClick={() => {}}>Interactive</Card>);
    const card = screen.getByRole("button");
    expect(card.className).toContain("focus-visible:outline-3");
    expect(card.className).toContain("focus-visible:outline-[var(--ua-accent)]");
  });

  it("TC-CRD-10: passes axe-core validation", async () => {
    const { container } = render(
      <div>
        <Card as="article">
          <Card.Header>
            <Card.Title>Static Card</Card.Title>
          </Card.Header>
          <Card.Body>Information content</Card.Body>
        </Card>
        <Card onClick={() => {}}>
          <Card.Title>Interactive Item</Card.Title>
        </Card>
      </div>
    );
    await expectNoAxeViolations(container);
  });

  it("TC-CRD-11: ignores Space and Enter keydown events bubbling from descendant controls", () => {
    const handleCardClick = vi.fn();
    const handleNestedBtnClick = vi.fn();

    render(
      <Card interactive data-testid="card-root" onClick={handleCardClick}>
        <Card.Body>
          <input data-testid="nested-input" type="text" />
          <button data-testid="nested-button" onClick={handleNestedBtnClick}>
            Child Action
          </button>
        </Card.Body>
      </Card>
    );

    const input = screen.getByTestId("nested-input");
    const nestedBtn = screen.getByTestId("nested-button");

    // Press Space and Enter inside input
    fireEvent.keyDown(input, { key: " " });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(handleCardClick).not.toHaveBeenCalled();

    // Press Space and Enter on nested button
    fireEvent.keyDown(nestedBtn, { key: " " });
    fireEvent.keyDown(nestedBtn, { key: "Enter" });
    expect(handleCardClick).not.toHaveBeenCalled();

    // Pressing Enter directly on card container DOES fire onClick
    const card = screen.getByTestId("card-root");
    fireEvent.keyDown(card, { key: "Enter" });
    expect(handleCardClick).toHaveBeenCalledTimes(1);
  });
});
