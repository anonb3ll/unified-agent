"""task_claim owns claims; task_update may explicitly release one.

A `claimed-by:` line in notes is descriptive text with no authorization
meaning, and the server must never synthesize one. See
docs/task-lifecycle-contract.md §2.
"""

from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import config, storage
from unified_agent.tools.tasks import task_add, task_claim, task_list, task_update


@pytest.fixture
def authed_agent():
    """An authenticated caller — the only case that used to trigger synthesis."""
    token = config.agent_identity_var.set("Desktop_Agent1")
    yield "Desktop_Agent1"
    config.agent_identity_var.reset(token)


def _column(task_id: int) -> str | None:
    conn = storage.connect()
    try:
        return conn.execute("SELECT claimed_by FROM tasks WHERE id=?", (task_id,)).fetchone()[
            "claimed_by"
        ]
    finally:
        conn.close()


def test_task_add_does_not_synthesize_claim(authed_agent):
    res = task_add("fresh task", notes="some note")
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT notes, claimed_by FROM tasks WHERE id=?", (res["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["notes"] == "some note"
    assert "claimed-by" not in row["notes"]
    assert row["claimed_by"] is None


def test_task_update_does_not_synthesize_claim(authed_agent):
    res = task_add("fresh task")
    updated = task_update(res["id"], notes="some note")

    assert updated["notes"] == "some note"
    assert "claimed-by" not in updated["notes"]
    assert _column(res["id"]) is None
    # _row()'s notes fallback finds nothing, so the read path reports unclaimed too.
    assert updated["claimed_by"] is None


def test_task_update_preserves_a_hand_written_claim_line(authed_agent):
    """Callers may still write the line themselves; that is unchanged."""
    res = task_add("fresh task")
    updated = task_update(res["id"], notes="working on it\nclaimed-by: Desktop_Agent1")

    assert updated["notes"] == "working on it\nclaimed-by: Desktop_Agent1"
    # ...but writing it by hand still does not touch the authoritative column.
    assert _column(res["id"]) is None
    assert updated["claimed_by"] is None

    listed = task_list(status="all", fields="compact")["tasks"]
    assert next(row for row in listed if row["id"] == res["id"])["claimed_by"] is None


def test_task_claim_remains_the_writer_of_the_column(authed_agent):
    res = task_add("fresh task", notes="some note")
    claimed = task_claim(res["id"], agent="Desktop_Agent1", timeout_seconds=60)

    assert claimed["claimed_by"] == "Desktop_Agent1"
    assert _column(res["id"]) == "Desktop_Agent1"


def test_task_update_releases_an_expired_claim(authed_agent):
    res = task_add("expired claim")
    task_claim(res["id"], agent="Desktop_Agent1", timeout_seconds=60)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), res["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    released = task_update(res["id"], release_claim=True)

    assert released["claimed_by"] is None
    assert _column(res["id"]) is None
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT claimed_at, lock_expires_at, status FROM tasks WHERE id=?", (res["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["claimed_at"] is None
    assert row["lock_expires_at"] is None
    assert row["status"] == "doing"


def test_task_update_cannot_release_an_active_claim_owned_by_other_agent(authed_agent):
    res = task_add("active claim")
    other_identity = config.agent_identity_var.set("Desktop_Agent4")
    try:
        task_claim(res["id"], agent="Desktop_Agent4", timeout_seconds=60)
    finally:
        config.agent_identity_var.reset(other_identity)

    with pytest.raises(Exception, match="active lease"):
        task_update(res["id"], release_claim=True)


def test_task_update_cannot_complete_an_unreviewed_task(authed_agent):
    res = task_add("review before done", notes="[waiting-for: review]")

    with pytest.raises(Exception, match="waiting for review"):
        task_update(res["id"], status="done")


def test_task_update_allows_explicit_no_merge_completion(authed_agent):
    res = task_add(
        "documented no-merge task",
        notes="[waiting-for: review]\n[review: no-merge @ 2026-08-07]",
    )

    completed = task_update(res["id"], status="done")
    assert completed["status"] == "done"


def test_task_update_validates_the_proposed_final_notes(authed_agent):
    res = task_add("review and complete together", notes="[waiting-for: review]")

    completed = task_update(
        res["id"],
        status="done",
        notes="[review: approved @ 2026-08-07]",
    )
    assert completed["status"] == "done"
