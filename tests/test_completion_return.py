"""Completion return (task #1751): origin, same-transaction outbox, drain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import completion_return, edition, run_records, session_credentials, storage
from unified_agent.tools import tasks

THREAD = "1700000000.000100"
CHANNEL = "C_REQUESTS"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.delenv("UA_PUBLIC_URL", raising=False)
    storage.init_db()
    scheduled: list[int | None] = []
    monkeypatch.setattr(
        completion_return, "schedule_drain", lambda task_id=None: scheduled.append(task_id)
    )
    return scheduled


@pytest.fixture
def slack_on(monkeypatch):
    monkeypatch.setenv("UA_SLACK_REPLY", "1")
    monkeypatch.setenv("UA_SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("UA_SLACK_CHANNEL_IDS", CHANNEL)
    monkeypatch.setenv("UA_SLACK_COORDINATOR_AGENT", "Desktop_Agent5")


class Poster:
    def __init__(self, response: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.response = response or {"ok": True, "ts": "1700000001.000200"}

    def __call__(self, url, data, headers):
        self.calls.append(dict(data))
        return dict(self.response)


def _task_with_origin(title: str = "ship the thing", **kwargs) -> int:
    origin = completion_return.Origin(
        surface="slack", channel=CHANNEL, thread=THREAD, requester="U_REQUESTER"
    )
    with completion_return.creation_origin(origin):
        return tasks.task_add(title=title, **kwargs)["id"]


def _deliveries(task_id: int) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM completion_deliveries WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_task_add_records_the_gateway_origin_with_the_task(hub):
    task_id = _task_with_origin()
    assert completion_return.get_origin(task_id) == {
        "task_id": task_id,
        "surface": "slack",
        "channel": CHANNEL,
        "thread": THREAD,
        "requester": "U_REQUESTER",
        "created_at": completion_return.get_origin(task_id)["created_at"],
    }
    # Outside the block no origin is attached.
    other = tasks.task_add(title="plain")["id"]
    assert completion_return.get_origin(other) is None


def test_terminal_transition_queues_one_row_in_the_update_transaction(hub):
    task_id = _task_with_origin()
    updated = tasks.task_update(task_id, status="done", actor="tester")
    rows = _deliveries(task_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert rows[0]["revision"] == updated["updated_at"]
    assert rows[0]["delivered_at"] is None and rows[0]["attempts"] == 0
    assert rows[0]["org_id"] is None
    assert hub == [task_id]  # drained after commit
    # Re-asserting done is not a transition: no second row.
    tasks.task_update(task_id, status="done", append_note="more", actor="tester")
    assert len(_deliveries(task_id)) == 1


def test_rolled_back_task_update_leaves_no_outbox_row(hub, monkeypatch):
    task_id = _task_with_origin()

    def boom(conn, task_ids, reason):
        raise RuntimeError("late failure inside the task_update transaction")

    monkeypatch.setattr(session_credentials, "revoke_for_tasks", boom)
    with pytest.raises(RuntimeError):
        tasks.task_update(task_id, status="done", actor="tester")
    assert _deliveries(task_id) == []
    assert tasks.task_list(status="all")["tasks"][0]["status"] == "open"
    assert hub == []


def test_task_without_origin_gets_no_row_and_no_send(hub, slack_on):
    task_id = tasks.task_add(title="no origin")["id"]
    tasks.task_update(task_id, status="done", actor="tester")
    assert _deliveries(task_id) == []
    assert hub == []
    poster = Poster()
    assert completion_return.drain_pending(slack_post_fn=poster)["delivered"] == []
    assert poster.calls == []


def test_drain_posts_in_thread_once_and_replay_is_idempotent(hub, slack_on, monkeypatch):
    monkeypatch.setenv("UA_PUBLIC_URL", "https://hub.example.test")
    task_id = _task_with_origin(notes="private note SECRETNOTE")
    tasks.task_update(
        task_id,
        status="done",
        actor="tester",
        evidence_ref="https://github.com/example/repo/pull/7",
    )
    poster = Poster()
    first = completion_return.drain_pending(slack_post_fn=poster)
    assert first["delivered"] == [_deliveries(task_id)[0]["id"]]
    assert len(poster.calls) == 1
    sent = poster.calls[0]
    assert sent["channel"] == CHANNEL
    assert sent["thread_ts"] == THREAD
    assert f"task-{task_id} is done" in sent["text"]
    assert "`ship the thing`" in sent["text"]
    assert "evidence: https://github.com/example/repo/pull/7" in sent["text"]
    assert f"https://hub.example.test/ui/runs/insights?task={task_id}" in sent["text"]
    assert "SECRETNOTE" not in sent["text"]
    assert _deliveries(task_id)[0]["delivered_at"] is not None

    # A delivered row is never selected again.
    assert completion_return.drain_pending(slack_post_fn=poster)["delivered"] == []
    # Even if the delivered mark were lost after the post, the Slack dedupe
    # key turns the retry into a recorded duplicate, not a second message.
    conn = storage.connect()
    conn.execute("UPDATE completion_deliveries SET delivered_at=NULL WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()
    replay = completion_return.drain_pending(slack_post_fn=poster)
    assert len(replay["delivered"]) == 1
    assert len(poster.calls) == 1


def test_reopen_supersedes_a_pending_delivery(hub, slack_on):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="done", actor="tester")
    tasks.task_update(task_id, status="open")
    (row,) = _deliveries(task_id)
    assert row["superseded_at"] is not None and row["delivered_at"] is None
    poster = Poster()
    completion_return.drain_pending(slack_post_fn=poster)
    assert poster.calls == []


def test_status_changed_by_another_writer_is_not_sent(hub, slack_on):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="blocked")
    conn = storage.connect()  # a writer other than task_update
    conn.execute("UPDATE tasks SET status='open' WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    poster = Poster()
    result = completion_return.drain_pending(slack_post_fn=poster)
    assert poster.calls == [] and len(result["superseded"]) == 1


def test_pending_rows_coalesce_into_one_send_of_the_latest_status(hub, slack_on):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="blocked")
    tasks.task_update(task_id, status="done", actor="tester")
    blocked, done = _deliveries(task_id)
    poster = Poster()
    result = completion_return.drain_pending(slack_post_fn=poster)
    assert len(poster.calls) == 1
    assert f"task-{task_id} is done" in poster.calls[0]["text"]
    assert result["delivered"] == [done["id"]]
    assert result["superseded"] == [blocked["id"]]
    blocked, done = _deliveries(task_id)
    assert blocked["superseded_at"] and blocked["delivered_at"] is None
    assert "coalesced" in blocked["last_error"]
    assert done["delivered_at"] is not None


def test_title_cannot_ping_or_link_and_is_redacted():
    secret = "sk-" + "A1b2" * 5
    title = (
        f"@channel <!here> <@U123> [click](https://evil.example) "
        f"<https://evil.example|x> key {secret} `tick`\nsecond line"
    )
    slack = completion_return.neutralise_title(title, surface="slack")
    assert slack.startswith("`") and slack.endswith("`") and slack.count("`") == 2
    assert "<" not in slack and ">" not in slack and "|x&gt;" in slack
    assert "@channel" not in slack and "@​channel" in slack
    assert "https://" not in slack and "https:​//" in slack
    assert secret not in slack
    assert "\n" not in slack
    zulip = completion_return.neutralise_title(
        "@**all** [x](https://evil.example)", surface="zulip"
    )
    assert "@**all**" not in zulip and "https://" not in zulip
    assert zulip.startswith("`") and zulip.count("`") == 2
    long = completion_return.neutralise_title("x" * 500, surface="slack")
    assert len(long) <= completion_return.TITLE_MAX_CHARS + 2


def test_evidence_is_only_an_https_url_or_a_sha(monkeypatch):
    monkeypatch.delenv("UA_PUBLIC_URL", raising=False)
    render = completion_return.render_message
    base = dict(task_id=1, title="t", status="done", surface="slack")
    assert "evidence: `0123abcd`" in render(evidence_ref="0123abcd", **base)
    for bad in (
        "see notes",
        "https://x.test/<!here>",
        "[a](https://x.test)",
        "https://a b",
        "http://github.com/example/repo/pull/7",
        "ftp://github.com/x",
    ):
        assert "evidence" not in render(evidence_ref=bad, **base)


def test_evidence_links_only_allowlisted_hosts(monkeypatch):
    render = completion_return.render_message
    base = dict(task_id=1, title="t", status="done")
    pr = "https://github.com/example/repo/pull/7"
    # An allowlisted host is a bare URL, which both surfaces render as a link.
    assert f"evidence: {pr} |" in render(evidence_ref=pr, surface="slack", **base) + " |"
    assert f"evidence: {pr}" in render(evidence_ref=pr, surface="zulip", **base)
    # Any other https host is shown, but inside a code span: not a link.
    other = "https://elsewhere.example/build/42"
    assert f"evidence: `{other}`" in render(evidence_ref=other, surface="slack", **base)
    assert f"evidence: `{other}`" in render(evidence_ref=other, surface="zulip", **base)
    # A look-alike of an allowlisted host is not on the allowlist.
    lookalike = "https://github.com.evil.example/x"
    assert f"evidence: `{lookalike}`" in render(evidence_ref=lookalike, surface="zulip", **base)
    # The Control Center host joins the allowlist when UA_PUBLIC_URL is set.
    hub_url = "https://hub.example.test/runs/9"
    assert f"evidence: `{hub_url}`" in render(evidence_ref=hub_url, surface="zulip", **base)
    monkeypatch.setenv("UA_PUBLIC_URL", "https://hub.example.test")
    assert f"evidence: {hub_url} |" in render(evidence_ref=hub_url, surface="zulip", **base)


def test_title_drops_bidi_and_format_characters():
    title = "safe\u202eexe.txt\u200d \u2066x\u2069 \ufeffend"
    for surface in ("slack", "zulip"):
        out = completion_return.neutralise_title(title, surface=surface)
        assert not any(c in out for c in "\u202e\u200d\u2066\u2069\ufeff")
        assert out == "`safeexe.txt x end`"


def test_slack_post_carries_the_neutralised_title(hub, slack_on):
    task_id = _task_with_origin(title="<!channel> deploy @here")
    tasks.task_update(task_id, status="cancelled")
    poster = Poster()
    completion_return.drain_pending(slack_post_fn=poster)
    text = poster.calls[0]["text"]
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text
    assert "@here" not in text
    assert f"task-{task_id} is cancelled" in text


def test_tenant_fence(hub, slack_on, monkeypatch):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
    task_id = _task_with_origin()

    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_b")
    with pytest.raises(ToolError, match="no task"):
        completion_return.record_origin(task_id, "slack", CHANNEL, THREAD, "U_OTHER")
    assert completion_return.get_origin(task_id) is None

    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
    tasks.task_update(task_id, status="done", actor="tester")
    (row,) = _deliveries(task_id)
    assert row["org_id"] == "org_a"

    poster = Poster()
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_b")
    assert completion_return.drain_pending(slack_post_fn=poster)["delivered"] == []
    assert poster.calls == []
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
    assert completion_return.drain_pending(slack_post_fn=poster)["delivered"] == [row["id"]]
    assert len(poster.calls) == 1


def test_failed_delivery_is_retried_then_abandoned(hub, slack_on):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="done", actor="tester")
    poster = Poster({"ok": False, "error": "channel_not_found"})
    now = datetime.now(UTC)
    first = completion_return.drain_pending(slack_post_fn=poster, now=now)
    (row,) = _deliveries(task_id)
    assert first["failed"] == [row["id"]]
    assert row["attempts"] == 1 and "channel_not_found" in row["last_error"]
    assert row["abandoned_at"] is None and row["next_attempt_at"] is not None

    # Not retried before the backoff elapses: the row is not even selected.
    waiting = completion_return.drain_pending(slack_post_fn=poster, now=now)
    assert waiting["failed"] == [] and waiting["waiting"] == [] and len(poster.calls) == 1

    for _ in range(completion_return.MAX_ATTEMPTS - 1):
        now += timedelta(hours=2)
        completion_return.drain_pending(slack_post_fn=poster, now=now)
    (row,) = _deliveries(task_id)
    assert row["attempts"] == completion_return.MAX_ATTEMPTS
    assert row["abandoned_at"] is not None and row["delivered_at"] is None
    assert len(poster.calls) == completion_return.MAX_ATTEMPTS

    now += timedelta(hours=2)
    completion_return.drain_pending(slack_post_fn=poster, now=now)
    assert len(poster.calls) == completion_return.MAX_ATTEMPTS


def test_unconfigured_slack_keeps_rows_pending_and_never_raises(hub, monkeypatch):
    monkeypatch.delenv("UA_SLACK_REPLY", raising=False)
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="done", actor="tester")
    poster = Poster()
    result = completion_return.drain_pending(slack_post_fn=poster)
    assert "error" not in result and poster.calls == []
    (row,) = _deliveries(task_id)
    assert row["delivered_at"] is None and row["attempts"] == 0 and row["abandoned_at"] is None
    assert completion_return.deliverable_surfaces() == frozenset()


def test_record_origin_validates_and_keeps_the_first(hub):
    task_id = tasks.task_add(title="linked later")["id"]
    first = completion_return.record_origin(task_id, "messages", "conv-1", "conv-1", "Desktop_X")
    assert first == {"task_id": task_id, "surface": "messages", "recorded": True}
    again = completion_return.record_origin(task_id, "slack", CHANNEL, THREAD, "U_OTHER")
    assert again["recorded"] is False
    assert completion_return.get_origin(task_id)["surface"] == "messages"
    with pytest.raises(ToolError):
        completion_return.record_origin(task_id, "email", "c", "t", "r")
    with pytest.raises(ToolError):
        completion_return.record_origin(task_id, "slack", "c\nX", "t", "r")


def test_deleting_a_task_removes_its_origin_and_deliveries(hub):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="cancelled")
    tasks.task_delete(task_id)
    assert completion_return.get_origin(task_id) is None
    assert _deliveries(task_id) == []


def test_complete_direct_queues_exactly_one_row(hub):
    """Close-on-merge, the merge orchestrator and the reviewer auto-close all
    complete through run_records.complete_direct, not task_update."""
    task_id = _task_with_origin()
    expected = run_records.capture_direct_completion(task_id)
    assert run_records.complete_direct(
        expected, actor="merge-orchestrator", note="merged", evidence_ref="0123abcd"
    )
    (row,) = _deliveries(task_id)
    task = tasks.task_list(status="all")["tasks"][0]
    assert task["status"] == "done"
    assert row["status"] == "done" and row["revision"] == task["updated_at"]
    assert hub == [task_id]  # drain scheduled after the commit
    # Re-running the queue hook for the same transition is a no-op.
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        assert not run_records.queue_completion_return(
            conn, task_id, old_status="open", new_status="done", revision=row["revision"]
        )
        conn.commit()
    finally:
        conn.close()
    assert len(_deliveries(task_id)) == 1


def test_complete_direct_without_origin_queues_nothing(hub):
    task_id = tasks.task_add(title="no origin")["id"]
    assert run_records.complete_direct(
        run_records.capture_direct_completion(task_id), actor="queue-closure"
    )
    assert _deliveries(task_id) == [] and hub == []


def test_in_tx_transition_recorder_queues_once_like_the_merge_closeout(hub):
    """The merge closeout writes status itself, then calls
    record_transition_in_tx in the same transaction with no revision: the row
    is keyed on the updated_at written there."""
    task_id = _task_with_origin()
    conn = storage.connect()
    tx = run_records.attach(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        stamp = storage.now()
        conn.execute(
            "UPDATE tasks SET status='done', updated_at=?, completed_at=? WHERE id=?",
            (stamp, stamp, task_id),
        )
        run_records.record_transition_in_tx(
            tx, task_id, old_status="open", new_status="done", actor="merger", claimed_by=None
        )
        conn.commit()
    finally:
        conn.close()
    run_records.flush(tx)
    (row,) = _deliveries(task_id)
    assert row["revision"] == stamp and row["status"] == "done"
    assert hub == [task_id]


def test_task_update_queues_exactly_one_row_through_the_shared_hook(hub):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="blocked")
    tasks.task_update(task_id, status="done", actor="tester")
    rows = _deliveries(task_id)
    assert [r["status"] for r in rows] == ["blocked", "done"]
    assert len({r["revision"] for r in rows}) == 2
    assert hub == [task_id, task_id]


def test_rows_older_than_a_day_expire_and_are_never_sent(hub, monkeypatch, slack_on):
    monkeypatch.delenv("UA_SLACK_REPLY", raising=False)  # delivery off while it piles up
    task_ids = [_task_with_origin(title=f"t{i}") for i in range(3)]
    for tid in task_ids:
        tasks.task_update(tid, status="done", actor="tester")
    assert completion_return.drain_pending()["expired"] == []  # fresh rows stay pending
    monkeypatch.setenv("UA_SLACK_REPLY", "1")  # enabled two days later
    poster = Poster()
    later = datetime.now(UTC) + timedelta(days=2)
    result = completion_return.drain_pending(slack_post_fn=poster, now=later)
    assert poster.calls == [] and result["delivered"] == []
    rows = [r for tid in task_ids for r in _deliveries(tid)]
    assert sorted(result["expired"]) == sorted(r["id"] for r in rows)
    assert all(r["abandoned_at"] and r["last_error"] == "expired" for r in rows)
    assert all(r["delivered_at"] is None for r in rows)
    # Expired rows stay expired.
    again = completion_return.drain_pending(slack_post_fn=poster, now=later)
    assert again["expired"] == [] and poster.calls == []


def test_rows_in_backoff_do_not_starve_a_new_row(hub, slack_on):
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat(timespec="seconds")
    backlog = [_task_with_origin(title=f"waiting {i}") for i in range(60)]
    conn = storage.connect()
    try:
        for tid in backlog:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
            conn.execute(
                "INSERT INTO completion_deliveries "
                "(task_id, revision, status, created_at, attempts, next_attempt_at) "
                "VALUES (?, ?, 'done', ?, 1, ?)",
                (tid, storage.now(), storage.now(), future),
            )
        conn.commit()
    finally:
        conn.close()
    fresh = _task_with_origin(title="new")
    tasks.task_update(fresh, status="done", actor="tester")
    poster = Poster()
    result = completion_return.drain_pending(slack_post_fn=poster)
    assert result["delivered"] == [_deliveries(fresh)[0]["id"]]
    assert len(poster.calls) == 1 and f"task-{fresh} is done" in poster.calls[0]["text"]


def test_stuck_slack_slot_is_eventually_abandoned(hub, slack_on, monkeypatch):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="done", actor="tester")
    outcomes = iter(
        [completion_return.SendOutcome("deferred", "slack rate-limited")]
        + [completion_return.SendOutcome("deferred", "slack send in flight", stuck=True)] * 50
    )
    monkeypatch.setattr(completion_return, "_send_slack", lambda *a, **k: next(outcomes))
    now = datetime.now(UTC)
    results = []
    for _ in range(completion_return.MAX_STUCK_DEFERRALS + 3):
        results.append(completion_return.drain_pending(now=now))
        now += timedelta(seconds=completion_return.DEFERRED_RETRY_S + 1)
    (row,) = _deliveries(task_id)
    # A rate limit is not a stuck slot and is not counted.
    assert row["deferrals"] == completion_return.MAX_STUCK_DEFERRALS
    assert row["abandoned_at"] is not None and row["last_error"] == "slack slot stuck"
    assert row["delivered_at"] is None and row["attempts"] == 0
    abandoned_at = [i for i, r in enumerate(results) if r["abandoned"]]
    assert abandoned_at == [completion_return.MAX_STUCK_DEFERRALS]
    assert all(not r["abandoned"] and not r["waiting"] for r in results[abandoned_at[0] + 1 :])


def test_reopen_between_claim_and_send_is_not_sent(hub, slack_on, monkeypatch):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="done", actor="tester")
    claim_latest = completion_return._claim_latest

    def claim_then_reopen(*args, **kwargs):
        claim = claim_latest(*args, **kwargs)
        tasks.task_update(task_id, status="open")
        return claim

    monkeypatch.setattr(completion_return, "_claim_latest", claim_then_reopen)
    poster = Poster()
    result = completion_return.drain_pending(slack_post_fn=poster)
    (row,) = _deliveries(task_id)
    assert poster.calls == [] and result["delivered"] == []
    assert result["superseded"] == [row["id"]]
    assert row["superseded_at"] is not None and row["delivered_at"] is None


def test_reopen_during_the_send_is_not_marked_delivered(hub, slack_on):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="done", actor="tester")

    class ReopeningPoster(Poster):
        def __call__(self, url, data, headers):
            tasks.task_update(task_id, status="open")
            return super().__call__(url, data, headers)

    poster = ReopeningPoster()
    result = completion_return.drain_pending(slack_post_fn=poster)
    (row,) = _deliveries(task_id)
    assert len(poster.calls) == 1
    assert result["delivered"] == [] and result["superseded"] == [row["id"]]
    assert row["superseded_at"] is not None and row["delivered_at"] is None


def test_a_zero_width_character_cannot_smuggle_a_token_past_redaction():
    token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    split = token[:6] + "\u200b" + token[6:]
    out = completion_return.neutralise_title(f"leak {split}", surface="slack")
    assert token not in out and token[6:] not in out


def test_expiry_leaves_a_freshly_claimed_row_to_its_drain(hub, monkeypatch, slack_on):
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="done", actor="tester")
    later = datetime.now(UTC) + timedelta(days=2)
    row = _deliveries(task_id)[0]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE completion_deliveries SET claimed_at=? WHERE id=?",
            (later.isoformat(timespec="seconds"), row["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    result = completion_return.drain_pending(slack_post_fn=Poster(), now=later)
    assert result["expired"] == []
    assert _deliveries(task_id)[0]["abandoned_at"] is None


def test_a_run_recorder_failure_does_not_drop_the_notice(hub, monkeypatch):
    task_id = _task_with_origin()

    def boom(*args, **kwargs):
        raise RuntimeError("recorder down")

    monkeypatch.setattr(run_records, "record_transition_in_tx", boom)
    tasks.task_update(task_id, status="done", actor="tester")
    assert [r["status"] for r in _deliveries(task_id)] == ["done"]


@pytest.mark.parametrize("hidden", ["\u034f", "\ufe0f", "\u3164", "\u2800", "\U000e0001"])
def test_invisible_non_format_characters_cannot_split_a_token(hidden):
    token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    split = token[:6] + hidden + token[6:]
    out = completion_return.neutralise_title(f"leak {split}", surface="slack")
    assert token not in out and token[6:] not in out


def test_a_reopen_and_reclose_between_claim_and_send_sends_only_the_new_row(
    hub, slack_on, monkeypatch
):
    # Task #2019: the pre-send check compared only the status, so a reopen and
    # re-close after the claim sent the superseded row and then the new one.
    task_id = _task_with_origin()
    tasks.task_update(task_id, status="done", actor="tester")
    claim_latest = completion_return._claim_latest

    def claim_then_reopen_and_reclose(*args, **kwargs):
        claim = claim_latest(*args, **kwargs)
        tasks.task_update(task_id, status="open")
        tasks.task_update(task_id, status="done", actor="tester")
        return claim

    monkeypatch.setattr(completion_return, "_claim_latest", claim_then_reopen_and_reclose)
    poster = Poster()
    first = completion_return.drain_pending(slack_post_fn=poster)
    old, new = _deliveries(task_id)
    assert poster.calls == [] and first["superseded"] == [old["id"]]
    monkeypatch.setattr(completion_return, "_claim_latest", claim_latest)
    assert completion_return.drain_pending(slack_post_fn=poster)["delivered"] == [new["id"]]
    assert len(poster.calls) == 1


def _pin_storage_clock(monkeypatch, instant: datetime) -> None:
    """Freeze the clock storage.now and storage.next_revision read."""

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(storage, "datetime", _Pinned)


def _move(task_id: int, status: str) -> None:
    """done goes through complete_direct (close-on-merge, the reviewer auto-close),
    the writer that stamped a seconds-precision revision; the rest through task_update."""
    if status == "done":
        expected = run_records.capture_direct_completion(task_id)
        assert run_records.complete_direct(expected, actor="merge-orchestrator", note="merged")
    else:
        tasks.task_update(task_id, status=status, actor="tester")


@pytest.mark.parametrize(
    "path", [("done", "open", "done"), ("done", "blocked", "done"), ("blocked", "done")]
)
def test_transitions_within_one_second_each_get_their_own_row(hub, slack_on, monkeypatch, path):
    """Task #2047: complete_direct keyed its row on a seconds-precision updated_at,
    so a re-close in the same second hit the old (task, revision) key and queued
    nothing, and a close after a microsecond task_update revision sorted as older
    and was coalesced away. Either way the requester never heard "done"."""
    task_id = _task_with_origin()
    # storage.now and storage.next_revision both read this clock, so every
    # transition lands in one instant and next_revision must bump each revision.
    _pin_storage_clock(monkeypatch, datetime.now(UTC))
    for status in path:
        _move(task_id, status)
    rows = _deliveries(task_id)
    assert [r["status"] for r in rows] == [s for s in path if s != "open"]
    revisions = [r["revision"] for r in rows]
    assert revisions == sorted(set(revisions))  # distinct and in transition order
    poster = Poster()
    result = completion_return.drain_pending(slack_post_fn=poster)
    assert result["delivered"] == [rows[-1]["id"]]
    assert len(poster.calls) == 1 and path[-1] in poster.calls[0]["text"]


@pytest.mark.parametrize("ahead", [timedelta(0), timedelta(hours=1)])
def test_next_revision_is_strictly_after_a_current_or_future_revision(monkeypatch, ahead):
    """Task #2047: a previous revision at the clock (same instant) or past it
    (a skewed future stamp) still yields a strictly newer revision."""
    clock = datetime.now(UTC)
    _pin_storage_clock(monkeypatch, clock)
    previous = (clock + ahead).isoformat(timespec="microseconds")
    revision = storage.next_revision(previous)
    assert datetime.fromisoformat(revision) > datetime.fromisoformat(previous)
