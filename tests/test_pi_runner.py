"""Unit tests for the Pi coding agent (pi.dev) RPC runner tool."""

import json
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config
from unified_agent.tools import pi_runner
from unified_agent.tools.pi_runner import (
    _build_env,
    parse_rpc_line,
    pi_run_task,
    resolve_pi_executable,
)

_STUB_PI = ["/usr/bin/pi"]


def _stub_pi(monkeypatch) -> None:
    monkeypatch.setattr(pi_runner, "resolve_pi_executable", lambda: list(_STUB_PI))


def _no_pi(monkeypatch) -> None:
    monkeypatch.delenv("UA_PI_EXECUTABLE", raising=False)
    monkeypatch.delenv("UA_PI_PACKAGE", raising=False)

    def which(cmd, path=None):
        if cmd == "npx":
            return "/usr/bin/npx"
        if cmd == "bunx":
            return "/usr/bin/bunx"
        return None

    monkeypatch.setattr(pi_runner.shutil, "which", which)
    monkeypatch.setattr(pi_runner.os.path, "isfile", lambda _path: False)
    monkeypatch.setattr(pi_runner.os, "access", lambda _path, _mode: False)


def _mock_proc(lines: list[str], returncode: int = 0):
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = lines
    mock_proc.returncode = returncode
    mock_proc.communicate.return_value = ("", "")
    return mock_proc


def test_resolve_pi_executable_prefers_local_pi(monkeypatch):
    monkeypatch.delenv("UA_PI_EXECUTABLE", raising=False)
    monkeypatch.setenv("UA_PI_PACKAGE", "@earendil-works/pi-coding-agent@9.9.9")

    def which(cmd, path=None):
        if cmd == "pi":
            return "/usr/local/bin/pi"
        if cmd == "npx":
            return "/usr/bin/npx"
        return None

    monkeypatch.setattr(pi_runner.shutil, "which", which)
    assert resolve_pi_executable() == ["/usr/local/bin/pi"]


def test_resolve_pi_executable_honours_absolute_override(monkeypatch, tmp_path):
    binary = tmp_path / "pi"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("UA_PI_EXECUTABLE", str(binary))
    assert resolve_pi_executable() == [str(binary)]


def test_resolve_pi_executable_rejects_relative_override(monkeypatch):
    monkeypatch.setenv("UA_PI_EXECUTABLE", "pi")
    with pytest.raises(ToolError, match="absolute"):
        resolve_pi_executable()


def test_resolve_pi_refuses_floating_npx_fallback(monkeypatch):
    """No local ``pi`` and no exact ``UA_PI_PACKAGE`` must not become ``npx -y``."""
    _no_pi(monkeypatch)
    with pytest.raises(ToolError, match=r"Refusing the unpinned `npx -y`"):
        resolve_pi_executable()


def test_resolve_pi_refuses_floating_package_tag(monkeypatch):
    _no_pi(monkeypatch)
    monkeypatch.setenv("UA_PI_PACKAGE", "@earendil-works/pi-coding-agent@latest")
    with pytest.raises(ToolError, match="exact version|floating"):
        resolve_pi_executable()


def test_resolve_pi_refuses_tag_with_an_embedded_digit(monkeypatch):
    _no_pi(monkeypatch)
    monkeypatch.setenv("UA_PI_PACKAGE", "@earendil-works/pi-coding-agent@next2")
    with pytest.raises(ToolError, match="exact version|floating"):
        resolve_pi_executable()


def test_resolve_pi_accepts_exact_package_spec(monkeypatch):
    _no_pi(monkeypatch)
    monkeypatch.setenv("UA_PI_PACKAGE", "@earendil-works/pi-coding-agent@1.2.3")

    def which(cmd, path=None):
        if cmd == "npx":
            return "/usr/bin/npx"
        return None

    monkeypatch.setattr(pi_runner.shutil, "which", which)
    assert resolve_pi_executable() == [
        "/usr/bin/npx",
        "-y",
        "@earendil-works/pi-coding-agent@1.2.3",
    ]


def test_parse_rpc_line():
    assert parse_rpc_line("") is None
    assert parse_rpc_line("   ") is None
    assert parse_rpc_line("not json") is None
    parsed = parse_rpc_line('{"type": "agent_start", "id": "1"}')
    assert parsed == {"type": "agent_start", "id": "1"}


def test_pi_run_task_empty_prompt():
    with pytest.raises(ToolError, match="prompt must not be empty"):
        pi_run_task("")


# -- Environment injection ---------------------------------------------------


def test_build_env_forwards_api_keys(monkeypatch):
    monkeypatch.setenv("PI_API_KEY", "pi-key-456")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key-789")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    env = _build_env("test_agent")

    assert env["UA_AGENT_LABEL"] == "test_agent"
    assert env["PI_API_KEY"] == "pi-key-456"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_build_env_strips_gemini_key_from_inherited_environment(monkeypatch):
    """A metered Gemini key must never reach Pi, even when the hub holds one.

    The child env is an allowlist, not ``os.environ.copy()``. A key the hub
    loaded at startup is still in this process and must not be forwarded.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key-123")

    env = _build_env("test_agent")

    assert "GEMINI_API_KEY" not in env


def test_build_env_does_not_scrape_key_files(monkeypatch, tmp_path):
    """Key files under HOME are not read into the child environment."""
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / ".env").write_text(
        "OPENAI_API_KEY=from-hermes\nGEMINI_API_KEY=gem-from-file\n"
    )
    ua = home / ".unified_agent"
    ua.mkdir()
    (ua / "sleep-pass.env").write_text("PI_API_KEY=from-sleep\n")
    (home / ".config" / "unified-agent").mkdir(parents=True)
    (home / ".config" / "unified-agent" / "gemini.env").write_text(
        "GEMINI_API_KEY=gem-from-config\n"
    )
    monkeypatch.setenv("HOME", str(home))
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    env = _build_env("test_agent")

    assert "GEMINI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "PI_API_KEY" not in env
    assert "from-hermes" not in env.values()
    assert "from-sleep" not in env.values()


def test_build_env_no_keys(monkeypatch):
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    env = _build_env("label")
    assert env["UA_AGENT_LABEL"] == "label"
    assert "UA_AGENT_TOKENS" not in env


# -- Subprocess success with tool telemetry ----------------------------------


@patch("subprocess.Popen")
def test_pi_run_task_success(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    lines = [
        json.dumps({"type": "agent_start"}),
        json.dumps(
            {
                "type": "tool_execution_start",
                "name": "read",
                "tool_call_id": "call_1",
                "arguments": {"path": "foo.py"},
            }
        ),
        json.dumps(
            {
                "type": "tool_execution_end",
                "name": "read",
                "tool_call_id": "call_1",
                "result": {"content": "file contents"},
            }
        ),
        json.dumps({"type": "message_update", "update": {"kind": "text_delta", "text": "Hello "}}),
        json.dumps({"type": "message_update", "update": {"kind": "text_delta", "text": "world!"}}),
        json.dumps({"type": "agent_end"}),
    ]
    mock_popen.return_value = _mock_proc(lines)

    result = pi_run_task(
        prompt="Say hello",
        cwd="",
        model="gpt-5.5",
        timeout_s=10,
    )

    assert result["status"] == "success"
    assert result["text"] == "Hello world!"
    assert len(result["tools_used"]) == 1
    assert result["tools_used"][0]["name"] == "read"
    assert result["tools_used"][0]["arguments"] == {"path": "foo.py"}
    assert result["tools_used"][0]["result"] == {"content": "file contents"}
    assert result["exit_code"] == 0
    assert result["error"] is None
    assert result["files_modified"] == []
    cmd = mock_popen.call_args[0][0]
    assert "--no-approve" in cmd
    assert cmd[0] == "/usr/bin/pi"


@patch("subprocess.Popen")
def test_popen_env_omits_hub_secrets(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("UA_AGENT_TOKENS", "sentinel-agent-tokens")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-sentinel")
    monkeypatch.setenv("SPECTRUM_PROJECT_SECRET", "spectrum-sentinel")
    monkeypatch.setenv("WORKOS_API_KEY", "workos-sentinel")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-sentinel")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-sentinel")
    monkeypatch.setenv("PI_API_KEY", "pi-ok")
    _stub_pi(monkeypatch)
    mock_popen.return_value = _mock_proc([json.dumps({"type": "agent_end"})])

    pi_run_task(prompt="print env", cwd="", timeout_s=10)

    env = mock_popen.call_args.kwargs["env"]
    assert env is not os.environ
    for banned in (
        "UA_AGENT_TOKENS",
        "SLACK_BOT_TOKEN",
        "SPECTRUM_PROJECT_SECRET",
        "WORKOS_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        assert banned not in env
    assert "sentinel-agent-tokens" not in env.values()
    assert "xoxb-sentinel" not in env.values()
    assert env["PI_API_KEY"] == "pi-ok"
    assert "PATH" in env
    assert env["UA_AGENT_LABEL"] == "pi_worker"


@patch("subprocess.Popen")
def test_steward_profile_raises_before_spawn(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    monkeypatch.setattr("unified_agent.profiles.current", lambda: "steward")
    with pytest.raises(ToolError, match="steward"):
        pi_run_task(prompt="print env", cwd="", timeout_s=10)
    mock_popen.assert_not_called()


@pytest.mark.parametrize("profile", ["developer", "chief-of-staff"])
@patch("subprocess.Popen")
def test_developer_and_chief_of_staff_can_spawn(mock_popen, profile, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    monkeypatch.setattr("unified_agent.profiles.current", lambda: profile)
    mock_popen.return_value = _mock_proc([json.dumps({"type": "agent_end"})])

    result = pi_run_task(prompt="allowed", cwd="", timeout_s=10)

    assert result["status"] == "success"
    mock_popen.assert_called_once()
    assert "--no-approve" in mock_popen.call_args[0][0]


def test_session_dir_outside_workspace_raises(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(workspace))
    with pytest.raises(ToolError, match="escapes the workspace"):
        pi_run_task(prompt="write logs", cwd="", session_dir=str(outside), timeout_s=10)


@patch("subprocess.Popen")
def test_session_dir_inside_workspace_is_resolved(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    session = tmp_path / "pi-sessions"
    session.mkdir()
    mock_popen.return_value = _mock_proc([json.dumps({"type": "agent_end"})])

    pi_run_task(prompt="continue", cwd="", session_dir="pi-sessions", timeout_s=10)

    cmd = mock_popen.call_args[0][0]
    idx = cmd.index("--session-dir")
    assert cmd[idx + 1] == str(session.resolve())


def test_agent_label_cannot_spoof_verified_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        with pytest.raises(ToolError, match="identity mismatch"):
            pi_run_task(prompt="journal", cwd="", agent_label="Desktop_Agent5", timeout_s=10)
    finally:
        config.agent_identity_var.reset(token)


@patch("subprocess.Popen")
def test_verified_identity_stamps_env_and_journal(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    mock_popen.return_value = _mock_proc([json.dumps({"type": "agent_end"})])
    journaled: list[dict] = []

    def _journal(**kwargs):
        journaled.append(kwargs)
        return {"path": "Logs/today.md", "entry": kwargs["entry"], "created_note": True}

    monkeypatch.setattr("unified_agent.tools.vault.vault_journal", _journal)
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        pi_run_task(
            prompt="note it",
            cwd="",
            agent_label="pi_worker",
            log_journal=True,
            timeout_s=10,
        )
    finally:
        config.agent_identity_var.reset(token)

    env = mock_popen.call_args.kwargs["env"]
    assert env["UA_AGENT_LABEL"] == "Desktop_Agent4"
    assert journaled and journaled[0]["agent"] == "Desktop_Agent4"


# -- File modification tracking ----------------------------------------------


@patch("subprocess.Popen")
def test_pi_run_task_tracks_modified_files(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    lines = [
        json.dumps({"type": "agent_start"}),
        json.dumps(
            {
                "type": "tool_execution_start",
                "name": "write",
                "tool_call_id": "call_w",
                "arguments": {"path": "src/app.py"},
            }
        ),
        json.dumps(
            {
                "type": "tool_execution_end",
                "name": "write",
                "tool_call_id": "call_w",
                "result": {"path": "src/app.py"},
            }
        ),
        json.dumps(
            {
                "type": "tool_execution_start",
                "name": "edit",
                "tool_call_id": "call_e",
                "arguments": {"path": "src/lib.py"},
            }
        ),
        json.dumps(
            {
                "type": "tool_execution_end",
                "name": "edit",
                "tool_call_id": "call_e",
                "result": {"path": "src/lib.py"},
            }
        ),
        json.dumps({"type": "message_update", "update": {"kind": "text_delta", "text": "Done"}}),
        json.dumps({"type": "agent_end"}),
    ]
    mock_popen.return_value = _mock_proc(lines)

    result = pi_run_task(prompt="Edit files", cwd="", timeout_s=10)

    assert result["status"] == "success"
    assert result["files_modified"] == ["src/app.py", "src/lib.py"]
    assert len(result["tools_used"]) == 2


# -- Session continuity ------------------------------------------------------


@patch("subprocess.Popen")
def test_pi_run_task_session_id_flag(mock_popen, tmp_path, monkeypatch):
    """Verify --session flag is passed when session_id is provided."""
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    mock_popen.return_value = _mock_proc([json.dumps({"type": "agent_end"})])

    pi_run_task(prompt="Continue work", cwd="", session_id="ses_abc123", timeout_s=10)

    cmd = mock_popen.call_args[0][0]
    assert "--session-id" in cmd
    idx = cmd.index("--session-id")
    assert cmd[idx + 1] == "ses_abc123"


# -- Error handling ----------------------------------------------------------


@patch("subprocess.Popen")
def test_pi_run_task_error(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    lines = [
        json.dumps({"type": "error", "message": "API key invalid"}),
    ]
    mock_proc = _mock_proc(lines, returncode=1)
    mock_proc.communicate.return_value = ("", "API error stderr")
    mock_popen.return_value = mock_proc

    result = pi_run_task(
        prompt="Failing task",
        cwd="",
        timeout_s=10,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 1
    assert result["error"] == "API key invalid"
    assert result["files_modified"] == []


@patch("subprocess.Popen")
def test_pi_run_task_timeout(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    import subprocess as sp

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = []
    mock_proc.kill = MagicMock()
    mock_popen.return_value = mock_proc

    call_count = [0]

    def smart_communicate(*_a, **_kw):
        call_count[0] += 1
        if call_count[0] == 1:
            raise sp.TimeoutExpired(cmd="pi", timeout=5)
        return ("", "")

    mock_proc.communicate.side_effect = smart_communicate

    result = pi_run_task(prompt="Slow task", cwd="", timeout_s=5)

    assert result["status"] == "timeout"
    assert result["exit_code"] == -1
    assert "timed out" in result["error"]
    assert result["files_modified"] == []
    mock_proc.kill.assert_called_once()


@patch("subprocess.Popen")
def test_timeout_is_capped_like_run_cmd(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    mock_popen.return_value = _mock_proc([json.dumps({"type": "agent_end"})])

    pi_run_task(prompt="long", cwd="", timeout_s=10_000)

    waited = mock_popen.return_value.communicate.call_args.kwargs["timeout"]
    cap = max(config.CMD_TIMEOUT_CAP_S, config.executor_timeout_s())
    assert config.CMD_TIMEOUT_CAP_S < waited <= cap


def test_stdout_loop_cannot_run_forever(tmp_path, monkeypatch):
    """A child that never closes stdout is killed at the capped deadline."""
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        pi_runner,
        "resolve_pi_executable",
        lambda: [sys.executable, "-c", "import time; time.sleep(30)"],
    )

    result = pi_run_task(prompt="hang", cwd="", timeout_s=1)

    assert result["status"] == "timeout"
    assert result["exit_code"] == -1
    assert "timed out after 1 seconds" in result["error"]


@patch("subprocess.Popen")
def test_broken_pipe_after_deadline_is_timeout(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    mock_proc = MagicMock()
    mock_proc.stdin.write.side_effect = BrokenPipeError
    mock_proc.stdout = []
    mock_proc.poll.return_value = None
    mock_proc.communicate.return_value = ("", "")
    mock_popen.return_value = mock_proc

    class _ImmediateTimer:
        def __init__(self, _interval, function):
            self._function = function
            self.daemon = True

        def start(self):
            self._function()

        def cancel(self):
            return None

    monkeypatch.setattr(pi_runner.threading, "Timer", _ImmediateTimer)

    result = pi_run_task(prompt="big prompt", cwd="", timeout_s=10)

    assert result["status"] == "timeout"
    mock_popen.assert_called_once()


# -- Re-land of #867: the child env is an allowlist ---------------------------

# Every key the Pi child may ever see. Anything else in the hub process must
# stay behind, whatever its name.
_ALLOWED_CHILD_KEYS = {
    "PATH",
    "LANG",
    "UA_AGENT_LABEL",
    "HOME",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "OPENAI_API_KEY",
    "PI_API_KEY",
}

_SECRET_LOOKING_KEYS = (
    "UA_AGENT_TOKENS",
    "UA_AGENT_BEARER_TOKEN",
    "UA_EVIDENCE_HMAC_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "SLACK_BOT_TOKEN",
    "SOME_SERVICE_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "DATABASE_URL",
    "TAVILY_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
)


def test_build_env_is_an_allowlist_not_a_copy(monkeypatch, tmp_path):
    """Secret-looking parent vars never reach Pi; PATH, HOME and the label do."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("PI_API_KEY", "pi-ok")
    for key in _SECRET_LOOKING_KEYS:
        monkeypatch.setenv(key, f"sentinel-{key.lower()}")

    env = _build_env("label")

    assert set(env) <= _ALLOWED_CHILD_KEYS, set(env) - _ALLOWED_CHILD_KEYS
    for key in _SECRET_LOOKING_KEYS:
        assert key not in env
    assert not any(v.startswith("sentinel-") for v in env.values())
    assert env["HOME"] == str(home)
    assert env["UA_AGENT_LABEL"] == "label"
    assert env["PI_API_KEY"] == "pi-ok"
    assert "/usr/bin" in env["PATH"].split(":")
    # Pi is commonly installed into ~/.local/bin; the child keeps reaching it.
    assert str(home / ".local" / "bin") in env["PATH"].split(":")


def test_blocked_metered_keys_stay_out_even_if_allowlisted(monkeypatch):
    """The GEMINI/ANTHROPIC block survives a future allowlist edit."""
    monkeypatch.setenv("GEMINI_API_KEY", "gem-sentinel")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-sentinel")
    monkeypatch.setattr(
        pi_runner,
        "_FORWARDED_API_KEYS",
        pi_runner._FORWARDED_API_KEYS + ("GEMINI_API_KEY",),
    )
    monkeypatch.setattr(
        pi_runner,
        "_OPTIONAL_ENV_KEYS",
        pi_runner._OPTIONAL_ENV_KEYS + ("ANTHROPIC_API_KEY",),
    )

    env = _build_env("label")

    assert "GEMINI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


# -- Mid-run cancellation via kill_check (task #1525) ------------------------


class _BlockingStdout:
    """Stands in for Pi's stdout: never produces a line on its own, so the
    reading loop blocks exactly like it would on a real, still-running Pi
    process — until the watcher thread's `kill_check` fires and the fake
    process "dies", at which point iteration ends like a closed pipe would."""

    def __init__(self, killed_event):
        self._killed = killed_event

    def __iter__(self):
        return self

    def __next__(self):
        import time as _time

        while not self._killed.is_set():
            _time.sleep(0.01)
        raise StopIteration


class _FakeLiveProc:
    """A `Popen`-shaped fake that stays "running" (`poll() is None`) until
    `.kill()` is called, so the watcher thread in `pi_run_task` has something
    real to poll and terminate."""

    def __init__(self):
        import threading as _threading

        self._killed = _threading.Event()
        self.stdin = MagicMock()
        self.stdout = _BlockingStdout(self._killed)
        self.returncode = None

    def poll(self):
        return -9 if self._killed.is_set() else None

    def kill(self):
        self._killed.set()
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, timeout=None):
        return ("", "")


@patch("subprocess.Popen")
def test_pi_run_task_kill_check_stops_a_running_subprocess(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    fake_proc = _FakeLiveProc()
    mock_popen.return_value = fake_proc

    result = pi_run_task(
        prompt="long task",
        cwd="",
        timeout_s=30,
        kill_check=lambda: True,
        poll_interval_s=0.02,
    )

    assert result["status"] == "killed"
    assert result["error"].startswith("kill switch triggered")
    assert result["cleanup_confirmed"] is False  # This direct-child fake has no owned PGID.
    assert fake_proc._killed.is_set()


@patch("subprocess.Popen")
def test_pi_exit_during_kill_check_is_not_reported_as_killed(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)

    class FinishedDuringCheck(_FakeLiveProc):
        def poll(self):
            return 0 if self._killed.is_set() else None

        def kill(self):
            raise AssertionError("already completed Pi process must not be killed")

        def communicate(self, timeout=None):
            self.returncode = 0
            return ("", "")

    proc = FinishedDuringCheck()
    mock_popen.return_value = proc

    def finish_then_cancel():
        proc._killed.set()  # Releases stdout exactly as Pi's natural exit would.
        return True

    result = pi_run_task(
        prompt="completed task", cwd="", kill_check=finish_then_cancel, poll_interval_s=0.01
    )
    assert result["status"] == "success"


@patch("subprocess.Popen")
def test_pi_run_task_without_kill_check_ignores_a_would_be_kill_signal(
    mock_popen, tmp_path, monkeypatch
):
    """No `kill_check` given: PiBackend's own default when a run is
    unbounded. No watcher thread starts, so nothing ever calls `.kill()`."""
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    mock_proc = _mock_proc([json.dumps({"type": "agent_end"})])
    mock_proc.poll.return_value = 0
    mock_popen.return_value = mock_proc

    result = pi_run_task(prompt="quick task", cwd="", timeout_s=5)

    assert result["status"] == "success"
    mock_proc.kill.assert_not_called()


@patch("subprocess.Popen")
def test_spawn_failure_raises_tool_error(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    mock_popen.side_effect = OSError("pi disappeared")

    with pytest.raises(ToolError, match="Failed to spawn"):
        pi_run_task(prompt="spawn later", cwd="", timeout_s=10)


# -- PR #936 review fixes (task #1677) ---------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        "pkg@2026",
        "@earendil-works/pi-coding-agent@2026",
        "pkg@1",
        "pkg@1.2",
        "pkg@1.2.x",
        "pkg@01.2.3",
        "pkg@1.2.3.4",
        "pkg@1.2.3 || 2.0.0",
        "pkg@>=1.2.3",
    ],
)
def test_package_spec_needs_full_semver(spec):
    """A bare number is an npm range or dist-tag, so it floats (Devin, PR #936)."""
    assert not pi_runner._is_exact_package_spec(spec)


@pytest.mark.parametrize(
    "spec",
    [
        "pkg@1.2.3",
        "@earendil-works/pi-coding-agent@0.40.1",
        "pkg@1.2.3-beta.1",
        "pkg@1.2.3+build.5",
        "pkg@1.2.3-rc.1+sha.abc",
    ],
)
def test_package_spec_accepts_full_semver(spec):
    assert pi_runner._is_exact_package_spec(spec)


def test_resolve_pi_refuses_numeric_dist_tag(monkeypatch):
    _no_pi(monkeypatch)
    monkeypatch.setenv("UA_PI_PACKAGE", "@earendil-works/pi-coding-agent@2026")
    with pytest.raises(ToolError, match="exact version"):
        resolve_pi_executable()


_FAKE_PI = """\
import json, sys
sys.stdin.readline()
print(json.dumps({"type": "message_update", "update": {"kind": "text_delta", "text": "done"}}))
print(json.dumps({"type": "agent_end"}), flush=True)
# Pi in RPC mode keeps serving stdin until EOF.
sys.stdin.read()
"""


class _Py311Popen(subprocess.Popen):
    """CPython 3.11 ``communicate`` flushes stdin even when it is closed and
    raises ``ValueError``; 3.14 ignores it. Emulate 3.11 on any interpreter."""

    def communicate(self, input=None, timeout=None):
        if self.stdin is not None and self.stdin.closed and not self._communication_started:
            raise ValueError("I/O operation on closed file.")
        return super().communicate(input=input, timeout=timeout)


def test_completed_real_pi_run_returns_success(tmp_path, monkeypatch):
    """Pi emits ``agent_end`` and exits 0: the run must return, not raise
    ``ValueError`` from ``communicate`` on an already-closed stdin."""
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    script = tmp_path / "fake_pi.py"
    script.write_text(_FAKE_PI)
    monkeypatch.setattr(pi_runner, "resolve_pi_executable", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(pi_runner.subprocess, "Popen", _Py311Popen)

    result = pi_run_task(prompt="finish", cwd="", timeout_s=20)

    assert result["status"] == "success"
    assert result["text"] == "done"
    assert result["exit_code"] == 0


# The descendant inherits Pi's stdout and outlives (or outlasts) its parent.
_SPAWN_HOLDER = (
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_timeout_unblocks_when_a_descendant_holds_stdout(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        pi_runner,
        "resolve_pi_executable",
        lambda: [sys.executable, "-c", _SPAWN_HOLDER + "time.sleep(0.1)"],
    )

    started = time.monotonic()
    result = pi_run_task(prompt="leak a child", cwd="", timeout_s=1)

    assert time.monotonic() - started < 15
    assert result["status"] == "timeout"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_kill_check_unblocks_when_a_descendant_holds_stdout(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        pi_runner,
        "resolve_pi_executable",
        lambda: [sys.executable, "-c", _SPAWN_HOLDER + "time.sleep(60)"],
    )

    started = time.monotonic()
    result = pi_run_task(
        prompt="cancel me",
        cwd="",
        timeout_s=60,
        kill_check=lambda: time.monotonic() - started > 0.5,
        poll_interval_s=0.05,
    )

    assert time.monotonic() - started < 15
    assert result["status"] == "killed"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
@pytest.mark.parametrize("leader_delay_s", [0, 0.75])
def test_kill_check_after_pi_exit_unblocks_a_descendant_holding_stdout(
    tmp_path, monkeypatch, leader_delay_s
):
    """Pi itself exited, but a descendant still holds stdout: cancellation
    must still reach the descendant instead of waiting for the deadline."""
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    # Read the prompt before exiting so a fast leader cannot close stdin
    # before the runner writes. The delay exercises cancellation ordering
    # even when leader startup takes longer than the former 0.5s trigger.
    script = _SPAWN_HOLDER + f"sys.stdin.readline(); time.sleep({leader_delay_s})"
    leader_command = [sys.executable, "-c", script]
    monkeypatch.setattr(
        pi_runner,
        "resolve_pi_executable",
        lambda: list(leader_command),
    )
    actual_popen = subprocess.Popen
    leaders = []
    cancelled_after_exit = []

    def capture_leader(command, *args, **kwargs):
        process = actual_popen(command, *args, **kwargs)
        if command[:3] == leader_command:
            leaders.append(process)
        return process

    def cancel_after_exit():
        if leaders and leaders[0].poll() == 0:
            cancelled_after_exit.append(leaders[0].pid)
            return True
        return False

    monkeypatch.setattr(pi_runner.subprocess, "Popen", capture_leader)

    started = time.monotonic()
    result = pi_run_task(
        prompt="cancel after exit",
        cwd="",
        timeout_s=60,
        kill_check=cancel_after_exit,
        poll_interval_s=0.05,
    )

    assert time.monotonic() - started < 15
    # Pi itself finished before the cancel: its completed result stands, and
    # reaping the leftover descendant must not rewrite it as killed/timeout.
    assert result["status"] == "success", result
    assert result["exit_code"] == 0
    assert len(leaders) == 1
    assert cancelled_after_exit == [leaders[0].pid]
    assert result["cleanup_confirmed"] is True
    with pytest.raises(ProcessLookupError):
        os.killpg(leaders[0].pid, 0)


# -- Provider pin (task #2025) -------------------------------------------------


@patch("subprocess.Popen")
def test_provider_is_pinned_even_without_a_model(mock_popen, tmp_path, monkeypatch):
    """A stale ~/.pi/agent/settings.json naming `google` must never be used:
    every call passes --provider and --model, so Pi never reads the default."""
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    pi_home = tmp_path / "home" / ".pi" / "agent"
    pi_home.mkdir(parents=True)
    (pi_home / "settings.json").write_text(
        json.dumps({"defaultProvider": "google", "defaultModel": "gemini-3.1-pro-preview"})
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    mock_popen.return_value = _mock_proc([json.dumps({"type": "agent_end"})])

    pi_run_task(prompt="Say hello", cwd="", timeout_s=10)

    cmd = mock_popen.call_args[0][0]
    assert cmd[cmd.index("--provider") + 1] == "openai-codex"
    assert cmd[cmd.index("--model") + 1] == "gpt-5.5"
    assert not any("google" in arg or "gemini" in arg for arg in cmd)


@patch("subprocess.Popen")
def test_explicit_codex_model_is_passed_with_the_pin(mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    mock_popen.return_value = _mock_proc([json.dumps({"type": "agent_end"})])

    pi_run_task(prompt="Say hello", cwd="", model="openai-codex/gpt-5.5-mini", timeout_s=10)

    cmd = mock_popen.call_args[0][0]
    assert cmd[cmd.index("--provider") + 1] == "openai-codex"
    assert cmd[cmd.index("--model") + 1] == "gpt-5.5-mini"


@pytest.mark.parametrize("model", ["google/gemini-3.1-pro-preview", "anthropic/claude", "/x"])
@patch("subprocess.Popen")
def test_model_for_another_provider_is_refused_before_spawn(
    mock_popen, tmp_path, monkeypatch, model
):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)
    with pytest.raises(ToolError, match="openai-codex"):
        pi_run_task(prompt="Say hello", cwd="", model=model, timeout_s=10)
    mock_popen.assert_not_called()
