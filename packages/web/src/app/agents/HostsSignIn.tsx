/**
 * Hosts page (spec §9.3; legacy Devices): the actions that change a machine
 * or a sign-in.
 *
 * - Restart the executor on the hub's own machine (`POST /api/action/fleet`).
 * - Signed-in devices: the hub's OAuth sign-ins, grouped by device, and
 *   revoking one device's sign-in or one session (`POST /api/action/oauth-revoke`,
 *   approval-gated).
 * - Approver passkeys: register a passkey for the approver on this device
 *   (WebAuthn), list them, remove one of this device approver's own.
 *
 * Each destructive action asks first, and a parked one says it is waiting
 * for approval and changed nothing (spec §6). None is replayed from storage.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ApproverPasskey } from "../../lib/api/auth";
import type { DeviceHost, OAuthDeviceGroup, OAuthSessionsPayload } from "../../lib/api/devices";
import { Button, ConfirmDialog, DataTable, Field, Input, UiLink, type Column } from "../../lib/ui";
import { useEpoch } from "../lib/epoch";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { AGENTS_KEYS, fetchPasskeys, WRITE_PATHS } from "./api";
import { formatWhen } from "./model";
import { createPasskey, issuerHostsLink, passkeysSupported, type CreationOptionsJSON, type PasskeyRegistration } from "./passkeys";
import { Section, shownFrom, WriteFeedback, type ShownOutcome } from "./shared";

export const EXECUTOR_UNIT = "unified-agent-executor.service";

// --- Restart the executor ------------------------------------------------------

/**
 * "Restart executor" for one machine row. The hub restarts services only on
 * its own machine (`local_host`), so other rows say so instead of offering a
 * button that would be refused.
 */
export function RestartExecutor({ host, localHost }: { host: DeviceHost; localHost: string | null | undefined }) {
  const epoch = useEpoch();
  const [confirming, setConfirming] = useState(false);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const name = host.host ?? "";
  const write = useHubWrite<{ action: "restart_service"; host: string; unit: string }, unknown>({
    path: WRITE_PATHS.fleet,
    resumable: false,
    invalidates: [AGENTS_KEYS.devices],
    label: `Restarting the executor on ${name}`,
    pendingMessage: false,
    successMessage: `The executor on ${name} is restarting.`,
  });
  if (!name || name !== localHost) return <span className="text-xs text-[var(--ua-muted)]">Only on the hub's machine</span>;
  const run = async () => {
    setShown({ kind: "none" });
    const outcome = await write.run({ action: "restart_service", host: name, unit: EXECUTOR_UNIT });
    epoch.run(() => {
      setShown(shownFrom(outcome));
      if (outcome.kind !== "error") setConfirming(false);
    });
  };
  const feedback = <WriteFeedback state={write.state} shown={shown} what={`Restarting the executor on ${name}`} checkHref="/agents/hosts" checkLabel="reload the machine list" />;
  return (
    <span className="flex flex-col items-start gap-1">
      <Button size="sm" variant="ghost" disabled={write.isRunning} onClick={() => setConfirming(true)}>
        Restart executor on {name}
      </Button>
      {write.state.status === "done" && (
        <span role="status" className="text-xs">
          Restart sent.
        </span>
      )}
      {!confirming && feedback}
      {confirming && (
        <ConfirmDialog
          open
          title={`Restart the executor on ${name}?`}
          objectName={name}
          consequence="The headless executor stops and starts again. A job it is running is interrupted and picked up again from the queue."
          confirmLabel="Restart executor"
          variant="danger"
          busy={write.isRunning}
          onCancel={() => setConfirming(false)}
          onConfirm={() => void run()}
        >
          {feedback}
        </ConfirmDialog>
      )}
    </span>
  );
}

// --- Signed-in devices ---------------------------------------------------------

type RevokeTarget = { kind: "device"; id: string; name: string } | { kind: "session"; id: string; name: string };

function deviceName(group: OAuthDeviceGroup): string {
  return group.bound ? group.device_node || group.device_node_id || "Unnamed device" : "No device binding";
}

export function SignedInDevices({ oauth }: { oauth: OAuthSessionsPayload }) {
  const epoch = useEpoch();
  const [target, setTarget] = useState<RevokeTarget | null>(null);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const [what, setWhat] = useState("Revoking a sign-in");
  const write = useHubWrite<{ device: string } | { session_id: string }, unknown>({
    path: WRITE_PATHS.oauthRevoke,
    resumable: false,
    invalidates: [AGENTS_KEYS.devices],
    label: "Revoking a sign-in",
    pendingMessage: false,
  });
  const revoke = async (t: RevokeTarget) => {
    const label = t.kind === "device" ? `Revoking the sign-in for ${t.name}` : `Revoking session ${t.name}`;
    setWhat(label);
    setShown({ kind: "none" });
    const outcome = await write.run(t.kind === "device" ? { device: t.id } : { session_id: t.id });
    epoch.run(() => {
      setShown(shownFrom(outcome));
      if (outcome.kind !== "error") setTarget(null);
    });
  };
  const columns: Column<OAuthDeviceGroup>[] = [
    { id: "device", header: "Device", cell: (g) => <span className="font-medium">{deviceName(g)}</span> },
    { id: "agents", header: "Agents", cell: (g) => [...new Set(g.sessions.map((s) => s.agent).filter(Boolean))].join(", ") || "None recorded" },
    { id: "method", header: "Sign-in method", cell: (g) => [...new Set(g.sessions.map((s) => s.auth_method).filter(Boolean))].join(", ") || "Not recorded" },
    { id: "sessions", header: "Sessions", cell: (g) => String(g.sessions.length) },
    {
      id: "actions",
      header: "Actions",
      cell: (g) => {
        const id = g.device_node_id || g.device_node;
        if (g.bound && id) {
          const name = deviceName(g);
          return (
            <Button size="sm" variant="ghost" disabled={write.isRunning} onClick={() => setTarget({ kind: "device", id, name })}>
              Revoke sign-in for {name}
            </Button>
          );
        }
        // Sessions with no device binding are revoked one at a time.
        return (
          <span className="flex flex-wrap gap-1">
            {g.sessions.map((s) => {
              const name = `${s.session_id.slice(0, 8)} (${s.agent ?? "unknown agent"})`;
              return (
                <Button key={s.session_id} size="sm" variant="ghost" disabled={write.isRunning} onClick={() => setTarget({ kind: "session", id: s.session_id, name })}>
                  Revoke session {name}
                </Button>
              );
            })}
          </span>
        );
      },
    },
  ];
  const feedback = <WriteFeedback state={write.state} shown={shown} what={what} checkHref="/agents/hosts" checkLabel="reload the signed-in devices" />;
  return (
    <Section
      title="Signed-in devices"
      id="hosts-signed-in"
      description={`Sign-ins to the hub (OAuth), grouped by device${oauth.session_count ? `: ${oauth.session_count} sessions` : ""}. They survive a hub restart. Revoking one needs approval.`}
    >
      {oauth.error && <p className="m-0 text-sm text-[var(--ua-warn)]">The hub reported a problem reading sign-ins: {oauth.error}</p>}
      <DataTable
        columns={columns}
        data={oauth.devices}
        getRowKey={(g, i) => `${g.device_node_id ?? g.device_node ?? "unbound"}-${i}`}
        caption="Signed-in devices"
        captionSrOnly
        scrollLabel="Signed-in devices"
        emptyText="No device is signed in."
      />
      {write.state.status === "done" && (
        <p role="status" className="m-0 text-sm">
          {what}: done. The device must sign in again.
        </p>
      )}
      {!target && feedback}
      {target && (
        <ConfirmDialog
          open
          title={target.kind === "device" ? `Revoke the sign-in for ${target.name}?` : `Revoke session ${target.name}?`}
          objectName={target.name}
          consequence={
            target.kind === "device"
              ? "Every session signed in from this device ends. Its agents must sign in again before they can reach the hub."
              : "This session ends. Its agent must sign in again before it can reach the hub."
          }
          confirmLabel="Revoke"
          variant="danger"
          busy={write.isRunning}
          onCancel={() => setTarget(null)}
          onConfirm={() => void revoke(target)}
        >
          {feedback}
        </ConfirmDialog>
      )}
    </Section>
  );
}

// --- Approver passkeys ---------------------------------------------------------

function passkeyWhen(seconds: number | null | undefined): string {
  return seconds ? formatWhen(new Date(seconds * 1000).toISOString()) : "never";
}

/**
 * Approver passkeys (task #1322). Approving a sign-in or an OAuth consent
 * needs a verified human: a tailnet device owned by an approver login, plus
 * a passkey registered on it here once.
 */
export function ApproverPasskeys() {
  const epoch = useEpoch();
  const data = useQuery({ queryKey: AGENTS_KEYS.passkeys, queryFn: ({ signal }) => fetchPasskeys(signal) });
  const [label, setLabel] = useState("");
  const [creating, setCreating] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const [removing, setRemoving] = useState<ApproverPasskey | null>(null);
  const [removeShown, setRemoveShown] = useState<ShownOutcome>({ kind: "none" });
  const options = useHubWrite<Record<string, never>, CreationOptionsJSON>({
    path: WRITE_PATHS.passkeyOptions,
    resumable: false,
    label: "Preparing a passkey",
    pendingMessage: false,
  });
  const register = useHubWrite<PasskeyRegistration, unknown>({
    path: WRITE_PATHS.passkeyRegister,
    // The body is a fresh signed attestation; it cannot be replayed.
    resumable: false,
    invalidates: [AGENTS_KEYS.passkeys],
    label: "Registering the passkey",
    pendingMessage: false,
    successMessage: "Passkey registered.",
  });
  const remove = useHubWrite<{ credential_id: string }, unknown>({
    path: WRITE_PATHS.passkeyDelete,
    resumable: false,
    invalidates: [AGENTS_KEYS.passkeys],
    label: "Removing the passkey",
    pendingMessage: false,
    successMessage: "Passkey removed.",
  });

  const start = async () => {
    const submitted = label.trim();
    setLocalError(null);
    setShown({ kind: "none" });
    setCreating(true);
    try {
      const prepared = await options.run({});
      if (prepared.kind !== "done") {
        epoch.run(() => setShown(shownFrom(prepared)));
        return;
      }
      let body: PasskeyRegistration;
      try {
        body = await createPasskey(prepared.data, submitted);
      } catch (exc) {
        epoch.run(() => setLocalError(exc instanceof Error ? exc.message : "The passkey could not be created."));
        return;
      }
      const outcome = await register.run(body);
      epoch.run(() => {
        setShown(shownFrom(outcome));
        if (outcome.kind === "done") setLabel((now) => (now.trim() === submitted ? "" : now));
      });
    } finally {
      epoch.run(() => setCreating(false));
    }
  };

  const removeKey = async (key: ApproverPasskey) => {
    setRemoveShown({ kind: "none" });
    const outcome = await remove.run({ credential_id: key.credential_id });
    epoch.run(() => {
      setRemoveShown(shownFrom(outcome));
      if (outcome.kind !== "error") setRemoving(null);
    });
  };

  return (
    <Section
      title="Approver passkeys"
      id="approver-passkeys"
      description="Approving a sign-in or an OAuth consent needs a verified human: a tailnet device owned by an approver, plus a passkey registered on it here once."
    >
      <QueryRegion query={data} loadingLabel="Loading approver passkeys" errorTitle="Approver passkeys could not be loaded" headingLevel={3} empty={{ title: "No passkey data" }}>
        {(d) => {
          const logins = d.approver_logins;
          const thisLogin = d.this_device_login ?? null;
          const issuer = issuerHostsLink(d.origin);
          const columns: Column<ApproverPasskey>[] = [
            { id: "label", header: "Passkey", cell: (k) => <span className="font-medium">{k.label || "Passkey"}</span> },
            { id: "login", header: "Approver", cell: (k) => k.login },
            { id: "id", header: "Credential", cell: (k) => <code className="text-xs">{k.credential_id.slice(0, 10)}…</code> },
            { id: "used", header: "Last used", cell: (k) => passkeyWhen(k.last_used_at) },
            {
              id: "actions",
              header: "Actions",
              cell: (k) =>
                thisLogin && k.login === thisLogin ? (
                  <Button size="sm" variant="ghost" disabled={remove.isRunning} onClick={() => setRemoving(k)}>
                    Remove {k.label || "passkey"}
                  </Button>
                ) : (
                  <span className="text-xs text-[var(--ua-muted)]">Removable from its approver's device</span>
                ),
            },
          ];
          return (
            <>
              {d.passphrase_fallback && <p className="m-0 text-sm text-[var(--ua-warn)]">The shared passphrase fallback is still on.</p>}
              {logins.length === 0 ? (
                <p className="m-0 text-sm">No approver login is configured. Set UA_MCP_OAUTH_APPROVER_LOGINS on the hub.</p>
              ) : thisLogin ? (
                <form
                  aria-label="Register a passkey"
                  className="flex flex-wrap items-end gap-2"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void start();
                  }}
                >
                  <Field label="Passkey label" hint="So you can tell your passkeys apart, for example the device and its unlock method." error={localError ?? undefined}>
                    <Input value={label} maxLength={80} onChange={(e) => setLabel(e.target.value)} />
                  </Field>
                  <Button type="submit" variant="primary" size="sm" loading={creating} disabled={!passkeysSupported()}>
                    {creating ? "Waiting for the passkey" : `Register a passkey for ${thisLogin}`}
                  </Button>
                  {!passkeysSupported() && <p className="m-0 w-full text-sm text-[var(--ua-warn)]">This browser does not support passkeys.</p>}
                </form>
              ) : (
                <p className="m-0 text-sm">
                  {d.peer_is_loopback
                    ? "The hub sees this request from a loopback address, which happens through a local forward or proxy. A loopback request cannot prove it comes from an approver's own tailnet device, so passkeys cannot be registered from here."
                    : "This browser is not on a tailnet device owned by an approver, so it cannot register a passkey."}{" "}
                  {issuer ? (
                    <>
                      Open the hub directly at{" "}
                      <UiLink href={issuer} className="text-[var(--ua-link)] underline">
                        {issuer}
                      </UiLink>{" "}
                      on a tailnet device owned by {logins.join(", ")}.
                    </>
                  ) : (
                    <>Open the hub directly from a tailnet device owned by {logins.join(", ")}.</>
                  )}
                </p>
              )}
              <WriteFeedback state={register.state.status === "idle" ? options.state : register.state} shown={shown} what="Registering the passkey" checkHref="/agents/hosts#approver-passkeys" checkLabel="reload the passkey list" />
              <DataTable columns={columns} data={d.passkeys} getRowKey={(k) => k.credential_id} caption="Approver passkeys" captionSrOnly scrollLabel="Approver passkeys" emptyText="No passkeys registered." />
              {!removing && <WriteFeedback state={remove.state} shown={removeShown} what="Removing the passkey" checkHref="/agents/hosts#approver-passkeys" checkLabel="reload the passkey list" />}
              {removing && (
                <ConfirmDialog
                  open
                  title={`Remove the passkey ${removing.label || removing.credential_id.slice(0, 10)}?`}
                  objectName={removing.label || removing.credential_id.slice(0, 10)}
                  consequence={`${removing.login} can no longer approve with this passkey. Register a new one to approve from this device again.`}
                  confirmLabel="Remove passkey"
                  variant="danger"
                  busy={remove.isRunning}
                  onCancel={() => setRemoving(null)}
                  onConfirm={() => void removeKey(removing)}
                >
                  <WriteFeedback state={remove.state} shown={removeShown} what="Removing the passkey" />
                </ConfirmDialog>
              )}
            </>
          );
        }}
      </QueryRegion>
    </Section>
  );
}
