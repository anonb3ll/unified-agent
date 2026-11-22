/**
 * Sessions area routes (spec §3, §7, §9.4): the session list, starting a
 * session (the legacy Dispatch pane) and one session's page.
 */
import type { RouteObject } from "react-router";
import type { PaletteCommand } from "../lib/commands";
import type { RouteHandle } from "../lib/routeHandle";
import { SessionPage } from "./SessionPage";
import { SessionsListPage } from "./SessionsListPage";
import { StartSessionPage } from "./StartSessionPage";

/** Mounted under the area path by `src/app/router.tsx`. */
export const routes: RouteObject[] = [
  { index: true, Component: SessionsListPage },
  { path: "new", Component: StartSessionPage, handle: { crumb: "Start a session" } satisfies RouteHandle },
  { path: ":id", Component: SessionPage, handle: { crumb: ({ id }) => `Session ${id ?? ""}` } satisfies RouteHandle },
];

/** Command palette entries for this area. */
export const commands: PaletteCommand[] = [
  { id: "sessions.new", label: "Start a session", group: "Sessions", to: "/sessions/new", keywords: ["dispatch", "launch", "run", "agent"] },
  { id: "sessions.open", label: "Open sessions", group: "Sessions", to: "/sessions?status=open", keywords: ["live", "running"] },
  { id: "sessions.all", label: "All sessions", group: "Sessions", to: "/sessions", keywords: ["history", "past"] },
  { id: "sessions.work", label: "Work sent to devices", group: "Sessions", to: "/sessions?tab=work", keywords: ["node", "queue", "device", "job"] },
  { id: "sessions.devices", label: "Sessions found on devices", group: "Sessions", to: "/sessions?tab=devices", keywords: ["adopt", "native", "claude code", "codex"] },
  { id: "sessions.teams", label: "Team snapshots", group: "Sessions", to: "/sessions?tab=teams", keywords: ["restore", "team"] },
  { id: "sessions.activity", label: "Live activity", group: "Sessions", to: "/sessions?tab=activity", keywords: ["events", "feed"] },
];
