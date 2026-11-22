"""MCP tools for typed inter-agent messages (tasks #483, #1859)."""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, untrusted
from ..messaging import grounding
from ..messaging.send import ack_messages, list_messages, send_message


def message_send(
    act: str,
    to_agent: str | None = None,
    body: str | None = None,
    from_agent: str | None = None,
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
    handoff: dict | None = None,
    to_session: str | None = None,
    from_session: str | None = None,
) -> dict:
    """Send a typed inter-agent message (request/inform/query/done/handoff).

    Stored on the event log with a derived per-recipient mailbox index written
    in the same transaction, so the send and its index row commit together.
    Duplicate ``message_id`` values are idempotent. Invalid recipients, hop
    limits, and oversized bodies are rejected. A ``handoff`` act is a speech act
    only; it does not call ``delegate`` or change approvals.

    ``body`` is required (it is optional in the signature only so that ``to_agent``
    can be omitted when ``to_session`` names the recipient). The first three
    positional parameters stay ``act, to_agent, body``. ``to_session`` and
    ``from_session`` stay the trailing parameters, after ``to_office``,
    ``to_role`` and ``handoff``.

    Address the recipient by label (``to_agent``: every session of that agent
    sees the message) or by one live session (``to_session``, a hub
    ``session_id`` from ``agent_context`` or ``session_list``: only that session
    sees it, and ``to_agent`` may be omitted). A session that has ended cannot be
    targeted. Pass your own ``session_id`` as ``from_session`` so the recipient
    can answer you alone: a ``reply_to`` reply to a message that has a
    ``from_session`` goes to that session by default (an explicit ``to_session``
    overrides it).

    Address an office with ``to_office`` or ``to_role`` instead of a label. A
    role is the office id or display name from the org registry (the same alias
    as ``task_list``); the body is not scanned. Both must name the same office
    when both are set, and neither combines with ``to_agent`` or ``to_session``.
    The send writes one label message per other member and refuses the whole
    fan-out when that list is over ``UA_MESSAGE_MAX_RECIPIENTS`` (default 8).
    Each recipient is charged against the hop and pair-streak guards on their
    own chain; if one is refused, nobody is written. The result then carries
    ``office``, ``count`` and ``recipients`` (one ordinary send result each).

    ``reply_to`` must be the ``message_id`` of a message you sent or received;
    the reply joins that message's conversation unless you pass
    ``conversation_id``. ``expects_reply`` marks the message as awaiting an
    answer, which ``message_list(waiting_on_reply=True)`` then tracks.

    ``handoff`` (only with ``act="handoff"``, task #2390) carries typed fields
    beside the body: ``done`` and ``open_questions`` (lists of strings),
    ``next_action``, ``why``, ``reviewed_head_sha``, ``repo`` (``owner/name``,
    the default for evidence) and ``evidence``: up to 15 items
    ``{kind, ref, repo?, expect?, claim?}`` where kind is message (a
    message_id), commit (a SHA), pr (a number), ci_run (a run id), task (an id)
    or doc (a vault path or ``memory:<id>``). The receiver checks them with
    ``coordination_manage(action="handoff_ground", params={"message_id": ...})``.
    A handoff sent without them is accepted with ``handoff_warnings``.
    """
    if body is None:
        raise ToolError("body is required")
    return send_message(
        act=act,
        to_agent=to_agent,
        to_session=to_session,
        body=body,
        from_agent=from_agent,
        from_session=from_session,
        conversation_id=conversation_id,
        reply_to=reply_to,
        message_id=message_id,
        hop=hop,
        task_id=task_id,
        delegation_id=delegation_id,
        event_id=event_id,
        expects_reply=expects_reply,
        to_office=to_office,
        to_role=to_role,
        handoff=handoff,
    )


def message_list(
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
    session_id: str | None = None,
    after_seq: int | None = None,
) -> dict:
    """List typed messages from the per-recipient mailbox index (read-only).

    A verified caller sees only mail it sent or received; the operator login and
    the chief-of-staff profile see all. Oldest first, in commit order. Pass
    ``after_seq`` (exclusive; the previous ``next_seq``) to page forward without
    ever skipping a late-committed message; without a cursor the newest ``limit``
    messages come back. Every row carries its ``seq``. ``since`` (a ULID
    cursor) is retired: it is kept as a parameter only to raise an error telling
    you to use ``after_seq``. ``unread_only`` with no
    ``to_agent``/``from_agent`` is your own inbox, ``count_only`` returns
    ``count`` and ``unread`` with no rows, and ``waiting_on_reply`` lists what
    you sent with ``expects_reply`` that nobody has answered. Each row carries
    ``status`` (delivered, read, answered), ``read_at``, ``acked_at`` and
    ``answered_by``. This tool never changes read state: use the
    ``coordination_manage`` actions ``message_inbox`` and ``message_ack``.

    ``session_id`` reads your mailbox as one of your sessions: label mail plus
    mail addressed to that session, with that session's own read state. Without
    it you see label mail only. The call refreshes the session's ``last_seen_at``.
    """
    listed = list_messages(
        conversation_id=conversation_id,
        to_agent=to_agent,
        from_agent=from_agent,
        act=act,
        limit=limit,
        since=since,
        unread_only=unread_only,
        count_only=count_only,
        waiting_on_reply=waiting_on_reply,
        message_id=message_id,
        session_id=session_id,
        after_seq=after_seq,
    )
    # Another agent's body is data, not instructions (task #1911).
    return untrusted.mark_messages(listed, config.verified_agent_identity())


def message_inbox(
    to_agent: str | None = None,
    conversation_id: str | None = None,
    act: str | None = None,
    limit: int = 50,
    since: str | None = None,
    unread_only: bool = True,
    session_id: str | None = None,
    after_seq: int | None = None,
) -> dict:
    """Read your inbox and mark what comes back as read (``coordination_manage`` action).

    Same filters and result as ``message_list`` for your own mailbox, newest
    window oldest first, but the returned messages addressed to you get
    ``read_at`` set. ``unread_only`` defaults to true. ``after_seq`` (the previous
    ``next_seq``) only moves forward, to mail newer than the window you read: while
    ``unread`` is above zero, older unread mail comes back by calling again without
    a cursor. ``since`` (a ULID) is retired and raises. ``to_agent`` names the
    mailbox only for a caller with no verified identity. Pass ``session_id`` to
    read as one of your sessions: you get label mail plus mail addressed to it,
    and reading label mail marks it read for that session only, so your other
    sessions still see it unread.
    """
    from .. import gateway

    def commit_receipts(conn, marked: int) -> None:
        gateway.commit_registered_call_on_connection(
            conn, "coordination_manage", {"marked_read": marked}
        )

    listed = list_messages(
        to_agent=to_agent,
        conversation_id=conversation_id,
        act=act,
        limit=limit,
        since=since,
        unread_only=unread_only,
        mark_read=True,
        commit_mark_read=commit_receipts,
        session_id=session_id,
        after_seq=after_seq,
    )
    return untrusted.mark_messages(listed, config.verified_agent_identity())


def message_ack(
    message_id: str | None = None,
    through: int | str | None = None,
    to_agent: str | None = None,
    session_id: str | None = None,
    include_pruned: bool = False,
) -> dict:
    """Ack your own mail (``coordination_manage`` action).

    Pass one ``message_id``, or ``through`` to ack everything up to and
    including a cursor: a ``seq`` (integer, e.g. ``next_seq``) or the ``message_id`` of
    a row in your mailbox (ULIDs are not accepted). Rows whose event was pruned
    before the index carried a seq have no position: ``include_pruned=True``
    also acks them. Only mail committed at or before the cursor is
    acked, so a message that landed after you paged is never acked unseen. An ack also marks
    the mail read. Only the recipient can ack. Repeating an ack is a no-op. With
    ``session_id`` the ack is that session's own: label mail stays unacked for
    your other sessions, and mail addressed to the session can be acked.
    """
    return ack_messages(
        message_id=message_id,
        through=through,
        to_agent=to_agent,
        session_id=session_id,
        include_pruned=include_pruned,
    )


def handoff_ground(
    message_id: str,
    session_id: str | None = None,
    to_agent: str | None = None,
) -> dict:
    """Ground a handoff you sent or received against live state (``coordination_manage`` action).

    Report mode (task #2390): it never blocks anything. Each claim in the
    handoff's typed fields (``reviewed_head_sha`` and each ``evidence`` item) is
    checked by one resolver call, at most 15 per handoff, and gets a tier (T0 a
    human message or git, T1 CI or PR state, T2 anything agent-written) and a
    verdict: verified, mismatch or insufficient. A ``next_action`` naming a
    high-risk step (merge, deploy, release, delete, ...) needs at least one
    verified T0 or T1 claim. The overall ``verdict`` is mismatch if any claim
    is, verified only when every claim is and that rule holds, else
    insufficient. An untyped handoff is insufficient: read it as prose. The
    verdict and counts (never the body) are recorded on
    ``agent.message.grounding`` so the mismatch rate can be measured.

    A resolver checks the reference, not the sender's ``claim`` about it (task
    #2485). Each claim reports ``checked`` (the predicate evaluated: that a
    commit or note exists, a PR's, CI run's or task's state, who sent a
    message) and ``reference`` (its result) beside ``verdict``. The report's
    ``authorization`` covers a handoff that says an approval or authorization
    was given (in ``done``, ``why`` or an evidence ``claim`` or ``expect``):
    ``not_claimed`` when it says none, ``not_checked`` when it says one.
    ``authorization_checked`` is always false: no message body is read, so an
    approval is never evaluated here. ``authorization_reference`` is
    ``operator_message`` when the operator's own message is cited: read it
    yourself, it may say anything. While ``authorization`` is ``not_checked``
    a claim worded as that approval is insufficient even if its reference
    exists, the verdict is not verified and ``high_risk_met`` is false. It
    reports only; it grants and gates nothing.
    """
    report = grounding.ground_message(message_id, session_id=session_id, to_agent=to_agent)
    caller = config.verified_agent_identity()
    if report.get("from_agent") and report["from_agent"] != caller:
        report = untrusted.mark_untrusted(report, "agent_message", report["from_agent"])
    return report
