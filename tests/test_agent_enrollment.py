"""`unified-agent agent add|remove|list` (task #2453).

An operator adds an agent with one command: the bearer it prints authenticates
on the next request with no env edit or restart, the seat puts the label in its
office and roster, and remove retires it so the same bearer is refused.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import (
    agent_enrollment,
    agent_lifecycle,
    agent_roster,
    config,
    profiles,
    registry,
    server,
    storage,
)
from unified_agent.agent_cli import run_agent_cli
from unified_agent.tools import doctor, token_lifecycle
from unified_agent.tools.profiles import profile_status
from unified_agent.tools.tasks import task_add, task_claim

NEW = "Local_Worker"
TOKEN_RE = re.compile(r"^  (\S{40,})$", re.M)


@pytest.fixture(autouse=True)
def _hub(monkeypatch, tmp_path):
    chart = tmp_path / "org.yaml"
    chart.write_text(
        "schema_version: 1\n"
        "offices:\n"
        "  - id: founder-ceo\n    name: Operator\n    lead: $operator\n"
        "    members: [$operator]\n"
        "  - id: engineering\n    name: Engineering\n    lead: Desktop_Agent2\n"
        "    members: [Desktop_Agent2]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(chart))
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.delenv("UA_MANAGED_SECRETS", raising=False)
    monkeypatch.delenv("UA_LIFECYCLE_OPERATORS", raising=False)
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    # `add` refuses an unencrypted bearer store unless told otherwise.
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    storage.init_db()
    agent_roster.invalidate()
    registry.reset_throttle()  # a heartbeat is throttled per label across tests
    yield
    agent_roster.invalidate()
    agent_lifecycle.invalidate_identity_cache()


def _cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = run_agent_cli(list(argv))
    return rc, out.getvalue(), err.getvalue()


def _add(label: str = NEW, *extra: str) -> str:
    rc, out, err = _cli("add", label, "--office", "engineering", "--no-wait", *extra)
    assert rc == 0, err
    return TOKEN_RE.search(out).group(1)


def _app():
    async def mcp(request):
        return JSONResponse(profile_status())

    return server.HubAuthASGIMiddleware(Starlette(routes=[Route("/mcp", mcp, methods=["POST"])]))


def _all_rows() -> str:
    conn = storage.connect()
    try:
        tables = [
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        return "\n".join(
            repr(tuple(row)) for t in tables for row in conn.execute(f'SELECT * FROM "{t}"')
        )
    finally:
        conn.close()


def _files_holding(needle: str) -> list[str]:
    """Every file under the hub's data directory whose bytes contain ``needle``."""
    hits = []
    for path in config.data_home().rglob("*"):
        if path.is_file() and needle.encode() in path.read_bytes():
            hits.append(str(path.relative_to(config.data_home())))
    return hits


def _active_tokens(label: str) -> int:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM agent_tokens WHERE agent_label=? AND status='active'", (label,)
        ).fetchone()[0]
    finally:
        conn.close()


def _token_rows() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM agent_tokens").fetchone()[0]
    finally:
        conn.close()


def test_add_authenticates_without_restart_then_remove_refuses_the_same_bearer(monkeypatch):
    # A closed roster that does not list the new label: the seat admits it.
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent2")
    assert not config.is_known_agent_label(NEW)
    token = _add(NEW, "--profile", "steward")
    since = agent_enrollment.list_agents()[-1]["token_issued_at"]
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(_app()) as client:
        res = client.post("/mcp", headers=auth)
        assert res.status_code == 200, res.text
        assert (res.json()["identity"], res.json()["profile"]) == (NEW, "steward")
        seen = agent_enrollment.wait_for_heartbeat(NEW, since, 0)
        assert seen is not None and seen["last_seen_via"] == "hub_request"

        rc, out, err = _cli("remove", NEW, "--reason", "decommissioned")
        assert rc == 0, err
        assert "1 managed token(s)" in out and "seat removed" in out
        assert client.post("/mcp", headers=auth).status_code in (401, 403)
    assert agent_lifecycle.agent_state(NEW)["state"] == "retired"
    assert agent_roster.seat(NEW) is None


def test_the_bearer_is_printed_once_and_stored_nowhere(caplog):
    caplog.set_level(logging.DEBUG)
    rc, out, err = _cli("add", NEW, "--office", "engineering", "--no-wait")
    assert rc == 0
    token = TOKEN_RE.search(out).group(1)
    assert out.count(token) == 1
    assert token not in err and token not in caplog.text
    rows = _all_rows()
    assert token not in rows
    # Only the digest and metadata are recorded, with the issued event.
    assert "security.agent_token.issued" in rows and agent_roster.CHANNEL in rows
    # On disk too: the managed store holds the bearer sealed, never verbatim.
    assert _files_holding(token) == []
    _cli("remove", NEW, "--reason", "done")
    assert token not in _all_rows() and token not in caplog.text
    assert _files_holding(token) == []


def test_add_refuses_a_plaintext_store_without_the_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("UA_MANAGED_SECRET_KEYS")
    rc, _out, err = _cli("add", NEW, "--office", "engineering", "--no-wait")
    assert rc == 1 and "UA_MANAGED_SECRET_KEYS" in err and "--allow-plaintext-at-rest" in err
    assert _token_rows() == 0 and agent_roster.seat(NEW) is None
    # The opt-in stores it, and says what that means.
    token = _add(NEW, "--allow-plaintext-at-rest")
    rc, out, _err = _cli(
        "add", "Local_Other", "--office", "engineering", "--no-wait", "--allow-plaintext-at-rest"
    )
    assert rc == 0 and "stored unencrypted" in out
    assert _files_holding(token) == [f"managed-secrets/agent-token:{NEW}"]


def test_add_and_remove_require_a_lifecycle_operator():
    rc, _out, err = _cli("--as", "Desktop_Agent2", "add", NEW, "--office", "engineering")
    assert rc == 1 and "operator action" in err
    assert agent_roster.seat(NEW) is None and _token_rows() == 0
    _add()
    rc, _out, err = _cli("--as", "Desktop_Agent2", "remove", NEW, "--reason", "x")
    assert rc == 1 and "operator action" in err
    assert agent_lifecycle.agent_state(NEW)["state"] == "active"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("add", NEW, "--office", "nowhere"), "no office"),
        (("add", NEW, "--office", "engineering", "--profile", "chief-of-staff"), "privileged"),
        (("add", NEW, "--office", "engineering", "--profile", "operator"), "unseatable"),
        (("add", "bad label", "--office", "engineering"), "not well formed"),
    ],
)
def test_add_validates_before_issuing(argv, message):
    rc, _out, err = _cli(*argv, "--no-wait")
    assert rc == 1 and message in err
    assert _token_rows() == 0


def test_add_refuses_the_operator_a_duplicate_and_a_retired_label():
    rc, _out, err = _cli("add", config.operator_label(), "--office", "engineering")
    assert rc == 1 and "operator's own identity" in err
    _add()
    rc, _out, err = _cli("add", NEW, "--office", "engineering", "--no-wait")
    assert rc == 1 and "already added" in err
    _cli("remove", NEW, "--reason", "gone")
    rc, _out, err = _cli("add", NEW, "--office", "engineering", "--no-wait")
    assert rc == 1 and "retired" in err


def test_doctor_stays_live_and_list_shows_the_seat(monkeypatch):
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent2")
    token = _add(NEW, "--expires-days", "30")
    with TestClient(_app()) as client:
        assert client.post("/mcp", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert doctor._org_registry_check()["status"] == "LIVE"
    assert doctor._agent_label_drift_check()["status"] == "LIVE"
    rc, out, _err = _cli("list", "--json")
    row = next(r for r in json.loads(out) if r["agent"] == NEW)
    assert row["office"] == "engineering" and row["profile"] == "developer"
    assert row["kind"] == "local" and row["token_source"] == "managed"
    assert row["token_expires_at"] and row["last_seen"] and row["added"] is True
    assert token not in out


def test_cloud_kind_gets_the_cloud_hosted_boundaries():
    rc, out, _err = _cli(
        "add", "cloud_helper", "--office", "engineering", "--kind", "cloud", "--no-wait"
    )
    assert rc == 0 and "OAuth" in out
    assert config.is_cloud_hosted_identity("cloud_helper")
    assert config.resolve_workspace_binding("cloud_helper") is config.WORKSPACE_DENIED
    assert not config.is_cloud_hosted_identity(NEW)


def test_cloud_kind_holds_from_the_first_request_in_another_process(monkeypatch):
    """The hub and `agent add` are separate processes; the hub's roster cache predates the add."""
    label = "cloud_probe"
    # Pin the hub's cache window open, so the result never depends on how fast
    # the second process starts.
    monkeypatch.setattr(agent_roster, "CACHE_TTL_S", 3600.0)
    assert not config.is_cloud_hosted_identity(label)  # the hub reads (and caches) first
    src = str(Path(agent_enrollment.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from unified_agent.agent_cli import run_agent_cli; "
            "sys.exit(run_agent_cli(sys.argv[1:]))",
            *("add", label, "--office", "engineering", "--kind", "cloud", "--no-wait"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    token = TOKEN_RE.search(proc.stdout).group(1)
    assert agent_roster.seat(label) is None  # still the stale pre-add read
    assert config.agent_tokens()[token] == label  # while the bearer already works
    assert config.is_cloud_hosted_identity(label)
    assert config.resolve_workspace_binding(label) is config.WORKSPACE_DENIED


def test_a_failed_seat_write_never_leaves_a_live_bearer_without_a_seat(monkeypatch, caplog):
    locked = sqlite3.OperationalError("database is locked")

    def seat_fails(*_a, **_k):
        raise locked

    monkeypatch.setattr(agent_roster, "record_on_connection", seat_fails)
    monkeypatch.setattr(agent_enrollment.time, "sleep", lambda _s: None)
    real_revoke = token_lifecycle.revoke_for_agent
    attempts = []

    def revoke_contended_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise locked
        return real_revoke(*args, **kwargs)

    # The revoke meets the same contention once, then gets through.
    monkeypatch.setattr(token_lifecycle, "revoke_for_agent", revoke_contended_once)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        agent_enrollment.add_agent(NEW, office="engineering", actor=config.operator_label())
    assert agent_roster.seat(NEW) is None and _active_tokens(NEW) == 0
    assert len(attempts) == 2

    # It never gets through: the error and the log name the live row.
    def revoke_always_locked(*_a, **_k):
        raise locked

    monkeypatch.setattr(token_lifecycle, "revoke_for_agent", revoke_always_locked)
    caplog.set_level(logging.WARNING)
    with pytest.raises(Exception) as raised:
        agent_enrollment.add_agent(
            "Local_Probe", office="engineering", actor=config.operator_label()
        )
    conn = storage.connect()
    try:
        live = conn.execute(
            "SELECT id FROM agent_tokens WHERE agent_label='Local_Probe' AND status='active'"
        ).fetchone()
    finally:
        conn.close()
    assert live is not None
    for text in (str(raised.value), caplog.text):
        assert "Local_Probe" in text and f"#{live['id']}" in text and "still active" in text


def test_a_seat_profile_may_lower_but_never_raise_the_default():
    _add(NEW, "--profile", "research")
    assert profiles.default_for_identity(NEW) == profiles.RESEARCH
    # A forged seat event cannot escalate: the code default stands.
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        agent_roster.record_on_connection(
            conn,
            agent_roster.KIND_ADDED,
            {
                "agent": "Local_Other",
                "office": "engineering",
                "profile": "chief-of-staff",
                "agent_kind": "local",
            },
            actor=config.operator_label(),
        )
        conn.commit()
    finally:
        conn.close()
    assert profiles.default_for_identity("Local_Other") == profiles.DEVELOPER


def test_an_added_fleet_prefixed_label_is_known_on_an_open_roster():
    assert not config.is_known_agent_label("Desktop_Newbie")
    _add("Desktop_Newbie")
    assert config.is_known_agent_label("Desktop_Newbie")


def test_remove_refuses_doing_claims_unless_force_release():
    _add()
    tid = task_add("held", lane="dispatch-ready")["id"]
    token = config.agent_identity_var.set(NEW)
    try:
        task_claim(tid, NEW, expected_lane="dispatch-ready")
    finally:
        config.agent_identity_var.reset(token)
    rc, _out, err = _cli("remove", NEW, "--reason", "reorg")
    assert rc == 1 and f"#{tid}" in err and "--force-release" in err
    assert agent_lifecycle.agent_state(NEW)["state"] == "active"

    rc, out, err = _cli("remove", NEW, "--reason", "reorg", "--force-release")
    assert rc == 0, err
    assert f"Released task #{tid}" in out
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status, claimed_by FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert (row["status"], row["claimed_by"]) == ("open", None)
    assert agent_lifecycle.agent_state(NEW)["state"] == "retired"


def test_remove_of_an_env_listed_agent_warns_and_the_hub_refuses_it(monkeypatch):
    env_token = "env-listed-synthetic-token-0001"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Local_Legacy:{env_token}")
    rc, out, err = _cli("remove", "Local_Legacy", "--reason", "replaced")
    assert rc == 0, err
    assert "UA_AGENT_TOKENS" in out and env_token not in out
    with TestClient(_app()) as client:
        res = client.post("/mcp", headers={"Authorization": f"Bearer {env_token}"})
    assert res.status_code == 403 and res.json()["code"] == "agent_retired"
    rows = json.loads(_cli("list", "--json")[1])
    legacy = next(r for r in rows if r["agent"] == "Local_Legacy")
    assert legacy["token_source"] == "env" and legacy["state"] == "retired"


def test_heartbeat_wait_is_bounded():
    _add()
    ticks = iter(range(100))
    seen = agent_enrollment.wait_for_heartbeat(
        NEW, storage.now(), 5, poll_s=1, sleep=lambda _s: None, clock=lambda: next(ticks)
    )
    assert seen is None
    rc, out, _err = _cli("add", "Local_Second", "--office", "engineering", "--wait", "0")
    assert rc == 0 and "No heartbeat yet" in out


def test_the_cli_never_creates_a_database(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "empty-home"))
    rc, _out, err = _cli("list")
    assert rc == 2 and "hub host" in err
    assert not config.db_path().exists()
