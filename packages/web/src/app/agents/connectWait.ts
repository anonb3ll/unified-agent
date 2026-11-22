/**
 * The add page's live check (task #2539): wait for a new agent's first
 * connection with the hub's long-poll (`GET /api/agents/{label}?wait_for=
 * connected&timeout=25&since=<issued_at>`, `docs/control-center-api.md`)
 * instead of reading the detail every 5 s.
 *
 * The loop asks again as soon as a held read times out (`wait.satisfied:
 * false`), stops on the first satisfied answer, and backs off with jitter on
 * a 429 (the hub's 16 waiters are taken), a 204, a 404, any other refusal
 * and any network failure; only a refusal other than 429 or 404, or a
 * network failure, shows the check as not known. The read in flight is
 * aborted when the component unmounts and the moment the owner epoch is
 * revoked; nothing is set after.
 */
import { useEffect, useState } from "react";
import { useEpoch } from "../lib/epoch";
import { fetchAgentWait, HubReadError, type AgentDetail } from "./api";

/** Fewest milliseconds between two held reads that came back unsatisfied. */
export const CONNECT_WAIT_MIN_GAP_MS = 1_000;
/** First retry window after a failure; doubles per failure in a row. */
export const CONNECT_WAIT_BASE_MS = 1_000;
/** Largest retry window. */
export const CONNECT_WAIT_MAX_MS = 30_000;

/**
 * Delay before the next read after `failures` failed reads in a row: a
 * window of `CONNECT_WAIT_BASE_MS * 2^(failures-1)` capped at
 * `CONNECT_WAIT_MAX_MS`, of which the second half is random ("equal
 * jitter"), so tabs that were refused together do not come back together.
 */
export function connectRetryDelay(failures: number, random: () => number = Math.random): number {
  const window = Math.min(CONNECT_WAIT_MAX_MS, CONNECT_WAIT_BASE_MS * 2 ** Math.max(0, failures - 1));
  return Math.round(window / 2 + (random() * window) / 2);
}

export type ConnectWaitState = {
  /** `unknown`: the last read failed (other than a 429); the loop keeps asking. */
  status: "waiting" | "connected" | "unknown";
  detail: AgentDetail | null;
};

/** Resolves after `ms`, or at once when `signal` aborts. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted || ms <= 0) {
      resolve();
      return;
    }
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done);
  });
}

/**
 * Wait for `label`'s first sighting at or after `since` (the add answer's
 * `issued_at`; null counts any sighting). Runs while mounted, inside the
 * epoch it mounted in.
 */
export function useAgentConnectWait(label: string, since: string | null): ConnectWaitState {
  const epoch = useEpoch();
  const [state, setState] = useState<ConnectWaitState>({ status: "waiting", detail: null });

  useEffect(() => {
    if (epoch.signal.aborted) return;
    const controller = new AbortController();
    const stop = () => controller.abort();
    epoch.signal.addEventListener("abort", stop);
    const { signal } = controller;
    const set = (next: ConnectWaitState) => {
      if (!signal.aborted) epoch.run(() => setState(next));
    };

    void (async () => {
      let failures = 0;
      while (!signal.aborted) {
        const started = Date.now();
        try {
          const answer = await fetchAgentWait(label, { since }, signal);
          if (signal.aborted) return;
          if (answer?.wait.satisfied) {
            set({ status: "connected", detail: answer.detail });
            return;
          }
          if (answer) {
            failures = 0;
            set({ status: "waiting", detail: answer.detail });
            // A held read normally takes the whole timeout; one that came
            // back early is not asked again at once.
            await pause(CONNECT_WAIT_MIN_GAP_MS - (Date.now() - started), signal);
            continue;
          }
          // 204: the hub saw this client go away. Ask again, calmly.
          failures += 1;
        } catch (error) {
          if (signal.aborted) return;
          failures += 1;
          // A 429 means the hub is busy, not that the check failed; a 404
          // (no seat and never seen yet) is still "not connected", as the
          // plain read treats it.
          const stillWaiting = error instanceof HubReadError && (error.status === 429 || error.status === 404);
          if (!stillWaiting) set({ status: "unknown", detail: null });
        }
        await pause(connectRetryDelay(failures), signal);
      }
    })();

    return () => {
      epoch.signal.removeEventListener("abort", stop);
      controller.abort();
    };
  }, [epoch, label, since]);

  return state;
}
