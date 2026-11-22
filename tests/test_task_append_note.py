from __future__ import annotations

from unified_agent import storage
from unified_agent.tools import tasks


def test_task_update_append_note_is_atomic_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    task_id = tasks.task_add(title="coordination", notes="original")["id"]

    updated = tasks.task_update(
        id=task_id,
        status="blocked",
        append_note="[slack-event: Ev-1] waiting on review",
        idempotency_key="slack:Ev-1:task-update",
    )
    assert updated["status"] == "blocked"
    assert updated["notes"] == "original\n[slack-event: Ev-1] waiting on review"

    repeated = tasks.task_update(
        id=task_id,
        status="blocked",
        append_note="[slack-event: Ev-1] waiting on review",
        idempotency_key="slack:Ev-1:task-update",
    )
    assert repeated["notes"] == updated["notes"]


def test_task_update_append_note_deduplicates_marker_without_idempotency_key(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    task_id = tasks.task_add(title="coordination", notes="original")["id"]

    first = tasks.task_update(id=task_id, append_note="[slack-event: Ev-2] waiting")
    repeated = tasks.task_update(id=task_id, append_note="[slack-event: Ev-2] waiting")

    assert repeated["notes"] == first["notes"]


def test_task_update_append_note_substring_of_existing_note_still_appends(tmp_path, monkeypatch):
    """task #1246: the dedup was a raw substring check — ``marker in
    current_notes`` — so any appended text that happened to appear *inside*
    the existing notes was silently discarded while ``updated_at`` still
    advanced. A shared ``[tag]``, a prefix of a longer line, or a repeated
    word are not duplicates; only a verbatim re-delivery of the same note is.
    """
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    task_id = tasks.task_add(title="groomed", notes="[grooming 2026-09-21] first pass complete")[
        "id"
    ]

    # "[grooming]" is a substring of the recorded line but genuinely new content.
    updated = tasks.task_update(id=task_id, append_note="[grooming]")
    assert updated["notes"] == "[grooming 2026-09-21] first pass complete\n[grooming]"

    # A fragment of a longer recorded line is new content, not a duplicate.
    updated = tasks.task_update(id=task_id, append_note="first pass")
    assert updated["notes"].endswith("\nfirst pass")


def test_task_update_append_note_same_tag_different_body_appends(tmp_path, monkeypatch):
    """task #1246: a bracketed prefix is a label, not an idempotency key for
    the whole line — two distinct notes under ``[grooming]`` must both land.
    The lost live notes were exactly this shape.
    """
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    task_id = tasks.task_add(title="groomed")["id"]

    first = tasks.task_update(id=task_id, append_note="[grooming] pass one ok")
    second = tasks.task_update(id=task_id, append_note="[grooming] pass two ok")

    assert second["notes"] == first["notes"] + "\n[grooming] pass two ok"
