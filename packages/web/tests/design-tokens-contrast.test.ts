// @vitest-environment node
//
// Design protocol §3.2: recompute the WCAG contrast of every token pair the
// Control Center relies on, from src/index.css itself, so a re-valued token
// cannot quietly drop text under 4.5:1 or a control boundary under 3:1.
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import ts from "typescript";
import { parseThemeColors } from "../design-system/build.mjs";

const WEB = path.resolve(__dirname, "..");
const { themes } = parseThemeColors(readFileSync(path.join(WEB, "src/index.css"), "utf8"));

function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(h.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const TEXT = 4.5;
const NON_TEXT = 3;

// [foreground, grounds it must read on, floor]
const PAIRS: [string, string[], number][] = [
  ["ua-fg", ["ua-bg", "ua-surface", "ua-raised", "ua-sunken", "ua-hover", "ua-selected"], TEXT],
  ["ua-muted", ["ua-bg", "ua-surface", "ua-raised", "ua-hover"], TEXT],
  // ua-faint is for captions on a surface; the light canvas is deliberately excluded.
  ["ua-faint", ["ua-surface", "ua-raised"], TEXT],
  ["ua-accent", ["ua-bg", "ua-surface", "ua-raised"], TEXT],
  ["ua-link", ["ua-bg", "ua-surface", "ua-raised"], TEXT],
  ["ua-ok", ["ua-bg", "ua-surface", "ua-ok-surface"], TEXT],
  ["ua-live", ["ua-bg", "ua-surface", "ua-live-surface"], TEXT],
  ["ua-warn", ["ua-bg", "ua-surface", "ua-warn-surface"], TEXT],
  ["ua-decision", ["ua-bg", "ua-surface", "ua-decision-surface"], TEXT],
  ["ua-danger", ["ua-bg", "ua-surface", "ua-danger-surface"], TEXT],
  ["ua-accent", ["ua-accent-surface"], TEXT],
  ["ua-info", ["ua-bg", "ua-surface"], TEXT],
  ["ua-accent-fg", ["ua-accent"], TEXT],
  ["ua-danger-fg", ["ua-danger"], TEXT],
  ["ua-border-strong", ["ua-bg", "ua-surface"], NON_TEXT],
  ["ua-accent", ["ua-bg", "ua-surface"], NON_TEXT], // focus ring
];

describe("design token contrast", () => {
  it("parses all three themes", () => {
    expect(Object.keys(themes).sort()).toEqual(["dark", "high-contrast", "light"]);
  });

  for (const theme of ["dark", "light", "high-contrast"]) {
    it(`${theme}: every text pair clears 4.5:1 and every control boundary 3:1`, () => {
      const t = themes[theme] as Record<string, string>;
      const failures: string[] = [];
      for (const [fg, grounds, floor] of PAIRS) {
        for (const bg of grounds) {
          expect(t[fg], `--${fg} in ${theme}`).toMatch(/^#[0-9a-f]{6}$/i);
          expect(t[bg], `--${bg} in ${theme}`).toMatch(/^#[0-9a-f]{6}$/i);
          const ratio = contrast(t[fg], t[bg]);
          if (ratio < floor) failures.push(`${fg} on ${bg}: ${ratio.toFixed(2)} < ${floor}`);
        }
      }
      expect(failures).toEqual([]);
    });
  }

  it("never gives the action colour a state colour's value", () => {
    for (const theme of ["dark", "light"]) {
      const t = themes[theme] as Record<string, string>;
      for (const state of ["ua-ok", "ua-live", "ua-warn", "ua-decision", "ua-danger"]) {
        expect(t["ua-accent"].toLowerCase(), `${theme}: accent equals ${state}`).not.toBe(
          t[state].toLowerCase(),
        );
      }
    }
  });
});

describe("native form control boundaries", () => {
  for (const file of ["lib/auth/SignInView.tsx"]) {
    it(`${file}: every native field uses a boundary that clears 3:1`, () => {
      const source = ts.createSourceFile(file, readFileSync(path.join(WEB, "src", file), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
      let controls = 0;
      function visit(node: ts.Node) {
        if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && ["input", "select", "textarea"].includes(node.tagName.getText(source))) {
          controls++;
          const attr = node.attributes.properties.find((p) => ts.isJsxAttribute(p) && p.name.getText(source) === "className");
          expect(attr && ts.isJsxAttribute(attr) && attr.initializer && ts.isStringLiteral(attr.initializer)).toBeTruthy();
          if (!attr || !ts.isJsxAttribute(attr) || !attr.initializer || !ts.isStringLiteral(attr.initializer)) throw new Error("Native field needs a verifiable className");
          const classes = attr.initializer.text;
          const border = classes.match(/border-\[var\(--(ua-[\w-]+)\)\]/)?.[1];
          const ground = classes.match(/bg-\[var\(--(ua-[\w-]+)\)\]/)?.[1];
          expect(border).toBe("ua-border-strong");
          expect(ground).toBeDefined();
          for (const [theme, tokens] of Object.entries(themes) as [string, Record<string, string>][]) {
            expect(contrast(tokens[border!], tokens[ground!]), `${file} ${theme}: ${border} on ${ground}`).toBeGreaterThanOrEqual(NON_TEXT);
          }
        }
        ts.forEachChild(node, visit);
      }
      visit(source);
      expect(controls).toBeGreaterThan(0);
    });
  }
});

describe("raw colour literals", () => {
  // Components reference tokens. Persona hues live in AgentPersonaBadge (the
  // design-system build reads them there); Excalidraw's canvas takes a literal.
  const ALLOWED = new Set(["lib/ui/AgentPersonaBadge.tsx", "lib/canvas/ExcalidrawHost.tsx"]);

  function files(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = path.join(dir, name);
      if (statSync(full).isDirectory()) return files(full);
      return /\.(tsx?|css)$/.test(name) ? [full] : [];
    });
  }

  it("keeps six-digit hex values out of components", () => {
    const src = path.join(WEB, "src");
    const offenders = files(src)
      .map((f) => path.relative(src, f).split(path.sep).join("/"))
      .filter((rel) => rel !== "index.css" && !ALLOWED.has(rel))
      .filter((rel) => /#[0-9a-fA-F]{6}\b/.test(readFileSync(path.join(src, rel), "utf8")));
    expect(offenders).toEqual([]);
  });
});
