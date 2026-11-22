/**
 * Policy revisions (legacy Org view, task #1352): the immutable history of
 * the hub's authority configuration (policy, profiles, approval rules, org)
 * on the evidence ledger, whether the live configuration has drifted from
 * the latest revision, and rolling back to an earlier revision. A rollback
 * needs approval; a parked one changes nothing until it is approved and
 * submitted again.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, CodeBlock, ConfirmDialog, DataTable, Field, Select, type Column } from "../../lib/ui";
import { useEpoch } from "../lib/epoch";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import { AGENTS_KEYS, CONFIG_REVISION_KINDS, fetchConfigRevisions, WRITE_PATHS, type ConfigRevisionRow } from "./api";
import { formatWhen } from "./model";
import { Section, shownFrom, WriteFeedback, type ShownOutcome } from "./shared";

const KIND_LABEL: Record<string, string> = { policy: "Policy", profile: "Profiles", approval_rule: "Approval rules", org: "Org registry" };

export function PolicyRevisions() {
  const epoch = useEpoch();
  const { values, update } = useUrlParams({ rev_kind: "policy" });
  const kind = (CONFIG_REVISION_KINDS as readonly string[]).includes(values.rev_kind) ? values.rev_kind : "policy";
  const data = useQuery({ queryKey: AGENTS_KEYS.configRevisions(kind), queryFn: ({ signal }) => fetchConfigRevisions(kind, signal) });
  const [target, setTarget] = useState<ConfigRevisionRow | null>(null);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const [what, setWhat] = useState("Rolling back");
  const write = useHubWrite<{ kind: string; revision: number }, { unchanged?: boolean }>({
    path: WRITE_PATHS.configRevision,
    resumable: false,
    invalidates: [AGENTS_KEYS.configRevisions(kind), AGENTS_KEYS.operating, AGENTS_KEYS.org],
    label: "Rolling back the configuration",
    pendingMessage: false,
  });
  const rollback = async (row: ConfigRevisionRow) => {
    const label = `Rolling ${KIND_LABEL[kind].toLowerCase()} back to revision #${row.revision}`;
    setWhat(label);
    setShown({ kind: "none" });
    const outcome = await write.run({ kind, revision: row.revision });
    epoch.run(() => {
      setShown(shownFrom(outcome));
      if (outcome.kind !== "error") setTarget(null);
    });
  };
  const columns: Column<ConfigRevisionRow>[] = [
    { id: "revision", header: "Revision", cell: (r) => <span className="font-medium">#{r.revision}</span> },
    { id: "action", header: "Change", cell: (r) => `${r.action}${r.restores_revision ? ` (restores #${r.restores_revision})` : ""}` },
    { id: "actor", header: "By", cell: (r) => r.actor },
    { id: "when", header: "When", cell: (r) => formatWhen(r.ts) },
    {
      id: "actions",
      header: "Actions",
      cell: (r) => (
        <Button size="sm" variant="ghost" disabled={write.isRunning} onClick={() => setTarget(r)}>
          Roll back to #{r.revision}
        </Button>
      ),
    },
  ];
  const feedback = <WriteFeedback state={write.state} shown={shown} what={what} checkHref={`/agents/org?rev_kind=${kind}#policy-revisions`} checkLabel="reload the revision list" />;
  return (
    <Section
      title="Policy revisions"
      id="policy-revisions"
      description="Every change to the hub's authority configuration, recorded on the evidence ledger. Rolling back restores an earlier revision and needs approval."
      actions={
        <Field label="Configuration" className="flex-row items-center gap-2">
          <Select value={kind} onChange={(e) => update({ rev_kind: e.target.value })} options={CONFIG_REVISION_KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] }))} />
        </Field>
      }
    >
      <QueryRegion
        query={data}
        loadingLabel="Loading revisions"
        errorTitle="Revisions could not be loaded"
        headingLevel={3}
        isEmpty={(d) => d.revisions.length === 0 && !d.error}
        empty={{ title: "No revisions recorded", description: "A revision is recorded each time this configuration changes through the hub." }}
      >
        {(d) => (
          <>
            {d.error && <p className="m-0 text-sm text-[var(--ua-warn)]">The hub reported a problem reading the history: {d.error}</p>}
            {d.drift && (
              <p role="status" className="m-0 text-sm text-[var(--ua-warn)]">
                The live configuration differs from the latest revision: it was changed outside the revision path.
              </p>
            )}
            <DataTable columns={columns} data={d.revisions} getRowKey={(r) => String(r.revision)} caption={`${KIND_LABEL[kind]} revisions`} captionSrOnly scrollLabel={`${KIND_LABEL[kind]} revisions`} />
            {write.state.status === "done" && (
              <p role="status" className="m-0 text-sm">
                {write.state.data?.unchanged ? `${what}: nothing to change, the live configuration already matches.` : `${what}: done.`}
              </p>
            )}
            {!target && feedback}
            {d.latest_diff && <CodeBlock label="Latest change" code={d.latest_diff} language="diff" copyable={false} />}
          </>
        )}
      </QueryRegion>
      {target && (
        <ConfirmDialog
          open
          title={`Roll back to revision #${target.revision}?`}
          objectName={`revision #${target.revision}`}
          consequence={`The live ${KIND_LABEL[kind].toLowerCase()} configuration is replaced by revision #${target.revision}, recorded as a new revision. It needs approval first.`}
          confirmLabel="Roll back"
          variant="danger"
          busy={write.isRunning}
          onCancel={() => setTarget(null)}
          onConfirm={() => void rollback(target)}
        >
          {feedback}
        </ConfirmDialog>
      )}
    </Section>
  );
}
