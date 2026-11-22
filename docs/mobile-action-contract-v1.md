# Mobile action contract, v1 (`mobile-action.v1`)

Status: **first mutating slice — one safe action (`acknowledge`)**

Produced for task **#779** (P2.F6 Watch slice 2), 2026-09-11. This is the
"separate versioned mutation envelope" that
[the projection contract §6](mobile-projection-contract-v1.md#6-action-and-deep-link-contract)
requires before any action leaves the wrist. It does **not** change
`mobile.v1`, which stays read-only.

## 1. Decision

The Watch never mutates hub state. It emits a bounded **action intent** —
an allowlisted verb plus references copied verbatim from a projection it has
already validated. The authenticated iPhone relay turns that intent into a
`mobile-action.v1` request and performs a fresh authenticated hub call. The
hub is the only place policy, safety, and audit are evaluated.

Slice-1 allowlist: **`acknowledge`** only. `acknowledge` is a TTL'd
operator-visible suppression of one attention item — idempotent, reversible
(it expires; a desktop "clear" is also possible), and it touches no task,
approval, cycle, executor, worktree, merge, or deploy state. A future
`redirect` / `dispatch` verb slots into the same envelope but routes through
the approval queue, not auto-apply.

## 2. The seam — what crosses each boundary

```text
Watch ──intent──▶ iPhone relay ──mobile-action.v1 request──▶ hub ──result──▶ iPhone ──fresh projection──▶ Watch
```

| Boundary | Crosses | Never crosses |
|---|---|---|
| Watch → iPhone (`WCSession.sendMessage`, reachable only) | `{action, item_ref, projection_id}` — verbatim from a validated `mobile.v1` projection | bearer token, cookie, CSRF, approval body, action arguments, a claim that the action is authorized, any free text |
| iPhone → hub (`POST /api/mobile/v1/action`) | `mobile-action.v1` request envelope + the iPhone's existing hub auth (bearer or cookie+`x-ua-csrf`) | Watch UI state as authority; the projection payload itself |
| hub → iPhone | `mobile-action-result.v1` — `{action_id, status, reason?, audit_ref}` | traceback, task notes, raw attention detail |
| iPhone → Watch (`updateApplicationContext`) | a fresh `mobile.v1` projection (acked item now filtered out) | the result envelope verbatim (Watch only re-renders state) |

The `projection_id` is **provenance only**. The hub resolves `item_ref`
against a *fresh* `attention_payload`, never against the projection the Watch
was holding. A stale or replayed projection cannot authorize anything.

## 3. Request envelope — `mobile-action.v1`

```json
{
  "schema_version": "mobile-action.v1",
  "action_id": "ma_1757548800_9f3c1a2b",
  "action": "acknowledge",
  "item_ref": "attention:blocked_task:1234",
  "projection_id": "mp_1757548790_0a1b2c3d",
  "issued_at": "2026-09-11T00:40:00Z"
}
```

| Field | Rule |
|---|---|
| `schema_version` | exactly `mobile-action.v1` |
| `action_id` | `^ma_[A-Za-z0-9._-]{1,61}$` — client nonce; the hub's replay key |
| `action` | allowlist: `{"acknowledge"}` (v1). Unknown verb → `rejected` |
| `item_ref` | `^attention:[a-z0-9_]+:[A-Za-z0-9._-]{1,96}$` — same grammar as `mobile.v1` `item_ref` |
| `projection_id` | `^mp_[A-Za-z0-9._-]{1,61}$` — provenance only |
| `issued_at` | ISO-8601 UTC, timezone-bearing; rejected if older than the replay window (default 10 min) or in the future by more than 60 s |

Exact-keys (no unexpected fields), 2 KiB max, fail closed on any violation —
same posture as `mobile_v1.parse_mobile_projection`.

## 4. Result envelope — `mobile-action-result.v1`

```json
{
  "schema_version": "mobile-action-result.v1",
  "action_id": "ma_1757548800_9f3c1a2b",
  "status": "applied",
  "reason": null,
  "audit_ref": "01J...ULID",
  "acknowledged_until": "2026-09-11T04:40:00Z"
}
```

`status` ∈ `applied` (ack recorded), `noop` (already acked, or the item no
longer resolves against fresh attention — still 200, idempotent), `rejected`
(`reason` ∈ `invalid_envelope`, `unknown_action`, `stale_request` → HTTP 400;
`policy_denied` → HTTP 403). `audit_ref` is the `event_log` ULID for `applied`
(null for `noop` when nothing was written, and for every `rejected`).

## 5. Hub processing

1. Parse + validate the envelope (`mobile_action_v1.parse_mobile_action`). Any
   failure → `rejected: invalid_envelope`, no side effect.
2. `issued_at` inside the replay window, else `rejected: stale_request`.
3. `action` in the allowlist, else `rejected: unknown_action`.
4. Resolve `item_ref` against a **fresh** `attention_payload()`. Absent →
   `status: noop` (the wrist was looking at a resolved item; not an error).
5. `policy.evaluate_policy("mobile_action.acknowledge", {...})`. `acknowledge`
   is safe and normally passes; a `PolicyViolation` → `rejected: policy_denied`.
6. `events.record_event("mobile.attention.ack", {item_ref, action, acked_by,
   acknowledged_until}, originator=<iPhone identity>, dedupe_key=action_id)`.
   `dedupe_key` makes a retried `action_id` return the first ULID with no
   second row → natural idempotency / replay protection.
7. Return the result envelope.

No offline queue. The iPhone retries a transport failure by re-issuing the
**same** `action_id` (safe by dedupe) or by dropping it.

## 6. Acknowledge semantics

- Writes one `mobile.attention.ack` event with `acknowledged_until = now + TTL`
  (default `UA_ATTENTION_ACK_TTL_S`, 14400 s / 4 h).
- `attention_payload()` reads recent `mobile.attention.ack` events and drops
  any item whose `item_ref` has a non-expired ack. Re-acking extends the TTL.
- Suppression is operator-visible only. The task stays blocked, the approval
  stays pending, the cycle stays stopped — `acknowledge` says "I've seen it",
  not "it's handled".
- Reversible: acks expire; a desktop control to clear one early is a follow-up
  (not in this slice).

## 7. Read-only fallback (unchanged path)

If the iPhone is unreachable, or the hub returns `rejected`, or any transport
error occurs: the Watch keeps rendering its last validated `mobile.v1`
projection **unchanged**, shows a transient "Couldn't acknowledge — open
iPhone" affordance, and does not queue or retry automatically. Nothing in the
read-only slice (#417/#418) changes; `acknowledge` is strictly additive.

## 8. Threat / failure matrix

| Threat / failure | Required behavior |
|---|---|
| Replayed request (same `action_id`) | `dedupe_key` returns the first ULID; no second event; `status: noop`/`applied` idempotent |
| Stale projection used as basis | `item_ref` resolved against fresh attention; unknown → `noop`; old `projection_id` carries no authority |
| Forged intent without auth | `POST /api/mobile/v1/action` requires the iPhone's authenticated identity (same guard as other mutating `/api` routes); no identity → 401 |
| CSRF (cookie-backed caller) | `x-ua-csrf` required and verified, same as `/api/action/*` |
| Unknown / future verb | `rejected: unknown_action`; envelope allowlist is closed |
| `issued_at` far in past/future | `rejected: stale_request` |
| Oversized / malformed envelope | `rejected: invalid_envelope`, fail closed, no side effect |
| Policy denies | `rejected: policy_denied`; audited as a denied attempt via the policy path |
| Hub unavailable to iPhone | Watch shows last projection + transient failure note; no queue |

## 9. Acceptance

- [ ] `mobile.v1` projection is byte-for-byte unchanged; read-only slice tests still pass.
- [ ] `POST /api/mobile/v1/action` is the only new route; it is authenticated + CSRF-guarded and appears in the route-inventory freeze as a mutating endpoint.
- [ ] Envelope validator fails closed on schema, key set, size, grammar, and replay-window violations, with deterministic `rejected` reasons.
- [ ] `acknowledge` records exactly one `mobile.attention.ack` event; a retried `action_id` records none.
- [ ] `attention_payload()` suppresses an acked item until its TTL expires and resurfaces it after.
- [ ] No credential, CSRF value, approval body, or action argument ever appears in a Watch-bound payload or a `mobile.attention.ack` event.
- [ ] Simulator slice: `swift test` in `native/UnifiedAgentMobile` exercises intent → request → result decode and the unchanged read-only fallback. **(Mac-only — no Swift/Xcode toolchain on the Linux hub host; runs on the Mac.)**
- [ ] Physical Apple Watch delivery + WCSession reachability behavior — **The operator-only** (device gap, tracked with #419).
