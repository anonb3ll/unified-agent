import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findPageHeading, renderRoute } from "../support/render";
import { UNRESOLVED_KEY } from "../../../src/app/settings/CredentialsPage";
import { fixtureEpoch } from "../support/epoch";
import { CREDENTIAL_ID } from "./fixtures";
import { settingsHub } from "./hub";

/**
 * The foundation's `cancelled` write outcome (the client stopped a write
 * that no longer belongs to the signed-in person). `useHubWrite` is
 * replaced so the outcome can be produced directly.
 */
const next: { outcome: unknown } = { outcome: null };

vi.mock("../../../src/app/lib/mutations", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../src/app/lib/mutations")>();
  return {
    ...actual,
    useHubWrite: () => ({
      run: async () => next.outcome,
      state: { status: "idle" },
      reset: () => {},
      isRunning: false,
    }),
  };
});

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
});

async function revoke() {
  await findPageHeading("Credentials");
  fireEvent.change(await screen.findByLabelText(/Credential id/), { target: { value: CREDENTIAL_ID } });
  fireEvent.click(screen.getByRole("button", { name: "Revoke…" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
}

describe("settings: a cancelled revoke", () => {
  it("that had left is an unknown outcome until the operator settles it", async () => {
    next.outcome = { kind: "cancelled", reason: "owner_changed", dispatched: true, message: "x" };
    renderRoute("/settings/credentials", { hub: settingsHub() });
    await revoke();
    expect(await screen.findByText(/We could not confirm whether the credentials were revoked/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revoke…" })).toBeDisabled();
    expect(screen.queryByText(/credential revoked/)).not.toBeInTheDocument();
  });

  it("that never left shows nothing, keeps the form and does not block", async () => {
    next.outcome = { kind: "cancelled", reason: "signed_out", dispatched: false, message: "x" };
    renderRoute("/settings/credentials", { hub: settingsHub() });
    await revoke();
    expect(screen.queryByText(/We could not confirm/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Credential id/)).toHaveValue(CREDENTIAL_ID);
    expect(screen.getByRole("button", { name: "Revoke…" })).toBeEnabled();
    expect(fixtureEpoch().storage.read(UNRESOLVED_KEY, null)).toBeNull();
  });
});
