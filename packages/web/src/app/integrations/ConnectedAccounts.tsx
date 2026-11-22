/**
 * Connected accounts (OAuth): status in plain words, connect and reconnect
 * (a full-page trip to the provider), and disconnect with a confirm that
 * names what stops working. Grants stay encrypted on the hub; this page only
 * ever sees status, scopes and a time. The sign-in address the hub returns
 * carries a one-use `state`: it is checked, used for the navigation and
 * dropped, never shown, stored or logged.
 *
 * Each write runs in the owner epoch it started in: leaving for the provider,
 * closing a dialog or showing a result after the answer happens only while
 * that epoch is live, so nothing reaches whoever is signed in next.
 */
import { useEffect, useRef, useState } from "react";
import { Button, ConfirmDialog, ErrorState, Skeleton } from "../../lib/ui";
import { ParkedWriteNotice } from "../lib/ParkedWriteNotice";
import { useEpoch } from "../lib/epoch";
import { useIdentity } from "../lib/identity";
import { useHubWrite, type WriteOutcome } from "../lib/mutations";
import {
  checkAuthorizeUrl,
  connectionText,
  describeIntegrationReadError,
  INTEGRATION_KEYS,
  IntegrationReadError,
  OAUTH_PROVIDERS,
  oauthPaths,
  when,
  type OAuthConnection,
} from "./api";
import { leaveForProvider } from "./navigate";
import { useOAuthConnection } from "./queries";
import { explainOrStale, Section, StatusLabel } from "./shared";

type Provider = (typeof OAUTH_PROVIDERS)[number];

/**
 * Whether this dialog instance is still on screen. An answer that arrives
 * after its dialog was closed (and perhaps opened again: a new instance)
 * never navigates, closes or shows anything.
 */
function useAlive(): { readonly current: boolean } {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return alive;
}

export function ConnectedAccounts() {
  return (
    <Section
      id="connected-accounts-heading"
      title="Connected accounts"
      description="Accounts the hub uses on your behalf. You connect once in your browser; the grant is kept encrypted on the hub and is never shown here."
    >
      <ul className="m-0 flex list-none flex-col gap-3 p-0">
        {OAUTH_PROVIDERS.map((provider) => (
          <li key={provider.id}>
            <ProviderCard provider={provider} />
          </li>
        ))}
      </ul>
      <p className="m-0 text-sm text-[var(--ua-muted)]">
        Google can be connected once its access has been reviewed for this hub.
      </p>
    </Section>
  );
}

/** Messages for a write that did not finish, in plain words (never the raw transport text). */
function writeProblem(outcome: Extract<WriteOutcome<unknown>, { kind: "error" }>, unknownText: string) {
  if (outcome.outcomeUnknown) return { message: unknownText, detail: outcome.detail ?? null };
  return { message: outcome.message, detail: outcome.detail ?? null };
}

function ProviderCard({ provider }: { provider: Provider }) {
  const query = useOAuthConnection(provider.id);
  // Open dialogs and their results belong to the person and the account they were opened for.
  const identityKey = `${useIdentity().identity ?? ""}|${provider.id}`;
  const headingId = `oauth-${provider.id}-heading`;
  let body;
  if (query.data === undefined) {
    if (query.isError) {
      const denied = query.error instanceof IntegrationReadError && query.error.status === 403;
      const { message, detail } = describeIntegrationReadError(query.error, `the ${provider.name} connection`);
      body = (
        <ErrorState
          title={denied ? "Only the human operator can manage connected accounts" : `Could not load the ${provider.name} connection`}
          message={
            denied
              ? "Connected accounts belong to the hub's human operator. Sign in as the operator to see and change them."
              : message
          }
          detail={detail}
          onRetry={() => void query.refetch()}
          headingLevel={3}
        />
      );
    } else {
      body = <Skeleton label={`Loading the ${provider.name} connection`} lines={2} />;
    }
  } else {
    body = <ProviderReady key={identityKey} provider={provider} connection={query.data} refreshFailed={query.isError} onRetry={() => void query.refetch()} error={query.error} updatedAt={query.dataUpdatedAt} />;
  }
  return (
    <article aria-labelledby={headingId} className="flex flex-col gap-3 rounded-lg border border-[var(--ua-border)] bg-[var(--ua-surface)] p-4">
      <h3 id={headingId} className="m-0 text-base font-semibold">
        {provider.name}
      </h3>
      {body}
    </article>
  );
}

function ProviderReady({
  provider,
  connection,
  refreshFailed,
  onRetry,
  error,
  updatedAt,
}: {
  provider: Provider;
  connection: OAuthConnection;
  refreshFailed: boolean;
  onRetry: () => void;
  error: unknown;
  updatedAt: number;
}) {
  const status = connectionText(connection.status);
  // Captured when the dialog opens, so a refetch never changes what an open dialog does.
  const [connectOpen, setConnectOpen] = useState<{ reconnect: boolean } | null>(null);
  const [disconnectOpen, setDisconnectOpen] = useState(false);
  const connected = connection.status === "connected";
  return (
    <>
      {refreshFailed && (
        <ErrorState
          headingLevel={4}
          title={`Could not refresh the ${provider.name} connection`}
          message={`${describeIntegrationReadError(error, `the ${provider.name} connection`).message} The status below was read at ${new Date(updatedAt).toLocaleTimeString()} and may be out of date.`}
          detail={describeIntegrationReadError(error, "").detail}
          onRetry={onRetry}
        />
      )}
      <div className="flex flex-wrap items-center gap-3">
        <StatusLabel tone={status.tone} label={status.label} stale={refreshFailed} testId={`oauth-${provider.id}-status`} />
        <span className="text-sm text-[var(--ua-muted)]">{explainOrStale(status.explain, refreshFailed)}</span>
      </div>
      {(connection.scopes.length > 0 || connection.updatedAt) && (
        <p className="m-0 text-sm text-[var(--ua-muted)]">
          {connection.scopes.length > 0 && <>Access granted: {connection.scopes.join(", ")}. </>}
          {connection.updatedAt && <>Last changed {when(connection.updatedAt)}.</>}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant={connected ? "secondary" : "primary"} onClick={() => setConnectOpen({ reconnect: connection.status !== "disconnected" })}>
          {connection.status === "disconnected" ? `Connect ${provider.name}` : `Reconnect ${provider.name}`}
        </Button>
        {connection.status !== "disconnected" && (
          <Button variant="danger" onClick={() => setDisconnectOpen(true)}>
            Disconnect {provider.name}
          </Button>
        )}
      </div>
      {connectOpen && <ConnectDialog provider={provider} reconnect={connectOpen.reconnect} onClose={() => setConnectOpen(null)} />}
      {disconnectOpen && <DisconnectDialog provider={provider} onClose={() => setDisconnectOpen(false)} />}
    </>
  );
}

function ConnectDialog({ provider, reconnect, onClose }: { provider: Provider; reconnect: boolean; onClose: () => void }) {
  const epoch = useEpoch();
  const alive = useAlive();
  const write = useHubWrite<Record<string, never>, { authorize_url?: unknown }>({
    path: oauthPaths.begin(provider.id),
    label: `Connect ${provider.name}`,
    // The answer carries a one-use sign-in address; repeating it from storage would be pointless.
    resumable: false,
  });
  const [problem, setProblemNow] = useState<{ message: string; detail: string | null } | null>(null);
  const [leaving, setLeaving] = useState<string | null>(null);
  const [parked, setParkedNow] = useState<string | null | undefined>(undefined);

  async function go() {
    // Everything after the answer belongs to the epoch the click was made in.
    const started = epoch;
    const setProblem = (next: { message: string; detail: string | null } | null) => started.run(() => setProblemNow(next));
    const setParked = (next: string | null) => started.run(() => setParkedNow(next));
    setProblem(null);
    const outcome = await write.run({});
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      const checked = checkAuthorizeUrl(outcome.data?.authorize_url, provider.host, window.location.origin);
      // Drop the answer (and its one-use state) from the write's memory at once.
      write.reset();
      if (!checked.ok) {
        setProblem({ message: `${checked.reason} Nothing was opened. Ask the operator to check the hub's ${provider.name} set-up.`, detail: null });
        return;
      }
      // A sign-in started for one owner never navigates the page of the next,
      // and one cancelled (its dialog closed) never leaves the page at all.
      if (!alive.current) return;
      started.run(() => {
        setLeaving(checked.host);
        leaveForProvider(checked.url);
      });
      return;
    }
    if (outcome.kind === "pending_approval") {
      setParked(outcome.approvalId);
      return;
    }
    if (outcome.kind === "cancelled") {
      // Not sent: show nothing. Sent, then cancelled: treat as unknown (starting again is safe).
      if (outcome.dispatched) {
        setProblem({
          message: `The hub may have started a ${provider.name} sign-in, but this page stopped waiting for it, so nothing was opened. Starting again is safe: an unused sign-in expires after 10 minutes.`,
          detail: null,
        });
      }
      return;
    }
    setProblem(
      writeProblem(
        outcome,
        `The hub may have started a ${provider.name} sign-in, but its answer was lost, so nothing was opened. Starting again is safe: an unused sign-in expires after 10 minutes.`,
      ),
    );
  }

  return (
    <ConfirmDialog
      open
      title={reconnect ? `Reconnect ${provider.name}?` : `Connect ${provider.name}?`}
      objectName={`You will go to ${provider.host}`}
      consequence={
        <>
          This tab leaves the Control Center and opens {provider.name}'s page at {provider.host}, where you sign in and
          approve access for the hub. Afterwards {provider.name} sends you to a short confirmation page on this hub. Then
          come back here (your browser's Back button, or Settings, Integrations) and this page shows the new status.
          {reconnect && " The current grant keeps working until the new one replaces it."}
        </>
      }
      confirmLabel={leaving ? `Opening ${leaving}…` : `Continue to ${provider.host}`}
      busy={write.isRunning || leaving !== null}
      onConfirm={() => void go()}
      onCancel={onClose}
    >
      {(problem || parked !== undefined) && (
        <div className="flex flex-col gap-2">
          {problem && (
            <ErrorState headingLevel={3} title={`${provider.name} was not opened`} message={problem.message} detail={problem.detail} />
          )}
          {parked !== undefined && <ParkedWriteNotice approvalId={parked} label={`Connect ${provider.name}`} />}
        </div>
      )}
    </ConfirmDialog>
  );
}

function DisconnectDialog({ provider, onClose }: { provider: Provider; onClose: () => void }) {
  const epoch = useEpoch();
  const alive = useAlive();
  const write = useHubWrite<Record<string, never>, unknown>({
    path: oauthPaths.disconnect(provider.id),
    label: `Disconnect ${provider.name}`,
    invalidates: [INTEGRATION_KEYS.oauth(provider.id)],
    successMessage: `${provider.name} is disconnected.`,
    resumable: false,
  });
  const [problem, setProblemNow] = useState<{ message: string; detail: string | null; unknown: boolean } | null>(null);
  const [parked, setParkedNow] = useState<string | null | undefined>(undefined);

  async function confirm() {
    const started = epoch;
    const closeInEpoch = started.wrap(onClose);
    // Only this instance closes itself: never a dialog opened again since.
    const close = () => {
      if (alive.current) closeInEpoch();
    };
    const setProblem = (next: { message: string; detail: string | null; unknown: boolean } | null) => started.run(() => setProblemNow(next));
    const setParked = (next: string | null) => started.run(() => setParkedNow(next));
    setProblem(null);
    const outcome = await write.run({});
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      close();
      return;
    }
    if (outcome.kind === "pending_approval") {
      setParked(outcome.approvalId);
      return;
    }
    if (outcome.kind === "cancelled") {
      // Not sent: show nothing. Sent before it was cancelled: never "not sent", the outcome is unknown.
      if (outcome.dispatched) {
        setProblem({
          message: `The request was sent, but this page stopped waiting for its answer, so it is not known whether ${provider.name} was disconnected. Close this and check the status above before trying again.`,
          detail: null,
          unknown: true,
        });
      }
      return;
    }
    const text = writeProblem(
      outcome,
      `The request was sent, but its answer was lost, so it is not known whether ${provider.name} was disconnected. Close this and check the status above before trying again.`,
    );
    setProblem({ ...text, unknown: Boolean(outcome.outcomeUnknown) });
  }

  return (
    <ConfirmDialog
      open
      variant="danger"
      title={`Disconnect ${provider.name}?`}
      objectName={`${provider.name} connected account`}
      consequence={
        <>
          The hub deletes its stored {provider.name} grant. {provider.uses} stop working until someone connects{" "}
          {provider.name} again. The access you approved stays listed in your {provider.name} settings until you remove it
          there.
        </>
      }
      confirmLabel={`Disconnect ${provider.name}`}
      busy={write.isRunning}
      confirmDisabled={problem?.unknown === true}
      onConfirm={() => void confirm()}
      onCancel={onClose}
    >
      {(problem || parked !== undefined) && (
        <div className="flex flex-col gap-2">
          {problem && (
            <ErrorState
              headingLevel={3}
              title={problem.unknown ? "It may have been disconnected" : `${provider.name} was not disconnected`}
              message={problem.message}
              detail={problem.detail}
            />
          )}
          {parked !== undefined && <ParkedWriteNotice approvalId={parked} label={`Disconnect ${provider.name}`} />}
        </div>
      )}
    </ConfirmDialog>
  );
}
