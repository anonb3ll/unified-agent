/**
 * Provenance (spec §9.7, under Audit): who authorized what, on which
 * machine, by which agent, with which proof, as a graph with a table
 * equivalent (`GET /api/platform/provenance-graph`). Retains the legacy
 * Provenance pane: the three query shapes (task, project or time window; by
 * person; by release), every filter, counts per kind, the scope and graph
 * fingerprints, and every node and link.
 */
import { useRef, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, DescriptionList, Field, GraphCanvas, Input, Select, type GraphEdge, type GraphNode } from "../../lib/ui";
import { localDateTimeToUtc, utcDateTimeToLocal } from "../../lib/provenance/filters";
import { isUnverified, kindLabel, nodeSummary, relationLabel } from "../../lib/provenance/types";
import { useDraft } from "../lib/drafts";
import { useIdentity } from "../lib/identity";
import { useUrlParams } from "../lib/urlState";
import { EMPTY_PROVENANCE_FILTERS, fetchProvenance, provenanceQuery, SETTINGS_KEYS, type ProvenanceFilters, type ProvenanceGraphPayload } from "./api";
import { AuditTabs, plural, Section, SettingsHeader, SettingsRegion, useLatest } from "./shared";

const QUERY_OPTIONS = [
  { value: "", label: "A task, a project or a time window" },
  { value: "by_human", label: "What one person authorized" },
  { value: "by_release", label: "One release (a set of tasks)" },
];

type FieldDef = { key: keyof ProvenanceFilters; label: string; hint?: string; type?: "datetime-local"; numeric?: boolean };

const FIELDS: Record<ProvenanceFilters["query"], FieldDef[]> = {
  "": [
    { key: "task_id", label: "Task number", numeric: true },
    { key: "project_id", label: "Project task number", hint: "Includes every task under it.", numeric: true },
    { key: "agent", label: "Agent" },
    { key: "pr", label: "Pull request or commit" },
    { key: "since", label: "From", type: "datetime-local" },
    { key: "until", label: "To", type: "datetime-local" },
    { key: "limit", label: "Most records", numeric: true },
  ],
  by_human: [
    { key: "human", label: "Person", hint: "The person whose approvals to follow." },
    { key: "since", label: "From", type: "datetime-local" },
    { key: "until", label: "To", type: "datetime-local" },
    { key: "limit", label: "Most records", numeric: true },
  ],
  by_release: [
    { key: "task_id", label: "Task number", numeric: true },
    { key: "task_ids", label: "Or several task numbers", hint: "Separated by commas, for example 12, 14, 15." },
  ],
};

const TONE: Record<string, GraphNode["tone"]> = { human: "decision", device: "default", agent: "live", task: "default", action: "default", approval: "warn", work_product: "ok" };

export function toGraph(data: ProvenanceGraphPayload): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes = data.nodes.map((n) => ({
    id: n.id,
    label: `${kindLabel(n.kind)}: ${n.label || n.id}`,
    description: [nodeSummary(n), isUnverified(n) ? "not verified" : null].filter(Boolean).join(" · ") || undefined,
    tone: isUnverified(n) ? ("danger" as const) : (TONE[n.kind] ?? "default"),
  }));
  const edges = data.edges.map((e, i) => ({ id: `${e.source}->${e.target}:${e.relation}:${i}`, source: e.source, target: e.target, label: relationLabel(e.relation) }));
  return { nodes, edges };
}

export function ProvenancePage() {
  const { values, update, clear } = useUrlParams<keyof ProvenanceFilters>(EMPTY_PROVENANCE_FILTERS);
  const filters = values as ProvenanceFilters;
  const query = useQuery({ queryKey: [...SETTINGS_KEYS.provenance, filters] as const, queryFn: ({ signal }) => fetchProvenance(filters, signal) });
  return (
    <>
      <SettingsHeader title="Provenance" description="Where each piece of work came from: the person who authorized it, the machine, the agent, the approvals and what it produced." />
      <AuditTabs />
      <div className="mt-4 flex flex-col gap-4">
        <ProvenanceForm filters={filters} onApply={(f) => update(f, { push: true })} onClear={clear} />
        <SettingsRegion
          query={query}
          what="the provenance graph"
          loadingLabel="Loading the provenance graph"
          errorTitle="The provenance graph could not be loaded"
          isEmpty={(d) => d.nodes.length === 0}
          empty={{ title: "Nothing matches", description: "No recorded work matches this query. Widen the dates or change the filters." }}
        >
          {(data) => <ProvenanceResult data={data} />}
        </SettingsRegion>
      </div>
    </>
  );
}

function ProvenanceForm({ filters, onApply, onClear }: { filters: ProvenanceFilters; onApply: (f: ProvenanceFilters) => void; onClear: () => void }) {
  const identity = useIdentity();
  const draft = useDraft<ProvenanceFilters>({
    identity,
    operation: "settings.provenance.filters",
    object: provenanceQuery(filters),
    initial: { ...filters, since: utcDateTimeToLocal(filters.since), until: utcDateTimeToLocal(filters.until) },
  });
  const latest = useLatest(draft.value);
  const [problem, setProblem] = useState<{ key: keyof ProvenanceFilters; text: string } | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const fields = FIELDS[draft.value.query] ?? FIELDS[""];
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const v = latest.current;
    const bad = fields.find((f) => f.numeric && v[f.key].trim() && !/^\d+$/.test(v[f.key].trim()));
    if (bad) {
      setProblem({ key: bad.key, text: "Use digits only." });
      formRef.current?.querySelector<HTMLInputElement>(`#provenance-${bad.key}`)?.focus();
      return;
    }
    if (v.query === "by_release" && !v.task_id.trim() && !v.task_ids.trim()) {
      setProblem({ key: "task_id", text: "Enter a task number, or several." });
      formRef.current?.querySelector<HTMLInputElement>("#provenance-task_id")?.focus();
      return;
    }
    setProblem(null);
    // Only the fields of the chosen query shape are sent.
    const next: ProvenanceFilters = { ...EMPTY_PROVENANCE_FILTERS, query: v.query };
    for (const f of fields) next[f.key as Exclude<keyof ProvenanceFilters, "query">] = f.type ? localDateTimeToUtc(v[f.key]) : v[f.key].trim();
    onApply(next);
  };
  return (
    <form ref={formRef} noValidate onSubmit={submit} aria-label="Provenance query" className="flex flex-col gap-3 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4">
      <h2 className="m-0 text-base font-semibold">Query</h2>
      <Field id="provenance-query" label="Show">
        <Select options={QUERY_OPTIONS} value={draft.value.query} onChange={(e) => draft.setValue((prev) => ({ ...prev, query: e.target.value as ProvenanceFilters["query"] }))} />
      </Field>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {fields.map((f) => (
          <Field key={f.key} id={`provenance-${f.key}`} label={f.label} hint={f.hint} error={problem?.key === f.key ? problem.text : undefined}>
            <Input
              type={f.type ?? "text"}
              inputMode={f.numeric ? "numeric" : undefined}
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
          Show
        </Button>
        <Button
          variant="secondary"
          onClick={() => {
            setProblem(null);
            draft.clear();
            onClear();
          }}
        >
          Clear
        </Button>
      </div>
    </form>
  );
}

function ProvenanceResult({ data }: { data: ProvenanceGraphPayload }) {
  const graph = toGraph(data);
  const counts = Object.entries(data.node_counts_by_kind).filter(([, n]) => n > 0);
  return (
    <div className="flex flex-col gap-4">
      <Section id="provenance-summary" title="Summary">
        <DescriptionList
          items={[
            { term: "Found", value: `${plural(data.node_count, "item")} and ${plural(data.edge_count, "link")}` },
            { term: "By kind", value: counts.map(([k, n]) => `${kindLabel(k)} ${n}`).join(", ") || null },
            { term: "Query fingerprint", value: <code className="break-all font-mono text-xs">{data.scope_hash || "–"}</code> },
            { term: "Graph fingerprint", value: <code className="break-all font-mono text-xs">{data.canonical_hash || "–"}</code> },
          ]}
        />
        <p className="m-0 text-xs text-[var(--ua-muted)]">The same query on the same records always gives the same graph fingerprint, so a copy can be checked against the hub.</p>
      </Section>
      <Section id="provenance-graph" title="Graph" description="Read each link from left to right. A person or machine that could not be verified is marked.">
        <GraphCanvas
          label="Provenance graph"
          nodes={graph.nodes}
          edges={graph.edges}
          defaultView={graph.nodes.length > 60 ? "table" : "graph"}
          legend={[
            { tone: "decision", label: "Person" },
            { tone: "live", label: "Agent" },
            { tone: "warn", label: "Approval" },
            { tone: "ok", label: "Work product" },
            { tone: "danger", label: "Not verified" },
          ]}
        />
      </Section>
    </div>
  );
}
