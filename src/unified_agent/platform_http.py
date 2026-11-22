"""Read-only HTTP/JSON adapter over the platform object model (task #1071).

MCP is one adapter over ``object_model``; this module is the other half of
the same idea. The MCP adapter derives resource registrations from the
registry (``mcp_live_state``); this adapter derives the JSON manifest and
per-object reads from the *same* declarations, so the HTTP plane can never
drift from what MCP serves. The registry stays the single source of truth,
and the readers — and therefore the profile gates they enforce — are the
very same callables the MCP resource path resolves.

Gate parity falls out of construction rather than being bolted on: every
reader enforces its declared ``profile_tool`` gate before touching storage
(``platform_readers.deny`` / ``mcp_live_state._deny``), and this module
calls the same reader with the same id. A profile that cannot read a
resource over MCP therefore cannot read it over HTTP either. The route
layer maps the reader's resource errors to HTTP status codes (403 / 404)
instead of reusing MCP error semantics — see
``tests/test_platform_readers.py::test_platform_readers_do_not_import_the_mcp_adapter``
for the note that this mapping is exactly what a future HTTP adapter should
do.

Nothing here imports the MCP adapter: readers resolve lazily from the
registry by dotted path, so this module stays an adapter over the platform,
not over the MCP layer.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ResourceError

from . import agent_access, object_model, profiles
from .platform_readers import PROFILE_DENIED_MESSAGE


class UnknownObjectKind(Exception):
    """No object type is declared with this kind."""


class ObjectNotServed(Exception):
    """The kind is declared but has no reader wired yet (``served=False``)."""


class ObjectRequestError(Exception):
    """The request itself is malformed (id on a collection, id missing)."""


def manifest_payload() -> dict[str, Any]:
    """The whole platform contract as plain JSON.

    This is exactly :func:`unified_agent.object_model.manifest` — the route
    adds nothing, restates nothing, and recomputes nothing. Byte-for-byte
    parity between this endpoint and the registry is asserted by
    ``tests/test_webui_platform.py``.
    """
    agent_access.enforce("platform_manifest", read_only=True)
    return object_model.manifest()


def _enforce_gate(object_type: object_model.ObjectType) -> None:
    """Apply the object's declared ``profile_tool`` gate for this request.

    Over HTTP there is no MCP session, so ``profiles.current()`` resolves to
    the identity's least-privilege default — the webui cannot activate
    profiles, and a profile that could not read this object over MCP must not
    read it here. ``tool_is_allowed_for`` is used rather than
    ``tool_is_allowed`` because the latter's ambient short-circuit
    (``exposure_is_active()``) is only true inside an MCP session; without
    this explicit check an HTTP call would see every tool as allowed and the
    reader's own deny would be a no-op — the exact permission bypass this
    slice exists to prevent. The wording stays the one both reader layers
    already publish (pinned by ``tests/test_platform_readers.py``).
    """
    profile = profiles.current()
    if not profiles.tool_is_allowed_for(profile, object_type.profile_tool):
        raise ResourceError(PROFILE_DENIED_MESSAGE.format(profile=profile))


def read_object(kind: str, object_id: str | None = None) -> dict[str, Any]:
    """Read one platform object by kind+id through the registry's own reader.

    ``kind`` is the registry's machine name (``board``, ``task``, ``run``,
    ``approval``, ``evidence_event``). ``object_id`` must be ``None`` for a
    collection kind (``board``) and required for every id-addressed kind;
    the reader validates the id against the declared ``id_pattern``.

    The declared profile gate is enforced here **before** the reader runs,
    then again inside the reader (its own ``deny``) — the first is what makes
    the HTTP plane safe, the second is the platform layer's standing guard
    for any route that reaches it.

    Raises ``UnknownObjectKind`` (no such kind), ``ObjectNotServed`` (declared
    but no reader), or ``ObjectRequestError`` (id shape mismatch). Reader
    failures propagate as the platform contract's own errors:
    :class:`mcp.server.mcpserver.exceptions.ResourceError` when the active
    profile denies the object's declared ``profile_tool``, and
    :class:`mcp.server.mcpserver.exceptions.ResourceNotFoundError` for a
    missing or malformed id. The caller maps those to HTTP status codes.
    """
    object_type = object_model.get(kind)
    agent_access.enforce(f"platform_object:{kind}", read_only=True)
    if object_type is None:
        raise UnknownObjectKind(f"unknown object kind: {kind!r}")
    if not object_type.served:
        raise ObjectNotServed(f"object type {kind!r} is not served over HTTP yet")

    _enforce_gate(object_type)

    reader = object_type.reader()
    if object_type.id_param is None:
        if object_id is not None:
            raise ObjectRequestError(f"object type {kind!r} is a collection and takes no id")
        return reader()
    if object_id is None:
        raise ObjectRequestError(f"object type {kind!r} requires an id ({object_type.id_param})")
    return reader(**{object_type.id_param: object_id})


#: Batch readers for list views (task #1786), by kind. Each one is the batch
#: form of that kind's declared reader: same gate, same id validation, same
#: row shape, one storage connection for the whole set.
_BATCH_READERS = {
    "approval": "unified_agent.platform_readers:read_approvals",
}


def read_objects(kind: str, object_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Read many platform objects of one kind: ``{id: <read_object payload>}``.

    Applies exactly the checks :func:`read_object` applies per call (agent
    access, the declared profile gate, then the reader's own gate and id
    validation), once for the batch, because none of them depends on the id.
    An id that :func:`read_object` would answer with not-found is absent from
    the result rather than raising, so one stale row cannot fail a whole list.
    Gate failures raise, as they do for a single read.
    """
    object_type = object_model.get(kind)
    agent_access.enforce(f"platform_object:{kind}", read_only=True)
    if object_type is None:
        raise UnknownObjectKind(f"unknown object kind: {kind!r}")
    if not object_type.served:
        raise ObjectNotServed(f"object type {kind!r} is not served over HTTP yet")
    path = _BATCH_READERS.get(kind)
    if path is None:
        raise ObjectNotServed(f"object type {kind!r} has no batch reader")

    _enforce_gate(object_type)

    return object_model._resolve_reader(path)(object_ids)


#: The provenance graph (task #1287) is gated the same way as an evidence
#: event read — it is derived from the same ledger tables event_log reads
#: from, so a profile that cannot read the ledger over MCP must not read its
#: graph shape here either.
_PROVENANCE_GRAPH_PROFILE_TOOL = "event_log_query"


def _parse_int(value: str | None, label: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ObjectRequestError(f"{label} must be an integer, got {value!r}") from exc


def _parse_int_list(value: str | None, label: str) -> list[int] | None:
    if value is None or value == "":
        return None
    ids: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except (TypeError, ValueError) as exc:
            raise ObjectRequestError(
                f"{label} must be a comma-separated list of integers, got {value!r}"
            ) from exc
    return ids or None


def provenance_graph_payload(
    *,
    query: str | None = None,
    task_id: str | None = None,
    task_ids: str | None = None,
    project_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    human: str | None = None,
    agent: str | None = None,
    pr: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    """The provenance graph query API (task #1287): human->device->agent->
    action->proof as queryable graph objects, over the same ledger tables
    ``read_object("evidence_event", ...)`` reads from.

    Two named queries, selected by ``query``:

    - ``"by_human"``: everything ``human`` authorized in ``[since, until]``
      (approvals they resolved, or records whose resolved chain human they
      are).
    - ``"by_release"``: every action behind a release. The ledger has no
      release/git-tag record today (documented gap — see
      ``docs/provenance-graph.md``), so this is keyed on ``task_id`` or a
      comma-separated ``task_ids`` naming the release's constituent tasks.

    With no ``query``, returns the task/project/time-window scoped graph —
    the same shape ``evidence_report.build_evidence_report``'s
    ``provenance_graph`` appendix carries for that scope.

    All string params arrive exactly as HTTP query params do (unparsed);
    this function is what parses and validates them, raising
    :class:`ObjectRequestError` for a malformed value rather than letting a
    bad ``task_id`` silently become ``None``.
    """
    # "platform_object:" prefix (not "platform_provenance_graph") deliberately:
    # agent_access.reject_arbitrary_execution() allowlists exactly that prefix
    # for managed-secret mode, and this is the same class of read — a
    # gated, profile-checked platform object read — as platform_object().
    agent_access.enforce("platform_object:provenance_graph", read_only=True)
    profile = profiles.current()
    if not profiles.tool_is_allowed_for(profile, _PROVENANCE_GRAPH_PROFILE_TOOL):
        raise ResourceError(PROFILE_DENIED_MESSAGE.format(profile=profile))

    from .audit import provenance_graph

    parsed_task_id = _parse_int(task_id, "task_id")
    parsed_task_ids = _parse_int_list(task_ids, "task_ids")
    parsed_project_id = _parse_int(project_id, "project_id")
    parsed_limit = _parse_int(limit, "limit")

    if query == "by_human":
        if not human:
            raise ObjectRequestError("query=by_human requires human")
        return provenance_graph.graph_for_human(human, since=since, until=until, limit=parsed_limit)
    if query == "by_release":
        if parsed_task_id is None and not parsed_task_ids:
            raise ObjectRequestError("query=by_release requires task_id or task_ids")
        return provenance_graph.graph_for_release(task_id=parsed_task_id, task_ids=parsed_task_ids)
    if query is not None:
        raise ObjectRequestError(f"unknown provenance graph query: {query!r}")

    return provenance_graph.graph_for_task_scope(
        task_id=parsed_task_id,
        project_id=parsed_project_id,
        since=since,
        until=until,
        agent=agent,
        pr=pr,
        limit=parsed_limit,
    )
