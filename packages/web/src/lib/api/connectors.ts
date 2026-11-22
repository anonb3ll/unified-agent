/**
 * Connector servers and packages (GET /api/connectors).
 * Re-exported from `hub.ts` (task #1575).
 */

/** Lifecycle label for a Horizon-2 server or package (task #1147). */
type ConnectorLifecycleStatus =
  "registered" | "authorized" | "dormant" | "revoked";

export type ConnectorServerRow = {
  server_id: string;
  connector: string;
  provenance?: string;
  scopes?: string[];
  reviewed_by?: string;
  review_ref?: string;
  revoked_at?: string | null;
  created_at?: string | null;
  active?: boolean;
  status: ConnectorLifecycleStatus | string;
  policy?: string;
  last_verified?: string | null;
  [key: string]: unknown;
};

/** ConnectorManifest shape (task #1204) — capabilities/secrets by NAME only. */
type ConnectorManifest = {
  name: string;
  version: string;
  capabilities: string[];
  required_secrets: string[];
  entry_points: Record<string, string>;
  default_enabled: boolean;
  horizon2_registration_supported?: boolean;
};

type ConnectorSecretStatus = {
  present: string[];
  missing: string[];
  satisfied: boolean;
  error?: string;
};

export type ConnectorPackageRow = {
  package: string;
  scopes?: string[];
  status: ConnectorLifecycleStatus | string;
  health?: string;
  health_detail?: string;
  health_checked_at?: string | null;
  health_cached_age_s?: number | null;
  policy?: string;
  last_verified?: string | null;
  /** Present only when a ConnectorManifest is registered for this package. */
  manifest?: ConnectorManifest;
  secrets?: ConnectorSecretStatus;
  [key: string]: unknown;
};

export type ConnectorDoctorCheck = {
  package: string;
  capability?: string;
  status: ConnectorLifecycleStatus | string;
  health?: string;
  health_checked_at?: string | null;
  health_cached_age_s?: number | null;
  detail?: string;
  gate?: string;
};
