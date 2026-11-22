import { useEffect, useRef, useState } from "react";
import { cx } from "../../lib/ui";
import { changeWord, formatWhen, type TranscriptEntry } from "./model";

function Stamp({ at }: { at: string | null }) {
  return <span className="text-xs text-[var(--ua-muted)]">{at ? formatWhen(at) : "Time not reported"}</span>;
}

function Bubble({ entry }: { entry: TranscriptEntry }) {
  switch (entry.role) {
    case "operator":
    case "agent": {
      const mine = entry.role === "operator";
      return (
        <article
          aria-label={`${mine ? "Message to the agent" : "Message from the agent"}, ${entry.author}`}
          className={cx(
            "max-w-[min(42rem,92%)] rounded-lg border px-3 py-2 text-sm",
            mine ? "ml-auto border-[var(--ua-accent)] bg-[var(--ua-surface)]" : "mr-auto border-[var(--ua-border)] bg-[var(--ua-bg)]",
          )}
        >
          <header className="mb-1 flex flex-wrap items-baseline gap-2">
            <span className="font-medium">{entry.author}</span>
            {entry.role === "operator" && entry.note ? <span className="text-xs text-[var(--ua-muted)]">{entry.note}</span> : null}
            <Stamp at={entry.at} />
          </header>
          <p className="m-0 whitespace-pre-wrap break-words">{entry.text || "(empty message)"}</p>
        </article>
      );
    }
    case "tool":
      return (
        <details className="mr-auto max-w-[min(42rem,92%)] rounded-md border border-dashed border-[var(--ua-border)] px-3 py-1.5 text-xs">
          <summary className="cursor-pointer text-[var(--ua-muted)]">
            {entry.title} · <Stamp at={entry.at} />
          </summary>
          {entry.detail ? (
            <pre className="m-0 mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words font-mono" tabIndex={0}>
              {entry.detail.length > 4000 ? `${entry.detail.slice(0, 3999)}…` : entry.detail}
            </pre>
          ) : (
            <p className="m-0 mt-1 text-[var(--ua-muted)]">No detail was recorded.</p>
          )}
        </details>
      );
    case "changes":
      return (
        <details className="mr-auto max-w-[min(42rem,92%)] rounded-md border border-[var(--ua-border)] px-3 py-1.5 text-xs">
          <summary className="cursor-pointer">
            {entry.error
              ? "No change summary for this turn"
              : entry.files.length === 0
                ? "No files changed in this turn"
                : `${entry.total} file${entry.total === 1 ? "" : "s"} changed`}{" "}
            · <Stamp at={entry.at} />
          </summary>
          {entry.error ? <p className="m-0 mt-1">{entry.error}</p> : null}
          {entry.files.length ? (
            <ul className="m-0 mt-1 list-none p-0" aria-label="Changed files">
              {entry.files.map((f) => (
                <li key={f.path} className="break-all font-mono">
                  {changeWord(f.status)}: {f.path}
                  {f.withheld ? " (content withheld)" : ""}
                </li>
              ))}
            </ul>
          ) : null}
          {entry.diff ? (
            <pre className="m-0 mt-1 max-h-72 overflow-auto whitespace-pre font-mono" tabIndex={0} aria-label="Diff">
              {entry.diff}
            </pre>
          ) : null}
        </details>
      );
    case "status":
      return (
        <p className="m-0 text-center text-xs text-[var(--ua-muted)]">
          {entry.text} · <Stamp at={entry.at} />
        </p>
      );
  }
}

/**
 * The conversation, oldest first. New messages are announced politely by a
 * separate status line, never by moving focus; the log itself is not a live
 * region, so long tool output is not read aloud.
 */
export function Transcript({ entries, agent }: { entries: readonly TranscriptEntry[]; agent: string }) {
  // One announcement per new message id (review 16): a count would miss a
  // message that arrives as retention drops an old one, and a repeated string
  // is not read again. The last few are kept as separate nodes so each
  // arrival is an addition to the live region.
  const [announcements, setAnnouncements] = useState<{ id: string; text: string }[]>([]);
  const seen = useRef<Set<string> | null>(null);
  const messages = entries.filter((e): e is Extract<TranscriptEntry, { role: "agent" | "operator" }> => e.role === "agent" || e.role === "operator");
  const ids = messages.map((m) => m.id).join("|");
  useEffect(() => {
    if (seen.current === null) {
      seen.current = new Set(messages.map((m) => m.id));
      return;
    }
    const fresh = messages.filter((m) => !seen.current?.has(m.id));
    if (fresh.length === 0) return;
    for (const m of fresh) seen.current.add(m.id);
    setAnnouncements((prev) =>
      [
        ...prev,
        ...fresh.map((m) => ({
          id: m.id,
          text: `New message from ${m.role === "agent" ? agent : m.author}: ${m.text.trim().slice(0, 120)}`,
        })),
      ].slice(-3),
    );
    // `ids` stands for the message list, which is a new array every render.
  }, [ids, agent]);

  return (
    <>
      <div role="status" aria-live="polite" aria-atomic="false" aria-relevant="additions" className="sr-only">
        {announcements.map((a) => (
          <p key={a.id}>{a.text}</p>
        ))}
      </div>
      {entries.length === 0 ? (
        <p className="m-0 rounded-md border border-[var(--ua-border)] px-3 py-6 text-center text-sm text-[var(--ua-muted)]">
          No messages yet. Messages appear here as the agent works.
        </p>
      ) : (
        <ol aria-label="Conversation, oldest first" className="m-0 flex list-none flex-col gap-2 p-0">
          {entries.map((entry) => (
            <li key={entry.id} className="flex">
              <Bubble entry={entry} />
            </li>
          ))}
        </ol>
      )}
    </>
  );
}
