"""Atomic task transition evidence (task #1106)."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from unified_agent import events, storage
from unified_agent.tools import tasks as tasks_tool
from unified_agent.tools.tasks import task_add, task_update


def _task() -> dict:
    return task_add("atomic transition", idempotency_key="add-atomic-task")


def _counts(task_id: int) -> tuple[str, int, int]:
    conn = storage.connect()
    try:
        task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        event = conn.execute(
            "SELECT COUNT(*) AS n FROM event_log WHERE channel='agent.task.updated' "
            "AND CAST(json_extract(payload, '$.task_id') AS TEXT)=?",
            (str(task_id),),
        ).fetchone()
        receipt = conn.execute(
            "SELECT COUNT(*) AS n FROM idempotency_keys WHERE key='transition-1'"
        ).fetchone()
        return task["status"], event["n"], receipt["n"]
    finally:
        conn.close()


def test_transition_event_and_receipt_commit_together(monkeypatch):
    task = _task()
    published = []
    monkeypatch.setattr(events, "publish", published.append)
    published.clear()

    result = task_update(task["id"], status="doing", worktree=False, idempotency_key="transition-1")

    assert result["status"] == "doing"
    assert _counts(task["id"]) == ("doing", 1, 1)
    # The self-pickup run record (task #1436) publishes on its own channel after
    # the task transaction; exactly one *task* transition is published.
    task_published = [e for e in published if e["channel"] == events.CHANNEL_TASK_UPDATED]
    assert len(task_published) == 1
    published_before_replay = len(published)

    conn = storage.connect()
    try:
        event = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ROWID DESC LIMIT 1",
            (events.CHANNEL_TASK_UPDATED,),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload["kind"] == "task"
        assert payload["ref_id"] == str(task["id"])
        assert payload["summary"] == "status open->doing: atomic transition"
        assert events.verify_event_chain(conn)["verified"] is True
    finally:
        conn.close()
    assert published[0]["channel"] == "agent.task.updated"

    replay = task_update(task["id"], status="doing", worktree=False, idempotency_key="transition-1")
    assert replay == result
    assert _counts(task["id"]) == ("doing", 1, 1)
    assert len(published) == published_before_replay


def test_event_failure_rolls_back_task_and_receipt(monkeypatch):
    task = _task()

    def fail_event(*args, **kwargs):
        raise RuntimeError("event ledger unavailable")

    monkeypatch.setattr(events, "record_event_once_on_connection", fail_event)
    with pytest.raises(RuntimeError, match="event ledger unavailable"):
        task_update(task["id"], status="doing", worktree=False, idempotency_key="transition-1")

    assert _counts(task["id"]) == ("open", 0, 0)


def test_event_failure_does_not_attempt_worktree_side_effect(monkeypatch):
    task = _task()
    called = []
    monkeypatch.setattr(
        tasks_tool, "_ensure_task_worktree", lambda *args, **kwargs: called.append(1)
    )
    monkeypatch.setattr(
        events,
        "record_event_once_on_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("event ledger unavailable")),
    )
    with pytest.raises(RuntimeError, match="event ledger unavailable"):
        task_update(task["id"], status="doing", worktree=True, idempotency_key="transition-1")
    assert called == []
    assert _counts(task["id"]) == ("open", 0, 0)


def test_event_dedupe_collision_rolls_back_un_evidenced_mutation(monkeypatch):
    task = _task()
    monkeypatch.setattr(
        events,
        "record_event_once_on_connection",
        lambda *args, **kwargs: ("existing", False, None),
    )
    with pytest.raises(RuntimeError, match="dedupe collision"):
        task_update(task["id"], status="doing", worktree=False, idempotency_key="transition-1")
    assert _counts(task["id"]) == ("open", 0, 0)


def test_receipt_failure_rolls_back_task_and_event(monkeypatch):
    task = _task()

    def fail_receipt(*args, **kwargs):
        raise RuntimeError("receipt unavailable")

    monkeypatch.setattr(storage, "record_idempotency_required", fail_receipt)
    with pytest.raises(RuntimeError, match="receipt unavailable"):
        task_update(task["id"], status="doing", worktree=False, idempotency_key="transition-1")

    assert _counts(task["id"]) == ("open", 0, 0)


def test_post_commit_publish_failure_does_not_undo_transition(monkeypatch):
    task = _task()

    def fail_publish(_payload):
        raise RuntimeError("socket unavailable")

    monkeypatch.setattr(events, "publish", fail_publish)
    result = task_update(task["id"], status="doing", worktree=False, idempotency_key="transition-1")

    assert result["status"] == "doing"
    assert _counts(task["id"]) == ("doing", 1, 1)


def test_idempotent_worktree_receipt_is_pending_and_replays_exactly(monkeypatch):
    task = _task()
    monkeypatch.setattr(
        tasks_tool, "_ensure_task_worktree", lambda *args, **kwargs: ("wt", "branch")
    )

    first = task_update(task["id"], status="doing", idempotency_key="transition-1")
    replay = task_update(task["id"], status="doing", idempotency_key="transition-1")

    assert first == replay
    assert first["worktree"] == "pending"


def test_idempotency_key_collision_with_another_tool_rolls_back():
    task = _task()
    with pytest.raises(ValueError, match="already registered"):
        task_update(task["id"], status="doing", worktree=False, idempotency_key="add-atomic-task")
    assert _counts(task["id"]) == ("open", 0, 0)


def test_tainted_keyed_transitions_in_one_second_keep_distinct_evidence(monkeypatch):
    task = _task()
    monkeypatch.setattr(storage, "now", lambda: "2026-09-18T12:00:00+00:00")

    first = task_update(
        task["id"],
        status="doing",
        untrusted=True,
        worktree=False,
        idempotency_key="tainted-transition-1",
    )
    second = task_update(
        task["id"],
        status="blocked",
        untrusted=True,
        worktree=False,
        idempotency_key="tainted-transition-2",
    )

    assert first["updated_at"] != second["updated_at"]
    assert second["status"] == "blocked"
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? "
            "AND CAST(json_extract(payload, '$.task_id') AS TEXT)=? ORDER BY ROWID",
            (events.CHANNEL_TASK_UPDATED, str(task["id"])),
        ).fetchall()
        receipts = conn.execute(
            "SELECT key FROM idempotency_keys WHERE key IN (?, ?) ORDER BY key",
            ("tainted-transition-1", "tainted-transition-2"),
        ).fetchall()
        assert [json.loads(row["payload"])["status"] for row in rows] == ["doing", "blocked"]
        assert [receipt["key"] for receipt in receipts] == [
            "tainted-transition-1",
            "tainted-transition-2",
        ]
        assert events.verify_event_chain(conn)["verified"] is True
    finally:
        conn.close()


def test_concurrent_same_key_has_one_transition_and_receipt():
    task = _task()
    barrier = Barrier(2)

    def update():
        barrier.wait()
        return task_update(
            task["id"], status="doing", worktree=False, idempotency_key="transition-1"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _item: update(), (1, 2)))

    assert results[0] == results[1]
    assert _counts(task["id"]) == ("doing", 1, 1)
