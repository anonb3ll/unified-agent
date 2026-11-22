export interface TeamSummary {
  team: string;
  description?: string | null;
  operating_profile?: string | null;
  spec_sha256: string;
  seats: Array<{ label: string; harness: string; model?: string | null }>;
  edges: number;
  launch_order: string[];
}

export interface TeamRefusal { seat: string; code: string; detail: string }

export interface TeamPlan {
  team: string;
  ready: boolean;
  plan_sha256: string;
  plan_text: string;
  refusals: TeamRefusal[];
  warnings: string[];
}

export interface SeatOutcome { label: string; outcome: string; item_id?: string | null; reason?: string }

export interface TeamUpResult {
  pending?: boolean;
  approval_id?: number | null;
  booted?: boolean;
  team_run_id?: string;
  seats?: SeatOutcome[];
  autonomy_review?: { mission_id: number; pending_id?: number | null; reason: string };
  error?: string;
}
