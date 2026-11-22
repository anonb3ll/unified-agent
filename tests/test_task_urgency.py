"""Structured urgency projection from task note markers (#1444)."""

import pytest

from unified_agent.tools.tasks import task_add, task_list, task_update


@pytest.mark.parametrize("fields", ["full", "compact"])
def test_urgency_is_projected_without_changing_priority_order(fields):
    plain = task_add("plain high", priority="high")["id"]
    now = task_add("now med", notes="[urgency: now]", priority="med")["id"]
    later = task_add("later low", notes="[urgency: later]", priority="low")["id"]
    rows = task_list(status="open", fields=fields)["tasks"]
    by_id = {row["id"]: row for row in rows}
    assert (by_id[now]["urgency"], by_id[now]["urgency_rank"]) == ("now", 0)
    assert (by_id[plain]["urgency"], by_id[plain]["urgency_rank"]) == (None, 1)
    assert (by_id[later]["urgency"], by_id[later]["urgency_rank"]) == ("later", 2)
    assert [row["id"] for row in rows] == [plain, now, later]
    if fields == "compact":
        assert all("notes" not in row for row in rows)


def test_urgency_updates_with_notes_and_uses_most_urgent_marker():
    t = task_add("marked", notes="[urgency: later]")["id"]
    updated = task_update(t, append_note="[urgency: next]")
    assert (updated["urgency"], updated["urgency_rank"]) == ("next", 1)
    updated = task_update(t, append_note="[urgency: now]")
    assert (updated["urgency"], updated["urgency_rank"]) == ("now", 0)
    listed = task_list(status="all", fields="compact")["tasks"][0]
    assert listed["urgency"] == "now"


def test_unrecognized_urgency_marker_is_neutral():
    t = task_add("invalid marker", notes="[urgency: soon]")["id"]
    row = task_list(status="open", fields="full")["tasks"][0]
    assert row["id"] == t
    assert row["urgency"] is None
    assert row["urgency_rank"] == 1


def test_urgency_marker_is_case_insensitive_in_full_and_compact():
    t = task_add("uppercase marker", notes="[URGENCY: NOW]")["id"]
    for fields in ("full", "compact"):
        row = task_list(status="open", fields=fields)["tasks"][0]
        assert row["id"] == t
        assert (row["urgency"], row["urgency_rank"]) == ("now", 0)


@pytest.mark.parametrize("fields", ["full", "compact"])
def test_quoted_urgency_examples_do_not_override_real_markers(fields):
    quoted = task_add("quoted example", notes="Use `[urgency: now]` in the guide")["id"]
    mixed = task_add("mixed", notes="Example `[urgency: now]`; [urgency: next]")["id"]
    one_sided = task_add("one sided", notes="Mention `[urgency: later] only")["id"]
    rows = {row["id"]: row for row in task_list(status="open", fields=fields)["tasks"]}
    assert rows[quoted]["urgency"] is None
    assert rows[mixed]["urgency"] == "next"
    assert rows[one_sided]["urgency"] is None
