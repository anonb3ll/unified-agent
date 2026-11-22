import { fireEvent, screen, waitFor, within, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WHOAMI_QUERY_KEY } from "../../../src/lib/api/auth";
import { draftKey, readDraft } from "../../../src/app/lib/drafts";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute, testQueryClient } from "../support/render";
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

/** A POST answer during which another tab signs in as B: the foundation cancels it (owner_changed, dispatched). */
const switchesOwner = (qc: ReturnType<typeof testQueryClient>, body: unknown) => () => {
  qc.setQueryData(WHOAMI_QUERY_KEY, { ...whoamiFixture({ agent: "Someone_Else" }), org: "org-2" });
  return { body };
};

// Review round 3, blocker 1: a cancelled write shows nothing, is not retried, keeps its draft,
// and a dispatched one keeps its attempt as outcome unknown.
describe("work: cancelled writes", () => {
  it("a plan create cancelled after it left is kept as an unsettled attempt under its owner", async () => {
    const qc = testQueryClient();
    renderRoute("/work/plans/new", { queryClient: qc, hub: workHub({ "POST /api/action/plan/create": switchesOwner(qc, { plan_id: "11", steps: [] }) }) });
    await findPageHeading("New plan");
    fireEvent.change(await screen.findByLabelText(/What the plan achieves/), { target: { value: "Launch" } });
    fireEvent.change(screen.getByLabelText("Step 1", { exact: true }), { target: { value: "Prepare" } });
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));
    await waitFor(() => {
      const stored = readDraft<{ goal: string; attempt: string }>(fixtureEpoch(), draftKey({ identity: "operator", operation: "plan.create" }));
      // Recorded before the send ("sending"), and never cleared by the cancellation; with no request
      // running, the form reads it as an attempt whose result is unknown.
      expect(stored?.goal).toBe("Launch");
      expect(["sending", "unknown"]).toContain(stored?.attempt);
    });
  });

  it("a goal close cancelled after it left keeps the dialog's choices", async () => {
    const qc = testQueryClient();
    renderRoute("/work/goals/7", { queryClient: qc, hub: workHub({ "POST /api/action/goal/close": switchesOwner(qc, { goal: { id: 7, status: "achieved" } }) }) });
    await findPageHeading("A usable Control Center");
    fireEvent.click(screen.getByRole("button", { name: "Close goal" }));
    const dialog = await screen.findByRole("dialog", { name: "Close this goal?" });
    fireEvent.change(within(dialog).getByLabelText(/Why/), { target: { value: "Superseded" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Close goal" }));
    await waitFor(() => {
      const stored = readDraft<{ open: boolean; reason: string }>(fixtureEpoch(), draftKey({ identity: "operator", operation: "goal.close", object: { goal: 7 } }));
      expect(stored).toMatchObject({ open: true, reason: "Superseded" });
    });
  });
});
