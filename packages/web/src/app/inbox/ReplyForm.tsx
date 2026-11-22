import { useState, type FormEvent } from "react";
import { DEFAULT_MESSAGE_MAX_BODY_BYTES, MESSAGES_QUERY_KEY, utf8Bytes, type SendMessageResult } from "../../lib/messages/api";
import { ATTENTION_QUERY_KEY } from "../../lib/api/attention";
import { Button, Field, Textarea } from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import { PATHS } from "./api";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { useDraftedWrite } from "./draftedWrite";
import { newMessageId } from "./identity";
import { WriteStatus } from "./WriteStatus";

export type SendBody = {
  act: "request" | "inform" | "query";
  body: string;
  message_id: string;
  to_agent?: string;
  to_session?: string;
  reply_to?: string;
  expects_reply: boolean;
};

/**
 * `POST /api/messages` through the one write helper. Resumable: the body
 * carries its own `message_id`, so the replay after an approval is the
 * identical call (and the hub drops a duplicate id), which
 * `tests/app/inbox/messages.test.tsx` checks.
 */
export function useSendMessage(label = "Your message") {
  return useHubWrite<SendBody, SendMessageResult>({
    path: PATHS.message,
    label,
    invalidates: [MESSAGES_QUERY_KEY, ATTENTION_QUERY_KEY],
  });
}

export function sentText(result: SendMessageResult): string {
  if (result.duplicate) return "Already sent earlier; it was not sent twice.";
  return result.target_live === false
    ? "Sent. The agent is not running right now and will read it when it next starts."
    : "Sent.";
}

type ReplyFormProps = { messageId: string; toAgent: string; maxBytes?: number };

/**
 * Answer one agent question inline. The text and the exact request (with
 * its client id) live in a draft (spec §6), so a reply parked behind an
 * approval is either run from the store or sent again from here unchanged,
 * never both. The reply names no session: the hub routes it to the asking
 * session while that is running, else to the agent.
 */
export function ReplyForm({ messageId, toAgent, maxBytes = DEFAULT_MESSAGE_MAX_BODY_BYTES }: ReplyFormProps) {
  const write = useDraftedWrite<{ body: string }, SendBody, SendMessageResult>({
    operation: "inbox.reply",
    object: { message: messageId },
    initial: { body: "" },
    write: { path: PATHS.message, label: "Your reply", invalidates: [MESSAGES_QUERY_KEY, ATTENTION_QUERY_KEY] },
  });
  const [error, setError] = useState<string | null>(null);
  const bytes = utf8Bytes(write.form.body);
  const tooLong = bytes > maxBytes;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (write.blockedByStore) return;
    if (!write.hasPending && !write.form.body.trim()) {
      setError("Write a reply first.");
      return;
    }
    if (tooLong) return;
    setError(null);
    await write.submit(
      (form) => ({
        act: "inform",
        body: form.body,
        // One id per drafted reply, kept with the draft, so a repeat is never a second message.
        message_id: newMessageId(),
        to_agent: toAgent,
        reply_to: messageId,
        expects_reply: false,
      }),
      // One action: the kept reply is repeated when it answers this message.
      (kept) => kept.reply_to === messageId,
    );
  };

  return (
    <form onSubmit={(e) => void submit(e)} aria-label={`Reply to ${toAgent}`} className="flex flex-col gap-2">
      <Field
        label={`Your reply to ${toAgent}`}
        hint={`${bytes} of ${maxBytes} bytes. ${write.restored ? "Your unsent draft was restored." : "Saved as a draft while you type."}`}
        error={error ?? (tooLong ? "This reply is too long for the hub. Shorten it." : undefined)}
      >
        <Textarea rows={4} value={write.form.body} onChange={(e) => write.setForm({ body: e.target.value })} />
      </Field>
      {write.storedForForm && <ParkedWriteNotice approvalId={write.pendingApprovalId} label="Your reply" />}
      {write.blockedByStore ? (
        <ParkedWriteNotice approvalId={write.pendingApprovalId} label="Your reply" />
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" variant="primary" loading={write.isRunning} disabled={tooLong}>
            Send reply
          </Button>
        </div>
      )}
      {!write.blockedByStore && <WriteStatus state={write.state} doneText={sentText} />}
    </form>
  );
}
