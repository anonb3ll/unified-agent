import { useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchWhoami, HttpAuthError, WHOAMI_QUERY_KEY } from "../../lib/api/auth";
import { useRootQueryClient } from "./epoch";

/**
 * The signed-in identity as a draft key or a form needs it:
 * - `loading`: whoami has not answered yet (`identity` is undefined);
 * - `ready`: `identity` is the label, or null when signed out or the
 *   session has no agent identity;
 * - `failed`: whoami failed after its retries, or answered 403. `identity`
 *   is undefined and `retry` reads it again.
 */
export type IdentityState = {
  status: "loading" | "ready" | "failed";
  identity: string | null | undefined;
  retry: () => void;
};

/**
 * The whoami query, always in the root (identity-only) client, never in an
 * owner epoch's client: who is signed in decides the epoch, not the reverse.
 */
export function useWhoami() {
  return useQuery(
    {
      queryKey: WHOAMI_QUERY_KEY,
      queryFn: ({ signal }) => fetchWhoami(signal),
      retry: (failureCount, error) =>
        !(error instanceof HttpAuthError && (error.status === 401 || error.status === 403)) && failureCount < 2,
    },
    useRootQueryClient(),
  );
}

export function useIdentity(): IdentityState {
  const whoami = useWhoami();
  const { refetch } = whoami;
  const retry = useCallback(() => void refetch(), [refetch]);
  if (whoami.isSuccess) return { status: "ready", identity: whoami.data?.agent ?? null, retry };
  if (whoami.isError) {
    if (whoami.error instanceof HttpAuthError && whoami.error.status === 401) return { status: "ready", identity: null, retry };
    return { status: "failed", identity: undefined, retry };
  }
  return { status: "loading", identity: undefined, retry };
}

/**
 * The identity alone: `undefined` while loading or failed, `null` when signed
 * out or without an agent identity, else the label. Prefer passing
 * `useIdentity()` to `useDraft`, which also tells a form that the read failed.
 */
export function useSignedInIdentity(): string | null | undefined {
  return useIdentity().identity;
}
