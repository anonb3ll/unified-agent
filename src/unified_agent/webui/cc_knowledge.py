"""Control Center v2 knowledge routes: memory notes and vault writes (tasks #2466, #2469).

Each handler is a thin adapter over one registered hub tool, called through
``api.call_tool`` so the tool's guarded wrapper still runs its profile ceiling,
policy gate, approval gate, rate limit and safety resolvers. Nothing here reads
or writes a path or a row itself. The route scope (``route_scopes.ROUTE_SCOPES``)
admits the caller first; this module only validates the body, binds identity
fields to the verified request identity and maps the tool's answer to HTTP.

The shared adapter helpers (``Field``, ``adapt``) are also used by
``cc_work``. Request and response shapes are in ``docs/control-center-api.md``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import agent_access, profiles, storage
from ..tools import approvals as approvals_tool
from . import api, routes

#: Largest JSON body any of these routes reads (bytes).
MAX_BODY_BYTES = 256_000
#: Largest serialized size of an object-valued field (bytes).
MAX_OBJECT_BYTES = 20_000

#: ToolError message prefixes that mean "the named object does not exist" (404).
#: Closed list, taken from the adapted tools' own wording.
_NOT_FOUND_PREFIXES = (
    "no memory note with id",
    "no such session",
    "no plan with id",
)


@dataclass(frozen=True)
class Field:
    """One accepted body or query field.

    ``kind`` is ``str``, ``int``, ``bool``, ``float``, ``strlist`` (list of strings),
    ``list`` (any JSON list) or ``object`` (JSON object). ``limit`` bounds a string's
    length, a list's item count or an integer's maximum. ``arg`` renames the field
    for the tool call. ``choices`` closes a string to a fixed vocabulary.
    """

    name: str
    kind: str = "str"
    required: bool = False
    limit: int = 2_000
    arg: str | None = None
    choices: tuple[str, ...] = ()
    item_limit: int = 200


def _check(field: Field, value: Any) -> str | None:
    """An error message for one value, or None when it fits the field."""
    name = field.name
    if field.kind == "str":
        if not isinstance(value, str):
            return f"{name} must be a string"
        if field.required and not value.strip():
            return f"{name} must not be empty"
        if len(value) > field.limit:
            return f"{name} is longer than {field.limit} characters"
        if field.choices and value not in field.choices:
            return f"{name} must be one of {', '.join(field.choices)}"
    elif field.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{name} must be an integer"
        if value < 0 or value > field.limit:
            return f"{name} must be between 0 and {field.limit}"
    elif field.kind == "bool":
        if not isinstance(value, bool):
            return f"{name} must be true or false"
    elif field.kind == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return f"{name} must be a number"
        if not math.isfinite(value):
            return f"{name} must be finite"
    elif field.kind in ("strlist", "list"):
        if not isinstance(value, list):
            return f"{name} must be a list"
        if len(value) > field.limit:
            return f"{name} has more than {field.limit} items"
        if field.kind == "strlist" and any(
            not isinstance(item, str) or len(item) > field.item_limit for item in value
        ):
            return f"{name} items must be strings of at most {field.item_limit} characters"
        if field.kind == "list" and len(json.dumps(value)) > MAX_OBJECT_BYTES:
            return f"{name} is larger than {MAX_OBJECT_BYTES} bytes"
    elif field.kind == "object":
        if not isinstance(value, dict):
            return f"{name} must be a JSON object"
        if len(json.dumps(value)) > MAX_OBJECT_BYTES:
            return f"{name} is larger than {MAX_OBJECT_BYTES} bytes"
    else:  # pragma: no cover - a programming error in a field table
        raise AssertionError(f"unknown field kind {field.kind!r}")
    return None


def bind(
    source: Mapping[str, Any], fields: tuple[Field, ...], *, extra: frozenset[str] = frozenset()
) -> dict[str, Any] | str:
    """Validate ``source`` against ``fields``: the tool kwargs, or an error message.

    Unknown keys are refused (the MCP argument models are ``extra="forbid"``);
    ``extra`` names keys the caller handles itself (``action``, identity fields).
    ``None`` counts as absent.
    """
    known = {field.name for field in fields} | extra
    unknown = sorted(key for key in source if key not in known)
    if unknown:
        return f"unknown field(s): {', '.join(unknown)}; allowed: {', '.join(sorted(known))}"
    out: dict[str, Any] = {}
    for field in fields:
        value = source.get(field.name)
        if value is None:
            if field.required:
                return f"missing {field.name}"
            continue
        error = _check(field, value)
        if error is not None:
            return error
        out[field.arg or field.name] = value
    return out


def _query_value(field: Field, raw: str) -> Any:
    if field.kind == "int":
        try:
            return int(raw)
        except ValueError:
            return raw  # _check reports the type
    if field.kind == "bool":
        return {"1": True, "true": True, "0": False, "false": False}.get(raw.lower(), raw)
    return raw


def bind_query(request: Request, fields: tuple[Field, ...]) -> dict[str, Any] | str:
    """``bind`` over query parameters, converting int and bool text first."""
    by_name = {field.name: field for field in fields}
    source: dict[str, Any] = {}
    for key, raw in request.query_params.items():
        field = by_name.get(key)
        source[key] = _query_value(field, raw) if field is not None else raw
    return bind(source, fields)


async def read_body(request: Request) -> dict[str, Any] | Response:
    """The JSON object body, or an error response.

    The size cap is enforced while the body streams (``routes._read_capped_body``):
    a declared or streamed body over ``MAX_BODY_BYTES`` answers 413 without being
    buffered.
    """
    raw, too_big = await routes._read_capped_body(request, MAX_BODY_BYTES)
    if too_big is not None:
        return too_big
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return routes._json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return routes._json({"error": "request body must be a JSON object"}, status=400)
    return body


def identity_or_401(request: Request) -> str | Response:
    identity = routes._writer_identity(request)
    if identity is None:
        return routes._json({"error": "authenticated identity required"}, status=401)
    return identity


def spoofed(body: Mapping[str, Any], identity: str, keys: tuple[str, ...]) -> Response | None:
    """400 when the body names an identity other than the verified one.

    The identity fields are always filled from the verified request identity; a
    body may repeat that identity but never name another.
    """
    for key in keys:
        if key in body and body[key] is not None and body[key] != identity:
            return routes._json(
                {"error": f"{key} is taken from the authenticated identity"}, status=400
            )
    return None


def _not_found(exc: BaseException) -> bool:
    text = str(exc).strip().lower()
    return text.startswith(_NOT_FOUND_PREFIXES)


def _revision_conflict(exc: BaseException) -> BaseException | None:
    """The optimistic-revision conflict behind ``exc``, if any.

    A joint plan write that lost the revision race raises a ``ToolError`` that
    carries ``expected_revision`` and ``current_revision`` (``joint_planning``).
    It has no typed code, so without this it would answer 400 and the caller
    could not tell "reload and retry" from "bad request". Matched by shape, so
    this module does not import the planning component.
    """
    for candidate in (exc, exc.__cause__):
        if (
            candidate is not None
            and isinstance(getattr(candidate, "expected_revision", None), int)
            and isinstance(getattr(candidate, "current_revision", None), int)
        ):
            return candidate
    return None


async def call(tool: str, /, **kwargs: Any) -> Response:
    """Run one registered tool through its guarded wrapper and map the outcome.

    * the tool's result: 200 with the result as the body
    * ``ApprovalPendingError``: 202 ``{status: approval_pending, approval_id, code, tool}``
    * agent-access refusal: 403
    * ``ToolError`` naming a missing object: 404; a conflict: 409; any other: 400
    """
    try:
        result = await api.call_tool(tool, **kwargs)
    except approvals_tool.ApprovalPendingError as exc:
        return pending(tool, exc)
    except agent_access.AgentAccessDenied as exc:
        return routes._json({"error": str(exc)[:300]}, status=403)
    except ToolError as exc:
        typed = routes._typed_error(exc)
        if isinstance(typed, approvals_tool.ApprovalPendingError):
            return pending(tool, typed)
        if typed is None and _not_found(exc):
            return routes._json({"error": str(exc)[:300]}, status=404)
        stale = _revision_conflict(exc) if typed is None else None
        if stale is not None:
            return routes._json(
                {
                    "error": str(exc)[:300],
                    "code": "conflict",
                    "expected_revision": stale.expected_revision,
                    "current_revision": stale.current_revision,
                },
                status=409,
            )
        if typed is not None and typed.code == "conflict" and typed.data:
            # A compare-and-set refusal (memory_save expected_revision) carries
            # the current revision, so the client can reload without guessing.
            return routes._json({"error": str(exc)[:300], **typed.wire()}, status=409)
        return routes._json_body_error(exc, status=400)
    return routes._json(result)


#: How long a settled write receipt is kept (``Idempotency-Key``, tasks #2515, #2516),
#: and when an unsettled reservation counts as abandoned. The same windows as Run now.
RECEIPT_TTL = timedelta(days=7)
RECEIPT_PENDING_STALE = timedelta(minutes=10)


def _idempotency_key(request: Request) -> str | None | Response:
    """The request's ``Idempotency-Key`` header: None when absent, 400 when malformed."""
    key = request.headers.get("idempotency-key")
    if key is None:
        return None
    if (
        not key
        or len(key) > 200
        or key != key.strip()
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in key)
    ):
        return routes._json(
            {
                "error": "Idempotency-Key must be 1-200 characters without surrounding "
                "whitespace or control characters"
            },
            status=400,
        )
    return key


def freeze_success(response: Response) -> bool:
    """The default answer to freeze: a 2xx that is not a parked approval.

    A parked approval (``routes._is_parked_approval``) ran nothing, so it is
    dropped and the identical repeat after the decision runs the write.
    """
    if not 200 <= response.status_code < 300:
        return False
    if response.status_code != 202:
        return True
    try:
        body = json.loads(bytes(response.body))
    except ValueError:
        return True
    return not routes._is_parked_approval(body)


async def respond_once(
    identity: str,
    key: str,
    name: str,
    args: Mapping[str, Any],
    run: Callable[[], Awaitable[Response]],
    *,
    freeze: Callable[[Response], bool] = freeze_success,
    check: str = "the object it writes",
) -> Response:
    """Run ``run`` at most once per ``Idempotency-Key`` (tasks #2515, #2516).

    The one keyed-write scheme the Control Center routes share; the receipt
    mechanics are ``storage.request_receipt_*`` (also Run now's). The key is
    scoped to the tenant (``storage.idempotency_storage_key``) and to the
    verified identity, and bound to the request that first used it (a digest of
    ``name`` and ``args``, the validated request). The reservation is committed
    before ``run`` starts, so a start that spawns work has its receipt on disk
    first.

    * The first answer ``freeze`` accepts (default: a 2xx that is not a parked
      approval) is frozen; every repeat gets it back and nothing runs again.
    * Any other answer is classified by phase, not by status code
      (``approvals.execution_probe``): when no governed call crossed its
      execution boundary it was a refusal before any effect (the profile
      ceiling, agent access, policy, a parked approval), so the receipt is
      dropped and the identical repeat runs again. Once execution began, any
      other answer (a tool error, a ledger write that failed after the tool
      ran, a halted graph) keeps the receipt as outcome unknown.
    * The same key with a different request: 409 ``conflict``.
    * A repeat while the first is still running: 409 ``in_flight``.
    * ``run`` raised or the request was cancelled, or the reservation is still
      unsettled after ``RECEIPT_PENDING_STALE``: the outcome is unknown, so a
      reservation is never taken over and every repeat answers 409
      ``outcome_unknown``. ``check`` names what the caller reads to find out.
    """
    receipt_tool = f"cc_{name}"
    logical = f"cc.{name}:{identity}:{key}"
    digest = hashlib.sha256(
        json.dumps({"tool": name, "args": args}, sort_keys=True, default=str).encode()
    ).hexdigest()
    try:
        owner, stored = await anyio.to_thread.run_sync(
            lambda: storage.request_receipt_begin(
                receipt_tool,
                logical,
                digest,
                ttl=RECEIPT_TTL,
                stale=RECEIPT_PENDING_STALE,
                takeover=False,
            )
        )
    except storage.ReceiptConflict:
        return routes._json(
            {
                "error": "Idempotency-Key was already used for a different request; use a "
                "new key for a new write",
                "code": "conflict",
            },
            status=409,
        )
    except storage.ReceiptInFlight:
        return routes._json(
            {
                "error": "the request with this Idempotency-Key is still in flight; repeat "
                "it shortly to read its answer",
                "code": "in_flight",
            },
            status=409,
        )
    except storage.ReceiptOutcomeUnknown:
        return _outcome_unknown(check)
    except storage.AmbiguousIdempotencyReceipt as exc:
        return routes._json({"error": str(exc), "code": "ambiguous_receipt"}, status=409)
    if stored is not None:
        return routes._json(stored["body"], status=stored["status_code"])

    async def abandon() -> None:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(
                lambda: storage.request_receipt_abandon(receipt_tool, logical, digest, owner)
            )

    try:
        with approvals_tool.execution_probe() as probe:
            response = await run()
    except BaseException:
        # The write may have happened: never let this key run it again.
        await abandon()
        raise
    first = None
    if freeze(response):
        first = {"status_code": response.status_code, "body": json.loads(bytes(response.body))}
    elif probe["started"]:
        # Execution began, so its effects may be committed (task #2516, review #1538).
        await abandon()
        return response
    with anyio.CancelScope(shield=True):
        still_owner, canonical = await anyio.to_thread.run_sync(
            lambda: storage.request_receipt_finish(receipt_tool, logical, digest, owner, first)
        )
    if canonical is not None:
        return routes._json(canonical["body"], status=canonical["status_code"])
    if not still_owner:
        return _outcome_unknown(check)
    return response


async def call_once(identity: str, key: str, tool: str, /, **kwargs: Any) -> Response:
    """``call`` under an ``Idempotency-Key`` receipt (``respond_once``)."""
    return await respond_once(identity, key, tool, kwargs, lambda: call(tool, **kwargs))


def _outcome_unknown(check: str = "the object it writes") -> Response:
    return routes._json(
        {
            "error": "an earlier request with this Idempotency-Key started but its outcome "
            f"was not recorded, so it is not run again. Read {check} to see whether it "
            "happened, and use a new key to run it again",
            "code": "outcome_unknown",
        },
        status=409,
    )


def pending(tool: str, exc: approvals_tool.ApprovalPendingError) -> Response:
    return routes._json(
        {
            "status": "approval_pending",
            "approval_id": exc.approval_id,
            "code": exc.code,
            "tool": tool,
        },
        status=202,
    )


def adapt(
    tool: str,
    fields: tuple[Field, ...],
    *,
    identity_args: tuple[str, ...] = (),
    fixed: Mapping[str, Any] | None = None,
    after: Callable[[dict[str, Any]], Response | None] | None = None,
    check: Callable[[dict[str, Any]], str | None] | None = None,
    operator_vault_write: bool = False,
    idempotent: bool = False,
) -> Callable[[Request], Awaitable[Response]]:
    """A CSRF-guarded POST handler that adapts one tool.

    ``identity_args`` are tool arguments filled from the verified identity; the body
    may not name another value for them. ``check`` refuses a body the tool is known
    to refuse, before any gate parks it. ``operator_vault_write`` lets the verified
    human operator write the configured vault dirs for this call only
    (``profiles.http_operator_vault_write_authority``). ``after`` may turn a tool result into a
    different response (a not-found answer the tool reports in its result).
    ``idempotent`` accepts an optional ``Idempotency-Key`` header (``call_once``).
    """

    async def handler(request: Request) -> Response:
        guard = routes._guard_mutation(request)
        if guard is not None:
            return guard
        identity = identity_or_401(request)
        if isinstance(identity, Response):
            return identity
        key = _idempotency_key(request) if idempotent else None
        if isinstance(key, Response):
            return key
        body = await read_body(request)
        if isinstance(body, Response):
            return body
        refused = spoofed(body, identity, identity_args)
        if refused is not None:
            return refused
        kwargs = bind(body, fields, extra=frozenset(identity_args))
        if isinstance(kwargs, str):
            return routes._json({"error": kwargs}, status=400)
        problem = check(kwargs) if check is not None else None
        if problem is not None:
            return routes._json({"error": problem}, status=400)
        kwargs.update(fixed or {})
        kwargs.update({arg: identity for arg in identity_args})

        async def run() -> Response:
            if key is not None:
                return await call_once(identity, key, tool, **kwargs)
            return await call(tool, **kwargs)

        if operator_vault_write:
            # The verified human operator's vault writes (profiles docstring).
            with profiles.http_operator_vault_write_authority():
                response = await run()
        else:
            response = await run()
        if after is not None and response.status_code == 200:
            result = json.loads(bytes(response.body))
            replaced = after(result) if isinstance(result, dict) else None
            if replaced is not None:
                return replaced
        return response

    handler.__name__ = f"cc_{tool}"
    handler.identity_args = identity_args  # type: ignore[attr-defined]
    return handler


# --- memory -------------------------------------------------------------------------

_KEY = Field("key", required=True, limit=200, arg="id")

_NOTE_FIELDS = (
    _KEY,
    Field("as_of", limit=64),
    Field("include_versions", "bool"),
)


async def memory_note(request: Request) -> Response:
    """GET /api/memory/note?key= — one note's full body, tags, author, dates and links."""
    guard = routes._guard(request)
    if guard is not None:
        return guard
    kwargs = bind_query(request, _NOTE_FIELDS)
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    # A person opening a note is not a recall: it must not raise "Read N times"
    # or the access term (task #2515).
    return await call("memory_get", count_access=False, **kwargs)


memory_save = adapt(
    "memory_save",
    (
        Field("content", required=True, limit=200_000),
        Field("title", limit=500),
        Field("tags", "strlist", limit=50, item_limit=100),
        Field("key", limit=200, arg="id"),
        Field("append", "bool"),
        Field("overwrite", "bool"),
        Field("check_only", "bool"),
        Field("salience", "float"),
        Field("confidence", "float"),
        Field("expires_at", limit=64),
        Field("idempotency_key", limit=200),
        Field("links", "strlist", limit=50, item_limit=300),
        Field("expected_revision", limit=80),
    ),
    identity_args=("author",),
)

memory_delete = adapt("memory_delete", (_KEY,))
memory_restore = adapt("memory_restore", (_KEY,))
memory_promote = adapt(
    "memory_promote",
    (
        _KEY,
        Field("slug", limit=200),
        Field("summary", limit=4_000),
        Field("dry_run", "bool"),
    ),
    identity_args=("agent",),
    operator_vault_write=True,
)
memory_feedback = adapt(
    "memory_feedback",
    (
        _KEY,
        Field(
            "signal",
            required=True,
            limit=32,
            choices=("helpful", "not_helpful", "stale", "wrong"),
        ),
    ),
)


# --- vault --------------------------------------------------------------------------

_VAULT_LIST_FIELDS = (
    Field("dir", limit=1_000, arg="path"),
    Field("glob", limit=200),
    Field("max_entries", "int", limit=500),
)


async def vault_list(request: Request) -> Response:
    """GET /api/vault/list?dir= — one vault directory, through ``vault_list``."""
    guard = routes._guard(request)
    if guard is not None:
        return guard
    kwargs = bind_query(request, _VAULT_LIST_FIELDS)
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    return await call("vault_list", **kwargs)


_VAULT_LINKS_FIELDS = (Field("path", required=True, limit=1_000),)


async def vault_links(request: Request) -> Response:
    """GET /api/vault/links?path= — one note's outgoing links and backlinks."""
    guard = routes._guard(request)
    if guard is not None:
        return guard
    kwargs = bind_query(request, _VAULT_LINKS_FIELDS)
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    return await call("vault_links", **kwargs)


vault_write = adapt(
    "vault_write",
    (
        Field("path", required=True, limit=1_000),
        Field("content", limit=200_000),
        Field("mode", limit=16, choices=("overwrite", "append")),
        # No ``force``: the tool's ``force=True`` writes a note marked ``locked: true``,
        # which is a human or steward override, not a browser checkbox. The route
        # refuses the key, so a locked note stays locked over HTTP.
        Field("template", limit=1_000),
        Field("expected_revision", limit=128),
    ),
    operator_vault_write=True,
    idempotent=True,
)

#: ``vault_journal`` collapses whitespace and caps the entry at this many characters
#: (task #379). A raw entry may be longer only by whitespace.
JOURNAL_ENTRY_MAX_CHARS = 240


def _journal_entry_fits(kwargs: dict[str, Any]) -> str | None:
    collapsed = " ".join(kwargs["entry"].split())
    if not collapsed:
        return "entry must not be empty"
    if len(collapsed) > JOURNAL_ENTRY_MAX_CHARS:
        return (
            f"entry is {len(collapsed)} characters after collapsing whitespace; the journal "
            f"caps it at {JOURNAL_ENTRY_MAX_CHARS}. Put the detail in a task or memory note "
            "and journal a pointer to it."
        )
    return None


vault_journal = adapt(
    "vault_journal",
    (
        Field("entry", required=True, limit=4_000),
        Field("date", limit=10),
        Field("tags", "strlist", limit=20, item_limit=100),
    ),
    identity_args=("agent",),
    check=_journal_entry_fits,
    operator_vault_write=True,
    idempotent=True,
)


ROUTES: list[Route] = [
    Route("/api/memory/note", memory_note, methods=["GET"]),
    Route("/api/action/memory/save", memory_save, methods=["POST"]),
    Route("/api/action/memory/delete", memory_delete, methods=["POST"]),
    Route("/api/action/memory/restore", memory_restore, methods=["POST"]),
    Route("/api/action/memory/promote", memory_promote, methods=["POST"]),
    Route("/api/action/memory/feedback", memory_feedback, methods=["POST"]),
    Route("/api/vault/list", vault_list, methods=["GET"]),
    Route("/api/vault/links", vault_links, methods=["GET"]),
    Route("/api/action/vault/write", vault_write, methods=["POST"]),
    Route("/api/action/vault/journal", vault_journal, methods=["POST"]),
]
