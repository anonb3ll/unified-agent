import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { draftKey, saveDraft } from "../../../src/app/lib/drafts";
import { clearParkedWrites, listParkedWrites } from "../../../src/app/lib/parkedWrites";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { TASK_102 } from "./fixtures";
import { PARKED, workHub } from "./hub";
import { fixtureEpoch } from "../support/epoch";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  // Unmount first: a form flushes its draft on unmount, which must not outlive the clear below.
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

const posts = (calls: { method: string; path: string; body: unknown }[], path: string) => calls.filter((c) => c.method === "POST" && c.path === path);

/** A remembered release, stored as the page stores it: a draft keyed by identity and task. */
function rememberRelease(taskId: number, value: { workProductId: number | null; approvalId: string | null; phase?: string }) {
  saveDraft(fixtureEpoch(), draftKey({ identity: "operator", operation: "work-product.release", object: { id: taskId } }), { active: true, phase: "", ...value });
}

describe("work: task page", () => {
  // Review round 3, blocker 2: the release continuation follows the task (and identity), not the mounted component.
  it("a remembered release belongs to its task: another task's page does not show it", async () => {
    rememberRelease(102, { workProductId: 5, approvalId: "12" });
    const { router } = renderRoute("/work/tasks/102", { hub: workHub({ "GET /api/approvals/12": { body: { id: 12, status: "pending" } } }) });
    await findPageHeading("Ship the Work area");
    expect(await within(screen.getByRole("group", { name: "Release follow-up" })).findByText(/Work product #5/)).toBeInTheDocument();
    await act(async () => {
      await router.navigate("/work/tasks/101");
    });
    await findPageHeading("Write the release notes");
    const followUp = screen.getByRole("group", { name: "Release follow-up" });
    expect(within(followUp).queryByText(/#5/)).not.toBeInTheDocument();
  });

  it("renders the task with activity, comments and the goal", async () => {
    renderRoute("/work/tasks/102", { hub: workHub() });
    await findPageHeading("Ship the Work area");
    expect(screen.getByText("Task #102")).toBeInTheDocument();
    expect(await screen.findByText("Looks good so far.")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "A usable Control Center" })).toHaveAttribute("href", "/ui/work/goals/7");
    expect(await screen.findByText("run-1")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "https://github.com/example/repo/pull/7" }).length).toBeGreaterThan(0);
    // 102 blocks 101 (101 depends on 102).
    const deps = screen.getByRole("region", { name: "Dependencies" });
    expect(await within(deps).findByText("Blocks")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Send to an agent" })).toHaveAttribute("href", "/ui/sessions/new?task=102");
  });

  // Review round 1, blocker 3: a claim that loses answers 200 with claimed: false.
  it("a claim another agent holds says who holds it and until when, not that you claimed it", async () => {
    renderRoute("/work/tasks/101", {
      hub: workHub({
        "POST /api/action/task/claim": {
          body: { id: 101, claimed: false, safe_wait: { owner: "Desktop_Agent2", waiting_on: "review", until: "2026-11-10T18:00:00Z", generation: 2, claimed_by: "Desktop_Agent2" } },
        },
      }),
    });
    await findPageHeading("Write the release notes");
    fireEvent.click(screen.getByRole("button", { name: "Claim" }));
    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent(/Desktop_Agent2 holds this task/);
    expect(notice).toHaveTextContent(/waiting on review/);
    expect(notice).toHaveTextContent(/2026-11-10/);
    expect(screen.queryByText(/You claimed task/)).not.toBeInTheDocument();
  });

  // Review round 1, finding 5: task_delete removes the subtree and can revoke sessions.
  it("the delete confirmation names the subtree it removes", async () => {
    renderRoute("/work/tasks/102", { hub: workHub() });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "More task actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete this task and every task under it?" });
    expect(within(dialog).getByRole("list", { name: "Subtasks that are deleted too" })).toHaveTextContent("#104 Add the export button");
    expect(within(dialog).getByRole("list", { name: "Subtasks that are deleted too" })).toHaveTextContent("#105 Write the board tests");
    expect(dialog).toHaveTextContent(/sessions working on them lose their credentials/);
    // Review round 2, blocker 10: the board cannot list old done or archived subtasks, so the list is
    // called incomplete and the person types the task number to confirm.
    expect(dialog).toHaveTextContent(/at least 2/i);
    expect(dialog).toHaveTextContent(/may not list every task/);
    const confirm = within(dialog).getByRole("button", { name: "Delete the task and its subtree" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/Type 102 to confirm/), { target: { value: "102" } });
    expect(confirm).toBeEnabled();
  });

  // Review round 1, finding 16: removal needs dependency_id, which #2480 adds to overview links.
  it("removes a dependency by its id after a confirmation", async () => {
    const { hub } = renderRoute("/work/tasks/101", { hub: workHub({ "POST /api/action/dependency": { body: { removed: true } } }) });
    await findPageHeading("Write the release notes");
    const deps = screen.getByRole("region", { name: "Dependencies" });
    fireEvent.click(await within(deps).findByRole("button", { name: "Remove dependency on #102" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Remove this dependency?" })).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/dependency")[0]?.body).toEqual({ operation: "remove", dependency_id: 55 }));
  });

  // Review round 1, finding 9: task_update treats parent_id null as unchanged; 0 detaches.
  it("clearing the parent sends parent_id 0", async () => {
    const { hub } = renderRoute("/work/tasks/102", {
      hub: workHub({
        "GET /api/platform/objects/task/102": { body: { ...TASK_102, task: { ...TASK_102.task, parent_id: 100 } } },
        "POST /api/action/task": { body: TASK_102.task },
      }),
    });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Edit parent task" }));
    const box = screen.getByRole("combobox", { name: "Parent task" });
    fireEvent.keyDown(box, { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/task")[0]?.body).toMatchObject({ id: 102, parent_id: 0 }));
  });

  // Review round 1, finding 21: the inline editor manages focus.
  it("the inline editor takes focus, Escape cancels, and focus returns to Edit", async () => {
    const { hub } = renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/action/task": { body: TASK_102.task } }) });
    await findPageHeading("Ship the Work area");
    const edit = () => screen.getByRole("button", { name: "Edit title" });
    fireEvent.click(edit());
    const input = screen.getByLabelText("Title");
    await waitFor(() => expect(input).toHaveFocus());
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(edit()).toHaveFocus());
    expect(screen.queryByLabelText("Title")).not.toBeInTheDocument();
    fireEvent.click(edit());
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(edit()).toHaveFocus());
    fireEvent.click(edit());
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/task")).toHaveLength(1));
    await waitFor(() => expect(edit()).toHaveFocus());
  });

  // Review round 1, finding 14: an unresolved or failed read is not "none".
  it("subtasks and the pull request say unknown, not none, when their reads fail", async () => {
    renderRoute("/work/tasks/101", {
      hub: workHub({ "GET /api/board": { status: 403, body: { error: "denied" } }, "GET /api/evidence": { status: 500, body: { error: "locked" } } }),
    });
    await findPageHeading("Write the release notes");
    const subtasks = screen.getByRole("region", { name: "Subtasks" });
    expect(await within(subtasks).findByText(/Subtasks could not be loaded/)).toBeInTheDocument();
    expect(within(subtasks).queryByText(/No open subtasks/)).not.toBeInTheDocument();
    const activity = screen.getByRole("region", { name: "Activity" });
    expect(await within(activity).findByText(/not known whether a pull request/, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(within(activity).queryByText(/No pull request is recorded/)).not.toBeInTheDocument();
  });

  // Review round 1, finding 11: run history follows next_cursor (runs_projection.list_runs).
  it("shows older runs through the runs cursor", async () => {
    const run = (id: string) => ({ run_id: id, kind: "dispatch", task_id: 102, agent: "Desktop_Agent1", status: "done", outcome: "done", started_at: "2026-11-09T10:00:00Z" });
    renderRoute("/work/tasks/102", {
      hub: workHub({
        "GET /api/runs": ({ url }) => (new URL(url, "http://x").searchParams.get("cursor") === "r2" ? { body: { runs: [run("run-old")], next_cursor: null } } : { body: { runs: [run("run-1")], next_cursor: "r2" } }),
      }),
    });
    await findPageHeading("Ship the Work area");
    fireEvent.click(await screen.findByRole("button", { name: "Show older runs" }));
    expect(await screen.findByText("run-old")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show older runs" })).not.toBeInTheDocument();
  });

  // Review round 2, blocker 2: a live refresh must not move the base of an open edit.
  it("an edit keeps the timestamp it started from when the task changes underneath, and says so", async () => {
    let reads = 0;
    const bob = { ...TASK_102, task: { ...TASK_102.task, notes: "Bob's notes", updated_at: "2026-11-10T11:30:00Z" } };
    const { hub, appClient } = renderRoute("/work/tasks/102", {
      hub: workHub({
        "GET /api/platform/objects/task/102": () => ({ body: reads++ === 0 ? TASK_102 : bob }),
        "POST /api/action/task": { status: 409, body: { error: "task 102 changed", code: "conflict" } },
      }),
    });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), { target: { value: "Alice's notes" } });
    await act(async () => {
      await appClient().invalidateQueries({ queryKey: ["work", "task", "102"] });
    });
    expect(await screen.findByText(/This changed while you were editing/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/task")).toHaveLength(1));
    expect(posts(hub.calls, "/api/action/task")[0].body).toEqual({ id: 102, notes: "Alice's notes", expected_updated_at: "2026-11-10T11:00:00Z" });
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("Alice's notes");
  });

  // Review round 3, item 7: a 409 on an edit whose field did not change is resolved against the reloaded task.
  it("an edit refused by a change to another field reloads, then retries against the new base", async () => {
    let fresh = false;
    let writes = 0;
    const { hub } = renderRoute("/work/tasks/102", {
      hub: workHub({
        "GET /api/platform/objects/task/102": () => ({ body: fresh ? { ...TASK_102, task: { ...TASK_102.task, status: "blocked", updated_at: "2026-11-10T11:30:00Z" } } : TASK_102 }),
        "POST /api/action/task": () => (writes++ === 0 ? { status: 409, body: { error: "task 102 changed", code: "conflict" } } : { body: TASK_102.task }),
      }),
    });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), { target: { value: "Alice's notes" } });
    fresh = true;
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.click(await screen.findByRole("button", { name: "Reload" }));
    const retry = await screen.findByRole("button", { name: "Retry" });
    await waitFor(() => expect(retry).toBeEnabled());
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("Alice's notes");
    fireEvent.click(retry);
    await waitFor(() => expect(posts(hub.calls, "/api/action/task")).toHaveLength(2));
    expect(posts(hub.calls, "/api/action/task")[1].body).toEqual({ id: 102, notes: "Alice's notes", expected_updated_at: "2026-11-10T11:30:00Z" });
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Description" })).not.toBeInTheDocument());
  });

  // Review round 2, item 4: every task update carries the snapshot it was based on.
  it("clear due date and the trust switch send expected_updated_at", async () => {
    const { hub } = renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/action/task": { body: TASK_102.task } }) });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Clear due date" }));
    fireEvent.click(screen.getByRole("switch", { name: "Treat the description as untrusted" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/task")).toHaveLength(2));
    expect(posts(hub.calls, "/api/action/task").map((c) => c.body)).toEqual([
      { id: 102, clear_due_at: true, expected_updated_at: "2026-11-10T11:00:00Z" },
      { id: 102, untrusted: true, expected_updated_at: "2026-11-10T11:00:00Z" },
    ]);
  });

  // Review round 2, item 11: a parked claim run from the notice is read like a direct claim.
  it("a parked claim run from the notice that loses shows who holds the task", async () => {
    let claims = 0;
    renderRoute("/work/tasks/101", {
      hub: workHub({
        "GET /api/whoami": { body: { ...whoamiFixture(), org: "default" } },
        "GET /api/approvals/41": { body: { id: 41, tool: "task_claim", status: "approved" } },
        "POST /api/action/task/claim": () =>
          claims++ === 0
            ? { status: 202, body: { approval_id: 41, code: "approval_required", status: "approval_pending", tool: "task_claim" } }
            : { body: { id: 101, claimed: false, safe_wait: { owner: "Desktop_Agent2", waiting_on: "review", until: "2026-11-10T18:00:00Z", generation: 2, claimed_by: "Desktop_Agent2" } } },
      }),
    });
    await findPageHeading("Write the release notes");
    fireEvent.click(screen.getByRole("button", { name: "Claim" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run it now" }, { timeout: 15_000 }));
    expect(await screen.findByText(/Desktop_Agent2 holds this task/)).toBeInTheDocument();
    expect(screen.queryByText(/You claimed task/)).not.toBeInTheDocument();
  });

  // Review round 3, item 5: the chosen status keeps the base it was chosen against.
  it("a status chosen before another writer changed the task keeps its base and says so", async () => {
    let fresh = false;
    const { hub, appClient } = renderRoute("/work/tasks/102", {
      hub: workHub({
        "GET /api/platform/objects/task/102": () => ({ body: fresh ? { ...TASK_102, task: { ...TASK_102.task, status: "blocked", updated_at: "2026-11-10T11:45:00Z" } } : TASK_102 }),
        "POST /api/action/task": { status: 409, body: { error: "task 102 changed", code: "conflict" } },
      }),
    });
    await findPageHeading("Ship the Work area");
    const status = screen.getByRole("region", { name: "Status" });
    fireEvent.change(within(status).getByLabelText("Status"), { target: { value: "done" } });
    fresh = true;
    await act(async () => {
      await appClient().invalidateQueries({ queryKey: ["work", "task", "102"] });
    });
    expect(await within(status).findByText(/changed since you chose/)).toBeInTheDocument();
    expect(within(status).getByLabelText("Status")).toHaveValue("done");
    fireEvent.click(within(status).getByRole("button", { name: "Change status" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/task")).toHaveLength(1));
    expect(posts(hub.calls, "/api/action/task")[0].body).toEqual({ id: 102, status: "done", expected_updated_at: "2026-11-10T11:00:00Z" });
  });

  // Review round 3, item 6: the release is fenced on the claim seen when the dialog opened.
  it("a claim that changes while the release dialog is open needs a new confirmation", async () => {
    let fresh = false;
    const { hub, appClient } = renderRoute("/work/tasks/102", {
      hub: workHub({
        "GET /api/platform/objects/task/102": () => ({
          body: fresh ? { ...TASK_102, task: { ...TASK_102.task, claimed_by: "Desktop_Agent2", current_run_id: "run-2", claimed_at: "2026-11-10T11:50:00Z" } } : TASK_102,
        }),
        "POST /api/tasks/102/claim-release": { body: { released: true } },
      }),
    });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Release claim" }));
    const dialog = await screen.findByRole("dialog", { name: "Release the claim?" });
    fireEvent.change(within(dialog).getByLabelText(/Why/), { target: { value: "Stalled" } });
    fresh = true;
    await act(async () => {
      await appClient().invalidateQueries({ queryKey: ["work", "task", "102"] });
    });
    expect(await within(dialog).findByText(/The claim changed/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Release claim" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Review the new claim" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Release claim" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/tasks/102/claim-release")[0]?.body).toEqual({ expected_claimant: "Desktop_Agent2", reason: "Stalled", expected_run_id: "run-2" }),
    );
  });

  it("an unknown task says so and offers Retry", async () => {
    renderRoute("/work/tasks/999", { hub: workHub({ "GET /api/platform/objects/task/999": { status: 404, body: { error: "unknown task: 999" } } }) });
    await findPageHeading("Task 999");
    expect(await screen.findByText("This task does not exist")).toBeInTheDocument();
  });

  it("edits the title in place with expected_updated_at", async () => {
    const { hub } = renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/action/task": { body: TASK_102.task } }) });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
    const input = screen.getByLabelText("Title");
    fireEvent.change(input, { target: { value: "Ship the whole Work area" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/task")).toHaveLength(1));
    expect(posts(hub.calls, "/api/action/task")[0].body).toEqual({ id: 102, title: "Ship the whole Work area", expected_updated_at: "2026-11-10T11:00:00Z" });
  });

  // Spec §12 defect 1: a 202 pending approval was shown as success in task detail.
  it("a parked status change shows Waiting for approval and keeps the replay exact", async () => {
    const { hub } = renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/action/task": PARKED("task_update", 31) }) });
    await findPageHeading("Ship the Work area");
    const status = screen.getByRole("region", { name: "Status" });
    fireEvent.change(within(status).getByLabelText("Status"), { target: { value: "done" } });
    fireEvent.click(within(status).getByRole("button", { name: "Change status" }));
    expect(await within(status).findByText(/Waiting for approval/)).toBeInTheDocument();
    expect(within(status).queryByText(/Status changed/)).not.toBeInTheDocument();
    const sent = posts(hub.calls, "/api/action/task")[0].body;
    expect(sent).toEqual({ id: 102, status: "done", expected_updated_at: "2026-11-10T11:00:00Z" });
    // task_update is rebuilt from exactly this body and the verified identity, so the replay repeats it unchanged.
    expect(listParkedWrites(fixtureEpoch()).map((w) => w.body)).toEqual([sent]);
  });

  it("reaches every task_update field from the page", { timeout: 120_000 }, async () => {
    const { hub } = renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/action/task": { body: TASK_102.task } }) });
    await findPageHeading("Ship the Work area");
    const edit = async (button: string, label: string, value: string) => {
      fireEvent.click(screen.getByRole("button", { name: button }));
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument());
    };
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), { target: { value: "New notes" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument());
    await edit("Edit priority", "Priority", "low");
    await edit("Edit project", "Project", "docs");
    await edit("Edit domain", "Domain", "backend");
    await edit("Edit queue", "Queue", "laptop");
    await edit("Edit due date", "Due date", "2026-12-01");
    await edit("Edit evidence link", "Evidence link", "https://example.com/pr/9");
    fireEvent.click(screen.getByRole("button", { name: "Clear due date" }));
    fireEvent.click(screen.getByRole("switch", { name: "Treat the description as untrusted" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Add a note" }), { target: { value: "Progress" } });
    fireEvent.click(screen.getByRole("button", { name: "Add note" }));
    const status = screen.getByRole("region", { name: "Status" });
    fireEvent.change(within(status).getByLabelText("Status"), { target: { value: "open" } });
    fireEvent.click(within(status).getByRole("button", { name: "Change status" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/task").length).toBeGreaterThanOrEqual(11));
    const keys = new Set(posts(hub.calls, "/api/action/task").flatMap((c) => Object.keys(c.body as object)));
    for (const k of ["notes", "priority", "project", "expected_project", "domain", "lane", "due_at", "clear_due_at", "evidence_ref", "untrusted", "append_note", "idempotency_key", "status", "expected_updated_at"]) {
      expect(keys, k).toContain(k);
    }
  });

  it("archive and delete go through confirmations; delete posts the id", async () => {
    const { hub, router } = renderRoute("/work/tasks/102", {
      hub: workHub({ "POST /api/action/task": { body: TASK_102.task }, "POST /api/action/task/delete": { body: { deleted: true } } }),
    });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "More task actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Archive" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Archive this task?" })).getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/task")[0]?.body).toEqual({ id: 102, archived: true, expected_updated_at: "2026-11-10T11:00:00Z" }));
    fireEvent.click(screen.getByRole("button", { name: "More task actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete this task and every task under it?" });
    // The delete dialog opens on its confirmation field; Enter there does not confirm a danger dialog.
    expect(within(dialog).getByLabelText(/Type 102 to confirm/)).toHaveFocus();
    fireEvent.change(within(dialog).getByLabelText(/Type 102 to confirm/), { target: { value: "102" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete the task and its subtree" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/task/delete")[0]?.body).toEqual({ id: 102 }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/ui/work"));
  });

  it("releases a claim with the fencing fields the hub needs", async () => {
    const { hub } = renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/tasks/102/claim-release": { body: { released: true } } }) });
    await findPageHeading("Ship the Work area");
    fireEvent.click(screen.getByRole("button", { name: "Release claim" }));
    const dialog = await screen.findByRole("dialog", { name: "Release the claim?" });
    fireEvent.change(within(dialog).getByLabelText(/Why/), { target: { value: "Stalled" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Release claim" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/tasks/102/claim-release")[0]?.body).toEqual({ expected_claimant: "Desktop_Agent1", reason: "Stalled", expected_run_id: "run-1" }),
    );
  });

  // Spec §12 defect 9: "approve, then submit the same again" dead ends. A parked
  // work-product release continues through /release, never by resubmitting.
  it("a parked work product continues through its release route, not a resubmission", async () => {
    const { hub } = renderRoute("/work/tasks/102", {
      hub: workHub({
        "POST /api/work-products": { status: 202, body: { status: "pending", approval_id: 12, code: "approval_required", work_product_id: 5 } },
        "GET /api/approvals/12": { body: { id: 12, status: "approved", payload: { work_product_id: 5 } } },
        "POST /api/work-products/5/release": { body: { id: 5, approval_id: 12, released: true } },
      }),
    });
    await findPageHeading("Ship the Work area");
    const form = screen.getByRole("form", { name: "Submit a work product" });
    fireEvent.change(within(form).getByLabelText(/File in the workspace/), { target: { value: "reports/a.md" } });
    fireEvent.change(within(form).getByLabelText(/Title/), { target: { value: "Report" } });
    fireEvent.change(within(form).getByLabelText(/Template/), { target: { value: "memo" } });
    fireEvent.click(within(form).getByRole("button", { name: "Submit for release" }));
    expect(listParkedWrites(fixtureEpoch())).toHaveLength(0);
    const followUp = screen.getByRole("group", { name: "Release follow-up" });
    expect(await within(followUp).findByText(/Approved. Continue the release of work product #5/)).toBeInTheDocument();
    fireEvent.click(within(followUp).getByRole("button", { name: "Continue release" }));
    await waitFor(() => expect(posts(hub.calls, "/api/work-products/5/release")).toHaveLength(1));
    expect(posts(hub.calls, "/api/work-products/5/release")[0].body).toBeUndefined();
    expect(posts(hub.calls, "/api/work-products")).toHaveLength(1);
    // A 200 that carries approval_id is a release, not another approval.
    await waitFor(() => expect(within(followUp).queryByText(/is waiting/)).not.toBeInTheDocument());
  });

  // Review round 1, finding 20: the submission's fields stay until the release can continue.
  it("a parked work product keeps its fields until its number is known, with a retry", async () => {
    let approvalReads = 0;
    renderRoute("/work/tasks/102", {
      hub: workHub({
        // A 202 whose work_product_id is null (routes._work_product_write could not read it): looked up from the approval.
        "POST /api/work-products": { status: 202, body: { status: "pending", approval_id: 12, code: "approval_required", work_product_id: null } },
        "GET /api/approvals/12": () => (approvalReads++ < 1 ? { status: 500, body: { error: "locked" } } : { body: { id: 12, status: "pending", payload: { work_product_id: 5 } } }),
      }),
    });
    await findPageHeading("Ship the Work area");
    const form = screen.getByRole("form", { name: "Submit a work product" });
    fireEvent.change(within(form).getByLabelText(/File in the workspace/), { target: { value: "reports/a.md" } });
    fireEvent.change(within(form).getByLabelText(/Title/), { target: { value: "Report" } });
    fireEvent.change(within(form).getByLabelText(/Template/), { target: { value: "memo" } });
    fireEvent.click(within(form).getByRole("button", { name: "Submit for release" }));
    const followUp = screen.getByRole("group", { name: "Release follow-up" });
    expect(await within(followUp).findByText(/could not be looked up yet/, {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(within(form).getByLabelText(/File in the workspace/)).toHaveValue("reports/a.md");
    fireEvent.click(within(followUp).getByRole("button", { name: "Look it up again" }));
    expect(await within(followUp).findByText(/Waiting for approval. Work product #5/, {}, { timeout: 15_000 })).toBeInTheDocument();
    await waitFor(() => expect(within(form).getByLabelText(/File in the workspace/)).toHaveValue(""));
  });

  // Review round 2, blocker 7: a parked submission continues as a release; nothing steers to resubmitting.
  it("a parked work product takes its number from the 202, blocks resubmission and never says submit again", async () => {
    const { hub } = renderRoute("/work/tasks/102", {
      hub: workHub({ "POST /api/work-products": { status: 202, body: { status: "pending", approval_id: 12, code: "approval_required", work_product_id: 5 } } }),
    });
    await findPageHeading("Ship the Work area");
    const form = screen.getByRole("form", { name: "Submit a work product" });
    fireEvent.change(within(form).getByLabelText(/File in the workspace/), { target: { value: "reports/a.md" } });
    fireEvent.change(within(form).getByLabelText(/Title/), { target: { value: "Report" } });
    fireEvent.change(within(form).getByLabelText(/Template/), { target: { value: "memo" } });
    fireEvent.click(within(form).getByRole("button", { name: "Submit for release" }));
    const followUp = screen.getByRole("group", { name: "Release follow-up" });
    expect(await within(followUp).findByText(/Work product #5/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Submit for release" })).toBeDisabled();
    // The section never steers to resubmitting (the foundation's generic toast is outside it: Needs from foundation).
    const section = screen.getByRole("region", { name: "Evidence and work products" });
    expect(within(section).queryByText(/submit this form again/i)).not.toBeInTheDocument();
    expect(posts(hub.calls, "/api/work-products")).toHaveLength(1);
  });

  // Review round 2, item 9: a release remembered across navigation reads its approval's real state.
  it.each([
    ["pending", /Waiting for approval/, false],
    ["approved", /Approved/, true],
    ["denied", /was denied/, false],
  ] as const)("a remembered release with a %s approval shows that state and gates Continue", async (status, text, enabled) => {
    rememberRelease(102, { workProductId: 5, approvalId: "12" });
    renderRoute("/work/tasks/102", { hub: workHub({ "GET /api/approvals/12": { body: { id: 12, tool: "work_product_submit", status } } }) });
    await findPageHeading("Ship the Work area");
    const followUp = screen.getByRole("group", { name: "Release follow-up" });
    expect(await within(followUp).findByText(text, {}, { timeout: 15_000 })).toBeInTheDocument();
    const button = within(followUp).getByRole("button", { name: "Continue release" });
    if (enabled) expect(button).toBeEnabled();
    else expect(button).toBeDisabled();
  });

  // Review round 2, item 8: a lost response is recorded and must be settled before trying again.
  it("a submission whose response is lost blocks another until it is settled", async () => {
    const { hub } = renderRoute("/work/tasks/102", {
      hub: workHub({
        "POST /api/work-products": () => {
          throw new TypeError("Failed to fetch");
        },
      }),
    });
    await findPageHeading("Ship the Work area");
    const form = screen.getByRole("form", { name: "Submit a work product" });
    fireEvent.change(within(form).getByLabelText(/File in the workspace/), { target: { value: "reports/a.md" } });
    fireEvent.change(within(form).getByLabelText(/Title/), { target: { value: "Report" } });
    fireEvent.change(within(form).getByLabelText(/Template/), { target: { value: "memo" } });
    fireEvent.click(within(form).getByRole("button", { name: "Submit for release" }));
    const followUp = screen.getByRole("group", { name: "Release follow-up" });
    expect(await within(followUp).findByText(/could not confirm whether the work product was submitted/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Submit for release" })).toBeDisabled();
    fireEvent.click(within(followUp).getByRole("button", { name: "It was not submitted, let me submit again" }));
    expect(within(form).getByRole("button", { name: "Submit for release" })).toBeEnabled();
    expect(within(form).getByLabelText(/File in the workspace/)).toHaveValue("reports/a.md");
    expect(posts(hub.calls, "/api/work-products")).toHaveLength(1);
  });

  it("a release whose response is lost asks before continuing again", async () => {
    rememberRelease(102, { workProductId: 5, approvalId: "12" });
    const { hub } = renderRoute("/work/tasks/102", {
      hub: workHub({
        "GET /api/approvals/12": { body: { id: 12, tool: "work_product_submit", status: "approved" } },
        "POST /api/work-products/5/release": () => {
          throw new TypeError("Failed to fetch");
        },
      }),
    });
    await findPageHeading("Ship the Work area");
    const followUp = screen.getByRole("group", { name: "Release follow-up" });
    await waitFor(() => expect(within(followUp).getByRole("button", { name: "Continue release" })).toBeEnabled(), { timeout: 15_000 });
    fireEvent.click(within(followUp).getByRole("button", { name: "Continue release" }));
    expect(await within(followUp).findByText(/could not confirm whether work product #5 was released/)).toBeInTheDocument();
    expect(within(followUp).getByRole("button", { name: "Continue release" })).toBeDisabled();
    fireEvent.click(within(followUp).getByRole("button", { name: "It was released" }));
    await waitFor(() => expect(within(followUp).queryByText(/Work product #5/)).not.toBeInTheDocument());
    expect(posts(hub.calls, "/api/work-products/5/release")).toHaveLength(1);
  });

  it("attaches evidence to the ledger", async () => {
    const { hub } = renderRoute("/work/tasks/102", { hub: workHub({ "POST /api/evidence": { status: 201, body: { event_ulid: "01X", inserted: true } } }) });
    await findPageHeading("Ship the Work area");
    const form = screen.getByRole("form", { name: "Attach evidence" });
    fireEvent.change(within(form).getByLabelText(/Reference/), { target: { value: "https://ci/run/1" } });
    fireEvent.click(within(form).getByRole("button", { name: "Attach evidence" }));
    await waitFor(() => expect(posts(hub.calls, "/api/evidence")[0]?.body).toMatchObject({ channel: "sdk.task_evidence", payload: { task_id: 102, kind: "link", ref: "https://ci/run/1" } }));
  });
});

describe("work: create task", () => {
  it("creates a task and opens it", async () => {
    const { hub, router } = renderRoute("/work/tasks/new?parent=102", { hub: workHub({ "POST /api/tasks": { status: 201, body: { id: 120, title: "New one" } } }) });
    await findPageHeading("New task");
    await screen.findByLabelText(/^Title/);
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    expect(await screen.findByText("Give the task a title.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/^Title/), { target: { value: "New one" } });
    fireEvent.change(screen.getByLabelText("Priority"), { target: { value: "high" } });
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/ui/work/tasks/120"));
    expect(posts(hub.calls, "/api/tasks")[0].body).toMatchObject({ title: "New one", priority: "high", parent_id: 102 });
  });

  // Spec §12 defect 8: drafts lost on navigation.
  it("keeps the draft when the person leaves and comes back", async () => {
    renderRoute("/work/tasks/new", { hub: workHub() });
    await findPageHeading("New task");
    await screen.findByLabelText(/^Title/);
    fireEvent.change(screen.getByLabelText(/^Title/), { target: { value: "Half written" } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Some context" } });
    cleanup();
    vi.unstubAllGlobals();
    renderRoute("/work/tasks/new", { hub: workHub() });
    await findPageHeading("New task");
    await screen.findByLabelText(/^Title/);
    expect(screen.getByLabelText(/^Title/)).toHaveValue("Half written");
    expect(screen.getByLabelText("Description")).toHaveValue("Some context");
    expect(screen.getByText(/unsaved draft was restored/)).toBeInTheDocument();
  });

  // Review round 1, finding 8: a second click after a parked create must not make a second task.
  it("a resubmitted create reuses the draft's idempotency key", async () => {
    let calls = 0;
    const { hub } = renderRoute("/work/tasks/new", {
      hub: workHub({ "POST /api/tasks": () => (++calls === 1 ? { status: 202, body: { approval_id: 40, code: "approval_required", status: "pending", tool: "task_add" } } : { status: 201, body: { id: 121, title: "Gated" } }) }),
    });
    await findPageHeading("New task");
    fireEvent.change(await screen.findByLabelText(/^Title/), { target: { value: "Gated" } });
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    expect(await screen.findByText(/Waiting for approval/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    await waitFor(() => expect(posts(hub.calls, "/api/tasks")).toHaveLength(2));
    const [first, second] = posts(hub.calls, "/api/tasks").map((c) => (c.body as { idempotency_key: string }).idempotency_key);
    expect(first).toBeTruthy();
    expect(second).toBe(first);
  });

  it("a parked create keeps the form and says it waits for approval", async () => {
    const { router } = renderRoute("/work/tasks/new", { hub: workHub({ "POST /api/tasks": PARKED("task_add", 40) }) });
    await findPageHeading("New task");
    await screen.findByLabelText(/^Title/);
    fireEvent.change(screen.getByLabelText(/^Title/), { target: { value: "Gated" } });
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    expect(await screen.findByText(/Waiting for approval/)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/ui/work/tasks/new");
    expect(screen.getByLabelText(/^Title/)).toHaveValue("Gated");
  });
});
