"""MCP 2.x elicitation as an additional approval-resolution surface (task #822/#829/#832).

The approval queue is unchanged and authoritative. ``check_approval_gate`` still
parks a ``pending`` row and raises ``ApprovalPendingError`` *after* a tool's own
safety checks. This module adds exactly one thing: when the caller's MCP client
declares the elicitation capability, that pending row is surfaced to the human as
an in-client form (action summary, attributed actor, approve/deny) instead of
being reachable only through the ``/ui`` approval queue.

Transport: the negotiated protocol (2026-07-28+) carries elicitation inside an
``InputRequiredResult`` — the tool returns the form request, the client collects
the response and retries ``tools/call`` with ``input_responses``. The response
resolves the *same* row through ``approval_resolve``:

* accept + approve  -> the row becomes an ``approved`` one-shot pass; the retried
  tool call consumes it and runs.
* accept + deny     -> the row is ``denied`` with the recorded reason; the retried
  call fails with that reason (the gate raises it).
* decline / cancel / no capability / any error
                     -> the row is left ``pending`` and ``ApprovalPendingError``
  propagates unchanged, so a thin client resolves it via ``/ui`` as before.

Round-two accept is bound to a server-issued one-time challenge (task #832).
``request_state`` carries ``approval_id`` plus a CSPRNG nonce; the nonce is
persisted as a SHA-256 hash on the hub and consumed exactly once in a single
SQLite write transaction. Client-supplied ``request_state`` is never
authoritative: a fabricated, replayed, or cross-row challenge leaves the
approval row pending and does not mutate its decision. Decline/cancel do not
consume the challenge — they are not resolving calls.

No path here approves an action without a human response landing in the
``approvals`` table via ``approval_resolve``. Elicitation is an additional surface
on the gate, never a bypass of it.

Two guards keep an autonomous agent from advertising elicitation support and
approving its own pending request:

* ``UA_ELICITATION_APPROVALS=1`` — OPT-IN, off by default. Unset, the whole
  surface is dormant: a gated call still parks its row and the ``/ui`` queue is
  the only resolver. Enabling it is the operator asserting elicitation on this
  hub reaches a human.
* the calling session's profile must permit ``approval_resolve``. Elicitation
  grants exactly the authority the session already has, so a ``developer`` /
  ``steward``-profile agent (denied ``approval_resolve``) still cannot resolve a
  row this way — only an operator-authority session can, same as ``/ui``.

URL mode (task #2256). A client that negotiates ``elicitation.url`` on a
2026-07-28+ session, on a hub with a public Control Center origin
(``config.public_origin()``), is offered the existing approval page
(``/ui/inbox?kind=approval&item=<id>``) instead of the form. The URL carries no
credential, nonce or token; the human signs in to the Control Center as usual.
URL accept is only consent to open the page and never resolves anything: the
retried call waits server-side (bounded, ``_URL_WAIT_SECONDS`` per leg) for the
row to leave ``pending`` through ``approval_resolve`` by an authenticated human,
and answers a still-pending row with a state-only ``InputRequiredResult`` so the
client re-polls on its own — no model-managed retry. The continuation is bound
to the approval id, MCP session, verified principal, tool and argument digest,
expires after ``_URL_CHALLENGE_TTL_SECONDS``, and consumes its challenge exactly
once when the row resolves. Because URL accept grants no authority, URL mode does
not need the resolver profile: a developer session may be shown the page but can
only wait for a different human (``approval_resolve`` still refuses the
requester). Clients without URL capability keep the form path or the queue.

Deliberately not implemented: the pre-2026-07-28 server-initiated ``ctx.elicit()``
round-trip and its URL counterpart (the ``-32042`` URL-elicitation-required
error). A legacy client that predates ``InputRequiredResult`` does not declare
the capability the modern way, so it simply falls through to the ``/ui`` queue —
the safe direction. ponytail: add the classic path only if a legacy client needs it.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio
import anyio.to_thread
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    InputRequiredResult,
)
from mcp.types.version import is_version_at_least
from pydantic import BaseModel, Field

from . import agent_access, config, edition, events, storage
from .tools.approvals import ApprovalPendingError, approval_resolve, get_approval

#: Event-log channel for every elicitation request and its response. Hash-chained
#: with the rest of the log when the ``audit_hash_chain`` edition feature is on.
ELICITATION_CHANNEL = "agent.approval.elicitation"

#: The single key under which the approval form rides ``input_requests`` /
#: ``input_responses``.
_REQUEST_KEY = "approval"

_ELICIT_CAPABILITY = ClientCapabilities(elicitation=ElicitationCapability())

#: 32 bytes of CSPRNG material; ``token_urlsafe`` expands this to ~43 chars.
_CHALLENGE_BYTES = 32

#: First protocol revision that carries elicitation inside ``InputRequiredResult``.
_INPUT_REQUIRED_VERSION = "2026-07-28"

#: ``request_state`` marker for a URL-mode round-trip (task #2256).
_URL_MODE = "url"

#: Prefix on the stored binding digest of a URL-mode challenge. A form-mode
#: challenge stores the bare MCP session id, so the two can never satisfy each other.
_URL_BINDING_PREFIX = "url:"

#: Longest one URL-mode continuation leg waits server-side for the human before
#: answering "still pending" (the client then re-polls by itself).
_URL_WAIT_SECONDS = 30.0

#: Poll interval for the approval row while a URL-mode leg waits.
_URL_POLL_SECONDS = 1.0

#: A URL-mode challenge older than this no longer continues the round-trip; the
#: row stays queued and a fresh call starts a new one.
_URL_CHALLENGE_TTL_SECONDS = 15 * 60


class ApprovalDecision(BaseModel):
    """Structured response the rich client collects for a gated action."""

    approve: bool = Field(
        description="Approve this action? Denying leaves it blocked and records the reason.",
    )
    reason: str = Field(
        default="",
        description="Optional note recorded on the decision (context for a denial).",
    )


#: The elicitation form schema, computed once. Both fields are primitives, so the
#: plain pydantic JSON schema is already a spec-valid ``requested_schema``.
_REQUESTED_SCHEMA = ApprovalDecision.model_json_schema()


def _elicitation_available(elicit_ctx: Any) -> bool:
    """True only when a gated row may actually be resolved through elicitation.

    Three conditions, all required — any one missing falls through to ``/ui``:

    * ``UA_ELICITATION_APPROVALS=1`` — the operator opted the hub in.
    * the client declared the elicitation capability.
    * the calling session's profile permits ``approval_resolve`` — elicitation
      grants exactly the authority the session already has, so an autonomous
      developer/steward-profile agent (which is denied ``approval_resolve``)
      cannot advertise elicitation support and approve its own pending request.
    """
    if elicit_ctx is None or not config.elicitation_approvals_enabled():
        return False
    if not _profile_permits_resolve():
        return False
    return "form" in _client_modes(elicit_ctx)


def _client_modes(elicit_ctx: Any) -> frozenset[str]:
    """Elicitation modes the client declared: a subset of ``{"form", "url"}``.

    Per the 2025-11-25 spec a bare ``elicitation: {}`` means form only, and a
    url-only client cannot render a form. A session that exposes no structured
    capabilities falls back to the SDK's presence check, which counts as form.
    """
    try:
        # A real Context raises (not AttributeError) outside an active request.
        session = elicit_ctx.session
        caps = getattr(session, "client_capabilities", None)
    except Exception:
        return frozenset()
    if isinstance(caps, ClientCapabilities):
        declared = caps.elicitation
        if declared is None:
            return frozenset()
        modes = set()
        if declared.form is not None or declared.url is None:
            modes.add("form")
        if declared.url is not None:
            modes.add(_URL_MODE)
        return frozenset(modes)
    try:
        present = bool(session.check_client_capability(_ELICIT_CAPABILITY))
    except Exception:
        present = False
    return frozenset({"form"}) if present else frozenset()


def _url_elicitation_available(elicit_ctx: Any) -> bool:
    """True when this call may use URL mode (task #2256).

    Requires the hub opt-in, a 2026-07-28+ session (URL requests ride
    ``InputRequiredResult``), a declared ``elicitation.url`` capability, a
    stable MCP session id to bind the challenge to (without one, distinct
    sessions of the same caller would share a binding), and a
    credential-free public Control Center origin to link to. The resolver
    profile is not required: URL accept resolves nothing, so the session gains
    no authority from it.
    """
    if elicit_ctx is None or not config.elicitation_approvals_enabled():
        return False
    try:
        protocol = getattr(elicit_ctx, "protocol_version", None)
        modern = bool(protocol) and is_version_at_least(str(protocol), _INPUT_REQUIRED_VERSION)
    except Exception:
        modern = False
    if not modern or _URL_MODE not in _client_modes(elicit_ctx):
        return False
    if not _session_key(elicit_ctx):
        return False
    return _approval_page_url(1) is not None


def _approval_page_url(approval_id: int) -> str | None:
    """The Control Center deep link for one approval, or None.

    Built from configuration (``config.public_origin()``), never a request Host
    header. The link carries only the Inbox kind and the approval id, which the
    agent can already see: no nonce, token or other credential, and an origin
    with userinfo, a query or a fragment is refused outright.
    """
    origin = config.public_origin()
    if not origin:
        return None
    try:
        parts = urlsplit(origin)
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment or any(ord(c) < 33 or ord(c) == 127 for c in origin):
        return None
    return f"{origin}/ui/inbox?{urlencode({'kind': 'approval', 'item': int(approval_id)})}"


def _profile_permits_resolve() -> bool:
    try:
        from . import profiles

        return profiles.tool_is_allowed("approval_resolve")
    except Exception:
        return False


def _log(action: str, *, approval_id: int, tool: str, actor: str | None, **extra: Any) -> None:
    """Best-effort append of one elicitation event. Never breaks the round-trip."""
    try:
        events.record_event(
            ELICITATION_CHANNEL,
            payload={"action": action, "approval_id": approval_id, "tool": tool, **extra},
            originator=actor,
        )
    except Exception:
        pass


def _summary(tool: str, payload: dict, actor: str | None) -> str:
    who = actor or "an unattributed agent"
    lines = [f"{who} is requesting approval to run {tool!r}.", ""]
    for key, value in payload.items():
        rendered = str(value)
        if len(rendered) > 500:
            rendered = rendered[:500] + f"… [{len(rendered)} chars]"
        lines.append(f"  {key}: {rendered}")
    lines.append("")
    lines.append("Approve to let the action proceed, or deny to block it.")
    return "\n".join(lines)


def _our_response(elicit_ctx: Any) -> Any | None:
    """The client's ``ApprovalDecision`` response for this call, or None on round one."""
    if elicit_ctx is None:
        return None
    responses = getattr(elicit_ctx, "input_responses", None) or {}
    return responses.get(_REQUEST_KEY)


def _decoded_state(elicit_ctx: Any) -> tuple[int | None, str | None]:
    """``(approval_id, challenge)`` echoed in ``request_state``, or Nones.

    The challenge value is client-echoed and is never treated as authority —
    ``_consume_challenge`` checks it against the server-stored hash.
    """
    try:
        data = json.loads(getattr(elicit_ctx, "request_state", None) or "{}")
        approval_id = int(data["approval_id"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, AttributeError):
        return None, None
    challenge = data.get("challenge")
    if not isinstance(challenge, str) or not challenge.strip():
        return approval_id, None
    return approval_id, challenge.strip()


def _decoded_url_state(elicit_ctx: Any) -> tuple[int, str | None] | None:
    """``(approval_id, challenge)`` when ``request_state`` marks a URL round-trip."""
    if elicit_ctx is None:
        return None
    try:
        data = json.loads(getattr(elicit_ctx, "request_state", None) or "{}")
        if not isinstance(data, dict) or data.get("mode") != _URL_MODE:
            return None
        approval_id = int(data["approval_id"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, AttributeError):
        return None
    challenge = data.get("challenge")
    if not isinstance(challenge, str) or not challenge.strip():
        return approval_id, None
    return approval_id, challenge.strip()


def _url_binding(elicit_ctx: Any, approval_id: int, tool_name: str, kwargs: dict) -> str:
    """Digest binding a URL-mode challenge to request, session, principal and payload.

    Covers the approval id, the MCP session, the verified caller, the tool and
    a canonical digest of its arguments (so any head SHA or other field in the
    payload is bound too). Stored in place of the bare session key.
    """
    material = json.dumps(
        {
            "approval_id": int(approval_id),
            "arguments": json.dumps(kwargs, sort_keys=True, default=str),
            "principal": config.verified_agent_identity() or "",
            # Never empty: _url_elicitation_available refuses a call without one.
            "session": _session_key(elicit_ctx),
            "tool": tool_name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _URL_BINDING_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _session_key(elicit_ctx: Any) -> str | None:
    """Best-effort MCP session identity to bind a challenge to this round-trip."""
    sid = config.current_mcp_session_id()
    if sid:
        return str(sid)[:256]
    session = getattr(elicit_ctx, "session", None) if elicit_ctx is not None else None
    raw = getattr(session, "session_id", None) if session is not None else None
    if raw is None:
        return None
    text = str(raw).strip()
    return text[:256] if text else None


def _hash_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _issue_challenge(approval_id: int, session_key: str | None) -> str | None:
    """Persist a new one-time nonce for ``approval_id``. Returns the plaintext.

    Re-issuing (a second round-one on a still-pending row) replaces the hash so
    a stale nonce cannot resolve later. Returns None when the row is missing or
    not pending — the caller then falls through to ``/ui``.
    """
    nonce = secrets.token_urlsafe(_CHALLENGE_BYTES)
    nonce_hash = _hash_nonce(nonce)
    ts = storage.now()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Another tenant's approval gets no challenge (task #1793).
        row = conn.execute(
            "SELECT status FROM approvals WHERE id=? AND org_id IS ?",
            (approval_id, edition.tenant_org_id()),
        ).fetchone()
        if row is None or row["status"] != "pending":
            conn.rollback()
            return None
        conn.execute(
            """
            INSERT INTO elicitation_challenges
              (approval_id, nonce_hash, issued_at, consumed_at, session_key)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(approval_id) DO UPDATE SET
              nonce_hash=excluded.nonce_hash,
              issued_at=excluded.issued_at,
              consumed_at=NULL,
              session_key=excluded.session_key
            """,
            (approval_id, nonce_hash, ts, session_key),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return nonce


def _challenge_problem(
    row: Any,
    nonce_hash: str,
    session_key: str | None,
    *,
    url_binding: bool,
    max_age_seconds: float | None,
) -> str | None:
    """Reason code for a stored challenge that does not verify, or None."""
    if row is None:
        return "invalid"
    if row["consumed_at"] is not None:
        return "replay"
    if not hmac.compare_digest(row["nonce_hash"], nonce_hash):
        return "invalid"
    stored_session = row["session_key"]
    if url_binding:
        # URL mode binds strictly: no stored digest (a form challenge) never matches.
        if (
            not stored_session
            or not session_key
            or not stored_session.startswith(_URL_BINDING_PREFIX)
        ):
            return "binding"
        if not hmac.compare_digest(stored_session, session_key):
            return "binding"
    elif stored_session and (
        not session_key or not hmac.compare_digest(stored_session, session_key)
    ):
        return "session"
    if max_age_seconds is not None:
        try:
            issued = datetime.fromisoformat(row["issued_at"])
        except (TypeError, ValueError):
            return "expired"
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=UTC)
        if (datetime.now(UTC) - issued).total_seconds() > max_age_seconds:
            return "expired"
    return None


def _verify_url_challenge(approval_id: int, nonce: str | None, binding: str) -> str | None:
    """Check a URL-mode challenge without consuming it (the leg may still wait)."""
    if not nonce:
        return "missing"
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT nonce_hash, issued_at, consumed_at, session_key "
            "FROM elicitation_challenges WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
    finally:
        conn.close()
    return _challenge_problem(
        row,
        _hash_nonce(nonce),
        binding,
        url_binding=True,
        max_age_seconds=_URL_CHALLENGE_TTL_SECONDS,
    )


def _consume_challenge(
    approval_id: int,
    nonce: str | None,
    session_key: str | None,
    *,
    url_binding: bool = False,
    max_age_seconds: float | None = None,
) -> str | None:
    """Atomically verify and consume the one-time challenge.

    Returns None on success, otherwise a reason code (``missing``, ``invalid``,
    ``replay``, ``session``; URL mode adds ``binding`` and ``expired``). Never
    writes the ``approvals`` row — an invalid challenge must not mutate the
    decision. Decline/cancel must not call this.
    """
    if not nonce:
        return "missing"
    nonce_hash = _hash_nonce(nonce)
    ts = storage.now()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT nonce_hash, issued_at, consumed_at, session_key "
            "FROM elicitation_challenges WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        problem = _challenge_problem(
            row,
            nonce_hash,
            session_key,
            url_binding=url_binding,
            max_age_seconds=max_age_seconds,
        )
        if problem is not None:
            conn.rollback()
            return problem
        cur = conn.execute(
            """
            UPDATE elicitation_challenges
               SET consumed_at=?
             WHERE approval_id=? AND consumed_at IS NULL AND nonce_hash=?
            """,
            (ts, approval_id, nonce_hash),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return "replay"
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _elicitation_request(
    elicit_ctx: Any,
    exc: ApprovalPendingError,
    tool_name: str,
    kwargs: dict | None = None,
):
    """Return the ``InputRequiredResult`` that surfaces the pending row, or re-raise.

    URL mode first where the client negotiated it (task #2256), then the form,
    then the untouched ``ApprovalPendingError`` — the ``/ui`` queue path — for a
    client without either (or a gate row we can't identify).
    """
    approval_id = exc.approval_id
    if approval_id is None:
        raise exc
    if _url_elicitation_available(elicit_ctx):
        return _url_elicitation_request(elicit_ctx, exc, tool_name, kwargs or {})
    if not _elicitation_available(elicit_ctx):
        raise exc

    row = get_approval(approval_id) or {}
    tool = row.get("tool") or tool_name
    payload = row.get("payload") or {}
    actor = (row.get("requested_by") or config.verified_agent_identity() or "").strip() or None
    if not actor:
        # Fail closed: do not mint a challenge or advertise an unattributed agent.
        raise exc

    challenge = _issue_challenge(int(approval_id), _session_key(elicit_ctx))
    if challenge is None:
        raise exc

    _log("requested", approval_id=approval_id, tool=tool, actor=actor)
    _log("challenge_issued", approval_id=approval_id, tool=tool, actor=actor)

    request = ElicitRequest(
        params=ElicitRequestFormParams(
            message=_summary(tool, payload, actor),
            requested_schema=_REQUESTED_SCHEMA,
        )
    )
    return InputRequiredResult(
        input_requests={_REQUEST_KEY: request},
        request_state=json.dumps({"approval_id": approval_id, "challenge": challenge}),
    )


def _url_elicitation_request(
    elicit_ctx: Any, exc: ApprovalPendingError, tool_name: str, kwargs: dict
) -> InputRequiredResult:
    """Round one of URL mode: offer the Control Center page for this approval.

    The URL and the challenge ride only the protocol result, which the client
    handles; neither is logged nor put in any text the model reads.
    """
    approval_id = int(exc.approval_id)
    row = get_approval(approval_id) or {}
    tool = row.get("tool") or tool_name
    actor = (row.get("requested_by") or config.verified_agent_identity() or "").strip() or None
    url = _approval_page_url(approval_id)
    if not actor or url is None:
        raise exc
    challenge = _issue_challenge(
        approval_id, _url_binding(elicit_ctx, approval_id, tool_name, kwargs)
    )
    if challenge is None:
        raise exc

    _log("requested", approval_id=approval_id, tool=tool, actor=actor, mode=_URL_MODE)
    _log("challenge_issued", approval_id=approval_id, tool=tool, actor=actor, mode=_URL_MODE)

    message = (
        f"{actor} is requesting approval to run {tool!r} (approval request {approval_id}). "
        "Open the Control Center approval page to review it and approve or deny it "
        "there. Opening the page does not approve the action."
    )
    return InputRequiredResult(
        input_requests={
            _REQUEST_KEY: ElicitRequest(params=ElicitRequestURLParams(message=message, url=url))
        },
        request_state=_url_state(approval_id, challenge),
    )


def _url_state(approval_id: int, challenge: str) -> str:
    return json.dumps({"approval_id": approval_id, "challenge": challenge, "mode": _URL_MODE})


def _response_action(response: Any) -> str | None:
    action = getattr(response, "action", None)
    if action is None and isinstance(response, dict):
        action = response.get("action")
    return action


async def _await_resolution(approval_id: int) -> str:
    """The row's status once it leaves ``pending``, or ``pending`` after the leg's wait."""
    deadline = anyio.current_time() + _URL_WAIT_SECONDS
    while True:
        row = await anyio.to_thread.run_sync(get_approval, approval_id)
        status = (row or {}).get("status") or "missing"
        if status != "pending" or anyio.current_time() >= deadline:
            return status
        await anyio.sleep(_URL_POLL_SECONDS)


async def _url_continuation(
    sync_fn: Callable[..., Any],
    kwargs: dict[str, Any],
    *,
    tool_name: str,
    elicit_ctx: Any,
    approval_id: int,
    challenge: str | None,
) -> Any:
    """Round two and later of URL mode (task #2256).

    The client's accept (or a state-only re-poll) is never authority. The leg
    verifies the bound challenge, waits for the row to leave ``pending`` through
    ``approval_resolve`` by a human, and only then consumes the challenge and
    runs the tool once; the tool's own gate consumes the human's one-shot pass
    or raises the denial. A still-pending row gets a state-only
    ``InputRequiredResult`` so the client re-polls without prompting again.
    """
    row = get_approval(approval_id) or {}
    tool = row.get("tool") or tool_name
    actor = row.get("requested_by") or config.verified_agent_identity()
    queued = (
        f"request {approval_id} for {tool!r} stays queued; a human resolves it in the "
        f"Control Center or with approval_resolve({approval_id}, 'approve'|'deny'), "
        "then retry the identical call."
    )

    if not _url_elicitation_available(elicit_ctx):
        raise ApprovalPendingError(
            f"URL elicitation is not available for this session; {queued}",
            approval_id=approval_id,
        )

    response = _our_response(elicit_ctx)
    if response is not None:
        action = _response_action(response)
        if action != "accept":
            # Not a resolving call: the challenge stays live, the row stays pending.
            _log(
                action or "cancel", approval_id=approval_id, tool=tool, actor=actor, mode=_URL_MODE
            )
            raise ApprovalPendingError(
                f"the approval page was not opened; {queued}", approval_id=approval_id
            )

    binding = _url_binding(elicit_ctx, approval_id, tool_name, kwargs)

    def rejected(reason: str) -> ApprovalPendingError:
        _log(
            "challenge_rejected",
            approval_id=approval_id,
            tool=tool,
            actor=actor,
            reason=reason,
            mode=_URL_MODE,
        )
        return ApprovalPendingError(
            f"URL elicitation state was rejected ({reason}); {queued}", approval_id=approval_id
        )

    reason = _verify_url_challenge(approval_id, challenge, binding)
    if reason is not None:
        raise rejected(reason)

    status = await _await_resolution(approval_id)
    if status == "pending":
        return InputRequiredResult(request_state=_url_state(approval_id, challenge))

    reason = _consume_challenge(
        approval_id,
        challenge,
        binding,
        url_binding=True,
        max_age_seconds=_URL_CHALLENGE_TTL_SECONDS,
    )
    if reason is not None:
        raise rejected(reason)
    _log("challenge_consumed", approval_id=approval_id, tool=tool, actor=actor, mode=_URL_MODE)
    _log(
        "url_resolved",
        approval_id=approval_id,
        tool=tool,
        actor=actor,
        mode=_URL_MODE,
        status=status,
    )
    # Exactly one run. A pending gate here (pass expired or revoked) propagates.
    return await anyio.to_thread.run_sync(functools.partial(sync_fn, **kwargs))


def _apply_decision(elicit_ctx: Any, tool_name: str, response: Any) -> None:
    """Fold the human's elicitation response into the approval queue row.

    Raises ``ApprovalPendingError`` for a decline/cancel (the row stays pending —
    never auto-approved). An approval or denial mutates the row only after the
    server-issued challenge is consumed; the retried tool call then either
    consumes the pass or hits the denied row and fails.
    """
    approval_id, challenge = _decoded_state(elicit_ctx)
    if approval_id is None:
        return  # can't identify the row: fall through, tool re-queues, /ui resolves it

    if not _elicitation_available(elicit_ctx):
        # A client sent a round-two response to a hub where elicitation is not
        # actually enabled (flag off, or the session's profile denies
        # approval_resolve). Do not touch the row — it stays pending for /ui.
        raise ApprovalPendingError(
            f"elicitation is not enabled for this session; request {approval_id} stays queued "
            f"for approval_resolve({approval_id}, 'approve'|'deny').",
            approval_id=approval_id,
        )

    row = get_approval(approval_id) or {}
    tool = row.get("tool") or tool_name
    actor = row.get("requested_by") or config.verified_agent_identity()

    action = getattr(response, "action", None)
    if action is None and isinstance(response, dict):
        action = response.get("action")
    content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    content = content or {}

    if action != "accept":
        # Decline/cancel are not resolving calls: leave the nonce live so a
        # later accept of the same round-trip can still consume it, and so a
        # subsequent round-one can replace it. Never mutate the decision.
        _log(action or "cancel", approval_id=approval_id, tool=tool, actor=actor)
        raise ApprovalPendingError(
            f"elicitation for {tool!r} was not approved (request {approval_id} still queued); "
            f"resolve it with approval_resolve({approval_id}, 'approve'|'deny') or retry to ask again.",
            approval_id=approval_id,
        )

    agent_access.enforce("approval_resolve", read_only=False)
    reason_code = _consume_challenge(approval_id, challenge, _session_key(elicit_ctx))
    if reason_code is not None:
        _log(
            "challenge_rejected",
            approval_id=approval_id,
            tool=tool,
            actor=actor,
            reason=reason_code,
        )
        raise ApprovalPendingError(
            f"elicitation challenge for {tool!r} was rejected (request {approval_id} still queued); "
            f"resolve it with approval_resolve({approval_id}, 'approve'|'deny') or retry to ask again.",
            approval_id=approval_id,
        )

    _log("challenge_consumed", approval_id=approval_id, tool=tool, actor=actor)

    reason = (str(content.get("reason") or "")).strip() or None
    if content.get("approve"):
        approval_resolve(approval_id, "approve", reason=reason)
        _log("approved", approval_id=approval_id, tool=tool, actor=actor, reason=reason)
    else:
        approval_resolve(
            approval_id, "deny", reason=reason or "denied via elicitation, no reason given"
        )
        _log("denied", approval_id=approval_id, tool=tool, actor=actor, reason=reason)


async def guarded_call(
    sync_fn: Callable[..., Any],
    kwargs: dict[str, Any],
    *,
    tool_name: str,
    elicit_ctx: Any,
) -> Any:
    """Run a (possibly gated) sync tool, surfacing a pending gate via elicitation.

    Round one: run the tool off the event loop. If it parks in the approval queue
    and the client is capable, return the URL request (task #2256) or the form
    request. Round two of a form: the client has retried with its decision —
    apply it to the queue row, then run the tool once more so an approval
    consumes its own one-shot pass. A URL round-trip is continued by
    ``_url_continuation``, which never applies a decision itself.
    """
    url_state = _decoded_url_state(elicit_ctx)
    if url_state is not None:
        return await _url_continuation(
            sync_fn,
            kwargs,
            tool_name=tool_name,
            elicit_ctx=elicit_ctx,
            approval_id=url_state[0],
            challenge=url_state[1],
        )

    response = _our_response(elicit_ctx)
    if response is not None:
        _apply_decision(elicit_ctx, tool_name, response)

    call = functools.partial(sync_fn, **kwargs)
    try:
        return await anyio.to_thread.run_sync(call)
    except ApprovalPendingError as exc:
        if response is not None:
            # Already elicited this call; do not loop.
            raise
        return _elicitation_request(elicit_ctx, exc, tool_name, kwargs)
