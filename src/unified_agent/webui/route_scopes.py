"""Declared authorization scope for every route the hub serves (task #1745).

Authentication is one central gate (``server._PUBLIC_PATH_PREFIXES`` and the
``HubAuthASGIMiddleware``). Authorization used to live only inside each
handler, so a new route could ship authenticated but with no authorization
decision at all. This module is the single table that closes that gap: every
``(method, path)`` the app serves maps to one ``Scope`` that says who may call
it. ``routes.web_routes()`` wraps each route so that

* a route with no entry here answers 403 before its handler runs (fail closed),
* the declared guard and the declared profile capabilities are enforced before
  the handler runs, and
* ``tests/test_route_scopes.py`` fails if a served route has no entry, or an
  entry matches no served route.

The handlers keep their own checks as defence in depth.

Vocabulary. A capability is a hub tool name, judged by the existing profile
ceiling (``profiles.tool_is_allowed_for``). Nothing here invents a second
permission model.

``Access``
    ``PUBLIC``             reachable without a hub credential; the route may verify
                           its own (login token, signature, one-use state).
    ``OWN_AUTH``           the route authenticates the caller itself and does not call
                           the shared origin/host/identity guard.
    ``ANY_AUTHENTICATED``  the shared guard only: any identity the hub accepts, with no
                           profile admission capability. Reviewed read helpers may
                           be declared below. A mutating entry must say why in ``note``.
    ``CAPABILITY``         the shared guard plus the named capabilities below.

``tools``
    Pure profile ceiling, enforced by the wrapper: every listed tool must be
    exposed to the request's profile (``profiles.tool_is_allowed_for``). The profile
    is resolved from the verified identity (``ua_identity``): the MCP session's
    activated profile when the request is bound to one, else the identity's default.
    The route uses the pure named-profile check. After admission, only these
    declared tools and actions become identity-bound handler call authority;
    the MCP session default ceiling remains active without a session id.

``actions``
    Consolidated-manager actions, as ``(manager, action)`` pairs, judged by
    ``profiles.consolidated_action_is_allowed_for``. This is how the MCP surface
    authorizes a legacy alias such as ``delegate`` (``delegation_manage``) that the
    default profile hides from discovery but still lets its manager run.

``read_tools`` / ``read_actions``
    Exact, closed-vocabulary helpers for shared authenticated read panes. They
    preserve existing read admission and become handler call authority only for
    GET/HEAD at the guarded callable, separately from writable authority. Mixed
    ingest reads must pin mark=False. They cannot skip policy/access/approval checks.

``operator``
    The verified human operator (``UA_OPERATOR_LABEL`` on a hub credential, the same
    test as the agent-access recovery exemption) passes the ``tools`` and ``actions``
    ceiling. The Control Center is the operator's write surface, and the operator
    profile is deliberately read-only over MCP: it cannot call ``task_update`` or
    ``crew_intervene``, yet operates both from the browser. With no ``tools`` or
    ``actions`` the route is the operator's alone. Agent identities on the operator
    profile do not pass: the exemption is the identity, not the profile.

``handler``
    Checks that stay in the handler and that the wrapper does not repeat, from the
    closed vocabulary ``HANDLER_CHECKS``. Where a check depends on the request body
    or on the caller's identity rather than on the request profile, the route is
    mapped to the weakest capability any call needs and the conditional check
    stays in the handler (tagged ``body-conditional`` or ``identity-profile``).

``delegates``
    Registered tools the handler calls. Their own gates (policy, approvals, rate
    limits, and the profile ceiling when exposure is active) run downstream.
    Informational; the wrapper does not enforce it.

``csrf``
    The route calls ``_guard_mutation`` (cookie sessions must present the CSRF token).

Where the handler checks something conditionally, the table follows the weakest
call; it never claims more than the handler enforces. A tighter rule for a route
is a behaviour change and belongs in its own change, not here.

Task #1865 was such a change. The action routes that used to say "no profile
ceiling at the route" now carry the ceiling of their MCP tool twin, so an identity
whose profile cannot call a tool over MCP cannot run the same logic over HTTP.
Routes with no registered twin take the twin of what they start (a ua-node session
is a dispatch) or are the operator's alone (fleet control, harness publish).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Access(StrEnum):
    PUBLIC = "public"
    OWN_AUTH = "own-auth"
    ANY_AUTHENTICATED = "any-authenticated"
    CAPABILITY = "capability"


#: Closed vocabulary for ``Scope.handler``: checks a handler performs itself.
HANDLER_CHECKS: dict[str, str] = {
    "identity": "a verified identity is required, even when auth enforcement is off",
    "human": "the identity must be a human actor (config.actor_kind_for_identity)",
    "bearer": "only a hub bearer token is accepted; cookie and OAuth sessions are refused",
    "device-signin": "a device sign-in is required unless UA_NODE_ALLOW_BEARER is set",
    "identity-profile": "authority comes from the identity's own default profile, not the "
    "request profile, and may skip for a human operator",
    "agent-access": "the agent-access switch (agent_access.enforce) applies",
    "approval": "the write goes through the approval gate and may answer 202 pending",
    "governed-read": "policy rules, rate limit and approval gate run for the read "
    "(routes._governed_read)",
    "body-conditional": "a further check depends on the request body",
    "own-credential": "the route verifies its own credential (token, signature or state)",
    "owner-only": "only the original submitter may act",
    "tailnet-approver": "the caller must be on a tailnet device owned by a configured approver",
    "internal-authz": "the callee authorizes the caller (profile or actor check) itself",
    "feature-flag": "the route answers 404 or 403 unless its feature or edition is enabled",
}


#: Handler checks that are themselves a capability. A scope that carries none of these
#: and names no tool is ``ANY_AUTHENTICATED``, whatever else its handler does.
_CAPABILITY_CHECKS = frozenset(
    {"human", "bearer", "device-signin", "identity-profile", "owner-only", "tailnet-approver"}
    | {"governed-read"}
)


# The shared panes intentionally admit every authenticated identity. Only these
# reviewed read helpers may inherit that admission; mutations stay capabilities.
_OPEN_READ_TOOLS = frozenset(
    {
        "agent_activity",
        "approval_pending",
        "autonomous_cycle_get",
        "autonomous_cycle_list",
        "check_anomalies",
        "credential_pool_list",
        "delegate_list",
        "dependency_list",
        "dispatch_delivery_receipts_list",
        "event_log_query",
        "gcloud_inventory",
        "handoff_list",
        "horizon2_package_list",
        "horizon2_server_list",
        "langgraph_checkpoints",
        "memory_list",
        "memory_search",
        "message_list",
        "metrics_snapshot",
        "model_quota_status",
        "notify_delivery_status",
        "perf_summary",
        "plan_list",
        "system_doctor",
        "task_list",
        "vault_briefing_digest",
        "vault_ingest_status",
        "vault_links",
        "vault_read",
        "vault_search",
        "workflow_list",
    }
)
_OPEN_READ_ACTIONS = frozenset({("plan_manage", "plan_list"), ("plan_manage", "plan_get")})


@dataclass(frozen=True)
class Scope:
    access: Access
    tools: tuple[str, ...] = ()
    actions: tuple[tuple[str, str], ...] = ()
    operator: bool = False
    handler: tuple[str, ...] = ()
    delegates: tuple[str, ...] = ()
    csrf: bool = False
    note: str = ""
    read_tools: tuple[str, ...] = ()
    read_actions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.read_tools or self.read_actions:
            if self.access is not Access.ANY_AUTHENTICATED or self.csrf:
                raise ValueError(
                    "shared read authority requires a non-mutating authenticated scope"
                )
            if (
                set(self.read_tools) - _OPEN_READ_TOOLS
                or set(self.read_actions) - _OPEN_READ_ACTIONS
            ):
                raise ValueError("unknown shared read capability")
        unknown = set(self.handler) - set(HANDLER_CHECKS)
        if unknown:
            raise ValueError(f"unknown handler check(s): {sorted(unknown)}")
        capability = bool(self.tools or self.actions or self.operator)
        if self.access in (Access.PUBLIC, Access.OWN_AUTH):
            if capability or self.csrf or self.delegates:
                raise ValueError(f"{self.access} scope cannot carry capabilities or csrf")
        elif self.access is Access.ANY_AUTHENTICATED:
            if capability or set(self.handler) & _CAPABILITY_CHECKS:
                raise ValueError("any-authenticated scope cannot name a capability")
            if self.csrf and not self.note:
                raise ValueError("a mutating any-authenticated scope must say who gates it")
        elif not (capability or set(self.handler) & _CAPABILITY_CHECKS):
            raise ValueError("capability scope names no capability")


def _public(note: str, *handler: str) -> Scope:
    return Scope(Access.PUBLIC, handler=handler, note=note)


def _own(note: str, *handler: str) -> Scope:
    return Scope(Access.OWN_AUTH, handler=handler, note=note)


def _open(
    *,
    handler: tuple[str, ...] = (),
    delegates: tuple[str, ...] = (),
    note: str = "",
    read_tools: tuple[str, ...] = (),
    read_actions: tuple[tuple[str, str], ...] = (),
) -> Scope:
    """Any authenticated identity, read (no CSRF), with exact read helpers."""
    return Scope(
        Access.ANY_AUTHENTICATED,
        handler=handler,
        delegates=delegates,
        note=note,
        read_tools=read_tools,
        read_actions=read_actions,
    )


def _open_write(
    note: str, *, handler: tuple[str, ...] = ("identity",), delegates: tuple[str, ...] = ()
) -> Scope:
    """Any authenticated identity, mutating, CSRF-guarded. ``note`` says who gates it."""
    return Scope(
        Access.ANY_AUTHENTICATED, handler=handler, delegates=delegates, csrf=True, note=note
    )


def _cap(
    *,
    tools: tuple[str, ...] = (),
    actions: tuple[tuple[str, str], ...] = (),
    operator: bool = False,
    handler: tuple[str, ...] = (),
    delegates: tuple[str, ...] = (),
    csrf: bool = False,
    note: str = "",
) -> Scope:
    return Scope(
        Access.CAPABILITY,
        tools=tools,
        actions=actions,
        operator=operator,
        handler=handler,
        delegates=delegates,
        csrf=csrf,
        note=note,
    )


_ID = ("identity",)
_NODE = ("identity", "device-signin")
#: Starting work on another agent (a delegation, a stack dispatch, a ua-node session)
#: is ``delegate`` through ``delegation_manage`` on the MCP surface.
_DISPATCH = (("delegation_manage", "delegate"),)
_CYCLE = tuple(
    ("autonomous_cycle_manage", action)
    for action in (
        "autonomous_cycle_start",
        "autonomous_cycle_transition",
        "autonomous_cycle_record_usage",
        "autonomous_cycle_kill",
    )
)

ROUTE_SCOPES: dict[tuple[str, str], Scope] = {
    # --- public: reachable without a hub credential ----------------------------------
    ("GET", "/healthz"): _public("content-free liveness probe"),
    ("GET", "/readyz"): _public(
        "readiness: live-store write/read and WAL; WorkOS reachability is advisory"
    ),
    ("GET", "/ui"): _public("static shell; every byte of data comes from the gated /api"),
    ("GET", "/ui/{path:path}"): _public("static shell assets and SPA fallback"),
    ("POST", "/api/session"): _public(
        "login: trades a bearer token for a cookie", "own-credential"
    ),
    ("DELETE", "/api/session"): _public(
        "logout: revokes the presented session cookie; needs that session's CSRF token"
    ),
    ("GET", "/api/session/options"): _public(
        "sign-in screen: whether WorkOS sign-in is offered (one boolean)"
    ),
    ("GET", "/api/session/workos/login"): _public(
        "login: redirects to WorkOS AuthKit with a signed state and PKCE"
    ),
    ("GET", "/api/session/workos/callback"): _public(
        "login: the signed one-use state and the WorkOS code are the credential",
        "own-credential",
    ),
    ("POST", "/api/slack/events"): _public(
        "Slack ingress; the Slack signature is the credential", "own-credential"
    ),
    ("GET", "/oauth/connectors/callback"): _public(
        "provider redirect; the one-use state is the credential", "own-credential"
    ),
    # --- own authentication ----------------------------------------------------------
    ("POST", "/api/llm/v1/chat/completions"): _own(
        "device-bound OAuth token required, checked by the broker", "identity", "device-signin"
    ),
    # --- identity and sessions -------------------------------------------------------
    ("GET", "/api/whoami"): _open(),
    # --- read panes: shared guard, composed from registered-tool reads ---------------
    ("GET", "/api/overview"): _open(
        read_tools=(
            "approval_pending",
            "check_anomalies",
            "delegate_list",
            "dependency_list",
            "event_log_query",
            "handoff_list",
            "message_list",
            "metrics_snapshot",
            "plan_list",
            "system_doctor",
            "task_list",
            "workflow_list",
        )
    ),
    ("GET", "/api/attention"): _open(
        read_tools=(
            "approval_pending",
            "autonomous_cycle_list",
            "check_anomalies",
            "system_doctor",
            "task_list",
        )
    ),
    ("GET", "/api/digest"): _open(
        read_tools=(
            "approval_pending",
            "autonomous_cycle_list",
            "check_anomalies",
            "system_doctor",
            "task_list",
        )
    ),
    ("GET", "/api/agents"): _open(read_tools=("task_list",)),
    ("GET", "/api/personas"): _open(),
    ("GET", "/api/personas/{label}"): _open(),
    ("GET", "/api/org"): _open(read_tools=("task_list",)),
    ("GET", "/api/dispatch-routes"): _open(),
    ("GET", "/api/agents/{label}"): _open(
        read_tools=("agent_activity",),
        note="a known label never seen answers never_connected (task #2523); "
        "?wait_for=connected holds the read at most 25 s, 16 waits per process",
    ),
    ("GET", "/api/sessions"): _cap(tools=("session_list",)),
    ("GET", "/api/sessions/{id}"): _cap(tools=("session_list",)),
    # Runs pane (task #1652): one read-only projection over dispatch_runs, the
    # selfpickup event channel and agent_sessions. Same ceiling as the run
    # object's MCP reader (object_model profile_tool); the verified human
    # operator passes it so the Control Center can render the pane.
    ("GET", "/api/runs"): _cap(
        tools=("dispatch_report_self",),
        operator=True,
        handler=_ID,
        note="read-only; abandoned-on-read, tenant-fenced, cursor-paginated",
    ),
    ("GET", "/api/runs/{run_id}"): _cap(
        tools=("dispatch_report_self",),
        operator=True,
        handler=_ID,
        note="read-only; 404 for an unknown or other-tenant run id",
    ),
    ("GET", "/api/sessions/{id}/crew"): _cap(tools=("session_list",)),
    ("POST", "/api/sessions/{id}/wake-grant"): _cap(
        operator=True,
        handler=_ID,
        csrf=True,
        note="grants or revokes one session's idle wakes (task #2050); the verified human "
        "operator's alone, with no MCP twin, because the session's own wake request "
        "comes from the bearer the agent also holds",
    ),
    ("GET", "/api/tasks"): _open(read_tools=("task_list",)),
    ("POST", "/api/tasks"): _cap(
        tools=("task_add",),
        operator=True,
        handler=_ID,
        delegates=("task_add",),
        csrf=True,
        note="task_add runs its own policy and approval gates",
    ),
    ("POST", "/api/tasks/next-work-projection"): _cap(
        tools=("task_list",), note="read-only projection; guard without CSRF"
    ),
    ("GET", "/api/tasks/search"): _cap(tools=("task_list",)),
    # Task subtree holds (task #1747): reading needs task_list; holding needs
    # task_update and the task_hold action, the MCP twin, or the operator.
    ("GET", "/api/tasks/holds"): _cap(tools=("task_list",)),
    ("POST", "/api/tasks/holds"): _cap(
        tools=("task_update",),
        actions=(("coordination_manage", "task_hold"),),
        operator=True,
        handler=_ID,
        delegates=("coordination_manage",),
        csrf=True,
        note="coordination_manage runs its own policy and approval gates",
    ),
    ("POST", "/api/tasks/holds/{hold_id}/release"): _cap(
        tools=("task_update",),
        actions=(("coordination_manage", "task_hold"),),
        operator=True,
        handler=_ID,
        delegates=("coordination_manage",),
        csrf=True,
        note="resume or restore through the same task_hold action",
    ),
    # Operator release of another agent's live claim (task #1700).
    ("POST", "/api/tasks/{task_id}/claim-release"): _open_write(
        "claim_release authorizes the actor: a lifecycle operator or the verified operator",
        handler=("identity", "internal-authz"),
    ),
    ("GET", "/api/goals"): _cap(tools=("task_list",)),
    ("POST", "/api/goals"): _cap(
        tools=("goal_manage", "task_list"),
        handler=("identity", "agent-access", "approval"),
        csrf=True,
        note="add and link both need goal_manage and the goal object's task_list ceiling",
    ),
    ("GET", "/api/goals/{goal_id}"): _cap(tools=("task_list",)),
    ("GET", "/api/tasks/{task_id}/why-chain"): _cap(tools=("task_list",)),
    ("GET", "/api/tasks/{task_id}/subtree"): _cap(
        tools=("task_list",),
        note="every descendant at any depth and status, tenant-fenced (task #2516)",
    ),
    ("GET", "/api/tasks/{task_id}/comments"): _cap(tools=("task_list",)),
    ("POST", "/api/tasks/{task_id}/comments"): _cap(
        tools=("task_comment", "task_list"),
        handler=("identity", "agent-access", "approval"),
        csrf=True,
    ),
    ("GET", "/api/work-graph"): _cap(
        tools=("task_list",), note="other object families appear only when their reader is exposed"
    ),
    ("POST", "/api/work-products"): _cap(
        tools=("work_product_submit",),
        handler=("identity", "agent-access", "approval"),
        delegates=("work_product_submit",),
        csrf=True,
    ),
    ("POST", "/api/work-products/{id}/release"): _cap(
        tools=("work_product_submit",),
        handler=("identity", "owner-only", "agent-access", "approval"),
        delegates=("work_product_submit",),
        csrf=True,
    ),
    ("GET", "/api/board"): _open(read_tools=("delegate_list", "dependency_list", "task_list")),
    ("GET", "/api/flow-metrics"): _open(read_tools=("task_list",)),
    ("GET", "/api/queue-health"): _open(read_tools=("task_list",)),
    ("GET", "/api/projects"): _open(),
    ("GET", "/api/review-approval"): _open(read_tools=("approval_pending", "task_list")),
    ("GET", "/api/executor-delegation"): _open(read_tools=("delegate_list", "system_doctor")),
    ("GET", "/api/approvals"): _open(
        delegates=("approval_pending",), read_tools=("approval_pending",)
    ),
    ("POST", "/api/approvals/request"): _cap(
        tools=("approval_pending",),
        handler=("identity", "agent-access", "body-conditional"),
        csrf=True,
        note="queues only: registered gated tool or validated existing-child dispatch approval",
    ),
    # Approval Ledger re-request (task #1854): queues a missed row, never resolves.
    # The ceiling of its MCP twin; github_app_worker then admits only a
    # lifecycle operator or the verified operator.
    ("POST", "/api/approvals/ledger-rerequest"): _cap(
        actions=(("coordination_manage", "approval_ledger_rerequest"),),
        operator=True,
        handler=("identity", "internal-authz", "approval"),
        delegates=("coordination_manage",),
        csrf=True,
        note="runs the coordination_manage action (policy and approval gates); "
        "github_app_worker admits a lifecycle operator or the verified operator",
    ),
    # Approval Ledger poll-now (task #1867): runs the scheduled poll once; it
    # requests rows and never resolves one. Same ceiling as the re-request.
    ("POST", "/api/approvals/ledger-poll"): _cap(
        actions=(("coordination_manage", "approval_ledger_poll"),),
        operator=True,
        handler=("identity", "internal-authz", "approval"),
        delegates=("coordination_manage",),
        csrf=True,
        note="runs the coordination_manage action (policy and approval gates); "
        "github_app_worker admits a lifecycle operator or the verified operator",
    ),
    # Explicit review reads (task #2407). No profile ceiling: an allowlist narrower
    # than any profile decides, and the human kind must work on the operator profile.
    ("POST", "/api/review/reads"): _open_write(
        "the handler admits humans and UA_REVIEW_READERS; the row's reader is the verified "
        "identity, never a body field, and the live-head and delta rules are the CLI's",
        handler=("identity", "body-conditional"),
    ),
    ("GET", "/api/approvals/{id}"): _open(),
    ("GET", "/api/memory"): _open(read_tools=("memory_list", "memory_search")),
    ("GET", "/api/vault/search"): _open(read_tools=("vault_search",)),
    ("GET", "/api/vault/page"): _open(read_tools=("vault_links", "vault_read")),
    ("GET", "/api/vault/ingest-status"): _open(read_tools=("vault_ingest_status",)),
    ("GET", "/api/vault/briefing"): _open(read_tools=("vault_briefing_digest",)),
    # --- connectors ------------------------------------------------------------------
    ("GET", "/api/connectors"): _open(
        read_tools=("gcloud_inventory", "horizon2_package_list", "horizon2_server_list")
    ),
    ("GET", "/api/connectors/oauth/{provider}"): _cap(handler=("identity", "human")),
    ("POST", "/api/connectors/oauth/{provider}/begin"): _cap(
        handler=("identity", "human", "agent-access"), csrf=True
    ),
    ("POST", "/api/connectors/oauth/{provider}/disconnect"): _cap(
        handler=("identity", "human", "agent-access"), csrf=True
    ),
    # --- status panes ----------------------------------------------------------------
    ("GET", "/api/quota"): _open(read_tools=("model_quota_status",)),
    ("GET", "/api/credential_pool"): _open(read_tools=("credential_pool_list",)),
    ("GET", "/api/slack_gateway"): _open(read_tools=("event_log_query",)),
    ("GET", "/api/notify_delivery"): _open(read_tools=("notify_delivery_status",)),
    ("GET", "/api/dispatch_receipts"): _open(read_tools=("dispatch_delivery_receipts_list",)),
    ("GET", "/api/agent-graph"): _open(
        read_tools=("delegate_list", "message_list", "system_doctor", "task_list")
    ),
    ("GET", "/api/zulip"): _open(),
    ("GET", "/api/execution-graph"): _open(
        read_tools=(
            "delegate_list",
            "dependency_list",
            "handoff_list",
            "system_doctor",
            "task_list",
        )
    ),
    ("GET", "/api/dispatch-stack"): _open(read_tools=("dependency_list", "task_list")),
    ("GET", "/api/cycles"): _open(read_tools=("autonomous_cycle_list",)),
    ("GET", "/api/cycles/{cycle_id}"): _open(read_tools=("autonomous_cycle_get",)),
    ("GET", "/api/plans"): _open(read_actions=(("plan_manage", "plan_list"),)),
    ("GET", "/api/plans/{plan_id}"): _open(read_actions=(("plan_manage", "plan_get"),)),
    ("GET", "/api/proposals"): _open(),
    ("GET", "/api/org-proposals"): _cap(tools=("task_list",)),
    ("GET", "/api/org-proposals/{proposal_id}"): _cap(tools=("task_list",)),
    ("GET", "/api/health"): _open(read_tools=("system_doctor",)),
    ("GET", "/api/cloud"): _open(read_tools=("gcloud_inventory",)),
    ("GET", "/api/devices"): _open(read_tools=("system_doctor",)),
    ("GET", "/api/nodes"): _open(),
    # --- device acceptance (task #2112) ----------------------------------------------
    ("GET", "/api/nodes/acceptance"): _open(
        note="read-only device acceptance state; ?node= computes one node's live result "
        "without recording it",
    ),
    ("POST", "/api/action/node-acceptance"): _cap(
        actions=(
            ("coordination_manage", "node_acceptance_start"),
            ("coordination_manage", "node_acceptance_evaluate"),
            ("coordination_manage", "node_acceptance_attest"),
        ),
        operator=True,
        handler=("identity", "internal-authz"),
        csrf=True,
        note="start or re-evaluate a ua-node's harmless acceptance job (no model, no "
        "harness, so no approval); node_acceptance.authorize admits only the node's own "
        "agent or a lifecycle operator",
    ),
    # --- node retire (task #2208; the actions are task #2181) ------------------------
    ("POST", "/api/action/node-retire"): _cap(
        actions=(
            ("coordination_manage", "node_retire"),
            ("coordination_manage", "node_unretire"),
        ),
        operator=True,
        handler=("identity", "internal-authz"),
        csrf=True,
        note="retire or un-retire a registered ua-node row (kept, never deleted); "
        "node_registry admits lifecycle operators only, and refuses a retire while the "
        "node has queued or claimed work (409)",
    ),
    # --- work proposals (task #2393) ------------------------------------------------
    ("POST", "/api/action/work-proposal-decide"): _cap(
        actions=(("coordination_manage", "work_proposal_decide"),),
        operator=True,
        handler=("identity", "internal-authz"),
        csrf=True,
        note="approve or reject the items of an agent-filed work proposal; "
        "work_proposals admits only a human identity, and each approval spawns one "
        "task with a frozen contract digest",
    ),
    ("GET", "/api/mobile/v1/projection"): _open(
        handler=("feature-flag",),
        read_tools=(
            "approval_pending",
            "autonomous_cycle_list",
            "check_anomalies",
            "system_doctor",
            "task_list",
        ),
    ),
    ("GET", "/api/langgraph/checkpoints"): _open(read_tools=("langgraph_checkpoints",)),
    ("GET", "/api/langgraph/runs/{run_id}"): _cap(
        handler=("identity", "owner-only"),
        note="read-only progress of one governed graph run from its checkpoints and bound "
        "plan; only the run's own caller or the verified human operator, else 404 (task "
        "#2516, review #1538)",
    ),
    # --- evidence and platform objects -----------------------------------------------
    ("GET", "/api/evidence"): _open(handler=("feature-flag",)),
    ("GET", "/api/evidence/export"): _cap(
        tools=("evidence_report",),
        operator=True,
        handler=("feature-flag",),
        note="the complete export (every matching record, CSV streamed by keyset page); "
        "the evidence_report ceiling because it is uncapped (task #2516)",
    ),
    ("GET", "/api/session-credentials"): _cap(
        operator=True,
        handler=_ID,
        note="session credential metadata, never a value or digest, tenant-scoped, "
        "no-store; the verified human operator's alone (task #2516)",
    ),
    ("POST", "/api/evidence"): _cap(
        tools=("event_log_query",),
        handler=("identity", "agent-access", "approval"),
        csrf=True,
        note="the evidence_event object's profile tool; the writer is sdk.-channel only",
    ),
    ("GET", "/api/platform/manifest"): _open(),
    ("GET", "/api/platform/objects/{kind}"): _open(
        handler=("identity",),
        note="each object kind is gated by its own profile tool inside the reader",
    ),
    ("GET", "/api/platform/objects/{kind}/{object_id}"): _open(
        handler=("identity",),
        note="each object kind is gated by its own profile tool inside the reader",
    ),
    ("GET", "/api/platform/provenance-graph"): _open(
        handler=("identity",), note="same per-object gate as the platform objects"
    ),
    ("GET", "/api/capability-catalog"): _open(),
    ("GET", "/api/perf"): _open(read_tools=("perf_summary",)),
    ("GET", "/api/delivery_slo"): _open(),
    ("GET", "/api/costs"): _open(),
    ("GET", "/api/tool-allowance"): _open(
        note="tenant-fenced: a caller reads the identity its calls are charged "
        "under; ?agent=<other>|* requires the verified operator on a human channel"
    ),
    ("GET", "/api/scorecards"): _open(),
    ("GET", "/api/events"): _open(delegates=("event_log_query",), read_tools=("event_log_query",)),
    ("GET", "/api/telemetry"): _open(),
    ("GET", "/api/events/stream"): _cap(
        tools=("event_log_query",),
        handler=("governed-read",),
        note="Server-Sent Events; admission runs before any replay row or stream slot",
    ),
    ("GET", "/api/ledger/rows"): _cap(
        tools=("event_log_query",),
        handler=("governed-read",),
        note="raw event_log rows for external hash-chain verifiers",
    ),
    # --- authority configuration, operating pane, teams, skills ----------------------
    ("GET", "/api/config-revisions"): _cap(
        handler=("identity", "identity-profile"),
        note="config_revision_manage on the identity's default profile (rollback recovery too)",
    ),
    ("POST", "/api/action/config-revision"): _cap(
        handler=("identity", "identity-profile", "approval"),
        csrf=True,
        note="approval-gated rollback; commit is not an HTTP action",
    ),
    ("GET", "/api/operating"): _cap(
        handler=("identity-profile",),
        note="goal_manage on the request and default profile; a human operator is exempt",
    ),
    ("GET", "/api/operating/resolve"): _cap(handler=("identity-profile",)),
    ("GET", "/api/operating/sprints"): _cap(handler=("identity-profile",)),
    ("POST", "/api/action/operating-sprint"): _cap(
        handler=("identity", "identity-profile", "agent-access", "approval"), csrf=True
    ),
    ("POST", "/api/action/operating-run"): _cap(
        handler=("identity", "identity-profile", "agent-access", "approval"), csrf=True
    ),
    ("GET", "/api/skills-teams"): _cap(
        handler=("identity", "identity-profile"),
        note="skill_registry on the identity's default profile; operator may read with crew_view",
    ),
    ("POST", "/api/action/skills-teams"): _cap(
        handler=("identity", "identity-profile", "body-conditional"),
        csrf=True,
        note="skill_registry for every action; install adds fs_write and team_install adds "
        "config_revision_manage inside the handler",
    ),
    ("GET", "/api/teams"): _open(handler=_ID),
    ("GET", "/api/teams/plan"): _cap(
        handler=("identity", "identity-profile"), note="coordination_manage via the callee"
    ),
    # Team snapshot and restore (task #1738): the coordination_manage action twins.
    ("GET", "/api/teams/snapshots"): _cap(
        tools=("session_list",),
        actions=(("coordination_manage", "team_snapshots"),),
        operator=True,
        handler=_ID,
        note="read-only list of team snapshots",
    ),
    ("POST", "/api/teams/snapshots"): _cap(
        tools=("session_list",),
        actions=(("coordination_manage", "team_snapshot"),),
        operator=True,
        handler=_ID,
        csrf=True,
        note="records one snapshot event; no transcript beyond the bounded packet",
    ),
    ("GET", "/api/teams/snapshots/{snapshot_id}/preview"): _cap(
        tools=("session_list",),
        actions=(("coordination_manage", "team_restore_preview"),),
        operator=True,
        handler=("identity", "internal-authz"),
        note="read-only planned outcome per seat, under the same ownership and claim "
        "checks restore applies",
    ),
    ("POST", "/api/teams/snapshots/{snapshot_id}/restore"): _cap(
        tools=("session_list", "crew_view"),
        actions=(("coordination_manage", "team_restore"), *_DISPATCH),
        operator=True,
        handler=("identity", "approval", "internal-authz"),
        csrf=True,
        note="reopens or re-dispatches each seat through the approval-gated node queue, so "
        "it is dispatch authority like a ua-node session; only the seat's owner, the task's "
        "claimant or the operator restores a seat, and a failed resume is a failed seat, "
        "never a fresh one",
    ),
    ("POST", "/api/action/team-up"): _cap(
        handler=("identity", "identity-profile", "approval"), csrf=True
    ),
    # --- actions: operator and agent writes ------------------------------------------
    ("POST", "/api/action/task"): _cap(
        tools=("task_update",),
        operator=True,
        handler=("approval",),
        delegates=("task_update",),
        csrf=True,
        note="task_update runs its own policy and approval gates",
    ),
    ("GET", "/api/agent-lifecycle"): _open(),
    ("POST", "/api/agent-lifecycle"): _open_write(
        "pause, resume, terminate, retire and unretire are authorized by agent_lifecycle "
        "against the actor",
        handler=("internal-authz",),
    ),
    ("POST", "/api/action/workflow"): _cap(
        tools=("workflow_run",),
        operator=True,
        delegates=("workflow_run",),
        csrf=True,
        note="workflow_run runs its own policy and approval gates; an optional "
        "Idempotency-Key runs it at most once (task #2516)",
    ),
    ("POST", "/api/action/dispatch"): _cap(
        tools=("task_update",),
        actions=_DISPATCH,
        operator=True,
        handler=_ID,
        delegates=("delegate", "task_update"),
        csrf=True,
        note="creates a delegation, marks the task, and starts the target agent's CLI",
    ),
    ("POST", "/api/action/dispatch-stack"): _cap(
        tools=("task_update",),
        actions=_DISPATCH,
        operator=True,
        handler=_ID,
        csrf=True,
        note="claims the next eligible task and starts its agent: the same twin as dispatch",
    ),
    # Launch attempts (task #2501): the state of a keyed launch for its own caller.
    # The weakest capability every launch route already needs: dispatch, the
    # stack and ua-node start/restart all require (delegation_manage, delegate).
    ("GET", "/api/launch-attempts/{key}"): _cap(
        actions=_DISPATCH,
        operator=True,
        handler=_ID,
        note="read-only; the verified caller's own attempt in its tenant, another identity's "
        "key reads as unknown; a stale in-progress attempt is reconciled on read",
    ),
    # Review #1521 pass 5: recovery of a keyed spawn whose launcher died after it
    # started. The verified human operator's alone, with no MCP twin: it releases a
    # launch key, and does so only once the hub proves the launcher and job gone.
    ("POST", "/api/dispatch-runs/{run_id}/settle-spawn"): _cap(
        operator=True,
        handler=_ID,
        csrf=True,
        note="settles one keyed run's unsettled spawn as failed, only once its launcher "
        "process and its job are proven gone; otherwise 409 with the reason",
    ),
    ("POST", "/api/action/dependency"): _cap(
        actions=(
            ("coordination_manage", "dependency_add"),
            ("coordination_manage", "dependency_remove"),
        ),
        operator=True,
        handler=_ID,
        delegates=("dependency_add", "dependency_remove"),
        csrf=True,
        note="add and remove share one ceiling on every profile",
    ),
    ("POST", "/api/action/handoff"): _cap(
        actions=(("coordination_manage", "handoff"),),
        operator=True,
        handler=_ID,
        delegates=("handoff",),
        csrf=True,
    ),
    ("POST", "/api/action/crew-intervene"): _cap(
        tools=("crew_intervene",),
        operator=True,
        handler=_ID,
        csrf=True,
        note="sends a typed intervention into a live session; the operator profile lacks "
        "crew_intervene over MCP by design and intervenes here as the verified operator",
    ),
    ("POST", "/api/action/node-session"): _cap(
        actions=_DISPATCH,
        operator=True,
        handler=("identity", "approval", "body-conditional"),
        csrf=True,
        note="start, cancel and restart of a ua-node agent session are dispatch authority; "
        "start and restart go through the approval gate, cancel is attributed to the caller",
    ),
    ("POST", "/api/action/cycle"): _cap(
        actions=_CYCLE,
        operator=True,
        handler=_ID,
        delegates=tuple(action for _, action in _CYCLE),
        csrf=True,
        note="every operation shares one ceiling on every profile; start takes an optional "
        "Idempotency-Key (task #2516)",
    ),
    ("POST", "/api/action/plan"): _cap(
        actions=(("plan_manage", "plan_review"),),
        operator=True,
        delegates=("plan_manage",),
        csrf=True,
        note="plan_manage review resolves the human confirmation gate",
    ),
    ("POST", "/api/action/proposal"): _cap(
        tools=("task_add", "vault_ingest_status"),
        operator=True,
        handler=_ID,
        csrf=True,
        note="closed action map: file_task is task_add, dismiss is vault_ingest_status(mark)",
    ),
    ("POST", "/api/action/fleet"): _cap(
        operator=True,
        handler=("identity", "approval"),
        csrf=True,
        note="approval-gated fleet control from the Devices pane; no MCP twin, so the "
        "human operator's alone",
    ),
    ("POST", "/api/action/oauth-revoke"): _cap(
        tools=("oauth_device_manage",),
        operator=True,
        handler=("identity", "approval"),
        csrf=True,
        note="approval-gated device sign-in revoke",
    ),
    ("POST", "/api/action/session-credential-revoke"): _cap(
        operator=True,
        handler=_ID,
        csrf=True,
        note="revokes session-scoped MCP credentials by credential, item or node (task #1744); "
        "the verified human operator's alone, with no MCP twin; answers what it revoked, and "
        "an optional Idempotency-Key replays that answer (task #2516)",
    ),
    ("POST", "/api/action/harness-config"): _cap(
        operator=True,
        handler=_ID,
        csrf=True,
        note="publishes a harness bundle, a human Control Center action with no MCP twin; "
        "apply stays on the Approvals pane",
    ),
    ("POST", "/api/action/approval"): _cap(
        tools=("approval_pending",),
        handler=("identity", "human", "agent-access"),
        csrf=True,
        note="approval resolution is human-only; the operator keeps the recovery exemption",
    ),
    ("POST", "/api/mobile/v1/action"): _open_write(
        "acknowledge only (an attention acknowledgement, no tool); the applier closes the "
        "verb list",
        handler=("identity", "feature-flag", "internal-authz"),
    ),
    # --- approver passkeys -----------------------------------------------------------
    ("GET", "/api/oauth/passkeys"): _open(),
    ("POST", "/api/oauth/passkeys"): _cap(
        handler=("identity", "human", "tailnet-approver"), csrf=True
    ),
    ("POST", "/api/oauth/passkeys/options"): _cap(
        handler=("identity", "human", "tailnet-approver"), csrf=True
    ),
    ("POST", "/api/oauth/passkeys/delete"): _cap(
        handler=("identity", "human", "tailnet-approver"), csrf=True
    ),
    # --- canvas and capture ----------------------------------------------------------
    ("GET", "/api/graph-canvas"): _open(),
    ("PUT", "/api/graph-canvas"): _cap(
        tools=("graph_canvas_write",),
        operator=True,
        csrf=True,
        note="stores the scene under the caller's identity, as graph_canvas_write does",
    ),
    ("POST", "/api/session-capture"): Scope(
        Access.ANY_AUTHENTICATED,
        handler=_ID,
        note="script caller: guard without CSRF; the memory note is stamped with the identity",
    ),
    # --- inbox endpoints for hooks (task #1861) ---------------------------------------
    ("POST", "/api/inbox/session"): _cap(
        tools=("session_start",),
        handler=("identity", "bearer"),
        note="hook caller: bearer only, so no CSRF (that guard defends cookies); the agent is "
        "the verified label and registration is idempotent on (agent, external_session_id)",
    ),
    ("POST", "/api/inbox/session/owed"): _cap(
        tools=("session_start",),
        handler=("identity", "bearer"),
        note="hook caller: bearer only; reports the caller's own session and refuses another's",
    ),
    ("GET", "/api/inbox/summary"): _cap(
        tools=("message_list",),
        handler=("identity", "bearer"),
        note="hook caller: bearer only; read-only digest that never marks mail read and "
        "refuses a session_id that is not the caller's own",
    ),
    # --- ua-node device plane --------------------------------------------------------
    ("POST", "/api/node/register"): _cap(handler=_NODE, csrf=True),
    ("POST", "/api/node/heartbeat"): _cap(handler=_NODE, csrf=True),
    ("POST", "/api/node/claim"): _cap(handler=_NODE, csrf=True, note="long poll"),
    ("POST", "/api/node/receipt"): _cap(handler=_NODE, csrf=True),
    ("POST", "/api/node/wake"): _cap(
        handler=_NODE,
        csrf=True,
        note="long poll; returns ids and unread counts only for opted-in sessions on the "
        "node's own host (task #1873)",
    ),
    ("POST", "/api/node/wake/check"): _cap(
        handler=_NODE,
        csrf=True,
        note="one-shot conservative attempt-budget reservation for a host-owned offer",
    ),
    ("POST", "/api/node/wake/receipt"): _cap(
        handler=_NODE,
        csrf=True,
        note="records delivered|failed|skipped for an offer to a session on the node's host",
    ),
    ("POST", "/api/node/dispatch"): _cap(
        actions=_DISPATCH,
        operator=True,
        handler=("identity", "approval"),
        csrf=True,
        note="queues one item for a registered node behind the approval gate",
    ),
    ("POST", "/api/node/events"): _cap(handler=_NODE, csrf=True),
    ("POST", "/api/node/permission"): _cap(handler=_NODE, csrf=True),
    ("POST", "/api/node/session_credential"): _cap(
        handler=(*_NODE, "feature-flag"),
        csrf=True,
        note="mints a session-scoped hub MCP credential for the node's own claimed ACP item "
        "(task #1744); stricter than device-signin: the handler refuses a static bearer even "
        "with UA_NODE_ALLOW_BEARER=1, and refuses everything while UA_ACP_SESSION_MCP is off",
    ),
    ("GET", "/api/node/config"): _cap(handler=_NODE),
    ("POST", "/api/node/config"): _cap(handler=(*_NODE, "approval"), csrf=True),
    ("GET", "/api/harness-config"): _open(),
    # --- routines --------------------------------------------------------------------
    ("POST", "/api/routines/{routine_id}/fire"): _cap(
        handler=("bearer", "agent-access"),
        delegates=("routine_fire",),
        note="a cookie or OAuth session is refused: the trigger is a webhook credential; "
        "the handler checks the routine_fire profile ceiling after the bearer check, so "
        "an unauthenticated caller in warn mode still answers 401",
    ),
    # --- routes served by the hub app but owned outside webui -----------------------
    # /mcp: the MCP transport. The hub middleware authenticates it and each tool call
    # passes the registered-tool gates (profile ceiling, policy, approvals).
    ("ANY", "/mcp"): _own("MCP transport; tool gates run per call", "internal-authz"),
    # Bare "/" only redirects to /ui (server._redirect_root_to_ui).
    ("GET", "/"): _public("redirects to /ui"),
    # OAuth authorization server (oauth.http_routes): present only when OAuth is
    # configured; the public paths mirror oauth._OAUTH_PUBLIC_PATHS.
    ("GET", "/.well-known/oauth-authorization-server"): _public("OAuth discovery metadata"),
    ("GET", "/.well-known/oauth-protected-resource/mcp"): _public("OAuth resource metadata"),
    ("GET", "/authorize"): _public("OAuth authorization endpoint", "own-credential"),
    ("POST", "/authorize"): _public("OAuth authorization endpoint", "own-credential"),
    ("POST", "/token"): _public("OAuth token endpoint", "own-credential"),
    ("POST", "/register"): _public("OAuth dynamic client registration", "own-credential"),
    ("POST", "/revoke"): _public("OAuth token revocation", "own-credential"),
    ("GET", "/oauth/consent"): _public("OAuth consent page", "own-credential"),
    ("POST", "/oauth/consent"): _public("OAuth consent submission", "own-credential"),
    ("POST", "/oauth/client-credentials"): _public("client-credentials grant", "own-credential"),
    ("POST", "/oauth/device_authorization"): _public("device grant start", "own-credential"),
    ("GET", "/oauth/device"): _public("device grant page", "own-credential"),
    ("POST", "/oauth/device"): _public("device grant approval", "own-credential"),
    ("GET", "/oauth/approver/workos/login"): _public("approver login redirect", "own-credential"),
    ("GET", "/oauth/approver/workos/callback"): _public(
        "approver login callback", "own-credential"
    ),
    ("GET", "/oauth/whoami"): _own(
        "authenticated identity probe for the public-Host slice", "identity"
    ),
    # --- ACP follow-ups: queue and steer (task #1740) --------------------------------
    ("POST", "/api/node/followups"): _cap(
        handler=_NODE,
        csrf=True,
        note="a node pulls queued follow-ups for its own claimed item only",
    ),
    ("POST", "/api/action/node-followup"): _cap(
        tools=("session_list", "crew_view"),
        handler=("identity", "body-conditional"),
        csrf=True,
        note="queue or steer a live ACP session; stricter than node-session, which has no "
        "profile ceiling: a profile without the session and crew views cannot send. "
        "crew_intervene is not required because the operator profile does not expose it "
        "(the operator intervenes through CSRF-gated routes). The handler refuses "
        "non-ACP, finished or cancelling items and a node without follow-up support; "
        "steer adds no permission, since the follow-up runs under the session's own "
        "permission relay",
    ),
    # --- Messages pane (task #1864) ---------------------------------------------------
    # Reads take the message_list ceiling and its scope: the operator login and the
    # chief-of-staff profile see every conversation, other identities their own mail.
    # The verified human operator passes the ceiling (operator=True): the Messages pane
    # is the operator's messaging surface, and the operator profile holds no message
    # tools over MCP. An agent on the operator profile does not pass.
    ("GET", "/api/messages/conversations"): _cap(
        tools=("message_list",),
        operator=True,
        handler=_ID,
        note="read-only; scoped like message_list and never changes read state",
    ),
    ("GET", "/api/messages/conversations/{conversation_id}"): _cap(
        tools=("message_list",),
        operator=True,
        handler=_ID,
        note="read-only; 404 for a conversation the caller has no visible message in",
    ),
    ("POST", "/api/messages/conversations/{conversation_id}/handoff"): _cap(
        tools=("task_add", "message_list"),
        operator=True,
        handler=("identity", "body-conditional"),
        delegates=("task_add", "task_update"),
        csrf=True,
        note="creates or links a task that references the conversation by id (no "
        "message copied); linking ({task_id}) also needs task_update in the caller's "
        "own profile; 404 for a conversation the caller cannot see",
    ),
    ("GET", "/api/messages/conversations/{conversation_id}/handoffs"): _cap(
        tools=("message_list",),
        operator=True,
        handler=_ID,
        note="read-only; tasks a visible conversation was handed off to",
    ),
    ("GET", "/api/messages/sessions"): _cap(
        tools=("session_list",),
        operator=True,
        handler=_ID,
        note="live message targets for the composer; no paths or transcripts",
    ),
    ("POST", "/api/messages"): _cap(
        tools=("message_send",),
        operator=True,
        handler=("identity", "agent-access", "approval"),
        delegates=("message_send",),
        csrf=True,
        note="the sender is the verified identity, never a body field; idempotent on "
        "message_id, and a message_id another sender holds answers 409",
    ),
    # --- resume and adopt harness sessions (task #1741) ------------------------------
    ("GET", "/api/nodes/native-sessions"): _cap(
        tools=("session_list", "crew_view"),
        handler=("identity",),
        note="read-only list of native Claude Code / Codex sessions each node discovered "
        "inside its worktree roots: metadata only, never transcript content",
    ),
    ("POST", "/api/action/node-resume"): _cap(
        tools=("session_list", "crew_view"),
        handler=("identity", "approval", "body-conditional"),
        csrf=True,
        note="reopen a finished node session (ACP session/resume or session/load, or the "
        "native resume flag) as a new approval-gated dispatch; refused, never downgraded "
        "to a fresh session, when the agent or node cannot resume; an adopted session "
        "stays with the identity that adopted it",
    ),
    ("POST", "/api/action/node-adopt"): _cap(
        tools=("session_list", "crew_view"),
        handler=("identity", "approval", "body-conditional"),
        csrf=True,
        note="adopt a discovered native session under the caller's verified identity "
        "(idempotent on harness and session id) and queue its resume behind the approval "
        "gate; a session attributed to another label, or whose task another label holds, "
        "is refused",
    ),
    # --- Control Center v2 knowledge and work adapters (tasks #2466, #2469) ----------
    # Each calls one registered tool through its guarded wrapper (cc_knowledge,
    # cc_work), so the tool's policy, approval and rate-limit gates still run; the
    # verified human operator passes the ceiling the operator profile lacks.
    ("GET", "/api/memory/note"): _cap(
        tools=("memory_get",),
        operator=True,
        delegates=("memory_get",),
        note="read-only memory_get(count_access=False): browsing is not a recall; 404 for an "
        "unknown key",
    ),
    ("GET", "/api/vault/links"): _open(read_tools=("vault_links",)),
    ("GET", "/api/vault/list"): _cap(
        tools=("vault_list",),
        operator=True,
        delegates=("vault_list",),
        note="read-only vault_list; the vault resolver bounds the path",
    ),
    ("POST", "/api/action/memory/save"): _cap(
        tools=("memory_save",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("memory_save",),
        csrf=True,
        note="author is the verified identity; replacing another author's note needs "
        "overwrite; a stale expected_revision answers 409",
    ),
    ("POST", "/api/action/memory/delete"): _cap(
        tools=("memory_delete",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("memory_delete",),
        csrf=True,
        note="memory_delete keeps its own verified-identity and approval gates",
    ),
    ("POST", "/api/action/memory/restore"): _cap(
        tools=("memory_restore",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("memory_restore",),
        csrf=True,
        note="memory_restore runs its own policy and approval gates; a deleted note is "
        "restored in place by its owner or a verified human",
    ),
    ("POST", "/api/action/memory/promote"): _cap(
        tools=("memory_promote",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("memory_promote",),
        csrf=True,
        note="the promoting agent is the verified identity",
    ),
    ("POST", "/api/action/memory/feedback"): _cap(
        tools=("memory_feedback",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("memory_feedback",),
        csrf=True,
        note="memory_feedback runs its own policy and approval gates",
    ),
    ("POST", "/api/action/vault/write"): _cap(
        tools=("vault_write",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("vault_write",),
        csrf=True,
        note="vault write dirs and the locked-note guard stay in vault_write; an optional "
        "Idempotency-Key replays the first answer",
    ),
    ("POST", "/api/action/vault/journal"): _cap(
        tools=("vault_journal",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("vault_journal",),
        csrf=True,
        note="the journal agent is the verified identity; an optional Idempotency-Key replays "
        "the first answer",
    ),
    # One route and one scope per plan_manage action: a scope requires every action
    # it lists, so alternatives never share one (spec section 8, task #2469).
    ("POST", "/api/action/plan/create"): _cap(
        actions=(("plan_manage", "plan_create"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
        note="an optional Idempotency-Key replays the first answer (task #2516)",
    ),
    ("POST", "/api/action/plan/update"): _cap(
        actions=(("plan_manage", "plan_update"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/checkpoint"): _cap(
        actions=(("plan_manage", "plan_checkpoint"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/resume"): _cap(
        actions=(("plan_manage", "plan_resume"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/retry"): _cap(
        actions=(("plan_manage", "plan_step_retry"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/claim"): _cap(
        actions=(("plan_manage", "plan_step_claim"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/handoff"): _cap(
        actions=(("plan_manage", "plan_handoff"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/close"): _cap(
        actions=(("plan_manage", "plan_close"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/delete"): _cap(
        actions=(("plan_manage", "plan_delete"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    # Plan structure (task #2480): one action per route, as above.
    ("POST", "/api/action/plan/step-add"): _cap(
        actions=(("plan_manage", "plan_step_add"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
        note="a claimed or done step is never renumbered; joint plans refused",
    ),
    ("POST", "/api/action/plan/step-edit"): _cap(
        actions=(("plan_manage", "plan_step_edit"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
        note="a claimed or done step keeps its text",
    ),
    ("POST", "/api/action/plan/step-remove"): _cap(
        actions=(("plan_manage", "plan_step_remove"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
        note="refused while another step depends on it",
    ),
    ("POST", "/api/action/plan/reorder"): _cap(
        actions=(("plan_manage", "plan_step_reorder"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
        note="refused when a step moves ahead of its dependency",
    ),
    ("POST", "/api/action/plan/edit"): _cap(
        actions=(("plan_manage", "plan_edit"),),
        operator=True,
        handler=("identity", "approval"),
        delegates=("plan_manage",),
        csrf=True,
        note="title and description live in the plan's goal field",
    ),
    # Joint planning keeps its office-membership, peer-review and authored-comment
    # rules inside the tool; operator=True passes the profile ceiling only.
    ("POST", "/api/action/plan/joint/bind"): _cap(
        actions=(("plan_manage", "plan_joint_bind"),),
        operator=True,
        handler=("identity", "approval", "internal-authz"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/joint/propose"): _cap(
        actions=(("plan_manage", "plan_joint_propose"),),
        operator=True,
        handler=("identity", "approval", "internal-authz"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/joint/review"): _cap(
        actions=(("plan_manage", "plan_joint_review"),),
        operator=True,
        handler=("identity", "approval", "internal-authz"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/joint/decide"): _cap(
        actions=(("plan_manage", "plan_joint_decide"),),
        operator=True,
        handler=("identity", "approval", "internal-authz"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("POST", "/api/action/plan/joint/recover"): _cap(
        actions=(("plan_manage", "plan_joint_recover"),),
        operator=True,
        handler=("identity", "approval", "internal-authz"),
        delegates=("plan_manage",),
        csrf=True,
    ),
    ("GET", "/api/plans/{plan_id}/joint"): _cap(
        actions=(("plan_manage", "plan_joint_get"),),
        operator=True,
        handler=("identity", "internal-authz"),
        delegates=("plan_manage",),
        note="joint_get refuses a caller outside the plan's office",
    ),
    ("POST", "/api/action/task/claim"): _cap(
        tools=("task_claim",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("task_claim",),
        csrf=True,
        note="the claiming agent is the verified identity",
    ),
    ("POST", "/api/action/task/delete"): _cap(
        tools=("task_delete",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("task_delete",),
        csrf=True,
        note="task_delete keeps its own destructive gates; 404 when nothing was deleted",
    ),
    ("POST", "/api/action/session/end"): _cap(
        tools=("session_end",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("session_end",),
        csrf=True,
        note="session_end refuses a session the verified caller does not own",
    ),
    # --- Control Center v2 hub operations (task #2480) -------------------------------
    ("POST", "/api/action/goal/edit"): _cap(
        tools=("goal_manage",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("goal_manage",),
        csrf=True,
        note="goal_manage edit; author is the verified identity",
    ),
    ("POST", "/api/action/goal/link"): _cap(
        tools=("goal_manage",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("goal_manage",),
        csrf=True,
        note="goal_manage link; author is the verified identity",
    ),
    ("POST", "/api/action/goal/unlink"): _cap(
        tools=("goal_manage",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("goal_manage",),
        csrf=True,
        note="goal_manage unlink; author is the verified identity",
    ),
    ("POST", "/api/action/goal/close"): _cap(
        tools=("goal_manage",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("goal_manage",),
        csrf=True,
        note="goal_manage close; author is the verified identity",
    ),
    ("POST", "/api/action/goal/reopen"): _cap(
        tools=("goal_manage",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("goal_manage",),
        csrf=True,
        note="goal_manage reopen; author is the verified identity",
    ),
    ("GET", "/api/routines"): _cap(
        tools=("routine_list",),
        operator=True,
        delegates=("routine_list",),
        note="read-only routine_list plus each cron routine's next scheduled slot",
    ),
    ("POST", "/api/action/routine/add"): _cap(
        tools=("routine_add",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("routine_add",),
        csrf=True,
        note="an optional Idempotency-Key replays the first answer (task #2516)",
    ),
    ("POST", "/api/action/routine/pause"): _cap(
        tools=("routine_pause",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("routine_pause",),
        csrf=True,
    ),
    ("POST", "/api/action/routine/resume"): _cap(
        tools=("routine_pause",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("routine_pause",),
        csrf=True,
        note="routine_pause with paused=false",
    ),
    ("POST", "/api/action/routine/run"): _cap(
        tools=("routine_fire",),
        operator=True,
        handler=("identity", "approval", "body-conditional"),
        delegates=("routine_fire",),
        csrf=True,
        note="Run now with an Idempotency-Key; only the verified human operator may fire a "
        "cron routine (routines.operator_run_now); the bearer-only webhook route is separate",
    ),
    ("POST", "/api/action/langgraph/run"): _cap(
        tools=("langgraph_workflow_run", "task_update", "langgraph_run", "web_search"),
        operator=True,
        handler=("identity", "approval", "body-conditional"),
        delegates=("langgraph_workflow_run", "langgraph_run"),
        csrf=True,
        note="the workflow's build and merge hops write through task_update; engine research "
        "continues a paused langgraph_run (its governed read is web_search, its outcome note "
        "task_update) and the tool checks the run's owner or the verified operator; the "
        "handler binds a registered session of the verified caller for the one call; an "
        "optional Idempotency-Key starts the run at most once (task #2516)",
    ),
    ("POST", "/api/action/session/operator-end"): _cap(
        operator=True,
        handler=("identity", "human", "agent-access", "approval"),
        csrf=True,
        note="the verified human operator alone; the session.operator_end platform writer "
        "runs behind the governed write adapter and records owner and reason in the ledger",
    ),
    # --- Control Center v2 Home and Inbox (tasks #2466, #2514, webui/cc_inbox.py) ------
    # Work-proposal reads carry the coordination_manage action ceiling of the MCP read,
    # and the tool fences the tenant; the verified human operator passes the ceiling.
    ("GET", "/api/work-proposals"): _cap(
        actions=(("coordination_manage", "work_proposal_list"),),
        operator=True,
        handler=_ID,
        delegates=("coordination_manage",),
        note="read-only work_proposal_list, tenant-fenced by the tool",
    ),
    ("GET", "/api/work-proposals/{proposal_id}"): _cap(
        actions=(("coordination_manage", "work_proposal_get"),),
        operator=True,
        handler=_ID,
        delegates=("coordination_manage",),
        note="read-only work_proposal_get; 404 for an unknown or other-tenant proposal",
    ),
    ("POST", "/api/sessions/{id}/wake-decline"): _cap(
        operator=True,
        handler=_ID,
        csrf=True,
        note="declines one session's wake request with a reason (task #2514); the verified "
        "human operator's alone, like the wake grant, with no MCP twin",
    ),
    ("POST", "/api/action/review-hold"): _cap(
        operator=True,
        handler=_ID,
        csrf=True,
        note="records the operator's release or keep of a review hold (review_holds); a "
        "release of a red-tier hold is refused, because only the Ledger approval lifts it",
    ),
    # --- Control Center v2 agents: enrolment, connectors, org proposals (#2466, #2470) --
    # Enrolment calls agent_enrollment services, not registered tools, so those routes
    # are the verified human operator's alone and the handler checks it again.
    ("GET", "/api/agent-roster"): _cap(
        operator=True,
        handler=("identity", "human"),
        note="agents with office, profile, kind and token metadata (never a bearer or "
        "digest), tenant-scoped; Cache-Control: no-store",
    ),
    # Task #2523: the roster without its credential columns is a shared read, like
    # /api/org (which already lists office members with profile and last seen).
    ("GET", "/api/agent-seats"): _open(
        note="agent_enrollment.list_seats: office, profile, kind, added, lifecycle state and "
        "last seen per agent, tenant-scoped; no token source, status or dates",
    ),
    ("GET", "/api/agents/{label}/profile"): _cap(
        tools=("skill_registry",),
        operator=True,
        handler=("identity",),
        delegates=("skill_registry",),
        note="the profile ceiling and the skill registry's assignments (no content) for a "
        "label the tenant knows; skill_registry's ceiling, as GET /api/skills-teams reads",
    ),
    ("POST", "/api/action/agent/add"): _cap(
        operator=True,
        handler=("identity", "human", "agent-access", "approval", "internal-authz"),
        csrf=True,
        note="agent_enrollment.add_agent behind the access, policy and approval gates "
        "(operation agent_add); the service requires a lifecycle operator; the bearer is "
        "returned once with Cache-Control: no-store and never logged or stored by the route",
    ),
    ("POST", "/api/action/agent/remove"): _cap(
        operator=True,
        handler=("identity", "human", "agent-access", "approval", "internal-authz"),
        csrf=True,
        note="agent_enrollment.remove_agent behind the access, policy and approval gates "
        "(operation agent_remove): retires the label and revokes its tokens, session "
        "credentials and device sign-ins",
    ),
    ("POST", "/api/action/connector/register"): _cap(
        tools=("horizon2_server_register",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("horizon2_server_register",),
        csrf=True,
        note="the tool forces a one-shot approval bound to the registration",
    ),
    ("POST", "/api/action/connector/register-package"): _cap(
        tools=("horizon2_package_register",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("horizon2_package_register",),
        csrf=True,
        note="the tool forces the horizon2_server_register approval for the package",
    ),
    ("POST", "/api/action/connector/authorize"): _cap(
        tools=("horizon2_connector_authorize",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("horizon2_connector_authorize",),
        csrf=True,
        note="the tool forces a one-shot approval bound to connector, scope, task and trace",
    ),
    ("POST", "/api/action/connector/revoke"): _cap(
        tools=("horizon2_server_revoke",),
        operator=True,
        handler=("identity", "approval"),
        delegates=("horizon2_server_revoke",),
        csrf=True,
        note="the tool forces a one-shot approval; an already revoked server answers 200",
    ),
    ("POST", "/api/action/org-proposal"): _cap(
        tools=("org_proposal",),
        operator=True,
        handler=("identity", "approval", "body-conditional", "internal-authz"),
        delegates=("org_proposal",),
        csrf=True,
        note="review follows the org_proposal ceiling; apply and reject are the verified "
        "human operator's; apply needs the proposal's approval row approved by a human",
    ),
}


def scope_for(method: str, path: str) -> Scope | None:
    """The declared scope for one served route, or None when it has no entry.

    ``path`` is the route template (``/api/sessions/{id}``), not a request URL.
    HEAD is served by the GET handler and shares its entry.
    """
    method = method.upper()
    if method == "HEAD":
        method = "GET"
    return ROUTE_SCOPES.get((method, path)) or ROUTE_SCOPES.get(("ANY", path))
