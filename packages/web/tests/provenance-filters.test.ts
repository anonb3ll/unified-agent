import { describe, expect, it } from "vitest";
import {
  localDateTimeToUtc,
  normalizeProvenanceFilter,
  readProvenanceFilter,
  utcDateTimeToLocal,
  writeProvenanceFilter,
} from "../src/lib/provenance/filters";

describe("provenance filter URL and API normalization", () => {
  it("keeps both visible release ids by merging them into task_ids", () => {
    expect(
      normalizeProvenanceFilter({ query: "by_release", task_id: "777", task_ids: "1180,1181" }),
    ).toMatchObject({ task_id: "", task_ids: "777,1180,1181" });
  });

  it("leaves a singular-only release query on its own parameter", () => {
    expect(
      normalizeProvenanceFilter({ query: "by_release", task_id: "777", task_ids: "" }),
    ).toMatchObject({ task_id: "777", task_ids: "" });
  });

  it("dedupes a release id that appears in both controls", () => {
    expect(
      normalizeProvenanceFilter({ query: "by_release", task_id: "777", task_ids: "777,1180" }),
    ).toMatchObject({ task_id: "", task_ids: "777,1180" });
  });

  it("converts local form time to a UTC instant", () => {
    expect(localDateTimeToUtc("2026-09-23T10:30")).toBe(new Date(2026, 8, 23, 10, 30).toISOString());
  });

  it("round-trips applied filters through shareable URL parameters", () => {
    const url = new URL("https://example.test/ui/?unrelated=keep");
    const filter = {
      query: "by_human" as const,
      human: "Desktop_Person",
      since: "2026-09-23T10:30",
      until: "2026-09-23T11:30",
      task_id: "777",
      task_ids: "",
      project_id: "819",
      agent: "Desktop_Agent1",
      pr: "704",
      limit: "25",
    };
    writeProvenanceFilter(url, filter);
    expect(url.searchParams.get("unrelated")).toBe("keep");
    const sinceUtc = new Date(2026, 8, 23, 10, 30).toISOString();
    const untilUtc = new Date(2026, 8, 23, 11, 30).toISOString();
    expect(url.searchParams.get("since")).toBe(sinceUtc);
    // Reading a shareable URL keeps the absolute instants; only the form field
    // renders them in local time.
    expect(readProvenanceFilter(url.search)).toMatchObject({
      ...filter,
      since: sinceUtc,
      until: untilUtc,
    });
  });

  it("preserves a deep-linked instant to the second", () => {
    const search = "?query=by_human&since=2026-09-23T10:30:45Z&until=2026-09-23T11:30:45.250Z";
    const filter = readProvenanceFilter(search);
    expect(filter.since).toBe("2026-09-23T10:30:45Z");
    expect(filter.until).toBe("2026-09-23T11:30:45.250Z");

    const url = new URL("https://example.test/ui/" + search);
    writeProvenanceFilter(url, filter);
    // The URL text is normalized, but the instants - and so the requested
    // window - are exactly the ones the shared link carried.
    expect(new Date(url.searchParams.get("since") ?? "").getTime()).toBe(
      new Date("2026-09-23T10:30:45Z").getTime(),
    );
    expect(new Date(url.searchParams.get("until") ?? "").getTime()).toBe(
      new Date("2026-09-23T11:30:45.250Z").getTime(),
    );
  });

  it("renders local form values with the precision they were given", () => {
    const iso = new Date(2026, 8, 23, 10, 30, 45).toISOString();
    expect(utcDateTimeToLocal(iso)).toBe("2026-09-23T10:30:45");
    expect(localDateTimeToUtc(utcDateTimeToLocal(iso))).toBe(iso);
  });
});
