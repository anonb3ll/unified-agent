import { describe, expect, it } from "vitest";
import { heldTaskMap, type TaskHold } from "../../src/lib/api/tasks";

function hold(overrides: Partial<TaskHold> = {}): TaskHold {
  return {
    id: 5,
    root_task_id: 890,
    mode: "pause",
    status: "active",
    reason: "sprint stop",
    release_policy: "manual",
    created_by: "Desktop_Operator",
    created_at: "2026-10-09T10:00:00+00:00",
    member_ids: [890, 891],
    ...overrides,
  };
}

describe("heldTaskMap", () => {
  it("maps every member to its hold, the oldest hold first", () => {
    const map = heldTaskMap([hold({ id: 9, member_ids: [891, 892] }), hold()]);
    expect(map.get(890)?.id).toBe(5);
    expect(map.get(891)?.id).toBe(5);
    expect(map.get(892)?.id).toBe(9);
    expect(map.has(10)).toBe(false);
  });
});
