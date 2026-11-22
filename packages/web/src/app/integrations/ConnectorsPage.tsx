/**
 * Settings, Integrations (spec §9.8): connected accounts (OAuth) and the
 * connector registry (Horizon-2 servers and packages), replacing the classic
 * `?view=connectors` pane. Register, authorize and revoke of a server or
 * package go through the hub's connector routes (#2470); each parks on an
 * approval that a different person decides.
 */
import { useState } from "react";
import { Button, DataTable, type Column } from "../../lib/ui";
import type { ConnectorPackageRow, ConnectorServerRow } from "../../lib/hub";
import { lifecycleText, seconds, secretsText, when } from "./api";
import { useIdentity } from "../lib/identity";
import { ConnectedAccounts } from "./ConnectedAccounts";
import { AuthorizeDialog, RegisterDialog, RevokeDialog } from "./ConnectorAdmin";
import { useConnectorInventory } from "./queries";
import { countOf, explainOrStale, IntegrationsPage, isStale, ReadRegion, Section, StatusLabel } from "./shared";

type Dialogs =
  | { kind: "register" }
  | { kind: "authorize"; serverId: string }
  | { kind: "revoke"; serverId: string; connector: string }
  | null;

export function ConnectorsPage() {
  const inventory = useConnectorInventory();
  const stale = isStale(inventory);
  // The dialog's target is captured when it opens; a refetch never moves it.
  const [dialog, setDialog] = useState<Dialogs>(null);
  // Dialog state belongs to one identity and one object: remount on either changing.
  const who = useIdentity().identity ?? "";

  const packageColumns: Column<ConnectorPackageRow>[] = [
    {
      id: "package",
      header: "Package",
      cell: (row) => (
        <span>
          <span className="font-medium">{row.package}</span>
          {row.manifest?.version && <span className="block text-xs text-[var(--ua-muted)]">Version {row.manifest.version}</span>}
        </span>
      ),
    },
    {
      id: "status",
      header: "Status",
      cell: (row) => {
        const s = lifecycleText(String(row.status));
        return (
          <div className="flex flex-col gap-1">
            <StatusLabel tone={s.tone} label={s.label} stale={stale} testId={`package-status-${row.package}`} />
            <span className="text-xs text-[var(--ua-muted)]">{explainOrStale(s.explain, stale)}</span>
            {row.health_detail && (
              <details className="text-xs text-[var(--ua-muted)]">
                <summary className="cursor-pointer">Health check</summary>
                <span className="block break-words">
                  {row.health_detail}
                  {typeof row.health_cached_age_s === "number" && ` (checked ${seconds(row.health_cached_age_s)} ago)`}
                </span>
              </details>
            )}
          </div>
        );
      },
    },
    {
      id: "does",
      header: "What it can do",
      cell: (row) => (row.manifest?.capabilities?.length ? row.manifest.capabilities.join(", ") : (row.scopes ?? []).join(", ") || "–"),
    },
    { id: "credentials", header: "Credentials", cell: (row) => secretsText(row.secrets) ?? "–" },
    { id: "policy", header: "Approval rule", cell: (row) => row.policy || "–" },
    { id: "verified", header: "Last verified", cell: (row) => when(row.last_verified) },
  ];

  const serverColumns: Column<ConnectorServerRow>[] = [
    { id: "server", header: "Server", cell: (row) => <span className="font-mono text-xs">{row.server_id}</span> },
    {
      id: "connector",
      header: "Connector",
      cell: (row) => (
        <span>
          {row.connector || "–"}
          {row.provenance && <span className="block break-all text-xs text-[var(--ua-muted)]">From {row.provenance}</span>}
        </span>
      ),
    },
    {
      id: "status",
      header: "Status",
      cell: (row) => <StatusLabel tone={lifecycleText(String(row.status)).tone} label={lifecycleText(String(row.status)).label} stale={stale} testId={`server-status-${row.server_id}`} />,
    },
    { id: "scopes", header: "Scopes", cell: (row) => (row.scopes ?? []).join(", ") || "–" },
    { id: "policy", header: "Approval rule", cell: (row) => row.policy || "–" },
    {
      id: "review",
      header: "Reviewed",
      cell: (row) => (
        <span className="text-xs">
          {row.reviewed_by ? `by ${row.reviewed_by}` : "–"}
          {row.review_ref ? <span className="block break-all text-[var(--ua-muted)]">{row.review_ref}</span> : null}
        </span>
      ),
    },
    { id: "registered", header: "Registered", cell: (row) => (row.revoked_at ? `Revoked ${when(row.revoked_at)}` : when(row.created_at)) },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      cell: (row) =>
        row.status === "revoked" ? null : (
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="secondary"
              aria-label={`Authorize ${row.server_id}`}
              onClick={() => setDialog({ kind: "authorize", serverId: row.server_id })}
            >
              Authorize
            </Button>
            <Button
              size="sm"
              variant="danger"
              aria-label={`Revoke ${row.server_id}`}
              onClick={() => setDialog({ kind: "revoke", serverId: row.server_id, connector: row.connector })}
            >
              Revoke
            </Button>
          </div>
        ),
    },
  ];

  return (
    <IntegrationsPage
      title="Integrations"
      description="Outside services the hub connects to: accounts it uses on your behalf, and the connectors agents may call."
    >
      <ConnectedAccounts />

      <Section
        id="connector-registry-heading"
        title="Connector registry"
        description={
          <>
            Horizon-2 is the hub's reviewed connector registry: a connector is packaged with the hub, then registered after a
            review, then authorized for a task before an agent can use it. Packages: {countOf(inventory, inventory.data?.packages.length)}.
            Registered servers: {countOf(inventory, inventory.data?.servers.length)}.
          </>
        }
        actions={
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setDialog({ kind: "register" })}
          >
            Register a connector
          </Button>
        }
      >
        <ReadRegion
          query={inventory}
          what="the connector registry"
          loadingLabel="Loading the connector registry"
          isEmpty={(d) => d.packages.length === 0 && d.servers.length === 0}
          empty={{
            title: "No connectors yet",
            description: "This hub has no packaged connectors and no registered servers.",
          }}
        >
          {(data) => (
            <div className="flex flex-col gap-6">
              <div className="flex flex-col gap-2">
                <h3 className="m-0 text-sm font-semibold">Packages</h3>
                <DataTable
                  columns={packageColumns}
                  data={data.packages}
                  getRowKey={(row) => row.package}
                  caption="Connector packages and their status"
                  scrollLabel="Connector packages"
                  emptyText="No connectors are packaged with this hub."
                />
              </div>
              <div className="flex flex-col gap-2">
                <h3 className="m-0 text-sm font-semibold">Registered servers</h3>
                <DataTable
                  columns={serverColumns}
                  data={data.servers}
                  getRowKey={(row) => row.server_id}
                  caption="Registered connector servers and their status"
                  scrollLabel="Registered connector servers"
                  emptyText="No connector servers are registered."
                />
              </div>
            </div>
          )}
        </ReadRegion>
      </Section>

      {dialog?.kind === "register" && (
        <RegisterDialog key={`${who}|register`} onClose={() => setDialog(null)} packages={(inventory.data?.packages ?? []).map((p) => p.package)} />
      )}
      {dialog?.kind === "authorize" && <AuthorizeDialog key={`${who}|authorize|${dialog.serverId}`} serverId={dialog.serverId} onClose={() => setDialog(null)} />}
      {dialog?.kind === "revoke" && <RevokeDialog key={`${who}|revoke|${dialog.serverId}`} serverId={dialog.serverId} connector={dialog.connector} onClose={() => setDialog(null)} />}
    </IntegrationsPage>
  );
}
