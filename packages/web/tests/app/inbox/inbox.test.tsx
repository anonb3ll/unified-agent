import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { addParkedWrite, recordParkedWriteResult } from "../../../src/app/lib/parkedWrites";
import { draftKey, saveDraft } from "../../../src/app/lib/drafts";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { APPROVAL_ID, approvalDetail, emptyHub, approvalsPayload, attentionPayload, fullHub, memoryProposals, orgProposal, taskRow, type HubTable } from "./fixtures";
import { vitestHub } from "./support";
import { fixtureEpoch } from "../support/epoch";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

function render(path: string, table: HubTable = fullHub(), extra: Parameters<typeof vitestHub>[1] = {}) {
  return renderRoute(path, { hub: vitestHub(table, extra) });
}

/** Tick the standing "anything not shown would also be approved" acknowledgement (review 2.1). */
async function acknowledge() {
  fireEvent.click(await screen.findByLabelText(/I understand that anything not shown here would also be approved/, {}, { timeout: 8000 }));
}

const kindTabs = () => screen.getByRole("navigation", { name: "Kinds of item" });
const waitingList = () => screen.findByRole("list", { name: "Items waiting on you" }, { timeout: 8000 });

describe("Inbox list", () => {
  it("merges every source into one list with a count per kind from complete reads", async () => {
    render("/inbox");
    await findPageHeading("Inbox");
    const list = await waitingList();
    const titles = within(list).getAllByRole("link").map((a) => a.textContent ?? "");
    expect(titles.some((t) => t.includes("task_delete wants to run"))).toBe(true);
    expect(titles.some((t) => t.includes("waiting for your reply"))).toBe(true);
    expect(titles.some((t) => t.includes("Rotate hub tokens"))).toBe(true);
    expect(titles.some((t) => t.includes("Add a review team"))).toBe(true);
    expect(titles.some((t) => t.includes("Pin the Playwright port"))).toBe(true);
    expect(titles.some((t) => t.includes("Claim has not moved"))).toBe(true);
    // The approval appears once, though both the approvals queue and the typed inbox name it.
    expect(titles.filter((t) => t.includes("task_delete")).length).toBe(1);
    await waitFor(() => expect(within(kindTabs()).getByRole("link", { name: /^Approvals/ })).toHaveTextContent("2"));
    expect(within(kindTabs()).getByRole("link", { name: /^Replies/ })).toHaveTextContent("1");
    expect(within(kindTabs()).getByRole("link", { name: /^Tasks and holds/ })).toHaveTextContent("2");
    expect(within(kindTabs()).getByRole("link", { name: /^Proposals/ })).toHaveTextContent("2+");
    expect(within(kindTabs()).getByRole("link", { name: /^Alerts/ })).toHaveTextContent("1");
    expect(within(kindTabs()).getByRole("link", { name: /^All/ })).toHaveTextContent("8+");
  });

  it("says plainly that work proposals cannot be listed, and asks the hub for no such route (review 1.4)", async () => {
    const { hub } = render("/inbox?kind=proposal");
    await waitingList();
    expect(screen.getByText(/Work proposals cannot be listed on this hub yet/)).toBeInTheDocument();
    expect(hub.calls.some((c) => c.path.startsWith("/api/work-proposals"))).toBe(false);
  });

  it("a failed source shows – for the counts it feeds and says what is missing; the rest still shows", async () => {
    const table = fullHub();
    table["GET /api/proposals"] = { status: 503, body: { error: "vault read timed out" } };
    render("/inbox", table);
    await waitingList();
    await waitFor(() => expect(within(kindTabs()).getByRole("link", { name: /^Replies/ })).toHaveTextContent("1"));
    const notice = await screen.findByRole("region", { name: "Part of the Inbox could not be read" }, { timeout: 8000 });
    expect(within(kindTabs()).getByRole("link", { name: /^Proposals/ })).toHaveTextContent("–");
    expect(within(kindTabs()).getByRole("link", { name: /^All/ })).toHaveTextContent("–");
    expect(within(notice).getByText("Memory proposals")).toBeInTheDocument();
    expect(within(notice).getByText("The hub had a problem answering. Try again in a moment.")).toBeInTheDocument();
    expect(within(notice).getByRole("button", { name: "Retry Memory proposals" })).toBeInTheDocument();
  });

  it("a source at the hub's page limit shows a lower-bound count and says more may exist (review 1.10)", async () => {
    // approval_pending returns at most 100 pending rows; org proposals are read 200 per status.
    const pending = Array.from({ length: 100 }, (_, i) => ({ ...approvalsPayload.pending[0], id: 1000 + i }));
    const orgs = Array.from({ length: 200 }, (_, i) => ({ ...orgProposal, id: `orgprop-${i}`, status: "proposed" }));
    render("/inbox?kind=approval", {
      ...fullHub(),
      "GET /api/approvals": { body: { ...approvalsPayload, pending } },
      "GET /api/org-proposals": (url) => ({ body: { proposals: url.searchParams.get("status") === "proposed" ? orgs : [], count: 0, self_organization_enabled: true } }),
    });
    await waitingList();
    await waitFor(() => expect(within(kindTabs()).getByRole("link", { name: /^Approvals/ })).toHaveTextContent("101+"));
    expect(within(kindTabs()).getByRole("link", { name: /^Proposals/ })).toHaveTextContent("201+");
    expect(within(kindTabs()).getByRole("link", { name: /^All/ })).toHaveTextContent(/\d+\+/);
    expect(screen.getByText(/The hub returns at most 100 waiting approvals at a time/)).toBeInTheDocument();
  });

  it("a wait already listed as a hold is not listed again as an alert; other alerts about the task stay (review 1.14)", async () => {
    const items = [
      { kind: "human_merge_wait", severity: "high", source: { task_id: 77 }, source_subject: "task 77", title: "Rotate hub tokens", reason: "waiting for a human merge", age_s: 3600 },
      { kind: "stale_claim", severity: "medium", source: { task_id: 77, agent_label: "Desktop_Agent2" }, source_subject: "task 77", title: "Claim has not moved for 3 hours", reason: "No heartbeat since the claim", age_s: 10800 },
    ];
    render("/inbox", { ...fullHub(), "GET /api/attention": { body: attentionPayload({ items }) } });
    const list = await waitingList();
    const titles = within(list).getAllByRole("link").map((a) => a.textContent ?? "");
    expect(titles.filter((t) => t.includes("Rotate hub tokens"))).toHaveLength(1);
    expect(titles.some((t) => t.includes("Claim has not moved"))).toBe(true);
    await waitFor(() => expect(within(kindTabs()).getByRole("link", { name: /^Alerts/ })).toHaveTextContent("1"));
  });

  it("past the org-proposal cap, author and scope filters reach the rest, and the page says only 200 per state can be read (review 2.7)", async () => {
    const newest = Array.from({ length: 200 }, (_, i) => ({ ...orgProposal, id: `orgprop-${i}`, status: "proposed", author: "Desktop_Agent2" }));
    const older = { ...orgProposal, id: "orgprop-old", title: "Retire the night shift", status: "proposed", author: "Desktop_Agent9" };
    render("/inbox?kind=proposal", {
      ...fullHub(),
      "GET /api/org-proposals": (url) => {
        const author = url.searchParams.get("author");
        if (url.searchParams.get("status") !== "proposed") return { body: { proposals: [], count: 0, self_organization_enabled: true } };
        return { body: { proposals: author === "Desktop_Agent9" ? [older] : newest, count: 0, self_organization_enabled: true } };
      },
    });
    await waitingList();
    expect(await screen.findByText(/Only the newest 200 organization proposals in each state can be read here/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Proposals by author"), { target: { value: "Desktop_Agent9" } });
    expect(await within(await waitingList()).findByText("Retire the night shift", {}, { timeout: 8000 })).toBeInTheDocument();
  });

  it("All and Proposals are qualified because work proposals cannot be listed (review 2.9)", async () => {
    render("/inbox", emptyHub());
    expect(await screen.findByText(/Work proposals cannot be listed on this hub yet, so this may not be everything/, {}, { timeout: 8000 })).toBeInTheDocument();
    await waitFor(() => expect(within(kindTabs()).getByRole("link", { name: /^All/ })).toHaveTextContent("0+"));
    expect(within(kindTabs()).getByRole("link", { name: /^Proposals/ })).toHaveTextContent("0+");
    expect(within(kindTabs()).getByRole("link", { name: /^Replies/ })).toHaveTextContent(/^Replies\s*0$/);
  });

  it("never shows 0 while loading, and an error with Retry when every read fails", async () => {
    const fail = { status: 503, body: { error: "sqlite busy: database is locked" } };
    render("/inbox", {
      "GET /api/attention": fail,
      "GET /api/approvals": fail,
      "GET /api/proposals": fail,
      "GET /api/org-proposals": fail,
    });
    await findPageHeading("Inbox");
    expect(within(kindTabs()).getByRole("link", { name: /^All/ })).toHaveTextContent("–");
    expect(within(kindTabs()).queryByText("0")).toBeNull();
    const error = await screen.findByRole("heading", { name: /The Inbox could not be loaded/ }, { timeout: 8000 });
    const region = error.closest("section") as HTMLElement;
    expect(within(region).getByText("The hub had a problem answering. Try again in a moment.")).toBeInTheDocument();
    // Raw transport text lives only in the collapsed detail (defect 10).
    expect(within(region).getByText("Technical detail")).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("filters by kind from the URL and keeps the selection in the URL", async () => {
    const { router } = render("/inbox?kind=hold");
    const list = await waitingList();
    expect(within(list).getAllByRole("link")).toHaveLength(2);
    expect(within(kindTabs()).getByRole("link", { name: /^Tasks and holds/ })).toHaveAttribute("aria-current", "page");
    fireEvent.click(within(list).getByRole("link", { name: /Rotate hub tokens/ }));
    await waitFor(() => expect(router.state.location.search).toBe("?kind=hold&item=red_tier_hold%3A77"));
    expect(await screen.findByRole("heading", { level: 2, name: "Rotate hub tokens" })).toBeInTheDocument();
  });

  it("j and k move the selection; Enter on a row opens its detail", async () => {
    const { router } = render("/inbox?kind=hold");
    const list = await waitingList();
    fireEvent.keyDown(document.body, { key: "j" });
    await waitFor(() => expect(router.state.location.search).toContain("item=red_tier_hold%3A77"));
    fireEvent.keyDown(document.body, { key: "j" });
    await waitFor(() => expect(router.state.location.search).toContain("item=operator_task%3A78"));
    expect(within(list).getByRole("link", { name: /Renew the domain/ })).toHaveFocus();
    fireEvent.keyDown(document.body, { key: "k" });
    await waitFor(() => expect(router.state.location.search).toContain("item=red_tier_hold%3A77"));
    // Enter activates the focused link: the detail heading takes focus.
    fireEvent.click(within(list).getByRole("link", { name: /Rotate hub tokens/ }));
    const heading = await screen.findByRole("heading", { level: 2, name: "Rotate hub tokens" });
    await waitFor(() => expect(heading).toHaveFocus());
  });
});

describe("approvals", () => {
  it("shows what will run (tool, arguments, requester) before Approve is offered, then the result", async () => {
    let status = "pending";
    const { hub } = render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      [`GET /api/approvals/${APPROVAL_ID}`]: () => ({ body: approvalDetail(status) }),
      "POST /api/action/approval": () => {
        status = "approved";
        return { body: { id: APPROVAL_ID, status: "approved" } };
      },
    });
    const section = (await screen.findByRole("heading", { name: "What will run" }, { timeout: 8000 })).closest("section") as HTMLElement;
    expect(within(section).getByText("task_delete")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Desktop_Agent2" })).toHaveAttribute("href", "/ui/agents/profile/Desktop_Agent2");
    const args = within(section).getByRole("list", { name: "Arguments summary" });
    expect(args).toHaveTextContent("task_id: 42");
    expect(args).toHaveTextContent("reason: duplicate of task 41");
    await acknowledge();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(hub.calls.some((c) => c.method === "POST" && c.path === "/api/action/approval")).toBe(true));
    const post = hub.calls.find((c) => c.path === "/api/action/approval");
    expect(post?.body).toEqual({ id: APPROVAL_ID, decision: "approve" });
    expect(post?.headers["x-ua-csrf"]).toBe("csrf-1");
    expect(await screen.findByText(/Approved, not used yet\. The requester may repeat exactly this call once/)).toBeInTheDocument();
  });

  it("every approval needs the standing acknowledgement before Approve, since the hub does not say when it shortened the preview (review 2.1)", async () => {
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`);
    const notice = await screen.findByRole("group", { name: /Before you approve/ }, { timeout: 8000 });
    expect(notice).toHaveTextContent(
      "The hub may shorten what it shows here (long lists, nested fields and large arguments), and does not yet say when it has. Anything not shown would also be approved.",
    );
    expect(notice).not.toHaveTextContent("This preview looks shortened");
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toBeDisabled();
    fireEvent.click(within(notice).getByLabelText(/I understand that anything not shown here would also be approved/));
    expect(approve).toBeEnabled();
  });

  it("a mapping of ten fields inside an argument is flagged as looking shortened (review 2.1)", async () => {
    const changes = Object.fromEntries(Array.from({ length: 10 }, (_, i) => [`field_${i}`, i]));
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      [`GET /api/approvals/${APPROVAL_ID}`]: { body: approvalDetail("pending", { payload: { task_id: 42, changes } }) },
    });
    const notice = await screen.findByRole("group", { name: /Before you approve/ }, { timeout: 8000 });
    expect(notice).toHaveTextContent("This preview looks shortened");
    expect(notice).toHaveTextContent("changes shows 10 fields");
  });

  it("a read that says its preview is complete needs no acknowledgement (review 2.1)", async () => {
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      [`GET /api/approvals/${APPROVAL_ID}`]: { body: approvalDetail("pending", { preview_complete: true }) },
    });
    expect(await screen.findByRole("button", { name: "Approve" }, { timeout: 8000 })).toBeEnabled();
    expect(screen.queryByRole("group", { name: /Before you approve/ })).toBeNull();
  });

  it("the acknowledgement is reset when the preview changes, and checked again at submit (review 2.2)", async () => {
    let reason = "duplicate of task 41";
    const { hub, appClient } = render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      [`GET /api/approvals/${APPROVAL_ID}`]: () => ({ body: approvalDetail("pending", { payload: { task_id: 42, reason } }) }),
      "POST /api/action/approval": { body: { id: APPROVAL_ID, status: "approved" } },
    });
    await acknowledge();
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toBeEnabled();
    reason = "delete everything under task 42";
    await appClient().refetchQueries({ queryKey: ["approval-detail", APPROVAL_ID] });
    expect(await screen.findByText("The request changed; review it again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByLabelText(/I understand that anything not shown here would also be approved/)).not.toBeChecked();
    expect(hub.calls.some((c) => c.path === "/api/action/approval")).toBe(false);
  });

  it("sweep (a): the approval's acknowledgement and decision state do not follow the item to another identity", async () => {
    const { queryClient } = render(`/inbox?kind=approval&item=${APPROVAL_ID}`);
    await acknowledge();
    fireEvent.click(screen.getByRole("button", { name: "Deny…" }));
    expect(screen.getByLabelText("Reason for denying")).toBeInTheDocument();
    queryClient.setQueryData(["whoami"], { ...whoamiFixture(), agent: "Desktop_Person" });
    await waitFor(() => expect(screen.queryByLabelText("Reason for denying")).toBeNull());
    expect(screen.getByLabelText(/I understand that anything not shown here would also be approved/)).not.toBeChecked();
  });

  it("no Approve button until the preview has loaded", async () => {
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      [`GET /api/approvals/${APPROVAL_ID}`]: () => ({ status: 503, body: { error: "busy" } }),
    });
    await screen.findByRole("heading", { name: "The approval could not be loaded" }, { timeout: 8000 });
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("deny takes a reason", async () => {
    const { hub } = render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      "POST /api/action/approval": { body: { id: APPROVAL_ID, status: "denied" } },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Deny…" }, { timeout: 8000 }));
    fireEvent.change(screen.getByLabelText("Reason for denying"), { target: { value: "Task 42 is still needed" } });
    fireEvent.click(screen.getByRole("button", { name: "Deny this call" }));
    await waitFor(() => expect(hub.calls.find((c) => c.path === "/api/action/approval")?.body).toEqual({ id: APPROVAL_ID, decision: "deny", reason: "Task 42 is still needed" }));
  });

  it("a decided approval stays readable after it leaves the waiting list", async () => {
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, { ...fullHub(), "GET /api/approvals": { body: { pending: [], approved_unconsumed: [], expired_passes: [] } } }, {
      [`GET /api/approvals/${APPROVAL_ID}`]: { body: approvalDetail("consumed") },
    });
    // The gate consumes the pass before the call runs: "used", never "the call has run" (review 1.9).
    const result = await screen.findByText(/The approval was used: the call was let through\./, {}, { timeout: 8000 });
    expect(result).toHaveTextContent("Whether the call finished is not recorded on the approval");
    expect(screen.queryByText(/has run/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("an approved pass from the expired bucket says it expired unused, not that the call may repeat (review 1.8)", async () => {
    const expiredRow = { id: 9, tool: "vault_write", requested_by: "Desktop_Agent5", status: "approved", created_at: "2026-11-09T08:00:00+00:00", expires_at: "2026-11-09T20:00:00+00:00", expires_in_hours: 0, expired: true };
    render("/inbox?kind=approval&item=9", { ...fullHub(), "GET /api/approvals": { body: { pending: [], approved_unconsumed: [], expired_passes: [expiredRow], expired_passes_total: 1 } } }, {
      "GET /api/approvals/9": { body: approvalDetail("approved", { id: 9, tool: "vault_write", requested_by: "Desktop_Agent5" }) },
    });
    expect(await screen.findByText(/expired before it was used/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByText(/may (now )?repeat/)).toBeNull();
  });

  it("an approved pass opened from a direct link does not claim it is still valid (review 1.8)", async () => {
    render("/inbox?kind=approval&item=9", { ...fullHub(), "GET /api/approvals": { body: { pending: [], approved_unconsumed: [], expired_passes: [] } } }, {
      "GET /api/approvals/9": { body: approvalDetail("approved", { id: 9 }) },
    });
    expect(await screen.findByText(/This page cannot tell whether the pass is still valid/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByText(/may (now )?repeat/)).toBeNull();
  });

  function park(organization: string | null, extra: Record<string, unknown> = {}) {
    // Stored under the owner the page will be signed in as (organization included).
    return addParkedWrite(fixtureEpoch(organization ? { ...whoamiFixture(), org: organization } : whoamiFixture()), {
      path: "/api/tasks",
      method: "POST",
      body: { title: "Ship it" },
      approvalId: String(APPROVAL_ID),
      label: "Create task",
      context: { hubOrigin: window.location.origin, organization, identity: whoamiFixture().agent ?? null },
      invalidates: [],
      formHref: "/work/tasks/new",
      ...extra,
    });
  }
  const parkedPanel = async () =>
    (await screen.findByRole("heading", { name: "Your change that waits on this" }, { timeout: 8000 })).closest("section") as HTMLElement;
  const withOrg = { "GET /api/whoami": { body: { ...whoamiFixture(), org: "org-1" } } };

  // The operator's own request: the hub lets only a different person approve it (approvals.approval_resolve).
  const mine = (status = "pending") => approvalDetail(status, { requested_by: whoamiFixture().agent });

  it("your own request offers no Approve, says why, and still lets you withdraw it by denying", async () => {
    park("org-1");
    const { hub } = render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      ...withOrg,
      [`GET /api/approvals/${APPROVAL_ID}`]: { body: mine() },
      "POST /api/action/approval": { body: { id: APPROVAL_ID, status: "denied" } },
    });
    expect(await screen.findByText(/You asked for this, so someone else has to approve it/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    const panel = await parkedPanel();
    // The shared notice (foundation round 3) says a different person must approve it.
    await waitFor(() => expect(panel).toHaveTextContent("This needs approval from a different person."));
    fireEvent.click(screen.getByRole("button", { name: "Withdraw (deny)…" }));
    fireEvent.click(screen.getByRole("button", { name: "Deny this call" }));
    await waitFor(() => expect(hub.calls.find((c) => c.path === "/api/action/approval")?.body).toEqual({ id: APPROVAL_ID, decision: "deny" }));
  });

  it("a stored write is not shown on someone else's request", async () => {
    // Made by another identity in this tab: the foundation lists stored writes only for their owner.
    park("org-1", { context: { hubOrigin: window.location.origin, organization: "org-1", identity: "Desktop_Agent5" } });
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), withOrg);
    expect(await screen.findByRole("button", { name: "Approve" }, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Your change that waits on this" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Approve and run" })).toBeNull();
  });

  it("a request attributed to another of your aliases is still yours when your stored write waits on it (review 2.3)", async () => {
    park("org-1");
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      ...withOrg,
      // The hub treats configured aliases as one person; the page cannot see the alias list.
      [`GET /api/approvals/${APPROVAL_ID}`]: { body: approvalDetail("pending", { requested_by: "Operator" }) },
    });
    expect(await screen.findByText(/You asked for this, so someone else has to approve it/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    expect(await parkedPanel()).toBeInTheDocument();
  });

  it("a requester with a different label is unknown, not 'someone else': Approve stays, with the reason (review 2.3)", async () => {
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`);
    expect(await screen.findByText(/This page cannot confirm that someone other than you asked for this/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("an approval with no requester keeps Approve but says who asked is unknown (review 2.3)", async () => {
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      [`GET /api/approvals/${APPROVAL_ID}`]: { body: approvalDetail("pending", { requested_by: null }) },
    });
    expect(await screen.findByText(/Who asked for this is not known/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("after a separation-of-duties refusal the item stays yours for the session (review 2.3)", async () => {
    const { router } = render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      "POST /api/action/approval": {
        status: 400,
        body: { error: "approval request 12 (task_delete) was requested by 'Operator'; the approver must be a different person (separation of duties)" },
      },
    });
    await acknowledge();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/The hub does not let anyone approve a request they made/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).toBeNull());
    await router.navigate("/inbox?kind=hold&item=red_tier_hold%3A77");
    await screen.findByRole("heading", { level: 2, name: "Rotate hub tokens" }, { timeout: 8000 });
    await router.navigate(`/inbox?kind=approval&item=${APPROVAL_ID}`);
    expect(await screen.findByText(/You asked for this, so someone else has to approve it/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
  });

  it("the hub's separation-of-duties refusal is explained plainly", async () => {
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      "POST /api/action/approval": {
        status: 400,
        body: { error: "approval request 12 (task_delete) was requested by 'Desktop_Person'; the approver must be a different person (separation of duties)" },
      },
    });
    await acknowledge();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/The hub does not let anyone approve a request they made/)).toBeInTheDocument();
    expect(screen.queryByText(/The hub did not accept this/)).toBeNull();
  });

  it("an approve whose answer was lost re-reads the approval before saying anything", async () => {
    let reads = 0;
    let posted = false;
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      [`GET /api/approvals/${APPROVAL_ID}`]: () => {
        reads += 1;
        return { body: approvalDetail(posted ? "approved" : "pending") };
      },
      "POST /api/action/approval": () => {
        posted = true;
        throw new TypeError("network connection was lost");
      },
    });
    await acknowledge();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    const before = reads;
    expect(await screen.findByText(/The answer to your decision was lost/)).toBeInTheDocument();
    expect(reads).toBeGreaterThan(before - 1);
    expect(await screen.findByText(/Approved, not used yet/)).toBeInTheDocument();
  });

  it("after someone else approved, the stored change offers Run it now", async () => {
    park("org-1");
    const { hub } = render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      ...withOrg,
      [`GET /api/approvals/${APPROVAL_ID}`]: { body: mine("approved") },
      "POST /api/tasks": { body: taskRow(900, "Ship it") },
    });
    const panel = await parkedPanel();
    fireEvent.click(await within(panel).findByRole("button", { name: "Run it now" }, { timeout: 8000 }));
    await waitFor(() => expect(hub.calls.filter((c) => c.path === "/api/tasks")).toHaveLength(1));
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
  });

  it("a stored change that cannot run from the store offers its form, never a run (no organization)", async () => {
    park(null);
    // Approved by someone else; this hub reports no organization, so the store cannot run it.
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), { [`GET /api/approvals/${APPROVAL_ID}`]: { body: mine("approved") } });
    const panel = await parkedPanel();
    expect(await within(panel).findByRole("link", { name: "Open the form to run it" }, { timeout: 8000 })).toHaveAttribute("href", "/ui/work/tasks/new");
    expect(within(panel).queryByRole("button", { name: "Run it now" })).toBeNull();
  });

  it("a stored write on an expired pass is never offered Run it now (review 2.4)", async () => {
    park("org-1");
    const expiredRow = { ...approvalsPayload.pending[0], requested_by: whoamiFixture().agent, status: "approved", expires_at: "2026-11-09T20:00:00+00:00", expires_in_hours: 0, expired: true };
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, { ...fullHub(), "GET /api/approvals": { body: { pending: [], approved_unconsumed: [], expired_passes: [expiredRow], expired_passes_total: 1 } } }, {
      ...withOrg,
      // The detail read still says "approved": only the list knows the pass expired.
      [`GET /api/approvals/${APPROVAL_ID}`]: { body: mine("approved") },
    });
    const panel = await parkedPanel();
    await waitFor(() => expect(panel).toHaveTextContent("the approval expired before it was used"));
    expect(within(panel).queryByRole("button", { name: "Run it now" })).toBeNull();
    expect(within(panel).getByRole("link", { name: "Open the form to ask again" })).toHaveAttribute("href", "/ui/work/tasks/new");
    expect(within(panel).getByRole("button", { name: "Discard" })).toBeInTheDocument();
  });

  it("an outcome-unknown stored change is shown through the shared notice and cannot run until confirmed", async () => {
    park("org-1", { outcome: "unknown" });
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), { ...withOrg, [`GET /api/approvals/${APPROVAL_ID}`]: { body: mine("approved") } });
    const panel = await parkedPanel();
    await waitFor(() => expect(panel).toHaveTextContent("Outcome unknown"));
    expect(within(panel).queryByRole("button", { name: "Run it now" })).toBeNull();
    // The foundation's notice: "It is there" closes it; sending again needs its own confirmation.
    expect(within(panel).getByRole("button", { name: "It is there" })).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "It is not there, send it again" }));
    expect(await screen.findByRole("dialog", { name: "Send it again?" })).toBeInTheDocument();
  });

  it("a denied approval's stored change says so through the shared notice", async () => {
    park("org-1");
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), { ...withOrg, [`GET /api/approvals/${APPROVAL_ID}`]: { body: mine("denied") } });
    const panel = await parkedPanel();
    await waitFor(() => expect(panel).toHaveTextContent("Create task was denied, so it did not run."));
  });

  it("the result of a stored change stays on the item after it ran", async () => {
    render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), { [`GET /api/approvals/${APPROVAL_ID}`]: { body: mine("consumed") } });
    recordParkedWriteResult(fixtureEpoch(), {
      approvalId: String(APPROVAL_ID),
      kind: "done",
      message: "Create task: done.",
      at: Date.now(),
      // Results are read back only by the identity that made the write.
      owner: { hubOrigin: window.location.origin, organization: null, identity: whoamiFixture().agent ?? null },
    });
    const panel = await parkedPanel();
    expect(await within(panel).findByText("Create task: done.")).toBeInTheDocument();
  });
});

describe("inline actions", () => {
  it("replies to an operator question (plain text body, reply_to and recipient set)", async () => {
    const { hub } = render("/inbox?kind=message&item=reply%3Amsg-1", fullHub(), {
      "POST /api/messages": { body: { accepted: true, message_id: "x", conversation_id: "conv-1" } },
    });
    const asks = (await screen.findByRole("heading", { name: "Desktop_Agent2 asks" }, { timeout: 8000 })).closest("section") as HTMLElement;
    // Agent text is never rendered as HTML.
    expect(asks).toHaveTextContent("Should I ship <b>now</b>?");
    expect(asks.querySelector("b")).toBeNull();
    fireEvent.change(screen.getByLabelText("Your reply to Desktop_Agent2"), { target: { value: "Yes, ship it." } });
    fireEvent.click(screen.getByRole("button", { name: "Send reply" }));
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/messages")).toBe(true));
    const body = hub.calls.find((c) => c.path === "/api/messages")?.body as Record<string, unknown>;
    expect(body).toMatchObject({ act: "inform", body: "Yes, ship it.", to_agent: "Desktop_Agent2", reply_to: "msg-1", expects_reply: false });
    // No explicit session: an ended asking session would make the hub refuse it (review 1.7).
    expect(body).not.toHaveProperty("to_session");
    expect(typeof body.message_id).toBe("string");
    expect(await screen.findByText("Sent.")).toBeInTheDocument();
  });

  it("a reply parked behind an approval says so and links it, never success (defect 1)", async () => {
    render("/inbox?kind=message&item=reply%3Amsg-1", fullHub(), {
      "POST /api/messages": { status: 202, body: { status: "pending", approval_id: 31, code: "approval_required" } },
      "GET /api/approvals/31": { body: approvalDetail("pending", { id: 31, tool: "message_send", payload: { to_agent: "Desktop_Agent2" } }) },
    });
    fireEvent.change(await screen.findByLabelText("Your reply to Desktop_Agent2", {}, { timeout: 8000 }), { target: { value: "Hold on." } });
    fireEvent.click(screen.getByRole("button", { name: "Send reply" }));
    const notice = await screen.findByRole("region", { name: "Approval status" });
    expect(notice).toHaveTextContent("Waiting for approval.");
    expect(within(notice).getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=31");
    expect(screen.queryByText(/Sent\./)).toBeNull();
    // This hub reports no organization, so the store cannot run it: the form carries the identical request.
    expect(notice).toHaveTextContent("cannot be run from here yet");
    expect(screen.getByLabelText("Your reply to Desktop_Agent2")).toHaveValue("Hold on.");
  });

  it("Refuse is shown disabled with a plain reason, since the hub cannot decline a wake yet (review 1.17)", async () => {
    const { hub } = render("/inbox?kind=approval&item=wake%3Asess-1");
    const refuse = await screen.findByRole("button", { name: "Refuse" }, { timeout: 8000 });
    expect(refuse).toBeDisabled();
    expect(refuse).toHaveAccessibleDescription(/not available on this hub yet/i);
    expect(screen.getByText(/An ungranted request simply stays/)).toBeInTheDocument();
    expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("grants a wake", async () => {
    const { hub } = render("/inbox?kind=approval&item=wake%3Asess-1", fullHub(), {
      "POST /api/sessions/sess-1/wake-grant": { body: { session_id: "sess-1", granted: true, requested: true, changed: true } },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Grant wakes" }, { timeout: 8000 }));
    await waitFor(() => expect(hub.calls.find((c) => c.path === "/api/sessions/sess-1/wake-grant")?.body).toEqual({ grant: true }));
    expect(await screen.findByText("Wakes granted for this session.")).toBeInTheDocument();
  });

  it("a hold offers what the hub can do: a note for the merge actor, with no promise of a release (review 1.2)", async () => {
    const { hub } = render("/inbox?kind=hold&item=red_tier_hold%3A77", fullHub(), {
      "POST /api/action/task": { body: taskRow(77, "Rotate hub tokens") },
    });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    await within(main).findByRole("heading", { level: 2, name: "Rotate hub tokens" }, { timeout: 8000 });
    expect(within(main).queryByRole("button", { name: /Release|give the go/i })).toBeNull();
    expect(main).toHaveTextContent("This hub cannot release a hold yet");
    fireEvent.change(within(main).getByLabelText("Note for the merge actor"), { target: { value: "Checked the token rotation; fine to merge." } });
    fireEvent.click(within(main).getByRole("button", { name: "Add a note for the merge actor" }));
    await waitFor(() => expect(hub.calls.filter((c) => c.path === "/api/action/task")).toHaveLength(1));
    const body = hub.calls.find((c) => c.path === "/api/action/task")?.body as { id: number; append_note: string };
    expect(body.id).toBe(77);
    expect(body.append_note).toMatch(/^\[operator-note @ .+\] Checked the token rotation; fine to merge\.$/);
    expect(await within(main).findByText(/Note added to task 77/)).toBeInTheDocument();
    expect(main).toHaveTextContent("The hold stays in the Inbox");
    expect(main).not.toHaveTextContent(/merges it next|will merge|released/i);
    // The item stays listed: the hub still reports the hold.
    expect(screen.getByRole("list", { name: "Items waiting on you" })).toHaveTextContent("Rotate hub tokens");
  });

  it("a parked task note is sent again with the identical body after approval (review 1.3)", async () => {
    let n = 0;
    const stamps = vi.spyOn(Date.prototype, "toISOString").mockImplementation(() => `2026-11-10T12:00:0${n++ % 10}.000Z`);
    let parked = true;
    const { hub } = render("/inbox?kind=hold&item=red_tier_hold%3A77", fullHub(), {
      "POST /api/action/task": () =>
        parked
          ? { status: 202, body: { approval_id: 41, status: "pending", code: "approval_required", tool: "task_update" } }
          : { body: taskRow(77, "Rotate hub tokens") },
    });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    fireEvent.change(await within(main).findByLabelText("Note for the merge actor", {}, { timeout: 8000 }), { target: { value: "Fine to merge." } });
    fireEvent.click(within(main).getByRole("button", { name: "Add a note for the merge actor" }));
    expect(await within(main).findByText(/Waiting for approval/)).toBeInTheDocument();
    parked = false;
    fireEvent.click(within(main).getByRole("button", { name: "Add a note for the merge actor" }));
    await waitFor(() => expect(hub.calls.filter((c) => c.path === "/api/action/task")).toHaveLength(2));
    const [first, second] = hub.calls.filter((c) => c.path === "/api/action/task").map((c) => c.body);
    expect(second).toEqual(first);
    stamps.mockRestore();
  });

  /** A request kept in the hold's draft from the other button (it parked, or its answer was lost). */
  function keepInHoldDraft(kind: "operator_task" | "red_tier_hold", taskId: number, pending: Record<string, unknown>) {
    const key = draftKey({ identity: whoamiFixture().agent, operation: "inbox.hold", object: { kind, task: taskId } });
    expect(saveDraft(fixtureEpoch(), key, { form: { note: "" }, pending, approvalId: null })).toBe(true);
  }

  it("a kept note is never sent when the person marks the task done (CTO review: one draft, two actions)", async () => {
    keepInHoldDraft("operator_task", 78, { id: 78, append_note: "[operator-note @ 2026-11-10T12:00:00.000Z] An earlier note" });
    const { hub } = render("/inbox?kind=hold&item=operator_task%3A78", fullHub(), {
      "POST /api/action/task": { body: taskRow(78, "Renew the domain") },
    });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    fireEvent.click(await within(main).findByRole("button", { name: "Mark done…" }, { timeout: 8000 }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Mark this task done?" })).getByRole("button", { name: "Mark done" }));
    await waitFor(() => expect(hub.calls.filter((c) => c.path === "/api/action/task")).toHaveLength(1));
    const body = hub.calls.find((c) => c.path === "/api/action/task")?.body as { id: number; status?: string; append_note: string };
    expect(body.status).toBe("done");
    expect(body.append_note).toMatch(/^\[operator-done @ /);
    expect(body.append_note).not.toContain("An earlier note");
  });

  it("a kept close is never sent when the person only adds a note (CTO review: one draft, two actions)", async () => {
    keepInHoldDraft("red_tier_hold", 77, { id: 77, status: "done", append_note: "[operator-done @ 2026-11-10T12:00:00.000Z] Closed by the operator from the Inbox." });
    const { hub } = render("/inbox?kind=hold&item=red_tier_hold%3A77", fullHub(), {
      "POST /api/action/task": { body: taskRow(77, "Rotate hub tokens") },
    });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    // Pressed with nothing typed: the kept close is not this button's request, so nothing is sent.
    fireEvent.click(await within(main).findByRole("button", { name: "Add a note for the merge actor" }, { timeout: 8000 }));
    expect(await within(main).findByText("Write the note first.")).toBeInTheDocument();
    expect(hub.calls.filter((c) => c.path === "/api/action/task")).toHaveLength(0);
    fireEvent.change(within(main).getByLabelText("Note for the merge actor"), { target: { value: "Fine to merge." } });
    fireEvent.click(within(main).getByRole("button", { name: "Add a note for the merge actor" }));
    await waitFor(() => expect(hub.calls.filter((c) => c.path === "/api/action/task")).toHaveLength(1));
    const body = hub.calls.find((c) => c.path === "/api/action/task")?.body as { id: number; status?: string; append_note: string };
    expect(body).not.toHaveProperty("status");
    expect(body.append_note).toMatch(/^\[operator-note @ .+\] Fine to merge\.$/);
  });

  it("the hold note survives leaving the item and coming back (review 1.16)", async () => {
    const { router } = render("/inbox?kind=hold&item=red_tier_hold%3A77");
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    fireEvent.change(await within(main).findByLabelText("Note for the merge actor", {}, { timeout: 8000 }), { target: { value: "Half-written note" } });
    await router.navigate("/inbox?kind=hold&item=operator_task%3A78");
    await within(main).findByRole("heading", { level: 2, name: "Renew the domain" }, { timeout: 8000 });
    await router.navigate("/inbox?kind=hold&item=red_tier_hold%3A77");
    expect(await within(main).findByDisplayValue("Half-written note", {}, { timeout: 8000 })).toBeInTheDocument();
  });

  it("org proposal decisions are disabled with a plain reason, and nothing is posted (review 1.4)", async () => {
    const { hub } = render("/inbox?kind=proposal&item=org%3Aorgprop-1");
    expect(await screen.findByRole("heading", { name: "Proposed changes" }, { timeout: 8000 })).toBeInTheDocument();
    for (const name of ["Apply", "Reject"]) expect(screen.getByRole("button", { name })).toBeDisabled();
    expect(screen.getAllByText(/Not available on this hub yet/).length).toBeGreaterThan(0);
    expect(hub.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("the memory proposal reason survives leaving the item and coming back (review 1.16)", async () => {
    const { router } = render("/inbox?kind=proposal&item=memory%3Aprop-1");
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    fireEvent.change(await within(main).findByLabelText("Reason", {}, { timeout: 8000 }), { target: { value: "Duplicate of an existing note" } });
    await router.navigate("/inbox?kind=proposal&item=org%3Aorgprop-1");
    await within(main).findByRole("heading", { level: 2, name: "Add a review team" }, { timeout: 8000 });
    await router.navigate("/inbox?kind=proposal&item=memory%3Aprop-1");
    expect(await within(main).findByDisplayValue("Duplicate of an existing note", {}, { timeout: 8000 })).toBeInTheDocument();
  });

  it("a deep link waits for its own source, not for the first source to finish (review 1.15)", async () => {
    render("/inbox?kind=proposal&item=memory%3Aprop-1", fullHub(), { "GET /api/proposals": () => new Promise<never>(() => {}) });
    await waitingList();
    const main = screen.getByRole("region", { name: "Selected item" });
    expect(within(main).getByRole("status")).toHaveTextContent("Loading the item");
    expect(within(main).queryByText("This item is no longer waiting")).toBeNull();
  });

  it("a deep link whose source failed says so instead of calling the item resolved (review 1.15)", async () => {
    render("/inbox?kind=proposal&item=memory%3Aprop-1", { ...fullHub(), "GET /api/proposals": { status: 503, body: { error: "vault read timed out" } } });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    expect(await within(main).findByRole("heading", { name: "This item could not be loaded" }, { timeout: 8000 })).toBeInTheDocument();
    expect(within(main).queryByText("This item is no longer waiting")).toBeNull();
  });

  it("a deep link is not called resolved when its kind could not be read completely (review 2.5)", async () => {
    render("/inbox?kind=hold&item=red_tier_hold%3A99", {
      ...fullHub(),
      "GET /api/attention": { body: attentionPayload({ needsErrors: ["red_tier_hold: could not be read (OperationalError)"] }) },
    });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    expect(await within(main).findByText(/We could not check whether this is still waiting/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(within(main).queryByText("This item is no longer waiting")).toBeNull();
  });

  it("a deep link beyond the org proposals the hub returned is not called resolved (review 2.5)", async () => {
    const orgs = Array.from({ length: 200 }, (_, i) => ({ ...orgProposal, id: `orgprop-${i}`, status: "proposed" }));
    render("/inbox?kind=proposal&item=org%3Aorgprop-999", {
      ...fullHub(),
      "GET /api/org-proposals": (url) => ({ body: { proposals: url.searchParams.get("status") === "proposed" ? orgs : [], count: 200, self_organization_enabled: true } }),
    });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    expect(await within(main).findByText(/We could not check whether this is still waiting/, {}, { timeout: 8000 })).toBeInTheDocument();
  });

  it("an item resolved by its action keeps its result on screen and moves focus to its heading (review 1.15)", async () => {
    let filed = false;
    render("/inbox?kind=proposal&item=memory%3Aprop-1", fullHub(), {
      "GET /api/proposals": () => ({ body: filed ? { proposals: [] } : memoryProposals }),
      "POST /api/action/proposal": () => {
        filed = true;
        return { body: { task_id: 600 } };
      },
    });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    fireEvent.click(await within(main).findByRole("button", { name: "File as a task" }, { timeout: 8000 }));
    expect(await within(main).findByText(/This item is no longer waiting/)).toBeInTheDocument();
    expect(within(main).getByText("Filed as task 600.")).toBeInTheDocument();
    await waitFor(() => expect(within(main).getByRole("heading", { level: 2, name: "Pin the Playwright port per worktree" })).toHaveFocus());
  });

  it("sweep (c): a memory proposal decision left mid-request shows outcome unknown on return", async () => {
    const { router } = render("/inbox?kind=proposal&item=memory%3Aprop-1", fullHub(), {
      "POST /api/action/proposal": () => new Promise<never>(() => {}),
    });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    fireEvent.click(await within(main).findByRole("button", { name: "File as a task" }, { timeout: 8000 }));
    await router.navigate("/inbox?kind=proposal&item=org%3Aorgprop-1");
    await within(main).findByRole("heading", { level: 2, name: "Add a review team" }, { timeout: 8000 });
    await router.navigate("/inbox?kind=proposal&item=memory%3Aprop-1");
    expect(await within(main).findByText(/Outcome unknown/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(within(main).queryByRole("button", { name: "File as a task" })).toBeNull();
    expect(within(main).getByRole("button", { name: "It is there" })).toBeInTheDocument();
  });

  it("sweep (c): an approval decision left mid-request says its answer did not arrive, with the state read again", async () => {
    const { router } = render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      "POST /api/action/approval": () => new Promise<never>(() => {}),
    });
    await acknowledge();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await router.navigate("/inbox?kind=hold&item=red_tier_hold%3A77");
    await screen.findByRole("heading", { level: 2, name: "Rotate hub tokens" }, { timeout: 8000 });
    await router.navigate(`/inbox?kind=approval&item=${APPROVAL_ID}`);
    expect(await screen.findByText(/Your decision was sent, but its answer did not arrive/, {}, { timeout: 8000 })).toBeInTheDocument();
  });

  it("sweep: an open Deny is withdrawn when a refetch changes the request underneath it", async () => {
    let reason = "duplicate of task 41";
    const { hub, appClient } = render(`/inbox?kind=approval&item=${APPROVAL_ID}`, fullHub(), {
      [`GET /api/approvals/${APPROVAL_ID}`]: () => ({ body: approvalDetail("pending", { payload: { task_id: 42, reason } }) }),
      "POST /api/action/approval": { body: { id: APPROVAL_ID, status: "denied" } },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Deny…" }, { timeout: 8000 }));
    expect(screen.getByRole("button", { name: "Deny this call" })).toBeInTheDocument();
    reason = "delete everything under task 42";
    await appClient().refetchQueries({ queryKey: ["approval-detail", APPROVAL_ID] });
    expect(await screen.findByText("The request changed while you were deciding; review it again.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deny this call" })).toBeNull();
    expect(hub.calls.some((c) => c.path === "/api/action/approval")).toBe(false);
  });

  it("sweep: an open Dismiss confirmation is withdrawn when the proposal changes underneath it", async () => {
    let content = "Two previews on 4183 collide. Use a per-worktree port.";
    const { hub, appClient } = render("/inbox?kind=proposal&item=memory%3Aprop-1", fullHub(), {
      "GET /api/proposals": () => ({ body: { proposals: [{ ...memoryProposals.proposals[0], content }] } }),
      "POST /api/action/proposal": { body: { ok: true } },
    });
    const main = await screen.findByRole("region", { name: "Selected item" }, { timeout: 8000 });
    fireEvent.click(await within(main).findByRole("button", { name: "Dismiss…" }, { timeout: 8000 }));
    expect(await screen.findByRole("dialog", { name: "Dismiss this proposal?" })).toBeInTheDocument();
    content = "Rewritten: use one shared preview server for every worktree.";
    await appClient().refetchQueries({ queryKey: ["proposals"] });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Dismiss this proposal?" })).toBeNull());
    expect(within(main).getByText("The proposal changed while you were deciding; review it again.")).toBeInTheDocument();
    expect(hub.calls.some((c) => c.path === "/api/action/proposal")).toBe(false);
  });

  it("files a memory proposal as a task with a reason", async () => {
    const { hub } = render("/inbox?kind=proposal&item=memory%3Aprop-1", fullHub(), {
      "POST /api/action/proposal": { body: { task_id: 600 } },
    });
    fireEvent.change(await screen.findByLabelText("Reason", {}, { timeout: 8000 }), { target: { value: "worth doing" } });
    fireEvent.click(screen.getByRole("button", { name: "File as a task" }));
    await waitFor(() =>
      expect(hub.calls.find((c) => c.path === "/api/action/proposal")?.body).toEqual({ action: "file_task", proposal_id: "prop-1", reason: "worth doing" }),
    );
    expect(await screen.findByText("Filed as task 600.")).toBeInTheDocument();
  });

  it("an alert links to the place that fixes it", async () => {
    render("/inbox?kind=anomaly", { ...fullHub(), "GET /api/attention": { body: attentionPayload() } });
    const list = await waitingList();
    fireEvent.click(within(list).getByRole("link", { name: /Claim has not moved/ }));
    expect(await screen.findByRole("link", { name: "Open task 55" })).toHaveAttribute("href", "/ui/work/tasks/55");
    expect(screen.getByRole("link", { name: "Open Desktop_Agent9" })).toHaveAttribute("href", "/ui/agents/profile/Desktop_Agent9");
  });
});
