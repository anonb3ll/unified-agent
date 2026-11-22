"""Why a pull request's head moved after its recorded review read (task #2398).

``shipping_preflight_cli.review_head_gate`` refuses a head that a rebase put
off every recorded read: no delta can be proven against a base that is no
longer an ancestor. That refusal is right, but it says nothing about *why* the
head moved, so a rebase that changed nothing reads exactly like one that
rewrote the diff. This module answers that question and names who owns the
next gate. It is report-only: nothing here changes a gate's conclusion.

``provenance`` compares what the reviewer read with what would merge, without a
checkout. GitHub's three-dot compare ``{base}...{sha}`` diffs ``sha`` against
``merge-base(base, sha)``, so it returns each side's own pull-request patch
even after ``base`` moved. Two sides whose normalized per-file patches match
are the same change re-applied (``rebase-identical``); anything GitHub cannot
show in full (truncation, a missing ``patch`` field, an outage) is ``unknown``,
never identical.

``record`` appends the answer to ``agent.review.head_change`` once per
``(reviewed_sha, current_head)`` so how often a rebase would have been bridged
can be measured before any gate relies on it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Callable
from typing import Any

from . import events, review_reads, storage

logger = logging.getLogger(__name__)

CHANNEL = "agent.review.head_change"
ORIGINATOR = "review-provenance"

#: Every value ``head_change_cause`` can take.
CAUSES = ("none", "no-read", "new-commits", "rebase-identical", "rebase-changed", "unknown")
#: Causes after which the old read no longer covers the head.
REREAD_CAUSES = frozenset({"no-read", "new-commits", "rebase-changed", "unknown"})

_FULL_READ_KINDS = frozenset(review_reads.FULL_READ_KINDS)
# Mirrors shipping_preflight_cli: GitHub's compare truncates `files` at 300
# entries, and a payload past this size is not parsed.
_MAX_COMPARE_FILES = 300
_COMPARE_JSON_LIMIT = 2 * 1024 * 1024
_GH_TIMEOUT = 30
_HUNK_HEADER = re.compile(r"^@@ [^@]* @@")


class _Unknown(Exception):
    """GitHub's answer cannot prove the cause; ``transient`` when a retry may."""

    def __init__(self, reason: str, *, transient: bool) -> None:
        super().__init__(reason)
        self.transient = transient


def _compare(repo: str, base: str, head: str, run_gh: Callable) -> dict:
    """One ``gh api`` compare call, with ``_changed_between``'s error handling."""
    try:
        result = run_gh(
            ["gh", "api", f"repos/{repo}/compare/{base}...{head}"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            raise _Unknown("Commit comparison is unavailable.", transient=True)
        if len(result.stdout) > _COMPARE_JSON_LIMIT:
            raise _Unknown("Commit comparison is too large to compare.", transient=False)
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise _Unknown("Commit comparison is unavailable.", transient=True) from None
    if not isinstance(payload, dict):
        raise _Unknown("Commit comparison is unavailable.", transient=True)
    return payload


def _files(payload: dict) -> list[dict]:
    files = payload.get("files")
    if payload.get("status") == "identical" and files is None:
        return []
    if not isinstance(files, list) or not all(isinstance(entry, dict) for entry in files):
        raise _Unknown("Commit comparison has no file list.", transient=True)
    if len(files) >= _MAX_COMPARE_FILES:
        raise _Unknown(
            f"Commit comparison lists {_MAX_COMPARE_FILES} or more files and may be truncated.",
            transient=False,
        )
    return files


def _names(files: list[dict]) -> set[str]:
    return {
        entry[key]
        for entry in files
        for key in ("filename", "previous_filename")
        if isinstance(entry.get(key), str)
    }


def normalize_patch(patch: str) -> str:
    """A patch with hunk-header line numbers dropped, so a rebase that only
    shifted a hunk compares equal. Context lines stay: keeping them is the
    conservative direction, since a change in surrounding code reads as a delta."""
    return "\n".join(
        _HUNK_HEADER.sub("@@", line) if line.startswith("@@") else line
        for line in patch.splitlines()
    )


def patch_map(files: list[dict]) -> dict[str, tuple[str, str, str]]:
    """``filename -> (status, previous_filename, sha256(normalized patch))``.

    Raises ``_Unknown`` when any file lacks a ``patch`` (GitHub omits it for
    binary and very large diffs): an unseen patch cannot be proven unchanged.
    """
    out: dict[str, tuple[str, str, str]] = {}
    for entry in files:
        name, patch = entry.get("filename"), entry.get("patch")
        if not isinstance(name, str):
            raise _Unknown("Commit comparison has a file without a name.", transient=False)
        if not isinstance(patch, str):
            raise _Unknown(f"GitHub returned no patch for {name}.", transient=False)
        digest = hashlib.sha256(normalize_patch(patch).encode("utf-8")).hexdigest()
        out[name] = (
            str(entry.get("status") or ""),
            str(entry.get("previous_filename") or ""),
            digest,
        )
    return out


def _delta(old: dict[str, tuple], new: dict[str, tuple]) -> list[str]:
    changed: set[str] = set()
    for name in old.keys() | new.keys():
        if old.get(name) != new.get(name):
            changed.add(name)
            for side in (old.get(name), new.get(name)):
                if side and side[1]:
                    changed.add(side[1])
    return sorted(changed)


def _newest_full_read(repo: str, number: int, conn: Any | None) -> dict | None:
    for row in review_reads.for_pr(repo, number, conn=conn):
        if row["kind"] in _FULL_READ_KINDS:
            return row
    return None


def _result(
    *,
    reviewed_sha: str | None,
    head_sha: str,
    cause: str,
    reader: str | None,
    reason: str,
    delta_files: list[str] | None = None,
    transient: bool = False,
) -> dict[str, Any]:
    if cause in REREAD_CAUSES:
        owner = {"role": "reviewer", "label": reader}
    else:
        owner = {"role": "deploy-steward", "label": None}
    return {
        "reviewed_sha": reviewed_sha,
        "current_head": head_sha,
        "head_change_cause": cause,
        "delta_files": list(delta_files or []),
        "next_gate_owner": owner,
        "bridged": cause == "rebase-identical",
        "reason": reason,
        "transient": transient,
    }


def provenance(
    *,
    repo: str,
    number: int,
    head_sha: str,
    base: str = "main",
    run_gh: Callable = subprocess.run,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Why ``head_sha`` differs from the newest full read of ``repo#number``.

    Returns ``reviewed_sha``, ``current_head``, ``head_change_cause`` (one of
    ``CAUSES``), ``delta_files``, ``next_gate_owner`` (``role`` and
    ``label``), ``bridged`` (the old read covers this head by proof),
    ``reason`` and ``transient`` (an ``unknown`` a retry may resolve).

    Reads the database and calls GitHub at most three times; writes nothing.
    """
    read = _newest_full_read(repo, number, conn)
    if read is None:
        return _result(
            reviewed_sha=None,
            head_sha=head_sha,
            cause="no-read",
            reader=None,
            reason="No full review read is recorded for this pull request.",
        )
    reviewed, reader = read["head_sha"], read["reader"]
    common = {"reviewed_sha": reviewed, "head_sha": head_sha, "reader": reader}
    if reviewed == head_sha:
        return _result(**common, cause="none", reason="The head is the head that was read.")
    try:
        direct = _compare(repo, reviewed, head_sha, run_gh)
        status = direct.get("status")
        if status == "identical":
            return _result(**common, cause="none", reason="GitHub reports the commits identical.")
        if status == "ahead":
            names = sorted(_names(_files(direct)))
            return _result(
                **common,
                cause="new-commits",
                delta_files=names,
                reason=f"{len(names)} file(s) changed in commits added after the read.",
            )
        if status not in {"diverged", "behind"}:
            raise _Unknown("Commit comparison has no usable status.", transient=True)
        old = patch_map(_files(_compare(repo, base, reviewed, run_gh)))
        new = patch_map(_files(_compare(repo, base, head_sha, run_gh)))
    except _Unknown as exc:
        return _result(**common, cause="unknown", reason=str(exc), transient=exc.transient)
    if old == new:
        return _result(
            **common,
            cause="rebase-identical",
            reason="The rebase re-applied the same patch to a newer base.",
        )
    delta = _delta(old, new)
    return _result(
        **common,
        cause="rebase-changed",
        delta_files=delta,
        reason=f"The rebase changed the patch in {len(delta)} file(s).",
    )


def dedupe_key(repo: str, number: int, result: dict[str, Any]) -> str:
    return (
        f"review-head-change:{repo}#{number}:{result.get('reviewed_sha')}:"
        f"{result.get('current_head')}"
    )


def record(result: dict[str, Any], repo: str, number: int) -> bool:
    """Append ``result`` to ``CHANNEL`` once per (reviewed, current) head pair.

    Best-effort: a failure is logged and swallowed, and ``False`` returned. A
    ``transient`` unknown is not recorded, so the dedupe key stays free for the
    answer a later retry can give. Returns whether this call wrote the row.
    """
    if result.get("transient"):
        return False
    payload = {"repo": repo, "number": number, **result}
    try:
        _ulid, inserted = events.record_event_once(
            CHANNEL,
            payload,
            originator=ORIGINATOR,
            dedupe_key=dedupe_key(repo, number, result),
        )
    except Exception:  # noqa: BLE001 - measurement only; never fail the caller
        logger.warning("review provenance record failed for %s#%s", repo, number, exc_info=True)
        return False
    return bool(inserted)


def recorded(
    repo: str, number: int, reviewed_sha: str | None, head_sha: str
) -> dict[str, Any] | None:
    """The answer already recorded for this head pair, or ``None``."""
    key = dedupe_key(repo, number, {"reviewed_sha": reviewed_sha, "current_head": head_sha})
    conn = storage.connect_read_only()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE dedupe_key=? AND channel=? LIMIT 1",
            (key, CHANNEL),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    payload = json.loads(row["payload"] or "{}")
    if not isinstance(payload, dict) or payload.get("head_change_cause") not in CAUSES:
        return None
    return {name: value for name, value in payload.items() if name not in {"repo", "number"}}


def observe(
    *,
    repo: str,
    number: int,
    head_sha: str,
    run_gh: Callable = subprocess.run,
    conn: Any | None = None,
) -> dict[str, Any]:
    """``provenance`` that reuses a recorded answer and records a new one.

    A recorded answer for the same head pair is returned without calling
    GitHub, so a repeated check neither spends compare calls nor flaps its
    summary. Errors propagate; the caller decides they are harmless.
    """
    read = _newest_full_read(repo, number, conn)
    reviewed = read["head_sha"] if read else None
    try:
        cached = recorded(repo, number, reviewed, head_sha)
    except Exception:  # noqa: BLE001 - a missing cache only costs compare calls
        cached = None
    if cached is not None:
        return cached
    result = provenance(repo=repo, number=number, head_sha=head_sha, run_gh=run_gh, conn=conn)
    record(result, repo, number)
    return result


def summary_sentence(result: dict[str, Any]) -> str:
    """One short sentence for a check summary."""
    cause = result.get("head_change_cause")
    reviewed = str(result.get("reviewed_sha") or "")[:12]
    if cause == "rebase-identical":
        return f"Provenance: rebase-identical; the patch read at {reviewed} is unchanged."
    if cause == "rebase-changed":
        count = len(result.get("delta_files") or [])
        return f"Provenance: rebase-changed; {count} file(s) differ from {reviewed}."
    return f"Provenance: {cause}."
