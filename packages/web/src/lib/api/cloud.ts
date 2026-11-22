
/** Whether a section of the GCP inventory could actually be read. */
type CloudSectionState = "ok" | "unavailable";

/**
 * Instance state as reported by the hub.
 *
 * `unknown` and `absent` are deliberately distinct: "we could not read the
 * instance list" and "there is no instance" call for opposite operator
 * responses, and must never render the same.
 */
export type CloudInstanceState = "running" | "stopped" | "transitional" | "absent" | "unknown";

interface CloudInstance {
  name?: string | null;
  status?: string | null;
  zone?: string | null;
  machineType?: string | null;
}

interface CloudBackupBucket {
  name?: string | null;
  location?: string | null;
  versioning_enabled?: boolean;
}

export interface CloudPayload {
  status: "ok" | "degraded" | "error" | "unknown";
  project_id: string | null;
  sections: Record<string, CloudSectionState>;
  /** At least one requested section could not be read; the whole reading is partial. */
  partial_coverage: boolean;
  instance_state: CloudInstanceState;
  instances: CloudInstance[];
  budgets: { state: "ok" | "unknown"; items: Array<Record<string, unknown>> };
  backups: {
    state: "present" | "none" | "unknown";
    detail: string;
    buckets: CloudBackupBucket[];
  };
  errors: Array<{ section?: string; error?: string }>;
  remediation: string;
  checked_at: string | null;
  cached_age_s: number;
}
