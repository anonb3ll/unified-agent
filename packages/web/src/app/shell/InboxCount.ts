import { useQuery } from "@tanstack/react-query";
import { ATTENTION_QUERY_KEY, fetchAttention } from "../../lib/api/attention";
import { deriveTruthState } from "../../lib/ui/truthState";
import { UNKNOWN_COUNT } from "../lib/queries";

/**
 * Items waiting on a person, from the typed inbox (`needs_operator.total`).
 * The number shows only from a complete read (no top-level and no nested
 * `needs_operator.errors`); anything else is "–".
 */
export function useInboxCount(): { text: string; known: boolean; count: number | null } {
  const query = useQuery({
    queryKey: ATTENTION_QUERY_KEY,
    queryFn: ({ signal }) => fetchAttention(signal),
    retry: 1,
  });
  const truth = deriveTruthState([query]);
  const operator = query.data?.needs_operator;
  const total = operator?.total;
  // The hub reports producers that failed inside the typed inbox separately
  // (`needs_operator.errors`): any failure, or no list at all, means the
  // count is incomplete, so no number is shown (not even 0).
  const complete = Array.isArray(operator?.errors) && operator.errors.length === 0;
  const known = (truth.state === "ready" || truth.state === "empty") && complete && typeof total === "number";
  if (!known) return { text: UNKNOWN_COUNT, known: false, count: null };
  return { text: total > 99 ? "99+" : String(total), known: true, count: total };
}
