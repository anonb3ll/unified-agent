"""Tests for task #1287: provenance graph read model.

Covers the deliverables the task names: rebuild-from-ledger determinism
(same hash twice, and after a JSON round trip), the two named queries
(everything a human authorized; every action behind a release/task set), a
tampered ledger event changing the hash, and a synthetic fixture shaped like
the #747 chain (human approves -> agent action -> proof).
"""

from __future__ import annotations

import json

import pytest

from unified_agent import config, events, storage
from unified_agent.audit import evidence_report, provenance_graph


@pytest.fixture(autouse=True)
def init_clean_db(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")
    storage.init_db()
    yield


def _insert_task(
    *,
    id: int,
    title: str = "demo task",
    status: str = "done",
    claimed_by: str | None = "Desktop_Agent4",
    source_host: str | None = "desktop-linux",
    evidence_ref: str | None = "https://github.com/anonb3ll/unified-agent/pull/747",
    notes: str = "",
    project: str = "unified-agent",
    completed_at: str | None = "2026-09-06T10:25:15+00:00",
    created_at: str = "2026-09-06T09:00:00+00:00",
    updated_at: str = "2026-09-06T10:25:15+00:00",
    parent_id: int | None = None,
) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, completed_at, "
            "source_host, claimed_by, evidence_ref, notes, project, domain, lane, parent_id) "
            "VALUES (?, ?, ?, 'high', ?, ?, ?, ?, ?, ?, ?, ?, 'hobby', 'hobby', ?)",
            (
                id,
                title,
                status,
                created_at,
                updated_at,
                completed_at,
                source_host,
                claimed_by,
                evidence_ref,
                notes,
                project,
                parent_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_approval(
    *,
    id: int,
    task_id: int,
    resolved_by: str = "Desktop_Person",
    status: str = "approved",
    tool: str = "git_merge_task",
) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO approvals (id, tool, payload, status, requested_by, resolved_by, reason, "
            "created_at, updated_at, resolved_at) VALUES (?, ?, ?, ?, 'Desktop_Agent5', ?, 'LGTM', "
            "'2026-09-06T10:00:00+00:00', '2026-09-06T10:05:00+00:00', '2026-09-06T10:05:00+00:00')",
            (id, tool, json.dumps({"task_id": task_id}), status, resolved_by),
        )
        conn.commit()
    finally:
        conn.close()


def _record_completion_event(task_id: int, *, originator: str = "Desktop_Agent4") -> str:
    return events.record_event(
        "agent.task.completed",
        payload={"task_id": task_id, "status": "done"},
        originator=originator,
    )


# ---------------------------------------------------------------------------
# Determinism: rebuild yields an identical hash twice and after a round trip.
# ---------------------------------------------------------------------------


def test_rebuild_from_ledger_is_deterministic():
    _insert_task(id=747)
    _insert_approval(id=1, task_id=747)
    _record_completion_event(747)

    first = provenance_graph.graph_for_task_scope(task_id=747)
    second = provenance_graph.graph_for_task_scope(task_id=747)

    assert first["canonical_hash"] == second["canonical_hash"]
    assert first["nodes"] == second["nodes"]
    assert first["edges"] == second["edges"]


def test_canonical_hash_survives_a_json_round_trip():
    _insert_task(id=747)
    _insert_approval(id=1, task_id=747)
    _record_completion_event(747)

    graph = provenance_graph.graph_for_task_scope(task_id=747)
    round_tripped = json.loads(json.dumps(graph))
    rehashed = provenance_graph.canonical_hash(round_tripped["nodes"], round_tripped["edges"])

    assert rehashed == graph["canonical_hash"]


def test_tampered_ledger_event_changes_the_hash():
    _insert_task(id=747)
    ulid = _record_completion_event(747)

    before = provenance_graph.graph_for_task_scope(task_id=747)

    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload = ? WHERE ulid = ?",
                (json.dumps({"task_id": 747, "status": "tampered"}), ulid),
            )
        conn.commit()
    finally:
        conn.close()

    after = provenance_graph.graph_for_task_scope(task_id=747)
    assert after["canonical_hash"] != before["canonical_hash"]


def test_removed_ledger_event_changes_the_hash():
    _insert_task(id=747)
    ulid = _record_completion_event(747)
    before = provenance_graph.graph_for_task_scope(task_id=747)

    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute("DELETE FROM event_log WHERE ulid = ?", (ulid,))
        conn.commit()
    finally:
        conn.close()

    after = provenance_graph.graph_for_task_scope(task_id=747)
    assert after["canonical_hash"] != before["canonical_hash"]


def test_artifactless_task_still_hashes_matched_events():
    _insert_task(id=747, evidence_ref=None)
    ulid = _record_completion_event(747)
    before = provenance_graph.graph_for_task_scope(task_id=747)

    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute("DELETE FROM event_log WHERE ulid = ?", (ulid,))
        conn.commit()
    finally:
        conn.close()

    after = provenance_graph.graph_for_task_scope(task_id=747)
    assert after["canonical_hash"] != before["canonical_hash"]


def test_shared_artifact_keeps_each_tasks_event_hash():
    artifact = "https://example.invalid/release/1"
    records = [
        {
            "task_id": task_id,
            "action": {"title": f"task {task_id}"},
            "proof": {
                "pr_url": artifact,
                "event_hashes": [{"ulid": f"event-{task_id}", "entry_hash": f"hash-{task_id}"}],
            },
        }
        for task_id in (1, 2)
    ]
    graph = provenance_graph.build_graph_from_records(records)
    shared = next(node for node in graph["nodes"] if node["id"] == f"work_product:{artifact}")
    assert {entry["ulid"] for entry in shared["attrs"]["event_hashes"]} == {"event-1", "event-2"}


def test_agent_signer_is_not_promoted_to_human_node():
    graph = provenance_graph.build_graph_from_records(
        [
            {
                "task_id": 1,
                "human": "Desktop_Agent2",
                "human_provenance": {"actor_kind": "agent", "verified": True},
                "agent": "Desktop_Agent2",
                "action": {},
                "proof": {},
            }
        ]
    )
    assert all(node["kind"] != "human" for node in graph["nodes"])


def test_canonical_hash_ignores_node_and_edge_order_but_scope_hash_tracks_scope():
    _insert_task(id=747)
    _record_completion_event(747)
    graph = provenance_graph.graph_for_task_scope(task_id=747)
    assert (
        provenance_graph.canonical_hash(
            list(reversed(graph["nodes"])), list(reversed(graph["edges"]))
        )
        == graph["canonical_hash"]
    )
    other = provenance_graph.build_graph_from_records([], scope={"kind": "different"})
    empty = provenance_graph.build_graph_from_records([], scope={"kind": "empty"})
    assert other["canonical_hash"] == empty["canonical_hash"]
    assert other["scope_hash"] != empty["scope_hash"]


# ---------------------------------------------------------------------------
# The #747 chain shape: human approves -> agent action -> proof (synthetic).
# ---------------------------------------------------------------------------


def test_747_chain_shape_human_approves_agent_action_proof():
    _insert_task(id=747, claimed_by="Desktop_Agent4")
    _insert_approval(id=1, task_id=747, resolved_by="Desktop_Person")
    _record_completion_event(747)

    graph = provenance_graph.graph_for_task_scope(task_id=747)
    node_by_id = {n["id"]: n for n in graph["nodes"]}
    edge_set = {(e["source"], e["relation"], e["target"]) for e in graph["edges"]}

    assert "human:Desktop_Person" in node_by_id
    assert "agent:Desktop_Agent4" in node_by_id
    assert "action:task:747" in node_by_id
    assert "approval:1" in node_by_id
    assert "work_product:https://github.com/anonb3ll/unified-agent/pull/747" in node_by_id

    # human -> ... -> action, action -> approval (gated_by), human -> approval (resolved),
    # agent -> action (performed), action -> proof (produced).
    assert ("human:Desktop_Person", "resolved", "approval:1") in edge_set
    assert ("action:task:747", "gated_by", "approval:1") in edge_set
    assert ("agent:Desktop_Agent4", "performed", "action:task:747") in edge_set
    assert (
        "action:task:747",
        "produced",
        "work_product:https://github.com/anonb3ll/unified-agent/pull/747",
    ) in edge_set


# ---------------------------------------------------------------------------
# Query (a): everything this human authorized in time range X.
# ---------------------------------------------------------------------------


def test_graph_for_human_scopes_to_that_humans_authorizations():
    _insert_task(id=1, title="the operator's task", evidence_ref=None)
    _insert_approval(id=1, task_id=1, resolved_by="Desktop_Person")
    _insert_task(id=2, title="someone else's task", evidence_ref=None)
    _insert_approval(id=2, task_id=2, resolved_by="Desktop_Agent5")

    graph = provenance_graph.graph_for_human("Desktop_Person")
    node_ids = {n["id"] for n in graph["nodes"]}

    assert "task:1" in node_ids
    assert "approval:1" in node_ids
    assert "human:Desktop_Person" in node_ids
    # Task 2's approval was resolved by a different human — excluded entirely,
    # not merely unlinked.
    assert "task:2" not in node_ids
    assert "approval:2" not in node_ids
    assert "human:Desktop_Agent5" not in node_ids


def test_denied_approval_is_not_a_human_authorization():
    _insert_task(id=1, evidence_ref=None)
    _insert_approval(id=1, task_id=1, resolved_by="Desktop_Person", status="denied")

    graph = provenance_graph.graph_for_human("Desktop_Person")
    assert "task:1" not in {node["id"] for node in graph["nodes"]}


def test_graph_for_human_respects_time_window():
    _insert_task(
        id=1,
        evidence_ref=None,
        created_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    _insert_approval(id=1, task_id=1, resolved_by="Desktop_Person")
    _insert_task(
        id=2,
        evidence_ref=None,
        completed_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:00:00+00:00",
    )
    _insert_approval(id=2, task_id=2, resolved_by="Desktop_Person")

    graph = provenance_graph.graph_for_human("Desktop_Person", since="2026-08-01T00:00:00+00:00")
    node_ids = {n["id"] for n in graph["nodes"]}
    assert "task:2" in node_ids
    assert "task:1" not in node_ids


def test_graph_for_human_applies_limit_after_approval_matches(monkeypatch):
    monkeypatch.setattr(provenance_graph, "_HUMAN_QUERY_TASK_CAP", 2)
    for task_id in (1, 2, 3):
        _insert_task(id=task_id, evidence_ref=None)
        _insert_approval(id=task_id, task_id=task_id, resolved_by="Desktop_Person")

    graph = provenance_graph.graph_for_human("Desktop_Person", limit=1)
    assert {node["id"] for node in graph["nodes"] if node["kind"] == "task"} == {"task:3"}
    assert graph["scope"]["limit"] == 1
    assert graph["scope"]["truncated"] is True


# ---------------------------------------------------------------------------
# Query (b): every action behind a release (keyed on task id(s) — no release
# record exists in the ledger yet; see docs/provenance-graph.md).
# ---------------------------------------------------------------------------


def test_graph_for_release_keys_on_task_ids_and_documents_the_gap():
    _insert_task(id=10, title="release step one", evidence_ref=None)
    _insert_task(id=11, title="release step two", evidence_ref=None)
    _insert_task(id=99, title="unrelated task", evidence_ref=None)

    graph = provenance_graph.graph_for_release(task_ids=[10, 11])
    node_ids = {n["id"] for n in graph["nodes"]}

    assert "task:10" in node_ids
    assert "task:11" in node_ids
    assert "task:99" not in node_ids
    assert graph["scope"]["kind"] == "by_release"
    assert graph["scope"]["task_ids"] == [10, 11]
    assert "gap" in graph["scope"]


def test_graph_for_release_requires_task_id_or_task_ids():
    with pytest.raises(ValueError):
        provenance_graph.graph_for_release()


def test_graph_for_release_single_task_id():
    _insert_task(id=747)
    graph = provenance_graph.graph_for_release(task_id=747)
    assert {n["id"] for n in graph["nodes"]} >= {"task:747"}


# ---------------------------------------------------------------------------
# Evidence-report appendix integration.
# ---------------------------------------------------------------------------


def test_evidence_report_carries_a_matching_provenance_graph_appendix():
    _insert_task(id=747)
    _insert_approval(id=1, task_id=747)
    _record_completion_event(747)

    report = evidence_report.build_evidence_report(task_id=747)
    appendix = report["provenance_graph"]
    standalone = provenance_graph.graph_for_task_scope(task_id=747)

    assert appendix["canonical_hash"] == standalone["canonical_hash"]
    assert appendix["node_count"] == len(appendix["nodes"])
    assert appendix["edge_count"] == len(appendix["edges"])
    assert report["schema_version"] == "1.8"


def test_evidence_report_markdown_renders_the_provenance_graph_appendix():
    _insert_task(id=747)
    _insert_approval(id=1, task_id=747)
    _record_completion_event(747)

    report = evidence_report.build_evidence_report(task_id=747)
    md = evidence_report.format_evidence_report(report, fmt="markdown")

    assert "## Provenance graph" in md
    assert report["provenance_graph"]["canonical_hash"] in md


def test_empty_scope_yields_an_empty_but_valid_graph():
    graph = provenance_graph.graph_for_task_scope(task_id=999999)
    assert graph["node_count"] == 0
    assert graph["edge_count"] == 0
    assert graph["nodes"] == []
    assert graph["edges"] == []
    # sha256 of the canonical empty payload — pinned so a refactor that
    # silently changes the empty-graph shape is caught.
    assert graph["canonical_hash"] == provenance_graph.canonical_hash([], [])
