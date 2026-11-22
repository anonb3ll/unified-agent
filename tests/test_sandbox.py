import subprocess
from unittest.mock import patch

from unified_agent import config
from unified_agent.tools.shell import run_cmd


def test_sandbox_disabled_by_default(isolated_env):
    # By default, sandbox is disabled, so we run on the host
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ls"], returncode=0, stdout="file.txt", stderr=""
        )

        res = run_cmd("ls")
        assert res["exit_code"] == 0
        assert res["stdout"] == "file.txt"

        # Verify it called ls directly, not docker or podman
        assert mock_run.call_args[0][0] == ["ls"]


def test_sandbox_enabled_docker(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("UA_SANDBOX_ENGINE", "docker")
    monkeypatch.setenv("UA_SANDBOX_IMAGE", "custom-developer-image:latest")

    ws_root = config.workspace_root().resolve()

    with patch("subprocess.run") as mock_run, patch("shutil.which") as mock_which:
        mock_which.return_value = "/usr/bin/docker"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="sandbox-out", stderr=""
        )

        res = run_cmd("ls -la")
        assert res["exit_code"] == 0
        assert res["stdout"] == "sandbox-out"

        # Verify the constructed docker command
        called_args = mock_run.call_args[0][0]
        assert called_args[0] == "docker"
        assert called_args[1] == "run"
        assert "--rm" in called_args

        # Check container name
        name_index = called_args.index("--name")
        assert called_args[name_index + 1].startswith("ua-sandbox-")

        # Check isolation flags
        assert "--cap-drop=ALL" in called_args
        assert "--security-opt=no-new-privileges" in called_args
        assert "--pids-limit" in called_args
        pids_index = called_args.index("--pids-limit")
        assert called_args[pids_index + 1] == "100"

        # Check default network isolation
        assert "--network" in called_args
        net_index = called_args.index("--network")
        assert called_args[net_index + 1] == "none"

        # Check volume mounting
        vol_index = called_args.index("-v")
        assert called_args[vol_index + 1] == f"{ws_root}:{ws_root}"

        # Check working directory
        wdir_index = called_args.index("-w")
        assert called_args[wdir_index + 1] == str(ws_root)

        # Check env mapping (PATH dropped, HOME set to workspace root)
        assert f"HOME={ws_root}" in called_args
        assert not any(arg.startswith("PATH=") for arg in called_args)

        # Check image and command
        assert "custom-developer-image:latest" in called_args
        assert called_args[-2:] == ["ls", "-la"]


def test_sandbox_network_opt_in(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("UA_SANDBOX_ENGINE", "docker")
    monkeypatch.setenv("UA_SANDBOX_NETWORK", "1")

    with patch("subprocess.run") as mock_run, patch("shutil.which") as mock_which:
        mock_which.return_value = "/usr/bin/docker"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        res = run_cmd("ls")
        assert res["exit_code"] == 0

        called_args = mock_run.call_args[0][0]
        # Network opt-in should NOT specify --network none
        assert "--network" not in called_args


def test_sandbox_timeout_kills_container(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("UA_SANDBOX_ENGINE", "docker")

    with patch("subprocess.run") as mock_run, patch("shutil.which") as mock_which:
        mock_which.return_value = "/usr/bin/docker"

        # Raise TimeoutExpired on run but succeed on rm cleanup
        def run_side_effect(cmd, *args, **kwargs):
            if "run" in cmd:
                raise subprocess.TimeoutExpired(cmd, timeout=30)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        res = run_cmd("ls")
        assert res["timed_out"] is True

        # Verify rm -f was called with the correct container name
        rm_call = None
        for call in mock_run.call_args_list:
            called_cmd = call[0][0]
            if "rm" in called_cmd:
                rm_call = called_cmd
                break

        assert rm_call is not None
        assert rm_call[0] == "docker"
        assert rm_call[1] == "rm"
        assert rm_call[2] == "-f"
        assert rm_call[3].startswith("ua-sandbox-")


def test_sandbox_enabled_podman_auto_detect(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("UA_SANDBOX_ENGINE", "auto")

    with patch("subprocess.run") as mock_run, patch("shutil.which") as mock_which:
        # Mock podman found, docker not found
        def side_effect(name):
            return "/usr/bin/podman" if name == "podman" else None

        mock_which.side_effect = side_effect

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="podman-out", stderr=""
        )

        res = run_cmd("echo hello")
        assert res["exit_code"] == 0
        assert res["stdout"] == "podman-out"

        # Verify podman was selected
        called_args = mock_run.call_args[0][0]
        assert called_args[0] == "podman"
        assert "python:3.11-slim" in called_args
        assert called_args[-2:] == ["echo", "hello"]


def test_sandbox_engine_missing_error(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("UA_SANDBOX_ENGINE", "auto")

    with patch("shutil.which") as mock_which:
        # Neither engine found
        mock_which.return_value = None

        res = run_cmd("ls")
        assert res["exit_code"] == 127
        assert "container engine not found or failed to start" in res["stderr"]
