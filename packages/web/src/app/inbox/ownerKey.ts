import { useRef } from "react";
import { useSignedInIdentity } from "../lib/identity";

/**
 * A key segment that changes only when the signed-in person changes from
 * one known identity to another (or signs out). The identity resolving for
 * the first time does not change it, so a form is not remounted under the
 * person while they type; a different person always gets fresh state.
 */
export function useOwnerKey(): string {
  const identity = useSignedInIdentity();
  const known = useRef<string | null | undefined>(undefined);
  const generation = useRef(0);
  if (identity !== undefined) {
    if (known.current !== undefined && known.current !== identity) generation.current += 1;
    known.current = identity;
  }
  return `owner-${generation.current}`;
}
