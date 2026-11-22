/**
 * `mockHub` override tables for the Sessions Playwright specs: realistic
 * data, empty data and failing reads.
 */
import type { HubOverride } from "../support/mockHub";
import { AGENTS, CREW, eventsPayload, nodesPayload, OVERVIEW, ROUTES, SESSIONS, sessionDetail, sessionRow, sessionsPayload } from "./fixtures";

const json = (body: unknown, status = 200): HubOverride => ({ status, json: body });

export const REALISTIC: Record<string, HubOverride> = {
  // The list carries the wake state (sessions_payload); the detail does not.
  "GET /api/sessions": json(sessionsPayload([...SESSIONS.filter((s) => s.id !== "s-live"), sessionRow({ wake: { requested: true, granted: false } })])),
  "GET /api/sessions/s-live": json(sessionDetail()),
  "GET /api/sessions/s-live/crew": json(CREW),
  "GET /api/agents": json(AGENTS),
  "GET /api/nodes": json(nodesPayload()),
  "GET /api/events": json(eventsPayload()),
  "GET /api/overview": json(OVERVIEW),
  "GET /api/dispatch-routes": json(ROUTES),
  "GET /api/dispatch-stack": json({ next_task_id: 42, items: [{ task_id: 42, eligible: true, title: "Fix the login form" }] }),
  "GET /api/nodes/native-sessions": json({
    sessions: [
      {
        node: "desktop",
        harness: "claude",
        external_session_id: "abc",
        cwd: "/home/dev/app",
        last_activity: "2026-11-10T10:00:00Z",
        title: "Refactor auth",
        backends: ["acp:claude", "cli:claude"],
        resume_ready: true,
        adopted: null,
        adoptable: true,
        reason: null,
      },
    ],
  }),
  "GET /api/teams/snapshots": json({ snapshots: [{ snapshot_id: "snap-1", team: "Web team", taken_at: "2026-11-09T18:00:00Z", by: "operator", seats: 3 }] }),
};

export const EMPTY: Record<string, HubOverride> = {
  "GET /api/sessions": json(sessionsPayload([])),
  "GET /api/sessions/s-live": json(sessionDetail({ meta: {}, task_id: null, task_title: null, model: null })),
  "GET /api/sessions/s-live/crew": json({ error: "unknown session" }, 404),
  "GET /api/agents": json({ agents: [], counts: {} }),
  "GET /api/nodes": json({ nodes: [], recent_items: [] }),
  "GET /api/events": json({ events: [], seq_cursor: 0 }),
  "GET /api/overview": json({ ...OVERVIEW, agents: { agents: [], counts: {} }, projects: [], tasks: { active: [] } }),
  "GET /api/dispatch-routes": json({ routes: [], scheduler: { enabled: false, real_spawn: false } }),
  "GET /api/nodes/native-sessions": json({ sessions: [] }),
  "GET /api/teams/snapshots": json({ snapshots: [] }),
};

const fail = json({ error: "upstream unavailable" }, 503);
export const FAILING: Record<string, HubOverride> = Object.fromEntries(Object.keys(REALISTIC).map((key) => [key, fail]));
