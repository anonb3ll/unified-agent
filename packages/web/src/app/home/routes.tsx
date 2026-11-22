/**
 * Home area (spec §9.1): what needs you, what is running now and what just
 * finished, scoped by project. Replaces the classic Home view (spec §11).
 */
import { type RouteObject } from "react-router";
import type { PaletteCommand } from "../lib/commands";
import { HomePage } from "./HomePage";

/** Mounted under the area path by `src/app/router.tsx`. */
export const routes: RouteObject[] = [{ index: true, Component: HomePage }];

/** Command palette entries for this area. */
export const commands: PaletteCommand[] = [
  { id: "home.needs", label: "What needs me", group: "Home", to: "/home", keywords: ["overview", "portfolio", "dashboard", "today"] },
  { id: "home.running", label: "What is running now", group: "Home", to: "/home", keywords: ["in flight", "sessions", "runs"] },
];
