"""Work product submit -> named-reviewer approval -> release (task #1278, H2).

The non-code pilot wedge: an agent produces a document/file (report, filing,
remediation, patient note, audit workpaper...), a named human approves it, it
is released, and evidence proves human -> device -> agent -> model ->
document hash -> approval.

Vertical-neutral by construction. Nothing here hardcodes a matter/client tag,
a reviewer role, or a release destination — those come from a chosen
*template pack* (YAML under ``templates/work_product/``, see
:func:`load_template_pack`). Two synthetic packs ship with this repo
(``legal``, ``accessibility-remediation``); a pilot supplies its own via
``UA_WORK_PRODUCT_TEMPLATE_DIR``.

Reuses rather than reinvents the platform's existing machinery:

* the approval queue (``tools.approvals.check_approval_gate`` /
  ``approval_resolve``) is the human gate — a submission always parks a
  ``require_approval`` row (``force=True``), exactly like
  ``managed_secret_set`` and ``agent_token_issue`` already do for other
  high-impact writes;
* the submitter can never release its own work: ``approval_resolve`` is
  denied to the developer profile (``profiles.DEVELOPER_DENIED_TOOLS``), and
  for ``work_product_release`` it refuses an approval by the identity that
  requested it (``approvals.SEPARATION_OF_DUTIES_TOOLS``) under every profile;
* the hash-chained evidence ledger (``events.record_event``) is the proof —
  every transition (submitted/approved/rejected/released/tampered) appends
  one entry, and ``approval_resolve`` itself already stamps a signed
  authenticated-producer event (``audit.producer_sign``) binding the
  resolving human's verified identity to the approval id.

Retry-consumes-the-pass idiom
------------------------------
:func:`work_product_submit` follows the same shape ``managed_secret_set`` and
``agent_token_issue`` use: called once, it always raises
``ApprovalPendingError`` for a brand-new submission (nothing could already be
approved for a payload keyed by a work product id that did not exist a
moment ago). A human resolves the parked approval with the existing
``approval_resolve`` tool. Calling :func:`work_product_submit` again with the
identical ``(source file, title, template pack)`` finds the same
``work_products`` row (content-hash keyed lookup, not a fresh insert) and the
identical approval payload — so the retry either consumes the now-approved
one-shot pass and performs the release, or discovers the denial and records
the rejection. No separate "release" tool or "resume" flag is needed.
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import mimetypes
import os
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from .. import agent_access, config, edition, events, safety, storage
from ..audit import producer_sign
from . import approvals

_PACK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TERMINAL_STATUSES = frozenset({"rejected", "released", "tampered"})
_OPEN_STATUSES = ("submitted", "approved")
# Reviewer diffs are for documents a human reads, not arbitrary binaries.
_DIFF_MAX_BYTES = 1024 * 1024
_TEXT_SUFFIXES = frozenset({".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".html", ".xml"})


@dataclass(frozen=True)
class TemplatePack:
    """A vertical's requirements for a work product: what tags it needs, who

    reviews it, and where an approved copy lands. Vertical specifics live
    entirely in the pack, never in this module.
    """

    id: str
    title: str
    description: str
    reviewer_role: str
    required_tags: tuple[str, ...]
    release_subfolder: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "reviewer_role": self.reviewer_role,
            "required_tags": list(self.required_tags),
            "release_subfolder": self.release_subfolder,
        }


# ---------------------------------------------------------------------------
# Template packs
# ---------------------------------------------------------------------------


def _pack_path(pack_id: str) -> Path:
    if not isinstance(pack_id, str) or not _PACK_ID_RE.fullmatch(pack_id):
        raise ToolError(
            f"invalid template pack id {pack_id!r}: must match ^[a-z][a-z0-9-]{{0,63}}$"
        )
    return config.work_product_template_dir() / f"{pack_id}.yaml"


def _load_pack_file(path: Path) -> TemplatePack:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ToolError(f"template pack {path} is unreadable or not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ToolError(f"template pack {path} must be a YAML mapping")

    pack_id = str(raw.get("id") or path.stem)
    if pack_id != path.stem:
        raise ToolError(
            f"template pack {path} declares id {pack_id!r}, which must match its "
            f"filename {path.stem!r}"
        )
    reviewer_role = str(raw.get("reviewer_role") or "").strip()
    if not reviewer_role:
        raise ToolError(f"template pack {pack_id!r} must declare a non-empty reviewer_role")

    required_tags_raw = raw.get("required_tags") or []
    if not isinstance(required_tags_raw, list):
        raise ToolError(f"template pack {pack_id!r}: required_tags must be a list")
    required_tags = tuple(str(t).strip() for t in required_tags_raw if str(t).strip())

    release_subfolder = str(raw.get("release_subfolder") or pack_id).strip()
    if not _SEGMENT_RE.fullmatch(release_subfolder):
        raise ToolError(
            f"template pack {pack_id!r}: release_subfolder must be a plain path segment "
            f"matching {_SEGMENT_RE.pattern!r}"
        )

    return TemplatePack(
        id=pack_id,
        title=str(raw.get("title") or pack_id),
        description=str(raw.get("description") or ""),
        reviewer_role=reviewer_role,
        required_tags=required_tags,
        release_subfolder=release_subfolder,
    )


def load_template_pack(pack_id: str) -> TemplatePack:
    """Load and validate one template pack by id. Raises ``ToolError`` on any

    problem — an unknown id, a malformed file, or a missing required field.
    Fails closed: a broken pack must never silently pass validation.
    """
    path = _pack_path(pack_id)
    if not path.is_file():
        raise ToolError(
            f"unknown template pack {pack_id!r}; looked in {config.work_product_template_dir()}"
        )
    return _load_pack_file(path)


def list_template_packs() -> list[dict[str, Any]]:
    """All valid packs found in the configured template directory."""
    pack_dir = config.work_product_template_dir()
    if not pack_dir.is_dir():
        return []
    packs: list[dict[str, Any]] = []
    for path in sorted(pack_dir.glob("*.yaml")):
        try:
            packs.append(_load_pack_file(path).to_dict())
        except ToolError:
            continue
    return packs


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _require_enabled() -> None:
    if not config.work_product_enabled():
        raise ToolError(
            "the work-product release wedge is disabled; set UA_WORK_PRODUCT_ENABLED=1 "
            "to enable work_product_submit and the `unified-agent work-product` CLI"
        )


@contextmanager
def _work_product_release_lock(work_product_id: int):
    """Own this work product's release across threads and process restarts.

    ``flock`` is released by the OS when its process exits, including an abrupt
    termination. A later retry can therefore distinguish an abandoned consumed
    pass from a live release without a lease timeout that could expire under a
    slow but healthy release.
    """
    db_path = Path(config.db_path()).resolve()
    lock_path = db_path.with_name(f".{db_path.name}.work-product-{work_product_id}.lock")
    coordinator_path = db_path.with_name(f".{db_path.name}.work-product-locks.lock")
    coordinator_fd = os.open(coordinator_path, os.O_CREAT | os.O_RDWR, 0o600)
    fd: int | None = None
    coordinator_locked = False
    locked = False
    try:
        fcntl.flock(coordinator_fd, fcntl.LOCK_EX)
        coordinator_locked = True
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ToolError(
                f"work product {work_product_id} release is already in progress; retry shortly"
            ) from None
        locked = True
        fcntl.flock(coordinator_fd, fcntl.LOCK_UN)
        coordinator_locked = False
        yield
    finally:
        try:
            if locked:
                # No process may open or lock this product inode while the
                # coordinator is held, so it is safe to unlink after unlock.
                fcntl.flock(coordinator_fd, fcntl.LOCK_EX)
                coordinator_locked = True
                fcntl.flock(fd, fcntl.LOCK_UN)
                lock_path.unlink(missing_ok=True)
        finally:
            if coordinator_locked:
                fcntl.flock(coordinator_fd, fcntl.LOCK_UN)
            if fd is not None:
                os.close(fd)
            os.close(coordinator_fd)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_to_dict(row: Any) -> dict[str, Any]:
    record = {key: row[key] for key in row.keys()}
    try:
        record["tags"] = json.loads(record.get("tags") or "{}")
    except (TypeError, ValueError):
        record["tags"] = {}
    return record


# A work product belongs to the tenant that submitted it (task #1834); another
# tenant's row behaves exactly like a missing row.
_OWNER_SQL = "org_id IS ?"


def get_work_product(work_product_id: int) -> dict[str, Any] | None:
    """Read one work-product row, tags decoded. ``None`` if it does not exist."""
    conn = storage.connect()
    try:
        row = conn.execute(
            f"SELECT * FROM work_products WHERE id=? AND {_OWNER_SQL}",
            (work_product_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row is not None else None


def list_work_products(
    status: str | None = None, template_pack: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """List work-product rows, newest first. Optional status/pack filters."""
    agent_access.enforce("work_product_list", read_only=True)
    clauses: list[str] = [_OWNER_SQL]
    params: list[Any] = [edition.tenant_org_id()]
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if template_pack is not None:
        clauses.append("template_pack = ?")
        params.append(template_pack)
    where = f"WHERE {' AND '.join(clauses)}"
    lim = max(1, min(limit, 500))
    params.append(lim)
    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM work_products {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
    finally:
        conn.close()
    return {"work_products": [_row_to_dict(r) for r in rows]}


def _transition(
    mutate: Callable[[Any], Any],
    *,
    channel: str,
    payload: Callable[[Any], dict[str, Any]],
    originator: str | None,
    happened: Callable[[Any], bool] = bool,
) -> Any:
    """Apply one state change and append its ledger event in ONE transaction.

    ``mutate(conn)`` performs the row change and returns a truthy result when
    the transition actually happened (a guarded UPDATE that matched, a fresh
    INSERT) or a falsy one for a no-op. The event is appended only for a real
    transition, on the same connection, before the single commit — so the
    row and its evidence either both land or neither does, and a crash
    between them can no longer leave a released row with no release event.
    Socket fan-out happens after commit and is best-effort, as everywhere.
    """
    conn = storage.connect()
    published: dict[str, Any] | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = mutate(conn)
        if happened(result):
            _, _, published = events.record_event_once_on_connection(
                conn, channel, payload=payload(result), originator=originator
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    if published is not None:
        events.publish(published)
    return result


def _canonical_tags(tags: dict[str, str]) -> str:
    return json.dumps(tags, sort_keys=True)


def _find_or_create(
    *,
    title: str,
    content_sha256: str,
    source_path: str,
    size_bytes: int,
    mime_type: str | None,
    producing_agent: str,
    producing_model: str | None,
    template_pack: str,
    tags: dict[str, str],
    submitted_by: str,
) -> tuple[int, bool]:
    """Find the open or released row for this exact resubmission, or insert one.

    A retry is "the same submission" only when every reviewed attribute
    matches: bytes, pack, title, agent, model and tags. Changing the tags or
    the model starts a fresh submission (and a fresh review) rather than
    silently releasing the bytes under the first submission's metadata. That
    identity is what lets the approval-gate retry idiom work here and makes a
    replay after release a no-op. A rejected or tampered row is final, so
    resubmitting those bytes starts a fresh review. The insert and its
    ``work_product.submitted`` event commit together.
    """
    reusable = (*_OPEN_STATUSES, "released")
    canonical_tags = _canonical_tags(tags)
    placeholders = ",".join("?" for _ in reusable)

    def insert(tx: Any) -> dict[str, Any]:
        # The lookup runs under the same BEGIN IMMEDIATE write lock as the
        # insert, so two concurrent identical submissions converge on one row.
        existing = tx.execute(
            f"SELECT id FROM work_products WHERE content_sha256=? AND template_pack=? "
            f"AND producing_agent=? AND title=? AND producing_model IS ? AND tags=? "
            f"AND status IN ({placeholders}) AND {_OWNER_SQL} ORDER BY id DESC LIMIT 1",
            (
                content_sha256,
                template_pack,
                producing_agent,
                title,
                producing_model,
                canonical_tags,
                *reusable,
                edition.tenant_org_id(),
            ),
        ).fetchone()
        if existing is not None:
            return {"id": int(existing["id"]), "created": False}
        # A new version of an earlier submission: same title under the same
        # pack, different bytes. Linking it lets the reviewer see what changed
        # (work_product_diff) instead of re-reading the whole document.
        previous = tx.execute(
            "SELECT id FROM work_products WHERE template_pack=? AND title=? "
            f"AND content_sha256<>? AND {_OWNER_SQL} ORDER BY id DESC LIMIT 1",
            (template_pack, title, content_sha256, edition.tenant_org_id()),
        ).fetchone()
        previous_version_id = int(previous["id"]) if previous is not None else None
        ts = storage.now()
        cur = tx.execute(
            "INSERT INTO work_products (title, content_sha256, source_path, size_bytes, "
            "mime_type, producing_agent, producing_model, template_pack, tags, status, "
            "previous_version_id, submitted_by, created_at, updated_at, org_id) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?)",
            (
                title,
                content_sha256,
                source_path,
                size_bytes,
                mime_type,
                producing_agent,
                producing_model,
                template_pack,
                canonical_tags,
                previous_version_id,
                submitted_by,
                ts,
                ts,
                edition.tenant_org_id(),
            ),
        )
        return {
            "id": int(cur.lastrowid),
            "previous_version_id": previous_version_id,
            "created": True,
        }

    result = _transition(
        insert,
        channel="work_product.submitted",
        payload=lambda result: {
            "work_product_id": result["id"],
            "title": title,
            "content_sha256": content_sha256,
            "size_bytes": size_bytes,
            "template_pack": template_pack,
            "tags": tags,
            "producing_agent": producing_agent,
            "producing_model": producing_model,
            "previous_version_id": result["previous_version_id"],
        },
        originator=submitted_by,
        happened=lambda outcome: outcome["created"],
    )
    return result["id"], result["created"]


def _link_approval(
    work_product_id: int, approval_id: int, expected_payload: dict[str, Any]
) -> None:
    conn = storage.connect()
    try:
        new_approval = conn.execute(
            "SELECT tool, payload, status FROM approvals WHERE id=? AND org_id IS ?",
            (approval_id, edition.tenant_org_id()),
        ).fetchone()
        if (
            new_approval is None
            or new_approval["tool"] != "work_product_release"
            or new_approval["payload"] != approvals._canonical_payload(expected_payload)
        ):
            raise ToolError(
                f"replacement approval {approval_id} does not match the work-product payload"
            )
        row = conn.execute(
            f"SELECT approval_id FROM work_products WHERE id=? AND {_OWNER_SQL}",
            (work_product_id, edition.tenant_org_id()),
        ).fetchone()
        if row is None:
            raise ToolError(f"work product {work_product_id} disappeared while linking approval")
        old_id = row["approval_id"]
        if old_id is None or old_id == approval_id:
            conn.execute(
                "UPDATE work_products SET approval_id=?, updated_at=? WHERE id=?",
                (approval_id, storage.now(), work_product_id),
            )
        else:
            old = conn.execute(
                "SELECT status, payload FROM approvals WHERE id=?", (old_id,)
            ).fetchone()
            # A pack edit can change the approval payload while the previous
            # approval is still approved. Once a new exact-payload request is
            # parked, link it immediately so a crash after that pass is
            # consumed can recover it. The old pass is not applicable to the
            # current submission; preserve its approval record unchanged.
            stale_payload = old is not None and old["payload"] != approvals._canonical_payload(
                expected_payload
            )
            if old is not None and (
                old["status"] in {"expired", "revoked", "consumed"} or stale_payload
            ):
                conn.execute(
                    "UPDATE work_products SET approval_id=?, updated_at=? "
                    "WHERE id=? AND approval_id=?",
                    (approval_id, storage.now(), work_product_id, old_id),
                )
        conn.commit()
    finally:
        conn.close()


def _mark_rejected(work_product_id: int, approval_row: dict[str, Any]) -> None:
    """Transition an open row to 'rejected' on a denied approval, once.

    Guarded by the status IN (...) clause so a second caller observing the
    same denial (a retried submit and a `work_product_verify` self-heal
    racing each other) never appends a second rejection event.
    """
    approval_id = approval_row.get("id")
    reason = approval_row.get("reason")
    resolved_by = approval_row.get("resolved_by")

    def update(tx: Any) -> bool:
        ts = storage.now()
        placeholders = ",".join("?" for _ in _OPEN_STATUSES)
        cur = tx.execute(
            f"UPDATE work_products SET status='rejected', reject_reason=?, resolved_by=?, "
            f"resolved_at=?, updated_at=? WHERE id=? AND status IN ({placeholders})",
            (reason, resolved_by, ts, ts, work_product_id, *_OPEN_STATUSES),
        )
        return cur.rowcount == 1

    _transition(
        update,
        channel="work_product.rejected",
        payload=lambda _: {
            "work_product_id": work_product_id,
            "approval_id": approval_id,
            "reason": reason,
            "resolved_by": resolved_by,
        },
        originator=resolved_by or config.verified_agent_identity(),
    )


def _mark_tampered(work_product_id: int, *, expected: str, actual: str, stage: str) -> None:
    def update(tx: Any) -> bool:
        ts = storage.now()
        cur = tx.execute(
            "UPDATE work_products SET status='tampered', updated_at=? "
            "WHERE id=? AND status<>'tampered'",
            (ts, work_product_id),
        )
        if cur.rowcount == 1:
            # A pending or approved pass must not survive a terminal tamper
            # verdict. A pass already consumed by _release remains consumed.
            tx.execute(
                "UPDATE approvals SET status='revoked', reason=?, updated_at=? "
                "WHERE id=(SELECT approval_id FROM work_products WHERE id=?) "
                "AND status IN ('pending','approved')",
                ("work product source tampered before release", ts, work_product_id),
            )
        return cur.rowcount == 1

    _transition(
        update,
        channel="work_product.tampered",
        payload=lambda _: {
            "work_product_id": work_product_id,
            "stage": stage,
            "expected_sha256": expected,
            "actual_sha256": actual,
        },
        originator=config.verified_agent_identity(),
    )


def _write_release_copy(dest_path: Path, data: bytes, *, expected_sha256: str) -> None:
    """Atomically create the released copy without replacing an existing record.

    Write to a temporary file in the destination directory, then hard-link it
    into place. A process exit during the write can leave only a temporary
    file, never a partial destination. A destination already present is
    accepted only when byte-identical (a retry after a failed commit); a
    different record is refused rather than overwritten.
    """
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ToolError(
            f"refusing to release bytes with SHA-256 {actual_sha256}; expected {expected_sha256}"
        )
    # The caller holds the work-product release lock. A deterministic name
    # lets a retry remove a temp file left by a process exit without touching
    # another live release attempt.
    temporary_path = dest_path.with_name(f".{dest_path.name}.tmp")
    temporary_path.unlink(missing_ok=True)
    fd = os.open(
        temporary_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o666,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, dest_path)
        except FileExistsError:
            if _sha256_file(dest_path) != expected_sha256:
                raise ToolError(
                    f"refusing to overwrite an existing, different file at {dest_path}"
                ) from None
    finally:
        temporary_path.unlink(missing_ok=True)


def _release(work_product_id: int, approval_id: int) -> dict[str, Any]:
    """Perform the release: copy bytes, re-hash on copy, append evidence.

    Runs inside ``approvals.approval_invocation()`` (see
    :func:`work_product_submit`): any failure before a terminal outcome —
    a missing source, an unwritable release folder, a failed commit —
    restores the consumed one-shot pass, so the retry completes the approved
    release instead of parking a second approval. The pass is spent only at
    a terminal outcome (``approvals.mark_governed_execution``): a committed
    release, or a tamper verdict.

    Persists through 'approved' before 'released' so the stored lifecycle
    matches what is documented (submitted -> approved -> released).
    """
    row = get_work_product(work_product_id)
    if row is None:
        raise ToolError(f"work product {work_product_id} disappeared before release")
    if row["status"] == "released":
        return dict(row, note="already released")

    approval_row = approvals.get_approval(approval_id) or {}
    resolved_by = approval_row.get("resolved_by")

    def approve(tx: Any) -> bool:
        ts = storage.now()
        cur = tx.execute(
            "UPDATE work_products SET status='approved', approval_id=?, resolved_by=?, "
            "resolved_at=?, updated_at=? WHERE id=? AND status='submitted'",
            (approval_id, resolved_by, ts, ts, work_product_id),
        )
        return cur.rowcount == 1

    _transition(
        approve,
        channel="work_product.approved",
        payload=lambda _: {
            "work_product_id": work_product_id,
            "approval_id": approval_id,
            "resolved_by": resolved_by,
            "content_sha256": row["content_sha256"],
        },
        originator=resolved_by or config.verified_agent_identity(),
    )

    # Re-hash the immutable bytes even after the ID-based retry checked the
    # source: another process can replace the file between that check and copy.
    source = Path(row["source_path"])
    if not source.is_file():
        raise ToolError(
            f"work product {work_product_id} was approved, but its source file is missing: "
            f"{source}; restore it and retry — the approval is kept"
        )
    # Verify the exact immutable byte snapshot that will be copied. Hashing
    # the path and then reading it again leaves a source-swap window.
    data = source.read_bytes()
    current_hash = hashlib.sha256(data).hexdigest()
    if current_hash != row["content_sha256"]:
        approvals.mark_governed_execution()
        _mark_tampered(
            work_product_id,
            expected=row["content_sha256"],
            actual=current_hash,
            stage="pre_release",
        )
        raise ToolError(
            f"tamper detected before release: work product {work_product_id}'s source "
            f"file changed since submission (expected {row['content_sha256']}, found "
            f"{current_hash})"
        )

    # The destination is part of what was approved: a pack edited after the
    # approval must not redirect the release.
    pack = load_template_pack(row["template_pack"])
    approved_subfolder = (approval_row.get("payload") or {}).get("release_subfolder")
    if approved_subfolder != pack.release_subfolder:
        raise ToolError(
            f"template pack {pack.id!r} now releases to {pack.release_subfolder!r}, but "
            f"approval {approval_id} was granted for {approved_subfolder!r}; resubmit for a "
            "fresh approval"
        )
    dest_dir = config.work_product_release_dir() / pack.release_subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{work_product_id}-{source.name}"
    _write_release_copy(dest_path, data, expected_sha256=row["content_sha256"])
    released_sha256 = hashlib.sha256(data).hexdigest()
    if released_sha256 != row["content_sha256"]:
        approvals.mark_governed_execution()
        _mark_tampered(
            work_product_id,
            expected=row["content_sha256"],
            actual=released_sha256,
            stage="post_copy",
        )
        raise ToolError(
            f"work product {work_product_id} released copy hash mismatch (expected "
            f"{row['content_sha256']}, wrote {released_sha256})"
        )

    def release(tx: Any) -> bool:
        ts = storage.now()
        cur = tx.execute(
            "UPDATE work_products SET status='released', released_path=?, released_sha256=?, "
            "released_at=?, updated_at=? WHERE id=? AND status='approved'",
            (str(dest_path), released_sha256, ts, ts, work_product_id),
        )
        return cur.rowcount == 1

    _transition(
        release,
        channel="work_product.released",
        payload=lambda _: {
            "work_product_id": work_product_id,
            "approval_id": approval_id,
            "human": resolved_by,
            # The approving human's device is NOT this caller's: release runs
            # on the submitting agent's retry. The human's verified device
            # rides the signed approval producer event, which
            # work_product_evidence() joins by approval id.
            "releasing_device": config.current_peer_device(),
            "producing_agent": row["producing_agent"],
            "producing_model": row["producing_model"],
            "document_sha256": released_sha256,
            "released_path": str(dest_path),
            "template_pack": row["template_pack"],
        },
        originator=resolved_by or config.verified_agent_identity(),
    )
    # Spent only once the release is durable. A failure before this point
    # (including the commit itself) restores the pass; one after it is
    # harmless, because a replay returns "already released" before any gate.
    approvals.mark_governed_execution()

    return get_work_product(work_product_id) or row


def _finish_submission(
    work_product_id: int,
    *,
    title: str,
    content_sha256: str,
    pack: TemplatePack,
    tags: dict[str, str],
    model: str | None,
    identity: str,
) -> dict[str, Any]:
    """Finish a submission while its per-product release lock is held."""
    row = get_work_product(work_product_id)
    if row is None:  # pragma: no cover - defensive, row was just written
        raise ToolError(f"work product {work_product_id} could not be read back after submit")
    if row["status"] == "released":
        return dict(row, note="already released")
    if row["status"] == "tampered":
        raise ToolError(
            f"work product {work_product_id} failed verification (tampered) and cannot "
            "be released; resubmit a fresh copy"
        )
    if row["status"] == "rejected":
        raise ToolError(
            f"work product {work_product_id} was rejected"
            + (f": {row.get('reject_reason')}" if row.get("reject_reason") else "")
        )

    approval_id = row.get("approval_id")
    approval_row = approvals.get_approval(int(approval_id)) if approval_id is not None else None
    if approval_row is not None and approval_row["status"] == "denied":
        _mark_rejected(work_product_id, approval_row)
        reason = approval_row.get("reason") or "no reason given"
        raise ToolError(
            f"work product {work_product_id} was rejected by approval {approval_id}: {reason}"
        )

    if approval_row is not None and approval_row["status"] == "consumed":
        # The exclusive OS lock proves the original consumer is no longer
        # active. flock is released on process exit, so this branch resumes a
        # release interrupted after consuming approval instead of getting
        # stuck forever or creating a second approval. _release is retry-safe:
        # it accepts an identical existing copy and commits each lifecycle
        # transition with its event.
        with approvals.approval_invocation() as receipts:
            receipts["recovered_consumed_pass"] = {"approval_id": int(approval_id)}
            return _release(work_product_id, int(approval_id))

    gate_payload = {
        "work_product_id": work_product_id,
        "title": title,
        "content_sha256": content_sha256,
        "template_pack": pack.id,
        "reviewer_role": pack.reviewer_role,
        "release_subfolder": pack.release_subfolder,
        "tags": tags,
        "producing_model": model,
        "previous_version_id": row.get("previous_version_id"),
    }
    with approvals.approval_invocation():
        try:
            gate = approvals.check_approval_gate(
                "work_product_release", gate_payload, requested_by=identity, force=True
            )
        except approvals.ApprovalPendingError as pending:
            if pending.approval_id is not None:
                _link_approval(work_product_id, pending.approval_id, gate_payload)
            raise
        return _release(work_product_id, int(gate["approval_id"]))


def work_product_submit(
    source_path: str,
    title: str,
    template_pack: str,
    *,
    tags: dict[str, str] | None = None,
    model: str | None = None,
    resume_work_product_id: int | None = None,
) -> dict[str, Any]:
    """Submit a document/file for named-reviewer approval and evidence-backed release.

    Vertical specifics (which tags are required, who reviews, where the
    release lands) come entirely from ``template_pack`` (see
    :func:`load_template_pack`) — this function hardcodes none of them.

    Always parks a ``require_approval`` request in the existing approval
    queue on first call (``ApprovalPendingError``, same UX as
    ``managed_secret_set``) — the submitter can never approve its own
    submission (``approvals.SEPARATION_OF_DUTIES_TOOLS``).
    A human resolves the pending row with ``approval_resolve``; calling this
    function again with the identical file/title/pack finds the same
    submission and either performs the release (approved) or records the
    rejection (denied) and raises.

    ``resume_work_product_id`` binds an HTTP release retry to one stored row;
    it never creates a replacement submission and records source tampering.

    Returns the stored work-product row, plus a ``note`` field on a no-op
    replay (already released).
    """
    _require_enabled()
    agent_access.enforce("work_product_submit", read_only=False)
    # Same fallback server.py's own instrumentation already uses for a local,
    # non-HTTP caller (UA_AGENT_LABEL / AGENT_LABEL): an MCP session carries a
    # verified bearer identity, but the local `unified-agent work-product`
    # CLI — the pilot's own on-prem install, no remote hub involved — never
    # goes through that HTTP auth layer at all, so it would otherwise never
    # have an identity to stamp as the producing agent.
    identity = (
        config.verified_agent_identity()
        or os.environ.get("UA_AGENT_LABEL")
        or os.environ.get("AGENT_LABEL")
    )
    if not identity or not identity.strip():
        raise ToolError(
            "work product submission requires an authenticated agent identity "
            "(an MCP bearer session, or UA_AGENT_LABEL for the local CLI)"
        )
    identity = identity.strip()
    if not isinstance(title, str) or not title.strip():
        raise ToolError("title must be a non-empty string")
    title = title.strip()

    tag_map = {str(k): str(v) for k, v in (tags or {}).items()}
    if resume_work_product_id is not None:
        # ID-addressed retry never does a content-hash lookup or inserts a row.
        work_product_id = int(resume_work_product_id)
        if work_product_id <= 0:
            raise ToolError("work product id must be positive")
        with _work_product_release_lock(work_product_id):
            row = get_work_product(work_product_id)
            if row is None:
                raise ToolError(f"unknown work product: {work_product_id}")
            if row["producing_agent"] != identity:
                raise ToolError("only the submitter may retry release")
            if (
                row["source_path"] != str(source_path)
                or row["title"] != title
                or row["template_pack"] != template_pack
                or row["tags"] != tag_map
                or row["producing_model"] != model
            ):
                raise ToolError(f"work product {work_product_id} inputs differ from submission")
            if row["status"] == "released":
                return dict(row, note="already released")
            if row["status"] == "rejected":
                raise ToolError(f"work product {work_product_id} was rejected")
            if row["status"] == "tampered":
                raise ToolError(
                    f"work product {work_product_id} was tampered and cannot be released"
                )

            pack = load_template_pack(template_pack)
            missing = sorted(t for t in pack.required_tags if t not in tag_map)
            if missing:
                raise ToolError(
                    f"template pack {pack.id!r} requires tags {missing}; got {sorted(tag_map)}"
                )
            path = safety.resolve_in_workspace(str(source_path))
            if not path.is_file():
                raise ToolError(
                    f"source path does not exist or is not a regular file: {source_path}"
                )
            current_hash = _sha256_file(path)
            if current_hash != row["content_sha256"]:
                _mark_tampered(
                    work_product_id,
                    expected=row["content_sha256"],
                    actual=current_hash,
                    stage="pre_release",
                )
                raise ToolError(
                    f"work product {work_product_id} source changed since submission; "
                    "submission is now tampered"
                )
            return _finish_submission(
                work_product_id,
                title=title,
                content_sha256=row["content_sha256"],
                pack=pack,
                tags=tag_map,
                model=model,
                identity=identity,
            )

    pack = load_template_pack(template_pack)
    missing = sorted(t for t in pack.required_tags if t not in tag_map)
    if missing:
        raise ToolError(f"template pack {pack.id!r} requires tags {missing}; got {sorted(tag_map)}")

    # Sandboxed like every other file-reading tool: a submission can only name
    # a file inside the workspace (or the caller's active task worktree), so
    # the tool can't be used to read and "release" arbitrary host files.
    path = safety.resolve_in_workspace(str(source_path))
    if not path.is_file():
        raise ToolError(f"source path does not exist or is not a regular file: {source_path}")
    content_sha256 = _sha256_file(path)
    size_bytes = path.stat().st_size
    mime_type = mimetypes.guess_type(path.name)[0]

    work_product_id, _created = _find_or_create(
        title=title,
        content_sha256=content_sha256,
        source_path=str(path),
        size_bytes=size_bytes,
        mime_type=mime_type,
        producing_agent=identity,
        producing_model=model,
        template_pack=pack.id,
        tags=tag_map,
        submitted_by=identity,
    )
    with _work_product_release_lock(work_product_id):
        return _finish_submission(
            work_product_id,
            title=title,
            content_sha256=content_sha256,
            pack=pack,
            tags=tag_map,
            model=model,
            identity=identity,
        )


def work_product_verify(work_product_id: int) -> dict[str, Any]:
    """Re-hash the source (and released copy, if any) against the stored hash.

    Self-heals a denied-but-never-retried submission: if the linked approval
    was denied and nobody retried the submit call, this call notices and
    records the rejection right here, so ``verify``/``list`` never show a
    stale 'submitted' status for a request a human already said no to.

    Flags ``tampered: true`` on a mismatch, marking the row 'tampered' the
    first time it is detected (idempotent past that point). Before release
    the source must still match; after release only the released copy
    counts (it is the record) — a missing or altered released copy is
    tampering, an edited draft is not.
    """
    # Verify can record a rejection or a tamper verdict, so it is a write for
    # agent-access purposes. It deliberately does not require
    # UA_WORK_PRODUCT_ENABLED: detecting tampering must keep working after the
    # submit path is switched off.
    agent_access.enforce("work_product_verify", read_only=False)
    numeric_id = int(work_product_id)
    row = get_work_product(numeric_id)
    if row is None:
        raise ToolError(f"unknown work product: {numeric_id}")

    approval_id = row.get("approval_id")
    if row["status"] in _OPEN_STATUSES and approval_id is not None:
        approval_row = approvals.get_approval(int(approval_id))
        if approval_row is not None and approval_row["status"] == "denied":
            _mark_rejected(numeric_id, approval_row)
            row = get_work_product(numeric_id) or row

    source_path = Path(row["source_path"]) if row.get("source_path") else None
    source_exists = bool(source_path and source_path.is_file())
    source_hash = _sha256_file(source_path) if source_exists and source_path else None
    source_matches = (source_hash == row["content_sha256"]) if source_exists else None

    released_path = Path(row["released_path"]) if row.get("released_path") else None
    released_exists = bool(released_path and released_path.is_file())
    released_hash = _sha256_file(released_path) if released_exists and released_path else None
    released_matches = (
        (released_hash == row["released_sha256"])
        if released_exists and row.get("released_sha256")
        else None
    )

    # Once released, the released copy is the record: a later edit to the
    # draft it came from is normal authoring, reported but not tampering.
    if row["status"] == "released":
        newly_tampered = released_matches is False or (
            released_path is not None and not released_exists
        )
    elif row["status"] in _OPEN_STATUSES:
        # Before release the draft is the only copy of the reviewed bytes:
        # losing it is as disqualifying as altering it.
        newly_tampered = not source_exists or source_matches is False
    else:
        newly_tampered = False
    if newly_tampered and row["status"] != "tampered":
        expected = row["content_sha256"]
        if row["status"] == "released":
            actual = released_hash or "missing"
        else:
            actual = source_hash or "missing"
        _mark_tampered(numeric_id, expected=expected, actual=actual, stage="verify")
        row = get_work_product(numeric_id) or row

    return {
        "work_product_id": numeric_id,
        "status": row["status"],
        "content_sha256": row["content_sha256"],
        "released_sha256": row.get("released_sha256"),
        "source_exists": source_exists,
        "source_hash_matches": source_matches,
        "released_exists": released_exists,
        "released_hash_matches": released_matches,
        "tampered": row["status"] == "tampered",
        "approval_id": row.get("approval_id"),
        "resolved_by": row.get("resolved_by"),
        "reject_reason": row.get("reject_reason"),
    }


# ---------------------------------------------------------------------------
# Evidence chain and reviewer diff
# ---------------------------------------------------------------------------


def _ledger_rows(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    conn = storage.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [
        {
            "ulid": r["ulid"],
            "channel": r["channel"],
            "payload": producer_sign.parse_event_payload(r["payload"]),
            "originator": r["originator"],
            "created_at": r["created_at"],
            "entry_hash": r["entry_hash"],
        }
        for r in rows
    ]


def _file_hashes(row: dict[str, Any]) -> dict[str, Any]:
    """Current on-disk hashes of a row's source and released copy (``None`` if absent)."""
    out: dict[str, Any] = {}
    for key in ("source", "released"):
        raw = row.get(f"{key}_path")
        path = Path(raw) if raw else None
        exists = bool(path and path.is_file())
        out[f"{key}_exists"] = exists
        out[f"{key}_hash"] = _sha256_file(path) if exists and path else None
    return out


def work_product_evidence(work_product_id: int) -> dict[str, Any]:
    """The accountability chain for one work product, read from the ledger.

    ``human -> device -> agent -> model -> document hash -> approval``. Every
    link is read back from the hash-chained evidence ledger, never from the
    row alone, and each carries how it is known:

    * the **human** is ``verified`` only when a signature-valid producer event
      for this approval id names the same identity that resolved it
      (``verified_via`` says whether that rests on an IdP session or only on
      an agent bearer — an agent bearer is never presented as IdP-verified,
      the #1217 honesty rule);
    * the **device** comes from that same signed event's tailnet node, or is
      reported unverified — never a bare hostname;
    * the whole answer is withheld as unverified when the ledger chain itself
      does not verify (``verifying_producer_events`` drops every producer);
    * agent, model and document hash come from the hash-chained
      ``work_product.submitted`` / ``work_product.released`` payloads, not the
      mutable row. They are cross-checked against the row and the files on
      disk; any disagreement or missing event sets ``consistent: false`` and
      is listed in ``mismatches``.

    Read-only: it never changes a row's status (``verify`` does that).
    """
    agent_access.enforce("work_product_evidence", read_only=True)
    numeric_id = int(work_product_id)
    row = get_work_product(numeric_id)
    if row is None:
        raise ToolError(f"unknown work product: {numeric_id}")

    chain = events.verify_event_chain()
    chain_valid = bool(chain.get("verified"))

    lifecycle = _ledger_rows(
        "SELECT ulid, channel, payload, originator, created_at, entry_hash FROM event_log "
        "WHERE channel LIKE 'work\\_product.%' ESCAPE '\\' "
        "AND CAST(json_extract(payload, '$.work_product_id') AS TEXT) = ? ORDER BY ulid ASC",
        (str(numeric_id),),
    )
    approval_id = row.get("approval_id")
    producers: list[dict[str, Any]] = []
    if approval_id is not None:
        producer_rows = _ledger_rows(
            "SELECT ulid, channel, payload, originator, created_at, entry_hash FROM event_log "
            "WHERE channel = ? AND json_extract(payload, '$.kind') = 'approval' "
            "AND CAST(json_extract(payload, '$.ref_id') AS TEXT) = ? ORDER BY ulid ASC",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED, str(approval_id)),
        )
        producers = producer_sign.verifying_producer_events(producer_rows, chain_valid=chain_valid)

    by_channel: dict[str, dict[str, Any]] = {}
    for event in lifecycle:
        by_channel.setdefault(event["channel"], event["payload"])
    submitted = by_channel.get("work_product.submitted")
    released_event = by_channel.get("work_product.released")

    mismatches: list[str] = []
    if submitted is None:
        mismatches.append("no work_product.submitted event in the ledger")
    submitted = submitted or {}
    for field, row_field in (
        ("producing_agent", "producing_agent"),
        ("producing_model", "producing_model"),
        ("content_sha256", "content_sha256"),
        ("template_pack", "template_pack"),
        ("tags", "tags"),
    ):
        if submitted and submitted.get(field) != row.get(row_field):
            mismatches.append(f"row {row_field} disagrees with the submitted event")
    if row["status"] == "released":
        if released_event is None:
            mismatches.append("row is released but no work_product.released event exists")
        elif released_event.get("document_sha256") != row.get("released_sha256"):
            mismatches.append("row released_sha256 disagrees with the released event")

    files = _file_hashes(row)
    if row["status"] == "released":
        if files["released_hash"] != (released_event or {}).get("document_sha256"):
            mismatches.append("released copy on disk no longer matches the ledger hash")
    elif row["status"] in _OPEN_STATUSES:
        if files["source_hash"] != submitted.get("content_sha256"):
            mismatches.append("source on disk no longer matches the submitted hash")

    resolved_by = row.get("resolved_by")
    if released_event is not None and released_event.get("human") != resolved_by:
        mismatches.append("row resolved_by disagrees with the released event")
    matching = [p for p in producers if resolved_by and p.get("identity") == resolved_by]
    signed = matching[-1] if matching else None
    human = {
        "identity": resolved_by,
        "verified": signed is not None,
        "verified_via": signed.get("verified_via") if signed else None,
        "producer_event": signed.get("ulid") if signed else None,
    }
    device_claim = signed.get("device") if signed else None
    device = (
        {"verified": True, "node": device_claim.get("node"), "node_id": device_claim.get("node_id")}
        if isinstance(device_claim, dict) and device_claim.get("node")
        else {"verified": False}
    )

    return {
        "work_product_id": numeric_id,
        "title": row["title"],
        "status": row["status"],
        "template_pack": row["template_pack"],
        "tags": row["tags"],
        "chain": {
            "human": human,
            "device": device,
            "agent": submitted.get("producing_agent"),
            "model": submitted.get("producing_model"),
            "document_sha256": submitted.get("content_sha256"),
            "released_sha256": (released_event or {}).get("document_sha256"),
            "approval_id": approval_id,
        },
        "consistent": not mismatches,
        "mismatches": mismatches,
        "files": files,
        "previous_version_id": row.get("previous_version_id"),
        "ledger": {
            "chain_status": chain.get("status"),
            "chain_valid": chain_valid,
            "events": [
                {"ulid": e["ulid"], "channel": e["channel"], "entry_hash": e["entry_hash"]}
                for e in lifecycle
            ],
        },
    }


def _readable_bytes(row: dict[str, Any]) -> bytes | None:
    """The approved bytes of a row, or ``None`` if no on-disk copy still matches.

    Prefers the released copy (it is the record) and falls back to the
    source. A copy whose hash no longer matches is never diffed — a reviewer
    must not be shown a tampered "previous version".
    """
    candidates = [
        (row.get("released_path"), row.get("released_sha256")),
        (row.get("source_path"), row.get("content_sha256")),
    ]
    for raw_path, expected in candidates:
        if not raw_path or not expected:
            continue
        path = Path(raw_path)
        if not path.is_file() or path.stat().st_size > _DIFF_MAX_BYTES:
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() == expected:
            return data
    return None


def _is_text(row: dict[str, Any]) -> bool:
    mime = row.get("mime_type") or ""
    suffix = Path(row.get("source_path") or "").suffix.lower()
    return mime.startswith("text/") or suffix in _TEXT_SUFFIXES


def work_product_diff(work_product_id: int) -> dict[str, Any]:
    """Unified diff of a work product against the version it supersedes.

    The reviewer's "what changed" view. Text documents only (by MIME type or
    suffix, ≤ 1 MiB); anything else reports ``diffable: false`` with the
    reason rather than guessing. Both sides must still hash to their recorded
    values, so a diff is never built from a tampered copy.
    """
    agent_access.enforce("work_product_diff", read_only=True)
    numeric_id = int(work_product_id)
    row = get_work_product(numeric_id)
    if row is None:
        raise ToolError(f"unknown work product: {numeric_id}")
    previous_id = row.get("previous_version_id")
    result: dict[str, Any] = {
        "work_product_id": numeric_id,
        "previous_version_id": previous_id,
        "diffable": False,
    }
    if previous_id is None:
        return dict(result, reason="first version: nothing to diff against")
    previous = get_work_product(int(previous_id))
    if previous is None:
        return dict(result, reason=f"previous version {previous_id} no longer exists")
    if not (_is_text(row) and _is_text(previous)):
        return dict(result, reason="not a text document; compare the hashes instead")

    current_bytes = _readable_bytes(row)
    previous_bytes = _readable_bytes(previous)
    if current_bytes is None or previous_bytes is None:
        return dict(result, reason="a version's bytes are missing, too large, or fail their hash")
    try:
        current_text = current_bytes.decode("utf-8").splitlines(keepends=True)
        previous_text = previous_bytes.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return dict(result, reason="not valid UTF-8 text")

    diff = "".join(
        difflib.unified_diff(
            previous_text,
            current_text,
            fromfile=f"work-product-{previous_id} ({previous['content_sha256'][:12]})",
            tofile=f"work-product-{numeric_id} ({row['content_sha256'][:12]})",
        )
    )
    return dict(result, diffable=True, diff=diff)
