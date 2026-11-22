import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it } from "vitest";
import { useUrlParams } from "../../../src/app/lib/urlState";

const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={["/work"]}>{children}</MemoryRouter>;

describe("useUrlParams", () => {
  it("two updates before the page re-renders both land (a filter, then a checkbox)", () => {
    const { result } = renderHook(() => ({ params: useUrlParams({ status: "", mine: "" }), location: useLocation() }), { wrapper });
    act(() => {
      result.current.params.update({ status: "doing" });
      result.current.params.update({ mine: "1" });
    });
    expect(result.current.location.search).toContain("status=doing");
    expect(result.current.location.search).toContain("mine=1");
    expect(result.current.params.values).toEqual({ status: "doing", mine: "1" });
  });

  it("a removal right after a set is not undone by the set", () => {
    const { result } = renderHook(() => useUrlParams({ status: "", mine: "" }), { wrapper });
    act(() => {
      result.current.update({ mine: "1" });
      result.current.update({ mine: null });
    });
    expect(result.current.values.mine).toBe("");
  });
});
