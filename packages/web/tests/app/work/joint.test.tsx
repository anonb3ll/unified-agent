import { fireEvent, screen, waitFor, within, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import type { MockHandler } from "../support/fetchMock";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { JOINT_3 } from "./fixtures";
import { workHub } from "./hub";
import { fixtureEpoch } from "../support/epoch";

vi.setConfig({ testTimeout: 60_000 });

afterEach(() => {
  // Unmount first: a form flushes its draft on unmount, which must not outlive the clear below.
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

// joint_planning.propose / review / decide (src/unified_agent/joint_planning.py).
const PROPOSAL = {
  steps: [{ description: "Build the foundation" }, { description: "Build every area" }, { description: "Cut over" }],
  author: "Desktop_Agent2",
  comment_id: 12,
  status: "proposed",
  role: "member",
  office: "web-office",
  live_fingerprint: "abc",
};
const REVIEW = { decision: "changes", reviewer: "Desktop_Agent1", role: "lead", comment_id: 11 };
const DECISION = { revision: 2, author: "Desktop_Agent1", role: "lead", text: "Cut over after the a11y pass", comment_id: 10, goal_id: 7, at: "2026-11-10T09:30:00+00:00" };

const as = (agent: string, joint: unknown, extra: Record<string, MockHandler> = {}) =>
  workHub({ "GET /api/whoami": { body: whoamiFixture({ agent }) }, "GET /api/plans/3/joint": { body: joint }, ...extra });

describe("work: shared plans", () => {
  // Review round 1, finding 17: a reviewer saw only the proposal's author.
  it("shows the pending proposal's steps, the last review and the decisions", async () => {
    renderRoute("/work/plans/3", { hub: as("Desktop_Agent1", { ...JOINT_3, pending: PROPOSAL, review: REVIEW, decisions: [DECISION] }) });
    await findPageHeading("Roll out Control Center v2");
    const panel = screen.getByRole("region", { name: "Shared plan" });
    const proposed = await within(panel).findByRole("list", { name: "Proposed steps" });
    expect(within(proposed).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Build the foundation", "Build every area", "Cut over"]);
    expect(within(panel).getByRole("list", { name: "Live steps" })).toHaveTextContent("Build the Work area");
    expect(within(panel).getByText(/Desktop_Agent1 asked for changes/)).toBeInTheDocument();
    expect(within(panel).getByRole("list", { name: "Decisions" })).toHaveTextContent("Cut over after the a11y pass");
  });

  // Review round 1, finding 19: Review was enabled for the caller's own or a held proposal.
  it.each([
    ["the caller's own proposal", "Desktop_Agent2", PROPOSAL, /cannot review your own proposal/],
    ["a proposal sent back for changes", "Desktop_Agent1", { ...PROPOSAL, status: "changes_requested" }, /sent back for changes/],
  ] as const)("Review is disabled with the reason for %s", async (_name, agent, pending, reason) => {
    renderRoute("/work/plans/3", { hub: as(agent, { ...JOINT_3, caller: { identity: agent, role: "member" }, pending }) });
    await findPageHeading("Roll out Control Center v2");
    const panel = screen.getByRole("region", { name: "Shared plan" });
    const button = await within(panel).findByRole("button", { name: "Review the proposal" });
    expect(button).toBeDisabled();
    expect(within(panel).getByText(reason)).toBeInTheDocument();
  });

  it("Review is enabled for another member's proposal", async () => {
    renderRoute("/work/plans/3", { hub: as("Desktop_Agent1", { ...JOINT_3, pending: PROPOSAL }) });
    await findPageHeading("Roll out Control Center v2");
    expect(await within(screen.getByRole("region", { name: "Shared plan" })).findByRole("button", { name: "Review the proposal" })).toBeEnabled();
  });

  // Review round 1, finding 18: a revision conflict left the dialog stuck on the old revision.
  it("a revision conflict offers Reload and Recover in the dialog and keeps the text", async () => {
    let reads = 0;
    const { hub } = renderRoute("/work/plans/3", {
      hub: as("Desktop_Agent1", JOINT_3, {
        "GET /api/plans/3/joint": () => ({ body: { ...JOINT_3, revision: reads++ === 0 ? 2 : 4 } }),
        "POST /api/action/plan/joint/decide": {
          status: 409,
          body: { error: "joint revision conflict on plan '3': expected 2, current 4. recover the plan and retry against current_revision", code: "conflict", expected_revision: 2, current_revision: 4 },
        },
      }),
    });
    await findPageHeading("Roll out Control Center v2");
    fireEvent.click(await screen.findByRole("button", { name: "Record a decision" }));
    const dialog = await screen.findByRole("dialog", { name: "Record a decision" });
    fireEvent.change(within(dialog).getByLabelText(/Comment number/), { target: { value: "12" } });
    fireEvent.change(within(dialog).getByLabelText(/^Decision/), { target: { value: "Ship Friday" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit" }));
    expect(await within(dialog).findByText(/Someone changed this shared plan/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Recover" })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Reload the shared plan" }));
    await waitFor(() => expect(within(dialog).getByLabelText(/Builds on revision/)).toHaveValue("4"));
    expect(within(dialog).getByLabelText(/^Decision/)).toHaveValue("Ship Friday");
    expect(hub.calls.filter((c) => c.path === "/api/plans/3/joint").length).toBeGreaterThanOrEqual(2);
  });
});
