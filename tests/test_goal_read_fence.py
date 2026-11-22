"""Every SQL read of ``goals`` and ``plans`` carries a tenant fence (tasks #2004, #2042).

The cross-tenant behaviour of each read is tested where it lives
(``test_goal_tenant_fence.py``, ``test_merge_autonomy_boundary.py``,
``test_operating_profiles.py``). This tripwire catches the next read added
without one: any string literal that selects or joins ``goals`` must name
``org_id IS`` (the caller's tenant, or the task row's on merge paths).

Plans follow the same rule (#2042); their cross-tenant tests are in
``test_plan_tenant_fence.py``. ``plan_checkpoints`` and ``plan_steps``
carry no tenant: they are read after a fenced plan check, or join the plan.

``tasks`` SQL in the plan modules is fenced too (#2045): the plan_review gate
reads and notes rewrite match tasks by plan id, so each names ``org_id IS``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import unified_agent

SRC = Path(unified_agent.__file__).parent
GOAL_READ = re.compile(r"\b(FROM|JOIN)\s+goals\b", re.IGNORECASE)
PLAN_READ = re.compile(r"\b(FROM|JOIN)\s+plans\b", re.IGNORECASE)
TASK_SQL = re.compile(r"\b(FROM|JOIN|UPDATE|INTO)\s+tasks\b", re.IGNORECASE)
PLAN_MODULES = {"plan.py", "joint_planning.py"}

# (file, function) -> how many unfenced goal reads it may hold, with why. A
# count, not a blanket pass, so a second read added there still trips.
UNFENCED = {
    # One-time migration backfill: copies each goal's own tenant onto the
    # sprints bound to it, so it reads every tenant by design.
    ("operating_policy.py", "ensure_schema"): 1,
}
FENCE = re.compile(r"org_id IS|_TENANT_SQL")


def _literal(node) -> str | None:
    """The literal SQL text of a string expression, or None if it is not one.

    A constant, an f-string's constant parts, or a ``+`` chain of them; any
    other operand (a name such as ``_TENANT_SQL``, a call) counts as a space,
    so ``"SELECT * FROM " + "goals"`` is still read as one query."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is None and right is None:
            return None
        return (left or " ") + (right or " ")
    return None


def _reads_in(name: str, text: str, pattern: re.Pattern[str] = GOAL_READ):
    """Yield (file, enclosing function, line, source) for each string reading ``pattern``'s table.

    The source of the whole string expression is checked, so an f-string fence
    such as ``{_TENANT_SQL}`` (tools/tasks.py) or a ``+ _TENANT_SQL`` counts.
    Limitation: a query assembled across statements (``sql += ...``) or in a
    helper is not followed; only one expression is read at a time."""
    tree = ast.parse(text)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    parts: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            parts.update(id(v) for v in n.values)
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            parts.update((id(n.left), id(n.right)))
    for node in ast.walk(tree):
        if id(node) in parts:
            continue
        literal = _literal(node)
        if literal is None or not pattern.search(literal):
            continue
        if isinstance(node, ast.Constant):
            source = node.value
        else:
            source = ast.get_source_segment(text, node) or ""
        inner = [f for f in funcs if f.lineno <= node.lineno <= f.end_lineno]
        func = max(inner, key=lambda f: f.lineno).name if inner else "<module>"
        yield name, func, node.lineno, source


# (file, function) pairs allowed to read plans without a tenant fence, with why.
# None today: every plan read, including the governed-workflow sweep, is fenced
# on the process tenant, which is also what plan_get/plan_close enforce.
PLAN_UNFENCED: set[tuple[str, str]] = set()


def _goal_reads(pattern: re.Pattern[str] = GOAL_READ):
    for path in sorted(SRC.rglob("*.py")):
        yield from _reads_in(path.name, path.read_text(encoding="utf-8"), pattern)


def _unfenced(reads) -> list[str]:
    allowance = dict(UNFENCED)
    out = []
    for file, func, line, sql in reads:
        if FENCE.search(sql):
            continue
        if allowance.get((file, func), 0) > 0:
            allowance[(file, func)] -= 1
            continue
        out.append(f"{file}:{line} in {func}")
    return out


def test_every_goal_read_is_tenant_fenced():
    unfenced = _unfenced(_goal_reads())
    assert unfenced == [], f"goal reads without an org_id IS fence: {unfenced}"


def test_tripwire_sees_the_known_goal_reads():
    files = {file for file, *_ in _goal_reads()}
    assert {"goals.py", "merge.py", "work_graph.py", "operating_profiles.py"} <= files


def test_every_plan_read_is_tenant_fenced():
    unfenced = [
        f"{file}:{line} in {func}"
        for file, func, line, sql in _goal_reads(PLAN_READ)
        if "org_id IS" not in sql and (file, func) not in PLAN_UNFENCED
    ]
    assert unfenced == [], f"plan reads without an org_id IS fence: {unfenced}"


def test_tripwire_sees_the_known_plan_reads():
    files = {file for file, *_ in _goal_reads(PLAN_READ)}
    assert {
        "plan.py",
        "platform_readers.py",
        "task_comments.py",
        "joint_planning.py",
        "langgraph_workflow.py",
        "work_graph.py",
    } <= files


def test_every_tasks_statement_in_the_plan_modules_is_tenant_fenced():
    found = [(f, fn, ln, sql) for f, fn, ln, sql in _goal_reads(TASK_SQL) if f in PLAN_MODULES]
    assert {fn for _, fn, _, _ in found} >= {
        "_gate_tasks_for_plan",
        "_approved_tasks_for_plan",
        "_rewrite_task_notes",
    }
    unfenced = [f"{f}:{ln} in {fn}" for f, fn, ln, sql in found if "org_id IS" not in sql]
    assert unfenced == [], f"tasks SQL in plan modules without an org_id IS fence: {unfenced}"


def test_tripwire_reads_assembled_queries_and_counts_the_exemption():
    """Review #1170: a read split across a ``+`` and a second read in the exempt
    function both trip; a ``+ _TENANT_SQL`` fence still counts."""
    src = """
def f(conn):
    conn.execute("SELECT * FROM " + "goals WHERE id=?")
    conn.execute("SELECT * FROM goals WHERE " + _TENANT_SQL)

def ensure_schema(conn):
    conn.execute("SELECT id FROM goals")
    conn.execute("SELECT org_id FROM goals")
"""
    reads = list(_reads_in("operating_policy.py", src))
    assert _unfenced(reads) == [
        "operating_policy.py:3 in f",
        "operating_policy.py:8 in ensure_schema",
    ]
