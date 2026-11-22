"""Every write of ``tasks.updated_at`` stamps ``storage.next_revision`` (task #2061).

``tasks.updated_at`` is the optimistic-concurrency revision that
``task_update(expected_updated_at=...)`` checks and the completion-return key
(task #2047). PR #1186 moved every close onto ``next_revision`` (microsecond,
strictly increasing). The reopen and status-change paths still stamped
second-precision ``storage.now()``, so a close followed by a reopen in the same
second wrote a revision that sorted *before* the close's.

Two layers here: a source tripwire that fails when any ``UPDATE tasks`` sets
``updated_at`` from anything but ``next_revision``, or computes it from a read
that is not fenced with the write; and behavioural tests that close (or claim)
then reopen within one pinned instant, or race a second connection between a
site's revision read and its write.

Task #2087 closes the other half: an ``UPDATE tasks`` that changes the row
(notes, claim, cancel flag...) without writing ``updated_at`` at all let a
caller holding the old revision pass ``expected_updated_at`` after the change.
The scan now flags every such site unless it shares a transaction with a
revision-stamping write or is listed as revision-neutral, with a reason.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unified_agent import agent_lifecycle, merge, queue_hygiene, routines, run_records, storage
from unified_agent.tools import doctor, tasks

SRC = Path(__file__).resolve().parents[1] / "src" / "unified_agent"

# Callables that return a strictly newer revision. ``_next_task_revision`` is the
# tools/tasks.py alias of storage.next_revision; ``next_task_revision`` reads the
# row's current revision on the caller's connection first.
_REVISION_CALLS = frozenset({"next_revision", "_next_task_revision", "next_task_revision"})

# A site key (task #2092): (path relative to src/unified_agent, qualified scope
# of the enclosing function, e.g. ``complete_direct.write`` or
# ``ExecutorDaemon.renew_lock``, its sorted SET columns, the 1-based occurrence
# of that signature in that scope in source order).
SiteKey = tuple[str, str, str, int]

# Every map below is keyed per site, by ``SiteKey``: (path, qualified scope, the
# sorted SET columns, 1-based occurrence of that signature in that scope in
# source order). The qualified scope (task #2092) keeps two same-named nested
# callbacks apart (run_records.py has a ``def write`` inside each of several
# functions); the occurrence makes each entry one site, so a new write with the
# same columns in a listed scope is a new, unlisted key. Each entry must match
# exactly one real site of its kind, or the scan fails it as stale.

# Stamped sites the scan cannot check by itself; every check is skipped.
_ALLOWLIST: dict[SiteKey, str] = {
    # The SET clause is assembled at runtime (f"UPDATE tasks SET {', '.join(sets)}"),
    # so the placeholder cannot be located statically. sets starts with
    # "updated_at=?" and params[0] is overwritten with
    # _next_task_revision(old["updated_at"]) under BEGIN IMMEDIATE before the
    # write; test_task_update_dynamic_set_stamps_next_revision pins that line.
    ("tools/tasks.py", "task_update", "", 1): "dynamic SET clause; params[0] pinned separately",
}

# Fence (task #2061 review): a revision computed from a read must be written
# atomically with that read, or two writers that read the same revision stamp
# the same next one. A site is fenced when the value comes from
# ``storage.next_task_revision`` (it takes the writer lock before reading), the
# UPDATE compares-and-sets ``updated_at`` in its WHERE clause, or the enclosing
# function takes ``BEGIN IMMEDIATE`` / ``storage.begin_task_write`` earlier.
# Otherwise the site must be listed here with the caller that owns the writer
# transaction it runs in.
_FENCE_ALLOWLIST: dict[SiteKey, str] = {
    # queue_hygiene.run_hygiene opens BEGIN IMMEDIATE on conn and passes it to
    # each pass; the rows these read were selected under that writer lock.
    (
        "queue_hygiene.py",
        "_demote_aging_open",
        "notes,priority,updated_at",
        1,
    ): "caller run_hygiene holds BEGIN IMMEDIATE",
    (
        "queue_hygiene.py",
        "_archive_old_done",
        "archived_at,notes,updated_at",
        1,
    ): "caller run_hygiene holds BEGIN IMMEDIATE",
    (
        "queue_hygiene.py",
        "_archive_old_done",
        "archived_at,updated_at",
        1,
    ): "caller run_hygiene holds BEGIN IMMEDIATE",
    # The WHERE clause is the f-string field {where}, which compares-and-sets
    # updated_at (plus notes, status and org) against the scanned row.
    (
        "reviewer.py",
        "_plan_approval_note",
        "notes,updated_at",
        1,
    ): "CAS on updated_at inside the {where} field",
    # Both nested write(conn) callbacks run inside run_records._tx, which opens
    # BEGIN IMMEDIATE before calling them.
    (
        "run_records.py",
        "append_scoped_direct_note.write",
        "notes,updated_at",
        1,
    ): "run_records._tx holds BEGIN IMMEDIATE",
    (
        "run_records.py",
        "complete_direct.write",
        "claimed_by,completed_at,evidence_ref,notes,status,updated_at",
        1,
    ): "run_records._tx holds BEGIN IMMEDIATE",
    # BEGIN IMMEDIATE is taken, then the row is read through the helper
    # _merge_task_fence(conn, ...), which the scan cannot follow into.
    (
        "merge.py",
        "_mission_merge_closeout",
        "completed_at,notes,status,updated_at",
        1,
    ): "reads via _merge_task_fence under its BEGIN IMMEDIATE",
    # task_hold opens BEGIN IMMEDIATE before calling _restore_in_tx.
    (
        "task_holds.py",
        "_restore_in_tx",
        "claimed_at,claimed_by,lock_expires_at,notes,status,updated_at",
        1,
    ): "caller task_hold holds BEGIN IMMEDIATE",
    # The #1818 descendant re-stamp: InvocationService.begin_mutation opened
    # BEGIN IMMEDIATE at the top of task_update, and the descendants' revisions
    # are re-read just before this loop; the scan cannot follow the helper.
    # (Before task #2092 this site rode on the function-wide _ALLOWLIST entry.)
    (
        "tools/tasks.py",
        "task_update",
        "updated_at",
        1,
    ): "begin_mutation holds BEGIN IMMEDIATE over the re-read",
}

# Task #2087: every ``UPDATE tasks`` that does not write ``updated_at`` must be
# listed in one of the two maps below. An unlisted site fails the scan: stamp
# ``storage.next_task_revision(conn, id)`` in it instead.
#
# Same transaction as a revision bump: the enclosing function also runs a
# revision-stamping ``UPDATE tasks`` on the same control-flow path and with no
# transaction end between the two, so the change commits together with a newer
# revision and no caller can see it under the old one. The scan checks: every
# conditional branch (if/else arm, loop body, except/else arm, match case)
# holding the stamping write also holds this one, and no ``commit()``,
# ``rollback()`` or known committing helper (_COMMITTING_CALLS) sits between
# them by source position. Limits: it does not follow calls into other helpers
# that might commit, does not prove both writes hit the same row, and treats an
# early ``return``/``raise`` between them as harmless. Each entry's reason
# states why the rest holds.
_SAME_TX_BUMP: dict[SiteKey, str] = {
    # Taints the subtree; the root's revision is the dynamic UPDATE above it and
    # each descendant's is stamped by the loop right after it (task #1818).
    (
        "tools/tasks.py",
        "task_update",
        "untrusted",
        1,
    ): "root and descendants re-stamped in this tx",
    # request_cancel flips the flag after the dynamic UPDATE that stamped params[0].
    (
        "tools/tasks.py",
        "task_update",
        "cancel_requested",
        1,
    ): "after the dynamic UPDATE's revision",
    # The two run-isolation rewrites (delegation suffix mismatch, then new
    # logical run) follow the dynamic UPDATE inside the same BEGIN IMMEDIATE.
    (
        "tools/tasks.py",
        "task_update",
        "branch,evidence_ref,notes,worktree_path",
        1,
    ): "delegation-suffix rewrite, after the dynamic UPDATE's revision",
    (
        "tools/tasks.py",
        "task_update",
        "branch,evidence_ref,notes,worktree_path",
        2,
    ): "new-run rewrite, after the dynamic UPDATE's revision",
    # The new-run isolation rewrite follows the claim UPDATE that stamped
    # ``revision`` and commits with it.
    (
        "tools/tasks.py",
        "task_claim",
        "branch,evidence_ref,notes,worktree_path",
        1,
    ): "after the claim UPDATE's revision",
}

# Calls that end the caller's transaction without being named commit/rollback.
_COMMITTING_CALLS = frozenset({"commit", "rollback", "commit_registered_call_on_connection"})

# Revision-neutral: bookkeeping no caller conditions a write on. A bump here
# would turn the revision a caller was just handed stale with no change it
# could act on, or (lease renewal) change what updated_at means.
_REVISION_NEUTRAL: dict[SiteKey, str] = {
    # A schema backfill of a column no reader treats as task state yet; bumping
    # the revision would invalidate every in-flight expected_updated_at (#2388).
    ("table_schemas.py", "task_ownership", "next_owner", 1): "ownership backfill",
    # renew_live_claims reads updated_at as the claim's activity signal; a bump
    # on renewal would keep every claim it renews alive forever.
    ("queue_hygiene.py", "renew_live_claims", "lock_expires_at", 1): "lease renewal",
    # The open-run cache's stamp=False statements. Only the in-transaction
    # recorders reach them: record_claim_in_tx (task_claim),
    # record_transition_in_tx (task_update, the merge closeout) and
    # complete_direct, each of which stamps the row's revision in the same
    # transaction. The paths that run in a transaction of their own
    # (record_abandoned, the standalone record_claim/record_transition and the
    # renewal backfill) pass stamp=True and take the stamped statement, which
    # the scan checks as a revision write (task #2087 review).
    (
        "run_records.py",
        "_bind_current_run",
        "cancel_requested,current_run_agent,current_run_id",
        1,
    ): "stamp=False: only inside a caller tx that stamps the revision",
    (
        "run_records.py",
        "_unbind_current_run",
        "cancel_requested,current_run_agent,current_run_id",
        1,
    ): "stamp=False: only inside a caller tx that stamps the revision",
    # Post-init hook after storage.init_db: fills the cache for 'doing' rows
    # claimed before the columns existed, before any caller holds a revision
    # of the new columns.
    (
        "run_records.py",
        "backfill_open_run_cache",
        "current_run_agent,current_run_id",
        1,
    ): "post-init backfill of the run-ledger cache",
    # Schema migration: rewrites legacy lane names before the store serves.
    ("storage.py", "_migrate", "lane", 1): "schema migration, legacy lane names",
    ("storage.py", "_migrate", "lane", 2): "schema migration, operator-only lane",
    # Hub checkout bookkeeping. Provisioning binds the checkout of the claim
    # just admitted (fenced on current_run_id, claimed_by and status); the
    # releases clear it after a durable close under a CAS on updated_at, so
    # they never land on a newer revision. Bumping would stale the revision
    # the claim or close just returned.
    (
        "tools/tasks.py",
        "_ensure_task_worktree",
        "branch,worktree_path",
        1,
    ): "checkout bind, fenced on run/claimant",
    (
        "tools/tasks.py",
        "task_update",
        "branch,worktree_path",
        1,
    ): "post-close release, CAS on revision",
    (
        "queue_closure.py",
        "_release_closed_worktree",
        "branch,worktree_path",
        1,
    ): "post-close release, CAS on revision",
}

# INSERT INTO tasks is out of scope: a new row has no earlier revision to collide
# with, so its creation stamp is the first revision.

_UPDATE_TASKS = re.compile(r"^\s*UPDATE\s+tasks\b", re.IGNORECASE)
_SET_CLAUSE = re.compile(r"\bSET\b(?P<set>.*?)(?:\bWHERE\b|$)", re.IGNORECASE | re.DOTALL)
_UPDATED_AT = re.compile(r"\bupdated_at\s*=\s*(?P<value>\S)", re.IGNORECASE)
_CAS_UPDATED_AT = re.compile(r"\bupdated_at\s*(?:=|IS)\s*\?", re.IGNORECASE)
# A SET target: a column name at the start of the clause or after a comma (so
# ``current_run_id=?`` inside a CASE WHEN is not mistaken for one).
_SET_COLUMN = re.compile(r"(?:^|,)\s*(\w+)\s*=", re.DOTALL)


def _set_columns(set_clause: str) -> str:
    return ",".join(sorted(set(_SET_COLUMN.findall(set_clause.strip()))))


def _sql_text(node: ast.AST) -> str | None:
    """Literal SQL text, with ``{}`` standing in for each f-string field."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _sql_text(node.left), _sql_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _call_name(node: ast.AST | None) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _is_revision_value(node: ast.AST, scope: ast.AST, names=_REVISION_CALLS) -> bool:
    """A call to one of ``names``, or a local name every assignment of which is one."""
    if _call_name(node) in names:
        return True
    if isinstance(node, ast.Name):
        values = [
            a.value
            for a in ast.walk(scope)
            if isinstance(a, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == node.id for t in a.targets)
        ]
        return bool(values) and all(_call_name(v) in names for v in values)
    return False


_TASK_READ = re.compile(
    r"^\s*SELECT\b(?=.*(?:\bupdated_at\b|\*)).*\bFROM\s+tasks\b", re.IGNORECASE | re.DOTALL
)


def _execute_sql(call: ast.Call, scope: ast.AST) -> str | None:
    """SQL of an ``execute`` call: a literal, or a local name bound to one."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Name):
        for a in ast.walk(scope):
            if isinstance(a, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == arg.id for t in a.targets
            ):
                text = _sql_text(a.value)
                if text is not None:
                    return text
        return None
    return _sql_text(arg)


def _exits_after(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """The statement holding ``node`` is followed in its block by raise/return,
    so control never reaches a later write after this release."""
    stmt = node
    while stmt in parents and not isinstance(stmt, ast.stmt):
        stmt = parents[stmt]
    block = parents.get(stmt)
    for field in ("body", "orelse", "finalbody", "handlers"):
        body = getattr(block, field, None)
        if isinstance(body, list) and stmt in body:
            return any(isinstance(s, ast.Raise | ast.Return) for s in body[body.index(stmt) :])
    return False


def _lock_fences_read(scope: ast.AST, lineno: int, parents: dict[ast.AST, ast.AST]) -> bool:
    """The writer lock is taken before the revision read and held to the write.

    The read is the last ``SELECT ... updated_at|* ... FROM tasks`` in the
    function before the UPDATE at ``lineno`` (a re-read under the lock, as
    doctor does, supersedes an earlier unlocked one). Fenced only when a
    ``begin_task_write(...)`` or ``execute("BEGIN IMMEDIATE")`` precedes that
    read by source position and no ``commit()``/``rollback()`` releases it
    between the read and the write (a release on a path that then raises or
    returns does not count). A row read elsewhere (no read in this function) is
    not fenced by a lock taken here."""
    reads, locks, releases = [], [], []
    for n in ast.walk(scope):
        if not isinstance(n, ast.Call) or n.lineno >= lineno:
            continue
        name = _call_name(n)
        if name == "begin_task_write":
            locks.append(n.lineno)
        elif name in {"commit", "rollback"} and not _exits_after(n, parents):
            releases.append(n.lineno)
        elif name == "execute":
            sql = _execute_sql(n, scope)
            if sql and re.match(r"\s*BEGIN\s+IMMEDIATE\b", sql, re.IGNORECASE):
                locks.append(n.lineno)
            elif sql and _TASK_READ.match(sql):
                reads.append(n.lineno)
    if not reads:
        return False
    read = max(reads)
    return any(lock < read for lock in locks) and not any(
        read < release < lineno for release in releases
    )


# A lambda or generator expression runs when it is called or iterated, which can
# be after the enclosing function committed, so it is its own scope: a write in
# it never shares the outer function's key, allowlist entry or revision bump.
# A class body is a scope too, so a site in it keeps the class name. Two
# same-named sibling functions in one scope are told apart only by occurrence;
# that residual is inherent to keying on a signature (Devin, PR #1219).
_SCOPE_NODES = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda | ast.GeneratorExp


def _enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST], tree: ast.AST) -> ast.AST:
    cur = parents.get(node)
    while cur is not None and not isinstance(cur, _SCOPE_NODES):
        cur = parents.get(cur)
    return cur or tree


def _releases(scope: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[int]:
    """Lines of calls in ``scope`` that end the transaction for code after them
    (``_COMMITTING_CALLS``; not ones on a path that then raises or returns)."""
    return [
        n.lineno
        for n in ast.walk(scope)
        if isinstance(n, ast.Call)
        and _call_name(n) in _COMMITTING_CALLS
        and not _exits_after(n, parents)
    ]


def _branches(node: ast.AST, scope: ast.AST, parents: dict[ast.AST, ast.AST]) -> set:
    """The conditional arms between ``node`` and its function: (owner, arm) for
    each if/else arm, loop body or else, except handler, try-else and match case
    it sits in. A try body, finally, or with block is not conditional."""
    arms = set()
    child, cur = node, parents.get(node)
    while cur is not None and child is not scope:
        if isinstance(cur, ast.If | ast.For | ast.AsyncFor | ast.While | ast.IfExp):
            for field in ("body", "orelse"):
                value = getattr(cur, field)
                if child is value or (isinstance(value, list) and child in value):
                    arms.add((id(cur), field))
        elif isinstance(cur, ast.Try | ast.TryStar):
            if child in cur.handlers:
                arms.add((id(cur), f"handler{cur.handlers.index(child)}"))
            elif child in cur.orelse:
                arms.add((id(cur), "orelse"))
        elif isinstance(cur, ast.Match) and child in cur.cases:
            arms.add((id(cur), f"case{cur.cases.index(child)}"))
        elif isinstance(cur, ast.BoolOp) and child is not cur.values[0]:
            arms.add((id(cur), f"value{cur.values.index(child)}"))
        child, cur = cur, parents.get(cur)
    return arms


def _shares_bump_tx(
    node: ast.AST,
    stamped: list[ast.AST],
    scope: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    """A revision-stamping write in the same function that runs whenever this
    one does (each of its conditional arms also holds this write) with no
    transaction end between the two by source position."""
    releases = _releases(scope, parents)
    arms = _branches(node, scope, parents)
    for bump in stamped:
        low, high = sorted((bump.lineno, node.lineno))
        if any(low < r < high for r in releases):
            continue
        if _branches(bump, scope, parents) <= arms:
            return True
    return False


class _ScopeNames(ast.NodeVisitor):
    """Qualified scope path of every function: ``outer.inner``, class names
    included (``Store.save.write``), from a stack kept while visiting (task
    #2092). A bare name let two nested ``def write`` callbacks in different
    outer functions share one key."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.names: dict[ast.AST, str] = {}
        self._anon: dict[tuple[str, ...], int] = {}

    def _scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.names[node] = ".".join(self.stack)
        self.generic_visit(node)
        self.stack.pop()

    def _anonymous(self, node: ast.Lambda | ast.GeneratorExp) -> None:
        # ``outer.<lambda:2>``: numbered per enclosing scope in visit order,
        # so two lambdas in one function are two scopes.
        kind = "lambda" if isinstance(node, ast.Lambda) else "genexpr"
        slot = (*self.stack, kind)
        self._anon[slot] = self._anon.get(slot, 0) + 1
        self.stack.append(f"<{kind}:{self._anon[slot]}>")
        self.names[node] = ".".join(self.stack)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope
    visit_Lambda = visit_GeneratorExp = _anonymous


def _scan(
    root: Path = SRC,
    allowlist: dict[SiteKey, str] | None = None,
    fence: dict[SiteKey, str] | None = None,
    same_tx: dict[SiteKey, str] | None = None,
    neutral: dict[SiteKey, str] | None = None,
) -> tuple[list[SiteKey], list[str], list[SiteKey]]:
    """Return (checked sites, violations, unstamped sites) over every
    ``UPDATE tasks`` in src/. A checked site writes ``updated_at``; an
    unstamped one does not. Both are keyed (path, qualified scope, SET columns,
    occurrence). An entry in any map that matches no site of its kind is a
    violation too."""
    allowlist = _ALLOWLIST if allowlist is None else allowlist
    fence = _FENCE_ALLOWLIST if fence is None else fence
    same_tx = _SAME_TX_BUMP if same_tx is None else same_tx
    neutral = _REVISION_NEUTRAL if neutral is None else neutral
    checked: list[SiteKey] = []
    violations: list[str] = []
    unstamped: list[SiteKey] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        scopes = _ScopeNames()
        scopes.visit(tree)
        sites: list[tuple[ast.AST, str]] = []
        for node in ast.walk(tree):
            if isinstance(parents.get(node), ast.JoinedStr | ast.BinOp):
                continue  # a fragment; its outermost string is scanned instead
            sql = _sql_text(node)
            if sql is not None and _UPDATE_TASKS.match(sql):
                sites.append((node, sql))
        stamped: dict[ast.AST, list[ast.AST]] = {}
        pending: list[tuple[SiteKey, ast.AST, ast.AST, str]] = []
        seen: dict[tuple[str, str], int] = {}

        # Source order, so occurrence N is the Nth site of its signature in its scope.
        for node, sql in sorted(sites, key=lambda s: (s[0].lineno, s[0].col_offset)):
            set_match = _SET_CLAUSE.search(sql)
            set_clause = set_match.group("set") if set_match else ""
            scope = _enclosing(node, parents, tree)
            qual = scopes.names.get(scope, "<module>")
            cols = _set_columns(set_clause)
            occurrence = seen[(qual, cols)] = seen.get((qual, cols), 0) + 1
            key = (rel, qual, cols, occurrence)
            where = f"{rel}:{node.lineno} ({qual})"
            dynamic = "{}" in set_clause
            col = _UPDATED_AT.search(set_clause)
            if col is None and not dynamic:
                # Task #2087: a state change with no revision bump; resolved
                # against the maps once every stamping site here is known.
                unstamped.append(key)
                pending.append((key, scope, node, where))
                continue
            checked.append(key)
            stamped.setdefault(scope, []).append(node)
            if key in allowlist:
                continue
            if dynamic:
                violations.append(f"{where}: dynamic SET clause; cannot verify updated_at")
                continue
            if col.group("value") != "?":
                violations.append(f"{where}: updated_at set from a SQL expression")
                continue
            prefix = sql[: set_match.start("set") + col.start("value")]
            if "{}" in prefix:
                violations.append(f"{where}: f-string field before the updated_at placeholder")
                continue
            index = prefix.count("?")
            call = parents.get(node)
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in {"execute", "executemany"}
                and call.args
                and call.args[0] is node
                and len(call.args) > 1
                and isinstance(call.args[1], ast.Tuple | ast.List)
            ):
                violations.append(f"{where}: SQL not passed inline to execute() with a tuple")
                continue
            params = call.args[1].elts
            if any(isinstance(p, ast.Starred) for p in params[: index + 1]) or index >= len(params):
                violations.append(f"{where}: cannot locate the updated_at parameter")
                continue
            if not _is_revision_value(params[index], scope):
                violations.append(
                    f"{where}: updated_at bound to {ast.unparse(params[index])!r}, "
                    "not storage.next_revision(...)"
                )
                continue
            fenced = (
                _is_revision_value(params[index], scope, {"next_task_revision"})
                or _CAS_UPDATED_AT.search(sql[set_match.end("set") :]) is not None
                or _lock_fences_read(scope, node.lineno, parents)
            )
            if not fenced and key not in fence:
                violations.append(
                    f"{where}: revision read is not fenced with the write (no "
                    "next_task_revision, updated_at CAS, or BEGIN IMMEDIATE before the read)"
                )

        for key, scope, node, where in pending:
            cols, occurrence = key[2], key[3]
            if key in neutral:
                continue
            if key not in same_tx:
                violations.append(
                    f"{where}: UPDATE tasks SET {cols} (occurrence {occurrence}) does not "
                    "write updated_at; stamp "
                    "storage.next_task_revision(conn, id) or list it in _SAME_TX_BUMP / "
                    "_REVISION_NEUTRAL with a reason"
                )
            elif not _shares_bump_tx(node, stamped.get(scope, []), scope, parents):
                violations.append(
                    f"{where}: listed in _SAME_TX_BUMP, but no revision-stamping UPDATE "
                    "tasks in the function shares its transaction and control-flow path"
                )

    # Task #2092: an entry must name one real site of its kind. A stale one
    # (writer removed, renamed or moved to another scope) would otherwise wait
    # to cover whatever site next takes its key.
    for label, table, sites_of_kind in (
        ("_ALLOWLIST", allowlist, set(checked)),
        ("_FENCE_ALLOWLIST", fence, set(checked)),
        ("_SAME_TX_BUMP", same_tx, set(unstamped)),
        ("_REVISION_NEUTRAL", neutral, set(unstamped)),
    ):
        for key in sorted(set(table) - sites_of_kind):
            violations.append(
                f"stale {label} entry {key!r}: no UPDATE tasks site has this key; "
                "drop it or re-key it to the site it is for"
            )
    return checked, violations, unstamped


def _scan_cases(pkg: Path, **maps: dict[SiteKey, str]):
    """Scan a self-test package with only the given map entries (the real
    ones would all be stale there)."""
    for table in ("allowlist", "fence", "same_tx", "neutral"):
        maps.setdefault(table, {})
    return _scan(pkg, **maps)


def test_every_tasks_updated_at_write_uses_next_revision():
    checked, violations, _ = _scan()
    # Guard the scanner itself: if parsing silently stops matching, this fails
    # instead of passing vacuously.
    assert len(checked) >= 25, checked
    assert violations == [], "\n".join(violations)


def test_allowlist_has_no_stale_entries():
    """Every entry names exactly one real site of its kind. Keys are unique per
    site by construction (the occurrence), so a match is exactly one; _scan
    reports an unmatched entry as stale (task #2092)."""
    checked, violations, unstamped = _scan()
    assert len(set(checked)) == len(checked) and len(set(unstamped)) == len(unstamped)
    assert [v for v in violations if v.startswith("stale ")] == []
    assert set(_ALLOWLIST) | set(_FENCE_ALLOWLIST) <= set(checked)
    assert set(_SAME_TX_BUMP) | set(_REVISION_NEUTRAL) <= set(unstamped)
    assert not set(_SAME_TX_BUMP) & set(_REVISION_NEUTRAL)
    assert not set(_ALLOWLIST) & set(_FENCE_ALLOWLIST)


def test_task_update_dynamic_set_stamps_next_revision(hub, monkeypatch):
    """task_update assembles its SET clause at runtime, so the scan allowlists it.
    Pin the behaviour instead: with the clock frozen at the row's revision, every
    update still stamps a strictly newer, microsecond revision of the row it read."""
    task_id = tasks.task_add(title="dynamic set")["id"]
    created = _row(task_id)["updated_at"]
    _pin_storage_clock(monkeypatch, _rev(created))

    tasks.task_update(task_id, notes="first edit")
    first = _row(task_id)
    tasks.task_update(task_id, title="second edit")
    second = _row(task_id)

    assert first["notes"] == "first edit" and second["title"] == "second edit"
    assert _rev(first["updated_at"]) == _rev(created) + timedelta(microseconds=1)
    assert _rev(second["updated_at"]) == _rev(first["updated_at"]) + timedelta(microseconds=1)


_READ = (
    "    row = conn.execute('SELECT notes, updated_at FROM tasks WHERE id=?', (i,)).fetchone()\n"
)
_WRITE = (
    "    rev = storage.next_revision(row['updated_at'])\n"
    "    conn.execute('UPDATE tasks SET updated_at=? WHERE id=?', (rev, i))\n"
)
_LOCK = "    storage.begin_task_write(conn)\n"
_SCANNER_CASES = {
    # name: (body, expected violation substring or None when the site is safe)
    "seconds": (
        "    conn.execute('UPDATE tasks SET updated_at=? WHERE id=?', (storage.now(), i))\n",
        "not storage.next_revision",
    ),
    "unfenced": (_READ + _WRITE, "not fenced"),
    "locked": (_LOCK + _READ + _WRITE, None),
    "immediate": ("    conn.execute('BEGIN IMMEDIATE')\n" + _READ + _WRITE, None),
    # Task #2061 round-2 review: a lock taken after the read leaves the gap
    # between the SELECT and BEGIN IMMEDIATE open to a competing commit.
    "lock_after_read": (_READ + _LOCK + _WRITE, "not fenced"),
    # A commit between the read and the write releases the lock it relied on.
    "released": (_LOCK + _READ + "    conn.commit()\n" + _WRITE, "not fenced"),
    # A row read by some other function is not fenced by a lock taken here.
    # A rollback on a path that then raises never reaches the write.
    "rollback_then_raise": (
        _LOCK
        + _READ
        + "    if not row:\n        conn.rollback()\n        raise ValueError(i)\n"
        + _WRITE,
        None,
    ),
    "row_from_elsewhere": (
        _LOCK
        + "    rev = storage.next_revision(row['updated_at'])\n"
        + "    conn.execute('UPDATE tasks SET updated_at=? WHERE id=?', (rev, i))\n",
        "not fenced",
    ),
    # A locked re-read supersedes an earlier unlocked one (the doctor shape).
    "relocked_reread": (
        "    sql = 'SELECT id, updated_at FROM tasks WHERE id=?'\n"
        "    row = conn.execute(sql, (i,)).fetchone()\n"
        + _LOCK
        + "    row = conn.execute(sql, (i,)).fetchone()\n"
        + _WRITE,
        None,
    ),
    "cas": (
        "    rev = storage.next_revision(row['updated_at'])\n"
        "    conn.execute(\n"
        "        'UPDATE tasks SET updated_at=? WHERE id=? AND updated_at=?',\n"
        "        (rev, i, row['updated_at']),\n"
        "    )\n",
        None,
    ),
    "helper": (
        "    conn.execute(\n"
        "        'UPDATE tasks SET updated_at=? WHERE id=?',\n"
        "        (storage.next_task_revision(conn, i), i),\n"
        "    )\n",
        None,
    ),
}


def test_scanner_flags_unsafe_revision_writes(tmp_path):
    """The tripwire itself catches the regressions it exists for: a
    seconds-precision stamp, a revision read with no lock, a lock taken after
    the read, and a lock released before the write."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "cases.py").write_text(
        "".join(
            f"def {name}(conn, storage, row, i):\n{body}"
            for name, (body, _) in _SCANNER_CASES.items()
        ),
        encoding="utf-8",
    )
    checked, violations, _ = _scan_cases(pkg)
    assert {func for _, func, _, _ in checked} == set(_SCANNER_CASES)
    flagged = {}
    for v in violations:
        func = v.split("(", 1)[1].split(")", 1)[0]
        flagged[func] = v
    expected = {name: want for name, (_, want) in _SCANNER_CASES.items() if want}
    assert set(flagged) == set(expected), violations
    for name, want in expected.items():
        assert want in flagged[name], flagged[name]


_NOTES = "    conn.execute('UPDATE tasks SET notes=? WHERE id=?', (n, i))\n"
_UNSTAMPED_CASES = {
    # name: (body, expected violation substring or None when the site is safe)
    # Task #2087: the bare notes write a caller's stale revision passed over.
    "notes_only": (_NOTES, "does not write updated_at"),
    "notes_after_bump": (_LOCK + _READ + _WRITE + _NOTES, None),
    "notes_before_bump": (_LOCK + _NOTES + _READ + _WRITE, None),
    # The merge.py shape: the bump committed, then a later transaction wrote notes.
    "notes_after_commit": (_LOCK + _READ + _WRITE + "    conn.commit()\n" + _NOTES, "shares"),
    # Listed as same-transaction, but the function never stamps a revision.
    "listed_without_bump": (_NOTES, "shares"),
    # The bump sits on the other arm of an if: the notes write runs without it.
    "bump_on_other_branch": (
        _LOCK
        + _READ
        + "    if n:\n"
        + _WRITE.replace("    ", "        ")
        + "    else:\n    "
        + _NOTES,
        "shares",
    ),
    # A conditional bump does not cover an unconditional notes write.
    "bump_only_if": (
        _LOCK + _READ + "    if n:\n" + _WRITE.replace("    ", "        ") + _NOTES,
        "shares",
    ),
    # An unconditional bump covers a notes write on a branch.
    "notes_only_if": (_LOCK + _READ + _WRITE + "    if n:\n    " + _NOTES, None),
    # A helper that commits ends the bump's transaction too.
    "committing_helper": (
        _LOCK
        + _READ
        + _WRITE
        + "    gateway.commit_registered_call_on_connection(conn, 't', {})\n"
        + _NOTES,
        "shares",
    ),
    # A second notes write in a listed function is its own, unlisted occurrence.
    "second_occurrence": (_LOCK + _READ + _WRITE + _NOTES + _NOTES, "occurrence 2"),
    "lease": ("    conn.execute('UPDATE tasks SET lock_expires_at=? WHERE id=?', (n, i))\n", None),
}


def test_scanner_flags_task_writes_that_skip_the_revision(tmp_path):
    """Task #2087: an ``UPDATE tasks`` that writes no ``updated_at`` fails
    unless it shares a transaction with a stamping write (and is listed as
    such) or is listed as revision-neutral."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "cases.py").write_text(
        "".join(
            f"def {name}(conn, storage, gateway, row, i, n):\n{body}"
            for name, (body, _) in _UNSTAMPED_CASES.items()
        ),
        encoding="utf-8",
    )
    same_tx = {
        ("cases.py", name, "notes", 1): "test"
        for name in _UNSTAMPED_CASES
        if name not in {"notes_only", "lease"}
    }
    neutral = {("cases.py", "lease", "lock_expires_at", 1): "test"}
    _, violations, unstamped = _scan_cases(pkg, same_tx=same_tx, neutral=neutral)
    assert {func for _, func, _, _ in unstamped} == set(_UNSTAMPED_CASES)
    flagged = {v.split("(", 1)[1].split(")", 1)[0]: v for v in violations}
    expected = {name: want for name, (_, want) in _UNSTAMPED_CASES.items() if want}
    assert set(flagged) == set(expected), violations
    for name, want in expected.items():
        assert want in flagged[name], flagged[name]


# Task #2092: two same-named nested callbacks in different outer functions (the
# run_records.py ``def write`` shape), plus a method, so keys carry the class.
_NESTED_CASES = (
    "def outer_a(conn, n, i):\n"
    "    def write(conn):\n"
    "        conn.execute('UPDATE tasks SET notes=? WHERE id=?', (n, i))\n"
    "    return write\n"
    "def outer_b(conn, n, i):\n"
    "    def write(conn):\n"
    "        conn.execute('UPDATE tasks SET notes=? WHERE id=?', (n, i))\n"
    "    return write\n"
    "class Store:\n"
    "    def save(self, conn, n, i):\n"
    "        def write(conn):\n"
    "            conn.execute('UPDATE tasks SET notes=? WHERE id=?', (n, i))\n"
    "        return write\n"
)


def _nested_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "cases.py").write_text(_NESTED_CASES, encoding="utf-8")
    return pkg


def test_scanner_keys_same_named_nested_functions_by_qualified_scope(tmp_path):
    """Task #2092: a key on the bare name gave both nested ``write`` callbacks
    one (path, "write", cols) signature, told apart only by occurrence, so
    deleting one and adding another inherited its entry."""
    _, _, unstamped = _scan_cases(_nested_pkg(tmp_path))
    assert sorted(unstamped) == [
        ("cases.py", "Store.save.write", "notes", 1),
        ("cases.py", "outer_a.write", "notes", 1),
        ("cases.py", "outer_b.write", "notes", 1),
    ]


def test_scanner_entry_for_one_nested_function_does_not_cover_another(tmp_path):
    neutral = {
        ("cases.py", "outer_a.write", "notes", 1): "test",
        ("cases.py", "Store.save.write", "notes", 1): "test",
    }
    _, violations, _ = _scan_cases(_nested_pkg(tmp_path), neutral=neutral)
    assert len(violations) == 1, violations
    assert "(outer_b.write)" in violations[0] and "does not write updated_at" in violations[0]


@pytest.mark.parametrize("table", ["allowlist", "fence", "same_tx", "neutral"])
def test_scanner_fails_a_stale_entry(tmp_path, table):
    """An entry that matches no real site fails the scan, in every map: a
    removed or renamed writer must drop its entry, not leave it to cover the
    next site that happens to take its key."""
    key = {
        "allowlist": ("cases.py", "outer_c.write", "notes,updated_at", 1),
        "fence": ("cases.py", "outer_c.write", "notes,updated_at", 1),
        "same_tx": ("cases.py", "outer_c.write", "notes", 1),
        "neutral": ("cases.py", "outer_c.write", "notes", 1),
    }[table]
    maps = {
        "allowlist": {},
        "fence": {},
        "same_tx": {},
        "neutral": {
            ("cases.py", "outer_a.write", "notes", 1): "test",
            ("cases.py", "outer_b.write", "notes", 1): "test",
            ("cases.py", "Store.save.write", "notes", 1): "test",
        },
    }
    maps[table] = {**maps[table], key: "stale"}
    _, violations, _ = _scan_cases(_nested_pkg(tmp_path), **maps)
    assert len(violations) == 1, violations
    assert "stale" in violations[0] and repr(key) in violations[0], violations


def test_scanner_fails_an_occurrence_past_the_last_site(tmp_path):
    """Occurrence 2 of a signature with one site is stale, not a spare slot."""
    neutral = {
        ("cases.py", "outer_a.write", "notes", 1): "test",
        ("cases.py", "outer_a.write", "notes", 2): "test",
        ("cases.py", "outer_b.write", "notes", 1): "test",
        ("cases.py", "Store.save.write", "notes", 1): "test",
    }
    _, violations, _ = _scan_cases(_nested_pkg(tmp_path), neutral=neutral)
    assert len(violations) == 1 and "stale" in violations[0], violations


def test_set_columns_ignores_comparisons_inside_case():
    assert (
        _set_columns(
            " current_run_id=?, current_run_agent=?, "
            "cancel_requested=CASE WHEN current_run_id=? THEN cancel_requested ELSE 0 END "
        )
        == "cancel_requested,current_run_agent,current_run_id"
    )


# ── behaviour: a reopen in the same instant as the close ─────────────────────


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.delenv("UA_PUBLIC_URL", raising=False)
    storage.init_db()


def _pin_storage_clock(monkeypatch, instant: datetime) -> None:
    """Freeze the clock storage.now and storage.next_revision read, so every
    transition lands in one instant (the same pattern as test_completion_return)."""

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(storage, "datetime", _Pinned)


def _row(task_id: int) -> dict:
    conn = storage.connect()
    try:
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    finally:
        conn.close()


def _rev(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _close(task_id: int) -> dict:
    expected = run_records.capture_direct_completion(task_id)
    assert run_records.complete_direct(expected, actor="merge-orchestrator", note="merged")
    row = _row(task_id)
    assert row["status"] == "done"
    return row


def _expire_lock(task_id: int) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (task_id,)
        )
        conn.commit()
    finally:
        conn.close()


def test_merge_reopen_after_close_in_the_same_second_advances_the_revision(hub, monkeypatch):
    task_id = tasks.task_add(title="merged, cleanup fails")["id"]
    _pin_storage_clock(monkeypatch, datetime.now(UTC))
    closed = _close(task_id)
    monkeypatch.setattr(merge, "_record_local_merge", lambda *a, **k: None)
    context = {"task_claimant": closed["claimed_by"], "current_run_id": closed["current_run_id"]}
    conn = storage.connect()
    try:
        merge._reopen_after_failed_cleanup(
            conn, task_id, "merge-orchestrator", context, None, "abc123", "cleanup failed"
        )
    finally:
        conn.close()
    reopened = _row(task_id)
    assert reopened["status"] == "waiting_review"
    assert _rev(reopened["updated_at"]) > _rev(closed["updated_at"])


def test_merge_retain_after_close_in_the_same_second_advances_the_revision(hub, monkeypatch):
    task_id = tasks.task_add(title="merged, retained")["id"]
    _pin_storage_clock(monkeypatch, datetime.now(UTC))
    closed = _close(task_id)
    context = {
        "head": "abc123",
        "current_run_id": closed["current_run_id"],
        "task_status": "done",
        "task_claimant": closed["claimed_by"],
    }
    conn = storage.connect()
    try:
        merge._retain_merge_work(conn, task_id, context, closed, "abc123", "cleanup failed")
        conn.commit()
    finally:
        conn.close()
    retained = _row(task_id)
    assert retained["status"] == "waiting_review"
    assert _rev(retained["updated_at"]) > _rev(closed["updated_at"])


def test_routine_release_after_block_in_the_same_second_advances_the_revision(hub, monkeypatch):
    task_id = tasks.task_add(title="queued routine run")["id"]
    _pin_storage_clock(monkeypatch, datetime.now(UTC))
    tasks.task_update(task_id, status="blocked", actor="tester")
    blocked = _row(task_id)
    ts = "2026-01-01T00:00:00+00:00"
    conn = storage.connect()
    try:
        rid = conn.execute(
            "INSERT INTO routines (title, trigger, schedule, lane, project, domain, "
            "concurrency, catch_up, cursor_at, created_at, updated_at) "
            "VALUES ('r', 'cron', '0 * * * *', 'default', 'p', 'd', 'queue', 'none', ?, ?, ?)",
            (ts, ts, ts),
        ).lastrowid
        conn.execute(
            "INSERT INTO routine_runs (routine_id, fire_key, scheduled_for, status, task_id, "
            "created_at) VALUES (?, 'k', ?, 'queued', ?, ?)",
            (rid, ts, task_id, ts),
        )
        conn.commit()
        assert routines._release_queued(conn, []) == [task_id]
        conn.commit()
    finally:
        conn.close()
    released = _row(task_id)
    assert released["status"] == "open"
    assert _rev(released["updated_at"]) > _rev(blocked["updated_at"])


def test_agent_lifecycle_release_after_claim_in_the_same_second(hub, monkeypatch):
    task_id = tasks.task_add(title="claimed then terminated")["id"]
    _pin_storage_clock(monkeypatch, datetime.now(UTC))
    tasks.task_claim(task_id, agent="Worker_A")
    claimed = _row(task_id)
    agent_lifecycle._release_claims("Worker_A", [task_id], "test")
    released = _row(task_id)
    assert released["status"] == "open"
    assert _rev(released["updated_at"]) > _rev(claimed["updated_at"])


def test_queue_hygiene_stale_release_after_claim_in_the_same_second(hub, monkeypatch):
    task_id = tasks.task_add(title="lapsed lease")["id"]
    _pin_storage_clock(monkeypatch, datetime.now(UTC))
    tasks.task_claim(task_id, agent="Worker_A")
    _expire_lock(task_id)
    claimed = _row(task_id)
    conn = storage.connect()
    try:
        released_ids = [r.task_id for r in queue_hygiene._reap_stale_claims(conn)]
        conn.commit()
    finally:
        conn.close()
    assert released_ids == [task_id]
    released = _row(task_id)
    assert released["status"] == "blocked"  # an open run with a gone owner: interrupted
    assert _rev(released["updated_at"]) > _rev(claimed["updated_at"])


def test_doctor_reclaim_after_claim_in_the_same_second(hub, monkeypatch):
    task_id = tasks.task_add(title="lapsed lease, doctor")["id"]
    _pin_storage_clock(monkeypatch, datetime.now(UTC))
    tasks.task_claim(task_id, agent="Worker_A")
    _expire_lock(task_id)
    claimed = _row(task_id)
    result = doctor.reclaim_stale_task_locks()
    assert [t["id"] for t in result["reclaimed_tasks"]] == [task_id]
    released = _row(task_id)
    assert released["status"] == "blocked"  # an open run with a gone owner: interrupted
    assert _rev(released["updated_at"]) > _rev(claimed["updated_at"])


# ── behaviour: a competing writer between the revision read and the write ────


def _competing_writer(monkeypatch, task_id: int) -> dict:
    """Run one competing task write the first time a site computes a revision.

    ``storage.next_revision`` is called after a site has read the current
    revision and before it writes, so this is the exact interleaving the #2061
    review reproduced. The competitor uses its own connection with no busy
    wait: if the site holds the writer lock, the competitor is refused
    ("locked"); if not, it commits a revision computed from the same read."""
    from unified_agent import config

    original = storage.next_revision
    outcome: dict = {}

    def hooked(current):
        if not outcome:
            raw = sqlite3.connect(config.db_path(), timeout=0)
            try:
                old = raw.execute("SELECT updated_at FROM tasks WHERE id=?", (task_id,)).fetchone()
                rev = original(old[0])
                raw.execute(
                    "UPDATE tasks SET notes=COALESCE(notes,'') || '\ncompetitor', updated_at=? "
                    "WHERE id=? AND updated_at=?",
                    (rev, task_id, old[0]),
                )
                raw.commit()
                outcome["committed"] = rev
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc) or "busy" in str(exc), exc
                outcome["locked"] = True
            finally:
                raw.close()
        return original(current)

    monkeypatch.setattr(storage, "next_revision", hooked)
    return outcome


# An agent label with a native headless backend, so ExecutorDaemon constructs.
_EXECUTOR = "Desktop_agent3"


def _claimed_task(title: str) -> int:
    task_id = tasks.task_add(title=title)["id"]
    tasks.task_claim(task_id, agent=_EXECUTOR, timeout_seconds=100)
    return task_id


def _site_renew_lock(task_id: int) -> None:
    from unified_agent.executor.daemon import ExecutorDaemon

    assert ExecutorDaemon(agent_label=_EXECUTOR, lock_timeout_s=3600).renew_lock(task_id)


def _site_reviewer_note(task_id: int) -> None:
    from unified_agent import reviewer

    reviewer._append_note(task_id, "[review: note]")


def _site_plan_notes(task_id: int) -> None:
    from unified_agent.tools import plan

    plan._rewrite_task_notes(task_id, lambda _notes: "rewritten", _row(task_id)["org_id"])


def _site_doctor_reclaim(task_id: int) -> None:
    _expire_lock(task_id)
    assert [t["id"] for t in doctor.reclaim_stale_task_locks()["reclaimed_tasks"]] == [task_id]


@pytest.mark.parametrize(
    "site",
    [_site_renew_lock, _site_reviewer_note, _site_plan_notes, _site_doctor_reclaim],
    ids=["executor-renew-lock", "reviewer-append-note", "plan-rewrite-notes", "doctor-reclaim"],
)
def test_a_competing_write_between_read_and_write_never_shares_a_revision(hub, monkeypatch, site):
    """Task #2061 review: with the read unfenced, a second connection committed
    revision R between the site's read and its write, and the site then also
    stamped R. A caller holding R passed expected_updated_at after a change it
    never saw. Fenced, the competitor is refused while the site holds the
    writer lock, and every committed revision is strictly newer than the last."""
    # Standalone ExecutorDaemon construction, as tests/test_executor.py sets it
    # up: a token, the direct-store opt-in, a closed hub port, the pi backend.
    monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-test")
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("UA_EXECUTOR_BACKEND", "pi")
    task_id = _claimed_task("contended row")
    _pin_storage_clock(monkeypatch, datetime.now(UTC))
    before = _row(task_id)["updated_at"]
    outcome = _competing_writer(monkeypatch, task_id)
    site(task_id)
    after = _row(task_id)["updated_at"]
    assert outcome, "the site never computed a revision"
    assert _rev(after) > _rev(before)
    if "committed" in outcome:
        assert _rev(after) > _rev(outcome["committed"]), (
            f"site reused the competitor's revision {outcome['committed']}"
        )
    assert outcome == {"locked": True}


# ── behaviour: run-cache writes in a transaction of their own (task #2087) ───


def _assert_stale_revision_refused(task_id: int, stale: str) -> None:
    from unified_agent.tool_errors import ConflictError

    with pytest.raises(ConflictError):
        tasks.task_update(task_id, append_note="stale writer", expected_updated_at=stale)


def test_record_abandoned_after_the_release_committed_advances_the_revision(hub):
    """record_abandoned runs after the release committed, in its own
    transaction, and clears current_run_id and cancel_requested. Without its
    own revision a caller holding the release's revision passed over that."""
    from unified_agent import run_records

    task_id = tasks.task_add(title="released by direct SQL")["id"]
    run_id = tasks.task_claim(task_id, agent="Worker_A")["current_run_id"]
    conn = storage.connect()
    try:
        storage.begin_task_write(conn)
        conn.execute(
            "UPDATE tasks SET status='open', claimed_by=NULL, claimed_at=NULL, "
            "lock_expires_at=NULL, cancel_requested=1, updated_at=? WHERE id=?",
            (storage.next_task_revision(conn, task_id), task_id),
        )
        conn.commit()
    finally:
        conn.close()
    released = _row(task_id)
    assert released["current_run_id"] == run_id
    assert (
        run_records.record_abandoned(
            task_id, agent="Worker_A", closed_by="tester", reason="lease lapsed"
        )
        == run_id
    )
    after = _row(task_id)
    assert after["current_run_id"] is None and after["cancel_requested"] == 0
    assert _rev(after["updated_at"]) > _rev(released["updated_at"])
    _assert_stale_revision_refused(task_id, released["updated_at"])


def test_renewal_backfill_of_the_run_cache_advances_the_revision(hub):
    """The standalone record_claim renewal fast path rebinds a stale run cache
    in a _tx of its own; that rebind carries its own revision."""
    from unified_agent import run_records

    task_id = tasks.task_add(title="cache lost its run")["id"]
    run_id = tasks.task_claim(task_id, agent="Worker_A")["current_run_id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET current_run_id=NULL, current_run_agent=NULL WHERE id=?", (task_id,)
        )
        conn.commit()
    finally:
        conn.close()
    unbound = _row(task_id)
    assert (
        run_records.record_claim(task_id, agent="Worker_A", claimed_at=storage.now(), renewal=True)
        == run_id
    )
    after = _row(task_id)
    assert after["current_run_id"] == run_id and after["current_run_agent"] == "Worker_A"
    assert _rev(after["updated_at"]) > _rev(unbound["updated_at"])
    _assert_stale_revision_refused(task_id, unbound["updated_at"])
    # A renewal whose cache already matches writes nothing, revision included.
    assert (
        run_records.record_claim(task_id, agent="Worker_A", claimed_at=storage.now(), renewal=True)
        == run_id
    )
    assert _row(task_id)["updated_at"] == after["updated_at"]


def test_scanner_treats_lambdas_and_generators_as_their_own_scope(tmp_path):
    """Codex review of PR #1219: a deferred lambda or generator expression can
    run after the enclosing function committed, so its write must not share the
    outer function's key or revision bump."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    deferred = "conn.execute('UPDATE tasks SET notes=? WHERE id=?', (n, i))"
    (pkg / "cases.py").write_text(
        "def outer(conn, storage, row, n, i):\n"
        + _LOCK
        + _READ
        + _WRITE
        + f"    first = lambda: {deferred}\n"
        + f"    second = lambda: {deferred}\n"
        + f"    rows = ({deferred} for _ in range(1))\n"
        + "    return first, second, rows\n",
        encoding="utf-8",
    )
    # The pre-fix key for the first lambda's write; it no longer names a site.
    same_tx = {("cases.py", "outer", "notes", 1): "test"}
    _, violations, unstamped = _scan_cases(pkg, same_tx=same_tx)
    assert sorted(unstamped) == [
        ("cases.py", "outer.<genexpr:1>", "notes", 1),
        ("cases.py", "outer.<lambda:1>", "notes", 1),
        ("cases.py", "outer.<lambda:2>", "notes", 1),
    ]
    assert any(v.startswith("stale ") for v in violations), violations
    # Listing the lambda's own key as same-transaction still fails: nothing in
    # the lambda stamps a revision.
    lam = {("cases.py", "outer.<lambda:1>", "notes", 1): "test"}
    _, violations, _ = _scan_cases(pkg, same_tx=lam)
    assert any("outer.<lambda:1>" in v and "stale" not in v for v in violations), violations


def test_scanner_keys_a_class_body_site_by_its_class(tmp_path):
    """Devin review of PR #1219: a site directly in a class body lost the class
    name and was keyed to the module."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "cases.py").write_text(
        "class Migrations:\n    SQL = 'UPDATE tasks SET notes=? WHERE id=?'\n",
        encoding="utf-8",
    )
    _, _, unstamped = _scan_cases(pkg)
    assert unstamped == [("cases.py", "Migrations", "notes", 1)]
