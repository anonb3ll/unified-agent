"""Tests for LangGraph Command Center integration (Increment A4 / task #753)."""

from __future__ import annotations

import secrets
import sqlite3

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, server, storage, webui
from unified_agent.langgraph_pilot import default_checkpoint_path
from unified_agent.webui import api as webui_api

TOKEN = f"tok-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(monkeypatch, tmp_path):
    data_home = tmp_path / "ua-home"
    monkeypatch.setenv("UA_HOME", str(data_home))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    config.ensure_dirs()
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def test_get_langgraph_checkpoints_empty(client):
    res = client.get("/api/langgraph/checkpoints")
    assert res.status_code == 200
    data = res.json()
    assert data["exists"] is False
    assert data["checkpoints"] == []
    assert data["count"] == 0
    assert "checkpoint_db" in data


def test_get_langgraph_checkpoints_populated_and_filtered(client):
    ckpt_path = default_checkpoint_path()
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(ckpt_path)
    try:
        conn.execute(
            """
            CREATE TABLE checkpoints (
                thread_id TEXT,
                checkpoint_ns TEXT,
                checkpoint_id TEXT,
                parent_checkpoint_id TEXT,
                type TEXT,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type)
            VALUES
                ('ua-lg:run-1', '', 'ckpt-002', 'ckpt-001', 'checkpoint'),
                ('ua-lg:run-1', '', 'ckpt-001', NULL, 'checkpoint'),
                ('ua-lg:run-2', '', 'ckpt-010', NULL, 'checkpoint')
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Query all
    res = client.get("/api/langgraph/checkpoints")
    assert res.status_code == 200
    data = res.json()
    assert data["exists"] is True
    assert data["count"] == 3
    assert len(data["checkpoints"]) == 3
    assert data["checkpoints"][0]["checkpoint_id"] == "ckpt-010"

    # Query with run_id filter
    res_run = client.get("/api/langgraph/checkpoints?run_id=run-1")
    assert res_run.status_code == 200
    data_run = res_run.json()
    assert data_run["count"] == 2
    for ckpt in data_run["checkpoints"]:
        assert ckpt["thread_id"] == "ua-lg:run-1"

    # Query with limit
    res_limit = client.get("/api/langgraph/checkpoints?limit=1")
    assert res_limit.status_code == 200
    assert len(res_limit.json()["checkpoints"]) == 1


def test_langgraph_checkpoints_read_only(client):
    res = client.post("/api/langgraph/checkpoints", json={})
    assert res.status_code == 405


@pytest.mark.asyncio
async def test_langgraph_checkpoints_payload_direct(monkeypatch, tmp_path):
    data_home = tmp_path / "ua-home-direct"
    monkeypatch.setenv("UA_HOME", str(data_home))
    config.ensure_dirs()
    storage.init_db()

    # Empty store
    payload_empty = await webui_api.langgraph_checkpoints_payload()
    assert payload_empty["exists"] is False
    assert payload_empty["count"] == 0
    assert payload_empty["checkpoints"] == []

    # Seed populated checkpoints in isolated store
    ckpt_path = default_checkpoint_path()
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ckpt_path)
    try:
        conn.execute(
            """
            CREATE TABLE checkpoints (
                thread_id TEXT,
                checkpoint_ns TEXT,
                checkpoint_id TEXT,
                parent_checkpoint_id TEXT,
                type TEXT,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type)
            VALUES ('ua-lg:run-direct-1', '', 'ckpt-100', NULL, 'checkpoint')
            """
        )
        conn.commit()
    finally:
        conn.close()

    payload_populated = await webui_api.langgraph_checkpoints_payload()
    assert payload_populated["exists"] is True
    assert payload_populated["count"] == 1
    assert len(payload_populated["checkpoints"]) == 1
    assert payload_populated["checkpoints"][0]["checkpoint_id"] == "ckpt-100"
    assert payload_populated["checkpoints"][0]["thread_id"] == "ua-lg:run-direct-1"

    # Filtered by run_id
    payload_filtered = await webui_api.langgraph_checkpoints_payload(run_id="run-direct-1")
    assert payload_filtered["count"] == 1
    assert payload_filtered["checkpoints"][0]["checkpoint_id"] == "ckpt-100"

    payload_nomatch = await webui_api.langgraph_checkpoints_payload(run_id="other-run")
    assert payload_nomatch["count"] == 0
    assert payload_nomatch["checkpoints"] == []
