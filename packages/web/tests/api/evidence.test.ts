import { describe, expect, it } from "vitest";
import { buildEvidenceQuery } from "../../src/lib/api/evidence";

describe("buildEvidenceQuery", () => {
  it("skips blank fields and trims values", () => {
    expect(buildEvidenceQuery({})).toBe("");
    expect(buildEvidenceQuery({ task_id: "  ", agent: "", item: " " })).toBe("");
    expect(buildEvidenceQuery({ item: " nd-0000000000000000 " })).toBe("?item=nd-0000000000000000");
    expect(buildEvidenceQuery({ task_id: "899", pr: " sha123 " })).toBe(
      "?task_id=899&pr=sha123",
    );
  });
});
