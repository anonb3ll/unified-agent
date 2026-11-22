/**
 * `POST /api/action/langgraph/run` through `useHubWrite`.
 *
 * A native graph approval answers 202 WITH the `run_id` the run is continued
 * with (`webui/cc_runs.langgraph_run`). That body arrives on the
 * `pending_approval` outcome; when it carries a `run_id` the run exists and is
 * reported as `graph_paused`. A 202 without one is the wrapper's approval gate
 * parking the call before any run started: an ordinary parked write.
 *
 * Never stored for replay (`resumable: false`): repeating the first request
 * starts a new run; a paused run is continued by its `run_id`.
 */
import { useCallback } from "react";
import type { QueryKey } from "@tanstack/react-query";
import { useHubWrite, type WriteOutcome } from "../lib/mutations";
import { WRITE_PATHS, type GraphRunResult } from "./api";

export type GraphRunBody = { task_id: number; query: string; run_id?: string; engine?: "workflow" };

export type GraphOutcome = WriteOutcome<GraphRunResult> | { kind: "graph_paused"; result: GraphRunResult; approvalId: string | null };

export function useGraphRun(invalidates: QueryKey[]) {
  const write = useHubWrite<GraphRunBody, GraphRunResult>({
    path: WRITE_PATHS.langgraphRun,
    label: "The graph run",
    invalidates,
    resumable: false,
  });
  const { run: send } = write;
  const run = useCallback(
    async (body: GraphRunBody): Promise<GraphOutcome | null> => {
      const outcome = await send(body);
      if (outcome.kind === "pending_approval") {
        const parked = outcome.body && typeof outcome.body === "object" ? (outcome.body as GraphRunResult) : null;
        if (parked && typeof parked.run_id === "string" && parked.run_id) {
          return { kind: "graph_paused", result: parked, approvalId: outcome.approvalId };
        }
      }
      return outcome;
    },
    [send],
  );
  return { run, running: write.isRunning };
}
