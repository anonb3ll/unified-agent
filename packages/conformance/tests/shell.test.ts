import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { ShellConformanceFixture } from "../src/index.ts";

const fixturePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "fixtures",
  "shell-decisions.json",
);

describe("shell run_cmd_check conformance fixture", () => {
  it("parses exact normalized decisions for the shared shell vectors", () => {
    const fixture = ShellConformanceFixture.parse(
      JSON.parse(readFileSync(fixturePath, "utf8")),
    );

    assert.equal(fixture.schema_version, "ua.conformance.v1");
    assert.equal(fixture.suite, "shell-run-cmd-check");
    assert.deepEqual(
      fixture.cases.map((item) => item.id),
      [
        "clean-read-only",
        "denied-program",
        "literal-shell-syntax",
        "argument-path-escape",
        "find-side-effect-bypass",
      ],
    );
    assert.deepEqual(
      fixture.cases.map((item) => item.expected),
      [
        { ok: true, verdict: "ok", mutates: "no", finding_codes: [] },
        {
          ok: false,
          verdict: "block",
          mutates: "maybe",
          finding_codes: ["path-escape", "rejected"],
        },
        { ok: true, verdict: "warn", mutates: "no", finding_codes: ["shell-syntax"] },
        { ok: true, verdict: "warn", mutates: "no", finding_codes: ["path-escape"] },
        {
          ok: false,
          verdict: "block",
          mutates: "yes",
          finding_codes: ["allowlist-bypass", "destructive"],
        },
      ],
    );
  });
});
