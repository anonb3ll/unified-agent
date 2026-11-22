"""Every reader of a node's stored heartbeat is accounted for (task #2196).

``nodes.last_heartbeat`` is last-seen history. After ``node_unretire`` it can
sit inside the live window while ``heartbeat_floor`` says it predates the
change, so judging it live directly makes that reader disagree with routing
(the PR #1290 review found six). Liveness goes through
``node_registry.node_live_beat``.

This inventory fails when a new mention of the raw beat appears. Route a
liveness judgement through ``node_live_beat``; add a display, history or write
here with its reason.

The scan reads the syntax tree, not lines (task #2192): every string literal
that names ``last_heartbeat`` (docstrings aside) and every ``.last_heartbeat``
attribute, so a wrapped ``.get(``, a SQL string continued over several lines
or a key held in a variable is still seen. Each is keyed by the source line
the name sits on. ``node_live_beat`` also raises on a row that carries the
beat without ``heartbeat_floor``, so a SELECT that forgets the floor fails in
any test that reaches it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from unified_agent import node_registry

SRC = Path(__file__).resolve().parents[1] / "src" / "unified_agent"

# ``last_heartbeat_at`` (agent sessions) is a different column.
_WORD = re.compile(r"\blast_heartbeat\b")

#: (file, stripped line) -> why that read is not a liveness judgement.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "node_registry.py",
        'return effective_heartbeat(row.get("last_heartbeat"), row.get("heartbeat_floor"))',
    ): "node_live_beat itself",
    ("node_registry.py", 'if row.get("last_heartbeat") is None:'): (
        "node_live_beat: no beat, nothing to fence"
    ),
    (
        "node_registry.py",
        '"node row has last_heartbeat but no heartbeat_floor; select heartbeat_floor "',
    ): "node_live_beat error message",
    ("node_registry.py", '"last_heartbeat": row["last_heartbeat"],'): "read_nodes history field",
    ("node_registry.py", 'if live is None and row.get("last_heartbeat"):'): (
        "node_status: a beat the floor fences reads offline, not unknown"
    ),
    (
        "node_registry.py",
        '"live_heartbeat": effective_heartbeat(row["last_heartbeat"], row["heartbeat_floor"]),',
    ): "read_nodes liveness field, floor applied",
    ("node_registry.py", '"last_heartbeat": row.get("last_heartbeat"),'): (
        "retire/unretire ledger payload (history)"
    ),
    (
        "host_registry.py",
        'for name, info in sorted(nodes.items(), key=lambda kv: kv[1].get("last_heartbeat") or ""):',
    ): "ordering only",
    ("host_registry.py", 'beat = info.get("last_heartbeat")'): (
        "Devices last_seen display; status uses node_live_beat"
    ),
    (
        "cli/nodes.py",
        '_LIST_FIELDS = ("node", "agent", "device_node", "version", "last_heartbeat", "acceptance")',
    ): "ua node list display column, read from GET /api/nodes",
    ("node_queue.py", 'if not node_row.get("last_heartbeat"):'): (
        "never-heartbeat reason; liveness uses node_live_beat"
    ),
    ("node_queue.py", '"last_heartbeat": wait.get("last_heartbeat"),'): "wait row display",
    ("node_queue.py", '"last_heartbeat": node_row.get("last_heartbeat"),'): (
        "beat recorded on the wait row (history)"
    ),
    (
        "node_queue.py",
        '(item_id, wait["reason"], wait["last_heartbeat"], wait["expires_at"], ts),',
    ): "wait row insert (history)",
    ("tools/registry.py", 'beat = (nodes.get(node_name) or {}).get("last_heartbeat")'): (
        "agent last_seen display; status uses node_live_beat"
    ),
    ("webui/api.py", '"last_heartbeat": info.get("last_heartbeat"),'): "display metadata",
    ("tools/doctor.py", 'return _parse_utc((nodes.get(name) or {}).get("last_heartbeat"))'): (
        "fleet_nodes 'last heartbeat ago' display; liveness uses _node_live_beat"
    ),
    ("tools/doctor.py", 'beat = info.get("last_heartbeat")'): (
        "fleet_node_acceptance day-long offline cutoff: an unretired node still counts"
    ),
    ("session_credentials.py", '"SELECT agent, device_node, last_heartbeat, heartbeat_floor "'): (
        "credential verify SELECT includes the floor"
    ),
    (
        "node_registry.py",
        '"last_heartbeat, heartbeat_floor, retired_at, retired_by, retired_reason "',
    ): "read_nodes SELECT includes the floor",
    ("node_registry.py", '"heartbeat_floor=last_heartbeat, updated_at=? WHERE node=?",'): (
        "retire/unretire write the floor"
    ),
    (
        "node_queue.py",
        '"UPDATE nodes SET last_heartbeat=?, updated_at=? WHERE node=?", (as_of, as_of, name)',
    ): "heartbeat write",
    ("node_queue.py", "last_heartbeat = excluded.last_heartbeat,"): "registration upsert write",
    ("node_queue.py", "last_heartbeat, updated_at)"): "registration insert column list",
    (
        "node_queue.py",
        '"INSERT INTO node_dispatch_wait (item_id, reason, last_heartbeat, expires_at, "',
    ): "wait row insert (history)",
    ("tools/doctor.py", '"last_heartbeat": beat.isoformat() if beat else None,'): (
        "fleet_nodes host row display"
    ),
    ("storage.py", "last_heartbeat TEXT,"): "nodes schema",
    ("table_schemas.py", "last_heartbeat TEXT,"): "node_dispatch_wait schema",
}


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def _reads() -> set[tuple[str, str]]:
    found = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text)
        docstrings = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "last_heartbeat":
                found.add((rel, lines[node.lineno - 1].strip()))
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and _WORD.search(node.value)
            ):
                for lineno in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    if _WORD.search(lines[lineno - 1]):
                        found.add((rel, lines[lineno - 1].strip()))
    return found


def test_every_raw_heartbeat_read_is_accounted_for():
    unexpected = sorted(_reads() - set(ALLOWED))
    assert not unexpected, (
        "new read of nodes.last_heartbeat; judge liveness with "
        f"node_registry.node_live_beat, or list a display read in ALLOWED: {unexpected}"
    )


def test_the_inventory_has_no_stale_entries():
    assert sorted(set(ALLOWED) - _reads()) == []


def test_the_scan_sees_wrapped_and_aliased_reads(tmp_path, monkeypatch):
    """A read split over lines or held in a variable is still an inventory entry."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "sneaky.py").write_text(
        'KEY = "last_heartbeat"\n'
        "def f(row, conn):\n"
        "    beat = row.get(\n"
        '        "last_heartbeat"\n'
        "    )\n"
        "    conn.execute(\n"
        '        "SELECT node, "\n'
        '        "last_heartbeat FROM nodes"\n'
        "    )\n"
        "    return row.last_heartbeat, row[KEY]\n"
    )
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    assert _reads() == {
        ("pkg/sneaky.py", 'KEY = "last_heartbeat"'),
        ("pkg/sneaky.py", '"last_heartbeat"'),
        ("pkg/sneaky.py", '"last_heartbeat FROM nodes"'),
        ("pkg/sneaky.py", "return row.last_heartbeat, row[KEY]"),
    }


def test_node_live_beat_refuses_a_row_without_the_floor():
    """A SELECT that forgot ``heartbeat_floor`` fails loudly, not as a live beat."""
    with pytest.raises(ValueError, match="heartbeat_floor"):
        node_registry.node_live_beat({"last_heartbeat": "2026-10-20T00:00:00+00:00"})


@pytest.mark.parametrize("row", [{}, {"last_heartbeat": None}, {"agent": "x"}])
def test_node_live_beat_without_any_beat_is_none(row):
    """No node, or a node that never beat: nothing to fence, nothing live."""
    assert node_registry.node_live_beat(row) is None


@pytest.mark.parametrize(
    ("row", "status"),
    [
        ({}, "unknown"),
        ({"last_heartbeat": None, "heartbeat_floor": None}, "unknown"),
        ({"last_heartbeat": None, "live_heartbeat": None}, "unknown"),
        ({"last_heartbeat": "2020-01-01T00:00:00+00:00", "heartbeat_floor": None}, "offline"),
        (
            {
                "last_heartbeat": "2026-10-20T00:00:00+00:00",
                "heartbeat_floor": "2026-10-20T00:00:00+00:00",
            },
            "offline",
        ),
    ],
)
def test_node_status_never_beat_is_unknown_and_fenced_is_offline(row, status):
    """A node that never beat has no status to report; a fenced one is offline."""
    assert node_registry.node_status(row) == status
