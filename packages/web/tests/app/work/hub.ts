import type { MockHandler } from "../support/fetchMock";
import { JOINT_REFUSED, WORK_GRAPH_7, WORK_GRAPH_ROOT, WORK_READS } from "./fixtures";

/** Fetch handlers for every Work read, overridable per test. */
export function workHub(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  const reads = Object.fromEntries(Object.entries(WORK_READS).map(([key, body]) => [key, { body } as MockHandler]));
  return {
    ...reads,
    "GET /api/plans/3/joint": { status: 400, body: JOINT_REFUSED },
    // Root goals without goal_id, the subtree with goal_id=7, as routes.work_graph_view answers.
    "GET /api/work-graph": ({ url }) => ({ body: new URL(url, "http://x").searchParams.get("goal_id") === "7" ? WORK_GRAPH_7 : WORK_GRAPH_ROOT }),
    ...overrides,
  };
}

/** The hub's parked-approval answer (routes._approval_pending). */
export const PARKED = (tool: string, approvalId = 77): MockHandler => ({
  status: 202,
  body: { approval_id: approvalId, code: "approval_required", status: "pending", tool },
});
