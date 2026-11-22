# Security posture

HTTP hubs fail closed when `UA_ENFORCE_AUTH` is unset. Stdio satellites stay
open so a local process keeps working, and they print one migration warning.
`delegate` is on the approval baseline unless the operator opts out.

These flags are read by `config.enforce_auth()`, `config.approval_gated_tools()`,
and `safety.check_prod_action_gate()`. Set them in the hub's environment
(`~/.unified_agent/service.env` or the unit) before restart. A blank value is
treated as unset.

## Flags

| Flag | Values | When unset |
|---|---|---|
| `UA_ENFORCE_AUTH` | `1`/`true`/`yes` on, `0`/`false`/`no` off. Any other explicit token is off. | HTTP enforces. Stdio does not. `UA_LEGACY_OPEN_POSTURE=1` forces off. |
| `UA_LEGACY_OPEN_POSTURE` | `1`/`true`/`yes` restores warn-mode auth, allows anonymous prod actions, and drops the approval baseline. | Safer defaults apply. |
| `UA_TRUST_STDIO` | `1`/`true`/`yes` acknowledges that stdio does not enforce identity gates. | Stdio still does not enforce; the process prints one warning instead of the acknowledgement. |
| `UA_REQUIRE_CALLER_IDENTITY` | `1`/`true`/`yes` denies anonymous prod actions on stdio and refuses an unverified claimed author. | Stdio allows both. HTTP already denies both. |
| `UA_APPROVAL_BASELINE` | `0`/`false`/`no` turns the baseline off (warns once). Any other set value leaves it on. | On. The gated set includes `delegate` plus every `UA_APPROVAL_TOOLS` entry. |

`UA_LEGACY_OPEN_POSTURE=1` does not override an explicit `UA_ENFORCE_AUTH`.
It still suppresses the approval baseline and still lets an unidentified
caller skip `check_prod_action_gate`.

Warnings are once per process. They are printed from `enforce_auth()` and
`approval_gated_tools()` (and from the prod-action skip path, which uses the
same lines). `UA_TRUST_STDIO=1` replaces the stdio warning with one
acknowledgement and then stays quiet.

## What each transport does

HTTP, `UA_ENFORCE_AUTH` unset, legacy flag unset:

- `enforce_auth()` is true. One info line names `UA_LEGACY_OPEN_POSTURE`.
- Anonymous `git_merge_task`, `task_delete`, `node_dispatch`,
  `fleet_host_action`, `oauth_device_manage`, `deploy`, and `acp_permission`
  raise `ToolError`.
- `delegate` is approval-gated.

Stdio, `UA_ENFORCE_AUTH` unset:

- `enforce_auth()` is false. One warning names `UA_TRUST_STDIO` and
  `UA_REQUIRE_CALLER_IDENTITY`, unless `UA_TRUST_STDIO=1` already acknowledged it.
- Anonymous prod actions skip the gate, unless `UA_REQUIRE_CALLER_IDENTITY=1`.
- A claimed author with no verified identity is still accepted, unless
  `UA_REQUIRE_CALLER_IDENTITY=1`.

## Migration

Live HTTP hub that already sets `UA_ENFORCE_AUTH=1` and has a token leg
(`UA_AGENT_TOKENS`, MCP OAuth, or WorkOS): authentication is unchanged.
`delegate` becomes approval-gated on the next restart. To keep `delegate`
opt-in for one window, set `UA_APPROVAL_BASELINE=0` before restart. That
prints a warning on boot. Prefer leaving the baseline on.

Live HTTP hub that relied on the unset default (warn-mode): the next restart
enforces authentication. Anonymous callers with a configured token leg receive
401. To keep warn-mode for one migration window, set `UA_LEGACY_OPEN_POSTURE=1`
before restart. That warns on every boot. Prefer the new default, and keep
the token file present and readable.

Stdio satellites: no token middleware, and they keep working. Set
`UA_TRUST_STDIO=1` to acknowledge the warning, or
`UA_REQUIRE_CALLER_IDENTITY=1` to deny anonymous prod actions and unverified
author claims. Do not point a satellite at `UA_LEGACY_OPEN_POSTURE=1` unless
you also want the HTTP-style open posture (anonymous prod actions and no
`delegate` baseline) on that process.

## Residuals

`server.py` still serves the request with no identity when no credential leg
is configured (no bearer tokens, no OAuth provider, WorkOS leg not enabled).
That fast path returns before `enforce_auth()` is consulted. The HTTP default
protects hubs that have a token leg: an anonymous request hits the existing
401 branch. A hub with no tokens configured is still anonymous even when
`enforce_auth()` is true. Fixing that fast path is a separate change.

`verify_write_identity` on stdio, when `UA_REQUIRE_CALLER_IDENTITY` is not
set, still accepts a claimed author if no verified identity is bound. That
includes stdio with `UA_ENFORCE_AUTH=1`. HTTP does not: a missing verified
identity returns `None` instead of the claimed author, including when
`UA_ENFORCE_AUTH=0` or `UA_LEGACY_OPEN_POSTURE=1`. Those flags restore
middleware warn-mode and the anonymous prod-action skip. They do not restore
spoofable authorship. A claim or delegation that requires a named agent must
present a credential. `UA_REQUIRE_CALLER_IDENTITY=1` returns `None` on stdio
as well.

`pi_run_task` is not in the approval baseline and has no in-tool gate. The
forced gate from #867 was vetoed (it failed on executor hosts with no local
approvals table) and is being redesigned in task #1524. Until that lands,
list `pi_run_task` in `UA_APPROVAL_TOOLS` to gate it.
`git_merge_task` and `node_dispatch` are not in the baseline either. They
already call `check_prod_action_gate` with `force=True` once an identity is
present. The anonymous-identity denial above covers the case that used to
skip that gate.

`system_doctor` reports the hub unit, not the process that invoked it. Hub
auth uses the unit's `UA_ENFORCE_AUTH`, `UA_TRANSPORT`, and
`UA_LEGACY_OPEN_POSTURE`. The approval-gate line uses the unit's
`UA_APPROVAL_TOOLS`, `UA_APPROVAL_BASELINE`, and `UA_LEGACY_OPEN_POSTURE`.
An HTTP unit that omits `UA_ENFORCE_AUTH` is reported as enforced. A unit
that sets `UA_APPROVAL_BASELINE=0` is not reported as gating `delegate`.
