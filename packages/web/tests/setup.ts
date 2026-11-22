import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// Panes load through React.lazy, and Vitest transforms each pane module on its
// first import. On a loaded CI runner that first transform can outlast Testing
// Library's 1s default wait, so a pane that is merely slow reads as missing.
configure({ asyncUtilTimeout: 5000 });

// Node 25+ ships a global Web Storage whose `localStorage` is undefined unless
// the process was started with --localstorage-file, and that global shadows
// jsdom's. Node 22 (CI) has no such global, so tests passed there and failed
// on newer local Node. Put jsdom's own storage back whenever the global is
// missing, which is a no-op on runtimes where jsdom's storage already wins.
const dom = (globalThis as { jsdom?: { window: Window } }).jsdom;
for (const name of ["localStorage", "sessionStorage"] as const) {
  const current = (globalThis as Record<string, unknown>)[name] as
    Storage | undefined;
  if (dom && (!current || typeof current.clear !== "function")) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      get: () => dom.window[name],
    });
  }
}
