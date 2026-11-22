/**
 * `/agents/hosts` (spec §9.3 Hosts; legacy Devices): the machines that run
 * agents (host reports with their agents, doctor, executor and worktrees,
 * and restarting the executor on the hub's machine), agents with no machine,
 * the registered device nodes with their acceptance status, node controls
 * (run, re-check or attest acceptance, retire, unretire, each with its
 * approval step), signed-in devices and revoking them, approver passkeys,
 * how to join a device, and publishing a harness configuration bundle.
 */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, CodeBlock, ConfirmDialog, DataTable, Field, Input, PageHeader, StatePill, Textarea, type Column } from "../../lib/ui";
import type { AgentCredential, DeviceHost } from "../../lib/api/devices";
import type { NodeAcceptanceSummary, NodeRow, RetiredNodeRow } from "../../lib/api/nodes";
import { useEpoch } from "../lib/epoch";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { AGENTS_KEYS, fetchDevices, fetchHarnessConfig, fetchNodes, WRITE_PATHS } from "./api";
import { formatWhen } from "./model";
import { ApproverPasskeys, RestartExecutor, SignedInDevices } from "./HostsSignIn";
import { AgentsTabs, Section, shownFrom, useAgentsLiveUpdates, WriteFeedback, type ShownOutcome } from "./shared";

/** Which credential the hub holds for an agent: token ids and key fingerprints, never a value. */
export function credentialText(c: AgentCredential | undefined): string {
  if (!c || c.source === "none") return "no credential on record";
  const ids = c.issued_token_ids.length ? `token #${c.issued_token_ids.join(", #")}` : "";
  const keys = c.fingerprints.length ? `key ${c.fingerprints.join(", ")}` : "";
  return [ids, keys].filter(Boolean).join(" · ") || c.source;
}

function doctorText(h: DeviceHost): string {
  if (h.doctor_freshness === "missing" || !h.doctor) return "Not reported";
  const d = h.doctor.summary ?? {};
  const parts = [`${d.live ?? 0} working`, `${d.dormant ?? 0} off`];
  if (d.degraded) parts.push(`${d.degraded} degraded`);
  if (d.misconfigured) parts.push(`${d.misconfigured} misconfigured`);
  return `${h.doctor_freshness ?? "reported"} · ${parts.join(" / ")}`;
}

function hostColumns(localHost: string | null | undefined): Column<DeviceHost>[] {
  return [
    {
      id: "host",
      header: "Host",
      cell: (h) => (
        <span className="font-medium">
          {h.host === "unattributed" ? "Unattributed (no host identity)" : (h.host ?? "Unnamed host")}
          {h.host_source && h.host_source !== "doctor" ? <span className="font-normal text-[var(--ua-muted)]"> · {String(h.host_source).replaceAll("_", " ")}</span> : null}
        </span>
      ),
    },
    {
      id: "agents",
      header: "Agents",
      cell: (h) =>
        (h.agents ?? []).length ? (
          <ul className="m-0 list-none p-0">
            {(h.agents ?? []).map((a) => (
              <li key={a.label ?? ""}>
                <span className="font-medium">{a.label}</span>{" "}
                <span className="text-xs text-[var(--ua-muted)]">
                  {a.status ?? "status unknown"}
                  {a.active_tasks ? ` · ${a.active_tasks} task(s)` : ""} · {credentialText(a.credential)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          "None reported"
        ),
    },
    {
      id: "surfaces",
      header: "Surfaces",
      cell: (h) => (h.surfaces ?? []).map((sf) => `${sf.kind}:${sf.name} → ${sf.agent ?? "unmapped"}`).join(", ") || "None",
    },
    { id: "freshness", header: "Freshness", cell: (h) => `${h.staleness ?? "unknown"}${h.as_of ? ` · ${formatWhen(h.as_of)}` : ""}` },
    { id: "doctor", header: "Doctor", cell: (h) => doctorText(h) },
    {
      id: "executor",
      header: "Executor",
      cell: (h) =>
        h.executor?.reported
          ? `${h.executor.unit_status === "UNKNOWN" ? "running (sign-in not verified)" : (h.executor.unit_status ?? "reported")}${h.executor.claim?.task_id ? ` · task #${h.executor.claim.task_id}` : h.executor.agent ? ` · ${h.executor.agent}` : ""}`
          : "Not reported",
    },
    { id: "worktrees", header: "Worktrees", cell: (h) => (h.worktrees?.reported ? `${h.worktrees.count ?? 0}${h.worktrees.stale ? ` · ${h.worktrees.stale} stale` : ""}` : "Not reported") },
    { id: "version", header: "Version", cell: (h) => h.version ?? "Not reported" },
    { id: "seen", header: "Last report", cell: (h) => formatWhen(h.last_seen ?? h.as_of ?? null) },
    { id: "actions", header: "Actions", cell: (h) => <RestartExecutor host={h} localHost={localHost} /> },
  ];
}

function acceptancePill(node: NodeRow) {
  const state = node.acceptance?.state;
  if (!state) return <StatePill kind="unknown" label="Not run" />;
  if (state === "accepted") return <StatePill kind="ok" label="Accepted" />;
  if (state === "failed") return <StatePill kind="danger" label="Failed" />;
  return <StatePill kind="live" label={state} />;
}

/** The checks behind a device's acceptance state, its reason and when it was last evaluated. */
function acceptanceDetail(a: NodeAcceptanceSummary | null | undefined): string | null {
  if (!a) return null;
  const checks = Object.values(a.checks ?? {});
  const passed = checks.filter((c) => c === "pass").length;
  const when = a.evaluated_at ?? a.started_at;
  return [checks.length ? `${passed}/${checks.length} checks pass` : null, a.reason ?? null, a.run_id ?? null, when ? formatWhen(when) : null].filter(Boolean).join(" · ") || null;
}

/** A pending run whose checks all passed can be attested by an independent operator (task #2299). */
export function canAttest(a: NodeAcceptanceSummary | null | undefined): a is NodeAcceptanceSummary & { run_id: string } {
  if (!a || a.state !== "pending" || !a.run_id) return false;
  const checks = Object.values(a.checks ?? {});
  return checks.length > 0 && checks.every((c) => c === "pass");
}

export function HostsPage() {
  useAgentsLiveUpdates();
  const devices = useQuery({ queryKey: AGENTS_KEYS.devices, queryFn: ({ signal }) => fetchDevices(signal) });
  const nodes = useQuery({ queryKey: AGENTS_KEYS.nodes, queryFn: ({ signal }) => fetchNodes(signal) });
  const [action, setActionNow] = useState<NodeActionState | null>(null);
  // Each click reopens the confirm, also for the same action after it was
  // parked (the typed reason or evidence is kept: the component stays mounted).
  const setAction = (next: Omit<NodeActionState, "opened"> | null) => setActionNow((prev) => (next ? { ...next, opened: (prev?.opened ?? 0) + 1 } : null));

  const nodeColumns: Column<NodeRow>[] = [
    { id: "node", header: "Device", cell: (n) => <span className="font-medium">{n.node}</span> },
    { id: "agent", header: "Agent", cell: (n) => n.agent ?? "None" },
    { id: "harnesses", header: "Harnesses", cell: (n) => n.backends.join(", ") || "None" },
    {
      id: "acceptance",
      header: "Acceptance",
      cell: (n) => (
        <span className="flex flex-col gap-0.5">
          {acceptancePill(n)}
          {acceptanceDetail(n.acceptance) && <span className="text-xs text-[var(--ua-muted)]">{acceptanceDetail(n.acceptance)}</span>}
        </span>
      ),
    },
    { id: "heartbeat", header: "Last heartbeat", cell: (n) => formatWhen(n.last_heartbeat) },
    {
      id: "actions",
      header: "Actions",
      cell: (n) => (
        <span className="flex flex-wrap gap-1">
          {n.acceptance && (
            <Button size="sm" onClick={() => setAction({ kind: "evaluate", node: n.node })}>
              Re-check {n.node}
            </Button>
          )}
          {/* A finished run (failed, timed out or accepted) stays terminal on the hub: re-checking
              it changes nothing, so recovery is a fresh run, as the classic Devices view offered. */}
          {(!n.acceptance || n.acceptance.state !== "pending") && (
            <Button size="sm" variant={n.acceptance ? "ghost" : undefined} onClick={() => setAction({ kind: "start", node: n.node })}>
              {n.acceptance ? `Run acceptance again on ${n.node}` : `Run acceptance on ${n.node}`}
            </Button>
          )}
          {canAttest(n.acceptance) && (
            <Button size="sm" variant="ghost" onClick={() => setAction({ kind: "attest", node: n.node, runId: (n.acceptance as { run_id: string }).run_id })}>
              Attest acceptance on {n.node}
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setAction({ kind: "retire", node: n.node })}>
            Retire {n.node}
          </Button>
        </span>
      ),
    },
  ];
  const retiredColumns: Column<RetiredNodeRow>[] = [
    { id: "node", header: "Device", cell: (n) => n.node },
    { id: "by", header: "Retired", cell: (n) => `${formatWhen(n.retired_at)}${n.retired_by ? ` by ${n.retired_by}` : ""}` },
    { id: "reason", header: "Reason", cell: (n) => n.retired_reason ?? "None given" },
    {
      id: "actions",
      header: "Actions",
      cell: (n) => (
        <Button size="sm" onClick={() => setAction({ kind: "unretire", node: n.node })}>
          Unretire {n.node}
        </Button>
      ),
    },
  ];

  return (
    <>
      <PageHeader title="Hosts" description="The machines and devices that run agents, whether each device passed acceptance, and the configuration they receive." tabs={<AgentsTabs />} />
      <div className="flex flex-col gap-4">
        <Section title="Machines" id="hosts-machines" description="Each machine's last report: its agents, executor and version.">
          <QueryRegion
            query={devices}
            loadingLabel="Loading machines"
            errorTitle="Machines could not be loaded"
            headingLevel={3}
            isEmpty={(d) => d.hosts.length === 0}
            empty={{ title: "No machine has reported", description: "A machine appears here once its node or doctor reports to the hub." }}
          >
            {(d) => (
              <>
                <DataTable columns={hostColumns(d.local_host)} data={d.hosts} getRowKey={(h, i) => h.host ?? `host-${i}`} caption="Machines" captionSrOnly scrollLabel="Machines" />
                {(d.unreported_peers ?? []).length > 0 && (
                  <p className="m-0 text-sm text-[var(--ua-muted)]">
                    On the network but not reporting: {(d.unreported_peers ?? []).map((p) => `${p.name}${p.os ? ` (${p.os})` : ""}${p.online === false ? ", offline" : ""}`).join("; ")}.
                  </p>
                )}
                {d.tailnet_census === "unavailable" && (
                  <p className="m-0 text-sm text-[var(--ua-muted)]">The hub could not list the tailnet, so machines without a node are not shown.</p>
                )}
                {(d.agents ?? []).some((a) => !a.host) && (
                  <div className="flex flex-col gap-1">
                    <h3 className="m-0 text-sm font-semibold">
                      Agents with no machine ({(d.agents ?? []).filter((a) => !a.host).length} of {(d.agents ?? []).length})
                    </h3>
                    <ul aria-label="Agents with no machine" className="m-0 list-none p-0 text-sm">
                      {(d.agents ?? [])
                        .filter((a) => !a.host)
                        .map((a) => (
                          <li key={a.label}>
                            <span className="font-medium">{a.label}</span>{" "}
                            <span className="text-[var(--ua-muted)]">
                              {a.status ?? "status unknown"}
                              {a.unplaced_host ? ` · says it runs on "${a.unplaced_host}", which is not a known machine` : ""} · {credentialText(a.credential)}
                            </span>
                          </li>
                        ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </QueryRegion>
        </Section>

        <Section title="Devices" id="hosts-devices" description="Registered device nodes. A device runs work only after it passes acceptance, when the hub requires it.">
          <QueryRegion
            query={nodes}
            loadingLabel="Loading devices"
            errorTitle="Devices could not be loaded"
            headingLevel={3}
            isEmpty={(d) => d.nodes.length === 0 && (d.retired_nodes ?? []).length === 0}
            empty={{ title: "No devices registered", description: "Join a device with the steps below." }}
          >
            {(d) => (
              <>
                {d.acceptance_required && <p className="m-0 text-sm">This hub sends work only to accepted devices.</p>}
                <DataTable columns={nodeColumns} data={d.nodes} getRowKey={(n) => n.node} caption="Registered devices" captionSrOnly scrollLabel="Registered devices" emptyText="No active device." />
                {(d.retired_nodes ?? []).length > 0 && (
                  <>
                    <h3 className="m-0 text-sm font-semibold">Retired devices</h3>
                    <DataTable columns={retiredColumns} data={d.retired_nodes ?? []} getRowKey={(n) => n.node} caption="Retired devices" captionSrOnly scrollLabel="Retired devices" />
                  </>
                )}
              </>
            )}
          </QueryRegion>
          {action && <NodeAction key={`${action.kind}:${action.node}:${action.runId ?? ""}`} action={action} onClose={() => setAction(null)} />}
        </Section>

        {devices.data?.oauth && <SignedInDevices oauth={devices.data.oauth} />}
        {devices.data?.oauth?.oauth_enabled && <ApproverPasskeys />}

        <Section title="Join a device" id="hosts-join">
          <p className="m-0 text-sm">
            On the device, sign in to the hub once, then run it as a node. The node connects out to the hub, registers itself and appears above. Run
            acceptance on it before it takes work.
          </p>
          <CodeBlock label="On the device" code={[`unified-agent login --hub ${typeof window === "undefined" ? "<hub address>" : window.location.origin}`, `unified-agent node --hub ${typeof window === "undefined" ? "<hub address>" : window.location.origin}`].join("\n")} />
        </Section>

        <HarnessConfigSection />
      </div>
    </>
  );
}

type NodeActionKind = "retire" | "unretire" | "start" | "evaluate" | "attest";
type NodeActionState = { kind: NodeActionKind; node: string; runId?: string; opened: number };

const NODE_TEXT: Record<NodeActionKind, { title: (n: string) => string; consequence: string; confirm: string; reason: boolean; danger: boolean }> = {
  attest: {
    title: (n) => `Attest ${n}'s acceptance?`,
    consequence:
      "Confirm you checked the device's output and cancellation yourself, outside what the device reports about itself. Only a lifecycle operator other than the device's own agent may attest.",
    confirm: "Attest acceptance",
    reason: true,
    danger: false,
  },
  start: { title: (n) => `Run acceptance on ${n}?`, consequence: "The device runs a fresh set of acceptance checks; an earlier finished run is replaced. It takes work only after it passes.", confirm: "Run acceptance", reason: false, danger: false },
  evaluate: { title: (n) => `Re-check ${n}?`, consequence: "The hub evaluates the device's latest acceptance run again.", confirm: "Re-check", reason: false, danger: false },
  retire: { title: (n) => `Retire ${n}?`, consequence: "The device takes no more work and leaves the device list. Refused while it has queued or claimed work.", confirm: "Retire", reason: true, danger: true },
  unretire: { title: (n) => `Unretire ${n}?`, consequence: "The device returns to the list and can take work again.", confirm: "Unretire", reason: true, danger: false },
};

/** One node control, from confirm to result. Keyed by action and node. */
function NodeAction({ action, onClose }: { action: NodeActionState; onClose: () => void }) {
  const epoch = useEpoch();
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(true);
  useEffect(() => setConfirming(true), [action.opened]);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const text = NODE_TEXT[action.kind];
  const acceptance = action.kind === "start" || action.kind === "evaluate" || action.kind === "attest";
  const write = useHubWrite<Record<string, string>, unknown>({
    path: acceptance ? WRITE_PATHS.nodeAcceptance : WRITE_PATHS.nodeRetire,
    resumable: false,
    invalidates: [AGENTS_KEYS.nodes, AGENTS_KEYS.devices],
    label: `${text.confirm} ${action.node}`,
    pendingMessage: false,
    successMessage: `${text.confirm}: done for ${action.node}.`,
  });
  const run = async () => {
    setShown({ kind: "none" });
    const body: Record<string, string> =
      action.kind === "attest"
        ? { action: "attest", node: action.node, run_id: action.runId ?? "", evidence_ref: reason.trim() }
        : acceptance
          ? { action: action.kind, node: action.node }
          : { action: action.kind, node: action.node, reason: reason.trim() };
    const outcome = await write.run(body);
    epoch.run(() => {
      setShown(
        shownFrom(outcome, (code) =>
          code === "conflict"
            ? action.kind === "attest"
              ? `${action.node}'s acceptance run is no longer pending; reload the device list.`
              : `${action.node} has queued or claimed work; finish or cancel it first.`
            : null,
        ),
      );
      if (outcome.kind === "done" || outcome.kind === "accepted") onClose();
      // A parked or cancelled action leaves the confirm; a refusal keeps it
      // open, with the reason as typed, so the person can fix and retry.
      else if (outcome.kind !== "error") setConfirming(false);
    });
  };
  const feedback = <WriteFeedback state={write.state} shown={shown} what={`${text.confirm} ${action.node}`} checkHref="/agents/hosts" checkLabel="reload the device list" />;
  return (
    <>
      {!confirming && feedback}
      {confirming && (
        <ConfirmDialog
          open
          title={text.title(action.node)}
          objectName={action.node}
          consequence={text.consequence}
          confirmLabel={text.confirm}
          variant={text.danger ? "danger" : "default"}
          busy={write.isRunning}
          confirmDisabled={text.reason && !reason.trim()}
          onCancel={onClose}
          onConfirm={() => void run()}
        >
          {text.reason && (
            <Field
              label={action.kind === "attest" ? "Independent evidence" : "Reason"}
              hint={action.kind === "attest" ? `What you checked for run ${action.runId ?? ""}: a link, a hash or a short note.` : undefined}
              required
            >
              <Input value={reason} maxLength={action.kind === "attest" ? 1000 : 500} onChange={(e) => setReason(e.target.value)} />
            </Field>
          )}
          {feedback}
        </ConfirmDialog>
      )}
    </>
  );
}

function HarnessConfigSection() {
  const epoch = useEpoch();
  const status = useQuery({ queryKey: AGENTS_KEYS.harness, queryFn: ({ signal }) => fetchHarnessConfig(signal) });
  const [bundle, setBundle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const write = useHubWrite<{ action: "publish"; bundle: Record<string, unknown> }, unknown>({
    path: WRITE_PATHS.harnessConfig,
    // A bundle can carry configuration the person would not want kept in the browser.
    resumable: false,
    invalidates: [AGENTS_KEYS.harness],
    label: "Publishing the harness configuration",
    pendingMessage: false,
    successMessage: "Harness configuration published. Machines apply it after its approval.",
  });
  const publish = async () => {
    // The text this publish sends; the editor is cleared against it, never against the latest text.
    const submitted = bundle;
    let parsed: unknown;
    try {
      parsed = JSON.parse(submitted);
    } catch {
      setError("The bundle is not valid JSON.");
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      setError("The bundle must be a JSON object.");
      return;
    }
    setError(null);
    setShown({ kind: "none" });
    const outcome = await write.run({ action: "publish", bundle: parsed as Record<string, unknown> });
    epoch.run(() => {
      setShown(shownFrom(outcome));
      // Text edited while the publish was in flight is newer work: keep it.
      if (outcome.kind === "done") setBundle((now) => (now === submitted ? "" : now));
    });
  };
  const published = status.data?.published ?? null;
  return (
    <Section
      title="Harness configuration"
      id="hosts-harness"
      description="The configuration bundle machines receive for their harnesses. Publishing records a new version; each machine applies it only after its approval."
    >
      <p className="m-0 text-sm" aria-live="polite">
        {status.isPending
          ? "Loading the published version"
          : status.isError
            ? "The published version could not be read."
            : published
              ? `Published version ${String(published.version ?? "unknown")}${published.published_by ? ` by ${published.published_by}` : ""}${published.published_at ? `, ${formatWhen(published.published_at)}` : ""}.`
              : "Nothing published yet."}
      </p>
      <Field label="Bundle (JSON)" hint="The full bundle to publish." error={error ?? undefined}>
        <Textarea value={bundle} rows={6} spellCheck={false} onChange={(e) => setBundle(e.target.value)} className="font-mono" />
      </Field>
      <div>
        <Button variant="primary" loading={write.isRunning} disabled={!bundle.trim()} onClick={() => void publish()}>
          Publish
        </Button>
      </div>
      <WriteFeedback state={write.state} shown={shown} what="Publishing the harness configuration" checkHref="/agents/hosts" checkLabel="reload this page" />
    </Section>
  );
}
