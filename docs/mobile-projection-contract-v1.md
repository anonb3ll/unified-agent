# Mobile projection contract, v1

Status: **contract proposal; no mobile route or transport added**

Produced for task **#412**, 2026-08-16. This contract covers bounded data for
Apple Watch, iPhone, and a future Browser mobile client. It complements the
existing Browser browser-cognition contract; it does not replace or widen it.

## 1. Decision

Mobile clients consume an allowlisted projection with explicit freshness and
provenance. They do not consume raw web-control payloads, task notes, browser
content, credentials, or approval request bodies.

The iPhone companion is the authenticated gateway. The Watch is a low-bandwidth
display and navigation surface. A mobile projection is never authorization:
mutations must return to an authenticated hub/web action and pass the existing
identity, profile, CSRF/session, policy, approval, and audit gates.

No new service or transport is justified by the current evidence. Task **#415**
must choose the iPhone/Watch relay and notification mechanism before
implementation.

## 2. Existing contracts and compatibility

| Producer or source | Current contract | Reuse decision | Gap |
|---|---|---|---|
| unified-agent attention | `GET /api/attention`: kind-tagged items, severity, age, source host/origin, reason/title | Reuse as a server-side source; project only aggregates and safe titles | No mobile envelope, expiry, or notification-safe identifier |
| unified-agent digest | `GET /api/digest`: generated timestamp, grouped attention, errors, omitted kinds | Reuse for iPhone detail and aggregation | Raw item reasons/titles need mobile allowlisting |
| unified-agent health | `GET /api/health`: doctor result and cache age | Reuse server-side | Doctor checks and process details are too verbose for mobile |
| unified-agent identity | `GET /api/whoami`: verified identity and capability discovery | Keep on iPhone/web only | Watch must not receive session/CSRF material |
| Browser S2 projection | `contract_version`, kind, opaque record id, provenance, allowlisted scalar payload | Keep as a separate browser-to-control-plane contract | Its free-text focus fields and browser origin semantics are not Watch payloads |
| notifications/deep links | No verified mobile route in current unified-agent source | Add only as a bounded identifier in this proposal | #415 must define delivery, replay, and auth behavior |

Current Browser v1 is implemented in
`~/dev/browser/src/lib/projection/contract-v1.ts` and independently validated
by `src/unified_agent/projection/browser_v1.py`. It intentionally carries
`origin_machine`, `origin_surface`, `captured_at`, `projected_at`,
`consent_basis`, and `trust_tier`. Mobile must preserve the same provenance
principle but must not copy the browser contract's `focus_entity`,
`state_digest`, URL-derived reference, or other user-authored content into a
notification or Watch payload.

## 3. Versioned envelope

The following shape is the proposed `mobile.v1` wire contract. It is a fixture
for #416 until a validator and endpoint are implemented.

```json
{
  "schema_version": "mobile.v1",
  "projection_id": "mp_01J...",
  "generated_at": "2026-08-16T20:00:00Z",
  "expires_at": "2026-08-16T20:05:00Z",
  "freshness": "live",
  "source": {
    "source_host": "desktop",
    "hub_origin": "local",
    "surface": "unified-agent"
  },
  "attention": {
    "total": 2,
    "highest_severity": "high",
    "items": [
      {
        "item_ref": "attention:approval:123",
        "kind": "approval",
        "severity": "high",
        "age_s": 42,
        "safe_title": "Human decision needed",
        "deep_link_ref": "ua://control-center/attention/approval/123"
      }
    ]
  },
  "health": {
    "state": "healthy",
    "safe_summary": "Service available"
  },
  "notification": {
    "event_ref": "attention:approval:123",
    "safe_title": "Unified-agent needs attention",
    "deep_link_ref": "ua://control-center/attention/approval/123"
  },
  "error": null,
  "fallback": {
    "surface": "iphone",
    "deep_link_ref": "ua://control-center"
  }
}
```

### Required field rules

| Field | Rule |
|---|---|
| `schema_version` | Exact `mobile.v1`; incompatible changes require a major version |
| `projection_id` | Opaque, non-secret, unique per projection; never a credential |
| `generated_at` | UTC timestamp from the authenticated producer |
| `expires_at` | UTC timestamp; must be later than `generated_at` |
| `freshness` | One of `live`, `stale`, `offline`, `error` |
| `source.*` | Bounded provenance labels; not authority claims |
| `attention` | Bounded aggregate and zero or more allowlisted item records |
| `health.state` | `healthy`, `degraded`, `unavailable`, or `unknown` |
| `error` | Null or a bounded, generic error code; never a traceback |
| `fallback.deep_link_ref` | Opaque navigation reference; never embeds a token or raw URL |

The initial validator must reject unknown top-level fields, nested payloads not
listed here, invalid timestamps, unbounded strings, and any secret-shaped or
URL-shaped value in notification text. Maximum encoded size is **8 KiB** for a
Watch projection and **32 KiB** for an iPhone projection. These limits are
implemented by [`src/unified_agent/projection/mobile_v1.py`](../src/unified_agent/projection/mobile_v1.py)
and pinned by [`tests/test_mobile_projection.py`](../tests/test_mobile_projection.py).

## 4. Privacy and allowlist

Allowed attention item fields are `item_ref`, `kind`, `severity`, `age_s`,
`safe_title`, and `deep_link_ref`. `safe_title` comes from a fixed mapping such
as `approval -> Human decision needed`; it is never copied from `reason`,
`title`, task notes, page titles, URLs, DOM text, or user-authored content.

Forbidden everywhere in a mobile projection or notification:

- raw page content, DOM text, browsing history, full URLs, and excerpts;
- task notes, approval request payloads, doctor check details, stack traces,
  filesystem paths, and unbounded user text;
- bearer tokens, session cookies, CSRF tokens, API keys, private keys, or device
  credentials;
- action arguments or a claim that an action is already authorized.

Notification text is generic: “Unified-agent needs attention.” The `notification`
object contains only an event reference, that fixed title, and a bounded deep
link. The deep link only identifies the authenticated destination; the
destination fetches current data after the iPhone re-establishes auth.

## 5. Freshness, errors, and provenance

- `live` means the iPhone received the projection before `expires_at` and the
  authenticated source call succeeded.
- `stale` means cached data exists but `expires_at` passed. It may be displayed
  with a stale marker; it cannot authorize or queue an action.
- `offline` means the iPhone or Watch cannot reach its next required hop. A
  cached aggregate may remain visible with an offline marker.
- `error` means no trustworthy projection is available. Show a generic error
  and the iPhone/web fallback.
- `source_host` and `hub_origin` remain visible to the iPhone and are preserved
  when the Watch receives a projection. They describe origin, not trust.
- `projection_id`, `generated_at`, and `expires_at` support duplicate and replay
  detection. The client must not replay a stale projection as a mutation.

## 6. Action and deep-link contract

`mobile.v1` is read-only. Its action surface is limited to `refresh` and
`open_authenticated_fallback`. These are navigation or retrieval requests, not
privileged operations.

Any future mutation envelope must be a separate versioned contract and include:

1. an explicit action allowlist and bounded arguments;
2. confirmation copy plus an unambiguous cancel path;
3. a fresh authenticated iPhone/web session and CSRF protection where the
   target is cookie-backed;
4. policy and approval evaluation at the hub, not on the Watch;
5. an audit correlation id, result, and failure path;
6. replay protection and no offline queue for privileged actions.

The first such envelope is [`mobile-action.v1`](mobile-action-contract-v1.md)
(task #779): one safe verb, `acknowledge`, a TTL'd operator-visible suppression
of a single attention item. It does not change `mobile.v1`.

The Watch may never treat a notification tap, cached approval, or deep-link
identifier as proof of authorization.

## 7. Threat and failure matrix

| Threat/failure | Required behavior |
|---|---|
| Stale payload | Mark stale; show last refresh; refresh or open iPhone; no mutation |
| Duplicate/replayed payload | De-duplicate by `projection_id`; ignore expired data |
| Lost Watch/phone | No hub secret on Watch; revoke/re-authenticate the iPhone session |
| Locked-screen leakage | Generic notification text; no raw item title/reason/content |
| Hub unavailable | Show unavailable/offline; do not retry as a privileged action |
| Auth/session expiry | Show reconnect state; open iPhone/web login path |
| Malformed or unknown fields | Fail closed at the mobile validator; do not partially authorize |
| Untrusted Browser input | Keep the existing independent Browser validator and allowlist |
| Wrong source or origin label | Display as provenance only; never use it as an auth decision |

## 8. Follow-up implementation gates

- **#413**: field-level source inventory and forbidden-field proof for Watch.
- **#415**: iPhone relay, notifications, replay, lost-device, and audit
  sequence/failure contract.
- **#416**: executable schema, size limits, deterministic fixtures, and
  validator; this is the implementation gate for `mobile.v1`.
- **#410/#417**: implement only the read-only Watch slice after #413/#414/#415
  and #416 are accepted.

No new endpoint, transport, notification entitlement, or mutation is implied by
this document.
