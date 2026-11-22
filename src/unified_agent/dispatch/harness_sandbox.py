"""Fail-closed sandbox posture for blanket-permission harness flags (task #1948).

Copilot, Cursor and Agy cannot run unattended without a flag that approves
tool calls wholesale:

- Copilot: ``--allow-all-tools`` (required by ``copilot -p``) and the
  ``UA_COPILOT_SKIP_PERMISSIONS`` opt-in ``--allow-all``.
- Cursor: ``--trust`` (headless workspace trust). The
  ``UA_CURSOR_SKIP_PERMISSIONS`` opt-in ``--force`` is refused outright.
- Agy: ``--dangerously-skip-permissions`` (forced by approved dispatch, or the
  ``UA_AGY_SKIP_PERMISSIONS`` opt-in in the executor).

``claude_sandbox`` pairs Claude's ``--dangerously-skip-permissions`` with a
per-run settings file. ``UA_CLAUDE_SKIP_PERMISSIONS=1`` is the same blanket
flag: it is attached only when ``resolve`` confirms that absolute path parses
as exactly ``build_claude_dispatch_settings`` for the launch cwd. Hermes
``--yolo`` (``UA_HERMES_SKIP_PERMISSIONS``) is the same kind of flag;
``hermes --help`` describes it as bypassing dangerous-command approval and
names no sandbox switch, so ``resolve("hermes")`` stays unverified and the
flag is refused. Devin ``--permission-mode dangerous``
(``UA_DEVIN_PERMISSION_MODE``) auto-approves every tool and has no sandbox
switch, so it is refused the same way. This module is the one chokepoint: a
blanket flag goes on
argv only together with the harness's confirmed sandbox, and only after the
host's sandbox primitive has been shown to work where that primitive is what
the harness sits on. If either is missing the launch is refused with an
actionable error. Nothing here widens a posture, and there is no environment
switch that turns the requirement off.

A posture is "verified" when all three hold:

1. The harness has a native sandbox switch (``NATIVE_SANDBOX``) that is put on
   argv with the blanket flag. Fail-closed behaviour of each switch, checked
   against the installed CLIs on 2026-10-07:

   - Cursor ``--sandbox enabled`` (cursor-agent 2026.09.28): in print mode the
     CLI exits 1 with "Sandbox mode is enabled but not available on this
     system" when its sandbox cannot start.
   - Copilot ``--sandbox`` (copilot 1.0.92, experimental): "every sandboxed
     shell command and every sandboxed MCP/LSP server fails" on a host that
     cannot run it (``copilot help sandbox``).
   - Agy ``--sandbox`` (agy 1.3.0): "Run in a sandbox with terminal
     restrictions enabled". Its behaviour on an unsupported host is not
     documented, which is why the host probe below is required too.

2. The host sandbox primitive those switches sit on passes a functional probe
   (``probe_host``) that both starts a process and enforces a write denial
   (``_smoke``). Anything short of a real permission denial is unverified.

3. The harness's own run-outside-the-sandbox escape is verifiably off for the
   launch (``bypass_disabled``): Copilot's effective ``sandbox.allowBypass``
   is ``false``; Agy has no such control, so its skip-permissions launches are
   always refused; Cursor has none needed once ``--force`` is refused.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Harness switch that turns its own sandbox on, paired with every blanket flag.
NATIVE_SANDBOX: dict[str, tuple[str, ...]] = {
    "copilot": ("--sandbox",),
    "cursor": ("--sandbox", "enabled"),
    "agy": ("--sandbox",),
}

# Flags that approve tool calls wholesale. Documentation and test oracle: the
# argv builders in ``routing`` / ``executor.backends`` attach them only with a
# verified posture.
BLANKET_PERMISSION_FLAGS: dict[str, tuple[str, ...]] = {
    "copilot": ("--allow-all-tools", "--allow-all", "--yolo"),
    "cursor": ("--trust", "--force", "--yolo"),
    "agy": ("--dangerously-skip-permissions",),
}

REFUSAL_PREFIX = "harness_sandbox_unavailable"

_PROBE_TIMEOUT_S = 10
_PROBE_TTL_S = 300.0


@dataclass(frozen=True)
class HostProbe:
    """Result of probing the host's OS sandbox primitive."""

    ok: bool
    backend: str
    detail: str
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessSandboxPosture:
    """Whether ``cli_backend`` may run with its blanket-permission flags."""

    cli_backend: str
    verified: bool
    sandbox_argv: tuple[str, ...]
    host_backend: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "cli_backend": self.cli_backend,
            "verified": self.verified,
            "sandbox_argv": list(self.sandbox_argv),
            "host_backend": self.host_backend,
            "reason": self.reason,
        }


class HarnessSandboxUnavailable(ValueError):
    """A blanket-permission launch was refused for want of a verified sandbox.

    A ``ValueError`` so the existing argv-builder call sites, which already turn
    ``ValueError`` into ``{"ok": False, "error": ...}``, fail closed unchanged.
    """


_PROBE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _run_probe(argv: list[str]) -> tuple[int | None, str]:
    """Run one probe command; ``(returncode, combined output)``, ``None`` on error."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            # The probe needs nothing from the hub's environment.
            env={"PATH": _PROBE_PATH},
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"[:300]
    return proc.returncode, f"{proc.stderr or ''}\n{proc.stdout or ''}".strip()


def _last_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    return (lines[-1] if lines else "no output")[:300]


# What ``touch`` prints when the sandbox refuses the write: EPERM from a
# Seatbelt deny, EROFS or EACCES from bwrap's read-only bind.
_DENIAL_SIGNATURES = ("Operation not permitted", "Read-only file system", "Permission denied")
# Output that means the sandbox tool itself failed, so a non-zero exit says
# nothing about confinement.
_SANDBOX_FAILURE_MARKERS = ("Sandbox initialization failed", "sandbox-exec:", "bwrap:", "execvp")


def _smoke(backend: str, allow_argv: list[str], deny_argv: list[str], target: Path) -> HostProbe:
    """Run ``allow_argv`` (must exit 0) and ``deny_argv`` (must be denied).

    ``deny_argv`` is ``touch <target>`` inside the probe's own temp dir. It
    counts as an enforced denial only when ``touch`` itself exits 1 with a
    permission error and the file is absent. A sandbox start-up failure (exit
    71 "Sandbox initialization failed", a ``bwrap:`` error), a signal, an exec
    failure (126/127) or any other outcome is a probe failure.
    """
    commands = (shlex.join(allow_argv), shlex.join(deny_argv))

    def fail(why: str) -> HostProbe:
        return HostProbe(False, backend, why, commands)

    rc, output = _run_probe(allow_argv)
    if rc != 0:
        why = output if rc is None else f"exit {rc}: {_last_line(output)}"
        return fail(f"{backend} could not run a sandbox ({why})")
    rc, output = _run_probe(deny_argv)
    if rc is None:
        return fail(f"{backend} denial check failed ({output})")
    if target.exists() or rc == 0:
        return fail(f"{backend} did not enforce a write denial ({target} was writable)")
    if any(marker in output for marker in _SANDBOX_FAILURE_MARKERS):
        return fail(f"{backend} failed during the denial check (exit {rc}: {_last_line(output)})")
    if rc != 1 or not any(sig in output for sig in _DENIAL_SIGNATURES):
        return fail(
            f"{backend} denial check was not a sandbox denial (exit {rc}: {_last_line(output)})"
        )
    return HostProbe(True, backend, "ok", commands)


def probe_host() -> HostProbe:
    """Show that the OS sandbox primitive works here and enforces a denial.

    Linux: ``bwrap`` with a read-only ``/`` and new user, pid and network
    namespaces runs ``true``, then must fail to ``touch`` a file in a fresh
    temp dir. macOS: ``sandbox-exec`` with a profile that denies network and
    writes under a fresh temp dir runs ``/usr/bin/true``, then must fail to
    ``touch`` a file there. Only that temp dir is ever written to, and it is
    removed afterwards.
    """
    system = platform.system()
    if system not in {"Linux", "Darwin"}:
        return HostProbe(False, "none", f"no supported sandbox primitive on {system or 'unknown'}")
    touch = shutil.which("touch", path=_PROBE_PATH) or "/usr/bin/touch"
    work = Path(tempfile.mkdtemp(prefix="ua-sbx-probe-"))
    try:
        target = Path(os.path.realpath(work)) / "denied"
        if system == "Linux":
            bwrap = shutil.which("bwrap", path=_PROBE_PATH) or shutil.which("bwrap")
            if not bwrap:
                return HostProbe(False, "bwrap", "bwrap not on PATH (install bubblewrap)")
            true_bin = shutil.which("true", path=_PROBE_PATH) or "/bin/true"
            base = [
                bwrap,
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--unshare-user",
                "--unshare-pid",
                "--unshare-net",
                "--die-with-parent",
            ]
            return _smoke("bwrap", [*base, true_bin], [*base, touch, str(target)], target)
        sandbox_exec = shutil.which("sandbox-exec", path=_PROBE_PATH) or "/usr/bin/sandbox-exec"
        profile = (
            "(version 1)(allow default)(deny network*)"
            f'(deny file-write* (subpath "{target.parent}"))'
        )
        base = [sandbox_exec, "-p", profile]
        return _smoke("sandbox-exec", [*base, "/usr/bin/true"], [*base, touch, str(target)], target)
    finally:
        shutil.rmtree(work, ignore_errors=True)


_cache_lock = threading.Lock()
_cached: tuple[float, HostProbe] | None = None


def reset_cache() -> None:
    global _cached
    with _cache_lock:
        _cached = None


def _host_probe_cached() -> HostProbe:
    global _cached
    now = time.monotonic()
    with _cache_lock:
        if _cached is not None and now - _cached[0] < _PROBE_TTL_S:
            return _cached[1]
    result = probe_host()
    with _cache_lock:
        # A failed probe is not cached, so a host that gains bwrap recovers on
        # the next launch rather than after the TTL.
        _cached = (now, result) if result.ok else None
    return result


def _copilot_effective_bypass_disabled(
    cwd: str | None, env: Mapping[str, str], binary: str
) -> tuple[bool, str]:
    """Read the installed CLI's local effective setting, without a session.

    Parsing the literal ourselves is not enough: a sibling/root schema error
    makes Copilot ignore the settings file. ``config KEY`` is a local query,
    not a model run or a setting write. Disable auto-update on this query and
    use exactly the binary, cwd and scrubbed environment used for the launch.
    Never cache a result or print query output (which may contain diagnostics).
    """
    try:
        proc = subprocess.run(
            [binary, "--no-auto-update", "config", "sandbox.allowBypass"],
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        return False, f"Copilot effective sandbox.allowBypass query failed ({type(exc).__name__})"
    if proc.returncode == 0 and proc.stdout.strip() == "false" and not proc.stderr.strip():
        return True, "Copilot effective sandbox.allowBypass is false"
    return False, (
        "Copilot effective sandbox.allowBypass query did not confirm false "
        f"(exit {proc.returncode}); fix child user settings or CLI configuration"
    )


def _copilot_bypass_disabled(
    cwd: str | None, env: Mapping[str, str] | None = None, binary: str | None = None
) -> tuple[bool, str]:
    """Check the user settings Copilot will read in this exact child context.

    ``COPILOT_HOME`` replaces ``HOME/.copilot``. Never fall back to the parent
    home when a scrubbed child environment was supplied. Ambiguous relative or
    home-relative config paths are refused rather than expanded in the parent.

    Copilot's ``sandbox.allowBypass`` is user/managed scoped, not a repository
    setting. A repository/local ``false`` cannot disable the default bypass.
    Managed policy can only restrict the user's policy further, so requiring
    explicit user ``false`` is sufficient without reading managed credentials.
    JSONC and legacy config migration are not inferred: unparseable, missing,
    or unsupported settings fail closed. No configuration is written here.

    Reference: GitHub Copilot CLI configuration directory, User settings and
    Repository settings (copilot 1.0.93 ``help sandbox``):
    https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
    """
    source = os.environ if env is None else env
    location = source.get("COPILOT_HOME")
    if location:
        home = Path(location)
    else:
        user_home = source.get("HOME")
        if not user_home:
            return False, "Copilot child HOME is missing; sandbox.allowBypass cannot be verified"
        home = Path(user_home) / ".copilot"
    if not home.is_absolute():
        return (
            False,
            "Copilot child config directory must be absolute to verify sandbox.allowBypass",
        )
    path = home / "settings.json"

    def invalid_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)
    except (OSError, ValueError) as exc:
        return False, f"could not read {path} to check sandbox.allowBypass ({type(exc).__name__})"
    sandbox = data.get("sandbox") if isinstance(data, dict) else None
    if isinstance(sandbox, dict) and sandbox.get("allowBypass") is False:
        ok, reason = _copilot_effective_bypass_disabled(
            cwd, source, binary or source.get("UA_COPILOT_BIN") or "copilot"
        )
        return ok, f"{reason} (child user settings {path})"
    return False, (
        "Copilot may run a model-requested command outside its sandbox: "
        f"sandbox.allowBypass must be false in child user settings {path}. Set "
        '"sandbox": {"allowBypass": false} there (copilot config, user scope)'
    )


def _agy_bypass_disabled() -> tuple[bool, str]:
    """Agy: no verifiable way to switch off ``BypassSandbox``.

    agy 1.3.0's run-command tool takes ``BypassSandbox: true`` ("requires
    manual user approval"), and ``--dangerously-skip-permissions`` approves
    every request. Its ``disable_sandbox_bypass`` field is an internal
    RunCommandToolConfig protobuf, not a documented CLI setting or flag, so no
    per-run or user-level control can be set and checked. Refuse.
    """
    return False, (
        "agy 1.3.0 has no documented flag or setting that disables BypassSandbox, "
        "and --dangerously-skip-permissions would approve a model-requested bypass"
    )


def claude_settings_argv(settings_path: str | Path) -> tuple[str, ...]:
    """Argv that pins a Claude launch to one settings file.

    ``--setting-sources ""`` keeps user and project settings from merging.
    The file itself is checked by ``_claude_settings_confirmed``.
    """
    return ("--setting-sources", "", "--settings", str(settings_path))


def _json_equal(actual: Any, expected: Any) -> bool:
    """Structural equality that does not treat JSON ``true`` as ``1``."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is bool and type(expected) is bool and actual is expected
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _json_equal(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _json_equal(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return type(actual) is type(expected) and actual == expected


def _drift_paths(actual: Any, expected: Any, prefix: str = "") -> list[str]:
    if _json_equal(actual, expected):
        return []
    if isinstance(actual, dict) and isinstance(expected, dict):
        paths: list[str] = []
        for key in sorted(set(actual) | set(expected), key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in actual or key not in expected:
                paths.append(child)
            else:
                paths.extend(_drift_paths(actual[key], expected[key], child))
        return paths
    return [prefix or "$"]


def _claude_settings_confirmed(path: Path, *, cwd: str) -> tuple[bool, str]:
    """Whether this absolute file is exactly the fail-closed dispatch settings.

    The comparison is the parsed body of
    ``claude_sandbox.build_claude_dispatch_settings(cwd=cwd)``. A relative
    path is refused without resolving it against the process cwd. Drift in
    any escape (``excludedCommands``, unix sockets, allowlists, denies)
    fails the check.
    """
    if not path.is_absolute():
        return False, (f"Claude settings path must be absolute ({path}); refusing to resolve it")

    def invalid_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)
    except (OSError, ValueError) as exc:
        return False, (
            f"could not read {path} to check the fail-closed Claude settings ({type(exc).__name__})"
        )
    # Local import: scripts/agy_teardown_canary.sh loads this file by path,
    # so a package import at module scope has no parent package.
    from . import claude_sandbox

    expected = claude_sandbox.build_claude_dispatch_settings(cwd=cwd)
    if not _json_equal(data, expected):
        drifted = _drift_paths(data, expected)[:8]
        shown = ", ".join(drifted) if drifted else "body"
        return False, (
            f"Claude settings {path} are not the fail-closed dispatch settings for {cwd} ({shown})"
        )
    return True, f"Claude settings {path} match the fail-closed dispatch sandbox for {cwd}"


def _resolve_claude(settings_path: str | Path | None, cwd: str | None) -> HarnessSandboxPosture:
    """Claude's sandbox is the per-run settings file plus the host probe.

    The probe runs only after the file matches. A missing, relative, or
    drifted file is refused without probing the host.
    """
    if settings_path is None:
        return HarnessSandboxPosture(
            cli_backend="claude",
            verified=False,
            sandbox_argv=(),
            host_backend="none",
            reason=(
                "UA_CLAUDE_SKIP_PERMISSIONS cannot add --dangerously-skip-permissions "
                "without the fail-closed dispatch settings file"
            ),
        )
    if not cwd:
        return HarnessSandboxPosture(
            "claude",
            False,
            (),
            "none",
            "launch cwd is required to confirm Claude sandbox settings",
        )
    # Do not resolve: a relative path must not be joined to the process cwd.
    path = Path(settings_path)
    ok, why = _claude_settings_confirmed(path, cwd=cwd)
    if not ok:
        return HarnessSandboxPosture("claude", False, (), "none", why)
    host = _host_probe_cached()
    if not host.ok:
        return HarnessSandboxPosture("claude", False, (), host.backend, host.detail)
    return HarnessSandboxPosture(
        "claude", True, claude_settings_argv(path), host.backend, "verified"
    )


def _bypass_disabled_impl(
    cli_backend: str,
    cwd: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    binary: str | None = None,
) -> tuple[bool, str]:
    if cli_backend == "copilot":
        return _copilot_bypass_disabled(cwd, env, binary)
    if cli_backend == "agy":
        return _agy_bypass_disabled()
    if cli_backend == "cursor":
        # cursor-agent --help: ``--force`` is "Force allow commands unless
        # explicitly denied" and ``--yolo`` is "Alias for --force (Run
        # Everything)". ``--sandbox`` is a separate switch (enabled|disabled)
        # that overrides config. Nothing in that help text, or in this repo,
        # shows that --force keeps commands inside the sandbox, so the flag
        # is refused in headless_argv even when --sandbox enabled would
        # otherwise verify. There is no settings knob analogous to Copilot's
        # allowBypass to read here.
        return True, "cursor runs without --force; --force is refused"
    return False, f"{cli_backend} has no known sandbox bypass control"


def bypass_disabled(
    cli_backend: str,
    cwd: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    binary: str | None = None,
) -> tuple[bool, str]:
    """Whether the harness's own run-outside-the-sandbox escape is off."""
    return _bypass_disabled_impl(cli_backend, cwd, env=env, binary=binary)


def resolve(
    cli_backend: str,
    cwd: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    binary: str | None = None,
    settings_path: str | Path | None = None,
) -> HarnessSandboxPosture:
    """The sandbox posture ``cli_backend`` would launch under on this host.

    Verified only when the host probe passes *and* the harness's own bypass is
    verifiably off for a launch in ``cwd`` with the exact child ``env``.
    Omitted ``env`` is the current process context (read-only diagnostic use).

    Claude is confirmed from ``settings_path`` (the fail-closed dispatch file)
    rather than a CLI ``--sandbox`` switch. Hermes has no known sandbox switch,
    so it stays unverified.
    """
    if cli_backend == "claude":
        return _resolve_claude(settings_path, cwd)
    native = NATIVE_SANDBOX.get(cli_backend)
    if native is None:
        return HarnessSandboxPosture(
            cli_backend=cli_backend,
            verified=False,
            sandbox_argv=(),
            host_backend="none",
            reason=f"{cli_backend} has no native sandbox switch known to unified-agent",
        )
    host = _host_probe_cached()
    if not host.ok:
        return HarnessSandboxPosture(cli_backend, False, (), host.backend, host.detail)
    off, why = bypass_disabled(cli_backend, cwd, env=env, binary=binary)
    if not off:
        return HarnessSandboxPosture(cli_backend, False, (), host.backend, why)
    return HarnessSandboxPosture(cli_backend, True, native, host.backend, "verified")


def refusal_message(cli_backend: str, flags: str, reason: str) -> str:
    return (
        f"{REFUSAL_PREFIX}:{cli_backend}: refusing to launch {cli_backend} with {flags} "
        f"without a verified fail-closed sandbox ({reason}). Fix the host sandbox "
        "(Linux: install bubblewrap and allow unprivileged user namespaces; macOS: "
        "sandbox-exec), or route the task to a harness with its own enforced sandbox "
        "(claude, codex). See docs/security.md#harness-sandbox-posture."
    )


def require(
    cli_backend: str,
    posture: HarnessSandboxPosture | None,
    *,
    flags: str,
) -> tuple[str, ...]:
    """Native sandbox argv to pair with ``flags``, or raise.

    The posture must be verified and for this backend; anything else, including
    ``None`` from a caller that never resolved one, is refused.
    """
    if posture is None:
        raise HarnessSandboxUnavailable(
            refusal_message(cli_backend, flags, "no sandbox posture was resolved")
        )
    if posture.cli_backend != cli_backend:
        raise HarnessSandboxUnavailable(
            refusal_message(cli_backend, flags, f"posture was resolved for {posture.cli_backend!r}")
        )
    if not posture.verified or not posture.sandbox_argv:
        raise HarnessSandboxUnavailable(refusal_message(cli_backend, flags, posture.reason))
    return posture.sandbox_argv


def planned_argv(cli_backend: str) -> tuple[str, ...]:
    """Sandbox argv shown in the observable (stored, never executed) argv."""
    return NATIVE_SANDBOX.get(cli_backend, ())


# ── read-only operator check (deploy window) ─────────────────────────────────

_DEFAULT_BINARIES = {
    "agy": ("UA_AGY_BIN", "agy"),
    "copilot": ("UA_COPILOT_BIN", "copilot"),
    "cursor": ("UA_CURSOR_BIN", "cursor-agent"),
}
_SENTINEL_FLAG = "--ua-sandbox-probe-sentinel"


def check_harness_flag(harness: str, binary: str | None = None) -> dict[str, Any]:
    """Show that the installed ``harness`` CLI accepts its native sandbox switch.

    Runs ``<bin> <sandbox switch> --ua-sandbox-probe-sentinel``. Each CLI
    (Go flag, commander, clap) stops at the first unknown option and exits
    non-zero naming it, so the parse never reaches a session. If the error
    names the sentinel, every token before it, the sandbox switch included,
    was accepted. ``--help``/``--version`` are not used: agy and cursor-agent
    honour them before rejecting unknown flags.
    """
    native = NATIVE_SANDBOX.get(harness)
    if native is None:
        return {"harness": harness, "ok": False, "reason": "no native sandbox switch known"}
    env_name, default = _DEFAULT_BINARIES[harness]
    binary = binary or os.environ.get(env_name) or default
    argv = [binary, *native, _SENTINEL_FLAG]
    result: dict[str, Any] = {"harness": harness, "binary": binary, "command": shlex.join(argv)}
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": os.environ.get("PATH", _PROBE_PATH),
                "HOME": os.environ.get("HOME", ""),
            },
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {**result, "ok": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    named = _SENTINEL_FLAG.lstrip("-")
    if proc.returncode != 0 and named in output:
        return {**result, "ok": True, "reason": f"{' '.join(native)} accepted"}
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return {
        **result,
        "ok": False,
        "reason": f"exit {proc.returncode}: {(lines[0] if lines else 'no output')[:300]}",
    }


def main(argv: list[str] | None = None) -> int:
    """``python -m unified_agent.dispatch.harness_sandbox --probe [--harness H]``.

    Prints JSON and exits 0 when the host probe passes and, for each named
    harness, its sandbox switch is accepted and its bypass is verifiably off;
    1 otherwise. Read-only apart from the probe's own
    temp dir; it does not read or change hub state.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="python -m unified_agent.dispatch.harness_sandbox")
    parser.add_argument("--probe", action="store_true", required=True)
    parser.add_argument(
        "--harness",
        action="append",
        choices=sorted(NATIVE_SANDBOX),
        default=[],
        help="also check that this harness CLI accepts its sandbox switch (repeatable)",
    )
    parser.add_argument("--bin", help="binary for a single --harness (default: its UA_*_BIN)")
    parser.add_argument(
        "--cwd",
        default=None,
        help="launch directory for the diagnostic context (default: none)",
    )
    args = parser.parse_args(argv)
    if args.bin and len(args.harness) != 1:
        parser.error("--bin needs exactly one --harness")

    host = probe_host()
    report: dict[str, Any] = {
        "ok": host.ok,
        "backend": host.backend,
        "reason": host.detail,
        "commands": list(host.commands),
        "harnesses": [],
    }
    for harness in args.harness:
        check = check_harness_flag(harness, args.bin)
        off, why = bypass_disabled(harness, args.cwd, binary=args.bin)
        check["bypass_disabled"] = off
        check["bypass_reason"] = why
        report["harnesses"].append(check)
        report["ok"] = report["ok"] and check["ok"] and off
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
