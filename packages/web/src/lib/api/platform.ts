/**
 * Platform manifest and platform objects.
 * Re-exported from `hub.ts` (task #1575).
 */

/** One object type row from GET /api/platform/manifest (task #1127). */
export type PlatformObjectTypeRow = {
  kind: string;
  uri: string;
  name?: string;
  title?: string;
  description?: string;
  mime_type?: string;
  collection?: boolean;
  served?: boolean;
  table?: string | null;
  id_param?: string | null;
  id_pattern?: string | null;
  profile_tool?: string;
  lifecycle?: string[];
  terminal_states?: string[];
  event_kinds?: string[];
  [key: string]: unknown;
};

/** Platform object-model manifest payload. */
export type PlatformManifestPayload = {
  schema_version?: string;
  product?: string;
  object_types: PlatformObjectTypeRow[];
  error?: string;
};
