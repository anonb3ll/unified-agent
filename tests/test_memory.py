"""Tests for memory tool optimizations (Task #161)."""

import json
import secrets

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, session_credentials, storage
from unified_agent.tools.memory import (
    MASTER_INDEX_ID,
    _refresh_indexes,
    _tag_index_id,
    memory_save,
    memory_search,
)


def test_refresh_indexes_tag_filtering(isolated_env):
    """Verify _refresh_indexes queries matching notes for affected tags."""
    memory_save("Content for project A", title="Project A Note", tags=["project_a"])
    memory_save("Content for project B", title="Project B Note", tags=["project_b"])

    conn = storage.connect()
    try:
        master = conn.execute(
            "SELECT content FROM memory WHERE id=?", (MASTER_INDEX_ID,)
        ).fetchone()
        assert master is not None
        assert "project_a" in master["content"]
        assert "project_b" in master["content"]

        tag_a_idx = conn.execute(
            "SELECT content FROM memory WHERE id=?", (_tag_index_id("project_a"),)
        ).fetchone()
        assert tag_a_idx is not None
        assert "Project A Note" in tag_a_idx["content"]

        _refresh_indexes(conn, {"project_a"})
        conn.commit()

        tag_a_idx = conn.execute(
            "SELECT content FROM memory WHERE id=?", (_tag_index_id("project_a"),)
        ).fetchone()
        assert tag_a_idx is not None
        assert "Project A Note" in tag_a_idx["content"]
    finally:
        conn.close()


def test_memory_search_sql_candidate_filtering(isolated_env):
    """Verify memory_search filters superseded notes and tag filters via SQL."""
    res1 = memory_save("Active note content about database", title="DB Active", tags=["database"])
    res2 = memory_save("Older note content about database", title="DB Old", tags=["database"])

    conn = storage.connect()
    try:
        conn.execute("UPDATE memory SET superseded_by=? WHERE id=?", (res1["id"], res2["id"]))
        conn.commit()
    finally:
        conn.close()

    search_res = memory_search("database", tags=["database"])
    result_ids = [r["id"] for r in search_res["results"]]
    assert res1["id"] in result_ids
    assert res2["id"] not in result_ids


def test_memory_search_index_exclusion(isolated_env):
    """Verify auto-maintained index notes are excluded unless meta:index is requested."""
    memory_save("Alpha note content", title="Alpha Note", tags=["alpha"])

    search_res = memory_search("Alpha Note")
    result_ids = [r["id"] for r in search_res["results"]]
    assert MASTER_INDEX_ID not in result_ids
    assert _tag_index_id("alpha") not in result_ids

    search_index_res = memory_search("Memory Index", tags=["meta:index"])
    idx_ids = [r["id"] for r in search_index_res["results"]]
    assert MASTER_INDEX_ID in idx_ids or any(i.startswith("tagindex_") for i in idx_ids)


def test_tag_filters_survive_json_escaping(isolated_env):
    """A tag containing a quote or backslash must still match.

    Regression for the task #161/#162 SQL pre-filter: the candidate query built
    its LIKE pattern as '%"' + tag + '"%', but memory_save stores tags as JSON,
    so a tag like say"hi is persisted escaped (["say\\"hi"]) and the raw pattern
    never matched. The note vanished from search and from its own tag index —
    silently, because the Python-side filter never saw the row.
    """
    quoted = 'say"hi'
    slashed = "back\\slash"
    res_q = memory_save("Quoted tag note about sqlite", title="Quoted Tag", tags=[quoted])
    res_s = memory_save("Slashed tag note about sqlite", title="Slashed Tag", tags=[slashed])

    hits = memory_search("sqlite", tags=[quoted])
    assert [r["id"] for r in hits["results"]] == [res_q["id"]]

    hits = memory_search("sqlite", tags=[slashed])
    assert [r["id"] for r in hits["results"]] == [res_s["id"]]

    conn = storage.connect()
    try:
        idx = conn.execute(
            "SELECT content FROM memory WHERE id=?", (_tag_index_id(quoted),)
        ).fetchone()
        assert idx is not None, "tag index note was not written for the quoted tag"
        assert "Quoted Tag" in idx["content"]
    finally:
        conn.close()


# --- a credential-shaped author is refused before identity checks (task #1893) ------

ORDINARY_AUTHOR = "Local_Writer"


def _inert_credential() -> str:
    """Shaped like a session credential, built at runtime, never minted."""
    return session_credentials.PREFIX + secrets.token_urlsafe(32)


def _fragments_of(value: str, size: int = 10) -> set[str]:
    return {value[i : i + size] for i in range(len(value) - size + 1)}


def _db_dump() -> dict[str, list]:
    conn = storage.connect()
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        out: dict[str, list] = {}
        for name in names:
            try:
                rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
            except Exception:  # noqa: BLE001 - virtual tables without a reader
                continue
            out[name] = [json.dumps(tuple(r), default=str) for r in rows]
        return out
    finally:
        conn.close()


@pytest.fixture
def stdio_unverified(isolated_env, monkeypatch):
    """Stdio, no verified identity, caller-identity requirement off."""
    monkeypatch.delenv("UA_TRANSPORT", raising=False)
    monkeypatch.delenv("UA_REQUIRE_CALLER_IDENTITY", raising=False)
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    assert config.agent_identity_var.get() is None
    storage.init_db()


def _refused_without_writing(call, value: str) -> None:
    before = _db_dump()
    with pytest.raises(ToolError) as exc:
        call()
    message = str(exc.value)
    assert "author" in message and "session credential" in message
    assert not any(f in message for f in _fragments_of(value)), message
    assert _db_dump() == before, "a refused save must write nothing"


class TestCredentialShapedAuthor:
    def test_create_is_refused(self, stdio_unverified):
        value = _inert_credential()
        _refused_without_writing(lambda: memory_save("a note", author=value), value)

    def test_append_and_update_are_refused(self, stdio_unverified):
        value = _inert_credential()
        note_id = memory_save("owned note", author=ORDINARY_AUTHOR)["id"]
        _refused_without_writing(
            lambda: memory_save("more", id=note_id, append=True, author=value), value
        )
        _refused_without_writing(lambda: memory_save("replaced", id=note_id, author=value), value)
        _refused_without_writing(
            lambda: memory_save("replaced", id=note_id, author=value, overwrite=True), value
        )

    def test_check_only_is_refused(self, stdio_unverified):
        value = _inert_credential()
        _refused_without_writing(lambda: memory_save("probe", check_only=True, author=value), value)

    def test_promote_refuses_a_credential_shaped_agent(self, stdio_unverified):
        from unified_agent.recall_curation import memory_promote

        value = _inert_credential()
        note_id = memory_save("a strong note", author=ORDINARY_AUTHOR, confidence=0.9)["id"]
        before = _db_dump()
        with pytest.raises(ToolError) as exc:
            memory_promote(note_id, agent=value, dry_run=True)
        assert "agent" in str(exc.value)
        assert not any(f in str(exc.value) for f in _fragments_of(value))
        assert _db_dump() == before

    # Controls: nothing else about authorship changes.

    def test_an_ordinary_author_still_works_on_stdio(self, stdio_unverified):
        note_id = memory_save("plain", author=ORDINARY_AUTHOR)["id"]
        assert _author_of(note_id) == ORDINARY_AUTHOR

    def test_a_verified_identity_is_unchanged(self, stdio_unverified, monkeypatch):
        token = config.agent_identity_var.set("Local_Verified")
        try:
            note_id = memory_save("verified")["id"]
            assert _author_of(note_id) == "Local_Verified"
            monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
            with pytest.raises(ToolError, match="identity mismatch"):
                memory_save("spoof", author=ORDINARY_AUTHOR)
        finally:
            config.agent_identity_var.reset(token)

    def test_http_saves_are_unchanged(self, stdio_unverified, monkeypatch):
        monkeypatch.setenv("UA_TRANSPORT", "http")
        note_id = memory_save("over http", author=ORDINARY_AUTHOR)["id"]
        assert _author_of(note_id) is None

    def test_required_identity_is_unchanged(self, stdio_unverified, monkeypatch):
        monkeypatch.setenv("UA_REQUIRE_CALLER_IDENTITY", "1")
        note_id = memory_save("required", author=ORDINARY_AUTHOR)["id"]
        assert _author_of(note_id) is None


def _author_of(note_id: str):
    conn = storage.connect()
    try:
        return conn.execute("SELECT author FROM memory WHERE id=?", (note_id,)).fetchone()[0]
    finally:
        conn.close()


def test_author_and_tag_filters_apply_before_the_cap(isolated_env, monkeypatch):
    """Task #2515: a filtered page is not a filtered slice of an unfiltered page."""
    from unified_agent import embeddings
    from unified_agent.tools.memory import memory_list

    def offline(_text: str):
        raise RuntimeError("lexical-only test")

    monkeypatch.setattr(embeddings, "generate_embedding", offline)
    # The newer and stronger matches belong to another author and project.
    for i in range(3):
        memory_save(f"zebra report {i}", title="mine", tags=["project:x", "fact"], author="A_one")
    for i in range(6):
        memory_save(
            f"zebra zebra report {i}", title="zebra", tags=["project:y", "fact"], author="B_two"
        )
    found = memory_search("zebra", limit=2, author="A_one", tags=["project:x", "fact"])
    assert [r["title"] for r in found["results"]] == ["mine", "mine"]
    assert memory_search("zebra", limit=5, author="Nobody")["results"] == []
    listed = memory_list(limit=2, author="A_one", tags=["project:x", "fact"])["items"]
    assert [n["author"] for n in listed] == ["A_one", "A_one"]
    assert memory_list(tag="project:x", tags=["project:y"])["items"] == []
