"""Control Center v2 agents routes: enrolment, connectors, org proposals (#2466, #2470).

Three groups, each a thin adapter; request and response shapes are in
``docs/control-center-api.md``.

* **Enrolment** (``GET /api/agent-roster``, ``POST /api/action/agent/add``,
  ``POST /api/action/agent/remove``) is the verified human operator's alone.
  ``GET /api/agent-seats`` is the roster without its credential columns, for any
  signed-in reader; ``GET /api/agents/{label}/profile`` is an agent's profile
  ceiling and assigned skills, on the ``skill_registry`` ceiling (task #2523).
  ``agent_enrollment`` holds services, not registered tools, so the scope is
  ``operator=True`` with no tools and each handler checks the operator again.
  Writes run the way the CLI runs them, behind the gates a registered write
  gets: the agent-access switch (with the operator's recovery exemption), the
  policy gate and the approval gate, on operations ``agent_add`` and
  ``agent_remove``. The service then checks the actor is a lifecycle operator.
  The bearer ``add`` issues is revealed once, into that one response, which is
  sent ``Cache-Control: no-store``. This module never logs it, never stores it
  and never puts it in an approval payload; a repeated add answers 409, never
  a second bearer.
* **Connectors** adapt the registered ``horizon2_*`` tools through
  ``api.call_tool``. Each forces its own one-shot approval, so the first call
  parks (202) and the identical repeat after approval runs it.
* **Org proposals** adapt the registered ``org_proposal`` tool (review, apply,
  reject). Apply and reject are decisions and are the verified human
  operator's; review follows the tool's profile ceiling. The work-proposal
  decision route already exists (``routes.action_work_proposal_decide``) and is
  reused, not twinned.

Every error answer carries a ``code`` (``ERROR_CODES``).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import (
    agent_access,
    agent_enrollment,
    agent_roster,
    edition,
    org_registry,
    policy,
    profiles,
)
from ..tools import approvals as approvals_tool
from ..tools import token_lifecycle
from . import api, routes
from .cc_knowledge import Field, adapt, bind, bind_query, call, identity_or_401, pending, read_body
from .cc_knowledge import spoofed as _spoofed

#: Sent on every enrolment answer; the add answer carries a bearer.
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

#: The ``code`` each error status carries when the cause has no narrower one.
ERROR_CODES = {
    400: "invalid_request",
    401: "authentication_required",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "body_too_large",
    429: "rate_limited",
    503: "unavailable",
}

OPERATOR_ONLY = "this action is reserved for the human operator"


def _coded(response: Response, headers: dict[str, str] | None = None) -> Response:
    """``response`` with a ``code`` on an error body (and ``headers`` added)."""
    status = response.status_code
    if status >= 400:
        try:
            body = json.loads(bytes(response.body))
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict) and "code" not in body:
            body["code"] = ERROR_CODES.get(status, "error")
            response = routes._json(body, status=status)
    if headers:
        response.headers.update(headers)
    return response


def _error(status: int, message: str, code: str | None = None, **extra: Any) -> Response:
    body = {"error": message, "code": code or ERROR_CODES.get(status, "error"), **extra}
    return routes._json(body, status=status)


def _operator_or_403(request: Request) -> str | Response:
    """The verified human operator's identity, or the refusal to answer."""
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return _coded(identity)
    if not routes._operator_recovery(request):
        return _error(403, OPERATOR_ONLY, "operator_required")
    return identity


def _classify(exc: BaseException, table: tuple[tuple[str, int, str], ...]) -> Response:
    """A ``ToolError`` as a coded answer: the first ``(substring, status, code)`` that
    appears in its message, else 400 ``invalid_request``."""
    text = str(exc)
    lowered = text.lower()
    for needle, status, code in table:
        if needle in lowered:
            return _error(status, text[:500], code)
    return _error(400, text[:500], "invalid_request")


# --- enrolment ----------------------------------------------------------------------

_ADD_FIELDS = (
    Field("label", required=True, limit=64),
    Field("office", required=True, limit=100),
    Field("profile", limit=64),
    Field("kind", limit=16, choices=agent_roster.KINDS),
    Field("expires_days", "int", limit=3_650),
    Field("note", limit=500),
    Field("allow_plaintext_at_rest", "bool"),
)

_REMOVE_FIELDS = (
    Field("label", required=True, limit=64),
    Field("reason", required=True, limit=2_000),
    Field("force_release", "bool"),
)

_ADD_ERRORS = (
    ("is already added", 409, "already_added"),
    ("already has an active managed token", 409, "already_added"),
    ("is retired", 409, "retired"),
    ("ua_managed_secret_keys is not set", 409, "sealed_store_required"),
    ("that is an operator action", 403, "operator_required"),
)

_REMOVE_ERRORS = (
    ("is not a known agent label", 404, "not_found"),
    ("holds doing claims", 409, "claims_held"),
    ("has not registered the stop handler", 503, "unavailable"),
    ("that is an operator action", 403, "operator_required"),
)


def _governed(operation: str, payload: dict[str, Any], actor: str, write: Callable[[], Any]):
    """Run one enrolment write behind the gates a registered write gets.

    The agent-access switch (the operator keeps the recovery exemption), the
    policy gate and the approval gate run on ``operation`` with ``payload``,
    which holds the validated body plus ``actor`` and never a secret. As in
    ``api._registered_platform_write``, a pass a policy rule already consumed
    is not asked for twice, and a refusal before the write restores it.
    """
    from .. import gateway, safety

    agent_access.enforce(operation, read_only=False, production=True, human_recovery=True)
    with gateway.invocation(operation), approvals_tool.approval_invocation() as receipts:
        safety.check_policy_gate(operation, payload, originator=actor)
        policy_consumed = any(
            isinstance(receipt, dict) and receipt.get("approval_id") is not None
            for receipt in receipts.values()
        )
        if not policy_consumed:
            approvals_tool.check_approval_gate(operation, payload, requested_by=actor)
        approvals_tool.mark_governed_execution()
        return write()


async def _run_governed(
    operation: str,
    payload: dict[str, Any],
    actor: str,
    write: Callable[[], Any],
    errors: tuple[tuple[str, int, str], ...],
) -> Any | Response:
    try:
        return await anyio.to_thread.run_sync(lambda: _governed(operation, payload, actor, write))
    except approvals_tool.ApprovalPendingError as exc:
        return pending(operation, exc)
    except policy.PolicyViolation as exc:
        if exc.reason_code in routes._THROTTLED_REASONS:
            return _error(429, str(exc)[:300], "rate_limited")
        return _error(403, str(exc)[:300], "policy_denied")
    except agent_access.AgentAccessDenied as exc:
        return _error(403, str(exc)[:300], "agent_access_denied")
    except ToolError as exc:
        typed = routes._typed_error(exc)
        if isinstance(typed, approvals_tool.ApprovalPendingError):
            return pending(operation, typed)
        return _classify(exc, errors)


async def _enrolment_body(
    request: Request, fields: tuple[Field, ...]
) -> tuple[str, dict[str, Any]] | Response:
    """Guard, operator check and validated body for an enrolment write."""
    guard = routes._guard_mutation(request)
    if guard is not None:
        return _coded(guard)
    identity = _operator_or_403(request)
    if isinstance(identity, Response):
        return identity
    body = await read_body(request)
    if isinstance(body, Response):
        return _coded(body)
    refused = _spoofed(body, identity, ("actor",))
    if refused is not None:
        return _coded(refused)
    kwargs = bind(body, fields, extra=frozenset({"actor"}))
    if isinstance(kwargs, str):
        return _error(400, kwargs, "invalid_request")
    return identity, kwargs


def _offices() -> list[dict[str, str]]:
    try:
        return [
            {"id": o["id"], "name": o.get("name") or o["id"]} for o in org_registry.load_registry()
        ]
    except Exception:  # noqa: BLE001 - the roster still lists agents without the chart
        return []


async def agent_roster_list(request: Request) -> Response:
    """GET /api/agent-roster — every agent with office, profile, kind and token dates.

    Token metadata only (source, status, issue and expiry dates); never a bearer
    or a digest. The operator's alone. ``offices`` and ``sealed_at_rest`` feed the
    add form.
    """
    guard = routes._guard(request)
    if guard is not None:
        return _coded(guard, NO_STORE)
    identity = _operator_or_403(request)
    if isinstance(identity, Response):
        return _coded(identity, NO_STORE)
    kwargs = bind_query(request, ())
    if isinstance(kwargs, str):
        return _coded(_error(400, kwargs, "invalid_request"), NO_STORE)
    rows, offices = await anyio.to_thread.run_sync(
        lambda: (agent_enrollment.list_agents(), _offices())
    )
    payload = {
        "agents": rows,
        "count": len(rows),
        "offices": offices,
        "kinds": list(agent_roster.KINDS),
        "default_profile": agent_enrollment.DEFAULT_PROFILE,
        "sealed_at_rest": token_lifecycle.sealed_at_rest(),
        "org": edition.tenant_org_id(),
    }
    return routes._json(payload, headers=NO_STORE)


async def agent_seats(request: Request) -> Response:
    """GET /api/agent-seats — every agent's office seat and host kind, for any signed-in reader.

    The roster without its credential columns (task #2523): the rows are
    ``agent_enrollment.list_seats`` (``SEAT_FIELDS``), so a viewer who is not the
    operator sees the hub's office and ``local``/``cloud`` kind instead of
    guessing them from the label. Tenant-scoped like the roster.
    """
    guard = routes._guard(request)
    if guard is not None:
        return _coded(guard)
    kwargs = bind_query(request, ())
    if isinstance(kwargs, str):
        return _error(400, kwargs, "invalid_request")
    rows, offices = await anyio.to_thread.run_sync(
        lambda: (agent_enrollment.list_seats(), _offices())
    )
    return routes._json(
        {
            "agents": rows,
            "count": len(rows),
            "offices": offices,
            "kinds": list(agent_roster.KINDS),
            "org": edition.tenant_org_id(),
        }
    )


#: Where an agent's installed skills would come from, and why the hub cannot say.
SKILLS_INSTALLED_GAP = (
    "the hub does not know which skills are installed on an agent's machine: no "
    "harness reports its skill directories. 'assigned' is the hub's authority, the "
    "skill registry's assignments that dispatch injects"
)


async def agent_profile(request: Request) -> Response:
    """GET /api/agents/{label}/profile — the agent's profile ceiling and assigned skills.

    The ceiling is the identity's default profile (``profiles.default_for_identity``:
    the code default, lowered by a seat or a committed overlay); a non-human caller
    cannot activate a more privileged one. ``tools`` is every registered tool that
    profile exposes. Skills are the registry's resolution for the label (agent
    assignments over profile assignments), through the registered ``skill_registry``
    tool, without their content.
    """
    from .. import registry

    guard = routes._guard(request)
    if guard is not None:
        return _coded(guard)
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return _coded(identity)
    kwargs = bind_query(request, ())
    if isinstance(kwargs, str):
        return _error(400, kwargs, "invalid_request")
    label = request.path_params["label"]
    seat, seen = await anyio.to_thread.run_sync(
        lambda: (agent_enrollment.seat_for(label), registry.get_agent(label))
    )
    if seat is None and seen is None:
        return _error(404, "unknown agent", "not_found")
    profile = profiles.default_for_identity(label)
    tools = sorted(
        name for name in api.registered_tool_names() if profiles.tool_is_allowed_for(profile, name)
    )
    resolved = await call(
        "skill_registry", action="resolve", params={"agent_label": label, "profile": profile}
    )
    if resolved.status_code != 200:
        return _coded(resolved)
    skills = json.loads(bytes(resolved.body)).get("skills") or []
    return routes._json(
        {
            "agent": label,
            "profile": profile,
            "ceiling": {"profile": profile, "tools": tools, "tool_count": len(tools)},
            "skills": {
                "assigned": [
                    {key: skill.get(key) for key in ("name", "version", "hash", "source")}
                    for skill in skills
                ],
                "installed": None,
                "installed_known": False,
                "note": SKILLS_INSTALLED_GAP,
            },
        }
    )


def _add_conflict(label: str) -> str | None:
    """``agent_enrollment.add_conflict`` for a well-formed label; None otherwise (the
    governed call then reports the bad label)."""
    try:
        name = agent_enrollment._check_label(label)
    except ToolError:
        return None
    return agent_enrollment.add_conflict(name)


async def agent_add(request: Request) -> Response:
    """POST /api/action/agent/add — issue a bearer and seat the agent; bearer shown once."""
    bound = await _enrolment_body(request, _ADD_FIELDS)
    if isinstance(bound, Response):
        return _coded(bound, NO_STORE)
    identity, kwargs = bound
    payload = {**kwargs, "actor": identity}
    label = kwargs.pop("label")
    # A repeated add is refused before the approval gate, so it never queues an
    # approval; add_agent checks the same state again when it runs.
    conflict = await anyio.to_thread.run_sync(lambda: _add_conflict(label))
    if conflict is not None:
        return _coded(_classify(ToolError(conflict), _ADD_ERRORS), NO_STORE)
    result = await _run_governed(
        "agent_add",
        payload,
        identity,
        lambda: agent_enrollment.add_agent(label, actor=identity, **kwargs),
        _ADD_ERRORS,
    )
    if isinstance(result, Response):
        return _coded(result, NO_STORE)
    token = result.pop("token")
    body = {**result, "token": token.reveal(), "token_shown_once": True}
    return routes._json(body, headers=NO_STORE)


agent_add.identity_args = ("actor",)  # type: ignore[attr-defined]


async def agent_remove(request: Request) -> Response:
    """POST /api/action/agent/remove — retire the agent and revoke its credentials."""
    bound = await _enrolment_body(request, _REMOVE_FIELDS)
    if isinstance(bound, Response):
        return _coded(bound, NO_STORE)
    identity, kwargs = bound
    payload = {**kwargs, "actor": identity}
    label = kwargs.pop("label")

    def write() -> dict[str, Any]:
        # Retirement must cancel in-flight work: these imports register the
        # dispatch and node stop handlers (agent_lifecycle.REQUIRED_STOP_HANDLERS),
        # as the CLI does.
        from .. import node_queue  # noqa: F401
        from ..dispatch import coordinator  # noqa: F401

        return agent_enrollment.remove_agent(label, actor=identity, **kwargs)

    result = await _run_governed("agent_remove", payload, identity, write, _REMOVE_ERRORS)
    if isinstance(result, Response):
        return _coded(result, NO_STORE)
    return routes._json(result, headers=NO_STORE)


agent_remove.identity_args = ("actor",)  # type: ignore[attr-defined]


# --- connectors ---------------------------------------------------------------------

_CONNECTOR_ERRORS = (
    ("unregistered horizon 2 server", 404, "not_found"),
    ("is already registered", 409, "already_registered"),
)

_SERVER_ID = Field("server_id", required=True, limit=128)


def _coded_handler(
    handler: Callable[[Request], Awaitable[Response]],
    table: tuple[tuple[str, int, str], ...],
) -> Callable[[Request], Awaitable[Response]]:
    """``handler`` whose plain 400 answers are reclassified by ``table`` and coded."""

    async def wrapped(request: Request) -> Response:
        response = await handler(request)
        if response.status_code == 400:
            body = json.loads(bytes(response.body))
            if isinstance(body, dict) and "code" not in body:
                response = _classify(ToolError(str(body.get("error", ""))), table)
        return _coded(response)

    wrapped.__name__ = handler.__name__
    wrapped.identity_args = getattr(handler, "identity_args", ())  # type: ignore[attr-defined]
    return wrapped


def _role_checked(
    tool: str, handler: Callable[[Request], Awaitable[Response]]
) -> Callable[[Request], Awaitable[Response]]:
    """``handler`` behind an explicit check of the roles its route declares (#2517).

    The connector routes admit the profiles whose ceiling exposes ``tool``
    (developer, steward, chief of staff) and the verified human operator, as
    ``route_scopes`` declares; the scope wrapper enforces that first and this
    repeats it in the handler, as the operator-only enrolment routes do. A request
    with no identity is left to the handler's own 401.
    """

    async def checked(request: Request) -> Response:
        if request.scope.get("ua_identity") and not routes._operator_recovery(request):
            profile = routes._request_profile(request)
            if not profiles.tool_is_allowed_for(profile, tool):
                return _error(403, f"{tool} is unavailable to this profile", "forbidden")
        return await handler(request)

    checked.__name__ = handler.__name__
    checked.identity_args = getattr(handler, "identity_args", ())  # type: ignore[attr-defined]
    return checked


def _connector(tool: str, fields: tuple[Field, ...]) -> Callable[[Request], Awaitable[Response]]:
    return _role_checked(tool, _coded_handler(adapt(tool, fields), _CONNECTOR_ERRORS))


connector_register = _connector(
    "horizon2_server_register",
    (
        _SERVER_ID,
        Field("connector", required=True, limit=100),
        Field("provenance", required=True, limit=500),
        Field("scopes", "strlist", required=True, limit=50, item_limit=200),
        Field("review_ref", required=True, limit=500),
    ),
)
connector_register_package = _connector(
    "horizon2_package_register",
    (
        Field("package", required=True, limit=100),
        Field("provenance", required=True, limit=500),
        Field("review_ref", required=True, limit=500),
        Field("server_id", limit=128),
        Field("scopes", "strlist", limit=50, item_limit=200),
    ),
)
connector_authorize = _connector(
    "horizon2_connector_authorize",
    (
        _SERVER_ID,
        Field("scope", required=True, limit=200),
        Field("task_id", "int", required=True, limit=2**31 - 1),
        Field("trace_id", required=True, limit=128),
        Field("connection_ref", limit=200),
    ),
)
connector_revoke = _connector("horizon2_server_revoke", (_SERVER_ID,))


# --- org proposals ------------------------------------------------------------------

#: Per action: the body fields it accepts beyond ``action`` and ``proposal_id``, and
#: the tool parameter the verified identity fills.
ORG_PROPOSAL_ACTIONS: dict[str, tuple[tuple[Field, ...], str]] = {
    "review": ((Field("notes", limit=4_000),), "reviewed_by"),
    "apply": ((Field("reason", limit=2_000),), "applied_by"),
    "reject": ((Field("reason", limit=2_000),), "rejected_by"),
}
#: Decisions: the verified human operator's alone, whatever the caller's profile.
ORG_PROPOSAL_DECISIONS = frozenset({"apply", "reject"})
_ORG_IDENTITY_ARGS = tuple(arg for _fields, arg in ORG_PROPOSAL_ACTIONS.values())

_ORG_ERRORS = (
    ("no org proposal with id", 404, "not_found"),
    ("already in terminal state", 409, "conflict"),
    ("has already been applied", 409, "conflict"),
    ("was rejected and cannot be applied", 409, "conflict"),
    ("cannot review proposal", 409, "conflict"),
    ("cannot reject proposal", 409, "conflict"),
    ("cannot mark as reviewed", 409, "conflict"),
    ("separation of duties", 403, "separation_of_duties"),
    ("was denied", 409, "approval_denied"),
)


async def org_proposal_decide(request: Request) -> Response:
    """POST /api/action/org-proposal — review, apply or reject one org proposal.

    Through the registered ``org_proposal`` tool, so its wrapper approval gate and
    its own checks run (separation of duties, the human approval row that apply
    needs). Apply before that row is approved answers 202 with its approval id.
    """
    guard = routes._guard_mutation(request)
    if guard is not None:
        return _coded(guard)
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return _coded(identity)
    body = await read_body(request)
    if isinstance(body, Response):
        return _coded(body)
    action = body.get("action")
    if not isinstance(action, str) or action not in ORG_PROPOSAL_ACTIONS:
        return _error(400, f"action must be one of {', '.join(ORG_PROPOSAL_ACTIONS)}")
    if action in ORG_PROPOSAL_DECISIONS and not routes._operator_recovery(request):
        return _error(403, OPERATOR_ONLY, "operator_required")
    fields, identity_arg = ORG_PROPOSAL_ACTIONS[action]
    refused = _spoofed(body, identity, _ORG_IDENTITY_ARGS)
    if refused is not None:
        return _coded(refused)
    others = frozenset(a for a in _ORG_IDENTITY_ARGS if a != identity_arg)
    if others & body.keys():
        names = ", ".join(sorted(others & body.keys()))
        return _error(400, f"unknown field(s) for {action}: {names}")
    params = bind(
        body,
        (Field("proposal_id", required=True, limit=128), *fields),
        extra=frozenset({"action", identity_arg}),
    )
    if isinstance(params, str):
        return _error(400, params)
    params[identity_arg] = identity
    response = await call("org_proposal", action=action, params=params)
    if response.status_code == 400:
        message = str(json.loads(bytes(response.body)).get("error", ""))
        if action == "apply" and "not 'approved'" in message:
            return await _apply_waiting(params["proposal_id"], message)
        return _classify(ToolError(message), _ORG_ERRORS)
    return _coded(response)


org_proposal_decide.identity_args = _ORG_IDENTITY_ARGS  # type: ignore[attr-defined]


async def _apply_waiting(proposal_id: str, message: str) -> Response:
    """Apply refused because the proposal's approval row is still open: 202 with its id.

    The row the proposal parked when it was filed is the approval step; the UI
    links it in the Inbox and the operator repeats the apply once it is approved.
    """
    from .. import self_organization

    proposal = await anyio.to_thread.run_sync(
        lambda: self_organization.get_org_proposal(proposal_id)
    )
    approval_id = (proposal or {}).get("approval_id")
    if approval_id is None:
        return _error(409, message[:500], "conflict")
    return routes._json(
        {
            "status": "approval_pending",
            "approval_id": int(approval_id),
            "code": approvals_tool.ApprovalPendingError.code,
            "tool": self_organization.APPLY_TOOL,
            "error": message[:500],
        },
        status=202,
    )


ROUTES: list[Route] = [
    Route("/api/agent-roster", agent_roster_list, methods=["GET"]),
    Route("/api/agent-seats", agent_seats, methods=["GET"]),
    Route("/api/agents/{label}/profile", agent_profile, methods=["GET"]),
    Route("/api/action/agent/add", agent_add, methods=["POST"]),
    Route("/api/action/agent/remove", agent_remove, methods=["POST"]),
    Route("/api/action/connector/register", connector_register, methods=["POST"]),
    Route("/api/action/connector/register-package", connector_register_package, methods=["POST"]),
    Route("/api/action/connector/authorize", connector_authorize, methods=["POST"]),
    Route("/api/action/connector/revoke", connector_revoke, methods=["POST"]),
    Route("/api/action/org-proposal", org_proposal_decide, methods=["POST"]),
]
