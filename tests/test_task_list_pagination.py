"""Honest pagination for task_list (task #1430, defect 1).

A sweep that hit `limit` used to return a plausible-looking subset with nothing
marking it partial. Every result now says whether it was cut short and, when it
was, carries a keyset cursor that resumes exactly where the page ended.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.tools.tasks import task_add, task_list, task_update


def _seed(n: int, prefix: str = "page") -> list[int]:
    ids = []
    for i in range(n):
        priority = ("high", "med", "low")[i % 3]
        ids.append(task_add(f"{prefix} {i}", priority=priority)["id"])
    return ids


def _walk(**kwargs) -> tuple[list[int], int]:
    seen: list[int] = []
    pages = 0
    cursor = None
    while True:
        res = task_list(cursor=cursor, **kwargs)
        pages += 1
        seen.extend(t["id"] for t in res["tasks"])
        if not res["truncated"]:
            assert res["next_cursor"] is None
            return seen, pages
        assert res["next_cursor"]
        cursor = res["next_cursor"]


def test_a_page_cut_short_by_limit_says_so():
    _seed(5)
    res = task_list(status="open", limit=3)
    assert len(res["tasks"]) == 3
    assert res["truncated"] is True
    assert res["next_cursor"]


def test_a_complete_result_says_so():
    _seed(3)
    res = task_list(status="open", limit=10)
    assert len(res["tasks"]) == 3
    assert res["truncated"] is False
    assert res["next_cursor"] is None


def test_an_exact_fit_is_not_reported_as_truncated():
    _seed(4)
    res = task_list(status="open", limit=4)
    assert len(res["tasks"]) == 4
    assert res["truncated"] is False


@pytest.mark.parametrize("fields", ["full", "compact"])
def test_walking_the_cursor_yields_every_row_once_in_list_order(fields):
    _seed(10)
    everything = [t["id"] for t in task_list(status="open", limit=100, fields=fields)["tasks"]]
    walked, pages = _walk(status="open", limit=3, fields=fields)
    assert walked == everything
    assert pages == 4


def test_cursor_is_keyset_so_an_insert_before_the_page_boundary_does_not_duplicate():
    _seed(6)
    first = task_list(status="open", limit=3)
    # A high-priority insert sorts before everything already paged.
    task_add("late high", priority="high")
    rest, _ = _walk(status="open", limit=3)  # fresh walk sees the new row
    second = task_list(status="open", limit=100, cursor=first["next_cursor"])
    ids_first = [t["id"] for t in first["tasks"]]
    ids_second = [t["id"] for t in second["tasks"]]
    assert not set(ids_first) & set(ids_second)
    assert len(ids_first) + len(ids_second) == 6
    assert len(rest) == 7


def test_cursor_respects_the_other_filters():
    _seed(6, prefix="lane-a")
    for t in task_list(status="open", limit=100)["tasks"][:4]:
        task_update(t["id"], status="blocked")
    walked, _ = _walk(status="blocked", limit=2)
    assert len(walked) == 4
    assert all(t["status"] == "blocked" for t in task_list(status="blocked", limit=100)["tasks"])


def test_limit_clamp_is_reported_as_truncation(monkeypatch):
    _seed(3)
    res = task_list(status="open", limit=0)
    assert len(res["tasks"]) == 1
    assert res["limit"] == 1
    assert res["truncated"] is True


def test_a_malformed_cursor_is_refused():
    with pytest.raises(ToolError, match="cursor"):
        task_list(cursor="not-a-cursor")


def test_a_tampered_cursor_is_refused():
    _seed(3)
    cursor = task_list(status="open", limit=1)["next_cursor"]
    with pytest.raises(ToolError, match="cursor"):
        task_list(cursor=cursor[:-4] + "AAAA")


def test_rows_sharing_priority_and_timestamp_page_by_id(monkeypatch):
    monkeypatch.setattr(storage, "now", lambda: "2026-09-28T00:00:00+00:00")
    ids = [task_add(f"same instant {i}", priority="med")["id"] for i in range(5)]
    walked, _ = _walk(status="open", limit=2)
    assert walked == sorted(ids)


def test_a_cursor_with_stray_characters_is_refused():
    _seed(3)
    cursor = task_list(status="open", limit=1)["next_cursor"]
    with pytest.raises(ToolError, match="cursor"):
        task_list(cursor=cursor + "!")


def test_a_cursor_whose_version_is_a_json_boolean_is_refused():
    import base64
    import json

    raw = json.dumps([True, 1, "2026-09-28T00:00:00+00:00", 1]).encode()
    forged = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(ToolError, match="cursor"):
        task_list(cursor=forged)
