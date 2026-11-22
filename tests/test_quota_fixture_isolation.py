"""Common fixtures isolate advisory telemetry without mocking quota readers."""

from __future__ import annotations

import json
from pathlib import Path

from unified_agent import quota_observe, storage
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RoutedLaunchBoundary, StubProcessSpawner
from unified_agent.tools import delegate, tasks


def _guard_operator_traversal(tmp_path, monkeypatch):
    # A synthetic home stands in for the operator. Never inspect real host logs.
    operator_home = tmp_path / "operator-home"
    operator_sessions = operator_home / ".codex" / "sessions"
    operator_sessions.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: operator_home))
    original_rglob = Path.rglob
    scanned = []

    def guarded_rglob(root, pattern, *args, **kwargs):
        assert root != operator_sessions, "quota collection escaped to operator sessions"
        # Embedding startup may independently walk its model cache. The
        # quota reader traverses JSONL telemetry; only track that traversal.
        if pattern == "*.jsonl":
            scanned.append(root)
        return original_rglob(root, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", guarded_rglob)
    return scanned


def test_routed_dispatch_uses_isolated_empty_quota_root(tmp_path, monkeypatch):
    scanned = _guard_operator_traversal(tmp_path, monkeypatch)
    storage.init_db()
    packed = delegate.delegate(
        task_spec=(
            "### Objective\nFixture quota isolation\n"
            "### Scope\n- tests/\n"
            "### Definition of Done\n- native quota gate uses isolated telemetry\n"
        ),
        from_agent="Desktop_Agent5",
        to_agent="Desktop_Agent2",
        callback="none",
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-09-30T00:00:00Z]")
    result = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=RoutedLaunchBoundary(spawner=StubProcessSpawner()),
    )
    assert result["launched"] is True
    codex = next(
        row for row in quota_observe.build_quota_status()["quotas"] if row["lane"] == "codex"
    )
    assert codex["availability"] == "unknown"
    assert codex["remaining"] == "unknown"
    assert codex["evidence"] is None
    assert scanned and set(scanned) == {tmp_path / "codex-sessions"}


def test_explicit_synthetic_meter_override_still_uses_native_reader(tmp_path, monkeypatch):
    scanned = _guard_operator_traversal(tmp_path, monkeypatch)
    storage.init_db()
    sessions = tmp_path / "explicit-synthetic-sessions"
    sessions.mkdir()
    path = sessions / "meter.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": storage.now(),
                "payload": {
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {"used_percent": 25, "window_minutes": 10080},
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))
    meter = quota_observe.read_codex_rate_limits()
    assert meter["used_percent"] == 25
    assert meter["source_path"] == str(path)
    codex = next(
        row for row in quota_observe.build_quota_status()["quotas"] if row["lane"] == "codex"
    )
    assert codex["availability"] == "available"
    assert codex["used_percent"] == 25
    assert set(scanned) == {sessions}
