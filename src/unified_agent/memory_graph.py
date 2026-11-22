"""Persisted memory graph and the ``graph`` retrieval leg (task #2096, M6).

Two tables, created by ``table_schemas.memory_graph``:

- ``memory_entities(note_id, kind, value, org_id)``: the identifiers a note
  names (task, branch, project, service, agent), extracted at save by the same
  deterministic extractor ``memory_search`` runs on a query.
- ``memory_links(src_id, dst_id, rel, actor, created_at, org_id)``: typed
  edges. ``supersedes`` and ``contradicts`` mirror the columns of the same
  name, ``evidence`` comes from a ``memory:<id>`` evidence ref, and
  ``extends``/``derives`` are explicit (``memory_save(links=[...])``).

Tenant fence: every row carries the org_id of the note(s) it belongs to.
Writes go through ``INSERT ... SELECT`` joins that require each note to be in
that org, and DB triggers refuse a row whose org_id differs from its notes'.
Every read filters the edge row and both note rows on the caller's org, so
an edge written around the triggers still never traverses.

The leg (``graph_leg``) takes the best lexical/semantic hits as seeds and
walks one hop: links in either direction and notes sharing an entity. An
entity carried by more than ``UA_MEMORY_GRAPH_ENTITY_MAX_DF`` notes is a hub
(a project name, an agent label) and is not walked. Each seed keeps at most
``UA_MEMORY_GRAPH_FANOUT`` neighbours and the leg at most
``UA_MEMORY_GRAPH_LIMIT``. Hop targets must pass the search's own eligibility
SQL (tenant, deletion, supersede or ``as_of`` validity, tags, host, expiry),
so the leg never surfaces a note the other legs would hide. The leg only adds
notes: ``memory_search`` gives no graph rank to a note a content leg matched.

``open_contradiction_counts`` (task #2097, M7) counts the open
``contradicts`` links against a set of notes, fenced the same way; they lower
a note's effective confidence at read time (``memory_ranking``).
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .memory_ranking import _env_int

LINK_RELS = ("supersedes", "contradicts", "extends", "derives", "evidence")
# Relations a caller may write directly; supersedes and contradicts have
# their own memory_save arguments and semantics, evidence comes from refs.
EXPLICIT_LINK_RELS = ("extends", "derives")
MAX_NOTE_ENTITIES = 64
MAX_NOTE_LINKS_SHOWN = 100
# Entities walked per seed (task and branch first); bounds the hub checks.
MAX_SEED_ENTITIES = 16

# Kind weight on a shared-entity edge: a task or branch names one piece of
# work; a project, service or agent is shared by many unrelated notes.
_KIND_WEIGHT = {"task": 1.0, "branch": 1.0}
_DEFAULT_KIND_WEIGHT = 0.5
_LINK_WEIGHT = 1.0


def graph_seed_count() -> int:
    return _env_int("UA_MEMORY_GRAPH_SEEDS", 5)


def graph_fanout() -> int:
    return _env_int("UA_MEMORY_GRAPH_FANOUT", 10)


def graph_limit() -> int:
    return _env_int("UA_MEMORY_GRAPH_LIMIT", 20)


def graph_entity_max_df() -> int:
    return _env_int("UA_MEMORY_GRAPH_ENTITY_MAX_DF", 25, minimum=2)


# --- writes -------------------------------------------------------------------


def replace_entities(
    conn: sqlite3.Connection,
    note_id: str,
    entities: Iterable[tuple[str, str]],
    org_id: str | None,
) -> None:
    """Make ``note_id``'s entity rows exactly ``entities`` (kind, value)."""
    conn.execute("DELETE FROM memory_entities WHERE note_id=?", (note_id,))
    add_entities(conn, note_id, entities, org_id)


def add_entities(
    conn: sqlite3.Connection,
    note_id: str,
    entities: Iterable[tuple[str, str]],
    org_id: str | None,
) -> None:
    """Insert entity rows for a note in ``org_id``; existing rows are kept."""
    rows = list(dict.fromkeys(entities))[:MAX_NOTE_ENTITIES]
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO memory_entities (note_id, kind, value, org_id) "
        "SELECT id, ?, ?, org_id FROM memory WHERE id=? AND org_id IS ?",
        [(kind, value, note_id, org_id) for kind, value in rows],
    )


def add_link(
    conn: sqlite3.Connection,
    src_id: str,
    dst_id: str,
    rel: str,
    *,
    actor: str | None,
    created_at: str,
    org_id: str | None,
) -> bool:
    """Insert one edge when both notes are in ``org_id``. True if a row was added."""
    if rel not in LINK_RELS or src_id == dst_id:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO memory_links (src_id, dst_id, rel, actor, created_at, org_id) "
        "SELECT s.id, d.id, ?, ?, ?, s.org_id FROM memory s JOIN memory d ON d.id=? "
        "WHERE s.id=? AND s.org_id IS ? AND d.org_id IS ?",
        (rel, actor, created_at, dst_id, src_id, org_id, org_id),
    )
    return cur.rowcount == 1


def replace_links(
    conn: sqlite3.Connection,
    src_id: str,
    rel: str,
    dst_ids: Iterable[str],
    *,
    actor: str | None,
    created_at: str,
    org_id: str | None,
) -> None:
    """Make ``src_id``'s outgoing ``rel`` edges exactly ``dst_ids``.

    Kept edges keep their original actor and created_at.
    """
    wanted = list(dict.fromkeys(dst_ids))
    marks = ",".join("?" * len(wanted))
    keep = f" AND dst_id NOT IN ({marks})" if wanted else ""
    conn.execute(
        f"DELETE FROM memory_links WHERE src_id=? AND rel=? AND org_id IS ?{keep}",
        (src_id, rel, org_id, *wanted),
    )
    for dst in wanted:
        add_link(conn, src_id, dst, rel, actor=actor, created_at=created_at, org_id=org_id)


def remove_link(
    conn: sqlite3.Connection, src_id: str, dst_id: str, rel: str, org_id: str | None
) -> None:
    conn.execute(
        "DELETE FROM memory_links WHERE src_id=? AND dst_id=? AND rel=? AND org_id IS ?",
        (src_id, dst_id, rel, org_id),
    )


# --- reads --------------------------------------------------------------------


def _instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _recorded_by(created_at: str | None, as_of: datetime | None) -> bool:
    """Whether an edge existed at ``as_of``, compared at microsecond precision.

    Same contract as ``memory_search``'s validity check (task #2095), which
    compares in Python because SQLite's julianday() rounds to milliseconds.
    An unparseable stamp counts as not yet recorded.
    """
    if as_of is None:
        return True
    stamp = _instant(created_at)
    return stamp is not None and stamp <= as_of


_RECORDED_BY_FN = "ua_memory_link_recorded_by"


def _register_recorded_by(conn: sqlite3.Connection, as_of: str | None) -> datetime | None:
    """Make ``_RECORDED_BY_FN(created_at, as_of)`` callable on ``conn``."""
    cutoff = _instant(as_of)
    if cutoff is not None:
        conn.create_function(
            _RECORDED_BY_FN,
            2,
            lambda created_at, _as_of: int(_recorded_by(created_at, cutoff)),
            deterministic=True,
        )
    return cutoff


def note_links(
    conn: sqlite3.Connection,
    note_id: str,
    org_id: str | None,
    *,
    as_of: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """A note's edges in both directions, fenced to ``org_id``.

    Each entry is ``{id, rel, title, actor, created_at}`` where ``id`` is the
    other end, oldest first, at most ``MAX_NOTE_LINKS_SHOWN`` per direction.
    The raw scan walks the ``(org_id, end, created_at)`` index and stops at
    the cap before tenant joins or the precise ``as_of`` predicate. Thus an
    ineligible prefix can yield fewer results, without scanning later rows.
    """
    out: dict[str, list[dict[str, Any]]] = {"out": [], "in": []}
    cutoff = _register_recorded_by(conn, as_of)
    when = f" AND {_RECORDED_BY_FN}(l.created_at, ?)" if cutoff is not None else ""
    for direction, mine, other in (("out", "src_id", "dst_id"), ("in", "dst_id", "src_id")):
        params: list[Any] = [org_id, note_id, MAX_NOTE_LINKS_SHOWN, org_id, org_id]
        if cutoff is not None:
            params.append(as_of)
        rows = conn.execute(
            f"SELECT l.{other} AS id, l.rel, o.title, l.actor, l.created_at "
            "FROM (SELECT src_id, dst_id, rel, actor, created_at FROM memory_links "
            f"WHERE org_id IS ? AND {mine}=? ORDER BY created_at LIMIT ?) l "
            f"JOIN memory me ON me.id = l.{mine} AND me.org_id IS ? "
            f"JOIN memory o ON o.id = l.{other} AND o.org_id IS ? "
            f"WHERE 1{when} ORDER BY l.created_at",
            params,
        ).fetchall()
        out[direction] = [dict(r) for r in rows]
    return out


def open_contradiction_counts(
    conn: sqlite3.Connection,
    note_ids: Iterable[str],
    org_id: str | None,
    *,
    source_sql: str,
    source_params: list[Any],
    as_of: str | None,
    cap: int,
) -> dict[str, int]:
    """Open ``contradicts`` links against each of ``note_ids``, at most ``cap``.

    Every filter runs in SQL before anything is counted, so closed links never
    crowd out open ones: the link row and both notes are fenced to
    ``org_id``, soft-deleted disputers are left out, ``source_sql`` (a
    predicate over the disputing note aliased ``s``, with ``source_params``)
    applies the caller's liveness rule, and under ``as_of`` the link must have
    been recorded by then (microsecond precision). One grouped statement over
    the ``(org_id, dst_id, created_at)`` index; work grows with the links
    pointing at the candidate notes, and the result is one capped count each.
    Notes with no open link are absent.

    ``memory_links`` keeps no deletion history, so under ``as_of`` a link
    cleared after that instant is not seen, and its contradiction is not
    counted then.
    """
    ids = list(dict.fromkeys(note_ids))
    if not ids:
        return {}
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _ua_contradiction_targets (id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _ua_contradiction_targets")
    conn.executemany(
        "INSERT OR IGNORE INTO _ua_contradiction_targets (id) VALUES (?)", ((i,) for i in ids)
    )
    cutoff = _register_recorded_by(conn, as_of)
    when = f" AND {_RECORDED_BY_FN}(l.created_at, ?)" if cutoff is not None else ""
    params: list[Any] = [org_id, org_id, org_id, *source_params]
    if cutoff is not None:
        params.append(as_of)
    rows = conn.execute(
        "SELECT l.dst_id, COUNT(*) AS n FROM memory_links l "
        "JOIN memory s ON s.id = l.src_id AND s.org_id IS ? "
        "JOIN memory d ON d.id = l.dst_id AND d.org_id IS ? "
        "WHERE l.org_id IS ? AND l.rel = 'contradicts' "
        "AND l.dst_id IN (SELECT id FROM temp._ua_contradiction_targets) "
        "AND instr(s.tags, '\"meta:deleted\"') = 0 "
        f"AND ({source_sql}){when} GROUP BY l.dst_id",
        params,
    ).fetchall()
    return {row["dst_id"]: min(int(row["n"]), cap) for row in rows if row["n"]}


def graph_scan_budget() -> int:
    """Link rows read per seed and direction: ten times the fan-out."""
    return graph_fanout() * 10


def _skip_table(conn: sqlite3.Connection, skip: set[str]) -> str:
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _ua_graph_skip (id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _ua_graph_skip")
    conn.executemany("INSERT OR IGNORE INTO _ua_graph_skip (id) VALUES (?)", ((i,) for i in skip))
    return "memory.id NOT IN (SELECT id FROM temp._ua_graph_skip)"


def _link_neighbours(
    conn: sqlite3.Connection,
    seed: str,
    org_id: str | None,
    *,
    eligible_sql: str,
    eligible_params: list[Any],
    as_of: str | None,
    cutoff: datetime | None,
) -> list[tuple[str, str]]:
    """(neighbour, rel) for one seed, at most ``graph_fanout()`` per direction.

    The inner scan walks the ``(org_id, end, created_at)`` index and reads at
    most ``graph_scan_budget()`` link rows; eligibility (tenant, deletion,
    validity, filters) and the skip set apply to those rows in the same
    statement after the raw cap, including the precise ``as_of`` check.
    Newest stored timestamps come first; under ``as_of`` the oldest. An
    ineligible prefix yields fewer neighbours rather than an unbounded scan.
    """
    fanout = graph_fanout()
    budget = graph_scan_budget()
    order = "ASC" if cutoff is not None else "DESC"
    when = f" AND {_RECORDED_BY_FN}(l.link_created, ?)" if cutoff is not None else ""
    out: list[tuple[str, str]] = []
    for mine, other in (("src_id", "dst_id"), ("dst_id", "src_id")):
        params: list[Any] = [org_id, seed, budget, org_id, *eligible_params]
        if cutoff is not None:
            params.append(as_of)
        params.append(fanout)
        rows = conn.execute(
            "SELECT l.nb, l.rel FROM ("
            f"SELECT {other} AS nb, rel, created_at AS link_created FROM memory_links "
            f"WHERE org_id IS ? AND {mine} = ? "
            f"ORDER BY created_at {order} LIMIT ?"
            ") l JOIN memory ON memory.id = l.nb "
            f"WHERE memory.org_id IS ? AND {eligible_sql}{when} LIMIT ?",
            params,
        ).fetchall()
        out.extend((r["nb"], r["rel"]) for r in rows)
    return out


def _entity_neighbours(
    conn: sqlite3.Connection,
    seed: str,
    org_id: str | None,
    *,
    eligible_sql: str,
    eligible_params: list[Any],
    max_df: int,
) -> list[tuple[str, str, str, int]]:
    """(neighbour, kind, value, df) for the entities one seed names, hubs skipped.

    At most ``MAX_SEED_ENTITIES`` of the seed's entities are walked, task and
    branch first. The hub check reads at most ``max_df + 1`` index rows and
    each walk at most ``max_df``, so a popular entity costs no more than a
    rare one.
    """
    fanout = graph_fanout()
    rows = conn.execute(
        "SELECT kind, value FROM memory_entities WHERE note_id=? AND org_id IS ?",
        (seed, org_id),
    ).fetchall()
    entities = sorted(
        ((r["kind"], r["value"]) for r in rows),
        key=lambda kv: (-_KIND_WEIGHT.get(kv[0], _DEFAULT_KIND_WEIGHT), kv),
    )[:MAX_SEED_ENTITIES]
    out: list[tuple[str, str, str, int]] = []
    for kind, value in entities:
        df = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM memory_entities "
            "WHERE org_id IS ? AND kind=? AND value=? LIMIT ?)",
            (org_id, kind, value, max_df + 1),
        ).fetchone()[0]
        if df < 2 or df > max_df:
            continue
        members = conn.execute(
            "SELECT e.nb FROM ("
            "SELECT note_id AS nb FROM memory_entities "
            "WHERE org_id IS ? AND kind=? AND value=? ORDER BY note_id LIMIT ?"
            ") e JOIN memory ON memory.id = e.nb "
            f"WHERE memory.org_id IS ? AND {eligible_sql} LIMIT ?",
            (org_id, kind, value, max_df, org_id, *eligible_params, fanout),
        ).fetchall()
        out.extend((r["nb"], kind, value, df) for r in members)
    return out


def graph_leg(
    conn: sqlite3.Connection,
    seeds: list[str],
    org_id: str | None,
    *,
    eligibility_sql: str,
    eligibility_params: list[Any],
    exclude: Iterable[str] = (),
    as_of: str | None = None,
) -> dict[str, dict[str, Any]]:
    """One hop from ``seeds`` (best first) over links and shared entities.

    Returns ``{note_id: {"score": float, "via": [...]}}`` in leg rank order
    (score descending, id ascending). Each ``via`` entry names the seed and
    either the link ``rel`` or the shared ``entity`` (``kind:value``).

    The leg expands, it does not re-rank: seeds and every id in ``exclude``
    (memory_search passes its lexical and semantic hits) are left out before
    the fan-out cap. Boosting content matches that merely share an entity with
    a top hit lifted them over the hit itself on the M0 eval (lexical MRR
    0.837 to 0.792), so the leg only adds notes no content leg found.

    Work is bounded by the caps, not by a seed's degree (review round 1):
    every statement is index-ordered with a LIMIT, eligibility runs inside it,
    and the statement count depends only on the seeds and their entities.
    ``eligibility_sql`` must use unqualified ``memory`` columns.
    """
    if not seeds:
        return {}
    fanout = graph_fanout()
    max_df = graph_entity_max_df()
    cutoff = _register_recorded_by(conn, as_of)
    # Seeds come from the tenant-fenced content legs; re-check the fence here
    # so the leg never walks from a foreign note whatever its caller passes.
    marks = ",".join("?" * len(seeds))
    in_org = {
        r[0]
        for r in conn.execute(
            f"SELECT id FROM memory WHERE id IN ({marks}) AND org_id IS ?", (*seeds, org_id)
        )
    }
    seeds = [s for s in seeds if s in in_org]
    skip_sql = _skip_table(conn, set(seeds) | set(exclude))
    eligible_sql = f"({eligibility_sql}) AND {skip_sql}"

    scores: dict[str, float] = defaultdict(float)
    via: dict[str, list[dict[str, str]]] = defaultdict(list)
    for position, seed in enumerate(seeds, start=1):
        strength: dict[str, float] = defaultdict(float)
        reasons: dict[str, list[dict[str, str]]] = defaultdict(list)
        for nb, rel in _link_neighbours(
            conn,
            seed,
            org_id,
            eligible_sql=eligible_sql,
            eligible_params=eligibility_params,
            as_of=as_of,
            cutoff=cutoff,
        ):
            strength[nb] += _LINK_WEIGHT
            reasons[nb].append({"seed": seed, "rel": rel})
        for nb, kind, value, df in _entity_neighbours(
            conn,
            seed,
            org_id,
            eligible_sql=eligible_sql,
            eligible_params=eligibility_params,
            max_df=max_df,
        ):
            strength[nb] += _KIND_WEIGHT.get(kind, _DEFAULT_KIND_WEIGHT) / math.log2(1 + df)
            reasons[nb].append({"seed": seed, "entity": f"{kind}:{value}"})
        ranked = sorted(strength.items(), key=lambda kv: (-kv[1], kv[0]))[:fanout]
        for nb, s in ranked:
            # A neighbour of the best seed counts more than one of the fifth.
            scores[nb] += s / position
            via[nb].extend(reasons[nb])
    ordered = sorted(scores.items(), key=lambda kv: (-round(kv[1], 9), kv[0]))[: graph_limit()]
    return {nid: {"score": score, "via": via[nid][:5]} for nid, score in ordered}
