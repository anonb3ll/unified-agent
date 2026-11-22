"""User-attributable agent evidence report surface (task #781, Phase 3.1).

Traces the 5-link accountability chain:
    human -> device -> agent -> action -> proof
for any task, PR, or time-window. Reads existing SQLite tables without mutation.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, events, gateway, run_records, storage

# Moved to the kernel (#2450); re-exported so callers and patches keep working.
from ..evidence_claims import _MERGED_RE, _PR_URL_RE, _SHA_RE, _merge_claim  # noqa: F401
from ..tools.tasks import _PROJECT_SPEC_MARKER_RX
from . import anchor as chain_anchor
from . import evidence_signing, producer_sign, provenance_graph

# Report-level attestation `attester_kind` values (task #1267, Epic F/F4).
ATTESTER_KIND_HUMAN = "human"
ATTESTER_KIND_AGENT_BEARER_ONLY = "agent-bearer-only"
ATTESTER_KIND_UNKNOWN = "unknown"

# Report schema version (task #1673 bumps it to 1.7, task #2309 to 1.8). Every
# report, interactive or complete export, carries the same version; a complete
# export also carries ``complete: true``.
REPORT_SCHEMA_VERSION = "1.8"

# `human_provenance.human_authority` values (task #1673). The value says what
# the evidence proves, nothing more:
# - credential-attributed: a credential (agent bearer HMAC or a hub-verified
#   IdP session) bound to a human-classified identity produced the claim. It
#   proves which credential was used, not that a human was present.
# - presence-verified: reserved for a statement-bound passkey assertion
#   (future H10). Nothing emits it yet.
# - unknown / conflict: the evidence does not establish a human, or disagrees.
HUMAN_AUTHORITY_CREDENTIAL_ATTRIBUTED = "credential-attributed"
HUMAN_AUTHORITY_PRESENCE_VERIFIED = "presence-verified"
HUMAN_AUTHORITY_UNKNOWN = "unknown"
HUMAN_AUTHORITY_CONFLICT = "conflict"
HUMAN_AUTHORITY_ATTRIBUTED = frozenset(
    {HUMAN_AUTHORITY_CREDENTIAL_ATTRIBUTED, HUMAN_AUTHORITY_PRESENCE_VERIFIED}
)
# Schema <= 1.6 wrote "verified" for what was only ever credential
# attribution. Historical exports are read through this map so they still
# render, and render honestly.
_LEGACY_HUMAN_AUTHORITY = {"verified": HUMAN_AUTHORITY_CREDENTIAL_ATTRIBUTED}

# Report-level separation-of-duties status (task #1673). With one configured
# human there is nobody to separate duties between, so the report says so
# instead of passing or failing a check that cannot apply.
SOD_NOT_APPLICABLE_SINGLE_OPERATOR = "not_applicable_single_operator"
SOD_PASS = "pass"
SOD_FAIL = "fail"
SOD_NO_DATA = "no_data"


def normalize_human_authority(value: Any, *, verified: Any = None) -> str:
    """Read a ``human_authority`` value from any schema version.

    Maps the legacy ``"verified"`` (schema 1.1 to 1.6) to credential-attributed.
    A record with no ``human_authority`` at all predates schema 1.1, where the
    boolean ``verified`` meant only that a producer signature verified, agent
    or human (task #1217 split the two). That bit is actor authentication, not
    human authority, so such a record reads as ``"unknown"``; its ``verified``
    flag is left as exported. ``verified`` is accepted for call-site symmetry
    and deliberately never promotes.
    """
    del verified  # pre-1.1 actor authentication; never human authority
    if isinstance(value, str) and value:
        return _LEGACY_HUMAN_AUTHORITY.get(value, value)
    return HUMAN_AUTHORITY_UNKNOWN


def _normalize_graph_appendix(graph: Any) -> Any:
    """Normalize legacy ``human_authority`` on a graph appendix's human nodes.

    The appendix copies each record's ``human_authority`` into its human node
    (provenance_graph.nodes_and_edges_from_records), so a re-rendered export
    must rename it there too or its two representations disagree. When a node
    changes, ``canonical_hash`` is recomputed over the normalized nodes so the
    view stays self-consistent; scope and ``scope_hash`` are kept as exported.
    """
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        return graph
    changed = False
    nodes = []
    for node in graph["nodes"]:
        attrs = node.get("attrs") if isinstance(node, dict) else None
        if isinstance(attrs, dict) and node.get("kind") == "human" and "human_authority" in attrs:
            normalized = normalize_human_authority(attrs.get("human_authority"))
            if normalized != attrs.get("human_authority"):
                node = {**node, "attrs": {**attrs, "human_authority": normalized}}
                changed = True
        nodes.append(node)
    if not changed:
        return graph
    upgraded = {**graph, "nodes": nodes}
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    upgraded["canonical_hash"] = provenance_graph.canonical_hash(nodes, edges)
    return upgraded


def read_historical_report(report: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a parsed (possibly historical) JSON export with every
    record's ``human_authority`` read through ``normalize_human_authority``.

    The ``provenance_graph`` appendix's human nodes are normalized the same way
    (with a recomputed ``canonical_hash``) so records and graph agree.
    ``schema_version`` is left as exported so a reader can still tell which
    schema produced the artifact; signatures cover the exported bytes, not
    this in-memory view.
    """
    upgraded = dict(report)
    records = []
    for record in report.get("records") or []:
        if not isinstance(record, dict):
            records.append(record)
            continue
        provenance = record.get("human_provenance")
        if isinstance(provenance, dict):
            provenance = dict(provenance)
            provenance["human_authority"] = normalize_human_authority(
                provenance.get("human_authority"), verified=provenance.get("verified")
            )
            record = {**record, "human_provenance": provenance}
        records.append(record)
    if "records" in report:
        upgraded["records"] = records
    if "provenance_graph" in report:
        upgraded["provenance_graph"] = _normalize_graph_appendix(report["provenance_graph"])
    return upgraded


_CLAIMED_BY_RE = re.compile(r"claimed-by:\s*([^\s\n]+)", re.IGNORECASE)
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500
_UNATTRIBUTED = "unattributed"
# Report-level attribution coverage gate (task #1139). Does not block task
# completion — visible failure on the evidence/weekly report surface only.
ATTRIBUTION_COVERAGE_THRESHOLD = 0.95
DEFAULT_ATTRIBUTION_WINDOW_DAYS = 30
ATTRIBUTION_COVERAGE_GATE_FAILURE = "attribution_coverage_below_threshold"
EVIDENCE_CHAIN_INTEGRITY_GATE_FAILURE = "evidence_chain_integrity_failed"
# Production task writers (storage.record_agent_event → events.mirror_agent_event)
# serialize {kind, ref_id}; lifecycle events may still carry task_id. Compare as
# text so task 7 never matches task 70.
_EVENT_TASK_SQL = (
    "CAST(json_extract(payload, '$.task_id') AS TEXT) = ? OR "
    "(json_extract(payload, '$.kind') = 'task' "
    "AND CAST(json_extract(payload, '$.ref_id') AS TEXT) = ?)"
)
# Delegate (and similar) payloads often carry parent_task_id rather than
# task_id; both must resolve for the approval hop to appear on the task's
# evidence chain (task #1140 real-release wedge).
_APPROVAL_TASK_SQL = (
    "CAST(json_extract(payload, '$.task_id') AS TEXT) = ? OR "
    "CAST(json_extract(payload, '$.parent_task_id') AS TEXT) = ?"
)


#: One node dispatch item: its id, or the ``node:<node>:<item>`` evidence_ref
#: a dispatch records (task #1989).
_ITEM_REF_RE = re.compile(r"(?:node:([A-Za-z0-9][A-Za-z0-9._-]{0,127}):)?(nd-[0-9a-f]{16})")


def parse_item_ref(item: str) -> tuple[str | None, str]:
    """``(node, item_id)`` for an item filter; ``node`` is None for a bare id."""
    match = _ITEM_REF_RE.fullmatch(str(item or "").strip())
    if match is None:
        raise ToolError(
            f"invalid node dispatch item {str(item)[:80]!r}: "
            "pass nd-<16 hex> or node:<node>:nd-<16 hex>"
        )
    return match.group(1), match.group(2)


#: Record fields an item-filtered record still reports for the whole task:
#: approvals authorize the task, not one dispatch of it (task #1989).
_ITEM_TASK_WIDE_FIELDS = ("human", "human_provenance", "action.approvals")


def _item_dispatch(conn: Any, item: str) -> dict[str, Any]:
    """The selected dispatch item: id, task id (None: no such item, or no
    task), evidence_ref, and the node and agent that executed it."""
    node, item_id = parse_item_ref(item)
    sql = (
        "SELECT task_id, node, agent, profile_agent, created_at, updated_at, completed_at "
        "FROM node_dispatch WHERE id = ?"
    )
    params: list[Any] = [item_id]
    if node is not None:
        sql += " AND node = ?"
        params.append(node)
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return {"id": item_id, "task_id": None, "ref": item, "node": None, "agent": None}
    return {
        "id": item_id,
        "task_id": row["task_id"],
        "ref": f"node:{row['node']}:{item_id}",
        "node": row["node"],
        # The registry session runs under the profile agent when one is set.
        "agent": row["profile_agent"] or row["agent"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


#: Denials listed per record before the list is cut (``truncated: true``).
_DENIALS_MAX = 200


def _self_pickup_run_ids(conn: Any, task_id: Any) -> list[str]:
    """The task's self-pickup run ids, oldest first (one ``selfpickup.claimed`` each)."""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return []
    rows = conn.execute(
        "SELECT json_extract(payload, '$.run_id') AS run_id FROM event_log "
        "WHERE channel = ? AND json_extract(payload, '$.kind') = ? "
        "AND json_extract(payload, '$.task_id') = ? ORDER BY ROWID",
        (run_records.CHANNEL, run_records.KIND_CLAIMED, tid),
    ).fetchall()
    return list(
        dict.fromkeys(
            str(row["run_id"])
            for row in rows
            if isinstance(row["run_id"], str)
            and row["run_id"].startswith(run_records.RUN_ID_PREFIX)
        )
    )


def _self_pickup_claimants(conn: Any, task_id: Any) -> set[str]:
    """Every agent that opened a self-pickup run of the task (immutable claim events)."""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return set()
    rows = conn.execute(
        "SELECT DISTINCT json_extract(payload, '$.agent') AS agent FROM event_log "
        "WHERE channel = ? AND json_extract(payload, '$.kind') = ? "
        "AND json_extract(payload, '$.task_id') = ?",
        (run_records.CHANNEL, run_records.KIND_CLAIMED, tid),
    ).fetchall()
    return {str(row["agent"]) for row in rows if isinstance(row["agent"], str) and row["agent"]}


def _record_runs(conn: Any, task_id: Any, scope: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The runs a record covers, each with the episode its denials bind to.

    A node dispatch item is a run with episode ``acp:<item>`` once a node has
    claimed it. A row still queued, or cancelled before its first claim, has
    not executed and is not a run. A self-pickup run (``sp-<ulid>``, opened by
    a claim) is a run with episode ``run:<id>`` (task #2313): an interactive
    request carries it while its caller holds that one claim and no other. A
    dispatch run no node item names has no episode at all. ``bound`` says an
    event was written under the episode. Minting a session credential happens
    before any authenticated call, so a mint alone does not bind the run.
    Rows written before the stamp existed, and runs whose caller held several
    claims at once, are unbound.
    """
    if scope is not None:
        row = None
        if scope["node"] is not None:
            row = conn.execute(
                "SELECT id, node FROM node_dispatch WHERE id = ? AND claimed_at IS NOT NULL",
                (scope["id"],),
            ).fetchone()
        rows = [] if row is None else [dict(row)]
        dispatch_runs: list[Any] = []
        self_pickup: list[str] = []
    else:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id, node FROM node_dispatch WHERE task_id = ? AND claimed_at IS NOT NULL "
                "ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
        ]
        dispatch_runs = conn.execute(
            "SELECT run_id FROM dispatch_runs WHERE task_id = ? AND run_id NOT IN "
            "(SELECT run_id FROM node_dispatch WHERE run_id IS NOT NULL) "
            "ORDER BY created_at, rowid",
            (task_id,),
        ).fetchall()
        self_pickup = _self_pickup_run_ids(conn, task_id)

    def _bound(episode: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM event_log WHERE episode_id = ? LIMIT 1", (episode,)
            ).fetchone()
            is not None
        )

    runs: list[dict[str, Any]] = []
    for row in rows:
        episode = gateway.session_episode_id(row["id"])
        runs.append(
            {
                "run": f"node:{row['node']}:{row['id']}",
                "episode_id": episode,
                "bound": _bound(episode),
            }
        )
    for run_id in self_pickup:
        episode = gateway.run_episode_id(run_id)
        runs.append({"run": run_id, "episode_id": episode, "bound": _bound(episode)})
    runs.extend(
        {"run": str(row["run_id"]), "episode_id": None, "bound": False} for row in dispatch_runs
    )
    return runs


def _participants(
    conn: Any, task: dict[str, Any], scope: dict[str, Any] | None, agent_label: str
) -> list[str] | None:
    """Every agent label that may have acted on the record's work.

    An item record: the dispatch's agent and profile agent and the agents its
    session credentials were minted for. A task record adds the record's
    agent, the claimant (current and ``claimed-by:`` history in notes), and
    the agents of every dispatch item, dispatch run and session credential of
    the task, and every agent that ever claimed it, read from the immutable
    ``selfpickup.claimed`` events (review of #2313): the claimant column and
    notes can be rewritten, so a former claimant's unbound denial must not
    drop out of the window. None (count every caller) when the record's agent
    is unknown, so an unattributed record never narrows its uncertainty.
    """
    if agent_label == _UNATTRIBUTED:
        return None
    labels: set[str] = {agent_label}
    if scope is not None:
        item_rows = conn.execute(
            "SELECT agent, profile_agent FROM node_dispatch WHERE id = ?", (scope["id"],)
        ).fetchall()
        cred_rows = conn.execute(
            "SELECT agent_label FROM session_credentials WHERE item_id = ?", (scope["id"],)
        ).fetchall()
        run_rows: list[Any] = []
    else:
        if task.get("claimed_by"):
            labels.add(str(task["claimed_by"]))
        labels.update(_CLAIMED_BY_RE.findall(task.get("notes") or ""))
        item_rows = conn.execute(
            "SELECT agent, profile_agent FROM node_dispatch WHERE task_id = ?", (task["id"],)
        ).fetchall()
        cred_rows = conn.execute(
            "SELECT agent_label FROM session_credentials WHERE task_id = ? OR item_id IN "
            "(SELECT id FROM node_dispatch WHERE task_id = ?)",
            (task["id"], task["id"]),
        ).fetchall()
        run_rows = conn.execute(
            "SELECT agent_label FROM dispatch_runs WHERE task_id = ?", (task["id"],)
        ).fetchall()
        labels.update(_self_pickup_claimants(conn, task["id"]))
    for row in item_rows:
        labels.update(str(value) for value in (row["agent"], row["profile_agent"]) if value)
    labels.update(str(row["agent_label"]) for row in (*cred_rows, *run_rows) if row["agent_label"])
    return sorted(label for label in labels if label.strip())


def _record_denials(
    conn: Any,
    runs: list[dict[str, Any]],
    *,
    since: str | None,
    until: str | None,
    callers: list[str] | None,
    org_id: str | None,
) -> dict[str, Any]:
    """Gateway denials bound to ``runs`` (task #2309).

    ``unbound_in_window`` counts denies with no episode written in the
    record's window by any participating agent (``callers``; every caller
    when None): they may belong to this work and can be neither attributed
    nor excluded. ``status`` is ``recorded`` only when every run is bound and
    that count is zero, so an empty ``items`` is a real "no denials" only
    then. One bound event does not prove complete coverage. ``partial`` lists
    the bound runs' denials while others may exist; ``not-recorded`` (no run,
    or no bound run) never claims zero. Items carry the redacted
    ``args_sha256``, never arguments or ``args_summary``.
    """
    bound = [run["episode_id"] for run in runs if run["bound"]]
    items: list[dict[str, Any]] = []
    count = 0
    if bound:
        marks = ",".join("?" for _ in bound)
        where = (
            f"FROM event_log WHERE channel = ? AND episode_id IN ({marks}) "
            "AND json_extract(payload, '$.decision') = 'deny'"
        )
        params = (gateway.CHANNEL_GATEWAY_DECISION, *bound)
        count = conn.execute(f"SELECT COUNT(*) {where}", params).fetchone()[0]
        for row in conn.execute(
            "SELECT ulid, created_at, episode_id, causal_parent_id, payload "
            f"{where} ORDER BY ROWID LIMIT ?",
            (*params, _DENIALS_MAX),
        ).fetchall():
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                payload = {}
            payload = payload if isinstance(payload, dict) else {}
            items.append(
                {
                    "ulid": row["ulid"],
                    "created_at": row["created_at"],
                    "episode_id": row["episode_id"],
                    "causal_parent_id": row["causal_parent_id"],
                    "tool": payload.get("tool_name"),
                    "rule": payload.get("rule"),
                    "reason_code": payload.get("reason_code"),
                    "caller": payload.get("caller"),
                    "profile": payload.get("profile"),
                    "args_sha256": payload.get("args_sha256"),
                }
            )
    unbound_sql = [
        "SELECT COUNT(*) FROM event_log WHERE channel = ? AND episode_id IS NULL",
        "AND json_extract(payload, '$.decision') = 'deny'",
    ]
    unbound_params: list[Any] = [gateway.CHANNEL_GATEWAY_DECISION]
    if since:
        unbound_sql.append("AND created_at >= ?")
        unbound_params.append(since)
    if until:
        unbound_sql.append("AND created_at <= ?")
        unbound_params.append(until)
    # An empty participant set proves nothing about who acted, so it counts
    # every caller (like None) rather than none: an unbound denial must never
    # be filtered out just because attribution came back empty.
    if callers:
        unbound_sql.append(f"AND originator IN ({','.join('?' for _ in callers)})")
        unbound_params.extend(callers)
    # An unattributed record passes no callers and would otherwise count every
    # tenant. The count stays inside this task's org either way.
    unbound_sql.append("AND org_id IS ?")
    unbound_params.append(org_id)
    unbound = conn.execute(" ".join(unbound_sql), unbound_params).fetchone()[0]
    if not bound:
        status = "not-recorded"
    elif len(bound) == len(runs) and unbound == 0:
        status = "recorded"
    else:
        status = "partial"
    return {
        "status": status,
        "runs": runs,
        "items": items,
        "count": count,
        "truncated": count > len(items),
        "unbound_in_window": unbound,
        "window": {"since": since, "until": until, "callers": callers},
    }


def _item_proof(
    scope: dict[str, Any], events_list: list[dict[str, Any]], chain_valid: bool
) -> dict[str, Any]:
    """Proof for one dispatch item, from that item's own evidence only.

    A node receipt names no PR or commit, so an item has no reference of its
    own: the task's ``evidence_ref`` and notes may describe another dispatch,
    and the item's ``node:<node>:nd-<hex>`` reference is an id, never commit
    evidence (its hex must not be read as a SHA). Timestamps are the
    dispatch's.
    """
    proof = _resolve_proof(
        {
            "evidence_ref": None,
            "notes": "",
            "created_at": scope["created_at"],
            "updated_at": scope["updated_at"],
            "completed_at": scope["completed_at"],
        },
        events_list,
        chain_valid,
    )
    proof["evidence_ref"] = scope["ref"]
    return proof


def _event_item_id(event: dict[str, Any]) -> Any:
    try:
        payload = json.loads(event.get("payload") or "{}")
    except (TypeError, ValueError):
        return None
    return payload.get("item_id") if isinstance(payload, dict) else None


def _operator_names_lower() -> frozenset[str]:
    """Lowercased names a delegation may carry for the configured human operator."""
    names = {config.operator_label(), *config.operator_aliases(), "operator"}
    return frozenset(n.lower() for n in names)


def _human_provenance(
    task: dict[str, Any],
    approvals: list[dict[str, Any]],
    delegations: list[dict[str, Any]],
    verified_producers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return attribution candidates; mark verified only via a verified HUMAN.

    A signed authenticated-producer event (task #1155) proves which credential
    produced the claim — it does not by itself prove a human being produced
    it. Desktop_Agent2, Desktop_Agent5, and every other agent label sign with
    exactly the same HMAC scheme a human operator (the configured operator label) does, so
    a unique verifying signature was previously promoted straight to
    ``human_provenance.verified = True`` regardless of whose identity it was
    (task #1217 audit finding, regression risk on #1155/#1138). That
    conflates "an authenticated actor produced this" with "a human
    authorized this" — an accountability misstatement.

    This now models the two dimensions explicitly:
    - ``actor_kind``: what kind of identity produced the strongest evidence
      (``"human"``, ``"agent"``, ``"unknown"``, or ``"conflict"``).
    - ``human_authority``: ``"credential-attributed"`` only when a
      credential bound to a *human* identity signed the claim (task #1673:
      that proves the credential, not a present human; "presence-verified"
      is reserved for statement-bound passkey assertions); ``"unknown"``
      when the evidence does not establish a human (including a cleanly
      verified agent signature — real, valuable evidence, just not a human
      mandate); ``"conflict"`` when the evidence disagrees with itself
      (ambiguous signed producers, or a human claim that resolves to a known
      agent identity).
    - ``verified`` is kept for backward compatibility and means exactly
      ``human_authority`` is credential-attributed (or presence-verified).
      ``credential_attributed`` carries the same bit under an honest name.

    The verified *actor* identity itself is never discarded: it still
    appears as ``value``/``source`` (so `human` on the record keeps showing
    who signed) and as a `verified` candidate in ``candidates``.
    """
    candidates: list[dict[str, Any]] = []
    human_claim_conflict = False
    for app in approvals:
        resolved_by = app.get("resolved_by")
        if not isinstance(resolved_by, str) or not resolved_by.strip():
            continue
        if app.get("status") not in (None, "approved", "consumed"):
            continue
        candidates.append(
            {"value": resolved_by.strip(), "source": "approval.resolved_by", "verified": False}
        )

    for d in delegations:
        from_agent = d.get("from_agent")
        if (
            isinstance(from_agent, str)
            and from_agent.strip()
            and from_agent.strip().lower() in _operator_names_lower()
        ):
            candidates.append(
                {"value": from_agent.strip(), "source": "delegation.from_agent", "verified": False}
            )

    notes = task.get("notes") or ""
    for m in re.finditer(
        r"\[(?P<kind>human|created-by|delegated-from):\s*(?P<value>[^\]]+)\]", notes, re.IGNORECASE
    ):
        if value := m.group("value").strip():
            candidates.append({"value": value, "source": "task.notes", "verified": False})
            # An explicit human claim ([human: ...]) whose value resolves to a
            # *known agent* identity is a direct conflict — a human authority
            # claim was made for a label the fleet map says is an agent.
            if m.group("kind").lower() == "human" and config.is_governed_agent_identity(value):
                human_claim_conflict = True

    configured = os.environ.get("UA_OPERATOR_NAME") or os.environ.get("UA_HUMAN_IDENTITY")
    if configured and configured.strip():
        candidates.append(
            {"value": configured.strip(), "source": "operator.environment", "verified": False}
        )

    # Cryptographic producer identity outranks inferred candidates (task #1155)
    # as *actor* evidence, but is only ever promoted to human authority when
    # the signing identity itself is classified human (task #1217). The
    # signature's backing class — ``"agent_bearer"`` or ``"idp"`` — rides along
    # as ``verified_via`` so a WorkOS-session producer is distinguishable from
    # a bearer-HMAC one (task #1249 / Epic E, E2).
    producers = verified_producers or []
    producer_identities = []
    for producer in producers:
        identity = producer.get("identity")
        if isinstance(identity, str) and identity.strip():
            identity = identity.strip()
            producer_identities.append(identity)
            candidates.append(
                {
                    "value": identity,
                    "source": "evidence.producer_authenticated",
                    "verified": True,
                    "actor_kind": config.actor_kind_for_identity(identity),
                    "verified_via": producer.get("verified_via"),
                }
            )

    # Only signed producer-authenticated candidates (set explicitly above)
    # may use the open-roster "well-formed label = agent" fallback — that
    # heuristic is safe only for identities that came through the agent
    # bearer infrastructure. Every other source (task notes, operator
    # environment, approval resolver, delegation from_agent) is classified
    # with the stricter, source-aware ``actor_kind_for_claim`` instead, so
    # an ordinary human name that happens to be label-shaped (e.g.
    # "Alice") is never misclassified as an agent (task #1217 finding 1).
    for item in candidates:
        item.setdefault("actor_kind", config.actor_kind_for_claim(item["value"]))

    unique = {(item["value"], item["source"]): item for item in candidates}
    values = sorted({item["value"] for item in unique.values()})
    inferred_values = {
        item["value"]
        for item in unique.values()
        if item["source"] != "evidence.producer_authenticated"
    }
    unique_producer_identities = set(producer_identities)

    # A verifying producer settles who the *actor* was. Whether that actor
    # holds human authority depends on what kind of identity it is.
    if len(unique_producer_identities) == 1:
        verified_identity = producer_identities[0]
        kind = config.actor_kind_for_identity(verified_identity)
        verified_via = next(
            (p.get("verified_via") for p in producers if p.get("identity") == verified_identity),
            None,
        )
        if kind == "human":
            return {
                "value": verified_identity,
                "source": "evidence.producer_authenticated",
                "candidates": list(unique.values()),
                "actor_kind": "human",
                "human_authority": HUMAN_AUTHORITY_CREDENTIAL_ATTRIBUTED,
                "credential_attributed": True,
                "verified": True,
                "verified_via": verified_via,
                "conflict": False,
            }
        if human_claim_conflict:
            # No human producer to settle the question, and an explicit
            # [human: ...] claim names a *known agent* identity that this
            # very signature confirms is an agent — surface the
            # contradiction rather than silently reporting the agent's
            # signature as if no human claim had ever been made (task
            # #1217 finding 2). A genuine verified human producer (handled
            # above) still overrides this unconditionally.
            return {
                "value": "conflict",
                "source": "conflict",
                "candidates": list(unique.values()),
                "actor_kind": "conflict",
                "human_authority": "conflict",
                "credential_attributed": False,
                "verified": False,
                "conflict": True,
            }
        # A verified agent (or unrecognized-but-well-formed) identity: keep
        # the actor evidence front and center, but human authority is
        # unknown, not falsely verified.
        return {
            "value": verified_identity,
            "source": "evidence.producer_authenticated",
            "candidates": list(unique.values()),
            "actor_kind": kind,
            "human_authority": "conflict" if len(inferred_values) > 1 else "unknown",
            "credential_attributed": False,
            "verified": False,
            "verified_via": verified_via,
            "conflict": len(inferred_values) > 1,
        }
    if len(unique_producer_identities) > 1:
        # Ambiguous signed producers → fail closed on verification.
        return {
            "value": "conflict",
            "source": "conflict",
            "candidates": list(unique.values()),
            "actor_kind": "conflict",
            "human_authority": "conflict",
            "credential_attributed": False,
            "verified": False,
            "conflict": True,
        }

    if human_claim_conflict:
        # No signed producer to arbitrate, and an inferred "human" claim
        # names a known agent identity — surface the disagreement rather
        # than silently accepting or silently dropping it.
        return {
            "value": "conflict",
            "source": "conflict",
            "candidates": list(unique.values()),
            "actor_kind": "conflict",
            "human_authority": "conflict",
            "credential_attributed": False,
            "verified": False,
            "conflict": True,
        }

    conflict = len(values) > 1
    first = next(iter(unique.values())) if values else None
    value = first["value"] if first else "unknown"
    return {
        "value": value,
        "source": "conflict" if conflict else (first["source"] if first else "unknown"),
        "candidates": list(unique.values()),
        "actor_kind": "conflict" if conflict else (first["actor_kind"] if first else "unknown"),
        "human_authority": "conflict" if conflict else "unknown",
        "credential_attributed": False,
        "verified": False,
        "conflict": conflict,
    }


def _sod_pairs(approvals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Granted approvals as (id, requested_by, resolved_by) for the SoD check."""
    pairs = []
    for app in approvals:
        if app.get("status") not in ("approved", "consumed"):
            continue
        pairs.append(
            {
                "approval_id": app.get("id"),
                "requested_by": app.get("requested_by"),
                "resolved_by": app.get("resolved_by"),
            }
        )
    return pairs


def _separation_of_duties(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Report-level separation-of-duties status (task #1673).

    A deployment with one configured human cannot separate duties between
    people, so the report states ``not_applicable_single_operator`` rather
    than a pass or fail that would claim a control which cannot exist.
    With more than one configured human, every granted approval in scope is
    checked: its resolver must be a different person from its requester
    (aliases included, the same rule ``approval_resolve`` enforces).
    """
    humans = config.human_identity_labels()
    if len(humans) <= 1:
        return {
            "status": SOD_NOT_APPLICABLE_SINGLE_OPERATOR,
            "configured_humans": len(humans),
            "approvals_checked": 0,
            "violations": [],
        }
    from ..tools.approvals import _same_person

    checked = 0
    violations: list[dict[str, Any]] = []
    for record in records:
        for pair in record.get("_sod_pairs") or []:
            checked += 1
            if not pair.get("resolved_by") or _same_person(
                pair.get("resolved_by"), pair.get("requested_by")
            ):
                violations.append({"task_id": record.get("task_id"), **pair})
    if checked == 0:
        status = SOD_NO_DATA
    else:
        status = SOD_FAIL if violations else SOD_PASS
    return {
        "status": status,
        "configured_humans": len(humans),
        "approvals_checked": checked,
        "violations": violations,
    }


def _resolve_human(
    task: dict[str, Any], approvals: list[dict[str, Any]], delegations: list[dict[str, Any]]
) -> str:
    return _human_provenance(task, approvals, delegations)["value"]


def _build_attestation(*, scope: dict[str, Any], generated_at: str) -> dict[str, Any]:
    """Report-level attestation (task #1267, Epic F/F4): who attests to THIS
    report's generation, under which identity, with which signing key, at
    what time, and over what scope — so a standalone export names its own
    accountable attester without a live hub lookup (Epic #1191: exports
    verifiable without the hub).

    Deliberately distinct from ``human_provenance`` (task #1217), which
    answers "who is accountable for the recorded task action". This block
    answers "who is vouching for the report artifact itself" — the caller
    that ran `evidence report` / the API request that built it.

    Fail-closed / honesty rule (task #1217, extended here to the report
    surface itself): the caller's resolved identity is promoted to a HUMAN
    attestation only when the request authenticated through the hub's
    WorkOS leg — a live IdP session the hub itself verified (JWKS, issuer,
    org, live re-check; see `producer_sign.py`). A plain bearer token or
    OAuth grant never earns human attestation, regardless of whether the
    resolved label happens to be human-classified — it renders
    `agent-bearer-only`. No verified caller at all renders `unknown`. Never
    includes key material — only the signing key's id, same as `signature`.
    """
    identity = config.verified_agent_identity()
    metadata = config.current_auth_metadata()
    auth_method = metadata.get("auth_method") if isinstance(metadata, dict) else None
    key_id = evidence_signing.attestation_key_id()

    block: dict[str, Any] = {
        "attester": identity,
        "key_id": key_id,
        "attested_at": generated_at,
        "scope": dict(scope),
    }

    if not identity:
        block["attester_kind"] = ATTESTER_KIND_UNKNOWN
        block["verified_via"] = None
        block["human_attested"] = False
        return block

    if auth_method == "workos" and isinstance(metadata, dict):
        # Require the private ContextVar binding from HubAuthASGIMiddleware
        # (via bind_workos_request_auth) plus the claim set. A fabricated
        # auth_metadata dict — even with idp_verified=True or claim strings —
        # cannot earn human attestation without that binding (task #1313 /
        # Devin on #753).
        workos_claims = (
            metadata.get("workos_user_id"),
            metadata.get("workos_organization_id"),
            metadata.get("workos_session_id"),
        )
        if not config.workos_request_is_bound() or not all(
            isinstance(value, str) and value.strip() for value in workos_claims
        ):
            block["attester"] = None
            block["attester_kind"] = ATTESTER_KIND_UNKNOWN
            block["verified_via"] = None
            block["human_attested"] = False
            return block
        block["attester_kind"] = ATTESTER_KIND_HUMAN
        block["verified_via"] = "idp"
        block["human_attested"] = True
        block["workos_subject"] = metadata.get("workos_user_id")
        block["workos_organization_id"] = metadata.get("workos_organization_id")
        block["workos_session_id"] = metadata.get("workos_session_id")
        return block

    # Bearer/OAuth/unrecognized: never promoted to human attestation, even
    # when `identity` itself is a human-classified label (e.g.
    # the configured operator label) — only a hub-verified live IdP session proves a human
    # was actually present for this call.
    block["attester_kind"] = ATTESTER_KIND_AGENT_BEARER_ONLY
    block["verified_via"] = "agent_bearer" if auth_method == "bearer" else auth_method
    block["human_attested"] = False
    return block


_TS_COLS = ("created_at", "updated_at", "completed_at")


def _clamp_limit(limit: int | None, *, project_subtree: bool = False) -> int:
    if limit is None:
        return _MAX_LIMIT if project_subtree else _DEFAULT_LIMIT
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if n < 1:
        return _DEFAULT_LIMIT
    return min(n, _MAX_LIMIT)


def _append_time_window(
    query: list[str],
    params: list[Any],
    since: str | None,
    until: str | None,
    *,
    cols: tuple[str, ...] = _TS_COLS,
) -> None:
    """Require at least one lifecycle timestamp inside the advertised window.

    ``cols`` names the timestamp columns tested (the task's by default).
    """
    if since is None and until is None:
        return
    # A date-only upper bound (documented form: --until 2026-09-06) is an
    # inclusive day: normalize to end-of-day so same-day timestamps match.
    if until is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", until.strip()):
        until = until.strip() + "T23:59:59.999999+00:00"
    clauses: list[str] = []
    for col in cols:
        if since is not None and until is not None:
            clauses.append(f"({col} IS NOT NULL AND {col} >= ? AND {col} <= ?)")
            params.extend([since, until])
        elif since is not None:
            clauses.append(f"({col} IS NOT NULL AND {col} >= ?)")
            params.append(since)
        else:
            clauses.append(f"({col} IS NOT NULL AND {col} <= ?)")
            params.append(until)
    query.append("AND (" + " OR ".join(clauses) + ")")


def _resolve_device(task: dict[str, Any], events_list: list[dict[str, Any]]) -> str:
    """Resolve the execution device / host machine in the chain.

    Execution events are preferred over tasks.source_host: that column is
    stamped by task_add (the machine that performed the *creation* write),
    while dispatch/transition events record the host that actually executed
    the work. Creation host is only the fallback.
    """
    for ev in events_list:
        payload = ev.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if isinstance(payload, dict):
            ev_host = payload.get("host") or payload.get("source_host")
            if ev_host and str(ev_host).strip():
                return str(ev_host).strip()

    host = task.get("source_host")
    if host and host.strip():
        return host.strip()

    return _UNATTRIBUTED


def _device_link(
    verified_producers: list[dict[str, Any]] | None, fallback_host: str
) -> dict[str, Any] | None:
    """Build the record's device link from tailnet-verified producer events.

    A ``device`` claim is trusted only when it rides a chain-valid,
    signature-verified producer event (``verifying_producer_events`` drops
    everything else), so this resolves to ``verified`` only when the verified
    producers agree on exactly one node. Multiple nodes or a hostname-only
    trail degrade to an explicit unverified marker — fail closed, never a
    bare hostname presented as device identity (task #1250, E3).
    """
    devices = [
        producer["device"]
        for producer in (verified_producers or [])
        if isinstance(producer.get("device"), dict) and producer["device"].get("node")
    ]
    nodes = sorted({str(d["node"]) for d in devices})
    if len(nodes) == 1:
        device = devices[0]
        return {
            "verified": True,
            "node": nodes[0],
            "node_id": device.get("node_id"),
            "user": device.get("user"),
            "resolved_at": device.get("resolved_at"),
        }
    if len(nodes) > 1:
        return {"verified": False, "ambiguous": True, "nodes": nodes}
    if fallback_host and fallback_host != _UNATTRIBUTED:
        return {"verified": False, "host": fallback_host}
    return None


def _device_display(link: dict[str, Any] | None, fallback_host: str) -> str:
    """Render the Execution Device column. A verified tailnet node renders as
    itself; a hostname-only trail is explicitly marked unverified-device."""
    if link and link.get("verified"):
        return f"{link['node']} (verified)"
    if link and link.get("ambiguous"):
        return "unverified-device:multiple-nodes"
    if fallback_host and fallback_host != _UNATTRIBUTED:
        return f"unverified-device:{fallback_host}"
    return _UNATTRIBUTED


def _resolve_agent(task: dict[str, Any], events_list: list[dict[str, Any]]) -> str:
    """Resolve the executing AI agent in the chain."""
    claimed = task.get("claimed_by")
    if claimed and claimed.strip():
        return claimed.strip()

    notes = task.get("notes") or ""
    m = _CLAIMED_BY_RE.search(notes)
    if m:
        return m.group(1).strip()

    for ev in events_list:
        originator = ev.get("originator")
        if originator and originator.strip():
            return originator.strip()

    return _UNATTRIBUTED


def _merge_authorization(
    events_list: list[dict[str, Any]], chain_valid: bool
) -> dict[str, Any] | None:
    """The Approval Ledger's policy-authorized outcome for this task, if any (#1407).

    Its own evidence class: it is never folded into ``human_provenance`` and
    always reports ``human_approval: false``.
    """
    from ..github_app import EVIDENCE_CLASS_POLICY, POLICY_AUTHORIZED_CHANNEL

    for ev in reversed(events_list):
        if ev.get("channel") != POLICY_AUTHORIZED_CHANNEL:
            continue
        payload = producer_sign.parse_event_payload(ev.get("payload"))
        return {
            "evidence_class": EVIDENCE_CLASS_POLICY,
            "human_approval": False,
            "policy_ref": payload.get("policy_ref"),
            "authorized_by": payload.get("authorized_by"),
            "repo": payload.get("repo"),
            "number": payload.get("number"),
            "head_sha": payload.get("head_sha"),
            "authorization_id": payload.get("authorization_id"),
            "outcome_event": ev.get("ulid"),
            "recorded_by": ev.get("originator"),
            "tamper_evident": bool(chain_valid and ev.get("entry_hash")),
        }
    return None


def _resolve_proof(
    task: dict[str, Any], events_list: list[dict[str, Any]], chain_valid: bool
) -> dict[str, Any]:
    """Assemble the proof link: PR/commit refs, completed timestamps, and cryptographic hashes."""
    evidence_ref = task.get("evidence_ref")
    notes = task.get("notes") or ""

    pr_url = None
    sha = None
    if evidence_ref:
        m_pr = _PR_URL_RE.search(evidence_ref)
        if m_pr:
            pr_url = m_pr.group(0)
        m_sha = _SHA_RE.search(evidence_ref)
        if m_sha:
            sha = m_sha.group(0)

    if not pr_url:
        m_pr_notes = _PR_URL_RE.search(notes)
        if m_pr_notes:
            pr_url = m_pr_notes.group(0)

    if not sha:
        m_sha_notes = re.search(r"\b(?:SHA|commit):\s*([0-9a-f]{7,40})\b", notes, re.IGNORECASE)
        if m_sha_notes:
            sha = m_sha_notes.group(1)

    event_hashes = []
    for ev in events_list:
        if ev.get("entry_hash"):
            event_hashes.append(
                {
                    "ulid": ev["ulid"],
                    "channel": ev["channel"],
                    "created_at": ev["created_at"],
                    "entry_hash": ev["entry_hash"],
                    "prev_hash": ev.get("prev_hash"),
                }
            )

    proof = {
        "evidence_ref": evidence_ref,
        "pr_url": pr_url,
        "commit_sha": sha,
        "merge_verified": False,
        "merge_claim": _merge_claim(notes, pr_url),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "completed_at": task.get("completed_at"),
        "event_hashes": event_hashes,
        "events_count": len(events_list),
        "tamper_evident": bool(chain_valid and len(event_hashes) > 0),
    }
    # Present only when the ledger recorded one, so every other record keeps
    # its exact pre-#1407 shape.
    merge_authorization = _merge_authorization(events_list, chain_valid)
    if merge_authorization is not None:
        proof["merge_authorization"] = merge_authorization
    return proof


def _merge_authorization_label(proof: dict[str, Any]) -> str | None:
    auth = proof.get("merge_authorization")
    if not auth:
        return None
    return (
        f"{auth.get('evidence_class')} ({auth.get('policy_ref')} by "
        f"{auth.get('authorized_by')}; not a human approval)"
    )


def compute_attribution_coverage(
    *,
    window_days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    now: str | None = None,
    threshold: float = ATTRIBUTION_COVERAGE_THRESHOLD,
    conn: Any = None,
) -> dict[str, Any]:
    """Compute claimed_by coverage over done completions in a trailing window.

    Read-only projection. Excludes ``cancelled`` (and every non-``done`` status).
    Empty windows return ``gate='no_data'`` (skipped — not a pass). Coverage
    below ``threshold`` returns ``gate='fail'`` with
    ``gate_failure='attribution_coverage_below_threshold'`` and the
    unattributed task ids. Does not block task transitions.
    """
    close_conn = False
    if conn is None:
        conn = storage.connect()
        close_conn = True

    try:
        window_end = until or now or storage.now()
        if until is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", until.strip()):
            window_end = until.strip() + "T23:59:59.999999+00:00"

        if since is not None:
            window_start = since
            resolved_days = window_days
        else:
            days = DEFAULT_ATTRIBUTION_WINDOW_DAYS if window_days is None else int(window_days)
            resolved_days = days
            try:
                end_dt = datetime.fromisoformat(window_end.replace("Z", "+00:00"))
            except ValueError:
                end_dt = datetime.now(UTC)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=UTC)
            window_start = (end_dt - timedelta(days=days)).isoformat(timespec="seconds")

        # status='done' only — cancelled is excluded by construction. Whitespace
        # or empty claimed_by counts as unattributed (column authority).
        rows = conn.execute(
            "SELECT id, claimed_by FROM tasks "
            "WHERE status = 'done' "
            "AND completed_at IS NOT NULL "
            "AND completed_at >= ? AND completed_at <= ? "
            "ORDER BY id ASC",
            (window_start, window_end),
        ).fetchall()

        unattributed_ids: list[int] = []
        attributed = 0
        for row in rows:
            claimed = row["claimed_by"]
            if isinstance(claimed, str) and claimed.strip():
                attributed += 1
            else:
                unattributed_ids.append(int(row["id"]))

        denominator = len(rows)
        numerator = attributed
        if denominator == 0:
            return {
                "window_days": resolved_days,
                "window_start": window_start,
                "window_end": window_end,
                "numerator": 0,
                "denominator": 0,
                "coverage": None,
                "threshold": threshold,
                "gate": "no_data",
                "gate_failure": None,
                "unattributed_task_ids": [],
            }

        coverage = numerator / denominator
        if coverage < threshold:
            gate = "fail"
            gate_failure: str | None = ATTRIBUTION_COVERAGE_GATE_FAILURE
        else:
            gate = "pass"
            gate_failure = None

        return {
            "window_days": resolved_days,
            "window_start": window_start,
            "window_end": window_end,
            "numerator": numerator,
            "denominator": denominator,
            "coverage": coverage,
            "threshold": threshold,
            "gate": gate,
            "gate_failure": gate_failure,
            "unattributed_task_ids": unattributed_ids,
        }
    finally:
        if close_conn:
            conn.close()


def build_evidence_report(
    *,
    task_id: int | None = None,
    project_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    agent: str | None = None,
    project: str | None = None,
    pr: str | None = None,
    limit: int | None = None,
    complete: bool = False,
    conn: Any = None,
    item: str | None = None,
    before_task_id: int | None = None,
    chain_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete human->device->agent->action->proof evidence report.

    Args:
        task_id: Optional task ID to narrow to a single task.
        project_id: Optional project root task ID; includes its full task subtree.
        since: Optional ISO timestamp lower bound.
        until: Optional ISO timestamp upper bound.
        agent: Optional agent label filter.
        project: Optional project name filter.
        pr: Optional PR URL or commit SHA filter.
        item: Optional node dispatch item, as ``nd-…`` or the
            ``node:<node>:<item>`` evidence_ref (task #1989). Narrows the
            report to that item's task, and each record's proof events to
            the ones that item recorded (enqueue, turn summaries, receipt).
            An item with no task, or no such item, yields no records.
        limit: Max records to return (default 50, capped at 500). Ignored
            when ``complete=True``.
        complete: When True, read every matching record in the window
            uncapped — no default-50/max-500 clamp — and mark the report
            ``complete: true``. Existing CLI/API callers never set this
            (an unbounded read is a latency/DoS risk over those surfaces);
            it exists for ``audit.evidence_export_sink.run_once`` (task
            #1266), where a signed, customer-facing export silently
            dropping records past the default clamp would be a truncation
            bug, not a pagination nicety. Mutually exclusive with ``limit``.
        conn: Optional SQLite connection.
        before_task_id: Keyset cursor (task #2516): only tasks with a lower id,
            in the default newest-first order. A capped report carries
            ``next_cursor``, the cursor for the following page, or None on the
            last page. Not compatible with ``project_id`` (its order is the tree).
        chain_info: A ledger verification already made for this export, reused
            by every page of a paged export instead of verifying again.
    """
    if before_task_id is not None and project_id is not None:
        raise ToolError("before_task_id pages the newest-first order; project_id has its own")
    if complete and limit is not None:
        raise ToolError("limit is not compatible with complete=True (it reads the entire window)")
    limit = None if complete else _clamp_limit(limit, project_subtree=project_id is not None)
    edition.require_feature("evidence_reporting")
    if item is not None:
        parse_item_ref(item)
    close_conn = False
    if conn is None:
        conn = storage.connect()
        close_conn = True

    try:
        if chain_info is None:
            chain_info = events.verify_event_chain(conn)
        dispatch = _item_dispatch(conn, item) if item is not None else None

        def record_for_row(row: Any, *, item_scoped: bool = True) -> dict[str, Any]:
            """One task's record. With an item filter and ``item_scoped``, proof,
            receipts, agent and device describe the selected dispatch only."""
            scope = dispatch if item_scoped else None
            task = dict(row)
            task_id_value = task["id"]
            task_key = str(task_id_value)
            approvals = [
                dict(item)
                for item in conn.execute(
                    f"SELECT * FROM approvals WHERE {_APPROVAL_TASK_SQL} ORDER BY id ASC",
                    (task_key, task_key),
                ).fetchall()
            ]
            delegations = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM delegations WHERE task_id = ? ORDER BY id ASC",
                    (task_id_value,),
                ).fetchall()
            ]
            event_list = [
                dict(item)
                for item in conn.execute(
                    "SELECT ulid, channel, payload, originator, created_at, prev_hash, entry_hash "
                    f"FROM event_log WHERE {_EVENT_TASK_SQL} "
                    "ORDER BY ROWID ASC",
                    (task_key, task_key),
                ).fetchall()
            ]
            # Producer HMAC stamps for approvals may omit task_id when the
            # gated payload only had parent_task_id (live delegate path). Pull
            # those events by approval ref_id so provenance still verifies.
            approval_ids = [str(a["id"]) for a in approvals if a.get("id") is not None]
            if approval_ids:
                placeholders = ",".join("?" for _ in approval_ids)
                seen = {str(e.get("ulid")) for e in event_list if e.get("ulid")}
                for item in conn.execute(
                    "SELECT ulid, channel, payload, originator, created_at, prev_hash, entry_hash "
                    "FROM event_log WHERE channel = ? "
                    "AND CAST(json_extract(payload, '$.kind') AS TEXT) = 'approval' "
                    f"AND CAST(json_extract(payload, '$.ref_id') AS TEXT) IN ({placeholders}) "
                    "ORDER BY ROWID ASC",
                    (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED, *approval_ids),
                ).fetchall():
                    row = dict(item)
                    ulid = str(row.get("ulid") or "")
                    if ulid and ulid not in seen:
                        event_list.append(row)
                        seen.add(ulid)
            from ..delivery_receipts import receipts_for_task

            chain_ok = bool(chain_info.get("verified", False))
            verified_producers = producer_sign.verifying_producer_events(
                event_list, chain_valid=chain_ok
            )
            human_provenance = _human_provenance(
                task, approvals, delegations, verified_producers=verified_producers
            )
            proof_events = event_list
            if scope is not None:
                proof_events = [e for e in event_list if _event_item_id(e) == scope["id"]]
                # Signed producer events carry no item id, so none is the
                # selected dispatch's own: its agent and node come from the
                # dispatch row, explicitly unverified, and the record's
                # in-scope producers are empty (fail closed).
                fallback_host = str(scope["node"] or _UNATTRIBUTED)
                device_link = _device_link([], fallback_host)
                agent_label = str(scope["agent"] or _UNATTRIBUTED)
                scoped_producers: list[dict[str, Any]] = []
            else:
                fallback_host = _resolve_device(task, event_list)
                device_link = _device_link(verified_producers, fallback_host)
                agent_label = _resolve_agent(task, event_list)
                scoped_producers = verified_producers
            record = {
                "task_id": task_id_value,
                "project": task.get("project", "unified-agent"),
                "human": human_provenance["value"],
                "human_provenance": human_provenance,
                "device": _device_display(device_link, fallback_host),
                "device_link": device_link,
                "agent": agent_label,
                "action": {
                    "type": "task",
                    "title": task.get("title", ""),
                    "status": task.get("status", "open"),
                    "priority": task.get("priority", "med"),
                    "lane": task.get("lane", "default"),
                    "parent_id": task.get("parent_id"),
                    "project_depth": task.get("project_depth"),
                    "approvals": [
                        {
                            "id": approval["id"],
                            "tool": approval["tool"],
                            "status": approval["status"],
                            "resolved_by": approval.get("resolved_by"),
                            "reason": approval.get("reason"),
                        }
                        for approval in approvals
                    ],
                },
                "proof": (
                    _resolve_proof(task, proof_events, chain_ok)
                    if scope is None
                    else _item_proof(scope, proof_events, chain_ok)
                ),
                "delivery_receipts": [
                    receipt
                    for receipt in receipts_for_task(task_id_value, conn=conn)
                    if scope is None or receipt.get("evidence_ref") == scope["ref"]
                ],
                "_verified_producers": scoped_producers,
                "_sod_pairs": _sod_pairs(approvals),
            }
            if scope is not None:
                window_since, window_until = scope.get("created_at"), scope.get("completed_at")
            else:
                window_since = task.get("created_at")
                window_until = task.get("completed_at")
            record["denials"] = _record_denials(
                conn,
                _record_runs(conn, task_id_value, scope),
                since=window_since,
                until=window_until,
                callers=_participants(conn, task, scope, agent_label),
                org_id=task.get("org_id"),
            )
            if scope is not None:
                # The attribution contract of an item-filtered record; absent
                # otherwise, so an unfiltered record keeps its exact shape.
                record["item_scope"] = {
                    "item": scope["ref"],
                    "agent_source": "node_dispatch",
                    "device_source": "node_dispatch",
                    "task_wide": list(_ITEM_TASK_WIDE_FIELDS),
                }
            return record

        project_records = None
        if project_id is not None:
            root = conn.execute("SELECT notes FROM tasks WHERE id=?", (project_id,)).fetchone()
            if root is None:
                raise ToolError(f"no task with id {project_id}")
            if not _PROJECT_SPEC_MARKER_RX.search(root["notes"] or ""):
                raise ToolError(f"task {project_id} does not carry [project-spec]")
            project_rows = conn.execute(
                "WITH RECURSIVE project_subtree(id, depth, sort_path) AS ("
                " SELECT id, 0, printf('%020d', id) FROM tasks WHERE id = ?"
                " UNION ALL"
                " SELECT child.id, parent.depth + 1,"
                " parent.sort_path || '.' || printf('%020d', child.id)"
                " FROM tasks child"
                " JOIN project_subtree parent ON child.parent_id = parent.id"
                ")"
                " SELECT tasks.*, project_subtree.depth AS project_depth"
                " FROM tasks JOIN project_subtree ON project_subtree.id = tasks.id"
                " ORDER BY project_subtree.sort_path",
                (project_id,),
            ).fetchall()
            # The project chain describes the whole subtree: an item filter
            # narrows the displayed records only, never the chain's proof.
            project_records = [record_for_row(row, item_scoped=False) for row in project_rows]

        if project_id is None:
            query = ["SELECT tasks.*, NULL AS project_depth FROM tasks WHERE 1=1"]
        else:
            query = [
                "WITH RECURSIVE project_subtree(id, depth, sort_path) AS (",
                "SELECT id, 0, printf('%020d', id) FROM tasks WHERE id = ?",
                "UNION ALL",
                "SELECT child.id, parent.depth + 1,",
                "parent.sort_path || '.' || printf('%020d', child.id)",
                "FROM tasks child",
                "JOIN project_subtree parent ON child.parent_id = parent.id",
                ")",
                "SELECT tasks.*, project_subtree.depth AS project_depth",
                "FROM tasks JOIN project_subtree ON project_subtree.id = tasks.id",
                "WHERE 1=1",
            ]
        params: list[Any] = []
        if project_id is not None:
            params.append(project_id)

        if task_id is not None:
            query.append("AND tasks.id = ?")
            params.append(task_id)

        if before_task_id is not None:
            query.append("AND tasks.id < ?")
            params.append(before_task_id)

        if item is not None:
            # An item without a task (or no such item) has no task evidence.
            query.append("AND tasks.id = ?")
            assert dispatch is not None
            params.append(dispatch["task_id"] if dispatch["task_id"] is not None else -1)

        if project is not None:
            query.append("AND tasks.project = ?")
            params.append(project)

        if agent is not None and dispatch is not None:
            # An item-filtered record is attributed to the dispatch's agent,
            # so the agent filter matches that, not the task's claimant.
            if dispatch["agent"] != agent:
                query.append("AND 0")
        elif agent is not None:
            query.append("AND (tasks.claimed_by = ? OR tasks.notes LIKE ?)")
            params.append(agent)
            params.append(f"%claimed-by: {agent}%")

        if dispatch is not None:
            # An item-filtered record's proof is the item's own (its
            # timestamps and node reference), so the window and the PR filter
            # test that, never the task row another dispatch may have moved.
            if since is not None or until is not None:
                window: list[str] = []
                window_params: list[Any] = []
                _append_time_window(
                    window,
                    window_params,
                    since,
                    until,
                    cols=tuple(f"nd.{col}" for col in _TS_COLS),
                )
                query.append(
                    "AND EXISTS (SELECT 1 FROM node_dispatch nd WHERE nd.id = ? "
                    + " ".join(window)
                    + ")"
                )
                params.append(dispatch["id"])
                params.extend(window_params)
            if pr is not None:
                query.append("AND ? LIKE ?")
                params.extend([dispatch["ref"], f"%{pr}%"])
        else:
            _append_time_window(query, params, since, until)

            if pr is not None:
                query.append("AND (tasks.evidence_ref LIKE ? OR tasks.notes LIKE ?)")
                params.append(f"%{pr}%")
                params.append(f"%{pr}%")

        query.append(
            "ORDER BY project_subtree.sort_path"
            if project_id is not None
            else "ORDER BY tasks.id DESC"
        )
        if not complete:
            # One row past the page says whether another page follows.
            query.append("LIMIT ?")
            params.append(limit + 1)

        task_rows = conn.execute(" ".join(query), params).fetchall()
        next_cursor = None
        if not complete and len(task_rows) > limit:
            task_rows = task_rows[:limit]
            if project_id is None:
                next_cursor = task_rows[-1]["id"]

        records = [record_for_row(row) for row in task_rows]

        project_chain = None
        if project_id is not None:
            assert project_records is not None
            done_evidence = [
                {
                    "task_id": record["task_id"],
                    "evidence_ref": record["proof"]["evidence_ref"],
                    "pr_url": record["proof"]["pr_url"],
                    "commit_sha": record["proof"]["commit_sha"],
                    "merge_verified": record["proof"]["merge_verified"],
                    "merge_claim": record["proof"].get("merge_claim"),
                    "completed_at": record["proof"]["completed_at"],
                }
                for record in project_records
                if record["task_id"] != project_id
                and record["action"]["status"] == "done"
                and any(
                    record["proof"][field]
                    for field in ("evidence_ref", "pr_url", "commit_sha", "event_hashes")
                )
            ]
            merged_prs = [
                item for item in done_evidence if item["pr_url"] and item["merge_verified"]
            ]
            merged_prs.sort(key=lambda item: (item["completed_at"] or "", item["task_id"]))
            project_chain = {
                "project_id": project_id,
                "status": "complete" if merged_prs else "partial",
                "task_ids": [record["task_id"] for record in project_records],
                "done_task_ids": [
                    record["task_id"]
                    for record in project_records
                    if record["task_id"] != project_id and record["action"]["status"] == "done"
                ],
                "evidence": done_evidence,
                "merged_prs": merged_prs,
                "last_merge": merged_prs[-1] if merged_prs else None,
                "steps": [
                    {
                        "task_id": record["task_id"],
                        "title": record["action"]["title"],
                        "status": record["action"]["status"],
                        "project_depth": record["action"].get("project_depth"),
                        "proof": record["proof"],
                    }
                    for record in project_records
                ],
            }

        excluded: list[int] = []
        if project_id is not None:
            # Buyer-facing project artifacts omit unresolved agent rows.
            # Do not invent identity or backfill from source_host (UA #849).
            excluded = [
                record["task_id"]
                for record in records
                if (record.get("agent") or "").strip() == _UNATTRIBUTED
            ]
            records = [
                record for record in records if (record.get("agent") or "").strip() != _UNATTRIBUTED
            ]

        # Attribution wedge (task #1155): both flags flip true only when the
        # hash chain is valid AND at least one in-scope signed producer event
        # verifies against the agent's existing bearer. Fail-closed otherwise.
        chain_ok = bool(chain_info.get("verified", False))
        in_scope_producers = [
            producer
            for record in records
            for producer in (record.pop("_verified_producers", None) or [])
        ]
        # project_records may hold producers for rows filtered out of `records`;
        # attribution must not leak from excluded/unrelated subtree members.
        actor_auth = bool(chain_ok and in_scope_producers)
        separation_of_duties = _separation_of_duties(records)
        for record in records:
            record.pop("_sod_pairs", None)
        provenance_ok = bool(
            actor_auth
            and any((record.get("human_provenance") or {}).get("verified") for record in records)
        )

        # Coverage gate uses the report's since/until when provided; otherwise
        # the default trailing window. Independent of the records filter/limit.
        attribution_coverage = compute_attribution_coverage(since=since, until=until, conn=conn)
        gate_failures: list[str] = []
        if not chain_info.get("verified", False):
            gate_failures.append(EVIDENCE_CHAIN_INTEGRITY_GATE_FAILURE)
        if attribution_coverage.get("gate_failure"):
            gate_failures.append(attribution_coverage["gate_failure"])
        # clean_pass is an overall pass: the evidence chain must verify and
        # attribution coverage must pass. no_data is skipped, not a pass.
        clean_pass = bool(
            chain_info.get("verified", False) and attribution_coverage.get("gate") == "pass"
        )

        # Provenance graph appendix (task #1287): the same records reshaped as
        # a node/edge graph with a canonical hash, scoped to exactly this
        # report's task/project/time-window filter. Pure derivation over
        # `records` — no extra storage reads, no second source of truth.
        provenance_graph_appendix = provenance_graph.build_graph_from_records(
            records,
            scope={
                "kind": "task_scope",
                "task_id": task_id,
                "project_id": project_id,
                "since": since,
                "until": until,
                "agent": agent,
                "pr": pr,
                # Present only when set: an unfiltered scope keeps its exact
                # pre-#1989 shape (and canonical hash).
                **({"item": item} if item is not None else {}),
            },
        )

        generated_at = storage.now()
        # Task #1425: when a `project` stream filter is set, surface its
        # bound source repository so a reader of this report's `pr_url` /
        # `commit_sha` evidence knows which repo they should belong to.
        # Read-only resolution (project_bindings.resolve): no new reach.
        # The key is omitted entirely (not `None`) when `project` is unset,
        # matching the `NotRequired` TypedDict field and leaving an
        # unfiltered report's shape exactly as it was before this task.
        project_repo_binding_field: dict[str, Any] = {}
        if project:
            from ..project_bindings import resolve as _resolve_project_repo

            project_repo_binding_field = {"project_repo_binding": _resolve_project_repo(project)}
        filter_block = {
            "task_id": task_id,
            "project_id": project_id,
            "since": since,
            "until": until,
            "agent": agent,
            "project": project,
            "pr": pr,
            "limit": limit,
            **({"item": item} if item is not None else {}),
            **({"before_task_id": before_task_id} if before_task_id is not None else {}),
        }

        return {
            "report_type": "user_attributable_evidence",
            # 1.1 (task #1217): human_provenance.verified was redefined to mean
            # human_authority == "verified" instead of "a signed producer
            # uniquely verified" (see docs/evidence-report.md "Actor identity
            # vs. human authority"). actor_kind/human_authority are new
            # fields. Version-aware consumers built against 1.0 must not
            # assume `verified` still means actor authentication.
            # 1.2 (task #1249 / Epic E, E2): producer candidates and the
            # provenance result carry `verified_via` — "agent_bearer" or
            # "idp" — so a human link verified through a WorkOS session is
            # distinguishable from one verified only by an agent bearer HMAC.
            # 1.2 (task #1250 / Epic E, E3): `device` changed meaning from a
            # bare hostname to a device link (`device_link` carries the
            # verified tailnet node).
            # 1.3 (task #1287): new `provenance_graph` appendix — the report's
            # records reshaped as a node/edge graph with a canonical hash.
            # 1.4 (task #1265 / Epic F, F2): a named `anchor` header field
            # carries the external witness (RFC-3161 TSA, signed git commit,
            # or the legacy vault note) for chain_integrity.head_hash, plus a
            # verification recipe. Kept as its own field (not string-appended
            # onto chain_integrity) so task #1267 (F4 attestation block) can
            # add its own header field the same way.
            # 1.5 (task #1267 / Epic F, F4): a named `attestation` header field
            # names who attests to THIS report — the caller/identity that
            # generated it, IdP-verified as human where a live WorkOS session
            # backs it, otherwise `agent-bearer-only` and never promoted to
            # human (task #1217 honesty rule) — plus the signing key id, the
            # generation time, and the report's own filter scope.
            # 1.6: complete exports expose a completeness guarantee and the
            # `complete: true` field.
            # 1.7 (task #1673): human_provenance.human_authority says what the
            # evidence proves. "verified" is renamed "credential-attributed"
            # (a bearer/IdP credential bound to a human identity was used; it
            # does not prove a present human), "presence-verified" is reserved
            # for statement-bound passkey assertions, and records gain
            # `credential_attributed`. New report-level `sod` (and
            # `sod_detail`): `not_applicable_single_operator` when one human is
            # configured, else pass/fail/no_data over in-scope approvals.
            # `normalize_human_authority` reads <= 1.6 exports. Interactive
            # reports and complete exports now share one version.
            # 1.8 (task #2309): every record gains `denials`, the gateway
            # denials bound to its runs by episode id, with a `status`
            # (recorded / partial / not-recorded) so unbound history never
            # reads as a clean run.
            "schema_version": REPORT_SCHEMA_VERSION,
            **({"complete": True} if complete else {}),
            "generated_at": generated_at,
            "chain_integrity": {
                "status": chain_info.get("status", "unknown"),
                "verified": chain_info.get("verified", False),
                "actor_authentication_verified": actor_auth,
                "provenance_verified": provenance_ok,
                "head_hash": chain_info.get("head_hash"),
                "total_verified": chain_info.get("total_verified", 0),
                "unhashed_skipped": chain_info.get("unhashed_skipped", 0),
            },
            "anchor": chain_anchor.header_for_head(chain_info.get("head_hash"), conn=conn),
            "attestation": _build_attestation(scope=filter_block, generated_at=generated_at),
            "filter": filter_block,
            "total_records": len(records),
            "records": records,
            **({} if complete else {"next_cursor": next_cursor}),
            "unattributed_excluded": excluded,
            "project_chain": project_chain,
            "attribution_coverage": attribution_coverage,
            "gate_failures": gate_failures,
            "sod": separation_of_duties["status"],
            "sod_detail": separation_of_duties,
            **project_repo_binding_field,
            "clean_pass": clean_pass,
            "provenance_graph": provenance_graph_appendix,
        }
    finally:
        if close_conn:
            conn.close()


def _escape_md(value: Any) -> str:
    """Escape table-breaking characters in Markdown cells and labels."""
    return (
        str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    )


def _human_authority_of(record: dict[str, Any]) -> str:
    provenance = record.get("human_provenance") or {}
    return normalize_human_authority(
        provenance.get("human_authority"), verified=provenance.get("verified")
    )


def _human_label(record: dict[str, Any]) -> str:
    provenance = record.get("human_provenance") or {}
    candidates = provenance.get("candidates") or []
    candidate_text = (
        ", ".join(
            f"{item.get('value')} [{item.get('source')}"
            f"{', signed' if item.get('verified') else ''}"
            f"{', ' + item['actor_kind'] if item.get('actor_kind') else ''}]"
            for item in candidates
        )
        or "none"
    )
    # Human authority renders as what the evidence proves (task #1673): a
    # human-bound credential is "credential-attributed" (with the credential
    # class), never "verified". A verified AGENT signature is real actor
    # evidence but no human mandate, so it renders unattributed. Historical
    # exports that still say "verified" are read as credential-attributed.
    human_authority = normalize_human_authority(
        provenance.get("human_authority"), verified=provenance.get("verified")
    )
    via = provenance.get("verified_via")
    authority_bit = (
        f"{human_authority}-by-{via}"
        if human_authority in HUMAN_AUTHORITY_ATTRIBUTED and via
        else human_authority
        if human_authority in HUMAN_AUTHORITY_ATTRIBUTED
        else "unattributed"
    )
    actor_kind = provenance.get("actor_kind", "unknown")
    return (
        f"{record.get('human', 'unknown')} (source={provenance.get('source', 'unknown')}; "
        f"{authority_bit}; actor_kind={actor_kind}; human_authority={human_authority}; "
        f"candidates={candidate_text})"
    )


def _anchor_summary_line(anchor: dict[str, Any]) -> str:
    """One-line external-witness summary for the report header (task #1265, F2)."""
    if not anchor or anchor.get("status") != "anchored":
        return "Chain Anchor: not anchored"
    verification = anchor.get("verification") or {}
    verified = verification.get("verified")
    verified_s = "true" if verified is True else ("false" if verified is False else "unknown")
    reason = verification.get("reason", "unknown")
    line = f"Chain Anchor: backend={anchor.get('backend')} verified={verified_s} ({reason})"
    if anchor.get("entries_after_anchor") is not None:
        line += f"; covers the chain through {str(anchor.get('anchored_head'))[:16]}, "
        line += f"{anchor['entries_after_anchor']} later entries linked by hash chain"
    recipe = anchor.get("recipe")
    if recipe:
        line += f"  Recipe: {recipe}"
    return line


def _attestation_summary_line(attestation: dict[str, Any]) -> str:
    """One-line report-level attestation summary (task #1267, F4).

    Never renders `agent-bearer-only` as human — the honesty rule (#1217)
    applies to this header the same as it applies to `human_provenance`.
    """
    if not attestation or not attestation.get("attester"):
        return "Attestation: unattested"
    attester = attestation.get("attester")
    kind = attestation.get("attester_kind", ATTESTER_KIND_UNKNOWN)
    verified_via = attestation.get("verified_via")
    key_id = attestation.get("key_id") or "unsigned"
    via = f" verified-by-{verified_via}" if verified_via else ""
    return f"Attestation: {attester} (attester_kind={kind}{via}) key_id={key_id}"


def _step_proof_label(proof: dict[str, Any]) -> str:
    """Describe merge proof without upgrading an unverified PR to merged."""
    pr_url = proof.get("pr_url")
    if pr_url and proof.get("merge_verified"):
        return f"merged {pr_url}"
    if pr_url or proof.get("evidence_ref") or proof.get("commit_sha"):
        extra = f" ({pr_url})" if pr_url else ""
        return f"evidence present, merge not verified{extra}"
    return "no merge proof"


def _format_project_chain_markdown(report: dict[str, Any]) -> list[str]:
    chain = report.get("project_chain")
    if not chain:
        return []
    records = report.get("records") or []
    step_rows = chain.get("steps") or [
        {
            "task_id": r["task_id"],
            "title": r["action"]["title"],
            "status": r["action"]["status"],
            "project_depth": r["action"].get("project_depth"),
            "proof": r.get("proof") or {},
        }
        for r in records
    ]
    spec = next((r for r in step_rows if r.get("task_id") == chain["project_id"]), None)
    spec_title = spec["title"] if spec else ""
    spec_status = spec["status"] if spec else ""
    lines = [
        f"- **Project**: #{chain['project_id']} {_escape_md(spec_title)} (status=`{chain['status']}`)",
        "",
        "## Project spec",
        "",
        f"- **Id**: #{chain['project_id']}",
        f"- **Title**: {_escape_md(spec_title)}",
        f"- **Status**: `{spec_status}`",
        "- **Marker**: `[project-spec]`",
        "",
        "## Steps",
        "",
    ]
    for record in step_rows:
        depth = int(record.get("project_depth") or 0)
        indent = "  " * depth
        title = _escape_md(record["title"])
        status = record["status"]
        proof = _step_proof_label(record.get("proof") or {})
        lines.append(f"{indent}- #{record['task_id']} {title} — `{status}` — {proof}")
    lines.extend(["", "## Merged PRs", ""])
    merged = chain.get("merged_prs") or []
    if not merged:
        lines.append("- None")
    else:
        for item in merged:
            pr = item.get("pr_url") or "unspecified"
            sha = item.get("commit_sha")
            sha_bit = f" (`{sha}`)" if sha else ""
            merged_flag = "true" if item.get("merge_verified") is True else "false"
            lines.append(f"- #{item['task_id']} {pr}{sha_bit} — merge_verified=`{merged_flag}`")
    lines.extend(["", "## Last merge", ""])
    last = chain.get("last_merge")
    if not last:
        lines.append("- None")
    else:
        ref = last.get("pr_url") or last.get("evidence_ref") or "unspecified"
        when = last.get("completed_at") or "unknown"
        lines.append(f"- #{last['task_id']} {ref} at `{when}`")
    integrity = report.get("chain_integrity") or {}
    head = (integrity.get("head_hash") or "none")[:16]
    verified = integrity.get("verified")
    verified_s = "true" if verified is True else ("false" if verified is False else str(verified))
    lines.extend(
        [
            "",
            "## Audit verification",
            "",
            f"- status=`{integrity.get('status', 'unknown')}` verified={verified_s} "
            f"actor_authentication_verified="
            f"{'true' if integrity.get('actor_authentication_verified') is True else 'false'} "
            f"provenance_verified="
            f"{'true' if integrity.get('provenance_verified') is True else 'false'} "
            f"total_verified={integrity.get('total_verified', 0)} head=`{head}...`",
            "",
        ]
    )
    return lines


def _format_project_chain_text(report: dict[str, Any]) -> list[str]:
    chain = report.get("project_chain")
    if not chain:
        return []
    integrity = report.get("chain_integrity") or {}
    head = (integrity.get("head_hash") or "none")[:16]
    return [
        f"PROJECT CHAIN: id={chain['project_id']} status={chain['status']}",
        f"  tasks={chain.get('task_ids')} done={chain.get('done_task_ids')}",
        f"  merged_prs={[item.get('pr_url') for item in (chain.get('merged_prs') or [])]}",
        f"  last_merge={(chain.get('last_merge') or {}).get('pr_url')}",
        f"AUDIT: status={integrity.get('status')} chain_verified={integrity.get('verified')} "
        f"actor_authentication_verified={integrity.get('actor_authentication_verified', False)} "
        f"head={head}... total_verified={integrity.get('total_verified', 0)}",
    ]


def _format_provenance_graph_markdown(report: dict[str, Any]) -> list[str]:
    graph = report.get("provenance_graph")
    if not graph:
        return []
    counts = graph.get("node_counts_by_kind") or {}
    counts_text = ", ".join(f"{kind}={n}" for kind, n in sorted(counts.items())) or "none"
    lines = [
        "## Provenance graph",
        "",
        f"- **Nodes**: {graph.get('node_count', 0)} ({counts_text})",
        f"- **Edges**: {graph.get('edge_count', 0)}",
        f"- **Canonical hash**: `{graph.get('canonical_hash', '')}`",
        "",
        "| Source | Relation | Target |",
        "| --- | --- | --- |",
    ]
    for edge in graph.get("edges") or []:
        lines.append(
            f"| {_escape_md(edge.get('source'))} | {_escape_md(edge.get('relation'))} "
            f"| {_escape_md(edge.get('target'))} |"
        )
    lines.append("")
    return lines


def _format_provenance_graph_text(report: dict[str, Any]) -> list[str]:
    graph = report.get("provenance_graph")
    if not graph:
        return []
    lines = [
        f"PROVENANCE GRAPH: nodes={graph.get('node_count', 0)} edges={graph.get('edge_count', 0)} "
        f"hash={graph.get('canonical_hash', '')}",
    ]
    for edge in graph.get("edges") or []:
        lines.append(f"  {edge.get('source')} -[{edge.get('relation')}]-> {edge.get('target')}")
    return lines


def _format_evidence_report_csv(report: dict[str, Any]) -> str:
    """Row-per-record CSV. Every row repeats the chain-integrity summary so a
    filtered or re-sorted export still self-certifies without cross-referencing
    a separate header (task #943, pilot-slice evidence export)."""
    chain = report.get("chain_integrity", {})
    chain_status = chain.get("status", "unknown")
    chain_head = chain.get("head_hash") or ""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "record_type",
            "task_id",
            "project",
            "human",
            "device",
            "agent",
            "action_type",
            "action_title",
            "action_status",
            "action_priority",
            "action_lane",
            "evidence_ref",
            "pr_url",
            "commit_sha",
            "merge_verified",
            "completed_at",
            "events_count",
            "tamper_evident",
            "chain_status",
            "chain_head_hash",
            "human_source",
            "human_credential_attributed",
            "merge_claim_status",
            "human_candidates",
            "human_actor_kind",
            "human_authority",
            "signature_scheme",
            "signature_key_id",
            "signature_chain_head",
            "signature_generated_at",
            "anchor_status",
            "anchor_backend",
            "anchor_verified",
            "anchor_reason",
            "attestation_attester",
            "attestation_attester_kind",
            "attestation_verified_via",
            "attestation_key_id",
            "attestation_attested_at",
            "attestation_scope",
            "project_repo_binding",
        ]
    )
    signature = report.get("signature") or {}
    anchor = report.get("anchor") or {}
    anchor_verification = anchor.get("verification") or {}
    attestation = report.get("attestation") or {}
    for r in report.get("records", []):
        action = r.get("action") or {}
        proof = r.get("proof") or {}
        writer.writerow(
            [
                "record",
                r.get("task_id"),
                r.get("project"),
                r.get("human"),
                r.get("device"),
                r.get("agent"),
                action.get("type"),
                action.get("title"),
                action.get("status"),
                action.get("priority"),
                action.get("lane"),
                proof.get("evidence_ref") or "",
                proof.get("pr_url") or "",
                proof.get("commit_sha") or "",
                proof.get("merge_verified"),
                proof.get("completed_at") or "",
                proof.get("events_count", 0),
                proof.get("tamper_evident"),
                chain_status,
                chain_head,
                (r.get("human_provenance") or {}).get("source"),
                _human_authority_of(r) in HUMAN_AUTHORITY_ATTRIBUTED,
                (proof.get("merge_claim") or {}).get("status", "none"),
                json.dumps((r.get("human_provenance") or {}).get("candidates", []), sort_keys=True),
                (r.get("human_provenance") or {}).get("actor_kind", "unknown"),
                _human_authority_of(r),
                signature.get("scheme", ""),
                signature.get("key_id", ""),
                signature.get("chain_head", ""),
                signature.get("generated_at", ""),
                anchor.get("status", "not_anchored"),
                anchor.get("backend") or "",
                anchor_verification.get("verified", ""),
                anchor_verification.get("reason", ""),
                attestation.get("attester") or "",
                attestation.get("attester_kind") or "",
                attestation.get("verified_via") or "",
                attestation.get("key_id") or "",
                attestation.get("attested_at") or "",
                json.dumps(attestation.get("scope") or {}, sort_keys=True),
                json.dumps(report.get("project_repo_binding"), sort_keys=True)
                if report.get("project_repo_binding") is not None
                else "",
            ]
        )
    if not report.get("records"):
        metadata_row = [""] * 41
        metadata_row[0] = "report_metadata"
        metadata_row[34] = attestation.get("attester") or ""
        metadata_row[35] = attestation.get("attester_kind") or ""
        metadata_row[36] = attestation.get("verified_via") or ""
        metadata_row[37] = attestation.get("key_id") or ""
        metadata_row[38] = attestation.get("attested_at") or ""
        metadata_row[39] = json.dumps(attestation.get("scope") or {}, sort_keys=True)
        metadata_row[40] = (
            json.dumps(report.get("project_repo_binding"), sort_keys=True)
            if report.get("project_repo_binding") is not None
            else ""
        )
        writer.writerow(metadata_row)
    return buf.getvalue()


#: Page size of a streamed complete export (task #2516).
EXPORT_PAGE_SIZE = 200


def iter_evidence_report_csv(*, page_size: int | None = None, **filters: Any) -> Iterator[str]:
    """The complete CSV export, streamed one keyset page at a time (task #2516).

    Every matching record in the newest-first order, with no 500-row cap. Each
    page opens and closes its own connection, so nothing thread-bound is held
    across a yield and the iterator may resume on any thread (a streaming
    response does). The first page's ledger verification is reused by every
    later page, so every row carries the same chain head, signature, anchor and
    attestation columns as the first page's header. Takes
    ``build_evidence_report``'s filters except ``limit``, ``complete``,
    ``project_id``, ``before_task_id`` and ``conn``.
    """
    return _iter_csv(page_size=page_size, decorate=None, **filters)


def _iter_csv(
    *,
    page_size: int | None,
    decorate: Callable[[dict[str, Any]], dict[str, Any]] | None,
    **filters: Any,
) -> Iterator[str]:
    """``iter_evidence_report_csv``; ``decorate`` rewrites the first page's header
    (an export signer adds its signature metadata there)."""
    page_size = page_size or EXPORT_PAGE_SIZE
    page = build_evidence_report(limit=page_size, **filters)
    if decorate is not None:
        page = decorate(page)
    header = {key: value for key, value in page.items() if key != "records"}
    yield _format_evidence_report_csv(page)
    while page.get("next_cursor") is not None:
        page = build_evidence_report(
            limit=page_size,
            before_task_id=page["next_cursor"],
            chain_info=_chain_info_of(header),
            **filters,
        )
        if not page["records"]:
            break
        rows = _format_evidence_report_csv({**header, "records": page["records"]})
        yield rows.split("\r\n", 1)[1]


def iter_signed_evidence_report_csv(
    *, page_size: int | None = None, **filters: Any
) -> tuple[Iterator[bytes], Callable[[], dict[str, str]]]:
    """The complete signed CSV, one page at a time, signed while it is produced.

    Returns the encoded chunks and a ``sidecar()`` to call once they are all
    consumed: the same sidecar ``evidence_signing.sign_export`` makes over the
    whole body, computed incrementally (``evidence_signing.ExportSigner``), so
    no page list or full body is ever held in memory (task #2516).
    """
    from . import evidence_signing

    signer = evidence_signing.ExportSigner()
    seen: dict[str, Any] = {}

    def decorate(page: dict[str, Any]) -> dict[str, Any]:
        page = evidence_signing.with_report_metadata(page)
        seen["chain_head"] = str((page.get("chain_integrity") or {}).get("head_hash") or "")
        seen["generated_at"] = str(page.get("generated_at") or "")
        return page

    def chunks() -> Iterator[bytes]:
        for text in _iter_csv(page_size=page_size, decorate=decorate, **filters):
            data = evidence_signing.encode_for_signing(text)
            signer.update(data)
            yield data

    def sidecar() -> dict[str, str]:
        return signer.finish(chain_head=seen["chain_head"], generated_at=seen["generated_at"])

    return chunks(), sidecar


def _chain_info_of(report: dict[str, Any]) -> dict[str, Any]:
    """The ``verify_event_chain`` result a report was built from, for its next page."""
    chain = report.get("chain_integrity") or {}
    return {
        "status": chain.get("status", "unknown"),
        "verified": chain.get("verified", False),
        "head_hash": chain.get("head_hash"),
        "total_verified": chain.get("total_verified", 0),
        "unhashed_skipped": chain.get("unhashed_skipped", 0),
    }


def _pdf_safe(value: Any) -> str:
    """fpdf2's built-in core fonts only encode latin-1; degrade unsupported
    characters (emoji, curly quotes) instead of raising mid-render."""
    return str(value).encode("latin-1", "replace").decode("latin-1")


def _format_evidence_report_pdf(report: dict[str, Any]) -> bytes:
    """Render the evidence report as a tamper-evident PDF (task #956).

    Raises ValueError — the route layer turns this into a clear 4xx — rather
    than emitting a PDF that silently omits the chain proof it claims to
    carry: an empty record set or a tampered chain would make the PDF's own
    self-certification (chain_status/chain_head_hash on every entry) false.
    """
    records = report.get("records") or []
    if not records:
        raise ValueError("cannot export PDF: report has no evidence records")

    chain = report.get("chain_integrity", {})
    chain_status = chain.get("status", "unknown")
    chain_head = chain.get("head_hash") or ""
    if chain_status == "tampered":
        raise ValueError(
            f"cannot export PDF: event chain integrity check failed (status={chain_status})"
        )

    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise RuntimeError(
            "PDF export requires the optional 'pdf' extra: "
            "install with `pip install 'unified-agent[pdf]'` or `uv sync --all-extras`"
        ) from exc

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "User-Attributable Agent Evidence Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(
        0,
        6,
        _pdf_safe(f"Generated At: {report.get('generated_at')}"),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.cell(
        0,
        6,
        _pdf_safe(f"Chain Status: {chain_status}  |  Chain Head Hash: {chain_head}"),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.cell(
        0,
        6,
        "Actor authentication: unavailable",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    binding = report.get("project_repo_binding")
    if binding is not None:
        pdf.multi_cell(
            0,
            6,
            _pdf_safe(f"Project Repository Binding: {json.dumps(binding, sort_keys=True)}"),
            new_x="LMARGIN",
            new_y="NEXT",
        )
    signature = report.get("signature") or {}
    if signature:
        pdf.cell(
            0,
            6,
            _pdf_safe(
                f"Export Signature: {signature.get('scheme')}  |  Key ID: {signature.get('key_id')}"
            ),
            new_x="LMARGIN",
            new_y="NEXT",
        )
    anchor = report.get("anchor") or {}
    pdf.cell(
        0,
        6,
        _pdf_safe(_anchor_summary_line(anchor)),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.cell(
        0,
        6,
        _pdf_safe(_attestation_summary_line(report.get("attestation") or {})),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.cell(
        0,
        6,
        _pdf_safe(f"Total Records: {report.get('total_records', 0)}"),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    if report.get("sod"):
        pdf.cell(
            0,
            6,
            _pdf_safe(f"Separation of duties: {report['sod']}"),
            new_x="LMARGIN",
            new_y="NEXT",
        )
    pdf.ln(4)

    for r in records:
        action = r.get("action") or {}
        proof = r.get("proof") or {}
        pdf.set_font("Helvetica", "B", 9)
        pdf.multi_cell(
            0,
            5,
            _pdf_safe(f"Task #{r.get('task_id')} - {action.get('title', '')}"),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.set_font("Helvetica", "", 8)
        pdf.multi_cell(
            0,
            5,
            new_x="LMARGIN",
            new_y="NEXT",
            text=_pdf_safe(
                f"Human: {_human_label(r)}   Device: {r.get('device')}   Agent: {r.get('agent')}\n"
                f"Status: {action.get('status')}   Priority: {action.get('priority')}   "
                f"Lane: {action.get('lane')}\n"
                f"Evidence Ref: {proof.get('evidence_ref') or ''}\n"
                f"PR: {proof.get('pr_url') or ''}   Commit: {proof.get('commit_sha') or ''}   "
                f"Merge Verified: {proof.get('merge_verified')}\n"
                f"Merge Claim: {(proof.get('merge_claim') or {}).get('status', 'none')} (unverified)\n"
                f"Completed At: {proof.get('completed_at') or ''}   "
                f"Events: {proof.get('events_count', 0)}   "
                f"Tamper Evident: {proof.get('tamper_evident')}\n"
                f"Chain Status: {chain_status}   Chain Head Hash: {chain_head}"
            ),
        )
        pdf.ln(2)

    output = pdf.output()
    return bytes(output)


def format_evidence_report(report: dict[str, Any], fmt: str = "json") -> str | bytes:
    """Format an evidence report into json, csv, markdown, text, or pdf representation."""
    fmt = fmt.lower().strip()
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=True)
    if fmt == "csv":
        return _format_evidence_report_csv(report)
    if fmt == "pdf":
        return _format_evidence_report_pdf(report)

    chain = report.get("chain_integrity", {})
    head = (chain.get("head_hash") or "none")[:16]
    status = chain.get("status", "unknown")
    total_v = chain.get("total_verified", 0)

    if fmt == "markdown":
        lines = [
            "# User-Attributable Agent Evidence Report",
            "",
            f"- **Generated At**: `{report.get('generated_at')}`",
            f"- **Ledger Chain Integrity**: `{status}` ({total_v} entries verified, head=`{head}...`)",
            "- **Actor authentication**: unavailable; hash-chain integrity does not authenticate the recorded actor or merge.",
            f"- **Total Records**: {report.get('total_records', 0)}",
        ]
        if report.get("sod"):
            lines.append(f"- **Separation of duties**: `{report['sod']}`")
        binding = report.get("project_repo_binding")
        if binding is not None:
            lines.append(
                f"- **Project Repository Binding**: `{json.dumps(binding, sort_keys=True)}`"
            )
        signature = report.get("signature") or {}
        if signature:
            lines.append(
                f"- **Export signature**: `{signature.get('scheme')}` "
                f"key_id=`{signature.get('key_id')}` chain_head=`{signature.get('chain_head')}`"
            )
        lines.append(f"- **{_anchor_summary_line(report.get('anchor') or {})}**")
        lines.append(f"- **{_attestation_summary_line(report.get('attestation') or {})}**")
        cov = report.get("attribution_coverage") or {}
        if cov:
            cov_gate = cov.get("gate", "unknown")
            window_start = cov.get("window_start", "unknown")
            window_end = cov.get("window_end", "unknown")
            if cov.get("denominator"):
                pct = f"{100.0 * (cov.get('coverage') or 0):.1f}%"
                lines.append(
                    f"- **Attribution coverage (platform-wide coverage window "
                    f"{window_start} through {window_end})**: "
                    f"{cov.get('numerator')}/{cov.get('denominator')} "
                    f"({pct}) gate=`{cov_gate}` threshold={cov.get('threshold')}"
                )
            else:
                lines.append(
                    f"- **Attribution coverage (platform-wide coverage window "
                    f"{window_start} through {window_end})**: gate=`{cov_gate}` "
                    "(no completions in window)"
                )
            lines.append(
                "- **Scope**: platform-wide selected coverage window, independent of the displayed task filter."
            )
            lines.append(
                "- **Attribution note**: this gate and clean pass do not certify each row's human or device attribution."
            )
            if report.get("clean_pass") is False and cov_gate == "fail":
                fail = cov.get("gate_failure") or ATTRIBUTION_COVERAGE_GATE_FAILURE
                ids = ", ".join(f"#{tid}" for tid in (cov.get("unattributed_task_ids") or []))
                lines.append(f"- **Gate failure**: `{fail}` unattributed=[{ids}]")
            for failure in report.get("gate_failures") or []:
                if failure != cov.get("gate_failure"):
                    lines.append(f"- **Gate failure**: `{failure}`")
            lines.append(
                f"- **Clean pass**: `{'true' if report.get('clean_pass') is True else 'false'}`"
            )
            failures = report.get("gate_failures") or []
            lines.append(f"- **Gate failures**: {', '.join(failures) if failures else 'none'}")
        excluded = report.get("unattributed_excluded") or []
        if excluded:
            ids = ", ".join(f"#{task_id}" for task_id in excluded)
            lines.append(f"- **Unattributed excluded**: {len(excluded)} ({ids})")
        project_lines = _format_project_chain_markdown(report)
        if project_lines:
            lines.extend(project_lines)
        else:
            lines.append("")
        lines.extend(
            [
                "| Task / Item | Accountable Human | Execution Device | Agent | Action / Status | Proof & Evidence |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for r in report.get("records", []):
            task_title = f"#{r['task_id']} {r['action']['title']}"
            human = _human_label(r)
            device = r.get("device", "-")
            agent = r.get("agent", "-")
            action = f"{r['action']['status']} ({r['action']['priority']})"
            proof_obj = r.get("proof", {})
            evidence_ref = (
                proof_obj.get("evidence_ref")
                or proof_obj.get("pr_url")
                or proof_obj.get("commit_sha")
                or "unspecified"
            )
            proof = f"{evidence_ref} (completed: {proof_obj.get('completed_at') or 'in-progress'})"
            proof += f"; merge claim: {(proof_obj.get('merge_claim') or {}).get('status', 'none')} (unverified)"
            merge_auth = _merge_authorization_label(proof_obj)
            if merge_auth:
                proof += f"; merge authorization: {merge_auth}"
            lines.append(
                f"| {_escape_md(task_title)} | {_escape_md(human)} | {_escape_md(device)} | {_escape_md(agent)} | {_escape_md(action)} | {_escape_md(proof)} |"
            )
        graph_lines = _format_provenance_graph_markdown(report)
        if graph_lines:
            lines.append("")
            lines.extend(graph_lines)
        return "\n".join(lines)

    lines = [
        f"EVIDENCE REPORT (generated: {report.get('generated_at')})",
        f"Chain Integrity: status={status} head={head}... verified={chain.get('verified')} ({total_v} entries); actor authentication unavailable",
        f"Total Records: {report.get('total_records', 0)}",
    ]
    if report.get("sod"):
        lines.append(f"Separation of duties: {report['sod']}")
    binding = report.get("project_repo_binding")
    if binding is not None:
        lines.append(f"Project Repository Binding: {json.dumps(binding, sort_keys=True)}")
    signature = report.get("signature") or {}
    if signature:
        lines.append(
            f"Export Signature: scheme={signature.get('scheme')} key_id={signature.get('key_id')} "
            f"chain_head={signature.get('chain_head')}"
        )
    lines.append(_anchor_summary_line(report.get("anchor") or {}))
    lines.append(_attestation_summary_line(report.get("attestation") or {}))
    cov = report.get("attribution_coverage") or {}
    if cov:
        window_start = cov.get("window_start", "unknown")
        window_end = cov.get("window_end", "unknown")
        if cov.get("denominator"):
            pct = f"{100.0 * (cov.get('coverage') or 0):.1f}%"
            lines.append(
                f"Attribution coverage (platform-wide coverage window "
                f"{window_start} through {window_end}): "
                f"{cov.get('numerator')}/{cov.get('denominator')} ({pct}) gate={cov.get('gate')}"
            )
        else:
            lines.append(
                f"Attribution coverage (platform-wide coverage window "
                f"{window_start} through {window_end}): gate={cov.get('gate')} "
                "(no completions in window)"
            )
        lines.append(
            "Scope: platform-wide selected coverage window, independent of the displayed task filter."
        )
        lines.append(
            "This gate and clean pass do not certify each row's human or device attribution."
        )
        failures = report.get("gate_failures") or []
        lines.append(f"Clean pass: {'true' if report.get('clean_pass') is True else 'false'}")
        lines.append(f"Gate failures: {', '.join(failures) if failures else 'none'}")
    lines.extend(_format_project_chain_text(report))
    lines.append("-" * 80)
    for r in report.get("records", []):
        merge_auth = (r["proof"].get("merge_authorization") or {}).get("evidence_class")
        lines.append(
            f"[Task #{r['task_id']}] {r['action']['title']}\n"
            f"  Human:  {_human_label(r)}\n"
            f"  Device: {r['device']}\n"
            f"  Agent:  {r['agent']}\n"
            f"  Action: {r['action']['status']} (lane={r['action']['lane']})\n"
            f"  Proof:  evidence_ref={r['proof']['evidence_ref']} completed_at={r['proof']['completed_at']} "
            f"merge_claim={(r['proof'].get('merge_claim') or {}).get('status', 'none')} (unverified) "
            f"tamper_evident={r['proof']['tamper_evident']}"
            + (f" merge_authorization={merge_auth} (not a human approval)" if merge_auth else "")
        )
    graph_lines = _format_provenance_graph_text(report)
    if graph_lines:
        lines.append("-" * 80)
        lines.extend(graph_lines)
    return "\n".join(lines)
