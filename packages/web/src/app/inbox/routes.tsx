/**
 * Inbox area (spec §9.1): everything waiting on a person at `/inbox`, and
 * conversations with agents at `/inbox/messages`. Replaces the classic
 * Attention, Approvals, Proposals and Messages views (spec §11).
 */
import { type RouteObject } from "react-router";
import type { PaletteCommand } from "../lib/commands";
import type { RouteHandle } from "../lib/routeHandle";
import { InboxPage } from "./InboxPage";
import { MessagesPage } from "./MessagesPage";

/** Mounted under the area path by `src/app/router.tsx`. */
export const routes: RouteObject[] = [
  { index: true, Component: InboxPage },
  { path: "messages", Component: MessagesPage, handle: { crumb: "Messages" } satisfies RouteHandle },
];

/** Command palette entries for this area. */
export const commands: PaletteCommand[] = [
  { id: "inbox.approvals", label: "Pending approvals", group: "Inbox", to: "/inbox?kind=approval", keywords: ["approve", "deny", "decisions", "actions"] },
  { id: "inbox.replies", label: "Agents waiting for your reply", group: "Inbox", to: "/inbox?kind=message", keywords: ["question", "answer", "reply"] },
  { id: "inbox.holds", label: "Tasks and holds waiting on you", group: "Inbox", to: "/inbox?kind=hold", keywords: ["red tier", "held", "merge", "pull request", "go"] },
  { id: "inbox.proposals", label: "Proposals to decide", group: "Inbox", to: "/inbox?kind=proposal", keywords: ["work proposal", "org proposal", "memory", "learning"] },
  { id: "inbox.alerts", label: "Alerts", group: "Inbox", to: "/inbox?kind=anomaly", keywords: ["attention", "anomaly", "stuck", "stale"] },
  { id: "inbox.messages", label: "Messages", group: "Inbox", to: "/inbox/messages", keywords: ["conversations", "chat", "mail"] },
  { id: "inbox.compose", label: "New message to an agent", group: "Create", to: "/inbox/messages?compose=1", keywords: ["send", "write", "message"] },
];
