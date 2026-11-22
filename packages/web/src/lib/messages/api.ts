/**
 * Messages pane data layer (task #1864).
 *
 * Mirrors the hub routes in `webui/routes.py`:
 *  - GET  /api/messages/conversations                    → messaging.conversations.list_conversations
 *  - GET  /api/messages/conversations/{conversation_id}  → messaging.conversations.conversation_thread
 *  - GET  /api/messages/sessions                         → registry.list_live_sessions
 *  - POST /api/messages                                  → the registered message_send tool
 *
 * Message bodies are untrusted text written by agents. The pane renders them as
 * React text nodes only, never as HTML or Markdown.
 */
import { fetchCsrfToken } from "../hub";

export const MESSAGE_ACTS = ["request", "inform", "query"] as const;
export type ComposerAct = (typeof MESSAGE_ACTS)[number];

export const CONVERSATION_FILTERS = ["all", "unread", "waiting"] as const;
export type ConversationFilter = (typeof CONVERSATION_FILTERS)[number];

/**
 * Body cap used until the hub reports its own (`max_body_bytes` on the
 * conversation list, from `UA_MESSAGE_MAX_BYTES`). The hub enforces the real one.
 */
export const DEFAULT_MESSAGE_MAX_BODY_BYTES = 4096;

export type MessageStatus = "delivered" | "read" | "answered" | string;

/** One `message_list` row, with session read receipts folded into `read_at`. */
export type MessageRow = {
  seq: number | null;
  message_id: string;
  act: string;
  conversation_id: string;
  reply_to: string | null;
  from_agent: string;
  to_agent: string;
  from_session: string | null;
  to_session: string | null;
  body: string | null;
  body_pruned?: boolean;
  created_at: string | null;
  expects_reply: boolean;
  read_at: string | null;
  acked_at: string | null;
  answered: boolean;
  answered_by: string | null;
  status: MessageStatus;
  task_id?: number | null;
};

export type ConversationLast = {
  message_id: string;
  seq: number | null;
  act: string;
  from_agent: string;
  to_agent: string;
  from_session: string | null;
  to_session: string | null;
  expects_reply: boolean;
  created_at: string | null;
  preview: string;
};

export type ConversationSummary = {
  conversation_id: string;
  participants: string[];
  sessions: string[];
  total: number;
  unread: number;
  unread_mine: number;
  waiting: number;
  last_seq: number;
  last: ConversationLast | null;
};

export type ConversationsPayload = {
  viewer: string | null;
  scope: "all" | "own" | string;
  filter: ConversationFilter;
  participant: string | null;
  conversations: ConversationSummary[];
  truncated: boolean;
  /** `before_seq` for the next (older) page; null on the last page. */
  next_before_seq: number | null;
  /** The hub's configured message body cap in UTF-8 bytes. */
  max_body_bytes: number;
};

export type ThreadPayload = {
  conversation_id: string;
  participants: string[];
  messages: MessageRow[];
  total: number;
  next_seq: number;
  /** The `before_seq` that fetches the page before this one. */
  oldest_seq: number | null;
  /** Whether visible messages older than this page exist. */
  has_older: boolean;
};

export type LiveSession = {
  session_id: string;
  agent: string;
  kind: string | null;
  host: string | null;
  branch: string | null;
  task_id: number | null;
  task_title: string | null;
  last_seen_at: string | null;
};

export type SendMessageInput = {
  act: ComposerAct;
  body: string;
  message_id: string;
  to_agent?: string;
  to_session?: string;
  reply_to?: string;
  expects_reply: boolean;
};

export type SendMessageResult = {
  accepted?: boolean;
  duplicate?: boolean;
  message_id?: string;
  conversation_id?: string;
  from_agent?: string;
  to_agent?: string;
  to_session?: string | null;
  target_live?: boolean;
  /** Set when an approval gate parked the send (HTTP 202). */
  status?: "pending";
  approval_id?: number;
};

/** Every Messages query key starts here, so one invalidation refreshes the pane. */
export const MESSAGES_QUERY_KEY = ["messages"] as const;

export function conversationsQueryKey(
  filter: ConversationFilter,
  participant: string,
) {
  return [
    ...MESSAGES_QUERY_KEY,
    "conversations",
    filter,
    participant || "any",
  ] as const;
}

export function threadQueryKey(conversationId: string) {
  return [...MESSAGES_QUERY_KEY, "thread", conversationId] as const;
}

export const LIVE_SESSIONS_QUERY_KEY = [
  ...MESSAGES_QUERY_KEY,
  "live-sessions",
] as const;

/** A fresh idempotency key for one composed message (reused on retry). */
export function newMessageId(): string {
  return crypto.randomUUID();
}

/** UTF-8 size of a body, as the hub counts it. */
export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

async function getJson<T>(
  path: string,
  what: string,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    let detail = "";
    try {
      const body = (await response.json()) as { error?: unknown };
      if (typeof body?.error === "string") detail = body.error;
    } catch {
      // non-JSON body
    }
    throw new Error(
      `${what} HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  return (await response.json()) as T;
}

export async function fetchConversations(
  filter: ConversationFilter,
  participant: string,
  beforeSeq: number | null = null,
  signal?: AbortSignal,
): Promise<ConversationsPayload> {
  const qs = new URLSearchParams({ filter, limit: "50" });
  if (participant) qs.set("participant", participant);
  if (beforeSeq != null) qs.set("before_seq", String(beforeSeq));
  const payload = await getJson<Partial<ConversationsPayload>>(
    `/api/messages/conversations?${qs}`,
    "conversations",
    signal,
  );
  return {
    viewer: typeof payload.viewer === "string" ? payload.viewer : null,
    scope: payload.scope ?? "own",
    filter: payload.filter ?? filter,
    participant: payload.participant ?? null,
    conversations: Array.isArray(payload.conversations)
      ? payload.conversations
      : [],
    truncated: Boolean(payload.truncated),
    next_before_seq:
      typeof payload.next_before_seq === "number"
        ? payload.next_before_seq
        : null,
    max_body_bytes:
      typeof payload.max_body_bytes === "number" && payload.max_body_bytes > 0
        ? payload.max_body_bytes
        : DEFAULT_MESSAGE_MAX_BODY_BYTES,
  };
}

export async function fetchThread(
  conversationId: string,
  beforeSeq: number | null = null,
  signal?: AbortSignal,
): Promise<ThreadPayload> {
  const qs = beforeSeq != null ? `?before_seq=${beforeSeq}` : "";
  const payload = await getJson<Partial<ThreadPayload>>(
    `/api/messages/conversations/${encodeURIComponent(conversationId)}${qs}`,
    "conversation",
    signal,
  );
  const messages = Array.isArray(payload.messages) ? payload.messages : [];
  return {
    conversation_id: payload.conversation_id ?? conversationId,
    participants: Array.isArray(payload.participants)
      ? payload.participants
      : [],
    messages,
    total: typeof payload.total === "number" ? payload.total : messages.length,
    next_seq: typeof payload.next_seq === "number" ? payload.next_seq : 0,
    oldest_seq:
      typeof payload.oldest_seq === "number" ? payload.oldest_seq : null,
    has_older: Boolean(payload.has_older),
  };
}

export async function fetchLiveSessions(
  signal?: AbortSignal,
): Promise<LiveSession[]> {
  const payload = await getJson<{ sessions?: LiveSession[] }>(
    "/api/messages/sessions",
    "live sessions",
    signal,
  );
  return Array.isArray(payload.sessions) ? payload.sessions : [];
}

/** A refused send, with the HTTP status and the hub's policy reason when it gave one. */
export class MessageSendError extends Error {
  readonly status: number;
  readonly reasonCode: string | null;

  constructor(message: string, status: number, reasonCode: string | null) {
    super(message);
    this.name = "MessageSendError";
    this.status = status;
    this.reasonCode = reasonCode;
  }
}

/**
 * POST /api/messages with CSRF (one refresh-and-retry on a stale token, as
 * `postCsrfJson` does). Refusals throw `MessageSendError` so the composer can tell
 * a throttle (429) or a policy refusal (403) from a send worth retrying.
 */
export async function sendMessage(
  input: SendMessageInput,
  signal?: AbortSignal,
): Promise<SendMessageResult> {
  const attempt = async (retried: boolean): Promise<SendMessageResult> => {
    const token = await fetchCsrfToken(signal);
    const response = await fetch("/api/messages", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "x-ua-csrf": token,
      },
      body: JSON.stringify(input),
      signal,
    });
    let payload: Record<string, unknown> = {};
    try {
      const parsed = (await response.json()) as unknown;
      if (parsed && typeof parsed === "object")
        payload = parsed as Record<string, unknown>;
    } catch {
      // empty or non-JSON body
    }
    const error = typeof payload.error === "string" ? payload.error : "";
    if (response.status === 403 && !retried && /csrf/i.test(error))
      return attempt(true);
    if (!response.ok) {
      const reason =
        typeof payload.reason_code === "string" ? payload.reason_code : null;
      throw new MessageSendError(
        error.slice(0, 200) || `send HTTP ${response.status}`,
        response.status,
        reason,
      );
    }
    return payload as SendMessageResult;
  };
  return attempt(false);
}

// --- conversation handoff (task #1426) ---------------------------------------------

/** A task a conversation was handed off to; the task references the thread by id. */
export type ConversationHandoff = {
  task_id: number;
  title: string;
  status: string;
  by: string | null;
  at: string | null;
};

export type HandoffInput =
  { task_id: number } | { title: string; project?: string };

export type HandoffResult = {
  conversation_id: string;
  task_id: number;
  created: boolean;
  reference: string;
};

export function handoffsQueryKey(conversationId: string) {
  return [...MESSAGES_QUERY_KEY, "handoffs", conversationId] as const;
}

export async function fetchHandoffs(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ConversationHandoff[]> {
  const payload = await getJson<{ handoffs: ConversationHandoff[] }>(
    `/api/messages/conversations/${encodeURIComponent(conversationId)}/handoffs`,
    "handoffs",
    signal,
  );
  return payload.handoffs ?? [];
}

/**
 * POST a handoff with CSRF (one refresh-and-retry on a stale token). The hub
 * creates or links a task that references the conversation; it copies no message.
 */
export async function handOffConversation(
  conversationId: string,
  input: HandoffInput,
  signal?: AbortSignal,
): Promise<HandoffResult> {
  const path = `/api/messages/conversations/${encodeURIComponent(conversationId)}/handoff`;
  const attempt = async (retried: boolean): Promise<HandoffResult> => {
    const token = await fetchCsrfToken(signal);
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "x-ua-csrf": token,
      },
      body: JSON.stringify(input),
      signal,
    });
    let payload: Record<string, unknown> = {};
    try {
      const parsed = (await response.json()) as unknown;
      if (parsed && typeof parsed === "object")
        payload = parsed as Record<string, unknown>;
    } catch {
      // empty or non-JSON body
    }
    const error = typeof payload.error === "string" ? payload.error : "";
    if (response.status === 403 && !retried && /csrf/i.test(error))
      return attempt(true);
    if (!response.ok)
      throw new Error(error.slice(0, 200) || `handoff HTTP ${response.status}`);
    if (response.status === 202) {
      // The task write parked for approval; nothing was created or linked yet.
      const id = payload.approval_id;
      throw new Error(
        `Waiting for approval${typeof id === "number" ? ` #${id}` : ""}; hand off again once it is approved.`,
      );
    }
    return payload as HandoffResult;
  };
  return attempt(false);
}
