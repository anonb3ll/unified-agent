import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EntryBoundary } from "../../src/app/EntryBoundary";

function Boom(): never {
  throw new Error("Failed to fetch dynamically imported module: /ui/assets/App-x.js");
}

describe("EntryBoundary (finding 10)", () => {
  it("a failed app chunk shows Reload, not a blank page", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const reload = vi.fn();
    render(
      <EntryBoundary onReload={reload}>
        <Boom />
      </EntryBoundary>,
    );
    expect(screen.getByRole("heading", { level: 1, name: /could not load/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(reload).toHaveBeenCalled();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText(/Failed to fetch dynamically imported module/)).toBeInTheDocument();
    spy.mockRestore();
  });
});


describe("entry chunk", () => {
  it("main.tsx and what it imports statically never use the src/lib/ui barrel (it carries the graph library)", async () => {
    const { readFileSync } = await import("node:fs");
    const path = await import("node:path");
    const web = path.resolve(__dirname, "../..");
    for (const file of ["src/main.tsx", "src/app/EntryBoundary.tsx", "src/app/lib/entry.ts", "src/app/lib/legacyRedirect.ts", "src/app/lib/returnTo.ts", "src/app/lib/storage.ts"]) {
      const source = readFileSync(path.join(web, file), "utf8");
      expect(source, file).not.toMatch(/from "(\.\.\/)*(\.\/)?(lib\/)?ui"/);
    }
  });
});
