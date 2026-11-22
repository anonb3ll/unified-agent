export type SkillEntry = {
  name: string;
  version: number;
  content_hash: string;
  source: string;
  published_by: string;
  published_at: string;
  latest: boolean;
  installable: boolean;
  install_block_reason?: string;
  provenance: { source: string; actor?: string | null; ts?: string | null };
};

export type TeamEntry = {
  id: string;
  name: string;
  lead: string | null;
  members: Array<{
    label: string;
    profile: string;
    role: string;
    skills: Array<{ name: string; version: number; content_hash: string; source: string }>;
  }>;
  declared_skill_versions?: Array<{ name: string; version: number; content_hash: string }>;
  declared_skill_versions_error?: string;
  skill_binding_source?: string;
  responsibility: string[];
  permitted_scope: { owned_epics: number[]; default_lanes: string[]; decision_rights: string[] };
  version: number | null;
  content_hash: string;
  provenance: { source: string; revision?: number | null; actor?: string | null; ts?: string | null };
  drift: boolean;
};

export type DriftEntry = {
  name: string;
  registry_version?: number;
  version?: number;
  on_disk_hash?: string;
  registry_hash?: string;
};

export type SurfaceDrift = {
  surface_dir: string;
  result: {
    root?: string;
    comparison?: string;
    missing?: DriftEntry[];
    stale?: DriftEntry[];
    unregistered?: DriftEntry[];
    unassigned?: DriftEntry[];
    synced?: DriftEntry[];
    error?: string;
  };
};

export type StarterTemplate = {
  id: string; version: number; content_hash: string; description: string; guidance: string;
  roles: Record<string, string>; independent_roles: string[];
};

export type StarterParams = {
  template_id: string; team_id: string; content_hash: string;
  bindings: Record<string, { label: string; harness: string; model?: string; host?: string; workspace?: { repo?: string; worktree: string } }>;
  operating_profile: string; trust_preset: string;
};

export type SkillsTeamsPayload = {
  starter_templates?: StarterTemplate[];
  skills: SkillEntry[];
  teams: TeamEntry[];
  surfaces: SurfaceDrift[];
  permissions: { publish: boolean; install: boolean; team_install: boolean; drift: boolean };
  error?: string;
};

export type CatalogAction =
  | { action: "starter_preview" | "starter_install"; params: StarterParams }
  | { action: "publish"; params: { name: string; path: string } }
  | { action: "install"; params: {
      name: string; version: number; content_hash: string;
      surface_dir: string; expected_hash?: string;
    } }
  | { action: "team_install"; params: { team_id: string; revision: number; content_hash: string } };

export type CatalogActionResult = {
  ok?: boolean;
  pending?: boolean;
  approval_id?: number;
  blocked?: boolean;
  error?: string;
  message?: string;
  review?: { findings?: Array<{ message?: string; severity?: string }> };
};
