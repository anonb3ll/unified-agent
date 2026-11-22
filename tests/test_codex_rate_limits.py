"""Codex local rollout rate_limits reader (task #1081).

Reads real JSONL fixtures — no stubs of the parser path.
"""

from __future__ import annotations

import json
from pathlib import Path

from unified_agent.quota_observe import read_codex_rate_limits


def _write_rollout(path: Path, *, timestamp: str, rate_limits: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {},
            "rate_limits": rate_limits,
        },
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def test_read_codex_rate_limits_returns_latest_meter_from_real_jsonl(tmp_path):
    day = tmp_path / "2026" / "09" / "18"
    older = day / "rollout-older.jsonl"
    newer = day / "rollout-newer.jsonl"
    _write_rollout(
        older,
        timestamp="2026-09-18T08:00:00.000Z",
        rate_limits={
            "limit_id": "codex",
            "plan_type": "plus",
            "primary": {
                "used_percent": 67.0,
                "window_minutes": 300,
                "resets_in_seconds": None,
            },
            "secondary": None,
        },
    )
    _write_rollout(
        newer,
        timestamp="2026-09-18T08:16:37.000Z",
        rate_limits={
            "limit_id": "codex",
            "plan_type": "prolite",
            "primary": {
                "used_percent": 5.0,
                "window_minutes": 10080,
                "resets_in_seconds": None,
            },
            "secondary": {"used_percent": 1.0, "window_minutes": 10080},
        },
    )

    meter = read_codex_rate_limits(sessions_root=tmp_path)

    assert meter is not None
    assert meter["limit_id"] == "codex"
    assert meter["plan_type"] == "prolite"
    assert meter["used_percent"] == 5.0
    assert meter["window_minutes"] == 10080
    assert meter["resets_in_seconds"] is None
    assert meter["observed_at"] == "2026-09-18T08:16:37.000Z"
    assert meter["source_path"] == str(newer)
    assert meter["secondary_used_percent"] == 1.0


def test_read_codex_rate_limits_treats_missing_resets_in_seconds_as_unknown(tmp_path):
    """Live Codex samples use resets_at; DoD requires resets_in_seconds UNKNOWN."""
    path = tmp_path / "2026" / "09" / "17" / "rollout.jsonl"
    _write_rollout(
        path,
        timestamp="2026-09-17T02:04:48.000Z",
        rate_limits={
            "limit_id": "codex",
            "plan_type": "plus",
            "primary": {
                "used_percent": 100.0,
                "window_minutes": 300,
                "resets_at": 1789639829,
            },
        },
    )

    meter = read_codex_rate_limits(sessions_root=tmp_path)

    assert meter is not None
    assert meter["used_percent"] == 100.0
    assert meter["resets_in_seconds"] is None


def test_read_codex_rate_limits_returns_none_when_no_samples(tmp_path):
    empty = tmp_path / "2026" / "09" / "18"
    empty.mkdir(parents=True)
    (empty / "rollout-empty.jsonl").write_text('{"type":"other"}\n', encoding="utf-8")

    assert read_codex_rate_limits(sessions_root=tmp_path) is None


def test_read_codex_rate_limits_skips_malformed_lines(tmp_path):
    path = tmp_path / "2026" / "09" / "18" / "rollout.jsonl"
    path.parent.mkdir(parents=True)
    good = {
        "timestamp": "2026-09-18T09:00:00.000Z",
        "payload": {
            "rate_limits": {
                "limit_id": "codex",
                "plan_type": "prolite",
                "primary": {"used_percent": 12.0, "window_minutes": 10080},
            }
        },
    }
    path.write_text(
        "not-json\n" + json.dumps(good) + "\n",
        encoding="utf-8",
    )

    meter = read_codex_rate_limits(sessions_root=tmp_path)
    assert meter is not None
    assert meter["used_percent"] == 12.0
