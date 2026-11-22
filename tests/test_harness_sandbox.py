"""Task #1948: blanket-permission harness flags need a fail-closed sandbox.

Audit S5 (residual of #1910): Copilot's ``--allow-all-tools``, Cursor's
``--trust``/``--force`` and Agy's ``--dangerously-skip-permissions`` were put on
argv unconditionally (or forced by dispatch). They now go on argv only with the
harness's own sandbox switch and a host sandbox primitive that passed a probe;
otherwise the launch is refused. Agy without skip-permissions keeps its
supervised (request-review) mode and needs no posture.

The tmux fallback of ``agy-interactive`` ran ``tmux new-session`` against the
user's default server, whose global environment holds whatever the first tmux
client had. It now starts a private server from the allowlisted env.

All values are fakes.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from unified_agent import config
from unified_agent.dispatch import claude_sandbox, harness_sandbox, routing
from unified_agent.dispatch.harness_sandbox import (
    BLANKET_PERMISSION_FLAGS,
    HarnessSandboxPosture,
    HarnessSandboxUnavailable,
    HostProbe,
)
from unified_agent.dispatch.launch import CliProcessSpawner
from unified_agent.executor import agy_interactive, backends

GUARDED = ["copilot", "cursor", "agy"]

# Captured at import, before conftest swaps in a passing stub for every test.
REAL_PROBE = harness_sandbox.probe_host
REAL_COPILOT_QUERY = harness_sandbox._copilot_effective_bypass_disabled


@pytest.fixture
def host_sandbox(monkeypatch):
    """Set the host probe result; returns a setter."""

    def _set(ok: bool, detail: str = "fake probe") -> None:
        harness_sandbox.reset_cache()
        monkeypatch.setattr(
            harness_sandbox,
            "probe_host",
            lambda: HostProbe(ok=ok, backend="fake-bwrap", detail=detail),
        )

    yield _set
    harness_sandbox.reset_cache()


def _verified(cli_backend: str) -> HarnessSandboxPosture:
    return HarnessSandboxPosture(
        cli_backend=cli_backend,
        verified=True,
        sandbox_argv=harness_sandbox.NATIVE_SANDBOX[cli_backend],
        host_backend="fake-bwrap",
        reason="verified",
    )


def _unverified(cli_backend: str) -> HarnessSandboxPosture:
    return HarnessSandboxPosture(
        cli_backend=cli_backend,
        verified=False,
        sandbox_argv=(),
        host_backend="fake-bwrap",
        reason="bwrap not on PATH (install bubblewrap)",
    )


def _blanket_in(cli_backend: str, argv: list[str]) -> list[str]:
    # The prompt is an operand (after ``--`` or inside ``--prompt=``), never a flag.
    flags = argv[: argv.index("--")] if "--" in argv else argv
    return [f for f in flags if f in BLANKET_PERMISSION_FLAGS[cli_backend]]


def _agy_skip_kwargs(cli_backend: str) -> dict:
    return {"skip_permissions": True} if cli_backend == "agy" else {}


# ── routing.headless_argv: the one argv builder dispatch and executor share ──


@pytest.mark.parametrize("cli_backend", GUARDED)
def test_headless_argv_refuses_blanket_flags_without_posture(cli_backend, monkeypatch):
    for name in ("UA_COPILOT_SKIP_PERMISSIONS", "UA_CURSOR_SKIP_PERMISSIONS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(
        HarnessSandboxUnavailable, match=f"harness_sandbox_unavailable:{cli_backend}"
    ):
        routing.headless_argv(
            cli_backend,
            cli_backend,
            prompt="do the task",
            cwd="/w",
            **_agy_skip_kwargs(cli_backend),
        )


@pytest.mark.parametrize("cli_backend", GUARDED)
def test_headless_argv_refuses_unverified_posture(cli_backend):
    with pytest.raises(HarnessSandboxUnavailable, match="bwrap not on PATH"):
        routing.headless_argv(
            cli_backend,
            cli_backend,
            prompt="do the task",
            cwd="/w",
            sandbox_posture=_unverified(cli_backend),
            **_agy_skip_kwargs(cli_backend),
        )


@pytest.mark.parametrize("cli_backend", GUARDED)
def test_headless_argv_refuses_posture_for_another_backend(cli_backend):
    other = "cursor" if cli_backend != "cursor" else "copilot"
    with pytest.raises(HarnessSandboxUnavailable, match="resolved for"):
        routing.headless_argv(
            cli_backend,
            cli_backend,
            prompt="do the task",
            sandbox_posture=_verified(other),
            **_agy_skip_kwargs(cli_backend),
        )


def test_refusal_is_a_value_error_so_existing_call_sites_fail_closed():
    assert issubclass(HarnessSandboxUnavailable, ValueError)


@pytest.mark.parametrize("cli_backend", GUARDED)
def test_headless_argv_pairs_blanket_flags_with_native_sandbox(cli_backend):
    argv = routing.headless_argv(
        cli_backend,
        cli_backend,
        prompt="do the task",
        cwd="/w",
        sandbox_posture=_verified(cli_backend),
        **_agy_skip_kwargs(cli_backend),
    )
    native = list(harness_sandbox.NATIVE_SANDBOX[cli_backend])
    start = next(i for i in range(len(argv)) if argv[i : i + len(native)] == native)  # contiguous
    assert _blanket_in(cli_backend, argv), argv
    first_blanket = min(argv.index(f) for f in _blanket_in(cli_backend, argv))
    assert start < first_blanket, argv


def test_copilot_argv_with_posture(monkeypatch):
    monkeypatch.delenv("UA_COPILOT_SKIP_PERMISSIONS", raising=False)
    argv = routing.headless_argv(
        "copilot", "copilot", prompt="p q", cwd="/w", sandbox_posture=_verified("copilot")
    )
    assert argv == [
        "copilot",
        "--no-auto-update",
        "--output-format",
        "json",
        "--sandbox",
        "--allow-all-tools",
        "-C",
        "/w",
        "--prompt=p q",
    ]


def test_cursor_argv_with_posture(monkeypatch):
    monkeypatch.delenv("UA_CURSOR_SKIP_PERMISSIONS", raising=False)
    argv = routing.headless_argv(
        "cursor", "agent", prompt="p q", cwd="/w", sandbox_posture=_verified("cursor")
    )
    assert argv == [
        "agent",
        "-p",
        "--output-format",
        "stream-json",
        "--sandbox",
        "enabled",
        "--trust",
        "--workspace",
        "/w",
        "--",
        "p q",
    ]


def test_cursor_force_is_refused_even_with_posture(monkeypatch):
    """Review 5449508428: --force ("Run Everything") is not shown to stay inside
    cursor's sandbox, so it is refused under any posture."""
    monkeypatch.setenv("UA_CURSOR_SKIP_PERMISSIONS", "1")
    with pytest.raises(HarnessSandboxUnavailable):
        routing.headless_argv("cursor", "agent", prompt="p q")
    with pytest.raises(HarnessSandboxUnavailable, match="--force"):
        routing.headless_argv("cursor", "agent", prompt="p q", sandbox_posture=_verified("cursor"))


def test_copilot_allow_all_opt_in_still_needs_posture(monkeypatch):
    monkeypatch.setenv("UA_COPILOT_SKIP_PERMISSIONS", "1")
    with pytest.raises(HarnessSandboxUnavailable):
        routing.headless_argv("copilot", "copilot", prompt="p q")
    argv = routing.headless_argv(
        "copilot", "copilot", prompt="p q", sandbox_posture=_verified("copilot")
    )
    assert "--allow-all" in argv and argv.index("--sandbox") < argv.index("--allow-all")


def test_agy_without_skip_needs_no_posture_and_stays_supervised(monkeypatch):
    monkeypatch.delenv("UA_AGY_SKIP_PERMISSIONS", raising=False)
    argv = routing.headless_argv("agy", "agy", prompt="p q")
    assert argv == ["agy", "-p", "p q", "--output-format", "stream-json"]


def test_agy_global_skip_env_needs_posture(monkeypatch):
    monkeypatch.setenv("UA_AGY_SKIP_PERMISSIONS", "1")
    with pytest.raises(HarnessSandboxUnavailable):
        routing.headless_argv("agy", "agy", prompt="p q")


@pytest.mark.parametrize("cli_backend", GUARDED)
def test_observable_argv_shows_the_sandboxed_shape(cli_backend):
    """The stored (never executed) argv documents the only shape that can launch."""
    argv = routing.headless_argv(cli_backend, cli_backend, **_agy_skip_kwargs(cli_backend))
    native = list(harness_sandbox.NATIVE_SANDBOX[cli_backend])
    assert any(argv[i : i + len(native)] == native for i in range(len(argv))), argv
    assert _blanket_in(cli_backend, argv)


# ── host probe ───────────────────────────────────────────────────────────────


def test_probe_linux_without_bwrap_is_unverified(monkeypatch):
    monkeypatch.setattr(harness_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(harness_sandbox.shutil, "which", lambda name, path=None: None)
    probe = REAL_PROBE()
    assert probe.ok is False
    assert "bwrap not on PATH" in probe.detail


def test_probe_linux_bwrap_failure_is_unverified(monkeypatch):
    monkeypatch.setattr(harness_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(harness_sandbox.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="bwrap: setting up uid map: Permission denied"
        )

    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    probe = REAL_PROBE()
    assert probe.ok is False
    assert "Permission denied" in probe.detail
    assert calls[0][0] == "/usr/bin/bwrap"
    assert "--unshare-user" in calls[0] and "--unshare-net" in calls[0]


def test_probe_timeout_is_unverified(monkeypatch):
    monkeypatch.setattr(harness_sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(harness_sandbox.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 10))

    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    probe = REAL_PROBE()
    assert probe.ok is False and probe.backend == "sandbox-exec"


def test_probe_probe_env_carries_no_parent_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    monkeypatch.setattr(harness_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(harness_sandbox.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.setdefault("envs", []).append(kwargs.get("env"))
        denied = "/usr/bin/touch" in argv
        return subprocess.CompletedProcess(
            argv, 1 if denied else 0, stdout="", stderr="Read-only file system"
        )

    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    assert REAL_PROBE().ok is True
    seen["env"] = seen["envs"][0]
    assert all(e is not None and "OPENAI_API_KEY" not in e for e in seen["envs"])
    assert seen["env"] is not None and "OPENAI_API_KEY" not in seen["env"]


def test_probe_unknown_platform_is_unverified(monkeypatch):
    monkeypatch.setattr(harness_sandbox.platform, "system", lambda: "Plan9")
    assert REAL_PROBE().ok is False


def test_resolve_failed_probe_is_not_cached(monkeypatch):
    results = iter([False, True])
    harness_sandbox.reset_cache()
    monkeypatch.setattr(
        harness_sandbox,
        "probe_host",
        lambda: HostProbe(ok=next(results), backend="fake", detail="d"),
    )
    assert harness_sandbox.resolve("copilot").verified is False
    assert harness_sandbox.resolve("copilot").verified is True
    harness_sandbox.reset_cache()


def test_resolve_unknown_backend_is_unverified(host_sandbox):
    host_sandbox(True)
    posture = harness_sandbox.resolve("hermes")
    assert posture.verified is False and posture.sandbox_argv == ()


# ── executor backends ────────────────────────────────────────────────────────


@pytest.fixture
def executor_env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harness_sandbox,
        "_copilot_effective_bypass_disabled",
        lambda cwd, env, binary: (True, "query stub"),
    )
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(backends.trust_presets, "require_native_allowed", lambda _label: None)
    for name in (
        "UA_COPILOT_SKIP_PERMISSIONS",
        "UA_CURSOR_SKIP_PERMISSIONS",
        "UA_AGY_SKIP_PERMISSIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    calls: list[list[str]] = []

    def fake_popen(argv, *, cwd, env, timeout_s, stdin=None, kill_check=None, **_kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_popen)
    return calls


def _executor_backend(cli_backend: str):
    if cli_backend == "agy":
        return backends.AgyBackend(skip_permissions=True)
    return {"copilot": backends.CopilotBackend, "cursor": backends.CursorBackend}[cli_backend]()


def _run(backend) -> dict:
    return backend.run(
        prompt="do the task",
        cwd=".",
        session_dir=".",
        session_id="sess-1948",
        agent_label="Desktop_Agent1",
        timeout_s=5,
    )


@pytest.mark.parametrize("cli_backend", GUARDED)
def test_executor_refuses_without_sandbox(cli_backend, executor_env, host_sandbox):
    host_sandbox(False, "bwrap not on PATH (install bubblewrap)")
    result = _run(_executor_backend(cli_backend))
    assert result["status"] == "error"
    assert result["error"].startswith(f"harness_sandbox_unavailable:{cli_backend}")
    assert "bwrap not on PATH" in result["error"]
    assert executor_env == [], "a refused launch must not spawn the harness"


@pytest.mark.parametrize("cli_backend", GUARDED)
def test_executor_launches_with_sandbox(cli_backend, executor_env, host_sandbox):
    host_sandbox(True)
    _run(_executor_backend(cli_backend))
    assert len(executor_env) == 1
    argv = executor_env[0]
    native = list(harness_sandbox.NATIVE_SANDBOX[cli_backend])
    assert any(argv[i : i + len(native)] == native for i in range(len(argv))), argv
    assert _blanket_in(cli_backend, argv), argv


def test_executor_agy_without_skip_runs_supervised_without_probe(executor_env, monkeypatch):
    def boom():
        raise AssertionError("supervised agy must not need a sandbox probe")

    harness_sandbox.reset_cache()
    monkeypatch.setattr(harness_sandbox, "probe_host", boom)
    _run(backends.AgyBackend(skip_permissions=False))
    assert executor_env and "--dangerously-skip-permissions" not in executor_env[0]
    assert "--sandbox" not in executor_env[0]


def test_agy_build_commands_refuse_skip_without_posture():
    backend = backends.AgyBackend(skip_permissions=True)
    with pytest.raises(HarnessSandboxUnavailable):
        backend.build_command("x")
    with pytest.raises(HarnessSandboxUnavailable):
        backend.build_continue_command()
    argv = backend.build_continue_command(sandbox_posture=_verified("agy"))
    assert argv.index("--sandbox") < argv.index("--dangerously-skip-permissions")


# ── approved dispatch (CliProcessSpawner) ────────────────────────────────────


@pytest.fixture
def spawner_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        harness_sandbox,
        "_copilot_effective_bypass_disabled",
        lambda cwd, env, binary: (True, "query stub"),
    )
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua_home"))
    for name in (
        "UA_COPILOT_SKIP_PERMISSIONS",
        "UA_CURSOR_SKIP_PERMISSIONS",
        "UA_AGY_SKIP_PERMISSIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in GUARDED:
        fake = bin_dir / name
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    spawned: list[list[str]] = []

    def fake_spawn(argv, *, cwd, env, log_path, run_id):
        spawned.append(list(argv))
        return 91948

    monkeypatch.setattr(
        "unified_agent.dispatch.supervise.spawn_via_detached_supervisor", fake_spawn
    )
    return tmp_path, spawned


def _spawn(cli_backend: str, cwd: Path) -> dict:
    return CliProcessSpawner().spawn(
        {
            "binary": cli_backend,
            "cli_backend": cli_backend,
            "cwd": str(cwd),
            "task_spec": "### Objective\nprove the sandbox gate\n",
            "run_id": f"run-1948-{cli_backend}",
            "agent_label": f"server_{cli_backend}",
        }
    )


@pytest.mark.parametrize("cli_backend", GUARDED)
def test_dispatch_refuses_without_sandbox(cli_backend, spawner_env, host_sandbox):
    cwd, spawned = spawner_env
    host_sandbox(False, "bwrap could not create a sandbox (exit 1: denied)")
    out = _spawn(cli_backend, cwd)
    assert out["ok"] is False
    assert out["spawned"] is False
    assert out["error"].startswith(f"harness_sandbox_unavailable:{cli_backend}")
    assert spawned == []


@pytest.mark.parametrize("cli_backend", GUARDED)
def test_dispatch_launches_with_sandbox(cli_backend, spawner_env, host_sandbox):
    cwd, spawned = spawner_env
    host_sandbox(True)
    out = _spawn(cli_backend, cwd)
    assert out["ok"] is True, out
    argv = spawned[0]
    native = list(harness_sandbox.NATIVE_SANDBOX[cli_backend])
    assert any(argv[i : i + len(native)] == native for i in range(len(argv))), argv
    assert _blanket_in(cli_backend, argv), argv


# ── agy-interactive tmux fallback ────────────────────────────────────────────


def test_tmux_fallback_uses_private_server_started_from_child_env(tmp_path, monkeypatch):
    """Every tmux call targets a private socket; the server starts from the child env."""
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", "fake-fernet")
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("env")))
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(agy_interactive.subprocess, "run", fake_run)
    child_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "UA_AGENT_LABEL": "x"}
    agy_interactive._run_with_tmux(
        agy_bin="agy",
        prompt="do it",
        cwd=str(tmp_path),
        env=child_env,
        timeout_s=1,
        kill_check=None,
        allow_classes=(),
        progress_cb=None,
        model=None,
    )
    tmux_calls = [(argv, env) for argv, env in calls if argv and argv[0] == "tmux"]
    assert tmux_calls
    sockets = set()
    for argv, _env in tmux_calls:
        assert argv[1] == "-S", argv
        sockets.add(argv[2])
    assert len(sockets) == 1
    socket = Path(next(iter(sockets)))
    assert socket.parent != Path("/tmp") and socket.parent.name.startswith("ua-agy-i-")

    new_session = [(a, e) for a, e in tmux_calls if "new-session" in a]
    assert len(new_session) == 1
    argv, env = new_session[0]
    assert "-f" in argv and argv[argv.index("-f") + 1] == os.devnull
    assert env is not None, "tmux server must not inherit the daemon's os.environ"
    assert "OPENAI_API_KEY" not in env and "UA_MANAGED_SECRET_KEYS" not in env
    assert env["UA_AGENT_LABEL"] == "x"
    assert any("kill-server" in a for a, _ in tmux_calls)
    assert not socket.parent.exists(), "private socket dir must be removed"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_tmux_private_server_global_env_has_no_ambient_secrets(tmp_path, monkeypatch):
    """Real tmux: the private server's global environment is the child env only."""
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/fake/agent.sock")
    # Same short base as production, so a long pytest basetemp cannot push the
    # socket past the Unix path limit (review 5449508428, finding 3).
    sock_dir = Path(
        agy_interactive.tempfile.mkdtemp(
            prefix="ua-agy-i-", dir=agy_interactive._tmux_socket_base()
        )
    )
    socket = str(sock_dir / "tmux.sock")
    child_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ALLOWED_MARK": "ok"}
    try:
        agy_interactive._tmux_new_session(
            socket, "ua-agy-i-test", str(tmp_path), "sleep 30", child_env
        )
        shown = subprocess.run(
            agy_interactive._tmux_argv(socket, "show-environment", "-g"),
            capture_output=True,
            text=True,
            timeout=5,
            env=child_env,
        ).stdout
        names = {line.split("=", 1)[0].lstrip("-") for line in shown.splitlines() if line}
        assert "ALLOWED_MARK" in names
        assert "OPENAI_API_KEY" not in names
        assert "SSH_AUTH_SOCK" not in names
    finally:
        agy_interactive._tmux_kill_server(socket, child_env)
        shutil.rmtree(sock_dir, ignore_errors=True)


# ── review round (PR #1081): probe denials, operator CLI, tmux hardening ─────


def _fake_probe_run(*, deny_rc: int = 1, create_target: bool = False, allow_rc: int = 0):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "/usr/bin/touch" in argv:
            if create_target:
                Path(argv[-1]).touch()
            return subprocess.CompletedProcess(
                argv, deny_rc, stdout="", stderr=f"touch: {argv[-1]}: Operation not permitted"
            )
        return subprocess.CompletedProcess(argv, allow_rc, stdout="", stderr="")

    return calls, fake_run


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_probe_requires_an_enforced_write_denial(system, monkeypatch):
    monkeypatch.setattr(harness_sandbox.platform, "system", lambda: system)
    monkeypatch.setattr(harness_sandbox.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    calls, fake_run = _fake_probe_run(deny_rc=1)
    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    probe = REAL_PROBE()
    assert probe.ok is True, probe
    assert len(calls) == 2
    allow, deny = calls
    assert allow[-1].endswith("true")
    assert deny[-2] == "/usr/bin/touch"
    assert probe.commands and "denied" in probe.commands[1]
    assert not Path(deny[-1]).parent.exists(), "probe temp dir must be removed"
    if system == "Darwin":
        assert allow[:2] == ["/usr/bin/sandbox-exec", "-p"]
        profile = allow[2]
        assert "(deny network*)" in profile
        assert f'(deny file-write* (subpath "{Path(deny[-1]).parent}"))' in profile
        assert "/usr/bin/true" == allow[-1]


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
@pytest.mark.parametrize("deny_rc, create_target", [(0, False), (1, True)])
def test_probe_fails_when_the_denial_is_not_enforced(system, deny_rc, create_target, monkeypatch):
    monkeypatch.setattr(harness_sandbox.platform, "system", lambda: system)
    monkeypatch.setattr(harness_sandbox.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    _calls, fake_run = _fake_probe_run(deny_rc=deny_rc, create_target=create_target)
    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    probe = REAL_PROBE()
    assert probe.ok is False
    assert "did not enforce a write denial" in probe.detail


def test_probe_fails_when_the_sandbox_cannot_start(monkeypatch):
    monkeypatch.setattr(harness_sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(harness_sandbox.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    calls, fake_run = _fake_probe_run(allow_rc=71)
    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    probe = REAL_PROBE()
    assert probe.ok is False and "could not run a sandbox" in probe.detail
    assert len(calls) == 1, "denial check is skipped when the sandbox cannot start"


def _flag_run(output: str, rc: int):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=output)

    return seen, fake_run


@pytest.mark.parametrize(
    "harness, output",
    [
        ("agy", "flags provided but not defined: -ua-sandbox-probe-sentinel"),
        ("cursor", "error: unknown option '--ua-sandbox-probe-sentinel'"),
        ("copilot", "error: unexpected argument '--ua-sandbox-probe-sentinel' found"),
    ],
)
def test_check_harness_flag_accepts_when_only_the_sentinel_is_rejected(
    harness, output, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    seen, fake_run = _flag_run(output, 2)
    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    result = harness_sandbox.check_harness_flag(harness, "/fake/bin")
    assert result["ok"] is True, result
    native = list(harness_sandbox.NATIVE_SANDBOX[harness])
    assert seen["argv"] == ["/fake/bin", *native, "--ua-sandbox-probe-sentinel"]
    assert "OPENAI_API_KEY" not in seen["env"]


@pytest.mark.parametrize(
    "output, rc",
    [
        ("flags provided but not defined: -sandbox", 2),  # switch unknown
        ("Usage of agy:", 0),  # parse never failed
        ("", 127),
    ],
)
def test_check_harness_flag_rejects_otherwise(output, rc, monkeypatch):
    _seen, fake_run = _flag_run(output, rc)
    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    assert harness_sandbox.check_harness_flag("agy", "/fake/agy")["ok"] is False


def test_check_harness_flag_missing_binary(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    result = harness_sandbox.check_harness_flag("agy", "/nope/agy")
    assert result["ok"] is False and "FileNotFoundError" in result["reason"]


@pytest.mark.parametrize(
    "probe_ok, flag_ok, code",
    [(True, True, 0), (False, True, 1), (True, False, 1)],
)
def test_probe_cli_json_and_exit_code(probe_ok, flag_ok, code, monkeypatch, capsys):
    import json

    monkeypatch.setattr(
        harness_sandbox,
        "probe_host",
        lambda: HostProbe(probe_ok, "sandbox-exec", "ok" if probe_ok else "bad", ("cmd1", "cmd2")),
    )
    monkeypatch.setattr(
        harness_sandbox,
        "check_harness_flag",
        lambda harness, binary=None: {"harness": harness, "ok": flag_ok, "reason": "r"},
    )
    rc = harness_sandbox.main(["--probe", "--harness", "agy"])
    out = json.loads(capsys.readouterr().out)
    assert rc == code
    assert out["ok"] is (probe_ok and flag_ok)
    assert out["backend"] == "sandbox-exec"
    assert out["commands"] == ["cmd1", "cmd2"]
    assert out["harnesses"][0]["harness"] == "agy"


def test_probe_cli_requires_probe_flag():
    with pytest.raises(SystemExit):
        harness_sandbox.main([])


def _tmux_fallback(tmp_path, monkeypatch, fake_run):
    monkeypatch.setattr(agy_interactive.subprocess, "run", fake_run)
    child_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "UA_AGENT_LABEL": "x"}
    return agy_interactive._run_with_tmux(
        agy_bin="agy",
        prompt="do it",
        cwd=str(tmp_path),
        env=child_env,
        timeout_s=1,
        kill_check=None,
        allow_classes=(),
        progress_cb=None,
        model=None,
    )


def test_every_tmux_client_gets_the_allowlisted_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("env")))
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    _tmux_fallback(tmp_path, monkeypatch, fake_run)
    tmux_calls = [(a, e) for a, e in calls if a and a[0] == "tmux"]
    kinds = {a[3] if a[3] != "-f" else a[5] for a, _ in tmux_calls}
    assert {"new-session", "kill-server"} <= kinds
    for argv, env in tmux_calls:
        assert env is not None, argv
        assert "OPENAI_API_KEY" not in env, argv
        assert env["UA_AGENT_LABEL"] == "x"


def test_tmux_socket_is_short_even_with_a_long_tmpdir(tmp_path, monkeypatch):
    long_tmp = tmp_path / ("d" * 60) / ("e" * 60)
    long_tmp.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(long_tmp))
    monkeypatch.setattr(agy_interactive.tempfile, "tempdir", None)
    sockets: list[str] = []

    def fake_run(argv, **kwargs):
        if argv[:2] == ["tmux", "-S"]:
            sockets.append(argv[2])
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    _tmux_fallback(tmp_path, monkeypatch, fake_run)
    assert sockets
    assert len(os.fsencode(sockets[0])) <= agy_interactive._MAX_SOCKET_PATH
    assert not sockets[0].startswith(str(long_tmp))


def test_tmux_overlong_socket_path_is_a_named_error(tmp_path, monkeypatch):
    long_tmp = tmp_path / ("d" * 60) / ("e" * 60)
    long_tmp.mkdir(parents=True)
    monkeypatch.setattr(agy_interactive, "_tmux_socket_base", lambda: str(long_tmp))
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = _tmux_fallback(tmp_path, monkeypatch, fake_run)
    assert result["status"] == "error"
    assert "Unix socket limit" in result["error"]
    assert not any("new-session" in c for c in calls)


def test_tmux_start_failure_is_a_result_not_an_exception(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        if "new-session" in argv:
            raise subprocess.CalledProcessError(1, argv, output="", stderr="error connecting")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = _tmux_fallback(tmp_path, monkeypatch, fake_run)
    assert result["status"] == "error"
    assert "failed to start" in result["error"] and "error connecting" in result["error"]


# ── review 5449508428: bypass must be off; probe denial must be a real denial ─


def _probe_with(monkeypatch, system, allow, deny):
    """``allow``/``deny`` are (rc, stderr); deny may be a callable(argv)."""
    monkeypatch.setattr(harness_sandbox.platform, "system", lambda: system)
    monkeypatch.setattr(harness_sandbox.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")

    def fake_run(argv, **kwargs):
        if "/usr/bin/touch" in argv:
            rc, err = deny(argv) if callable(deny) else deny
        else:
            rc, err = allow
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=err)

    monkeypatch.setattr(harness_sandbox.subprocess, "run", fake_run)
    return REAL_PROBE()


@pytest.mark.parametrize(
    "deny",
    [
        (71, "sandbox-exec: Sandbox initialization failed: profile error"),
        (1, "sandbox-exec: Sandbox initialization failed"),
        (-9, ""),  # killed by a signal
        (126, "sandbox-exec: execvp() of '/usr/bin/touch' failed: Permission denied"),
        (127, "touch: command not found"),
        (1, "touch: something else went wrong"),  # no denial signature
        (2, "bwrap: Can't create file at /tmp: Operation not permitted"),
        (1, "bwrap: setting up uid map: Permission denied"),
    ],
)
@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_probe_rejects_a_deny_step_that_is_not_a_sandbox_denial(system, deny, monkeypatch):
    probe = _probe_with(monkeypatch, system, (0, ""), deny)
    assert probe.ok is False, (system, deny, probe)


@pytest.mark.parametrize(
    "system, err",
    [
        ("Darwin", "touch: /private/tmp/x/denied: Operation not permitted"),
        ("Linux", "touch: cannot touch '/tmp/x/denied': Read-only file system"),
        ("Linux", "touch: cannot touch '/tmp/x/denied': Permission denied"),
    ],
)
def test_probe_accepts_a_real_denial(system, err, monkeypatch):
    assert _probe_with(monkeypatch, system, (0, ""), (1, err)).ok is True


@pytest.mark.parametrize("allow_rc", [1, 71, -6])
def test_probe_positive_run_must_exit_zero(allow_rc, monkeypatch):
    probe = _probe_with(monkeypatch, "Darwin", (allow_rc, "x"), (1, "Operation not permitted"))
    assert probe.ok is False


def test_failed_probe_is_never_cached(monkeypatch):
    harness_sandbox.reset_cache()
    monkeypatch.setattr(harness_sandbox, "probe_host", REAL_PROBE)
    monkeypatch.setattr(
        harness_sandbox,
        "bypass_disabled",
        lambda b, cwd=None, env=None, binary=None: (True, "stub"),
    )
    _probe_with(monkeypatch, "Darwin", (0, ""), (71, "Sandbox initialization failed"))
    assert harness_sandbox.resolve("copilot").verified is False
    assert harness_sandbox._cached is None
    harness_sandbox.reset_cache()


# Copilot: bypass must be explicitly off in the effective settings.


@pytest.fixture
def copilot_home(tmp_path, monkeypatch):
    monkeypatch.setattr(
        harness_sandbox,
        "_copilot_effective_bypass_disabled",
        lambda cwd, env, binary: (True, "query stub"),
    )
    home = tmp_path / "copilot-home"
    home.mkdir()
    monkeypatch.setenv("COPILOT_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return home, repo


def _write(path: Path, data) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def test_copilot_bypass_unset_is_refused(copilot_home):
    _home, repo = copilot_home
    ok, reason = harness_sandbox._bypass_disabled_impl("copilot", str(repo))
    assert ok is False
    assert "allowBypass" in reason and "settings.json" in reason


def test_copilot_bypass_false_in_user_settings_is_accepted(copilot_home):
    home, repo = copilot_home
    _write(home / "settings.json", {"sandbox": {"allowBypass": False}})
    ok, reason = harness_sandbox._bypass_disabled_impl("copilot", str(repo))
    assert ok is True, reason


@pytest.mark.parametrize("value", [True, "false", 0, None])
def test_copilot_bypass_not_strictly_false_is_refused(value, copilot_home):
    home, repo = copilot_home
    _write(home / "settings.json", {"sandbox": {"allowBypass": value}})
    assert harness_sandbox._bypass_disabled_impl("copilot", str(repo))[0] is False


@pytest.mark.parametrize("layer", ["settings.json", "settings.local.json"])
def test_copilot_repo_layer_cannot_override_user_bypass_setting(layer, copilot_home):
    home, repo = copilot_home
    _write(home / "settings.json", {"sandbox": {"allowBypass": False}})
    _write(repo / ".github" / "copilot" / layer, {"sandbox": {"allowBypass": True}})
    ok, reason = harness_sandbox._bypass_disabled_impl("copilot", str(repo / "sub"))
    assert ok is True and "child user settings" in reason


def test_copilot_repo_layer_cannot_disable_bypass(copilot_home):
    _home, repo = copilot_home
    _write(
        repo / ".github" / "copilot" / "settings.local.json", {"sandbox": {"allowBypass": False}}
    )
    assert harness_sandbox._bypass_disabled_impl("copilot", str(repo))[0] is False


def test_copilot_unparseable_settings_is_refused(copilot_home):
    home, repo = copilot_home
    _write(home / "settings.json", "{not json")
    ok, reason = harness_sandbox._bypass_disabled_impl("copilot", str(repo))
    assert ok is False and "could not read" in reason


def test_copilot_resolve_needs_both_probe_and_bypass(copilot_home, host_sandbox, monkeypatch):
    home, repo = copilot_home
    host_sandbox(True)
    monkeypatch.setattr(harness_sandbox, "bypass_disabled", harness_sandbox._bypass_disabled_impl)
    posture = harness_sandbox.resolve("copilot", cwd=str(repo))
    assert posture.verified is False and "allowBypass" in posture.reason
    with pytest.raises(HarnessSandboxUnavailable, match="allowBypass"):
        routing.headless_argv("copilot", "copilot", prompt="p q", sandbox_posture=posture)
    _write(home / "settings.json", {"sandbox": {"allowBypass": False}})
    assert harness_sandbox.resolve("copilot", cwd=str(repo)).verified is True


# Agy: no verifiable per-run or user setting disables BypassSandbox -> refuse.


def test_agy_bypass_is_never_verifiable(tmp_path):
    ok, reason = harness_sandbox._bypass_disabled_impl("agy", str(tmp_path))
    assert ok is False
    assert "BypassSandbox" in reason


def test_agy_skip_permissions_is_refused_even_when_host_probe_passes(host_sandbox, monkeypatch):
    host_sandbox(True)
    monkeypatch.setattr(harness_sandbox, "bypass_disabled", harness_sandbox._bypass_disabled_impl)
    posture = harness_sandbox.resolve("agy", cwd="/w")
    assert posture.verified is False
    with pytest.raises(HarnessSandboxUnavailable, match="BypassSandbox"):
        routing.headless_argv(
            "agy", "agy", prompt="p q", skip_permissions=True, sandbox_posture=posture
        )


def test_cursor_bypass_rule_has_no_settings_dependency(tmp_path):
    ok, _reason = harness_sandbox._bypass_disabled_impl("cursor", str(tmp_path))
    assert ok is True


@pytest.mark.parametrize("cli_backend", ["copilot", "agy"])
def test_dispatch_refuses_when_bypass_is_not_off(
    cli_backend, spawner_env, host_sandbox, monkeypatch
):
    cwd, spawned = spawner_env
    host_sandbox(True)
    monkeypatch.setenv("COPILOT_HOME", str(cwd / "empty-copilot-home"))
    monkeypatch.setattr(harness_sandbox, "bypass_disabled", harness_sandbox._bypass_disabled_impl)
    out = _spawn(cli_backend, cwd)
    assert out["ok"] is False and spawned == []
    assert out["error"].startswith(f"harness_sandbox_unavailable:{cli_backend}")


@pytest.mark.parametrize("cli_backend", ["copilot", "agy"])
def test_executor_refuses_when_bypass_is_not_off(
    cli_backend, executor_env, host_sandbox, monkeypatch, tmp_path
):
    host_sandbox(True)
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "empty-copilot-home"))
    monkeypatch.setattr(harness_sandbox, "bypass_disabled", harness_sandbox._bypass_disabled_impl)
    result = _run(_executor_backend(cli_backend))
    assert result["status"] == "error" and executor_env == []
    assert result["error"].startswith(f"harness_sandbox_unavailable:{cli_backend}")


def test_probe_cli_reports_bypass_per_harness(monkeypatch, capsys, tmp_path):
    import json

    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "h"))
    monkeypatch.setattr(harness_sandbox, "probe_host", lambda: HostProbe(True, "bwrap", "ok", ()))
    monkeypatch.setattr(
        harness_sandbox,
        "check_harness_flag",
        lambda harness, binary=None: {"harness": harness, "ok": True, "reason": "accepted"},
    )
    monkeypatch.setattr(harness_sandbox, "bypass_disabled", harness_sandbox._bypass_disabled_impl)
    rc = harness_sandbox.main(["--probe", "--harness", "copilot", "--cwd", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False
    entry = out["harnesses"][0]
    assert entry["bypass_disabled"] is False and "allowBypass" in entry["bypass_reason"]


@pytest.mark.parametrize("allowed", [False, True])
def test_dispatch_copilot_checks_scrubbed_child_home(
    allowed, spawner_env, host_sandbox, monkeypatch
):
    """R3: parent COPILOT_HOME is dropped; request HOME is the child's context."""
    cwd, spawned = spawner_env
    parent_config = cwd / "parent-config"
    child_home = cwd / "child-home"
    _write(parent_config / "settings.json", {"sandbox": {"allowBypass": allowed}})
    _write(child_home / ".copilot" / "settings.json", {"sandbox": {"allowBypass": not allowed}})
    monkeypatch.setenv("COPILOT_HOME", str(parent_config))
    monkeypatch.setattr(harness_sandbox, "bypass_disabled", harness_sandbox._bypass_disabled_impl)
    host_sandbox(True)
    seen = []
    queries = []

    def query(cwd, env, binary):
        queries.append((cwd, env, binary))
        return True, "synthetic effective false"

    monkeypatch.setattr(harness_sandbox, "_copilot_effective_bypass_disabled", query)

    def fake_spawn(argv, *, cwd, env, **kwargs):
        seen.append((argv, cwd, env))
        return 91948

    monkeypatch.setattr(
        "unified_agent.dispatch.supervise.spawn_via_detached_supervisor", fake_spawn
    )
    result = CliProcessSpawner().spawn(
        {
            "binary": "copilot",
            "cli_backend": "copilot",
            "cwd": str(cwd),
            "task_spec": "verify child context",
            "run_id": "run-1968-child-context",
            "agent_label": "server_agent6",
            "env": {"HOME": str(child_home), "COPILOT_HOME": str(parent_config)},
        }
    )
    assert result["ok"] is allowed
    assert spawned == []  # The fixture's original spawn spy was replaced.
    if allowed:
        argv, launch_cwd, env = seen[0]
        assert launch_cwd == str(cwd)
        assert env["HOME"] == str(child_home)
        assert "COPILOT_HOME" not in env
        assert queries == [(launch_cwd, env, "copilot")]
        assert queries[0][1] is env
        assert "--sandbox" in argv and "--allow-all-tools" in argv
        assert "--no-auto-update" in argv
    else:
        assert seen == [] and result["spawned"] is False
        assert "child user settings" in result["error"]


@pytest.mark.parametrize("allowed", [False, True])
def test_executor_copilot_checks_the_exact_launch_environment(
    allowed, executor_env, host_sandbox, monkeypatch, tmp_path
):
    parent_config = tmp_path / "parent-config"
    child_config = tmp_path / "child-config"
    _write(parent_config / "settings.json", {"sandbox": {"allowBypass": allowed}})
    _write(child_config / "settings.json", {"sandbox": {"allowBypass": not allowed}})
    monkeypatch.setenv("COPILOT_HOME", str(parent_config))
    monkeypatch.setattr(harness_sandbox, "bypass_disabled", harness_sandbox._bypass_disabled_impl)
    host_sandbox(True)
    launch_env = {"HOME": str(tmp_path), "COPILOT_HOME": str(child_config), "PATH": "/usr/bin"}
    builds = []
    runs = []
    queries = []

    def query(cwd, env, binary):
        queries.append((cwd, env, binary))
        return True, "synthetic effective false"

    monkeypatch.setattr(harness_sandbox, "_copilot_effective_bypass_disabled", query)

    def build(label, backend):
        builds.append((label, backend))
        return launch_env

    def run_native(**kwargs):
        runs.append(kwargs)
        return {"status": "completed"}

    monkeypatch.setattr(backends, "_child_env", build)
    monkeypatch.setattr(backends, "_run_native_cli", run_native)
    result = _run(backends.CopilotBackend(copilot_bin="/synthetic/copilot-with-hub"))
    assert builds == [("Desktop_Agent1", "copilot")]
    assert executor_env == []  # No real or mocked child process was invoked.
    if allowed:
        assert result["status"] == "completed"
        assert runs[0]["env"] is launch_env
        assert queries == [(str(tmp_path), launch_env, "/synthetic/copilot-with-hub")]
        assert queries[0][1] is launch_env
        assert runs[0]["cwd_abs"] == str(tmp_path)
        assert "--sandbox" in runs[0]["argv"]
        assert "--no-auto-update" in runs[0]["argv"]
    else:
        assert result["status"] == "error" and runs == []
        assert "child user settings" in result["error"]


@pytest.mark.parametrize("env", [{}, {"HOME": "relative"}, {"COPILOT_HOME": "~/.copilot"}])
def test_copilot_unknown_child_config_location_is_refused(env, copilot_home):
    home, repo = copilot_home
    _write(home / "settings.json", {"sandbox": {"allowBypass": False}})
    ok, reason = harness_sandbox._bypass_disabled_impl("copilot", str(repo), env=env)
    assert ok is False and "child" in reason


@pytest.mark.parametrize("data", [[], None, {"sandbox": []}, {"sandbox": {"allowBypass": "false"}}])
def test_copilot_unsupported_child_settings_are_refused(data, copilot_home):
    home, repo = copilot_home
    _write(home / "settings.json", data)
    assert harness_sandbox._bypass_disabled_impl("copilot", str(repo))[0] is False


@pytest.mark.parametrize("allow_bypass", [False, True])
def test_node_copilot_enforces_child_user_policy(
    allow_bypass, executor_env, host_sandbox, monkeypatch, tmp_path
):
    from unified_agent import node_agent

    repo = tmp_path / "repo"
    repo.mkdir()
    parent_config = tmp_path / "parent-config"
    child_home = tmp_path / "child-home"
    _write(parent_config / "settings.json", {"sandbox": {"allowBypass": not allow_bypass}})
    _write(child_home / ".copilot" / "settings.json", {"sandbox": {"allowBypass": allow_bypass}})
    monkeypatch.setenv("COPILOT_HOME", str(parent_config))
    monkeypatch.setattr(harness_sandbox, "bypass_disabled", harness_sandbox._bypass_disabled_impl)
    host_sandbox(True)
    launch_env = {"HOME": str(child_home), "PATH": "/usr/bin"}
    monkeypatch.setattr(backends, "_child_env", lambda label, backend: launch_env)
    seen = []

    def run_native(**kwargs):
        seen.append(kwargs)
        return {"status": "success", "text": "synthetic result"}

    monkeypatch.setattr(backends, "_run_native_cli", run_native)

    def unexpected_transport(*args, **kwargs):
        raise AssertionError("local node execution must not call the hub")

    agent = node_agent.NodeAgent(
        node="synthetic-1968",
        transport=unexpected_transport,
        roots=[tmp_path],
        backends=("copilot",),
        session_root=tmp_path / "sessions",
        spool_dir=tmp_path / "spool",
    )
    result = agent.execute(
        {
            "item_id": "synthetic-1968-item",
            "backend": "copilot",
            "agent": "Desktop_Agent6",
            "cwd": str(repo),
            "prompt": "verify child config",
        }
    )
    assert executor_env == []
    if allow_bypass:
        assert result["status"] == "failed" and seen == []
        assert "harness_sandbox_unavailable:copilot" in result["reason"]
    else:
        assert result["status"] == "finished"
        assert seen[0]["env"] is launch_env
        assert seen[0]["cwd_abs"] == str(repo)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_copilot_non_json_constants_cannot_establish_policy(constant, copilot_home):
    home, repo = copilot_home
    _write(home / "settings.json", '{"sandbox": {"allowBypass": false}, "extra": ' + constant + "}")
    ok, reason = harness_sandbox._bypass_disabled_impl("copilot", str(repo))
    assert ok is False and "could not read" in reason


@pytest.mark.parametrize(
    ("rc", "output", "stderr", "allowed"),
    [
        (0, "false\n", "", True),
        (0, "true", "", False),
        (1, "false", "", False),
        (1, "", "", False),
        (0, '{"allowBypass":false}', "", False),
        (0, "false\nextra", "", False),
        (0, "FALSE", "", False),
        (0, "false", "configuration warning", False),
    ],
)
def test_copilot_effective_query_requires_exact_native_false(
    rc, output, stderr, allowed, monkeypatch, tmp_path
):
    env = {"HOME": str(tmp_path), "COPILOT_HOME": str(tmp_path / "config"), "PATH": "/usr/bin"}
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, rc, output, stderr)

    monkeypatch.setattr(harness_sandbox.subprocess, "run", run)
    ok, reason = REAL_COPILOT_QUERY(str(tmp_path), env, "/synthetic/copilot-with-hub")
    assert ok is allowed
    argv, kwargs = calls[0]
    assert argv == [
        "/synthetic/copilot-with-hub",
        "--no-auto-update",
        "config",
        "sandbox.allowBypass",
    ]
    assert kwargs["env"] == env and kwargs["cwd"] == str(tmp_path)
    assert kwargs["timeout"] == 10 and kwargs["stdin"] is subprocess.DEVNULL
    assert output.strip() not in reason or output.strip() in ("", "false")
    assert "configuration warning" not in reason


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing"),
        subprocess.TimeoutExpired("query", 10),
        UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid byte"),
    ],
)
def test_copilot_effective_query_failure_refuses(error, monkeypatch, tmp_path):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(harness_sandbox.subprocess, "run", run)
    ok, reason = REAL_COPILOT_QUERY(str(tmp_path), {"HOME": str(tmp_path)}, "/synthetic/copilot")
    assert ok is False and type(error).__name__ in reason


def test_copilot_native_schema_failure_refuses_raw_false(copilot_home, monkeypatch):
    home, repo = copilot_home
    _write(home / "settings.json", {"sandbox": {"allowBypass": False, "enabled": "invalid"}})
    monkeypatch.setattr(harness_sandbox, "_copilot_effective_bypass_disabled", REAL_COPILOT_QUERY)
    monkeypatch.setattr(
        harness_sandbox.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", ""),
    )
    ok, reason = harness_sandbox._bypass_disabled_impl("copilot", str(repo))
    assert ok is False and "did not confirm false" in reason


def test_copilot_effective_query_is_not_cached(copilot_home, monkeypatch):
    home, repo = copilot_home
    _write(home / "settings.json", {"sandbox": {"allowBypass": False}})
    monkeypatch.setattr(harness_sandbox, "_copilot_effective_bypass_disabled", REAL_COPILOT_QUERY)
    results = iter([(0, "false"), (1, "")])
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        rc, output = next(results)
        return subprocess.CompletedProcess(argv, rc, output, "")

    monkeypatch.setattr(harness_sandbox.subprocess, "run", run)
    assert harness_sandbox._bypass_disabled_impl("copilot", str(repo))[0] is True
    assert harness_sandbox._bypass_disabled_impl("copilot", str(repo))[0] is False
    assert len(calls) == 2


# --- env knobs stay on the sandbox chokepoint (task #1965) -----------------------
# Task #1965: blanket-permission env knobs stay on the sandbox chokepoint.
#
# An env knob must not add a blanket-permission flag unless
# ``harness_sandbox`` has confirmed that harness's sandbox posture. A settings
# file that turns the harness's own run-outside-the-sandbox knob back on is not
# a sandbox. The agy teardown canary and the tmux fallback follow the same rule:
# no unsandboxed skip-permissions flag, and the bound bearer never appears on
# an argv.
#
# All values are fakes.


_CANARY = Path(__file__).resolve().parents[1] / "scripts" / "agy_teardown_canary.sh"
_TOKEN = "fixture-bearer-value"


def _verified_with_argv(cli_backend: str, sandbox_argv: tuple[str, ...]) -> HarnessSandboxPosture:
    return HarnessSandboxPosture(
        cli_backend=cli_backend,
        verified=True,
        sandbox_argv=sandbox_argv,
        host_backend="test-stub",
        reason="verified",
    )


def _fail_closed_claude_settings(path: Path, cwd: Path) -> None:
    body = claude_sandbox.build_claude_dispatch_settings(cwd=str(cwd))
    path.write_text(json.dumps(body), encoding="utf-8")


def _venv_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    bindir = str(Path(sys.executable).resolve().parent)
    env = {**os.environ, "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
    if extra:
        env.update(extra)
    return env


# ── Claude: the env knob needs the fail-closed settings file ────────────────


def test_claude_skip_env_without_settings_is_refused(monkeypatch):
    monkeypatch.setenv("UA_CLAUDE_SKIP_PERMISSIONS", "1")
    with pytest.raises(HarnessSandboxUnavailable, match="harness_sandbox_unavailable:claude"):
        routing.headless_argv("claude", "claude", prompt="do the task")


def test_claude_skip_env_with_fail_closed_settings_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_CLAUDE_SKIP_PERMISSIONS", "1")
    settings = tmp_path / "settings.json"
    _fail_closed_claude_settings(settings, tmp_path)
    argv = routing.headless_argv(
        "claude",
        "claude",
        prompt="do the task",
        cwd=str(tmp_path),
        dispatch_settings_path=settings,
    )
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv.index("--settings") < argv.index("--dangerously-skip-permissions")
    assert argv[argv.index("--settings") + 1] == str(settings)
    assert argv[-2:] == ["--", "do the task"]


def test_claude_allow_unsandboxed_commands_is_not_a_sandbox(tmp_path):
    body = claude_sandbox.build_claude_dispatch_settings(cwd=str(tmp_path))
    body["sandbox"]["allowUnsandboxedCommands"] = True
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(HarnessSandboxUnavailable, match="allowUnsandboxedCommands"):
        routing.headless_argv(
            "claude",
            "claude",
            prompt="do the task",
            cwd=str(tmp_path),
            dispatch_settings_path=settings,
        )


def _mutated_claude_settings(tmp_path: Path, mutate) -> Path:
    body = claude_sandbox.build_claude_dispatch_settings(cwd=str(tmp_path))
    mutate(body)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(body), encoding="utf-8")
    return settings


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda body: body["sandbox"].__setitem__("excludedCommands", ["*"]), "excludedCommands"),
        (
            lambda body: body["sandbox"]["network"].__setitem__("allowAllUnixSockets", True),
            "allowAllUnixSockets",
        ),
        (
            lambda body: body["sandbox"]["network"]["allowedDomains"].append("*"),
            "allowedDomains",
        ),
        (
            lambda body: body["sandbox"]["filesystem"]["allowWrite"].append("/"),
            "allowWrite",
        ),
        (lambda body: body["permissions"].pop("deny"), "permissions.deny"),
    ],
)
def test_claude_settings_escape_keys_are_refused(tmp_path, mutate, needle):
    settings = _mutated_claude_settings(tmp_path, mutate)
    with pytest.raises(HarnessSandboxUnavailable, match=needle):
        routing.headless_argv(
            "claude",
            "claude",
            prompt="do the task",
            cwd=str(tmp_path),
            dispatch_settings_path=settings,
        )


def test_claude_relative_settings_path_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "settings.json"
    _fail_closed_claude_settings(settings, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        harness_sandbox,
        "probe_host",
        lambda: calls.append("probed") or harness_sandbox.HostProbe(True, "stub", "ok"),
    )
    harness_sandbox.reset_cache()
    with pytest.raises(HarnessSandboxUnavailable, match="absolute"):
        routing.headless_argv(
            "claude",
            "claude",
            prompt="do the task",
            cwd=str(tmp_path),
            dispatch_settings_path=Path("settings.json"),
        )
    assert calls == []
    harness_sandbox.reset_cache()


def test_claude_bad_settings_skip_the_host_probe(tmp_path, monkeypatch):
    settings = _mutated_claude_settings(
        tmp_path, lambda body: body["sandbox"].__setitem__("excludedCommands", ["*"])
    )
    calls: list[str] = []
    monkeypatch.setattr(
        harness_sandbox,
        "probe_host",
        lambda: calls.append("probed") or harness_sandbox.HostProbe(False, "stub", "probe failed"),
    )
    harness_sandbox.reset_cache()
    with pytest.raises(HarnessSandboxUnavailable, match="excludedCommands"):
        routing.headless_argv(
            "claude",
            "claude",
            prompt="do the task",
            cwd=str(tmp_path),
            dispatch_settings_path=settings,
        )
    assert calls == []
    harness_sandbox.reset_cache()


def test_dispatch_runs_dir_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    settings = claude_sandbox.write_claude_dispatch_settings("run-fixture", cwd=str(tmp_path))
    assert stat.S_IMODE(settings.parent.stat().st_mode) == 0o700
    settings.parent.chmod(0o755)
    sidecar = claude_sandbox.write_dispatch_sidecar(
        "run-fixture-2", task_id=1, agent_label="fixture-agent"
    )
    assert sidecar.parent == settings.parent
    assert stat.S_IMODE(settings.parent.stat().st_mode) == 0o700


def test_claude_empty_settings_file_is_not_a_sandbox(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    with pytest.raises(HarnessSandboxUnavailable, match="harness_sandbox_unavailable:claude"):
        routing.headless_argv(
            "claude",
            "claude",
            prompt="do the task",
            cwd=str(tmp_path),
            dispatch_settings_path=settings,
        )


def test_claude_executor_refuses_skip_when_host_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    harness_sandbox.reset_cache()
    monkeypatch.setattr(
        harness_sandbox,
        "probe_host",
        lambda: harness_sandbox.HostProbe(ok=False, backend="fake-bwrap", detail="probe failed"),
    )
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(backends.trust_presets, "require_native_allowed", lambda _label: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        backends,
        "run_popen_with_kill_check",
        lambda argv, **_kw: calls.append(list(argv)),
    )
    result = backends.ClaudeBackend(claude_bin="claude", skip_permissions=True).run(
        prompt="do the task",
        cwd=".",
        session_dir=".",
        session_id="sess-fixture",
        agent_label="fixture-agent",
        timeout_s=5,
    )
    assert result["status"] == "error"
    assert result["error"].startswith("harness_sandbox_unavailable:claude")
    assert calls == []
    harness_sandbox.reset_cache()


def test_claude_executor_skip_with_host_sandbox_attaches_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(backends.trust_presets, "require_native_allowed", lambda _label: None)
    calls: list[list[str]] = []
    bodies: list[dict] = []

    def fake_popen(argv, **_kw):
        calls.append(list(argv))
        # Read while the run is live: the run's end removes the file (#2191).
        settings = Path(argv[argv.index("--settings") + 1])
        bodies.append(json.loads(settings.read_text(encoding="utf-8")))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_popen)
    backends.ClaudeBackend(claude_bin="claude", skip_permissions=True).run(
        prompt="do the task",
        cwd=".",
        session_dir=".",
        session_id="sess-fixture",
        agent_label="fixture-agent",
        timeout_s=5,
    )
    assert len(calls) == 1
    argv = calls[0]
    assert "--dangerously-skip-permissions" in argv
    assert argv.index("--settings") < argv.index("--dangerously-skip-permissions")
    body = bodies[0]
    assert body["sandbox"]["allowUnsandboxedCommands"] is False
    assert body["sandbox"]["failIfUnavailable"] is True
    assert not Path(argv[argv.index("--settings") + 1]).exists()


# ── Hermes: --yolo has no known sandbox switch ──────────────────────────────


def test_hermes_skip_env_without_sandbox_is_refused(monkeypatch):
    monkeypatch.setenv("UA_HERMES_SKIP_PERMISSIONS", "1")
    with pytest.raises(HarnessSandboxUnavailable, match="harness_sandbox_unavailable:hermes"):
        routing.headless_argv("hermes", "hermes", prompt="do the task", cwd="/work")


def test_hermes_skip_env_with_verified_posture_keeps_sandbox_argv(monkeypatch):
    monkeypatch.setenv("UA_HERMES_SKIP_PERMISSIONS", "1")
    posture = _verified_with_argv("hermes", ("sandbox-switch",))
    argv = routing.headless_argv(
        "hermes", "hermes", prompt="do the task", cwd="/work", sandbox_posture=posture
    )
    assert argv.index("sandbox-switch") < argv.index("--yolo")
    assert argv[-1] == "--oneshot=do the task"


def test_hermes_resolve_is_unverified_without_a_native_switch():
    posture = harness_sandbox.resolve("hermes")
    assert posture.verified is False
    assert posture.sandbox_argv == ()
    assert "no native sandbox switch" in posture.reason


def test_hermes_executor_skip_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HERMES_SKIP_PERMISSIONS", "1")
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(backends.trust_presets, "require_native_allowed", lambda _label: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        backends,
        "run_popen_with_kill_check",
        lambda argv, **_kw: calls.append(list(argv)),
    )
    result = backends.HermesBackend(hermes_bin="hermes").run(
        prompt="do the task",
        cwd=".",
        session_dir=".",
        session_id="sess-fixture",
        agent_label="fixture-agent",
        timeout_s=5,
    )
    assert result["status"] == "error"
    assert result["error"].startswith("harness_sandbox_unavailable:hermes")
    assert calls == []


# ── Devin: --permission-mode dangerous has no sandbox switch ────────────────


def test_devin_dangerous_env_is_refused(monkeypatch):
    monkeypatch.setenv("UA_DEVIN_PERMISSION_MODE", "dangerous")
    with pytest.raises(HarnessSandboxUnavailable, match="harness_sandbox_unavailable:devin"):
        routing.headless_argv("devin", "devin", prompt="do the task", cwd="/work")


def test_devin_dangerous_explicit_mode_is_refused(monkeypatch):
    monkeypatch.delenv("UA_DEVIN_PERMISSION_MODE", raising=False)
    with pytest.raises(HarnessSandboxUnavailable, match="harness_sandbox_unavailable:devin"):
        routing.headless_argv("devin", "devin", prompt="do the task", permission_mode="dangerous")


def test_devin_narrow_modes_stay_available(monkeypatch):
    monkeypatch.delenv("UA_DEVIN_PERMISSION_MODE", raising=False)
    argv = routing.headless_argv(
        "devin", "devin", prompt="do the task", permission_mode="accept-edits"
    )
    assert argv[1:3] == ["--permission-mode", "accept-edits"]
    assert "dangerous" not in argv


def test_devin_executor_dangerous_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_DEVIN_PERMISSION_MODE", "dangerous")
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(backends.trust_presets, "require_native_allowed", lambda _label: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        backends, "run_popen_with_kill_check", lambda argv, **_kw: calls.append(list(argv))
    )
    result = backends.DevinBackend(devin_bin="devin").run(
        prompt="do the task",
        cwd=".",
        session_dir=".",
        session_id="sess-fixture",
        agent_label="fixture-agent",
        timeout_s=5,
    )
    assert result["status"] == "error"
    assert result["error"].startswith("harness_sandbox_unavailable:devin")
    assert calls == []


# ── agy teardown canary ─────────────────────────────────────────────────────


def _sandbox_python(tmp_path: Path, *, allow: bool) -> Path:
    """A python3 on PATH that answers only the canary's posture query."""
    real = Path(sys.executable).resolve()
    bin_dir = tmp_path / "py-bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "python3"
    if allow:
        answer = "printf '%s' '--sandbox'\nexit 0\n"
    else:
        answer = "printf '%s\\n' 'harness_sandbox_unavailable:agy: refused' >&2\nexit 1\n"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-c" ] && printf \'%s\\n\' "${2:-}" | grep -q ua_harness_sandbox; then\n'
        f"{answer}"
        "fi\n"
        f'exec {shlex_quote(str(real))} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return bin_dir


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def test_agy_canary_refuses_without_a_verified_sandbox(tmp_path):
    fake = tmp_path / "fake-agy"
    marker = tmp_path / "ran"
    fake.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        f'  if [ "$arg" = --dangerously-skip-permissions ]; then touch {shlex_quote(str(marker))}; fi\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    out = subprocess.run(
        ["bash", str(_CANARY), "--agy-bin", str(fake), "--workdir", str(tmp_path / "repo")],
        capture_output=True,
        text=True,
        timeout=60,
        env=_venv_env({"UA_AGY_CANARY_MARKER_WAIT_S": "0"}),
    )
    assert out.returncode == 2, out.stdout + out.stderr
    assert "harness_sandbox_unavailable:agy" in out.stderr
    assert not marker.exists()


def test_agy_canary_with_sandbox_argv_places_it_before_skip(tmp_path):
    args_file = tmp_path / "argv.txt"
    fake = tmp_path / "fake-agy"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex_quote(str(args_file))}\n"
        "printf 'survived\\n' > canary_marker.txt\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    bin_dir = _sandbox_python(tmp_path, allow=True)
    out = subprocess.run(
        ["bash", str(_CANARY), "--agy-bin", str(fake), "--workdir", str(tmp_path / "repo")],
        capture_output=True,
        text=True,
        timeout=60,
        env=_venv_env(
            {
                "PATH": str(bin_dir) + os.pathsep + _venv_env()["PATH"],
                "UA_AGY_CANARY_MARKER_WAIT_S": "0",
            }
        ),
    )
    assert out.returncode == 0, out.stdout + out.stderr
    recorded = args_file.read_text(encoding="utf-8").splitlines()
    assert recorded.index("--sandbox") < recorded.index("--dangerously-skip-permissions")


# ── tmux fallback: bearer stays off argv ────────────────────────────────────


def test_tmux_exec_argv_never_contains_the_bearer(tmp_path, monkeypatch):
    token = _TOKEN
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        if "new-session" in argv:
            script = Path(argv[-1])
            captured["script"] = script.read_text(encoding="utf-8")
            captured["script_mode"] = stat.S_IMODE(script.stat().st_mode)
            bearer = script.parent / "bearer"
            captured["bearer"] = bearer.read_text(encoding="utf-8")
            captured["bearer_mode"] = stat.S_IMODE(bearer.stat().st_mode)
            captured["dir_mode"] = stat.S_IMODE(script.parent.stat().st_mode)
            captured["tmux_env"] = dict(kwargs.get("env") or {})
        captured.setdefault("argvs", []).append(list(argv))
        captured.setdefault("envs", []).append(dict(kwargs.get("env") or {}))
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(agy_interactive.subprocess, "run", fake_run)
    agy_interactive._run_with_tmux(
        agy_bin="agy",
        prompt="do it",
        cwd=str(tmp_path),
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "UA_AGENT_LABEL": "fixture-agent",
            "UA_AGENT_BEARER_TOKEN": token,
        },
        timeout_s=1,
        kill_check=None,
        allow_classes=(),
        progress_cb=None,
        model=None,
    )
    script = str(captured["script"])
    assert token not in script
    assert "UA_AGENT_BEARER_TOKEN=" not in script
    assert "3<" in script
    for argv in captured["argvs"]:
        assert all(token not in arg for arg in argv)
    for env in captured["envs"]:
        assert "UA_AGENT_BEARER_TOKEN" not in env
    assert "UA_AGENT_BEARER_TOKEN" not in captured["tmux_env"]
    assert captured["bearer"] == token + "\n"
    assert captured["bearer_mode"] == 0o600
    assert captured["script_mode"] == 0o700
    assert captured["dir_mode"] == 0o700


def test_generated_tmux_script_runs_child_with_bearer_off_argv(tmp_path, monkeypatch):
    """Execute the script ``_run_with_tmux`` writes, not just its text."""
    token = _TOKEN
    report = tmp_path / "child-bearer"
    argv_out = tmp_path / "child-argv"
    fake = tmp_path / "fake-child"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s' \"$UA_AGENT_BEARER_TOKEN\" > {shlex_quote(str(report))}\n"
        f'printf \'%s\\n\' "$0" "$@" > {shlex_quote(str(argv_out))}\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    real_run = subprocess.run
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        if "new-session" in argv:
            script = Path(argv[-1])
            ran = real_run(["bash", str(script)], capture_output=True, text=True, timeout=10)
            captured["returncode"] = ran.returncode
            captured["stderr"] = ran.stderr
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(agy_interactive.subprocess, "run", fake_run)
    agy_interactive._run_with_tmux(
        agy_bin=str(fake),
        prompt="do it",
        cwd=str(tmp_path),
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "UA_AGENT_BEARER_TOKEN": token,
        },
        timeout_s=1,
        kill_check=None,
        allow_classes=(),
        progress_cb=None,
        model=None,
    )
    assert captured["returncode"] == 0, captured.get("stderr")
    assert report.read_text(encoding="utf-8") == token
    recorded = argv_out.read_text(encoding="utf-8")
    assert token not in recorded
    assert "UA_AGENT_BEARER_TOKEN=" not in recorded


def test_tmux_read_keeps_a_bearer_that_has_no_trailing_newline(tmp_path):
    token = _TOKEN
    bearer = tmp_path / "bearer"
    bearer.write_bytes(token.encode("utf-8"))
    report = tmp_path / "child-bearer"
    fake = tmp_path / "fake-child"
    fake.write_text(
        f"#!/bin/sh\nprintf '%s' \"$UA_AGENT_BEARER_TOKEN\" > {shlex_quote(str(report))}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    script = tmp_path / "env.sh"
    script.write_text(
        agy_interactive._tmux_exec_script(
            shlex_quote(str(fake)),
            {"PATH": "/usr/bin:/bin", "UA_AGENT_BEARER_TOKEN": token},
            bearer_file=str(bearer),
        ),
        encoding="utf-8",
    )
    assert token not in script.read_text(encoding="utf-8")
    script.chmod(0o700)
    ran = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    assert report.read_text(encoding="utf-8") == token


def test_tmux_exec_without_bearer_still_uses_env_i(tmp_path):
    script = agy_interactive._tmux_exec_script(
        "agy -p 'do it'", {"PATH": "/usr/bin", "HOME": "/tmp"}
    )
    assert script.startswith("#!/bin/sh\nexec /usr/bin/env -i ")
    assert "UA_AGENT_BEARER_TOKEN" not in script
    assert "PATH=" in script
