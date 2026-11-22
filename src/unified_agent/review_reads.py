"""Machine-readable record of the head a review read was performed at (task #2259).

The review pipeline already records verdicts, but only as prose in task notes
(``[review: approved @ <ts>]``, ``reviewer.py``) and as free-form PR comments.
Nothing could therefore answer the one question a release gate has to ask:
*was the tree that is about to merge actually read?*

PR #1327 is the case that exposed it. A read was recorded at ``163c8512``; two
commits touching red-tier ``deploy/gcp`` paths landed after it; the merge
happened at ``19fedaa9``. The human Approval Ledger row was bound to the final
head and was therefore legitimate, and the pinned merge was pinned to the right
commit — every existing check passed. The gap was that no check could tell
"approved" from "read".

A row here is the read. This module is storage and validation only: it holds no
policy, so the rule stays in one place — ``shipping_preflight_cli.review_head_gate``
— where its tests live. The release preflight consults it under the narrow
head-moved rule (task #2259, operator decision 2026-10-31). Two writers feed
it (task #2294):

- ``policy`` — the review pipeline's validated verdict
  (``reviewer._validate_and_hold``), written only when the pull request's live
  head is the head the pipeline validated;
- ``steward-read`` / ``human`` / ``delta-read`` — an explicit read, recorded
  with ``unified-agent-review record-read`` after it happened. ``human`` needs
  a human reader and an evidence URL. From a hub client the command goes
  through ``POST /api/review/reads`` (task #2407), which takes the reader from
  the verified credential and admits only ``UA_REVIEW_READERS`` and humans.

An Approval Ledger approval writes nothing here. It answers whether a human
authorized the head, not whether anyone read it: PR #1327 had an approval at
its final head and an unread red-tier delta, and an approval that recorded a
full read would have passed exactly that merge.

The pipeline writer is best-effort (``record_best_effort``): a failed write is
logged and returned to the caller, never allowed to lose the verdict it
describes. A head no writer reached still answers ``no-read-recorded`` and
passes, because the approval and its independent review stay the authority.

Every row is scoped to ``(repo, number, head_sha, kind, reader)``: one row per
reader at a head, so a second reader never overwrites the first and the gate can
tell an independent read from a re-read (task #2641). The same reader recording
the same head and kind again updates that reader's own row in place, so a
re-read never looks like two reads.

``from_sha`` is set only on a ``delta-read`` row and names the head the reader
was moving from, so a delta read can never be mistaken for a full read of the
same head. A delta read vouches only for what changed since ``from_sha``, so
``record`` refuses one unless a full read (``FULL_READ_KINDS``) is recorded at
``from_sha`` for the same pull request — by any reader, because the read is of
the tree, not of the reader. A delta is not a base for another delta: after
several pushes, record the delta from the last full read. The gate repeats the
check, so a row written around this function is not counted either, and it
counts a delta read only at its own head: never as the base for a later push.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from . import config, storage

#: A read performed by the steward/reviewer over the full diff of that head.
STEWARD_READ = "steward-read"
#: The policy pipeline's own verdict at that head.
POLICY = "policy"
#: A read of only the delta between ``from_sha`` and ``head_sha``.
DELTA_READ = "delta-read"
#: A human's own full read at that head, recorded explicitly (never a Ledger approval).
HUMAN = "human"

KINDS = (STEWARD_READ, POLICY, DELTA_READ, HUMAN)
#: Kinds that read the whole tree at their head. Only these anchor a delta read.
FULL_READ_KINDS = (STEWARD_READ, POLICY, HUMAN)
#: Kinds a person or agent records by hand. ``policy`` is the pipeline's alone.
EXPLICIT_KINDS = (STEWARD_READ, HUMAN, DELTA_READ)

logger = logging.getLogger(__name__)

_HEAD = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_MAX_TEXT = 500
#: A pull request cannot list more changed paths than this, and one path
#: cannot be longer than this. Both exist so a caller cannot write a row whose
#: size makes the gate's own read the expensive part.
_MAX_FILES = 2000
_MAX_PATH = 500


#: One row per reader at a head. A second reader adds its own row; the same
#: reader re-recording the same head and kind updates its row in place.
_UNIQUE_KEY = "UNIQUE (repo, number, head_sha, kind, reader)"

_CREATE = (
    "CREATE TABLE {name} ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "repo TEXT NOT NULL, "
    "number INTEGER NOT NULL, "
    "head_sha TEXT NOT NULL, "
    "from_sha TEXT, "
    "kind TEXT NOT NULL, "
    "reader TEXT NOT NULL, "
    "verdict TEXT NOT NULL, "
    "files_json TEXT NOT NULL DEFAULT '[]', "
    "evidence_url TEXT, "
    "created_at TEXT NOT NULL, "
    f"{_UNIQUE_KEY})"
)

_INDEX = "CREATE INDEX IF NOT EXISTS review_reads_pr ON review_reads (repo, number, created_at)"

_COLUMNS = (
    "id, repo, number, head_sha, from_sha, kind, reader, verdict, files_json, "
    "evidence_url, created_at"
)


def _unique_key_is_per_reader(sql: str) -> bool:
    """Whether a table's DDL already carries the per-reader key.

    Read from ``sqlite_master`` because a constraint cannot be inspected any
    other way, and normalized because SQLite stores the definition as written.
    """
    return "unique(repo,number,head_sha,kind,reader)" in re.sub(r"\s+", "", sql).lower()


def _sequence_high_water(conn: sqlite3.Connection, table: str) -> int | None:
    """The ``AUTOINCREMENT`` high-water mark ``sqlite_sequence`` holds for ``table``.

    Dropping a table deletes its ``sqlite_sequence`` row, so a rebuild that only
    copies the surviving rows would let the next insert reuse the id of a
    deleted row. Read it before the drop and restore it afterwards.
    """
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
    return None if row is None else int(row["seq"])


def _restore_sequence(conn: sqlite3.Connection, table: str, high_water: int | None) -> None:
    """Keep ``table``'s high-water mark at least ``high_water`` after a rebuild."""
    if high_water is None:
        return
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, high_water))
    elif int(row["seq"]) < high_water:
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (high_water, table))


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ``review_reads`` and its index, rebuilding an older table.

    Idempotent; called from ``storage._migrate()`` at startup. The table
    predating task #2641 keyed a read by
    ``(repo, number, head_sha, kind)``, so a second reader overwrote the first.
    SQLite cannot alter a constraint, so an existing table with the old key is
    rebuilt: every surviving row is copied with its own reader, the id
    high-water mark is carried over so a deleted id is never reissued, and the
    index is recreated. Rows the old key already merged cannot be recovered.
    """
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_reads'"
    ).fetchone()
    if existing is not None and not _unique_key_is_per_reader(existing["sql"] or ""):
        # A savepoint makes the rebuild atomic on its own, whether or not the
        # caller already holds a transaction: a failure cannot leave the table
        # dropped or half-copied.
        conn.execute("SAVEPOINT review_reads_rebuild")
        try:
            high_water = _sequence_high_water(conn, "review_reads")
            conn.execute("DROP TABLE IF EXISTS review_reads_rebuild")
            conn.execute(_CREATE.format(name="review_reads_rebuild"))
            conn.execute(
                f"INSERT INTO review_reads_rebuild ({_COLUMNS}) SELECT {_COLUMNS} FROM review_reads"
            )
            conn.execute("DROP TABLE review_reads")
            conn.execute("ALTER TABLE review_reads_rebuild RENAME TO review_reads")
            _restore_sequence(conn, "review_reads", high_water)
        except BaseException:
            conn.execute("ROLLBACK TO review_reads_rebuild")
            conn.execute("RELEASE review_reads_rebuild")
            raise
        conn.execute("RELEASE review_reads_rebuild")
    elif existing is None:
        conn.execute(_CREATE.format(name="review_reads"))
    conn.execute(_INDEX)


def _text(value: str, *, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field} must be a non-empty string of at most {max_length} chars")
    return value


def _paths(value: object) -> list[str]:
    """Validate and normalize the changed-path list of a read.

    A path is read as evidence that the reader looked at that file, so a
    malformed or runaway list has to be refused here rather than stored and
    trusted later. Repeated paths collapse (a file changed twice is one file),
    and the order the caller gave is kept.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("files must be a list of changed paths")
    out: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str) or not entry.strip() or len(entry) > _MAX_PATH:
            raise ValueError(
                f"each files entry must be a non-empty string of at most {_MAX_PATH} chars"
            )
        if entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    if len(out) > _MAX_FILES:
        raise ValueError(f"files may list at most {_MAX_FILES} paths")
    return out


def record(
    *,
    repo: str,
    number: int,
    head_sha: str,
    kind: str,
    reader: str,
    verdict: str,
    from_sha: str | None = None,
    files: list[str] | None = None,
    evidence_url: str | None = None,
) -> int:
    """Record one read by ``reader`` at ``head_sha``.

    The same reader re-recording the same ``(repo, number, head, kind)`` updates
    its own row in place, so a re-read never looks like two reads. A different
    reader recording the same head and kind gets its own row: the earlier
    reader's evidence is never overwritten (task #2641), so an author's later
    read cannot replace an independent one and the gate can name every reader at
    a head. Raises ``ValueError`` on anything malformed: a gate that reads this
    table must not have to defend against its own rows."""
    if not isinstance(repo, str) or _REPO.fullmatch(repo) is None:
        raise ValueError("repo must be 'owner/name'")
    if type(number) is not int or number <= 0:
        raise ValueError("number must be a positive integer")
    if not isinstance(head_sha, str) or _HEAD.fullmatch(head_sha) is None:
        raise ValueError("head_sha must be 40 lowercase hex characters")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    _text(reader, field="reader", max_length=120)
    _text(verdict, field="verdict", max_length=60)
    if kind == DELTA_READ:
        if not isinstance(from_sha, str) or _HEAD.fullmatch(from_sha) is None:
            raise ValueError("a delta-read row must name the from_sha it read from")
        if from_sha == head_sha:
            raise ValueError("a delta-read row must move between two different heads")
    elif from_sha is not None:
        raise ValueError(f"only a delta-read row carries from_sha, not {kind}")
    payload = json.dumps(_paths(files), sort_keys=True)
    if evidence_url is not None:
        _text(evidence_url, field="evidence_url", max_length=_MAX_TEXT)
    created_at = datetime.now(UTC).isoformat()
    conn = storage.connect()
    try:
        with conn:
            if kind == DELTA_READ and not _has_full_read(conn, repo, number, from_sha):
                raise ValueError(
                    f"a delta-read row needs a full read at {from_sha[:12]}, and no full read "
                    "is recorded at that head for this pull request; record the delta from "
                    "the last full read"
                )
            conn.execute(
                "INSERT INTO review_reads "
                "(repo, number, head_sha, from_sha, kind, reader, verdict, files_json, "
                "evidence_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (repo, number, head_sha, kind, reader) DO UPDATE SET "
                "from_sha=excluded.from_sha, verdict=excluded.verdict, "
                "files_json=excluded.files_json, evidence_url=excluded.evidence_url, "
                "created_at=excluded.created_at",
                (
                    repo,
                    number,
                    head_sha,
                    from_sha,
                    kind,
                    reader,
                    verdict,
                    payload,
                    evidence_url,
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT id FROM review_reads WHERE repo=? AND number=? AND head_sha=? "
                "AND kind=? AND reader=?",
                (repo, number, head_sha, kind, reader),
            ).fetchone()
        return int(row["id"])
    finally:
        conn.close()


def readers() -> frozenset[str]:
    """Agent identities allowed to record a read over the hub (``UA_REVIEW_READERS``).

    Unset or empty means none: the hub's own shell stays the only agent path
    until the operator names who reads. No role, profile or office answers this
    question. The deploy steward moves round by round, and the org chart states
    that it grants no authority, so the list is explicit.
    """
    raw = os.environ.get("UA_REVIEW_READERS", "")
    return frozenset(label.strip() for label in raw.split(",") if label.strip())


def caller_refusal(identity: str, kind: object) -> str | None:
    """Why a verified hub caller may not record this kind of read, or ``None``.

    A ``human`` read is judged by ``explicit_read_refusal``, which requires a
    human reader. Every other kind needs a human or a listed reader.
    """
    if kind == HUMAN or config.actor_kind_for_identity(identity) == "human":
        return None
    if identity in readers():
        return None
    return f"{identity!r} is not named in UA_REVIEW_READERS, so it cannot record a {kind} row"


def explicit_read_refusal(
    *,
    repo: object,
    number: object,
    head_sha: object,
    kind: object,
    reader: str,
    verdict: object,
    from_sha: object,
    files: object,
    evidence_url: object,
    live_head: Callable[[str, int], str | None],
    delta_refusal: Callable[[str, str, str, list[str]], str | None],
) -> str | None:
    """Why this explicit read must not be recorded, or ``None``.

    The one rule behind the hub-shell CLI and the HTTP route. Shape is checked
    first, so GitHub is never asked about a value ``record`` would refuse.
    ``live_head`` and ``delta_refusal`` are passed in because they need GitHub,
    which this module does not touch.
    """
    try:
        _validate_pr_args(repo, number)  # type: ignore[arg-type]
        _text(verdict, field="verdict", max_length=60)  # type: ignore[arg-type]
        paths = _paths(files)
        if evidence_url is not None:
            _text(evidence_url, field="evidence_url", max_length=_MAX_TEXT)  # type: ignore[arg-type]
    except ValueError as exc:
        return str(exc)
    if kind not in EXPLICIT_KINDS:
        return f"kind must be one of {', '.join(EXPLICIT_KINDS)}"
    actor = config.actor_kind_for_identity(reader)
    if actor == "unknown":
        return f"reader {reader!r} is not a known identity label"
    if kind == HUMAN and actor != "human":
        return f"reader {reader!r} is not a human identity; a human row needs one"
    if kind in (DELTA_READ, HUMAN) and not evidence_url:
        return f"a {kind} row must name its --evidence-url (where the read is written up)"
    if not isinstance(head_sha, str) or _HEAD.fullmatch(head_sha) is None:
        return "head must be the full 40-character lowercase SHA"
    if kind == DELTA_READ:
        if not isinstance(from_sha, str) or _HEAD.fullmatch(from_sha) is None:
            return "a delta-read row must name the from_sha it read from"
    elif from_sha is not None:
        return f"only a delta-read row carries from_sha, not {kind}"
    live = live_head(repo, number)  # type: ignore[arg-type]
    if live != head_sha:
        shown = live[:12] if live else "unavailable"
        return (
            f"head {head_sha[:12]} is not the pull request's current head ({shown}); "
            "record the read at the head you read, while it is the live head"
        )
    if kind == DELTA_READ:
        return delta_refusal(repo, from_sha, head_sha, paths)  # type: ignore[arg-type]
    return None


def _has_full_read(conn: sqlite3.Connection, repo: str, number: int, head_sha: str) -> bool:
    marks = ", ".join("?" for _ in FULL_READ_KINDS)
    row = conn.execute(
        "SELECT 1 FROM review_reads WHERE repo=? AND number=? AND head_sha=? "
        f"AND kind IN ({marks}) LIMIT 1",
        (repo, number, head_sha, *FULL_READ_KINDS),
    ).fetchone()
    return row is not None


def record_best_effort(**kwargs: Any) -> dict[str, Any]:
    """``record`` for a writer whose own outcome must not depend on this row.

    Returns ``{"recorded": id, "error": None}``, or ``{"recorded": None,
    "error": reason}`` with a logged warning when the row was refused or the
    store failed. The reviewer and the Ledger approval call this after their
    own verdict has committed, so a refused read can only leave the gate at
    ``no-read-recorded``; it can never undo the verdict or the approval.
    """
    try:
        return {"recorded": record(**kwargs), "error": None}
    except Exception as exc:  # the caller's verdict already stands
        reason = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.warning(
            "review read not recorded for %s#%s at %s (%s): %s",
            kwargs.get("repo"),
            kwargs.get("number"),
            str(kwargs.get("head_sha"))[:12],
            kwargs.get("kind"),
            reason,
        )
        return {"recorded": None, "error": reason}


def _public(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["files"] = json.loads(out.pop("files_json") or "[]")
    return out


def for_head(repo: str, number: int, head_sha: str) -> list[dict[str, Any]]:
    """Every read recorded for exactly this head, newest first."""
    conn = storage.connect_read_only()
    try:
        rows = conn.execute(
            "SELECT * FROM review_reads WHERE repo=? AND number=? AND head_sha=? "
            "ORDER BY created_at DESC, id DESC",
            (repo, number, head_sha),
        ).fetchall()
        return [_public(r) for r in rows]
    finally:
        conn.close()


def _validate_pr_args(repo: str, number: int) -> None:
    if not isinstance(repo, str) or _REPO.fullmatch(repo) is None:
        raise ValueError("repo must be 'owner/name'")
    if type(number) is not int or number <= 0:
        raise ValueError("number must be a positive integer")


def for_pr(
    repo: str,
    number: int,
    *,
    limit: int | None = None,
    after_id: int | None = None,
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    """Every read recorded for this pull request, newest first.

    The merge gate walks this to find the newest read whose head is an ancestor
    of the head that would merge; ancestry itself is the caller's question,
    because it needs the repository, not the database.

    ``limit`` defaults to ``None``, which returns every row: a gate that asked
    a bounded question and got a bounded answer cannot tell "no read" from "the
    read was on page two", so the cap is the caller's explicit choice, never a
    default it did not ask for. To page, pass ``limit`` and then feed the last
    row's ``id`` back as ``after_id``: the walk is keyset over
    ``(created_at, id)``, so a row inserted mid-walk neither duplicates nor
    shifts the rows already returned.
    """
    _validate_pr_args(repo, number)
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("limit must be a positive integer or None")
    if after_id is not None and (type(after_id) is not int or after_id <= 0):
        raise ValueError("after_id must be a positive integer or None")
    own_conn = conn is None
    if own_conn:
        conn = storage.connect_read_only()
    try:
        sql = "SELECT * FROM review_reads WHERE repo=? AND number=?"
        params: list[Any] = [repo, number]
        if after_id is not None:
            anchor = conn.execute(
                "SELECT created_at, id FROM review_reads WHERE id=? AND repo=? AND number=?",
                (after_id, repo, number),
            ).fetchone()
            if anchor is None:
                raise ValueError("after_id must be an id already returned for this pull request")
            sql += " AND (created_at, id) < (?, ?)"
            params.extend([anchor["created_at"], anchor["id"]])
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [_public(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


def prs_read_since(repo: str, since: str) -> set[int]:
    """Pull requests of ``repo`` with a read recorded after ``since`` (ISO-8601,
    UTC). The poll schedule asks this so a new read does not wait out the idle
    interval before the check at that head is recomputed (task #2320)."""
    conn = storage.connect_read_only()
    try:
        rows = conn.execute(
            "SELECT DISTINCT number FROM review_reads WHERE repo=? AND created_at>?",
            (repo, since),
        ).fetchall()
        return {int(row["number"]) for row in rows}
    finally:
        conn.close()


def latest_reads_since(repo: str, since: str, *, limit: int = 50) -> dict[int, tuple[str, str]]:
    """``{number: (head_sha, created_at)}`` of the newest read on each PR of
    ``repo`` read after ``since`` (ISO-8601, UTC), for at most ``limit`` PRs."""
    conn = storage.connect_read_only()
    try:
        rows = conn.execute(
            "SELECT number, head_sha, created_at FROM review_reads WHERE repo=? AND created_at>? "
            "ORDER BY created_at DESC, id DESC",
            (repo, since),
        ).fetchall()
    finally:
        conn.close()
    latest: dict[int, tuple[str, str]] = {}
    for row in rows:
        if len(latest) >= limit:
            break
        latest.setdefault(int(row["number"]), (str(row["head_sha"]), str(row["created_at"])))
    return latest


def count_for_pr(repo: str, number: int) -> int:
    """How many reads exist for this pull request. A paging caller compares
    this with the rows it has seen, so a short page reads as a truncated answer
    rather than as the whole history."""
    _validate_pr_args(repo, number)
    conn = storage.connect_read_only()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM review_reads WHERE repo=? AND number=?",
            (repo, number),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()
