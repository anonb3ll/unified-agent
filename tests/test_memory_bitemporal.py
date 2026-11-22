"""Bi-temporal memory validity (task #2095, epic #1915 M4/M12).

A superseded note is invalidated, never deleted: superseding stamps its
``valid_to``. ``memory_search`` and ``memory_get`` take ``as_of`` and answer
for that instant, including notes that were superseded later. Without
``as_of`` both behave exactly as before.
"""

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import consolidate, embeddings, storage
from unified_agent.tools import memory as memory_tools

PAST = "2026-01-01T00:00:00+00:00"
BEFORE = "2026-06-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch):
    def _offline(_text: str):
        raise RuntimeError("lexical-only test")

    monkeypatch.setattr(embeddings, "generate_embedding", _offline)


def _save(title: str, content: str, **kwargs) -> str:
    kwargs.setdefault("author", "Desktop_Agent2")
    return memory_tools.memory_save(content=content, title=title, **kwargs)["id"]


def _backdate(note_id: str, stamp: str = PAST) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE memory SET created_at=?, updated_at=? WHERE id=?", (stamp, stamp, note_id)
        )
        conn.commit()
    finally:
        conn.close()


def _raw(note_id: str) -> dict:
    conn = storage.connect()
    try:
        return dict(conn.execute("SELECT * FROM memory WHERE id=?", (note_id,)).fetchone())
    finally:
        conn.close()


def _ids(query: str, **kwargs) -> list[str]:
    return [hit["id"] for hit in memory_tools.memory_search(query, **kwargs)["results"]]


def _later(minutes: int = 5) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _supersede_pair() -> tuple[str, str]:
    old = _save("Quasarport format", "Quasarport format is CSV.")
    _backdate(old)
    new = _save("Quasarport format", "Quasarport format is JSON Lines.", supersedes=old)
    return old, new


# --- supersede stamps valid_to ------------------------------------------------


def test_save_supersede_stamps_valid_to_now_and_never_deletes():
    old, new = _supersede_pair()
    raw = _raw(old)
    assert raw["superseded_by"] == new
    assert raw["valid_to"] == _raw(new)["created_at"]
    assert raw["content"] == "Quasarport format is CSV."
    assert memory_tools.memory_get(old)["valid_to"] == raw["valid_to"]
    assert _raw(new)["valid_to"] is None


def test_save_supersede_uses_superseding_valid_from():
    old = _save("Nebulink port", "Nebulink port is 8080.")
    _backdate(old)
    _save(
        "Nebulink port",
        "Nebulink port is 9090.",
        valid_from="2026-03-01T00:00:00+00:00",
        supersedes=old,
    )
    assert _raw(old)["valid_to"] == "2026-03-01T00:00:00+00:00"


def test_superseding_valid_from_before_old_start_falls_back_to_now():
    # The old note only became valid now; an earlier valid_from on its
    # replacement must not invert the old note's interval.
    old = _save("Pulsarite cap", "Pulsarite cap is 5.", epistemic="fact")
    new = _save("Pulsarite cap", "Pulsarite cap is 7.", valid_from="2026-03-01", supersedes=old)
    raw = _raw(old)
    end = datetime.fromisoformat(raw["valid_to"])
    assert end > datetime.fromisoformat(raw["valid_from"])
    assert end >= datetime.fromisoformat(_raw(new)["created_at"])


def test_apply_supersede_stamps_valid_to_and_records_actor():
    keep = _save("Keeper note", "Gravitonic keeper body.")
    drop = _save("Dropped note", "Gravitonic dropped body.")
    _backdate(drop)
    res = consolidate.apply_supersede(keep, [drop], actor="Desktop_Steward")
    raw = _raw(drop)
    assert raw["superseded_by"] == keep
    assert raw["valid_to"] == res["ts"]
    assert res["actor"] == "Desktop_Steward"


def test_restore_clears_valid_to():
    old, _new = _supersede_pair()
    assert memory_tools.memory_restore(old)["restored"] is True
    assert _raw(old)["valid_to"] is None
    assert old in _ids("Quasarport", as_of=_later())

    keep = _save("Keeper two", "Magnetar keeper body.")
    drop = _save("Dropped two", "Magnetar dropped body.")
    consolidate.apply_supersede(keep, [drop])
    consolidate.restore_note(drop)
    assert _raw(drop)["valid_to"] is None


# --- M12 guards -----------------------------------------------------------------


def test_double_supersede_is_refused_and_keeps_valid_to():
    old, new = _supersede_pair()
    stamped = _raw(old)["valid_to"]
    with pytest.raises(ToolError, match="already superseded"):
        _save("Quasarport format", "Quasarport is Parquet.", supersedes=old)
    other = _save("Other survivor", "Unrelated survivor body.")
    with pytest.raises(ToolError, match="already superseded"):
        consolidate.apply_supersede(other, [old])
    raw = _raw(old)
    assert raw["superseded_by"] == new
    assert raw["valid_to"] == stamped


def test_cross_org_supersede_refused_and_as_of_excludes_other_tenant(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    foreign = _save("Tachyonid plan", "Tachyonid plan for tenant a.")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    mine = _save("Tachyonid plan", "Tachyonid plan for tenant b.")

    with pytest.raises(ToolError, match="does not exist"):
        consolidate.apply_supersede(mine, [foreign])
    with pytest.raises(ToolError, match="no memory note"):
        _save("Tachyonid plan", "Tachyonid v2.", supersedes=foreign)
    assert _raw(foreign)["valid_to"] is None
    assert _raw(foreign)["superseded_by"] is None

    assert _ids("Tachyonid", as_of=_later()) == [mine]
    with pytest.raises(ToolError, match="no memory note"):
        memory_tools.memory_get(foreign, as_of=_later())


# --- as_of reads ------------------------------------------------------------------


def test_as_of_before_and_after_a_supersede():
    old, new = _supersede_pair()
    # Default: today's behaviour, the superseded note is hidden.
    assert _ids("Quasarport") == [new]
    # Before the supersede the old note was the valid one; the new one did not exist.
    assert _ids("Quasarport", as_of=BEFORE) == [old]
    # After it, only the replacement is valid.
    assert _ids("Quasarport", as_of=_later()) == [new]
    # Before either note existed nothing is returned.
    assert _ids("Quasarport", as_of="2025-01-01T00:00:00+00:00") == []

    got = memory_tools.memory_get(old, as_of=BEFORE)
    assert got["id"] == old
    assert got["superseded_by"] == new
    with pytest.raises(ToolError, match="not valid at"):
        memory_tools.memory_get(old, as_of=_later())
    with pytest.raises(ToolError, match="not valid at"):
        memory_tools.memory_get(new, as_of=BEFORE)
    # Without as_of memory_get still returns a superseded note.
    assert memory_tools.memory_get(old)["superseded_by"] == new


def test_as_of_accepts_any_utc_offset():
    old, _new = _supersede_pair()
    assert _ids("Quasarport", as_of="2026-05-31T19:00:00-05:00") == [old]


def test_valid_from_after_as_of_is_excluded():
    note = _save("Fermionic rollout", "Fermionic rollout starts.", valid_from=_later(60 * 24))
    assert _ids("Fermionic", as_of=_later()) == []
    assert _ids("Fermionic") == [note]


def test_legacy_superseded_note_without_valid_to_stays_hidden_under_as_of():
    old, new = _supersede_pair()
    conn = storage.connect()
    try:
        conn.execute("UPDATE memory SET valid_to=NULL WHERE id=?", (old,))
        conn.commit()
    finally:
        conn.close()
    assert old not in _ids("Quasarport", as_of=BEFORE)
    assert _ids("Quasarport", as_of=_later()) == [new]


@pytest.mark.parametrize("bad", ["2026-06-01T00:00:00", "2026-06-01", "yesterday"])
def test_naive_or_malformed_as_of_is_refused(bad):
    note = _save("Bosonic note", "Bosonic body.")
    with pytest.raises(ToolError, match="UTC offset"):
        memory_tools.memory_search("Bosonic", as_of=bad)
    with pytest.raises(ToolError, match="UTC offset"):
        memory_tools.memory_get(note, as_of=bad)


def test_validity_survives_reopening_the_database():
    old, new = _supersede_pair()
    stamped = _raw(old)["valid_to"]
    storage.init_db()  # a hub restart re-runs init and migrations
    assert _raw(old)["valid_to"] == stamped
    assert _ids("Quasarport", as_of=BEFORE) == [old]
    assert _ids("Quasarport", as_of=_later()) == [new]


def test_migration_adds_valid_to_to_an_older_database():
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute("ALTER TABLE memory DROP COLUMN valid_to")
        conn.commit()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory)")}
        assert "valid_to" not in cols
    finally:
        conn.close()
    storage.init_db()
    conn = storage.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory)")}
    finally:
        conn.close()
    assert "valid_to" in cols


# --- recency leg ------------------------------------------------------------------


def test_recency_leg_uses_valid_from_when_set():
    fresh = _save("Hadronic note fresh", "Hadronic status is green.")
    aged = _save(
        "Hadronic note aged",
        "Hadronic status.",
        valid_from=(datetime.now(UTC) - timedelta(days=200)).isoformat(),
    )
    hits = {h["id"]: h for h in memory_tools.memory_search("Hadronic")["results"]}
    assert hits[fresh]["recency"] > 0.9
    assert hits[aged]["recency"] < 0.01
    assert (
        hits[fresh]["explanation"]["ranks"]["recency"]
        < hits[aged]["explanation"]["ranks"]["recency"]
    )


def test_recency_is_measured_from_as_of():
    note = _save(
        "Leptonic note",
        "Leptonic status.",
        valid_from=(datetime.now(UTC) - timedelta(days=200)).isoformat(),
    )
    as_of = (datetime.now(UTC) - timedelta(days=199)).isoformat()
    _backdate(note, (datetime.now(UTC) - timedelta(days=200)).isoformat())
    hit = memory_tools.memory_search("Leptonic", as_of=as_of)["results"][0]
    assert hit["id"] == note
    assert hit["recency"] > 0.9


# --- HTTP pass-through ------------------------------------------------------------


async def test_control_center_memory_search_passes_as_of_through(monkeypatch):
    from unified_agent import server
    from unified_agent.webui import api

    seen: dict = {}

    def fake_search(**kwargs):
        seen.update(kwargs)
        return {"results": []}

    monkeypatch.setitem(server.REGISTERED_TOOLS, "memory_search", fake_search)
    await api.memory_payload(query="hub", as_of=BEFORE)
    assert seen["as_of"] == BEFORE
    seen.clear()
    await api.memory_payload(query="hub")
    assert "as_of" not in seen


# --- review round 1 (Codex, PR #1227) ----------------------------------------------


def test_historical_semantic_search_returns_a_superseded_note(monkeypatch):
    # Every text embeds to the same vector, so only the semantic leg can match.
    monkeypatch.setattr(embeddings, "generate_embedding", lambda _text: [0.1] * 384)
    old = _save("Car note", "The automobile runs on petrol.")
    _backdate(old)
    new = _save("Car note", "The automobile runs on electricity.", supersedes=old)

    assert old in _ids("vehicle fuel", as_of=BEFORE)
    assert new not in _ids("vehicle fuel", as_of=BEFORE)
    assert old not in _ids("vehicle fuel")
    assert memory_tools.memory_get(old, as_of=BEFORE)["id"] == old


def test_supersede_refuses_to_invert_a_future_validity_window():
    future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    old = _save("Bariontic launch", "Bariontic launch is on.", valid_from=future)
    with pytest.raises(ToolError, match="no valid instant"):
        _save("Bariontic launch", "Bariontic launch is off.", supersedes=old)
    raw = _raw(old)
    assert raw["superseded_by"] is None and raw["valid_to"] is None
    assert _ids("Bariontic") == [old]  # the replacement was rolled back with it

    keep = _save("Bariontic keeper", "Bariontic keeper body.")
    with pytest.raises(ToolError, match="no valid instant"):
        consolidate.apply_supersede(keep, [old])
    raw = _raw(old)
    assert raw["superseded_by"] is None and raw["valid_to"] is None


def test_same_second_supersede_leaves_a_nonempty_window(monkeypatch):
    stamp = "2026-06-01T12:00:00+00:00"
    monkeypatch.setattr(storage, "now", lambda: stamp)
    old = _save("Gluonic cap", "Gluonic cap is 5.")
    new = _save("Gluonic cap", "Gluonic cap is 6.", supersedes=old)
    keep = _save("Gluonic keep", "Gluonic keep body.")
    drop = _save("Gluonic drop", "Gluonic drop body.")
    consolidate.apply_supersede(keep, [drop])
    for note in (old, drop):
        raw = _raw(note)
        assert datetime.fromisoformat(raw["valid_to"]) > datetime.fromisoformat(raw["created_at"])
    assert memory_tools.memory_get(old, as_of=stamp)["id"] == old
    assert old in _ids("Gluonic", as_of=stamp)
    assert new in _ids("Gluonic", as_of="2026-06-01T12:00:01+00:00")


def test_fractional_second_boundary_matches_between_get_and_search():
    old = _save("Muonic limit", "Muonic limit is 3.")
    _backdate(old)
    _save(
        "Muonic limit",
        "Muonic limit is 4.",
        valid_from="2026-06-01T00:00:00.123400+00:00",
        supersedes=old,
    )
    inside = "2026-06-01T00:00:00.123100+00:00"
    at_end = "2026-06-01T00:00:00.123400+00:00"
    assert memory_tools.memory_get(old, as_of=inside)["id"] == old
    assert _ids("Muonic", as_of=inside) == [old]
    with pytest.raises(ToolError, match="not valid at"):
        memory_tools.memory_get(old, as_of=at_end)
    assert old not in _ids("Muonic", as_of=at_end)
    # The same instant written with another offset is the same boundary.
    assert _ids("Muonic", as_of="2026-05-31T19:00:00.123100-05:00") == [old]


def test_save_supersede_rolls_back_when_its_actor_cannot_be_recorded():
    old = _save("Kaonic flag", "Kaonic flag is red.")
    conn = storage.connect()
    try:
        conn.execute(
            "CREATE TRIGGER block_supersede_audit BEFORE INSERT ON agent_events "
            "WHEN NEW.summary LIKE 'supersede:%' "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(Exception, match="audit unavailable"):
        _save("Kaonic flag", "Kaonic flag is blue.", supersedes=old)
    raw = _raw(old)
    assert raw["superseded_by"] is None and raw["valid_to"] is None
    assert _ids("Kaonic") == [old]


def test_save_supersede_records_its_actor():
    old = _save("Pionic flag", "Pionic flag is red.")
    _save("Pionic flag", "Pionic flag is blue.", supersedes=old, author="Desktop_Agent2")
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT agent FROM agent_events WHERE ref_id=? AND summary LIKE 'supersede:%'",
            (old,),
        ).fetchall()
    finally:
        conn.close()
    assert [r["agent"] for r in rows] == ["Desktop_Agent2"]


@pytest.mark.parametrize(
    ("parser", "value"),
    [
        ("_parse_expires_at", "9999-12-31T23:00:00-05:00"),
        ("_parse_valid_from", "9999-12-31T23:00:00-05:00"),
        ("_parse_as_of", "0001-01-01T00:00:00+05:00"),
    ],
)
def test_out_of_range_timestamp_is_a_tool_error_not_overflow(parser, value):
    """Converting an edge-of-calendar time to UTC overflows; refuse it cleanly."""
    with pytest.raises(ToolError):
        getattr(memory_tools, parser)(value)
