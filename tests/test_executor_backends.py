"""Tests for executor backend selection and the Codex backend (task #196).

Backend-level tests mock the subprocess boundary. The JSONL fixtures below
are copied from real `codex exec --json` output (codex-cli 0.145.0), so the
parser is tested against the vocabulary Codex actually emits rather than one
invented to match the parser. TestRunPopenWithKillCheck (task #383) is the
one exception: it spawns real Python subprocesses to exercise the actual
Popen/poll/kill-check mechanism, since that is the thing under test there.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from unified_agent import config
from unified_agent.dispatch import harness_sandbox
from unified_agent.executor import backends
from unified_agent.executor.daemon import ExecutorDaemon


def _posture(cli_backend: str):
    """A verified sandbox posture (conftest stubs a passing host probe, #1948)."""
    return harness_sandbox.resolve(cli_backend)


# Verbatim shapes observed from codex-cli 0.145.0.
REAL_EVENTS = "\n".join(
    [
        "Reading additional input from stdin...",  # non-JSON preamble line
        json.dumps({"type": "thread.started", "thread_id": "019fa5d7-374f"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.started", "item": {"id": "item_0", "type": "command_execution"}}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "command_execution",
                    "command": "echo hi",
                    "exit_code": 0,
                    "status": "completed",
                    "aggregated_output": "hi\n",
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_3",
                    "type": "file_change",
                    "changes": [{"path": "/tmp/cx-probe/f.txt", "kind": "update"}],
                    "status": "completed",
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_4", "type": "agent_message", "text": "done"},
            }
        ),
        json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 13661, "output_tokens": 5}}
        ),
    ]
)


@pytest.mark.parametrize(
    ("build_env", "agent_label"),
    [
        (backends.CodexBackend._child_env, "Desktop_Agent2"),
        (backends.AgyBackend._child_env, "Desktop_agent3"),
        (backends._child_env, "Desktop_Agent1"),
    ],
)
def test_child_env_uses_writable_tool_caches_and_overrides_parent(
    build_env, agent_label, monkeypatch, tmp_path
):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "caller-uv-cache"))
    monkeypatch.setenv("RUFF_CACHE_DIR", str(tmp_path / "caller-ruff-cache"))

    env = build_env(agent_label)

    assert env["UA_AGENT_LABEL"] == agent_label
    for key, leaf in backends.CHILD_CACHE_ENV.items():
        path = tmp_path / "unified-agent-executor-cache" / leaf
        assert env[key] == str(path)
        (path / "probe").write_text("ok", encoding="utf-8")
        assert (path / "probe").read_text(encoding="utf-8") == "ok"


_DUMMY_REGISTRY = (
    "Desktop_Agent2:dummy-codex-token,"
    "Desktop_agent3:dummy-agy-token,"
    "Desktop_Agent1:dummy-claude-token,"
    "Desktop_Agent4:dummy-cursor-token,"
    "Desktop_Agent6:dummy-copilot-token,"
    "Desktop_Agent7:dummy-devin-token"
)


@pytest.mark.parametrize(
    ("build_env", "agent_label", "expected_token"),
    [
        (backends.CodexBackend._child_env, "Desktop_Agent2", "dummy-codex-token"),
        (backends.AgyBackend._child_env, "Desktop_agent3", "dummy-agy-token"),
        (backends._child_env, "Desktop_Agent1", "dummy-claude-token"),
        (backends._child_env, "Desktop_Agent4", "dummy-cursor-token"),
        (backends._child_env, "Desktop_Agent6", "dummy-copilot-token"),
        (backends._child_env, "Desktop_Agent7", "dummy-devin-token"),
    ],
)
def test_child_env_binds_the_requested_agents_bearer_not_the_ambient_one(
    build_env, agent_label, expected_token, monkeypatch
):
    """Task #1216: a child spawned with another agent's ambient
    UA_AGENT_BEARER_TOKEN must be rebound to its own registry bearer."""
    monkeypatch.setenv("UA_AGENT_TOKENS", _DUMMY_REGISTRY)
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "ambient-wrong-token")
    monkeypatch.setenv("UA_EXECUTOR_AGENT", "Desktop_Agent5")
    monkeypatch.setenv("UA_EXECUTOR_TOKEN", "dummy-hermes-executor-token")

    env = build_env(agent_label)

    assert env["UA_AGENT_LABEL"] == agent_label
    assert env["UA_AGENT_BEARER_TOKEN"] == expected_token
    assert env["UA_AGENT_BEARER_TOKEN"] != "ambient-wrong-token"
    assert "UA_AGENT_TOKENS" not in env
    assert "UA_EXECUTOR_TOKEN" not in env


@pytest.mark.parametrize(
    ("build_env", "agent_label"),
    [
        (backends.CodexBackend._child_env, "Desktop_Agent2"),
        (backends.AgyBackend._child_env, "Desktop_agent3"),
        (backends._child_env, "Desktop_Agent7"),
    ],
)
def test_child_env_falls_back_to_same_label_executor_token(build_env, agent_label, monkeypatch):
    """An executor unit carrying only UA_EXECUTOR_AGENT + UA_EXECUTOR_TOKEN
    keeps auth for its own child — registry wins when present, but a
    same-label executor token is a legitimate fallback, not a leak."""
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "ambient-wrong-token")
    monkeypatch.setenv("UA_EXECUTOR_AGENT", agent_label)
    monkeypatch.setenv("UA_EXECUTOR_TOKEN", "dummy-own-executor-token")

    env = build_env(agent_label)

    assert env["UA_AGENT_BEARER_TOKEN"] == "dummy-own-executor-token"
    assert "UA_EXECUTOR_TOKEN" not in env


@pytest.mark.parametrize(
    "build_env",
    [
        backends.CodexBackend._child_env,
        backends.AgyBackend._child_env,
        backends._child_env,
    ],
)
def test_child_env_fails_closed_when_no_registry_bearer_resolves(build_env, monkeypatch):
    """An agent with no registry token gets NO bearer — never the ambient one."""
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "ambient-wrong-token")
    monkeypatch.setenv("UA_EXECUTOR_AGENT", "Desktop_Agent1")
    monkeypatch.setenv("UA_EXECUTOR_TOKEN", "dummy-claude-executor-token")

    env = build_env("Unknown_Agent")

    assert "UA_AGENT_BEARER_TOKEN" not in env
    assert "UA_EXECUTOR_TOKEN" not in env
    assert "UA_AGENT_TOKENS" not in env


@pytest.fixture
def primary_hub_url(monkeypatch):
    """A configured canonical hub URL for primary-client daemon construction.

    Task #1080 makes a primary client fail closed when its hub URL is not
    declared explicitly, so constructing an ``ExecutorDaemon`` for a Desktop_*
    persona requires ``UA_HTTP_HUB_URL``. The tests below are about backend
    selection and sandbox gating, not hub resolution, so they run against a
    valid configured hub instead of asserting the fail-closed path.
    """
    monkeypatch.setenv("UA_HTTP_HUB_URL", "https://hub.example.test/mcp")
    return "https://hub.example.test/mcp"


class TestBackendSelection:
    def test_unset_does_not_default_to_pi(self, monkeypatch):
        monkeypatch.delenv("UA_EXECUTOR_BACKEND", raising=False)
        assert config.executor_backend() == ""
        with pytest.raises(ValueError, match="executor backend not specified"):
            backends.get_backend()

    def test_label_selects_native_persona_cli(self, monkeypatch):
        monkeypatch.delenv("UA_EXECUTOR_BACKEND", raising=False)
        assert backends.get_backend(agent_label="Desktop_agent3").name == "agy"
        assert backends.get_backend(agent_label="Desktop_Agent1").name == "claude"
        assert backends.get_backend(agent_label="Desktop_Agent4").name == "cursor"
        assert backends.get_backend(agent_label="Desktop_Agent6").name == "copilot"
        assert backends.get_backend(agent_label="Desktop_Agent5").name == "hermes"
        assert backends.get_backend(agent_label="Desktop_Agent2").name == "codex"

    def test_explicit_pi_alias_still_available(self, monkeypatch):
        monkeypatch.setenv("UA_EXECUTOR_BACKEND", "pi")
        assert backends.get_backend().name == "pi"
        assert backends.get_backend(agent_label="Desktop_agent3").name == "pi"

    def test_selects_codex(self, monkeypatch):
        monkeypatch.setenv("UA_EXECUTOR_BACKEND", "codex")
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        assert backends.get_backend().name == "codex"

    def test_selects_agy(self, monkeypatch):
        monkeypatch.setenv("UA_EXECUTOR_BACKEND", "agy")
        assert backends.get_backend().name == "agy"

    def test_name_is_normalised(self, monkeypatch):
        monkeypatch.setenv("UA_EXECUTOR_BACKEND", "  CODEX  ")
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        assert backends.get_backend().name == "codex"

    def test_unknown_backend_raises_rather_than_defaulting(self, monkeypatch):
        """A typo must stop the daemon, not silently run Pi."""
        monkeypatch.setenv("UA_EXECUTOR_BACKEND", "codx")
        with pytest.raises(ValueError, match="unknown executor backend"):
            backends.get_backend()

    def test_daemon_defaults_to_label_persona(self, monkeypatch, primary_hub_url):
        monkeypatch.delenv("UA_EXECUTOR_BACKEND", raising=False)
        monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
        daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=True)
        assert daemon.backend.name == "agy"
        assert daemon.backend.session_kind == "agy_run"

    def test_daemon_cursor_persona_is_cursor_backend(self, monkeypatch, primary_hub_url):
        monkeypatch.delenv("UA_EXECUTOR_BACKEND", raising=False)
        monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
        daemon = ExecutorDaemon(agent_label="Desktop_Agent4", shadow_mode=True)
        assert daemon.backend.name == "cursor"
        assert daemon.backend.session_kind == "cursor_run"

    def test_daemon_copilot_persona_is_copilot_backend(self, monkeypatch, primary_hub_url):
        monkeypatch.delenv("UA_EXECUTOR_BACKEND", raising=False)
        monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
        daemon = ExecutorDaemon(agent_label="Desktop_Agent6", shadow_mode=True)
        assert daemon.backend.name == "copilot"
        assert daemon.backend.session_kind == "copilot_run"

    def test_daemon_accepts_injected_backend(self, monkeypatch, primary_hub_url):
        monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
        sentinel = backends.CodexBackend(sandbox="workspace-write")
        daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=True, backend=sentinel)
        assert daemon.backend is sentinel
        assert daemon.backend.session_kind == "codex_run"

    def test_backend_name_matches_dispatch_routes(self):
        from unified_agent.dispatch import routing

        for label, route in routing.all_routes().items():
            assert backends.backend_name_for_label(label) == route.cli_backend

    def test_server_labels_resolve_their_lowercase_family(self):
        """Mac hub executors are server_<token> (lowercase) — task #610."""
        assert backends.backend_name_for_label("server_agent1") == "claude"
        assert backends.backend_name_for_label("server_agent2") == "codex"
        assert backends.backend_name_for_label("server_agent3") == "agy"
        assert backends.backend_name_for_label("server_agent4") == "cursor"
        assert backends.backend_name_for_label("server_agent5") == "hermes"

    def test_lowercase_family_stays_unresolvable_off_the_server_host(self):
        """The relaxed lookup is scoped to server_*, not a global case fold."""
        for label in ("Desktop_agent1", "Laptop_agent4", "desktop_agent1"):
            with pytest.raises(ValueError, match="no native headless backend"):
                backends.backend_name_for_label(label)


class TestSandboxGating:
    def test_default_is_workspace_write(self, monkeypatch):
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        assert backends._resolve_codex_sandbox() == "workspace-write"

    def test_invalid_mode_raises(self, monkeypatch):
        monkeypatch.setenv("UA_CODEX_SANDBOX", "wide-open")
        with pytest.raises(ValueError, match="not a valid codex sandbox mode"):
            backends._resolve_codex_sandbox()

    def test_full_access_refused_without_second_optin(self, monkeypatch):
        monkeypatch.setenv("UA_CODEX_SANDBOX", "danger-full-access")
        monkeypatch.delenv("UA_CODEX_ALLOW_FULL_ACCESS", raising=False)
        with pytest.raises(ValueError, match="refusing to run codex"):
            backends._resolve_codex_sandbox()

    def test_full_access_allowed_with_explicit_optin(self, monkeypatch):
        monkeypatch.setenv("UA_CODEX_SANDBOX", "danger-full-access")
        monkeypatch.setenv("UA_CODEX_ALLOW_FULL_ACCESS", "1")
        assert backends._resolve_codex_sandbox() == "danger-full-access"

    def test_misconfigured_sandbox_stops_daemon_construction(self, monkeypatch, primary_hub_url):
        """Fail at startup, not on the first claimed task."""
        monkeypatch.setenv("UA_EXECUTOR_BACKEND", "codex")
        monkeypatch.setenv("UA_CODEX_SANDBOX", "danger-full-access")
        monkeypatch.delenv("UA_CODEX_ALLOW_FULL_ACCESS", raising=False)
        monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
        with pytest.raises(ValueError, match="refusing to run codex"):
            ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=True)


class TestCommandConstruction:
    def _git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

    def test_argv_shape(self, monkeypatch):
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        backend = backends.CodexBackend(codex_bin="codex-dev", model="gpt-5.5")
        argv = backend.build_command("do the thing", "/tmp/wt")
        assert argv == [
            "codex-dev",
            "exec",
            "--json",
            "--model",
            "gpt-5.5",
            "--sandbox",
            "workspace-write",
            "--cd",
            "/tmp/wt",
            "--",
            "do the thing",
        ]

    def test_linked_worktree_allows_repo_git_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        repo = tmp_path / "repo"
        worktree = tmp_path / "task-979"
        repo.mkdir()
        self._git(repo, "init")
        self._git(repo, "config", "user.email", "test@example.com")
        self._git(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("x\n")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "initial")
        self._git(repo, "worktree", "add", str(worktree), "-b", "task-979")

        argv = backends.CodexBackend().build_command("p", str(worktree))

        git_dir = str(repo / ".git")
        assert argv[argv.index("--add-dir") + 1] == git_dir
        assert "worktrees/task-979" not in argv

    def test_plain_checkout_keeps_argv_shape(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init")

        argv = backends.CodexBackend(codex_bin="codex-dev", model="gpt-5.5").build_command(
            "do the thing", str(repo)
        )

        assert argv == [
            "codex-dev",
            "exec",
            "--json",
            "--model",
            "gpt-5.5",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(repo),
            "--",
            "do the thing",
        ]

    def test_prompt_is_argv_not_stdin(self, monkeypatch):
        """Codex blocks reading a piped stdin; the prompt must be positional."""
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        argv = backends.CodexBackend().build_command("PROMPT-MARKER", "/tmp/wt")
        assert argv[-1] == "PROMPT-MARKER"

    def test_bin_and_model_from_env(self, monkeypatch):
        monkeypatch.setenv("UA_CODEX_BIN", "/custom/codex")
        monkeypatch.setenv("UA_CODEX_MODEL", "gpt-5.5-codex")
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        backend = backends.CodexBackend()
        argv = backend.build_command("p", "/tmp/wt")
        assert argv[0] == "/custom/codex"
        assert "gpt-5.5-codex" in argv

    def test_child_env_carries_agent_label(self):
        env = backends.CodexBackend._child_env("Desktop_Agent2")
        assert env["UA_AGENT_LABEL"] == "Desktop_Agent2"


class TestJsonlParsing:
    def test_parses_real_event_stream(self):
        out = backends.parse_codex_jsonl(REAL_EVENTS)
        assert out["text"] == "done"
        assert out["tools_used"] == ["shell:echo hi"]
        assert out["files_modified"] == ["/tmp/cx-probe/f.txt"]
        assert out["error"] is None
        # The non-JSON preamble line must not be counted as an event.
        assert out["events_count"] == 7

    def test_turn_failed_becomes_error(self):
        stream = "\n".join(
            [
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "turn.failed", "error": {"message": "model refused"}}),
            ]
        )
        assert backends.parse_codex_jsonl(stream)["error"] == "model refused"

    def test_missing_turn_completed_is_an_error(self):
        """A stream that just stops must not read as success."""
        stream = json.dumps({"type": "turn.started"})
        assert "no turn.completed" in backends.parse_codex_jsonl(stream)["error"]

    def test_malformed_lines_are_skipped(self):
        stream = "\n".join(["{not json", REAL_EVENTS])
        assert backends.parse_codex_jsonl(stream)["error"] is None

    def test_duplicate_file_paths_deduped(self):
        change = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [{"path": "/a.txt", "kind": "update"}],
            },
        }
        stream = "\n".join(
            [json.dumps(change), json.dumps(change), json.dumps({"type": "turn.completed"})]
        )
        assert backends.parse_codex_jsonl(stream)["files_modified"] == ["/a.txt"]

    def test_unknown_tool_item_types_are_forwarded(self):
        """Forward-compat: an MCP tool item this version didn't emit still lands."""
        stream = "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call"}}),
                json.dumps({"type": "turn.completed"}),
            ]
        )
        assert backends.parse_codex_jsonl(stream)["tools_used"] == ["mcp_tool_call"]


class TestCodexRun:
    """The subprocess boundary is mocked; no Codex process is spawned."""

    def _patch_run(self, monkeypatch, *, stdout="", stderr="", returncode=0, capture=None):
        def fake_run(argv, *, cwd, env, timeout_s, stdin, kill_check=None, poll_interval_s=1.0):
            if capture is not None:
                capture["argv"] = argv
                capture["kwargs"] = {"cwd": cwd, "env": env, "stdin": stdin}
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)

    def test_success_maps_to_contract(self, monkeypatch):
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        self._patch_run(monkeypatch, stdout=REAL_EVENTS)
        res = backends.CodexBackend().run(
            prompt="p",
            cwd=".",
            session_dir=".worktrees/sessions",
            session_id="task-1-session",
            agent_label="Desktop_Agent2",
            timeout_s=30,
        )
        assert res["status"] == "success"
        assert res["text"] == "done"
        assert res["exit_code"] == 0
        assert res["error"] is None
        # Same keys the daemon's failure policy reads off the Pi backend, plus
        # the harness usage the daemon reports to budgets (task #1917).
        assert set(res) == {
            "status",
            "text",
            "tools_used",
            "files_modified",
            "events_count",
            "exit_code",
            "error",
            "usage",
        }
        assert res["usage"]["input_tokens"] == 13661

    def test_stdin_is_closed(self, monkeypatch):
        """Regression guard: an inherited stdin makes codex exec hang."""
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        capture = {}
        self._patch_run(monkeypatch, stdout=REAL_EVENTS, capture=capture)
        backends.CodexBackend().run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_Agent2",
            timeout_s=30,
        )
        assert capture["kwargs"]["stdin"] is subprocess.DEVNULL

    def test_nonzero_exit_is_error(self, monkeypatch):
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        self._patch_run(monkeypatch, stdout=REAL_EVENTS, stderr="boom", returncode=2)
        res = backends.CodexBackend().run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_Agent2",
            timeout_s=30,
        )
        assert res["status"] == "error"
        assert res["error"] == "boom"
        assert res["exit_code"] == 2

    def test_timeout_preserves_partial_output(self, monkeypatch):
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)

        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 30, output=REAL_EVENTS)

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)
        res = backends.CodexBackend().run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_Agent2",
            timeout_s=30,
        )
        assert res["status"] == "timeout"
        assert res["exit_code"] == -1
        assert res["files_modified"] == ["/tmp/cx-probe/f.txt"]

    def test_kill_switch_reported_distinctly_from_timeout(self, monkeypatch):
        """task #383: a kill_check firing mid-run must not look like a timeout
        or a plain error — the daemon routes 'killed' straight to terminal
        instead of retrying it through the failure/backoff policy."""
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)

        def fake_run(argv, **kwargs):
            raise backends.KillSwitchTriggered(REAL_EVENTS, "")

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)
        res = backends.CodexBackend().run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_Agent2",
            timeout_s=30,
            kill_check=lambda: True,
        )
        assert res["status"] == "killed"
        assert res["exit_code"] == -1
        assert res["text"] == "done"  # partial stdout still parsed

    def test_missing_binary_is_error_not_crash(self, monkeypatch):
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)

        def fake_run(argv, **kwargs):
            raise FileNotFoundError("no such file")

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)
        res = backends.CodexBackend(codex_bin="nope").run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_Agent2",
            timeout_s=30,
        )
        assert res["status"] == "error"
        assert "not found" in res["error"]

    def test_cwd_resolved_against_workspace_root(self, monkeypatch):
        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        capture = {}
        self._patch_run(monkeypatch, stdout=REAL_EVENTS, capture=capture)
        backends.CodexBackend().run(
            prompt="p",
            cwd=".worktrees/task-196",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_Agent2",
            timeout_s=30,
        )
        expected = str((config.workspace_root() / ".worktrees/task-196").resolve())
        assert capture["kwargs"]["cwd"] == expected
        assert capture["argv"][capture["argv"].index("--cd") + 1] == expected


# Verbatim shapes captured from a real `agy -p ... --output-format
# stream-json` run (2026-08-10), except the terminal status flipped to
# SUCCESS: the live capture hit the stdin-read timeout quirk build_command's
# docstring warns about (status ERROR, "timeout waiting for response") even
# though the turn itself had already completed — a fresh success fixture
# with the same event shapes is more useful as ground truth than that quirk.
AGY_REAL_EVENTS = "\n".join(
    [
        json.dumps(
            {
                "event": "init",
                "conversation_id": "2ccc29e5",
                "init": {"cwd": "/home/you/dev/unified_agent", "tools": ["list_dir"]},
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_index": 0, "state": "DONE", "step_type": "user_input"},
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 3,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "list_dir",
                    "tool_info": {"name": "list_dir", "parameters": {"DirectoryPath": "/x"}},
                },
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 3,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "list_dir",
                    "duration_seconds": 0.04,
                    "tool_info": {"name": "list_dir", "parameters": {"DirectoryPath": "/x"}},
                },
            }
        ),
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "2ccc29e5",
                    "status": "SUCCESS",
                    "response": "3",
                    "duration_seconds": 3.4,
                    "num_turns": 1,
                    "usage": {"input_tokens": 51805, "output_tokens": 985},
                },
            }
        ),
    ]
)


class TestAgyCommandConstruction:
    def test_argv_shape_no_model_no_skip(self, monkeypatch):
        monkeypatch.delenv("UA_AGY_MODEL", raising=False)
        monkeypatch.delenv("UA_AGY_SKIP_PERMISSIONS", raising=False)
        backend = backends.AgyBackend(agy_bin="agy-dev")
        argv = backend.build_command("do the thing")
        assert argv == ["agy-dev", "-p", "do the thing", "--output-format", "stream-json"]

    def test_argv_includes_model_when_set(self, monkeypatch):
        monkeypatch.delenv("UA_AGY_SKIP_PERMISSIONS", raising=False)
        backend = backends.AgyBackend(model="claude-sonnet-4-6")
        argv = backend.build_command("p")
        assert argv[-2:] == ["--model", "claude-sonnet-4-6"]

    def test_argv_includes_skip_permissions_only_when_enabled(self, monkeypatch):
        backend = backends.AgyBackend(skip_permissions=True)
        argv = backend.build_command("p", sandbox_posture=_posture("agy"))
        assert argv[-2:] == ["--sandbox", "--dangerously-skip-permissions"]

    def test_skip_permissions_off_by_default(self, monkeypatch):
        monkeypatch.delenv("UA_AGY_SKIP_PERMISSIONS", raising=False)
        assert backends.AgyBackend().skip_permissions is False

    def test_skip_permissions_from_env(self, monkeypatch):
        monkeypatch.setenv("UA_AGY_SKIP_PERMISSIONS", "1")
        assert backends.AgyBackend().skip_permissions is True

    def test_bin_and_model_from_env(self, monkeypatch):
        monkeypatch.setenv("UA_AGY_BIN", "/custom/agy")
        monkeypatch.setenv("UA_AGY_MODEL", "gemini-3.6-flash-high")
        backend = backends.AgyBackend()
        argv = backend.build_command("p")
        assert argv[0] == "/custom/agy"
        assert "gemini-3.6-flash-high" in argv

    def test_child_env_carries_agent_label(self):
        env = backends.AgyBackend._child_env("Desktop_agent3")
        assert env["UA_AGENT_LABEL"] == "Desktop_agent3"


class TestAgyStreamJsonParsing:
    def test_parses_real_event_stream(self):
        out = backends.parse_agy_stream_json(AGY_REAL_EVENTS)
        assert out["text"] == "3"
        assert out["tools_used"] == ["list_dir"]
        assert out["error"] is None
        assert out["events_count"] == 5

    def test_error_status_becomes_error(self):
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "ERROR", "response": "", "error": "boom"},
                    }
                )
            ]
        )
        assert backends.parse_agy_stream_json(stream)["error"] == "boom"

    def test_missing_result_is_an_error(self):
        stream = json.dumps({"event": "init", "init": {}})
        assert "no result event" in backends.parse_agy_stream_json(stream)["error"]

    def test_malformed_lines_are_skipped(self):
        stream = "\n".join(["{not json", AGY_REAL_EVENTS])
        assert backends.parse_agy_stream_json(stream)["error"] is None

    def test_write_tool_files_modified_best_effort(self):
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "step_type": "tool",
                            "tool_name": "write_to_file",
                            "tool_info": {"parameters": {"TargetFile": "/a.txt"}},
                        },
                    }
                ),
                json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}),
            ]
        )
        out = backends.parse_agy_stream_json(stream)
        assert out["files_modified"] == ["/a.txt"]
        assert out["tools_used"] == ["write_to_file"]

    def test_non_tool_step_updates_ignored(self):
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"state": "DONE", "step_type": "agent_response"},
                    }
                ),
                json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}),
            ]
        )
        assert backends.parse_agy_stream_json(stream)["tools_used"] == []


# Fixture pattern matching live agy print-mode teardown (task #1158): the CLI
# backgrounds work, waits ~5s idle, kills background tasks, and may still stamp
# SUCCESS. stream-json is already on argv; detection is what defeats the lie.
AGY_TEARDOWN_RACE_STDOUT = "\n".join(
    [
        json.dumps({"event": "init", "init": {"cwd": "/tmp"}}),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                },
            }
        ),
        "root agent idle; waiting up to 5s for 1 background task(s)",
        "terminating 1 background task(s) on exit",
        json.dumps(
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": ("I have launched the test run and will wait for it to complete"),
                    "error": "",
                },
            }
        ),
    ]
)


class TestAgyBackgroundTeardownDetection:
    def test_detects_idle_drain_signatures(self):
        msg = backends.detect_agy_background_teardown(AGY_TEARDOWN_RACE_STDOUT)
        assert msg is not None
        assert backends.AGY_BACKGROUND_TEARDOWN_REASON in msg

    def test_clean_success_stream_is_not_teardown(self):
        assert backends.detect_agy_background_teardown(AGY_REAL_EVENTS) is None

    def test_parser_alone_still_reports_success_on_teardown_fixture(self):
        """Reproduce: without detection, SUCCESS+teardown looks like progress."""
        parsed = backends.parse_agy_stream_json(AGY_TEARDOWN_RACE_STDOUT)
        assert parsed["error"] is None
        assert "launched the test run" in parsed["text"]


class TestAgyRun:
    """The subprocess boundary is mocked; no agy process is spawned."""

    def _patch_run(self, monkeypatch, *, stdout="", stderr="", returncode=0, capture=None):
        def fake_run(argv, *, cwd, env, timeout_s, stdin, kill_check=None, poll_interval_s=1.0):
            if capture is not None:
                capture["argv"] = argv
                capture["kwargs"] = {"cwd": cwd, "env": env, "stdin": stdin}
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)

    def test_success_maps_to_contract(self, monkeypatch):
        self._patch_run(monkeypatch, stdout=AGY_REAL_EVENTS)
        res = backends.AgyBackend().run(
            prompt="p",
            cwd=".",
            session_dir=".worktrees/sessions",
            session_id="task-1-session",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert res["status"] == "success"
        assert res["text"] == "3"
        assert res["exit_code"] == 0
        assert res["error"] is None
        assert set(res) == {
            "status",
            "text",
            "tools_used",
            "files_modified",
            "events_count",
            "exit_code",
            "error",
            "resume_count",
            "usage",
        }
        assert res["resume_count"] == 0
        assert res["usage"]["output_tokens"] == 985

    def test_teardown_race_fixture_is_error_not_success(self, monkeypatch):
        """Repro + fix: SUCCESS result after idle drain must not pass as success."""
        self._patch_run(monkeypatch, stdout=AGY_TEARDOWN_RACE_STDOUT)
        res = backends.AgyBackend().run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert res["status"] == "error"
        assert backends.AGY_BACKGROUND_TEARDOWN_REASON in (res["error"] or "")
        assert "stream-json" in " ".join(
            backends.AgyBackend().build_command("p")
        )  # stream-json always on; detection is the real fix

    def test_teardown_race_via_real_spawn_monitor_path(self, tmp_path, monkeypatch):
        """Exercise run_popen_with_kill_check against a fixture binary.

        The live agy binary is not required — this script emits the same
        stdout pattern the CLI prints when it drains background tasks.
        """
        fake = tmp_path / "fake-agy"
        # Ignore argv; always emit the teardown race pattern then exit 0.
        fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{AGY_TEARDOWN_RACE_STDOUT}\nEOF\n")
        fake.chmod(0o755)
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        (tmp_path / ".keep").write_text("")
        res = backends.AgyBackend(agy_bin=str(fake)).run(
            prompt="run pytest",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert res["status"] == "error"
        assert backends.AGY_BACKGROUND_TEARDOWN_REASON in (res["error"] or "")
        assert res["exit_code"] == 0  # CLI exited cleanly; we still refuse SUCCESS

    def test_stdin_is_closed(self, monkeypatch):
        """Regression guard: an inherited stdin makes agy print-mode report a
        false timeout even after the turn already completed (see
        AgyBackend.build_command docstring)."""
        capture = {}
        self._patch_run(monkeypatch, stdout=AGY_REAL_EVENTS, capture=capture)
        backends.AgyBackend().run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert capture["kwargs"]["stdin"] is subprocess.DEVNULL

    def test_nonzero_exit_is_error(self, monkeypatch):
        self._patch_run(monkeypatch, stdout=AGY_REAL_EVENTS, stderr="boom", returncode=2)
        res = backends.AgyBackend().run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert res["status"] == "error"
        assert res["exit_code"] == 2

    def test_timeout_preserves_partial_output(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 30, output=AGY_REAL_EVENTS)

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)
        res = backends.AgyBackend().run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert res["status"] == "timeout"
        assert res["exit_code"] == -1
        assert res["tools_used"] == ["list_dir"]

    def test_kill_switch_reported_distinctly_from_timeout(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise backends.KillSwitchTriggered(AGY_REAL_EVENTS, "")

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)
        res = backends.AgyBackend().run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
            kill_check=lambda: True,
        )
        assert res["status"] == "killed"
        assert res["exit_code"] == -1
        assert res["text"] == "3"  # partial stdout still parsed

    def test_missing_binary_is_error_not_crash(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise FileNotFoundError("no such file")

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)
        res = backends.AgyBackend(agy_bin="nope").run(
            prompt="p",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert res["status"] == "error"
        assert "not found" in res["error"]

    def test_no_cd_flag_cwd_set_via_subprocess_kwarg(self, monkeypatch):
        """agy has no --cd flag (unlike Codex) — cwd must go through subprocess.run's cwd kwarg."""
        capture = {}
        self._patch_run(monkeypatch, stdout=AGY_REAL_EVENTS, capture=capture)
        backends.AgyBackend().run(
            prompt="p",
            cwd=".worktrees/task-318",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        expected = str((config.workspace_root() / ".worktrees/task-318").resolve())
        assert capture["kwargs"]["cwd"] == expected
        assert "--cd" not in capture["argv"]


class TestRunPopenWithKillCheck:
    """task #383: real subprocesses, no mocking — this is the mechanism that
    makes the kill switch instant, so it is tested against a real child
    process rather than through backend-level mocks."""

    def _python(self, code: str) -> list[str]:
        return [sys.executable, "-c", code]

    def test_normal_completion_returns_completed_process(self):
        res = backends.run_popen_with_kill_check(
            self._python("print('hi')"),
            cwd=".",
            env=dict(os.environ),
            timeout_s=10,
            poll_interval_s=0.05,
        )
        assert res.returncode == 0
        assert res.stdout.strip() == "hi"

    def test_timeout_raises_and_kills_child(self):
        with pytest.raises(subprocess.TimeoutExpired):
            backends.run_popen_with_kill_check(
                self._python("import time; time.sleep(5)"),
                cwd=".",
                env=dict(os.environ),
                timeout_s=1,
                poll_interval_s=0.05,
            )

    def test_kill_check_stops_child_before_timeout(self):
        """The whole point of task #383: kill_check firing stops the child in
        one poll interval, not after waiting out the (much longer) timeout."""

        started = time.monotonic()
        with pytest.raises(backends.KillSwitchTriggered):
            backends.run_popen_with_kill_check(
                self._python("import time; time.sleep(30)"),
                cwd=".",
                env=dict(os.environ),
                timeout_s=30,
                poll_interval_s=0.05,
                kill_check=lambda: True,
            )
        elapsed = time.monotonic() - started
        assert elapsed < 2, (
            f"kill_check took {elapsed}s to stop the child, expected well under timeout_s"
        )

    def test_kill_switch_carries_partial_stdout(self):
        """Regression guard, not a race: kill_check=lambda: True fires on the
        FIRST poll, which can beat the child's own Python interpreter
        startup under load (observed flaky: the process gets terminated
        before it ever reaches its print). Gate the kill on wall-clock time
        instead, giving the child a generous real margin to start, print,
        and flush before termination — the assertion is about partial
        output surviving a kill, not about killing as early as possible."""
        started = time.monotonic()

        def kill_after_child_has_started():
            return time.monotonic() - started > 0.5

        with pytest.raises(backends.KillSwitchTriggered) as excinfo:
            backends.run_popen_with_kill_check(
                self._python("import sys, time; print('partial', flush=True); time.sleep(30)"),
                cwd=".",
                env=dict(os.environ),
                timeout_s=30,
                poll_interval_s=0.05,
                kill_check=kill_after_child_has_started,
            )
        assert "partial" in excinfo.value.stdout

    def test_large_output_does_not_deadlock(self):
        """Regression guard: draining stdout/stderr on background threads
        (rather than after Popen.wait()) is what prevents a chatty child from
        filling the OS pipe buffer and hanging both sides forever."""
        res = backends.run_popen_with_kill_check(
            self._python("print('x' * 200_000)"),
            cwd=".",
            env=dict(os.environ),
            timeout_s=10,
            poll_interval_s=0.05,
        )
        assert res.returncode == 0
        assert len(res.stdout) >= 200_000


class TestRunStallDetection:
    """Task #1916: a live child that stops producing output is a stalled run.

    Received stdout/stderr is the progress signal every streaming backend
    already emits (codex --json, agy/claude/cursor stream-json). No new bytes
    for the stall window terminates the child as a timeout, which the daemon
    routes to retry-with-backoff like any other failed attempt.
    """

    def _python(self, code: str) -> list[str]:
        return [sys.executable, "-c", code]

    def test_silent_child_is_stopped_as_stalled(self):
        started = time.monotonic()
        with pytest.raises(backends.RunStalled) as excinfo:
            backends.run_popen_with_kill_check(
                self._python("import time; print('start', flush=True); time.sleep(30)"),
                cwd=".",
                env=dict(os.environ),
                timeout_s=30,
                poll_interval_s=0.05,
                stall_timeout_s=1,
            )
        assert time.monotonic() - started < 10
        # Same shape as a timeout, so every backend's handler still applies.
        assert isinstance(excinfo.value, subprocess.TimeoutExpired)
        assert "start" in excinfo.value.stdout

    def test_chatty_child_is_not_stalled(self):
        res = backends.run_popen_with_kill_check(
            self._python(
                # Output every 0.1 s for 0.8 s: gaps well inside the stall
                # timeout, the whole run longer than it.
                "import time\nfor i in range(8):\n    print(i, flush=True)\n    time.sleep(0.1)"
            ),
            cwd=".",
            env=dict(os.environ),
            timeout_s=30,
            poll_interval_s=0.05,
            stall_timeout_s=0.5,
        )
        assert res.returncode == 0

    @pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
    @pytest.mark.parametrize("ending", ["", "\n", "\r"], ids=["unterminated", "newline", "cr"])
    def test_flushed_output_is_activity_without_waiting_for_a_line(self, stream_name, ending):
        fragment = "fragment" + ending
        res = backends.run_popen_with_kill_check(
            self._python(
                "import sys, time\n"
                f"stream = sys.{stream_name}\n"
                # 0.45 s of fragments 0.03 s apart, against a 0.3 s stall timeout.
                "for _ in range(15):\n"
                f"    stream.write({fragment!r})\n"
                "    stream.flush()\n"
                "    time.sleep(0.03)\n"
            ),
            cwd=".",
            env=dict(os.environ),
            timeout_s=10,
            poll_interval_s=0.02,
            stall_timeout_s=0.3,
        )
        assert res.returncode == 0
        # Keep subprocess text=True's existing universal-newline behavior.
        assert getattr(res, stream_name) == fragment.replace("\r", "\n") * 15
        assert getattr(res, "stderr" if stream_name == "stdout" else "stdout") == ""

    def test_truly_silent_child_is_stopped(self):
        started = time.monotonic()
        with pytest.raises(backends.RunStalled) as excinfo:
            backends.run_popen_with_kill_check(
                self._python("import time; time.sleep(30)"),
                cwd=".",
                env=dict(os.environ),
                timeout_s=10,
                poll_interval_s=0.02,
                stall_timeout_s=0.5,
            )
        assert time.monotonic() - started < 5
        assert excinfo.value.stdout == excinfo.value.stderr == ""

    @pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
    def test_split_utf8_bytes_are_activity_before_a_character_completes(
        self, stream_name, monkeypatch
    ):
        # Pin the real pipe decoder rather than relying on the host locale.
        # The child still runs and writes individual bytes to the OS pipe.
        real_popen = subprocess.Popen

        def utf8_popen(*args, **kwargs):
            kwargs["encoding"] = "utf-8"
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", utf8_popen)
        text = "\u20ac\U0001f642"
        res = backends.run_popen_with_kill_check(
            self._python(
                "import sys, time\n"
                f"stream = sys.{stream_name}.buffer\n"
                f"for byte in {text.encode('utf-8')!r}:\n"
                "    stream.write(bytes([byte]))\n"
                "    stream.flush()\n"
                "    time.sleep(0.2)\n"
            ),
            cwd=".",
            env=dict(os.environ),
            timeout_s=10,
            poll_interval_s=0.02,
            stall_timeout_s=0.5,
        )
        assert res.returncode == 0
        assert getattr(res, stream_name) == text
        assert getattr(res, "stderr" if stream_name == "stdout" else "stdout") == ""

    @pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
    def test_unterminated_output_is_preserved_when_child_goes_silent(self, stream_name):
        with pytest.raises(backends.RunStalled) as excinfo:
            backends.run_popen_with_kill_check(
                self._python(
                    f"import sys, time; sys.{stream_name}.write('partial'); "
                    f"sys.{stream_name}.flush(); time.sleep(30)"
                ),
                cwd=".",
                env=dict(os.environ),
                timeout_s=10,
                poll_interval_s=0.02,
                stall_timeout_s=0.5,
            )
        assert getattr(excinfo.value, stream_name) == "partial"

    def test_stall_window_defaults_from_config(self, monkeypatch):
        monkeypatch.setenv("UA_EXECUTOR_STALL_TIMEOUT_S", "1")
        with pytest.raises(backends.RunStalled):
            backends.run_popen_with_kill_check(
                self._python("import time; time.sleep(30)"),
                cwd=".",
                env=dict(os.environ),
                timeout_s=30,
                poll_interval_s=0.05,
            )

    def test_zero_disables_stall_detection(self, monkeypatch):
        monkeypatch.setenv("UA_EXECUTOR_STALL_TIMEOUT_S", "0")
        assert config.executor_stall_timeout_s() == 0
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            backends.run_popen_with_kill_check(
                self._python("import time; time.sleep(30)"),
                cwd=".",
                env=dict(os.environ),
                timeout_s=1,
                poll_interval_s=0.05,
            )
        assert not isinstance(excinfo.value, backends.RunStalled)

    def test_config_default_is_1800(self, monkeypatch):
        monkeypatch.delenv("UA_EXECUTOR_STALL_TIMEOUT_S", raising=False)
        assert config.executor_stall_timeout_s() == 1800
        monkeypatch.setenv("UA_EXECUTOR_STALL_TIMEOUT_S", "nonsense")
        assert config.executor_stall_timeout_s() == 1800

    def test_backend_reports_a_stall_as_timeout_naming_the_stall(self, monkeypatch):
        monkeypatch.setenv("UA_EXECUTOR_STALL_TIMEOUT_S", "1")
        res = backends._run_native_cli(
            argv=self._python("import time; time.sleep(30)"),
            cwd_abs=".",
            env=dict(os.environ),
            timeout_s=30,
            kill_check=None,
            parse_stdout=lambda out: {
                "text": out,
                "tools_used": [],
                "files_modified": [],
                "events_count": 0,
            },
            bin_name="fake",
        )
        assert res["status"] == "timeout"
        assert "stalled" in res["error"]


class TestPersonaHeadlessBackends:
    def test_claude_print_argv(self, monkeypatch):
        monkeypatch.delenv("UA_CLAUDE_SKIP_PERMISSIONS", raising=False)
        argv = backends.ClaudeBackend(claude_bin="claude").build_command("fix it")
        assert argv == [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--",
            "fix it",
        ]

    def test_claude_skip_permissions_flag(self):
        from unified_agent.dispatch.harness_sandbox import HarnessSandboxUnavailable

        with pytest.raises(HarnessSandboxUnavailable, match="harness_sandbox_unavailable:claude"):
            backends.ClaudeBackend(skip_permissions=True).build_command("x")

    def test_cursor_uses_agent_with_hub_and_print(self, monkeypatch):
        monkeypatch.delenv("UA_CURSOR_SKIP_PERMISSIONS", raising=False)
        monkeypatch.delenv("UA_CURSOR_BIN", raising=False)
        argv = backends.CursorBackend().build_command(
            "fix it", cwd="/tmp/wt", sandbox_posture=_posture("cursor")
        )
        assert argv[0] == "agent-with-hub"
        assert "-p" in argv
        assert "--trust" in argv
        assert argv[argv.index("--trust") - 2 : argv.index("--trust")] == ["--sandbox", "enabled"]
        assert argv[-1] == "fix it"
        assert "--workspace" in argv

    def test_copilot_uses_copilot_with_hub_and_json(self, monkeypatch):
        monkeypatch.delenv("UA_COPILOT_SKIP_PERMISSIONS", raising=False)
        monkeypatch.delenv("UA_COPILOT_BIN", raising=False)
        argv = backends.CopilotBackend().build_command(
            "fix it", cwd="/tmp/wt", sandbox_posture=_posture("copilot")
        )
        assert argv[0] == "copilot-with-hub"
        assert "--output-format" in argv
        assert "json" in argv
        assert "--allow-all-tools" in argv
        assert argv[argv.index("--allow-all-tools") - 1] == "--sandbox"
        assert argv[-1] == "--prompt=fix it"
        assert "-C" in argv

    def test_devin_uses_devin_with_hub_by_default(self, monkeypatch):
        """Task #1237: DEFAULT_DEVIN_BIN isolates Desktop_Agent7's bearer the
        same way Cursor and Copilot already do (defense-in-depth)."""
        monkeypatch.delenv("UA_DEVIN_BIN", raising=False)
        monkeypatch.delenv("UA_DEVIN_PERMISSION_MODE", raising=False)
        argv = backends.DevinBackend().build_command("fix it")
        assert argv[0] == "devin-with-hub"
        assert "--permission-mode" in argv
        assert argv[-1] == "--print=fix it"

    def test_devin_honors_ua_devin_bin_override(self, monkeypatch):
        monkeypatch.setenv("UA_DEVIN_BIN", "/custom/devin")
        argv = backends.DevinBackend().build_command("fix it")
        assert argv[0] == "/custom/devin"

    def test_parse_copilot_jsonl_result(self):
        stream = "\n".join(
            [
                json.dumps({"type": "tool_call", "name": "fs_read"}),
                json.dumps({"type": "result", "result": "shipped", "is_error": False}),
            ]
        )
        out = backends.parse_copilot_jsonl(stream)
        assert out["text"] == "shipped"
        assert out["error"] is None
        assert "fs_read" in out["tools_used"]

    def test_parse_copilot_jsonl_nested_sdk_events(self):
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant.message",
                        "data": {"content": "planning the fix"},
                    }
                ),
                json.dumps(
                    {
                        "type": "tool.execution_start",
                        "data": {"toolName": "fs_read", "arguments": {"path": "x"}},
                    }
                ),
            ]
        )
        out = backends.parse_copilot_jsonl(stream)
        assert out["text"] == "planning the fix"
        assert out["error"] is None
        assert "fs_read" in out["tools_used"]

    def test_parse_copilot_jsonl_session_error(self):
        stream = json.dumps(
            {
                "type": "session.error",
                "data": {"message": "authentication failed: invalid bearer token"},
            }
        )
        out = backends.parse_copilot_jsonl(stream)
        assert out["error"] == "authentication failed: invalid bearer token"
        assert out["text"] == ""

    def test_hermes_oneshot_argv(self, monkeypatch):
        monkeypatch.delenv("UA_HERMES_SKIP_PERMISSIONS", raising=False)
        argv = backends.HermesBackend(hermes_bin="hermes").build_command("fix it", cwd="/tmp/wt")
        assert argv[:3] == ["hermes", "--in", "/tmp/wt"]
        assert argv[-1] == "--oneshot=fix it"
        assert "--oneshot" not in argv

    def test_parse_claude_result_event(self):
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "working"}]},
                    }
                ),
                json.dumps({"type": "result", "result": "done", "is_error": False}),
            ]
        )
        out = backends.parse_claude_stream_json(stream)
        assert out["text"] == "done"
        assert out["error"] is None

    def test_parse_hermes_oneshot_stdout(self):
        out = backends.parse_hermes_oneshot("shipped the fix\n")
        assert out["text"] == "shipped the fix"
        assert out["error"] is None


class TestPromptIsNeverAFlag:
    """``--`` ends options before the prompt (claude -p, codex exec, codex exec resume)."""

    def test_claude_and_codex_put_double_dash_before_the_prompt(self, tmp_path):
        from unified_agent.dispatch.routing import headless_argv

        claude = backends.ClaudeBackend(claude_bin="claude", skip_permissions=False)
        assert claude.build_command("--dangerously-skip-permissions")[-2:] == [
            "--",
            "--dangerously-skip-permissions",
        ]
        codex = backends.CodexBackend(codex_bin="codex", model="m", sandbox="read-only")
        assert codex.build_command("--sandbox=danger-full-access", str(tmp_path))[-2:] == [
            "--",
            "--sandbox=danger-full-access",
        ]
        assert codex.build_command("-x", str(tmp_path), resume_session_id="abc-1")[-4:] == [
            "resume",
            "abc-1",
            "--",
            "-x",
        ]
        assert headless_argv("claude", "claude", prompt="-p")[-2:] == ["--", "-p"]
        assert headless_argv("codex", "codex", prompt="--cd=/")[-2:] == ["--", "--cd=/"]

    def test_codex_refuses_a_stdin_prompt(self, tmp_path):
        from unified_agent.dispatch.routing import headless_argv

        codex = backends.CodexBackend(codex_bin="codex", model="m", sandbox="read-only")
        with pytest.raises(ValueError):
            codex.build_command(" - ", str(tmp_path))
        with pytest.raises(ValueError):
            headless_argv("codex", "codex", prompt="-")


class TestPromptIsNeverAFlagOtherHarnesses:
    """Cursor, hermes, copilot and devin cannot be steered by a flag-shaped prompt."""

    HOSTILE = ["--force", "--yolo", "-p", "--model=x", "  --trust", "-"]

    @pytest.mark.parametrize("prompt", HOSTILE)
    def test_cursor_prompt_follows_double_dash(self, prompt):
        from unified_agent.dispatch.routing import headless_argv

        argv = headless_argv(
            "cursor", "agent", prompt=prompt, cwd="/tmp/wt", sandbox_posture=_posture("cursor")
        )
        assert argv[-2:] == ["--", prompt]
        # Exactly one separator, and the prompt is the only token after it.
        assert argv.count("--") == 1
        assert argv.index("--") == len(argv) - 2
        backend_argv = backends.CursorBackend(cursor_bin="agent").build_command(
            prompt, "/tmp/wt", sandbox_posture=_posture("cursor")
        )
        assert backend_argv[-2:] == ["--", prompt]

    # Known subcommands plus names a future CLI version might add: the guard
    # does not depend on any one version's subcommand list.
    @pytest.mark.parametrize(
        "name", ["logout", "login", "update", "status", "mcp", "help", "newcmd", "v2", "sync-all"]
    )
    def test_cursor_refuses_a_prompt_that_names_a_subcommand(self, name):
        from unified_agent.dispatch.routing import headless_argv

        posture = _posture("cursor")
        with pytest.raises(ValueError, match="subcommand"):
            headless_argv("cursor", "agent", prompt=name, sandbox_posture=posture)
        # A multi-word prompt that merely starts with the name is a normal prompt.
        argv = headless_argv("cursor", "agent", prompt=f"{name} the cache", sandbox_posture=posture)
        assert argv[-1] == f"{name} the cache"

    @pytest.mark.parametrize("prompt", HOSTILE)
    def test_hermes_binds_the_prompt_to_oneshot_with_equals(self, prompt):
        from unified_agent.dispatch.routing import headless_argv

        argv = headless_argv("hermes", "hermes", prompt=prompt, cwd="/tmp/wt")
        assert argv[-1] == f"--oneshot={prompt}"
        assert prompt not in argv  # never a standalone token a parser could read
        assert "--oneshot" not in argv
        backend_argv = backends.HermesBackend(hermes_bin="hermes").build_command(prompt, "/tmp/wt")
        assert backend_argv[-1] == f"--oneshot={prompt}"

    def test_hermes_observable_form_keeps_bare_oneshot(self):
        from unified_agent.dispatch.routing import headless_argv

        assert headless_argv("hermes", "hermes")[-1] == "--oneshot"

    FLAG_FORM = {"copilot": "--prompt=", "devin": "--print="}

    @pytest.mark.parametrize("cli", ["copilot", "devin"])
    @pytest.mark.parametrize(
        "prompt", ["--force", "-p", "--model=x", "  --yolo", "\n- item", "-", "---\nfront: matter"]
    )
    def test_flag_value_harnesses_bind_the_prompt_with_equals(self, cli, prompt):
        from unified_agent.dispatch.routing import headless_argv

        argv = headless_argv(cli, cli, prompt=prompt, sandbox_posture=_posture(cli))
        assert argv[-1] == f"{self.FLAG_FORM[cli]}{prompt}"
        assert prompt not in argv  # never a standalone token a parser could read
        assert "-p" not in argv

    def test_copilot_and_devin_backends_bind_a_leading_dash_prompt(self):
        argv = backends.CopilotBackend().build_command(
            "--allow-all", sandbox_posture=_posture("copilot")
        )
        assert argv[-1] == "--prompt=--allow-all"
        assert "--allow-all" not in argv
        argv = backends.DevinBackend().build_command("--permission-mode dangerous")
        assert argv[-1] == "--print=--permission-mode dangerous"
        assert argv.count("--permission-mode") == 1

    @pytest.mark.parametrize("cli", ["copilot", "devin"])
    def test_flag_value_harnesses_pass_an_ordinary_prompt_unchanged(self, cli):
        from unified_agent.dispatch.routing import headless_argv

        argv = headless_argv(cli, cli, prompt="fix a - b", sandbox_posture=_posture(cli))
        assert argv[-1] == f"{self.FLAG_FORM[cli]}fix a - b"

    def test_agy_prompt_is_the_value_of_dash_p(self):
        """agy's ``-p`` binds the next token even when it is flag-shaped (live
        soak: ``-p --output-format`` consumed ``--output-format`` as the prompt)."""
        from unified_agent.dispatch.routing import headless_argv

        argv = headless_argv("agy", "agy", prompt="--dangerously-skip-permissions")
        assert argv[:3] == ["agy", "-p", "--dangerously-skip-permissions"]
        build = backends.AgyBackend(agy_bin="agy").build_command("--model=x")
        assert build[:3] == ["agy", "-p", "--model=x"]


@pytest.mark.parametrize(
    "setting, getter, default",
    [
        ("UA_EXECUTOR_RECLAIM_INTERVAL_S", config.executor_reclaim_interval_s, 300),
        ("UA_EXECUTOR_STALL_TIMEOUT_S", config.executor_stall_timeout_s, 1800),
        ("UA_EXECUTOR_BACKOFF_CAP_S", config.executor_backoff_cap_s, 3600),
    ],
)
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_nonfinite_executor_timing_uses_default(monkeypatch, setting, getter, default, value):
    monkeypatch.setenv(setting, value)
    assert getter() == default


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_stall_kills_harness_tool_descendants(tmp_path):
    marker = tmp_path / "surviving-tool"
    pidfile = tmp_path / "tool.pid"
    # The tool records its own pid (atomically). The harness keeps printing
    # until that record exists, so the stall cannot fire before the pid is
    # known, then goes silent.
    child = (
        "import os, time; from pathlib import Path; "
        f"tmp = Path({str(pidfile)!r} + '.tmp'); tmp.write_text(str(os.getpid())); "
        f"os.replace(tmp, {str(pidfile)!r}); time.sleep(30); Path({str(marker)!r}).touch()"
    )
    parent = (
        "import os, subprocess, time\n"
        f"subprocess.Popen([{sys.executable!r}, '-c', {child!r}])\n"
        f"while not os.path.exists({str(pidfile)!r}):\n"
        "    print('.', flush=True)\n"
        "    time.sleep(0.01)\n"
        "time.sleep(30)\n"
    )
    with pytest.raises(backends.RunStalled):
        backends.run_popen_with_kill_check(
            [sys.executable, "-c", parent],
            cwd=str(tmp_path),
            env=dict(os.environ),
            timeout_s=10,
            stall_timeout_s=0.3,
            poll_interval_s=0.05,
        )
    tool_pid = None
    try:
        # Never vacuous: the tool's pid must be on record (a survivor may write it late).
        deadline = time.monotonic() + 5
        while not pidfile.exists():
            assert time.monotonic() < deadline, "the tool never recorded its pid"
            time.sleep(0.01)
        tool_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while not _process_gone(tool_pid):
            assert time.monotonic() < deadline, "a tool child survived the stalled harness"
            time.sleep(0.01)
        assert not marker.exists(), "a tool child survived the stalled harness and kept writing"
    finally:
        # A survivor of a failed run must not outlive the test and write later.
        # The reaper can take it between that check and this kill.
        if tool_pid is not None and not _process_gone(tool_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(tool_pid, signal.SIGKILL)


def _process_gone(pid: int) -> bool:
    """True once ``pid`` no longer runs (a zombie awaiting its reaper counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def test_git_common_dir_is_bounded_and_survives_timeout(tmp_path, monkeypatch):
    seen = {}

    def hang(cmd, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(backends.subprocess, "run", hang)
    assert backends._git_common_dir(tmp_path) is None
    assert seen["timeout"] == 30


def test_backend_name_for_label_resolves_exported_segments(monkeypatch):
    from unified_agent import config

    monkeypatch.setitem(config.SEGMENT_TO_FAMILY, "agent3", "agy")
    monkeypatch.setitem(config.SEGMENT_TO_FAMILY_LOWER, "agent3", "agy")
    monkeypatch.setitem(config.SEGMENT_TO_FAMILY, "Agent2", "Codex")
    monkeypatch.setitem(config.SEGMENT_TO_FAMILY_LOWER, "agent2", "Codex")

    assert backends.backend_name_for_label("Desktop_agent3") == "agy"
    assert backends.backend_name_for_label("server_agent3") == "agy"
    assert backends.backend_name_for_label("Desktop_Agent2") == "codex"
    assert backends.backend_name_for_label("server_agent2") == "codex"
    with pytest.raises(ValueError, match="no native headless backend"):
        backends.backend_name_for_label("Desktop_agent2")
    with pytest.raises(ValueError, match="no native headless backend"):
        backends.backend_name_for_label("Desktop_Agent11")
