/**
 * Operating at `/agents/teams?tab=operating` (task #1757), ported from the
 * classic Operating view's tests: profiles with their effect, department
 * bindings, the policy draft, sprints (create, edit, link, stale edits) and
 * per-run settings (resolve, set, clear), each write parked or refused as
 * the hub answers.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { formatValue, parseOverrides, policyBlockDraft, resolvedRows, type OperatingResolved } from "../../../src/lib/operating/api";
import { clearParkedWrites, listParkedWrites } from "../../../src/app/lib/parkedWrites";
import { fixtureEpoch } from "../support/epoch";
import { findPageHeading, renderRoute } from "../support/render";
import { agentsHub, OPERATING, parked, RESOLVED, SPRINTS } from "./fixtures";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

const open = async (path = "/agents/teams?tab=operating", overrides = {}) => {
  const result = renderRoute(path, { hub: agentsHub(overrides) });
  await findPageHeading("Teams");
  return result;
};
const posts = (hub: { calls: { method: string; path: string; body: unknown }[] }, path: string) => hub.calls.filter((c) => c.method === "POST" && c.path === path).map((c) => c.body);

describe("operating: profiles and policy", () => {
  it("shows each profile's settings with their effect, and the office bindings with communication mode", async () => {
    await open();
    const profile = await screen.findByRole("table", { name: "steady settings" });
    expect(profile).toHaveTextContent('{"daily":10}');
    expect(within(profile).getByText("Enforced")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Operating profile per office" })).toHaveTextContent("steady (default)");
  });

  it("prepares the policy block as a draft for a policy revision, checks it, and writes nothing", async () => {
    const { hub } = await open();
    const draft = await screen.findByLabelText(/operating_profiles draft/);
    expect(JSON.parse((draft as HTMLTextAreaElement).value)).toEqual(policyBlockDraft(OPERATING as never));
    expect(screen.getByText(/policy_revision\(action="commit", kind="policy"/)).toBeInTheDocument();
    fireEvent.change(draft, { target: { value: "{not json" } });
    fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
    expect(await screen.findByText("The draft is not valid JSON.")).toBeInTheDocument();
    fireEvent.change(draft, { target: { value: '{"profiles": {}}' } });
    fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
    expect(await screen.findByText("The block must name at least one profile.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open policy revisions" })).toHaveAttribute("href", "/ui/agents/org#policy-revisions");
    expect(hub.calls.filter((c) => c.method === "POST")).toEqual([]);
  });

  it("explains a backend error instead of an empty tab", async () => {
    await open("/agents/teams?tab=operating", { "GET /api/operating": { body: { ...OPERATING, error: "policy file unreadable" } } });
    expect(await screen.findByText(/Operating profiles are unavailable: policy file unreadable/)).toBeInTheDocument();
  });
});

describe("operating: sprints", () => {
  it("creates a sprint with profile, office, overrides and dates, and clears the form after success", async () => {
    const { hub } = await open("/agents/teams?tab=operating", { "POST /api/action/operating-sprint": { body: { id: 8, name: "Next" } } });
    const form = await screen.findByRole("form", { name: "Create a sprint" });
    fireEvent.change(within(form).getByLabelText(/^Sprint name/), { target: { value: "   " } });
    fireEvent.click(within(form).getByRole("button", { name: "Create sprint" }));
    expect(await within(form).findByText("Give the sprint a name.")).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText(/^Sprint name/), { target: { value: " Next " } });
    fireEvent.change(within(form).getByLabelText("Profile"), { target: { value: "steady" } });
    fireEvent.change(within(form).getByLabelText(/^Office/), { target: { value: "steward" } });
    fireEvent.change(within(form).getByLabelText(/^Overrides/), { target: { value: "[1]" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create sprint" }));
    expect(await within(form).findByText("Overrides must be a JSON object.")).toBeInTheDocument();
    expect(posts(hub, "/api/action/operating-sprint")).toEqual([]);
    fireEvent.change(within(form).getByLabelText(/^Overrides/), { target: { value: '{"planning": "P1"}' } });
    fireEvent.change(within(form).getByLabelText("Starts"), { target: { value: "2026-11-12" } });
    fireEvent.change(within(form).getByLabelText("Ends"), { target: { value: "2026-11-30" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create sprint" }));
    await waitFor(() => expect(posts(hub, "/api/action/operating-sprint")).toHaveLength(1));
    expect(posts(hub, "/api/action/operating-sprint")[0]).toEqual({
      op: "add",
      name: "Next",
      profile: "steady",
      org_unit: "steward",
      overrides: { planning: "P1" },
      starts_at: "2026-11-12",
      ends_at: "2026-11-30",
    });
    await waitFor(() => expect(within(form).getByLabelText(/^Sprint name/)).toHaveValue(""));
  });

  it("shows the hub's refusal of a sprint and keeps what was typed", async () => {
    await open("/agents/teams?tab=operating", { "POST /api/action/operating-sprint": { status: 400, body: { error: "starts_at must be before ends_at" } } });
    const form = await screen.findByRole("form", { name: "Create a sprint" });
    fireEvent.change(within(form).getByLabelText(/^Sprint name/), { target: { value: "Backwards" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create sprint" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("starts_at must be before ends_at");
    expect(within(form).getByLabelText(/^Sprint name/)).toHaveValue("Backwards");
  });

  it("saves status, profile and overrides together, sending only what changed, and links and unlinks tasks", async () => {
    const { hub } = await open("/agents/teams?tab=operating", { "POST /api/action/operating-sprint": { body: { ok: true } } });
    const row = await screen.findByRole("listitem", { name: "Sprint 7: Cutover" });
    expect(within(row).getByRole("button", { name: "Save sprint 7" })).toBeDisabled();
    fireEvent.change(within(row).getByLabelText("Status of sprint 7"), { target: { value: "closed" } });
    fireEvent.change(within(row).getByLabelText("Overrides of sprint 7 (JSON)"), { target: { value: '{ "planning": "P3" }' } });
    fireEvent.click(within(row).getByRole("button", { name: "Save sprint 7" }));
    await waitFor(() => expect(posts(hub, "/api/action/operating-sprint")).toHaveLength(1));
    expect(posts(hub, "/api/action/operating-sprint")[0]).toEqual({ op: "update", sprint_id: 7, status: "closed", overrides: { planning: "P3" } });
    fireEvent.change(within(row).getByLabelText("Task id to link to sprint 7"), { target: { value: "x" } });
    fireEvent.click(within(row).getByRole("button", { name: "Link task" }));
    expect(await within(row).findByText("A task id is a positive whole number.")).toBeInTheDocument();
    fireEvent.change(within(row).getByLabelText("Task id to link to sprint 7"), { target: { value: "41" } });
    fireEvent.click(within(row).getByRole("button", { name: "Link task" }));
    await waitFor(() => expect(posts(hub, "/api/action/operating-sprint")).toHaveLength(2));
    expect(posts(hub, "/api/action/operating-sprint")[1]).toEqual({ op: "link", sprint_id: 7, task_id: 41 });
    await waitFor(() => expect(within(row).getByRole("button", { name: "Unlink task 2479 from sprint 7" })).toBeEnabled());
    fireEvent.click(within(row).getByRole("button", { name: "Unlink task 2479 from sprint 7" }));
    await waitFor(() => expect(posts(hub, "/api/action/operating-sprint")).toHaveLength(3));
    expect(posts(hub, "/api/action/operating-sprint")[2]).toEqual({ op: "unlink", sprint_id: 7, task_id: 2479 });
  });

  it("follows a refreshed sprint while clean and refuses to save a dirty edit over a newer stored value", async () => {
    let overrides: Record<string, unknown> = { planning: "P2" };
    const { hub, appClient } = await open("/agents/teams?tab=operating", {
      "GET /api/operating/sprints": () => ({ body: { ...SPRINTS, sprints: [{ ...SPRINTS.sprints[0], overrides }] } }),
    });
    const row = await screen.findByRole("listitem", { name: "Sprint 7: Cutover" });
    const field = within(row).getByLabelText("Overrides of sprint 7 (JSON)");
    expect(field).toHaveValue('{"planning":"P2"}');
    // Clean: a refresh is adopted.
    overrides = { planning: "P4" };
    await appClient().invalidateQueries();
    await waitFor(() => expect(field).toHaveValue('{"planning":"P4"}'));
    // Dirty: a refresh is not adopted, and saving is refused.
    fireEvent.change(field, { target: { value: '{"planning": "P9"}' } });
    overrides = { planning: "P5" };
    await appClient().invalidateQueries();
    expect(await within(row).findByText("Changed on the hub since you started editing.")).toBeInTheDocument();
    expect(field).toHaveValue('{"planning": "P9"}');
    fireEvent.click(within(row).getByRole("button", { name: "Save sprint 7" }));
    expect(await within(row).findByText(/changed on the hub since you started editing. Reload its values first/)).toBeInTheDocument();
    expect(posts(hub, "/api/action/operating-sprint")).toEqual([]);
    fireEvent.click(within(row).getByRole("button", { name: "Reload the hub's values" }));
    expect(field).toHaveValue('{"planning":"P5"}');
  });

  it("a parked sprint change is waiting, not done, and nothing is stored to replay", async () => {
    const result = await open("/agents/teams?tab=operating", {
      "POST /api/action/operating-sprint": { status: 202, body: parked("operating_sprint", 31) },
      "GET /api/approvals/31": { body: { id: 31, tool: "operating_sprint", status: "pending", requested_by: "operator" } },
    });
    const row = await screen.findByRole("listitem", { name: "Sprint 7: Cutover" });
    fireEvent.change(within(row).getByLabelText("Status of sprint 7"), { target: { value: "closed" } });
    fireEvent.click(within(row).getByRole("button", { name: "Save sprint 7" }));
    expect(await within(row).findByRole("region", { name: "Approval status" })).toBeInTheDocument();
    expect(within(row).queryByText(/: done\./)).not.toBeInTheDocument();
    expect(listParkedWrites(result.epoch())).toEqual([]);
  });
});

describe("operating: run settings", () => {
  it("resolves a task's next run with the layer that set each value, and starts the form from the stored run layer", async () => {
    const { router } = await open();
    fireEvent.change(await screen.findByLabelText(/^Task id/), { target: { value: "41" } });
    fireEvent.click(screen.getByRole("button", { name: "Show resolved settings" }));
    await waitFor(() => expect(router.state.location.search).toContain("run_task=41"));
    const table = await screen.findByRole("table", { name: "Resolved run settings" });
    expect(table).toHaveTextContent("budget.daily");
    expect(table).toHaveTextContent("office:steward");
    expect(table).toHaveTextContent("run:task-41:overrides");
    expect(screen.getByLabelText("Run profile")).toHaveValue("steady");
    expect(screen.getByLabelText(/^Run overrides/)).toHaveValue('{"planning":"P3"}');
  });

  it("sets the next run (keeping the stored profile) and clears the run layer only after a confirm", async () => {
    const { hub } = await open("/agents/teams?tab=operating&run_task=41", { "POST /api/action/operating-run": { body: { ok: true } } });
    const overrides = await screen.findByLabelText(/^Run overrides/);
    await waitFor(() => expect(overrides).toHaveValue('{"planning":"P3"}'));
    fireEvent.change(overrides, { target: { value: '{"planning": "P1"}' } });
    fireEvent.click(screen.getByRole("button", { name: "Set for next run" }));
    await waitFor(() => expect(posts(hub, "/api/action/operating-run")).toHaveLength(1));
    expect(posts(hub, "/api/action/operating-run")[0]).toEqual({ op: "set", task_id: 41, profile: "steady", overrides: { planning: "P1" } });
    fireEvent.click(screen.getByRole("button", { name: "Clear run settings" }));
    const dialog = await screen.findByRole("dialog", { name: "Clear task #41's run settings?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Clear run settings" }));
    await waitFor(() => expect(posts(hub, "/api/action/operating-run")).toHaveLength(2));
    expect(posts(hub, "/api/action/operating-run")[1]).toEqual({ op: "clear", task_id: 41 });
  });

  it("refuses an empty set before sending, and shows a parked set as waiting", async () => {
    const { hub } = await open("/agents/teams?tab=operating&run_task=41", {
      "GET /api/operating/resolve": { body: { ...RESOLVED, run_settings: null } },
      "POST /api/action/operating-run": { status: 202, body: parked("operating_run", 32) },
      "GET /api/approvals/32": { body: { id: 32, tool: "operating_run", status: "pending", requested_by: "operator" } },
    });
    await screen.findByRole("table", { name: "Resolved run settings" });
    expect(screen.getByRole("button", { name: "Clear run settings" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Set for next run" }));
    expect(await screen.findByText(/Pick a profile, give overrides, or both/)).toBeInTheDocument();
    expect(posts(hub, "/api/action/operating-run")).toEqual([]);
    fireEvent.change(screen.getByLabelText("Run profile"), { target: { value: "steady" } });
    fireEvent.click(screen.getByRole("button", { name: "Set for next run" }));
    expect(await screen.findByRole("region", { name: "Approval status" })).toBeInTheDocument();
  });
});

describe("operating: helpers", () => {
  it("expands mapping fields and keeps each field's enforcement", () => {
    const rows = resolvedRows(RESOLVED as OperatingResolved);
    expect(rows.map((r) => [r.key, r.source, r.enforcement])).toEqual([
      ["budget.daily", "office:steward", "enforced"],
      ["planning", "run:task-41:overrides", "recorded"],
    ]);
    expect(formatValue(null)).toBe("—");
    expect(parseOverrides("  ")).toEqual({});
    expect(() => parseOverrides("[]")).toThrow("Overrides must be a JSON object");
  });
});
