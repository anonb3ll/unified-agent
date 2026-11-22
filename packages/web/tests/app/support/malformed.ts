import type { AttentionPayload } from "../../../src/lib/attention/types";

/**
 * Deliberately malformed hub responses, for tests that prove the UI does not
 * trust them. Each is cast on purpose and named `malformed...`; well-formed
 * fixtures live in `fixtures.ts` and are type-checked.
 */

/** `needs_operator` without its required `errors` list: the count is not known to be complete. */
export function malformedAttentionWithoutOperatorErrors(total: number): AttentionPayload {
  return {
    items: [],
    informational: [],
    counts: {},
    total,
    errors: [],
    needs_operator: { rows: [], counts: {}, total } as unknown as AttentionPayload["needs_operator"],
  };
}
