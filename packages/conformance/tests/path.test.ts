import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { PolicyDecision } from "@unified-agent/protocol";

import { PathConformanceFixture } from "../src/index.ts";

const fixturePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "fixtures",
  "path-decisions.json",
);

describe("workspace path conformance fixture", () => {
  it("parses the shared versioned fixture and exact decision shapes", () => {
    const fixture = PathConformanceFixture.parse(
      JSON.parse(readFileSync(fixturePath, "utf8")),
    );

    assert.equal(fixture.schema_version, "ua.conformance.v1");
    assert.equal(fixture.suite, "workspace-path-containment");
    assert.deepEqual(
      fixture.cases.map((item) => item.id),
      ["workspace-file", "parent-escape", "symlink-escape"],
    );
    for (const item of fixture.cases) {
      assert.deepEqual(PolicyDecision.parse(item.expected), item.expected);
    }
    assert.equal(fixture.cases[0]?.expected.allow, true);
    assert.equal(fixture.cases[1]?.expected.allow, false);
    assert.equal(fixture.cases[2]?.expected.allow, false);
  });
});
