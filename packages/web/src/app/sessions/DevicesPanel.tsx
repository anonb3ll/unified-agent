import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchNativeSessions, NATIVE_SESSIONS_QUERY_KEY, NODES_QUERY_KEY, type NativeSession } from "../../lib/api/nodes";
import { Button, Dialog, Field, Select, Textarea } from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useSignedInIdentity } from "../lib/identity";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { sessionQueryKeys, WRITE_PATHS, type NodeStartResult } from "./api";
import { formatWhen } from "./model";
import { KeptNewerText, SectionHeading, useLatest, WriteStatus } from "./parts";

/**
 * Claude Code and Codex sessions that devices found on disk (legacy
 * Sessions "Native sessions", task #1741). Adopting one records it under the
 * signed-in identity and continues it with a new message.
 */
export function DevicesPanel() {
  const query = useQuery({ queryKey: NATIVE_SESSIONS_QUERY_KEY, queryFn: ({ signal }) => fetchNativeSessions(signal) });
  const [adopting, setAdopting] = useState<NativeSession | null>(null);
  return (
    <section aria-labelledby="devices-heading">
      <SectionHeading id="devices-heading">Sessions found on your devices</SectionHeading>
      <p className="m-0 mb-3 text-sm text-[var(--ua-muted)]">
        Terminal sessions a connected device found in its work folders. Adopt one to bring it under the hub and continue it from here.
      </p>
      <QueryRegion
        query={query}
        loadingLabel="Loading sessions found on devices"
        errorTitle="Device sessions could not be loaded"
        headingLevel={3}
        isEmpty={(data) => data.sessions.length === 0}
        empty={{
          title: "No sessions found on devices",
          description: "A device lists the Claude Code and Codex sessions in its work folders once it is connected to the hub.",
        }}
      >
        {(data) => (
          <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Sessions found on devices">
            {data.sessions.map((row) => (
              <li key={`${row.node}:${row.external_session_id}`} className="rounded-md border border-[var(--ua-border)] p-3 text-sm">
                <p className="m-0 font-medium">
                  {row.title || `${row.harness === "claude" ? "Claude Code" : "Codex"} session`} on {row.node}
                </p>
                <p className="m-0 break-all text-xs text-[var(--ua-muted)]">
                  {row.cwd} · last active {formatWhen(row.last_activity)}
                </p>
                {row.adopted ? (
                  <p className="m-0 mt-1 text-xs">
                    Adopted by {row.adopted.agent} ({row.adopted.status}).
                  </p>
                ) : null}
                {row.adoptable ? (
                  <Button size="sm" className="mt-2" onClick={() => setAdopting(row)} aria-label={`${row.adopted ? "Resume" : "Adopt"} ${row.harness} session on ${row.node}`}>
                    {row.adopted ? "Resume" : "Adopt"}
                  </Button>
                ) : (
                  <p className="m-0 mt-1 text-xs text-[var(--ua-muted)]">Cannot be adopted: {row.reason ?? "the device did not say why"}.</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </QueryRegion>
      {adopting ? <AdoptDialog row={adopting} onClose={() => setAdopting(null)} /> : null}
    </section>
  );
}

function AdoptDialog({ row, onClose }: { row: NativeSession; onClose: () => void }) {
  // The message is a draft keyed by identity and the session found on disk (review 14).
  const identity = useSignedInIdentity();
  const draft = useDraft<{ prompt: string }>({
    identity,
    operation: "session.adopt",
    object: { node: row.node, harness: row.harness, session: row.external_session_id },
    initial: { prompt: "" },
  });
  const prompt = draft.value.prompt;
  const setPrompt = (value: string) => draft.setValue({ prompt: value });
  const [backend, setBackend] = useState(row.backends[0] ?? "");
  const write = useHubWrite<Record<string, unknown>, NodeStartResult>({
    path: WRITE_PATHS.nodeAdopt,
    label: "Adopting the session",
    invalidates: [NATIVE_SESSIONS_QUERY_KEY, NODES_QUERY_KEY, sessionQueryKeys.all],
    successMessage: "Session adopted. Your message is queued.",
  });
  // Clear and close only if the text is still what was sent (review 2, finding 10).
  const latest = useLatest(draft.value);
  const [kept, setKept] = useState(false);
  const submit = async () => {
    if (!prompt.trim()) return;
    const sent = JSON.stringify(draft.value);
    setKept(false);
    const outcome = await write.run({
      node: row.node,
      harness: row.harness,
      external_session_id: row.external_session_id,
      prompt: prompt.trim(),
      ...(backend ? { backend } : {}),
    });
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      if (JSON.stringify(latest.current) === sent) {
        draft.clear();
        onClose();
      } else setKept(true);
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={row.adopted ? "Resume this session" : "Adopt this session"}
      description={`${row.harness === "claude" ? "Claude Code" : "Codex"} session on ${row.node}, in ${row.cwd}.`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={write.isRunning} disabled={!prompt.trim()}>
            {row.adopted ? "Resume" : "Adopt and send"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label="Message to continue with" required hint="The agent receives this as its next instruction.">
          <Textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={4} />
        </Field>
        {row.backends.length > 1 ? (
          <Field label="How to connect" hint={row.backend_note ?? undefined}>
            <Select value={backend} onChange={(e) => setBackend(e.target.value)} options={row.backends.map((b) => ({ value: b, label: b }))} />
          </Field>
        ) : null}
        <KeptNewerText shown={kept} />
        <WriteStatus state={write.state} label="Adopting this session" />
      </div>
    </Dialog>
  );
}
