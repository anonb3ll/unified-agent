"""doctor's agent_token_collisions row (task #1657).

``config.agent_tokens`` merges UA_AGENT_TOKENS with the managed agent_tokens
store and fails closed when one token maps to two labels. A client reads its
bearer from its own environment, so a cross-source collision looks configured
there and is refused by the hub. The row names the labels, never a value.

Every token value here is an obviously synthetic placeholder.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from unified_agent import config, managed_secrets, storage
from unified_agent.tools import doctor

SHARED = "synthetic-collided-token-0001"
OTHER = "synthetic-distinct-token-0002"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    storage._connection = None
    storage.init_db()
    yield
    storage._connection = None


# Collisions list labels sorted, so the expected order follows the label text
# (the public export renames these labels, which can flip it).
PAIR = tuple(sorted(("Desktop_Person", "Desktop_Agent2")))
PAIR_TEXT = ", ".join(PAIR)


def _manage(label: str, value: str) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, hash_algo, status, issued_at, "
            "expires_at, issued_by) VALUES (?, ?, 'sha256', 'active', ?, NULL, 'test')",
            (label, hashlib.sha256(value.encode()).hexdigest(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    managed_secrets.set_secret(
        f"agent-token:{label}", value, requested_by="Desktop_Person", consumer="agent_token"
    )


def _row() -> dict:
    return doctor._agent_token_collisions_check()


def test_a_cross_source_collision_names_both_labels_and_no_value(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Person:{SHARED},Desktop_Agent1:{OTHER}")
    _manage("Desktop_Agent2", SHARED)

    assert config.agent_token_collisions() == [
        {
            "labels": list(PAIR),
            "sources": ["UA_AGENT_TOKENS", "managed"],
            "cross_source": True,
        }
    ]
    row = _row()
    assert row["capability"] == "agent_token_collisions"
    assert row["status"] == "MISCONFIGURED"
    assert f"{PAIR_TEXT} share one token" in row["detail"]
    assert "across UA_AGENT_TOKENS and the managed store" in row["detail"]
    assert "Desktop_Agent1" not in row["detail"]
    assert SHARED not in json.dumps(row) and OTHER not in json.dumps(row)


def test_a_collision_inside_the_env_list_is_named_too(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Person:{SHARED},Desktop_Agent2:{SHARED}")
    row = _row()
    assert row["status"] == "MISCONFIGURED"
    assert f"{PAIR_TEXT} share one token (within UA_AGENT_TOKENS)" in (row["detail"])
    assert SHARED not in json.dumps(row)


def test_live_when_every_label_has_its_own_token(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Person:{SHARED}")
    _manage("Desktop_Agent2", OTHER)
    assert config.agent_token_collisions() == []
    assert _row()["status"] == "LIVE"


def test_the_hub_still_fails_closed_on_the_collided_token(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Person:{SHARED}")
    _manage("Desktop_Agent2", SHARED)
    _manage("Desktop_Agent1", OTHER)
    assert config.agent_tokens() == {OTHER: "Desktop_Agent1"}


def test_system_doctor_reports_the_row_without_any_value(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Person:{SHARED}")
    _manage("Desktop_Agent2", SHARED)
    report = doctor.system_doctor(refresh=True)
    rows = [r for r in report["checks"] if r["capability"] == "agent_token_collisions"]
    assert len(rows) == 1 and rows[0]["status"] == "MISCONFIGURED"
    assert SHARED not in json.dumps(report)


def test_a_hub_with_its_own_home_is_probed_there(monkeypatch, tmp_path):
    """The hub unit's UA_HOME differs from this process: the collision read
    runs in a child against the hub's store, as the token read does."""
    hub_home = tmp_path / "hub-home"
    monkeypatch.setenv("UA_HOME", str(hub_home))
    storage._connection = None
    storage.init_db()
    _manage("Desktop_Agent2", SHARED)
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    storage._connection = None
    hub_env = {"UA_HOME": str(hub_home), "UA_AGENT_TOKENS": f"Desktop_Person:{SHARED}"}
    monkeypatch.setattr(doctor, "_service_env", lambda unit: hub_env)

    row = _row()
    assert row["status"] == "MISCONFIGURED", row
    assert f"{PAIR_TEXT} share one token" in row["detail"]
    assert doctor._hub_service_tokens() == {}
