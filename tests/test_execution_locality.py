"""Devices-only execution boundary for a cloud control-plane hub (task #2109).

Every launch surface inventoried for the epic (#2107) is exercised with
``UA_EXECUTION_LOCALITY=devices``. Each must refuse to start an agent on the
hub, or route it to a ua-node device. ``TestSpawnSiteInventory`` is the tripwire:
a module that gains a process-spawn call must be classified here before it can
land.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import execution_locality as el
from unified_agent import node_queue, storage
from unified_agent.dispatch import scheduler
from unified_agent.dispatch.launch import (
    CliProcessSpawner,
    DevicesOnlyLaunchBoundary,
    NodeRoutedLaunchBoundary,
    RoutedLaunchBoundary,
    StubProcessSpawner,
    scheduler_launch_boundary,
    ui_launch_boundary,
)
from unified_agent.tools import delegate, tasks

SRC = Path(__file__).parent.parent / "src" / "unified_agent"
AGENT = "Desktop_Agent2"


@pytest.fixture
def devices(monkeypatch):
    monkeypatch.setenv("UA_EXECUTION_LOCALITY", "devices")


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("UA_DISPATCH_SCHEDULER_ENABLED", "UA_DISPATCH_NODE_ROUTING"):
        monkeypatch.delenv(name, raising=False)
    storage.init_db()
    return tmp_path


# --- the setting ------------------------------------------------------------


class TestLocalitySetting:
    def test_default_is_local(self, monkeypatch):
        for name in ("UA_EXECUTION_LOCALITY", "RAILWAY_ENVIRONMENT_ID", "UA_HOSTED"):
            monkeypatch.delenv(name, raising=False)
        assert el.locality() == "local"
        assert not el.devices_only()

    @pytest.mark.parametrize(
        "env", [{"RAILWAY_ENVIRONMENT_ID": "e1"}, {"UA_HOSTED": "1"}], ids=["railway", "flag"]
    )
    def test_hosted_platform_defaults_to_devices(self, env):
        assert el.locality(env) == "devices"
        assert el.source(env) == "hosted platform default"

    @pytest.mark.parametrize("hosted", [{"RAILWAY_ENVIRONMENT_ID": "e1"}, {"UA_HOSTED": "1"}])
    def test_hosted_local_is_refused_and_fails_closed(self, hosted):
        env = {**hosted, "UA_EXECUTION_LOCALITY": "local"}
        assert el.locality(env) == "devices"
        with pytest.raises(ValueError, match="local is refused on a hosted hub"):
            el.validate(env)

    def test_standalone_local_remains_supported(self):
        el.validate({"UA_EXECUTION_LOCALITY": "local"})
        assert el.locality({"UA_EXECUTION_LOCALITY": "local"}) == "local"

    def test_unrecognised_value_fails_closed_and_refuses_start(self):
        env = {"UA_EXECUTION_LOCALITY": "device"}
        assert el.locality(env) == "devices"
        with pytest.raises(ValueError, match="UA_EXECUTION_LOCALITY"):
            el.validate(env)

    def test_refusal_message_points_at_node_dispatch(self, devices):
        msg = el.refusal_message("x")
        assert "UA_EXECUTION_LOCALITY=devices" in msg
        assert "unified-agent node" in msg


class TestAgentLaunchClassifier:
    @pytest.mark.parametrize(
        "argv",
        [
            ["claude", "-p", "hi"],
            ["/usr/local/bin/codex", "exec"],
            "agy -p hello",
            ["sh", "-c", "claude -p hi"],
            ["bash", "-lc", "opencode run x"],
            [sys.executable, "-m", "unified_agent.dispatch.supervisor_spawn"],
            ["python3", "-m", "unified_agent.executor.daemon"],
            ["python3", "-Ium", "unified_agent.executor.daemon"],
            ["python3", "-W", "ignore", "-Xutf8", "-m", "unified_agent.node_agent"],
            ["python3", "-munified_agent.executor"],
            ["bash", "-c", "python3 -I -m unified_agent.executor"],
            [b"/opt/bin/cursor-agent"],
        ],
    )
    def test_agent_launches(self, argv):
        assert el.is_agent_launch(argv)

    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "log", "--grep", "pi"],
            ["ls", "claude"],
            ["sh", "-c", "git status"],
            ["gh", "pr", "view"],
            ["git", "log", "--grep", "unified_agent.executor"],
            ["gh", "pr", "comment", "1", "--body", "see unified_agent.node_agent"],
            ["git", "commit", "-m", "unified_agent.executor"],
            ["printf", "%s %s", "-m", "unified_agent.executor"],
            ["python3", "script.py", "-m", "unified_agent.executor"],
            ["python3", "-c", "print('hello')", "-m", "unified_agent.executor"],
            ["python3", "--", "-m", "unified_agent.executor"],
            ["python3", "-W", "-m", "unified_agent.executor"],
            ["python3", "-X", "-m", "unified_agent.executor"],
            ["python3", "-m", "json", "-m", "unified_agent.executor"],
            [],
        ],
    )
    def test_ordinary_commands(self, argv):
        assert not el.is_agent_launch(argv)


# --- layer 2: the process guard ----------------------------------------------

_GUARD_PROBE = r"""
import os, subprocess, sys
from unified_agent import execution_locality as el
assert el.install_process_guard()
results = {}
for name, argv in (("claude", ["claude", "--version"]), ("shell", ["sh", "-c", "codex x"])):
    try:
        subprocess.run(argv, capture_output=True)
        results[name] = "ran"
    except el.ServerExecutionRefused:
        results[name] = "refused"
    except FileNotFoundError:
        results[name] = "not-found"
try:
    subprocess.run(["git", "--version"], capture_output=True, check=True)
    results["git"] = "ran"
except el.ServerExecutionRefused:
    results["git"] = "refused"
try:
    os.system("claude --version >/dev/null 2>&1")
    results["system"] = "ran"
except el.ServerExecutionRefused:
    results["system"] = "refused"
ordinary = subprocess.run(["/usr/bin/printf", "%s %s", "-m", "unified_agent.executor"], capture_output=True, text=True, check=True)
assert ordinary.stdout == "-m unified_agent.executor"
pid = os.fork()
if pid == 0:
    try:
        os.execv("/bin/true", ["pi", "--mode", "rpc"])
    except el.ServerExecutionRefused:
        os._exit(42)
    os._exit(0)
_, status = os.waitpid(pid, 0)
results["exec"] = "refused" if os.waitstatus_to_exitcode(status) == 42 else "ran"
print(sorted(results.items()))
"""


def test_process_guard_refuses_agent_spawns_in_the_hub_process():
    env = {k: v for k, v in os.environ.items() if not k.startswith("UA_")}
    env["UA_EXECUTION_LOCALITY"] = "devices"
    env["PYTHONPATH"] = str(SRC.parent)
    proc = subprocess.run(
        [sys.executable, "-c", _GUARD_PROBE], capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(
        [
            ("claude", "refused"),
            ("exec", "refused"),
            ("git", "refused"),
            ("shell", "refused"),
            ("system", "refused"),
        ]
    )


def test_process_guard_is_not_installed_on_a_local_hub(monkeypatch):
    monkeypatch.setenv("UA_EXECUTION_LOCALITY", "local")
    assert el.install_process_guard() is False


def test_preconfigured_git_helper_is_refused_by_tool_and_process_backstop(tmp_path, monkeypatch):
    from unified_agent.tools.files import fs_write
    from unified_agent.tools.git import git_diff_staged

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.fsmonitor", "/bin/sh monitor.sh"],
        check=True,
    )
    marker = repo / "helper-ran"
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("UA_EXECUTION_LOCALITY", "devices")
    fs_write("repo/monitor.sh", f"#!/bin/sh\ntouch '{marker}'\n")
    original_config = (repo / ".git" / "config").read_bytes()

    # Prove that this pre-existing configuration executes a writable helper.
    subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True)
    assert marker.exists()
    marker.unlink()
    with pytest.raises(ToolError, match="git repository operation refused"):
        git_diff_staged("repo")
    assert not marker.exists()

    # Other hub Git consumers share the audit hook, even without the tool wrapper.
    code = """
import subprocess, sys
from unified_agent import execution_locality as el
el.install_process_guard()
for argv in (["git", "status", "--porcelain"], ["sh", "-c", "git status --porcelain"]):
    try:
        subprocess.run(argv, cwd=sys.argv[1], check=True, capture_output=True)
    except el.ServerExecutionRefused:
        continue
    raise AssertionError("Git helper reached")
"""
    env = {**os.environ, "PYTHONPATH": str(SRC.parent)}
    probe = subprocess.run(
        [sys.executable, "-c", code, str(repo)], capture_output=True, text=True, env=env, timeout=30
    )
    assert probe.returncode == 0, probe.stderr
    assert not marker.exists()
    assert (repo / ".git" / "config").read_bytes() == original_config


def test_hub_start_installs_the_guard(monkeypatch):
    """The hub validates the locality, installs the process guard, then serves."""
    from unified_agent import execution_locality, server

    order: list[str] = []
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setattr(sys, "argv", ["unified-agent"])
    monkeypatch.setattr(server.config, "validate_deployment_mode", lambda: None)
    monkeypatch.setattr(execution_locality, "validate", lambda: order.append("validate"))
    monkeypatch.setattr(
        execution_locality, "install_process_guard", lambda: order.append("install_guard")
    )
    for name in ("_require_node_queue_settings", "_start_embedded_event_bus"):
        monkeypatch.setattr(server, name, lambda: None)
    monkeypatch.setattr(server.config, "require_configured_http_credentials", lambda: None)
    monkeypatch.setattr(server.storage, "init_db", lambda: order.append("init_db"))
    monkeypatch.setattr(server.hub_runtime, "record_hub_start", lambda: None)
    monkeypatch.setattr(server, "_serve_http_hub", lambda: order.append("serve"))

    server.main()

    assert order == ["validate", "install_guard", "init_db", "serve"]


@pytest.mark.parametrize("hosted", ["RAILWAY_ENVIRONMENT_ID", "UA_HOSTED"])
def test_hosted_local_hub_refuses_start_before_storage_or_transport(monkeypatch, capsys, hosted):
    from unified_agent import server

    monkeypatch.setenv(hosted, "1")
    monkeypatch.setenv("UA_EXECUTION_LOCALITY", "local")
    monkeypatch.setattr(sys, "argv", ["unified-agent"])
    monkeypatch.setattr(server.config, "validate_deployment_mode", lambda: None)
    monkeypatch.setattr(server.storage, "init_db", lambda: pytest.fail("database reached"))
    monkeypatch.setattr(server, "_serve_http_hub", lambda: pytest.fail("transport reached"))
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 1
    assert "local is refused on a hosted hub" in capsys.readouterr().err


# --- layer 1: each launch surface ----------------------------------------------


class TestDispatchBoundaries:
    def test_scheduler_real_spawn_routes_to_nodes_without_fallback(self, devices, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
        boundary = scheduler_launch_boundary()
        assert isinstance(boundary, NodeRoutedLaunchBoundary)
        assert isinstance(boundary.fallback, DevicesOnlyLaunchBoundary)

    def test_scheduler_without_opt_in_stays_a_stub(self, devices, monkeypatch):
        """Review (#1234): devices mode must not drop the unattended-launch opt-in."""
        monkeypatch.delenv("UA_DISPATCH_REAL_SPAWN", raising=False)
        monkeypatch.setenv("UA_DISPATCH_NODE_ROUTING", "1")
        boundary = scheduler_launch_boundary()
        assert type(boundary) is RoutedLaunchBoundary
        assert isinstance(boundary.spawner, StubProcessSpawner)

    def test_control_center_dispatch_routes_to_a_node(self, devices):
        boundary = ui_launch_boundary()
        assert isinstance(boundary, NodeRoutedLaunchBoundary)
        assert isinstance(boundary.fallback, DevicesOnlyLaunchBoundary)

    def test_local_hub_boundaries_are_unchanged(self, monkeypatch):
        monkeypatch.setenv("UA_EXECUTION_LOCALITY", "local")
        monkeypatch.delenv("UA_DISPATCH_REAL_SPAWN", raising=False)
        monkeypatch.delenv("UA_DISPATCH_NODE_ROUTING", raising=False)
        ui = ui_launch_boundary()
        assert type(ui) is RoutedLaunchBoundary
        assert isinstance(ui.spawner, CliProcessSpawner)
        sched = scheduler_launch_boundary()
        assert type(sched) is RoutedLaunchBoundary
        assert isinstance(sched.spawner, StubProcessSpawner)

    def test_devices_only_fallback_reports_waiting_for_a_device(self):
        out = DevicesOnlyLaunchBoundary().launch(
            run_id="r1", task_id=1, agent_label=AGENT, delegation_id=None
        )
        assert out["ok"] is False
        assert out["error"] == f"no_device_for_agent:{AGENT}"
        assert out["spawned"] is False

    def test_cli_spawner_refuses_before_any_process(self, devices, monkeypatch):
        from unified_agent.dispatch import supervise

        def boom(*a, **k):
            raise AssertionError("supervisor must not be reached")

        monkeypatch.setattr(supervise, "spawn_via_detached_supervisor", boom)
        out = CliProcessSpawner().spawn(
            {"agent_label": AGENT, "binary": "codex", "run_id": "r1", "cwd": "."}
        )
        assert out["ok"] is False
        assert out["error"] == "server_execution_refused"
        assert out["spawned"] is False

    def test_supervisor_spawn_refuses(self, devices, tmp_path):
        from unified_agent.dispatch.supervise import spawn_via_detached_supervisor

        with pytest.raises(el.ServerExecutionRefused):
            spawn_via_detached_supervisor(
                ["codex"], cwd=str(tmp_path), env={}, log_path=tmp_path / "l", run_id="r1"
            )


def _delegated_approved() -> int:
    result = delegate.delegate(
        task_spec=(
            "### Objective\nDevices-only dispatch\n"
            "### Scope\n- src/unified_agent/execution_locality.py\n"
            "### Definition of Done\n- the hub never runs the agent itself\n"
        ),
        from_agent="Desktop_Agent5",
        to_agent=AGENT,
        callback="none",
    )
    tasks.task_update(result["task_id"], append_note="[plan: approved @ 2026-10-16T00:00:00Z]")
    return result["task_id"]


def _node_items() -> list[dict]:
    conn = storage.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM node_dispatch")]
    finally:
        conn.close()


class TestScheduledDispatchEndToEnd:
    def test_no_device_leaves_the_run_retryable(self, db, devices, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
        _delegated_approved()
        out = scheduler.scheduler_tick(force=True)
        assert out["launched"] == []
        failed = [s for s in out["skipped"] if s.get("reason") == "launch_failed"]
        assert len(failed) == 1
        assert failed[0]["error"] == f"no_device_for_agent:{AGENT}"
        assert _node_items() == []

    def test_registered_device_receives_the_work(self, db, devices, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
        node_queue.register(
            "dev1",
            identity=AGENT,
            facts={
                "os": {"system": "Linux"},
                "backends": ["codex"],
                "worktree_roots": [{"path": "/home/a/dev", "exists": True, "worktrees": 1}],
            },
        )
        _delegated_approved()
        out = scheduler.scheduler_tick(force=True)
        assert len(out["launched"]) == 1
        items = _node_items()
        assert len(items) == 1
        assert items[0]["node"] == "dev1"


class TestToolSurfaces:
    def test_pi_run_task_refuses(self, devices):
        from unified_agent.tools.pi_runner import pi_run_task

        with pytest.raises(ToolError, match="UA_EXECUTION_LOCALITY=devices"):
            pi_run_task("write a test")

    def test_claude_cli_leg_does_not_exist(self, devices, tmp_path, monkeypatch):
        from unified_agent import llm_client

        fake = tmp_path / "claude"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        monkeypatch.setenv("UA_CLAUDE_CLI_BIN", str(fake))
        assert llm_client.resolve_claude_cli() is None
        monkeypatch.setenv("UA_EXECUTION_LOCALITY", "local")
        assert llm_client.resolve_claude_cli() == str(fake)

    def test_gemini_never_runs_agy(self, devices, monkeypatch):
        from unified_agent.tools import gemini

        monkeypatch.setenv("UA_PREFER_AGY_CLI", "1")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr(gemini.shutil, "which", lambda name: f"/usr/bin/{name}")

        def no_run(*a, **k):
            raise AssertionError("agy must not run on a devices-only hub")

        monkeypatch.setattr(gemini.subprocess, "run", no_run)
        with pytest.raises(ToolError, match="GEMINI_API_KEY"):
            gemini.gemini_ask("hello")

    @pytest.mark.parametrize(
        "command",
        ["ls", "git status", "python3 -c print(1)", "env claude", "timeout 2 claude"],
        ids=str,
    )
    def test_run_cmd_is_refused_whole(self, devices, monkeypatch, command):
        """Review (#1234): wrappers, interpreters and repo-configured git helpers
        (core.fsmonitor) all start children the audit hook cannot see."""
        from unified_agent.tools.shell import run_cmd, run_cmd_check

        monkeypatch.setenv("UA_SHELL_ALLOWLIST", "env:timeout:nohup")
        (Path(os.environ["UA_WORKSPACE"]) / "f.txt").write_text("x")
        with pytest.raises(ToolError, match="UA_EXECUTION_LOCALITY=devices"):
            run_cmd(command)
        check = run_cmd_check(command)
        assert check["verdict"] == "block"
        assert "server-execution" in {f["code"] for f in check["findings"]}

    def test_run_cmd_runs_on_a_local_hub(self, monkeypatch):
        from unified_agent.tools.shell import run_cmd

        monkeypatch.setenv("UA_EXECUTION_LOCALITY", "local")
        (Path(os.environ["UA_WORKSPACE"]) / "f.txt").write_text("x")
        assert run_cmd("ls")["exit_code"] == 0

    def test_red_team_qa_refuses(self, devices):
        from unified_agent.tools.red_team import red_team_qa

        with pytest.raises(ToolError, match="red_team_qa refused"):
            red_team_qa(".")

    def test_merge_test_gate_refuses(self, devices, tmp_path):
        from unified_agent import merge

        with pytest.raises(el.ServerExecutionRefused, match="merge test gate"):
            merge._run_pytest(str(tmp_path))

    @pytest.mark.parametrize("direct", [False, True], ids=["runner-selection", "direct-bun"])
    def test_bun_project_gate_refuses_before_execution(
        self, devices, tmp_path, monkeypatch, direct
    ):
        from unified_agent import merge

        (tmp_path / "package.json").write_text('{"scripts":{"test":"bun test"}}')
        marker = tmp_path / "ran-project-code"
        runner = tmp_path / "bun"
        runner.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        runner.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
        with pytest.raises(el.ServerExecutionRefused, match="merge test gate"):
            if direct:
                merge._run_bun_test(str(tmp_path), ["test"])
            else:
                merge._run_tests(str(tmp_path))
        assert not marker.exists()

    def test_lint_refuses_project_shadow_module(self, devices, tmp_path, monkeypatch):
        from unified_agent.tools.lint import python_lint_and_format

        marker = tmp_path / "shadow-ran"
        (tmp_path / "ruff.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        )
        (tmp_path / "target.py").write_text("x = 1\n")
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ToolError, match="python_lint_and_format refused"):
            python_lint_and_format("target.py")
        assert not marker.exists()

    def test_bug_worker_gates_refuse(self, devices, tmp_path):
        from unified_agent.bug_worker import gates

        with pytest.raises(el.ServerExecutionRefused, match="bug-worker"):
            gates._run(["make", "test"], cwd=tmp_path)

    def test_session_end_skips_project_checks(self, devices, tmp_path, monkeypatch):
        from unified_agent import session_end

        def no_run(*a, **k):
            raise AssertionError("project checks must not run on a devices-only hub")

        monkeypatch.setattr(session_end.subprocess, "run", no_run)
        out = session_end._get_test_linter_status(tmp_path, run_checks=True)
        assert "devices-only" in out

    def test_sleep_pass_carries_the_locality(self):
        from unified_agent import sleep_pass

        assert "UA_EXECUTION_LOCALITY" in sleep_pass._HUB_LOCAL_MODEL_KEYS

    def test_executor_refuses_to_start(self, devices, monkeypatch):
        from unified_agent.executor import daemon

        monkeypatch.setenv("UA_EXECUTOR_AGENT", AGENT)
        monkeypatch.setattr(
            daemon, "ExecutorDaemon", lambda **k: pytest.fail("executor constructed")
        )
        with pytest.raises(SystemExit) as exc:
            daemon.main()
        assert exc.value.code == 2

    def test_node_subcommand_refuses(self, devices, monkeypatch, capsys):
        from unified_agent import server

        monkeypatch.setattr(sys, "argv", ["unified-agent", "node", "run"])
        with pytest.raises(SystemExit) as exc:
            server.main()
        assert exc.value.code == 2
        assert "unified-agent node refused" in capsys.readouterr().err


class TestDoctorRow:
    def _row(self, monkeypatch, env: dict[str, str]) -> dict:
        from unified_agent.tools import doctor

        monkeypatch.setattr(doctor, "_hub_posture_setting", lambda name: (env.get(name), True))
        return doctor._execution_locality_check()

    def test_devices_is_live(self, monkeypatch):
        row = self._row(monkeypatch, {"UA_EXECUTION_LOCALITY": "devices"})
        assert row["status"] == "LIVE"
        assert row["capability"] == "execution_locality"

    def test_hosted_default_is_live(self, monkeypatch):
        row = self._row(monkeypatch, {"RAILWAY_ENVIRONMENT_ID": "e1"})
        assert row["status"] == "LIVE"
        assert "hosted platform default" in row["detail"]

    def test_hosted_but_local_is_misconfigured(self, monkeypatch):
        row = self._row(
            monkeypatch, {"RAILWAY_ENVIRONMENT_ID": "e1", "UA_EXECUTION_LOCALITY": "local"}
        )
        assert row["status"] == "MISCONFIGURED"

    def test_plain_host_is_dormant(self, monkeypatch):
        assert self._row(monkeypatch, {})["status"] == "DORMANT"

    def test_settings_file_local_on_hosted_is_misconfigured(self, monkeypatch, tmp_path):
        """Review (#1234): the hub seeds UA_* from settings.toml [env]."""
        settings_file = tmp_path / "settings.toml"
        settings_file.write_text('[env]\nUA_EXECUTION_LOCALITY = "local"\n')
        row = self._row(
            monkeypatch,
            {"RAILWAY_ENVIRONMENT_ID": "e1", "UA_SETTINGS_FILE": str(settings_file)},
        )
        assert row["status"] == "MISCONFIGURED"

    def test_unit_value_beats_settings_file(self, monkeypatch, tmp_path):
        settings_file = tmp_path / "settings.toml"
        settings_file.write_text('[env]\nUA_EXECUTION_LOCALITY = "local"\n')
        row = self._row(
            monkeypatch,
            {"UA_EXECUTION_LOCALITY": "devices", "UA_SETTINGS_FILE": str(settings_file)},
        )
        assert row["status"] == "LIVE"

    def test_unrecognised_is_misconfigured(self, monkeypatch):
        row = self._row(monkeypatch, {"UA_EXECUTION_LOCALITY": "cloud"})
        assert row["status"] == "MISCONFIGURED"


# --- tripwire ------------------------------------------------------------------

# References as well as calls: a default argument such as
# ``run: Callable = subprocess.run`` is a spawn site too (Codex review, #1234).
_SPAWN_CALL = re.compile(
    r"subprocess\.(Popen|run|call|check_call|check_output)\b|pexpect\.spawn|"
    r"os\.exec[lv]p?e?\(|create_subprocess_(exec|shell)|os\.posix_spawn|"
    r"os\.system\(|os\.popen\(|os\.spawn[lv]p?e?\(|pty\.spawn"
)

# Every module that can start a process, with why it is safe on a devices-only
# hub. "guarded" means a layer-1 check runs before the spawn. The audit-hook
# backstop applies to all of them.
SPAWN_SITE_REVIEW = {
    "acp_client.py": "node-only: imported by node_agent, which refuses to start",
    "affected_tests.py": "docstring only: names the spawn API it analyses",
    "audit/anchor_git.py": "git",
    "audit/evidence_cli.py": "operator CLI: gh api / gh repo view for the merge receipt; starts no agent",
    "audit/engine.py": "git, read-only queries of the audited repository",
    "audit/rules.py": "git",
    "backup.py": "litestream / sqlite backup tooling",
    "bug_worker/gates.py": "guarded: make / uv project gates refuse",
    "cycle_eligibility.py": "git / gh",
    "demo/real_release_wedge.py": "demo CLI, git",
    "dispatch/harness_sandbox.py": "bwrap capability probe",
    "dispatch/supervise.py": "guarded: spawn_via_detached_supervisor refuses",
    "dispatch/supervisor_spawn.py": "child of supervise (refused upstream)",
    "dispatch/worker.py": "fake e2e worker (python -c script), test smoke only",
    "execution_locality.py": "docstring only",
    "executor/agy_interactive.py": "executor-only: main() refuses to start",
    "executor/backends.py": "executor-only: main() refuses to start",
    "executor/daemon.py": "guarded: main() refuses to start",
    "fleet_actions.py": "launchctl / systemctl",
    "harness_billing.py": "executor-only billing probe",
    "held_pr.py": "gh",
    "llm_client.py": "guarded: resolve_claude_cli is None, so the claude_cli leg is absent",
    "merge.py": "git backstop; guarded: all project test gates refuse",
    "messaging/grounding.py": "gh api / pr view / run view and git cat-file, fixed argv, "
    "read-only handoff grounding (#2390); starts no agent",
    "monitor.py": "systemctl / gh",
    "pdf_worker.py": "bwrap-sandboxed PDF text extraction",
    "planning_loop.py": "git",
    "release.py": "git",
    "review_cli.py": "operator CLI: gh pr view / compare for record-read; starts no agent",
    "reviewer.py": "git / gh / bun",
    "safety.py": "git",
    "scaffold_cli.py": "operator CLI: git init and unified-agent-sync",
    "service_probes.py": "systemctl",
    "session_end.py": "git; guarded: pytest/ruff project checks skip",
    "skill_sources.py": "git",
    "sync_cli.py": "git / ps",
    "tailnet.py": "tailscale whois",
    "tools/doctor.py": "probes (python config, litestream); claude via resolve_claude_cli",
    "tools/gcloud.py": "gcloud",
    "tools/gemini.py": "guarded: agy path disabled",
    "tools/git.py": "guarded: Git repository operations refuse; process backstop for other consumers",
    "tools/lint.py": "guarded: refuses project-shadowable Ruff module launch",
    "tools/notify.py": "bun send.ts notification bridge",
    "tools/pi_runner.py": "guarded: pi_run_task refuses",
    "tools/red_team.py": "guarded: red_team_qa refuses (pytest runs project code)",
    "tools/shell.py": "guarded: run_cmd is refused whole",
    "tools/tasks.py": "git worktree",
    "tools/work_product_cli.py": "operator CLI",
    "tools/worktree_policy.py": "git",
    "tui.py": "operator TUI: journalctl / systemctl",
    "github_app.py": "gh, injected as run_gh",
    "github_app_fastpath.py": "gh, injected as run_gh",
    "github_app_worker.py": "gh, injected as run_gh",
    "mcp_live_verification.py": "test patch target string only",
    "node_agent.py": "device side: refuses to start on a devices-only hub",
    "node_wake_transport.py": "device side: herdr wake transport (node only)",
    "pr_watch.py": "gh, injected as run_gh",
    "pytest_attestation.py": "operator CLI / CI: reruns pytest on the attested test ids; "
    "what those tests spawn is the test suite's own, as in any pytest run",
    "shipping_preflight_cli.py": "gh, injected as run_gh",
    "review_provenance.py": "gh api compare, fixed argv, injected as run_gh; read-only head-change provenance (#2398); starts no agent",
    "tools/cycles.py": "docstring only",
    "turn_checkpoints.py": "git",
    "vault_sync.py": "rsync / ssh vault sync",
}


class TestSpawnSiteInventory:
    def test_every_spawning_module_is_reviewed(self):
        found = {
            str(p.relative_to(SRC))
            for p in SRC.rglob("*.py")
            if _SPAWN_CALL.search(p.read_text(encoding="utf-8", errors="replace"))
        }
        unreviewed = sorted(found - set(SPAWN_SITE_REVIEW))
        assert not unreviewed, (
            "New process-spawn site(s). Classify each in SPAWN_SITE_REVIEW: guard it "
            "with execution_locality.refuse() if it can start an agent, or record why "
            f"it cannot (task #2109): {unreviewed}"
        )
        stale = sorted(set(SPAWN_SITE_REVIEW) - found)
        assert not stale, f"SPAWN_SITE_REVIEW lists modules that no longer spawn: {stale}"
