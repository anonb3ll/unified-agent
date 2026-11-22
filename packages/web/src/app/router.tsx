import {
  createBrowserRouter,
  createMemoryRouter,
  Navigate,
  type PatchRoutesOnNavigationFunction,
  type RouteObject,
} from "react-router";
import type { PaletteCommand } from "./lib/commands";
import { APP_BASENAME } from "./lib/entry";
import type { RouteHandle } from "./lib/routeHandle";
import { DESTINATIONS, type DestinationId } from "./routes";
import { AppLayout } from "./shell/AppLayout";
import { NotFound } from "./shell/NotFound";
import { RouteError } from "./shell/RouteError";

/** What an area's `routes.tsx` exports (spec §7). */
export type AreaModule = {
  routes: RouteObject[];
  commands?: PaletteCommand[];
};

/**
 * Each destination's area, loaded on first visit. The integrations area is
 * mounted by `settings/routes.tsx` under `/settings/integrations`. Adding an
 * area page never edits this file: a slice changes only `<area>/routes.tsx`.
 */
export const AREA_LOADERS: Record<DestinationId, () => Promise<AreaModule>> = {
  home: () => import("./home/routes"),
  inbox: () => import("./inbox/routes"),
  work: () => import("./work/routes"),
  agents: () => import("./agents/routes"),
  sessions: () => import("./sessions/routes"),
  knowledge: () => import("./knowledge/routes"),
  runs: () => import("./runs/routes"),
  settings: () => import("./settings/routes"),
};

export const LAYOUT_ROUTE_ID = "app";

/** The static tree: the shell, a redirect from `/` and the not-found page. Areas are patched in. */
export function appRoutes(): RouteObject[] {
  return [
    {
      id: LAYOUT_ROUTE_ID,
      path: "/",
      Component: AppLayout,
      ErrorBoundary: RouteError,
      children: [
        { index: true, element: <Navigate to="/home" replace /> },
        { path: "*", Component: NotFound, handle: { crumb: "Page not found" } satisfies RouteHandle },
      ],
    },
  ];
}

/** First path segment after the basename: "/ui/work/goals" and "/work/goals" both give "work". */
export function areaOf(path: string): DestinationId | undefined {
  const rest = path.startsWith(`${APP_BASENAME}/`) ? path.slice(APP_BASENAME.length) : path;
  const segment = rest.split("/")[1] ?? "";
  return DESTINATIONS.find((d) => d.id === segment)?.id;
}

/**
 * Lazy areas over react-router's route discovery: the first navigation into
 * an area imports its `routes.tsx` and patches it under the shell, with the
 * destination label as its breadcrumb and an error boundary that keeps the
 * shell on screen. One patcher per router, so tests can build many routers.
 */
export function createAreaPatcher(
  loaders: Record<DestinationId, () => Promise<AreaModule>> = AREA_LOADERS,
): PatchRoutesOnNavigationFunction {
  const loaded = new Set<DestinationId>();
  return async ({ path, patch, signal }) => {
    const area = areaOf(path);
    if (!area || loaded.has(area)) return;
    const module = await loaders[area]();
    // A navigation cancelled while the area loaded (the person went elsewhere)
    // has its patch ignored by the router. Do not mark the area as loaded
    // then, or every later visit would skip discovery and show not-found.
    if (signal.aborted || loaded.has(area)) return;
    loaded.add(area);
    const destination = DESTINATIONS.find((d) => d.id === area);
    patch(LAYOUT_ROUTE_ID, [
      {
        id: `area-${area}`,
        path: area,
        handle: { crumb: destination?.label } satisfies RouteHandle,
        ErrorBoundary: RouteError,
        children: module.routes,
      },
    ]);
  };
}

export function createAppRouter() {
  return createBrowserRouter(appRoutes(), {
    basename: APP_BASENAME,
    patchRoutesOnNavigation: createAreaPatcher(),
  });
}

/** The same app over a memory history, for tests. */
export function createTestRouter(initialPath: string) {
  return createMemoryRouter(appRoutes(), {
    basename: APP_BASENAME,
    initialEntries: [`${APP_BASENAME}${initialPath}`],
    patchRoutesOnNavigation: createAreaPatcher(),
  });
}

/** Every area's palette commands (loads every area chunk; used when the palette opens). */
export async function loadAreaCommands(): Promise<PaletteCommand[]> {
  const modules = await Promise.allSettled([
    ...Object.values(AREA_LOADERS).map((load) => load()),
    import("./integrations/routes"),
  ]);
  return modules.flatMap((m) => (m.status === "fulfilled" ? (m.value.commands ?? []) : []));
}
