/**
 * Attention pane types matching GET /api/attention (webui/api.attention_payload).
 */

export type AttentionSeverity = "critical" | "high" | "medium" | "low" | "info" | string;

export type AttentionSource = {
  task_id?: number | null;
  approval_id?: number | null;
  agent_label?: string | null;
  worktree?: string | null;
  anomaly?: string | null;
  [key: string]: unknown;
};

export type AttentionItem = {
  kind: string;
  severity: AttentionSeverity;
  source: AttentionSource;
  source_subject?: string;
  source_host?: string | null;
  hub_origin?: string | null;
  age_s?: number | null;
  reason?: string;
  title?: string;
  decision_type?: string;
};

export type NeedsOperatorRow = {
  kind: string;
  subject: string;
  title: string;
  since?: string | null;
  act: { pane: "approvals" | "tasks" | "messages" | "sessions"; params: Record<string, string> };
};

export type NeedsOperator = {
  rows: NeedsOperatorRow[];
  counts: Record<string, number>;
  total: number;
  errors: string[];
};

export type AttentionPayload = {
  needs_operator?: NeedsOperator;
  items: AttentionItem[];
  informational: AttentionItem[];
  counts: Record<string, number>;
  total: number;
  errors: string[];
  omitted?: string[];
  error?: string;
};

export type AttentionNextAction = {
  label: string;
  /** Nav mode when the SPA already has that pane; otherwise null (monitor-only). */
  mode: "tasks" | "approvals" | "plans" | "attention" | "doctor" | null;
  monitorOnly: boolean;
  params?: Record<string, string>;
};

export function attentionTarget(item: AttentionItem): {
  mode: "tasks" | "approvals" | "plans";
  params: Record<string, string>;
  label: string;
} | null {
  const approvalId = item.source?.approval_id;
  if (
    item.kind === "approval" &&
    typeof approvalId === "number" &&
    Number.isInteger(approvalId) &&
    approvalId > 0
  ) {
    return {
      mode: "approvals",
      params: { approval: String(approvalId) },
      label: `Open approval #${approvalId}`,
    };
  }
  if (item.kind === "plan_approval_wait") {
    return {
      mode: "plans",
      params: {},
      label: "Open plans",
    };
  }
  const taskId = item.source?.task_id;
  if (typeof taskId === "number" && Number.isInteger(taskId) && taskId > 0) {
    return { mode: "tasks", params: { task: String(taskId) }, label: `Open task #${taskId}` };
  }
  if (typeof approvalId === "number" && Number.isInteger(approvalId) && approvalId > 0) {
    return {
      mode: "approvals",
      params: { approval: String(approvalId) },
      label: `Open approval #${approvalId}`,
    };
  }
  return null;
}

/** Display title: "subject: title" when both exist (legacy attentionTitle). */
export function attentionTitle(item: AttentionItem): string {
  const subject = item.source_subject || "";
  if (item.title && subject) return `${subject}: ${item.title}`;
  if (item.title) return item.title;
  return subject || "—";
}

export function formatAge(ageS: number | null | undefined): string {
  if (ageS == null || !Number.isFinite(ageS) || ageS < 0) return "—";
  const s = Math.floor(ageS);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d`;
}

/**
 * Next-action navigation from the top attention item (legacy renderAttentionNextAction).
 * Only maps to panes that already exist in the modern SPA (tasks / approvals).
 */
export function nextActionForItems(items: AttentionItem[]): AttentionNextAction {
  if (!items.length) {
    return {
      label: "monitor the dashboard",
      mode: null,
      monitorOnly: true,
    };
  }
  const item = items[0];
  const kind = item?.kind;
  const target = attentionTarget(item);
  if (kind === "approval") {
    return {
      label: "Review pending approvals",
      mode: target?.mode ?? "approvals",
      monitorOnly: false,
      params: target?.params,
    };
  }
  if (
    kind === "review_wait" ||
    kind === "awaiting_operator" ||
    kind === "awaiting_legacy_operator" || // Legacy hub alias for one release.
    kind === "plan_approval_wait" ||
    kind === "human_merge_wait" ||
    kind === "blocked_task" ||
    kind === "stale_claim"
  ) {
    if (kind === "plan_approval_wait") {
      return {
        label: "Review waiting plans",
        mode: target?.mode ?? "plans",
        monitorOnly: false,
        params: target?.params,
      };
    }
    return {
      label: "Open task queue",
      mode: target?.mode ?? "tasks",
      monitorOnly: false,
      params: target?.params,
    };
  }
  return {
    label: "Open task queue",
    mode: target?.mode ?? "tasks",
    monitorOnly: false,
    params: target?.params,
  };
}
