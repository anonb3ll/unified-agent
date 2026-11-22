import type { ReactNode } from "react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { onTestFinished } from "vitest";
import { WHOAMI_QUERY_KEY, type WhoamiPayload } from "../../../src/lib/api/auth";
import { createEpochHost, OwnerEpochProvider, type EpochHost, type OwnerEpoch } from "../../../src/app/lib/epoch";
import { whoamiFixture } from "./fixtures";
import { testQueryClient } from "./render";

export type EpochSetup = {
  /** The root (identity-only) client: set whoami here to change the owner. */
  root: QueryClient;
  host: EpochHost;
  /** The current epoch (re-checked). */
  readonly epoch: OwnerEpoch;
  wrapper: (props: { children: ReactNode }) => ReactNode;
};

/**
 * A root client with whoami seeded (pass null for none), an epoch host over
 * it, connected for the test, and a wrapper that renders under it.
 */
export function epochSetup(
  whoami: WhoamiPayload | Record<string, unknown> | null = whoamiFixture(),
  root: QueryClient = testQueryClient(),
  options: { connect?: boolean } = {},
): EpochSetup {
  if (whoami) root.setQueryData(WHOAMI_QUERY_KEY, whoami);
  const host = createEpochHost(root);
  // Connected (eager revocation, the cross-tab channel) for the test; without
  // it the epoch still re-checks whoami and the auth generation when read.
  if (options.connect !== false) onTestFinished(host.connect());
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={root}>
      <OwnerEpochProvider host={host}>{children}</OwnerEpochProvider>
    </QueryClientProvider>
  );
  return {
    root,
    host,
    get epoch() {
      return host.current;
    },
    wrapper,
  };
}

/**
 * An epoch for this owner outside any rendered tree, for seeding and reading
 * owner storage (drafts, stored writes) around a page. Owner storage is keyed
 * by the owner, so it sees what a page signed in as the same owner sees.
 */
export function fixtureEpoch(whoami: WhoamiPayload | Record<string, unknown> = whoamiFixture()): OwnerEpoch {
  return epochSetup(whoami, undefined, { connect: false }).epoch;
}
