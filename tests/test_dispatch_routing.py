"""Named-agent / profile routing adapters (task #549).

Real route resolution, identity, and workspace policy; subprocess spawn stubbed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_agent import monitor, storage
from unified_agent.dispatch import coordinator, harness_sandbox, routing
from unified_agent.dispatch.launch import (
    CliProcessSpawner,
    RoutedLaunchBoundary,
    StubProcessSpawner,
)
from unified_agent.tools import delegate, tasks

SUPPORTED = (
    "Desktop_Agent1",
    "Desktop_Agent2",
    "Desktop_agent3",
    "Desktop_Agent4",
    "Desktop_Agent6",
    "Desktop_Agent5",
    "Desktop_Agent7",
    "Laptop_Agent1",
    "Laptop_Agent2",
    "Laptop_agent3",
    "Laptop_Agent4",
    "Laptop_Agent6",
    "Laptop_Agent5",
    "server_agent1",
    "server_agent2",
    "server_agent3",
    "server_agent4",
    "server_agent5",
)
NATIVE_CLIS = {"claude", "codex", "agy", "cursor", "hermes", "copilot", "devin"}


def _assert_native_headless_flags(cli_backend: str, argv: list[str]) -> None:
    if cli_backend == "claude":
        assert "-p" in argv
        assert "stream-json" in argv
        assert "--verbose" in argv
    elif cli_backend == "agy":
        # Observable form omits -p; Agy's -p consumes the next token as prompt.
        assert "-p" not in argv
        assert "--output-format" in argv
        assert "stream-json" in argv
    elif cli_backend == "cursor":
        assert "-p" in argv
        assert "stream-json" in argv
    elif cli_backend == "codex":
        assert "exec" in argv
        assert "--json" in argv
    elif cli_backend == "hermes":
        assert "--oneshot" in argv
    elif cli_backend == "copilot":
        assert "--output-format" in argv
        assert "json" in argv
        assert "--allow-all-tools" in argv
        assert "-p" not in argv  # prompt stays off the observable form
    elif cli_backend == "devin":
        assert argv[1:3] == ["--permission-mode", "accept-edits"]
    else:
        raise AssertionError(cli_backend)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    # Explicit Laptop bindings so device-scoped identities are launchable in tests.
    laptop_ws = tmp_path / "laptop-ws"
    laptop_ws.mkdir()
    monkeypatch.setenv(
        "UA_AGENT_WORKSPACES",
        ",".join(
            [
                f"Laptop_Agent1:{laptop_ws}",
                f"Laptop_Agent2:{laptop_ws}",
                f"Laptop_agent3:{laptop_ws}",
                f"Laptop_Agent4:{laptop_ws}",
                f"Laptop_Agent6:{laptop_ws}",
                f"Laptop_Agent5:{laptop_ws}",
            ]
        ),
    )
    storage.init_db()
    return tmp_path


def _spec(title: str = "Routing adapter fixture") -> str:
    return (
        f"### Objective\n{title}\n"
        "### Scope\n- src/unified_agent/dispatch/routing.py\n"
        "- src/unified_agent/dispatch/launch.py\n"
        "### Definition of Done\n"
        "- each supported label resolves to CLI route + workspace + identity\n"
        "- unknown/ambiguous/malformed refuse\n"
        "### TDD Constraints\n"
        "- Stub: subprocess / external agent binaries only\n"
        "- Must be real: route resolution, identity, workspace policy\n"
        "### Boundaries\n"
        "- Must not enable broad autonomy or add credentials\n"
    )


def _delegated(*, to_agent: str) -> dict:
    result = delegate.delegate(
        task_spec=_spec(),
        from_agent="Operator",
        to_agent=to_agent,
        callback="none",
    )
    return {
        "delegation_id": result["delegation_id"],
        "task_id": result["task_id"],
    }


def _approve(task_id: int) -> None:
    tasks.task_update(task_id, append_note="[plan: approved @ 2026-08-24T00:00:00Z]")


@pytest.mark.parametrize("label", SUPPORTED)
def test_every_supported_label_resolves(label):
    result = routing.resolve_named_agent(label)
    assert result.ok is True
    assert result.reason == "ok"
    assert result.route is not None
    assert result.route.agent_label == label
    # The CTO seats keep their profile under dispatch (#1884); every other
    # label, chief-of-staff ones included, runs as developer.
    expected = "cto" if label in {"Desktop_Agent5", "server_agent5"} else "developer"
    assert result.route.profile == expected
    assert result.route.cli_backend in NATIVE_CLIS
    assert result.route.binary
    host, segment = label.split("_", 1)
    if host == "server":
        assert result.route.family == routing._SEGMENT_TO_FAMILY_LOWER[segment]
        assert result.route.host == "server"
    else:
        assert result.route.family == routing._SEGMENT_TO_FAMILY[segment]


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("/Desktop_Agent1", "Desktop_Agent1"),
        ("desktop-agent1", "Desktop_Agent1"),
        ("Desktop_Agent2", "Desktop_Agent2"),
        ("/laptop_agent3", "Laptop_agent3"),
        ("Laptop-Agent2", "Laptop_Agent2"),
        ("/Desktop_Agent4", "Desktop_Agent4"),
        ("desktop-agent6", "Desktop_Agent6"),
        ("Laptop-Agent6", "Laptop_Agent6"),
        ("desktop-agent5", "Desktop_Agent5"),
        ("Laptop-Agent4", "Laptop_Agent4"),
    ],
)
def test_interactive_aliases_map_to_canonical(alias, expected):
    result = routing.resolve_named_agent(alias)
    assert result.ok is True
    assert result.route is not None
    assert result.route.agent_label == expected


@pytest.mark.parametrize(
    "stem",
    [
        "/claude",
        "claude",
        "/codex",
        "agy",
        "/agy",
        "/cursor",
        "hermes",
        "/hermes",
        "copilot",
        "/copilot",
    ],
)
def test_bare_family_alias_is_ambiguous(stem):
    result = routing.resolve_named_agent(stem)
    assert result.ok is False
    assert result.reason == "ambiguous_label"
    assert len(result.candidates) >= 2


def test_unknown_label_fails_closed():
    result = routing.resolve_named_agent("Desktop_Nobody")
    assert result.ok is False
    assert result.reason == "unknown_label"


def test_malformed_route_fails_closed():
    assert routing.resolve_named_agent("").reason == "malformed_route"
    assert routing.resolve_named_agent("   ").reason == "malformed_route"
    assert routing.resolve_named_agent("/").reason == "malformed_route"


def test_known_non_dispatchable_roster_label(monkeypatch):
    monkeypatch.setenv("UA_AGENT_LABELS", "Operator:Desktop_Agent1")
    result = routing.resolve_named_agent("Operator")
    assert result.ok is False
    assert result.reason == "not_dispatchable"


def test_route_inventory_is_secret_free_and_reports_preflight(db, monkeypatch):
    monkeypatch.setattr(routing.shutil, "which", lambda binary: f"/bin/{binary}")
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "0")
    rows = routing.route_inventory()
    row = next(item for item in rows if item["agent_label"] == "Desktop_Agent1")

    assert row["status"] == "ready"
    assert row["backend"] == "claude"
    assert row["binary_available"] is True
    assert row["workspace"]["ok"] is True
    assert row["approval_boundary"] == "governed_dispatch"
    assert row["click_spawn"]["enabled"] is True
    assert row["scheduler_spawn"]["enabled"] is False
    assert "binary_env" not in row
    assert "UA_AGENT_BEARER_TOKEN" not in repr(row)


def test_route_inventory_marks_retired_scheduler_even_with_stale_gate(db, monkeypatch):
    monkeypatch.setattr(monitor, "_service_probe", lambda *_: {"LoadState": "not-found"})
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    rows = routing.route_inventory()
    assert rows
    assert all(row["scheduler_spawn"]["enabled"] is False for row in rows)
    assert all(row["scheduler_spawn"]["mode"] == "retired" for row in rows)
    assert all("retired" in row["scheduler_spawn"]["next_action"] for row in rows)


def test_disabled_launchd_override_retires_unloaded_scheduler(db, monkeypatch):
    monkeypatch.setattr(routing.sys, "platform", "darwin")
    monkeypatch.setattr(monitor.os, "getuid", lambda: 501)
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")

    def fake_run(cmd, capture_output, text, timeout):
        assert capture_output is True and text is True
        assert timeout == monitor.SERVICE_CHECK_TIMEOUT_S
        if cmd == ["launchctl", "print", "gui/501/com.unified-agent.dispatch-tick"]:
            return SimpleNamespace(
                returncode=113,
                stdout="",
                stderr=(
                    'Bad request.\nCould not find service "com.unified-agent.dispatch-tick" '
                    "in domain for user gui: 501\n"
                ),
            )
        assert cmd == ["launchctl", "print-disabled", "gui/501"]
        return SimpleNamespace(
            returncode=0,
            stdout='disabled services = {\n\t"com.unified-agent.dispatch-tick" => disabled\n}\n',
            stderr="",
        )

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    state = routing.scheduler_runtime_state()
    assert state == {"enabled": False, "real_spawn": False, "status": "retired", "mode": "retired"}
    rows = routing.route_inventory(state)
    assert all(row["scheduler_spawn"]["enabled"] is False for row in rows)


@pytest.mark.parametrize("failure", ["timeout", "wrong_label", "malformed", "extra_error"])
def test_failed_launchd_print_cannot_imply_retirement(db, monkeypatch, failure):
    monkeypatch.setattr(routing.sys, "platform", "darwin")
    monkeypatch.setattr(monitor.os, "getuid", lambda: 501)
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")

    def fake_run(cmd, capture_output, text, timeout):
        assert cmd == ["launchctl", "print", "gui/501/com.unified-agent.dispatch-tick"]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        if failure == "wrong_label":
            return SimpleNamespace(
                returncode=113,
                stdout="",
                stderr='Could not find service "com.unified-agent.other" in domain',
            )
        if failure == "extra_error":
            return SimpleNamespace(
                returncode=113,
                stdout="",
                stderr=(
                    "Permission denied.\nCould not find service "
                    '"com.unified-agent.dispatch-tick" in domain for user gui: 501\n'
                ),
            )
        return SimpleNamespace(returncode=0, stdout="malformed output", stderr="")

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    assert routing.scheduler_runtime_state() == {
        "enabled": False,
        "real_spawn": False,
        "status": "unknown",
        "mode": "unknown",
    }


@pytest.mark.parametrize(
    "output",
    [
        '"com.unified-agent.dispatch-tick-other" => disabled',
        '"com.unified-agent.dispatch-tick" => enabled',
        '"com.unified-agent.dispatch-tick" => disabled\n'
        '"com.unified-agent.dispatch-tick" => enabled',
        '"com.unified-agent.dispatch-tick" => disabled extra',
    ],
)
def test_uncertain_launchd_override_preserves_unknown_scheduler(db, monkeypatch, output):
    monkeypatch.setattr(routing.sys, "platform", "darwin")
    monkeypatch.setattr(monitor.os, "getuid", lambda: 501)
    monkeypatch.setattr(monitor, "_service_probe", lambda *_: {"LoadState": "not-found"})
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )
    assert routing.scheduler_runtime_state() == {
        "enabled": False,
        "real_spawn": False,
        "status": "unknown",
        "mode": "unknown",
    }


def test_route_inventory_exposes_reloaded_retired_tick(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    monkeypatch.setattr(
        monitor, "_service_probe", lambda *_: {"LoadState": "loaded", "ActiveState": "active"}
    )
    rows = routing.route_inventory()
    assert all(row["scheduler_spawn"]["enabled"] is True for row in rows)
    assert all(row["scheduler_spawn"]["mode"] == "native_cli" for row in rows)
    assert all("retired timer loaded" in row["scheduler_spawn"]["next_action"] for row in rows)


@pytest.mark.parametrize("active_state", ["inactive", "unknown"])
def test_loaded_timer_preserves_future_spawn_capability(db, monkeypatch, active_state):
    monkeypatch.setattr(routing.sys, "platform", "darwin")
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    monkeypatch.setattr(
        monitor,
        "_service_probe",
        lambda *_: {"LoadState": "loaded", "ActiveState": active_state},
    )
    state = routing.scheduler_runtime_state()
    assert state["status"] == f"misconfigured_{active_state}"
    assert state["enabled"] is True and state["real_spawn"] is True
    rows = routing.route_inventory(state)
    assert all(row["scheduler_spawn"]["enabled"] is True for row in rows)
    assert all("retired timer loaded" in row["scheduler_spawn"]["next_action"] for row in rows)


def test_stopped_linux_timer_cannot_spawn(db, monkeypatch):
    monkeypatch.setattr(routing.sys, "platform", "linux")
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    monkeypatch.setattr(
        monitor,
        "_service_probe",
        lambda *_: {"LoadState": "loaded", "ActiveState": "inactive"},
    )
    state = routing.scheduler_runtime_state()
    assert state == {
        "enabled": False,
        "real_spawn": False,
        "status": "misconfigured_inactive",
        "mode": "disabled",
    }
    rows = routing.route_inventory(state)
    assert all(row["scheduler_spawn"]["enabled"] is False for row in rows)


def test_dispatch_route_handler_offloads_probe_from_event_loop(monkeypatch):
    from unified_agent.webui import routes

    main_thread = threading.get_ident()
    monkeypatch.setattr(
        routes.api,
        "dispatch_routes_payload",
        lambda: {"probe_thread": threading.get_ident()},
    )
    result = asyncio.run(routes._dispatch_routes(None))
    assert result["probe_thread"] != main_thread


@pytest.mark.parametrize("label", SUPPORTED)
def test_routed_launch_builds_observable_request(db, label, monkeypatch, tmp_path):
    if label.startswith("server_"):
        ws = tmp_path / "server-ws"
        ws.mkdir()
        monkeypatch.setenv("UA_AGENT_WORKSPACES", f"{label}:{ws}")
        monkeypatch.setenv(
            "UA_AGENT_LABELS",
            f"Desktop_Agent1:Desktop_Agent2:Operator:{label}",
        )
    packed = _delegated(to_agent=label)
    _approve(packed["task_id"])
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)

    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=launch,
    )
    assert out["launched"] is True
    assert out["run_id"]
    assert len(spawner.calls) == 1
    req = spawner.calls[0]
    assert req["run_id"] == out["run_id"]
    assert req["task_id"] == packed["task_id"]
    assert req["agent_label"] == label
    assert req["execution_identity"] == label
    expected = "cto" if label in {"Desktop_Agent5", "server_agent5"} else "developer"
    assert req["profile"] == expected
    assert req["cli_backend"] in NATIVE_CLIS
    assert req["argv"][0] == req["binary"]
    assert "--ua-dispatch" not in req["argv"]
    assert not any(a.startswith("--backend=") for a in req["argv"])
    assert not any(a.startswith("--run-id=") for a in req["argv"])
    _assert_native_headless_flags(req["cli_backend"], req["argv"])
    assert req["workspace"]
    assert req["repo_scope"]
    assert "task_spec" in req
    # Prompt stays in the redacted task_spec field, never on argv.
    assert req["task_spec"] not in req["argv"]
    # No secret material in the launch envelope (env is identity metadata only).
    assert set(req["env"]) <= {
        "UA_DISPATCH_RUN_ID",
        "UA_DISPATCH_TASK_ID",
        "UA_DISPATCH_AGENT",
        "UA_DISPATCH_PROFILE",
        "UA_AGENT_LABEL",
    }
    assert req["env"]["UA_AGENT_LABEL"] == label
    assert "Authorization" not in req
    assert "token" not in {k.lower() for k in req["env"]}
    assert "bearer " not in req["task_spec"].lower()
    assert "UA_AGENT_BEARER_TOKEN" not in req["task_spec"]


def test_denied_task_never_reaches_router(db):
    packed = _delegated(to_agent="Desktop_Agent1")
    tasks.task_update(
        packed["task_id"],
        append_note="[plan: denied @ 2026-08-24T00:00:00Z]",
    )
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "denied"
    assert spawner.calls == []


def test_not_dispatchable_agent_refused_before_launch(db):
    packed = _delegated(to_agent="Desktop_Agent1")
    _approve(packed["task_id"])
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE delegations SET to_agent=? WHERE id=?",
            ("Operator", packed["delegation_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)
    out = coordinator.claim_and_dispatch(
        delegation_id=packed["delegation_id"],
        launch=launch,
    )
    assert out["launched"] is False
    assert out["reason"] == "not_dispatchable"
    assert spawner.calls == []


def test_workspace_mismatch_refuses(db, tmp_path):
    packed = _delegated(to_agent="Desktop_Agent1")
    _approve(packed["task_id"])
    other = tmp_path / "other-ws"
    other.mkdir()
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner, expected_workspace=other)
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "launch_failed"
    assert out["error"] == "workspace_mismatch"
    assert out["spawned"] is False
    run = coordinator.get_run(out["run_id"])
    assert run is not None
    assert run["state"] == "retryable"
    assert run["failure_reason"] == "workspace_mismatch"
    assert spawner.calls == []


def test_laptop_without_workspace_binding_denied(db, monkeypatch, tmp_path):
    monkeypatch.setenv("UA_AGENT_WORKSPACES", "")
    packed = _delegated(to_agent="Laptop_Agent1")
    _approve(packed["task_id"])
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "launch_failed"
    assert out["error"] == "workspace_denied"
    assert out["spawned"] is False
    run = coordinator.get_run(out["run_id"])
    assert run["failure_reason"] == "workspace_denied"
    assert spawner.calls == []


def test_desktop_explicit_deny_falls_back_to_process_workspace(db, monkeypatch, tmp_path):
    """Mac hub DENY for Desktop_* blocks HTTP fs_*, not local CLI spawn cwd."""
    monkeypatch.setenv("UA_AGENT_WORKSPACES", "Desktop_Agent1:DENY")
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    packed = _delegated(to_agent="Desktop_Agent1")
    _approve(packed["task_id"])
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    # StubProcessSpawner always reports spawned=False; success is launch + cwd bind.
    assert out["launched"] is True
    assert out["spawned"] is False
    assert spawner.calls
    assert Path(spawner.calls[0]["cwd"]).resolve() == tmp_path.resolve()


def test_desktop_conflicting_workspace_binding_stays_denied(db, monkeypatch, tmp_path):
    """Conflict fail-closed must not get the Desktop DENY spawn-cwd fallback."""
    intended = tmp_path / "intended"
    intended.mkdir()
    monkeypatch.setenv(
        "UA_AGENT_WORKSPACES",
        f"Desktop_Agent1:DENY,Desktop_Agent1:{intended}",
    )
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    packed = _delegated(to_agent="Desktop_Agent1")
    _approve(packed["task_id"])
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "launch_failed"
    assert out["error"] == "workspace_denied"
    assert out["spawned"] is False
    assert spawner.calls == []


def test_server_Agent1_is_dispatchable_and_uses_binding(db, monkeypatch, tmp_path):
    ws = tmp_path / "server-ws"
    ws.mkdir()
    monkeypatch.setenv("UA_AGENT_WORKSPACES", f"server_agent1:{ws}")
    monkeypatch.setenv(
        "UA_AGENT_LABELS",
        "Desktop_Agent1:Desktop_Agent2:server_agent1:Operator",
    )
    packed = _delegated(to_agent="server_agent1")
    _approve(packed["task_id"])
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is True
    assert out["spawned"] is False
    assert spawner.calls
    assert Path(spawner.calls[0]["cwd"]).resolve() == ws.resolve()
    assert spawner.calls[0]["agent_label"] == "server_agent1"


def test_bound_task_spec_redacts_secrets():
    notes = "Objective ok\nbearer SECRETTOKEN123\napi_key=sk-abcdefghijklmnopqrstuvwxyz\n"
    bound = routing.bound_task_spec(notes)
    assert "SECRETTOKEN123" not in bound
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in bound
    assert "[REDACTED]" in bound


def test_malformed_route_at_launch_boundary(db):
    """Direct boundary call with garbage label fails closed (no spawn)."""
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)
    result = launch.launch(
        run_id="run-x",
        task_id=1,
        agent_label="???not-a-route",
        delegation_id=None,
    )
    assert result["ok"] is False
    assert result["error"] in {"unknown_label", "malformed_route"}
    assert spawner.calls == []


def test_launch_boundary_rejects_missing_task_row(db):
    packed = _delegated(to_agent="Desktop_Agent1")
    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM tasks WHERE id=?", (packed["task_id"],))
        conn.commit()
    finally:
        conn.close()
    result = launch.launch(
        run_id="run-missing",
        task_id=packed["task_id"],
        agent_label="Desktop_Agent1",
        delegation_id=packed["delegation_id"],
    )
    assert result["ok"] is False
    assert result["error"] == "unknown_task"
    assert spawner.calls == []


def test_default_claim_uses_routed_boundary(db):
    packed = _delegated(to_agent="Desktop_Agent2")
    _approve(packed["task_id"])
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"])
    assert out["launched"] is True
    run = coordinator.get_run(out["run_id"])
    assert run["agent_label"] == "Desktop_Agent2"
    assert run["state"] == "launched"


def test_all_routes_catalog_complete():
    catalog = routing.all_routes()
    assert set(catalog) == set(SUPPORTED)
    for label, route in catalog.items():
        assert route.agent_label == label
        assert Path(route.binary).name  # non-empty binary path/name


def test_cursor_route_uses_agent_with_hub_persona_wrapper(monkeypatch):
    monkeypatch.delenv("UA_CURSOR_BIN", raising=False)
    route = routing.build_route("Desktop_Agent4")
    assert route is not None
    assert route.cli_backend == "cursor"
    assert route.binary == "agent-with-hub"
    assert route.binary_env == "UA_CURSOR_BIN"


def test_copilot_route_uses_copilot_with_hub_persona_wrapper(monkeypatch):
    monkeypatch.delenv("UA_COPILOT_BIN", raising=False)
    route = routing.build_route("Desktop_Agent6")
    assert route is not None
    assert route.cli_backend == "copilot"
    assert route.binary == "copilot-with-hub"
    assert route.binary_env == "UA_COPILOT_BIN"
    argv = routing.build_launch_argv(route, run_id="run-1", task_id=1)
    assert argv[0] == "copilot-with-hub"
    assert "--output-format" in argv
    assert "json" in argv
    assert "--allow-all-tools" in argv
    assert "-p" not in argv


def test_devin_route_uses_devin_with_hub_persona_wrapper(monkeypatch):
    """Task #1237: DEFAULT_DEVIN_BIN isolates Desktop_Agent7's bearer the same
    way Cursor and Copilot already do, as defense-in-depth against
    mcp_config.json ever being regenerated with env interpolation."""
    monkeypatch.delenv("UA_DEVIN_BIN", raising=False)
    route = routing.build_route("Desktop_Agent7")
    assert route is not None
    assert route.cli_backend == "devin"
    assert route.binary == "devin-with-hub"
    assert route.binary_env == "UA_DEVIN_BIN"
    argv = routing.build_launch_argv(route, run_id="run-1", task_id=1)
    assert argv[0] == "devin-with-hub"
    assert argv[1:3] == ["--permission-mode", "accept-edits"]


def test_devin_route_honors_ua_devin_bin_override(monkeypatch):
    monkeypatch.setenv("UA_DEVIN_BIN", "/custom/devin")
    route = routing.build_route("Desktop_Agent7")
    assert route is not None
    assert route.binary == "/custom/devin"


def test_hermes_route_uses_hermes_cli(monkeypatch):
    monkeypatch.delenv("UA_HERMES_BIN", raising=False)
    route = routing.build_route("Desktop_Agent5")
    assert route is not None
    assert route.cli_backend == "hermes"
    assert route.binary == "hermes"
    argv = routing.build_launch_argv(route, run_id="run-1", task_id=1)
    assert argv[0] == "hermes"
    assert "--oneshot" in argv


def test_headless_argv_keeps_prompt_off_observable_form():
    observable = routing.headless_argv("claude", "claude")
    assert observable == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    spawned = routing.headless_argv("claude", "claude", prompt="do the task")
    assert spawned[-1] == "do the task"
    assert "--verbose" in spawned


def test_headless_argv_agy_binds_prompt_to_print_flag():
    """Regression for soak #626 / run-626-aa0bdb13: Agy -p eats the next token."""
    observable = routing.headless_argv("agy", "agy")
    assert observable == ["agy", "--output-format", "stream-json"]
    assert "-p" not in observable

    spawned = routing.headless_argv("agy", "agy", prompt="do the task")
    assert spawned == ["agy", "-p", "do the task", "--output-format", "stream-json"]
    # Never let -p precede a flag — that made live Agy exit 2.
    p_idx = spawned.index("-p")
    assert spawned[p_idx + 1] == "do the task"
    assert spawned[p_idx + 1] != "--output-format"


def test_headless_argv_agy_skip_permissions_after_format(monkeypatch):
    monkeypatch.setenv("UA_AGY_SKIP_PERMISSIONS", "1")
    argv = routing.headless_argv(
        "agy", "agy", prompt="p", sandbox_posture=harness_sandbox.resolve("agy")
    )
    assert argv == [
        "agy",
        "-p",
        "p",
        "--output-format",
        "stream-json",
        "--sandbox",
        "--dangerously-skip-permissions",
    ]


def test_headless_argv_agy_dispatch_skip_permissions_without_global_env(monkeypatch):
    """Soak #628: CliProcessSpawner must pass skip_permissions=True, not hub env."""
    monkeypatch.delenv("UA_AGY_SKIP_PERMISSIONS", raising=False)
    argv = routing.headless_argv(
        "agy",
        "agy",
        prompt="p",
        skip_permissions=True,
        sandbox_posture=harness_sandbox.resolve("agy"),
    )
    assert argv == [
        "agy",
        "-p",
        "p",
        "--output-format",
        "stream-json",
        "--sandbox",
        "--dangerously-skip-permissions",
    ]
    observable = routing.headless_argv("agy", "agy", skip_permissions=True)
    assert observable == [
        "agy",
        "--output-format",
        "stream-json",
        "--sandbox",
        "--dangerously-skip-permissions",
    ]


def test_headless_argv_codex_uses_sandbox_gate(monkeypatch):
    monkeypatch.delenv("UA_CODEX_ALLOW_FULL_ACCESS", raising=False)
    monkeypatch.setenv("UA_CODEX_SANDBOX", "danger-full-access")
    with pytest.raises(ValueError, match="UA_CODEX_ALLOW_FULL_ACCESS"):
        routing.headless_argv("codex", "codex", prompt="do it")
    monkeypatch.setenv("UA_CODEX_ALLOW_FULL_ACCESS", "1")
    argv = routing.headless_argv("codex", "codex", prompt="do it")
    assert "--sandbox" in argv
    assert "danger-full-access" in argv


def test_routed_launch_persists_spawn_pid(db):
    packed = _delegated(to_agent="Desktop_Agent1")
    _approve(packed["task_id"])

    class PidSpawner:
        def spawn(self, request):
            return {"ok": True, "stub": False, "spawned": True, "pid": 4242}

    launch = RoutedLaunchBoundary(spawner=PidSpawner())
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is True
    run = coordinator.get_run(out["run_id"])
    # Task #602: evidence_ref is pid:<n>:<start> when start time is readable,
    # else legacy pid:<n> (fake pids have no /proc start time).
    from unified_agent.dispatch.supervise import parse_pid_token

    pid, _start = parse_pid_token(run["evidence_ref"])
    assert pid == 4242
    assert run["evidence_ref"].startswith("pid:4242")


def test_pi_label_is_not_dispatchable(monkeypatch):
    monkeypatch.setenv("UA_AGENT_LABELS", "pi:pi_worker")
    result = routing.resolve_named_agent("pi")
    assert result.ok is False
    assert result.reason == "not_dispatchable"


def test_grok_is_not_a_dispatch_spawn_target(monkeypatch):
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent8:Desktop_Agent1")
    result = routing.resolve_named_agent("Desktop_Agent8")
    assert result.ok is False
    assert result.reason == "not_dispatchable"


def _live_native_run(run_id: str, agent: str = "Desktop_agent3") -> None:
    """Record ``run_id`` as a live dispatch run for ``agent``.

    Since #885 the detached supervisor re-verifies the exact run against the
    task store before it spawns anything (``supervise._require_native_run``):
    an unknown or terminal run id is refused and no child pid is reported.
    """
    task = tasks.task_add(title=f"spawner test {run_id}")
    now = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor,"
            " created_at, updated_at) VALUES (?, ?, ?, 'launched', ?, ?, ?)",
            (run_id, task["id"], agent, agent, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def test_cli_process_spawner_starts_python_child(db, tmp_path, monkeypatch):
    marker = tmp_path / "spawned.txt"

    def fake_argv(cli_backend, binary, *, prompt=None, cwd=None, skip_permissions=None, **_kw):
        return [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ok')",
        ]

    monkeypatch.setattr(routing, "headless_argv", fake_argv)
    _live_native_run("run-spawn-1")
    result = CliProcessSpawner().spawn(
        {
            "binary": "agy",
            "cli_backend": "agy",
            "cwd": str(tmp_path),
            "task_spec": "do the work",
            "run_id": "run-spawn-1",
            "agent_label": "Desktop_agent3",
            "env": {},
        }
    )
    assert result["ok"] is True
    assert result["spawned"] is True
    assert result["stub"] is False
    assert isinstance(result["pid"], int)
    # spawn_exit_watcher owns proc.wait(); polling the marker avoids racing waitpid.
    # Poll for the content, not the file: write_text creates it empty first.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not (marker.exists() and marker.read_text()):
        time.sleep(0.01)
    assert marker.read_text() == "ok"
    log_path = Path(result["log_path"])
    assert log_path.exists()
    assert "run-spawn-1" in log_path.name


def test_local_cli_dispatch_delivers_assigned_skill_in_prompt(db, monkeypatch):
    from unified_agent import skills_registry
    from unified_agent.dispatch import launch as launch_module

    skill_dir = Path(os.environ["UA_WORKSPACE"]) / "dispatch-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: dispatch-skill\ndescription: A focused dispatch test skill.\n---\n\n"
        "LOCAL_DISPATCH_SKILL_MARKER\n"
    )
    skills_registry.publish("dispatch-skill", str(skill_dir), author="test")
    skills_registry.assign("dispatch-skill", "Desktop_agent3", author="test")

    received = {}
    monkeypatch.setattr(
        routing,
        "headless_argv",
        lambda _backend, _binary, *, prompt=None, **_kwargs: (
            received.update(prompt=prompt) or [sys.executable]
        ),
    )
    monkeypatch.setattr(launch_module, "_argv0_unavailable", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "unified_agent.dispatch.supervise.spawn_via_detached_supervisor",
        lambda *_a, **_k: 4242,
    )

    result = CliProcessSpawner().spawn(
        {
            "binary": sys.executable,
            "cli_backend": "codex",
            "cwd": os.environ["UA_WORKSPACE"],
            "task_spec": "Complete the local dispatch fixture.",
            "run_id": "run-skill-injection-test",
            "agent_label": "Desktop_agent3",
            "profile": "developer",
            "env": {},
        }
    )
    assert result["ok"] is True
    assert "LOCAL_DISPATCH_SKILL_MARKER" in received["prompt"]


def test_cli_process_spawner_missing_binary(db):
    result = CliProcessSpawner().spawn(
        {
            "binary": "definitely-not-a-ua-cli",
            "cli_backend": "agy",
            "cwd": ".",
            "task_spec": "do the work",
            "run_id": "run-missing",
            "agent_label": "Desktop_agent3",
            "env": {},
        }
    )
    assert result["ok"] is False
    assert result["spawned"] is False
    assert result["pid"] is None
    assert result["error"] == "binary_not_found:definitely-not-a-ua-cli"


def test_cli_process_spawner_resolves_relative_binary_against_launch_cwd(db, tmp_path, monkeypatch):
    """Relative binaries must resolve from the dispatched workspace."""
    tools = tmp_path / "tools"
    tools.mkdir()
    script = tools / "fake-agent"
    script.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n", encoding="utf-8")
    script.chmod(0o755)

    def fake_argv(cli_backend, binary, *, prompt=None, cwd=None, skip_permissions=None, **_kw):
        return ["./tools/fake-agent", "run"]

    monkeypatch.setattr(routing, "headless_argv", fake_argv)
    _live_native_run("run-relative-bin")
    result = CliProcessSpawner().spawn(
        {
            "binary": "./tools/fake-agent",
            "cli_backend": "agy",
            "cwd": str(tmp_path),
            "task_spec": "do the work",
            "run_id": "run-relative-bin",
            "agent_label": "Desktop_agent3",
            "env": {},
        }
    )
    assert result["ok"] is True
    assert result["spawned"] is True


def test_cli_process_spawner_supervisor_uses_package_root_not_workspace_shadow(
    db, tmp_path, monkeypatch
):
    """The supervisor import must not be shadowed by the dispatched workspace."""
    shadow = tmp_path / "unified_agent"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise ImportError('workspace shadow')\n", encoding="utf-8")
    marker = tmp_path / "agent-ran.txt"

    def fake_argv(cli_backend, binary, *, prompt=None, cwd=None, skip_permissions=None, **_kw):
        return [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ok')",
        ]

    monkeypatch.setattr(routing, "headless_argv", fake_argv)
    _live_native_run("run-shadow-cwd")
    result = CliProcessSpawner().spawn(
        {
            "binary": sys.executable,
            "cli_backend": "agy",
            "cwd": str(tmp_path),
            "task_spec": "do the work",
            "run_id": "run-shadow-cwd",
            "agent_label": "Desktop_agent3",
            "env": {},
        }
    )
    assert result["ok"] is True
    assert result["spawned"] is True
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not (
        marker.exists() and marker.read_text(encoding="utf-8")
    ):
        time.sleep(0.01)
    assert marker.read_text(encoding="utf-8") == "ok"


def test_cli_process_spawner_spawn_setup_oserror_returns_spawn_failed(db, monkeypatch):
    """Codex PR #296. Filesystem errors during log setup must not strand claimed runs."""
    from unified_agent.dispatch.launch import CliProcessSpawner

    original_mkdir = Path.mkdir

    def fail_dispatch_log_dir(self, *args, **kwargs):
        if "dispatch-runs" in str(self):
            raise OSError(28, "simulated filesystem full")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_dispatch_log_dir)
    result = CliProcessSpawner().spawn(
        {
            "binary": sys.executable,
            "cli_backend": "agy",
            "cwd": ".",
            "task_spec": "do the work",
            "run_id": "run-fs-fail",
            "agent_label": "Desktop_agent3",
            "env": {},
        }
    )
    assert result["ok"] is False
    assert result["spawned"] is False
    assert result["error"].startswith("spawn_failed:")


def test_cli_process_spawner_spawn_setup_filenotfound_returns_spawn_failed(db, monkeypatch):
    """PR #304: log-dir FileNotFoundError must not masquerade as binary_not_found."""
    from unified_agent.dispatch.launch import CliProcessSpawner

    original_mkdir = Path.mkdir

    def fail_dispatch_log_dir(self, *args, **kwargs):
        if "dispatch-runs" in str(self):
            raise FileNotFoundError(2, "simulated missing parent path")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_dispatch_log_dir)
    result = CliProcessSpawner().spawn(
        {
            "binary": sys.executable,
            "cli_backend": "agy",
            "cwd": ".",
            "task_spec": "do the work",
            "run_id": "run-fs-filenotfound",
            "agent_label": "Desktop_agent3",
            "env": {},
        }
    )
    assert result["ok"] is False
    assert result["spawned"] is False
    assert result["error"].startswith("spawn_failed:")
    assert "binary_not_found" not in result["error"]


def test_cli_process_spawner_rejects_unknown_backend(db):
    result = CliProcessSpawner().spawn(
        {
            "binary": "agy",
            "cli_backend": "not-a-backend",
            "cwd": ".",
            "task_spec": "do the work",
            "run_id": "run-bad-backend",
            "agent_label": "Desktop_agent3",
            "env": {},
        }
    )
    assert result["ok"] is False
    assert result["spawned"] is False
    assert result["error"]


# ── real-spawn gate and concurrency ceiling (task #599) ──────────────────────


def test_default_spawner_is_the_stub_unless_the_flag_is_set(monkeypatch):
    """The shipped default must start nothing.

    Before #599 this was not a choice at all: RoutedLaunchBoundary hardcoded
    StubProcessSpawner, so a scheduler tick claimed work, leased it, routed it,
    and launched no process. Making it configurable must not change what
    happens with the flag unset."""
    from unified_agent.dispatch.launch import default_spawner

    monkeypatch.delenv("UA_DISPATCH_REAL_SPAWN", raising=False)
    assert isinstance(default_spawner(), StubProcessSpawner)
    assert isinstance(RoutedLaunchBoundary().spawner, StubProcessSpawner)

    for falsey in ("0", "", "true", "yes", "TRUE"):
        monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", falsey)
        assert isinstance(default_spawner(), StubProcessSpawner), (
            f"{falsey!r} must not enable real spawn — only the exact string '1' does"
        )


def test_flag_set_to_one_selects_the_real_cli_spawner(monkeypatch):
    from unified_agent.dispatch.launch import default_spawner

    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    assert isinstance(default_spawner(), CliProcessSpawner)
    assert isinstance(RoutedLaunchBoundary().spawner, CliProcessSpawner)


def _live_run(
    task_id: int,
    run_id: str,
    state: str = "running",
    termination_outcome: str | None = None,
) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs "
            "(run_id, task_id, agent_label, state, actor, termination_outcome, created_at, updated_at) "
            "VALUES (?, ?, 'Desktop_agent3', ?, 'Desktop_agent3', ?, ?, ?)",
            (run_id, task_id, state, termination_outcome, storage.now(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()


def test_live_run_count_counts_only_live_states(db):
    """One live run PER TASK is already enforced by idx_dispatch_runs_live_task
    (a partial unique index over state in claimed/launched/running), so the
    three live rows here need three distinct tasks. The ceiling this feeds is
    the host-wide one — total concurrent agent processes, across tasks."""
    from unified_agent.dispatch.launch import live_run_count

    assert live_run_count() == 0
    live_tasks = [_delegated(to_agent="Desktop_agent3")["task_id"] for _ in range(3)]
    for index, (task_id, state) in enumerate(
        zip(live_tasks, ("claimed", "launched", "running"), strict=True)
    ):
        _live_run(task_id, f"live-{index}", state)
    assert live_run_count() == 3

    dead_task = _delegated(to_agent="Desktop_agent3")["task_id"]
    for index, state in enumerate(("finished", "failed", "denied")):
        _live_run(dead_task, f"dead-{index}", state)
    _live_run(dead_task, "dead-cancelled", "cancelled", termination_outcome="confirmed")
    assert live_run_count() == 3, "terminal runs must not count against the ceiling"

    blocked_task = _delegated(to_agent="Desktop_agent3")["task_id"]
    _live_run(blocked_task, "cancelled-unknown", "cancelled", termination_outcome="unknown")
    assert live_run_count() == 4, "unconfirmed cancellation must retain its slot"


def test_live_run_count_can_exclude_the_asking_run(db):
    """A run asking for a slot must not be counted as one of its own competitors.

    ``claim_and_dispatch`` inserts the run as ``claimed`` *before* it invokes the
    launch boundary, so a boundary that counts every live slot also counts the
    run that is asking (task #1327 review finding).
    """
    from unified_agent.dispatch.launch import live_run_count

    _live_run(_delegated(to_agent="Desktop_agent3")["task_id"], "self-run", "claimed")
    _live_run(_delegated(to_agent="Desktop_agent3")["task_id"], "other-run", "running")
    assert live_run_count() == 2
    assert live_run_count(exclude_run_id="self-run") == 1
    assert live_run_count(exclude_run_id="no-such-row") == 2, (
        "excluding a run that is not live must not change the count"
    )


def test_a_launch_does_not_count_itself_against_the_ceiling(db, monkeypatch):
    """At UA_DISPATCH_MAX_LIVE_RUNS=1 self-counting refuses EVERY launch.

    Reproduces the coordinator's real ordering: the run row already exists in
    state ``claimed`` when the boundary checks the ceiling. Before the fix this
    returned ``max_live_runs_reached:1/1`` and spawned nothing.
    """
    monkeypatch.setenv("UA_DISPATCH_MAX_LIVE_RUNS", "1")
    packed = _delegated(to_agent="Desktop_agent3")
    _approve(packed["task_id"])
    # Already claimed by the coordinator — the run that is about to be launched.
    _live_run(packed["task_id"], "run-asking", "claimed")

    calls: list[dict] = []

    class RecordingCliSpawner(CliProcessSpawner):
        """Real spawner type (so the ceiling applies) that never execs."""

        def spawn(self, request):
            calls.append(request)
            return {"ok": True, "spawned": True, "pid": 4321}

    launch = RoutedLaunchBoundary(spawner=RecordingCliSpawner())
    out = launch.launch(
        run_id="run-asking",
        task_id=packed["task_id"],
        agent_label="Desktop_agent3",
        delegation_id=None,
    )
    assert out["ok"] is True, out
    assert len(calls) == 1, "a run must not be refused by its own slot"

    # The ceiling still bites once the host is genuinely occupied by someone else.
    _live_run(_delegated(to_agent="Desktop_agent3")["task_id"], "occupier", "running")
    refused = launch.launch(
        run_id="run-asking-2",
        task_id=packed["task_id"],
        agent_label="Desktop_agent3",
        delegation_id=None,
    )
    assert refused["ok"] is False
    assert refused["error"].startswith("max_live_runs_reached:")
    assert len(calls) == 1, "the second launch must not have spawned"


def test_finished_bookkeeping_ignores_another_tasks_unconfirmed_cancellation(db):
    """A cancelled/unconfirmed row on ANOTHER task is not this task's live run.

    Same class as the ceiling fix: the slot predicate is an unparenthesized OR
    chain, so any site that ANDs it with a task constraint must parenthesize it —
    otherwise the OR branches escape that constraint and match other tasks' rows.
    """
    from unified_agent.dispatch import scheduler

    mine = _delegated(to_agent="Desktop_agent3")["task_id"]
    # Another task's run ended, but its cessation was never confirmed, so per
    # #1221 the row keeps reserving a live slot — for ITS task, not for this one.
    other = _delegated(to_agent="Desktop_agent3")["task_id"]
    _live_run(other, "other-unconfirmed", "cancelled", termination_outcome="unknown")

    assert scheduler._task_needs_finished_bookkeeping(mine, "mine-finished") is True, (
        "another task's unconfirmed cancellation must not suppress this task's bookkeeping"
    )


def test_real_spawn_refuses_once_the_live_ceiling_is_reached(db, monkeypatch):
    """A scheduler bug or a burst of approvals must not fork unbounded agents."""
    monkeypatch.setenv("UA_DISPATCH_MAX_LIVE_RUNS", "1")
    packed = _delegated(to_agent="Desktop_agent3")
    _approve(packed["task_id"])
    # The occupier must be a DIFFERENT task: one live run per task is already
    # enforced by a partial unique index, and this test is about the host-wide
    # ceiling, not that per-task rule.
    _live_run(_delegated(to_agent="Desktop_agent3")["task_id"], "occupier", "running")

    calls: list[dict] = []

    class RecordingCliSpawner(CliProcessSpawner):
        """Real spawner type (so the ceiling applies) that never execs."""

        def spawn(self, request):
            calls.append(request)
            return {"ok": True, "spawned": True, "pid": 1234}

    launch = RoutedLaunchBoundary(spawner=RecordingCliSpawner())

    out = launch.launch(
        run_id="run-over-ceiling",
        task_id=packed["task_id"],
        agent_label="Desktop_agent3",
        delegation_id=None,
    )
    assert out["ok"] is False
    assert out["spawned"] is False
    assert out["pid"] is None
    assert out["error"].startswith("max_live_runs_reached:")
    assert calls == [], "the ceiling must be enforced BEFORE the process is spawned"


def test_the_ceiling_does_not_apply_to_the_stub(db, monkeypatch):
    """The stub starts nothing, so capping it would only change existing tick
    behaviour for no safety gain."""
    monkeypatch.setenv("UA_DISPATCH_MAX_LIVE_RUNS", "1")
    packed = _delegated(to_agent="Desktop_agent3")
    _approve(packed["task_id"])
    _live_run(packed["task_id"], "occupier", "running")

    spawner = StubProcessSpawner()
    launch = RoutedLaunchBoundary(spawner=spawner)
    out = launch.launch(
        run_id="run-stub-uncapped",
        task_id=packed["task_id"],
        agent_label="Desktop_agent3",
        delegation_id=None,
    )
    assert out["ok"] is True
    assert len(spawner.calls) == 1
