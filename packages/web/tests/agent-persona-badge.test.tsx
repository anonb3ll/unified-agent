import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentPersonaBadge } from "../src/lib/ui/AgentPersonaBadge";

describe("AgentPersonaBadge operator presentation", () => {
  it.each(["Desktop_Operator", "Desktop_Person"])("does not infer an operator role from %s", (label) => {
    render(<AgentPersonaBadge label={label} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText("Founder & CEO")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "avatar" })).toHaveTextContent("🤖");
  });

  it("uses the server persona for a configured operator label", () => {
    render(<AgentPersonaBadge label="operator_custom" persona={{
      display_name: "Founder & CEO", avatar: "👑", color: "#eab308",
    }} />);
    expect(screen.getByTitle("Founder & CEO (operator_custom)")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "avatar" })).toHaveTextContent("👑");
  });
});
