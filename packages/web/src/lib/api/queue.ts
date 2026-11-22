/**
 * Queue health and backpressure.
 * Re-exported from `hub.ts` (task #1575).
 */
import { QueueHealth } from "@unified-agent/client";
import type { QueueHealth as QueueHealthData } from "@unified-agent/client";
export { QueueHealth } from "@unified-agent/client";
export type {
  QueueHealth as QueueHealthType,
  QueueHealthCounts,
  QueueHealthTask,
} from "@unified-agent/client";
export type { UnifiedAgentClientOptions } from "@unified-agent/client";

/** TanStack Query key for GET /api/queue-health. */
export const QUEUE_HEALTH_QUERY_KEY = ["queue-health"] as const;

/**
 * Load queue-health via same-origin cookies (EventSource/SPA path).
 * Parses with the shared Zod schema — no parallel classification API.
 */
/**
 * Queue backpressure (task #1430): faults in the claim machinery (`signals`)
 * and backlog shape (`notes`) from the hub's queue_pressure read. Parsed here,
 * not in @unified-agent/client.
 */
type QueueBackpressure = {
  level: "ok" | "pressure" | "unknown";
  signals: string[];
  notes: string[];
  error?: string;
};

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string")
    : [];
}

function normalizeBackpressure(raw: unknown): QueueBackpressure | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const level =
    r.level === "ok" || r.level === "pressure" ? r.level : "unknown";
  const out: QueueBackpressure = {
    level,
    signals: strings(r.signals),
    notes: strings(r.notes),
  };
  if (typeof r.error === "string") out.error = r.error;
  return out;
}

export type QueueHealthWithPressure = QueueHealthData & {
  backpressure: QueueBackpressure | null;
};

export async function fetchQueueHealth(
  signal?: AbortSignal,
): Promise<QueueHealthWithPressure> {
  const response = await fetch("/api/queue-health", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `queue-health HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload: unknown = await response.json();
  const backpressure =
    payload && typeof payload === "object"
      ? normalizeBackpressure((payload as Record<string, unknown>).backpressure)
      : null;
  return { ...QueueHealth.parse(payload), backpressure };
}
