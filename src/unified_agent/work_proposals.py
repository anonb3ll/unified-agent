"""Batch work proposals with frozen authority (task #2393).

``org_proposal`` covers changes to the organisation and the proposals pane
covers learnings and skill clusters. Neither lets an agent propose a *batch of
work*. This module does: an agent files a proposal of several work items, the
human operator approves or rejects each item, and every approved item becomes
one task.

Authority
    Any verified identity may file (an agent files as itself; the operator may
    file for anyone). Only the verified human operator
    (``config.is_human_identity``) may approve or reject. An agent token is
    refused even when it names the operator as ``approver``. A caller with no
    verified identity (the hub's own shell or an in-process caller) must name
    the operator's label as ``approver``, which is the same rule
    ``org_proposal`` applies to ``apply``. Only the proposer may withdraw, and
    only while nothing has been decided.

Item states
    ``pending`` -> ``approving`` -> ``approved``; ``pending`` -> ``rejected``;
    ``pending`` -> ``withdrawn``. ``approving`` is the claim an approval holds
    while it creates the task. The operator may edit an item's title, notes,
    scope or priority as part of approving it; the edit is applied before the
    claim, so the frozen contract is the edited one (``filed_digest`` keeps the
    digest as filed). The proposal's own status rolls up from its items
    (``_rollup``).

Frozen contract
    ``contract_digest`` is SHA-256 over the canonical JSON of the item's
    title, notes and scope. The spawned task's notes start with the item notes
    followed by ``[proposal: <id>#<index>]``, ``[contract: <digest>]``,
    ``[scope: <canonical json>]`` and ``[reporting: <mode>]`` lines.
    ``check_contract(task_row)`` recomputes the digest from the stored item and
    compares it with the task's marker, title and notes prefix; any mismatch
    raises (fail closed). Lines appended to the task later (a claim line, a
    progress note) do not break the check, because only the prefix is compared.

Crash recovery
    The task is created by the real ``task_add`` in its own transaction, so an
    approval can stop between "task exists" and "item approved". An item left
    in ``approving`` is recovered by ``work_proposal_recover``: forward to
    ``approved`` when a task with its proposal marker *and* its current
    contract digest exists, otherwise back to ``pending``. Each step is a
    conditional update on ``status='approving'``, so recovery is idempotent and
    safe to run alongside a live approval. ``task_add`` is called with an
    idempotency key bound to the proposal, the item and its digest, so a
    retried approval cannot create a second task for the same contract.

Tenancy
    Every row carries ``org_id = edition.tenant_org_id()`` and every read and
    write is fenced with ``org_id IS <tenant>``; another tenant's proposal reads
    as unknown. The private hub stamps and fences NULL.

Every transition appends one ``agent.work_proposal.*`` event in the same
transaction as the state change. Payloads carry ids, status and digests only.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage
from .tool_errors import PermissionDeniedError

PENDING = "pending"
APPROVING = "approving"
APPROVED = "approved"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
STATUSES = (PENDING, APPROVING, APPROVED, REJECTED, WITHDRAWN)

#: ``final_only`` (the default): the executor reports once, when the item's
#: task finishes. ``milestones``: it may also report intermediate milestones.
REPORTING_MODES = ("final_only", "milestones")
DEFAULT_REPORTING_MODE = "final_only"

#: Mirrors ``tools.tasks.PRIORITIES``; kept local so this module does not
#: import the task tool family at schema-creation time.
PRIORITIES = ("high", "med", "low")

CHANNEL_PREFIX = "agent.work_proposal."
CH_FILED = CHANNEL_PREFIX + "filed"
CH_APPROVING = CHANNEL_PREFIX + "item_approving"
CH_APPROVED = CHANNEL_PREFIX + "item_approved"
CH_REJECTED = CHANNEL_PREFIX + "item_rejected"
CH_WITHDRAWN = CHANNEL_PREFIX + "withdrawn"
CH_RECOVERED = CHANNEL_PREFIX + "item_recovered"
CH_ROLLED_BACK = CHANNEL_PREFIX + "item_rolled_back"

#: Seconds an item may sit in ``approving`` before recovery touches it.
DEFAULT_STALE_SECONDS = 300

MAX_ITEMS = 50
MAX_TITLE = 200
MAX_NOTES = 8000
MAX_FILES = 200
MAX_PATH = 500
MAX_REPO = 200
MAX_LIST = 200

_ITEM_FIELDS = frozenset({"title", "notes", "scope", "priority"})
_EDIT_FIELDS = _ITEM_FIELDS
_SCOPE_FIELDS = frozenset({"repo", "files"})

_ID_RE = re.compile(r"wp_[0-9a-f]{12}")
_PROPOSAL_MARK_RE = re.compile(r"\[proposal: (wp_[0-9a-f]{12})#(\d+)\]")
_CONTRACT_MARK_RE = re.compile(r"\[contract: ([^\]]*)\]")

_DDL = (
    """
CREATE TABLE IF NOT EXISTS work_proposals (
  id TEXT PRIMARY KEY,
  org_id TEXT,
  title TEXT NOT NULL,
  proposer TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  reporting_mode TEXT NOT NULL DEFAULT 'final_only',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  withdrawn_at TEXT
)""",
    """
CREATE TABLE IF NOT EXISTS work_proposal_items (
  proposal_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  org_id TEXT,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  scope_json TEXT NOT NULL DEFAULT '{}',
  priority TEXT NOT NULL DEFAULT 'med',
  status TEXT NOT NULL DEFAULT 'pending',
  filed_digest TEXT NOT NULL,
  contract_digest TEXT NOT NULL,
  edited INTEGER NOT NULL DEFAULT 0,
  approving_by TEXT,
  approving_at TEXT,
  decided_by TEXT,
  decided_at TEXT,
  task_id INTEGER,
  PRIMARY KEY (proposal_id, idx)
)""",
    # Leading org_id: the tenancy inventory requires it of a partitioned table.
    "CREATE INDEX IF NOT EXISTS idx_work_proposals_org "
    "ON work_proposals(org_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_work_proposal_items_org "
    "ON work_proposal_items(org_id, proposal_id, idx)",
    "CREATE INDEX IF NOT EXISTS idx_work_proposal_items_approving "
    "ON work_proposal_items(org_id, approving_at) WHERE status='approving'",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create both tables and their indexes. Idempotent; called from
    ``storage.init_db``. Statement-at-a-time so the caller's transaction is not
    committed early (``executescript`` would commit it)."""
    for statement in _DDL:
        conn.execute(statement)


# --- helpers -----------------------------------------------------------------


def _tenant() -> str | None:
    return edition.tenant_org_id()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _iso_ago(seconds: float, now: datetime | None = None) -> str:
    base = now or datetime.now(UTC)
    return (base - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scope(value: object) -> dict[str, Any]:
    """Validate a scope and return its canonical form (files deduplicated, sorted)."""
    if value is None:
        return {"files": [], "repo": None}
    if not isinstance(value, dict):
        raise ToolError("scope must be an object with optional repo and files")
    extra = set(value) - _SCOPE_FIELDS
    if extra:
        raise ToolError(f"scope may name only repo and files, not {sorted(extra)}")
    repo = value.get("repo")
    if repo is not None and (not isinstance(repo, str) or not repo.strip() or len(repo) > MAX_REPO):
        raise ToolError(f"scope.repo must be a non-empty string of at most {MAX_REPO} chars")
    files = value.get("files") or []
    if not isinstance(files, (list, tuple)):
        raise ToolError("scope.files must be a list of paths")
    for path in files:
        if not isinstance(path, str) or not path.strip() or len(path) > MAX_PATH:
            raise ToolError(
                f"each scope.files entry must be a non-empty string of at most {MAX_PATH} chars"
            )
    clean = sorted(set(files))
    if len(clean) > MAX_FILES:
        raise ToolError(f"scope.files may list at most {MAX_FILES} paths")
    return {"files": clean, "repo": repo}


def _title(value: object, *, field: str = "title") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > MAX_TITLE:
        raise ToolError(f"{field} must be at most {MAX_TITLE} chars")
    if "\n" in text or text.startswith("#"):
        raise ToolError(f"{field} must be a one-line summary, not a heading or a body")
    return text


def _notes(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_NOTES:
        raise ToolError(f"notes must be a string of at most {MAX_NOTES} chars")
    return value.strip()


def _priority(value: object) -> str:
    if value is None:
        return "med"
    if value not in PRIORITIES:
        raise ToolError(f"priority must be one of {PRIORITIES}")
    return str(value)


def contract_digest(title: str, notes: str, scope: object) -> str:
    """SHA-256 over the canonical JSON of an item's title, notes and scope."""
    body = {"notes": notes or "", "scope": _scope(scope), "title": title}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def _item(raw: object, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ToolError(f"item {index} must be an object")
    extra = set(raw) - _ITEM_FIELDS
    if extra:
        raise ToolError(f"unknown item field(s) on item {index}: {sorted(extra)}")
    item = {
        "title": _title(raw.get("title"), field=f"item {index} title"),
        "notes": _notes(raw.get("notes")),
        "scope": _scope(raw.get("scope")),
        "priority": _priority(raw.get("priority")),
    }
    item["digest"] = contract_digest(item["title"], item["notes"], item["scope"])
    return item


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and config.identity_fold(a) == config.identity_fold(b)


def _emit(conn: sqlite3.Connection, pending: list, channel: str, actor: str | None, **payload):
    _ulid, _new, publish = events.record_event_once_on_connection(
        conn, channel, payload=payload, originator=actor
    )
    if publish is not None:
        pending.append(publish)


def _publish(pending: list) -> None:
    for event in pending:
        try:
            events.publish(event)
        except Exception:  # noqa: BLE001 - the event is already durable
            pass


def _load(conn: sqlite3.Connection, proposal_id: object) -> sqlite3.Row:
    pid = proposal_id.strip() if isinstance(proposal_id, str) else ""
    row = None
    if _ID_RE.fullmatch(pid):
        row = conn.execute(
            "SELECT * FROM work_proposals WHERE id=? AND org_id IS ?", (pid, _tenant())
        ).fetchone()
    if row is None:
        raise ToolError(f"unknown work proposal {proposal_id!r}")
    return row


def _items(conn: sqlite3.Connection, proposal_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM work_proposal_items WHERE proposal_id=? AND org_id IS ? ORDER BY idx",
        (proposal_id, _tenant()),
    ).fetchall()


def _rollup(statuses: list[str]) -> str:
    """The proposal status implied by its items' statuses."""
    if all(s == WITHDRAWN for s in statuses):
        return WITHDRAWN
    for status in (APPROVING, PENDING, APPROVED):
        if status in statuses:
            return status
    return REJECTED


def _refresh(conn: sqlite3.Connection, proposal_id: str) -> str:
    status = _rollup([r["status"] for r in _items(conn, proposal_id)])
    conn.execute(
        "UPDATE work_proposals SET status=?, updated_at=? WHERE id=? AND org_id IS ?",
        (status, _now_iso(), proposal_id, _tenant()),
    )
    return status


def _public_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "index": row["idx"],
        "title": row["title"],
        "notes": row["notes"],
        "scope": json.loads(row["scope_json"]),
        "priority": row["priority"],
        "status": row["status"],
        "filed_digest": row["filed_digest"],
        "contract_digest": row["contract_digest"],
        "edited": bool(row["edited"]),
        "approving_by": row["approving_by"],
        "approving_at": row["approving_at"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
        "task_id": row["task_id"],
    }


def _public(conn: sqlite3.Connection, row: sqlite3.Row, *, with_items: bool = True):
    out = {
        "id": row["id"],
        "title": row["title"],
        "proposer": row["proposer"],
        "status": row["status"],
        "reporting_mode": row["reporting_mode"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "withdrawn_at": row["withdrawn_at"],
    }
    items = _items(conn, row["id"])
    if with_items:
        out["items"] = [_public_item(r) for r in items]
    else:
        counts: dict[str, int] = {}
        for r in items:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out["item_counts"] = counts
    return out


def _snapshot(proposal_id: str) -> dict[str, Any]:
    conn = storage.connect()
    try:
        return _public(conn, _load(conn, proposal_id))
    finally:
        conn.close()


def compose_task_notes(item: dict[str, Any] | sqlite3.Row, proposal_id: str, mode: str) -> str:
    """The frozen notes a spawned task starts with: item notes, then markers."""
    scope = item["scope_json"] if isinstance(item, sqlite3.Row) else _canonical(item["scope"])
    index = item["idx"] if isinstance(item, sqlite3.Row) else item["index"]
    markers = "\n".join(
        (
            f"[proposal: {proposal_id}#{index}]",
            f"[contract: {item['contract_digest']}]",
            f"[scope: {scope}]",
            f"[reporting: {mode}]",
        )
    )
    notes = item["notes"] or ""
    return f"{notes}\n\n{markers}" if notes else markers


# --- file --------------------------------------------------------------------


def work_proposal_file(
    title: str,
    items: list[dict],
    proposer: str | None = None,
    reporting_mode: str = DEFAULT_REPORTING_MODE,
) -> dict[str, Any]:
    """File a batch of work items for the human operator to approve per item.

    ``items``: 1-50 objects with ``title`` (one line), optional ``notes``,
    ``scope`` ``{repo, files}`` and ``priority`` (high|med|low). Each item is
    stored with its contract digest. An agent files as itself; ``proposer`` is
    for the operator or an unauthenticated in-process caller.
    """
    verified = config.verified_agent_identity()
    if verified:
        if proposer and not _same(proposer, verified) and not config.is_human_identity(verified):
            raise ToolError(f"caller {verified!r} cannot file a work proposal as {proposer!r}")
        who = proposer or verified
    else:
        who = proposer or config.operator_label()
    if not isinstance(who, str) or not who.strip():
        raise ToolError("proposer is required when no verified identity is present")
    clean_title = _title(title)
    if reporting_mode not in REPORTING_MODES:
        raise ToolError(f"reporting_mode must be one of {REPORTING_MODES}")
    if not isinstance(items, (list, tuple)) or not items:
        raise ToolError("a work proposal needs at least one item")
    if len(items) > MAX_ITEMS:
        raise ToolError(f"a work proposal may carry at most {MAX_ITEMS} items")
    clean = [_item(raw, i) for i, raw in enumerate(items)]

    pid = "wp_" + secrets.token_hex(6)
    ts = _now_iso()
    tenant = _tenant()
    pending: list = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO work_proposals (id, org_id, title, proposer, status, reporting_mode, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, tenant, clean_title, who, PENDING, reporting_mode, ts, ts),
        )
        for i, item in enumerate(clean):
            conn.execute(
                "INSERT INTO work_proposal_items (proposal_id, idx, org_id, title, notes, "
                "scope_json, priority, status, filed_digest, contract_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    i,
                    tenant,
                    item["title"],
                    item["notes"],
                    _canonical(item["scope"]),
                    item["priority"],
                    PENDING,
                    item["digest"],
                    item["digest"],
                ),
            )
        _emit(
            conn,
            pending,
            CH_FILED,
            who,
            proposal_id=pid,
            proposer=who,
            status=PENDING,
            items=len(clean),
            digests=[item["digest"] for item in clean],
            reporting_mode=reporting_mode,
        )
        conn.commit()
        result = _public(conn, _load(conn, pid))
    finally:
        conn.close()
    _publish(pending)
    return {"ok": True, "proposal": result}


# --- decide ------------------------------------------------------------------


def _approver(approver: str | None) -> str:
    refusal = (
        "only the human operator may approve or reject work proposal items; agents may "
        "file proposals but not decide them"
    )
    verified = config.verified_agent_identity()
    if verified:
        if not config.is_human_identity(verified):
            raise PermissionDeniedError(f"{refusal} ({verified!r} is not the operator)")
        if approver and not _same(approver, verified):
            raise ToolError(f"approver must be the verified caller {verified!r}, not {approver!r}")
        return verified
    if not approver or not config.is_human_identity(approver):
        raise PermissionDeniedError(
            f"{refusal}; with no verified identity, pass approver=<the operator's label>"
        )
    return approver


def _decisions(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ToolError("decide needs at least one decision {index, approve, edits?}")
    seen: set[int] = set()
    out = []
    for d in raw:
        if not isinstance(d, dict) or set(d) - {"index", "approve", "edits"}:
            raise ToolError("each decision is an object with index, approve and optional edits")
        index = d.get("index")
        if type(index) is not int or index < 0:
            raise ToolError("decision index must be a non-negative integer")
        if index in seen:
            raise ToolError(f"item {index} is decided twice in one call")
        seen.add(index)
        approve = d.get("approve")
        if not isinstance(approve, bool):
            raise ToolError(f"approve must be a boolean (item {index})")
        edits = d.get("edits")
        if edits is not None:
            if not approve:
                raise ToolError(f"only an approval may carry edits (item {index})")
            if not isinstance(edits, dict) or not edits or set(edits) - _EDIT_FIELDS:
                raise ToolError(
                    f"edits may change only {sorted(_EDIT_FIELDS)} (item {index}), "
                    "as a non-empty object"
                )
        out.append({"index": index, "approve": approve, "edits": edits})
    return out


def _apply_edits(row: sqlite3.Row, edits: dict[str, Any] | None) -> dict[str, Any]:
    index = row["idx"]
    current = {
        "title": row["title"],
        "notes": row["notes"],
        "scope": json.loads(row["scope_json"]),
        "priority": row["priority"],
    }
    if not edits:
        return {**current, "digest": row["contract_digest"], "edited": bool(row["edited"])}
    merged = {**current, **edits}
    item = _item(merged, index)
    return {**item, "edited": True}


def _spawn_task(proposal_id: str, row: sqlite3.Row, mode: str, *, untrusted: bool) -> int:
    """Create the item's task through the real ``task_add``."""
    from .tools import tasks as task_tools

    result = task_tools.task_add(
        row["title"],
        notes=compose_task_notes(row, proposal_id, mode),
        priority=row["priority"],
        untrusted=untrusted,
        idempotency_key=f"work-proposal:{proposal_id}#{row['idx']}:{row['contract_digest']}",
    )
    return int(result["id"])


def _mark_approved(proposal_id: str, index: int, task_id: int, approver: str) -> None:
    pending: list = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE work_proposal_items SET status=?, task_id=?, decided_by=?, decided_at=? "
            "WHERE proposal_id=? AND idx=? AND org_id IS ? AND status=?",
            (APPROVED, task_id, approver, _now_iso(), proposal_id, index, _tenant(), APPROVING),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT status, task_id FROM work_proposal_items "
                "WHERE proposal_id=? AND idx=? AND org_id IS ?",
                (proposal_id, index, _tenant()),
            ).fetchone()
            conn.rollback()
            if row is not None and row["status"] == APPROVED and row["task_id"] == task_id:
                return  # recovery already moved it forward to this task
            state = row["status"] if row is not None else "missing"
            raise ToolError(
                f"item {index} of {proposal_id} left 'approving' while task {task_id} was "
                f"created (now {state}); run work_proposal_recover and decide again"
            )
        row = conn.execute(
            "SELECT contract_digest FROM work_proposal_items WHERE proposal_id=? AND idx=? "
            "AND org_id IS ?",
            (proposal_id, index, _tenant()),
        ).fetchone()
        _emit(
            conn,
            pending,
            CH_APPROVED,
            approver,
            proposal_id=proposal_id,
            index=index,
            status=APPROVED,
            contract_digest=row["contract_digest"],
            task_id=task_id,
        )
        _refresh(conn, proposal_id)
        conn.commit()
    finally:
        conn.close()
    _publish(pending)


def work_proposal_decide(
    proposal_id: str, decisions: list[dict], approver: str | None = None
) -> dict[str, Any]:
    """Approve or reject items of a work proposal. Human operator only.

    ``decisions``: ``[{index, approve, edits?}]``. ``edits`` (approval only)
    may change ``title``, ``notes``, ``scope`` or ``priority`` and re-freezes
    the contract digest. Each approved item moves through ``approving``, gets
    one task, then becomes ``approved`` with its ``task_id``.
    """
    who = _approver(approver)
    plan = _decisions(decisions)
    pending: list = []
    claimed: list[int] = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prop = _load(conn, proposal_id)
        pid = prop["id"]
        mode = prop["reporting_mode"]
        if prop["status"] == WITHDRAWN:
            raise ToolError(f"work proposal {pid} is withdrawn")
        rows = {r["idx"]: r for r in _items(conn, pid)}
        for d in plan:
            row = rows.get(d["index"])
            if row is None:
                raise ToolError(f"work proposal {pid} has no item {d['index']}")
            if row["status"] != PENDING:
                raise ToolError(f"item {d['index']} is {row['status']}, not pending")
        ts = _now_iso()
        for d in plan:
            row, index = rows[d["index"]], d["index"]
            if not d["approve"]:
                conn.execute(
                    "UPDATE work_proposal_items SET status=?, decided_by=?, decided_at=? "
                    "WHERE proposal_id=? AND idx=? AND org_id IS ? AND status=?",
                    (REJECTED, who, ts, pid, index, _tenant(), PENDING),
                )
                _emit(
                    conn,
                    pending,
                    CH_REJECTED,
                    who,
                    proposal_id=pid,
                    index=index,
                    status=REJECTED,
                    contract_digest=row["contract_digest"],
                )
        for d in plan:
            if not d["approve"]:
                continue
            row, index = rows[d["index"]], d["index"]
            frozen = _apply_edits(row, d["edits"])
            conn.execute(
                "UPDATE work_proposal_items SET title=?, notes=?, scope_json=?, priority=?, "
                "contract_digest=?, edited=?, status=?, approving_by=?, approving_at=? "
                "WHERE proposal_id=? AND idx=? AND org_id IS ? AND status=?",
                (
                    frozen["title"],
                    frozen["notes"],
                    _canonical(frozen["scope"]),
                    frozen["priority"],
                    frozen["digest"],
                    int(frozen["edited"]),
                    APPROVING,
                    who,
                    ts,
                    pid,
                    index,
                    _tenant(),
                    PENDING,
                ),
            )
            _emit(
                conn,
                pending,
                CH_APPROVING,
                who,
                proposal_id=pid,
                index=index,
                status=APPROVING,
                contract_digest=frozen["digest"],
                edited=frozen["edited"],
            )
            claimed.append(index)
        _refresh(conn, pid)
        conn.commit()
        untrusted = config.is_agent_untrusted(prop["proposer"])
        frozen_rows = {r["idx"]: r for r in _items(conn, pid) if r["idx"] in claimed}
    finally:
        conn.close()
    _publish(pending)

    for position, index in enumerate(claimed):
        row = frozen_rows[index]
        try:
            task_id = _spawn_task(pid, row, mode, untrusted=untrusted)
        except Exception:
            # This item's task may or may not exist, and the items after it were
            # never spawned: recover them all at once (forward where the task
            # exists, else back to pending), then re-raise.
            for rest in claimed[position:]:
                _recover_item(pid, rest, actor=who)
            raise
        _mark_approved(pid, index, task_id, who)
    return {"ok": True, "proposal": _snapshot(pid)}


# --- withdraw ----------------------------------------------------------------


def work_proposal_withdraw(
    proposal_id: str, actor: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Withdraw a proposal. Proposer only, and only while no item is decided."""
    verified = config.verified_agent_identity()
    if verified and actor and not _same(actor, verified):
        raise ToolError(f"caller {verified!r} cannot withdraw as {actor!r}")
    who = verified or actor
    if not who:
        raise ToolError("actor is required when no verified identity is present")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise ToolError("reason must be a string of at most 500 chars")
    pending: list = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prop = _load(conn, proposal_id)
        pid = prop["id"]
        if not _same(who, prop["proposer"]):
            raise ToolError(
                f"only the proposer {prop['proposer']!r} may withdraw work proposal {pid}"
            )
        statuses = [r["status"] for r in _items(conn, pid)]
        if prop["status"] == WITHDRAWN:
            raise ToolError(f"work proposal {pid} is already withdrawn")
        if APPROVING in statuses:
            raise ToolError(f"work proposal {pid} has an approval in progress")
        if any(s != PENDING for s in statuses):
            raise ToolError(
                f"work proposal {pid} already has decided items; it can no longer be withdrawn"
            )
        ts = _now_iso()
        conn.execute(
            "UPDATE work_proposal_items SET status=?, decided_by=?, decided_at=? "
            "WHERE proposal_id=? AND org_id IS ? AND status=?",
            (WITHDRAWN, who, ts, pid, _tenant(), PENDING),
        )
        conn.execute(
            "UPDATE work_proposals SET withdrawn_at=? WHERE id=? AND org_id IS ?",
            (ts, pid, _tenant()),
        )
        _refresh(conn, pid)
        _emit(
            conn,
            pending,
            CH_WITHDRAWN,
            who,
            proposal_id=pid,
            status=WITHDRAWN,
            reason=(reason or "")[:500],
        )
        conn.commit()
        result = _public(conn, _load(conn, pid))
    finally:
        conn.close()
    _publish(pending)
    return {"ok": True, "proposal": result}


# --- recovery ----------------------------------------------------------------


def _find_task(conn: sqlite3.Connection, proposal_id: str, index: int, digest: str) -> int | None:
    """The task spawned for this item's *current* contract, if one exists."""
    rows = conn.execute(
        "SELECT id, notes FROM tasks WHERE org_id IS ? AND instr(notes, ?) > 0 ORDER BY id",
        (_tenant(), f"[proposal: {proposal_id}#{index}]"),
    ).fetchall()
    for row in rows:
        if f"[contract: {digest}]" in (row["notes"] or ""):
            return int(row["id"])
    return None


def _recover_item(
    proposal_id: str,
    index: int,
    *,
    actor: str | None = None,
    cutoff: str | None = None,
) -> dict[str, Any] | None:
    """Move one ``approving`` item forward or back. None if it was not stale."""
    pending: list = []
    outcome: dict[str, Any] | None = None
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM work_proposal_items WHERE proposal_id=? AND idx=? AND org_id IS ? "
            "AND status=?",
            (proposal_id, index, _tenant(), APPROVING),
        ).fetchone()
        if row is None or (cutoff is not None and (row["approving_at"] or "") > cutoff):
            conn.rollback()
            return None
        task_id = _find_task(conn, proposal_id, index, row["contract_digest"])
        if task_id is not None:
            conn.execute(
                "UPDATE work_proposal_items SET status=?, task_id=?, decided_by=approving_by, "
                "decided_at=? WHERE proposal_id=? AND idx=? AND org_id IS ? AND status=?",
                (APPROVED, task_id, _now_iso(), proposal_id, index, _tenant(), APPROVING),
            )
            channel = CH_RECOVERED
            outcome = {"proposal_id": proposal_id, "index": index, "task_id": task_id}
            status = APPROVED
        else:
            conn.execute(
                "UPDATE work_proposal_items SET status=?, approving_by=NULL, approving_at=NULL "
                "WHERE proposal_id=? AND idx=? AND org_id IS ? AND status=?",
                (PENDING, proposal_id, index, _tenant(), APPROVING),
            )
            channel = CH_ROLLED_BACK
            outcome = {"proposal_id": proposal_id, "index": index}
            status = PENDING
        _emit(
            conn,
            pending,
            channel,
            actor or "work-proposal-recovery",
            proposal_id=proposal_id,
            index=index,
            status=status,
            contract_digest=row["contract_digest"],
            task_id=task_id,
        )
        _refresh(conn, proposal_id)
        conn.commit()
    finally:
        conn.close()
    _publish(pending)
    return outcome


def work_proposal_recover(
    now: datetime | None = None, older_than_s: float = DEFAULT_STALE_SECONDS
) -> dict[str, Any]:
    """Recover items stuck in ``approving`` for longer than ``older_than_s``.

    Forward to ``approved`` when the task for the item's current contract
    exists, else back to ``pending``. Idempotent: a second pass finds nothing.
    """
    if not isinstance(older_than_s, (int, float)) or older_than_s < 0:
        raise ToolError("older_than_s must be a non-negative number")
    if now is not None and not isinstance(now, datetime):
        raise ToolError("now is an in-process override; omit it over MCP")
    cutoff = _iso_ago(older_than_s, now)
    conn = storage.connect()
    try:
        stale = conn.execute(
            "SELECT proposal_id, idx FROM work_proposal_items WHERE org_id IS ? AND status=? "
            "AND approving_at <= ? ORDER BY proposal_id, idx",
            (_tenant(), APPROVING, cutoff),
        ).fetchall()
    finally:
        conn.close()
    forward: list[dict[str, Any]] = []
    back: list[dict[str, Any]] = []
    for row in stale:
        outcome = _recover_item(row["proposal_id"], row["idx"], cutoff=cutoff)
        if outcome is None:
            continue
        (forward if "task_id" in outcome else back).append(outcome)
    return {"recovered_forward": forward, "rolled_back": back}


# --- reads -------------------------------------------------------------------


def work_proposal_get(proposal_id: str) -> dict[str, Any]:
    """One work proposal with its items."""
    return {"proposal": _snapshot(proposal_id)}


def work_proposal_list(
    status: str | None = None, proposer: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """Work proposals, newest first, with per-status item counts."""
    if status is not None and status not in STATUSES:
        raise ToolError(f"status must be one of {STATUSES}")
    if type(limit) is not int or not 1 <= limit <= MAX_LIST:
        raise ToolError(f"limit must be an integer from 1 to {MAX_LIST}")
    where, params = ["org_id IS ?"], [_tenant()]
    if status is not None:
        where.append("status=?")
        params.append(status)
    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM work_proposals WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, rowid DESC",
            params,
        ).fetchall()
        if proposer is not None:
            rows = [r for r in rows if _same(r["proposer"], proposer)]
        return {"proposals": [_public(conn, r, with_items=False) for r in rows[:limit]]}
    finally:
        conn.close()


# --- claim-time contract check -------------------------------------------------


def check_contract(task_row: Any, conn: sqlite3.Connection | None = None) -> None:
    """Fail closed when a proposal-spawned task no longer matches its contract.

    ``task_row`` needs ``id``, ``title`` and ``notes``. A task with no proposal
    or contract marker is not a proposal task and passes (returns None). Pass
    the caller's ``conn`` to read inside its transaction.
    """
    notes = task_row["notes"] or ""
    task_id = task_row["id"]
    proposals = set(_PROPOSAL_MARK_RE.findall(notes))
    contracts = _CONTRACT_MARK_RE.findall(notes)
    if not proposals and not contracts:
        return None
    if not proposals:
        raise ToolError(
            f"task {task_id} carries a [contract: ...] marker but no [proposal: ...] marker; "
            "its contract cannot be verified, so the claim is refused"
        )
    if len(proposals) > 1:
        raise ToolError(f"task {task_id} names more than one proposal; contract refused")
    (pid, raw_index) = next(iter(proposals))
    index = int(raw_index)
    own = conn is None
    db = storage.connect() if own else conn
    try:
        row = db.execute(
            "SELECT * FROM work_proposal_items WHERE proposal_id=? AND idx=? AND org_id IS ?",
            (pid, index, _tenant()),
        ).fetchone()
        mode_row = db.execute(
            "SELECT reporting_mode FROM work_proposals WHERE id=? AND org_id IS ?",
            (pid, _tenant()),
        ).fetchone()
    finally:
        if own:
            db.close()
    if row is None or mode_row is None:
        raise ToolError(f"task {task_id} names unknown work proposal item {pid}#{index}")
    where = f"task {task_id} (proposal {pid}#{index})"
    if row["status"] != APPROVED:
        raise ToolError(
            f"{where}: item is {row['status']}, not approved; its contract is not in force"
        )
    if row["task_id"] is not None and row["task_id"] != task_id:
        raise ToolError(f"{where}: the contract belongs to task {row['task_id']}")
    recomputed = contract_digest(row["title"], row["notes"], json.loads(row["scope_json"]))
    if recomputed != row["contract_digest"]:
        raise ToolError(f"{where}: the frozen item no longer matches its contract digest")
    if contracts != [row["contract_digest"]]:
        raise ToolError(f"{where}: the task's [contract: ...] marker does not match the item")
    if task_row["title"] != row["title"]:
        raise ToolError(f"{where}: the task title differs from the approved contract")
    expected = compose_task_notes(row, pid, mode_row["reporting_mode"])
    if not (notes == expected or notes.startswith(expected + "\n")):
        raise ToolError(f"{where}: the task notes differ from the approved contract")
    return None
