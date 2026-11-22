"""Completion return for Control Center handoffs (task #1993).

A conversation handed off to a new task records a ``messages`` origin with the
task (the conversation id and the verified requester). When the task reaches
done, blocked or cancelled, the drain posts one short status into that
conversation from the configured service label (``UA_MESSAGES_REPORT_AGENT``),
with the same message rules as Slack: task id, status, neutralised title and
evidence link. Linking an existing task records no origin.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import completion_return, config, storage
from unified_agent.messaging import handoff
from unified_agent.messaging import index as message_index
from unified_agent.messaging import send as messaging_send
from unified_agent.tools import tasks as task_tools

ME = "Desktop_Agent2"
PEER = "Desktop_Agent1"
REPORTER = "Hub_Completion"  # test fixture service label; see the PR for the roster change


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
    monkeypatch.delenv("UA_PUBLIC_URL", raising=False)
    monkeypatch.delenv("UA_MESSAGES_REPORT_AGENT", raising=False)
    storage.init_db()
    # Drain explicitly in each test, not on the worker pool.
    monkeypatch.setattr(completion_return, "schedule_drain", lambda task_id=None: False)
    token = config.agent_identity_var.set(ME)
    yield
    config.agent_identity_var.reset(token)


@pytest.fixture
def reporter(monkeypatch):
    monkeypatch.setenv("UA_MESSAGES_REPORT_AGENT", REPORTER)
    assert config.is_known_agent_label(REPORTER)


def _conversation() -> str:
    sent = messaging_send.send_message(
        act="request", from_agent=ME, to_agent=PEER, body="please look into this"
    )
    return sent["conversation_id"]


def _thread(cid: str) -> list[dict]:
    return messaging_send.list_messages(conversation_id=cid, limit=50)["messages"]


def _deliveries(task_id: int) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM completion_deliveries WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_a_created_handoff_records_a_messages_origin_with_the_task(hub):
    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Follow up")["task_id"]
    origin = completion_return.get_origin(tid)
    assert origin is not None
    assert (origin["surface"], origin["channel"], origin["thread"], origin["requester"]) == (
        "messages",
        cid,
        cid,
        ME,
    )


def test_linking_an_existing_task_records_no_origin(hub):
    cid = _conversation()
    tid = task_tools.task_add(title="already filed")["id"]
    handoff.hand_off_conversation(cid, task_id=tid)
    assert completion_return.get_origin(tid) is None


def test_a_repeated_handoff_keeps_the_one_origin(hub):
    cid = _conversation()
    first = handoff.hand_off_conversation(cid, title="Same")["task_id"]
    again = handoff.hand_off_conversation(cid, title="Same")["task_id"]
    assert again == first
    assert completion_return.get_origin(first)["requester"] == ME


def test_done_posts_once_into_the_conversation(hub, reporter):
    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Fix <!here> @channel the thing")["task_id"]
    assert "messages" in completion_return.deliverable_surfaces()
    task_tools.task_update(
        tid, status="done", evidence_ref="https://github.com/o/r/pull/1", actor=ME
    )

    first = completion_return.drain_pending()
    assert len(first["delivered"]) == 1
    notices = [m for m in _thread(cid) if m["from_agent"] == REPORTER]
    assert len(notices) == 1
    notice = notices[0]
    assert notice["to_agent"] == ME
    assert notice["act"] == "inform"
    # The notice is linked to its task in the mailbox index (review P3).
    assert notice["task_id"] == tid
    assert notice["body"].startswith(f"task-{tid} is done: `")
    assert "@​channel" in notice["body"]
    assert "evidence: https://github.com/o/r/pull/1" in notice["body"]

    again = completion_return.drain_pending()
    assert again["delivered"] == []
    assert len([m for m in _thread(cid) if m["from_agent"] == REPORTER]) == 1


def test_a_lost_delivered_mark_does_not_post_twice(hub, reporter):
    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Once only")["task_id"]
    task_tools.task_update(tid, status="done", actor=ME)
    assert len(completion_return.drain_pending()["delivered"]) == 1
    conn = storage.connect()
    try:
        conn.execute("UPDATE completion_deliveries SET delivered_at=NULL WHERE task_id=?", (tid,))
        conn.commit()
    finally:
        conn.close()
    # The resend reuses the row's message id, so the hub records a duplicate.
    assert len(completion_return.drain_pending()["delivered"]) == 1
    assert len([m for m in _thread(cid) if m["from_agent"] == REPORTER]) == 1


def test_without_a_report_agent_rows_stay_pending(hub):
    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Wait")["task_id"]
    task_tools.task_update(tid, status="done", actor=ME)
    assert "messages" not in completion_return.deliverable_surfaces()
    assert completion_return.drain_pending()["delivered"] == []
    rows = _deliveries(tid)
    assert len(rows) == 1 and rows[0]["delivered_at"] is None and rows[0]["attempts"] == 0
    assert [m for m in _thread(cid) if m["from_agent"] == REPORTER] == []


def test_a_report_agent_off_the_roster_is_not_a_sender(hub, monkeypatch):
    # Colon-separated, as config.agent_labels() parses it.
    monkeypatch.setenv("UA_AGENT_LABELS", f"{ME}:{PEER}")
    assert config.is_known_agent_label(ME) and config.is_known_agent_label(PEER)
    monkeypatch.setenv("UA_MESSAGES_REPORT_AGENT", REPORTER)
    assert "messages" not in completion_return.deliverable_surfaces()


def test_reporter_handoff_receives_its_own_completion_notice(hub, reporter):
    token = config.agent_identity_var.set(REPORTER)
    try:
        cid = messaging_send.send_message(act="request", to_agent=PEER, body="reporter request")[
            "conversation_id"
        ]
        tid = handoff.hand_off_conversation(cid, title="Reporter follow up")["task_id"]
    finally:
        config.agent_identity_var.reset(token)
    task_tools.task_update(tid, status="done", actor=ME)
    result = completion_return.drain_pending()
    assert result["delivered"] == [_deliveries(tid)[0]["id"]]
    token = config.agent_identity_var.set(REPORTER)
    try:
        notices = [m for m in _thread(cid) if m["act"] == "inform"]
        assert len(notices) == 1
        assert notices[0]["from_agent"] == notices[0]["to_agent"] == REPORTER
    finally:
        config.agent_identity_var.reset(token)


def test_tampered_origin_cannot_post_into_an_unrelated_conversation(hub, reporter):
    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Private follow up")["task_id"]
    token = config.agent_identity_var.set(PEER)
    try:
        other = messaging_send.send_message(
            act="request", to_agent="Desktop_Agent5", body="unrelated conversation"
        )["conversation_id"]
    finally:
        config.agent_identity_var.reset(token)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE task_origins SET channel=?, thread=? WHERE task_id=?", (other, other, tid)
        )
        conn.commit()
    finally:
        conn.close()
    task_tools.task_update(tid, status="done", actor=ME)
    assert completion_return.drain_pending()["delivered"] == []
    token = config.agent_identity_var.set(PEER)
    try:
        assert len(_thread(other)) == 1
    finally:
        config.agent_identity_var.reset(token)


def test_public_send_still_refuses_self_messages(hub, reporter):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="must differ"):
        messaging_send.send_message(act="inform", to_agent=ME, body="self")


@pytest.mark.parametrize("boundary", ["requester", "handoff", "tenant", "membership"])
def test_completion_notice_requires_its_recorded_authority(hub, reporter, boundary, monkeypatch):
    cid = _conversation()
    if boundary in {"handoff", "tenant"}:
        with completion_return.creation_origin(completion_return.Origin("messages", cid, cid, ME)):
            tid = task_tools.task_add(title="Unbound follow up")["id"]
        if boundary == "tenant":
            from unified_agent import events

            # Matching authority in another tenant must not authorize this
            # task. The evidence ledger remains append-only in the test too.
            with monkeypatch.context() as tenant_env:
                tenant_env.setenv("UA_EDITION", "enterprise")
                tenant_env.setenv("WORKOS_ORGANIZATION_ID", "foreign-org")
                events.record_event(
                    handoff.HANDOFF_CHANNEL,
                    payload={"task_id": tid, "conversation_id": cid, "by": ME},
                    originator=ME,
                )
    else:
        tid = handoff.hand_off_conversation(cid, title="Bound follow up")["task_id"]
    task_tools.task_update(tid, status="done", actor=ME)
    conn = storage.connect()
    try:
        if boundary == "requester":
            conn.execute("UPDATE task_origins SET requester=? WHERE task_id=?", (PEER, tid))
        elif boundary == "membership":
            conn.execute("DELETE FROM message_index WHERE conversation_id=?", (cid,))
        conn.commit()
    finally:
        conn.close()
    assert completion_return.drain_pending()["delivered"] == []
    assert [m for m in _thread(cid) if m["from_agent"] == REPORTER] == []


OPERATOR = "Op_Human"  # the human login: never on a closed roster


def test_an_operator_requester_is_skipped_once_not_retried(hub, monkeypatch):
    """The operator login cannot be addressed as hub mail on a closed roster (review P2).

    The notice is recorded as a terminal skip, not a failed send retried until
    it is abandoned, and nothing is posted.
    """
    monkeypatch.setenv("UA_AGENT_LABELS", f"{ME}:{PEER}:{REPORTER}")
    monkeypatch.setenv("UA_OPERATOR_LABEL", OPERATOR)
    monkeypatch.setenv("UA_MESSAGES_REPORT_AGENT", REPORTER)
    assert config.operator_label() == OPERATOR
    assert not config.is_known_agent_label(OPERATOR)
    cid = _conversation()
    token = config.agent_identity_var.set(OPERATOR)
    try:
        tid = handoff.hand_off_for_route(cid, title="operator handoff")["task_id"]
    finally:
        config.agent_identity_var.reset(token)
    assert completion_return.get_origin(tid)["requester"] == OPERATOR
    task_tools.task_update(tid, status="done", actor=ME)

    first = completion_return.drain_pending()
    rows = _deliveries(tid)
    assert first["skipped"] == [rows[0]["id"]]
    assert first["delivered"] == [] and first["failed"] == []
    assert rows[0]["abandoned_at"] is not None
    assert rows[0]["last_error"] == "skipped: operator recipient"
    assert rows[0]["attempts"] == 0
    assert [m for m in _thread(cid) if m["from_agent"] == REPORTER] == []

    again = completion_return.drain_pending()
    assert again["skipped"] == [] and again["failed"] == []
    assert _deliveries(tid)[0]["attempts"] == 0


@pytest.mark.parametrize("squatter", [PEER, REPORTER])
def test_a_public_send_cannot_take_a_completion_notice_id(hub, reporter, squatter):
    """PR #1146 review P2-8: notice ids are sequential, so a public send that took
    ``completion-return-<delivery id>`` first made every notice attempt fail
    until the delivery was abandoned. The prefix is reserved for the outbox."""
    from mcp.server.mcpserver.exceptions import ToolError

    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Squatted follow up")["task_id"]
    task_tools.task_update(tid, status="done", actor=ME)
    [row] = _deliveries(tid)
    reserved = f"{completion_return.MESSAGES_ID_PREFIX}-{row['id']}"
    token = config.agent_identity_var.set(squatter)
    try:
        for message_id in (reserved, reserved.upper(), f"  {reserved}  "):
            with pytest.raises(ToolError, match="reserved"):
                messaging_send.send_message(
                    act="inform", to_agent=ME, body="first!", message_id=message_id
                )
    finally:
        config.agent_identity_var.reset(token)
    assert completion_return.drain_pending()["delivered"] == [row["id"]]
    notices = [m for m in _thread(cid) if m["from_agent"] == REPORTER]
    assert [m["message_id"] for m in notices] == [reserved]


@pytest.mark.parametrize("how", ["reopen", "status-only"])
def test_a_reopen_after_the_presend_check_stops_the_notice(hub, reporter, monkeypatch, how):
    """PR #1146 second review: the task was re-read before the send but not under
    the send's write lock, so a reopen committed in between let an obsolete
    "done" notice post. The locked authorization re-checks it."""
    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Reopened mid-send")["task_id"]
    task_tools.task_update(tid, status="done", actor=ME)
    real = completion_return._superseded_before_send

    def then_reopen(conn, claim, tenant, now):
        stale = real(conn, claim, tenant, now)
        if how == "reopen":
            task_tools.task_update(tid, status="open", actor=ME, worktree=False)
        else:
            raw = storage.connect()
            try:
                raw.execute("UPDATE tasks SET status='open' WHERE id=?", (tid,))
                raw.commit()
            finally:
                raw.close()
        return stale

    monkeypatch.setattr(completion_return, "_superseded_before_send", then_reopen)
    result = completion_return.drain_pending()
    assert result["delivered"] == []
    assert [m for m in _thread(cid) if m["from_agent"] == REPORTER] == []


def test_a_concurrent_handoff_by_another_participant_keeps_the_requester_evidence(
    hub, reporter, monkeypatch
):
    """PR #1146 second review: identical concurrent handoffs shared one event
    dedupe key, so the other participant's event could win and the requester's
    handoff evidence was dropped; every notice then failed authorization."""
    from unified_agent import events

    cid = _conversation()
    real = events.record_event_once
    raced = []

    def peer_first(channel, *args, **kwargs):
        if channel == handoff.HANDOFF_CHANNEL and not raced:
            raced.append(True)
            token = config.agent_identity_var.set(PEER)
            try:
                handoff.hand_off_conversation(cid, title="Raced follow up")
            finally:
                config.agent_identity_var.reset(token)
        return real(channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event_once", peer_first)
    tid = handoff.hand_off_conversation(cid, title="Raced follow up")["task_id"]
    monkeypatch.setattr(events, "record_event_once", real)
    assert raced and completion_return.get_origin(tid)["requester"] == ME
    task_tools.task_update(tid, status="done", actor=ME)
    assert len(completion_return.drain_pending()["delivered"]) == 1
    notices = [m for m in _thread(cid) if m["from_agent"] == REPORTER]
    assert [m["to_agent"] for m in notices] == [ME]


# --- PR #1146 review P3 follow-ups (task #2019) ---------------------------------


@pytest.mark.parametrize(
    "field", ["to_agent", "conversation_id", "task_id", "message_id", "act", "from_agent"]
)
def test_a_duplicate_notice_counts_only_when_it_is_this_notice(hub, reporter, field):
    """A recorded result under the notice id was taken as delivered without
    checking its recipient, conversation or task. A receipt for another message
    cannot become this one, so the row is a terminal skip, not a delivery."""
    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Receipt mismatch")["task_id"]
    task_tools.task_update(tid, status="done", actor=ME)
    [row] = _deliveries(tid)
    mid = f"{completion_return.MESSAGES_ID_PREFIX}-{row['id']}"
    receipt = {
        "accepted": True,
        "duplicate": False,
        "message_id": mid,
        "act": "inform",
        "from_agent": REPORTER,
        "to_agent": ME,
        "conversation_id": cid,
        "task_id": tid,
    }
    # "act": a legacy receipt from before the id prefix was reserved (review #1182).
    # "from_agent": the id is held by another sender, which send_message refused
    # with a plain ToolError, so the row was retried until abandoned (#2060).
    receipt[field] = {
        "from_agent": PEER,
        "to_agent": PEER,
        "conversation_id": "other",
        "task_id": tid + 1,
        "act": "request",
    }.get(field, "completion-return-0")
    conn = storage.connect()
    try:
        storage.record_idempotency_required(conn, mid, "message_send", receipt)
        conn.commit()
    finally:
        conn.close()

    first = completion_return.drain_pending()
    assert first["delivered"] == [] and first["skipped"] == [row["id"]]
    [after] = _deliveries(tid)
    assert after["delivered_at"] is None and after["abandoned_at"] is not None
    assert after["last_error"] == "skipped: notice id holds another message"
    assert after["attempts"] == 0


@pytest.mark.parametrize("boundary", ["requester", "handoff", "membership"])
def test_an_authorization_refusal_is_terminal_and_recorded(hub, reporter, boundary):
    """A refusal by the handoff authority was a failed send, retried until it
    was abandoned. Retrying cannot change it, so it is skipped once, with why."""
    cid = _conversation()
    if boundary == "handoff":
        with completion_return.creation_origin(completion_return.Origin("messages", cid, cid, ME)):
            tid = task_tools.task_add(title="Unbound follow up")["id"]
    else:
        tid = handoff.hand_off_conversation(cid, title="Bound follow up")["task_id"]
    task_tools.task_update(tid, status="done", actor=ME)
    conn = storage.connect()
    try:
        if boundary == "requester":
            conn.execute("UPDATE task_origins SET requester=? WHERE task_id=?", (PEER, tid))
        elif boundary == "membership":
            conn.execute("DELETE FROM message_index WHERE conversation_id=?", (cid,))
        conn.commit()
    finally:
        conn.close()

    first = completion_return.drain_pending()
    [row] = _deliveries(tid)
    assert first["skipped"] == [row["id"]] and first["failed"] == []
    assert row["abandoned_at"] is not None and row["attempts"] == 0
    assert row["last_error"] == "skipped: not authorized by its conversation handoff"
    again = completion_return.drain_pending()
    assert again["skipped"] == [] and again["failed"] == []
    assert [m for m in _thread(cid) if m["from_agent"] == REPORTER] == []


def test_a_status_change_under_the_send_lock_is_not_a_failed_attempt(hub, reporter, monkeypatch):
    """A task moved by another writer after the pre-send check is refused under
    the send's lock. That is a stale notice, not an authorization refusal or a
    failure: no attempt is counted, and the next drain supersedes the row."""
    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Moved mid-send")["task_id"]
    task_tools.task_update(tid, status="done", actor=ME)
    real = completion_return._superseded_before_send

    def then_move(conn, claim, tenant, now):
        stale = real(conn, claim, tenant, now)
        raw = storage.connect()
        try:
            raw.execute("UPDATE tasks SET status='open' WHERE id=?", (tid,))
            raw.commit()
        finally:
            raw.close()
        return stale

    monkeypatch.setattr(completion_return, "_superseded_before_send", then_move)
    first = completion_return.drain_pending()
    [row] = _deliveries(tid)
    assert first["delivered"] == [] and first["failed"] == [] and first["skipped"] == []
    assert row["attempts"] == 0 and row["abandoned_at"] is None
    monkeypatch.setattr(completion_return, "_superseded_before_send", real)
    later = datetime.now(UTC) + timedelta(seconds=completion_return.DEFERRED_RETRY_S + 1)
    assert completion_return.drain_pending(now=later)["superseded"] == [row["id"]]
    assert [m for m in _thread(cid) if m["from_agent"] == REPORTER] == []


# --- PR #1182 review P3 follow-ups (task #2060) ---------------------------------


def test_a_notice_id_held_by_an_indexed_message_is_a_terminal_skip(hub, reporter):
    """An indexed message with no receipt (backfilled history) holding the notice
    id can never become the notice: skipped once, not retried until abandoned."""
    cid = _conversation()
    tid = handoff.hand_off_conversation(cid, title="Indexed id clash")["task_id"]
    task_tools.task_update(tid, status="done", actor=ME)
    [row] = _deliveries(tid)
    mid = f"{completion_return.MESSAGES_ID_PREFIX}-{row['id']}"
    conn = storage.connect()
    try:
        message_index.insert_on_connection(
            conn,
            message_id=mid,
            recipient=ME,
            sender=PEER,
            act="inform",
            conversation_id="elsewhere",
            reply_to=None,
            expects_reply=False,
            task_id=None,
            delegation_id=None,
            event_ulid="backfilled-2060",
            created_at="2026-01-01T00:00:00+00:00",
            event_seq=0,
        )
        conn.commit()
    finally:
        conn.close()

    first = completion_return.drain_pending()
    assert first["skipped"] == [row["id"]] and first["failed"] == []
    [after] = _deliveries(tid)
    assert after["abandoned_at"] is not None and after["attempts"] == 0
    assert after["last_error"] == "skipped: notice id holds another message"
    assert [m for m in _thread(cid) if m["from_agent"] == REPORTER] == []


def test_an_ordinary_send_on_a_taken_id_stays_a_plain_tool_error(hub):
    """The terminal refusal is for completion notices only; an agent reusing an
    id keeps the ordinary error, not the outbox's skip signal."""
    _conversation_id = messaging_send.send_message(
        act="request", from_agent=ME, to_agent=PEER, body="first", message_id="taken-2060"
    )["conversation_id"]
    assert _conversation_id
    token = config.agent_identity_var.set(PEER)
    try:
        with pytest.raises(ToolError, match="already used by another message") as exc:
            messaging_send.send_message(
                act="request", from_agent=PEER, to_agent=ME, body="again", message_id="taken-2060"
            )
    finally:
        config.agent_identity_var.reset(token)
    assert not isinstance(exc.value, messaging_send.CompletionNoticeRefused)


def test_an_ordinary_send_on_an_indexed_id_stays_a_plain_tool_error(hub):
    """The index-only clash (no receipt) keeps the plain error for agents too."""
    conn = storage.connect()
    try:
        message_index.insert_on_connection(
            conn,
            message_id="indexed-2060",
            recipient=PEER,
            sender=ME,
            act="inform",
            conversation_id="backfilled",
            reply_to=None,
            expects_reply=False,
            task_id=None,
            delegation_id=None,
            event_ulid="backfilled-2060-b",
            created_at="2026-01-01T00:00:00+00:00",
            event_seq=0,
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ToolError, match="already used by another message") as exc:
        messaging_send.send_message(
            act="request", from_agent=ME, to_agent=PEER, body="again", message_id="indexed-2060"
        )
    assert not isinstance(exc.value, messaging_send.CompletionNoticeRefused)
