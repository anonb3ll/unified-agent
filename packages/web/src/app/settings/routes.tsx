/**
 * Settings area routes (spec §3, §7, §9.7). `/settings` opens Health; the
 * separate integrations area is mounted under `/settings/integrations`.
 */
import { useLayoutEffect } from "react";
import { Navigate, Outlet, type RouteObject } from "react-router";
import type { PaletteCommand } from "../lib/commands";
import type { RouteHandle } from "../lib/routeHandle";
import { routes as integrationsRoutes } from "../integrations/routes";
import { useEpoch } from "../lib/epoch";
import { applyStoredAppearance } from "./appearance";
import { AppearancePage } from "./AppearancePage";
import { AuditPage } from "./AuditPage";
import { CloudPage } from "./CloudPage";
import { CredentialsPage } from "./CredentialsPage";
import { HealthPage } from "./HealthPage";
import { PlatformPage } from "./PlatformPage";
import { ProvenancePage } from "./ProvenancePage";
import { UsagePage } from "./UsagePage";

/**
 * The Settings area's layout: the stored density applies when the area
 * mounts (see "Needs from foundation"), inside the routed tree and so under
 * the owner epoch, never as a side effect of importing this module.
 */
export function SettingsArea() {
  const storage = useEpoch().sharedStorage;
  useLayoutEffect(() => {
    applyStoredAppearance(storage);
  }, [storage]);
  return <Outlet />;
}

/** The Settings pages, under `SettingsArea`. */
const pages: RouteObject[] = [
  { index: true, element: <Navigate to="health" replace /> },
  { path: "health", Component: HealthPage, handle: { crumb: "Health" } satisfies RouteHandle },
  { path: "platform", Component: PlatformPage, handle: { crumb: "Platform" } satisfies RouteHandle },
  { path: "cloud", Component: CloudPage, handle: { crumb: "Cloud" } satisfies RouteHandle },
  { path: "usage", Component: UsagePage, handle: { crumb: "Usage" } satisfies RouteHandle },
  { path: "credentials", Component: CredentialsPage, handle: { crumb: "Credentials" } satisfies RouteHandle },
  { path: "audit", Component: AuditPage, handle: { crumb: "Audit" } satisfies RouteHandle },
  { path: "audit/provenance", Component: ProvenancePage, handle: { crumb: "Provenance" } satisfies RouteHandle },
  { path: "appearance", Component: AppearancePage, handle: { crumb: "Appearance" } satisfies RouteHandle },
  { path: "integrations", handle: { crumb: "Integrations" } satisfies RouteHandle, children: integrationsRoutes },
];

/** Mounted under the area path by `src/app/router.tsx`. */
export const routes: RouteObject[] = [{ Component: SettingsArea, children: pages }];

/** Command palette entries for this area. */
export const commands: PaletteCommand[] = [
  { id: "settings.health", group: "Settings", label: "Hub health", to: "/settings/health", keywords: ["doctor", "status", "checks", "working", "attention"] },
  { id: "settings.platform", group: "Settings", label: "Platform objects and harness configuration", to: "/settings/platform", keywords: ["manifest", "object kinds", "harness"] },
  { id: "settings.cloud", group: "Settings", label: "Cloud worker", to: "/settings/cloud", keywords: ["gcp", "google", "instance", "backups", "budget"] },
  { id: "settings.usage", group: "Settings", label: "Usage, quota and spend", to: "/settings/usage", keywords: ["quota", "cost", "budget", "delivery", "allowance", "rate limit"] },
  { id: "settings.credentials", group: "Settings", label: "Credentials", to: "/settings/credentials", keywords: ["credential pool", "provider", "keys"] },
  { id: "settings.revoke", group: "Settings", label: "Revoke a session credential", to: "/settings/credentials", keywords: ["stop access", "session", "token"] },
  { id: "settings.audit", group: "Settings", label: "Evidence ledger", to: "/settings/audit", keywords: ["audit", "evidence", "verify chain", "export", "pdf", "csv"] },
  { id: "settings.provenance", group: "Settings", label: "Provenance", to: "/settings/audit/provenance", keywords: ["who authorized", "release", "graph"] },
  { id: "settings.appearance", group: "Settings", label: "Appearance: theme and density", to: "/settings/appearance", keywords: ["theme", "dark", "light", "high contrast", "density", "compact"] },
];
