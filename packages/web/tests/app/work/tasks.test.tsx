import { fireEvent, screen, waitFor, within, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findPageHeading, renderRoute } from "../support/render";
import { BOARD, BOARD_WITHOUT_DOMAIN, EMPTY_BOARD } from "./fixtures";
import { PARKED, workHub } from "./hub";

// The first test pays for the lazy area import; on a loaded machine that alone can take seconds.
vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  // Unmount first: a form flushes its draft on unmount, which must not outlive the clear below.
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

const taskWrites = <T extends { method: string; path: string }>(calls: T[]) => calls.filter((c) => c.method === "POST" && c.path === "/api/action/task");

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  listeners = new Map<string, Set<(e: Event) => void>>();
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (e: Event) => void) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(listener);
  }
  removeEventListener() {}
  close() {}
  emit(type: string, data?: unknown) {
    const event = data === undefined ? new Event(type) : new MessageEvent(type, { data: JSON.stringify(data) });
    for (const l of this.listeners.get(type) ?? []) l(event);
  }
}

describe("work: task set", () => {
  // Review round 1, finding 6: Work reads never refreshed while the shell said live updates were on.
  it("refetches the task set when the hub stream reports a task change", async () => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    const { hub } = renderRoute("/work", { hub: workHub() });
    await screen.findByRole("table", { name: "Tasks" }, { timeout: 15_000 });
    const boardReads = () => hub.calls.filter((c) => c.method === "GET" && c.path === "/api/board").length;
    const before = boardReads();
    await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0));
    for (const source of FakeEventSource.instances) source.emit("message", { channel: "agent.task.updated", data: { id: 101 } });
    await waitFor(() => expect(boardReads()).toBeGreaterThan(before), { timeout: 10_000 });
  });

  it("lists tasks and keeps filters in the URL", async () => {
    const { router } = renderRoute("/work?status=doing", { hub: workHub() });
    await findPageHeading("Work");
    const table = await screen.findByRole("table", { name: "Tasks" });
    expect(within(table).getByRole("link", { name: "Ship the Work area" })).toHaveAttribute("href", "/ui/work/tasks/102");
    expect(within(table).queryByText("Write the release notes")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Waiting for review" }));
    await waitFor(() => expect(router.state.location.search).toContain("review=1"));
    expect(await screen.findByText(/No tasks match these filters/)).toBeInTheDocument();
  });

  // Review round 1, finding 15: a Domain filter only where the board sends domain (#2480).
  it("filters by domain when the board carries it, and hides the filter when it does not", async () => {
    const { router } = renderRoute("/work?domain=personal", { hub: workHub() });
    const table = await screen.findByRole("table", { name: "Tasks" }, { timeout: 15_000 });
    expect(within(table).getByRole("link", { name: "Wait for the design sign-off" })).toBeInTheDocument();
    expect(within(table).queryByRole("link", { name: "Ship the Work area" })).not.toBeInTheDocument();
    const filters = screen.getByRole("group", { name: "Task filters" });
    expect(within(filters).getByLabelText("Domain")).toHaveValue("personal");
    expect(router.state.location.search).toContain("domain=personal");
  });

  it("hides the Domain filter for a board without domain", async () => {
    renderRoute("/work", { hub: workHub({ "GET /api/board": { body: BOARD_WITHOUT_DOMAIN } }) });
    await screen.findByRole("table", { name: "Tasks" }, { timeout: 15_000 });
    expect(within(screen.getByRole("group", { name: "Task filters" })).queryByLabelText("Domain")).not.toBeInTheDocument();
  });

  // Review round 1, finding 12: no total or column count from a partial read.
  it("a partial board read shows no counts and says the list is incomplete", async () => {
    renderRoute("/work?view=board", { hub: workHub({ "GET /api/board": { body: { ...BOARD, errors: ["task_list page 3: database is locked"] } } }) });
    const doing = await screen.findByRole("heading", { level: 2, name: /^Doing/ }, { timeout: 15_000 });
    expect(doing).toHaveTextContent("–");
    expect(doing).not.toHaveTextContent(/\d/);
    expect(screen.getByText(/Some tasks could not be read/)).toBeInTheDocument();
    expect(screen.queryByText(/of 7 tasks/)).not.toBeInTheDocument();
    expect(screen.getByText(/Done shows tasks finished in the last 30 days/)).toBeInTheDocument();
  });

  // Review round 1, finding 10: the root work graph has goals only; a goal drills in (URL-held) and the filters apply.
  it("work graph: choosing a goal opens its subtree in the URL, with the task filters applied", async () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    const { router, hub } = renderRoute("/work?view=graph&scope=work&status=doing", { hub: workHub() });
    const view = await screen.findByRole("group", { name: "Goals, plans and tasks: view" }, { timeout: 15_000 });
    fireEvent.click(within(view).getByRole("button", { name: "Table" }));
    const nodes = await screen.findByRole("region", { name: "Goals, plans and tasks: nodes" });
    fireEvent.click(within(nodes).getByRole("button", { name: "A usable Control Center" }));
    await waitFor(() => expect(router.state.location.search).toContain("goal=7"));
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/work-graph" && c.method === "GET")).toBe(true));
    const goalView = await screen.findByRole("group", { name: "A usable Control Center: view" }, { timeout: 15_000 });
    fireEvent.click(within(goalView).getByRole("button", { name: "Table" }));
    const expanded = await screen.findByRole("region", { name: "A usable Control Center: nodes" });
    // status=doing keeps task 102 and hides done task 100.
    expect(within(expanded).getByRole("button", { name: "Ship the Work area" })).toBeInTheDocument();
    expect(within(expanded).queryByRole("button", { name: "Draft the spec" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All goals" })).toBeInTheDocument();
  });

  // Review round 1, finding 22: the dependency graph keeps plans, agents, claims, delegations and attribution.
  it("dependency graph keeps plans, agents and their relations, with detail navigation", async () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    const { router } = renderRoute("/work?view=graph", { hub: workHub() });
    const view = await screen.findByRole("group", { name: "Task dependencies: view" }, { timeout: 15_000 });
    fireEvent.click(within(view).getByRole("button", { name: "Table" }));
    const nodes = await screen.findByRole("region", { name: "Task dependencies: nodes" });
    const claude = within(nodes).getByRole("button", { name: "Desktop_Agent1" }).closest("tr")!;
    expect(claude).toHaveTextContent("delegates to Desktop_Agent2");
    const task = within(nodes).getByRole("button", { name: "#102 Ship the Work area" }).closest("tr")!;
    expect(task).toHaveTextContent("claimed by Desktop_Agent1");
    expect(task).toHaveTextContent(/evidence/);
    expect(within(nodes).getByRole("button", { name: "operator" }).closest("tr")!).toHaveTextContent("authored Roll out Control Center v2");
    fireEvent.click(within(nodes).getByRole("button", { name: "Roll out Control Center v2" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/ui/work/plans/3"));
  });

  // Review round 1, finding 23: the board keeps delegation cards, WIP limits and aging/lock alerts.
  it("board shows pending delegations, WIP limits and card alerts", async () => {
    renderRoute("/work?view=board", { hub: workHub() });
    const backlog = (await screen.findByRole("heading", { level: 2, name: /^Backlog/ }, { timeout: 15_000 })).closest("section")!;
    const delegation = within(backlog).getByText("Review the board tests").closest("li")!;
    expect(delegation).toHaveTextContent(/Delegation from Desktop_Agent1 to Desktop_Agent2/);
    expect(delegation).toHaveTextContent(/Overdue/);
    expect(within(delegation).queryByRole("button", { name: /Move/ })).not.toBeInTheDocument();
    expect(within(backlog).getByRole("link", { name: /#101/ }).closest("li")!).toHaveTextContent(/Waiting a long time/);
    const doing = screen.getByRole("heading", { level: 2, name: /^Doing/ }).closest("section")!;
    expect(within(doing).getByRole("link", { name: /#102/ }).closest("li")!).toHaveTextContent(/Claim lock expired/);
    const wip = screen.getByRole("region", { name: "Work in progress limits" });
    expect(wip).toHaveTextContent(/Default queue: 3 in progress, limit 2/);
    expect(wip).toHaveTextContent(/over the limit/);
  });

  // Review round 2, item 13: each view is gated on its own data.
  it("the work graph shows goals even when the board has no tasks", async () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    renderRoute("/work?view=graph&scope=work", { hub: workHub({ "GET /api/board": { body: EMPTY_BOARD } }) });
    expect(await screen.findByRole("group", { name: "Goals, plans and tasks: view" }, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.queryByText("No tasks yet")).not.toBeInTheDocument();
  });

  it("a board with only delegations shows them, not an empty state", async () => {
    const onlyDelegation = { ...EMPTY_BOARD, columns: EMPTY_BOARD.columns.map((c) => (c.status === "open" ? { ...c, cards: BOARD.columns[0].cards.filter((card) => card.kind === "delegation") } : c)) };
    renderRoute("/work?view=board", { hub: workHub({ "GET /api/board": { body: onlyDelegation } }) });
    expect(await screen.findByText("Review the board tests", {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.queryByText("No tasks yet")).not.toBeInTheDocument();
  });

  it("filters that match no task still leave the graph explorable", async () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    renderRoute("/work?view=graph&q=zzz-nothing", { hub: workHub() });
    expect(await screen.findByRole("group", { name: "Task dependencies: view" }, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.getByText(/No tasks match these filters/)).toBeInTheDocument();
  });

  // Review round 2, item 14: root goals past the first page, and a goal past the page cap, stay reachable.
  it("work graph pages through root goals and reads on past the subtree's page cap", async () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    const goal = (id: number, label: string) => ({ id: `goal:${id}`, kind: "goal", goal_id: id, label, status: "open", project: "web", parent_goal_id: null, updated_at: "2026-11-10T09:00:00+00:00" });
    const base = { edges: [], scope: { root_goals: true }, cursor: null, as_of: "2026-11-10T12:00:00+00:00", revision: "r0", unknowns: [] };
    const { hub } = renderRoute("/work?view=graph&scope=work", {
      hub: workHub({
        "GET /api/work-graph": ({ url }) => {
          const q = new URL(url, "http://x").searchParams;
          if (q.get("goal_id") === "9") {
            const n = Number(q.get("cursor") ?? "0");
            return { body: { ...base, scope: { goal_id: 9 }, nodes: [goal(9, "Second page goal"), { id: `task:${200 + n}`, kind: "task", task_id: 200 + n, label: `Task ${200 + n}`, status: "open", stub: false }], next_cursor: String(n + 1) } };
          }
          return q.get("cursor") === "g2"
            ? { body: { ...base, nodes: [goal(9, "Second page goal")], next_cursor: null } }
            : { body: { ...base, nodes: [goal(7, "A usable Control Center")], next_cursor: "g2" } };
        },
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Show more goals" }, { timeout: 15_000 }));
    const view = await screen.findByRole("group", { name: "Goals, plans and tasks: view" });
    fireEvent.click(within(view).getByRole("button", { name: "Table" }));
    const nodes = await screen.findByRole("region", { name: "Goals, plans and tasks: nodes" });
    fireEvent.click(await within(nodes).findByRole("button", { name: "Second page goal" }));
    expect(await screen.findByText(/has more work than one view reads/, {}, { timeout: 15_000 })).toBeInTheDocument();
    const before = hub.calls.filter((c) => c.path === "/api/work-graph").length;
    fireEvent.click(screen.getByRole("button", { name: "Read more of this goal" }));
    await waitFor(() => expect(hub.calls.filter((c) => c.path === "/api/work-graph").length).toBeGreaterThan(before + 1));
  });

  // Review round 2, item 16: node detail keeps attribution, proof and readiness; every node kind opens its page.
  it("the task drawer shows attribution, proof and readiness, with activity links", async () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    renderRoute("/work?view=graph&scope=work&goal=7&task=102", { hub: workHub() });
    const drawer = await screen.findByRole("dialog", { name: "#102 Ship the Work area" }, { timeout: 15_000 });
    expect(await within(drawer).findByText(/Operator → desktop → Desktop_Agent1 → task → evidence/)).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: "https://github.com/example/repo/pull/7" })).toBeInTheDocument();
    expect(await within(drawer).findByText(/Blocked: unmet dependency/)).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: "#100" })).toHaveAttribute("href", "/ui/work/tasks/100");
    expect(within(drawer).getByRole("link", { name: "Runs, evidence and comments" })).toHaveAttribute("href", "/ui/work/tasks/102#task-activity");
  });

  it.each([
    ["Roll out Control Center v2", "/ui/work/plans/3"],
    ["Session s-1", "/ui/sessions/s-1"],
    ["Evidence for #102", "/ui/settings/audit"],
  ])("work graph: choosing %s opens its page", async (name, path) => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    const { router } = renderRoute("/work?view=graph&scope=work&goal=7", { hub: workHub() });
    const view = await screen.findByRole("group", { name: "A usable Control Center: view" }, { timeout: 15_000 });
    fireEvent.click(within(view).getByRole("button", { name: "Table" }));
    fireEvent.click(within(await screen.findByRole("region", { name: "A usable Control Center: nodes" })).getByRole("button", { name }));
    await waitFor(() => expect(router.state.location.pathname).toBe(path));
  });

  // Review round 2, item 15: ranked search through /api/tasks/search, and the queue's health buckets and back-pressure.
  it("search ranks hub results with their explanation, including tasks outside the board", async () => {
    const { hub } = renderRoute("/work?q=release", { hub: workHub() });
    const results = await screen.findByRole("region", { name: "Search results" }, { timeout: 15_000 });
    expect(await within(results).findByRole("link", { name: /#90 Release checklist from last quarter/ })).toHaveAttribute("href", "/ui/work/tasks/90");
    expect(within(results).getAllByText(/Matched words/)).toHaveLength(2);
    expect(within(results).getByText(/the release checklist/)).toBeInTheDocument();
    expect(hub.calls.some((c) => c.path === "/api/tasks/search")).toBe(true);
  });

  it("shows the queue's health buckets and back-pressure", async () => {
    renderRoute("/work", { hub: workHub() });
    const health = await screen.findByRole("region", { name: "Queue health" }, { timeout: 15_000 });
    expect(await within(health).findByText(/Stale claims: 1/)).toBeInTheDocument();
    expect(within(health).getByText(/Blocked or archived: 1/)).toBeInTheDocument();
    expect(within(health).getByText(/1 task in doing has no live claimant/)).toBeInTheDocument();
    expect(within(health).getByText(/backlog in default queue/)).toBeInTheDocument();
    fireEvent.click(within(health).getByText(/Stale claims: 1/));
    expect(within(health).getByRole("link", { name: /#107 Old claimed task/ })).toBeInTheDocument();
  });

  it("shows loading, then an error with Retry, never a count", async () => {
    renderRoute("/work", { hub: workHub({ "GET /api/board": { status: 403, body: { error: "denied" } } }) });
    await findPageHeading("Work");
    expect(await screen.findByText("Tasks could not be loaded")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText(/of \d+ task/)).not.toBeInTheDocument();
  });

  it("shows the empty state with a create action", async () => {
    renderRoute("/work", { hub: workHub({ "GET /api/board": { body: EMPTY_BOARD } }) });
    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
  });

  it("tree view nests subtasks and collapses them", async () => {
    renderRoute("/work?view=tree", { hub: workHub() });
    const tree = await screen.findByRole("navigation", { name: "Task tree" });
    expect(within(tree).getByRole("link", { name: /#105 Write the board tests/ })).toBeInTheDocument();
    fireEvent.click(within(tree).getByRole("button", { name: "Collapse subtasks of #102" }));
    expect(within(tree).queryByRole("link", { name: /#105/ })).not.toBeInTheDocument();
  });

  // Spec §12 defect 2: arrow keys on the board changed status with no confirmation.
  it("board: keys on a card never write; Move to opens a confirm first", async () => {
    const { hub } = renderRoute("/work?view=board", { hub: workHub({ "POST /api/action/task": { body: { id: 101, status: "doing" } } }) });
    const card = (await screen.findByRole("link", { name: /#101 Write the release notes/ })).closest("li")!;
    for (const key of ["ArrowRight", "ArrowLeft", "ArrowDown", "Enter", " "]) fireEvent.keyDown(card, { key });
    expect(taskWrites(hub.calls)).toHaveLength(0);
    fireEvent.click(within(card).getByRole("button", { name: "Move task #101 to" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Doing" }));
    const dialog = await screen.findByRole("dialog", { name: "Move this task?" });
    expect(taskWrites(hub.calls)).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Move to Doing" }));
    await waitFor(() => expect(taskWrites(hub.calls)).toHaveLength(1));
    // Review round 2, item 4: the move carries the card's updated_at, so a stale card answers 409.
    expect(taskWrites(hub.calls)[0].body).toEqual({ id: 101, status: "doing", expected_updated_at: "2026-11-09T08:00:00+00:00" });
  });

  // Spec §12 defect 1: a 202 pending approval was shown as success on the board.
  it("board: a parked move says it waits for approval, not that it moved", async () => {
    renderRoute("/work?view=board", { hub: workHub({ "POST /api/action/task": PARKED("task_update") }) });
    const card = (await screen.findByRole("link", { name: /#101 Write the release notes/ })).closest("li")!;
    fireEvent.click(within(card).getByRole("button", { name: "Move task #101 to" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Done" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Move this task?" })).getByRole("button", { name: "Move to Done" }));
    expect(await screen.findByText(/Waiting for approval/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=77");
    expect(screen.queryByText(/moved to Done/)).not.toBeInTheDocument();
  });

  it("board: a refused move explains why", async () => {
    renderRoute("/work?view=board", { hub: workHub({ "POST /api/action/task": { status: 400, body: { error: "status done requires evidence_ref" } } }) });
    const card = (await screen.findByRole("link", { name: /#101 Write the release notes/ })).closest("li")!;
    fireEvent.click(within(card).getByRole("button", { name: "Move task #101 to" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Done" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Move this task?" })).getByRole("button", { name: "Move to Done" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Task #101 was not moved.");
    expect(alert).toHaveTextContent(/needs evidence first/);
  });

  it("graph: correct edge words in the table fallback, and a node opens the drawer", async () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    const { router } = renderRoute("/work?view=graph", { hub: workHub() });
    const figure = await screen.findByRole("group", { name: "Task dependencies: view" });
    fireEvent.click(within(figure).getByRole("button", { name: "Table" }));
    const nodes = await screen.findByRole("region", { name: "Task dependencies: nodes" });
    const row = within(nodes).getByRole("button", { name: "#102 Ship the Work area" }).closest("tr")!;
    expect(row).toHaveTextContent("blocks #101 Write the release notes");
    expect(row).toHaveTextContent("parent of #104 Add the export button");
    expect(screen.getByRole("list", { name: "Line labels" })).toHaveTextContent("blocks");
    fireEvent.click(within(nodes).getByRole("button", { name: "#102 Ship the Work area" }));
    await waitFor(() => expect(router.state.location.search).toContain("task=102"));
    expect(await screen.findByRole("dialog", { name: "#102 Ship the Work area" })).toBeInTheDocument();
  });
});
