/**
 * Devices, OAuth sign-ins and fleet actions.
 * Re-exported from `hub.ts` (task #1575).
 */
import type { HealthSummary } from "./health";

export type DeviceHost = {
  host?: string | null;
  host_source?: string | null;
  staleness?: string | null;
  as_of?: string | null;
  doctor_freshness?: string | null;
  doctor?: { summary?: HealthSummary } | null;
  executor?: {
    reported?: boolean;
    unit_status?: string | null;
    detail?: string | null;
    agent?: string | null;
    claim?: { task_id?: number | null } | null;
  } | null;
  worktrees?: {
    reported?: boolean;
    count?: number | null;
    stale?: number | null;
  } | null;
  version?: string | null;
  last_seen?: string | null;
  agents?: DeviceAgent[];
  surfaces?: DeviceSurface[];
  [key: string]: unknown;
};

/** One live OAuth refresh-token session from the hub's durable store (task #1321). */
type OAuthSession = {
  session_id: string;
  client_id?: string | null;
  agent?: string | null;
  auth_method?: string | null;
  device_node?: string | null;
  device_node_id?: string | null;
  issued_at?: number | null;
  expires_at?: number | null;
};

export type OAuthDeviceGroup = {
  device_node?: string | null;
  device_node_id?: string | null;
  bound: boolean;
  sessions: OAuthSession[];
};

export type OAuthSessionsPayload = {
  devices: OAuthDeviceGroup[];
  session_count: number;
  oauth_enabled?: boolean;
  error?: string;
};

/** Credential metadata for one agent (task #1388) — ids and hash prefixes, never values. */
export type AgentCredential = {
  issued_token_ids: number[];
  fingerprints: string[];
  source: "lifecycle" | "static" | "none";
};

/** An agent attached to a host row (task #1388). */
type DeviceAgent = {
  label?: string | null;
  kind?: string | null;
  status?: string | null;
  last_seen?: string | null;
  active_tasks?: number;
  host_source?: string | null;
  credential?: AgentCredential;
};

/** A surface a ua-node reported, mapped to an agent without guessing (task #1391). */
type DeviceSurface = {
  kind: "hermes" | "kanban";
  name: string;
  agent: string | null;
  mapping: "explicit" | "exact" | "unmapped";
};

/** One agent's resolved placement (fleet_payload()["agents"], task #1388). */
type FleetAgent = {
  label: string;
  host: string | null;
  host_source: string | null;
  unplaced_host?: string | null;
  status?: string | null;
  credential?: AgentCredential;
};

/** A tailnet peer with no host row (task #1389): display only, not a registry. */
type UnreportedPeer = {
  name: string;
  dns_name?: string | null;
  node_id?: string | null;
  ip?: string | null;
  online?: boolean;
  os?: string | null;
};

export type DevicesPayload = {
  hosts: DeviceHost[];
  local_host?: string | null;
  oauth?: OAuthSessionsPayload | null;
  agents?: FleetAgent[];
  unreported_peers?: UnreportedPeer[];
  tailnet_census?: "ok" | "unavailable";
  error?: string;
  errors?: string[];
};
