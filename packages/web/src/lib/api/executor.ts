/**
 * Executor delegations (GET /api/executor).
 * Re-exported from `hub.ts` (task #1575).
 */
import type { DoctorCheck } from "./health";

export type DelegationRow = {
  id?: number | string;
  task_id?: number | null;
  from_agent?: string | null;
  to_agent?: string | null;
  status?: string | null;
  deadline?: string | null;
  [key: string]: unknown;
};

export type ExecutorDelegationPayload = {
  delegations: DelegationRow[];
  recent_failures: DelegationRow[];
  recent_sessions?: Array<{
    id?: string;
    agent?: string | null;
    kind?: string | null;
    status?: string | null;
    model?: string | null;
    task_id?: number | null;
    [key: string]: unknown;
  }>;
  stale_worktrees: Array<Record<string, unknown>>;
  counts?: Record<string, number>;
  health?: { check?: DoctorCheck };
  error?: string;
  errors?: string[];
};
