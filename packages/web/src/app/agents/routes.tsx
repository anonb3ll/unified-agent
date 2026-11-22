/**
 * Agents area routes (spec §3, §7, §9.3). The agent page lives at
 * `/agents/profile/:label`, never `/agents/:label`, because a label may equal
 * a fixed word. Legacy views (spec §11): org → `/agents/org`, agent-topology
 * → `/agents/topology`, skills-teams → `/agents/teams`, operating →
 * `/agents/teams?tab=operating`, capabilities → `/agents/capabilities`,
 * devices → `/agents/hosts`.
 */
import type { RouteObject } from "react-router";
import type { PaletteCommand } from "../lib/commands";
import type { RouteHandle } from "../lib/routeHandle";
import { AddAgentPage } from "./AddAgentPage";
import { AgentPage } from "./AgentPage";
import { HostsPage } from "./HostsPage";
import { CapabilitiesPage, OrgPage, SkillsPage, TopologyPage } from "./OverviewPages";
import { RosterPage } from "./RosterPage";
import { TeamsPage } from "./TeamsPage";

/** Mounted under the area path by `src/app/router.tsx`. */
export const routes: RouteObject[] = [
  { index: true, Component: RosterPage },
  { path: "add", Component: AddAgentPage, handle: { crumb: "Add an agent" } satisfies RouteHandle },
  { path: "teams", Component: TeamsPage, handle: { crumb: "Teams" } satisfies RouteHandle },
  { path: "skills", Component: SkillsPage, handle: { crumb: "Skills" } satisfies RouteHandle },
  { path: "org", Component: OrgPage, handle: { crumb: "Organization chart" } satisfies RouteHandle },
  { path: "topology", Component: TopologyPage, handle: { crumb: "Topology" } satisfies RouteHandle },
  { path: "hosts", Component: HostsPage, handle: { crumb: "Hosts" } satisfies RouteHandle },
  { path: "capabilities", Component: CapabilitiesPage, handle: { crumb: "Capabilities" } satisfies RouteHandle },
  { path: "profile/:label", Component: AgentPage, handle: { crumb: ({ label }) => `${label ?? ""}` } satisfies RouteHandle },
];

/** Command palette entries for this area. */
export const commands: PaletteCommand[] = [
  { id: "agents.add", label: "Add an agent", group: "Agents", to: "/agents/add", keywords: ["new", "connect", "enroll", "token"] },
  { id: "agents.roster", label: "Agent roster", group: "Agents", to: "/agents", keywords: ["fleet", "who", "seats"] },
  { id: "agents.retired", label: "Retired agents", group: "Agents", to: "/agents?state=retired", keywords: ["removed"] },
  { id: "agents.teams", label: "Start a team", group: "Agents", to: "/agents/teams", keywords: ["team up", "boot"] },
  { id: "agents.operating", label: "Operating profiles, sprints and run settings", group: "Agents", to: "/agents/teams?tab=operating", keywords: ["operating", "profile", "sprint", "run settings"] },
  { id: "agents.passkeys", label: "Approver passkeys and signed-in devices", group: "Agents", to: "/agents/hosts#approver-passkeys", keywords: ["passkey", "sign-in", "revoke", "oauth", "webauthn"] },
  { id: "agents.revisions", label: "Policy revisions", group: "Agents", to: "/agents/org#policy-revisions", keywords: ["policy", "rollback", "revision", "config"] },
  { id: "agents.skills", label: "Skills", group: "Agents", to: "/agents/skills", keywords: ["skill", "registry"] },
  { id: "agents.org", label: "Organization chart", group: "Agents", to: "/agents/org", keywords: ["offices", "org"] },
  { id: "agents.topology", label: "Agent topology", group: "Agents", to: "/agents/topology", keywords: ["graph", "delegation"] },
  { id: "agents.hosts", label: "Hosts and devices", group: "Agents", to: "/agents/hosts", keywords: ["devices", "nodes", "machines", "acceptance"] },
  { id: "agents.capabilities", label: "Capability catalog", group: "Agents", to: "/agents/capabilities", keywords: ["capabilities", "catalog"] },
];
