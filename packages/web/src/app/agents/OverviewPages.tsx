/**
 * Agents overview pages (spec §9.3): Skills (legacy Skills & Teams, with
 * publish, install and surface drift in `SkillActions.tsx`), the Org chart
 * (legacy Org, with policy revisions and rollback) and the agent Topology
 * (legacy Agent topology),
 * both on `GraphCanvas` with a table fallback, and the Capability catalog
 * (legacy Capabilities).
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { DataTable, GraphCanvas, layeredLayout, PageHeader, StatePill, UiLink, type Column, type GraphEdge, type GraphNode } from "../../lib/ui";
import type { CapabilityItem } from "../../lib/capabilities/types";
import type { SkillEntry } from "../../lib/skills-teams/types";
import { QueryRegion } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import { AGENTS_KEYS, fetchAgentGraph, fetchCapabilities, fetchOrg, fetchSkillsTeams, type AgentGraphPayload, type OrgPayload } from "./api";
import { formatWhen } from "./model";
import { PolicyRevisions } from "./PolicyRevisions";
import { DriftReport, SkillInstallSection, SkillPublishSection, SurfaceForm } from "./SkillActions";
import { AgentsTabs, Section, useAgentsLiveUpdates } from "./shared";

// --- Skills ------------------------------------------------------------------

const SKILL_COLUMNS: Column<SkillEntry>[] = [
  { id: "name", header: "Skill", cell: (s) => <span className="font-medium">{s.name}</span> },
  { id: "version", header: "Version", cell: (s) => `v${s.version}${s.latest ? " (latest)" : ""}` },
  { id: "source", header: "Source", cell: (s) => s.source },
  { id: "by", header: "Published", cell: (s) => `${s.published_by}, ${formatWhen(s.published_at)}` },
  { id: "install", header: "Installable", cell: (s) => (s.installable ? "Yes" : `No${s.install_block_reason ? `: ${s.install_block_reason}` : ""}`) },
];

export function SkillsPage() {
  const { values, update } = useUrlParams({ surface: "" });
  const surface = values.surface.trim();
  const data = useQuery({ queryKey: [...AGENTS_KEYS.skillsTeams, surface], queryFn: ({ signal }) => fetchSkillsTeams(signal, surface) });
  const permissions = data.data?.permissions;
  return (
    <>
      <PageHeader title="Skills" description="What agents know how to do: every published skill version, where it is installed, and installing or publishing one." tabs={<AgentsTabs />} />
      <div className="flex flex-col gap-4">
        <QueryRegion
          query={data}
          loadingLabel="Loading skills"
          errorTitle="Skills could not be loaded"
          isEmpty={(d) => d.skills.length === 0 && d.surfaces.length === 0}
          empty={{ title: "No skills published", description: "A skill appears here once it is published to the hub's registry." }}
        >
          {(d) => (
            <div className="flex flex-col gap-4">
              <DataTable columns={SKILL_COLUMNS} data={d.skills} getRowKey={(s) => `${s.name}@${s.version}`} caption="Published skills" captionSrOnly scrollLabel="Published skills" emptyText="No skills published." />
              <Section title="Compare installed skills" id="skills-surface" description="Compare a directory of installed skills with the registry, and choose where Install writes.">
                <SurfaceForm key={surface} surface={surface} setSurface={(value) => update({ surface: value }, { push: true })} canCheck={d.permissions.drift} />
              </Section>
              {d.surfaces.map((s) => (
                <DriftReport key={s.surface_dir} surface={s} />
              ))}
              <SkillInstallSection skills={d.skills} surface={surface} enabled={d.permissions.install} />
            </div>
          )}
        </QueryRegion>
        {data.data && <SkillPublishSection enabled={permissions?.publish === true} />}
      </div>
    </>
  );
}

// --- Org chart ---------------------------------------------------------------

function orgGraph(data: OrgPayload): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  const seen = new Set<string>();
  for (const office of data.offices) {
    nodes.push({ id: `office:${office.id}`, label: office.name, description: `${office.open_tasks} open tasks`, tone: "decision" });
    for (const m of office.members) {
      const id = `agent:${m.label}`;
      if (!seen.has(id)) {
        seen.add(id);
        nodes.push({ id, label: m.label, description: m.status, tone: m.status === "online" ? "live" : m.status === "idle" ? "ok" : "default" });
      }
      edges.push({ id: `${office.id}->${m.label}`, source: `office:${office.id}`, target: id, label: m.label === office.lead ? "led by" : "has member" });
    }
  }
  const layout = layeredLayout(nodes, edges);
  return { nodes: nodes.map((n) => ({ ...n, position: layout.get(n.id) })), edges };
}

export function OrgPage() {
  useAgentsLiveUpdates();
  const navigate = useNavigate();
  const data = useQuery({ queryKey: AGENTS_KEYS.org, queryFn: ({ signal }) => fetchOrg(signal) });

  return (
    <>
      <PageHeader title="Organization chart" description="Offices, who leads each, and who sits in them, from the org registry. Presence comes from hub heartbeats." tabs={<AgentsTabs />} />
      <div className="flex flex-col gap-4">
      <QueryRegion
        query={data}
        loadingLabel="Loading the org chart"
        errorTitle="The org chart could not be loaded"
        isEmpty={(d) => d.offices.length === 0}
        empty={{ title: "No offices configured", description: "Offices come from the hub's org registry." }}
      >
        {(d) => (
          <div className="flex flex-col gap-4">
            {d.error && <p className="m-0 text-sm text-[var(--ua-warn)]">Open task counts may be incomplete: {d.error}</p>}
            <OrgCanvas data={d} onSelect={(id) => id.startsWith("agent:") && navigate(`/agents/profile/${encodeURIComponent(id.slice(6))}`)} />
            {d.offices.map((office) => (
              <Section key={office.id} title={office.name} id={`office-${office.id}`} description={`Lead: ${office.lead} · ${office.open_tasks} open tasks · Epics: ${office.owned_epics.map((e) => `#${e}`).join(", ") || "none"}`}>
                <ul aria-label={`${office.name} members`} className="m-0 flex list-none flex-wrap gap-2 p-0">
                  {office.members.map((m) => (
                    <li key={m.label} className="rounded border border-[var(--ua-border)] px-2 py-1 text-sm">
                      <UiLink href={`/agents/profile/${encodeURIComponent(m.label)}`} className="font-medium text-[var(--ua-link)] underline">
                        {m.display_name && m.display_name !== m.label ? `${m.display_name} (${m.label})` : m.label}
                      </UiLink>{" "}
                      <span className="text-[var(--ua-muted)]">
                        {m.status}
                        {m.profile ? ` · ${m.profile}` : ""}
                      </span>
                    </li>
                  ))}
                </ul>
                <p className="m-0 text-xs text-[var(--ua-muted)]">
                  Queues: {office.default_lanes.join(", ") || "none"} · Decision rights: {office.decision_rights.join(", ") || "none"}
                </p>
              </Section>
            ))}
          </div>
        )}
      </QueryRegion>
      <PolicyRevisions />
      </div>
    </>
  );
}

function OrgCanvas({ data, onSelect }: { data: OrgPayload; onSelect: (id: string) => void }) {
  const graph = useMemo(() => orgGraph(data), [data]);
  return (
    <GraphCanvas
      label="Organization chart"
      nodes={graph.nodes}
      edges={graph.edges}
      onNodeSelect={onSelect}
      legend={[
        { tone: "decision", label: "Office" },
        { tone: "live", label: "Online agent" },
        { tone: "ok", label: "Idle agent" },
        { tone: "default", label: "Offline or unknown" },
      ]}
    />
  );
}

// --- Topology ----------------------------------------------------------------

const LINK_WORDS: Record<string, string> = { claim: "has claimed", subtask: "parent of", delegates: "delegates to", message: "messages" };

function topologyGraph(data: AgentGraphPayload): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes: GraphNode[] = data.nodes.map((n) => ({
    id: n.id,
    label: n.label ?? n.id,
    description: [n.type, n.status].filter(Boolean).join(" · ") || undefined,
    tone: n.type === "agent" ? "live" : n.status === "blocked" ? "warn" : "default",
  }));
  const ids = new Set(nodes.map((n) => n.id));
  const edges: GraphEdge[] = data.links
    .filter((l) => ids.has(l.source) && ids.has(l.target))
    .map((l, i) => ({ id: `${l.source}->${l.target}-${i}`, source: l.source, target: l.target, label: LINK_WORDS[l.kind ?? ""] ?? l.kind ?? "linked to" }));
  const layout = layeredLayout(nodes, edges);
  return { nodes: nodes.map((n) => ({ ...n, position: layout.get(n.id) })), edges };
}

export function TopologyPage() {
  useAgentsLiveUpdates();
  const navigate = useNavigate();
  const data = useQuery({ queryKey: AGENTS_KEYS.graph, queryFn: ({ signal }) => fetchAgentGraph(signal) });
  return (
    <>
      <PageHeader title="Topology" description="How agents connect: the tasks each has claimed, who delegates to whom and who messages whom." tabs={<AgentsTabs />} />
      <QueryRegion
        query={data}
        loadingLabel="Loading the topology"
        errorTitle="The topology could not be loaded"
        isEmpty={(d) => d.nodes.length === 0}
        empty={{ title: "Nothing connected yet", description: "Claims, delegations and messages between agents appear here." }}
      >
        {(d) => <TopologyCanvas data={d} onSelect={(id) => {
          const node = d.nodes.find((n) => n.id === id);
          if (node?.type === "agent") navigate(`/agents/profile/${encodeURIComponent(String(node.label ?? node.id))}`);
          else if (node?.type === "task" && /\d+/.test(id)) navigate(`/work/tasks/${id.match(/\d+/)?.[0]}`);
        }} />}
      </QueryRegion>
    </>
  );
}

function TopologyCanvas({ data, onSelect }: { data: AgentGraphPayload; onSelect: (id: string) => void }) {
  const graph = useMemo(() => topologyGraph(data), [data]);
  return (
    <GraphCanvas
      label="Agent topology"
      nodes={graph.nodes}
      edges={graph.edges}
      onNodeSelect={onSelect}
      legend={[
        { tone: "live", label: "Agent" },
        { tone: "default", label: "Task" },
        { tone: "warn", label: "Blocked task" },
      ]}
    />
  );
}

// --- Capabilities ------------------------------------------------------------

const CONNECTION_KIND: Record<string, "ok" | "warn" | "danger" | "unknown" | "live"> = {
  live: "ok",
  degraded: "warn",
  error: "danger",
  misconfigured: "danger",
  blocked: "danger",
  dormant: "unknown",
};

const CAPABILITY_COLUMNS: Column<CapabilityItem>[] = [
  { id: "capability", header: "Capability", cell: (c) => <span className="font-medium">{c.capability}</span> },
  { id: "purpose", header: "What it is for", cell: (c) => c.customer_purpose },
  { id: "state", header: "Built", cell: (c) => c.implementation_state },
  {
    id: "connection",
    header: "Connected",
    cell: (c) => <StatePill kind={CONNECTION_KIND[c.connection_state] ?? "unknown"} label={c.connection_state} />,
  },
  { id: "entry", header: "Entry point", cell: (c) => <span className="font-mono text-xs">{c.entry_point.ref}</span> },
];

export function CapabilitiesPage() {
  const data = useQuery({ queryKey: AGENTS_KEYS.capabilities, queryFn: ({ signal }) => fetchCapabilities(signal) });
  const { values } = useUrlParams({ family: "" });
  return (
    <>
      <PageHeader title="Capabilities" description="Every capability the platform has, how far it is built and whether it is connected, with its evidence." tabs={<AgentsTabs />} />
      <QueryRegion
        query={data}
        loadingLabel="Loading capabilities"
        errorTitle="Capabilities could not be loaded"
        isEmpty={(d) => !d.catalog || d.catalog.items.length === 0}
        empty={{ title: "No capability catalog", description: "The hub has no capability catalog to show." }}
      >
        {(d) => {
          const families = d.catalog?.families ?? [];
          const items = (d.catalog?.items ?? []).filter((i) => !values.family || i.family === values.family);
          return (
            <div className="flex flex-col gap-3">
              <nav aria-label="Capability families" className="flex flex-wrap gap-2 text-sm">
                <UiLink href="/agents/capabilities" aria-current={!values.family ? "page" : undefined} className="text-[var(--ua-link)] underline">
                  All
                </UiLink>
                {families.map((f) => (
                  <UiLink
                    key={f.id}
                    href={`/agents/capabilities?family=${encodeURIComponent(f.id)}`}
                    aria-current={values.family === f.id ? "page" : undefined}
                    className="text-[var(--ua-link)] underline"
                  >
                    {f.label}
                  </UiLink>
                ))}
              </nav>
              <DataTable columns={CAPABILITY_COLUMNS} data={items} getRowKey={(c) => c.id} caption="Capability catalog" captionSrOnly scrollLabel="Capability catalog" emptyText="No capability in this family." />
            </div>
          );
        }}
      </QueryRegion>
    </>
  );
}
