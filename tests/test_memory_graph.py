"""Memory graph leg (task #2096, epic #1915 M6).

Notes carry persisted entities (``memory_entities``) and typed links
(``memory_links``: supersedes, contradicts, extends, derives, evidence),
written at save and backfilled for older notes. ``memory_search`` adds a
``graph`` leg: one hop from the top lexical/semantic hits over links and
shared entities, fanned out under caps, fused by RRF like every other leg.
Every read and write is fenced to the caller's tenant.
"""

import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import consolidate, embeddings, storage
from unified_agent.tools import memory as memory_tools

PAST = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch):
    def _offline(_text: str):
        raise RuntimeError("lexical-only test")

    monkeypatch.setattr(embeddings, "generate_embedding", _offline)


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _save(title: str, content: str, **kwargs) -> str:
    kwargs.setdefault("author", "Desktop_Agent2")
    return memory_tools.memory_save(content=content, title=title, **kwargs)["id"]


def _rows(sql: str, params: tuple = ()) -> list[tuple]:
    conn = storage.connect()
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _links(note_id: str | None = None) -> set[tuple[str, str, str]]:
    sql = "SELECT src_id, dst_id, rel FROM memory_links"
    params: tuple = ()
    if note_id is not None:
        sql += " WHERE src_id=? OR dst_id=?"
        params = (note_id, note_id)
    return {(r[0], r[1], r[2]) for r in _rows(sql, params)}


def _entities(note_id: str) -> set[tuple[str, str]]:
    return {
        (r[0], r[1])
        for r in _rows("SELECT kind, value FROM memory_entities WHERE note_id=?", (note_id,))
    }


def _search(query: str, **kwargs) -> dict:
    return memory_tools.memory_search(query, **kwargs)


def _ids(query: str, **kwargs) -> list[str]:
    return [hit["id"] for hit in _search(query, **kwargs)["results"]]


def _hit(query: str, note_id: str, **kwargs) -> dict | None:
    for hit in _search(query, **kwargs)["results"]:
        if hit["id"] == note_id:
            return hit
    return None


# --- persisted entities and links at save -------------------------------------


def test_save_persists_entities_from_text_and_tags():
    nid = _save(
        "Quokka rollout",
        "Ships with task #4321 on branch task/4321-quokka; Desktop_Agent2 owns it.",
        tags=["project:unified-agent"],
    )
    found = _entities(nid)
    assert ("task", "4321") in found
    assert ("branch", "task/4321-quokka") in found
    assert ("project", "unified-agent") in found
    assert ("agent", "desktop_agent2") in found


def test_update_rewrites_entities():
    nid = _save("Quokka", "Tracked in task #111.")
    assert ("task", "111") in _entities(nid)
    memory_tools.memory_save(content="Now tracked in task #222.", id=nid, author="Desktop_Agent2")
    found = _entities(nid)
    assert ("task", "222") in found
    assert ("task", "111") not in found


def test_contradicts_evidence_and_explicit_links_are_written_at_save():
    base = _save("Wombat cadence", "Wombat deploys weekly.")
    disputing = _save(
        "Wombat cadence dispute",
        "Wombat deploys daily now.",
        contradicts=[base],
        evidence=[f"memory:{base}", "task:2096"],
    )
    extending = _save("Wombat canary", "Canary waves for the deploy.", links=[f"extends:{base}"])
    deriving = _save("Wombat runbook", "Derived steps.", links=[f"derives:{base}"])
    assert _links(base) == {
        (disputing, base, "contradicts"),
        (disputing, base, "evidence"),
        (extending, base, "extends"),
        (deriving, base, "derives"),
    }
    actor = _rows("SELECT actor FROM memory_links WHERE src_id=?", (extending,))
    assert actor == [("Desktop_Agent2",)]


def test_contradicts_update_replaces_links():
    a = _save("Numbat A", "First claim.")
    b = _save("Numbat B", "Second claim.")
    c = _save("Numbat C", "Disputes.", contradicts=[a])
    memory_tools.memory_save(content="Disputes.", id=c, contradicts=[b], author="Desktop_Agent2")
    assert _links(c) == {(c, b, "contradicts")}
    memory_tools.memory_save(content="Disputes.", id=c, contradicts=[], author="Desktop_Agent2")
    assert _links(c) == set()


def test_explicit_link_validation():
    base = _save("Bilby", "Base.")
    with pytest.raises(ToolError, match="links"):
        _save("Bad rel", "x", links=[f"supersedes:{base}"])
    with pytest.raises(ToolError, match="no memory note"):
        _save("Missing", "x", links=["extends:000000000000"])
    with pytest.raises(ToolError, match="links"):
        _save("Malformed", "x", links=["extends"])


def test_supersede_creates_a_supersedes_link_and_restore_removes_it():
    old = _save("Quasarport format", "Quasarport format is CSV.")
    new = _save("Quasarport format", "Quasarport format is JSON Lines.", supersedes=old)
    assert _links(old) == {(new, old, "supersedes")}
    memory_tools.memory_restore(old)
    assert _links(old) == set()


def test_consolidate_apply_creates_a_supersedes_link():
    keep = _save("Dingo survivor", "Dingo canonical.")
    drop = _save("Dingo dup", "Dingo duplicate.")
    consolidate.apply_supersede(keep, [drop], actor="Desktop_Steward")
    assert _links(drop) == {(keep, drop, "supersedes")}
    consolidate.restore_note(drop)
    assert _links(drop) == set()


def test_memory_get_returns_links_in_and_out():
    base = _save("Echidna", "Base.")
    child = _save("Echidna child", "Child.", links=[f"extends:{base}"])
    got_base = memory_tools.memory_get(base)["links"]
    got_child = memory_tools.memory_get(child)["links"]
    assert [(link["id"], link["rel"]) for link in got_base["in"]] == [(child, "extends")]
    assert got_base["out"] == []
    assert [(link["id"], link["rel"]) for link in got_child["out"]] == [(base, "extends")]
    assert got_child["out"][0]["title"] == "Echidna"


# --- backfill -----------------------------------------------------------------


def test_backfill_is_batched_and_idempotent():
    a = _save("Platypus", "Mentions task #777.")
    b = _save("Platypus dispute", "Disputes it.", contradicts=[a])
    c = _save("Platypus cite", "Cites.", evidence=[f"memory:{a}"])
    d = _save("Platypus old", "Old fact.")
    conn = storage.connect()
    try:
        # Simulate a database from before #2096: the graph tables are empty and a
        # supersede was recorded only in the column.
        conn.execute("UPDATE memory SET superseded_by=? WHERE id=?", (a, d))
        conn.execute("DELETE FROM memory_links")
        conn.execute("DELETE FROM memory_entities")
        conn.execute("DELETE FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,))
        conn.commit()
        memory_tools.backfill_memory_graph(conn, batch_size=2)
        conn.commit()
        links_once = _links()
        entities_once = _rows("SELECT note_id, kind, value FROM memory_entities ORDER BY 1, 2, 3")
        assert links_once == {
            (b, a, "contradicts"),
            (c, a, "evidence"),
            (a, d, "supersedes"),
        }
        assert ("task", "777") in _entities(a)
        # A second run, and a run from a reset cursor, change nothing.
        memory_tools.backfill_memory_graph(conn, batch_size=2)
        conn.execute("DELETE FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,))
        memory_tools.backfill_memory_graph(conn, batch_size=3)
        conn.commit()
    finally:
        conn.close()
    assert _links() == links_once
    assert _rows("SELECT note_id, kind, value FROM memory_entities ORDER BY 1, 2, 3") == (
        entities_once
    )


# --- graph leg ----------------------------------------------------------------


def test_one_hop_recall_of_a_linked_note_the_query_does_not_mention():
    seed = _save("Zephyrine rollout", "Zephyrine ships in canary waves.")
    linked = _save("Kill switch", "Flip the kill switch to roll back.", links=[f"extends:{seed}"])
    hit = _hit("zephyrine", linked)
    assert hit is not None
    assert hit["explanation"]["ranks"]["graph"] == 1
    assert hit["explanation"]["ranks"]["lexical"] is None
    assert "graph" in hit["explanation"]["why"]
    via = hit["explanation"]["graph"]["via"]
    assert {"seed": seed, "rel": "extends"} in via
    # Graph-only recall crosses the same trust boundary as a direct hit (#1952).
    assert hit["trust"]["level"] == "untrusted"
    assert hit["trust"]["source"] == "memory"
    note = memory_tools.memory_get(seed)
    assert note["trust"]["level"] == "untrusted"
    assert note["links"]["in"][0]["id"] == linked


def test_one_hop_over_a_shared_entity():
    seed = _save("Bandicoot migration", "Bandicoot migration tracked in task #4545.")
    sibling = _save("Schema note", "Column rename done for #4545.")
    hit = _hit("bandicoot", sibling)
    assert hit is not None
    assert hit["explanation"]["ranks"]["graph"] is not None
    assert {"seed": seed, "entity": "task:4545"} in hit["explanation"]["graph"]["via"]


def test_graph_leg_expands_and_never_reranks_content_matches():
    seed = _save("Zephyrine rollout", "Zephyrine ships in canary waves.")
    lexical = _save("Zephyrine owners", "Zephyrine is owned by ops.", links=[f"extends:{seed}"])
    res = _search("zephyrine")
    by_id = {h["id"]: h for h in res["results"]}
    assert set(by_id) == {seed, lexical}
    assert by_id[lexical]["explanation"]["ranks"]["graph"] is None
    assert by_id[lexical]["explanation"]["graph"] is None
    assert res["retrieval"]["graph"]["matched_count"] == 0


def test_graph_weight_zero_drops_the_leg(monkeypatch):
    seed = _save("Zephyrine rollout", "Zephyrine ships in canary waves.")
    linked = _save("Kill switch", "Flip the kill switch.", links=[f"extends:{seed}"])
    monkeypatch.setenv("UA_MEMORY_WEIGHT_GRAPH", "0")
    res = _search("zephyrine")
    assert linked not in [hit["id"] for hit in res["results"]]
    assert res["retrieval"]["graph"]["status"] == "disabled"


def test_superseded_hop_target_is_hidden_unless_valid_at_as_of():
    seed = _save("Wallaby setup", "Wallaby uses port 9000.")
    old = _save("Proxy host", "Proxy on the old host.", links=[f"extends:{seed}"])
    conn = storage.connect()
    try:
        for nid in (seed, old):
            conn.execute(
                "UPDATE memory SET created_at=?, updated_at=? WHERE id=?", (PAST, PAST, nid)
            )
        conn.execute("UPDATE memory_links SET created_at=?", (PAST,))
        conn.commit()
    finally:
        conn.close()
    new = _save("Proxy host", "Proxy on the new host.", supersedes=old)
    # Without as_of the superseded hop target is hidden, like everywhere else.
    assert _ids("wallaby") == [seed]
    # At an instant when it was still valid, the hop reaches it; the
    # replacement did not exist yet and is not a neighbour of the seed.
    then = memory_tools.memory_search("wallaby", as_of="2026-03-01T00:00:00+00:00")
    by_id = {h["id"]: h for h in then["results"]}
    assert set(by_id) == {seed, old}
    assert by_id[old]["explanation"]["ranks"]["graph"] == 1
    assert by_id[old]["explanation"]["ranks"]["lexical"] is None
    assert new not in by_id
    # A link recorded after as_of is not walked.
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE memory_links SET created_at='2026-02-15T00:00:00+00:00' WHERE rel='extends'"
        )
        conn.commit()
    finally:
        conn.close()
    assert _ids("wallaby", as_of="2026-02-01T00:00:00+00:00") == [seed]


def test_fanout_cap_bounds_neighbours_per_seed(monkeypatch):
    seed = _save("Cassowary hub", "Cassowary is the hub note.")
    for i in range(12):
        _save(f"Spoke {i}", f"Spoke body {i}.", links=[f"extends:{seed}"])
    monkeypatch.setenv("UA_MEMORY_GRAPH_FANOUT", "4")
    res = _search("cassowary", limit=50)
    graph_hits = [h for h in res["results"] if h["explanation"]["ranks"]["graph"] is not None]
    assert len(graph_hits) == 4
    assert res["retrieval"]["graph"]["matched_count"] == 4


def test_hub_entities_above_the_df_cap_are_not_traversed(monkeypatch):
    _save("Koala seed", "Koala note about task #9999.")
    for i in range(3):
        _save(f"Other {i}", f"Unrelated body {i} also task #9999.")
    monkeypatch.setenv("UA_MEMORY_GRAPH_ENTITY_MAX_DF", "3")
    res = _search("koala")
    assert [h["title"] for h in res["results"]] == ["Koala seed"]


# --- tenant fence --------------------------------------------------------------


def test_cross_org_links_are_refused_at_write(as_org):
    as_org("org_a")
    mine = _save("Org A note", "Alpha.")
    as_org("org_b")
    with pytest.raises(ToolError, match="no memory note"):
        _save("Org B note", "Beta.", links=[f"extends:{mine}"])
    theirs = _save("Org B note", "Beta.")
    conn = storage.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_links (src_id, dst_id, rel, actor, created_at, org_id) "
                "VALUES (?, ?, 'extends', 'x', ?, 'org_b')",
                (theirs, mine, PAST),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_entities (note_id, kind, value, org_id) "
                "VALUES (?, 'task', '1', 'org_b')",
                (mine,),
            )
    finally:
        conn.close()


def test_cross_org_never_traverses_at_read(as_org):
    as_org("org_a")
    seed = _save("Tenant seed", "Ocelot seed mentions task #6060.")
    as_org("org_b")
    foreign_entity = _save("Foreign sibling", "Shares task #6060.")
    foreign_link = _save("Foreign linked", "Linked across tenants.")
    conn = storage.connect()
    try:
        # A link written around the fence (trigger dropped) still never traverses.
        conn.execute("DROP TRIGGER IF EXISTS memory_links_org_fence_insert")
        conn.execute(
            "INSERT INTO memory_links (src_id, dst_id, rel, actor, created_at, org_id) "
            "VALUES (?, ?, 'extends', 'x', ?, 'org_a')",
            (foreign_link, seed, PAST),
        )
        conn.commit()
    finally:
        conn.close()
    as_org("org_a")
    res = _search("ocelot", limit=50)
    ids = [h["id"] for h in res["results"]]
    assert ids == [seed]
    assert foreign_entity not in ids and foreign_link not in ids
    assert memory_tools.memory_get(seed)["links"] == {"out": [], "in": []}


def test_hard_delete_cascades_entities_and_links():
    base = _save("Tapir", "Tapir tracked in task #3131.")
    child = _save("Tapir child", "Child.", links=[f"extends:{base}"])
    conn = storage.connect()
    try:
        assert storage.hard_delete_memory(conn, base, org_id=None)
        conn.commit()
    finally:
        conn.close()
    assert _links(child) == set()
    assert _entities(base) == set()


def test_slotted_ranks_never_move_base_items():
    from unified_agent import memory_ranking

    age = {"a": 1, "b": 3, "c": 3, "d": 9, "x": 0, "y": 3, "z": 20}
    ranks = memory_ranking.slotted_ranks(["a", "b", "c", "d"], ["x", "y", "z"], key=age.get)
    assert ranks == {"a": 1, "b": 2, "c": 2, "d": 4, "x": 1, "y": 2, "z": 5}


def test_link_as_of_compares_at_microsecond_precision():
    # julianday() rounds to milliseconds; the #2095 validity check does not,
    # and neither may the link cut-off.
    seed = _save("Kinkajou", "Kinkajou base.")
    child = _save("Child note", "Child.", links=[f"extends:{seed}"])
    conn = storage.connect()
    try:
        for nid in (seed, child):
            conn.execute(
                "UPDATE memory SET created_at=?, updated_at=? WHERE id=?", (PAST, PAST, nid)
            )
        conn.execute(
            "UPDATE memory_links SET created_at='2026-02-15T00:00:00.000500+00:00' WHERE src_id=?",
            (child,),
        )
        conn.commit()
    finally:
        conn.close()
    before = "2026-02-15T00:00:00.000400+00:00"
    after = "2026-02-15T00:00:00.000600+00:00"
    assert memory_tools.memory_get(seed, as_of=before)["links"]["in"] == []
    assert [lk["id"] for lk in memory_tools.memory_get(seed, as_of=after)["links"]["in"]] == [child]
    assert child not in _ids("kinkajou", as_of=before)
    assert child in _ids("kinkajou", as_of=after)


# --- review round 1: bounded traversal work, backfill under the write lock ----


def _bulk_linked(seed: str, count: int, prefix: str) -> None:
    """``count`` legacy-style notes, each with an extends link into ``seed``."""
    rows = [(f"{prefix}{i:06d}", f"Bulk {i}", "Bulk body.", "[]", PAST, PAST) for i in range(count)]
    conn = storage.connect()
    try:
        conn.executemany(
            "INSERT INTO memory (id, title, content, tags, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO memory_links (src_id, dst_id, rel, actor, created_at, org_id) "
            "VALUES (?, ?, 'extends', 'bulk', ?, NULL)",
            [(row[0], seed, PAST) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _graph_work(seed: str) -> tuple[dict, int, int]:
    from unified_agent import memory_graph

    conn = storage.connect()
    steps = [0]
    statements: list[str] = []
    try:
        conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 100)
        conn.set_trace_callback(statements.append)
        found = memory_graph.graph_leg(
            conn, [seed], None, eligibility_sql="org_id IS ?", eligibility_params=[None]
        )
    finally:
        conn.close()
    return found, steps[0], len(statements)


def test_graph_work_is_bounded_by_the_caps_not_the_seed_degree():
    small = _save("Small hub", "Small hub.")
    large = _save("Large hub", "Large hub.")
    _bulk_linked(small, 150, "s")
    _bulk_linked(large, 5000, "l")
    found_small, steps_small, statements_small = _graph_work(small)
    found_large, steps_large, statements_large = _graph_work(large)
    assert len(found_small) == len(found_large) == 10
    # Same statements, and VM work within a small factor although the large
    # seed has over 30x the links: the scan stops at the budget.
    assert statements_large == statements_small
    assert steps_large <= 1.5 * steps_small + 20


def test_note_links_reads_only_the_shown_rows():
    hub = _save("Busy hub", "Busy hub.")
    _bulk_linked(hub, 3000, "n")
    conn = storage.connect()
    steps = [0]
    try:
        from unified_agent import memory_graph

        conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 100)
        links = memory_graph.note_links(conn, hub, None)
    finally:
        conn.close()
    assert len(links["in"]) == memory_graph.MAX_NOTE_LINKS_SHOWN
    # Reading all 3000 rows and sorting them costs well over this.
    assert steps[0] < 400


def test_backfill_holds_the_write_lock_from_read_to_write(monkeypatch):
    from unified_agent import config, memory_graph

    nid = _save("Legacy", "Tracked in task #111.")
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM memory_entities")
        conn.execute("DELETE FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,))
        conn.commit()
    finally:
        conn.close()

    blocked: list[str] = []
    real_replace = memory_graph.replace_entities

    def racing_replace(c, note_id, entities, org_id):
        if note_id == nid and not blocked:
            # A concurrent save lands between the batch read and this write.
            writer = sqlite3.connect(config.db_path(), timeout=0)
            try:
                writer.execute(
                    "UPDATE memory SET content='Tracked in task #222.' WHERE id=?", (nid,)
                )
                writer.commit()
                blocked.append("committed")
            except sqlite3.OperationalError as exc:
                blocked.append(str(exc))
            finally:
                writer.close()
        return real_replace(c, note_id, entities, org_id)

    monkeypatch.setattr(memory_graph, "replace_entities", racing_replace)
    conn = storage.connect()
    try:
        memory_tools.backfill_memory_graph(conn)
    finally:
        conn.close()
    assert blocked and "locked" in blocked[0]
    monkeypatch.setattr(memory_graph, "replace_entities", real_replace)
    memory_tools.memory_save(content="Tracked in task #222.", id=nid, author="Desktop_Agent2")
    assert {v for k, v in _entities(nid) if k == "task"} == {"222"}


def test_backfill_reconciles_stale_graph_rows():
    a = _save("Stale A", "Tracked in task #222.")
    b = _save("Stale B", "B.")
    c = _save("Stale C", "C.", contradicts=[a])
    conn = storage.connect()
    try:
        # Rows a racing pre-fix backfill could leave: an entity the text no
        # longer names, a contradicts edge the column dropped, and a
        # supersedes edge for a note that is not superseded.
        conn.execute(
            "INSERT INTO memory_entities (note_id, kind, value, org_id) VALUES (?, 'task', '111', NULL)",
            (a,),
        )
        conn.execute("UPDATE memory SET contradicts=NULL WHERE id=?", (c,))
        conn.execute(
            "INSERT INTO memory_links (src_id, dst_id, rel, actor, created_at, org_id) "
            "VALUES (?, ?, 'supersedes', 'x', ?, NULL)",
            (b, a, PAST),
        )
        conn.execute("DELETE FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,))
        conn.commit()
        memory_tools.backfill_memory_graph(conn)
    finally:
        conn.close()
    assert {v for k, v in _entities(a) if k == "task"} == {"222"}
    assert _links(a) == set()


@pytest.mark.parametrize("api", ["graph", "links"])
@pytest.mark.parametrize("qualifying", [0, 3])
def test_historical_link_work_stays_bounded_with_newer_edges(api, qualifying):
    from unified_agent import memory_graph

    small = _save("Historical small hub", "Hub.")
    large = _save("Historical large hub", "Hub.")
    _bulk_linked(small, 150, "hs")
    _bulk_linked(large, 5000, "hl")
    conn = storage.connect()
    try:
        conn.execute("UPDATE memory_links SET created_at='2026-06-01T00:00:00+00:00'")
        for prefix in ("hs", "hl"):
            for i in range(qualifying):
                conn.execute(
                    "UPDATE memory_links SET created_at=? WHERE src_id=?",
                    (PAST, f"{prefix}{i:06d}"),
                )
        conn.commit()

        def measured(seed):
            steps = [0]
            conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 100)
            try:
                if api == "graph":
                    found = memory_graph.graph_leg(
                        conn,
                        [seed],
                        None,
                        eligibility_sql="org_id IS NULL",
                        eligibility_params=[],
                        as_of="2026-03-01T00:00:00+00:00",
                    )
                else:
                    found = memory_graph.note_links(
                        conn, seed, None, as_of="2026-03-01T00:00:00+00:00"
                    )["in"]
            finally:
                conn.set_progress_handler(None, 0)
            return len(found), steps[0]

        small_count, small_work = measured(small)
        large_count, large_work = measured(large)
    finally:
        conn.close()
    assert small_count == large_count == qualifying
    assert large_work <= 1.5 * small_work + 20


def test_backfill_derives_without_blocking_writes_and_retries_stale_snapshot(monkeypatch):
    from unified_agent import config, memory_graph

    nid = _save("Large legacy", "Tracked in task #111. " * 10000)
    _save("Later legacy", "Tracked in task #333.")
    conn = storage.connect()
    conn.execute("DELETE FROM memory_entities")
    conn.execute("DELETE FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,))
    conn.commit()
    real_extract = memory_tools._note_entities
    raced = []

    def concurrent_extract(title, content, tags):
        if title == "Large legacy" and not raced:
            raced.append(conn.in_transaction)
            writer = sqlite3.connect(config.db_path(), timeout=0)
            try:
                # Deliberately leave updated_at unchanged: timestamp equality
                # alone cannot establish that the derived snapshot is current.
                writer.execute("UPDATE memory SET content='task #222' WHERE id=?", (nid,))
                memory_graph.replace_entities(writer, nid, [("task", "222")], None)
                writer.commit()
            finally:
                writer.close()
        return real_extract(title, content, tags)

    monkeypatch.setattr(memory_tools, "_note_entities", concurrent_extract)
    try:
        memory_tools.backfill_memory_graph(conn)
    finally:
        conn.close()
    assert raced == [False]
    assert {v for k, v in _entities(nid) if k == "task"} == {"222"}


def test_unrelated_concurrent_writes_do_not_starve_backfill(monkeypatch):
    from unified_agent import config

    nid = _save("Legacy under load", "Tracked in task #444.")
    conn = storage.connect()
    conn.execute("DELETE FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,))
    conn.commit()
    real_extract = memory_tools._note_entities
    calls = []

    def busy_extract(title, content, tags):
        calls.append(title)
        assert len(calls) <= 4, "backfill repeatedly restarts on unrelated writes"
        with sqlite3.connect(config.db_path(), timeout=0) as writer:
            writer.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('unrelated_tick', ?)",
                (str(len(calls)),),
            )
        return real_extract(title, content, tags)

    monkeypatch.setattr(memory_tools, "_note_entities", busy_extract)
    try:
        memory_tools.backfill_memory_graph(conn)
    finally:
        conn.close()
    assert {v for k, v in _entities(nid) if k == "task"} == {"444"}


@pytest.mark.parametrize("field", ["title", "content"])
@pytest.mark.parametrize("body", [b"Tracks task #777.", b"\xff Tracks task #777."])
def test_backfill_accepts_legacy_blob_text_and_advances_cursor(field, body):
    broken = _save("Legacy blob", "Legacy body.")
    later = _save("Following note", "Tracks task #888.")
    conn = storage.connect()
    try:
        conn.execute(f"UPDATE memory SET {field}=? WHERE id=?", (body, broken))
        conn.execute("DELETE FROM memory_entities")
        conn.execute("DELETE FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,))
        conn.commit()
        assert memory_tools.backfill_memory_graph(conn) >= 2
        assert ("task", "777") in _entities(broken)
        assert ("task", "888") in _entities(later)
        assert memory_tools.backfill_memory_graph(conn) == 0
    finally:
        conn.close()


@pytest.mark.parametrize("relation", ["contradicts", "evidence"])
def test_backfill_caps_oversized_legacy_relations_before_locked_insert_probes(relation):
    import json

    first = _save("First target", "First.")
    beyond = _save("Beyond cap", "Outside legacy relation prefix.")
    source = _save("Legacy source", "Source.")
    refs = [first, *(f"absent-{i}" for i in range(5000)), beyond]
    if relation == "evidence":
        refs = [f"memory:{ref}" for ref in refs]
    conn = storage.connect()
    statements = []
    try:
        conn.execute(f"UPDATE memory SET {relation}=? WHERE id=?", (json.dumps(refs), source))
        conn.execute("DELETE FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,))
        conn.commit()
        conn.set_trace_callback(statements.append)
        memory_tools.backfill_memory_graph(conn)
        conn.set_trace_callback(None)
        # SQLite also traces trigger invocations of the same INSERT.
        probes = {s for s in statements if s.startswith("INSERT OR IGNORE INTO memory_links")}
        assert len(probes) <= memory_tools.MAX_EVIDENCE_REFS
        assert (source, first, relation) in _links(source)
        assert (source, beyond, relation) not in _links(source)
        assert memory_tools.backfill_memory_graph(conn) == 0
    finally:
        conn.close()


@pytest.mark.parametrize("batch_size", [1, 500])
@pytest.mark.parametrize("id_shape", ["blob", "empty_blob", "index_blob", "collision", "null"])
def test_startup_graph_hook_skips_non_text_ids_without_changing_identity(batch_size, id_shape):
    from unified_agent import post_init

    valid = _save("Valid identity", "Tracks task #222.")
    malformed = {
        "blob": b"\xfflegacy-id",
        "empty_blob": b"",
        "index_blob": b"masterindex00",
        "collision": valid.encode(),
        "null": None,
    }[id_shape]
    conn = storage.connect()
    try:
        for nid, title, content in (
            (malformed, "Malformed identity", "Tracks task #111."),
            ("legacyafter1", "Following note", "Tracks task #333."),
        ):
            conn.execute(
                "INSERT INTO memory (id,title,content,tags,author,created_at,updated_at) "
                "VALUES (?,?,?,'[]','Desktop_Agent2',?,?)",
                (nid, title, content, PAST, PAST),
            )
        conn.execute("DELETE FROM memory_entities")
        conn.execute("DELETE FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,))
        conn.commit()
        if batch_size == 500:
            # Exercise the foreground post-init entry point as used at startup.
            post_init._memory_graph_backfill(conn)
        else:
            memory_tools.backfill_memory_graph(conn, batch_size=batch_size)
        assert {v for k, v in _entities(valid) if k == "task"} == {"222"}
        assert ("task", "333") in _entities("legacyafter1")
        assert (
            conn.execute(
                "SELECT count(*) FROM memory_entities WHERE typeof(note_id)!='text'"
            ).fetchone()[0]
            == 0
        )
        source = conn.execute(
            "SELECT id, content FROM memory WHERE title='Malformed identity'"
        ).fetchone()
        assert source["id"] == malformed
        assert source["content"] == "Tracks task #111."
        cursor = conn.execute(
            "SELECT value FROM meta WHERE key=?", (memory_tools.GRAPH_BACKFILL_META_KEY,)
        ).fetchone()[0]
        assert int(cursor) == conn.execute("SELECT max(rowid) FROM memory").fetchone()[0]
        assert memory_tools.backfill_memory_graph(conn, batch_size=batch_size) == 0
    finally:
        conn.close()
