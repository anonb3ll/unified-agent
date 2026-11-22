import { useCallback, useMemo, useState } from "react";
import { useParams } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchCrewLive, CREW_LIVE_QUERY_KEY, type CrewMessage } from "../../lib/api/crew";
import { fetchEvents, fetchNodes, NODES_QUERY_KEY } from "../../lib/api/nodes";
import type { ActivityEvent, SessionRow } from "../../lib/sessions/types";
import { Button, DescriptionList, Drawer, EmptyState, ErrorState, Field, LinkButton, PageHeader, Skeleton, StatePill, Textarea, UiLink } from "../../lib/ui";
import { httpStatusOf } from "../../lib/ui/truthState";
import { useSignedInIdentity } from "../lib/identity";
import { useHubWrite } from "../lib/mutations";
import { describeReadError } from "../lib/queries";
import { fetchSession, fetchSessionList, sessionQueryKeys, WRITE_PATHS } from "./api";
import { Composer, composerTarget } from "./Composer";
import { useLiveEvents } from "./live";
import { buildTranscript, filesChanged, formatWhen, hostOf, isOpen, lastActivity, nodeItemId, pullRequestOf, sessionState, usageOf, changeWord } from "./model";
import { LiveIndicator, useIsOperator, useWhoami, WriteStatus } from "./parts";
import { SessionActions } from "./SessionActions";
import { Transcript } from "./Transcript";
import { useItemTranscript } from "./itemTranscript";
import { useMessageDraft } from "./messageDraft";


/** `/sessions/:id`: the conversation, its live state, the actions and the facts (spec §9.4). */
export function SessionPage() {
  const { id = "" } = useParams();
  const session = useQuery({ queryKey: sessionQueryKeys.detail(id), queryFn: ({ signal }) => fetchSession(id, signal), enabled: id !== "" });
  const title = `Session ${id}`;

  if (session.isPending) {
    return (
      <>
        <PageHeader title={title} description="One agent session: the conversation, the task and what it changed." />
        <Skeleton label="Loading the session" lines={6} />
      </>
    );
  }
  if (session.isError) {
    const notFound = httpStatusOf(session.error) === 404;
    const { message, detail } = describeReadError(session.error);
    return (
      <>
        <PageHeader title={title} description="One agent session: the conversation, the task and what it changed." />
        {notFound ? (
          <EmptyState
            title="No session with this id"
            description="It may have been removed, or the link is wrong. The session list shows every session the hub knows about."
            action={<LinkButton href="/sessions">Back to sessions</LinkButton>}
          />
        ) : (
          <ErrorState title="The session could not be loaded" message={message} detail={detail} onRetry={() => void session.refetch()} />
        )}
      </>
    );
  }
  // Keyed by session: moving between cached session routes starts a fresh view (review 8).
  return <SessionView key={session.data.id} session={session.data} />;
}

function SessionView({ session }: { session: SessionRow }) {
  const queryClient = useQueryClient();
  const whoami = useWhoami();
  const operator = useIsOperator();
  const identity = whoami.data?.agent ?? null;
  const draftIdentity = useSignedInIdentity();
  const itemId = nodeItemId(session);
  const open = isOpen(session.status);

  // Idle-wake state is only on the list read (`sessions_payload`), not the
  // detail (`registry.get_session`); read it from the agent's list until the
  // detail carries it (hub task #2498). Review finding 10.
  const listFilter = { agent: session.agent, status: "all" };
  const listed = useQuery({ queryKey: sessionQueryKeys.list(listFilter), queryFn: ({ signal }) => fetchSessionList(listFilter, signal) });
  const wake = listed.data?.sessions.find((s) => s.id === session.id)?.wake ?? null;

  const nodes = useQuery({ queryKey: NODES_QUERY_KEY, queryFn: ({ signal }) => fetchNodes(signal), enabled: itemId !== null });
  const item = itemId ? (nodes.data?.recent_items.find((i) => i.id === itemId) ?? null) : null;
  const control = item ? (nodes.data?.nodes.find((n) => n.node === item.node)?.controls?.[item.backend] ?? null) : null;

  const transcript = useItemTranscript(itemId);
  const history = transcript.history;
  const crew = useQuery({
    queryKey: sessionQueryKeys.crew(session.id),
    queryFn: ({ signal }) => fetchCrewLive(session.id, signal),
    retry: false,
  });
  const crewMessages: CrewMessage[] = crew.data?.messages ?? [];
  const hasCrew = crew.isSuccess && (crew.data.live || crewMessages.length > 0 || crew.data.worktree_interventions.length > 0);

  const onEvent = useCallback(
    (event: ActivityEvent) => {
      const payload = event.payload as { item_id?: unknown; session_id?: unknown } | null;
      if (event.channel === "agent.node") {
        if (transcript.push(event)) void queryClient.invalidateQueries({ queryKey: NODES_QUERY_KEY });
        return;
      }
      // Session lifecycle and crew messages for this session.
      void queryClient.invalidateQueries({ queryKey: sessionQueryKeys.detail(session.id) });
      if (event.channel.startsWith("agent.message") || event.channel.startsWith("agent.crew")) {
        void queryClient.invalidateQueries({ queryKey: sessionQueryKeys.crew(session.id) });
      }
    },
    [transcript.push, queryClient, session.id],
  );
  const liveState = useLiveEvents(["agent.node", "agent.session*", "agent.message*", "agent.crew*", "agent.wake.*"], onEvent, transcript.connected);

  const events = transcript.events;
  const entries = useMemo(
    () => buildTranscript({ events, item, crew: crewMessages, agent: session.agent }),
    [events, item, crewMessages, session.agent],
  );
  const [crewOpen, setCrewOpen] = useState(false);

  const state = sessionState(session.status);
  const usage = usageOf(session, events);
  const files = filesChanged(entries);
  const pr = pullRequestOf(session);
  const host = hostOf(session);

  return (
    <>
      <PageHeader
        title={`Session ${session.id}`}
        meta={
          <span className="inline-flex flex-wrap items-center gap-2">
            <StatePill kind={state.kind} label={state.label} />
            <span>{session.agent}</span>
          </span>
        }
        description={session.summary || (session.task_title ? `Working on: ${session.task_title}` : "One agent session: the conversation, the task and what it changed.")}
        actions={<LinkButton href="/sessions">All sessions</LinkButton>}
      />
      <div className="mb-4">
        <SessionActions
          session={{ ...session, wake }}
          item={item}
          identity={identity}
          draftIdentity={draftIdentity}
          isOperator={operator.isSuccess ? operator.data : operator.isError ? false : null}
          hasCrew={hasCrew}
          onOpenCrew={() => setCrewOpen(true)}
        />
      </div>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <section aria-labelledby="conversation-heading" className="flex min-w-0 flex-col gap-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 id="conversation-heading" className="m-0 text-base font-semibold">
              Conversation
            </h2>
            <LiveIndicator state={liveState} />
          </div>
          {itemId && history.isPending ? (
            <Skeleton label="Loading the conversation" lines={5} />
          ) : (
            <>
              {itemId && history.isError ? (
                <ErrorState
                  headingLevel={3}
                  title="Earlier messages could not be loaded"
                  message={`${describeReadError(history.error).message} New messages still appear below as they arrive.`}
                  detail={describeReadError(history.error).detail}
                  onRetry={() => void history.refetch()}
                />
              ) : null}
              {transcript.truncated ? (
                <p className="m-0 text-xs text-[var(--ua-muted)]">This is a long session: only the most recent part of it is shown.</p>
              ) : null}
              <Transcript entries={entries} agent={session.agent} />
            </>
          )}
          <Composer
            sessionId={session.id}
            identity={draftIdentity}
            target={composerTarget({ sessionId: session.id, open, item, itemKnown: nodes.isSuccess, control, hasCrew })}
          />
        </section>
        <aside aria-labelledby="facts-heading" className="min-w-0">
          <h2 id="facts-heading" className="m-0 mb-3 text-base font-semibold">
            About this session
          </h2>
          <DescriptionList
            items={[
              {
                term: "Task",
                value:
                  session.task_id != null ? (
                    <UiLink href={`/work/tasks/${session.task_id}`} className="underline">
                      #{session.task_id}
                      {session.task_title ? ` ${session.task_title}` : ""}
                    </UiLink>
                  ) : (
                    "No task"
                  ),
              },
              {
                term: "Agent",
                value: (
                  <UiLink href={`/agents/profile/${encodeURIComponent(session.agent)}`} className="underline">
                    {session.agent}
                  </UiLink>
                ),
              },
              { term: "Host", value: host ?? "Not reported" },
              { term: "Started", value: formatWhen(session.started_at) },
              { term: "Last activity", value: formatWhen(lastActivity(session)) },
              ...(session.ended_at ? [{ term: "Ended", value: formatWhen(session.ended_at) }] : []),
              ...(session.model ? [{ term: "Model", value: session.model }] : []),
              ...(usage.cost ? [{ term: "Cost", value: usage.cost }] : []),
              ...(usage.tokens ? [{ term: "Tokens", value: usage.tokens }] : []),
              ...(session.branch ? [{ term: "Branch", value: <span className="break-all font-mono text-xs">{session.branch}</span> }] : []),
              {
                term: "Pull request",
                value: pr ? (
                  <UiLink href={pr} target="_blank" rel="noreferrer" className="break-all underline">
                    {pr.replace(/^https?:\/\/(www\.)?/, "")}
                  </UiLink>
                ) : (
                  "None recorded"
                ),
              },
              {
                term: "Files changed",
                value: files.length ? (
                  <ul className="m-0 list-none p-0" aria-label="Files changed in this session">
                    {files.map((f) => (
                      <li key={f.path} className="break-all font-mono text-xs">
                        {changeWord(f.status)}: {f.path}
                      </li>
                    ))}
                  </ul>
                ) : (
                  "None reported"
                ),
              },
            ]}
          />
          <details className="mt-4 text-sm">
            <summary className="cursor-pointer">Technical details</summary>
            <DescriptionList
              className="mt-2"
              items={[
                { term: "Session id", value: <span className="break-all font-mono text-xs">{session.id}</span> },
                { term: "Kind", value: session.kind },
                { term: "Worktree", value: session.worktree_path ? <span className="break-all font-mono text-xs">{session.worktree_path}</span> : null },
                { term: "Harness session", value: session.external_session_id },
                { term: "Transcript file", value: session.transcript_path },
                { term: "Device work item", value: itemId },
                { term: "Evidence", value: item?.evidence_ref ?? null },
                { term: "Process", value: session.pid != null ? String(session.pid) : null },
                { term: "Exit status", value: session.exit_status },
              ]}
            />
          </details>
        </aside>
      </div>
      {crewOpen ? (
        <CrewDrawer sessionId={session.id} identity={draftIdentity} messages={crewMessages} interventions={crew.data?.worktree_interventions ?? []} onClose={() => setCrewOpen(false)} />
      ) : null}
    </>
  );
}

/** The crew view (legacy "Live crew", task #1025): the crew conversation and a message into it. */
function CrewDrawer({
  sessionId,
  identity,
  messages,
  interventions,
  onClose,
}: {
  sessionId: string;
  identity: string | null | undefined;
  messages: CrewMessage[];
  interventions: CrewMessage[];
  onClose: () => void;
}) {
  // The text and its message_id are kept with the draft (review 4, 14).
  const message = useMessageDraft({ identity, operation: "session.crew", sessionId, prefix: "crew" });
  const text = message.text;
  const write = useHubWrite<{ session_id: string; body: string; message_id: string }, { duplicate?: boolean; to_agent?: string }>({
    path: WRITE_PATHS.crewIntervene,
    label: "Your message to the crew",
    invalidates: [CREW_LIVE_QUERY_KEY, sessionQueryKeys.crew(sessionId)],
    successMessage: (data) => (data?.duplicate ? "That message was already recorded." : `Sent${data?.to_agent ? ` to ${data.to_agent}` : ""}.`),
  });
  return (
    <Drawer open onClose={onClose} title="Crew view" description="The messages between the agents working in this session.">
      <div className="flex flex-col gap-3">
        {messages.length === 0 ? (
          <p className="m-0 text-sm text-[var(--ua-muted)]">No crew messages yet.</p>
        ) : (
          <ol aria-label="Crew messages" className="m-0 flex list-none flex-col gap-2 p-0 text-sm">
            {messages.map((m, i) => (
              <li key={typeof m.message_id === "string" ? m.message_id : i} className="rounded-md border border-[var(--ua-border)] p-2">
                <p className="m-0 text-xs text-[var(--ua-muted)]">
                  {m.from_agent ?? "Unknown"} to {m.to_agent ?? "everyone"} · {formatWhen(typeof m.created_at === "string" ? m.created_at : null)}
                </p>
                <p className="m-0 whitespace-pre-wrap break-words">{typeof m.body === "string" ? m.body : ""}</p>
              </li>
            ))}
          </ol>
        )}
        {interventions.length ? (
          <p className="m-0 text-xs text-[var(--ua-muted)]">
            {interventions.length} message{interventions.length === 1 ? "" : "s"} left in the agent's worktree for it to pick up.
          </p>
        ) : null}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const body = text.trim();
            if (!body) return;
            const attempt = message.prepare(body, "crew", sessionId);
            void write.run({ session_id: sessionId, body, message_id: attempt.key }).then((o) => message.settle(attempt, o));
          }}
          className="flex flex-col gap-2"
        >
          <Field label="Message to the crew">
            <Textarea value={text} rows={3} onChange={(e) => message.setText(e.target.value)} />
          </Field>
          <Button type="submit" variant="primary" disabled={!text.trim()} loading={write.isRunning}>
            Send to crew
          </Button>
          <WriteStatus state={write.state} label="Your message" />
        </form>
      </div>
    </Drawer>
  );
}

