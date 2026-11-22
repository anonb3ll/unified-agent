"""task_list time windows and the operator local-day rule (task #1430).

Tasks stay UTC in storage. An operator asking "what happened today" means
their LOCAL day, which in America/Indiana/Knox (CDT, UTC-5) runs from 05:00Z to
05:00Z the next UTC day. A naive same-UTC-date filter drops every evening's work.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.tools.tasks import task_add, task_list, task_update


def _add_at(monkeypatch, stamp: str, title: str) -> int:
    monkeypatch.setattr(storage, "now", lambda: stamp)
    return task_add(title)["id"]


def _ids(res: dict) -> set[int]:
    return {t["id"] for t in res["tasks"]}


def test_a_late_evening_task_falls_inside_its_local_day(monkeypatch):
    # 23:30 CDT on 2026-09-27 is 04:30Z on 2026-09-28: next UTC day.
    evening = _add_at(monkeypatch, "2026-09-28T04:30:00+00:00", "late evening local")
    morning = _add_at(monkeypatch, "2026-09-27T13:00:00+00:00", "morning local")
    next_day = _add_at(monkeypatch, "2026-09-28T05:30:00+00:00", "after local midnight")
    res = task_list(status="all", local_day="2026-09-27", tz="America/Indiana/Knox")
    assert _ids(res) >= {evening, morning}
    assert next_day not in _ids(res)
    assert res["window"] == {
        "local_day": "2026-09-27",
        "tz": "America/Indiana/Knox",
        "start_utc": "2026-09-27T05:00:00+00:00",
        "end_utc": "2026-09-28T05:00:00+00:00",
    }


def test_the_same_task_is_outside_the_naive_utc_day(monkeypatch):
    evening = _add_at(monkeypatch, "2026-09-28T04:30:00+00:00", "late evening local")
    res = task_list(status="all", local_day="2026-09-27", tz="UTC")
    assert evening not in _ids(res)


def test_local_day_matches_work_completed_that_day(monkeypatch):
    tid = _add_at(monkeypatch, "2026-09-20T12:00:00+00:00", "old task")
    # Pin completion inside the local day and a later touch outside it, so only
    # completed_at can put the row in the window.
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, updated_at=? WHERE id=?",
            ("2026-09-27T20:00:00+00:00", "2026-09-29T20:00:00+00:00", tid),
        )
        conn.commit()
    finally:
        conn.close()
    res = task_list(status="all", local_day="2026-09-27", tz="America/Indiana/Knox")
    assert tid in _ids(res)


def test_local_day_defaults_to_operator_tz_env(monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_TZ", "America/Indiana/Knox")
    evening = _add_at(monkeypatch, "2026-09-28T04:30:00+00:00", "late evening local")
    res = task_list(status="all", local_day="2026-09-27")
    assert evening in _ids(res)
    assert res["window"]["tz"] == "America/Indiana/Knox"


def test_local_day_today_resolves_in_the_operator_zone(monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_TZ", "America/Indiana/Knox")
    res = task_list(status="all", local_day="today")
    assert len(res["window"]["local_day"]) == 10


def test_created_since_compares_instants_not_strings(monkeypatch):
    before = _add_at(monkeypatch, "2026-09-27T04:59:59+00:00", "before")
    after = _add_at(monkeypatch, "2026-09-27T05:00:00.250000+00:00", "fractional after")
    res = task_list(status="all", created_since="2026-09-27T00:00:00-05:00")
    assert after in _ids(res)
    assert before not in _ids(res)


def test_updated_since_filters_on_last_update(monkeypatch):
    stale = _add_at(monkeypatch, "2026-09-01T00:00:00+00:00", "stale")
    touched = _add_at(monkeypatch, "2026-09-01T00:00:00+00:00", "touched")
    monkeypatch.setattr(storage, "now", lambda: "2026-09-27T12:00:00+00:00")
    task_update(touched, priority="high")
    res = task_list(status="all", updated_since="2026-09-27T00:00:00+00:00")
    assert touched in _ids(res)
    assert stale not in _ids(res)


def test_a_naive_since_timestamp_is_refused():
    with pytest.raises(ToolError, match="offset"):
        task_list(created_since="2026-09-27T00:00:00")


@pytest.mark.parametrize("day", ["2026-13-01", "yesterday-ish", "2026-09-27T00:00:00+00:00"])
def test_a_malformed_local_day_is_refused(day):
    with pytest.raises(ToolError, match="local_day"):
        task_list(local_day=day, tz="UTC")


def test_an_unknown_timezone_is_refused():
    with pytest.raises(ToolError, match="tz"):
        task_list(local_day="2026-09-27", tz="Mars/Olympus_Mons")


def test_no_window_means_no_window_key(monkeypatch):
    _add_at(monkeypatch, "2026-09-27T12:00:00+00:00", "plain")
    assert "window" not in task_list(status="all")


def test_window_composes_with_the_cursor(monkeypatch):
    for i in range(5):
        _add_at(monkeypatch, f"2026-09-27T1{i}:00:00+00:00", f"in window {i}")
    _add_at(monkeypatch, "2026-09-25T12:00:00+00:00", "outside")
    seen, cursor = [], None
    while True:
        res = task_list(status="all", local_day="2026-09-27", tz="UTC", limit=2, cursor=cursor)
        seen += [t["id"] for t in res["tasks"]]
        if not res["truncated"]:
            break
        cursor = res["next_cursor"]
    assert len(seen) == len(set(seen)) == 5


def test_since_is_exact_below_a_millisecond(monkeypatch):
    # julianday() keeps only milliseconds; these two share one.
    early = _add_at(monkeypatch, "2026-09-27T12:00:00.000100+00:00", "early in the ms")
    late = _add_at(monkeypatch, "2026-09-27T12:00:00.000900+00:00", "late in the ms")
    whole = _add_at(monkeypatch, "2026-09-27T12:00:00+00:00", "whole second")
    res = task_list(status="all", created_since="2026-09-27T12:00:00.000500+00:00")
    assert late in _ids(res)
    assert early not in _ids(res)
    assert whole not in _ids(res)
    at_second = task_list(status="all", created_since="2026-09-27T12:00:00+00:00")
    assert {early, late, whole} <= _ids(at_second)
