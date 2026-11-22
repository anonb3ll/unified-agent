"""Send, list and ack typed inter-agent messages (tasks #483, #1859).

Every message is an ``agent.message.<act>`` row in ``event_log`` (the source of
truth) plus one derived ``message_index`` row written in the same transaction.
Reads go through the index, so mail is looked up by recipient and never falls
out of a fleet-wide newest-N window.

Read access (task #1859): a caller authenticated with a verified identity may
list only mail it sent or received. The operator's own login and the
chief-of-staff profile may list all mail (queue sweeps). A caller with no
verified identity (an unauthenticated stdio session or an in-process caller)
is not restricted, which is what it could do before; it names the mailbox it
acts for with ``to_agent``, as it names itself with ``from_agent`` on send.

Session addressing (task #1860): ``to_session`` on send targets one registered
session of the recipient label, and ``session_id`` on list, inbox and ack reads the
mailbox as that session. A session must belong to the mailbox it is used with. See
``messaging.index`` for how label mail and session mail keep separate read state.

Office addressing (task #2392): ``to_office`` or ``to_role`` names an office in
the org registry (a role is the office id or display name, the same alias
``task_list`` uses). The send writes one ordinary label message per member
except the sender. The body is not scanned for addresses.

Cursors (task #1870): mail pages and acks by ``seq`` (the event's ``event_log``
ROWID, the commit order), never by ULID. ``after_seq`` and ``next_seq`` are the
cursor, and ``message_ack(through=...)`` takes a seq or a ``message_id``. The ULID
cursors (``since``, ``next_cursor``, ``through=<ulid>``) are retired: any ULID cursor
is ambiguous when ULID order and commit order differ, so a page ordered by one can
skip mail or ack it unseen. ``since`` is still accepted as a parameter, only to
refuse it with a clear error.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, events, org_registry, profiles, registry, storage
from ..telemetry.redaction import redact_sensitive_content
from . import availability, delivery, grounding
from . import index as message_index
from .schema import (
    CHANNEL_MESSAGE_PREFIX,
    CONVERSATION_ID_RE,
    CONVERSATION_ID_RULE,
    EXTERNAL_REPLY_PREFIXES,
    MAX_LIST_LIMIT,
    MESSAGE_ACTS,
)

_TOOL = "message_send"
#: A body at least this long counts as reporting work, and ends a short-message streak.
_SUBSTANTIVE_BODY_CHARS = 200

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CompletionNotice:
    task_id: int
    delivery_id: int
    sender: str
    requester: str
    conversation: str


_completion_notice: ContextVar[_CompletionNotice | None] = ContextVar(
    "message_completion_notice", default=None
)


@dataclass(frozen=True)
class _DeliveryNotice:
    """A hub service notice: its fixed sender, recipient, id and task.

    Used by the dead-letter notice (``hub-delivery``, task #2391), the
    ready-dependent notice (``hub-graph``, task #2454) and the approval-receipt
    message (``hub-approvals``, task #2435). ``delivery.SERVICE_SENDERS`` is the
    one list of them.
    """

    original_id: str
    recipient: str
    message_id: str
    task_id: int | None
    sender: str = delivery.NOTICE_SENDER


#: Re-exported from ``delivery``, which owns the registry of service senders and
#: their reserved message-id prefixes (tasks #2391, #2435, #2454).
APPROVAL_NOTICE_SENDER = delivery.APPROVAL_NOTICE_SENDER
APPROVAL_NOTICE_ID_PREFIX = delivery.APPROVAL_NOTICE_ID_PREFIX


_delivery_notice: ContextVar[_DeliveryNotice | None] = ContextVar(
    "message_delivery_notice", default=None
)


def send_delivery_notice(
    *, original_id: str, recipient: str, body: str, task_id: int | None
) -> dict[str, Any]:
    """Tell a sender its message dead-lettered (task #2391). Internal only.

    The sender is the fixed service label ``delivery.NOTICE_SENDER``, which no
    caller can claim: the authority is this in-process path, as for a
    completion notice. The id is derived from the original message, so a retry
    is a duplicate, never a second notice.
    """
    return send_service_notice(
        sender=delivery.NOTICE_SENDER,
        message_id=f"{delivery.NOTICE_ID_PREFIX}{original_id}",
        recipient=recipient,
        body=body,
        task_id=task_id,
    )


def send_service_notice(
    *, sender: str, message_id: str, recipient: str, body: str, task_id: int | None
) -> dict[str, Any]:
    """Send one ``inform`` as an in-process service sender. Internal only.

    ``sender`` must be one of ``SERVICE_SENDERS`` and ``message_id`` must carry
    that sender's prefix, so the id is fixed by what the notice is about and a
    retry is a duplicate, never a second notice. ``send_message`` re-checks both
    against the notice it was handed, and refuses either sender or prefix from
    any other caller.
    """
    prefix = delivery.SERVICE_SENDERS.get(sender)
    if prefix is None or not message_id.startswith(prefix):
        raise ToolError("service notice sender or message id is not registered")
    notice = _DeliveryNotice(
        message_id.removeprefix(prefix), recipient, message_id, task_id, sender
    )
    identity_token = config.agent_identity_var.set(None)
    notice_token = _delivery_notice.set(notice)
    try:
        return send_message(
            act="inform",
            from_agent=sender,
            to_agent=recipient,
            body=body,
            message_id=message_id,
            task_id=task_id,
        )
    finally:
        _delivery_notice.reset(notice_token)
        config.agent_identity_var.reset(identity_token)


def send_ready_notice(
    *, event_ulid: str, recipient: str, body: str, task_id: int
) -> dict[str, Any]:
    """Tell a dependent's owner its prerequisites closed (task #2454). Internal only.

    The sender is the fixed service label ``delivery.READY_NOTICE_SENDER``,
    which no caller can claim. The id is derived from the readiness event, so a
    retry is a duplicate, never a second notice. ``expects_reply`` lets the
    existing wake gates ring an idle session that opted in.
    """
    notice = _DeliveryNotice(
        event_ulid,
        recipient,
        f"{delivery.READY_NOTICE_ID_PREFIX}{event_ulid}",
        int(task_id),
        delivery.READY_NOTICE_SENDER,
    )
    identity_token = config.agent_identity_var.set(None)
    notice_token = _delivery_notice.set(notice)
    try:
        return send_message(
            act="inform",
            from_agent=notice.sender,
            to_agent=recipient,
            body=body,
            message_id=notice.message_id,
            task_id=notice.task_id,
            expects_reply=True,
        )
    finally:
        _delivery_notice.reset(notice_token)
        config.agent_identity_var.reset(identity_token)


class CompletionNoticeRefused(ToolError):
    """The notice's handoff authority does not hold; retrying cannot change that."""


class CompletionNoticeIdTaken(CompletionNoticeRefused):
    """The notice's id is held by another message; it can never become this notice."""


class CompletionNoticeStale(ToolError):
    """The notice's delivery row or task status changed after the drain's check."""


def send_completion_notice(*, task_id: int, delivery_id: int, body: str) -> dict[str, Any]:
    """Internal outbox send; authority comes from the recorded handoff, not a caller.

    The service is delegated only this task's origin conversation. A notice to
    the service itself is allowed here; ordinary agent self-messages remain
    refused. The origin, outbox and handoff are checked under the send's lock.
    """
    from .. import completion_return

    origin = completion_return.get_origin(task_id)
    sender = completion_return._messages_report_agent()
    if not sender or not origin or origin["surface"] != "messages":
        raise ToolError("completion notice has no configured sender or messages origin")
    notice = _CompletionNotice(task_id, delivery_id, sender, origin["requester"], origin["thread"])
    identity_token = config.agent_identity_var.set(None)
    notice_token = _completion_notice.set(notice)
    try:
        return send_message(
            act="inform",
            from_agent=notice.sender,
            to_agent=notice.requester,
            body=body,
            conversation_id=notice.conversation,
            message_id=f"{completion_return.MESSAGES_ID_PREFIX}-{delivery_id}",
            task_id=task_id,
        )
    finally:
        _completion_notice.reset(notice_token)
        config.agent_identity_var.reset(identity_token)


def _reserved_message_prefix() -> str:
    from .. import completion_return

    return f"{completion_return.MESSAGES_ID_PREFIX}-"


def _is_reserved_message_id(message_id: str | None) -> bool:
    """Whether ``message_id`` is in the completion-notice id space (any case)."""
    mid = (message_id or "").strip().lower()
    return mid.startswith(_reserved_message_prefix())


def _check_completion_notice(conn: Any, notice: _CompletionNotice) -> None:
    tenant = message_index.tenant()
    # Under the send's write lock: a reopen (or any later transition) that
    # committed after the drain's pre-send check makes this notice obsolete.
    # Checked apart from the authority below, so a stale notice is not
    # recorded as a refusal.
    current = conn.execute(
        "SELECT 1 FROM tasks t JOIN completion_deliveries d ON d.task_id=t.id "
        "WHERE t.id=? AND t.org_id IS ? AND d.id=? AND d.org_id IS ? "
        "AND d.superseded_at IS NULL AND d.abandoned_at IS NULL AND t.status=d.status",
        (notice.task_id, tenant, notice.delivery_id, tenant),
    ).fetchone()
    if current is None:
        raise CompletionNoticeStale("completion notice is obsolete: its task or row changed")
    authorized = conn.execute(
        "SELECT 1 FROM task_origins o JOIN tasks t ON t.id=o.task_id "
        "JOIN completion_deliveries d ON d.task_id=t.id "
        "WHERE t.id=? AND t.org_id IS ? AND d.id=? AND d.org_id IS ? "
        "AND o.surface='messages' AND o.channel=? AND o.thread=? AND o.requester=? "
        "AND EXISTS (SELECT 1 FROM event_log h WHERE h.org_id IS ? "
        "AND h.channel='agent.conversation.handoff' AND h.originator=? "
        "AND json_extract(h.payload, '$.by')=? "
        "AND json_extract(h.payload, '$.task_id')=t.id "
        "AND json_extract(h.payload, '$.conversation_id')=o.thread) "
        "AND (?=? OR EXISTS (SELECT 1 FROM message_index i WHERE i.org_id IS ? "
        "AND i.conversation_id=o.thread AND (i.sender=? OR i.recipient=?)))",
        (
            notice.task_id,
            tenant,
            notice.delivery_id,
            tenant,
            notice.conversation,
            notice.conversation,
            notice.requester,
            tenant,
            notice.requester,
            notice.requester,
            notice.requester,
            config.operator_label(),
            tenant,
            notice.requester,
            notice.requester,
        ),
    ).fetchone()
    if authorized is None:
        raise CompletionNoticeRefused(
            "completion notice is not authorized by its conversation handoff"
        )


def _id_taken(mid: str, notice: _CompletionNotice | None) -> ToolError:
    """The refusal for a message id another message holds.

    A completion notice's id is derived from its delivery row, so a clash never
    clears: it is a terminal refusal the outbox skips (task #2060), not a
    failure retried until it is abandoned. Any other send keeps the plain error.
    """
    error = CompletionNoticeIdTaken if notice is not None else ToolError
    return error(f"message_id {mid!r} is already used by another message")


def _check_comms(sender: str, recipient: str, *, task_id: int | None) -> None:
    """Apply the per-run comms policy (task #1735).

    Imported at the call so a patch of ``operating_profiles.check_comms`` is the
    function that runs. Tests replace that module attribute.
    """
    from ..operating_profiles import check_comms

    check_comms(sender, recipient, task_id=task_id, route="message")


def send_message(
    *,
    act: str,
    to_agent: str | None = None,
    body: str,
    to_session: str | None = None,
    from_agent: str | None = None,
    from_session: str | None = None,
    conversation_id: str | None = None,
    reply_to: str | None = None,
    message_id: str | None = None,
    hop: int = 0,
    task_id: int | None = None,
    delegation_id: int | None = None,
    event_id: str | None = None,
    expects_reply: bool = False,
    to_office: str | None = None,
    to_role: str | None = None,
    handoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one typed message. Duplicate message_id is idempotent.

    ``reply_to`` must name an existing message the sender sent or received; the
    reply then joins that message's conversation unless ``conversation_id`` is
    given. The event append, the mailbox index row and the idempotency receipt
    commit together or not at all.

    ``to_session`` is a hub ``session_id``. It resolves the session's agent label as
    the recipient (``to_agent`` is optional, and must match if given), and only that
    session sees the message. Without it the message is label mail, visible to every
    session of the recipient. A session that has ended (``completed`` or ``failed``)
    cannot be targeted; a stale one can, and the result reports ``target_live``.

    ``from_session`` records the sender's own session on the message. It must be a
    session of the sender, and the call counts as that session's heartbeat. A reply
    (``reply_to``) to a message that has a ``from_session``, sent by that message's
    recipient to its sender, defaults its ``to_session`` to that session, so two
    sessions can converse; pass an explicit ``to_session`` to send it elsewhere. If
    the default session has ended the reply falls back to label mail.

    ``to_office`` / ``to_role`` address every other member of one org-registry
    office (a role is that office's id or display name). They are not combined
    with ``to_agent`` or ``to_session``, and the message body is not parsed for
    addresses. The send refuses when that member list is over
    ``UA_MESSAGE_MAX_RECIPIENTS``. Each recipient is checked against the hop and
    pair-streak guards on their own chain; one refusal writes nothing.

    ``handoff`` (task #2390) carries typed fields on a ``handoff`` act: ``done``,
    ``next_action``, ``why``, ``open_questions``, ``evidence`` and
    ``reviewed_head_sha`` (see ``messaging.grounding``). They are stored on the
    event beside the body. A malformed payload, or one on another act, is
    refused; a ``handoff`` without one is sent with a ``handoff_warnings`` entry
    (report mode).
    """
    act_val = str(act or "").strip()
    if act_val not in MESSAGE_ACTS:
        raise ToolError(f"act must be one of {tuple(sorted(MESSAGE_ACTS))}")
    typed_handoff, handoff_warnings = grounding.validate_handoff(handoff, act_val)

    explicit_conversation = (conversation_id or "").strip() or None
    if explicit_conversation is not None and not CONVERSATION_ID_RE.fullmatch(
        explicit_conversation
    ):
        # The id reaches task notes, the Messages pane, events and bridges; refuse
        # anything that could forge a marker there (#1982). Generated ids are ULIDs.
        raise ToolError(CONVERSATION_ID_RULE)

    sender = config.verified_agent_identity() or from_agent
    dnotice = _delivery_notice.get()
    if dnotice is not None:
        # The service sender is not a roster label; only this path may use it.
        if sender != dnotice.sender or sender not in delivery.SERVICE_SENDERS:
            raise ToolError("delivery notice does not match its sender")
    else:
        if (sender or "").strip().lower() in delivery.SERVICE_SENDERS:
            raise ToolError(f"from_agent {sender.strip()!r} is reserved for the hub")
        sender = _validate_label(sender, "from_agent")
    # The sender's own session is checked and heartbeated first, so a session that
    # only retries a committed send stays live and a bad from_session fails on a
    # retry as it does on a first send.
    sender_session = _label_or_none(from_session)
    if sender_session is not None:
        _own_session(sender_session, sender)
    # A retry of a committed send returns its saved result before anything that can
    # change after the first attempt is checked: the target session may have ended
    # since, and a retry must not fail for that.
    if _completion_notice.get() is None and _is_reserved_message_id(message_id):
        # Notice ids are sequential, so a public send could take one first and
        # make every notice attempt fail until abandoned (PR #1146 review).
        raise ToolError(
            f"message_id prefix {_reserved_message_prefix()!r} is reserved for "
            "completion notices; choose another message_id or omit it"
        )
    if dnotice is None:
        for prefix in delivery.SERVICE_SENDERS.values():
            if (message_id or "").strip().lower().startswith(prefix):
                raise ToolError(
                    f"message_id prefix {prefix!r} is reserved for hub service notices; "
                    "choose another message_id or omit it"
                )
    saved = _saved_result(message_id, sender)
    if saved is not None:
        return saved
    office = _addressed_office(to_office, to_role)
    if office is not None and dnotice is not None:
        raise ToolError("delivery notice does not match its recipient")
    if office is not None:
        # A completion notice is one recorded recipient. An office target cannot
        # become that notice, and it must not fall through to label addressing.
        if _completion_notice.get() is not None:
            raise CompletionNoticeRefused("completion notice does not match its handoff")
        return _send_to_office(
            office=office,
            act=act_val,
            sender=sender,
            sender_session=sender_session,
            body=body,
            to_agent=to_agent,
            to_session=to_session,
            to_office=to_office,
            to_role=to_role,
            explicit_conversation=explicit_conversation,
            reply_to=reply_to,
            message_id=message_id,
            hop=hop,
            task_id=task_id,
            delegation_id=delegation_id,
            event_id=event_id,
            expects_reply=expects_reply,
            typed_handoff=typed_handoff,
            handoff_warnings=handoff_warnings,
        )
    target_session = _label_or_none(to_session)
    if target_session is None and _label_or_none(reply_to):
        target_session = _reply_default_session(_label_or_none(reply_to), sender, to_agent)
    target_live: bool | None = None
    fallback: dict[str, Any] | None = None
    if target_session is not None:
        session = registry.get_session(target_session)
        if session is None:
            raise ToolError(f"unknown session {target_session!r}")
        if session["status"] in ("completed", "failed"):
            raise ToolError(
                f"session {target_session!r} has ended ({session['status']}); "
                "send to the agent label instead"
            )
        named = _label_or_none(to_agent)
        if named is not None and named != session["agent"]:
            raise ToolError(
                f"to_agent {named!r} does not match session {target_session!r}, "
                f"which belongs to {session['agent']!r}"
            )
        to_agent = session["agent"]
        target_live = bool(session["live"])
        if session["status"] == "abandoned" or not target_live:
            # Task #2547: the reaper ends almost every session as abandoned, and a
            # session past the live window has most likely restarted under a new
            # id. Session mail to either would wait unread and unseen by the
            # label's other sessions, so it goes to the label instead, as an
            # unaddressed reply already does (_reply_default_session).
            fallback = {
                "requested": target_session,
                "reason": "abandoned" if session["status"] == "abandoned" else "not_live",
                "delivered_as": "label",
            }
            target_session, target_live = None, None
    recipient = _validate_label(to_agent, "to_agent")
    notice = _completion_notice.get()
    if notice is not None and (
        act_val != "inform"
        or sender != notice.sender
        or recipient != notice.requester
        or explicit_conversation != notice.conversation
        or task_id != notice.task_id
        or (message_id or "").strip() != f"{_reserved_message_prefix()}{notice.delivery_id}"
    ):
        raise CompletionNoticeRefused("completion notice does not match its handoff")
    if dnotice is not None and (
        act_val != "inform"
        or recipient != dnotice.recipient
        or (message_id or "").strip() != dnotice.message_id
        or task_id != dnotice.task_id
        or typed_handoff is not None
    ):
        raise ToolError("delivery notice does not match its message")
    if sender == recipient and notice is None:
        raise ToolError("from_agent and to_agent must differ")
    # Per-run comms policy (task #1735): a funnel run routes agent traffic
    # for the operator through its liaison. Covers the crew path too, and
    # replies, which are ordinary sends.
    _check_comms(sender, recipient, task_id=task_id)
    # Availability preflight (task #2392). A read, never a write: it reports and
    # warns, and only an unaddressable label (which addressing already refuses)
    # stops a send. A resolver failure fails open.
    ready = availability.preflight_label(recipient)
    refusal = availability.reject_reason(ready)
    if refusal:
        raise ToolError(refusal)

    max_hops = config.message_max_hops()
    try:
        hop_val = int(hop)
    except (TypeError, ValueError) as exc:
        raise ToolError("hop must be an integer") from exc
    if hop_val < 0 or hop_val >= max_hops:
        raise ToolError(f"hop {hop_val} exceeds limit {max_hops} (0..{max_hops - 1})")

    text = body if isinstance(body, str) else str(body)
    encoded = text.encode("utf-8")
    max_bytes = config.message_max_body_bytes()
    if len(encoded) > max_bytes:
        raise ToolError(f"body exceeds {max_bytes} bytes")
    text = redact_sensitive_content(text, max_len=max_bytes)
    text = _enforce_utf8_byte_limit(text, max_bytes)

    mid = (message_id or "").strip() or events.new_ulid()
    reply = (reply_to or "").strip() or None

    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if notice is not None:
            _check_completion_notice(conn, notice)
        if not storage.try_claim_idempotency(conn, mid, _TOOL):
            cached = storage.get_idempotency_record(conn, mid, _TOOL) or {}
            conn.rollback()
            # Another sender's receipt (recipient, conversation) is not the caller's
            # to see: a message_id held by someone else is refused, not echoed.
            if cached.get("from_agent") != sender:
                raise _id_taken(mid, notice)
            return {**cached, "duplicate": True}
        # An indexed message with no receipt of its own (backfilled history) still
        # holds its id. The lookup is this tenant's only (#1979): another tenant's
        # use of the id neither blocks it nor is disclosed.
        if message_index.get_message(conn, mid) is not None:
            conn.rollback()
            raise _id_taken(mid, notice)

        inherited = _check_reply(conn, reply, sender)
        conversation = explicit_conversation or inherited or events.new_ulid()
        loop_warning: str | None = None
        if notice is None and not (
            config.is_human_identity(sender) or config.is_human_identity(recipient)
        ):
            hop_val = _reply_hop(conn, reply, hop_val)
            if hop_val >= max_hops:
                conn.rollback()
                raise ToolError(
                    f"reply chain reached hop {hop_val} (limit {max_hops}); "
                    "stop replying here, or send without reply_to if this is new work"
                )
            loop_warning = _check_pair_streak(conn, reply, sender, recipient, text)
        payload = {
            "message_id": mid,
            "act": act_val,
            "conversation_id": conversation,
            "reply_to": reply,
            "from_agent": sender,
            "to_agent": recipient,
            "to_session": target_session,
            "from_session": sender_session,
            "hop": hop_val,
            "body": text,
            "task_id": task_id,
            "delegation_id": delegation_id,
            "event_id": (event_id or "").strip() or None,
            "expects_reply": bool(expects_reply),
        }
        if typed_handoff is not None:
            payload["handoff"] = typed_handoff
        ulid, _created, publish_payload = events.record_event_once_on_connection(
            conn,
            f"{CHANNEL_MESSAGE_PREFIX}.{act_val}",
            payload=payload,
            originator=sender,
            episode_id=conversation,
            causal_parent_id=reply or payload["event_id"],
        )
        stored = conn.execute(
            "SELECT created_at, ROWID AS seq FROM event_log WHERE ulid=?", (ulid,)
        ).fetchone()
        message_index.insert_on_connection(
            conn,
            message_id=mid,
            recipient=recipient,
            sender=sender,
            act=act_val,
            conversation_id=conversation,
            reply_to=reply,
            expects_reply=bool(expects_reply),
            task_id=task_id,
            delegation_id=delegation_id,
            event_ulid=ulid,
            created_at=stored["created_at"],
            event_seq=int(stored["seq"]),
            target_session=target_session,
            sender_session=sender_session,
        )
        result = {
            "accepted": True,
            "duplicate": False,
            "message_id": mid,
            "event_ulid": ulid,
            "act": act_val,
            "conversation_id": conversation,
            "from_agent": sender,
            "to_agent": recipient,
            "to_session": target_session,
            "from_session": sender_session,
            "hop": hop_val,
            "task_id": task_id,
            "delegation_id": delegation_id,
            "reply_to": reply,
            "expects_reply": bool(expects_reply),
        }
        if target_live is not None:
            result["target_live"] = target_live
        if fallback is not None:
            result["session_fallback"] = fallback
        if loop_warning is not None:
            result["loop_warning"] = loop_warning
        result["availability"] = ready.as_dict()
        if ready.warning is not None:
            result["delivery_warning"] = ready.warning
        _note_handoff(result, typed_handoff, handoff_warnings)
        storage.record_idempotency_required(conn, mid, _TOOL, result)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    if publish_payload is not None:
        events.publish(publish_payload)
    try:
        from .. import slack_outbound

        slack_outbound.schedule_mirror_hub_message(message_result=result, body=text)
    except (ImportError, RuntimeError) as exc:
        log.warning(
            "Slack mirror scheduling skipped after message_send %s: %s",
            mid,
            type(exc).__name__,
        )
    return result


def _note_handoff(
    result: dict[str, Any], typed: dict[str, Any] | None, warnings: list[str]
) -> None:
    """Report a handoff's typed fields on the send result (task #2390, report mode)."""
    if typed is not None:
        result["handoff_typed"] = True
        result["handoff_claims"] = len(grounding.claims_of(typed))
    elif warnings:
        result["handoff_typed"] = False
    if warnings:
        result["handoff_warnings"] = list(warnings)


def _saved_result(message_id: str | None, sender: str) -> dict[str, Any] | None:
    """The committed result for ``message_id`` when ``sender`` sent it, else None."""
    mid = (message_id or "").strip()
    if not mid:
        return None
    conn = storage.connect()
    try:
        cached = storage.get_idempotency_record(conn, mid, _TOOL)
    finally:
        conn.close()
    if not cached or not cached.get("accepted") or cached.get("from_agent") != sender:
        return None
    return {**cached, "duplicate": True}


def _reply_default_session(reply_to: str, sender: str, to_agent: str | None) -> str | None:
    """The session a reply should go to when the caller names none, or None.

    Applies when ``sender`` is the parent's recipient and is answering the parent's
    sender (``to_agent`` omitted or that label), and the parent recorded a
    ``from_session`` that still belongs to that sender, has not ended and is live.
    Anything else is label mail; ``_check_reply`` still decides whether the reply is allowed.
    """
    conn = storage.connect()
    try:
        parent = message_index.get_message(conn, reply_to)
    finally:
        conn.close()
    if parent is None or not parent["sender_session"] or parent["recipient"] != sender:
        return None
    if _label_or_none(to_agent) not in (None, parent["sender"]):
        return None
    session = registry.get_session(parent["sender_session"])
    if session is None or session["agent"] != parent["sender"]:
        return None
    if session["status"] in ("completed", "failed"):
        return None
    if not session.get("live"):
        # Open but not seen within the live window: the asker most likely
        # restarted under a new session. Session mail would wait for that dead
        # session and dead-letter; label mail reaches the agent's live sessions.
        return None
    return parent["sender_session"]


def _check_reply(conn: Any, reply_to: str | None, sender: str) -> str | None:
    """Validate ``reply_to`` and return the parent's conversation id.

    A reply must name an existing message the sender was party to. A reference
    in an external namespace (``ts:``) is stored as given and not checked. The
    same error covers "no such message" and "not yours" so a probe learns
    nothing about other agents' mail.
    """
    if reply_to is None or reply_to.startswith(EXTERNAL_REPLY_PREFIXES):
        return None
    parent = message_index.get_message(conn, reply_to)
    if parent is None or sender not in (parent["sender"], parent["recipient"]):
        raise ToolError(
            f"reply_to {reply_to!r} is not a message you sent or received; "
            "reply_to must name an existing message you were party to"
        )
    return parent["conversation_id"]


def _chain_row(conn: Any, message_id: str) -> Any:
    """One message of a reply chain, with the hop and body from its event, or None.

    Retention can prune the event while the index row stays; ``hop`` and ``body``
    are then NULL.
    """
    return conn.execute(
        "SELECT m.sender, m.recipient, m.reply_to, "
        "json_extract(e.payload, '$.hop') AS hop, "
        "json_extract(e.payload, '$.body') AS body "
        "FROM message_index m LEFT JOIN event_log e ON e.ulid = m.event_ulid "
        "WHERE m.message_id = ? AND m.org_id IS ?",
        (message_id, message_index.tenant()),
    ).fetchone()


def _reply_hop(conn: Any, reply_to: str | None, requested: int) -> int:
    """The hop for a reply: one past its parent, or ``requested`` when that is higher.

    A caller cannot lower it, because a loop's own messages would then never reach
    the limit (task #2387). A bridge may still raise it. An external reference
    (``ts:``) has no row here and keeps ``requested``. A parent whose event was
    pruned has no hop left, so counting restarts there; a loop would have to outlast
    event retention to benefit, and refusing would block replies to old mail.
    """
    if reply_to is None:
        return requested
    parent = _chain_row(conn, reply_to)
    if parent is None:
        return requested
    return max(requested, int(parent["hop"] or 0) + 1)


def _check_pair_streak(
    conn: Any, reply_to: str | None, sender: str, recipient: str, text: str
) -> str | None:
    """Refuse a reply that extends a run of short messages between one pair.

    Two agents trading acknowledgements stay under the hop limit for a long time
    and do nothing. The count comes from the stored reply chain, so it survives a
    restart. A substantive body ends the run, as does a third party, a parent whose
    body was pruned, or one side sending twice in a row: follow-ups to your own
    message are progress, not an exchange. Returns a warning on the last message
    still allowed.
    """
    limit = config.message_pair_streak_max()
    if limit == 0 or len(text.strip()) >= _SUBSTANTIVE_BODY_CHARS:
        return None
    pair = {sender, recipient}
    streak = 1
    speaker = sender
    current = reply_to
    while current is not None and streak < limit:
        row = _chain_row(conn, current)
        if row is None or {row["sender"], row["recipient"]} != pair or row["sender"] == speaker:
            break
        body = row["body"]
        if not isinstance(body, str) or len(body.strip()) >= _SUBSTANTIVE_BODY_CHARS:
            break
        streak += 1
        speaker = row["sender"]
        current = row["reply_to"]
    if streak >= limit:
        raise ToolError(
            f"{streak} short messages in a row between {sender} and {recipient}; "
            "do the work and report the result, or stop replying"
        )
    if streak == limit - 1:
        return (
            f"{streak} short messages in a row with {recipient}; the next short reply "
            "in this chain will be refused"
        )
    return None


def _addressed_office(to_office: str | None, to_role: str | None) -> dict[str, Any] | None:
    """The office a structured target names, or None when this send is not one.

    ``to_role`` is an alias for the office id or display name. Both arguments,
    when set, have to name that same office. The message body is not consulted.
    """
    office_token = _label_or_none(to_office)
    role_token = _label_or_none(to_role)
    if office_token is None and role_token is None:
        return None
    try:
        office_row = org_registry.find_office(office_token) if office_token else None
        role_row = org_registry.find_office(role_token) if role_token else None
    except ValueError as exc:
        raise ToolError(f"org registry could not be loaded: {exc}") from exc
    if (
        office_token
        and role_token
        and (office_row is None or role_row is None or office_row["id"] != role_row["id"])
    ):
        raise ToolError("office and role must resolve to the same office")
    selected = office_row if office_row is not None else role_row
    if selected is None:
        named = office_token if office_token is not None else role_token
        raise ToolError(f"unknown office or role {named!r}")
    return selected


def _office_recipients(office: dict[str, Any], sender: str) -> list[str]:
    """Members who will receive the send, in charter order, under the fan-out cap.

    The sender is omitted. A direct send already refuses a message to yourself,
    and the rest of the office should still be reachable.
    """
    members = [label for label in office["members"] if label != sender]
    if not members:
        raise ToolError(f"office {office['id']!r} includes only the sender; name another recipient")
    cap = config.message_max_recipients()
    if len(members) > cap:
        raise ToolError(
            f"office {office['id']!r} resolves to {len(members)} recipients, "
            f"over the per-send limit of {cap}"
        )
    return members


def _send_to_office(
    *,
    office: dict[str, Any],
    act: str,
    sender: str,
    sender_session: str | None,
    body: str,
    to_agent: str | None,
    to_session: str | None,
    to_office: str | None,
    to_role: str | None,
    explicit_conversation: str | None,
    reply_to: str | None,
    message_id: str | None,
    hop: int,
    task_id: int | None,
    delegation_id: int | None,
    event_id: str | None,
    expects_reply: bool,
    typed_handoff: dict[str, Any] | None = None,
    handoff_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Write one label message per office member, or write nothing.

    Hop and pair-streak are the single-recipient guards, run once per member
    against that member's own chain. Any refusal rolls the whole send back.
    """
    if _label_or_none(to_agent) is not None or _label_or_none(to_session) is not None:
        raise ToolError("address an office or role, or one agent or session; not both")
    recipients = _office_recipients(office, sender)
    for label in recipients:
        _validate_label(label, "to_agent")
    # Availability preflight per member (task #2392). An unaddressable member
    # refuses the whole send; a quiet one is reported and still written, because
    # label mail waits. A resolver failure fails open.
    readiness = {label: availability.preflight_label(label) for label in recipients}
    for ready in readiness.values():
        refusal = availability.reject_reason(ready)
        if refusal:
            raise ToolError(refusal)

    text = body if isinstance(body, str) else str(body)
    encoded = text.encode("utf-8")
    max_bytes = config.message_max_body_bytes()
    if len(encoded) > max_bytes:
        raise ToolError(f"body exceeds {max_bytes} bytes")
    text = redact_sensitive_content(text, max_len=max_bytes)
    text = _enforce_utf8_byte_limit(text, max_bytes)

    max_hops = config.message_max_hops()
    try:
        hop_val = int(hop)
    except (TypeError, ValueError) as exc:
        raise ToolError("hop must be an integer") from exc
    if hop_val < 0 or hop_val >= max_hops:
        raise ToolError(f"hop {hop_val} exceeds limit {max_hops} (0..{max_hops - 1})")

    for label in recipients:
        _check_comms(sender, label, task_id=task_id)

    mid = (message_id or "").strip() or events.new_ulid()
    reply = (reply_to or "").strip() or None
    child_ids = [f"{mid}:{index}" for index in range(len(recipients))]

    conn = storage.connect()
    publish_payloads: list[dict[str, Any]] = []
    delivered: list[dict[str, Any]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not storage.try_claim_idempotency(conn, mid, _TOOL):
            cached = storage.get_idempotency_record(conn, mid, _TOOL) or {}
            conn.rollback()
            if cached.get("from_agent") != sender:
                raise _id_taken(mid, None)
            return {**cached, "duplicate": True}
        for child in child_ids:
            if message_index.get_message(conn, child) is not None:
                conn.rollback()
                raise _id_taken(child, None)
        inherited = _check_reply(conn, reply, sender)
        conversation = explicit_conversation or inherited or events.new_ulid()
        # Guards first, then inserts, so a later recipient's refusal cannot
        # leave an earlier recipient's row behind.
        plans: list[tuple[str, str, int, str | None]] = []
        for label, child in zip(recipients, child_ids, strict=True):
            charged = hop_val
            warning: str | None = None
            if not (config.is_human_identity(sender) or config.is_human_identity(label)):
                charged = _reply_hop(conn, reply, hop_val)
                if charged >= max_hops:
                    conn.rollback()
                    raise ToolError(
                        f"reply chain reached hop {charged} (limit {max_hops}); "
                        "stop replying here, or send without reply_to if this is new work"
                    )
                warning = _check_pair_streak(conn, reply, sender, label, text)
            plans.append((label, child, charged, warning))
        for label, child, charged, warning in plans:
            payload = {
                "message_id": child,
                "act": act,
                "conversation_id": conversation,
                "reply_to": reply,
                "from_agent": sender,
                "to_agent": label,
                "to_session": None,
                "from_session": sender_session,
                "hop": charged,
                "body": text,
                "task_id": task_id,
                "delegation_id": delegation_id,
                "event_id": (event_id or "").strip() or None,
                "expects_reply": bool(expects_reply),
            }
            if typed_handoff is not None:
                payload["handoff"] = typed_handoff
            ulid, _created, publish_payload = events.record_event_once_on_connection(
                conn,
                f"{CHANNEL_MESSAGE_PREFIX}.{act}",
                payload=payload,
                originator=sender,
                episode_id=conversation,
                causal_parent_id=reply or payload["event_id"],
            )
            stored = conn.execute(
                "SELECT created_at, ROWID AS seq FROM event_log WHERE ulid=?", (ulid,)
            ).fetchone()
            message_index.insert_on_connection(
                conn,
                message_id=child,
                recipient=label,
                sender=sender,
                act=act,
                conversation_id=conversation,
                reply_to=reply,
                expects_reply=bool(expects_reply),
                task_id=task_id,
                delegation_id=delegation_id,
                event_ulid=ulid,
                created_at=stored["created_at"],
                event_seq=int(stored["seq"]),
                target_session=None,
                sender_session=sender_session,
            )
            result = {
                "accepted": True,
                "duplicate": False,
                "message_id": child,
                "event_ulid": ulid,
                "act": act,
                "conversation_id": conversation,
                "from_agent": sender,
                "to_agent": label,
                "to_session": None,
                "from_session": sender_session,
                "hop": charged,
                "task_id": task_id,
                "delegation_id": delegation_id,
                "reply_to": reply,
                "expects_reply": bool(expects_reply),
            }
            if warning is not None:
                result["loop_warning"] = warning
            result["availability"] = readiness[label].as_dict()
            if readiness[label].warning is not None:
                result["delivery_warning"] = readiness[label].warning
            delivered.append(result)
            if publish_payload is not None:
                publish_payloads.append(publish_payload)
        batch = {
            "accepted": True,
            "duplicate": False,
            "message_id": mid,
            "from_agent": sender,
            "office": office["id"],
            "to_office": _label_or_none(to_office),
            "to_role": _label_or_none(to_role),
            "conversation_id": conversation,
            "count": len(delivered),
            "recipients": delivered,
            "availability": {label: ready.as_dict() for label, ready in readiness.items()},
            "warnings": [w for w in (ready.warning for ready in readiness.values()) if w],
        }
        _note_handoff(batch, typed_handoff, handoff_warnings or [])
        storage.record_idempotency_required(conn, mid, _TOOL, batch)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    for payload in publish_payloads:
        events.publish(payload)
    try:
        from .. import slack_outbound

        for result in delivered:
            slack_outbound.schedule_mirror_hub_message(message_result=result, body=text)
    except (ImportError, RuntimeError) as exc:
        log.warning(
            "Slack mirror scheduling skipped after message_send %s: %s",
            mid,
            type(exc).__name__,
        )
    return batch


@dataclass(frozen=True)
class _Viewer:
    """Who is reading, and how far their read access reaches."""

    identity: str | None  # verified label; None for an unauthenticated caller
    unrestricted: bool


def _viewer() -> _Viewer:
    identity = config.verified_agent_identity()
    if not identity:
        # No verified identity: stdio without auth or an in-process caller.
        # Not restricted, as before; it names its mailbox with ``to_agent``.
        return _Viewer(None, True)
    unrestricted = (
        identity == config.operator_label() or profiles.current() == profiles.CHIEF_OF_STAFF
    )
    return _Viewer(identity, unrestricted)


def _label_or_none(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _own_session(session_id: str, mailbox: str, *, revive: bool = True) -> dict[str, Any]:
    """Return the session if it belongs to ``mailbox``, and count the call as its heartbeat.

    Mailbox reads pass ``revive=False`` (task #2547): reading an ended session's
    stranded mail is recovery by another session, not a sign it is alive.

    One error covers "no such session" and "someone else's session", so a probe
    learns nothing about other agents' sessions.
    """
    session = registry.get_session(session_id)
    if session is None or session["agent"] != mailbox:
        raise ToolError(f"session {session_id!r} is not a session of {mailbox!r}")
    registry.touch_session(session_id, revive=revive)
    return session


def list_messages(
    *,
    conversation_id: str | None = None,
    to_agent: str | None = None,
    from_agent: str | None = None,
    act: str | None = None,
    limit: int = 50,
    since: str | None = None,
    unread_only: bool = False,
    count_only: bool = False,
    waiting_on_reply: bool = False,
    message_id: str | None = None,
    mark_read: bool = False,
    session_id: str | None = None,
    after_seq: int | None = None,
    commit_mark_read: Callable[[Any, int], None] | None = None,
) -> dict[str, Any]:
    """Read the mailbox index, oldest first, with an exclusive ``after_seq`` cursor.

    Rows come in commit order (``seq``). Page with ``after_seq=<previous
    next_seq>``: a message that committed late (an older ULID than one you already
    paged past) still has a higher seq, so it is never skipped. ``since`` (a ULID
    cursor) is retired and raises an error; it is kept as a parameter only so the
    error is explicit.

    Filters are exact matches. A verified caller sees only mail it sent or
    received (see the module docstring); the operator login and the
    chief-of-staff profile see all of it. ``unread_only`` with no ``to_agent``
    or ``from_agent`` means the caller's own inbox. ``waiting_on_reply`` lists
    mail the caller sent with ``expects_reply`` that its recipient has not
    answered. ``count_only`` returns counts and no rows. ``mark_read`` stamps
    ``read_at`` on the returned rows addressed to the caller; it is not offered
    by the read-only ``message_list`` tool, only by the ``message_inbox``
    coordination action.

    ``session_id`` reads the mailbox as one of the caller's sessions: it sees label
    mail plus mail addressed to that session, and read state (``read_at``,
    ``unread_only``, ``unread``, ``mark_read``) is that session's own. It must be a
    session of the mailbox owner, and the call counts as a heartbeat for it. Without
    it the caller sees label mail only.
    """
    if act is not None and str(act).strip() not in MESSAGE_ACTS:
        raise ToolError(f"act must be one of {tuple(sorted(MESSAGE_ACTS))}")
    act_val = str(act).strip() if act else None
    viewer = _viewer()
    recipient = _label_or_none(to_agent)
    sender = _label_or_none(from_agent)
    me = viewer.identity
    if _label_or_none(since) is not None:
        raise ToolError("since (ULID) cursors are retired; page with after_seq and next_seq")
    after = _after_seq(after_seq)

    if waiting_on_reply and me is not None and not viewer.unrestricted:
        # The waiting view is what the caller sent and still awaits an answer to.
        if sender not in (None, me):
            raise ToolError("waiting_on_reply lists your own sent messages; from_agent must be you")
        sender = me
    party: str | None = None
    if me is not None and not viewer.unrestricted:
        if recipient not in (None, me) and sender != me:
            raise ToolError(
                "you may list only messages you sent or received; "
                f"to_agent {recipient!r} is outside your mailbox"
            )
        if recipient is None:
            # No recipient named: scope to my own mail, which for a foreign
            # from_agent means that agent's messages to me.
            party = me

    # The mailbox whose read state this call touches: the verified caller's
    # own, or for an unauthenticated caller the one it names with to_agent.
    mailbox = me or recipient
    if (unread_only or mark_read) and recipient is None and sender is None:
        if mailbox is None:
            raise ToolError("to_agent is required without a verified identity")
        recipient = mailbox
    if mark_read and mailbox is None:
        raise ToolError("to_agent is required without a verified identity")
    sid = _label_or_none(session_id)
    if sid is not None:
        if mailbox is None:
            raise ToolError("to_agent is required without a verified identity")
        _own_session(sid, mailbox, revive=False)
    # Acting as the mailbox owner: apply the session-addressing rule.
    scope = (mailbox, sid) if mailbox is not None and recipient in (None, mailbox) else None

    filters: dict[str, Any] = {
        "recipient": recipient,
        "sender": sender,
        "party": party,
        "conversation_id": _label_or_none(conversation_id),
        "act": act_val,
        "message_id": _label_or_none(message_id),
        "after_seq": after,
        "unread_only": bool(unread_only),
        "waiting_on_reply": bool(waiting_on_reply),
        "mailbox": scope,
        "session_id": sid,
    }
    conn = storage.connect()
    try:
        if count_only:
            result: dict[str, Any] = {
                "messages": [],
                "total": 0,
                "count": message_index.count(conn, **filters),
            }
        else:
            rows = message_index.query(
                conn, **filters, limit=max(1, min(int(limit), MAX_LIST_LIMIT))
            )
            marked = 0
            if mark_read and mailbox is not None:
                own = [
                    r["message_id"] for r in rows if r["to_agent"] == mailbox and not r["read_at"]
                ]
                if own:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        marked, stamp = message_index.mark_read(conn, mailbox, own, sid)
                        if commit_mark_read is None:
                            conn.commit()
                        else:
                            commit_mark_read(conn, marked)
                    except BaseException:
                        conn.rollback()
                        raise
                    for row in rows:
                        if row["message_id"] in own:
                            row["read_at"] = stamp
                            if row["status"] == "delivered":
                                row["status"] = "read"
            # Delivery facts (task #2391): admitted, appended, read, terminal.
            delivery.attach_facts(conn, rows)
            seqs = [r["seq"] for r in rows if r["seq"] is not None]
            result = {
                "messages": rows,
                "total": len(rows),
                # The cursor to resume with: the largest seq returned, else the
                # cursor sent (0 for an empty first call). Never moves backwards.
                "next_seq": max(seqs) if seqs else (after if after is not None else 0),
            }
            if mark_read:
                result["marked_read"] = marked
        if mailbox is not None:
            result["unread"] = message_index.unread_count(mailbox, conn, sid)
        if sid is not None:
            result["session_id"] = sid
        return result
    finally:
        conn.close()


def _after_seq(value: int | None) -> int | None:
    """Validate an ``after_seq`` cursor: a non-negative integer, or None."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolError("after_seq must be a non-negative integer (a next_seq from a prior call)")
    return value


def ack_messages(
    *,
    message_id: str | None = None,
    through: int | str | None = None,
    to_agent: str | None = None,
    session_id: str | None = None,
    include_pruned: bool = False,
) -> dict[str, Any]:
    """Ack the caller's own mail: one message, or everything up to a cursor.

    ``through`` is a ``seq`` (an integer, such as ``next_seq``) or the
    ``message_id`` of a row in the caller's mailbox, which resolves to that row's
    seq. It acks only mail whose seq is at or below the cursor's, so mail that
    committed after the cursor was taken is never acked unseen, even if its ULID
    sorts earlier. ULIDs are not accepted, and a digit string is a ``message_id``,
    not a seq. Rows whose event retention pruned before the index carried a seq
    have no position and are not matched by a seq cursor; ``include_pruned=True``
    also acks those (they are older than anything the log still holds). Only the
    recipient can ack: a verified
    caller acks its own mailbox, an unauthenticated one the mailbox it names
    with ``to_agent``. Acking also marks the mail read, and repeating an ack
    changes nothing. With ``session_id`` the ack is that session's: it can ack
    label mail (leaving the label's other sessions' copy unacked) and mail
    addressed to the session; without it only label mail is in reach.
    """
    if not isinstance(include_pruned, bool):
        # The coordination action takes a free-form params dict, so a string such
        # as "false" must not read as true and ack mail the caller meant to keep.
        raise ToolError("include_pruned must be a boolean (true or false)")
    mid = _label_or_none(message_id)
    cursor: str | None = None
    seq_cursor: int | None = None
    if isinstance(through, bool):
        raise ToolError("through must be a seq (integer, a next_seq) or a message_id")
    if isinstance(through, int):
        if through < 0:
            raise ToolError("through must be a non-negative seq")
        seq_cursor = through
    else:
        cursor = _label_or_none(through)
    if (mid is None) == (cursor is None and seq_cursor is None):
        raise ToolError("pass exactly one of message_id or through")
    viewer = _viewer()
    named = _label_or_none(to_agent)
    if viewer.identity is not None:
        if named not in (None, viewer.identity):
            raise ToolError("you can ack only your own mailbox")
        mailbox = viewer.identity
    else:
        if named is None:
            raise ToolError("to_agent is required without a verified identity")
        mailbox = named
    sid = _label_or_none(session_id)
    if sid is not None:
        _own_session(sid, mailbox, revive=False)

    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            through_seq: int | None = seq_cursor
            if mid is not None:
                row = conn.execute(
                    "SELECT message_id FROM message_index WHERE message_id=? AND recipient=? "
                    "AND org_id IS ? AND (target_session IS NULL OR target_session=?)",
                    (mid, mailbox, message_index.tenant(), sid),
                ).fetchone()
                if row is None:
                    raise ToolError(
                        f"no message {mid!r} in the {mailbox!r} mailbox "
                        "(mail addressed to one session needs that session_id)"
                    )
            elif cursor is not None:
                row = conn.execute(
                    "SELECT event_seq FROM message_index WHERE recipient=? AND message_id=? "
                    "AND org_id IS ? AND (target_session IS NULL OR target_session=?)",
                    (mailbox, cursor, message_index.tenant(), sid),
                ).fetchone()
                if row is None:
                    raise ToolError(
                        f"through {cursor!r} is not a message_id in the {mailbox!r} mailbox; "
                        "ULID cursors are retired, so pass a seq (an integer next_seq from "
                        "after_seq paging) or a message_id"
                    )
                if row["event_seq"] is None:
                    raise ToolError(
                        f"message {cursor!r} has no event seq (its event was pruned); "
                        "pass an integer seq, or ack it with message_id"
                    )
                through_seq = int(row["event_seq"])
            acked = message_index.ack(
                conn,
                mailbox,
                message_id=mid,
                through_seq=through_seq,
                session_id=sid,
                include_pruned=include_pruned and mid is None,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return {
            "acked": acked,
            "mailbox": mailbox,
            "message_id": mid,
            "through": cursor,
            "through_seq": through_seq,
            "session_id": sid,
            "unread": message_index.unread_count(mailbox, conn, sid),
        }
    finally:
        conn.close()


def _enforce_utf8_byte_limit(text: str, max_bytes: int) -> str:
    """Re-check UTF-8 size after redaction; truncate so the body still fits."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    raise ToolError(f"body exceeds {max_bytes} bytes after redaction")


def _validate_label(label: str | None, field: str) -> str:
    name = (label or "").strip()
    if not name:
        raise ToolError(f"{field} is required")
    if not config.is_known_agent_label(name):
        raise ToolError(config.unknown_agent_label_error(field, name))
    return name
