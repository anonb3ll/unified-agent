// @vitest-environment node
/**
 * Navigation contract (Control Center v2 spec §3, §10, §11), checked against
 * the spec itself: the eight destinations in sidebar order, a page (or a
 * redirect) for every route in the §3 route table, and a new path for every
 * legacy `?view=` id and alias in §11. A route or a legacy id added to the
 * spec without the app following it fails here.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { LEGACY_VIEW_ALIASES, LEGACY_VIEW_PATHS } from "../src/app/lib/legacyRedirect";
import { DESTINATIONS, ROUTE_REDIRECTS, ROUTE_SAMPLES } from "../src/app/routes";

const SPEC = readFileSync(path.resolve(__dirname, "../../../docs/designs/control-center-v2-spec.md"), "utf8");

function section(heading: string): string {
  const start = SPEC.indexOf(`\n## ${heading}`);
  expect(start, `spec section "${heading}"`).toBeGreaterThan(-1);
  const end = SPEC.indexOf("\n## ", start + 1);
  return SPEC.slice(start, end === -1 ? undefined : end);
}

/** Every path in the §3 route table, `a|b|c` alternatives expanded. */
function specRoutes(): string[] {
  const block = /Full route table[\s\S]*?```\n([\s\S]*?)```/.exec(section("3. Destinations and URLs"));
  expect(block).not.toBeNull();
  const routes = new Set<string>();
  // Prose in parentheses ("never /agents/:label") is not a route.
  for (const token of block![1].replace(/\([^)]*\)/g, "").match(/(?<=^|\s)\/[^\s]+/g) ?? []) {
    const alternatives = /^(.*\/)([^/]*\|[^/]*)$/.exec(token);
    if (alternatives) for (const leaf of alternatives[2].split("|")) routes.add(alternatives[1] + leaf);
    else routes.add(token);
  }
  return [...routes];
}

/** `/work/tasks/:id` matches `/work/tasks/42`. */
function matches(route: string, samplePath: string): boolean {
  const pattern = route.replace(/:[a-z]+/g, "[^/]+");
  return new RegExp(`^${pattern}$`).test(samplePath);
}

/** §11: legacy id → new path, and the alias sentence. */
function specLegacyMap(): { paths: Record<string, string>; aliases: Record<string, string> } {
  const text = section("11. Legacy view map");
  const paths: Record<string, string> = {};
  for (const row of text.matchAll(/^\| ([a-z_-]+) \| `([^`]+)` \| [a-z]+ \|$/gm)) paths[row[1]] = row[2];
  const aliases: Record<string, string> = {};
  const sentence = /Aliases:([\s\S]*?)(?:\n\n|\n?$)/.exec(text)![1];
  for (const clause of sentence.split(";")) {
    const [from, to] = clause.split("→");
    const target = to.replace(/[`.\s]/g, "");
    for (const name of from.matchAll(/`([^`]+)`/g)) aliases[name[1]] = target;
  }
  return { paths, aliases };
}

describe("the eight destinations (spec §3, §4)", () => {
  it("are Home, Inbox, Work, Agents, Sessions, Knowledge, Runs and Settings, in that order", () => {
    expect(DESTINATIONS.map((d) => [d.id, d.label, d.path])).toEqual([
      ["home", "Home", "/home"],
      ["inbox", "Inbox", "/inbox"],
      ["work", "Work", "/work"],
      ["agents", "Agents", "/agents"],
      ["sessions", "Sessions", "/sessions"],
      ["knowledge", "Knowledge", "/knowledge"],
      ["runs", "Runs", "/runs"],
      ["settings", "Settings", "/settings"],
    ]);
  });

  it("match the destination table in the spec", () => {
    const table = section("3. Destinations and URLs");
    const rows = [...table.matchAll(/^\| ([A-Z][a-z]+) \| `(\/[a-z]+)` \|/gm)].map((m) => [m[1], m[2]]);
    expect(rows).toEqual(DESTINATIONS.map((d) => [d.label, d.path]));
  });

  it("take the g-chord keys h i w a s k r t", () => {
    expect(DESTINATIONS.map((d) => d.shortcut).join(" ")).toBe("h i w a s k r t");
  });
});

describe("every route in the spec §3 table", () => {
  const routes = specRoutes();

  it("parses the whole table", () => {
    expect(routes.length).toBeGreaterThanOrEqual(48);
    expect(routes).toEqual(expect.arrayContaining(["/home", "/agents/profile/:label", "/settings/integrations/notifications"]));
  });

  it.each(routes)("%s has a page sample or a redirect", (route) => {
    const sampled = ROUTE_SAMPLES.some((s) => matches(route, s.path));
    const redirected = ROUTE_REDIRECTS.some((r) => r.from === route);
    expect(sampled || redirected, `${route} is in the spec but not in ROUTE_SAMPLES or ROUTE_REDIRECTS`).toBe(true);
  });

  it("has no page sample or redirect that the spec does not list", () => {
    const unlisted = [...ROUTE_SAMPLES.map((s) => s.path), ...ROUTE_REDIRECTS.map((r) => r.from)].filter(
      (p) => !routes.some((route) => matches(route, p)),
    );
    expect(unlisted).toEqual([]);
  });

  it("redirects land on a sampled page", () => {
    for (const r of ROUTE_REDIRECTS) expect(ROUTE_SAMPLES.map((s) => s.path)).toContain(r.to);
  });
});

describe("the legacy view map (spec §11)", () => {
  const spec = specLegacyMap();

  it("lists all 39 legacy views", () => {
    expect(Object.keys(spec.paths)).toHaveLength(39);
  });

  it("redirects every legacy id where the spec says", () => {
    expect(LEGACY_VIEW_PATHS).toEqual(spec.paths);
  });

  it("redirects every alias where the spec says", () => {
    expect(LEGACY_VIEW_ALIASES).toEqual(spec.aliases);
    for (const target of Object.values(LEGACY_VIEW_ALIASES)) expect(LEGACY_VIEW_PATHS).toHaveProperty(target);
  });

  it.each(Object.entries(spec.paths))("%s lands on a page of the new app (%s)", (_id, target) => {
    const pathname = target.split("?")[0];
    expect(ROUTE_SAMPLES.map((s) => s.path)).toContain(pathname);
  });
});
