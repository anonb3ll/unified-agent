/**
 * Work area routes (spec §3, §7, §9.2): tasks in four views, the task page,
 * goals, plans and the planning canvas.
 */
import type { RouteObject } from "react-router";
import type { PaletteCommand } from "../lib/commands";
import type { RouteHandle } from "../lib/routeHandle";
import { CanvasPage } from "./CanvasPage";
import { GoalPage, GoalsPage } from "./GoalsPages";
import { PlanNewPage, PlanPage, PlansPage } from "./PlansPages";
import { TaskNewPage } from "./TaskNewPage";
import { TaskPage } from "./TaskPage";
import { TasksPage } from "./TasksPage";

/** Mounted under the area path by `src/app/router.tsx`. */
export const routes: RouteObject[] = [
  { index: true, Component: TasksPage },
  { path: "tasks/new", Component: TaskNewPage, handle: { crumb: "New task" } satisfies RouteHandle },
  { path: "tasks/:id", Component: TaskPage, handle: { crumb: ({ id }) => `Task ${id ?? ""}` } satisfies RouteHandle },
  { path: "goals", Component: GoalsPage, handle: { crumb: "Goals" } satisfies RouteHandle },
  { path: "goals/:id", Component: GoalPage, handle: { crumb: ({ id }) => `Goal ${id ?? ""}` } satisfies RouteHandle },
  { path: "plans", Component: PlansPage, handle: { crumb: "Plans" } satisfies RouteHandle },
  { path: "plans/new", Component: PlanNewPage, handle: { crumb: "New plan" } satisfies RouteHandle },
  { path: "plans/:id", Component: PlanPage, handle: { crumb: ({ id }) => `Plan ${id ?? ""}` } satisfies RouteHandle },
  { path: "canvas", Component: CanvasPage, handle: { crumb: "Planning canvas" } satisfies RouteHandle },
];

/** Command palette entries for this area. */
export const commands: PaletteCommand[] = [
  { id: "work.create-task", label: "Create a task", group: "Work", to: "/work/tasks/new", keywords: ["new", "add"], shortcut: "c" },
  { id: "work.list", label: "Task list", group: "Work", to: "/work", keywords: ["tasks", "queue"] },
  { id: "work.board", label: "Task board", group: "Work", to: "/work?view=board", keywords: ["kanban", "columns"] },
  { id: "work.tree", label: "Task tree", group: "Work", to: "/work?view=tree", keywords: ["subtasks", "parent"] },
  { id: "work.graph", label: "Task dependency graph", group: "Work", to: "/work?view=graph", keywords: ["dependencies", "blocks"] },
  { id: "work.work-graph", label: "Work graph: goals to tasks", group: "Work", to: "/work?view=graph&scope=work", keywords: ["goals", "plans"] },
  { id: "work.mine", label: "My tasks", group: "Work", to: "/work?mine=1", keywords: ["claimed", "assigned"] },
  { id: "work.review", label: "Tasks waiting for review", group: "Work", to: "/work?review=1", keywords: ["review"] },
  { id: "work.goals", label: "Goals", group: "Work", to: "/work/goals", keywords: ["why", "outcomes"] },
  { id: "work.plans", label: "Plans", group: "Work", to: "/work/plans", keywords: ["steps", "checkpoints"] },
  { id: "work.create-plan", label: "Create a plan", group: "Work", to: "/work/plans/new", keywords: ["new"] },
  { id: "work.canvas", label: "Planning canvas", group: "Work", to: "/work/canvas", keywords: ["excalidraw", "draw", "sketch"] },
];
