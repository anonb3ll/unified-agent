"""Tenant (org_id) fence on every memory read and maintenance path (task #1762).

PR #951 fenced update/supersedes/contradicts/learning/export. These tests pin
the rest: a note in another tenant must read exactly like a missing id (no
existence oracle), search must not leak its ids, snippets or scores, and the
auto-maintained index notes are built per tenant.
"""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, embeddings, storage
from unified_agent.platform_readers import ResourceNotFoundError, read_memory_note
from unified_agent.recall_curation import memory_promote, recall_maintenance
from unified_agent.task_comments import append_comment
from unified_agent.tools import memory as memory_tools
from unified_agent.tools import tasks as task_tools
from unified_agent.tools.memory_prune import memory_prune_candidates

ORG_A = "org_a"
ORG_B = "org_b"


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _save(title: str, content: str, **kwargs) -> str:
    kwargs.setdefault("author", "Desktop_Agent2")
    return memory_tools.memory_save(content=content, title=title, **kwargs)["id"]


def _missing_message(fn, note_id: str) -> str:
    with pytest.raises(ToolError) as exc:
        fn(note_id)
    return str(exc.value)


def _raw(note_id: str) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM memory WHERE id=?", (note_id,)).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def _as_human():
    return config.agent_identity_var.set(config.operator_label())


# --- by-id reads: another tenant's note == a nonexistent id ------------------


@pytest.mark.parametrize(
    "tool",
    [
        memory_tools.memory_get,
        memory_tools.memory_restore,
        lambda note_id: memory_tools.memory_feedback(note_id, "wrong"),
        lambda note_id: memory_promote(note_id, dry_run=True),
    ],
    ids=["memory_get", "memory_restore", "memory_feedback", "memory_promote"],
)
def test_by_id_tools_treat_another_tenant_like_a_missing_id(as_org, tool):
    as_org(ORG_A)
    theirs = _save("Zirconfence byid", "Zirconfence org a only.", confidence=0.9)
    before = _raw(theirs)
    as_org(ORG_B)
    foreign = _missing_message(tool, theirs)
    absent = _missing_message(tool, "0123456789ab")
    assert foreign == absent.replace("0123456789ab", theirs)
    assert "no memory note" in foreign
    # Nothing on the foreign row moved: no access bump, no feedback count.
    after = _raw(theirs)
    for column in ("access_count", "last_accessed", "feedback_wrong", "superseded_by"):
        assert after[column] == before[column]


def test_restore_cannot_repromote_another_tenants_superseded_note(as_org):
    as_org(ORG_A)
    old = _save("Zirconfence restore v1", "Zirconfence restore old.")
    new = memory_tools.memory_save(
        content="Zirconfence restore new.",
        title="Zirconfence restore v2",
        author="Desktop_Agent2",
        supersedes=old,
    )["id"]
    as_org(ORG_B)
    with pytest.raises(ToolError, match="no memory note"):
        memory_tools.memory_restore(old)
    assert _raw(old)["superseded_by"] == new


def test_get_lists_only_same_tenant_supersedes(as_org):
    as_org(ORG_A)
    old = _save("Zirconfence chain v1", "Zirconfence chain old.")
    new = memory_tools.memory_save(
        content="Zirconfence chain new.",
        title="Zirconfence chain v2",
        author="Desktop_Agent2",
        supersedes=old,
    )["id"]
    assert memory_tools.memory_get(new)["supersedes"] == [old]
    # A stray cross-tenant pointer (written outside the tools) is not reported.
    as_org(ORG_B)
    mine = _save("Zirconfence chain b", "Zirconfence chain b.")
    conn = storage.connect()
    try:
        conn.execute("UPDATE memory SET superseded_by=? WHERE id=?", (mine, old))
        conn.commit()
    finally:
        conn.close()
    assert "supersedes" not in memory_tools.memory_get(mine)


def test_delete_of_another_tenants_note_reads_as_missing(as_org):
    as_org(ORG_A)
    theirs = _save("Zirconfence delete", "Zirconfence delete org a.")
    as_org(ORG_B)
    token = _as_human()
    try:
        assert memory_tools.memory_delete(theirs) == {"deleted": False}
        assert memory_tools.memory_delete("0123456789ab") == {"deleted": False}
    finally:
        config.agent_identity_var.reset(token)
    row = _raw(theirs)
    assert row["content"] == "Zirconfence delete org a."
    assert memory_tools.DELETED_TAG not in json.loads(row["tags"])


def test_save_by_id_refuses_another_tenants_index_note(as_org):
    as_org(ORG_A)
    _save("Zirconfence idx", "Zirconfence idx a.", tags=["project:zirconfence"])
    index_id = memory_tools._tag_index_id("project:zirconfence", ORG_A)
    assert memory_tools.memory_get(index_id)
    as_org(ORG_B)
    with pytest.raises(ToolError, match="no memory note"):
        memory_tools.memory_save(id=index_id, content="clobbered", overwrite=True)
    with pytest.raises(ToolError, match="no memory note"):
        memory_tools.memory_get(memory_tools._master_index_id(ORG_A))


def test_resource_reader_hides_another_tenants_note(as_org):
    as_org(ORG_A)
    theirs = _save("Zirconfence resource", "Zirconfence resource a.")
    assert read_memory_note(theirs)["memory_note"]["id"] == theirs
    as_org(ORG_B)
    with pytest.raises(ResourceNotFoundError, match="unknown memory note"):
        read_memory_note(theirs)


def test_task_comment_decision_ref_does_not_confirm_another_tenants_note(as_org):
    as_org(ORG_A)
    theirs = _save("Zirconfence decision", "Zirconfence decision a.", tags=["decision"])
    as_org(ORG_B)
    task_id = task_tools.task_add(title="Zirconfence comment task")["id"]
    with pytest.raises(ToolError, match="unknown decision"):
        append_comment(
            task_id, "see decision", refs={"decision_id": theirs}, author="Desktop_Agent2"
        )


# --- search: no ids, snippets or scores from another tenant ------------------


def test_search_fts_and_identifier_paths_are_fenced(as_org):
    as_org(ORG_A)
    theirs = _save("Zirconfence search", "Zirconfence secret detail for task #7712.")
    assert [h["id"] for h in memory_tools.memory_search("Zirconfence")["results"]] == [theirs]
    as_org(ORG_B)
    mine = _save("Zirconfence search b", "Zirconfence org b note.")
    for query in ("Zirconfence", "secret detail", "task #7712"):
        results = memory_tools.memory_search(query)["results"]
        assert theirs not in {h["id"] for h in results}
        assert "secret" not in json.dumps(results)
    assert [h["id"] for h in memory_tools.memory_search("Zirconfence")["results"]] == [mine]
    # Index notes in search are this tenant's only.
    idx = memory_tools.memory_search("Memory Index", tags=["meta:index"])["results"]
    assert idx and all(_raw(h["id"])["org_id"] == ORG_B for h in idx)


@pytest.mark.parametrize("dims", [4, 384], ids=["udf-scan", "vec-mirror-width"])
def test_semantic_search_and_dedup_are_fenced(as_org, monkeypatch, dims):
    vector = [1.0] + [0.0] * (dims - 1)
    monkeypatch.setattr(embeddings, "generate_embedding", lambda _text: vector)
    as_org(ORG_A)
    theirs = _save("Quartz alpha", "Quartz semantic content for org a.")
    as_org(ORG_B)
    # Dedup suggestions (check_only and a real create) never name the other
    # tenant's note, though it is an exact vector match.
    check = memory_tools.memory_save(content="Totally different words", check_only=True)
    assert theirs not in {s["id"] for s in check["similar"]}
    created = memory_tools.memory_save(content="Totally different words b", title="Quartz b")
    assert theirs not in {s["id"] for s in created["similar"]}
    conn = storage.connect()
    try:
        rows = memory_tools._semantic_similarity_rows(conn, embeddings.embedding_to_bytes(vector))
    finally:
        conn.close()
    ids = {r["note_id"] for r in rows}
    assert theirs not in ids and created["id"] in ids
    hits = memory_tools.memory_search("some natural language question here")["results"]
    assert theirs not in {h["id"] for h in hits}
    assert created["id"] in {h["id"] for h in hits}


# --- listings and maintenance reports --------------------------------------


def test_list_is_fenced_with_and_without_a_tag(as_org):
    as_org(ORG_A)
    theirs = _save("Zirconfence list a", "Zirconfence list a.", tags=["project:shared"])
    as_org(ORG_B)
    mine = _save("Zirconfence list b", "Zirconfence list b.", tags=["project:shared"])
    for kwargs in ({}, {"tag": "project:shared"}, {"include_expired": True}):
        ids = {i["id"] for i in memory_tools.memory_list(**kwargs)["items"]}
        assert mine in ids and theirs not in ids
    index_ids = {i["id"] for i in memory_tools.memory_list(tag="meta:index")["items"]}
    assert index_ids == {
        memory_tools._master_index_id(ORG_B),
        memory_tools._tag_index_id("project:shared", ORG_B),
    }
    tag_index_ids = {i["id"] for i in memory_tools.memory_list(tag="meta:index:tag")["items"]}
    assert tag_index_ids == {memory_tools._tag_index_id("project:shared", ORG_B)}


def test_prune_and_recall_maintenance_reports_are_fenced(as_org):
    as_org(ORG_A)
    theirs = _save("Zirconfence prune a", "Zirconfence prune a.", salience=0.1, confidence=0.1)
    as_org(ORG_B)
    mine = _save("Zirconfence prune b", "Zirconfence prune b.", salience=0.1, confidence=0.1)
    prune = memory_prune_candidates()
    assert {c["id"] for c in prune["candidates"]} == {mine}
    assert prune["candidate_count"] == 1
    report = recall_maintenance()
    assert {c["id"] for c in report["low_confidence"]} == {mine}
    assert theirs not in json.dumps(report)


# --- index notes are built per tenant ----------------------------------------


def test_index_notes_are_built_per_tenant(as_org):
    as_org(ORG_A)
    _save("Alpha private title", "Alpha body.", tags=["project:shared", "project:a-only"])
    as_org(ORG_B)
    _save("Bravo title", "Bravo body.", tags=["project:shared"])

    master_b = memory_tools.memory_get(memory_tools._master_index_id(ORG_B))["content"]
    assert "Bravo title" in master_b
    assert "Alpha private title" not in master_b
    assert "project:a-only" not in master_b
    assert "(1 note)" in master_b
    shared_b = memory_tools.memory_get(memory_tools._tag_index_id("project:shared", ORG_B))
    assert "Bravo title" in shared_b["content"]
    assert "Alpha private title" not in shared_b["content"]
    assert _raw(shared_b["id"])["org_id"] == ORG_B

    # Org b's refresh neither rewrote nor dropped org a's index notes.
    as_org(ORG_A)
    shared_a = memory_tools.memory_get(memory_tools._tag_index_id("project:shared", ORG_A))
    assert "Alpha private title" in shared_a["content"]
    assert "Bravo title" not in shared_a["content"]
    a_only = memory_tools.memory_get(memory_tools._tag_index_id("project:a-only", ORG_A))
    assert "Alpha private title" in a_only["content"]
    master_a = memory_tools.memory_get(memory_tools._master_index_id(ORG_A))["content"]
    assert "Bravo title" not in master_a


def test_untenanted_index_ids_are_unchanged():
    assert memory_tools._master_index_id(None) == memory_tools.MASTER_INDEX_ID
    assert memory_tools._tag_index_id("project:acme") == memory_tools._tag_index_id(
        "project:acme", None
    )
    assert "-" not in memory_tools._tag_index_id("project:acme")
    tenant_ids = {
        memory_tools._master_index_id(ORG_A),
        memory_tools._tag_index_id("project:acme", ORG_A),
    }
    assert all(memory_tools._is_index_id(i) for i in tenant_ids)
    assert memory_tools._tag_index_id("project:acme", ORG_A) != memory_tools._tag_index_id(
        "project:acme", ORG_B
    )


def test_batch_maintenance_candidates_are_fenced(as_org):
    from unified_agent import consolidate, evolve, sleep_pass

    as_org(ORG_A)
    kwargs = {"salience": 0.05, "confidence": 0.95, "tags": ["fact"]}
    theirs = _save("Zirconfence batch a", "Zirconfence batch a is a durable fact.", **kwargs)
    as_org(ORG_B)
    mine = _save("Zirconfence batch b", "Zirconfence batch b is a durable fact.", **kwargs)
    conn = storage.connect()
    try:
        merge_ids = {c["id"] for c in consolidate._load_candidates(conn)}
        evolve_ids = {n["id"] for n in evolve._select_notes(conn, 0.9)}
        prune_ids = {
            c["id"]
            for c in sleep_pass._find_pruning_candidates(conn, salience_max=1.0, min_age_days=0)
        }
    finally:
        conn.close()
    for ids in (merge_ids, evolve_ids, prune_ids):
        assert mine in ids and theirs not in ids


def test_sleep_pass_preflight_counts_only_the_callers_tenant(as_org, monkeypatch):
    """Preflight admission must measure the same tenant-fenced candidate set
    consolidate loads; another tenant's volume must not defer this pass."""
    from unified_agent import sleep_pass

    monkeypatch.setenv("UA_SLEEP_PASS_MAX_CANDIDATES", "2")
    monkeypatch.setenv("UA_SLEEP_PASS_MAX_RECENT_CHANGES", "2")
    as_org(ORG_A)
    for i in range(3):
        _save(f"Zirconfence preflight a{i}", f"Zirconfence preflight a{i} durable fact.")
    as_org(ORG_B)
    mine = _save("Zirconfence preflight b", "Zirconfence preflight b durable fact.")
    conn = storage.connect()
    try:
        snapshot = sleep_pass._preflight(conn)
        loaded = {c["id"] for c in sleep_pass.consolidate._load_candidates(conn)}
    finally:
        conn.close()
    assert loaded == {mine}
    assert snapshot["active_candidate_count"] == len(loaded)
    assert snapshot["recent_created_count"] == len(loaded)
    assert "candidate_limit" not in snapshot["reasons"]
    assert "recent_change_limit" not in snapshot["reasons"]
