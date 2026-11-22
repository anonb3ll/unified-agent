"""Pluggable external witnesses for the audit-ledger chain head (task #1265, F2).

Today's default witness (task #768, :mod:`.reanchor`) is a dated note written
into the operator's own vault: it detects a wholesale SQLite rewrite against
yesterday's file, but the note lives inside infrastructure the operator
already controls, so it is not independent of the operator. This module adds
two backends whose trust does *not* run back through the hub's own database:

- ``tsa`` (:mod:`.anchor_tsa`): an RFC-3161 Time-Stamp Authority stamps the
  chain-head digest. The resulting token is signed by the TSA's key, which
  the operator does not hold, and verification trusts only a certificate the
  operator supplies out of band (``UA_EVIDENCE_TSA_CERT_PATH``) — never a
  certificate embedded in the token itself.
- ``git`` (:mod:`.anchor_git`): a commit to a *separately* configured git
  repository. Git's content addressing plus (optionally) the operator's own
  commit-signing key make the commit tamper-evident independent of the hub's
  SQLite file — this module never generates or stores a signing key, it just
  invokes ``git commit`` and lets the operator's ambient git config decide.

``vault`` (:class:`VaultAnchorBackend`) remains the default so the existing
daily ``unified-agent-audit re-anchor`` cron and its tests keep working
unchanged; ``tsa``/``git`` are opt-in via ``UA_EVIDENCE_ANCHOR_BACKEND``.

Anchor *records* — the backend-specific reference returned by ``anchor()`` —
are cached in ``event_log`` (channel ``audit.chain.anchored``) purely so an
evidence report can look up "what did we anchor for head H" without a
network round trip. That cache is not the source of trust: independence
comes from the TSA's signature or the separate git repository, not from
where unified-agent happens to keep a copy of the reference. The legacy
``vault`` backend deliberately does *not* write to this cache (the existing
``reanchor_chain_head`` contract is "does not mutate event_log" — see its
docstring and ``tests/test_audit_ledger_chain.py``); its lookup instead scans
the dated vault notes directly.
"""

from __future__ import annotations

import abc
import json
import re
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, events, storage
from .safe_read import read_regular_text

ANCHOR_CHANNEL = "audit.chain.anchored"

_HEAD_HASH_RE = re.compile(r"^- head_hash:\s*(\S+)\s*$", re.MULTILINE)


class AnchorBackend(abc.ABC):
    """One small interface every chain-head witness implements."""

    name: str

    @abc.abstractmethod
    def anchor(self, head_digest: str, *, day: str | None = None) -> dict[str, Any]:
        """Witness ``head_digest`` externally and return a JSON-safe anchor ref."""

    @abc.abstractmethod
    def verify(self, ref: dict[str, Any], head_digest: str) -> dict[str, Any]:
        """Check ``ref`` still witnesses ``head_digest``.

        Returns ``{"verified": bool, "reason": str, "detail": {...}}``. Never
        raises for an ordinary tamper/mismatch — that is a normal ``False``
        result. Raises :class:`ToolError` only for a configuration problem
        (e.g. no verification cert configured) that the caller cannot resolve
        by looking at the ref.
        """

    def verification_recipe(self, ref: dict[str, Any]) -> str:
        """A short, human-readable recipe a third party can follow by hand."""
        return f"Use `unified-agent audit re-anchor --backend {self.name}` tooling to re-verify this reference."


def _all_head_hashes(text: str) -> set[str]:
    return {m.group(1) for m in _HEAD_HASH_RE.finditer(text) if m.group(1)}


class VaultAnchorBackend(AnchorBackend):
    """Legacy/default witness: a dated, locked note in the operator's vault."""

    name = "vault"

    def anchor(self, head_digest: str, *, day: str | None = None) -> dict[str, Any]:
        from .reanchor import reanchor_chain_head

        result = reanchor_chain_head(day=day)
        return {
            "backend": self.name,
            "path": result["anchor_path"],
            "head_hash": result.get("head_hash") or head_digest,
            "anchored_at": result["anchored_at"],
        }

    def verify(self, ref: dict[str, Any], head_digest: str) -> dict[str, Any]:
        from ..safety import resolve_in_vault

        path_str = ref.get("path")
        if not path_str:
            return {"verified": False, "reason": "anchor ref missing path", "detail": {}}
        try:
            path = resolve_in_vault(path_str)
        except ToolError as exc:
            return {"verified": False, "reason": f"cannot resolve vault path: {exc}", "detail": {}}
        if not path.is_file():
            return {
                "verified": False,
                "reason": "anchor note missing",
                "detail": {"path": path_str},
            }
        # Not path.read_text: a note swapped for a FIFO after the check would
        # block report generation (#2212). Decoding stays strict UTF-8.
        text = read_regular_text(path)
        if text is None:
            return {
                "verified": False,
                "reason": "anchor note is not a readable regular file",
                "detail": {"path": path_str},
            }
        hashes = _all_head_hashes(text)
        if head_digest in hashes:
            return {
                "verified": True,
                "reason": "head hash present in vault note",
                "detail": {"path": path_str},
            }
        return {
            "verified": False,
            "reason": "head hash not present in vault note",
            "detail": {"path": path_str, "note_hashes": sorted(hashes)},
        }

    def verification_recipe(self, ref: dict[str, Any]) -> str:
        path = ref.get("path", "?")
        return (
            f"Open the vault note `{path}` (type: audit-ledger-head, locked) and confirm a "
            "`- head_hash: <head>` line matches the report's anchor.anchored_head. "
            "If chain_integrity.head_hash differs, verify the later ledger link too."
        )

    def candidate_refs(self) -> list[tuple[str, dict[str, Any]]]:
        """Every ``(head_hash, ref)`` witnessed by a dated anchor note."""
        from ..safety import resolve_in_vault
        from .reanchor import ANCHOR_DIR

        try:
            directory = resolve_in_vault(ANCHOR_DIR)
        except ToolError:
            return []
        if not directory.is_dir():
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(directory.glob("*.md")):
            # Never path.read_text: a note that is, or becomes, a FIFO
            # would block it forever (#2212). Decoding stays strict UTF-8.
            text = read_regular_text(path)
            if text is None:
                continue
            for head in _all_head_hashes(text):
                out.append((head, {"backend": self.name, "path": f"{ANCHOR_DIR}/{path.name}"}))
        return out

    def find_ref_for_head(self, head_digest: str) -> dict[str, Any] | None:
        """Scan the dated anchor notes for one witnessing ``head_digest``.

        Used by :func:`header_for_head` since the vault backend, unlike
        ``tsa``/``git``, does not populate the ``event_log`` cache.
        """
        from ..safety import resolve_in_vault
        from .reanchor import ANCHOR_DIR

        try:
            directory = resolve_in_vault(ANCHOR_DIR)
        except ToolError:
            return None
        if not directory.is_dir():
            return None
        for path in sorted(directory.glob("*.md"), reverse=True):
            # Never path.read_text: a note that is, or becomes, a FIFO
            # would block it forever (#2212). Decoding stays strict UTF-8.
            text = read_regular_text(path)
            if text is None:
                continue
            if head_digest in _all_head_hashes(text):
                return {"backend": self.name, "path": f"{ANCHOR_DIR}/{path.name}"}
        return None


def _backends() -> dict[str, AnchorBackend]:
    from .anchor_git import GitAnchorBackend
    from .anchor_tsa import TsaAnchorBackend

    return {"vault": VaultAnchorBackend(), "tsa": TsaAnchorBackend(), "git": GitAnchorBackend()}


def get_backend(name: str | None) -> AnchorBackend:
    backends = _backends()
    key = (name or config.evidence_anchor_backend()).strip().lower()
    if key not in backends:
        raise ToolError(
            f"unknown evidence anchor backend {name!r}; choose one of {sorted(backends)}"
        )
    return backends[key]


def anchor_chain_head(*, day: str | None = None, backend: str | None = None) -> dict[str, Any]:
    """Anchor the current chain head with the configured (or given) backend.

    ``vault`` is a thin pass-through to :func:`reanchor.reanchor_chain_head`
    so the existing daily cron and its tests are untouched — no extra
    ``event_log`` row, no change in return shape. The other backends compute
    the head fresh, call the backend, and cache the resulting reference in
    ``event_log`` (channel :data:`ANCHOR_CHANNEL`) for report-header lookup.
    """
    name = (backend or config.evidence_anchor_backend()).strip().lower()
    if name == "vault":
        from .reanchor import reanchor_chain_head

        result = reanchor_chain_head(day=day)
        return {**result, "anchor_backend": "vault"}

    edition.require_feature("audit_reanchor")
    storage.init_db()
    chain_report = events.verify_event_chain()
    if not chain_report.get("verified"):
        raise ToolError("cannot anchor: the event chain did not verify")
    head_digest = chain_report.get("head_hash") or ""
    if not head_digest:
        raise ToolError("cannot anchor: the event chain has no hashed entries yet")

    instance = get_backend(name)
    # The cache event is itself a new ledger entry. Repeated calls with no
    # intervening domain event should reuse its verified witness rather than
    # witness the cache event and create an endless sequence of cache events.
    conn = storage.connect()
    try:
        last = conn.execute(
            "SELECT channel, payload FROM event_log WHERE entry_hash = ? "
            "ORDER BY ROWID DESC LIMIT 1",
            (head_digest,),
        ).fetchone()
    finally:
        conn.close()
    if last is not None and last["channel"] == ANCHOR_CHANNEL:
        try:
            cached = json.loads(last["payload"])
        except (TypeError, ValueError):
            cached = {}
        previous_ref = cached.get("ref") if isinstance(cached, dict) else None
        same_day = (
            day is None
            or name == "tsa"
            or (
                isinstance(previous_ref, dict)
                and str(previous_ref.get("path", "")).endswith(f"/{day}.json")
            )
        )
        if (
            isinstance(previous_ref, dict)
            and cached.get("backend") == name
            and same_day
            and verify_anchor(previous_ref, cached.get("head_hash", ""), backend=name).get(
                "verified"
            )
        ):
            return {
                **chain_report,
                "anchor_backend": name,
                "anchor_ref": {**previous_ref, "anchor_action": "unchanged"},
                "anchored_at": cached.get("anchored_at"),
            }
    ref = instance.anchor(head_digest, day=day)
    verification = verify_anchor(ref, head_digest, backend=name)
    if not verification.get("verified"):
        raise ToolError(
            f"{name} anchor was created but did not verify: {verification.get('reason', 'unknown')}"
        )
    anchored_at = ref.get("anchored_at") or datetime.now(UTC).isoformat(timespec="seconds")
    ref.setdefault("anchored_at", anchored_at)
    ref.setdefault("head_hash", head_digest)
    events.record_event(
        ANCHOR_CHANNEL,
        payload={"head_hash": head_digest, "backend": name, "ref": ref, "anchored_at": anchored_at},
        originator="audit.anchor",
        notify=False,
    )
    return {**chain_report, "anchor_backend": name, "anchor_ref": ref, "anchored_at": anchored_at}


def verify_anchor(
    ref: dict[str, Any], head_digest: str, *, backend: str | None = None
) -> dict[str, Any]:
    """Verify ``ref`` (as embedded in a report header) against ``head_digest``."""
    name = backend or ref.get("backend")
    instance = get_backend(name)
    try:
        return instance.verify(ref, head_digest)
    except OSError as exc:
        raise ToolError(f"{instance.name} anchor verification failed: {exc}") from exc


def _chain_positions(conn: Any) -> dict[str, int]:
    """``entry_hash -> ROWID`` for every hashed ledger entry."""
    rows = conn.execute(
        "SELECT ROWID AS pos, entry_hash FROM event_log WHERE entry_hash IS NOT NULL"
    ).fetchall()
    return {row["entry_hash"]: int(row["pos"]) for row in rows}


def _anchor_candidates(conn: Any) -> list[dict[str, Any]]:
    """Every recorded anchor: the ``event_log`` cache (tsa/git) plus vault notes."""
    out: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT payload FROM event_log WHERE channel = ? ORDER BY ROWID", (ANCHOR_CHANNEL,)
    ).fetchall()
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if isinstance(payload, dict) and payload.get("head_hash") and payload.get("ref"):
            out.append(
                {
                    "backend": payload.get("backend"),
                    "ref": payload["ref"],
                    "anchored_head": payload["head_hash"],
                    "anchored_at": payload.get("anchored_at"),
                }
            )
    for head, ref in VaultAnchorBackend().candidate_refs():
        out.append({"backend": "vault", "ref": ref, "anchored_head": head, "anchored_at": None})
    return out


_NOT_ANCHORED: dict[str, Any] = {
    "status": "not_anchored",
    "backend": None,
    "ref": None,
    "anchored_head": None,
    "entries_after_anchor": None,
    "anchored_at": None,
    "verification": None,
    "recipe": None,
}


def header_for_head(head_hash: str | None, conn: Any = None) -> dict[str, Any]:
    """Build the ``anchor`` block for an evidence report header.

    An anchor witnesses one chain head H; everything recorded before H is
    covered by it directly, and everything after H is linked to it only by
    the hash chain. (Recording a tsa/git anchor itself appends a ledger row,
    so the head a report sees is never the exact head that was anchored.)
    This therefore picks the most recent anchor whose ``anchored_head`` is an
    entry at or before ``head_hash`` in the chain, verifies the external
    proof against *that* head, and reports ``entries_after_anchor`` so a
    reader knows how much of the ledger the witness covers.

    Purely offline: TSA verification checks a locally configured certificate,
    git verification runs ``git show``/``git verify-commit`` against a local
    repo path, and vault verification reads a local file.
    """
    if not head_hash:
        return dict(_NOT_ANCHORED)

    close_conn = False
    if conn is None:
        conn = storage.connect()
        close_conn = True
    try:
        positions = _chain_positions(conn)
        candidates = _anchor_candidates(conn)
    finally:
        if close_conn:
            conn.close()

    head_pos = positions.get(head_hash)
    if head_pos is None:
        return dict(_NOT_ANCHORED)
    covering = [
        c
        for c in candidates
        if positions.get(c["anchored_head"]) is not None
        and positions[c["anchored_head"]] <= head_pos
    ]
    if not covering:
        return dict(_NOT_ANCHORED)
    best = max(covering, key=lambda c: positions[c["anchored_head"]])
    anchored_head = best["anchored_head"]
    entries_after = sum(
        1 for pos in positions.values() if positions[anchored_head] < pos <= head_pos
    )

    block = {
        "status": "anchored",
        "backend": best["backend"],
        "ref": best["ref"],
        "anchored_head": anchored_head,
        "entries_after_anchor": entries_after,
        "anchored_at": best["anchored_at"],
    }
    try:
        instance = get_backend(best["backend"])
    except ToolError as exc:
        return {
            **block,
            "verification": {"verified": False, "reason": f"unknown backend: {exc}", "detail": {}},
            "recipe": None,
        }
    try:
        verification = verify_anchor(best["ref"], anchored_head, backend=best["backend"])
    except ToolError as exc:
        verification = {"verified": False, "reason": f"verification error: {exc}", "detail": {}}
    return {
        **block,
        "verification": verification,
        "recipe": instance.verification_recipe(best["ref"]),
    }
