/**
 * Cloud (spec §9.7): the Google Cloud project behind the hub, read only.
 * Retains the legacy Cloud pane: instance state (never softening "could not
 * read" into "no instance"), backups, budgets, the reading's age, a partial
 * reading warning and the sections that failed.
 */
import { useQuery } from "@tanstack/react-query";
import { DescriptionList, StatePill, type StateKind } from "../../lib/ui";
import type { CloudInstanceState, CloudPayload } from "../../lib/api/cloud";
import { backoffRefetchInterval } from "../../lib/polling";
import { fetchCloudReport, SETTINGS_KEYS } from "./api";
import { agoText, Notice, Section, SettingsHeader, SettingsRegion } from "./shared";

const INSTANCE: Record<CloudInstanceState, { word: string; kind: StateKind; meaning: string }> = {
  running: { word: "Running", kind: "ok", meaning: "The cloud worker is up." },
  stopped: { word: "Stopped", kind: "warn", meaning: "The cloud worker exists but is not running." },
  transitional: { word: "Starting or stopping", kind: "live", meaning: "The cloud worker is changing state." },
  absent: { word: "No instance", kind: "warn", meaning: "The project has no cloud worker." },
  unknown: { word: "Could not read", kind: "unknown", meaning: "The hub could not read the instance list, so it is not known whether a worker exists." },
};

export function CloudPage() {
  // The hub caches gcloud for 120 s, so a faster poll only re-serves the same reading.
  const query = useQuery({ queryKey: SETTINGS_KEYS.cloud, queryFn: ({ signal }) => fetchCloudReport(signal), refetchInterval: backoffRefetchInterval(60_000) });
  return (
    <>
      <SettingsHeader
        title="Cloud"
        description="The hosted cloud worker and its project: whether it runs, its backups and its budgets. Read only: this page never starts, stops or repairs anything."
      />
      <SettingsRegion query={query} what="the cloud project" loadingLabel="Loading the cloud project" errorTitle="The cloud project could not be loaded">
        {(data) => <CloudReport data={data} />}
      </SettingsRegion>
    </>
  );
}

function CloudReport({ data }: { data: CloudPayload }) {
  const instance = INSTANCE[data.instance_state] ?? INSTANCE.unknown;
  return (
    <div className="flex flex-col gap-4">
      {data.partial_coverage && (
        <Notice tone="warn" role="status">
          <p className="m-0 font-medium">This is a partial reading.</p>
          <p className="m-0 mt-1">
            At least one part of the project could not be read, so this is not the full picture. The usual cause is a missing permission on the hub's service
            account (<code>compute.viewer</code> covers the instance list).
          </p>
        </Notice>
      )}
      <Section id="cloud-summary" title={data.project_id ? `Project ${data.project_id}` : "Project"}>
        <DescriptionList
          columns={2}
          items={[
            {
              term: "Cloud worker",
              value: (
                <span className="flex flex-col gap-1">
                  <StatePill kind={instance.kind} label={instance.word} />
                  <span className="text-xs text-[var(--ua-muted)]">{instance.meaning}</span>
                </span>
              ),
            },
            {
              term: "Backups",
              value: data.backups.state === "unknown" ? "Could not read" : data.backups.detail || (data.backups.state === "none" ? "No backup bucket" : "Present"),
            },
            { term: "Budgets", value: data.budgets.state === "unknown" ? "Could not read" : `${data.budgets.items.length} set up` },
            { term: "Read", value: data.checked_at ? agoText(Math.round(data.cached_age_s) * 1000) : "Never read" },
            { term: "Overall", value: data.status === "ok" ? "All parts read" : data.status === "degraded" ? "Some parts could not be read" : data.status === "error" ? "The project could not be read" : "Not known" },
            { term: "Repair", value: data.remediation === "manual" ? "By hand, through an approved cloud operation" : data.remediation },
          ]}
        />
      </Section>
      {data.instances.length > 0 && (
        <Section id="cloud-instances" title="Instances">
          <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
            {data.instances.map((i, index) => (
              <li key={i.name ?? index}>
                <span className="font-medium">{i.name ?? "Unnamed"}</span>{" "}
                <span className="text-[var(--ua-muted)]">{[i.status, i.zone, i.machineType].filter(Boolean).join(" · ")}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {data.backups.buckets.length > 0 && (
        <Section id="cloud-backups" title="Backup buckets">
          <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
            {data.backups.buckets.map((b, index) => (
              <li key={b.name ?? index}>
                <span className="font-medium">{b.name ?? "Unnamed"}</span>{" "}
                <span className="text-[var(--ua-muted)]">
                  {[b.location, b.versioning_enabled === undefined ? null : b.versioning_enabled ? "keeps old versions" : "does not keep old versions"].filter(Boolean).join(" · ")}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {data.errors.length > 0 && (
        <Section id="cloud-errors" title="Parts that could not be read" description="Each part the hub tried to read and could not, with the reason it gave.">
          <ul className="m-0 flex list-none flex-col gap-2 p-0 text-sm">
            {data.errors.map((e, index) => (
              <li key={`${e.section ?? "part"}-${index}`}>
                <span className="font-medium">{e.section ?? "Unknown part"}</span>
                {e.error && (
                  <details className="mt-0.5 text-xs">
                    <summary className="cursor-pointer">Technical detail</summary>
                    <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{e.error.slice(0, 400)}</pre>
                  </details>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}
