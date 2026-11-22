"""MCP surface for immutable config revisions (task #1352).

``list``, ``diff``, and ``show`` are reads. ``capture`` records drifted live
config without changing it. ``commit`` writes through the existing store.
``rollback`` restores a prior revision only after the approval gate accepts
that exact restore; the restore is itself a new revision.
"""

from __future__ import annotations

from typing import Any

from .. import config_revisions


def config_revision_manage(
    action: str = "list",
    kind: str = "policy",
    revision: int | None = None,
    from_revision: int | None = None,
    to_revision: int | None = None,
    document: dict | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List, diff, show, capture, commit, or roll back authority config.

    ``kind`` is ``policy``, ``profile``, ``approval_rule``, or ``org``.
    Revisions are append-only evidence events. ``rollback`` is always
    approval-gated (``config_revision_rollback``) and does not delete history.
    The pass is bound to the live file, so a later edit needs a new approval.
    A commit that changes ``require_approval`` rules is gated the same way
    (``config_revision_commit``). A profile commit may only repeat an
    identity's code default or reduce its privilege without granting a tool
    that default refuses. ``UA_APPROVAL_TOOLS`` is observed and never written.
    """
    if action == "list":
        listed = config_revisions.list_revisions(kind, limit=limit)
        if from_revision is not None:
            listed["diff"] = config_revisions.diff_revisions(kind, from_revision, to_revision)
        return listed
    if action == "diff":
        if from_revision is None:
            raise config_revisions.ConfigRevisionError("diff requires from_revision")
        return config_revisions.diff_revisions(kind, from_revision, to_revision)
    if action == "show":
        if revision is None:
            raise config_revisions.ConfigRevisionError("show requires revision")
        return config_revisions.show_revision(kind, revision)
    if action == "capture":
        try:
            return config_revisions.capture(kind)
        except config_revisions.ConfigFanoutError as exc:
            return exc.result
    if action == "commit":
        if document is None:
            raise config_revisions.ConfigRevisionError("commit requires document")
        try:
            return config_revisions.commit(kind, document)
        except config_revisions.ConfigFanoutError as exc:
            return exc.result
    if action == "rollback":
        if revision is None:
            raise config_revisions.ConfigRevisionError("rollback requires revision")
        try:
            return config_revisions.rollback(kind, revision)
        except config_revisions.ConfigFanoutError as exc:
            return exc.result
    raise config_revisions.ConfigRevisionError(
        "action must be list, diff, show, capture, commit, or rollback"
    )
