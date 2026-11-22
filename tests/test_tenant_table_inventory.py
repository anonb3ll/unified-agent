"""Every table declares its tenancy (task #1433).

``unified_agent.tenant_tables.TABLE_TENANCY`` classifies each SQLite table as
``partitioned``, ``derived``, ``accelerator`` or ``global``. These tests make
that inventory structural rather than a convention:

1. static: every table created or renamed into existence by SQL in
   ``src/unified_agent`` (Python string expressions, read through the AST, and
   ``*.sql`` files) is inventoried, or is an exempt rebuild temporary. A
   statement whose table name cannot be resolved fails unless its site is on
   an explicit allowlist. Every inventoried table must still be created;
2. runtime: on freshly built stores, each table is inventoried or is a real
   shadow table of an inventoried virtual table, and each kind's contract
   holds (an indexed ``org_id`` for partitions, a NOT NULL reference for
   derived tables, a verified rowid mapping for accelerators, no tenant text
   in global tables);
3. ratchet: ``PARTITION_PENDING`` and ``ORG_INDEX_PENDING`` stay within frozen
   baselines and their members really lack what they are pending;
4. inheritance: derived and accelerator parents resolve to a partition.

Each check is a pure function over a connection and an inventory, so the
``*_rejects_*`` tests feed it mutation fixtures the guard must catch.
"""

from __future__ import annotations

import ast
import importlib
import re
import sqlite3
import string
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import pytest

from unified_agent import edition, storage, tenant_tables
from unified_agent.tenant_tables import (
    ORG_INDEX_PENDING,
    ORG_INDEX_PENDING_BASELINE,
    PARTITION_PENDING,
    PARTITION_PENDING_BASELINE,
    TABLE_TENANCY,
    TenancyEntry,
)

_SRC = Path(__file__).resolve().parent.parent / "src" / "unified_agent"

# Transient tables a table-rebuild migration creates, copies into and renames
# over the original in one transaction. Listed by name, never by pattern, so a
# real table cannot hide behind a suffix.
_REBUILD_TEMPORARIES = frozenset(
    {
        "tasks_new",
        "approvals_new",
        "delegations_new",
        "autonomous_cycles_new",
        "executor_attempts_new",
        "executor_attempts__rebuild",
        "routine_runs__rebuild",
    }
)

# Sites with a CREATE TABLE / RENAME TO whose name cannot be resolved
# statically: (path under src/unified_agent, enclosing function) -> why the
# tables it creates are covered anyway. Keep this list short.
_DYNAMIC_SITES: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("edition.py", "ensure_store_tenant"): (
            "the literal 'CREATE VIRTUAL TABLE' is a prefix compared against "
            "sqlite_master.sql; it creates nothing"
        ),
        ("constraint_migration.py", "_rebuild_table"): (
            "copies one inventoried table (constraint_migration.REBUILDS) into a "
            "scratch name and renames it back in the same transaction; the "
            "scratch table does not survive the commit"
        ),
    }
)

# Derived references with no declared foreign key, named on purpose (from the
# review follow-up on this PR). A new derived reference must declare
# REFERENCES <parent> instead of joining this set. task_comments.task_id left
# it with task #2326, whose one-shot rebuild gives existing hubs the key that
# CREATE TABLE gives fresh ones (constraint_migration).
_DERIVED_FK_EXEMPT = frozenset(
    {
        "task_goal_links.task_id",
        "task_comment_threads.task_id",
        "task_liveness.task_id",
        "run_settings.task_id",
        "autonomy_action_reviews.pending_id",
        "message_reads.message_id",
        "node_followups.item_id",
        "node_claim_ops.node",
        "harness_config_drift.node",
        "session_wake_offers.session_id",
    }
)

# Column names that mark tenant text or a tenant identifier. A global table
# may not carry one, except as listed with a reason.
_TENANT_COLUMNS = frozenset(
    {
        "agent",
        "agent_label",
        "actor",
        "author",
        "subject",
        "login",
        "user_id",
        "task_id",
        "run_id",
        "session_id",
        "device_id",
        "node",
        "org_id",
        "query",
        "prompt",
        "error",
        "reason",
        "message",
        "content",
        "body",
        "summary",
        "notes",
        "note",
        "title",
        "text",
        "payload",
    }
)
_GLOBAL_COLUMN_EXEMPT: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("event_log_maintenance", "reason"): (
            "events.prune_events inserts the row inside its own transaction and deletes "
            "it before commit; the reason is a fixed code string"
        ),
    }
)

# --- static extraction ---------------------------------------------------------

_HOLE = "\x00"  # stands in for any part of a string that cannot be resolved
_IDENT = r"(?:\"[^\"]*\"|`[^`]*`|\[[^\]]*\]|'[^']*'|[A-Za-z_\x00][\w$\x00]*)"
_QNAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT})?"
_CREATE = re.compile(
    r"\bCREATE\s+(?P<temp>TEMP(?:ORARY)?\s+)?(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{_QNAME})(?=\s*\(|\s+USING\b|\s+AS\b|\s*\Z)",
    re.IGNORECASE,
)
_RENAME = re.compile(
    rf"\bALTER\s+TABLE\s+{_QNAME}\s+RENAME\s+TO\s+(?P<name>{_QNAME})", re.IGNORECASE
)
# A statement whose name continues outside this literal.
_DANGLING = re.compile(
    r"\b(?:CREATE\s+(?:TEMP(?:ORARY)?\s+)?(?:VIRTUAL\s+)?TABLE(?:\s+IF\s+NOT\s+EXISTS)?"
    r"|ALTER\s+TABLE\s+\S+\s+RENAME\s+TO)\s*\Z",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"%(?:\([^)]*\))?[-#0 +]*\d*(?:\.\d+)?[sdrifx%]")


def _normalise(raw: str) -> str:
    """Last part of a possibly schema-qualified, possibly quoted name, lowercased."""
    last = re.findall(_IDENT, raw)[-1]
    if last[0] in "\"'`[":
        last = last[1:-1]
    return last.lower()


def _names_in(sql: str) -> tuple[dict[str, bool], bool]:
    """Tables a SQL text creates or renames into (name -> created TEMP), and
    whether any statement's name is unresolved."""
    names: dict[str, bool] = {}
    unresolved = bool(_DANGLING.search(sql))
    for pattern in (_CREATE, _RENAME):
        for match in pattern.finditer(sql):
            name = _normalise(match.group("name"))
            if _HOLE in name:
                unresolved = True
            else:
                temp = bool(pattern is _CREATE and match.group("temp"))
                names[name] = names.get(name, True) and temp
    return names, unresolved


class _Strings:
    """Evaluate Python string expressions as far as static analysis allows.

    Handles literals, f-strings, ``+``, ``%`` and ``str.format``; module-level
    and function-local string constants resolve by name. Anything else becomes
    ``_HOLE``. ``value`` returns ``None`` for a non-string expression."""

    def __init__(self, env: dict[str, str]):
        self.env = env

    def value(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            return "".join(
                v.value if isinstance(v, ast.Constant) else self.piece(v.value) for v in node.values
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(node.left), self.value(node.right)
            if left is None and right is None:
                return None
            return (left if left is not None else self.piece(node.left)) + (
                right if right is not None else self.piece(node.right)
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            template = self.value(node.left)
            if template is None:
                return None
            args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            values = iter([self.piece(a) for a in args])
            return _PERCENT.sub(
                lambda m: "%" if m.group() == "%%" else next(values, _HOLE), template
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
        ):
            template = self.value(node.func.value)
            if template is None:
                return None
            return self._format(template, node)
        return None

    def piece(self, node: ast.AST) -> str:
        text = self.value(node)
        if text is not None:
            return text
        if isinstance(node, ast.Name) and node.id in self.env:
            return self.env[node.id]
        return _HOLE

    def _format(self, template: str, call: ast.Call) -> str:
        positional = [self.piece(a) for a in call.args]
        named = {k.arg: self.piece(k.value) for k in call.keywords if k.arg}
        out, auto = [], 0
        try:
            fields = list(string.Formatter().parse(template))
        except ValueError:
            return template
        for literal, field, _spec, _conversion in fields:
            out.append(literal)
            if field is None:
                continue
            if field == "":
                out.append(positional[auto] if auto < len(positional) else _HOLE)
                auto += 1
            elif field.isdigit():
                index = int(field)
                out.append(positional[index] if index < len(positional) else _HOLE)
            else:
                out.append(named.get(field, _HOLE))
        return "".join(out)


def _string_constants(body: list[ast.stmt], strings: _Strings) -> dict[str, str]:
    """Names assigned exactly once, in ``body``, to a fully resolvable string."""
    seen: dict[str, int] = {}
    values: dict[str, str] = {}
    for stmt in body:
        for node in ast.walk(stmt) if not isinstance(stmt, ast.FunctionDef) else ():
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                seen[node.id] = seen.get(node.id, 0) + 1
        target = value = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        if isinstance(target, ast.Name):
            text = strings.value(value)
            if text is not None and _HOLE not in text:
                values[target.id] = text
    return {k: v for k, v in values.items() if seen.get(k) == 1}


def _scan_python(source: str, label: str) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """(table -> sites, unresolved (label, function) sites) for one module."""
    tree = ast.parse(source)
    module_env = _string_constants(tree.body, _Strings({}))
    found: dict[str, set[str]] = {}
    unresolved: set[tuple[str, str]] = set()

    def visit(node: ast.AST, qual: str, strings: _Strings) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner = f"{qual}.{node.name}" if qual else node.name
            if not isinstance(node, ast.ClassDef):
                params = {a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)}
                outer = {k: v for k, v in strings.env.items() if k not in params}
                strings = _Strings({**outer, **_string_constants(node.body, _Strings(outer))})
            for child in ast.iter_child_nodes(node):
                visit(child, inner, strings)
            return
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
            text = strings.value(node)
            if text is not None:
                names, missing = _names_in(text)
                for name, temp in names.items():
                    site = f"{label}:{node.lineno}" + (":temp" if temp else "")
                    found.setdefault(name, set()).add(site)
                if missing:
                    unresolved.add((label, qual or "<module>"))
                return
        for child in ast.iter_child_nodes(node):
            visit(child, qual, strings)

    visit(tree, "", _Strings(module_env))
    return found, unresolved


def _scan_tree() -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    found: dict[str, set[str]] = {}
    unresolved: set[tuple[str, str]] = set()
    for path in sorted(_SRC.rglob("*.py")) + sorted(_SRC.parent.rglob("*.sql")):
        label = str(path.relative_to(_SRC if path.suffix == ".py" else _SRC.parent))
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            names, missing = _scan_python(text, label)
        else:
            created, dynamic = _names_in(text)
            names = {n: {label + (":temp" if t else "")} for n, t in created.items()}
            missing = {(label, "<file>")} if dynamic else set()
        for name, sites in names.items():
            found.setdefault(name, set()).update(sites)
        unresolved |= missing
    return found, unresolved


# --- runtime schema checks -------------------------------------------------------

_FTS5_SHADOW = frozenset({"data", "idx", "content", "docsize", "config"})
# sqlite-vec does not register its shadow tables with SQLite, so PRAGMA
# table_list reports them as ordinary tables: match its exact suffixes.
_VEC0_SHADOW = re.compile(
    r"(?:info|chunks|rowids|auxiliary|(?:vector_chunks|metadatachunks|metadatatext)\d{2})"
)


def _table_list(conn: sqlite3.Connection) -> dict[str, tuple[str, int]]:
    """Table name -> (PRAGMA table_list type, without-rowid flag), main schema."""
    return {
        r[1]: (r[2], r[4])
        for r in conn.execute("PRAGMA table_list")
        if r[0] == "main" and not r[1].startswith("sqlite_")
    }


def _sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
    return (row[0] or "") if row else ""


def _module_of(sql: str) -> str | None:
    match = re.search(r"\bUSING\s+(\w+)", sql, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _shadow_tables(conn: sqlite3.Connection) -> set[str]:
    """Tables SQLite (or sqlite-vec) owns on behalf of a virtual table."""
    tables = _table_list(conn)
    virtual = {n: _module_of(_sql(conn, n)) for n, (kind, _) in tables.items() if kind == "virtual"}
    shadows = set()
    for name, (kind, _) in tables.items():
        for owner, module in virtual.items():
            if not name.startswith(f"{owner}_"):
                continue
            suffix = name[len(owner) + 1 :]
            if kind == "shadow" and (module != "fts5" or suffix in _FTS5_SHADOW):
                shadows.add(name)
            elif kind == "table" and module == "vec0" and _VEC0_SHADOW.fullmatch(suffix):
                shadows.add(name)
    return shadows


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple[str, int, int]]:
    """Column name -> (declared type, notnull, pk position)."""
    return {r[1]: (r[2], r[3], r[5]) for r in conn.execute(f"PRAGMA table_info('{table}')")}


def _rowid_alias(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = _columns(conn, table)
    pks = [c for c, (_t, _n, pk) in cols.items() if pk]
    without_rowid = _table_list(conn)[table][1]
    return not without_rowid and pks == [column] and cols[column][0].upper() == "INTEGER"


def _effectively_not_null(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """SQLite enforces NOT NULL on a PRIMARY KEY only in a WITHOUT ROWID table
    or for an INTEGER PRIMARY KEY rowid alias; elsewhere a PK column takes NULL."""
    _type, notnull, pk = _columns(conn, table)[column]
    if notnull:
        return True
    if pk and _table_list(conn)[table][1]:
        return True
    return _rowid_alias(conn, table, column)


def _has_parent_fk(conn: sqlite3.Connection, table: str, column: str, parent: str) -> bool:
    return any(
        r[3] == column and r[2].lower() == parent
        for r in conn.execute(f"PRAGMA foreign_key_list('{table}')")
    )


def _has_leading_org_index(conn: sqlite3.Connection, table: str) -> bool:
    for index in conn.execute(f"PRAGMA index_list('{table}')").fetchall():
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info('{index[1]}')")]
        if cols and cols[0] == "org_id":
            return True
    return False


def _accelerator_problems(conn: sqlite3.Connection, table: str, entry: TenancyEntry) -> list:
    tables = _table_list(conn)
    sql = _sql(conn, table)
    module = _module_of(sql)
    if entry.parent not in tables:
        return [f"{table}: parent {entry.parent} is not in the store"]
    if entry.via == "rowid":
        if tables[entry.parent][1]:
            return [f"{table}: parent {entry.parent} is WITHOUT ROWID, so it has no rowid"]
    elif entry.via not in _columns(conn, entry.parent) or not _rowid_alias(
        conn, entry.parent, entry.via
    ):
        return [f"{table}: {entry.parent}.{entry.via} is not the parent's rowid alias"]
    if module == "fts5":
        content = re.search(r"\bcontent\s*=\s*'([^']*)'", sql, re.IGNORECASE)
        content_rowid = re.search(r"\bcontent_rowid\s*=\s*'([^']*)'", sql, re.IGNORECASE)
        problems = []
        if not content or content.group(1).lower() != entry.parent:
            problems.append(f"{table}: content= does not name its parent {entry.parent}")
        if (content_rowid.group(1) if content_rowid else "rowid") != entry.via:
            problems.append(f"{table}: content_rowid does not match via={entry.via}")
        synced = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=? AND sql LIKE ?",
            (entry.parent, f"%INSERT INTO {table}%"),
        ).fetchone()
        if not synced:
            problems.append(f"{table}: no trigger on {entry.parent} keeps it in sync")
        return problems
    if module == "vec0":
        # vec0 rows are written by storage.upsert_memory_vec and its chunk twin,
        # keyed by the parent's rowid; there is no declarative mapping to read.
        return [] if entry.via == "rowid" else [f"{table}: vec0 mirrors are keyed by rowid"]
    return [f"{table}: unknown accelerator module {module!r}"]


def _schema_problems(
    conn: sqlite3.Connection,
    store: str,
    inventory: Mapping[str, TenancyEntry],
    partition_pending: Mapping[str, str],
    index_pending: Mapping[str, str],
    fk_exempt: frozenset[str] = frozenset(),
) -> list[str]:
    """Everything wrong between one store's real schema and the inventory."""
    tables = _table_list(conn)
    declared = {t for t, e in inventory.items() if e.store == store}
    problems = [
        f"{store} store lacks inventoried table {t}" for t in sorted(declared - set(tables))
    ]
    problems += [
        f"{store} store has table {t} missing from TABLE_TENANCY"
        for t in sorted(set(tables) - declared - _shadow_tables(conn))
    ]
    for table in sorted(declared & set(tables)):
        entry, cols = inventory[table], _columns(conn, table)
        virtual = tables[table][0] == "virtual"
        if virtual != (entry.kind == "accelerator"):
            problems.append(f"{table}: virtual tables, and only they, are accelerators")
            continue
        if entry.kind == "partitioned":
            if table in partition_pending:
                if "org_id" in cols:
                    problems.append(f"{table} has org_id: remove it from PARTITION_PENDING")
            elif "org_id" not in cols:
                problems.append(f"{table}: partitioned without org_id")
            elif table in index_pending:
                if _has_leading_org_index(conn, table):
                    problems.append(
                        f"{table} has an org_id index: remove it from ORG_INDEX_PENDING"
                    )
            elif not _has_leading_org_index(conn, table):
                problems.append(f"{table}: partitioned without an index leading on org_id")
        elif entry.kind == "derived":
            if entry.via not in cols:
                problems.append(f"{table}.{entry.via} does not exist")
            elif not _effectively_not_null(conn, table, entry.via):
                problems.append(f"{table}.{entry.via} accepts NULL, so a row can lack a tenant")
            elif f"{table}.{entry.via}" not in fk_exempt and not _has_parent_fk(
                conn, table, entry.via, entry.parent
            ):
                problems.append(f"{table}.{entry.via} has no foreign key to {entry.parent}")
        elif entry.kind == "accelerator":
            problems += _accelerator_problems(conn, table, entry)
        else:
            for column in sorted(set(cols) & _TENANT_COLUMNS):
                if (table, column) not in _GLOBAL_COLUMN_EXEMPT:
                    problems.append(f"{table}: global table holds tenant column {column}")
    return problems


def _inheritance_problems(inventory: Mapping[str, TenancyEntry]) -> list[str]:
    problems = []
    for table, entry in inventory.items():
        if entry.kind not in tenant_tables.INHERITING:
            continue
        seen, parent = {table}, entry.parent
        while parent in inventory and inventory[parent].kind in tenant_tables.INHERITING:
            if parent in seen:
                problems.append(f"{table}: inheritance cycle through {parent}")
                break
            seen.add(parent)
            parent = inventory[parent].parent
        else:
            if parent not in inventory:
                problems.append(f"{table}: parent {parent} is not inventoried")
            elif inventory[parent].kind != "partitioned":
                problems.append(f"{table}: parent chain ends at non-partition {parent}")
            elif inventory[parent].store != entry.store:
                problems.append(f"{table}: parent {parent} lives in another store")
    return problems


def _ratchet_problems(
    pending: Mapping[str, str], baseline: frozenset[str], inventory: Mapping[str, TenancyEntry]
) -> list[str]:
    problems = [f"{t} is not in the frozen baseline" for t in sorted(set(pending) - baseline)]
    problems += [
        f"{t} is not a partitioned table"
        for t in sorted(pending)
        if t not in inventory or inventory[t].kind != "partitioned"
    ]
    problems += [
        f"{t}: follow-up names no task" for t, f in pending.items() if not re.search(r"#\d+", f)
    ]
    return problems


# --- stores -----------------------------------------------------------------------


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(_SRC.parent).with_suffix("").parts)


def _hub_store() -> sqlite3.Connection:
    """The hub store with every table created: init_db, each module's
    ``ensure_schema``, and the creators that build their table lazily."""
    from unified_agent import llm_spend, workflow_roles
    from unified_agent.tools import digest, export, notify

    storage.init_db()
    conn = storage.connect()
    for path in sorted(_SRC.rglob("*.py")):
        if re.search(r"^def ensure_schema\(", path.read_text(encoding="utf-8"), re.M):
            importlib.import_module(_module_name(path)).ensure_schema(conn)
    workflow_roles._ensure_schema(conn)
    llm_spend._table(conn)
    conn.executescript(notify._DELIVERY_LOG_SCHEMA)
    digest._init_digest_db(conn)
    export._ensure_graph_import_ownership(conn)
    export._ensure_import_row_ownership(conn)
    conn.commit()
    return conn


def _other_store(store: str, tmp_path: Path) -> sqlite3.Connection:
    if store == tenant_tables.CLOUD_MIRROR:
        from unified_agent import cloud_mirror_receiver

        conn = sqlite3.connect(":memory:")
        conn.executescript(cloud_mirror_receiver._SCHEMA_SQL)
        return conn
    if store == tenant_tables.INVOCATION:
        from unified_agent import invocation_service

        conn = sqlite3.connect(":memory:")
        conn.executescript(invocation_service.RECEIPT_SCHEMA)
        return conn
    if store == tenant_tables.SYNTHETIC:
        from unified_agent.tools import synthetic_workflow

        conn = sqlite3.connect(":memory:")
        conn.executescript(synthetic_workflow.PROVIDER_SCHEMA)
        return conn
    if store == tenant_tables.LANGGRAPH:
        from unified_agent import langgraph_pilot

        conn = sqlite3.connect(":memory:")
        conn.executescript(langgraph_pilot.RESUME_LEASE_SCHEMA)
        return conn
    assert store == tenant_tables.PILOT
    from unified_agent.pilot_auth import PilotAuth

    db = tmp_path / "pilot" / "pilot.db"
    PilotAuth(db, "org_inventory", {"user_1": "Desktop_Test"}, provider=object())
    return sqlite3.connect(db)


@pytest.fixture
def stores(tmp_path):
    conns = {tenant_tables.HUB: _hub_store()}
    for store in tenant_tables.STORES - {tenant_tables.HUB}:
        conns[store] = _other_store(store, tmp_path)
    yield conns
    for conn in conns.values():
        conn.close()


# --- 1. static ------------------------------------------------------------------


def test_every_created_table_declares_its_tenancy():
    found, unresolved = _scan_tree()
    temp = set(tenant_tables.CONNECTION_TEMP_TABLES)
    unclassified = sorted(set(found) - set(TABLE_TENANCY) - _REBUILD_TEMPORARIES - temp)
    assert not unclassified, (
        "New table(s) without a tenancy decision. Add each to TABLE_TENANCY in "
        "src/unified_agent/tenant_tables.py (see docs/architecture/tenancy.md): "
        + ", ".join(f"{t} ({', '.join(sorted(found[t]))})" for t in unclassified)
    )
    assert not (_REBUILD_TEMPORARIES | temp) & set(TABLE_TENANCY)
    persistent = sorted(
        f"{t} ({site})" for t in temp for site in found.get(t, ()) if not site.endswith(":temp")
    )
    assert not persistent, f"connection temp tables created in the main schema: {persistent}"
    unknown = sorted(unresolved - set(_DYNAMIC_SITES))
    assert not unknown, (
        f"CREATE TABLE / RENAME TO with a name that cannot be resolved statically: {unknown}. "
        "Use a literal or module constant name, or add the site to _DYNAMIC_SITES with a reason."
    )
    assert not set(_DYNAMIC_SITES) - unresolved, "stale _DYNAMIC_SITES entries"


def test_every_inventoried_table_is_still_created():
    found = set(_scan_tree()[0])
    assert not sorted(set(TABLE_TENANCY) - found), "TABLE_TENANCY lists tables no source creates"
    assert not sorted(set(tenant_tables.CONNECTION_TEMP_TABLES) - found), "stale temp tables"
    assert not sorted(_REBUILD_TEMPORARIES - found), "exempt rebuild temporaries no longer created"


_STATIC_FIXTURES = {
    "concatenation": ('conn.execute("CREATE TABLE " + "probe_a" + " (x)")', {"probe_a"}),
    "format": ('conn.execute("CREATE TABLE {} (x)".format("probe_b"))', {"probe_b"}),
    "format_keyword": ('q = "CREATE TABLE {t} (x)".format(t="probe_c")', {"probe_c"}),
    "percent": ('conn.execute("CREATE TABLE %s (x)" % ("probe_d",))', {"probe_d"}),
    "single_quoted": ("conn.execute(\"CREATE TABLE 'probe_e' (x)\")", {"probe_e"}),
    "ctas": ('conn.execute("CREATE TABLE probe_f AS SELECT 1")', {"probe_f"}),
    "schema_qualified": ("conn.execute('CREATE TABLE main.\"Probe_G\" (x)')", {"probe_g"}),
    "temp_bracketed": ('conn.execute("CREATE TEMP TABLE [probe_h] (x)")', {"probe_h"}),
    "temp_then_main": (
        'a = "CREATE TEMP TABLE probe_l (x)"\nb = "CREATE TABLE IF NOT EXISTS probe_l (x)"',
        {"probe_l"},
    ),
    "rename_to": ('conn.execute("ALTER TABLE tasks RENAME TO probe_i")', {"probe_i"}),
    "module_constant": ('T = "probe_j"\nSQL = f"CREATE TABLE IF NOT EXISTS {T} (x)"', {"probe_j"}),
    "local_constant": (
        'def f(conn):\n    t = "probe_k"\n    conn.execute(f"CREATE TABLE {t}_new (x)")',
        {"probe_k_new"},
    ),
    "prose": ('"""CREATE TABLE IF NOT EXISTS is idempotent; CREATE TABLE text is kept."""', set()),
}


@pytest.mark.parametrize("case", sorted(_STATIC_FIXTURES))
def test_static_scan_rejects_hidden_table_names(case):
    source, expected = _STATIC_FIXTURES[case]
    found, unresolved = _scan_python(source, "fixture.py")
    assert set(found) == expected
    assert not unresolved
    temp_only = {t for t, sites in found.items() if all(s.endswith(":temp") for s in sites)}
    assert temp_only == ({"probe_h"} if case == "temp_bracketed" else set())


def test_static_scan_rejects_unresolvable_names():
    dynamic = (
        "def make(conn, name):\n"
        '    conn.execute(f"CREATE TABLE IF NOT EXISTS {name} (x)")\n'
        "def rename(conn, name):\n"
        '    conn.execute("ALTER TABLE tasks RENAME TO " + name)\n'
        'PREFIX = "CREATE TABLE "\n'
    )
    assert _scan_python(dynamic, "fixture.py") == (
        {},
        {("fixture.py", "make"), ("fixture.py", "rename"), ("fixture.py", "<module>")},
    )
    assert _names_in("CREATE TABLE IF NOT EXISTS probe_sql (x);\n") == ({"probe_sql": False}, False)


# --- 2. runtime -----------------------------------------------------------------


def test_runtime_schema_matches_the_inventory(stores):
    problems = []
    for store, conn in stores.items():
        problems += _schema_problems(
            conn, store, TABLE_TENANCY, PARTITION_PENDING, ORG_INDEX_PENDING, _DERIVED_FK_EXEMPT
        )
    assert not problems, problems
    derived = {f"{t}.{e.via}": e for t, e in TABLE_TENANCY.items() if e.kind == "derived"}
    assert _DERIVED_FK_EXEMPT <= set(derived), "stale _DERIVED_FK_EXEMPT entries"
    hub = stores[tenant_tables.HUB]
    gained = sorted(
        ref
        for ref in _DERIVED_FK_EXEMPT
        if _has_parent_fk(hub, ref.split(".")[0], derived[ref].via, derived[ref].parent)
    )
    assert not gained, f"remove from _DERIVED_FK_EXEMPT, they now have a foreign key: {gained}"


def test_legacy_tenant_tables_are_indexed_partitions():
    """``edition._LEGACY_TENANT_TABLES`` is hand-kept; every member must be an
    inventoried partition that already carries org_id."""
    ready = tenant_tables.partitioned_tables() - set(PARTITION_PENDING)
    assert edition._LEGACY_TENANT_TABLES <= ready


def _fixture_conn(sql: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)
    return conn


_PARENT_SQL = (
    "CREATE TABLE parent (id TEXT PRIMARY KEY, org_id TEXT, title TEXT);"
    "CREATE INDEX idx_parent_org ON parent(org_id);"
)
_PARENT = {"parent": TenancyEntry("partitioned", "fixture parent")}


def _problems(sql: str, inventory: dict) -> list[str]:
    return _schema_problems(_fixture_conn(sql), "hub", {**_PARENT, **inventory}, {}, {})


def test_shadow_exclusion_rejects_a_lookalike_table():
    sql = _PARENT_SQL + (
        "CREATE VIRTUAL TABLE parent_fts USING fts5(title, content='parent', content_rowid='rowid');"
        "CREATE TRIGGER parent_fts_ai AFTER INSERT ON parent BEGIN "
        "INSERT INTO parent_fts(rowid, title) VALUES (new.rowid, new.title); END;"
        "CREATE TABLE parent_fts_review_private (content TEXT);"
    )
    fts = {"parent_fts": TenancyEntry("accelerator", "fts", parent="parent", via="rowid")}
    assert _problems(sql, fts) == [
        "hub store has table parent_fts_review_private missing from TABLE_TENANCY"
    ]


def test_derived_check_rejects_a_nullable_primary_key():
    sql = _PARENT_SQL + (
        "CREATE TABLE child_text (pid TEXT PRIMARY KEY REFERENCES parent(id));"
        "CREATE TABLE child_int (pid INTEGER PRIMARY KEY REFERENCES parent(id));"
        "CREATE TABLE child_wr (pid TEXT PRIMARY KEY REFERENCES parent(id)) WITHOUT ROWID;"
        "CREATE TABLE child_nn (pid TEXT NOT NULL REFERENCES parent(id));"
        "CREATE TABLE child_nofk (pid TEXT NOT NULL);"
    )
    derived = {
        t: TenancyEntry("derived", "fixture", parent="parent", via="pid")
        for t in ("child_text", "child_int", "child_wr", "child_nn", "child_nofk")
    }
    assert _problems(sql, derived) == [
        "child_nofk.pid has no foreign key to parent",
        "child_text.pid accepts NULL, so a row can lack a tenant",
    ]


def test_global_check_rejects_tenant_text():
    sql = _PARENT_SQL + "CREATE TABLE samples (ts TEXT, error TEXT); CREATE TABLE sizes (ts TEXT);"
    globals_ = {t: TenancyEntry("global", "fixture") for t in ("samples", "sizes")}
    assert _problems(sql, globals_) == ["samples: global table holds tenant column error"]


def test_accelerator_check_rejects_a_wrong_mapping():
    sql = _PARENT_SQL + (
        "CREATE TABLE other (id INTEGER PRIMARY KEY, title TEXT);"
        "CREATE VIRTUAL TABLE wrong_fts USING fts5(title, content='other');"
        "CREATE VIRTUAL TABLE as_global USING fts5(title);"
    )
    inventory = {
        "other": TenancyEntry("global", "fixture"),
        "wrong_fts": TenancyEntry("accelerator", "fts", parent="parent", via="rowid"),
        "as_global": TenancyEntry("global", "fixture"),
    }
    problems = _problems(sql, inventory)
    assert "as_global: virtual tables, and only they, are accelerators" in problems
    assert "wrong_fts: content= does not name its parent parent" in problems
    assert "wrong_fts: no trigger on parent keeps it in sync" in problems
    assert "other: global table holds tenant column title" in problems


# --- 3. ratchet -----------------------------------------------------------------


def test_pending_ratchets_stay_within_frozen_baselines():
    for pending, baseline in (
        (PARTITION_PENDING, PARTITION_PENDING_BASELINE),
        (ORG_INDEX_PENDING, ORG_INDEX_PENDING_BASELINE),
    ):
        assert not _ratchet_problems(pending, baseline, TABLE_TENANCY)
    assert not set(PARTITION_PENDING) & set(ORG_INDEX_PENDING)


def test_ratchet_rejects_a_substitution():
    swapped = {t: f for t, f in PARTITION_PENDING.items() if t != "agents"}
    swapped["tasks"] = "#1433 slice 2"
    assert len(swapped) == len(PARTITION_PENDING)
    assert _ratchet_problems(swapped, PARTITION_PENDING_BASELINE, TABLE_TENANCY) == [
        "tasks is not in the frozen baseline"
    ]


# --- 4. inheritance -------------------------------------------------------------


def test_inheriting_tables_resolve_to_a_partition():
    assert not _inheritance_problems(TABLE_TENANCY)
    broken = {
        "g": TenancyEntry("global", "fixture"),
        "d": TenancyEntry("derived", "fixture", parent="g", via="gid"),
        "a": TenancyEntry("accelerator", "fixture", parent="missing", via="rowid"),
    }
    assert _inheritance_problems(broken) == [
        "d: parent chain ends at non-partition g",
        "a: parent missing is not inventoried",
    ]


def test_entry_contract_rejects_malformed_entries():
    with pytest.raises(ValueError):
        TenancyEntry("derived", "no parent")
    with pytest.raises(ValueError):
        TenancyEntry("accelerator", "no parent")
    with pytest.raises(ValueError):
        TenancyEntry("global", "has parent", parent="tasks", via="task_id")
    with pytest.raises(ValueError):
        TenancyEntry("shared", "unknown kind")
    with pytest.raises(KeyError):
        tenant_tables.kind_of("no_such_table")
