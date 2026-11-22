/**
 * Classic deep links after the cutover (spec §10, §11): `/ui/?view=<id>` and
 * `/ui/#<id>` open the new path, for every legacy id and alias.
 */
import { describe, expect, it } from "vitest";
import { LEGACY_VIEW_ALIASES, LEGACY_VIEW_PATHS, legacyRedirect, resolveLegacyView } from "../../../src/app/lib/legacyRedirect";

const at = (pathname: string, search = "", hash = "") => legacyRedirect({ pathname, search, hash });

const IDS = Object.entries(LEGACY_VIEW_PATHS);
const ALIASES = Object.entries(LEGACY_VIEW_ALIASES).map(([alias, id]) => [alias, LEGACY_VIEW_PATHS[id]] as const);

describe("legacyRedirect", () => {
  it.each(IDS)("?view=%s goes to %s", (id, target) => {
    expect(at("/ui/", `?view=${id}`)).toBe(`/ui${target}`);
    expect(at("/ui", `?view=${id}`)).toBe(`/ui${target}`);
  });

  it.each(IDS)("#%s goes to %s", (id, target) => {
    expect(at("/ui/", "", `#${id}`)).toBe(`/ui${target}`);
  });

  it.each(ALIASES)("the alias %s goes to %s, as a query and as a hash", (alias, target) => {
    expect(at("/ui/", `?view=${alias}`)).toBe(`/ui${target}`);
    expect(at("/ui/", "", `#${alias}`)).toBe(`/ui${target}`);
  });

  it("reads ids the way the classic app did: trimmed, any case, URL-encoded", () => {
    expect(at("/ui/", "?view=%20Board%20")).toBe("/ui/work?view=board");
    expect(at("/ui/", "", "#Agent-Topology")).toBe("/ui/agents/topology");
    expect(at("/ui/", "", "#dispatch%5Freceipts")).toBe("/ui/runs/receipts");
  });

  it("sends an unknown ?view= id to Home, dropping the classic parameters", () => {
    expect(at("/ui/", "?view=nowhere&task=4")).toBe("/ui/home");
    expect(at("/ui/", "?view=")).toBe("/ui/home");
  });

  it("leaves a hash that is not a view alone, so the router opens Home", () => {
    expect(at("/ui/", "", "#main-content")).toBeNull();
    expect(at("/ui/", "", "#")).toBeNull();
    expect(at("/ui/", "", "#%E0%A4%A")).toBeNull();
  });

  it("does nothing for the landing without a view, or for a new-app path", () => {
    expect(at("/ui/")).toBeNull();
    expect(at("/ui/", "?showcase=primitives")).toBeNull();
    expect(at("/ui/work", "?view=board")).toBeNull();
    expect(at("/ui/home", "", "#tasks")).toBeNull();
    expect(at("/", "?view=tasks")).toBeNull();
  });

  it("prefers ?view= over the hash", () => {
    expect(at("/ui/", "?view=goals", "#tasks")).toBe("/ui/work/goals");
  });

  it("opens the approval a push or Slack link names in the Inbox", () => {
    expect(at("/ui", "?view=approvals&approval=42")).toBe("/ui/inbox?item=42&kind=approval");
    expect(at("/ui", "?view=decisions&approval=x")).toBe("/ui/inbox?kind=approval");
  });

  it("opens a task or a plan as its own page", () => {
    expect(at("/ui/", "?view=tasks&task=17")).toBe("/ui/work/tasks/17");
    expect(at("/ui/", "?view=queue&task=17&status=open")).toBe("/ui/work/tasks/17?status=open");
    expect(at("/ui/", "?view=tasks&task=x")).toBe("/ui/work?task=x");
    expect(at("/ui/", "?view=plans&plan=plan-9")).toBe("/ui/work/plans/plan-9");
    expect(at("/ui/", "?view=plans&plan=a%2Fb")).toBe("/ui/work/plans");
  });

  it("keeps the parameters the new page reads under the same name", () => {
    expect(at("/ui/", "?view=flow&task=12")).toBe("/ui/runs/insights?task=12");
    expect(at("/ui/", "?view=evidence&task_id=3&item=abc")).toBe("/ui/settings/audit?task_id=3&item=abc");
    expect(at("/ui/", "?view=vault&path=Wiki%2FA.md")).toBe("/ui/knowledge/vault?path=Wiki%2FA.md");
  });

  it("lets the target's own parameters win over carried ones", () => {
    expect(at("/ui/", "?view=operating&tab=teams")).toBe("/ui/agents/teams?tab=operating");
    expect(at("/ui/", "?view=work-graph&scope=x")).toBe("/ui/work?scope=work&view=graph");
  });
});

describe("resolveLegacyView", () => {
  it("names the canonical id for an id or an alias, null otherwise", () => {
    expect(resolveLegacyView("kanban")).toBe("board");
    expect(resolveLegacyView("board")).toBe("board");
    expect(resolveLegacyView("toString")).toBeNull();
    expect(resolveLegacyView(null)).toBeNull();
  });
});
