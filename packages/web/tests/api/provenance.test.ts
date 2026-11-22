import { describe, expect, it } from "vitest";
import { buildProvenanceGraphQuery } from "../../src/lib/api/provenance";

describe("buildProvenanceGraphQuery", () => {
  it("skips blank fields and trims values", () => {
    expect(buildProvenanceGraphQuery({})).toBe("");
    expect(buildProvenanceGraphQuery({ human: "  ", task_id: "" })).toBe("");
    expect(buildProvenanceGraphQuery({ query: "by_human", human: " Desktop_Operator " })).toBe(
      "?query=by_human&human=Desktop_Operator",
    );
    expect(buildProvenanceGraphQuery({ query: "by_release", task_ids: " 1180,1181,1182 " })).toBe(
      "?query=by_release&task_ids=1180%2C1181%2C1182",
    );
  });
});
