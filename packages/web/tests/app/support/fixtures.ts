import type { ApprovalsPayload } from "../../../src/lib/api/approvals";
import type { WhoamiPayload } from "../../../src/lib/api/auth";
import type { AttentionPayload } from "../../../src/lib/attention/types";

/**
 * Typed hub responses for Control Center v2 tests. Each fixture is checked
 * with `satisfies` against the real response type (and
 * `fixtures-typecheck.test.ts` compiles this file), so a fixture that drifts
 * from the hub's shape fails. Deliberately malformed responses live in
 * `malformed.ts`, named as such.
 */
export const OPERATOR = "operator";

export function whoamiFixture(overrides: Partial<WhoamiPayload> = {}): WhoamiPayload {
  return {
    hub_version: "9.9.9",
    api_version: 1,
    agent: OPERATOR,
    via: "cookie",
    csrf: "csrf-1",
    enforce_auth: true,
    tokens_configured: true,
    control_enabled: true,
    control_disabled_reason: null,
    ...overrides,
  } satisfies WhoamiPayload;
}

export function approvalsFixture(overrides: Partial<ApprovalsPayload> = {}): ApprovalsPayload {
  return { pending: [], approved_unconsumed: [], expired_passes: [], ...overrides } satisfies ApprovalsPayload;
}

/**
 * GET /api/attention. `waiting` is the typed inbox's `needs_operator.total`;
 * `null` omits `needs_operator` (an older hub). `operatorErrors` are the
 * producers that failed inside `needs_operator` (an incomplete count).
 */
export function attentionFixture(
  waiting: number | null = 2,
  options: { operatorErrors?: string[] } = {},
): AttentionPayload {
  const base = { items: [], informational: [], counts: {}, total: waiting ?? 0, errors: [] } satisfies AttentionPayload;
  if (waiting === null) return base;
  return {
    ...base,
    needs_operator: { rows: [], counts: {}, total: waiting, errors: options.operatorErrors ?? [] },
  } satisfies AttentionPayload;
}
