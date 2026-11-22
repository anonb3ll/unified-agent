import { describe, expect, it } from "vitest";
import type { BoardPayload } from "../../../src/lib/api/tasks";
import { PENDING_HUB_ROUTES, TASK_UPDATE_FIELDS } from "../../../src/app/work/api";
import { buildGraph } from "../../../src/app/work/TaskViews";
import { buildTree, explainRefusal, filterTasks, FILTER_DEFAULTS, relationEdge, relationFor, sortTasks, tasksFromBoard } from "../../../src/app/work/model";
import { BOARD, TASK_GRAPH_OVERVIEW, WORK_GRAPH_7 } from "./fixtures";

const tasks = tasksFromBoard(BOARD as unknown as BoardPayload);

describe("work model", () => {
  it("flattens the board into one task set, nested subtasks included and delegations left out", () => {
    expect(tasks.map((t) => t.id).sort()).toEqual([100, 101, 102, 103, 104, 105, 106]);
    expect(tasks.find((t) => t.id === 105)?.parentId).toBe(102);
  });

  it("filters by every URL filter", () => {
    const f = (patch: Partial<typeof FILTER_DEFAULTS>, me: string | null = null) => filterTasks(tasks, { ...FILTER_DEFAULTS, ...patch }, me).map((t) => t.id).sort();
    expect(f({ status: "doing" })).toEqual([102, 105]);
    expect(f({ project: "web" })).toEqual([102, 104, 105, 106]);
    expect(f({ priority: "high" })).toEqual([102]);
    expect(f({ owner: "Desktop_Agent2" })).toEqual([103]);
    expect(f({ queue: "laptop" })).toEqual([104]);
    expect(f({ q: "board" })).toEqual([105]);
    expect(f({ mine: "1" }, "Desktop_Agent1")).toEqual([102, 105]);
    expect(f({ mine: "1" }, null)).toEqual([]);
    expect(f({ blocked: "1" })).toEqual([106]);
    expect(f({ review: "1" })).toEqual([103]);
    expect(f({ domain: "personal" })).toEqual([106]);
  });

  it("sorts and builds the parent tree", () => {
    expect(sortTasks(tasks, "priority", true)[0].id).toBe(102);
    const { roots, children } = buildTree(tasks);
    expect(roots.map((r) => r.id)).not.toContain(104);
    expect(children.get(102)?.map((c) => c.id)).toEqual([104, 105]);
  });

  // Spec §12 defect 6: graph edges carried the wrong relation words.
  it("labels dependency and parent edges correctly in both hub vocabularies", () => {
    // overview (webui/api.py task_graph_payload): prerequisite → dependent, kept as drawn.
    expect(relationEdge("depends_on", "task:102", "task:101", "e")).toEqual({ id: "e", source: "task:102", target: "task:101", label: "blocks" });
    // work graph (work_graph.py): dependent → prerequisite, so it is reversed.
    expect(relationEdge("task_dependency", "task:102", "task:100", "e")).toMatchObject({ source: "task:100", target: "task:102", label: "blocks" });
    // overview subtask: parent → child.
    expect(relationEdge("subtask", "task:102", "task:104", "e")).toMatchObject({ source: "task:102", target: "task:104", label: "parent of" });
    // work graph task_parent: child → parent.
    expect(relationEdge("task_parent", "task:104", "task:102", "e")).toMatchObject({ source: "task:102", target: "task:104", label: "parent of" });
    const blocks = relationEdge("depends_on", "task:102", "task:101", "e")!;
    expect(relationFor("task:101", blocks)).toEqual({ label: "blocked by", other: "task:102" });
    expect(relationFor("task:102", blocks)).toEqual({ label: "blocks", other: "task:101" });
  });

  it("builds the drawn graph with the correct words from both payloads", () => {
    const overview = buildGraph(
      TASK_GRAPH_OVERVIEW.task_graph.nodes.map((n) => ({ ...n })),
      TASK_GRAPH_OVERVIEW.task_graph.links.map((l) => ({ kind: l.kind, from: l.source, to: l.target })),
    );
    expect(overview.edges.map((e) => `${e.source} ${e.label} ${e.target}`)).toEqual([
      "task:102 claimed by agent:Desktop_Agent1",
      "task:102 parent of task:104",
      "task:102 blocks task:101",
      "agent:Desktop_Agent1 delegates to agent:Desktop_Agent2",
      "agent:operator authored plan:3",
    ]);
    const work = buildGraph(
      WORK_GRAPH_7.nodes.map((n) => ({ ...n, label: n.label ?? n.id })),
      WORK_GRAPH_7.edges,
    );
    expect(work.edges.map((e) => `${e.source} ${e.label} ${e.target}`)).toEqual(["plan:3 serves goal:7", "session:s-1 ran task:102", "evidence:102 evidence for task:102", "task:102 serves goal:7", "task:100 serves goal:7", "task:100 blocks task:102"]);
  });

  it("explains a refused move in words a person can act on", () => {
    expect(explainRefusal("done", "The hub did not accept this: done requires evidence_ref")).toMatch(/needs evidence first/);
    expect(explainRefusal("done", "The hub did not accept this: task must pass review")).toMatch(/needs a review first/);
    expect(explainRefusal("doing", "x", "task is claimed by Desktop_Agent2")).toMatch(/Another agent has claimed/);
    expect(explainRefusal("doing", "Something else")).toBe("Something else");
  });

  it("names every task_update field and every hub route this slice still needs", () => {
    expect(TASK_UPDATE_FIELDS).toContain("archived");
    expect(TASK_UPDATE_FIELDS).toHaveLength(18);
    expect(PENDING_HUB_ROUTES.map((r) => r.route)).toContain("POST /api/action/goal/{edit,link,unlink,close,reopen}");
  });
});
