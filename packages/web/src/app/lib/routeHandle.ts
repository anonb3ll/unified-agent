import type { Params } from "react-router";

/**
 * What a route tells the shell (spec §4): its breadcrumb text and, when it
 * differs, its document title. A function receives the route params, so
 * `/work/tasks/:id` can read "Task 42".
 */
export type RouteHandle = {
  crumb?: string | ((params: Params) => string);
  title?: string | ((params: Params) => string);
};

export function resolveText(value: RouteHandle["crumb"], params: Params): string | undefined {
  return typeof value === "function" ? value(params) : value;
}

export function isRouteHandle(handle: unknown): handle is RouteHandle {
  return typeof handle === "object" && handle !== null && ("crumb" in handle || "title" in handle);
}
