import { useMemo, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { AGENTS_QUERY_KEY, fetchAgents } from "../../lib/api/sessions";
import { ATTENTION_QUERY_KEY } from "../../lib/api/attention";
import {
  CONVERSATION_FILTERS,
  DEFAULT_MESSAGE_MAX_BODY_BYTES,
  fetchHandoffs,
  handoffsQueryKey,
  utf8Bytes,
  type ConversationFilter,
  type ConversationSummary,
  type HandoffResult,
  type LiveSession,
  type MessageRow,
  MESSAGES_QUERY_KEY,
  type SendMessageResult,
} from "../../lib/messages/api";
import { useConversations, useLiveSessions, useMessagesStream, useThread } from "../../lib/messages/useMessages";
import {
  Badge,
  Button,
  Checkbox,
  EmptyState,
  ErrorState,
  Field,
  Input,
  LinkButton,
  LinkTabs,
  PageHeader,
  RadioGroup,
  Select,
  Skeleton,
  Textarea,
  UiLink,
  cx,
} from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useHubWrite } from "../lib/mutations";
import { describeReadError } from "../lib/queries";
import { useAreaTabs } from "../lib/navigation";
import { agentLabelParam, useUrlParams } from "../lib/urlState";
import { PATHS } from "./api";
import { PlainText } from "./DetailParts";
import { useSignedInIdentity } from "../lib/identity";
import { useOwnerKey } from "./ownerKey";
import { AttemptNotice } from "./AttemptNotice";
import { useUnknownAttempt, withAttempt } from "./attempts";
import { useEpoch } from "../lib/epoch";
import { newMessageId } from "./identity";
import { SECTION_TABS } from "./InboxPage";
import { ago, humanizeKind } from "./model";
import { sentText, type SendBody } from "./ReplyForm";
import { useDraftedWrite } from "./draftedWrite";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { WriteStatus } from "./WriteStatus";

const FILTER_LABEL: Record<ConversationFilter, string> = { all: "All", unread: "Unread", waiting: "Waiting on a reply" };

const ACTS: { value: SendBody["act"]; label: string }[] = [
  { value: "inform", label: "Tell them something" },
  { value: "request", label: "Ask them to do something" },
  { value: "query", label: "Ask a question" },
];

function conversationHref(params: { filter: string; participant: string }, conversation: string | null, compose = false) {
  const qs = new URLSearchParams();
  if (params.filter && params.filter !== "all") qs.set("filter", params.filter);
  if (params.participant) qs.set("participant", params.participant);
  if (conversation) qs.set("conversation", conversation);
  if (compose) qs.set("compose", "1");
  const s = qs.toString();
  return s ? `/inbox/messages?${s}` : "/inbox/messages";
}

function who(row: ConversationSummary): string {
  return row.participants.join(" and ") || "Unknown participants";
}

/** Conversations between agents and you, with send, reply and hand off to a task (retains legacy Messages). */
export function MessagesPage() {
  useMessagesStream();
  const { values, update } = useUrlParams({ filter: "all", participant: "", conversation: "", compose: "", reply: "", to: "" });
  // `?to=`: a new message to this agent (an agent page's "Send a message").
  const presetTo = agentLabelParam(values.to) ?? "";
  const filter = (CONVERSATION_FILTERS as readonly string[]).includes(values.filter) ? (values.filter as ConversationFilter) : "all";
  const selected = values.conversation || null;
  const sectionTabs = useAreaTabs(SECTION_TABS);
  // Forms and their write state belong to one person: keyed by identity as well as conversation.
  const owner = useOwnerKey();
  const conversations = useConversations(filter, values.participant);
  const thread = useThread(selected);
  const liveSessions = useLiveSessions();
  const agents = useQuery({ queryKey: AGENTS_QUERY_KEY, queryFn: ({ signal }) => fetchAgents(signal), staleTime: 30_000 });
  const agentLabels = useMemo(() => (agents.data?.agents ?? []).map((a) => a.label).sort(), [agents.data]);
  const firstPage = conversations.data?.pages[0];
  const viewer = firstPage?.viewer ?? null;
  // The message being answered is in the URL (`reply=`), so "Open the form to run it" lands on the same reply.
  const threadRows = (thread.data?.pages ?? []).flatMap((p) => p.messages);
  const replyTo = values.reply ? threadRows.find((m) => m.message_id === values.reply) ?? null : null;
  const setReplyTo = (row: MessageRow | null) => update({ reply: row?.message_id ?? null });

  const seen = new Set<string>();
  const rows = (conversations.data?.pages ?? []).flatMap((p) => p.conversations).filter((r) => !seen.has(r.conversation_id) && Boolean(seen.add(r.conversation_id)));

  return (
    <>
      <PageHeader
        title="Messages"
        description={`${firstPage?.scope === "all" ? "Every conversation on the hub." : "Conversations you sent or received."} You send as ${viewer ?? "your signed-in identity"}. Reading here does not mark anything read.`}
        tabs={<LinkTabs label="Inbox sections" items={sectionTabs} />}
        actions={
          <LinkButton variant="primary" href={conversationHref({ filter, participant: values.participant }, null, true)}>
            New message
          </LinkButton>
        }
      />
      <div className="grid gap-5 lg:grid-cols-[minmax(16rem,24rem)_1fr]">
        <div className={cx("flex flex-col gap-3", selected && "max-lg:hidden")}>
          <RadioGroup
            legend="Show"
            orientation="horizontal"
            value={filter}
            onChange={(v) => update({ filter: v, conversation: null })}
            options={CONVERSATION_FILTERS.map((f) => ({ value: f, label: FILTER_LABEL[f] }))}
          />
          <Field label="With">
            <Select
              value={values.participant}
              placeholder="Anyone"
              options={agentLabels.map((l) => ({ value: l, label: l }))}
              onChange={(e) => update({ participant: e.target.value, conversation: null })}
            />
          </Field>
          {conversations.status === "pending" ? (
            <Skeleton label="Loading conversations" lines={4} />
          ) : conversations.status === "error" ? (
            <ErrorState title="Conversations could not be loaded" {...describeReadError(conversations.error)} onRetry={() => void conversations.refetch()} />
          ) : rows.length === 0 ? (
            <EmptyState title="No conversations" description={filter === "all" ? "Messages between agents and you show here." : "None match this filter."} />
          ) : (
            <ul aria-label="Conversations" className="m-0 flex list-none flex-col gap-1.5 p-0">
              {rows.map((row) => (
                <li key={row.conversation_id}>
                  <UiLink
                    href={conversationHref({ filter, participant: values.participant }, row.conversation_id)}
                    aria-current={row.conversation_id === selected ? "true" : undefined}
                    className={cx(
                      "flex min-h-11 flex-col gap-1 rounded border px-3 py-2 text-sm text-[var(--ua-fg)] no-underline hover:bg-[var(--ua-hover)]",
                      "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-[var(--ua-accent)]",
                      row.conversation_id === selected ? "border-[var(--ua-accent)] bg-[var(--ua-hover)]" : "border-[var(--ua-border)] bg-[var(--ua-surface)]",
                    )}
                  >
                    <span className="flex flex-wrap items-center gap-1.5">
                      <span className="break-all font-medium">{who(row)}</span>
                      {row.unread > 0 && <Badge variant={row.unread_mine > 0 ? "info" : "neutral"}>{row.unread} unread{row.unread_mine > 0 ? `, ${row.unread_mine} for you` : ""}</Badge>}
                      {row.waiting > 0 && <Badge variant="warning">{row.waiting} waiting</Badge>}
                    </span>
                    {row.last && (
                      <span className="break-words text-[var(--ua-muted)]">
                        {row.last.from_agent}: {row.last.preview || "(no text)"}
                      </span>
                    )}
                    <span className="text-xs text-[var(--ua-muted)]">
                      {row.total} message{row.total === 1 ? "" : "s"}
                      {row.last?.created_at ? ` · last ${ago(row.last.created_at)}` : ""}
                    </span>
                  </UiLink>
                </li>
              ))}
            </ul>
          )}
          {conversations.hasNextPage && (
            <Button size="sm" variant="ghost" loading={conversations.isFetchingNextPage} onClick={() => void conversations.fetchNextPage()}>
              Load more conversations
            </Button>
          )}
        </div>
        <div className={cx("flex min-w-0 flex-col gap-5", !selected && !values.compose && "max-lg:hidden")}>
          {selected ? (
            <>
              <UiLink href={conversationHref({ filter, participant: values.participant }, null)} className="text-sm underline lg:hidden">
                Back to conversations
              </UiLink>
              <Thread conversationId={selected} query={thread} viewer={viewer} onReply={setReplyTo} />
              <Handoff key={`${owner}|${selected}`} conversationId={selected} />
            </>
          ) : (
            !values.compose && <p className="m-0 text-sm text-[var(--ua-muted)]">Pick a conversation, or write a new message.</p>
          )}
          <Composer
            key={`${owner}|${selected ?? "new"}|${values.reply}|${presetTo}`}
            conversationId={selected}
            presetTo={presetTo}
            agentLabels={agentLabels}
            liveSessions={liveSessions.data ?? []}
            liveSessionsFailed={liveSessions.isError}
            viewer={viewer}
            maxBodyBytes={firstPage?.max_body_bytes ?? DEFAULT_MESSAGE_MAX_BODY_BYTES}
            replyTo={replyTo}
            onCancelReply={() => setReplyTo(null)}
            onSent={(conversationId) => {
              setReplyTo(null);
              if (conversationId && conversationId !== selected) update({ conversation: conversationId, compose: null, to: null }, { push: true });
            }}
          />
        </div>
      </div>
    </>
  );
}

function Thread({
  conversationId,
  query,
  viewer,
  onReply,
}: {
  conversationId: string;
  query: ReturnType<typeof useThread>;
  viewer: string | null;
  onReply: (row: MessageRow) => void;
}) {
  const pages = query.data?.pages ?? [];
  const rows = [...pages].reverse().flatMap((p) => p.messages);
  const participants = pages[0]?.participants ?? [];
  return (
    <section aria-labelledby="thread-heading" className="flex flex-col gap-2">
      <h2 id="thread-heading" className="m-0 text-lg font-semibold">
        {participants.length ? participants.join(" and ") : `Conversation ${conversationId.slice(0, 8)}`}
      </h2>
      {query.hasNextPage && (
        <Button size="sm" variant="ghost" loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
          Load earlier messages
        </Button>
      )}
      {query.status === "pending" ? (
        <Skeleton label="Loading the conversation" />
      ) : query.status === "error" ? (
        <ErrorState headingLevel={3} title="The conversation could not be loaded" {...describeReadError(query.error)} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <EmptyState headingLevel={3} title="No messages in this conversation" />
      ) : (
        <ol aria-label="Messages in this conversation" className="m-0 flex list-none flex-col gap-2 p-0">
          {rows.map((row) => {
            const party = viewer !== null && (row.from_agent === viewer || row.to_agent === viewer);
            return (
              <li key={row.message_id} className="flex flex-col gap-1 rounded border border-[var(--ua-border)] bg-[var(--ua-surface)] p-3">
                <div className="flex flex-wrap items-center gap-1.5 text-xs text-[var(--ua-muted)]">
                  <span className="font-medium text-[var(--ua-fg)]">{row.from_agent}</span>
                  <span>to</span>
                  <span className="font-medium text-[var(--ua-fg)]">{row.to_agent}</span>
                  <span>· {humanizeKind(row.act)}</span>
                  {row.created_at && <time dateTime={row.created_at}>· {ago(row.created_at)}</time>}
                </div>
                {row.body_pruned ? <p className="m-0 text-sm text-[var(--ua-muted)]">The text was removed by the hub's retention rules.</p> : <PlainText text={row.body ?? ""} />}
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge variant={row.status === "answered" ? "success" : row.status === "read" ? "neutral" : "info"}>
                    {row.status === "answered" ? "Answered" : row.status === "read" ? "Read" : "Unread"}
                  </Badge>
                  {row.expects_reply && !row.answered && <Badge variant="warning">Waiting on a reply</Badge>}
                  {party && (
                    <Button size="sm" variant="ghost" onClick={() => onReply(row)} aria-label={`Reply to ${row.from_agent === viewer ? row.to_agent : row.from_agent}`}>
                      Reply
                    </Button>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

/** Hand the conversation off to a task: create one or link an existing one. The task references the thread. */
function Handoff({ conversationId }: { conversationId: string }) {
  const identity = useSignedInIdentity();
  const handoffs = useQuery({
    queryKey: handoffsQueryKey(conversationId),
    queryFn: ({ signal }) => fetchHandoffs(conversationId, signal),
    retry: 1,
  });
  const draft = useDraft<{ mode: string; title: string; project: string; taskId: string }>({
    identity,
    operation: "messages.handoff",
    object: { conversation: conversationId },
    initial: { mode: "new", title: "", project: "", taskId: "" },
  });
  const [error, setError] = useState<string | null>(null);
  const write = useHubWrite<{ title: string; project?: string } | { task_id: number }, HandoffResult>({
    path: PATHS.handoff(conversationId),
    label: "Hand off",
    resumable: false,
    invalidates: [handoffsQueryKey(conversationId)],
  });
  // Creating a task must not be repeated blindly: the attempt is stored before it is sent.
  const attemptKey = draft.identityPending ? null : draft.key;
  const epoch = useEpoch();
  const { attempt: unknownAttempt, dismiss: dismissAttempt } = useUnknownAttempt(attemptKey, write.isRunning);
  const v = draft.value;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const taskId = Number.parseInt(v.taskId, 10);
    if (v.mode === "new" && !v.title.trim()) return setError("Give the task a title.");
    if (v.mode === "existing" && !(Number.isInteger(taskId) && taskId > 0)) return setError("Enter the number of an existing task.");
    setError(null);
    if (unknownAttempt) return;
    const body = v.mode === "new" ? { title: v.title.trim(), ...(v.project.trim() ? { project: v.project.trim() } : {}) } : { task_id: taskId };
    const outcome = attemptKey ? await withAttempt(epoch, attemptKey, "The hand-off", () => write.run(body)) : await write.run(body);
    if (outcome.kind === "done") draft.clear();
  };
  return (
    <section aria-labelledby="handoff-heading" className="flex flex-col gap-2 rounded border border-[var(--ua-border)] p-3">
      <h2 id="handoff-heading" className="m-0 text-base font-semibold">Hand off to a task</h2>
      {handoffs.status === "error" ? (
        <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">Where this conversation was handed off could not be loaded. {describeReadError(handoffs.error).message}</p>
      ) : handoffs.data && handoffs.data.length > 0 ? (
        <ul aria-label="Handed off to" className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
          {handoffs.data.map((h) => (
            <li key={h.task_id}>
              <UiLink className="underline" href={`/work/tasks/${h.task_id}`}>Task {h.task_id}</UiLink> · {h.title} <Badge>{humanizeKind(h.status)}</Badge>
            </li>
          ))}
        </ul>
      ) : null}
      <form onSubmit={(e) => void submit(e)} aria-label="Hand off this conversation" className="flex flex-col gap-2">
        <RadioGroup
          legend="Hand off to"
          orientation="horizontal"
          value={v.mode}
          onChange={(mode) => draft.setValue((x) => ({ ...x, mode }))}
          options={[
            { value: "new", label: "A new task" },
            { value: "existing", label: "An existing task" },
          ]}
        />
        {v.mode === "new" ? (
          <div className="grid gap-2 sm:grid-cols-[1fr_12rem]">
            <Field label="Task title" required error={error ?? undefined}>
              <Input value={v.title} maxLength={300} onChange={(e) => draft.setValue((x) => ({ ...x, title: e.target.value }))} />
            </Field>
            <Field label="Project" hint="Optional">
              <Input value={v.project} onChange={(e) => draft.setValue((x) => ({ ...x, project: e.target.value }))} />
            </Field>
          </div>
        ) : (
          <Field label="Task number" required error={error ?? undefined}>
            <Input inputMode="numeric" value={v.taskId} onChange={(e) => draft.setValue((x) => ({ ...x, taskId: e.target.value.replace(/[^0-9]/g, "") }))} />
          </Field>
        )}
        <p className="m-0 text-xs text-[var(--ua-muted)]">
          {v.mode === "new"
            ? "Creates a task that points to this conversation. Nobody is assigned, and no message is copied."
            : "Adds a pointer to this conversation on the task. Its owner does not change."}
        </p>
        {unknownAttempt ? (
          <AttemptNotice
            attempt={unknownAttempt}
            onMade={() => {
              dismissAttempt();
              draft.clear();
            }}
            onNotMade={dismissAttempt}
          />
        ) : (
          <div>
            <Button type="submit" loading={write.isRunning}>
              {v.mode === "new" ? "Create the task" : "Link the task"}
            </Button>
          </div>
        )}
        <WriteStatus state={write.state} doneText={(r) => (r.created ? `Created task ${r.task_id}.` : `Linked task ${r.task_id}.`)} />
      </form>
    </section>
  );
}

function sessionLabel(s: LiveSession): string {
  const bits = [s.agent, s.kind, s.host, s.branch].filter(Boolean);
  return `${bits.join(" · ")}${s.task_id ? ` · task ${s.task_id}` : ""} (${s.session_id.slice(0, 8)})`;
}

type ComposeForm = { toAgent: string; toSession: string; act: string; expectsReply: boolean; body: string };

/**
 * New message or reply. Fields and the exact request (with its client id)
 * live in a draft (spec §6): a parked send is either run from the store or
 * sent again from here unchanged, never both, and a send that settles after
 * further typing leaves the newer text alone.
 */
function Composer({
  conversationId,
  presetTo = "",
  agentLabels,
  liveSessions,
  liveSessionsFailed,
  viewer,
  maxBodyBytes,
  replyTo,
  onCancelReply,
  onSent,
}: {
  conversationId: string | null;
  /** The recipient a new message starts with (`?to=`); a reply goes to its counterpart. */
  presetTo?: string;
  agentLabels: string[];
  liveSessions: LiveSession[];
  liveSessionsFailed: boolean;
  viewer: string | null;
  maxBodyBytes: number;
  replyTo: MessageRow | null;
  onCancelReply: () => void;
  onSent: (conversationId: string | null) => void;
}) {
  const counterpart = replyTo ? (replyTo.from_agent === viewer ? replyTo.to_agent : replyTo.from_agent) : "";
  const to = replyTo ? "" : presetTo;
  const write = useDraftedWrite<ComposeForm, SendBody, SendMessageResult>({
    operation: "messages.compose",
    // A preset recipient has its own draft, so an unsent message to someone else is not shown under it.
    object: { conversation: conversationId ?? "", reply: replyTo?.message_id ?? "", to },
    initial: { toAgent: counterpart || to, toSession: "", act: "inform", expectsReply: false, body: "" },
    write: { path: PATHS.message, label: "Your message", invalidates: [MESSAGES_QUERY_KEY, ATTENTION_QUERY_KEY] },
  });
  const [error, setError] = useState<string | null>(null);
  const v = write.form;
  const bytes = utf8Bytes(v.body);
  const tooLong = bytes > maxBodyBytes;
  const sessions = v.toAgent ? liveSessions.filter((s) => s.agent === v.toAgent) : liveSessions;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (write.blockedByStore) return;
    if (!write.hasPending) {
      if (!v.toAgent && !v.toSession) return setError("Choose who the message is for.");
      if (!v.body.trim()) return setError("Write the message first.");
      if (tooLong) return;
    }
    setError(null);
    const outcome = await write.submit(
      (form) => ({
        act: (ACTS.find((a) => a.value === form.act)?.value ?? "inform") as SendBody["act"],
        body: form.body,
        // One id per drafted message, kept with the draft, so a repeat is never a second message.
        message_id: newMessageId(),
        expects_reply: form.expectsReply,
        ...(form.toAgent ? { to_agent: form.toAgent } : {}),
        ...(form.toSession ? { to_session: form.toSession } : {}),
        ...(replyTo ? { reply_to: replyTo.message_id } : {}),
      }),
      // One action: the kept message is repeated when it answers the same message (or none).
      (kept) => (kept.reply_to ?? null) === (replyTo?.message_id ?? null),
    );
    // Moving to the conversation (or closing the reply) changes the form; do it only if nothing newer was typed.
    if (outcome && (outcome.kind === "done" || outcome.kind === "accepted") && outcome.draftUnchanged) onSent(outcome.data?.conversation_id ?? null);
  };

  return (
    <form onSubmit={(e) => void submit(e)} aria-labelledby="composer-heading" className="flex flex-col gap-3 rounded border border-[var(--ua-border)] p-3">
      <h2 id="composer-heading" className="m-0 text-base font-semibold">
        {replyTo ? `Reply to ${counterpart}` : "New message"}
      </h2>
      {replyTo && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--ua-muted)]">
          <span className="break-words">Replying to {replyTo.from_agent}: {(replyTo.body ?? "").slice(0, 120)}</span>
          <Button size="sm" variant="ghost" onClick={onCancelReply}>
            Cancel reply
          </Button>
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="To" required>
          <Select
            value={v.toAgent}
            placeholder="Choose an agent"
            options={agentLabels.map((l) => ({ value: l, label: l }))}
            onChange={(e) => write.setForm({ toAgent: e.target.value, toSession: "" })}
          />
        </Field>
        <Field label="Running session" hint={liveSessionsFailed ? "Running sessions could not be loaded; the message goes to every session of the agent." : "Optional. Leave empty to reach every session of the agent."}>
          <Select
            value={v.toSession}
            placeholder="Every session of the agent"
            options={sessions.map((s) => ({ value: s.session_id, label: sessionLabel(s) }))}
            onChange={(e) => {
              const id = e.target.value;
              const s = liveSessions.find((x) => x.session_id === id);
              write.setForm({ toSession: id, ...(s ? { toAgent: s.agent } : {}) });
            }}
          />
        </Field>
        <Field label="Kind of message">
          <Select value={v.act} options={ACTS} onChange={(e) => write.setForm({ act: e.target.value })} />
        </Field>
        <div className="self-end">
          <Checkbox label="Ask for a reply" checked={v.expectsReply} onChange={(e) => write.setForm({ expectsReply: e.target.checked })} />
        </div>
      </div>
      <Field
        label="Message"
        required
        hint={`${bytes} of ${maxBodyBytes} bytes. ${write.restored ? "Your unsent draft was restored." : "Kept as a draft while you type."}`}
        error={error ?? (tooLong ? "This message is too long for the hub. Shorten it." : undefined)}
      >
        <Textarea rows={4} value={v.body} onChange={(e) => write.setForm({ body: e.target.value })} />
      </Field>
      {write.storedForForm && <ParkedWriteNotice approvalId={write.pendingApprovalId} label="Your message" />}
      {write.blockedByStore ? (
        <ParkedWriteNotice approvalId={write.pendingApprovalId} label="Your message" />
      ) : (
        <div>
          <Button type="submit" variant="primary" loading={write.isRunning} disabled={tooLong}>
            Send
          </Button>
        </div>
      )}
      {!write.blockedByStore && <WriteStatus state={write.state} doneText={sentText} />}
    </form>
  );
}
