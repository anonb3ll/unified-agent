/**
 * Platform (spec §9.7): the kinds of object the platform owns, from the live
 * object-model manifest (legacy Platform pane: kind, address, lifecycle,
 * permission, whether the hub serves it), plus the harness configuration
 * the hub hands to machines (`GET /api/harness-config`, read only here;
 * publishing lives with Agents › Hosts).
 */
import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, DescriptionList, LinkButton, type Column } from "../../lib/ui";
import type { PlatformObjectTypeRow } from "../../lib/api/platform";
import { backoffRefetchInterval } from "../../lib/polling";
import { fetchHarnessConfig, fetchManifest, HARNESS_RECENT_LIMIT, SETTINGS_KEYS, type HarnessConfigStatus } from "./api";
import { humanize, plural, Section, SettingsHeader, SettingsRegion } from "./shared";

const KIND_COLUMNS: Column<PlatformObjectTypeRow>[] = [
  {
    id: "kind",
    header: "Kind",
    cell: (row) => (
      <div className="min-w-[10rem]">
        <div className="font-medium">{row.title || humanize(row.kind)}</div>
        <div className="font-mono text-xs text-[var(--ua-muted)]">{row.kind}</div>
        {row.description ? <div className="mt-1 text-xs text-[var(--ua-muted)]">{row.description}</div> : null}
      </div>
    ),
  },
  { id: "uri", header: "Address", cell: (row) => <span className="break-all font-mono text-xs">{row.uri || "–"}</span> },
  {
    id: "lifecycle",
    header: "Lifecycle",
    cell: (row) => <span className="text-xs">{row.lifecycle && row.lifecycle.length > 0 ? row.lifecycle.join(" → ") : "None"}</span>,
  },
  {
    id: "gate",
    header: "Permission needed",
    cell: (row) => (row.profile_tool ? <code className="font-mono text-xs">{row.profile_tool}</code> : <span className="text-xs">None</span>),
  },
  {
    id: "served",
    header: "Served",
    cell: (row) => <Badge variant={row.served ? "success" : "neutral"}>{row.served ? "Yes" : "Not yet"}</Badge>,
  },
];

export function PlatformPage() {
  const manifest = useQuery({
    queryKey: SETTINGS_KEYS.platform,
    queryFn: ({ signal }) => fetchManifest(signal),
    staleTime: 30_000,
    refetchInterval: backoffRefetchInterval(300_000),
  });
  const harness = useQuery({
    queryKey: SETTINGS_KEYS.harness,
    queryFn: ({ signal }) => fetchHarnessConfig(signal),
    refetchInterval: backoffRefetchInterval(60_000),
  });
  return (
    <>
      <SettingsHeader title="Platform" description="What the platform keeps track of, and the configuration it hands to machines. Read only." />
      <div className="flex flex-col gap-4">
        <Section
          id="platform-kinds"
          title="Object kinds"
          description="Each kind of object the hub owns: its address, the states it moves through, the permission an agent needs to read it, and whether this hub serves it yet."
        >
          <SettingsRegion
            query={manifest}
            what="the object kinds"
            loadingLabel="Loading object kinds"
            errorTitle="The object kinds could not be loaded"
            headingLevel={3}
            isEmpty={(d) => d.object_types.length === 0}
            empty={{ title: "No object kinds", description: "The hub's manifest lists no object kinds." }}
          >
            {(data) => (
              <>
                <p className="m-0 text-sm text-[var(--ua-muted)]">
                  {plural(data.object_types.length, "kind")}
                  {data.schema_version ? ` · manifest version ${data.schema_version}` : ""}
                  {data.product ? ` · ${data.product}` : ""}
                </p>
                <DataTable
                  columns={KIND_COLUMNS}
                  data={data.object_types}
                  getRowKey={(row) => row.kind}
                  caption="Object kinds the platform owns"
                  scrollLabel="Object kinds (scrolls sideways)"
                />
              </>
            )}
          </SettingsRegion>
        </Section>
        <Section
          id="platform-harness"
          title="Harness configuration"
          description="The configuration bundle the hub hands to machines, and changes machines have asked to apply."
          actions={
            <LinkButton href="/agents/hosts" variant="secondary" size="sm">
              Publish from Hosts
            </LinkButton>
          }
        >
          <SettingsRegion query={harness} what="the harness configuration" loadingLabel="Loading the harness configuration" errorTitle="The harness configuration could not be loaded" headingLevel={3}>
            {(data) => <HarnessStatus data={data} />}
          </SettingsRegion>
        </Section>
      </div>
    </>
  );
}

const PROPOSAL_COLUMNS: Column<HarnessConfigStatus["recent_proposals"][number]>[] = [
  { id: "node", header: "Machine", cell: (row) => row.node ?? "–" },
  { id: "surface", header: "Where", cell: (row) => <span className="break-all text-xs">{[row.surface, row.path].filter(Boolean).join(" · ") || "–"}</span> },
  { id: "version", header: "Version", cell: (row) => (row.bundle_version === null ? "–" : String(row.bundle_version)) },
  { id: "status", header: "Status", cell: (row) => humanize(row.status) },
  { id: "created", header: "Asked", cell: (row) => row.created_at ?? "–" },
];

function HarnessStatus({ data }: { data: HarnessConfigStatus }) {
  const capped = data.recent_proposals.length >= HARNESS_RECENT_LIMIT;
  return (
    <div className="flex flex-col gap-3">
      <DescriptionList
        items={[
          { term: "Active bundle", value: data.active ? `Version ${String(data.active.version)}` : "None published" },
          { term: "Published", value: data.active ? [data.active.published_at, data.active.published_by && `by ${data.active.published_by}`].filter(Boolean).join(" ") : null },
          { term: "Waiting to apply", value: plural(data.pending_proposals, "change") },
          { term: "How changes apply", value: `A person approves each change in the Inbox (tool ${data.apply_tool}).` },
        ]}
      />
      {data.recent_proposals.length > 0 && (
        <>
          <DataTable
            columns={PROPOSAL_COLUMNS}
            data={data.recent_proposals}
            getRowKey={(row) => String(row.id)}
            caption="Recent configuration changes asked for by machines"
            scrollLabel="Recent configuration changes (scrolls sideways)"
          />
          {capped && <p className="m-0 text-xs text-[var(--ua-muted)]">Showing the latest {HARNESS_RECENT_LIMIT}; the hub does not list older ones here.</p>}
        </>
      )}
    </div>
  );
}
