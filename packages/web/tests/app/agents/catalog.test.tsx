/**
 * Skill and team catalog actions and policy revisions, ported from the
 * classic Skills & Teams and Org views: surface drift, install, publish
 * (a blocked review is a refusal, not a success), team install, starter
 * templates (preview before install) and rolling back a policy revision.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { catalogRefusal } from "../../../src/app/agents/SkillActions";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import { fixtureEpoch } from "../support/epoch";
import { findPageHeading, renderRoute } from "../support/render";
import { agentsHub, parked, SKILLS_TEAMS } from "./fixtures";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

const posts = (hub: { calls: { method: string; path: string; body: unknown }[] }, path: string) => hub.calls.filter((c) => c.method === "POST" && c.path === path).map((c) => c.body);
const ALLOWED = { body: { ...SKILLS_TEAMS, permissions: { publish: true, install: true, team_install: true, drift: true } } };
const HASH = "a".repeat(64);

describe("skills: drift, install and publish", () => {
  it("checks a directory through the URL, then installs a version into it, replacing only a matching hash", async () => {
    const { hub, router } = renderRoute("/agents/skills", { hub: agentsHub({ "GET /api/skills-teams": ALLOWED, "POST /api/action/skills-teams": { body: { ok: true, message: "Installed review v3." } } }) });
    await findPageHeading("Skills");
    const install = await screen.findByRole("form", { name: "Install review" });
    expect(within(install).getByRole("button", { name: "Install review v3" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/^Skill directory/), { target: { value: " /work/skills " } });
    fireEvent.click(screen.getByRole("button", { name: "Check against the registry" }));
    await waitFor(() => expect(new URLSearchParams(router.state.location.search).get("surface")).toBe("/work/skills"));
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/skills-teams" && c.method === "GET")).toBe(true));
    const form = await screen.findByRole("form", { name: "Install review" });
    expect(form).toHaveTextContent("/work/skills/review/SKILL.md");
    fireEvent.click(within(form).getByLabelText(/Replace an existing review\/SKILL.md/));
    fireEvent.change(within(form).getByLabelText(/^Current SHA-256/), { target: { value: "short" } });
    expect(within(form).getByRole("button", { name: "Install review v3" })).toBeDisabled();
    fireEvent.change(within(form).getByLabelText(/^Current SHA-256/), { target: { value: HASH.toUpperCase() } });
    fireEvent.click(within(form).getByRole("button", { name: "Install review v3" }));
    await waitFor(() => expect(posts(hub, "/api/action/skills-teams")).toHaveLength(1));
    expect(posts(hub, "/api/action/skills-teams")[0]).toEqual({
      action: "install",
      params: { name: "review", version: 3, content_hash: "h", surface_dir: "/work/skills", expected_hash: HASH },
    });
    expect(await within(form).findByText("Installed review v3.")).toBeInTheDocument();
  });

  it("a publish the review blocked is shown as refused, with the findings", async () => {
    const { hub } = renderRoute("/agents/skills", {
      hub: agentsHub({ "GET /api/skills-teams": ALLOWED, "POST /api/action/skills-teams": { body: { blocked: true, review: { findings: [{ message: "shell pipe to sh" }] } } } }),
    });
    await findPageHeading("Skills");
    const form = await screen.findByRole("form", { name: "Publish a skill" });
    fireEvent.change(within(form).getByLabelText("Skill name"), { target: { value: "deploy" } });
    fireEvent.change(within(form).getByLabelText(/^Skill source path/), { target: { value: "skills/deploy" } });
    fireEvent.click(within(form).getByRole("button", { name: "Review and publish" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("shell pipe to sh");
    expect(posts(hub, "/api/action/skills-teams")[0]).toEqual({ action: "publish", params: { name: "deploy", path: "skills/deploy" } });
    expect(within(form).getByLabelText("Skill name")).toHaveValue("deploy");
  });

  it("explains what a profile without catalog rights cannot do", async () => {
    renderRoute("/agents/skills", { hub: agentsHub() });
    await findPageHeading("Skills");
    expect(await screen.findByText("Your profile cannot install skills.")).toBeInTheDocument();
    expect(screen.getByText("Your profile cannot publish skills.")).toBeInTheDocument();
  });

  it("names a 200 that is still a refusal", () => {
    expect(catalogRefusal({ ok: false, error: "no" })).toBe("no");
    expect(catalogRefusal({ pending: true })).toBeNull();
    expect(catalogRefusal({ ok: true })).toBeNull();
  });
});

describe("teams registry: team install and starter templates", () => {
  it("installs a captured team revision after a confirm", async () => {
    const { hub } = renderRoute("/agents/teams?tab=registry", { hub: agentsHub({ "GET /api/skills-teams": ALLOWED, "POST /api/action/skills-teams": { body: { ok: true } } }) });
    await findPageHeading("Teams");
    fireEvent.click(await screen.findByRole("button", { name: "Install Steward team revision #1" }));
    const dialog = await screen.findByRole("dialog", { name: "Install Steward team revision #1?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Install team" }));
    await waitFor(() => expect(posts(hub, "/api/action/skills-teams")).toEqual([{ action: "team_install", params: { team_id: "steward", revision: 1, content_hash: "t" } }]));
  });

  it("previews a starter template before it can be installed, and a changed choice needs a new preview", async () => {
    const { hub } = renderRoute("/agents/teams?tab=registry", { hub: agentsHub({ "GET /api/skills-teams": ALLOWED, "POST /api/action/skills-teams": { body: { ok: true, ready: true } } }) });
    await findPageHeading("Teams");
    const card = await screen.findByRole("region", { name: "Starter review-pair" });
    const install = within(card).getByRole("button", { name: "Install review-pair" });
    fireEvent.change(within(card).getByLabelText("Existing office id"), { target: { value: "steward" } });
    fireEvent.change(within(card).getByLabelText("Existing operating profile"), { target: { value: "steady" } });
    fireEvent.change(within(card).getByLabelText("builder roster label"), { target: { value: "Desktop_Agent2" } });
    fireEvent.change(within(card).getByLabelText("reviewer roster label"), { target: { value: "Desktop_Agent1" } });
    fireEvent.change(within(card).getByLabelText("reviewer harness"), { target: { value: "claude" } });
    expect(install).toBeDisabled();
    fireEvent.click(within(card).getByRole("button", { name: "Preview review-pair" }));
    await waitFor(() => expect(install).toBeEnabled());
    fireEvent.change(within(card).getByLabelText("Existing trust preset"), { target: { value: "inspection" } });
    expect(install).toBeDisabled();
    fireEvent.click(within(card).getByRole("button", { name: "Preview review-pair" }));
    await waitFor(() => expect(install).toBeEnabled());
    fireEvent.click(install);
    await waitFor(() => expect(posts(hub, "/api/action/skills-teams")).toHaveLength(3));
    const sent = posts(hub, "/api/action/skills-teams") as { action: string; params: Record<string, unknown> }[];
    expect(sent.map((a) => a.action)).toEqual(["starter_preview", "starter_preview", "starter_install"]);
    expect(sent[2].params).toEqual({
      template_id: "review-pair",
      team_id: "steward",
      operating_profile: "steady",
      trust_preset: "inspection",
      content_hash: "c".repeat(64),
      bindings: { builder: { label: "Desktop_Agent2", harness: "codex" }, reviewer: { label: "Desktop_Agent1", harness: "claude" } },
    });
  });
});

describe("org: policy revisions", () => {
  const stubResize = () => vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });

  it("lists revisions with drift and the latest change, and rolls back only after a confirm; a parked rollback is waiting", async () => {
    stubResize();
    const { hub } = renderRoute("/agents/org", {
      hub: agentsHub({
        "POST /api/action/config-revision": { status: 202, body: { ...parked("config_revision", 51), pending: true } },
        "GET /api/approvals/51": { body: { id: 51, tool: "config_revision", status: "pending", requested_by: "operator" } },
      }),
    });
    await findPageHeading("Organization chart");
    const section = await screen.findByRole("region", { name: "Policy revisions" });
    const table = await within(section).findByRole("table", { name: "Policy revisions" });
    expect(table).toHaveTextContent("#3");
    expect(within(section).getByText(/live configuration differs from the latest revision/)).toBeInTheDocument();
    expect(section).toHaveTextContent("+ approval: on");
    fireEvent.click(within(table).getByRole("button", { name: "Roll back to #2" }));
    const dialog = await screen.findByRole("dialog", { name: "Roll back to revision #2?" });
    expect(posts(hub, "/api/action/config-revision")).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(posts(hub, "/api/action/config-revision")).toEqual([{ kind: "policy", revision: 2 }]));
    expect(await within(section).findByRole("region", { name: "Approval status" })).toBeInTheDocument();
  });

  it("switches the configuration kind through the URL", async () => {
    stubResize();
    const { hub, router } = renderRoute("/agents/org", { hub: agentsHub() });
    await findPageHeading("Organization chart");
    fireEvent.change(await screen.findByLabelText("Configuration"), { target: { value: "approval_rule" } });
    await waitFor(() => expect(router.state.location.search).toBe("?rev_kind=approval_rule"));
    await waitFor(() => expect(hub.calls.filter((c) => c.path === "/api/config-revisions").length).toBeGreaterThan(1));
  });
});
