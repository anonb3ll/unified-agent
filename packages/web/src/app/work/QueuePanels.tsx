/**
 * The task list's hub-side reads that the classic queue pane showed (review
 * round 2, item 15): ranked search over every task (`GET /api/tasks/search`,
 * not only the board window) with why each matched, and the queue's health
 * buckets with back-pressure diagnostics (`GET /api/queue-health`).
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { fetchQueueHealth, type QueueHealthWithPressure } from "../../lib/api/queue";
import { fetchTaskSearch, type TaskSearchResult } from "../../lib/api/tasks";
import { UiLink } from "../../lib/ui";
import { QueryRegion } from "../lib/queries";
import { statusLabel } from "./model";
import { Section } from "./shared";

const WHY_LABEL: Record<string, string> = { lexical: "Matched words", semantic: "Similar meaning", recency: "Recently active" };

/** The hub's snippet marks matches with tags; show it as plain text. */
const plain = (html: string) => html.replace(/<[^>]*>/g, "");

function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** Ranked results for the search box, from the hub's search over every task. */
export function SearchResults({ q }: { q: string }) {
  const query = useDebounced(q.trim(), 300);
  const search = useQuery({
    queryKey: ["work", "search", query],
    queryFn: ({ signal }) => fetchTaskSearch(query, signal),
    enabled: query.length >= 2,
    staleTime: 15_000,
  });
  if (query.length < 2) return null;
  return (
    <Section title="Search results" id="work-search">
      <p className="m-0 text-xs text-[var(--ua-muted)]">
        Ranked by the hub across every task, including done and archived ones outside the views below.
      </p>
      <QueryRegion
        query={search}
        loadingLabel="Searching tasks"
        errorTitle="Search could not run"
        headingLevel={3}
        isEmpty={(d) => d.tasks.length === 0}
        empty={{ title: "No tasks match", description: "Try other words." }}
      >
        {(d) => (
          <ol className="m-0 flex list-none flex-col gap-2 p-0 text-sm">
            {d.tasks.map((t: TaskSearchResult) => (
              <li key={t.id} className="flex flex-col gap-0.5">
                <span>
                  <UiLink href={`/work/tasks/${t.id}`} className="text-[var(--ua-link)] underline">
                    #{t.id} {t.title}
                  </UiLink>{" "}
                  <span className="text-[var(--ua-muted)]">· {statusLabel(String(t.status))}</span>
                </span>
                <span className="text-xs text-[var(--ua-muted)]">
                  {(t.explanation?.why ?? []).map((w) => WHY_LABEL[w] ?? w).join(", ")}
                  {t.explanation?.snippet ? ` · “${plain(t.explanation.snippet)}”` : ""}
                </span>
              </li>
            ))}
          </ol>
        )}
      </QueryRegion>
      {search.data?.truncated && <p className="m-0 text-xs">Only the best {search.data.limit ?? 20} matches are shown.</p>}
    </Section>
  );
}

const BUCKETS: readonly { key: keyof QueueHealthWithPressure["counts"]; label: string; help: string }[] = [
  { key: "stale_claims", label: "Stale claims", help: "claimed, but the claim's lock has lapsed" },
  { key: "blocked_archived", label: "Blocked or archived", help: "blocked, or marked archived in the title" },
  { key: "waiting_review", label: "Waiting for review", help: "done by an agent, waiting on a reviewer" },
  { key: "inconsistent", label: "Inconsistent", help: "status and claim disagree" },
  { key: "laptop_lane", label: "Laptop queue", help: "only the laptop picks these up" },
  { key: "operator_only", label: "Needs the operator", help: "only a person can do these" },
];

/** The queue's buckets and back-pressure, as the classic queue pane showed them. */
export function QueueHealthPanel() {
  const health = useQuery({ queryKey: ["work", "queue-health"], queryFn: ({ signal }) => fetchQueueHealth(signal), staleTime: 15_000 });
  return (
    <Section title="Queue health" id="work-queue-health">
      <QueryRegion query={health} loadingLabel="Loading queue health" errorTitle="Queue health could not be loaded" headingLevel={3} empty={{ title: "" }}>
        {(d) => (
          <div className="flex flex-col gap-2 text-sm">
            {d.backpressure && (
              <div>
                <p className="m-0">
                  {d.backpressure.level === "pressure" ? <strong>The queue is under pressure.</strong> : d.backpressure.level === "ok" ? "The queue is flowing." : "Queue pressure is unknown."}
                </p>
                {d.backpressure.signals.length > 0 && (
                  <ul aria-label="Pressure signals" className="m-0 pl-5">
                    {d.backpressure.signals.map((x) => (
                      <li key={x}>{x}</li>
                    ))}
                  </ul>
                )}
                {d.backpressure.notes.length > 0 && (
                  <ul aria-label="Queue notes" className="m-0 pl-5 text-[var(--ua-muted)]">
                    {d.backpressure.notes.map((x) => (
                      <li key={x}>{x}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            <div className="flex flex-col gap-1">
              {BUCKETS.filter((b) => (d.counts[b.key] ?? 0) > 0 || b.key === "stale_claims" || b.key === "blocked_archived").map((b) => {
                const rows = (d[b.key as keyof QueueHealthWithPressure] as { id: number; title: string; claimed_by?: string | null }[] | undefined) ?? [];
                return (
                  <details key={b.key}>
                    <summary className="cursor-pointer">
                      {b.label}: {d.counts[b.key]} <span className="text-xs text-[var(--ua-muted)]">({b.help})</span>
                    </summary>
                    <ul className="m-0 mt-1 flex list-none flex-col gap-0.5 pl-4">
                      {rows.map((t) => (
                        <li key={t.id}>
                          <UiLink href={`/work/tasks/${t.id}`} className="text-[var(--ua-link)] underline">
                            #{t.id} {t.title}
                          </UiLink>
                          {t.claimed_by ? <span className="text-[var(--ua-muted)]"> · {t.claimed_by}</span> : null}
                        </li>
                      ))}
                    </ul>
                  </details>
                );
              })}
            </div>
          </div>
        )}
      </QueryRegion>
    </Section>
  );
}
