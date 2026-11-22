import { defineConfig, searchForWorkspaceRoot, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { LOOPBACK_HOST, assertLoopbackDevServer, devHubProxy } from "./dev-proxy";

const rootDir = path.dirname(fileURLToPath(import.meta.url));
const BASE = "/ui/";

/**
 * Prebuilt dependency files that are a single indivisible module larger than
 * Rollup's 500 kB advisory. No amount of `manualChunks` splits them — they
 * are one module each — so instead of bundling them we copy them into the
 * output as plain static assets and let the browser fetch them at runtime,
 * on the first call that actually needs them.
 *
 * Both are cold paths: the harfbuzz payload only runs when Excalidraw
 * subsets fonts for an export, and the Langium parser only runs when a
 * mermaid diagram is parsed. Neither belongs in the bundle graph.
 *
 * `dir` is content-hashed so an upgraded dependency cannot be served from a
 * stale cache. Siblings are emitted into the same directory under their
 * original basenames, which keeps the relative imports inside these files
 * working untouched.
 */
type ExternalVendorModule = {
  /** Absolute path of the module to externalize. */
  file: string;
  /** Extra files the module imports relatively; copied alongside it. */
  siblings: string[];
  /** Output directory prefix under `dist/vendor/`. */
  name: string;
};

function resolveExternalVendorModules(): ExternalVendorModule[] {
  const modules: Array<{ name: string; from: string; siblings: string[] }> = [
    {
      // harfbuzz WASM, base64-inlined: 1.82 MB in one module.
      name: "excalidraw-font-subset",
      from: "@excalidraw/excalidraw/dist/prod/chunk-EIO257PC.js",
      // Stateless esbuild helper shims; duplicating them is harmless.
      siblings: ["@excalidraw/excalidraw/dist/prod/chunk-SRAX5OIU.js"],
    },
    {
      // Langium grammar runtime: 1.32 MB in one module, no imports.
      name: "mermaid-parser-core",
      from: "mermaid/node_modules/@mermaid-js/parser/dist/chunks/mermaid-parser.core/chunk-FOHPRMQF.mjs",
      siblings: [],
    },
  ];

  const resolved: ExternalVendorModule[] = [];
  for (const entry of modules) {
    const file = path.resolve(rootDir, "node_modules", entry.from);
    // A dependency upgrade renames these hashed files. Skip silently rather
    // than failing the build; the chunk simply gets bundled again and the
    // size advisory comes back to tell us to refresh the path.
    if (!fs.existsSync(file)) continue;
    resolved.push({
      file: fs.realpathSync(file),
      name: entry.name,
      siblings: entry.siblings
        .map((sibling) => path.resolve(rootDir, "node_modules", sibling))
        .filter((sibling) => fs.existsSync(sibling))
        .map((sibling) => fs.realpathSync(sibling)),
    });
  }
  return resolved;
}

/** Resolve symlinks so a linked `node_modules` still matches by identity. */
function realpath(file: string): string {
  try {
    return fs.realpathSync(file);
  } catch {
    return file;
  }
}

function externalizeOversizedVendorModules(): Plugin {
  const modules = resolveExternalVendorModules();
  /** absolute file path -> public URL to import it from */
  const publicUrls = new Map<string, string>();
  /** output fileName -> absolute source path */
  const emitted = new Map<string, string>();

  for (const entry of modules) {
    const source = fs.readFileSync(entry.file);
    const hash = createHash("sha256").update(source).digest("hex").slice(0, 8);
    const dir = `vendor/${entry.name}-${hash}`;
    for (const file of [entry.file, ...entry.siblings]) {
      emitted.set(`${dir}/${path.basename(file)}`, file);
    }
    publicUrls.set(entry.file, `${BASE}${dir}/${path.basename(entry.file)}`);
  }

  return {
    name: "ua:externalize-oversized-vendor-modules",
    apply: "build",
    enforce: "pre",

    async resolveId(source, importer, options) {
      if (!importer || publicUrls.size === 0) return null;
      const resolved = await this.resolve(source, importer, {
        ...options,
        skipSelf: true,
      });
      if (!resolved) return null;
      const url = publicUrls.get(realpath(resolved.id));
      return url ? { id: url, external: true } : null;
    },

    generateBundle() {
      for (const [fileName, file] of emitted) {
        this.emitFile({ type: "asset", fileName, source: fs.readFileSync(file) });
      }
    },
  };
}

/**
 * Split the heaviest prebuilt vendor files into chunks of their own.
 *
 * The Canvas host already dynamic-imports Excalidraw (`src/lib/canvas/ExcalidrawHost.tsx`),
 * so what stays over the 500 kB advisory is entirely third-party: Excalidraw's
 * prebuilt dist and the mermaid core that `@excalidraw/mermaid-to-excalidraw`
 * pulls in. Those ship as a handful of large pre-minified files that Rollup
 * links statically and therefore cannot split on its own.
 *
 * This function only ever *separates* modules that would otherwise be merged.
 * It deliberately does not group a whole library into one named chunk —
 * doing that defeats the library's own dynamic-import boundaries and makes
 * things worse (grouping all of `mermaid/` produced a single 1.81 MB chunk
 * where the natural split was several sub-300 kB ones).
 *
 * Headroom warning: `excalidraw-index` lands at ~496 kB, and 502 kB of that is
 * Excalidraw's own prebuilt `dist/prod/index.js` — a single module. The next
 * Excalidraw release that grows it will trip the advisory again, and the fix
 * then is to externalize it (see `resolveExternalVendorModules`), not to raise
 * the limit.
 */
function manualVendorChunk(id: string): string | undefined {
  if (!id.includes("node_modules")) return undefined;

  // React and its renderer are on every path; one shared chunk rather than a
  // copy folded into the entry.
  if (
    /node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id) &&
    !id.includes("react-dom/server")
  ) {
    return "react-vendor";
  }

  // Excalidraw's prebuilt dist is a handful of large statically-linked files;
  // one chunk each instead of a single 1.13 MB blob.
  const excalidraw =
    /node_modules[\\/]@excalidraw[\\/]excalidraw[\\/]dist[\\/]prod[\\/](.+)\.js$/.exec(id);
  if (excalidraw) return `excalidraw-${path.basename(excalidraw[1])}`;

  // mermaid core's shared prebuilt chunks, likewise. Diagram-specific files
  // are left alone — mermaid already dynamic-imports those one per diagram
  // type, and naming them here would merge their dependencies back together.
  const mermaidCore =
    /node_modules[\\/]mermaid[\\/]dist[\\/]chunks[\\/]mermaid\.core[\\/](chunk-.+)\.mjs$/.exec(id);
  if (mermaidCore) return `mermaid-core-${path.basename(mermaidCore[1])}`;

  // Large single-purpose libraries that would otherwise be absorbed into
  // whichever chunk happens to reach them first.
  // Excalidraw's own UI dependency stack. Kept together and kept out of
  // `excalidraw-index`, whose single prebuilt source file is already 502 kB
  // and has no room left under the advisory.
  if (
    /node_modules[\\/](@radix-ui[\\/]|jotai|jotai-scope|tslib|lodash\.|@excalidraw[\\/]laser-pointer)/.test(
      id,
    )
  ) {
    return "excalidraw-ui-vendor";
  }

  if (/node_modules[\\/]pako[\\/]/.test(id)) return "pako";
  if (/node_modules[\\/]@floating-ui[\\/]/.test(id)) return "floating-ui";
  if (/node_modules[\\/]cytoscape[\\/]/.test(id)) return "cytoscape";
  if (/node_modules[\\/]dompurify[\\/]/.test(id)) return "dompurify";

  return undefined;
}

/**
 * Warnings that carry no action for us, suppressed narrowly so the build stays
 * readable and a real warning cannot hide in the noise.
 *
 * - `UNUSED_EXTERNAL_IMPORT` against `dist/vendor/…`: Rollup cannot see inside
 *   an external module, so it reports every name imported from one as unused.
 *   These imports are real and resolve at runtime.
 * - `INVALID_ANNOTATION` inside `node_modules`: a dependency's own
 *   `@__PURE__` comment sits where Rollup will not read it. Not our source and
 *   not fixable here.
 *
 * Anything else — including either of these in our own `src/` — is passed
 * through and must be fixed rather than muted.
 */
function onBuildWarning(
  warning: { code?: string; exporter?: string; id?: string; loc?: { file?: string } },
  warn: (warning: unknown) => void,
): void {
  if (
    warning.code === "UNUSED_EXTERNAL_IMPORT" &&
    warning.exporter?.startsWith(`${BASE}vendor/`)
  ) {
    return;
  }
  const source = warning.id ?? warning.loc?.file ?? "";
  if (warning.code === "INVALID_ANNOTATION" && source.includes("node_modules")) return;
  warn(warning);
}

/**
 * Dev-only proxy to an explicit hub (`UA_DEV_HUB_URL`, task #1579); see
 * `dev-proxy.ts`. The guard runs on the resolved config, so a `--host` flag
 * cannot expose the proxied hub session to the network.
 */
const hubProxy = devHubProxy(process.env.UA_DEV_HUB_URL);

function loopbackOnlyHubProxy(): Plugin {
  return {
    name: "ua-loopback-only-hub-proxy",
    apply: "serve",
    configResolved(config) {
      assertLoopbackDevServer(config.server.host, hubProxy);
    },
  };
}

export default defineConfig({
  base: BASE,
  plugins: [
    loopbackOnlyHubProxy(),
    externalizeOversizedVendorModules(),
    react(),
    tailwindcss(),
  ],
  resolve: {
    dedupe: ["react", "react-dom"],
    alias: {
      "@": path.resolve(rootDir, "src"),
    },
  },
  server: {
    host: LOOPBACK_HOST,
    proxy: hubProxy,
    fs: {
      allow: [searchForWorkspaceRoot(process.cwd())],
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    emptyOutDir: true,
    rollupOptions: {
      onwarn: onBuildWarning,
      output: {
        manualChunks: manualVendorChunk,
      },
    },
  },
});
