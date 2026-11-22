import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { findPageHeading, renderRoute } from "../support/render";
import { chainVerdict } from "../../../src/app/settings/AuditPage";
import { selectorProblem, providerStatuses, UNRESOLVED_KEY } from "../../../src/app/settings/CredentialsPage";
import { fixtureEpoch } from "../support/epoch";
import { DENSITY_KEY, THEME_PREF_KEY } from "../../../src/app/settings/appearance";
import { evidenceQuery, EMPTY_EVIDENCE_FILTERS } from "../../../src/app/settings/api";
import { CREDENTIAL_ID, EVIDENCE_TAMPERED, manyRecords, POOL, PROVENANCE } from "./fixtures";
import { posts, settingsHub } from "./hub";

vi.setConfig({ testTimeout: 40_000 });

beforeEach(() => {
  // GraphCanvas (React Flow) measures with ResizeObserver, which jsdom lacks.
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", RO);
});

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
  document.documentElement.removeAttribute("data-density");
});

const REVOKE = "/api/action/session-credential-revoke";

describe("settings: revoke a session credential", () => {
  it("validates, focuses the invalid field and sends nothing", async () => {
    const { hub } = renderRoute("/settings/credentials", { hub: settingsHub() });
    await findPageHeading("Credentials");
    const field = await screen.findByLabelText(/Credential id/);
    fireEvent.change(field, { target: { value: "not-hex" } });
    fireEvent.click(screen.getByRole("button", { name: "Revoke…" }));
    expect(await screen.findByText(/This is not a credential id/)).toBeInTheDocument();
    expect(document.activeElement).toBe(field);
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(posts(hub.calls, REVOKE)).toHaveLength(0);
  });

  it("confirms with the object and consequence, sends exactly one selector, reports the count", async () => {
    const { hub } = renderRoute("/settings/credentials", { hub: settingsHub({ [`POST ${REVOKE}`]: { body: { revoked: [CREDENTIAL_ID], count: 1 } } }) });
    await findPageHeading("Credentials");
    fireEvent.click(await screen.findByLabelText("Every credential of one session run"));
    const field = screen.getByLabelText(/Session run id/);
    fireEvent.change(field, { target: { value: "nd-0123456789abcdef" } });
    fireEvent.click(screen.getByRole("button", { name: "Revoke…" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke this session's access?" });
    expect(within(dialog).getByText("Every credential of session run nd-0123456789abcdef")).toBeInTheDocument();
    expect(within(dialog).getByText(/refused on its next request.*cannot be undone/)).toBeInTheDocument();
    expect(posts(hub.calls, REVOKE)).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(posts(hub.calls, REVOKE)).toHaveLength(1));
    const call = posts(hub.calls, REVOKE)[0];
    expect(call.body).toEqual({ item_id: "nd-0123456789abcdef" });
    expect(call.headers["x-ua-csrf"]).toBe("csrf-1");
    expect(call.headers["idempotency-key"]).toMatch(/^revoke-/);
    expect(await screen.findByText(/1 credential revoked/)).toBeInTheDocument();
    // Nothing was typed after the submit, so the form is cleared.
    await waitFor(() => expect(screen.getByLabelText(/Credential id/)).toHaveValue(""));
  });

  it("cancel sends nothing and keeps the text", async () => {
    const { hub } = renderRoute("/settings/credentials", { hub: settingsHub() });
    await findPageHeading("Credentials");
    fireEvent.change(await screen.findByLabelText(/Credential id/), { target: { value: CREDENTIAL_ID } });
    fireEvent.click(screen.getByRole("button", { name: "Revoke…" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByLabelText(/Credential id/)).toHaveValue(CREDENTIAL_ID);
    expect(posts(hub.calls, REVOKE)).toHaveLength(0);
  });

  it("explains a refusal for a non-operator in plain words and keeps the text", async () => {
    renderRoute("/settings/credentials", {
      hub: settingsHub({ [`POST ${REVOKE}`]: { status: 403, body: { error: "revoking session credentials is an operator action" } } }),
    });
    await findPageHeading("Credentials");
    fireEvent.change(await screen.findByLabelText(/Credential id/), { target: { value: CREDENTIAL_ID } });
    fireEvent.click(screen.getByRole("button", { name: "Revoke…" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke" }));
    const alert = await screen.findByText(/The signed-in identity may not do this/);
    expect(alert.closest('[role="alert"]')).not.toBeNull();
    expect(screen.getByLabelText(/Credential id/)).toHaveValue(CREDENTIAL_ID);
  });

  it("says the outcome is unknown when the answer is lost", async () => {
    renderRoute("/settings/credentials", {
      hub: settingsHub({
        [`POST ${REVOKE}`]: () => {
          throw new TypeError("network connection lost");
        },
      }),
    });
    await findPageHeading("Credentials");
    fireEvent.change(await screen.findByLabelText(/Credential id/), { target: { value: CREDENTIAL_ID } });
    fireEvent.click(screen.getByRole("button", { name: "Revoke…" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke" }));
    expect(await screen.findByText(/may or may not have\s+happened/)).toBeInTheDocument();
  });

  it("after a lost answer, blocks another revoke until the operator says what happened, across a remount", async () => {
    const lost = settingsHub({
      [`POST ${REVOKE}`]: () => {
        throw new TypeError("network connection lost");
      },
    });
    const first = renderRoute("/settings/credentials", { hub: lost });
    await findPageHeading("Credentials");
    fireEvent.click(await screen.findByLabelText("Every credential on one machine"));
    fireEvent.change(screen.getByLabelText(/Machine name/), { target: { value: "desktop-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Revoke…" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke" }));
    expect(await screen.findByText(/We could not confirm whether the credentials were revoked/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revoke…" })).toBeDisabled();
    // Only the kind and the time are kept, never the machine name.
    const all = Object.keys(sessionStorage).map((k) => `${k}=${sessionStorage.getItem(k)}`).join("\n");
    expect(all).toContain("node");
    expect(all).not.toContain("desktop-1");
    first.unmount();
    vi.unstubAllGlobals();
    const second = renderRoute("/settings/credentials", { hub: settingsHub() });
    expect(await screen.findByText(/We could not confirm whether the credentials were revoked/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revoke…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "It was not revoked, let me try again" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Revoke…" })).toBeEnabled());
    expect(screen.queryByText(/We could not confirm/)).not.toBeInTheDocument();
    expect(posts(second.hub.calls, REVOKE)).toHaveLength(0);
  });

  it("'It was revoked' clears the uncertainty too", async () => {
    fixtureEpoch().storage.write(UNRESOLVED_KEY, { identity: "operator", at: Date.now(), kind: "credential_id" });
    renderRoute("/settings/credentials", { hub: settingsHub() });
    fireEvent.click(await screen.findByRole("button", { name: "It was revoked" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Revoke…" })).toBeEnabled());
    expect(fixtureEpoch().storage.read(UNRESOLVED_KEY, null)).toBeNull();
  });

  it("keeps revoke input in memory only: nothing reaches session or local storage", async () => {
    // A token-shaped value (the hub's `uas_` plus 43 characters), built at run time so no literal is committed.
    const SECRET = ["uas", "Q".repeat(43)].join("_");
    const stored = () => [...Object.keys(sessionStorage).map((k) => `${k}=${sessionStorage.getItem(k)}`), ...Object.keys(localStorage).map((k) => `${k}=${localStorage.getItem(k)}`)].join("\n");
    const first = renderRoute("/settings/credentials", { hub: settingsHub({ [`POST ${REVOKE}`]: { body: { revoked: [CREDENTIAL_ID], count: 1 } } }) });
    await findPageHeading("Credentials");
    const field = await screen.findByLabelText(/Credential id/);
    // A pasted bearer is rejected and never stored.
    fireEvent.change(field, { target: { value: `Bearer ${SECRET}` } });
    fireEvent.click(screen.getByRole("button", { name: "Revoke…" }));
    expect(await screen.findByText(/This is not a credential id/)).toBeInTheDocument();
    // A valid id, through the confirm step, is not stored either.
    fireEvent.change(field, { target: { value: CREDENTIAL_ID } });
    fireEvent.click(screen.getByRole("button", { name: "Revoke…" }));
    await screen.findByRole("dialog");
    await new Promise((r) => setTimeout(r, 700));
    expect(stored()).not.toContain(SECRET);
    expect(stored()).not.toContain(CREDENTIAL_ID);
    expect(stored()).not.toContain("credential_id");
    first.unmount();
    expect(stored()).not.toContain(CREDENTIAL_ID);
    expect(window.location.href).not.toContain(CREDENTIAL_ID);
    // Nothing comes back after a remount.
    vi.unstubAllGlobals();
    renderRoute("/settings/credentials", { hub: settingsHub() });
    expect(await screen.findByLabelText(/Credential id/)).toHaveValue("");
  });

  it("checks selectors with the hub's own patterns", () => {
    expect(selectorProblem("credential_id", CREDENTIAL_ID)).toBeNull();
    expect(selectorProblem("item_id", "nd-xyz")).toMatch(/not a session run id/);
    expect(selectorProblem("node", "desktop-1")).toBeNull();
    expect(selectorProblem("node", "")).toMatch(/Enter the machine name/);
    expect(providerStatuses(POOL.credentials).map((p) => [p.provider, p.present])).toEqual([
      ["openai", false],
      ["anthropic", true],
    ]);
  });
});

describe("settings: audit", () => {
  it("applies filters to the URL and the hub query", async () => {
    const urls: string[] = [];
    const { router } = renderRoute("/settings/audit", {
      hub: settingsHub({
        "GET /api/evidence": ({ url }) => {
          urls.push(url);
          return { body: manyRecords(1) };
        },
      }),
    });
    await findPageHeading("Audit");
    fireEvent.change(await screen.findByLabelText("Task number"), { target: { value: "2493" } });
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "Desktop_Agent1" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(router.state.location.search).toBe("?task_id=2493&agent=Desktop_Agent1"));
    await waitFor(() => expect(urls.some((u) => u.includes("task_id=2493") && u.includes("agent=Desktop_Agent1"))).toBe(true));
  });

  it("refuses a non-numeric task and focuses it", async () => {
    renderRoute("/settings/audit", { hub: settingsHub() });
    const field = await screen.findByLabelText("Task number");
    fireEvent.change(field, { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    expect(await screen.findByText("A task number has digits only.")).toBeInTheDocument();
    expect(document.activeElement).toBe(field);
  });

  it("checks the chain again and says the result plainly", async () => {
    let n = 0;
    renderRoute("/settings/audit", {
      hub: settingsHub({
        "GET /api/evidence": () => {
          n += 1;
          return { body: n === 1 ? manyRecords(1) : EVIDENCE_TAMPERED };
        },
      }),
    });
    expect(await screen.findByText("The ledger is intact")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check the ledger now" }));
    expect(await screen.findByText("The ledger has been changed")).toBeInTheDocument();
    expect(screen.getByText(/Checked again at/)).toBeInTheDocument();
  });

  it("a failed re-check never reads as intact or as checked just now", async () => {
    let n = 0;
    renderRoute("/settings/audit", {
      hub: settingsHub({
        "GET /api/evidence": () => {
          n += 1;
          return n === 1 ? { body: manyRecords(1) } : { status: 503, body: { error: "database is locked" } };
        },
      }),
    });
    expect(await screen.findByText("The ledger is intact")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check the ledger now" }));
    expect(await screen.findByText(/We could not re-check the ledger; the result below is from/)).toBeInTheDocument();
    expect(screen.queryByText(/Checked again just now/)).not.toBeInTheDocument();
    expect(screen.queryByText("The ledger is intact")).not.toBeInTheDocument();
    expect(screen.getByText(/Last known result/)).toBeInTheDocument();
  });

  it("a successful re-check shows its real time", async () => {
    renderRoute("/settings/audit", { hub: settingsHub({ "GET /api/evidence": { body: manyRecords(1) } }) });
    await screen.findByText("The ledger is intact");
    fireEvent.click(screen.getByRole("button", { name: "Check the ledger now" }));
    expect(await screen.findByText(/^Checked again at \d/)).toBeInTheDocument();
  });

  it("says when the list is capped and offers the rest", async () => {
    const { router } = renderRoute("/settings/audit", { hub: settingsHub({ "GET /api/evidence": { body: manyRecords(50) } }) });
    expect(await screen.findByText("Showing the first 50. There may be more.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show up to 500" }));
    await waitFor(() => expect(router.state.location.search).toBe("?limit=500"));
  });

  it("states the export's real limit and never offers it for the rest", async () => {
    const { unmount } = renderRoute("/settings/audit", { hub: settingsHub({ "GET /api/evidence": { body: manyRecords(1) } }) });
    const exportSection = await screen.findByRole("region", { name: "Export" });
    expect(exportSection).toHaveTextContent(/at most 50 records/);
    expect(exportSection).toHaveTextContent(/not a complete copy/);
    expect(exportSection).not.toHaveTextContent(/whole ledger/);
    unmount();
    vi.unstubAllGlobals();
    renderRoute("/settings/audit?limit=500", { hub: settingsHub({ "GET /api/evidence": { body: manyRecords(500) } }) });
    const records = await screen.findByRole("region", { name: "Records" }, { timeout: 20_000 });
    expect(records).toHaveTextContent(/at most 500 at once/);
    expect(records).toHaveTextContent(/Narrow the dates or other filters/);
    expect(records).not.toHaveTextContent(/export/i);
    expect(screen.getByRole("region", { name: "Export" })).toHaveTextContent(/at most 500 records/);
  });

  it("downloads the export from the hub route with the active filters", async () => {
    const created: string[] = [];
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      created.push(this.download);
    });
    const { hub } = renderRoute("/settings/audit?agent=Desktop_Agent1", {
      hub: settingsHub({
        "GET /api/evidence": ({ url }) => (url.includes("format=csv") ? { text: "task_id,agent\n1,x\n" } : { body: manyRecords(1) }),
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Export as CSV" }));
    expect(await screen.findByText(/Downloaded evidence-report.csv/)).toBeInTheDocument();
    const exportCall = hub.fn.mock.calls.map((c) => String(c[0])).find((u) => u.includes("format=csv"));
    expect(exportCall).toBe("/api/evidence?agent=Desktop_Agent1&format=csv");
    expect(created).toEqual(["evidence-report.csv"]);
    click.mockRestore();
  });

  it("a download cut off mid-body clears the busy state and explains it", async () => {
    renderRoute("/settings/audit", { hub: settingsHub({ "GET /api/evidence": { body: manyRecords(1) } }) });
    await screen.findByText("The ledger is intact");
    const hubFetch = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("format=csv")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers({ "content-disposition": 'attachment; filename="evidence-report.csv"' }),
          blob: () => Promise.reject(new TypeError("network error while reading the body")),
          text: () => Promise.reject(new TypeError("network error while reading the body")),
        } as unknown as Response;
      }
      return hubFetch(input, init);
    });
    fireEvent.click(screen.getByRole("button", { name: "Export as CSV" }));
    expect(await screen.findByText(/The hub could not be reached. Check your connection and try again./)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Export as CSV" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Export as PDF" })).toBeEnabled();
    expect(screen.getByText(/reading the body/).closest("details")).not.toBeNull();
  });

  it("explains a PDF the hub cannot make", async () => {
    renderRoute("/settings/audit", {
      hub: settingsHub({
        "GET /api/evidence": ({ url }) => (url.includes("format=pdf") ? { status: 503, body: { error: "reportlab not installed" } } : { body: manyRecords(1) }),
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Export as PDF" }));
    expect(await screen.findByText(/cannot make a PDF right now/)).toBeInTheDocument();
  });

  it("names the chain states", () => {
    expect(chainVerdict({ status: "valid", verified: true, head_hash: "x", total_verified: 3, unhashed_skipped: 0 }).tone).toBe("ok");
    expect(chainVerdict({ status: "tampered", verified: false, head_hash: null, total_verified: 1, unhashed_skipped: 0 }).tone).toBe("danger");
    // Valid but not verified, or an unknown status, is never intact.
    expect(chainVerdict({ status: "valid", verified: false, head_hash: "x", total_verified: 3, unhashed_skipped: 0 }).tone).not.toBe("ok");
    expect(chainVerdict({ status: "partial", verified: true, head_hash: "x", total_verified: 3, unhashed_skipped: 0 }).tone).not.toBe("ok");
    expect(chainVerdict({ status: "empty", verified: true, head_hash: null, total_verified: 0, unhashed_skipped: 0 }).title).toMatch(/no entries/);
    expect(evidenceQuery({ ...EMPTY_EVIDENCE_FILTERS, pr: " 1510 " }, "pdf")).toBe("?pr=1510&format=pdf");
  });
});

describe("settings: provenance", () => {
  it("shows the graph summary and puts the query in the URL", async () => {
    const urls: string[] = [];
    const { router } = renderRoute("/settings/audit/provenance", {
      hub: settingsHub({
        "GET /api/platform/provenance-graph": ({ url }) => {
          urls.push(url);
          return { body: PROVENANCE };
        },
      }),
    });
    await findPageHeading("Provenance");
    expect(await screen.findByText("4 items and 3 links")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Show"), { target: { value: "by_human" } });
    fireEvent.change(await screen.findByLabelText("Person"), { target: { value: "operator" } });
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    await waitFor(() => expect(router.state.location.search).toBe("?query=by_human&human=operator"));
    await waitFor(() => expect(urls.some((u) => u.includes("query=by_human") && u.includes("human=operator"))).toBe(true));
  });

  it("asks for a task when a release query has none", async () => {
    renderRoute("/settings/audit/provenance?query=by_release", { hub: settingsHub() });
    await findPageHeading("Provenance");
    fireEvent.click(await screen.findByRole("button", { name: "Show" }));
    expect(await screen.findByText("Enter a task number, or several.")).toBeInTheDocument();
    expect(document.activeElement).toBe(screen.getByLabelText("Task number"));
  });
});

describe("settings: appearance", () => {
  it("sets the theme the way the legacy toggle persists it", async () => {
    renderRoute("/settings/appearance", { hub: settingsHub() });
    await findPageHeading("Appearance");
    fireEvent.click(screen.getByLabelText("High contrast"));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("high-contrast"));
    expect(localStorage.getItem("ua-web-theme")).toBe("high-contrast");
    fireEvent.click(screen.getByLabelText("Light"));
    await waitFor(() => expect(localStorage.getItem("ua-web-theme")).toBe("light"));
  });

  it("follows the device for System and remembers the choice", async () => {
    renderRoute("/settings/appearance", { hub: settingsHub() });
    await findPageHeading("Appearance");
    fireEvent.click(screen.getByLabelText("Same as this device"));
    await waitFor(() => expect(fixtureEpoch().sharedStorage.read<{ mode?: string } | null>(THEME_PREF_KEY, null)?.mode).toBe("system"));
    expect(screen.getByLabelText("Same as this device")).toBeChecked();
    expect(screen.getByText(/from your device's setting/)).toBeInTheDocument();
  });

  it("stores density and applies it", async () => {
    renderRoute("/settings/appearance", { hub: settingsHub() });
    await findPageHeading("Appearance");
    fireEvent.click(screen.getByLabelText("Compact"));
    expect(document.documentElement.dataset.density).toBe("compact");
    expect(fixtureEpoch().sharedStorage.read(DENSITY_KEY, null)).toBe("compact");
    fireEvent.click(screen.getByLabelText("Comfortable"));
    expect(document.documentElement.dataset.density).toBeUndefined();
  });
});
