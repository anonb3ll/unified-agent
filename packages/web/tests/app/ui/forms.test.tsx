import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Checkbox, Combobox, Field, Input, RadioGroup, Select, Switch, Textarea } from "../../../src/lib/ui";

const AGENTS = [
  { value: "claude", label: "Desktop_Agent1", description: "Claude Code" },
  { value: "codex", label: "Desktop_Agent2" },
  { value: "grok", label: "cloud_agent8", disabled: true },
];

describe("Field with Input, Textarea and Select", () => {
  it("wires label, hint, error, required and invalid", () => {
    render(
      <Field label="Title" hint="One line." error="Add a title." required>
        <Input />
      </Field>,
    );
    const input = screen.getByRole("textbox", { name: /Title/ });
    expect(input).toBeRequired();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("One line. Add a title.");
  });

  it("supports controlled and uncontrolled controls", () => {
    function Controlled() {
      const [v, setV] = useState("a");
      return (
        <Field label="Name">
          <Input value={v} onChange={(e) => setV(e.target.value.toUpperCase())} />
        </Field>
      );
    }
    render(
      <>
        <Controlled />
        <Field label="Notes">
          <Textarea defaultValue="draft" />
        </Field>
        <Field label="Priority">
          <Select defaultValue="low" placeholder="Any" options={[{ value: "low", label: "Low" }, { value: "high", label: "High" }]} />
        </Field>
      </>,
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "bc" } });
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("BC");
    expect(screen.getByRole("textbox", { name: "Notes" })).toHaveValue("draft");
    expect(screen.getByRole("combobox", { name: "Priority" })).toHaveValue("low");
    expect(screen.getByRole("option", { name: "Any" })).toBeInTheDocument();
  });
});

describe("Checkbox, Switch and RadioGroup", () => {
  it("are labelled native controls", () => {
    const onRadio = vi.fn();
    render(
      <>
        <Checkbox label="Only mine" description="Hide others." />
        <Switch label="Live updates" defaultChecked />
        <RadioGroup legend="Where" options={[{ value: "here", label: "Here" }, { value: "cloud", label: "Cloud" }]} defaultValue="here" onChange={onRadio} />
      </>,
    );
    const box = screen.getByRole("checkbox", { name: "Only mine" });
    expect(box).toHaveAccessibleDescription("Hide others.");
    fireEvent.click(box);
    expect(box).toBeChecked();
    expect(screen.getByRole("switch", { name: "Live updates" })).toBeChecked();
    expect(screen.getByRole("group", { name: "Where" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Cloud" }));
    expect(onRadio).toHaveBeenCalledWith("cloud");
  });
});

describe("Combobox", () => {
  function setup(onChange = vi.fn()) {
    render(
      <Field label="Owner">
        <Combobox options={AGENTS} onChange={onChange} name="owner" />
      </Field>,
    );
    return { input: screen.getByRole("combobox", { name: "Owner" }), onChange };
  }

  it("follows the ARIA 1.2 pattern: filter, arrows, Enter", () => {
    const { input, onChange } = setup();
    expect(input).toHaveAttribute("aria-expanded", "false");
    fireEvent.change(input, { target: { value: "codex" } });
    expect(input).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("codex");
    expect(input).toHaveValue("Desktop_Agent2");
    expect(document.querySelector('input[type=hidden][name="owner"]')).toHaveValue("codex");
  });

  it("moves the active option with the keyboard and skips nothing silently", () => {
    const { input, onChange } = setup();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const first = input.getAttribute("aria-activedescendant");
    expect(first).toBeTruthy();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input.getAttribute("aria-activedescendant")).not.toBe(first);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("codex");
  });

  it("scrolls the active option into view on arrows and filtering (finding 13)", () => {
    const scroll = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    const { input } = setup();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const active = input.getAttribute("aria-activedescendant");
    expect(scroll.mock.contexts.some((el) => (el as Element).id === active)).toBe(true);
    scroll.mockClear();
    fireEvent.change(input, { target: { value: "desk" } });
    expect(scroll).toHaveBeenCalled();
    Element.prototype.scrollIntoView = original;
  });

  it("Escape closes, then clears; disabled options cannot be picked; no match says so", () => {
    const { input, onChange } = setup();
    fireEvent.change(input, { target: { value: "grok" } });
    fireEvent.mouseDown(screen.getByRole("option", { name: /cloud_agent8/ }));
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "zzz" } });
    expect(screen.getByText("No matches")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "claude" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onChange).toHaveBeenLastCalledWith(null);
  });
});
