/**
 * Zulip, Slack gateway and notification delivery views.
 * Re-exported from `hub.ts` (task #1575).
 */

export type ZulipChannelRow = {
  agent_label?: string | null;
  channel?: string | null;
  status?: string | null;
  cursor?: number | string | null;
  cursor_age_seconds?: number | null;
  messages_processed?: number | null;
  [key: string]: unknown;
};

export type ZulipInboundRow = {
  created_at?: string | null;
  channel?: string | null;
  topic?: string | null;
  task_id?: number | null;
  sender?: string | null;
  actionable_for?: string[] | null;
  is_bot?: boolean | null;
  zulip_url?: string | null;
  [key: string]: unknown;
};

/**
 * Slack gateway ingress status (GET /api/slack_gateway, task #1185).
 * Status-only projection of the Slack HTTP ingress adapter — never a chat
 * client and never a secret value (presence booleans only).
 */
export type SlackGatewayEventRow = {
  ulid: string | null;
  channel: string | null;
  created_at: string | null;
};
/**
 * One delivery outcome row from GET /api/notify_delivery (task #1183).
 * Outcome metadata only — never the notification message text.
 */
export type NotifyDeliveryRow = {
  agent: string | null;
  delivered: boolean;
  chars: number;
  error: string | null;
  sent_at: string;
};
