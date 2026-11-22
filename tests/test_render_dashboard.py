"""Static dashboard preserves all doctor health states."""

import datetime as dt
import sys
from pathlib import Path

import pytest

from unified_agent import config

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from render_dashboard import build, render_queue  # noqa: E402


def test_dashboard_shows_degraded_doctor_count():
    html = build(
        {
            "health": {
                "summary": {
                    "live": 20,
                    "dormant": 5,
                    "degraded": 1,
                    "misconfigured": 0,
                }
            }
        },
        {"items": []},
        dt.datetime(2026, 9, 23, tzinfo=dt.UTC),
    )

    assert "1 degraded" in html
    assert "color:var(--idle)" in html


@pytest.mark.parametrize("operator_key", ["operator_only", config.LEGACY_OPERATOR_QUEUE_KEY])
def test_queue_dashboard_shows_every_open_lane(operator_key):
    html = render_queue(
        {
            "counts": {
                "desktop_open": 0,
                "dispatch_ready": 1,
                "interactive_only": 1,
                operator_key: 1,
            },
            "dispatch_ready": [{"id": 1, "title": "Dispatch task"}],
            "interactive_only": [{"id": 2, "title": "Interactive task"}],
            operator_key: [{"id": 3, "title": "Operator task"}],
        }
    )
    for value in (
        "Dispatch task",
        "Interactive task",
        "Operator task",
        "dispatch ready",
        "interactive only",
        "Operator only",
    ):
        assert value in html


def test_queue_dashboard_reserves_rows_for_new_lanes_after_25_doing_tasks():
    html = render_queue(
        {
            "counts": {"doing": 25, "interactive_only": 1, "operator_only": 1},
            "doing": [{"id": i, "title": f"Doing {i}"} for i in range(25)],
            "interactive_only": [{"id": 26, "title": "Interactive task"}],
            "operator_only": [{"id": 27, "title": "Operator task"}],
        }
    )
    assert "Interactive task" in html
    assert "Operator task" in html
    assert html.count('<span class="mono">#') == 25


def test_queue_dashboard_prefers_canonical_bucket_without_duplicate_alias_rows():
    html = render_queue(
        {
            "counts": {"operator_only": 0, config.LEGACY_OPERATOR_QUEUE_KEY: 1},
            "operator_only": [],
            config.LEGACY_OPERATOR_QUEUE_KEY: [{"id": 99, "title": "Retired alias row"}],
        }
    )
    assert "Retired alias row" not in html
