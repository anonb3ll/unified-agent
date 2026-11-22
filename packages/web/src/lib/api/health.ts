/**
 * Health and doctor summary (GET /api/health).
 * Re-exported from `hub.ts` (task #1575).
 */

/** system_doctor check row from GET /api/health. */
export type DoctorCheck = {
  capability?: string;
  check?: string;
  status?: string;
  detail?: string | null;
  gate?: string | null;
  [key: string]: unknown;
};

export type HealthSummary = {
  live?: number;
  dormant?: number;
  degraded?: number;
  misconfigured?: number;
  unknown?: number;
  [key: string]: unknown;
};

export type HealthPayload = {
  checks: DoctorCheck[];
  summary: HealthSummary;
  /** Seconds since the probes behind this report ran (hub and pane caches combined). */
  cached_age_s?: number | null;
  /** When the run behind this report finished (ISO). */
  cached_at?: string | null;
  /** Older than the hub's fresh TTL; served while a background run replaces it. */
  stale?: boolean;
  /** A background run is replacing this report on the hub (task #1897). */
  refreshing?: boolean;
  /** The last background run failed; this is the last good report. */
  refresh_error?: { at?: string; error?: string } | null;
  error?: string;
};
