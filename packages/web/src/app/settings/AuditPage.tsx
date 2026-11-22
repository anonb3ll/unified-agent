/**
 * Audit (spec §9.7): the evidence ledger. Filters live in the URL; "Check
 * the ledger" re-runs the hub's chain verification (every `/api/evidence`
 * read recomputes the hash chain, `events.verify_event_chain`) and says the
 * result plainly; Export downloads the hub's own PDF or CSV with the active
 * filters (`/api/evidence?format=pdf|csv`). Retains the legacy Evidence pane:
 * every filter, the chain summary, each record's human, device, agent,
 * action and proof with its event hashes, the project chain and the
 * unattributed tasks left out.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, DataTable, Field, Input, StatePill, UiLink, type Column } from "../../lib/ui";
import { useStreamInvalidation } from "../../lib/polling";
import { localDateTimeToUtc, utcDateTimeToLocal } from "../../lib/provenance/filters";
import type { EvidenceChainIntegrity, EvidenceProjectChain, EvidenceRecord } from "../../lib/evidence/types";
import { useDraft } from "../lib/drafts";
import { useIdentity } from "../lib/identity";
import { useUrlParams } from "../lib/urlState";
import {
  downloadEvidence,
  EMPTY_EVIDENCE_FILTERS,
  EVIDENCE_DEFAULT_LIMIT,
  EVIDENCE_MAX_LIMIT,
  evidenceQuery,
  fetchEvidenceReport,
  SETTINGS_KEYS,
  type EvidenceFilters,
  type EvidencePayload,
} from "./api";
import { AuditTabs, Notice, plural, Section, SettingsHeader, SettingsRegion, useLatest } from "./shared";

const FILTER_FIELDS: { key: Exclude<keyof EvidenceFilters, "limit">; label: string; hint?: string; type: "text" | "datetime-local"; inputMode?: "numeric" }[] = [
  { key: "task_id", label: "Task number", type: "text", inputMode: "numeric", hint: "For example 899." },
  { key: "project", label: "Project", type: "text", hint: "For example unified-agent." },
  { key: "agent", label: "Agent", type: "text", hint: "For example Desktop_Agent1." },
  { key: "pr", label: "Pull request or commit", type: "text", hint: "A pull request address or a commit id." },
  { key: "item", label: "Session run", type: "text", hint: "A queued session run id, nd-…" },
  { key: "since", label: "From", type: "datetime-local" },
  { key: "until", label: "To", type: "datetime-local" },
];

/** The chain result, in plain words. */
export function chainVerdict(ci: EvidenceChainIntegrity): { tone: "ok" | "danger" | "unknown"; title: string; text: string } {
  if (ci.status === "tampered") {
    return {
      tone: "danger",
      title: "The ledger has been changed",
      text: `An entry does not match the one before it, or an entry was added outside the ledger. ${plural(ci.total_verified, "entry", "entries")} checked before the break. Tell the operator and keep an export of this report.`,
    };
  }
  if (ci.status === "empty") return { tone: "unknown", title: "The ledger has no entries yet", text: "Nothing has been recorded, so there is nothing to check." };
  if (ci.status === "valid" && ci.verified) {
    return {
      tone: "ok",
      title: "The ledger is intact",
      text: `${plural(ci.total_verified, "entry", "entries")} checked; each one links to the one before it and none was changed.${
        ci.unhashed_skipped ? ` ${plural(ci.unhashed_skipped, "older entry", "older entries")} from before the chain started cannot be checked.` : ""
      }`,
    };
  }
  return { tone: "unknown", title: "The ledger could not be checked", text: "The hub did not say whether the chain is intact. Check it again." };
}

export function AuditPage() {
  const queryClient = useQueryClient();
  const { values, update, clear } = useUrlParams<keyof EvidenceFilters>(EMPTY_EVIDENCE_FILTERS);
  const filters = values as EvidenceFilters;
  const query = useQuery({
    queryKey: [...SETTINGS_KEYS.evidence, filters] as const,
    queryFn: ({ signal }) => fetchEvidenceReport(filters, signal),
  });
  // New ledger entries arrive as hub events; refetch at most once per few seconds.
  useStreamInvalidation(() => void queryClient.invalidateQueries({ queryKey: SETTINGS_KEYS.evidence }));
  // The result of the last "Check the ledger now": set only from what the
  // refetch returned (TanStack's refetch reports an error without throwing).
  const [check, setCheck] = useState<{ ok: boolean; at: number } | null>(null);
  const [verifying, setVerifying] = useState(false);
  const onVerify = async () => {
    setVerifying(true);
    setCheck(null);
    const result = await query.refetch();
    setVerifying(false);
    setCheck(result.status === "success" && !result.isError ? { ok: true, at: result.dataUpdatedAt } : { ok: false, at: Date.now() });
  };

  return (
    <>
      <SettingsHeader
        title="Audit"
        description="The evidence ledger: what was done, who asked for it, on which machine, by which agent, and the proof. Each entry is chained to the one before, so a change shows."
      />
      <AuditTabs />
      <div className="mt-4 flex flex-col gap-4">
        <FilterForm filters={filters} onApply={(next) => update(next, { push: true })} onClear={() => clear()} />
        <SettingsRegion query={query} what="the evidence ledger" loadingLabel="Loading the evidence ledger" errorTitle="The evidence ledger could not be loaded">
          {(data) => (
            <>
              <ChainSection
                data={data}
                readAt={query.dataUpdatedAt}
                onVerify={() => void onVerify()}
                verifying={verifying}
                check={check}
                stale={query.status === "error"}
              />
              <ExportSection filters={filters} />
              <Records data={data} filters={filters} onShowMore={(limit) => update({ limit: String(limit) }, { push: true })} />
              {data.project_chain && <ProjectChain chain={data.project_chain} />}
            </>
          )}
        </SettingsRegion>
      </div>
    </>
  );
}

function FilterForm({ filters, onApply, onClear }: { filters: EvidenceFilters; onApply: (f: EvidenceFilters) => void; onClear: () => void }) {
  const identity = useIdentity();
  const applied = { ...filters, since: utcDateTimeToLocal(filters.since), until: utcDateTimeToLocal(filters.until) };
  // The draft belongs to the filter set on screen, so a shared link opens with its own filters.
  const draft = useDraft<EvidenceFilters>({ identity, operation: "settings.audit.filters", object: evidenceQuery(filters), initial: applied });
  const [problem, setProblem] = useState<string | null>(null);
  const taskRef = useRef<HTMLInputElement>(null);
  const latest = useLatest(draft.value);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const v = latest.current;
    if (v.task_id.trim() && !/^\d+$/.test(v.task_id.trim())) {
      setProblem("A task number has digits only.");
      taskRef.current?.focus();
      return;
    }
    setProblem(null);
    onApply({ ...v, since: localDateTimeToUtc(v.since), until: localDateTimeToUtc(v.until), limit: filters.limit });
  };
  return (
    <form noValidate onSubmit={submit} aria-label="Evidence filters" className="flex flex-col gap-3 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4">
      <h2 className="m-0 text-base font-semibold">Filters</h2>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {FILTER_FIELDS.map((f) => (
          <Field key={f.key} id={`evidence-${f.key}`} label={f.label} hint={f.hint} error={f.key === "task_id" ? (problem ?? undefined) : undefined}>
            <Input
              ref={f.key === "task_id" ? taskRef : undefined}
              type={f.type}
              inputMode={f.inputMode}
              value={draft.value[f.key]}
              onChange={(e) => {
                const value = e.target.value;
                draft.setValue((prev) => ({ ...prev, [f.key]: value }));
              }}
            />
          </Field>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary">
          Apply filters
        </Button>
        <Button
          variant="secondary"
          onClick={() => {
            setProblem(null);
            draft.clear();
            onClear();
          }}
        >
          Clear filters
        </Button>
      </div>
    </form>
  );
}

function clock(ms: number): string {
  return new Date(ms).toLocaleString();
}

function ChainSection({
  data,
  readAt,
  onVerify,
  verifying,
  check,
  stale,
}: {
  data: EvidencePayload;
  /** When the result on screen was read. */
  readAt: number;
  onVerify: () => void;
  verifying: boolean;
  check: { ok: boolean; at: number } | null;
  /** The latest read failed: what is on screen is an older result. */
  stale: boolean;
}) {
  const ci = data.chain_integrity;
  const verdict = chainVerdict(ci);
  // A result that could not be re-read is never presented as a current verdict.
  const old = stale || (check !== null && !check.ok);
  const pill = old
    ? { kind: "unknown" as const, label: "Not re-checked" }
    : verdict.tone === "ok"
      ? { kind: "ok" as const, label: "Intact" }
      : verdict.tone === "danger"
        ? { kind: "danger" as const, label: "Changed" }
        : { kind: "unknown" as const, label: "Not known" };
  return (
    <Section
      id="audit-chain"
      title="Is the ledger intact?"
      description="The hub re-checks every link of the chain each time the ledger is read."
      actions={
        <Button variant="secondary" onClick={onVerify} loading={verifying}>
          Check the ledger now
        </Button>
      }
    >
      <div className="flex flex-col gap-1" role="status" aria-live="polite">
        {old && (
          <p className="m-0 font-medium text-[var(--ua-danger)]">
            We could not re-check the ledger; the result below is from <time dateTime={new Date(readAt).toISOString()}>{clock(readAt)}</time>.
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <StatePill kind={pill.kind} label={pill.label} />
          <p className="m-0 font-semibold">{old ? `Last known result: ${verdict.title.charAt(0).toLowerCase()}${verdict.title.slice(1)}` : verdict.title}</p>
        </div>
        <p className="m-0 text-sm">{verdict.text}</p>
        <p className="m-0 text-xs text-[var(--ua-muted)]">
          {check?.ok ? `Checked again at ${clock(check.at)}. ` : `Read at ${clock(readAt)}. `}
          {data.generated_at ? `Report made ${data.generated_at}. ` : ""}
          Latest entry fingerprint <code className="break-all font-mono">{ci.head_hash ?? "none"}</code> · state <code className="font-mono">{ci.status}</code>
        </p>
      </div>
    </Section>
  );
}

function ExportSection({ filters }: { filters: EvidenceFilters }) {
  const [busy, setBusy] = useState<"pdf" | "csv" | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string; detail?: string | null } | null>(null);
  const run = async (format: "pdf" | "csv") => {
    setBusy(format);
    setMessage(null);
    try {
      const result = await downloadEvidence(filters, format);
      if (!result.ok) {
        setMessage({ tone: "error", text: result.message, detail: result.detail });
        return;
      }
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = result.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      setMessage({ tone: "ok", text: `Downloaded ${result.filename}.` });
    } catch (error) {
      setMessage({ tone: "error", text: "The download could not be saved. Try again.", detail: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(null);
    }
  };
  const active = Object.entries(filters).filter(([k, v]) => k !== "limit" && v.trim()).length;
  // The export is built from the same capped read as the list (`_evidence`:
  // default 50, at most 500), so it is never a complete copy of the ledger.
  const limit = Number(filters.limit) > 0 ? Math.min(Number(filters.limit), EVIDENCE_MAX_LIMIT) : EVIDENCE_DEFAULT_LIMIT;
  return (
    <Section
      id="audit-export"
      title="Export"
      description={`The hub's report of the records shown above, ${active ? `with ${plural(active, "active filter")}` : "with no filters"}: at most ${limit} records, the same as the list. It is not a complete copy of the ledger; to cover more, narrow the filters (for example by dates) and export each part. Signed when the hub has a signing key.`}
    >
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" onClick={() => void run("pdf")} loading={busy === "pdf"} disabled={busy !== null}>
          Export as PDF
        </Button>
        <Button variant="secondary" onClick={() => void run("csv")} loading={busy === "csv"} disabled={busy !== null}>
          Export as CSV
        </Button>
      </div>
      {message && (
        <Notice tone={message.tone === "ok" ? "info" : "danger"} role={message.tone === "ok" ? "status" : "alert"}>
          {message.text}
          {message.detail && (
            <details className="mt-1 text-xs">
              <summary className="cursor-pointer">Technical detail</summary>
              <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{message.detail}</pre>
            </details>
          )}
        </Notice>
      )}
    </Section>
  );
}

function who(value: string | null | undefined): string {
  return !value || value === "unattributed" || value === "—" ? "Not recorded" : value;
}

const RECORD_COLUMNS: Column<EvidenceRecord>[] = [
  {
    id: "task",
    header: "Task",
    cell: (r) => (
      <span className="flex flex-col">
        <UiLink href={`/work/tasks/${r.task_id}`} className="text-[var(--ua-link)] underline">
          Task {r.task_id}
        </UiLink>
        <span className="text-xs text-[var(--ua-muted)]">{r.project}</span>
      </span>
    ),
  },
  {
    id: "what",
    header: "What was done",
    cell: (r) => (
      <span className="flex min-w-[12rem] flex-col">
        <span className="break-words">{r.action.title || "Untitled"}</span>
        <span className="text-xs text-[var(--ua-muted)]">
          {r.action.status} · priority {r.action.priority}
          {r.action.approvals.length ? ` · ${plural(r.action.approvals.length, "approval")}` : ""}
        </span>
      </span>
    ),
  },
  { id: "human", header: "Asked by", cell: (r) => who(r.human) },
  { id: "device", header: "Machine", cell: (r) => who(r.device) },
  { id: "agent", header: "Agent", cell: (r) => who(r.agent) },
  {
    id: "proof",
    header: "Proof",
    cell: (r) => (
      <span className="flex min-w-[12rem] flex-col gap-1">
        <span className="flex flex-wrap items-center gap-2">
          {r.proof.pr_url ? (
            <a href={r.proof.pr_url} target="_blank" rel="noopener noreferrer" className="text-[var(--ua-link)] underline">
              Pull request<span className="sr-only"> (opens in a new tab)</span>
            </a>
          ) : null}
          {r.proof.commit_sha ? <code className="font-mono text-xs" title={r.proof.commit_sha}>{r.proof.commit_sha.slice(0, 7)}</code> : null}
          {r.proof.tamper_evident ? <Badge variant="success">Chained</Badge> : <Badge variant="warning">Not chained</Badge>}
        </span>
        {r.proof.event_hashes.length > 0 ? (
          <details>
            <summary className="cursor-pointer text-xs">{plural(r.proof.event_hashes.length, "ledger entry", "ledger entries")}</summary>
            <ul className="m-0 mt-1 flex list-none flex-col gap-1 p-0 text-xs">
              {r.proof.event_hashes.map((ev) => (
                <li key={ev.ulid} className="break-all">
                  <code className="font-mono">{ev.ulid}</code> · {ev.channel}
                  {ev.created_at ? ` · ${ev.created_at}` : ""} · <code className="font-mono" title={ev.entry_hash}>{ev.entry_hash.slice(0, 12)}…</code>
                </li>
              ))}
            </ul>
          </details>
        ) : (
          <span className="text-xs text-[var(--ua-muted)]">No ledger entries</span>
        )}
      </span>
    ),
  },
];

function Records({ data, filters, onShowMore }: { data: EvidencePayload; filters: EvidenceFilters; onShowMore: (limit: number) => void }) {
  const limit = Number(filters.limit) > 0 ? Math.min(Number(filters.limit), EVIDENCE_MAX_LIMIT) : EVIDENCE_DEFAULT_LIMIT;
  const capped = data.records.length >= limit;
  const moreRef = useRef<HTMLButtonElement>(null);
  const [focusMore, setFocusMore] = useState(false);
  useEffect(() => {
    if (focusMore) {
      moreRef.current?.focus();
      setFocusMore(false);
    }
  }, [focusMore, data]);
  return (
    <Section id="audit-records" title="Records" description="One record per task: who asked, on which machine, which agent did it, and the proof.">
      {data.records.length === 0 ? (
        <p className="m-0 text-sm text-[var(--ua-muted)]">No records match these filters.</p>
      ) : (
        <>
          <p className="m-0 text-sm" role="status">
            {capped ? `Showing the first ${data.records.length}. There may be more.` : plural(data.records.length, "record")}
          </p>
          <DataTable columns={RECORD_COLUMNS} data={data.records} getRowKey={(r) => String(r.task_id)} caption="Evidence records" scrollLabel="Evidence records (scrolls sideways)" />
          {capped &&
            (limit < EVIDENCE_MAX_LIMIT ? (
              <div>
                <Button
                  ref={moreRef}
                  variant="secondary"
                  onClick={() => {
                    setFocusMore(true);
                    onShowMore(EVIDENCE_MAX_LIMIT);
                  }}
                >
                  Show up to {EVIDENCE_MAX_LIMIT}
                </Button>
              </div>
            ) : (
              <p className="m-0 text-sm text-[var(--ua-muted)]">The hub lists at most {EVIDENCE_MAX_LIMIT} at once. Narrow the dates or other filters to see the rest.</p>
            ))}
        </>
      )}
      {data.unattributed_excluded.length > 0 && (
        <p className="m-0 text-xs text-[var(--ua-muted)]">
          {plural(data.unattributed_excluded.length, "task")} left out because no person or agent was recorded for {data.unattributed_excluded.length === 1 ? "it" : "them"}:{" "}
          {data.unattributed_excluded.map((id) => `task ${id}`).join(", ")}.
        </p>
      )}
    </Section>
  );
}

function ProjectChain({ chain }: { chain: EvidenceProjectChain }) {
  return (
    <Section id="audit-project" title={`Project chain for task ${chain.project_id}`} description="Every task under this project, in order, with the proof of each.">
      <p className="m-0 text-sm">
        {chain.status === "complete" ? <StatePill kind="ok" label="Complete" /> : <StatePill kind="warn" label="Partial" />} {chain.done_task_ids.length} of {chain.task_ids.length} tasks done
        {chain.merged_prs.length ? `, ${plural(chain.merged_prs.length, "pull request")} merged` : ""}.
      </p>
      <ol className="m-0 flex flex-col gap-1 pl-5 text-sm">
        {chain.steps.map((step) => (
          <li key={step.task_id}>
            <UiLink href={`/work/tasks/${step.task_id}`} className="text-[var(--ua-link)] underline">
              Task {step.task_id}
            </UiLink>{" "}
            {step.title} · {step.status}
            {step.proof.tamper_evident ? " · chained" : ""}
          </li>
        ))}
      </ol>
    </Section>
  );
}
