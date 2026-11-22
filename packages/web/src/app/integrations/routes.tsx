/**
 * Integrations area routes (spec §3, §7, §9.8), mounted under
 * `/settings/integrations` by the settings area.
 */
import type { RouteObject } from "react-router";
import type { PaletteCommand } from "../lib/commands";
import type { RouteHandle } from "../lib/routeHandle";
import { ConnectorsPage } from "./ConnectorsPage";
import { NotificationsPage } from "./NotificationsPage";
import { SlackPage } from "./SlackPage";
import { ZulipPage } from "./ZulipPage";

/** Mounted under the area path by `src/app/settings/routes.tsx`. */
export const routes: RouteObject[] = [
  { index: true, Component: ConnectorsPage },
  { path: "slack", Component: SlackPage, handle: { crumb: "Slack" } satisfies RouteHandle },
  { path: "zulip", Component: ZulipPage, handle: { crumb: "Zulip" } satisfies RouteHandle },
  { path: "notifications", Component: NotificationsPage, handle: { crumb: "Notifications" } satisfies RouteHandle },
];

/** Command palette entries for this area. */
export const commands: PaletteCommand[] = [
  {
    id: "integrations.connectors",
    label: "Connected accounts and connectors",
    group: "Settings",
    to: "/settings/integrations",
    keywords: ["integrations", "github", "oauth", "connect", "horizon"],
  },
  { id: "integrations.slack", label: "Slack status", group: "Settings", to: "/settings/integrations/slack", keywords: ["integrations", "chat"] },
  { id: "integrations.zulip", label: "Zulip status", group: "Settings", to: "/settings/integrations/zulip", keywords: ["integrations", "chat"] },
  {
    id: "integrations.notifications",
    label: "Notification delivery",
    group: "Settings",
    to: "/settings/integrations/notifications",
    keywords: ["integrations", "imessage", "alerts", "notify"],
  },
];
