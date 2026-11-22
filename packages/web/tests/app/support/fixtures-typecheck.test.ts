// @vitest-environment node
import path from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

/**
 * The repo's tsc run covers `src` only. The shared test fixtures must match
 * the hub's response types, so compile them here: a fixture that drifts from
 * the real shape fails `npm test`. (Deliberately malformed responses are in
 * `malformed.ts` and cast on purpose.)
 */
const WEB = path.resolve(__dirname, "../../..");
const FILES = ["tests/app/support/fixtures.ts", "tests/app/support/mockHub.ts", "tests/app/support/malformed.ts"];

describe("shared test fixtures type-check against the hub types", () => {
  it("compile with no errors under strict settings", () => {
    const program = ts.createProgram(
      FILES.map((f) => path.join(WEB, f)),
      {
        strict: true,
        noEmit: true,
        skipLibCheck: true,
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.ESNext,
        moduleResolution: ts.ModuleResolutionKind.Bundler,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
        lib: ["lib.es2022.d.ts", "lib.dom.d.ts", "lib.dom.iterable.d.ts"],
        types: [],
      },
    );
    const diagnostics = ts
      .getPreEmitDiagnostics(program)
      .filter((d) => d.file && FILES.some((f) => d.file!.fileName.endsWith(f)))
      .map((d) => `${path.relative(WEB, d.file!.fileName)}: ${ts.flattenDiagnosticMessageText(d.messageText, "\n")}`);
    expect(diagnostics).toEqual([]);
  }, 120_000);
});
