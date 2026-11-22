"""Service probes: what a hub unit runs with, and whether it is up (task #1722).

The systemd and LaunchAgent probes ``system_doctor`` reports with, moved out of
``tools/doctor.py`` so the executor, the delivery receipts and the Horizon 2
tools can use them without importing the doctor, which is an adapter. Each
probe reads the SERVICE's own configuration (unit ``Environment=`` plus
``EnvironmentFile=``, or the Mac LaunchAgent plist and env files), never the
caller's environment, and reports "cannot tell" rather than "off" when that
configuration is unreadable.

``tools.doctor`` re-exports every name in ``DOCTOR_REEXPORTS``, and a patch
made through either module reaches every caller (see doctor's
``_ProbeForwardingModule``). The timer exit-code policy stays in the doctor:
``_unit_check`` takes it as arguments.
"""

from __future__ import annotations

import contextvars
import functools
import os
import plistlib
import shlex
import subprocess
import sys
import types
import urllib.error
from collections.abc import Callable, Mapping
from pathlib import Path

from . import config
from .monitor import SERVICE_CHECK_TIMEOUT_S, _service_probe, launchd_label_for_unit

_SERVICE_UNITS = {
    "hub_service": "unified-agent.service",
    "active_executor_service": "unified-agent-executor.service",
    "zulip_worker_service": "unified-agent-zulip-worker.service",
}


# Bounded so an unreachable or hanging hub cannot stall the whole report. Kept
# below SERVICE_CHECK_TIMEOUT_S: this is a loopback request in the normal case.
HUB_PROBE_TIMEOUT_S = 3.0


def _check(capability: str, status: str, detail: str, gate: str) -> dict:
    return {"capability": capability, "status": status, "detail": detail, "gate": gate}


# Per-run memo of `_unit_property` readings (task #1905). Without it one
# `_deployment_mode_check` spawned systemctl ~46 times: `_service_env` reads two
# properties for every one of ~22 hub settings. Installed for one doctor run
# (`_execute_run`) or one standalone deployment-mode check, never globally, so a
# later run always takes a new reading.
_unit_property_memo: contextvars.ContextVar[dict[tuple[str, str], str | None] | None] = (
    contextvars.ContextVar("system_doctor_unit_property_memo", default=None)
)


def _systemctl_memo(fn):
    """Run ``fn`` with a `_unit_property` memo, or inside the caller's run memo."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if _unit_property_memo.get() is not None:
            return fn(*args, **kwargs)
        token = _unit_property_memo.set({})
        try:
            return fn(*args, **kwargs)
        finally:
            _unit_property_memo.reset(token)

    return wrapper


def _unit_property(unit: str, prop: str) -> str | None:
    """`systemctl --user show <unit> -p <prop> --value`, or None on probe failure.

    Inside a doctor run the first reading of each (unit, prop) is reused,
    a failed probe included, so the run spawns systemctl once per pair.
    """
    memo = _unit_property_memo.get()
    if memo is None:
        return _read_unit_property(unit, prop)
    key = (unit, prop)
    if key not in memo:
        memo[key] = _read_unit_property(unit, prop)
    return memo[key]


def _read_unit_property(unit: str, prop: str) -> str | None:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", prop, "--value"],
            capture_output=True,
            text=True,
            timeout=SERVICE_CHECK_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


# LaunchAgent entrypoints source these files; systemd EnvironmentFile= does not
# exist on the Mac hub. Kept in sync with deploy/mac-hub/run-*.sh.
_MAC_ENVIRONMENT_FILES = {
    "unified-agent.service": ("~/.unified_agent/service.env",),
    "unified-agent-executor.service": ("~/.config/unified-agent/executor.env",),
    "unified-agent-zulip-worker.service": ("~/.unified_agent/service.env",),
    "com.unified-agent.pr-watch": ("~/.unified_agent/service.env",),
}


def _mac_home() -> Path:
    """Home directory the LaunchAgent config is read from.

    The single seam for every Mac service-config path (plists, env files), so
    tests can point it at a tmp dir and never read the real host's hub config
    (task #1373: on the Mac hub the live plist leaked into doctor tests).
    """
    return Path.home()


def _mac_environment_files(unit: str) -> list[Path]:
    home = _mac_home()
    return [home / p.removeprefix("~/") for p in _MAC_ENVIRONMENT_FILES.get(unit, ())]


def _mac_launchagent_plist(unit: str) -> Path | None:
    label = launchd_label_for_unit(unit)
    if not label:
        return None
    return _mac_home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _literal_shell_exports(path: Path) -> dict[str, str] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    env: dict[str, str] = {}
    for line in lines:
        statement = line.strip()
        if not statement.startswith("export "):
            continue
        key, sep, raw_value = statement.removeprefix("export ").partition("=")
        if not sep or not key.isidentifier():
            continue
        if not raw_value.strip():
            env[key] = ""
            continue
        try:
            values = shlex.split(raw_value, comments=True, posix=True)
        except ValueError:
            if key == "UA_EXECUTOR_AGENT":
                return None
            continue
        literal = len(values) == 1 and "$" not in values[0] and "`" not in values[0]
        if not literal:
            if key == "UA_EXECUTOR_AGENT":
                return None
            continue
        env[key] = values[0]
    return env


def _mac_launchagent_environment_layers(
    unit: str,
) -> tuple[dict[str, str], dict[str, str], bool] | None:
    plist_path = _mac_launchagent_plist(unit)
    if plist_path is None or not plist_path.is_file():
        return None
    try:
        with plist_path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return {}, {}, False
    if not isinstance(data, dict):
        # Preserve the already-landed #1803 guard while moving the helper:
        # plist roots may be None, a list or another scalar.
        return {}, {}, False
    raw_env = data.get("EnvironmentVariables", {})
    if not isinstance(raw_env, dict):
        return {}, {}, False
    if any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in raw_env.items()
    ):
        return {}, {}, False
    base = dict(raw_env)
    args = data.get("ProgramArguments")
    if not isinstance(args, list) or not args or not isinstance(args[0], str):
        return base, {}, False
    launcher = Path(args[0]).expanduser()
    if launcher.suffix != ".sh":
        return base, {}, True
    exports = _literal_shell_exports(launcher)
    if exports is None:
        return base, {}, False
    return base, exports, True


def _unit_environment_files(unit: str) -> list[Path]:
    """EnvironmentFile= paths declared by a systemd user unit, plus Mac fallbacks."""
    props = _unit_property(unit, "EnvironmentFiles")
    paths = []
    for entry in (props or "").split():
        entry = entry.strip()
        if entry.startswith("/"):
            paths.append(Path(entry))
    if sys.platform == "darwin":
        for path in _mac_environment_files(unit):
            if path not in paths:
                paths.append(path)
    return paths


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep:
                # init writes values with shlex.quote(); parse them the way a
                # sourcing shell would, so an apostrophe-containing path like
                # '/home/x/it'"'"'s ws' round-trips instead of being mangled
                # by a naive quote-strip (tests/test_bootstrap.py).
                try:
                    parts = shlex.split(value)
                except ValueError:
                    parts = []
                env[key.strip()] = parts[0] if len(parts) == 1 else value.strip().strip("\"'")
    except OSError:
        return {}
    return env


def _service_env(unit: str) -> dict[str, str] | None:
    """The environment the SERVICE runs with: unit Environment= plus EnvironmentFile=.

    system_doctor is usually invoked from a shell or from the hub process,
    neither of which carries a given unit's environment. Reading `os.environ`
    and reporting on it describes the CALLER, not the service — which is how
    `hub_auth` came to report "no bearer tokens configured — every HTTP caller
    is anonymous" about a hub running UA_ENFORCE_AUTH=1 with three tokens.

    Returns None when the unit's configuration cannot be determined at all, so
    callers can say "cannot tell" rather than asserting "off" (task #176).
    On Darwin, systemd properties are unavailable; readable LaunchAgent env
    files are the determined source. Missing files stay "cannot tell".
    """
    props = _unit_property(unit, "Environment")
    files = _unit_environment_files(unit)
    if props is None:
        if sys.platform != "darwin":
            return None
        readable = [path for path in files if path.is_file()]
        layers = _mac_launchagent_environment_layers(unit)
        if layers is None:
            if not readable:
                return None
            env: dict[str, str] = {}
            launcher_exports: dict[str, str] = {}
        else:
            env, launcher_exports, determined = layers
            if not determined:
                return None
        for env_path in readable:
            env.update(_load_env_file(env_path))
        env.update(launcher_exports)
        return env
    env = {}
    for entry in props.split():
        key, sep, value = entry.partition("=")
        if sep:
            env[key] = value
    # EnvironmentFile is applied after Environment=, so it wins on conflict.
    for env_path in files:
        env.update(_load_env_file(env_path))
    return env


def connector_secret_environment() -> tuple[dict[str, str], bool]:
    """Return the connector secret source and whether hub config is readable.

    Connector status describes the hub service, so a readable hub environment
    is authoritative. The caller environment is only a fallback when that
    service configuration cannot be inspected.
    """
    hub_env = _service_env(_SERVICE_UNITS["hub_service"])
    if hub_env is not None:
        return dict(hub_env), True
    return dict(os.environ), False


def _executor_token(agent_label: str, unit: str | None = None) -> str | None:
    """Resolve the token the EXECUTOR would present, not the one this shell has.

    Same order as executor.daemon.HubClient.__init__, but with the unit's own
    ``EnvironmentFile`` consulted first. The doctor usually runs from a shell or
    from the hub process, neither of which loads the executor's environment, so
    reading only ``os.environ`` made it report "no bearer token resolves — it
    cannot authenticate" about a correctly-credentialed daemon. Asserting a
    misconfiguration that is really just the doctor looking in the wrong place
    is its own kind of false report.
    """
    env = _service_env(unit or _SERVICE_UNITS["active_executor_service"]) or {}
    for key in ("UA_EXECUTOR_TOKEN", "UA_AGENT_BEARER_TOKEN"):
        if env.get(key):
            return env[key]
    direct = os.environ.get("UA_EXECUTOR_TOKEN") or os.environ.get("UA_AGENT_BEARER_TOKEN")
    if direct:
        return direct
    for token, label in config.agent_tokens().items():
        if label == agent_label:
            return token
    return None


def _executor_agent_label(unit: str | None = None) -> tuple[str, bool]:
    """Resolve the identity the EXECUTOR unit actually claims, not the caller's.

    Same rationale as ``_executor_token``: ``config.executor_agent()`` reads
    only ``os.environ``, which describes whoever invoked ``system_doctor`` —
    usually a shell or the hub process — not the systemd/launchd unit. Task
    #1215 caught this reporting a correctly-configured, actively-polling
    executor (``UA_EXECUTOR_AGENT`` set right in the unit's own
    ``Environment=``) as MISCONFIGURED with "UA_EXECUTOR_AGENT is unset",
    purely because the desktop shell running `unified-agent doctor` did not
    itself export the variable. Prefer the unit's own environment
    (``Environment=`` plus ``EnvironmentFile=``, via ``_service_env``) and
    fall back to ``config.executor_agent()`` only when the unit's environment
    cannot be determined at all. Returns ``(label, determined)`` — determined
    is False when neither the unit configuration nor the caller environment
    can answer, so "cannot tell" never masquerades as "unset".
    """
    env = _service_env(unit or _SERVICE_UNITS["active_executor_service"])
    if env is not None:
        return env.get("UA_EXECUTOR_AGENT", "").strip(), True
    if "UA_EXECUTOR_AGENT" in os.environ:
        return config.executor_agent().strip(), True
    return "", False


def _hub_auth_probe(hub_url: str, token: str, timeout_s: float) -> tuple[bool, str]:
    """One real hub round-trip using the DAEMON'S OWN client. Returns (ok, detail).

    Uses ``HubClient.ping`` with ``allow_fallback=False``, so the check exercises
    the exact request the executor makes. The first version of this check built
    its own request instead, and that gap bit immediately: it sent the ``Accept``
    header the daemon lacked, so it would have reported LIVE while every one of
    the daemon's calls died on ``406 Not Acceptable``. A health check that does
    not use the code path it is vouching for is a false all-clear waiting to
    happen — which is the whole reason #174 existed.

    ``allow_fallback=False`` matters just as much: it guarantees the probe can
    never satisfy itself through the in-process bypass PR #82 removed.

    The client is ``hub_client.HubClient``, the transport the executor's own
    ``HubClient`` subclasses (task #1724). The subclass adds only the
    in-process fallback, which ``allow_fallback=False`` never reaches, so the
    request is the executor's and this module stays below the runtime layer
    (task #1722).
    """
    from .hub_client import HubClient

    try:
        client = HubClient(
            agent_label=config.executor_agent(),
            hub_url=hub_url,
            bearer_token=token,
            allow_fallback=False,
        )
        _bounded_ping(client, timeout_s)
        return True, f"authenticated to {hub_url}, MCP session established"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"hub rejected the executor credential: HTTP {e.code} at {hub_url}"
        return False, f"hub round-trip failed at {hub_url}: HTTP {e.code}"
    except Exception as e:
        return False, f"hub round-trip failed at {hub_url}: {e}"


def _running_inside_http_hub() -> bool:
    """True when this process is serving the HTTP hub itself.

    The executor auth probe must call the hub over HTTP. Doing that from inside
    a hub-served ``system_doctor`` tool call is a self-call: the hub is occupied
    serving the current request, so the nested request can time out even when the
    executor is healthy. CLI callers do not import ``unified_agent.server`` and
    still run the real auth probe.
    """
    return config.transport() == "http" and "unified_agent.server" in sys.modules


def _bounded_ping(client, timeout_s: float) -> None:
    """client.ping() with the probe's timeout applied to each request."""
    original = client._post

    def bounded(body, timeout=timeout_s):
        return original(body, timeout=min(timeout, timeout_s))

    client._post = bounded
    client.ping()


def _active_executor_check() -> dict:
    """LIVE only when the executor unit is active AND can authenticate to the hub.

    A unit-active check alone reported the Active Executor Daemon healthy for six
    days (2026-07-19 → 07-25) while every one of its polls was 401-rejected and it
    had done no work since 07-24 — 27,342 rejections in the hub journal. "Running"
    and "running and able to do its job" are different questions, and only the
    second one is worth reporting. See tasks #173/#174 and PR #82.
    """
    unit = _SERVICE_UNITS["active_executor_service"]
    display = _unit_display(unit)
    base = _unit_check("active_executor_service", unit)
    if base["status"] != "LIVE":
        # Not installed, not active, or unprobeable — nothing to authenticate.
        return base

    agent_label, agent_label_determined = _executor_agent_label(unit)
    gate = f"{display} + UA_HTTP_HUB_URL + UA_EXECUTOR_TOKEN/UA_AGENT_TOKENS"

    if not agent_label_determined:
        return _check(
            "active_executor_service",
            "UNKNOWN",
            f"{display}: active but cannot determine UA_EXECUTOR_AGENT from unit configuration",
            gate,
        )

    if not agent_label:
        return _check(
            "active_executor_service",
            "MISCONFIGURED",
            f"{display}: active but UA_EXECUTOR_AGENT is unset — "
            "refusing a compiled fleet identity",
            gate,
        )

    try:
        hub_url = config.http_hub_url(agent_label)
    except RuntimeError as exc:
        return _check("active_executor_service", "MISCONFIGURED", f"{display}: {exc}", gate)

    token = _executor_token(agent_label)
    if not token:
        return _check(
            "active_executor_service",
            "MISCONFIGURED",
            f"{display}: active but no bearer token resolves for agent {agent_label!r} — "
            "it cannot authenticate to the hub",
            gate,
        )

    if _running_inside_http_hub():
        return _check(
            "active_executor_service",
            "UNKNOWN",
            f"{display}: active, auth probe skipped during a hub-served system_doctor call "
            "because the required HTTP round-trip would be a self-call; run system_doctor "
            "from a separate process to verify executor authentication",
            gate,
        )

    try:
        ok, detail = _hub_auth_probe(hub_url, token, HUB_PROBE_TIMEOUT_S)
    except Exception as e:
        # Doctor's contract: a probe failure surfaces on this check only.
        return _check(
            "active_executor_service", "MISCONFIGURED", f"{display}: auth probe failed: {e}", gate
        )

    status = "LIVE" if ok else "MISCONFIGURED"
    # Keep the stable four-field doctor contract.  Provenance makes the
    # authoritative standalone probe distinguishable from the deliberate
    # UNKNOWN result inside the hub process without adding a new field.
    return _check(
        "active_executor_service",
        status,
        f"{display}: active, probe_process=separate, authenticated={ok}; {detail}",
        gate,
    )


def _unit_display(unit: str) -> str:
    """Human-facing probe target: systemd unit, plus LaunchAgent label on Darwin."""
    label = launchd_label_for_unit(unit) if sys.platform == "darwin" else None
    return f"{unit} / {label}" if label else unit


def _unit_check(
    capability: str,
    unit: str,
    designed_exit_codes: Mapping[str, Mapping[str, str]] | None = None,
    designed_exit_evidence: Mapping[str, Mapping[str, Callable[[], bool]]] | None = None,
) -> dict:
    """LIVE / DORMANT / DEGRADED / MISCONFIGURED / UNKNOWN for one unit.

    ``designed_exit_codes`` maps a capability to the nonzero exit codes its own
    script uses as a deliberate signal, and ``designed_exit_evidence`` to the
    check each code must pass first. The doctor passes its timer tables; with
    none, every nonzero exit is MISCONFIGURED.
    """
    display = _unit_display(unit)
    props = _service_probe(unit, SERVICE_CHECK_TIMEOUT_S)
    if props is None:
        return _check(
            capability,
            "UNKNOWN" if sys.platform == "darwin" else "DORMANT",
            f"{display}: probe unavailable "
            "(no systemd --user session, launchctl, or systemctl missing)",
            unit,
        )
    if props.get("LoadState") != "loaded":
        return _check(capability, "DORMANT", f"{display}: not installed on this host", unit)
    active = props.get("ActiveState")
    if active == "active":
        return _check(capability, "LIVE", f"{display}: active", unit)
    if active == "unknown":
        return _check(
            capability,
            "UNKNOWN",
            f"{display}: never run yet and its first scheduled fire looks overdue, but it may "
            "simply have been bootstrapped well after its plist was installed (this repo ships "
            "these timers copied-but-not-bootstrapped for later manual enablement) — re-check "
            "after the grace window if this persists",
            unit,
        )
    last_exit = props.get("LastExitCode")
    designed = (designed_exit_codes or {}).get(capability)
    evidence = (designed_exit_evidence or {}).get(capability, {}).get(last_exit or "")
    if designed and last_exit in designed and (evidence is None or evidence()):
        return _check(
            capability,
            "DEGRADED",
            f"{display}: ran, exited {last_exit} — {designed[last_exit]}",
            unit,
        )
    return _check(capability, "MISCONFIGURED", f"{display}: installed but {active}", unit)


# Names ``tools.doctor`` re-exports, and forwards patches for. ``_unit_check``
# is not one: the doctor keeps a wrapper that applies its timer tables.
DOCTOR_REEXPORTS = (
    "SERVICE_CHECK_TIMEOUT_S",
    "_service_probe",
    "launchd_label_for_unit",
    "_SERVICE_UNITS",
    "HUB_PROBE_TIMEOUT_S",
    "_check",
    "_unit_property_memo",
    "_systemctl_memo",
    "_unit_property",
    "_read_unit_property",
    "_MAC_ENVIRONMENT_FILES",
    "_mac_home",
    "_mac_environment_files",
    "_mac_launchagent_plist",
    "_literal_shell_exports",
    "_mac_launchagent_environment_layers",
    "_unit_environment_files",
    "_load_env_file",
    "_service_env",
    "connector_secret_environment",
    "_executor_token",
    "_executor_agent_label",
    "_hub_auth_probe",
    "_running_inside_http_hub",
    "_bounded_ping",
    "_active_executor_check",
    "_unit_display",
)

#: Namespaces that re-export ``DOCTOR_REEXPORTS``: the doctor registers its
#: module globals at import. A rebinding of one of those names here (a patch,
#: or its undo) is copied into each, so a direct read through the doctor and
#: the doctor's own bare-name callers see the same object as this module.
_REEXPORT_MIRRORS: list[dict] = []


def register_reexport_namespace(namespace: dict) -> None:
    """Keep ``namespace``'s copies of ``DOCTOR_REEXPORTS`` in step with this module."""
    if not any(existing is namespace for existing in _REEXPORT_MIRRORS):
        _REEXPORT_MIRRORS.append(namespace)


class _ReexportSyncModule(types.ModuleType):
    """This module, copying a rebinding of a re-exported name to its mirrors."""

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name in DOCTOR_REEXPORTS:
            for namespace in _REEXPORT_MIRRORS:
                namespace[name] = value


sys.modules[__name__].__class__ = _ReexportSyncModule
