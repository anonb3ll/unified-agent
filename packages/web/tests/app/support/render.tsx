import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, type RenderResult } from "@testing-library/react";
import { RouterProvider } from "react-router";
import { createTestRouter } from "../../../src/app/router";
import { createEpochHost, EpochHostProvider } from "../../../src/app/lib/epoch";
import { ThemeProvider } from "../../../src/lib/theme/ThemeProvider";
import { approvalsFixture, attentionFixture, whoamiFixture } from "./fixtures";
import { mockFetch, type MockHandler } from "./fetchMock";

export function testQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } } });
}

/** The reads the shell makes on every page, answered with calm defaults. */
export function shellHandlers(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  return {
    "GET /api/whoami": { body: whoamiFixture() },
    "GET /api/attention": { body: attentionFixture(2) },
    "GET /api/approvals": { body: approvalsFixture() },
    ...overrides,
  };
}

export type RenderRouteOptions = {
  /** Extra or replacement fetch handlers ("METHOD /path"). */
  hub?: Record<string, MockHandler>;
  queryClient?: QueryClient;
};

/**
 * Render Control Center v2 at an app path ("/work/goals") over a memory
 * router with a fresh QueryClient and a mocked hub.
 */
export function renderRoute(path: string, options: RenderRouteOptions = {}) {
  const queryClient = options.queryClient ?? testQueryClient();
  const hub = mockFetch(shellHandlers(options.hub));
  const router = createTestRouter(path);
  const host = createEpochHost(queryClient);
  const result: RenderResult = render(
    <QueryClientProvider client={queryClient}>
      <EpochHostProvider host={host}>
        <ThemeProvider>
          <RouterProvider router={router} />
        </ThemeProvider>
      </EpochHostProvider>
    </QueryClientProvider>,
  );
  return {
    ...result,
    router,
    /** The root client: whoami only. Set whoami here to change the owner. */
    queryClient,
    hub,
    /** The owner epoch the page runs in now (its storage, its signal). */
    epoch: () => host.current,
    /** The current epoch's QueryClient, which holds every app read. */
    appClient: () => host.current.queryClient,
  };
}

/**
 * Wait for the page heading (`h1`) with this name. Areas load lazily and the
 * first import of an area module is transformed on demand, so allow longer
 * than Testing Library's default on a loaded machine.
 */
export function findPageHeading(name: string | RegExp) {
  return screen.findByRole("heading", { level: 1, name }, { timeout: 12_000 });
}

/** Render a primitive with a QueryClient (for components that toast or query). */
export function renderUi(ui: ReactElement) {
  const queryClient = testQueryClient();
  return { ...render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>), queryClient };
}
