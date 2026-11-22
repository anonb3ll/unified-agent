import {
  Bot,
  BookOpen,
  Home,
  Inbox,
  ListChecks,
  MessagesSquare,
  Play,
  Settings,
  type LucideIcon,
} from "lucide-react";

/** One of the eight destinations (spec §3), in sidebar order. */
export type Destination = {
  id: DestinationId;
  label: string;
  /** App path under the `/ui` basename. */
  path: string;
  icon: LucideIcon;
  /** Second key of the `g` chord (spec §4). */
  shortcut: string;
  description: string;
};

export type DestinationId = "home" | "inbox" | "work" | "agents" | "sessions" | "knowledge" | "runs" | "settings";

export const DESTINATIONS: readonly Destination[] = [
  { id: "home", label: "Home", path: "/home", icon: Home, shortcut: "h", description: "What needs you, what is running, what just finished" },
  { id: "inbox", label: "Inbox", path: "/inbox", icon: Inbox, shortcut: "i", description: "Everything waiting on a person, with the action inline" },
  { id: "work", label: "Work", path: "/work", icon: ListChecks, shortcut: "w", description: "Tasks, goals and plans" },
  { id: "agents", label: "Agents", path: "/agents", icon: Bot, shortcut: "a", description: "Who is in the organization and what each can do" },
  { id: "sessions", label: "Sessions", path: "/sessions", icon: MessagesSquare, shortcut: "s", description: "Live and past agent sessions" },
  { id: "knowledge", label: "Knowledge", path: "/knowledge", icon: BookOpen, shortcut: "k", description: "Memory notes and the vault" },
  { id: "runs", label: "Runs", path: "/runs", icon: Play, shortcut: "r", description: "Workflows, graphs, routines and cycles, with their history" },
  { id: "settings", label: "Settings", path: "/settings", icon: Settings, shortcut: "t", description: "Health, cloud, usage, credentials, integrations, audit" },
];

/** Phone bottom bar: these four, then "More" for the rest (spec §4). */
export const PHONE_TABS: readonly DestinationId[] = ["home", "inbox", "work", "sessions"];

export function destinationFor(pathname: string): Destination | undefined {
  const segment = pathname.replace(/^\/ui(?=\/|$)/, "").split("/")[1] ?? "";
  return DESTINATIONS.find((d) => d.path === `/${segment}`);
}

/** Items of the top bar's "New" menu (spec §4). */
export const CREATE_ACTIONS = [
  { id: "task", label: "Task", path: "/work/tasks/new", shortcut: "c" },
  { id: "session", label: "Session", path: "/sessions/new" },
  { id: "plan", label: "Plan", path: "/work/plans/new" },
  { id: "memory", label: "Memory note", path: "/knowledge/memory?new=1" },
  { id: "agent", label: "Agent", path: "/agents/add" },
] as const;

/**
 * Every route in spec §3, with a concrete sample for each dynamic segment.
 * Tests walk this list: every entry must resolve to a page, never not-found.
 */
export const ROUTE_SAMPLES: readonly { path: string; title: string }[] = [
  { path: "/home", title: "Home" },
  { path: "/inbox", title: "Inbox" },
  { path: "/inbox/messages", title: "Messages" },
  { path: "/work", title: "Work" },
  { path: "/work/tasks/new", title: "New task" },
  { path: "/work/tasks/42", title: "Task 42" },
  { path: "/work/goals", title: "Goals" },
  { path: "/work/goals/7", title: "Goal 7" },
  { path: "/work/plans", title: "Plans" },
  { path: "/work/plans/new", title: "New plan" },
  { path: "/work/plans/3", title: "Plan 3" },
  { path: "/work/canvas", title: "Planning canvas" },
  { path: "/agents", title: "Agents" },
  { path: "/agents/add", title: "Add an agent" },
  { path: "/agents/teams", title: "Teams" },
  { path: "/agents/skills", title: "Skills" },
  { path: "/agents/org", title: "Organization chart" },
  { path: "/agents/topology", title: "Topology" },
  { path: "/agents/hosts", title: "Hosts" },
  { path: "/agents/capabilities", title: "Capabilities" },
  { path: "/agents/profile/Desktop_Agent1", title: "Desktop_Agent1" },
  { path: "/sessions", title: "Sessions" },
  { path: "/sessions/new", title: "Start a session" },
  { path: "/sessions/abc123", title: "Session abc123" },
  { path: "/knowledge/memory", title: "Memory" },
  { path: "/knowledge/vault", title: "Vault" },
  { path: "/runs", title: "Runs" },
  { path: "/runs/workflows", title: "Workflows" },
  { path: "/runs/graphs", title: "Graphs" },
  { path: "/runs/routines", title: "Routines" },
  { path: "/runs/cycles", title: "Cycles" },
  { path: "/runs/cycles/9", title: "Cycle 9" },
  { path: "/runs/receipts", title: "Receipts" },
  { path: "/runs/telemetry", title: "Telemetry" },
  { path: "/runs/insights", title: "Insights" },
  { path: "/runs/executor", title: "Executor" },
  { path: "/settings/health", title: "Health" },
  { path: "/settings/platform", title: "Platform" },
  { path: "/settings/cloud", title: "Cloud" },
  { path: "/settings/usage", title: "Usage" },
  { path: "/settings/credentials", title: "Credentials" },
  { path: "/settings/audit", title: "Audit" },
  { path: "/settings/audit/provenance", title: "Provenance" },
  { path: "/settings/integrations", title: "Integrations" },
  { path: "/settings/integrations/slack", title: "Slack" },
  { path: "/settings/integrations/zulip", title: "Zulip" },
  { path: "/settings/integrations/notifications", title: "Notifications" },
  { path: "/settings/appearance", title: "Appearance" },
];

/** Paths that redirect (spec §3): from → to. */
export const ROUTE_REDIRECTS: readonly { from: string; to: string }[] = [
  { from: "/knowledge", to: "/knowledge/memory" },
  { from: "/settings", to: "/settings/health" },
];
