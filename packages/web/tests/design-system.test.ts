// @vitest-environment node
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import vm from "node:vm";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterAll, describe, expect, it, vi } from "vitest";
import {
  COMPONENTS,
  NAMESPACE,
  buildDesignSystem,
  buildTokens,
  parsePersonas,
  parseThemeColors,
} from "../design-system/build.mjs";

const CSS = `
:root,
[data-theme="dark"] { --ua-bg: #0F1419; --ua-fg: #e7ecf3; font-family: x; }
[data-theme="light"] { --ua-bg: #f4f7fb; --ua-fg: #142033; }
@media (prefers-color-scheme: light) {
  :root:not([data-theme]) { --ua-bg: #f4f7fb; --ua-fg: #142033; }
}
body { margin: 0; }
`;
const PERSONAS = `
const FLEET_DEFAULT_PERSONAS = {
  Desktop_Agent1: { displayName: "Claude Code", avatar: "x", color: "#D97706" },
};
const FALLBACK_PALETTE = [
  "#D97706",
  "#ef4444",
];
`;
const META = {
  name: "Test",
  themes: [
    { id: "dark", name: "Dark" },
    { id: "light", name: "Light" },
  ],
  colors: [
    { name: "ua-bg", usage: "ground" },
    { name: "ua-fg", usage: "text" },
  ],
  personas: [
    { name: "persona-claude", agent: "Desktop_Agent1", usage: "stripe" },
    { name: "persona-red", fallbackIndex: 1, usage: "fallback" },
  ],
  type: { fonts: [], families: {}, groups: [] },
  spacing: { tokens: [] },
  radius: { tokens: [] },
  size: { tokens: [] },
  opacity: { tokens: [] },
};
const SRC = { css: CSS, personaSource: PERSONAS, ref: "main@abc1234", synced: "2026-10-01" };

describe("design system tokens", () => {
  it("reads --ua-* values per data-theme and keeps the media fallback apart", () => {
    const { themes, fallbacks } = parseThemeColors(CSS);
    expect(themes.dark).toEqual({ "ua-bg": "#0F1419", "ua-fg": "#e7ecf3" });
    expect(themes.light["ua-fg"]).toBe("#142033");
    expect(fallbacks).toHaveLength(1);
  });

  it("reads persona hues and the fallback palette", () => {
    const { byLabel, fallback } = parsePersonas(PERSONAS);
    expect(byLabel.Desktop_Agent1).toBe("#d97706");
    expect(fallback).toEqual(["#d97706", "#ef4444"]);
  });

  it("emits list-shaped colour tokens with one value per theme, lowercased", () => {
    const tokens = buildTokens(META, SRC);
    expect(tokens.color.tokens).toEqual([
      { name: "ua-bg", value: { dark: "#0f1419", light: "#f4f7fb" }, usage: "ground" },
      { name: "ua-fg", value: { dark: "#e7ecf3", light: "#142033" }, usage: "text" },
      { name: "persona-claude", value: "#d97706", usage: "stripe" },
      { name: "persona-red", value: "#ef4444", usage: "fallback" },
    ]);
    expect(tokens.meta).toMatchObject({ source: "github", ref: "main@abc1234", package: "packages/web" });
  });

  it("fails on a CSS variable the metadata does not describe", () => {
    const css = CSS.replace("--ua-fg: #142033; }", "--ua-fg: #142033; --ua-new: #fff; }");
    expect(() => buildTokens(META, { ...SRC, css })).toThrow(/--ua-new \(light\) has no entry/);
  });

  it("fails when a described colour is missing from a theme", () => {
    const css = CSS.replace('[data-theme="light"] { --ua-bg: #f4f7fb;', '[data-theme="light"] {');
    expect(() => buildTokens(META, { ...SRC, css })).toThrow(/--ua-bg is missing from the light theme/);
  });

  it("fails when the prefers-color-scheme fallback drifts from every theme", () => {
    const css = CSS.replace(":root:not([data-theme]) { --ua-bg: #f4f7fb", ":root:not([data-theme]) { --ua-bg: #000000");
    expect(() => buildTokens(META, { ...SRC, css })).toThrow(/fallback block matches no data-theme/);
  });

  it("fails on a data-theme block the metadata does not list", () => {
    const css = CSS + '[data-theme="sepia"] { --ua-bg: #f0e0c0; --ua-fg: #3b2f1e; }\n';
    expect(() => buildTokens(META, { ...SRC, css })).toThrow(/\[data-theme="sepia"\] has no entry in tokens.meta.json themes/);
  });

  it("fails when the prefers-color-scheme fallback drops a variable", () => {
    const css = CSS.replace(":root:not([data-theme]) { --ua-bg: #f4f7fb; ", ":root:not([data-theme]) { ");
    expect(() => buildTokens(META, { ...SRC, css })).toThrow(/fallback block matches no data-theme/);
  });

  it("reads a last declaration that has no semicolon", () => {
    const css = CSS.replace('[data-theme="light"] { --ua-bg: #f4f7fb; --ua-fg: #142033; }', '[data-theme="light"] { --ua-bg: #f4f7fb; --ua-fg: #142033; --ua-new: #fff }');
    expect(parseThemeColors(css).themes.light["ua-new"]).toBe("#fff");
    expect(() => buildTokens(META, { ...SRC, css })).toThrow(/--ua-new \(light\) has no entry/);
  });

  it("requires a light-scheme fallback to equal the light theme, not any theme", () => {
    const css = CSS.replace(":root:not([data-theme]) { --ua-bg: #f4f7fb; --ua-fg: #142033; }", ":root:not([data-theme]) { --ua-bg: #0F1419; --ua-fg: #e7ecf3; }");
    expect(() => buildTokens(META, { ...SRC, css })).toThrow(/fallback block matches no data-theme block.*theme "light"/);
  });

  it("keeps the light scheme through a comment, an earlier rule or a nested @media", () => {
    const dark = ":root:not([data-theme]) { --ua-bg: #0F1419; --ua-fg: #e7ecf3; }";
    for (const block of [
      `/* light override */\n  ${dark}`,
      `html { color-scheme: light; }\n  ${dark}`,
      `@media (min-width: 600px) { ${dark} }`,
    ]) {
      const css = CSS.replace("  :root:not([data-theme]) { --ua-bg: #f4f7fb; --ua-fg: #142033; }", `  ${block}`);
      expect(() => buildTokens(META, { ...SRC, css })).toThrow(/theme "light"/);
    }
  });

  it("fails on a persona hue no token covers, but accepts hues shared between labels", () => {
    // FALLBACK_PALETTE[0] equals Desktop_Agent1's hue, so persona-claude covers both.
    expect(() => buildTokens(META, SRC)).not.toThrow();
    const personaSource = PERSONAS.replace(
      "};",
      '  Desktop_New: { displayName: "New", avatar: "n", color: "#123456" },\n};',
    );
    expect(() => buildTokens(META, { ...SRC, personaSource })).toThrow(/#123456 \(Desktop_New\) has no persona token/);
    const palette = PERSONAS.replace('"#ef4444",', '"#ef4444",\n  "#0000ff",');
    expect(() => buildTokens(META, { ...SRC, personaSource: palette })).toThrow(/#0000ff \(FALLBACK_PALETTE\[2\]\) has no persona token/);
  });

  it("fails when a persona has no colour in the source", () => {
    const meta = { ...META, personas: [{ name: "persona-x", agent: "Nobody", usage: "u" }] };
    expect(() => buildTokens(meta, SRC)).toThrow(/persona-x: no colour for Nobody/);
  });
});

describe("design system build", () => {
  const out = mkdtempSync(path.join(tmpdir(), "ua-design-system-"));
  afterAll(() => rmSync(out, { recursive: true, force: true }));

  it("writes a publishable project folder whose bundle renders the real components", async () => {
    const files = await buildDesignSystem({ outDir: out, ref: "main@test", synced: "2026-10-01" });
    for (const f of ["README.md", "tokens.json", "components/bundle.js", "components/bundle.css", "components/index.d.ts", "components/Cover/preview.html"]) {
      expect(files).toContain(f);
    }
    for (const c of COMPONENTS) {
      expect(files).toContain(`components/${c}/README.md`);
      expect(readFileSync(path.join(out, "project/components", c, "preview.html"), "utf8")).toMatch(/^<!-- @dsCard /);
    }

    const tokens = JSON.parse(readFileSync(path.join(out, "project/tokens.json"), "utf8"));
    expect(tokens.color.themes.map((t: { id: string }) => t.id)).toEqual(["dark", "light", "high-contrast"]);
    expect(tokens.color.tokens.find((t: { name: string }) => t.name === "ua-accent").value).toEqual({
      dark: "#cba6f7",
      light: "#7028d4",
      "high-contrast": "#ffff00",
    });

    const bundle = readFileSync(path.join(out, "project/components/bundle.js"), "utf8");
    const header = JSON.parse(bundle.split("\n")[0].replace(/^\/\* @ds-bundle: /, "").replace(/ \*\/$/, ""));
    expect(header).toMatchObject({ format: 4, namespace: NAMESPACE });
    expect(header.components.map((c: { name: string }) => c.name)).toEqual(COMPONENTS);
    expect(bundle).not.toMatch(/<\/script|<!--/i);

    const window: Record<string, unknown> = { React };
    vm.runInNewContext(bundle, { window });
    const ns = window[NAMESPACE] as Record<string, React.ComponentType<Record<string, unknown>>>;
    expect(Object.keys(ns).sort()).toEqual([...COMPONENTS].sort());
    const html = renderToStaticMarkup(
      React.createElement(ns.Button, { variant: "primary" }, "Review and publish"),
    );
    expect(html).toContain("Review and publish");
    expect(html).toContain("bg-[var(--ua-accent)]");

    // jsxs spreads static children, so development React sees no keyless array.
    const warn = vi.spyOn(console, "error").mockImplementation(() => {});
    renderToStaticMarkup(React.createElement(ns.Card, null, "body"));
    renderToStaticMarkup(React.createElement(ns.StatusIndicator, { status: "online", label: "Online" }));
    expect(warn.mock.calls.filter((c) => String(c[0]).includes("unique \"key\""))).toEqual([]);
    warn.mockRestore();

    const css = readFileSync(path.join(out, "project/components/bundle.css"), "utf8");
    expect(css).toContain(".bg-\\[var\\(--ua-accent\\)\\]");
    // The app's @theme font stacks reach the exported CSS (not Tailwind's defaults).
    expect(css).toMatch(/--font-sans:\s*"Geist Variable"/);
    expect(css).toMatch(/--font-mono:\s*"Geist Mono Variable"/);
    expect(css).not.toMatch(/--ua-bg:/);
  }, 60_000);

  it("leaves no partial project behind when a build fails, so a rerun succeeds", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "ua-design-system-retry-"));
    const missing = path.join(dir, "no-such-content");
    await expect(buildDesignSystem({ outDir: dir, contentDir: missing, ref: "x", synced: "x" })).rejects.toThrow();
    expect(readdirSync(dir)).toEqual([]);
    const files = await buildDesignSystem({ outDir: dir, ref: "x", synced: "x" });
    expect(files).toContain("tokens.json");
    rmSync(dir, { recursive: true, force: true });
  }, 60_000);

  it("refuses to delete an output folder that is not a previous build", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "ua-design-system-guard-"));
    mkdirSync(path.join(dir, "project"));
    writeFileSync(path.join(dir, "project", "keep.txt"), "mine");
    await expect(buildDesignSystem({ outDir: dir, ref: "x", synced: "x" })).rejects.toThrow(/refusing to delete/);
    expect(readFileSync(path.join(dir, "project", "keep.txt"), "utf8")).toBe("mine");
    rmSync(dir, { recursive: true, force: true });
  });
});
