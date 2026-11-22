import { describe, expect, it } from "vitest";
import {
  ancestorDirs,
  applyLocalFilters,
  isLockedNote,
  lockState,
  lineDiff,
  isLockedRefusal,
  journalLength,
  journalProblem,
  newNoteNameProblem,
  newNotePath,
  parseTags,
  serverTagFor,
  wikilinkCandidates,
  writeFoldersFromRefusal,
} from "../../../src/app/knowledge/api";
import { routes, commands } from "../../../src/app/knowledge/routes";
import { MEMORY_ROWS } from "./fixtures";

describe("knowledge rules", () => {
  it("counts a journal entry the way vault_journal does: runs of whitespace are one space", () => {
    expect(journalLength("  a \n\n b\t c  ")).toBe(5);
    expect(journalProblem("a".repeat(240))).toBeNull();
    expect(journalProblem(`${"a ".repeat(120)}`)).toBeNull();
    expect(journalProblem("a".repeat(241))).toMatch(/241 characters/);
    expect(journalProblem("   ")).toBe("Write the entry first.");
  });

  it("sends one tag to the hub and filters author and project locally", () => {
    expect(serverTagFor({ q: "", tag: "", author: "", project: "vault" })).toBe("project:vault");
    expect(serverTagFor({ q: "", tag: "fact", author: "", project: "vault" })).toBe("fact");
    const rows = applyLocalFilters(MEMORY_ROWS, { q: "", tag: "", author: "Desktop_Agent2", project: "unified-agent" });
    expect(rows.map((r) => r.id)).toEqual(["b41c09e2aa10"]);
    expect(applyLocalFilters(MEMORY_ROWS, { q: "", tag: "fact", author: "", project: "unified-agent" })).toEqual([]);
  });

  it("resolves a wikilink beside the linking note, then at the root; a folder path as given", () => {
    expect(wikilinkCandidates("Runbook", "Wiki/Deploys.v2.md")).toEqual(["Wiki/Runbook.md", "Runbook.md"]);
    expect(wikilinkCandidates("Runbook#Step 2", "Index.md")).toEqual(["Runbook.md"]);
    expect(wikilinkCandidates("Logs/2026-11-10", "Wiki/a.md")).toEqual(["Logs/2026-11-10.md"]);
    expect(wikilinkCandidates("^block", "Wiki/a.md")).toEqual([]);
  });

  it("reads the write folders and the lock from the hub's own refusal text", () => {
    expect(writeFoldersFromRefusal("vault writes are restricted to 00 - Inbox, Logs, Wiki; 'x.md' is outside. Extend …")).toEqual(["00 - Inbox", "Logs", "Wiki"]);
    expect(writeFoldersFromRefusal("vault writes are restricted to no directories; 'x' is outside.")).toEqual([]);
    expect(writeFoldersFromRefusal("something else")).toBeNull();
    expect(isLockedRefusal("note 'Wiki/a.md' is locked (frontmatter `locked: true`); pass force=True")).toBe(true);
    expect(isLockedNote("---\ntitle: a\nlocked: true\n---\nbody")).toBe(true);
    expect(isLockedNote("locked: true\n")).toBe(false);
  });

  it("reads the lock the way the hub does, conservatively (review 1, finding 8)", () => {
    const fm = (body: string) => `---\n${body}\n---\n# Note\n`;
    expect(lockState(fm("locked: true # curated"))).toBe("locked");
    expect(lockState(fm('"locked": true'))).toBe("locked");
    expect(lockState(fm("Locked: yes"))).toBe("locked");
    expect(lockState(fm("locked: 'true'"))).toBe("locked");
    expect(lockState(fm("locked:\n  - true"))).toBe("locked");
    expect(lockState(`\ufeff---\r\nlocked: on\r\n---\r\nbody`)).toBe("locked");
    expect(lockState(fm("locked: false"))).toBe("unlocked");
    expect(lockState(fm("locked: 'no' # open"))).toBe("unlocked");
    expect(lockState(fm("title: a"))).toBe("unlocked");
    expect(lockState("# no front matter\nlocked: true")).toBe("unlocked");
    expect(lockState(fm("{locked: true}"))).toBe("uncertain");
    expect(lockState(fm("meta:\n  locked: true"))).toBe("uncertain");
    expect(lockState("---\nlocked: true\nno closing line")).toBe("uncertain");
    expect(lockState("---\ntitle: a\nno closing line")).toBe("unlocked");
    // The hub's legacy block scan reads past a YAML "..." end to the first "---" line (review 2, finding 11).
    expect(lockState("---\ntitle: a\n...\nlocked: true\n---\nbody")).toBe("locked");
    expect(lockState("---\ntitle: a\n...\nlocked: maybe # x\n---\nbody")).toBe("locked");
    expect(lockState("---\ntitle: a\n...\nlocked: false\n---\nbody")).toBe("unlocked");
    // No "---" line after the "...": the hub reads only the YAML block, as here.
    expect(lockState("---\ntitle: a\n...\nbody text\nlocked: true")).toBe("unlocked");
  });

  it("builds a new note path and refuses unsafe names", () => {
    expect(newNotePath("Wiki", "Plan")).toBe("Wiki/Plan.md");
    expect(newNotePath("00 - Inbox", "idea.txt")).toBe("00 - Inbox/idea.txt");
    expect(newNoteNameProblem("../escape")).toMatch(/cannot/);
    expect(newNoteNameProblem("pic.png")).toMatch(/\.md or \.txt/);
    expect(newNoteNameProblem("ok name")).toBeNull();
    expect(ancestorDirs("a/b/c.md")).toEqual(["a", "a/b"]);
    expect(parseTags("a, b,,a\nc")).toEqual(["a", "b", "c"]);
  });

  it("diffs lines, and declines texts too long to compare", () => {
    expect(lineDiff("a\nb\nc", "a\nc\nd")).toEqual([
      { kind: "removed", text: "b" },
      { kind: "added", text: "d" },
    ]);
    expect(lineDiff("x".repeat(20_001), "y")).toBeNull();
  });

  it("exports the area routes and palette commands", () => {
    expect(routes.map((r) => r.path ?? "(index)")).toEqual(["(index)", "memory", "vault"]);
    expect(commands.map((c) => c.to)).toEqual([
      "/knowledge/memory?new=1",
      "/knowledge/memory",
      "/knowledge/vault",
      "/knowledge/vault?write=new",
      "/knowledge/vault?write=journal",
    ]);
  });
});
