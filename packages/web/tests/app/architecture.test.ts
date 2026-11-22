/**
 * Architecture rules for Control Center v2 (spec §6, "Owner epoch").
 *
 * Everything under src/app that can outlive an await goes through the owner
 * epoch (`src/app/lib/epoch.tsx`): storage, toasts and query clients. This
 * test reads the source and fails on any other route to them, so a slice
 * cannot write to storage, show a toast or keep a cache that survives an
 * owner change by accident.
 *
 * The scan covers src/app and every module it imports, directly or not,
 * from the rest of src/ (the sign-in view, the ui primitives, the theme):
 * a component outside src/app can hold a callback that outlives the owner
 * just as well (review round 8, item 3). Files outside src/app are named
 * relative to src/app ("../lib/auth/SignInView.tsx"); the few that may use an
 * API are listed with the reason.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { fixtureProgram, projectProgram, unwrappedCompletionCallbacks } from "./support/completionCallbacks";

const here = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(here, "../../src/app");
const SRC = path.resolve(APP, "..");

type Rule = {
  name: string;
  pattern: RegExp;
  /** Files (relative to src/app) allowed to match. */
  allowed: string[];
};

const RULES: Rule[] = [
  {
    name: "direct Web Storage (localStorage / sessionStorage)",
    pattern: /\b(localStorage|sessionStorage)\b/,
    // The raw, never-throwing accessors; only the two modules below use them.
    // The theme is a per-device display preference, not owner data, and nothing in it outlives an await.
    allowed: ["lib/storage.ts", "../lib/theme/ThemeProvider.tsx"],
  },
  {
    name: "raw storage areas (readRaw, writeRaw, removeRaw, keysRaw)",
    pattern: /\b(readRaw|writeRaw|removeRaw|keysRaw)\b/,
    // epoch.tsx scopes every key under the verified owner.
    allowed: ["lib/storage.ts", "lib/epoch.tsx"],
  },
  {
    name: "unscoped session helpers (readSession, writeSession, removeSession, sessionKeys)",
    pattern: /\b(readSession|writeSession|removeSession|sessionKeys)\b/,
    // The return path is kept before anyone is signed in, so it has no owner.
    allowed: ["lib/storage.ts", "lib/returnTo.ts"],
  },
  {
    name: "imports of the storage module",
    pattern: /from\s+["'][./]*(lib\/)?storage["']/,
    // Anything new it exports is unscoped; only these two may use it.
    allowed: ["lib/epoch.tsx", "lib/returnTo.ts"],
  },
  {
    name: "direct toasts (useToast, ToastProvider, ToastApi)",
    pattern: /\b(useToast|ToastProvider|ToastApi)\b/,
    // Pages toast through `epoch.toast`, which drops a toast once revoked.
    // ui/Toast.tsx defines the primitive, ui/index.ts re-exports it, and the
    // primitives showcase renders demo toasts with no hub operation behind them.
    allowed: ["lib/epoch.tsx", "../lib/ui/Toast.tsx", "../lib/ui/index.ts", "../lib/ui/PrimitivesShowcase.tsx"],
  },
  {
    name: "a QueryClient made or provided outside the epoch",
    pattern: /\bnew QueryClient\b|\bQueryClientProvider\b/,
    // Each epoch owns one client, made when the epoch is made.
    allowed: ["lib/epoch.tsx"],
  },
];

/** Source without comments, so prose that names a forbidden API does not count. */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return /\.(ts|tsx)$/.test(name) ? [full] : [];
  });
}

/** `file:line: text` for every line that breaks `rule`. */
function violations(rule: Rule, files: { rel: string; source: string }[]): string[] {
  const found: string[] = [];
  for (const { rel, source } of files) {
    if (rule.allowed.includes(rel)) continue;
    stripComments(source)
      .split("\n")
      .forEach((line, index) => {
        if (rule.pattern.test(line)) found.push(`src/app/${rel}:${index + 1}: ${line.trim()}`);
      });
  }
  return found;
}

function resolveImport(from: string, spec: string): string | null {
  const base = path.resolve(path.dirname(from), spec);
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts"), path.join(base, "index.tsx")]) {
    if (existsSync(candidate) && statSync(candidate).isFile()) return candidate;
  }
  return null;
}

/** src/app and everything in src/ it imports, directly or not (relative imports, static and dynamic). */
function appClosure(): string[] {
  const seen = new Set<string>();
  const queue = sourceFiles(APP);
  while (queue.length > 0) {
    const file = queue.pop()!;
    if (seen.has(file)) continue;
    seen.add(file);
    for (const match of readFileSync(file, "utf8").matchAll(/(?:from\s+|import\s*\(\s*)["'](\.[^"']+)["']/g)) {
      const target = resolveImport(file, match[1]);
      if (target && target.startsWith(SRC + path.sep)) queue.push(target);
    }
  }
  return [...seen].sort();
}

const FILES = appClosure().map((full) => ({
  rel: path.relative(APP, full).split(path.sep).join("/"),
  source: readFileSync(full, "utf8"),
}));

describe("src/app reaches storage, toasts and query clients only through the owner epoch", () => {
  it("scans the app sources", () => {
    expect(FILES.map((f) => f.rel)).toEqual(
      expect.arrayContaining(["lib/epoch.tsx", "shell/AppLayout.tsx", "../lib/auth/SignInView.tsx", "../lib/ui/Toast.tsx"]),
    );
  });

  it.each(RULES.map((rule) => [rule.name, rule] as const))("no %s outside its sanctioned module", (_name, rule) => {
    expect(violations(rule, FILES)).toEqual([]);
  });

  it("the epoch module makes its QueryClient per epoch, never at module level", () => {
    const epoch = stripComments(FILES.find((f) => f.rel === "lib/epoch.tsx")!.source);
    const lines = epoch.split("\n").filter((line) => /\bnew QueryClient\b/.test(line));
    expect(lines.length).toBeGreaterThan(0);
    // Indented: inside a function body (makeEpoch), not a top-level statement.
    for (const line of lines) expect(line).toMatch(/^\s+/);
  });

  it("catches what it is meant to catch", () => {
    const fake = [
      { rel: "work/Page.tsx", source: 'const t = useToast();\nwindow.localStorage.setItem("k", "v"); // sessionStorage\nconst qc = new QueryClient();' },
      { rel: "work/Ok.tsx", source: "/* localStorage useToast */\n// new QueryClient()\nconst url = \"http://x\";" },
    ];
    expect(RULES.flatMap((rule) => violations(rule, fake))).toEqual([
      'src/app/work/Page.tsx:2: window.localStorage.setItem("k", "v");',
      "src/app/work/Page.tsx:1: const t = useToast();",
      "src/app/work/Page.tsx:3: const qc = new QueryClient();",
    ]);
  });
});

/**
 * Completion callbacks (onSuccess, onComplete, onDone, onSettled) handed on
 * as JSX props must be `.wrap(...)`ed by the epoch or traceable to such a
 * call. Parsed with the TypeScript compiler and its type checker (review
 * round 9: a regex let `{...{ onSuccess: done }}` and `onSuccess = {done}`
 * through). See support/completionCallbacks.ts.
 */
describe("completion callbacks are bound to the owner epoch (TypeScript AST)", () => {
  const DECLS = `
    declare const epoch: { wrap<T>(callback: T): T };
    declare function SignInView(props: { onSuccess?: () => void; label?: string }): any;
    declare function done(): void;
  `;
  const check = (source: string) => {
    const program = fixtureProgram({ "/fixture.tsx": DECLS + source });
    return unwrappedCompletionCallbacks(program.getSourceFile("/fixture.tsx")!, program.getTypeChecker(), "fixture");
  };

  it("scans every file in the closure with the project's compiler options", () => {
    const files = FILES.map((f) => path.resolve(APP, f.rel));
    const program = projectProgram(files, path.resolve(APP, "../../tsconfig.json"));
    const checker = program.getTypeChecker();
    const found = files.flatMap((file) => {
      const sourceFile = program.getSourceFile(file);
      // Fail closed: every scanned file must have been parsed.
      if (!sourceFile) return [`${file}: not parsed`];
      return unwrappedCompletionCallbacks(sourceFile, checker, `src/${path.relative(SRC, file).split(path.sep).join("/")}`);
    });
    expect(found).toEqual([]);
  }, 120_000);

  it.each([
    ["an arrow function", "const a = <SignInView onSuccess={() => done()} />;"],
    ["spaces around =", "const a = <SignInView onSuccess = {done} />;"],
    ["a spread object literal", "const a = <SignInView {...{ onSuccess: done }} />;"],
    ["a shorthand in a spread literal", "const onSuccess = done; const a = <SignInView {...{ onSuccess }} />;"],
    ["a nested spread literal", "const a = <SignInView {...{ ...{ onSuccess: done } }} />;"],
    ["a string-literal computed key", 'const a = <SignInView {...{ ["onSuccess"]: done }} />;'],
    ["a non-literal computed key", "declare const k: string; const a = <SignInView {...{ [k]: done }} />;"],
    ["a spread of a typed variable that carries it", "declare const p: { onSuccess: () => void }; const a = <SignInView {...p} />;"],
    ["a spread of an untyped value", "declare const p: any; const a = <SignInView {...p} />;"],
    ["a let binding (could be reassigned)", "let w = epoch.wrap(done); const a = <SignInView onSuccess={w} />;"],
    ["a function parameter", "function F(cb: () => void) { return <SignInView onSuccess={cb} />; }"],
    ["a method in a spread literal", "const a = <SignInView {...{ onSuccess() {} }} />;"],
    // Review round 10: an assertion must not hide the value, and every union member counts.
    [
      "a spread whose type is asserted away",
      "const p = { onSuccess: done } as any; const a = <SignInView {...(p as { label?: string })} />;",
    ],
    [
      "a spread typed as a union with one member carrying it",
      "declare const p: { onSuccess: () => void } | { label: string }; const a = <SignInView {...p} />;",
    ],
    [
      "a spread literal behind an assertion",
      "const a = <SignInView {...({ onSuccess: done } as { label?: string })} />;",
    ],
    [
      "a spread typed as an intersection carrying it",
      "declare const p: { label: string } & { onSuccess: () => void }; const a = <SignInView {...p} />;",
    ],
    ["a spread of an unknown value", "declare const p: unknown; const a = <SignInView {...(p as object)} />;"],
    ["a spread of a generic props value", "function F<T>(p: T) { return <SignInView {...(p as object)} />; }"],
  ])("fails %s", (_name, source) => {
    expect(check(source)).not.toEqual([]);
  });

  it.each([
    ["a direct .wrap(...) call", "const a = <SignInView onSuccess={epoch.wrap(done)} />;"],
    ["a const bound to .wrap(...)", "const ok = epoch.wrap(done); const a = <SignInView onSuccess={ok} />;"],
    ["a const chain to .wrap(...)", "const w = epoch.wrap(done); const ok = w; const a = <SignInView onSuccess = {ok} />;"],
    ["a spread literal with .wrap(...)", "const a = <SignInView {...{ onSuccess: epoch.wrap(done) }} />;"],
    ["a spread of a typed variable without completion props", "declare const p: { label: string }; const a = <SignInView {...p} />;"],
    ["an unrelated prop", "const a = <SignInView label={String(done)} />;"],
    ["a const literal without completion props behind an assertion", 'const p = { label: "x" } as const; const a = <SignInView {...p} />;'],
  ])("passes %s", (_name, source) => {
    expect(check(source)).toEqual([]);
  });
});
