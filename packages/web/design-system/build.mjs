// Rebuilds the Unified Agent design system (the claude.ai Design System
// artifact) from this package, so the artifact's values never drift from the
// Control Center's code.
//
//   npm run design-system            -> design-system/out/project/
//   node design-system/build.mjs DIR -> DIR/project/
//
// Values the code owns are read from the code: theme colours from
// src/index.css, persona hues from src/lib/ui/AgentPersonaBadge.tsx, components
// from src/lib/ui (bundled with esbuild), utilities from Tailwind. Everything a
// person writes (usage notes, type styles, the brand book, component guides,
// previews, the cover) lives in tokens.meta.json and content/. The output
// folder is what gets published to the artifact; see packages/web/README.md.
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.resolve(HERE, "..");
const require = createRequire(path.join(WEB, "package.json"));

// esbuild is a direct dev dependency (vite 8 bundles with rolldown and no
// longer ships it). The Tailwind compiler comes with @tailwindcss/vite, so
// resolve it from there if not hoisted.
function requireVia(parent, name) {
  try {
    return require(name);
  } catch {
    return createRequire(require.resolve(`${parent}/package.json`))(name);
  }
}

export const NAMESPACE = "UnifiedAgent";
/**
 * Bundled source files in src/lib/ui and the components each one exports. Every
 * exported name is a design-system component with a README and a preview.
 * GraphCanvas is documented (content/components/GraphCanvas) but not
 * bundled: @xyflow/react and its stylesheet are too heavy for the artifact.
 */
const FILE_EXPORTS = {
  Button: ["Button"],
  Badge: ["Badge", "Chip"],
  Card: ["Card"],
  DataTable: ["DataTable"],
  StatusIndicator: ["StatusIndicator"],
  AgentPersonaBadge: ["AgentPersonaBadge"],
  StatePill: ["StatePill"],
  FreshnessStamp: ["FreshnessStamp"],
  Region: ["Region"],
  LinkButton: ["LinkButton"],
  IconButton: ["IconButton"],
  Field: ["Field"],
  Input: ["Input", "Textarea", "Select"],
  Choice: ["Checkbox", "Switch", "RadioGroup"],
  Combobox: ["Combobox"],
  Dialog: ["Dialog", "ConfirmDialog", "Drawer"],
  Menu: ["Menu"],
  Tabs: ["LinkTabs", "Tabs"],
  PageHeader: ["PageHeader"],
  Toolbar: ["Toolbar", "FilterBar"],
  States: ["EmptyState", "ErrorState", "Skeleton"],
  Toast: ["ToastProvider"],
  DescriptionList: ["DescriptionList"],
  CodeBlock: ["CodeBlock"],
  Markdown: ["Markdown"],
};
export const COMPONENTS = Object.values(FILE_EXPORTS).flat();
const UI_FILES = Object.keys(FILE_EXPORTS);
const fileOf = (component) => UI_FILES.find((f) => FILE_EXPORTS[f].includes(component));
const FONTS_IMPORT =
  '@import url("https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap");\n';

/**
 * `--ua-*` values per theme from index.css. A rule's theme is its
 * `[data-theme="<id>"]` selector; `:root:not([data-theme])` (the
 * prefers-color-scheme fallback) has none and is returned as `fallback`.
 */
const SCHEME = Symbol("scheme");

/** The colour scheme of the innermost `@media (prefers-color-scheme: X)` block open at `at`, or null. */
function enclosingScheme(css, at) {
  let scheme = null;
  for (const media of css.slice(0, at).matchAll(/@media([^{]*)\{/g)) {
    let depth = 0;
    let inside = true;
    for (const ch of css.slice(media.index + media[0].length, at)) {
      if (ch === "{") depth++;
      else if (ch === "}" && --depth < 0) {
        inside = false;
        break;
      }
    }
    // A nested @media without a colour-scheme condition keeps its parent's scheme.
    if (inside) scheme = media[1].match(/prefers-color-scheme:\s*(light|dark)/)?.[1] ?? scheme;
  }
  return scheme;
}

export function parseThemeColors(source) {
  const css = source.replace(/\/\*[\s\S]*?\*\//g, "");
  const themes = {};
  const fallbacks = [];
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = m[1];
    const vars = {};
    for (const v of m[2].matchAll(/--(ua-[a-z0-9-]+)\s*:\s*([^;}]+?)\s*(?:;|$)/g)) vars[v[1]] = v[2].trim();
    if (!Object.keys(vars).length) continue;
    const ids = [...selector.matchAll(/data-theme="([a-z0-9-]+)"/g)].map((t) => t[1]);
    if (ids.length) for (const id of ids) themes[id] = { ...themes[id], ...vars };
    else {
      // An @media (prefers-color-scheme: X) wrapper names the theme it must equal.
      const scheme = enclosingScheme(css, m.index);
      fallbacks.push(scheme ? { ...vars, [SCHEME]: scheme } : vars);
    }
  }
  return { themes, fallbacks };
}

/** Persona label -> hex, and the hash fallback palette, from AgentPersonaBadge.tsx. */
export function parsePersonas(tsx) {
  const byLabel = {};
  for (const m of tsx.matchAll(/^\s*(\w+):\s*\{[^}]*color:\s*"(#[0-9a-fA-F]{3,8})"/gm)) {
    byLabel[m[1]] = m[2].toLowerCase();
  }
  const palette = tsx.match(/FALLBACK_PALETTE\s*=\s*\[([^\]]*)\]/);
  const fallback = palette ? [...palette[1].matchAll(/"(#[0-9a-fA-F]{3,8})"/g)].map((h) => h[1].toLowerCase()) : [];
  return { byLabel, fallback };
}

/** tokens.json in the Design System type's list shape. Throws on drift between meta and code. */
export function buildTokens(meta, { css, personaSource, ref, synced }) {
  const { themes, fallbacks } = parseThemeColors(css);
  const themeIds = meta.themes.map((t) => t.id);
  const problems = [];

  for (const id of themeIds) if (!themes[id]) problems.push(`index.css has no [data-theme="${id}"] block`);
  for (const id of Object.keys(themes)) {
    if (!themeIds.includes(id)) problems.push(`[data-theme="${id}"] has no entry in tokens.meta.json themes`);
  }
  const known = new Set(meta.colors.map((c) => c.name));
  for (const id of Object.keys(themes)) {
    for (const name of Object.keys(themes[id])) {
      if (!known.has(name)) problems.push(`--${name} (${id}) has no entry in tokens.meta.json colors`);
    }
  }
  // The fallback must equal a theme exactly: a variable it drops would fall
  // through to the dark :root value for unthemed light-mode visitors.
  for (const fb of fallbacks) {
    const candidates = fb[SCHEME] ? [themes[fb[SCHEME]] ?? {}] : Object.values(themes);
    const match = candidates.find(
      (vars) =>
        Object.keys(vars).length === Object.keys(fb).length &&
        Object.entries(fb).every(([k, v]) => vars[k]?.toLowerCase() === v.toLowerCase()),
    );
    if (!match) {
      problems.push(
        `the prefers-color-scheme fallback block matches no data-theme block (same variables and values${fb[SCHEME] ? `, theme "${fb[SCHEME]}"` : ""})`,
      );
    }
  }

  const colors = meta.colors.map(({ name, usage }) => {
    const value = {};
    for (const id of themeIds) {
      const v = themes[id]?.[name];
      if (v === undefined) problems.push(`--${name} is missing from the ${id} theme`);
      else value[id] = v.toLowerCase();
    }
    return { name, value, usage };
  });

  const { byLabel, fallback } = parsePersonas(personaSource);
  for (const p of meta.personas) {
    const v = p.agent !== undefined ? byLabel[p.agent] : fallback[p.fallbackIndex];
    if (!v) problems.push(`${p.name}: no colour for ${p.agent ?? `FALLBACK_PALETTE[${p.fallbackIndex}]`}`);
    else colors.push({ name: p.name, value: v, usage: p.usage });
  }
  // Labels may share a hue, so one token can cover several; but every distinct
  // hue in the source needs a token or it silently drops out of the palette.
  const covered = new Set(colors.filter((c) => typeof c.value === "string").map((c) => c.value));
  const sourceHues = [
    ...Object.entries(byLabel).map(([label, hex]) => [hex, label]),
    ...fallback.map((hex, i) => [hex, `FALLBACK_PALETTE[${i}]`]),
  ];
  for (const [hex, where] of sourceHues) {
    if (!covered.has(hex)) problems.push(`${hex} (${where}) has no persona token in tokens.meta.json`);
  }

  if (problems.length) throw new Error("design system drift:\n  " + problems.join("\n  "));

  return {
    name: meta.name,
    version: 1,
    meta: {
      source: "github",
      repo: "anonb3ll/unified-agent",
      ref,
      package: "packages/web",
      paths: {
        tokens: ["packages/web/src/index.css", "packages/web/src/lib/ui/AgentPersonaBadge.tsx"],
        fonts: [],
        assets: [],
        docs: ["packages/web/design-system/content"],
      },
      components: Object.fromEntries(
        COMPONENTS.map((c) => [c, `packages/web/src/lib/ui/${fileOf(c)}.tsx`]),
      ),
      synced,
    },
    color: { themes: meta.themes, tokens: colors },
    type: meta.type,
    spacing: meta.spacing,
    radius: meta.radius,
    size: meta.size,
    opacity: meta.opacity,
  };
}

// react, react-dom and the automatic JSX runtime resolve to the page's
// window.React / window.ReactDOM; jsx() is shimmed over createElement.
const reactGlobals = {
  name: "react-globals",
  setup(build) {
    build.onResolve({ filter: /^react(-dom)?(\/.*)?$/ }, (a) => ({ path: a.path, namespace: "react-global" }));
    build.onLoad({ filter: /.*/, namespace: "react-global" }, (a) => ({
      loader: "js",
      contents: a.path.startsWith("react-dom")
        ? "module.exports = window.ReactDOM;"
        : /jsx-(dev-)?runtime$/.test(a.path)
          ? "var R = window.React;" +
            "function mk(t, p, k, isStatic) { var c = Object.assign({}, p); var ch = c.children; delete c.children;" +
            " if (k !== undefined) c.key = k;" +
            " if (ch === undefined) return R.createElement(t, c);" +
            " return isStatic && Array.isArray(ch) ? R.createElement.apply(null, [t, c].concat(ch)) : R.createElement(t, c, ch); }" +
            "function jsx(t, p, k) { return mk(t, p, k, false); }" +
            "function jsxs(t, p, k) { return mk(t, p, k, true); }" +
            "module.exports = { jsx: jsx, jsxs: jsxs, jsxDEV: jsx, Fragment: R.Fragment };"
          : "module.exports = window.React;",
    }));
  },
};

async function buildBundle() {
  const esbuild = requireVia("vite", "esbuild");
  const entry = UI_FILES.map((f) => {
    const names = FILE_EXPORTS[f].join(", ");
    return `export { ${names} } from ${JSON.stringify(path.join(WEB, "src/lib/ui", f))};`;
  }).join("\n");
  const res = await esbuild.build({
    stdin: { contents: entry, resolveDir: WEB, loader: "ts" },
    bundle: true,
    format: "iife",
    globalName: NAMESPACE,
    minify: true,
    write: false,
    target: "es2019",
    define: { "process.env.NODE_ENV": '"production"' },
    plugins: [reactGlobals],
    absWorkingDir: WEB,
    logLevel: "silent",
  });
  const header = `/* @ds-bundle: ${JSON.stringify({
    format: 4,
    namespace: NAMESPACE,
    components: COMPONENTS.map((name) => ({ name })),
  })} */\n`;
  const js = header + res.outputFiles[0].text + `window.${NAMESPACE}=${NAMESPACE};\n`;
  if (/<\/script|<!--/i.test(js)) throw new Error("bundle.js contains </script or <!--; consumers inline it");
  return js;
}

async function buildCss(contentDir) {
  const { compile } = requireVia("@tailwindcss/vite", "@tailwindcss/node");
  const { Scanner } = requireVia("@tailwindcss/vite", "@tailwindcss/oxide");
  const indexCss = readFileSync(path.join(WEB, "src/index.css"), "utf8");
  // The app's @theme (Geist font stacks) must reach the artifact's Tailwind
  // build too, or exported previews fall back to Tailwind's default fonts.
  const appTheme = indexCss.match(/@theme\s*\{[^}]*\}/)?.[0] ?? "";
  const compiler = await compile(`@import "tailwindcss";\n${appTheme}\n`, {
    base: WEB,
    onDependency() {},
  });
  const scanner = new Scanner({
    sources: [
      { base: path.join(WEB, "src/lib/ui"), pattern: "**/*", negated: false },
      { base: path.join(contentDir, "components"), pattern: "**/preview.html", negated: false },
    ],
  });
  // Token values come from tokens.css on the artifact; keep only the rules after them.
  const bodyAt = indexCss.indexOf("body {");
  if (bodyAt < 0) throw new Error('src/index.css has no "body {" rule to start the base rules from');
  const baseRules = indexCss.slice(bodyAt);
  const css =
    FONTS_IMPORT +
    compiler.build(scanner.scan()) +
    "\n/* base rules from packages/web/src/index.css */\n" +
    ":root{font-family:var(--font-sans);line-height:1.45;}\n" +
    baseRules;
  if (/<\/style/i.test(css)) throw new Error("bundle.css contains </style");
  return css;
}

/** Declarations for the bundled components, one section per source file (documentation only). */
function buildTypes() {
  const ts = require("typescript");
  const files = UI_FILES.map((f) => path.join(WEB, "src/lib/ui", `${f}.tsx`));
  const out = {};
  const program = ts.createProgram(files, {
    declaration: true,
    emitDeclarationOnly: true,
    jsx: ts.JsxEmit.ReactJSX,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    esModuleInterop: true,
    skipLibCheck: true,
    noEmitOnError: false,
  });
  program.emit(undefined, (name, text) => {
    out[path.basename(name, ".d.ts")] = text;
  });
  return (
    `// Types of window.${NAMESPACE}, emitted from packages/web/src/lib/ui (React 19 source).\n` +
    UI_FILES.map((f) => `\n// ---- src/lib/ui/${f}.tsx ----\n${out[f] ?? ""}`).join("")
  );
}

function gitRef() {
  try {
    const sha = execFileSync("git", ["rev-parse", "--short", "HEAD"], { cwd: WEB, encoding: "utf8" }).trim();
    const branch = execFileSync("git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd: WEB, encoding: "utf8" }).trim();
    return `${branch}@${sha}`;
  } catch {
    return "unknown";
  }
}

/** Writes <outDir>/project/ and returns the list of written paths (relative to project/). */
export async function buildDesignSystem({
  outDir = path.join(HERE, "out"),
  contentDir = path.join(HERE, "content"),
  ref = gitRef(),
  synced = new Date().toISOString().slice(0, 10),
} = {}) {
  const meta = JSON.parse(readFileSync(path.join(HERE, "tokens.meta.json"), "utf8"));
  const tokens = buildTokens(meta, {
    css: readFileSync(path.join(WEB, "src/index.css"), "utf8"),
    personaSource: readFileSync(path.join(WEB, "src/lib/ui/AgentPersonaBadge.tsx"), "utf8"),
    ref,
    synced,
  });
  const [bundle, css] = await Promise.all([buildBundle(), buildCss(contentDir)]);

  const project = path.join(outDir, "project");
  if (existsSync(project) && !existsSync(path.join(project, "tokens.json"))) {
    throw new Error(`${project} exists and is not a previous design-system build; refusing to delete it`);
  }
  // Stage beside the target and swap in only after every write succeeds, so an
  // interrupted build never leaves a partial project that blocks the next run.
  const staged = path.join(outDir, `.project-staging-${process.pid}`);
  rmSync(staged, { recursive: true, force: true });
  try {
    mkdirSync(path.join(staged, "components"), { recursive: true });
    cpSync(contentDir, staged, { recursive: true });
    writeFileSync(path.join(staged, "components/bundle.js"), bundle);
    writeFileSync(path.join(staged, "components/bundle.css"), css);
    writeFileSync(path.join(staged, "components/index.d.ts"), buildTypes());
    writeFileSync(path.join(staged, "tokens.json"), JSON.stringify(tokens, null, 1) + "\n");
    rmSync(project, { recursive: true, force: true });
    renameSync(staged, project);
  } catch (err) {
    rmSync(staged, { recursive: true, force: true });
    throw err;
  }
  return listFiles(project);
}

function listFiles(dir, prefix = "") {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? listFiles(path.join(dir, e.name), `${prefix}${e.name}/`) : [`${prefix}${e.name}`],
  );
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const outDir = process.argv[2] ? path.resolve(process.argv[2]) : undefined;
  buildDesignSystem({ outDir }).then(
    (files) => {
      console.log(`design system: ${files.length} files in ${path.join(outDir ?? path.join(HERE, "out"), "project")}`);
    },
    (err) => {
      console.error(err.message);
      process.exit(1);
    },
  );
}
