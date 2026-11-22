/**
 * Runs area routes (spec §3, §7, §9.6): overview, workflows, graph runs,
 * routines, cycles, and the history pages that replace the legacy panes.
 */
import type { RouteObject } from "react-router";
import type { PaletteCommand } from "../lib/commands";
import type { RouteHandle } from "../lib/routeHandle";
import { CyclePage } from "./CyclePage";
import { CyclesPage } from "./CyclesPage";
import { GraphsPage } from "./GraphsPage";
import { ExecutorPage, ReceiptsPage, TelemetryPage } from "./HistoryPages";
import { InsightsPage } from "./InsightsPage";
import { OverviewPage } from "./OverviewPage";
import { RoutinesPage } from "./RoutinesPage";
import { WorkflowsPage } from "./WorkflowsPage";

/** Mounted under the area path by `src/app/router.tsx`. */
export const routes: RouteObject[] = [
  { index: true, Component: OverviewPage },
  { path: "workflows", Component: WorkflowsPage, handle: { crumb: "Workflows" } satisfies RouteHandle },
  { path: "graphs", Component: GraphsPage, handle: { crumb: "Graphs" } satisfies RouteHandle },
  { path: "routines", Component: RoutinesPage, handle: { crumb: "Routines" } satisfies RouteHandle },
  { path: "cycles", Component: CyclesPage, handle: { crumb: "Cycles" } satisfies RouteHandle },
  { path: "cycles/:id", Component: CyclePage, handle: { crumb: ({ id }) => `Cycle ${id ?? ""}` } satisfies RouteHandle },
  { path: "receipts", Component: ReceiptsPage, handle: { crumb: "Receipts" } satisfies RouteHandle },
  { path: "telemetry", Component: TelemetryPage, handle: { crumb: "Telemetry" } satisfies RouteHandle },
  { path: "insights", Component: InsightsPage, handle: { crumb: "Insights" } satisfies RouteHandle },
  { path: "executor", Component: ExecutorPage, handle: { crumb: "Executor" } satisfies RouteHandle },
];

/**
 * Command palette entries for this area: actions only. The shell already
 * lists every Runs page by its title, so page jumps are not repeated here.
 */
export const commands: PaletteCommand[] = [
  { id: "runs.running", label: "What is running now", group: "Runs", to: "/runs", keywords: ["active", "live", "recent"] },
  { id: "runs.workflow.run", label: "Run a workflow", group: "Runs", to: "/runs/workflows", keywords: ["steps", "start"] },
  { id: "runs.graph.start", label: "Start a graph run", group: "Runs", to: "/runs/graphs", keywords: ["langgraph", "checkpoint", "continue", "resume"] },
  { id: "runs.routine.create", label: "Schedule recurring work", group: "Runs", to: "/runs/routines", keywords: ["cron", "schedule", "run now"] },
  { id: "runs.cycle.start", label: "Start a cycle", group: "Runs", to: "/runs/cycles", keywords: ["autonomous", "loop", "budget", "pause", "stop"] },
  { id: "runs.lineage", label: "See how a task was carried out", group: "Runs", to: "/runs/insights", keywords: ["lineage", "flow", "cycle time", "lead time"] },
];
