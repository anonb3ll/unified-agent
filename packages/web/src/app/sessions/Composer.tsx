import type { HarnessControl, NodeDispatchItem } from "../../lib/api/nodes";
import { acceptsFollowUps } from "./nodeHistory";
import { Button, Field, Textarea } from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import { sessionQueryKeys, WRITE_PATHS } from "./api";
import { useMessageDraft } from "./messageDraft";
import { WriteStatus } from "./parts";
import { CREW_LIVE_QUERY_KEY } from "../../lib/api/crew";
import { NODES_QUERY_KEY } from "../../lib/api/nodes";

const USABLE = new Set(["supported", "emulated"]);
export const FOLLOWUP_MAX = 20_000;

export type ComposerTarget =
  | { kind: "device"; item: NodeDispatchItem; control: HarnessControl | null }
  | { kind: "crew"; sessionId: string }
  | { kind: "none"; reason: string };

/** Which way a follow-up can reach this session, or why it cannot. */
export function composerTarget(input: {
  sessionId: string;
  open: boolean;
  item: NodeDispatchItem | null;
  itemKnown: boolean;
  control: HarnessControl | null;
  hasCrew: boolean;
}): ComposerTarget {
  if (input.item && acceptsFollowUps(input.item)) return { kind: "device", item: input.item, control: input.control };
  if (input.hasCrew && input.open) return { kind: "crew", sessionId: input.sessionId };
  if (!input.open) return { kind: "none", reason: "This session has ended, so it cannot take a follow-up. Use Resume to continue it, if it offers that." };
  if (input.item) return { kind: "none", reason: "The agent on this device is not taking messages now: it has finished or is being stopped." };
  if (input.itemKnown) return { kind: "none", reason: "This session ran on a device that is no longer reporting it, so a follow-up cannot be delivered." };
  return {
    kind: "none",
    reason: "This session was started outside the hub (for example in a terminal) and has no channel for messages from here.",
  };
}

/**
 * The follow-up composer (spec §9.4). The text is a draft keyed by identity
 * and session, so it survives navigation (defect 8) and is cleared once the
 * hub takes it. Each composed message carries one idempotency key, reused
 * when the same message is sent again, so a retry or an approval replay never
 * delivers it twice (the hub dedupes on `idempotency_key`).
 */
export function Composer({ target, identity, sessionId }: { target: ComposerTarget; identity: string | null | undefined; sessionId: string }) {
  const message = useMessageDraft({ identity, operation: "session.followup", sessionId, prefix: target.kind === "crew" ? "crew" : "fu" });
  const draft = message.draft;
  const device = useHubWrite<Record<string, unknown>, { seq?: number; mode?: string; duplicate?: boolean }>({
    path: WRITE_PATHS.nodeFollowup,
    label: "Your follow-up",
    invalidates: [NODES_QUERY_KEY, sessionQueryKeys.detail(sessionId)],
    successMessage: (data) => (data?.duplicate ? "That message was already sent." : data?.mode === "steer" ? "Steer sent." : "Follow-up queued."),
  });
  const crew = useHubWrite<{ session_id: string; body: string; message_id: string }, { duplicate?: boolean }>({
    path: WRITE_PATHS.crewIntervene,
    label: "Your message",
    invalidates: [CREW_LIVE_QUERY_KEY, sessionQueryKeys.crew(sessionId)],
    successMessage: (data) => (data?.duplicate ? "That message was already recorded." : "Message sent to the crew."),
  });
  const text = draft.value.text;
  const body = text.trim();

  if (target.kind === "none") {
    return (
      <section aria-labelledby="composer-heading" className="rounded-md border border-[var(--ua-border)] p-3">
        <h2 id="composer-heading" className="m-0 text-sm font-semibold">
          Send a follow-up
        </h2>
        <p className="m-0 mt-1 text-sm text-[var(--ua-muted)]">{target.reason}</p>
      </section>
    );
  }

  const caps = target.kind === "device" ? (target.control?.capabilities ?? {}) : {};
  const started = target.kind === "device" && target.item.status === "claimed";
  const queueOk = target.kind === "crew" || (!!caps.queue && USABLE.has(caps.queue.state));
  const steerOk = target.kind === "device" && !!caps.steer && USABLE.has(caps.steer.state) && started;
  const write = target.kind === "device" ? device : crew;

  /** The gate the Send and Steer buttons show; every way to send (the buttons, Ctrl/Cmd+Enter) goes through it. */
  const canSend = (mode: "queue" | "steer") => Boolean(body) && !write.isRunning && (mode === "queue" ? queueOk : steerOk);

  const send = async (mode: "queue" | "steer") => {
    if (!canSend(mode)) return;
    if (target.kind === "crew") {
      // One message_id per message, reused on retry and by an approval replay (the hub dedupes on it).
      const attempt = message.prepare(body, "crew", sessionId);
      const outcome = await crew.run({ session_id: sessionId, body, message_id: attempt.key });
      message.settle(attempt, outcome);
      return;
    }
    // The key, body, mode and item are stored with the draft before sending,
    // so a retry after a lost answer or a navigation reuses the same key.
    const attempt = message.prepare(body, mode, target.item.id);
    const outcome = await device.run({ item_id: target.item.id, mode, body, idempotency_key: attempt.key });
    message.settle(attempt, outcome);
  };

  const hints: string[] = [];
  if (target.kind === "crew") hints.push("Your message goes into the session's crew conversation.");
  else {
    hints.push(
      caps.queue
        ? `Send waits until the agent finishes its current step${USABLE.has(caps.queue.state) ? "" : `; not available here: ${caps.queue.detail}`}.`
        : "This device has not said whether it can take queued messages.",
    );
    hints.push(
      !caps.steer
        ? "Steer is not reported for this device."
        : !started
          ? "Steer becomes available once the agent starts."
          : `Steer interrupts the agent and redirects it now${USABLE.has(caps.steer.state) ? "" : `; not available here: ${caps.steer.detail}`}.`,
    );
  }

  return (
    <section aria-labelledby="composer-heading" className="rounded-md border border-[var(--ua-border)] p-3">
      <h2 id="composer-heading" className="m-0 mb-2 text-sm font-semibold">
        Send a follow-up
      </h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send("queue");
        }}
        className="flex flex-col gap-2"
      >
        <Field label="Message" labelHidden hint={hints.join(" ")}>
          <Textarea
            value={text}
            maxLength={FOLLOWUP_MAX}
            rows={3}
            placeholder={`Message ${target.kind === "device" ? target.item.agent : "the crew"}`}
            onChange={(e) => message.setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void send("queue");
              }
            }}
          />
        </Field>
        {draft.restored && text ? <p className="m-0 text-xs text-[var(--ua-muted)]">Your unsent message was restored.</p> : null}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" variant="primary" disabled={!body || !queueOk} loading={write.isRunning}>
            Send
          </Button>
          {target.kind === "device" ? (
            <Button variant="secondary" disabled={!body || !steerOk || write.isRunning} onClick={() => void send("steer")}>
              Steer now
            </Button>
          ) : null}
          <span className="text-xs text-[var(--ua-muted)]">Ctrl+Enter sends</span>
        </div>
        <WriteStatus state={write.state} label="Your follow-up" formHref={`/sessions/${encodeURIComponent(sessionId)}`} />
      </form>
    </section>
  );
}
