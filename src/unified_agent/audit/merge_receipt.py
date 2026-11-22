"""Merge receipt v1: one merged PR projected from records, verifiable offline (task #2307).

A receipt (``schema: merge_receipt/1``) answers two questions GitHub cannot:
what the agent tried and was refused, and whether the tree that merged is the
tree that was read. It is a projection, never prose: every value comes from a
record (the evidence report for the bound task, ``review_reads`` through the
review-head gate, approval rows, run records, gateway decision rows, and the
GitHub pull request, reviews and check runs fetched at render time).

The honesty rules are mechanical and each one is checked twice, once by
:func:`finalize` before a receipt is emitted and again by :func:`verify_receipt`
on a machine with no hub:

1. A ``null`` value appears in ``omitted`` with a reason, or nothing is
   emitted; and ``omitted`` names no field that is populated.
2. A tick renders only where a third party can check the claim without our
   secrets (``checkable_without_hub``, a GitHub record, a public anchor).
3. ``no-read-recorded`` is a finding with text, never a blank.
4. Denials appear only with a coverage label; with no bound gateway rows the
   field is ``null`` and omitted. A zero count is never rendered.
5. Authority is never ``verified``. A GitHub approving review is
   ``credential-attributed``; a hub approval row is ``hub-attested``. Only a
   record that names this repo, PR and merged head counts as authority.

The PR is bound to exactly one task: the one whose ``evidence_ref`` carries
the exact PR URL. Mentions in notes never bind, two claimants are refused, and
an operator's ``--task`` only asserts that binding.

Ledger rows are referenced, not copied: each embedded row carries its id,
channel, time, chain hashes and a SHA-256 of its payload, never the payload
text. A third party therefore cannot recompute a row hash from the receipt
alone; that needs the private row export, which this module does not produce.

This module holds no network or database handle of its own: the builder takes
a connection, a GitHub fetcher and the review gate as arguments, and the
verifier reads only the receipt (plus an anchor witness when one is
available locally).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..run_records import merge_usage

SCHEMA = "merge_receipt/1"
VERIFY_COMMAND = "unified-agent evidence verify receipt.json"
#: The most ledger rows one receipt references; past this the rest are omitted.
MAX_EMBEDDED_EVENTS = 200
#: Anchor backends a third party can check with public material.
_THIRD_PARTY_ANCHORS = {"rfc3161", "git"}
#: The code names the RFC-3161 backend ``tsa``; the contract says ``rfc3161``.
_BACKEND_NAMES = {"tsa": "rfc3161"}
_REVIEW_HEAD_CHECK = "unified-agent/review-head"
#: Commit-status state -> check conclusion, as ``github_app._commit_status_state`` maps it.
_STATUS_CONCLUSIONS = {"success": "success", "failure": "failure", "error": "action_required"}
_NO_READ_FINDING = "no review read recorded at this head"
_GATEWAY_CHANNELS = ("agent.gateway.decision", "agent.gateway.decision.rollup")
_RUN_CHANNEL = "agent.run.selfpickup"
_COVERING_OUTCOMES = ("completed", "handed_off")
_REVIEW_COVERAGE = {
    "read-at-head": "full",
    "delta-read": "delta(red-tier complete)",
    "delta-clean": "delta-clean(ancestor read; red-tier untouched)",
}
_WITNESS_UNAVAILABLE = {
    "witness repo is not configured",
    "witness repo is not available",
}
#: The approval the GitHub App merge path asks for (``github_app.TOOL_NAME``).
#: Named here, not imported: the connector pulls in its HTTP stack.
_PR_APPROVAL_TOOL = "github_app.process_pr_event"
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_URL_RE = re.compile(r"https://github\.com/[^\s,;()<>\[\]\"']+", re.IGNORECASE)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_REVIEW_STATES = ("read-at-head", "delta-read", "delta-clean", "no-read-recorded", "not-recorded")
_AUTHORITY_STATES = ("credential-attributed", "hub-attested", "not-recorded")
#: GitHub review states that change a reviewer's standing; COMMENTED does not.
_DECISIVE = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
_EMBEDDED_FIELDS = ("ulid", "channel", "created_at", "prev_hash", "entry_hash", "payload_sha256")
#: What ``verify`` can never establish from the receipt alone.
NOT_VERIFIED = (
    "authenticity: the receipt is unsigned; its digest shows internal consistency, "
    "not who produced it",
    "ledger-head membership: the link from the referenced rows to chain.head_hash is hub-attested",
    "row hashes: payloads are withheld, so recomputing a row hash needs the private row export",
)


class ReceiptError(Exception):
    """The receipt cannot be rendered honestly; the message says why."""


# --------------------------------------------------------------------------
# Canonical form and the omitted[] rule (shared by finalize and verify)
# --------------------------------------------------------------------------


def canonical_bytes(receipt: dict[str, Any]) -> bytes:
    """Sorted-key, compact UTF-8 JSON of the receipt without its ``digest``."""
    body = {k: v for k, v in receipt.items() if k != "digest"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _digest(receipt: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(receipt)).hexdigest()


def _null_paths(value: Any, path: str = "") -> list[str]:
    """Every path whose value is ``null``; list indices normalize to ``[]``."""
    if value is None:
        return [path]
    out: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            out += _null_paths(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for child in value:
            out += _null_paths(child, f"{path}[]")
    return out


def _resolve(receipt: dict[str, Any], path: str) -> list[Any]:
    """Every value at ``path`` (``a.b[].c``); empty when the path is absent."""
    values: list[Any] = [receipt]
    for part in path.split("."):
        each = part.endswith("[]")
        key = part[:-2] if each else part
        nxt: list[Any] = []
        for value in values:
            if isinstance(value, dict) and key in value:
                child = value[key]
                if each:
                    nxt += child if isinstance(child, list) else []
                else:
                    nxt.append(child)
        values = nxt
    return values


def omitted_problems(receipt: dict[str, Any]) -> list[str]:
    """Rule 1, both ways: a null not covered by an ``omitted`` entry, an entry
    without a reason, or an entry naming a populated field. An entry covers its
    own path and everything under it. ``false`` counts as unpopulated: it is the
    negative claim the contract itself lists (``device.attested``)."""
    omitted = receipt.get("omitted")
    if not isinstance(omitted, list):
        return ["omitted must be a list"]
    problems: list[str] = []
    covered: list[str] = []
    for entry in omitted:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("field"), str)
            or not entry["field"].strip()
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"].strip()
        ):
            problems.append(f"omitted entry without a field and reason: {entry!r}")
            continue
        covered.append(entry["field"])
        values = _resolve(receipt, entry["field"])
        if values and all(v is not None and v is not False for v in values):
            problems.append(f"{entry['field']} is listed in omitted but populated")
    body = {k: v for k, v in receipt.items() if k not in ("omitted", "digest")}
    for path in _null_paths(body):
        if not any(
            path == f or path.startswith(f + ".") or path.startswith(f + "[") for f in covered
        ):
            problems.append(f"{path} is null but not listed in omitted")
    return problems


def honesty_problems(receipt: dict[str, Any]) -> list[str]:
    """Violations of rules 2-5 that the receipt's own fields can show."""
    problems: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("human_authority") == "verified":
                problems.append("human_authority: verified is never emitted")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(receipt)
    authority = receipt.get("authority") or {}
    if authority.get("status") == "verified":
        problems.append("authority.status verified is never emitted")
    if authority.get("checkable_without_hub") and not (
        authority.get("kind") == "github_review"
        and str(authority.get("review_id") or "").startswith("github:")
    ):
        problems.append("authority claims checkable_without_hub without a GitHub review id")
    if authority.get("status") == "not-recorded" and authority.get("checkable_without_hub"):
        problems.append("a not-recorded authority cannot be checkable")
    head = (receipt.get("head") or {}).get("merged_head")
    if authority.get("status") != "not-recorded" and authority.get("commit_id") != head:
        problems.append("authority is not bound to the merged head")
    agent = receipt.get("agent") or {}
    if agent.get("attestation") != "hub-attested":
        problems.append("agent.attestation must be hub-attested")
    device = receipt.get("device") or {}
    if device.get("attested") is True and not device.get("attestation_ref"):
        problems.append("device.attested without an accepted attestation record")
    review = receipt.get("review") or {}
    if review.get("status") == "no-read-recorded" and not review.get("finding"):
        problems.append("review no-read-recorded must carry a finding")
    policy = receipt.get("policy") or {}
    if policy.get("coverage") in (None, "none") and policy.get("denials") is not None:
        problems.append("policy.denials rendered without a coverage label")
    for row in (receipt.get("chain") or {}).get("events") or []:
        if isinstance(row, dict) and "payload" in row:
            problems.append("chain.events must not carry payload text")
            break
    return problems


def finalize(receipt: dict[str, Any]) -> dict[str, Any]:
    """Refuse a receipt that breaks a rule; otherwise stamp its digest."""
    problems = (
        structure_problems({**receipt, "digest": {"alg": "sha256", "value": ""}})
        + omitted_problems(receipt)
        + honesty_problems(receipt)
    )
    if problems:
        raise ReceiptError("refusing to emit receipt: " + "; ".join(problems))
    out = {k: v for k, v in receipt.items() if k != "digest"}
    out["digest"] = {"alg": "sha256", "canonical": "json-sorted-compact-utf8"}
    out["digest"]["value"] = _digest(out)
    return out


# --------------------------------------------------------------------------
# Structure (what verify can rely on before reading anything)
# --------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_STR, _OPT_STR = (str,), (str, type(None))
_SHAPE: dict[str, dict[str, Any]] = {
    "pr": {
        "repo": _STR,
        "number": "int",
        "url": _STR,
        "merge_commit": _STR,
        "task_id": "int",
        "binding": _STR,
        "title": _OPT_STR,
        "base": _OPT_STR,
        "merged_at": _OPT_STR,
    },
    "head": {
        "merged_head": _STR,
        "parents": (list,),
        "contains_tip": (bool, type(None)),
        "source": _STR,
    },
    "review": {
        "status": _REVIEW_STATES,
        "source": _STR,
        "coverage": _STR,
        "reviewed_head": _OPT_STR,
        "reader": _OPT_STR,
        "kind": _OPT_STR,
        "at": _OPT_STR,
        "check_run": (dict, type(None)),
    },
    "authority": {
        "status": _AUTHORITY_STATES,
        "kind": _OPT_STR,
        "actor": _OPT_STR,
        "review_id": _OPT_STR,
        "commit_id": _OPT_STR,
        "checkable_without_hub": (bool,),
    },
    "agent": {
        "label": _OPT_STR,
        "harness": _OPT_STR,
        "model": _OPT_STR,
        "attestation": _STR,
        "run_id": _OPT_STR,
    },
    "device": {"host": _OPT_STR, "attested": (bool,), "attestation_ref": _OPT_STR},
    "policy": {"profile": _OPT_STR, "denials": (list, type(None)), "coverage": _STR},
    "chain": {
        "head_hash": _OPT_STR,
        "verified_entries": "int",
        "event_count": "int",
        "link": _STR,
        "payloads": _STR,
        "events": (list,),
        "anchor": (dict, type(None)),
    },
    "verify": {"command": _STR, "offline": (bool,)},
}


def structure_problems(receipt: Any) -> list[str]:
    """Required blocks and field types. Nothing else is read until this passes."""
    if not isinstance(receipt, dict):
        return ["receipt is not a JSON object"]
    problems: list[str] = []
    if receipt.get("schema") != SCHEMA:
        problems.append(f"schema is not {SCHEMA}")
    if not isinstance(receipt.get("generated_at"), str):
        problems.append("generated_at must be a string")
    if not isinstance(receipt.get("omitted"), list):
        problems.append("omitted must be a list")
    if "cost" not in receipt or not isinstance(receipt["cost"], (dict, type(None))):
        problems.append("cost must be an object or null")
    digest = receipt.get("digest")
    if (
        not isinstance(digest, dict)
        or digest.get("alg") != "sha256"
        or not isinstance(digest.get("value"), str)
    ):
        problems.append("digest must be {alg: sha256, value: <hex>}")
    for block, fields in _SHAPE.items():
        value = receipt.get(block)
        if not isinstance(value, dict):
            problems.append(f"{block} must be an object")
            continue
        for name, kind in fields.items():
            if name not in value:
                problems.append(f"{block}.{name} is missing")
            elif kind == "int":
                if not _is_int(value[name]):
                    problems.append(f"{block}.{name} must be an integer")
            elif kind in (_REVIEW_STATES, _AUTHORITY_STATES):
                if value[name] not in kind:
                    problems.append(f"{block}.{name} has an unknown value {value[name]!r}")
            elif not isinstance(value[name], kind):
                problems.append(f"{block}.{name} has the wrong type")
    chain = receipt.get("chain")
    for row in (
        chain.get("events")
        if isinstance(chain, dict) and isinstance(chain.get("events"), list)
        else []
    ):
        if not isinstance(row, dict):
            problems.append("chain.events entries must be objects")
            break
        if not isinstance(row.get("payload_sha256"), str) or not _HEX64.fullmatch(
            row["payload_sha256"]
        ):
            problems.append("chain.events[].payload_sha256 must be 64 hex characters")
            break
        if any(not isinstance(row.get(k), _OPT_STR) for k in _EMBEDDED_FIELDS):
            problems.append("chain.events[] fields must be strings or null")
            break
    return problems


# --------------------------------------------------------------------------
# Binding: PR -> task -> run
# --------------------------------------------------------------------------


def pr_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def _pr_refs(text: str | None) -> set[str]:
    """Every GitHub URL in ``text``, lowercased, without trailing punctuation."""
    return {m.group(0).rstrip("/.").lower() for m in _URL_RE.finditer(text or "")}


def resolve_task(conn: Any, repo: str, number: int, task_id: int | None = None) -> dict[str, Any]:
    """The one task this PR's receipt describes.

    The binding is the exact PR URL (this repo, this number) as one of the
    URLs in a task's ``evidence_ref``; a longer number, another repo, or a
    URL only in notes never binds. Zero or several bound tasks are refused,
    and ``task_id`` only asserts the binding: a different id is refused.
    """
    url = pr_url(repo, number)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM tasks WHERE evidence_ref LIKE ? OR notes LIKE ? "
            "OR evidence_ref LIKE ? OR notes LIKE ? ORDER BY id",
            (f"%/pull/{number}%", f"%/pull/{number}%", f"%#{number}%", f"%#{number}%"),
        ).fetchall()
    ]
    bound = [t for t in rows if url.lower() in _pr_refs(t.get("evidence_ref"))]
    if len(bound) > 1:
        ids = ", ".join(str(t["id"]) for t in bound)
        raise ReceiptError(
            f"ambiguous binding: tasks {ids} all carry {url} as evidence_ref; "
            "keep the PR URL on exactly one task"
        )
    if not bound:
        bare = re.compile(rf"#{number}(?![0-9])")
        mentions = [
            t
            for t in rows
            if url.lower() in _pr_refs(t.get("notes"))
            or any(bare.search(t.get(k) or "") for k in ("evidence_ref", "notes"))
        ]
        if mentions:
            ids = ", ".join(str(t["id"]) for t in mentions)
            raise ReceiptError(
                f"no binding: no task carries {url} as evidence_ref; tasks {ids} only "
                "mention it. Record the PR URL as the task's evidence_ref"
            )
        raise ReceiptError(f"no binding: no task carries {url} as evidence_ref")
    task = bound[0]
    if task_id is not None and int(task_id) != int(task["id"]):
        raise ReceiptError(
            f"--task {task_id} does not match the binding: {url} is bound to task {task['id']}"
        )
    return task


# --------------------------------------------------------------------------
# Projection helpers
# --------------------------------------------------------------------------


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _bound_events(conn: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
    ulids = [e["ulid"] for e in (record.get("proof") or {}).get("event_hashes") or []]
    if not ulids:
        return []
    out: list[dict[str, Any]] = []
    for start in range(0, len(ulids), 500):
        chunk = ulids[start : start + 500]
        out += [
            dict(r)
            for r in conn.execute(
                "SELECT ulid, channel, payload, created_at, prev_hash, entry_hash FROM event_log "
                f"WHERE ulid IN ({','.join('?' * len(chunk))}) ORDER BY ROWID ASC",
                chunk,
            ).fetchall()
        ]
    return out


def _reference(row: dict[str, Any]) -> dict[str, Any]:
    """A ledger row as a receipt carries it: hashes and metadata, never payload text."""
    payload = row.get("payload")
    return {
        "ulid": row.get("ulid"),
        "channel": row.get("channel"),
        "created_at": row.get("created_at"),
        "prev_hash": row.get("prev_hash"),
        "entry_hash": row.get("entry_hash"),
        "payload_sha256": hashlib.sha256(
            (payload if isinstance(payload, str) else "").encode("utf-8")
        ).hexdigest(),
    }


def _runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Self-pickup runs in claim order, each with its finish when one exists."""
    runs: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("channel") != _RUN_CHANNEL:
            continue
        payload = _payload(row)
        run_id = payload.get("run_id")
        if not isinstance(run_id, str):
            continue
        run = runs.setdefault(run_id, {"run_id": run_id})
        kind = payload.get("kind")
        if kind == "selfpickup.claimed":
            run["agent"] = payload.get("agent")
            run["harness"] = payload.get("harness")
        elif kind == "selfpickup.finished":
            run["outcome"] = payload.get("outcome")
            run["usage"] = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            run.setdefault("agent", payload.get("agent"))
        elif kind == "selfpickup.usage_reported" and isinstance(payload.get("usage"), dict):
            run["usage"] = merge_usage(
                run.get("usage") if isinstance(run.get("usage"), dict) else {},
                payload["usage"],
            )
    return list(runs.values())


def _bound_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    finished = [r for r in runs if r.get("outcome") in _COVERING_OUTCOMES]
    return (finished or runs or [None])[-1]


def _episode_ids(conn: Any, task_id: int, runs: list[dict[str, Any]]) -> list[str]:
    ids = [r["run_id"] for r in runs]
    try:
        ids += [
            f"acp:{r['id']}"
            for r in conn.execute("SELECT id FROM node_dispatch WHERE task_id=?", (task_id,))
        ]
    except Exception:  # noqa: BLE001 - a store without the node queue has no items
        pass
    return ids


def _gateway_rows(conn: Any, episode_ids: list[str]) -> list[dict[str, Any]]:
    if not episode_ids:
        return []
    marks = ",".join("?" * len(episode_ids))
    return [
        dict(r)
        for r in conn.execute(
            "SELECT channel, payload, episode_id, created_at FROM event_log "
            f"WHERE channel IN (?, ?) AND episode_id IN ({marks}) ORDER BY ROWID ASC",
            (*_GATEWAY_CHANNELS, *episode_ids),
        ).fetchall()
    ]


def _device_attestation(conn: Any, link: dict[str, Any] | None, agent: str | None) -> str | None:
    """The accepted, operator-attested acceptance run for the verified node, if any."""
    if not link or not link.get("verified") or not link.get("node"):
        return None
    try:
        row = conn.execute(
            "SELECT * FROM node_acceptance WHERE device_node=? AND state='accepted'",
            (link["node"],),
        ).fetchone()
    except Exception:  # noqa: BLE001 - no acceptance table means nothing is attested
        return None
    if row is None:
        return None
    row = dict(row)
    if row.get("agent") != agent or not row.get("attested_by") or not row.get("attestation_ref"):
        return None
    return str(row["attestation_ref"])


def _review_block(
    repo: str,
    number: int,
    head: str,
    gate: Callable[..., dict],
    github: dict[str, Any],
    conn: Any,
) -> dict[str, Any]:
    check = github.get("check_run")
    base: dict[str, Any] = {
        "status": "not-recorded",
        "source": "hub",
        "reviewed_head": None,
        "reader": None,
        "kind": None,
        "coverage": "unknown",
        "at": None,
        "check_run": check,
    }
    try:
        result = gate(repo=repo, number=number, head_sha=head, conn=conn)
    except Exception as exc:  # noqa: BLE001 - a moved head is a finding, not a crash
        # The gate lives in an adapter module this layer cannot import, so its
        # PreflightError is matched by name, subclasses included (a rejected
        # read, #2420, or a rebase off every read). A refusal is a finding about
        # this merge; "unavailable" means GitHub could not answer, which is ours.
        refused = any(cls.__name__ == "PreflightError" for cls in type(exc).__mro__)
        if refused and "unavailable" not in str(exc).lower():
            return {
                **base,
                "status": "no-read-recorded",
                "finding": f"{_NO_READ_FINDING}: {exc}",
            }
        return {**base, "unavailable": f"review_reads could not be read: {type(exc).__name__}"}
    status = result.get("status")
    if status == "no-read-recorded":
        return {**base, "status": status, "finding": _NO_READ_FINDING}
    read_head = result.get("read_head")
    kind = result.get("kind") or ("delta-read" if status == "delta-read" else None)
    at = None
    try:
        from .. import review_reads

        for row in review_reads.for_pr(repo, number, conn=conn):
            if row["head_sha"] == (head if status == "delta-read" else read_head) and (
                kind is None or row["kind"] == kind
            ):
                at, kind = row["created_at"], row["kind"]
                break
    except Exception:  # noqa: BLE001 - the time is detail; the gate answer stands
        pass
    return {
        **base,
        "status": status,
        "reviewed_head": read_head,
        "reader": result.get("reader"),
        "readers": result.get("readers") or ([result["reader"]] if result.get("reader") else []),
        "kind": kind,
        "coverage": _REVIEW_COVERAGE.get(status, "unknown"),
        "at": at,
    }


def effective_reviews(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each reviewer's standing: their latest APPROVED / CHANGES_REQUESTED /
    DISMISSED review. A later change request or a dismissal supersedes an
    earlier approval; a comment changes nothing."""
    latest: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        (r for r in reviews if r.get("user") and r.get("state") in _DECISIVE),
        key=lambda r: (str(r.get("submitted_at") or ""), r.get("id") or 0),
    )
    for review in ordered:
        latest[str(review["user"])] = review
    return latest


def _hub_pr_approvals(conn: Any, repo: str, number: int, head: str) -> list[dict[str, Any]]:
    """Approval rows that authorized this repo, PR and head through the GitHub
    App merge path. Any other approval (a file write, a delegate) is not merge
    authority, whatever task it is attached to."""
    try:
        rows = conn.execute(
            "SELECT id, status, resolved_by, payload FROM approvals WHERE tool=? "
            "AND status IN ('approved', 'consumed') ORDER BY id",
            (_PR_APPROVAL_TOOL,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a store without approvals has none
        return []
    out = []
    for row in rows:
        payload = _payload(dict(row))
        if (
            str(payload.get("repo") or "").lower() == repo.lower()
            and payload.get("number") == number
            and payload.get("head_sha") == head
        ):
            out.append(dict(row))
    return out


def _authority_block(
    conn: Any, repo: str, number: int, github: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """The authority block and, when nothing qualifies, why."""
    head = github.get("head_sha")
    reviews = github.get("reviews")
    standing = effective_reviews(reviews or [])
    approved = [r for r in standing.values() if r.get("state") == "APPROVED"]
    at_head = [r for r in approved if r.get("commit_id") == head]
    if at_head:
        pick = max(at_head, key=lambda r: (str(r.get("submitted_at") or ""), r.get("id") or 0))
        return {
            "status": "credential-attributed",
            "kind": "github_review",
            "actor": f"@{pick['user']}",
            "review_id": f"github:{pick['id']}",
            "commit_id": head,
            "checkable_without_hub": True,
        }, ""
    hub = _hub_pr_approvals(conn, repo, number, head)
    if hub:
        row = hub[-1]
        return {
            "status": "hub-attested",
            "kind": "hub_bearer",
            "actor": row.get("resolved_by"),
            "review_id": f"hub:approval:{row['id']}",
            "commit_id": head,
            "checkable_without_hub": False,
        }, ""
    if reviews is None:
        why = "GitHub reviews could not be fetched, and no hub approval names this head"
    elif approved:
        why = "approving reviews are not at the merged head, and no hub approval names this head"
    else:
        why = "no effective approving GitHub review and no hub approval for this PR head"
    return {
        "status": "not-recorded",
        "kind": None,
        "actor": None,
        "review_id": None,
        "commit_id": None,
        "checkable_without_hub": False,
    }, why


def _anchor_block(report: dict[str, Any]) -> dict[str, Any] | None:
    anchor = report.get("anchor") or {}
    if anchor.get("status") != "anchored":
        return None
    backend = _BACKEND_NAMES.get(anchor.get("backend"), anchor.get("backend"))
    return {
        "backend": backend,
        "ref": anchor.get("ref"),
        "anchored_head": anchor.get("anchored_head"),
        "entries_after_anchor": anchor.get("entries_after_anchor"),
        "verified": bool((anchor.get("verification") or {}).get("verified")),
        "checkable_without_hub": backend in _THIRD_PARTY_ANCHORS,
    }


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build_receipt(
    *,
    repo: str,
    number: int,
    conn: Any,
    github: dict[str, Any],
    review_gate: Callable[..., dict],
    task_id: int | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Project one merged PR into a finalized ``merge_receipt/1``.

    ``github`` is :func:`fetch_github`'s answer (or a test double of it).
    Raises :class:`ReceiptError` when the PR is not merged, the binding is
    missing or ambiguous, the ledger does not verify, or a rule would break.
    """
    from .evidence_report import build_evidence_report

    if not isinstance(repo, str) or _REPO_RE.fullmatch(repo) is None:
        raise ReceiptError("repo must be 'owner/name'")
    if not github.get("merged") or not github.get("merge_commit") or not github.get("head_sha"):
        raise ReceiptError(f"{repo}#{number} is not merged; a receipt describes a merged PR")
    task = resolve_task(conn, repo, number, task_id)
    report = build_evidence_report(task_id=int(task["id"]), conn=conn)
    chain = report.get("chain_integrity") or {}
    if not chain.get("verified"):
        raise ReceiptError(f"evidence ledger does not verify ({chain.get('status')})")
    if not report.get("records"):
        raise ReceiptError(f"task {task['id']} has no evidence record")
    record = report["records"][0]
    errors = github.get("errors") or {}
    omitted: list[dict[str, str]] = []

    def omit(field: str, reason: str) -> None:
        omitted.append({"field": field, "reason": reason})

    head = github["head_sha"]
    head_block = {
        "merged_head": head,
        "parents": github.get("parents") or [],
        "contains_tip": github.get("contains_tip"),
        "source": "github",
    }
    if head_block["contains_tip"] is None:
        omit(
            "head.contains_tip",
            errors.get("compare") or "GitHub compare of merged head and merge commit unavailable",
        )

    review = _review_block(repo, number, head, review_gate, github, conn)
    unavailable = review.pop("unavailable", None)
    for field in ("reviewed_head", "reader", "kind", "at"):
        if review[field] is None:
            omit(f"review.{field}", unavailable or "no review read row recorded at this head")
    if review["check_run"] is None:
        omit(
            "review.check_run",
            errors.get("check_runs") or f"no {_REVIEW_HEAD_CHECK} check run at the merged head",
        )

    authority, why = _authority_block(conn, repo, number, github)
    for field in ("kind", "actor", "review_id", "commit_id"):
        if authority[field] is None:
            omit(f"authority.{field}", why or "the approving record names no actor")

    rows = _bound_events(conn, record)
    runs = _runs(rows)
    run = _bound_run(runs)
    usage = (run or {}).get("usage") or {}
    agent = {
        "label": (run or {}).get("agent") or record.get("agent"),
        "harness": (run or {}).get("harness"),
        "model": usage.get("model"),
        "attestation": "hub-attested",
        "run_id": (run or {}).get("run_id"),
    }
    if agent["label"] in (None, "unattributed"):
        agent["label"] = None
        omit("agent.label", "no run or claim names the agent")
    if agent["harness"] is None:
        omit("agent.harness", "run records carry no harness")
    if agent["model"] is None:
        omit("agent.model", "run usage reported no model")
    if agent["run_id"] is None:
        omit("agent.run_id", "no self-pickup run recorded for the task")

    link = record.get("device_link") or {}
    ref = _device_attestation(conn, link, agent["label"])
    device = {
        "host": link.get("node") or link.get("host"),
        "attested": ref is not None,
        "attestation_ref": ref,
    }
    if device["host"] is None:
        omit("device.host", "no device recorded for the task")
    if ref is None:
        why = (
            "no accepted device attestation for a verified node"
            if link.get("verified")
            else "device is a self-reported hostname, not a verified node"
        )
        omit("device.attested", why)
        omit("device.attestation_ref", why)

    gateway = _gateway_rows(conn, _episode_ids(conn, int(task["id"]), runs))
    harnesses = sorted({str(_payload(r).get("harness") or "hub-gateway") for r in gateway})
    profiles = sorted({str(_payload(r)["profile"]) for r in gateway if _payload(r).get("profile")})
    policy: dict[str, Any] = {
        "profile": profiles[0] if len(profiles) == 1 else None,
        "denials": None,
        "coverage": "none",
    }
    if gateway:
        policy["coverage"] = harnesses[0] if len(harnesses) == 1 else "multiple"
        policy["denials"] = [
            {
                "rule": p.get("rule"),
                "tool": p.get("tool_name") or p.get("tool"),
                "reason_code": p.get("reason_code"),
                "args_sha256": p.get("args_sha256"),
                "episode_id": r.get("episode_id"),
                "at": r.get("created_at"),
            }
            for r in gateway
            if (p := _payload(r)).get("decision") == "deny"
        ]
        for key in ("args_sha256", "rule", "tool", "reason_code"):
            if any(d[key] is None for d in policy["denials"]):
                omit(f"policy.denials[].{key}", "the gateway row did not record it")
    else:
        omit(
            "policy.denials",
            "no gateway decision rows are bound to this task's runs (coverage: none)",
        )
    if policy["profile"] is None:
        omit(
            "policy.profile",
            "bound gateway rows name several profiles" if profiles else "no profile recorded",
        )

    cost = None
    if usage.get("billing") in ("subscription", "local"):
        omit("cost", f"{usage['billing']} run: no USD price")
    elif (
        isinstance(usage.get("cost_usd"), (int, float)) and usage.get("cost_complete") is not False
    ):
        cost = {"usd": usage["cost_usd"], "billing": usage.get("billing"), "source": "run usage"}
        if cost["billing"] is None:
            omit("cost.billing", "run usage reported no billing mode")
    else:
        omit("cost", "run usage not recorded")

    embedded = [_reference(r) for r in rows[:MAX_EMBEDDED_EVENTS]]
    anchor = _anchor_block(report)
    chain_block = {
        "head_hash": chain.get("head_hash"),
        "verified_entries": int(chain.get("total_verified") or 0),
        "event_count": len(rows),
        "link": "hub-attested",
        "payloads": "withheld",
        "events": embedded,
        "anchor": anchor,
    }
    if chain_block["head_hash"] is None:
        omit("chain.head_hash", "the ledger has no hashed entries")
    if anchor is None:
        omit("chain.anchor", "no anchor covers the ledger head")
    for key in _EMBEDDED_FIELDS:
        if any(r.get(key) is None for r in embedded):
            omit(f"chain.events[].{key}", "the ledger column is empty (hashed as null)")
    if len(rows) > len(embedded):
        omit(
            f"chain.events[{MAX_EMBEDDED_EVENTS}:]",
            f"only the first {MAX_EMBEDDED_EVENTS} of {len(rows)} rows are referenced",
        )

    pr = {
        "repo": repo,
        "number": number,
        "url": github.get("url") or pr_url(repo, number),
        "title": github.get("title"),
        "base": github.get("base"),
        "merged_at": github.get("merged_at"),
        "merge_commit": github["merge_commit"],
        "task_id": int(task["id"]),
        "binding": "evidence_ref",
    }
    for key in ("title", "base", "merged_at"):
        if pr[key] is None:
            omit(f"pr.{key}", "GitHub returned no value")

    receipt = {
        "schema": SCHEMA,
        "generated_at": now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pr": pr,
        "head": head_block,
        "review": review,
        "authority": authority,
        "agent": agent,
        "device": device,
        "policy": policy,
        "cost": cost,
        "chain": chain_block,
        "omitted": _dedupe(omitted),
        "verify": {"command": VERIFY_COMMAND, "offline": True},
    }
    return finalize(receipt)


def _dedupe(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out = []
    for e in entries:
        if e["field"] not in seen:
            seen.add(e["field"])
            out.append(e)
    return out


# --------------------------------------------------------------------------
# GitHub, fetched at render time
# --------------------------------------------------------------------------


def _pull_review_pages(api: Callable[[str], Any], repo: str, number: int) -> list[Any]:
    """Every review object, following GitHub's 100-item pages."""
    reviews: list[Any] = []
    seen: set[Any] = set()
    for page in range(1, 51):
        batch = api(f"repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}") or []
        if not isinstance(batch, list) or not batch:
            break
        added = 0
        for raw in batch:
            if not isinstance(raw, dict) or raw.get("id") is None or raw["id"] in seen:
                continue
            seen.add(raw["id"])
            reviews.append(raw)
            added += 1
        if added == 0 or len(batch) < 100:
            break
    return reviews


def fetch_github(repo: str, number: int, api: Callable[[str], Any]) -> dict[str, Any]:
    """The PR facts a receipt needs. ``api(path)`` returns parsed JSON or raises.

    Only the PR and merge-commit reads may raise out of here: without them
    there is no merged head to describe. The compare, reviews and check-run
    reads fail independently, and each failure is recorded in ``errors`` so
    the receipt omits that field with the reason instead of aborting.
    """
    pull = api(f"repos/{repo}/pulls/{number}")
    if not isinstance(pull, dict):
        raise ValueError("GitHub returned no pull request object")
    head = (pull.get("head") or {}).get("sha")
    merge_commit = pull.get("merge_commit_sha") if pull.get("merged") else None
    errors: dict[str, str] = {}
    out: dict[str, Any] = {
        "url": pull.get("html_url"),
        "title": pull.get("title"),
        "base": (pull.get("base") or {}).get("ref"),
        "merged": bool(pull.get("merged")),
        "merged_at": pull.get("merged_at"),
        "merge_commit": merge_commit,
        "head_sha": head,
        "parents": [],
        "contains_tip": None,
        "reviews": [],
        "check_run": None,
        "errors": errors,
    }
    if not merge_commit or not head:
        return out
    commit = api(f"repos/{repo}/commits/{merge_commit}")
    out["parents"] = [
        p.get("sha") for p in (commit or {}).get("parents") or [] if isinstance(p, dict)
    ]
    out["parents"] = [p for p in out["parents"] if isinstance(p, str)]
    if head in out["parents"] or head == merge_commit:
        out["contains_tip"] = True
    else:
        try:
            compare = api(f"repos/{repo}/compare/{head}...{merge_commit}")
            out["contains_tip"] = compare.get("status") in ("ahead", "identical")
        except Exception as exc:  # noqa: BLE001 - unknown, and omitted as such
            errors["compare"] = f"GitHub compare could not be fetched: {type(exc).__name__}"
    try:
        reviews = _pull_review_pages(api, repo, number)
        out["reviews"] = [
            {
                "id": r.get("id"),
                "user": (r.get("user") or {}).get("login"),
                "state": r.get("state"),
                "commit_id": r.get("commit_id"),
                "submitted_at": r.get("submitted_at"),
            }
            for r in reviews
            if isinstance(r, dict) and r.get("id") is not None
        ]
    except Exception as exc:  # noqa: BLE001 - authority then falls back, with the reason
        out["reviews"] = None
        errors["reviews"] = f"GitHub reviews could not be fetched: {type(exc).__name__}"
    try:
        runs = api(f"repos/{repo}/commits/{head}/check-runs?check_name={_REVIEW_HEAD_CHECK}")
        found = [r for r in (runs or {}).get("check_runs") or [] if isinstance(r, dict)]
        if found:
            r = found[0]
            out["check_run"] = {
                "name": r.get("name"),
                "conclusion": r.get("conclusion"),
                "id": r.get("id"),
                "url": r.get("html_url"),
            }
    except Exception as exc:  # noqa: BLE001 - omitted with the reason
        errors["check_runs"] = f"GitHub check runs could not be fetched: {type(exc).__name__}"
    if out.get("check_run") is None:
        # User-token mode publishes the same verdict as a commit status
        # (task #2320), since only an App can drive the Checks API, and such a
        # token may be unable to read check runs at all. The row names its
        # source so it never reads as a check run.
        try:
            statuses = api(f"repos/{repo}/commits/{head}/statuses?per_page=100")
            for status in statuses if isinstance(statuses, list) else []:
                if isinstance(status, dict) and status.get("context") == _REVIEW_HEAD_CHECK:
                    out["check_run"] = {
                        "name": _REVIEW_HEAD_CHECK,
                        "conclusion": _STATUS_CONCLUSIONS.get(status.get("state")),
                        "id": status.get("id"),
                        "url": status.get("target_url"),
                        "source": "commit_status",
                    }
                    errors.pop("check_runs", None)
                    break  # newest first
        except Exception as exc:  # noqa: BLE001 - omitted with the reason
            errors.setdefault(
                "check_runs", f"GitHub commit statuses could not be fetched: {type(exc).__name__}"
            )
    return out


# --------------------------------------------------------------------------
# Verify (offline)
# --------------------------------------------------------------------------


def verify_receipt(receipt: Any, *, check_anchor: bool = True) -> dict[str, Any]:
    """Consistency checks over a receipt, with nothing but the receipt.

    Never opens the hub store and never raises on a malformed document.
    Returns ``{"ok", "malformed", "checks": [{"name", "ok", "detail"}],
    "not_verified": [...]}``. ``ok`` means the receipt is internally
    consistent; ``not_verified`` names what no offline check can establish
    (authorship, ledger-head membership, row hashes).
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    def result(malformed: bool = False) -> dict[str, Any]:
        return {
            "ok": not malformed and all(c["ok"] is not False for c in checks),
            "malformed": malformed,
            "checks": checks,
            "not_verified": list(NOT_VERIFIED),
        }

    try:
        problems = structure_problems(receipt)
        if problems:
            add("structure", False, "; ".join(problems[:8]))
            return result(malformed=True)
        add("structure", True, f"{SCHEMA} blocks and field types")

        actual = _digest(receipt)
        same = actual == receipt["digest"]["value"]
        add(
            "digest",
            same,
            "sha256 over canonical JSON matches"
            if same
            else f"digest mismatch: recomputed {actual[:16]}",
        )

        problems = omitted_problems(receipt)
        add("omitted", not problems, "; ".join(problems) or "every null is explained, both ways")
        problems = honesty_problems(receipt)
        add("honesty", not problems, "; ".join(problems) or "no unearned claim")

        _check_rows(receipt, add)

        anchor = receipt["chain"]["anchor"]
        if anchor is None:
            add("anchor", None, "no anchor in receipt (omitted)")
        elif not anchor.get("checkable_without_hub"):
            add(
                "anchor",
                None,
                f"{anchor.get('backend')} anchor is hub-attested; not checked offline",
            )
        elif not check_anchor:
            add("anchor", None, "anchor check skipped")
        else:
            add_anchor(anchor, add)
    except Exception as exc:  # noqa: BLE001 - a malformed receipt is an answer, not a crash
        add("structure", False, f"malformed receipt: {type(exc).__name__}")
        return result(malformed=True)
    return result()


def _check_rows(receipt: dict[str, Any], add: Callable[[str, bool | None, str], None]) -> None:
    """What the referenced rows can show without their payloads: well-formed
    hashes, a count that matches, and every link between two referenced rows
    that are adjacent in the ledger. Row hashes themselves are not recomputed."""
    chain = receipt["chain"]
    rows = chain["events"]
    bad = [
        str(r.get("ulid"))
        for r in rows
        if r.get("entry_hash") is not None and not _HEX64.fullmatch(r["entry_hash"])
    ]
    hashes = {r.get("entry_hash") for r in rows if r.get("entry_hash")}
    linked = sum(1 for r in rows if r.get("prev_hash") in hashes)
    truncated = any(
        str(e.get("field", "")).startswith("chain.events[")
        and not str(e.get("field", "")).startswith("chain.events[]")
        for e in receipt["omitted"]
        if isinstance(e, dict)
    )
    count_ok = chain["event_count"] == len(rows) or (chain["event_count"] > len(rows) and truncated)
    if bad or not count_ok:
        add(
            "ledger_rows",
            False,
            f"malformed entry_hash: {', '.join(bad[:5])}" if bad else "event_count mismatch",
        )
        return
    add(
        "ledger_rows",
        True,
        f"{len(rows)} rows referenced by hash, {linked} internal links consistent; "
        "row hashes not recomputed (payloads withheld)",
    )


def add_anchor(anchor: dict[str, Any], add: Callable[[str, bool | None, str], None]) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    from . import anchor as chain_anchor

    backend = {v: k for k, v in _BACKEND_NAMES.items()}.get(anchor["backend"], anchor["backend"])
    ref = anchor.get("ref")
    head = anchor.get("anchored_head")
    if not isinstance(ref, dict) or not isinstance(head, str):
        add("anchor", False, "malformed anchor block")
        return
    try:
        result = chain_anchor.verify_anchor(ref, head, backend=backend)
    except ToolError as exc:
        add("anchor", None, f"witness not available here: {exc}")
        return
    except Exception:  # noqa: BLE001 - a malformed ref must not crash verify
        add("anchor", False, "anchor verification could not complete")
        return
    if not result.get("verified") and result.get("reason") in _WITNESS_UNAVAILABLE:
        add("anchor", None, f"witness not available here: {result.get('reason')}")
        return
    add(
        "anchor",
        bool(result.get("verified")),
        str(result.get("reason") or ("verified" if result.get("verified") else "failed")),
    )


# --------------------------------------------------------------------------
# Render (the PR comment)
# --------------------------------------------------------------------------

_TICK = "✓"


def _policy_text(policy: dict[str, Any]) -> str:
    """Denials with their coverage caveat; a zero count is never rendered."""
    denials = policy["denials"]
    coverage = policy["coverage"]
    if denials:
        return f"captured: {coverage} (partial) — {len(denials)} denied"
    return f"no denials recorded (coverage: {coverage}, partial)"


def render_markdown(receipt: dict[str, Any]) -> str:
    """One screen. A tick only where a third party can check the row."""
    pr, head, review = receipt["pr"], receipt["head"], receipt["review"]
    authority, agent, device = receipt["authority"], receipt["agent"], receipt["device"]
    policy, chain = receipt["policy"], receipt["chain"]
    rows: list[tuple[str, str, str]] = []
    rows.append(
        (
            _TICK if head.get("contains_tip") else "",
            "Head",
            f"merge `{str(pr['merge_commit'])[:12]}` contains head `{str(head['merged_head'])[:12]}`"
            if head.get("contains_tip")
            else f"merged head `{str(head['merged_head'])[:12]}`: containment not established",
        )
    )
    check = review.get("check_run") or {}
    if review["status"] == "no-read-recorded":
        rows.append(("⚠", "Review", f"**finding:** {review.get('finding') or _NO_READ_FINDING}"))
    elif review["status"] == "not-recorded":
        rows.append(("", "Review", "not-recorded"))
    else:
        rows.append(
            (
                _TICK if check.get("conclusion") == "success" else "",
                "Review",
                f"{review['status']} by {review.get('reader')} ({review.get('kind')}, hub record)",
            )
        )
    if authority["status"] == "not-recorded":
        rows.append(("", "Approval", "not-recorded"))
    else:
        rows.append(
            (
                _TICK if authority.get("checkable_without_hub") else "",
                "Approval",
                f"{authority.get('actor') or '?'} `{authority['review_id']}` "
                f"({authority['status']}, at head)",
            )
        )
    rows.append(
        (
            "",
            "Agent",
            f"{agent.get('label') or 'unattributed'} (hub-attested)"
            + (f", model {agent['model']}" if agent.get("model") else ""),
        )
    )
    rows.append(
        (
            "",
            "Device",
            f"{device.get('host') or 'unknown'} "
            + ("(attested, hub record)" if device.get("attested") else "(not attested)"),
        )
    )
    if policy.get("denials") is not None:
        rows.append(("", "Policy", _policy_text(policy)))
    rows.append(
        (
            "",
            "Ledger rows",
            f"{len(chain.get('events') or [])} of {chain.get('event_count')} rows referenced by "
            "hash; payloads withheld (hub-attested)",
        )
    )
    anchor = chain.get("anchor")
    if anchor:
        rows.append(
            (
                _TICK if anchor.get("checkable_without_hub") and anchor.get("verified") else "",
                "Anchor",
                f"{anchor['backend']} witness of `{str(anchor.get('anchored_head'))[:12]}`"
                + ("" if anchor.get("checkable_without_hub") else " (hub-attested)"),
            )
        )
    lines = [
        f"### Merge receipt — {pr['repo']}#{pr['number']}",
        "",
        "| | Row | Record |",
        "|---|---|---|",
        *[f"| {mark} | {name} | {text} |" for mark, name, text in rows],
        "",
        "✓ = a third party can check this without the hub.",
        "",
        "**Omitted:** "
        + ("; ".join(f"`{e['field']}` ({e['reason']})" for e in receipt["omitted"]) or "nothing"),
        "",
        "**Unsigned:** the digest shows this receipt is internally consistent, not who "
        "produced it. Ledger rows are referenced by hash; their payloads are withheld.",
        "",
        f"Consistency check: `{receipt['verify']['command']}` · digest "
        f"`{receipt['digest']['value'][:16]}`",
    ]
    return "\n".join(lines) + "\n"
