"""Provenance graph: the evidence ledger's 5-link chain as queryable graph
objects (task #1287).

``evidence_report.py`` already derives the human -> device -> agent -> action
-> proof chain from the ledger (``tasks``, ``approvals``, ``delegations``,
``event_log``, ``dispatch_runs`` via delivery receipts) and renders it as a
table/report. This module adds a second *shape* over the same derivation: a
node/edge graph, with a canonical hash so two builds — or a build and a
round trip through storage — can be compared for equality.

Design rules, inherited from the rest of the platform:

1. **No second source of truth.** This module never writes anything and
   never invents a fact the ledger does not already carry. It is rebuilt
   from ``evidence_report.build_evidence_report`` (itself a read-only
   derivation over the ledger tables) every time it is asked for a graph —
   there is no persisted graph table anywhere.
2. **Deterministic.** :func:`canonical_hash` sorts nodes and edges into one
   order and serializes them with ``sort_keys=True`` before hashing, so the
   same ledger state always yields the same hash regardless of dict/row
   iteration order, and a changed or removed ledger event changes the hash
   (event hashes ride along on the ``work_product`` node's ``event_hashes``
   attribute, so tampering with ``event_log`` is not invisible to the graph
   even though the graph's own edges are keyed on task/approval/human
   values).
3. **Honest about gaps.** Two of the node kinds the read model declares —
   ``model`` and ``release`` — have no backing column anywhere in the ledger
   today (grepped: no ``release`` channel, no per-record model attribution
   on a delivery receipt). Rather than fabricate them, this module never
   emits a node of those kinds; see ``docs/provenance-graph.md`` for the
   documented gap and how :func:`graph_for_release` keys off task ids
   instead of an actual release record.

Node ids are ``"{kind}:{value}"`` so two records that independently name the
same human/device/agent/work-product collapse into one node rather than
duplicating it. Edge identity is ``(source, target, relation)`` — attrs merge
(last write wins) when the same edge recurs across records, which happens
routinely (e.g. the same human used the same device across many tasks).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from .. import config

#: Kept byte-identical to ``evidence_report._UNATTRIBUTED`` — pinned by test
#: so the two modules' notion of "no identity resolved" never drifts apart.
UNATTRIBUTED = "unattributed"

#: Values ``_human_provenance``/``_resolve_agent`` can return that are not a
#: real identity and must never become a graph node.
_NON_IDENTITY_VALUES = frozenset({UNATTRIBUTED, "conflict", "unknown", None, ""})

#: An approval "counts" as an authorization only in these terminal states —
#: mirrors evidence_report._human_provenance's own candidate acceptance rule
#: (``app.get("status") not in (None, "approved", "consumed")`` is rejected
#: there too). A denied/revoked/expired approval is not an authorization.
_ACCEPTED_APPROVAL_STATUSES = frozenset({"approved", "consumed"})

#: Hard ceiling on the direct approvals-table scan `graph_for_human` runs to
#: find every task this human authorized, independent of
#: ``evidence_report.build_evidence_report``'s 500-row task-recency LIMIT
#: (see ``graph_for_human``). Generous relative to that 500, but still bounded
#: — a caller pathologically over one human is told ``scope["truncated"]``
#: rather than the query running unbounded.
_HUMAN_QUERY_TASK_CAP = 2000

#: Node kinds this module is prepared to emit today. ``model`` and
#: ``release`` are declared in the task but have no ledger column yet (see
#: module docstring) — they are listed here only so a future reader knows
#: they are reserved, not forgotten.
NODE_KINDS = (
    "human",
    "device",
    "agent",
    "model",
    "task",
    "action",
    "approval",
    "work_product",
    "release",
)

GRAPH_SCHEMA_VERSION = "1.0"


def _is_identity(value: Any) -> bool:
    return isinstance(value, str) and value.strip() and value.strip() not in _NON_IDENTITY_VALUES


def _is_human_actor_kind(actor_kind: Any) -> bool:
    """True only when a classification says ``"human"`` outright.

    Shared gate for every place this module decides whether an identity may
    become a ``human`` node. A verified *agent* signer (or an unresolved
    ``"unknown"``/``"conflict"``) must never be promoted to the human kind —
    that would misrepresent a machine credential as a person in the
    accountability chain (mirrors evidence_report's own #1217 actor-kind vs.
    human-authority distinction).
    """
    return actor_kind == "human"


class _GraphBuilder:
    """Accumulates deduplicated nodes/edges while walking evidence records."""

    def __init__(self) -> None:
        self._nodes: dict[str, dict[str, Any]] = {}
        self._edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_node(self, kind: str, value: Any, **attrs: Any) -> str | None:
        if not _is_identity(value) and not isinstance(value, int):
            return None
        label = str(value)
        node_id = f"{kind}:{label}"
        node = self._nodes.setdefault(
            node_id, {"id": node_id, "kind": kind, "label": label, "attrs": {}}
        )
        for key, val in attrs.items():
            if val is not None:
                node["attrs"][key] = val
        return node_id

    def merge_event_hashes(self, node_id: str | None, event_hashes: list[dict[str, Any]]) -> None:
        """Union event-hash entries into a node's ``event_hashes`` attr, keyed
        by ULID — never last-write-wins.

        ``add_node``'s generic attrs merge overwrites a list attr wholesale,
        which is correct for scalars but wrong here: when two records share
        one node (the same PR referenced by two tasks, or simply the same
        task's action node revisited), each record's matched ledger events
        must all keep influencing :func:`canonical_hash` — tampering with an
        earlier record's event must not "disappear" because a later record's
        merge replaced the list.
        """
        if not node_id or not event_hashes:
            return
        node = self._nodes.get(node_id)
        if node is None:
            return
        merged: dict[Any, dict[str, Any]] = {
            entry.get("ulid"): entry for entry in node["attrs"].get("event_hashes") or []
        }
        for entry in event_hashes:
            merged[entry.get("ulid")] = entry
        node["attrs"]["event_hashes"] = sorted(
            merged.values(), key=lambda entry: entry.get("ulid") or ""
        )

    def add_edge(self, source: str | None, target: str | None, relation: str, **attrs: Any) -> None:
        if not source or not target:
            return
        key = (source, target, relation)
        edge = self._edges.setdefault(
            key, {"source": source, "target": target, "relation": relation, "attrs": {}}
        )
        for k, val in attrs.items():
            if val is not None:
                edge["attrs"][k] = val

    def build(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        nodes = sorted(self._nodes.values(), key=lambda n: (n["kind"], n["id"]))
        edges = sorted(
            self._edges.values(), key=lambda e: (e["relation"], e["source"], e["target"])
        )
        return nodes, edges


def nodes_and_edges_from_records(
    records: list[dict[str, Any]], *, human_scope: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Derive a canonical, sorted (nodes, edges) pair from evidence records.

    ``records`` is exactly the ``records`` list ``evidence_report.
    build_evidence_report`` returns — this function reads only the shape
    that module already documents (``human``, ``human_provenance``,
    ``device_link``, ``agent``, ``action``, ``proof``), so it never touches
    storage itself.

    ``human_scope``, when given, narrows the chain to one human's
    authorizations (query (a), "everything this human authorized in time
    range X" — see :func:`graph_for_human`): the human -> device -> agent
    edges for a record are only added when that record's resolved human is
    the scoped one, approvals resolved by anyone else (or resolved by the
    scoped human but not in an accepted status — denied/revoked/expired is
    not an authorization) are dropped entirely, and the task/action/
    work-product nodes still appear (they describe what happened, not who
    is in scope) so the reader can see what the scoped human's approval
    gated.

    A ``human`` node is only ever emitted for an identity classified as an
    actual human — a verified *agent* signer stays out of the human kind
    even though ``record["human"]`` legitimately carries its identity as the
    strongest available actor evidence (see ``evidence_report``'s #1217
    actor-kind vs. human-authority distinction; promoting it here would
    misrepresent a machine credential as a person in the graph).

    Every matched ledger-event hash rides on the ``action`` node (always
    present for a real task) as well as the ``work_product`` node (only
    present when an external artifact ref exists) — an artifactless task
    with matched events must still be hash-sensitive to tampering with
    those events. When multiple records share one node (most commonly a
    ``work_product`` two tasks both reference), their event hashes are
    unioned by ULID rather than overwritten.
    """
    builder = _GraphBuilder()

    for record in records:
        task_id = record.get("task_id")
        action = record.get("action") or {}
        proof = record.get("proof") or {}
        human_provenance = record.get("human_provenance") or {}
        primary_human = record.get("human")

        event_hashes = sorted(
            (
                {
                    "ulid": entry.get("ulid"),
                    "channel": entry.get("channel"),
                    "entry_hash": entry.get("entry_hash"),
                }
                for entry in proof.get("event_hashes") or []
            ),
            key=lambda entry: entry.get("ulid") or "",
        )

        task_nid = builder.add_node(
            "task",
            task_id,
            title=action.get("title"),
            status=action.get("status"),
            priority=action.get("priority"),
            project=record.get("project"),
        )
        action_nid = builder.add_node(
            "action",
            f"task:{task_id}",
            task_id=task_id,
            type=action.get("type"),
            title=action.get("title"),
            status=action.get("status"),
            lane=action.get("lane"),
        )
        # Always attach this record's matched event hashes to the action
        # node — the one node every task graph unconditionally emits — so a
        # tampered/removed ledger event affects canonical_hash even when the
        # task has no PR/commit/evidence_ref of its own (Devin finding: an
        # artifactless task previously hid tampering because event hashes
        # only ever rode on the work_product node, which didn't exist).
        builder.merge_event_hashes(action_nid, event_hashes)
        builder.add_edge(task_nid, action_nid, "has_action")

        record_in_scope = human_scope is None or primary_human == human_scope

        human_nid = None
        if (
            record_in_scope
            and _is_identity(primary_human)
            and _is_human_actor_kind(human_provenance.get("actor_kind"))
        ):
            human_nid = builder.add_node(
                "human",
                primary_human,
                source=human_provenance.get("source"),
                verified=human_provenance.get("verified"),
                actor_kind=human_provenance.get("actor_kind"),
                human_authority=human_provenance.get("human_authority"),
                verified_via=human_provenance.get("verified_via"),
            )

        device_nid = None
        if record_in_scope:
            device_link = record.get("device_link") or {}
            if device_link.get("verified") and _is_identity(device_link.get("node")):
                device_nid = builder.add_node(
                    "device",
                    device_link.get("node"),
                    verified=True,
                    node_id=device_link.get("node_id"),
                    user=device_link.get("user"),
                )
            elif _is_identity(device_link.get("host")):
                device_nid = builder.add_node("device", device_link.get("host"), verified=False)

        agent_value = record.get("agent")
        agent_nid = None
        if record_in_scope and _is_identity(agent_value):
            agent_nid = builder.add_node("agent", agent_value)

        if human_nid and device_nid:
            builder.add_edge(human_nid, device_nid, "used_device")
        if device_nid and agent_nid:
            builder.add_edge(device_nid, agent_nid, "ran_agent")
        elif human_nid and agent_nid and not device_nid:
            # No verified/unverified device trail at all — still show the
            # human -> agent link rather than dropping it, honestly marked.
            builder.add_edge(human_nid, agent_nid, "ran_agent", device_verified=False)
        if agent_nid:
            builder.add_edge(agent_nid, action_nid, "performed")

        work_product_value = (
            proof.get("pr_url") or proof.get("commit_sha") or proof.get("evidence_ref")
        )
        work_product_nid = None
        if _is_identity(work_product_value):
            work_product_nid = builder.add_node(
                "work_product",
                work_product_value,
                pr_url=proof.get("pr_url"),
                commit_sha=proof.get("commit_sha"),
                evidence_ref=proof.get("evidence_ref"),
                merge_verified=proof.get("merge_verified"),
                completed_at=proof.get("completed_at"),
                tamper_evident=proof.get("tamper_evident"),
            )
            # Union by ULID, not last-write-wins: two tasks referencing the
            # same PR/commit/evidence_ref collapse onto one work_product
            # node, and each task's matched events must keep contributing to
            # the hash rather than the later record silently replacing the
            # earlier one's (Devin finding: shared artifacts lost earlier
            # proofs).
            builder.merge_event_hashes(work_product_nid, event_hashes)
            builder.add_edge(action_nid, work_product_nid, "produced")

        for approval in action.get("approvals") or []:
            resolved_by = approval.get("resolved_by")
            status = approval.get("status")
            if human_scope is not None and (
                resolved_by != human_scope or status not in _ACCEPTED_APPROVAL_STATUSES
            ):
                # Scoped to one human's authorizations: someone else's
                # approval, and a denied/revoked/expired approval even by
                # the scoped human, is not an authorization — drop it
                # entirely rather than showing it unlinked.
                continue
            approval_nid = builder.add_node(
                "approval",
                approval.get("id"),
                tool=approval.get("tool"),
                status=status,
                reason=approval.get("reason"),
            )
            builder.add_edge(action_nid, approval_nid, "gated_by")
            # Same human-only gate as the primary chain human: an approval
            # resolver is an inferred (non-bearer-authenticated) claim, the
            # same class evidence_report classifies via actor_kind_for_claim
            # — a governed agent identity auto-resolving an approval must
            # not become a human node either.
            if _is_identity(resolved_by) and _is_human_actor_kind(
                config.actor_kind_for_claim(resolved_by)
            ):
                resolver_nid = builder.add_node("human", resolved_by)
                builder.add_edge(resolver_nid, approval_nid, "resolved")

    return builder.build()


def _node_sort_key(node: dict[str, Any]) -> tuple[str, str]:
    return (str(node.get("kind", "")), str(node.get("id", "")))


def _edge_sort_key(edge: dict[str, Any]) -> tuple[str, str, str]:
    return (str(edge.get("relation", "")), str(edge.get("source", "")), str(edge.get("target", "")))


def canonical_serialization(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """The exact bytes :func:`canonical_hash` hashes — exposed for tests/debugging.

    Sorts defensive *copies* of ``nodes``/``edges`` itself (by the documented
    ``(kind, id)`` / ``(relation, source, target)`` keys) rather than trusting
    the caller to have pre-sorted them. ``canonical_hash`` is a public
    function; a caller that reorders an equivalent node/edge list (e.g. after
    round-tripping through a store with no defined iteration order) must
    still get the same hash. This never mutates the lists passed in.
    """
    payload = {
        "nodes": sorted(nodes, key=_node_sort_key),
        "edges": sorted(edges, key=_edge_sort_key),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """SHA-256 of the canonical (sorted, sort_keys) node/edge serialization.

    Covers **only** the graph's structure (nodes + edges) — not the query
    ``scope`` that produced it. Two different queries ("by_human=Alice" vs.
    "task_id=7") can legitimately yield an identical subgraph and therefore
    the same ``canonical_hash``; that is intentional (it is a structural
    hash, not a request hash). Use :func:`scope_hash` — carried as
    ``scope_hash`` alongside ``canonical_hash`` in every graph payload — to
    additionally distinguish *which query* produced a graph.
    """
    return hashlib.sha256(canonical_serialization(nodes, edges).encode("utf-8")).hexdigest()


def scope_hash(scope: dict[str, Any]) -> str:
    """SHA-256 of the canonical (``sort_keys``) serialization of a query scope.

    Deliberately separate from :func:`canonical_hash` (see its docstring):
    the graph payload carries both, so a consumer that needs to tell "the
    same subgraph, but from two different queries" apart compares
    ``scope_hash`` too, without conflating scope into the structural hash.
    """
    payload = json.dumps(scope, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_graph_from_records(
    records: list[dict[str, Any]],
    *,
    scope: dict[str, Any] | None = None,
    human_scope: str | None = None,
) -> dict[str, Any]:
    """Pure derivation: records in, a full graph payload out. No storage access."""
    nodes, edges = nodes_and_edges_from_records(records, human_scope=human_scope)
    node_counts = Counter(node["kind"] for node in nodes)
    resolved_scope = scope or {}
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "scope": resolved_scope,
        "scope_hash": scope_hash(resolved_scope),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_counts_by_kind": dict(sorted(node_counts.items())),
        "nodes": nodes,
        "edges": edges,
        "canonical_hash": canonical_hash(nodes, edges),
    }


def graph_for_task_scope(
    *,
    task_id: int | None = None,
    project_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    agent: str | None = None,
    pr: str | None = None,
    limit: int | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """The provenance graph for a task/project/time-window scope.

    This is the report-appendix shape: whatever
    ``evidence_report.build_evidence_report`` would return for the same
    filters, reshaped as a graph. Lazy-imports ``evidence_report`` so the two
    modules do not form an import cycle (``evidence_report`` imports this
    module at top level to build its own appendix).
    """
    from . import evidence_report

    report = evidence_report.build_evidence_report(
        task_id=task_id,
        project_id=project_id,
        since=since,
        until=until,
        agent=agent,
        pr=pr,
        limit=limit,
        conn=conn,
    )
    scope = {
        "kind": "task_scope",
        "task_id": task_id,
        "project_id": project_id,
        "since": since,
        "until": until,
        "agent": agent,
        "pr": pr,
    }
    return build_graph_from_records(report.get("records") or [], scope=scope)


def _record_in_window(record: dict[str, Any], since: str | None, until: str | None) -> bool:
    """Same "at least one lifecycle timestamp in window" rule as
    ``evidence_report._append_time_window``, applied after the fact to a
    record built without a since/until filter (see ``graph_for_human``,
    which pulls some tasks in through a direct table scan that has no SQL
    window clause of its own)."""
    if since is None and until is None:
        return True
    until_bound = until
    if until is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", until.strip()):
        until_bound = until.strip() + "T23:59:59.999999+00:00"
    proof = record.get("proof") or {}
    for ts in (proof.get("created_at"), proof.get("updated_at"), proof.get("completed_at")):
        if not ts:
            continue
        if since is not None and ts < since:
            continue
        if until_bound is not None and ts > until_bound:
            continue
        return True
    return False


def graph_for_human(
    human: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Query (a): everything ``human`` authorized in a time range.

    "Authorized" means either the record's resolved chain human is
    ``human``, or ``human`` resolved at least one of the task's approvals in
    an accepted status (``approved``/``consumed`` — a denied approval is not
    an authorization) — a human can gate work they did not personally
    execute. Within a matched record only approvals resolved by ``human`` in
    an accepted status produce an approval node (see
    :func:`nodes_and_edges_from_records`'s ``human_scope``).

    Finding this human's task set does **not** start from
    ``evidence_report.build_evidence_report``'s default task-recency
    ``LIMIT`` (500) — filtering by human *after* that cap would silently
    drop an older authorization behind 500 newer, unrelated tasks. Instead
    this runs a direct scan of the ``approvals`` table for every row this
    human resolved in an accepted status (bounded by
    :data:`_HUMAN_QUERY_TASK_CAP`, not by task recency), unions in whatever
    the capped report itself already resolved to this human's *primary*
    chain identity (covers producer-verified/delegation/task-notes
    candidates that never went through an approval at all), then builds one
    evidence-report record per resulting task id. ``scope["truncated"]`` is
    ``True`` when the direct approvals scan hit its cap or the requested
    result limit omitted matching records.
    """
    from . import evidence_report

    requested_limit = evidence_report._clamp_limit(limit)

    close_conn = conn is None
    if close_conn:
        conn = evidence_report.storage.connect()

    try:
        query = [
            "SELECT CAST(json_extract(payload, '$.task_id') AS TEXT) AS tid1,",
            "CAST(json_extract(payload, '$.parent_task_id') AS TEXT) AS tid2",
            "FROM approvals WHERE resolved_by = ? AND status IN ('approved', 'consumed')",
        ]
        params: list[Any] = [human]
        if since is not None:
            query.append("AND (resolved_at IS NULL OR resolved_at >= ?)")
            params.append(since)
        if until is not None:
            until_bound = until
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until.strip()):
                until_bound = until.strip() + "T23:59:59.999999+00:00"
            query.append("AND (resolved_at IS NULL OR resolved_at <= ?)")
            params.append(until_bound)
        query.append("ORDER BY id DESC LIMIT ?")
        params.append(_HUMAN_QUERY_TASK_CAP + 1)

        approval_rows = conn.execute(" ".join(query), params).fetchall()
        truncated = len(approval_rows) > _HUMAN_QUERY_TASK_CAP
        approval_rows = approval_rows[:_HUMAN_QUERY_TASK_CAP]

        task_ids: set[int] = set()
        for row in approval_rows:
            for raw in (row["tid1"], row["tid2"]):
                if raw is None:
                    continue
                try:
                    task_ids.add(int(raw))
                except (TypeError, ValueError):
                    continue

        # Union in the recency-capped report's own primary-human matches —
        # covers a record where `human` resolves to this identity without
        # any approval at all (producer-verified event, delegation.from_agent,
        # task-notes [human: ...] claim).
        capped_report = evidence_report.build_evidence_report(
            since=since,
            until=until,
            limit=evidence_report._MAX_LIMIT,
            conn=conn,
        )
        for record in capped_report.get("records") or []:
            if record.get("human") == human:
                task_ids.add(int(record["task_id"]))

        records: list[dict[str, Any]] = []
        for task_id in sorted(task_ids):
            sub_report = evidence_report.build_evidence_report(task_id=task_id, conn=conn)
            records.extend(sub_report.get("records") or [])

        matched = [
            record
            for record in records
            if record.get("human") == human
            or any(
                approval.get("resolved_by") == human
                and approval.get("status") in _ACCEPTED_APPROVAL_STATUSES
                for approval in (record.get("action") or {}).get("approvals") or []
            )
        ]
        # Tasks reached only through the direct approvals scan were never
        # run through evidence_report's own since/until SQL clause — apply
        # the same "at least one lifecycle timestamp in window" rule here.
        matched = [record for record in matched if _record_in_window(record, since, until)]
        matched.sort(key=lambda record: int(record["task_id"]), reverse=True)
        truncated = truncated or len(matched) > requested_limit
        matched = matched[:requested_limit]

        scope = {
            "kind": "by_human",
            "human": human,
            "since": since,
            "until": until,
            "limit": requested_limit,
            "truncated": truncated,
        }
        return build_graph_from_records(matched, scope=scope, human_scope=human)
    finally:
        if close_conn:
            conn.close()


def graph_for_release(
    *,
    task_id: int | None = None,
    task_ids: list[int] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Query (b): every action behind a release.

    **Documented gap**: the ledger has no release/git-tag record today —
    grepped ``events.py``/``storage.py``/``producer_sign.py`` for a
    ``release`` channel or table and found none. Until one exists, this
    query is keyed on a task id or an explicit set of task ids (the
    release's constituent tasks, tracked by whoever cuts the release) rather
    than a release identity the ledger itself could resolve. When a release
    record lands in the ledger, this function should be rewritten to resolve
    its task set instead of accepting one from the caller.
    """
    from . import evidence_report

    ids = list(task_ids) if task_ids else ([task_id] if task_id is not None else [])
    if not ids:
        raise ValueError("graph_for_release requires task_id or task_ids")

    records: list[dict[str, Any]] = []
    for one_id in ids:
        sub_report = evidence_report.build_evidence_report(task_id=one_id, conn=conn)
        records.extend(sub_report.get("records") or [])

    scope = {
        "kind": "by_release",
        "task_ids": sorted(set(ids)),
        "gap": "no release record in ledger",
    }
    return build_graph_from_records(records, scope=scope)
