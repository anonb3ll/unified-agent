"""The one storage chokepoint for ``org_id`` on a partitioned store (task #1433, slice 3).

A single-org store has always meant ``org_id IS NULL``: every writer stamps
``edition.tenant_org_id()`` (``None``), and hundreds of reads and writes fence
on ``org_id IS ?`` with that same ``None``. A store that has been through the
explicit backfill (``org_partition_migration``) holds ``SENTINEL_ORG_ID`` in
those rows instead, so ``org_id`` can later become NOT NULL.

The sentinel must stay invisible, so the rule is: **Python keeps seeing NULL,
SQLite keeps the sentinel, and the connection is the boundary.** Application
code does not change and does not know the store was partitioned.

The ``meta`` row ``MARKER_KEY`` decides which side a store is on, not the
process environment, so every process sharing a data directory agrees. Without
the marker (every store, until an operator applies the backfill)
``open_store`` returns a plain ``sqlite3.Connection`` and nothing here runs.
With it, connections are ``ScopedConnection``:

* **Reads.** A SELECT gets a shadowing common table expression per scoped
  table it names. Unqualified ``tasks`` then resolves to
  ``SELECT ..., NULLIF(org_id, sentinel) AS org_id ... WHERE org_id IS sentinel``:
  rows of any other org are gone and the sentinel reads as NULL, so every
  spelling of the old predicate (``org_id IS ?``, ``COALESCE(org_id, '')``,
  joins on ``a.org_id IS b.org_id``) and every ``SELECT *`` behave as before.
* **Writes.** A leading WITH would make Python's sqlite3 stop treating the
  statement as DML (no implicit BEGIN, no ``rowcount``, no ``executemany``),
  so writes are edited in place instead: an ``org_id`` value in ``VALUES``
  becomes ``COALESCE(value, sentinel)``, which stamps the row before SQLite
  checks unique keys; ``org_id IS ...`` compares through ``NULLIF``; and a
  table read in a subquery is replaced by the filtered subquery.
* **Below the connection.** Persistent triggers (``trigger_statements``) stamp
  a row that was inserted without an org and abort any write of another org's
  row, so a raw connection or an ``executescript`` cannot get a foreign row in
  or out. They are installed by the backfill and removed by its downgrade.

Scope: partitioned hub tables that have a nullable ``org_id``
(``SCOPED_TABLES``). Tables in ``tenant_tables.PARTITION_PENDING`` join by
themselves when they gain the column. ``derived`` tables are reached through
their parent and are not filtered here yet.

``tests/test_org_partition.py`` is the ratchet: it fails when a partitioned
table is neither scoped nor listed, or when a new raw ``sqlite3.connect`` or a
``main.<table>`` reference could step around this module.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from types import MappingProxyType
from typing import Any, NamedTuple

from . import edition, tenant_tables

#: The org of every row on a partitioned single-org store. Stored, never shown.
SENTINEL_ORG_ID = "__single_org__"
#: ``meta`` key whose presence means this store holds the sentinel. Written only
#: by an explicit backfill, so a fresh store never has it.
MARKER_KEY = "org_partition_1433"
#: Prefix of every trigger this feature owns.
TRIGGER_PREFIX = "ua_org_part_"

# Partitioned tables with an org_id the chokepoint leaves alone. May only
# shrink (tests/test_org_partition.py holds the frozen baseline).
EXEMPT_TABLES: MappingProxyType[str, str] = MappingProxyType(
    {
        "connector_oauth_pending": "org_id is NOT NULL; '' is the private hub's own scope",
        "connector_oauth_connections": "org_id is NOT NULL; '' is the private hub's own scope",
        "llm_broker_spend": "org_id is NOT NULL; '' is the private hub's own scope",
    }
)

SCOPED_TABLES: frozenset[str] = frozenset(
    table
    for table, entry in tenant_tables.TABLE_TENANCY.items()
    if entry.kind == "partitioned"
    and entry.store == tenant_tables.HUB
    and table not in tenant_tables.PARTITION_PENDING
    and table not in EXEMPT_TABLES
)
_LITERAL = "'" + SENTINEL_ORG_ID + "'"
_ORG = '"org_id"'
_ENTRY_WORDS = frozenset({"SELECT", "VALUES", "WITH", "INSERT", "REPLACE", "UPDATE", "DELETE"})
_VERBS = _ENTRY_WORDS - {"WITH"}
_READ_VERBS = frozenset({"SELECT", "VALUES"})
_DDL = ("CREATE", "ALTER ", "DROP ")
# Words that can follow a table name in a FROM clause and are not an alias.
_NOT_ALIAS = frozenset(
    "CROSS EXCEPT FULL GROUP HAVING INDEXED INNER INTERSECT JOIN LEFT LIMIT NATURAL NOT "
    "ON ORDER OUTER RETURNING RIGHT SET UNION USING WHERE WINDOW".split()
)
# Words that end a FROM clause.
_AFTER_FROM = frozenset("EXCEPT GROUP HAVING INTERSECT LIMIT ORDER UNION WHERE WINDOW".split())
_SQL_TOKEN = re.compile(
    r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/"""
    r"""|[A-Za-z_][\w$]*|\?\d*|\d[\w.]*|\S""",
    re.DOTALL,
)


class _Tok(NamedTuple):
    text: str
    start: int
    end: int

    @property
    def word(self) -> str:
        return self.text.upper()

    @property
    def ident(self) -> str:
        """The identifier this token names, lower-cased; '' for a string literal."""
        head = self.text[0]
        if head == "'":
            return ""
        return (self.text[1:-1] if head in '"`[' else self.text).lower()


def _tokens(sql: str) -> list[_Tok]:
    return [
        _Tok(m.group(), m.start(), m.end())
        for m in _SQL_TOKEN.finditer(sql)
        if not m.group().startswith(("--", "/*"))
    ]


def _main_verb(toks: list[_Tok]) -> int | None:
    """Index of the statement's verb, looking past a leading WITH clause."""
    if toks[0].word != "WITH":
        return 0
    depth = 0
    for index, tok in enumerate(toks):
        if tok.text == "(":
            depth += 1
        elif tok.text == ")":
            depth -= 1
        elif depth == 0 and tok.word in _VERBS:
            return index
    return None


def _own_cte_names(toks: list[_Tok], verb: int) -> set[str]:
    """Names the statement's own WITH clause defines (they shadow a table already)."""
    names: set[str] = set()
    depth = 0
    for tok in toks[1:verb]:
        if tok.text == "(":
            depth += 1
        elif tok.text == ")":
            depth -= 1
        elif depth == 0 and tok.word not in {"AS", "RECURSIVE", "NOT", "MATERIALIZED"}:
            names.add(tok.ident)
    return names


def _select_list(columns: tuple[str, ...], *, masked: bool, rowid: bool) -> str:
    quoted = [f'"{name}"' for name in columns]
    if masked:
        quoted = [
            f"NULLIF({_ORG},{_LITERAL}) AS {_ORG}" if name == _ORG else name for name in quoted
        ]
    return ", ".join((["rowid AS rowid"] if rowid else []) + quoted)


def _filtered(table: str, columns: tuple[str, ...], *, masked: bool, rowid: bool) -> str:
    return (
        f"SELECT {_select_list(columns, masked=masked, rowid=rowid)} "
        f'FROM main."{table}" WHERE {_ORG} IS {_LITERAL}'
    )


def _fence_edits(toks: list[_Tok]) -> list[tuple[int, str]]:
    """In a write, compare every ``[x.]org_id`` operand of ``IS`` through NULLIF.

    The operand may be a real column holding the sentinel or a parameter still
    holding ``None``; NULLIF makes both sides read as the application expects.
    """
    edits: list[tuple[int, str]] = []
    for index, tok in enumerate(toks):
        if tok.ident != "org_id":
            continue
        first = index - 2 if index >= 2 and toks[index - 1].text == "." else index
        after = toks[index + 1].word if index + 1 < len(toks) else ""
        before = toks[first - 1].word if first >= 1 else ""
        before2 = toks[first - 2].word if first >= 2 else ""
        if after == "IS" or before == "IS" or (before == "NOT" and before2 == "IS"):
            edits += [(toks[first].start, "NULLIF("), (tok.end, f",{_LITERAL})")]
    return edits


def _target_index(toks: list[_Tok], verb: int) -> int | None:
    """Index of the table a write targets."""
    word = toks[verb].word
    index = verb + 1
    if word in {"INSERT", "REPLACE"}:
        while index < len(toks) and toks[index].word != "INTO":
            index += 1
        index += 1
    elif word == "DELETE":
        index += 1  # FROM
    elif word == "UPDATE" and index < len(toks) and toks[index].word == "OR":
        index += 2
    if index + 1 < len(toks) and toks[index + 1].text == ".":
        index += 2
    return index if index < len(toks) else None


def _insert_edits(
    toks: list[_Tok], target: int, columns: Callable[[str], tuple[str, ...] | None]
) -> list[tuple[int, str]]:
    """Stamp ``org_id`` in an ``INSERT ... (cols) VALUES (...)``, before key checks.

    A statement that omits the column gets it added: a BEFORE trigger cannot
    change NEW, and the memory org fences would refuse the NULL.
    """
    if not columns(toks[target].ident):
        return []
    index = target + 1
    if index < len(toks) and toks[index].word == "AS":
        index += 2
    if index >= len(toks) or toks[index].text != "(":
        return []
    names: list[str] = []
    index += 1
    while index < len(toks) and toks[index].text != ")":
        if toks[index].text != ",":
            names.append(toks[index].ident)
        index += 1
    index += 1
    if index >= len(toks) or toks[index].word != "VALUES":
        return []
    wanted = names.index("org_id") if "org_id" in names else None
    edits: list[tuple[int, str]] = []
    if wanted is None:
        edits.append((toks[index - 1].start, ", org_id"))
    index += 1
    while index < len(toks) and toks[index].text == "(":
        depth = 0
        position = 0
        begin = index + 1
        while index < len(toks):
            text = toks[index].text
            if text == "(":
                depth += 1
            elif text == ")":
                depth -= 1
            at_end = depth == 0 or (depth == 1 and text == ",")
            if at_end:
                if position == wanted:
                    edits += [
                        (toks[begin].start, "COALESCE("),
                        (toks[index - 1].end, f",{_LITERAL})"),
                    ]
                position += 1
                begin = index + 1
            if depth == 0:
                if wanted is None:
                    edits.append((toks[index].start, f", {_LITERAL}"))
                index += 1
                break
            index += 1
        if index < len(toks) and toks[index].text == ",":
            index += 1
        else:
            break
    return edits


def _subquery_edits(
    toks: list[_Tok],
    tables: set[str],
    target: int | None,
    columns: Callable[[str], tuple[str, ...] | None],
    rowid: bool,
) -> list[tuple[int, str, int]]:
    """Replace each table a write reads in a FROM or JOIN with the filtered rows."""
    edits: list[tuple[int, str, int]] = []
    for index, tok in enumerate(toks):
        name = tok.ident
        if index == target or name not in tables or index == 0:
            continue
        if toks[index - 1].word not in {"FROM", "JOIN"}:
            continue
        cols = columns(name)
        if not cols:
            continue
        follower = toks[index + 1] if index + 1 < len(toks) else None
        aliased = follower is not None and (
            follower.word == "AS"
            or (follower.text[0].isalpha() or follower.text[0] in '_"`[')
            and follower.word not in _NOT_ALIAS
        )
        body = f"({_filtered(name, cols, masked=False, rowid=rowid)})"
        edits.append((tok.start, body if aliased else f'{body} AS "{name}"', tok.end))
    return edits


def _apply(sql: str, spans: list[tuple[int, int, str]]) -> str:
    for start, end, text in sorted(spans, key=lambda span: span[0], reverse=True):
        sql = sql[:start] + text + sql[end:]
    return sql


def _index_hint_spans(toks: list[_Tok], tables: set[str]) -> list[tuple[int, int, str]]:
    """Drop ``INDEXED BY``: a filtered table has no index to name; the planner picks."""
    spans: list[tuple[int, int, str]] = []
    for index, tok in enumerate(toks[:-2]):
        if tok.word == "INDEXED" and toks[index + 1].word == "BY":
            owners = {toks[at].ident for at in range(max(index - 3, 0), index)}
            if owners & tables:
                spans.append((tok.start, toks[index + 2].end, ""))
    return spans


def _alias_tables(toks: list[_Tok], tables: list[str]) -> dict[str, str]:
    """Each name a scoped table goes by in a FROM or JOIN: its own, and its alias."""
    aliases: dict[str, str] = {}
    for index, tok in enumerate(toks):
        if index == 0 or tok.ident not in tables:
            continue
        if toks[index - 1].word not in {"FROM", "JOIN"} and toks[index - 1].text != ",":
            continue
        aliases[tok.ident] = tok.ident
        at = index + 2 if index + 1 < len(toks) and toks[index + 1].word == "AS" else index + 1
        if at < len(toks) and toks[at].word not in _NOT_ALIAS:
            if toks[at].text[0].isalpha() or toks[at].text[0] in '_"`[':
                aliases[toks[at].ident] = tok.ident
    return aliases


def _from_clause(toks: list[_Tok], star: int) -> list[_Tok]:
    """The top-level tokens of the FROM clause of the SELECT that ``star`` belongs to."""
    clause: list[_Tok] = []
    depth = 0
    inside = False
    for tok in toks[star + 1 :]:
        if tok.text == "(":
            depth += 1
        elif tok.text == ")":
            depth -= 1
            if depth < 0:
                break
        elif depth == 0 and tok.word == "FROM":
            inside = True
        elif depth == 0 and inside:
            if tok.word in _AFTER_FROM:
                break
            clause.append(tok)
    return clause


def _star_spans(
    toks: list[_Tok], tables: list[str], columns: Callable[[str], tuple[str, ...] | None]
) -> list[tuple[int, int, str]]:
    """Spell out ``*`` and ``x.*``: a CTE exposing rowid must not add it to ``*``.

    A bare ``*`` over a join would need every source's columns in FROM order, and
    a table outside the chokepoint has none here, so it is refused rather than
    returned with a column more or a source less. Only the ``*``'s own FROM
    clause counts: a subquery beside it adds nothing to its columns.
    """
    aliases = _alias_tables(toks, tables)
    spans: list[tuple[int, int, str]] = []
    for index, tok in enumerate(toks):
        if tok.text != "*" or index == 0:
            continue
        previous = toks[index - 1]
        if previous.word in {"SELECT", "DISTINCT", "ALL"}:
            clause = _from_clause(toks, index)
            scoped = [source.ident for source in clause if source.ident in tables]
            if not scoped:
                continue
            if any(source.word == "JOIN" or source.text == "," for source in clause):
                raise sqlite3.NotSupportedError(
                    "SELECT * over a join with rowid cannot run on a partitioned store; "
                    "name the columns or use alias.*"
                )
            names = columns(scoped[0]) or ()
            spans.append((tok.start, tok.end, ", ".join(f'"{name}"' for name in names)))
        elif previous.text == "." and index >= 2:
            table = aliases.get(toks[index - 2].ident)
            if table is not None:
                alias = toks[index - 2].text
                listed = ", ".join(f'{alias}."{name}"' for name in columns(table) or ())
                spans.append((toks[index - 2].start, tok.end, listed))
    return spans


def _returning_index(toks: list[_Tok]) -> int | None:
    """Index of a write's own RETURNING keyword, if it has one."""
    depth = 0
    for index, tok in enumerate(toks):
        if tok.text == "(":
            depth += 1
        elif tok.text == ")":
            depth -= 1
        elif depth == 0 and tok.word == "RETURNING":
            return index
    return None


def _returning_spans(
    toks: list[_Tok], at: int, columns: tuple[str, ...]
) -> list[tuple[int, int, str]]:
    """Mask ``org_id`` in a RETURNING clause, as a read's shadow does."""
    spans: list[tuple[int, int, str]] = []
    depth = 0
    for index in range(at + 1, len(toks)):
        tok = toks[index]
        if tok.text == "(":
            depth += 1
        elif tok.text == ")":
            depth -= 1
        first = index - 2 if toks[index - 1].text == "." else index
        before = toks[first - 1]
        after = toks[index + 1].text if index + 1 < len(toks) else ","
        alone = depth == 0 and (first - 1 == at or before.text == ",") and after == ","
        if tok.text == "*" and alone:
            listed = _select_list(columns, masked=True, rowid=False)
            spans.append((tok.start, tok.end, listed))
        elif tok.ident == "org_id" and before.word != "AS":
            name = f"{toks[first].text}.{tok.text}" if first != index else tok.text
            masked = f"NULLIF({name},{_LITERAL})"
            spans.append((toks[first].start, tok.end, f"{masked} AS {_ORG}" if alone else masked))
    return spans


def rewrite(sql: str, columns: Callable[[str], tuple[str, ...] | None]) -> str:
    """``sql`` as it must run on a partitioned store.

    ``columns(table)`` returns a scoped table's column names, or ``None`` when
    the table is absent here or carries no nullable ``org_id``. Anything that
    is not a SELECT or a write (PRAGMA, DDL, BEGIN) comes back unchanged.
    """
    toks = _tokens(sql)
    if not toks or toks[0].word not in _ENTRY_WORDS:
        return sql
    verb = _main_verb(toks)
    if verb is None:
        return sql
    own = _own_cte_names(toks, verb) if verb else set()
    rowid = any(tok.ident in {"rowid", "_rowid_"} for tok in toks)

    if toks[verb].word not in _READ_VERBS:
        target = _target_index(toks, verb)
        inserts: list[tuple[int, str]] = []
        if target is not None and toks[verb].word in {"INSERT", "REPLACE"}:
            inserts = _insert_edits(toks, target, columns)
        returning = _returning_index(toks)
        head = toks if returning is None else toks[:returning]
        spans = [(at, at, text) for at, text in _fence_edits(head) + inserts]
        spans += [
            (start, end, text)
            for start, text, end in _subquery_edits(
                toks, set(SCOPED_TABLES) - own, target, columns, rowid
            )
        ]
        spans += _index_hint_spans(toks, set(SCOPED_TABLES) - own)
        written = columns(toks[target].ident) if target is not None else None
        if returning is not None and written:
            spans += _returning_spans(toks, returning, written)
        return _apply(sql, spans)

    named: list[str] = []
    for index, tok in enumerate(toks):
        name = tok.ident
        if (
            name in SCOPED_TABLES
            and name not in own
            and name not in named
            and (index == 0 or toks[index - 1].text != ".")
            and columns(name)
        ):
            named.append(name)
    if not named:
        return sql
    spans = _index_hint_spans(toks, set(named))
    if rowid:
        spans += _star_spans(toks, named, columns)
    sql = _apply(sql, spans)
    # NOT MATERIALIZED: a table named twice (a self-join) must keep its indexes.
    defs = ", ".join(
        f'"{name}" AS NOT MATERIALIZED '
        f"({_filtered(name, columns(name) or (), masked=True, rowid=rowid)})"
        for name in named
    )
    if toks[0].word == "WITH":
        anchor = toks[1] if toks[1].word == "RECURSIVE" else toks[0]
        return f"{sql[: anchor.end]} {defs},{sql[anchor.end :]}"
    return f"WITH {defs} {sql}"


class ScopedCursor(sqlite3.Cursor):
    """A cursor whose statements pass through the connection's ``rewrite``."""

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        return super().execute(self.connection.scoped_sql(sql), *args)

    def executemany(self, sql: str, *args: Any) -> sqlite3.Cursor:
        return super().executemany(self.connection.scoped_sql(sql), *args)


class ScopedConnection(sqlite3.Connection):
    """A connection to a partitioned store. See the module docstring.

    ``executescript`` is deliberately not rewritten: it carries schema scripts,
    and the triggers still stamp and fence whatever one writes.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._org_columns: dict[str, tuple[str, ...] | None] = {}
        self._org_sql: dict[str, str] = {}

    def cursor(self, factory: Any = ScopedCursor) -> sqlite3.Cursor:
        return super().cursor(factory)

    # The C shortcuts make a plain cursor of their own, so route them here.
    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        return self.cursor().execute(sql, *args)

    def executemany(self, sql: str, *args: Any) -> sqlite3.Cursor:
        return self.cursor().executemany(sql, *args)

    def _scoped_columns(self, table: str) -> tuple[str, ...] | None:
        if table not in SCOPED_TABLES:
            return None
        if table not in self._org_columns:
            info = list(self.execute(f'PRAGMA table_info("{table}")'))
            usable = any(row[1] == "org_id" and not row[3] for row in info)
            self._org_columns[table] = tuple(row[1] for row in info) if usable else None
        return self._org_columns[table]

    def scoped_sql(self, sql: str) -> str:
        cached = self._org_sql.get(sql)
        if cached is not None:
            return cached
        if sql.lstrip()[:6].upper().startswith(_DDL):
            self._org_columns.clear()
            self._org_sql.clear()
            return sql
        scoped = rewrite(sql, self._scoped_columns)
        if len(self._org_sql) >= 1024:
            self._org_sql.clear()
        self._org_sql[sql] = scoped
        return scoped


def is_partitioned(conn: sqlite3.Connection) -> bool:
    """Whether this store holds the sentinel. A store with no ``meta`` does not.

    Only a missing table means no: a locked or unreadable store raises, because
    reading it unscoped would look like an empty store.
    """
    try:
        return (
            conn.execute("SELECT 1 FROM meta WHERE key=? LIMIT 1", (MARKER_KEY,)).fetchone()
            is not None
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise


def refuse_tenant() -> None:
    """A partitioned store is one org's, so a tenanted process may not read it."""
    if edition.tenant_org_id() is not None:
        raise edition.TenantStoreMismatchError(
            "this store was partitioned for a single-org hub, but this process "
            "stamps a real organization; run `unified-agent db org-partition "
            "--downgrade` before configuring a tenant"
        )


def open_store(database: Any, **kwargs: Any) -> sqlite3.Connection:
    """``sqlite3.connect`` for a hub store: scoped when the store is partitioned.

    Every reader of the hub store outside ``storage.connect`` must open it
    here, or its ``org_id IS ?`` fences would stop matching after a backfill.
    """
    conn = sqlite3.connect(database, **kwargs)
    try:
        partitioned = is_partitioned(conn)
    except BaseException:
        conn.close()
        raise
    if not partitioned:
        return conn
    conn.close()
    refuse_tenant()
    return sqlite3.connect(database, factory=ScopedConnection, **kwargs)


def trigger_statements(table: str) -> list[str]:
    """The persistent stamp and fence triggers of one scoped table."""
    foreign = f"OLD.org_id IS NOT NULL AND OLD.org_id IS NOT {_LITERAL}"
    refuse = f"SELECT RAISE(ABORT, 'org partition: row belongs to another org ({table})');"
    statements = [
        f"CREATE TRIGGER IF NOT EXISTS {TRIGGER_PREFIX}insert_{table} BEFORE INSERT ON {table} "
        f"WHEN NEW.org_id IS NOT NULL AND NEW.org_id IS NOT {_LITERAL} BEGIN {refuse} END",
        f"CREATE TRIGGER IF NOT EXISTS {TRIGGER_PREFIX}update_{table} BEFORE UPDATE ON {table} "
        f"WHEN ({foreign}) OR NEW.org_id IS NOT {_LITERAL} BEGIN {refuse} END",
        f"CREATE TRIGGER IF NOT EXISTS {TRIGGER_PREFIX}delete_{table} BEFORE DELETE ON {table} "
        f"WHEN {foreign} BEGIN {refuse} END",
    ]
    stamp = f"UPDATE {table} SET org_id = {_LITERAL} WHERE rowid = NEW.rowid;"
    if table == "event_log":  # append-only: a stamp is a sanctioned rewrite (the hash skips org_id)
        lift = "'org partition stamp'"
        stamp = (
            f"INSERT INTO event_log_maintenance (reason, started_at) VALUES ({lift}, ''); "
            f"{stamp} DELETE FROM event_log_maintenance WHERE reason = {lift};"
        )
    statements.append(
        f"CREATE TRIGGER IF NOT EXISTS {TRIGGER_PREFIX}stamp_{table} AFTER INSERT ON {table} "
        f"WHEN NEW.org_id IS NULL BEGIN {stamp} END"
    )
    return statements
