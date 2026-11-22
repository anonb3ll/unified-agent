"""Typed handoff fields and receiver grounding, in report mode (task #2390).

A ``handoff`` message may carry a structured payload beside its free-text body:
what is ``done``, the ``next_action``, ``why``, ``open_questions``, the
``evidence`` it rests on, and the ``reviewed_head_sha``. ``validate_handoff``
checks its shape when it is sent. ``ground`` is the receiver's side: it splits
the payload into claims and checks each against live state, so a handoff is a
pointer to check, never an authority by itself (the same rule as a claimed
approval).

Tiers, by where the resolver found the fact, not by what the sender called it:

* **T0** a human's own message (the operator's login label), or a git commit.
* **T1** CI or pull-request state read live from GitHub.
* **T2** anything an agent wrote: another agent's message, a task row, a vault
  note or a memory note.

Each claim gets one verdict: ``verified``, ``mismatch`` (live state contradicts
it) or ``insufficient`` (it could not be checked: no resolver, an error, still
running, or the budget ran out). A ``next_action`` that names a high-risk step
(merge, deploy, release, delete, ...) needs at least one verified T0 or T1
claim. The whole check costs at most ``RESOLVER_BUDGET`` resolver calls.

A resolver checks the reference, not what the sender says it shows (task
#2485). It can tell that a commit or a note exists, what state a PR, a CI run
or a task is in, and who sent a message. It cannot tell that a commit
"is the operator's approval". Each claim therefore reports the two apart:
``checked`` names the predicate the resolver evaluated and ``reference`` is its
result, while ``claim`` stays the sender's own words. For most claims the
verdict is the reference's. The exception is an approval or authorization: when
the handoff says one was given (``asserts_authorization``), nothing here can
stand behind it. No resolver reads what a message says, so even the operator's
own message, cited, shows only that the operator wrote something: it may be
about lunch, or say "do not deploy" (task #2495). The report therefore always
says ``authorization: "not_checked"`` and ``authorization_checked: false`` for
such a handoff, a claim worded as that approval is ``insufficient`` however
real its reference is, and the high-risk rule is not met. When the operator's
own message is among the evidence, ``authorization_reference`` names it as the
place for a person to read; it changes no verdict.

Report mode: nothing here refuses a send or blocks an agent. The verdict is
returned to the caller and recorded as one ``agent.message.grounding`` event
(counts only, no body), so the mismatch rate can be measured before anyone
decides to enforce it.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, events, storage
from ..telemetry.redaction import redact_sensitive_content
from . import index as message_index
from .schema import (
    EVIDENCE_FIELDS,
    EVIDENCE_KINDS,
    HANDOFF_FIELDS,
    HANDOFF_MAX_BYTES,
    HANDOFF_MAX_EVIDENCE,
    HANDOFF_MAX_ITEM_TEXT,
    HANDOFF_MAX_ITEMS,
    HANDOFF_MAX_REF,
    HANDOFF_MAX_TEXT,
    HANDOFF_RECOMMENDED,
)

GROUNDING_CHANNEL = "agent.message.grounding"
RESOLVER_BUDGET = 15
GH_TIMEOUT_SECONDS = 10

VERIFIED = "verified"
MISMATCH = "mismatch"
INSUFFICIENT = "insufficient"

T0, T1, T2 = "T0", "T1", "T2"

_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_NUMBER_RE = re.compile(r"#?([0-9]{1,12})")
_PR_STATES = ("open", "merged", "closed")
_CI_CONCLUSIONS = ("success", "failure", "cancelled", "skipped", "neutral", "timed_out")

# A next action that changes shared state irreversibly or ships it. Matched on
# whole words, case-insensitively, against the typed ``next_action``.
_HIGH_RISK_RE = re.compile(
    r"\b(merge|merging|deploy|deploying|ship|shipping|release|releasing|roll|rollout|"
    r"rollback|delete|deleting|drop|force[- ]push|rotate|revoke|approve|migrate|restart)\b",
    re.IGNORECASE,
)

# What each resolver evaluates: the existence, state or identity of the
# reference. Never the sender's free-text ``claim`` about it.
CHECKED = {
    "message": "the message exists between the handoff's parties, and who sent it",
    "commit": "the commit exists",
    "pr": "the PR's state and head",
    "ci_run": "the CI run's status, conclusion and head",
    "task": "the task's status",
    "doc": "the note exists",
}

AUTH_NOT_CLAIMED = "not_claimed"
AUTH_NOT_CHECKED = "not_checked"
#: ``authorization_reference`` when the operator's own message is cited. It says
#: where the approval can be read, never that it was read.
AUTH_REF_OPERATOR_MESSAGE = "operator_message"

# Wording that says an approval or authorization was given. Matched as an
# assertion (a past-tense verb, "approval was granted", "operator approval"),
# case-insensitively, so the product nouns ("approval receipts", "Approval
# Ledger") and "unauthorized" do not count. A heuristic, like _HIGH_RISK_RE.
_AUTH_NOUN = r"(?:approvals?|authori[sz]ations?|permission|sign[- ]?off|go[- ]ahead|consent)"
_AUTHORIZATION_RE = re.compile(
    r"\b(?:"
    r"approved|authori[sz]ed|signed[- ]off|green[- ]?lit|green[- ]?lighted|okayed|ok'd|"
    r"consented|go[- ]ahead"
    rf"|{_AUTH_NOUN}\s+(?:(?:was|were|is|has\s+been)\s+)?(?:given|granted|received|obtained|from|by)"
    r"|(?:operator|owner|human|maintainer|admin|steward|founder|ceo|manager|lead)(?:'s)?\s+"
    rf"{_AUTH_NOUN}"
    r")\b",
    re.IGNORECASE,
)


# --- send side: shape -------------------------------------------------------------


def validate_handoff(raw: Any, act: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Normalize a typed handoff payload, or refuse a malformed one.

    Returns ``(payload, warnings)``. ``raw=None`` on a ``handoff`` act is an
    untyped handoff: allowed (report mode) with a warning. Typed fields on any
    other act, an unknown key or a wrong type are refused, because the caller
    asked for the structure and an error tells it how to fix it. A missing
    recommended field is warned, not refused.
    """
    if raw is None:
        if act == "handoff":
            return None, [
                "untyped handoff: pass handoff={done, next_action, why, open_questions, "
                "evidence, reviewed_head_sha} so the receiver can ground it (#2390)"
            ]
        return None, []
    if act != "handoff":
        raise ToolError("handoff fields are only accepted on act='handoff'")
    if not isinstance(raw, dict):
        raise ToolError("handoff must be an object with fields " + ", ".join(HANDOFF_FIELDS))
    unknown = sorted(set(raw) - set(HANDOFF_FIELDS))
    if unknown:
        raise ToolError(f"unknown handoff field(s) {unknown}; allowed: {', '.join(HANDOFF_FIELDS)}")
    out: dict[str, Any] = {}
    for key in ("done", "open_questions"):
        if raw.get(key) is not None:
            out[key] = _text_list(raw[key], key)
    for key, limit in (("next_action", HANDOFF_MAX_ITEM_TEXT), ("why", HANDOFF_MAX_TEXT)):
        if raw.get(key) is not None:
            out[key] = _text(raw[key], key, limit)
    if raw.get("reviewed_head_sha") is not None:
        out["reviewed_head_sha"] = _sha(raw["reviewed_head_sha"], "reviewed_head_sha")
    if raw.get("repo") is not None:
        out["repo"] = _repo(raw["repo"], "repo")
    if raw.get("evidence") is not None:
        out["evidence"] = _evidence(raw["evidence"])
    if len(json.dumps(out).encode("utf-8")) > HANDOFF_MAX_BYTES:
        raise ToolError(f"handoff fields exceed {HANDOFF_MAX_BYTES} bytes")
    warnings = [
        f"handoff has no {key!r}; the receiver has less to check"
        for key in HANDOFF_RECOMMENDED
        if not out.get(key)
    ]
    return out, warnings


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ToolError(f"handoff.{name} must be a string")
    text = value.strip()
    if len(text) > limit:
        raise ToolError(f"handoff.{name} is over {limit} characters")
    return redact_sensitive_content(text, max_len=limit)


def _text_list(value: Any, name: str) -> list[str]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise ToolError(f"handoff.{name} must be a string or a list of strings")
    if len(items) > HANDOFF_MAX_ITEMS:
        raise ToolError(f"handoff.{name} has more than {HANDOFF_MAX_ITEMS} items")
    return [
        _text(item, name, HANDOFF_MAX_ITEM_TEXT)
        for item in items
        if isinstance(item, str) and item.strip()
    ]


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value.strip()):
        raise ToolError(f"{name} must be a 7-40 character hex commit SHA")
    return value.strip().lower()


def _repo(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _REPO_RE.fullmatch(value.strip()):
        raise ToolError(f"{name} must be a GitHub 'owner/name'")
    return value.strip()


def _evidence(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ToolError("handoff.evidence must be a list of {kind, ref, repo?, expect?, claim?}")
    if len(value) > HANDOFF_MAX_EVIDENCE:
        raise ToolError(f"handoff.evidence has more than {HANDOFF_MAX_EVIDENCE} items")
    out = []
    for position, item in enumerate(value):
        where = f"handoff.evidence[{position}]"
        if not isinstance(item, dict):
            raise ToolError(f"{where} must be an object")
        unknown = sorted(set(item) - set(EVIDENCE_FIELDS))
        if unknown:
            raise ToolError(f"{where} has unknown field(s) {unknown}")
        kind = item.get("kind")
        if kind not in EVIDENCE_KINDS:
            raise ToolError(f"{where}.kind must be one of {list(EVIDENCE_KINDS)}")
        ref = item.get("ref")
        if isinstance(ref, int) and not isinstance(ref, bool):
            ref = str(ref)
        if not isinstance(ref, str) or not ref.strip() or len(ref) > HANDOFF_MAX_REF:
            raise ToolError(f"{where}.ref must be a non-empty string of at most {HANDOFF_MAX_REF}")
        entry = {"kind": kind, "ref": ref.strip()}
        if item.get("repo") is not None:
            entry["repo"] = _repo(item["repo"], f"{where}.repo")
        for key in ("expect", "claim"):
            if item.get(key) is not None:
                entry[key] = _text(item[key], f"evidence[{position}].{key}", HANDOFF_MAX_REF)
        out.append(entry)
    return out


# --- receive side: grounding ------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    kind: str
    ref: str
    repo: str | None = None
    expect: str | None = None
    claim: str | None = None


@dataclass
class Outcome:
    verdict: str
    tier: str | None
    detail: str
    observed: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Context:
    """Who the handoff is between, and the head it says was reviewed."""

    sender: str
    caller: str | None
    reviewed_head_sha: str | None


Resolver = Callable[[Claim, Context], Outcome]
GhRunner = Callable[[list[str]], subprocess.CompletedProcess]


def claims_of(handoff: dict[str, Any]) -> list[Claim]:
    """The checkable claims in a typed handoff, in order, without duplicates.

    ``reviewed_head_sha`` is itself a claim: that the commit exists. Each
    evidence item is one claim. Free-text fields (``done``, ``why``) carry no
    pointer to check and are not claims.
    """
    out: list[Claim] = []
    default_repo = handoff.get("repo")
    sha = handoff.get("reviewed_head_sha")
    if sha:
        out.append(Claim("commit", sha, default_repo, None, "reviewed head exists"))
    for item in handoff.get("evidence") or ():
        claim = Claim(
            item["kind"],
            item["ref"],
            item.get("repo") or default_repo,
            item.get("expect"),
            item.get("claim"),
        )
        if claim not in out:
            out.append(claim)
    return out


def is_high_risk(handoff: dict[str, Any]) -> bool:
    return bool(_HIGH_RISK_RE.search(handoff.get("next_action") or ""))


def _says_authorized(*texts: str | None) -> bool:
    return any(_AUTHORIZATION_RE.search(text) for text in texts if text)


def asserts_authorization(handoff: dict[str, Any]) -> bool:
    """Whether the handoff says an approval or authorization was given.

    Read from what the sender asserts: ``done``, ``why`` and each evidence
    item's ``claim`` and ``expect``. ``next_action`` and ``open_questions`` are
    a plan and questions, not assertions, and are not read.
    """
    if _says_authorized(handoff.get("why"), *(handoff.get("done") or ())):
        return True
    return any(
        _says_authorized(item.get("claim"), item.get("expect"))
        for item in handoff.get("evidence") or ()
    )


def ground(
    handoff: dict[str, Any] | None,
    *,
    sender: str,
    caller: str | None,
    resolvers: dict[str, Resolver] | None = None,
    budget: int = RESOLVER_BUDGET,
) -> dict[str, Any]:
    """Check each claim in ``handoff`` against live state and return the report.

    Never raises for a claim: a resolver error is an ``insufficient`` claim.
    After ``budget`` resolver calls the remaining claims are ``insufficient``.

    Each claim carries ``checked`` (the predicate its resolver evaluated, None
    when none ran) and ``reference`` (that predicate's result) beside
    ``verdict``. The two differ only for a claim worded as an approval or
    authorization that nothing checked: its reference may be ``verified`` while
    its verdict is ``insufficient``, and it carries ``authorization``.

    The report's ``authorization`` is ``not_claimed`` when the handoff asserts
    none and ``not_checked`` when it asserts one. ``authorization_checked`` is
    always false: no resolver reads what a message says, so nothing here ever
    evaluates an approval. ``authorization_reference`` is ``operator_message``
    when the handoff asserts one and cites a message the resolver found to be
    the operator's own, else None; it says where a person can read the
    approval and changes no verdict. While ``authorization`` is ``not_checked``
    the verdict is never ``verified`` and ``high_risk_met`` is false. Report
    mode still: nothing here grants, refuses or gates anything.
    """
    if not handoff:
        return {
            "mode": "report",
            "typed": False,
            "verdict": INSUFFICIENT,
            "reasons": ["untyped handoff: no claims to ground; read it as unverified prose"],
            "high_risk": False,
            "high_risk_met": False,
            "authorization": AUTH_NOT_CLAIMED,
            "authorization_checked": False,
            "authorization_reference": None,
            "claims": [],
            "resolver_calls": 0,
            "budget": budget,
        }
    table = resolvers if resolvers is not None else default_resolvers()
    ctx = Context(sender, caller, handoff.get("reviewed_head_sha"))
    calls = 0
    results: list[dict[str, Any]] = []
    for claim in claims_of(handoff):
        resolver = table.get(claim.kind)
        checked = None
        if resolver is None:
            outcome = Outcome(INSUFFICIENT, None, f"no resolver for {claim.kind!r}")
        elif calls >= budget:
            outcome = Outcome(INSUFFICIENT, None, f"resolver budget of {budget} calls spent")
        else:
            calls += 1
            try:
                outcome = resolver(claim, ctx)
                checked = CHECKED.get(claim.kind)
            except Exception as exc:  # noqa: BLE001 - a failed check is unverified, never fatal
                outcome = Outcome(INSUFFICIENT, None, f"resolver failed: {type(exc).__name__}")
        results.append(
            {
                "kind": claim.kind,
                "ref": claim.ref,
                "repo": claim.repo,
                "expect": claim.expect,
                "claim": claim.claim,
                "tier": outcome.tier,
                "checked": checked,
                "reference": outcome.verdict,
                "verdict": outcome.verdict,
                "detail": outcome.detail,
                **({"observed": outcome.observed} if outcome.observed else {}),
            }
        )
    # No resolver reads a message's body, so a claimed approval is never checked
    # here, whatever is cited (task #2495). The operator's own message (T0 on
    # the message resolver) is named as where a person can read it, no more.
    asserted = asserts_authorization(handoff)
    authorization = AUTH_NOT_CHECKED if asserted else AUTH_NOT_CLAIMED
    cited = asserted and any(
        r["kind"] == "message" and r["reference"] == VERIFIED and r["tier"] == T0 for r in results
    )
    authorization_reference = AUTH_REF_OPERATOR_MESSAGE if cited else None
    for result in results:
        if not _says_authorized(result["claim"], result["expect"]):
            continue
        result["authorization"] = authorization
        if authorization == AUTH_NOT_CHECKED and result["verdict"] == VERIFIED:
            result["verdict"] = INSUFFICIENT
            result["detail"] += "; the approval or authorization it is cited for was not checked"
    high_risk = is_high_risk(handoff)
    strong = any(r["verdict"] == VERIFIED and r["tier"] in (T0, T1) for r in results)
    reasons: list[str] = []
    mismatched = [r for r in results if r["verdict"] == MISMATCH]
    if mismatched:
        verdict = MISMATCH
        reasons += [f"{r['kind']} {r['ref']}: {r['detail']}" for r in mismatched]
    else:
        verdict = VERIFIED
        if not results:
            verdict = INSUFFICIENT
            reasons.append("typed handoff names no evidence and no reviewed head")
        unchecked = [r for r in results if r["verdict"] != VERIFIED]
        if unchecked:
            verdict = INSUFFICIENT
            reasons += [f"{r['kind']} {r['ref']}: {r['detail']}" for r in unchecked]
    if high_risk and not strong:
        if verdict == VERIFIED:
            verdict = INSUFFICIENT
        reasons.append(
            "next_action is high-risk and no claim is verified at T0 (human or git) "
            "or T1 (CI or PR state)"
        )
    if authorization == AUTH_NOT_CHECKED:
        if verdict == VERIFIED:
            verdict = INSUFFICIENT
        reasons.append(
            "the handoff says an approval or authorization was given and nothing here "
            "checks that: a reference that exists is not that approval, and a cited "
            "message is not read. "
            + (
                "The operator's own message is cited: read what it says"
                if cited
                else "Cite the operator's own message so a person can read it"
            )
        )
    return {
        "mode": "report",
        "typed": True,
        "verdict": verdict,
        "reasons": reasons,
        "high_risk": high_risk,
        "high_risk_met": strong and authorization != AUTH_NOT_CHECKED,
        "authorization": authorization,
        "authorization_checked": False,
        "authorization_reference": authorization_reference,
        "claims": results,
        "resolver_calls": calls,
        "budget": budget,
    }


def ground_message(
    message_id: str,
    *,
    session_id: str | None = None,
    to_agent: str | None = None,
    resolvers: dict[str, Resolver] | None = None,
) -> dict[str, Any]:
    """Ground one handoff message the caller can read, and record the verdict.

    Visibility is ``message_list``'s: a verified caller grounds only mail it
    sent or received. The ``agent.message.grounding`` event carries the verdict
    and counts, never the body or the claims' text.
    """
    from .send import list_messages

    mid = (message_id or "").strip()
    if not mid:
        raise ToolError("message_id is required")
    listed = list_messages(message_id=mid, session_id=session_id, to_agent=to_agent, limit=1)
    rows = listed.get("messages") or []
    if not rows:
        raise ToolError(f"no message {mid!r} that you sent or received")
    row = rows[0]
    if row.get("act") != "handoff":
        raise ToolError(f"message {mid!r} is a {row.get('act')!r}, not a handoff")
    caller = config.verified_agent_identity() or (to_agent or "").strip() or None
    report = ground(
        row.get("handoff"), sender=row["from_agent"], caller=caller, resolvers=resolvers
    )
    report["message_id"] = mid
    report["from_agent"] = row["from_agent"]
    report["to_agent"] = row["to_agent"]
    counts: dict[str, int] = {}
    for claim in report["claims"]:
        counts[claim["verdict"]] = counts.get(claim["verdict"], 0) + 1
    try:
        report["event_ulid"] = events.record_event(
            GROUNDING_CHANNEL,
            payload={
                "message_id": mid,
                "conversation_id": row.get("conversation_id"),
                "from_agent": row["from_agent"],
                "grounded_by": caller,
                "typed": report["typed"],
                "verdict": report["verdict"],
                "high_risk": report["high_risk"],
                "high_risk_met": report["high_risk_met"],
                "authorization": report["authorization"],
                "authorization_reference": report["authorization_reference"],
                "claims": len(report["claims"]),
                "counts": counts,
                "resolver_calls": report["resolver_calls"],
            },
            originator=caller,
            episode_id=row.get("conversation_id"),
            causal_parent_id=row.get("ulid"),
        )
    except Exception:  # noqa: BLE001 - the measurement is best-effort; the report stands
        report["event_ulid"] = None
    return report


# --- resolvers ----------------------------------------------------------------------


def default_resolvers(gh: GhRunner | None = None) -> dict[str, Resolver]:
    run = gh or _run_gh
    return {
        "message": _resolve_message,
        "commit": lambda claim, ctx: _resolve_commit(claim, ctx, run),
        "pr": lambda claim, ctx: _resolve_pr(claim, ctx, run),
        "ci_run": lambda claim, ctx: _resolve_ci_run(claim, ctx, run),
        "task": _resolve_task,
        "doc": _resolve_doc,
    }


def _run_gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SECONDS,
        check=False,
    )


def _gh_json(run: GhRunner, args: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """``(data, None)`` on success; ``(None, 'missing')`` when GitHub says it does not
    exist; ``(None, reason)`` for anything else."""
    try:
        proc = run(args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"gh unavailable: {type(exc).__name__}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").lower()
        if any(
            s in err
            for s in ("http 404", "http 422", "not found", "could not resolve", "no commit found")
        ):
            return None, "missing"
        return None, "gh failed"
    try:
        data = json.loads(proc.stdout or "null")
    except ValueError:
        return None, "gh returned non-JSON"
    return (data, None) if isinstance(data, dict) else (None, "gh returned an unexpected shape")


def _same_sha(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a.startswith(b) or b.startswith(a)


def _resolve_message(claim: Claim, ctx: Context) -> Outcome:
    """T0 when the cited message is the operator's own, T2 when an agent wrote it.

    Resolved only when the handoff's sender or the caller was party to it, and
    only who sent it and when is reported, never its body.
    """
    conn = storage.connect()
    try:
        row = message_index.get_message(conn, claim.ref)
    finally:
        conn.close()
    parties = {ctx.sender, ctx.caller}
    if row is None or not ({row["sender"], row["recipient"]} & parties):
        return Outcome(MISMATCH, None, "no such message between the handoff's parties")
    human = config.is_human_identity(row["sender"])
    return Outcome(
        VERIFIED,
        T0 if human else T2,
        "operator's own message" if human else "agent-written message",
        {"sender": row["sender"], "created_at": row["created_at"]},
    )


def _resolve_commit(claim: Claim, ctx: Context, run: GhRunner) -> Outcome:
    if not _SHA_RE.fullmatch(claim.ref):
        return Outcome(MISMATCH, None, "not a commit SHA")
    if claim.repo:
        data, err = _gh_json(run, ["api", f"repos/{claim.repo}/commits/{claim.ref}"])
        if err == "missing":
            return Outcome(MISMATCH, None, f"no commit {claim.ref} in {claim.repo}")
        if data is None:
            return Outcome(INSUFFICIENT, None, err or "unreadable")
        return Outcome(VERIFIED, T0, "commit exists", {"sha": data.get("sha")})
    for root in config.project_repo_roots():
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "cat-file", "-e", f"{claim.ref}^{{commit}}"],
                capture_output=True,
                timeout=GH_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return Outcome(VERIFIED, T0, "commit exists", {"checkout": root.name})
    # A local miss proves nothing: the checkout may not have fetched it yet.
    return Outcome(INSUFFICIENT, None, "commit not in a local checkout; name its repo")


def _resolve_pr(claim: Claim, ctx: Context, run: GhRunner) -> Outcome:
    match = _NUMBER_RE.fullmatch(claim.ref)
    if not claim.repo or not match:
        return Outcome(INSUFFICIENT, None, "a PR claim needs a number and a repo")
    data, err = _gh_json(
        run,
        [
            "pr",
            "view",
            match.group(1),
            "--repo",
            claim.repo,
            "--json",
            "state,headRefOid,isDraft,mergeCommit",
        ],
    )
    if err == "missing":
        return Outcome(MISMATCH, None, f"no PR {match.group(1)} in {claim.repo}")
    if data is None:
        return Outcome(INSUFFICIENT, None, err or "unreadable")
    state = str(data.get("state") or "").lower()
    head = data.get("headRefOid")
    observed = {"state": state, "head": head}
    expect = (claim.expect or "").strip().lower()
    if expect in _PR_STATES and state != expect:
        return Outcome(MISMATCH, T1, f"PR is {state}, not {expect}", observed)
    if expect and expect not in _PR_STATES and _SHA_RE.fullmatch(expect):
        if not _same_sha(head, expect):
            return Outcome(MISMATCH, T1, f"PR head is {head}, not {expect}", observed)
    if ctx.reviewed_head_sha and not _same_sha(head, ctx.reviewed_head_sha):
        return Outcome(
            MISMATCH,
            T1,
            f"PR head {head} is not the reviewed head {ctx.reviewed_head_sha}",
            observed,
        )
    return Outcome(VERIFIED, T1, f"PR is {state}", observed)


def _resolve_ci_run(claim: Claim, ctx: Context, run: GhRunner) -> Outcome:
    match = _NUMBER_RE.fullmatch(claim.ref)
    if not claim.repo or not match:
        return Outcome(INSUFFICIENT, None, "a CI claim needs a run id and a repo")
    data, err = _gh_json(
        run,
        [
            "run",
            "view",
            match.group(1),
            "--repo",
            claim.repo,
            "--json",
            "status,conclusion,headSha",
        ],
    )
    if err == "missing":
        return Outcome(MISMATCH, None, f"no CI run {match.group(1)} in {claim.repo}")
    if data is None:
        return Outcome(INSUFFICIENT, None, err or "unreadable")
    status = str(data.get("status") or "").lower()
    conclusion = str(data.get("conclusion") or "").lower()
    head = data.get("headSha")
    observed = {"status": status, "conclusion": conclusion, "head": head}
    if ctx.reviewed_head_sha and head and not _same_sha(head, ctx.reviewed_head_sha):
        return Outcome(
            MISMATCH,
            T1,
            f"run covers {head}, not the reviewed head {ctx.reviewed_head_sha}",
            observed,
        )
    if status != "completed":
        return Outcome(INSUFFICIENT, T1, f"run is {status or 'unknown'}, not finished", observed)
    expect = (claim.expect or "success").strip().lower()
    if expect in _CI_CONCLUSIONS and conclusion != expect:
        return Outcome(MISMATCH, T1, f"run concluded {conclusion}, not {expect}", observed)
    return Outcome(VERIFIED, T1, f"run concluded {conclusion}", observed)


def _resolve_task(claim: Claim, ctx: Context) -> Outcome:
    match = _NUMBER_RE.fullmatch(claim.ref)
    if not match:
        return Outcome(MISMATCH, None, "not a task id")
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status, claimed_by FROM tasks WHERE id=? AND org_id IS ?",
            (int(match.group(1)), message_index.tenant()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return Outcome(MISMATCH, None, f"no task {match.group(1)}")
    observed = {"status": row["status"], "claimed_by": row["claimed_by"]}
    expect = (claim.expect or "").strip().lower()
    if expect and expect != str(row["status"]).lower():
        return Outcome(MISMATCH, T2, f"task is {row['status']}, not {expect}", observed)
    return Outcome(VERIFIED, T2, f"task is {row['status']}", observed)


def _resolve_doc(claim: Claim, ctx: Context) -> Outcome:
    """A vault path or ``memory:<id>``. Agent-written, so T2 at best."""
    if claim.ref.startswith("memory:"):
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT author FROM memory WHERE id=? AND org_id IS ?",
                (claim.ref.removeprefix("memory:").strip(), message_index.tenant()),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return Outcome(MISMATCH, None, "no such memory note")
        return Outcome(VERIFIED, T2, "memory note exists", {"author": row["author"]})
    from .. import safety

    try:
        path = safety.resolve_in_vault(claim.ref)
    except Exception:  # noqa: BLE001 - an unreachable path is unchecked, not a crash
        return Outcome(INSUFFICIENT, None, "not a readable vault path")
    if not path.is_file():
        return Outcome(MISMATCH, None, "no such vault note")
    return Outcome(VERIFIED, T2, "vault note exists")
