/**
 * Telemetry spans and exceptions.
 * Re-exported from `hub.ts` (task #1575).
 */

export type TelemetrySpan = {
  trace_id?: string | null;
  span_id?: string | null;
  kind?: string | null;
  outcome?: string | null;
  duration_ms?: number | null;
  agent?: string | null;
  task_id?: number | null;
  usage?: { tokens_total?: number | null; cost_usd?: number | null } | null;
  evidence_ref?: { ulid?: string | null } | null;
  [key: string]: unknown;
};

export type TelemetryException = {
  trace_id?: string | null;
  kind?: string | null;
  severity?: string | null;
  count?: number | null;
  message?: string | null;
  task_id?: number | null;
  evidence_ref?: { ulid?: string | null } | null;
  [key: string]: unknown;
};

export type TelemetryPayload = {
  waterfall: {
    traces?: unknown[];
    spans: TelemetrySpan[];
    summary?: {
      traces?: number;
      spans?: number;
      duration_ms?: number;
      tokens_total?: number;
      cost_usd?: number | null;
      [key: string]: unknown;
    };
  };
  exception_digest: {
    exceptions: TelemetryException[];
    total?: number;
    normal_spans_omitted?: number;
    scanned_spans?: number;
  };
  cursor?: string | null;
  source?: { channel?: string; event_count?: number };
  error?: string;
};
