import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { EntryBoundary } from "./app/EntryBoundary";
import { legacyRedirect } from "./app/lib/legacyRedirect";
import { takeReturnPath } from "./app/lib/returnTo";
import type { TaskStatusType } from "./lib/hub";
import { CALM_QUERY_DEFAULTS } from "./lib/polling";
import { ThemeProvider } from "./lib/theme/ThemeProvider";
// Self-hosted Geist (OFL): Vite bundles the woff2 subsets, so the UI makes no
// font request off the hub (design protocol §3.3).
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "./index.css";

// Keep a protocol type reference so CI typecheck fails if protocol/client drift.
const _statusProbe: TaskStatusType = "open";
void _statusProbe;

// Retry a failed read once with backoff, never poll a hidden tab (task #1850).
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { ...CALM_QUERY_DEFAULTS },
  },
});

// The app loads as its own chunk, so a chunk that fails to load (a deploy
// replaced it) shows the EntryBoundary instead of a blank page.
const App = lazy(() => import("./app/App"));
// The design-system primitives on one page, for their axe and keyboard specs.
const ShowcasePage = lazy(() => import("./lib/ui/ShowcasePage"));
const showcase =
  window.location.pathname.replace(/\/$/, "") === "/ui" &&
  new URLSearchParams(window.location.search).get("showcase") === "primitives";

// Before the router starts (spec §10): back from an off-site sign-in (WorkOS),
// return to the page that was asked for; otherwise rewrite a classic
// `/ui/?view=<id>` or `/ui/#<id>` link to its new path (spec §11).
if (!showcase) {
  const target = takeReturnPath(window.location.pathname) ?? legacyRedirect(window.location);
  if (target) window.history.replaceState(null, "", target);
}

const root = document.getElementById("root");
if (!root) {
  throw new Error("missing #root");
}

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <EntryBoundary>
          <Suspense fallback={null}>{showcase ? <ShowcasePage /> : <App />}</Suspense>
        </EntryBoundary>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
);
