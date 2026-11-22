import { describe, expect, it } from "vitest";
import { ulidFloor } from "../../../src/app/runs/api";
import { bothDayFieldsRestricted, nextRuns, parseCron } from "../../../src/app/runs/cron";
import { canPause, canResume, canStop, fireResultText, isRunning, looksLikeEvidence, runPill } from "../../../src/app/runs/model";
import { cycle, runRow } from "./hubData";
import type { Cycle, RunRow } from "../../../src/app/runs/api";

function next(schedule: string, fromIso: string, n = 3) {
  const p = parseCron(schedule);
  if (!p.ok) throw new Error(p.error);
  return nextRuns(p.fields, new Date(fromIso), n).map((d) => d.toISOString());
}

describe("cron, as the hub reads it (routines.cron_next)", () => {
  it("lists the next three UTC slots strictly after now", () => {
    expect(next("0 9 * * 1", "2026-11-10T12:00:00Z")).toEqual(["2026-11-16T09:00:00.000Z", "2026-11-23T09:00:00.000Z", "2026-11-30T09:00:00.000Z"]);
    expect(next("*/15 * * * *", "2026-11-10T12:00:00Z")).toEqual(["2026-11-10T12:15:00.000Z", "2026-11-10T12:30:00.000Z", "2026-11-10T12:45:00.000Z"]);
    expect(next("30 6 1 1 *", "2026-11-10T12:00:00Z", 1)).toEqual(["2027-01-01T06:30:00.000Z"]);
  });

  it("needs both day fields to match, like the hub (not classic cron's either)", () => {
    // Fridays the 13th; values checked against the hub's own cron_next.
    expect(next("0 0 13 * 5", "2026-11-10T00:00:00Z", 2)).toEqual(["2026-11-13T00:00:00.000Z", "2027-08-13T00:00:00.000Z"]);
    expect(bothDayFieldsRestricted("0 0 13 * 5")).toBe(true);
    expect(bothDayFieldsRestricted("0 0 * * 5")).toBe(false);
  });

  it("treats 7 as Sunday and a stepped single value as running to the top of the range", () => {
    expect(next("0 12 * * 7", "2026-11-10T00:00:00Z", 1)).toEqual(["2026-11-15T12:00:00.000Z"]);
    expect(next("50/5 10 * * *", "2026-11-10T00:00:00Z")).toEqual(["2026-11-10T10:50:00.000Z", "2026-11-10T10:55:00.000Z", "2026-11-11T10:50:00.000Z"]);
  });

  it("refuses what the hub refuses, in plain words", () => {
    expect(parseCron("0 9 * *")).toMatchObject({ ok: false, error: expect.stringMatching(/five parts/) });
    expect(parseCron("61 * * * *")).toMatchObject({ ok: false, error: expect.stringMatching(/minute must be between 0 and 59/) });
    expect(parseCron("* * * * mon")).toMatchObject({ ok: false, error: expect.stringMatching(/day of the week must be a number/) });
    expect(parseCron("*/0 * * * *")).toMatchObject({ ok: false, error: expect.stringMatching(/step/) });
    expect(parseCron("5-1 * * * *")).toMatchObject({ ok: false });
  });
});

describe("run states", () => {
  it("open runs are running; ended or abandoned ones are not", () => {
    expect(isRunning(runRow({ status: "running", ended_at: null }) as RunRow)).toBe(true);
    expect(isRunning(runRow({ kind: "selfpickup", status: "open", ended_at: null }) as RunRow)).toBe(true);
    expect(isRunning(runRow({ status: "running", ended_at: null, abandoned: true }) as RunRow)).toBe(false);
    expect(runPill(runRow({ status: "failed", outcome: "failed" }) as RunRow)).toEqual({ kind: "danger", label: "Failed" });
    expect(runPill(runRow({ status: "abandoned", abandoned: true }) as RunRow).label).toBe("Abandoned");
  });
});

describe("routine Run now answers", () => {
  it("explains skipped and queued when a run is already active", () => {
    expect(fireResultText({ status: "skipped", task_id: null })).toMatch(/Nothing was started: an earlier run .* still active/);
    expect(fireResultText({ status: "queued", task_id: 77 })).toMatch(/queued as task #77\. It starts when the earlier one finishes/);
    expect(fireResultText({ status: "created", task_id: 78 })).toMatch(/created task #78/);
    expect(fireResultText({ status: "suppressed", task_id: null })).toMatch(/no(ne)?/);
  });
});

describe("cycle actions", () => {
  it("offers pause, resume and stop only where the hub allows them", () => {
    const c = (state: string) => cycle({ state }) as Cycle;
    expect([canPause(c("executing")), canResume(c("executing")), canStop(c("executing"))]).toEqual([true, false, true]);
    expect([canPause(c("blocked")), canResume(c("blocked")), canStop(c("blocked"))]).toEqual([false, true, true]);
    expect([canPause(c("killed")), canResume(c("killed")), canStop(c("killed"))]).toEqual([false, false, false]);
  });

  it("accepts a link or a commit as pull request evidence (tools/cycles._looks_like_evidence)", () => {
    expect(looksLikeEvidence("https://github.com/x/y/pull/1")).toBe(true);
    expect(looksLikeEvidence("abc1234")).toBe(true);
    expect(looksLikeEvidence("see the PR")).toBe(false);
  });
});

describe("ulidFloor", () => {
  it("encodes the time as the smallest ULID at that millisecond", () => {
    expect(ulidFloor(0)).toBe("00000000000000000000000000");
    expect(ulidFloor(1)).toBe("00000000010000000000000000");
    expect(ulidFloor(Date.UTC(2026, 10, 10))).toHaveLength(26);
  });
});
