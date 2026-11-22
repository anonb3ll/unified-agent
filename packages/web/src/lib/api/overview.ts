/**
 * Projects, workflows and the overview payload.
 * Re-exported from `hub.ts` (task #1575).
 */
import { QueueHealth } from "@unified-agent/client";
import type { AgentsPayload } from "../sessions/types";
import type { ApprovalsPayload } from "./approvals";
import type { QueueHealth as QueueHealthData } from "@unified-agent/client";

/** Project registry row from GET /api/projects. */
type ProjectRow = {
  project: string;
  label: string;
  repo_path?: string | null;
  checked_out?: boolean;
};

type ProjectsPayload = {
  projects: ProjectRow[];
  error?: string;
};

/** TanStack Query key for GET /api/projects. */
export const PROJECTS_QUERY_KEY = ["projects"] as const;

export async function fetchProjects(
  signal?: AbortSignal,
): Promise<ProjectsPayload> {
  const response = await fetch("/api/projects", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `projects HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as ProjectsPayload;
  return {
    projects: Array.isArray(payload.projects) ? payload.projects : [],
    error: payload.error,
  };
}

/** Workflow definition from overview.workflows / workflow_list. */
type WorkflowDef = {
  name: string;
  description?: string;
  steps?: string[];
  inputs?: string[];
};

type WorkflowsPayload = {
  workflows: WorkflowDef[];
  error?: string;
};

/** Active-task slice used by Action Deck / Dispatch parent pickers. */
export type OverviewTask = {
  id: number;
  title?: string | null;
  status?: string | null;
  project?: string | null;
  claimed_by?: string | null;
};

/**
 * Slim overview for Dispatch Studio + Action Deck (agents, projects, queue,
 * workflows, approvals). Full /api/overview is large; we normalize the fields
 * these panes need and ignore the rest.
 */
type OverviewPayload = {
  agents: AgentsPayload;
  projects: ProjectRow[];
  queue_health?: QueueHealthData | null;
  workflows: WorkflowsPayload;
  approvals: ApprovalsPayload;
  tasks?: { active?: OverviewTask[]; error?: string };
  error?: string;
};

/** TanStack Query key for GET /api/overview (operations panes). */
export const OVERVIEW_QUERY_KEY = ["overview"] as const;

export async function fetchOverview(
  signal?: AbortSignal,
): Promise<OverviewPayload> {
  const response = await fetch("/api/overview", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `overview HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as Record<string, unknown>;
  const agentsRaw = (payload.agents ?? {}) as AgentsPayload;
  const projectsRaw = payload.projects;
  const workflowsRaw = (payload.workflows ?? {}) as WorkflowsPayload;
  const approvalsRaw = (payload.approvals ?? {}) as ApprovalsPayload;
  const tasksRaw = (payload.tasks ?? {}) as {
    active?: OverviewTask[];
    error?: string;
  };

  let projects: ProjectRow[] = [];
  if (Array.isArray(projectsRaw)) {
    projects = projectsRaw as ProjectRow[];
  } else if (
    projectsRaw &&
    typeof projectsRaw === "object" &&
    Array.isArray((projectsRaw as ProjectsPayload).projects)
  ) {
    projects = (projectsRaw as ProjectsPayload).projects;
  }

  return {
    agents: {
      agents: Array.isArray(agentsRaw.agents) ? agentsRaw.agents : [],
      counts:
        agentsRaw.counts && typeof agentsRaw.counts === "object"
          ? agentsRaw.counts
          : {},
      error: agentsRaw.error,
    },
    projects,
    queue_health:
      payload.queue_health == null ? null : QueueHealth.parse(payload.queue_health),
    workflows: {
      workflows: Array.isArray(workflowsRaw.workflows)
        ? workflowsRaw.workflows
        : [],
      error: workflowsRaw.error,
    },
    approvals: {
      pending: Array.isArray(approvalsRaw.pending) ? approvalsRaw.pending : [],
      approved_unconsumed: Array.isArray(approvalsRaw.approved_unconsumed)
        ? approvalsRaw.approved_unconsumed
        : [],
      expired_passes: Array.isArray(approvalsRaw.expired_passes)
        ? approvalsRaw.expired_passes
        : [],
      gated_tools: Array.isArray(approvalsRaw.gated_tools)
        ? approvalsRaw.gated_tools
        : [],
      pass_ttl_hours: approvalsRaw.pass_ttl_hours,
      error: approvalsRaw.error,
      errors: approvalsRaw.errors,
    },
    tasks: {
      active: Array.isArray(tasksRaw.active) ? tasksRaw.active : [],
      error: tasksRaw.error,
    },
  };
}
