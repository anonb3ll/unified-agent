"""Doctor/Studio runtime truth from shared probes, with OS boundaries mocked."""

import pytest

from unified_agent import monitor
from unified_agent.dispatch import routing
from unified_agent.tools import doctor


@pytest.mark.parametrize("returncode", [0, 1, 4])
@pytest.mark.parametrize(
    "stdout,stderr",
    [
        ("LoadState=not-found\nActiveState=active\n", ""),
        ("LoadState=not-found\n", ""),
        ("LoadState=not-found\nActiveState=inactive\n", "Permission denied"),
        ("LoadState=not-found\nActiveState=inactive\nNRestarts=garbage\n", ""),
        ("LoadState=not-found\nActiveState=inactive\nNRestarts=1\n", ""),
        ("LoadState=loaded\nLoadState=not-found\nActiveState=inactive\n", ""),
        ("LoadState=not-found\nActiveState=inactive\nmalformed\n", ""),
    ],
)
def test_systemd_rejects_unproven_absence(monkeypatch, stdout, stderr, returncode):
    """Even exit zero cannot turn partial/contradictory/error output into absence."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )
    assert monitor._systemd_probe("ua-dispatch-tick.timer", 1) is None


@pytest.mark.parametrize("restarts", ["", "NRestarts=0\n"])
def test_systemd_valid_absence_across_surfaces(monkeypatch, restarts):
    """v261.2 show returns zero; timers omit the service-only NRestarts property."""
    from types import SimpleNamespace

    monkeypatch.setattr(monitor.sys, "platform", "linux")
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0,
            stdout="LoadState=not-found\nActiveState=inactive\n" + restarts,
            stderr="",
        ),
    )
    assert monitor._systemd_probe("ua-dispatch-tick.timer", 1)["LoadState"] == "not-found"
    assert routing.scheduler_runtime_state()["status"] == "retired"
    assert doctor._dispatch_scheduler_check()["status"] == "DORMANT"
    assert (
        doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer")["status"]
        == "DORMANT"
    )


@pytest.mark.parametrize("returncode", [1, 4])
@pytest.mark.parametrize("stderr", ["", "Unit ua-dispatch-tick.timer could not be found."])
@pytest.mark.parametrize("restarts", ["", "NRestarts=0\n"])
def test_systemd_nonzero_valid_absence_across_surfaces(monkeypatch, returncode, stderr, restarts):
    """Bounded compatibility; local v261.2 show itself returns zero for absence."""
    from types import SimpleNamespace

    monkeypatch.setattr(monitor.sys, "platform", "linux")
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=returncode,
            stdout="LoadState=not-found\nActiveState=inactive\n" + restarts,
            stderr=stderr,
        ),
    )
    expected = {"LoadState": "not-found", "ActiveState": "inactive"}
    if restarts:
        expected["NRestarts"] = "0"
    assert monitor._systemd_probe("ua-dispatch-tick.timer", 1) == expected
    runtime = routing.scheduler_runtime_state()
    assert runtime["status"] == runtime["mode"] == "retired"
    assert runtime["enabled"] is runtime["real_spawn"] is False
    assert doctor._dispatch_scheduler_check()["status"] == "DORMANT"
    assert (
        doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer")["status"]
        == "DORMANT"
    )


@pytest.mark.parametrize(
    "returncode,stderr",
    [
        (1, "Permission denied"),
        (4, "Permission denied"),
        (1, "Failed to connect to bus"),
        (4, "Unit ua-dispatch-tick.timer could not be found.\nFailed to connect to bus"),
        (4, "Unit another.timer could not be found."),
        (2, ""),
        (7, ""),
        (-15, ""),
    ],
)
def test_systemd_rejects_nonzero_errors(monkeypatch, returncode, stderr):
    """Absence properties cannot override errors or unsupported exit codes."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=returncode,
            stdout="LoadState=not-found\nActiveState=inactive\nNRestarts=0\n",
            stderr=stderr,
        ),
    )
    assert monitor._systemd_probe("ua-dispatch-tick.timer", 1) is None


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("active", ["active", "inactive", "unknown"])
@pytest.mark.parametrize("gates", [(False, False), (False, True), (True, False), (True, True)])
def test_scheduler_loaded_runtime(monkeypatch, platform, active, gates):
    monkeypatch.setattr(routing.sys, "platform", platform)
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", str(int(gates[0])))
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", str(int(gates[1])))
    calls = []

    def probe(unit, timeout):
        calls.append(unit)
        return {"LoadState": "loaded", "ActiveState": active}

    monkeypatch.setattr(monitor, "_service_probe", probe)
    runtime = routing.scheduler_runtime_state()
    check = doctor._dispatch_scheduler_check()
    unit_check = doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer")
    assert unit_check["status"] == "MISCONFIGURED"
    assert check["status"] == "MISCONFIGURED"
    assert runtime["status"] == f"misconfigured_{active}"
    expected_enabled = gates[0] and (platform == "darwin" or active == "active")
    assert runtime["enabled"] == expected_enabled
    assert runtime["real_spawn"] == (expected_enabled and gates[1])
    assert f"mode={runtime['mode']}" in check["detail"]
    assert f"enabled={runtime['enabled']}" in check["detail"]
    assert f"real_spawn={runtime['real_spawn']}" in check["detail"]
    expected_mode = (
        "native_cli"
        if expected_enabled and gates[1]
        else "stub"
        if expected_enabled
        else "disabled"
    )
    assert runtime["mode"] == expected_mode
    assert calls == ["ua-dispatch-tick.timer"] * 3


@pytest.mark.parametrize(
    "platform,props,disabled,status",
    [
        ("linux", {"LoadState": "not-found"}, None, "DORMANT"),
        ("darwin", {"LoadState": "not-found"}, True, "DORMANT"),
        ("darwin", {"LoadState": "not-found"}, False, "UNKNOWN"),
        ("darwin", {"LoadState": "not-found"}, None, "UNKNOWN"),
        ("linux", None, None, "UNKNOWN"),
        ("darwin", None, None, "UNKNOWN"),
        ("linux", {}, None, "UNKNOWN"),
        ("darwin", {"LoadState": "invalid"}, None, "UNKNOWN"),
    ],
)
def test_scheduler_absence_and_unknown(monkeypatch, platform, props, disabled, status):
    monkeypatch.setattr(routing.sys, "platform", platform)
    monkeypatch.setattr(monitor, "_service_probe", lambda *a: props)
    monkeypatch.setattr(monitor, "_launchd_disabled_override", lambda *a: disabled)
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    runtime = routing.scheduler_runtime_state()
    assert doctor._dispatch_scheduler_check()["status"] == status
    assert runtime["status"] == ("retired" if status == "DORMANT" else "unknown")
    assert runtime["enabled"] is False
    assert runtime["real_spawn"] is False


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize(
    "props,status",
    [
        (None, "UNKNOWN"),
        ({}, "UNKNOWN"),
        ({"LoadState": "invalid"}, "UNKNOWN"),
        ({"LoadState": "not-found"}, "DORMANT"),
        ({"LoadState": "loaded", "ActiveState": "active"}, "MISCONFIGURED"),
        ({"LoadState": "loaded", "ActiveState": "inactive"}, "MISCONFIGURED"),
    ],
)
def test_generic_retired_probe_contract(monkeypatch, platform, props, status):
    monkeypatch.setattr(doctor.sys, "platform", platform)
    monkeypatch.setitem(doctor._RETIRED_UNITS, "generic_retired", ("#test", "retired"))
    monkeypatch.setattr(doctor, "_service_probe", lambda *a: props)
    assert doctor._retired_unit_check("generic_retired", "generic.service")["status"] == status


@pytest.mark.parametrize(
    "case,expected",
    [
        ("timeout", "UNKNOWN"),
        ("permission", "UNKNOWN"),
        ("malformed", "UNKNOWN"),
        ("absent", "UNKNOWN"),
        ("disabled", "DORMANT"),
        ("active", "MISCONFIGURED"),
        ("inactive", "MISCONFIGURED"),
    ],
)
def test_real_launchd_probe_across_surfaces(monkeypatch, case, expected):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(monitor.sys, "platform", "darwin")
    monkeypatch.setattr(monitor.os, "getuid", lambda: 501)
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "print-disabled":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    '"com.unified-agent.dispatch-tick" => '
                    + ("disabled" if case == "disabled" else "enabled")
                ),
                stderr="",
            )
        assert cmd == ["launchctl", "print", "gui/501/com.unified-agent.dispatch-tick"]
        if case == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        if case == "permission":
            raise PermissionError("denied")
        if case in {"absent", "disabled"}:
            return SimpleNamespace(
                returncode=113,
                stdout="",
                stderr=(
                    'Could not find service "com.unified-agent.dispatch-tick" in domain for user gui: 501'
                ),
            )
        stdout = (
            "malformed"
            if case == "malformed"
            else ("state = not running\nlast exit code = " + ("0" if case == "active" else "1"))
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(monitor.subprocess, "run", run)
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    runtime = routing.scheduler_runtime_state()
    scheduler_check = doctor._dispatch_scheduler_check()
    before = len(calls)
    unit_check = doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer")
    assert unit_check["status"] == expected
    assert scheduler_check["status"] == expected
    assert runtime["status"] == (
        "retired"
        if expected == "DORMANT"
        else "unknown"
        if expected == "UNKNOWN"
        else "misconfigured_" + case
    )
    # One print per consumer; only proven absence permits print-disabled.
    assert len(calls) - before == (2 if case in {"absent", "disabled"} else 1)


@pytest.mark.parametrize(
    "case",
    [
        "absence_stdout",
        "override_stderr",
        "malformed_contradiction",
        "clean",
        "absence_wrapper",
        "override_wrapper",
        "other_labels",
        "malformed_other_label",
        "malformed_target",
        "malformed_target_first",
        "duplicate_disabled",
        "duplicate_contradictory",
        "wrong_override_label",
        "enabled",
        "wrong_absence_label",
        "wrong_uid",
        "absence_extra_error",
    ],
)
def test_launchd_retirement_evidence_integrity(monkeypatch, capsys, case):
    """Tainted OS evidence stays UNKNOWN through routing, both rows, and CLI."""
    import json
    from types import SimpleNamespace

    from unified_agent import bootstrap

    label = "com.unified-agent.dispatch-tick"
    target = f'"{label}" => disabled'
    missing = f'Could not find service "{label}" in domain for user gui: 501'
    stdout = "state = running\n" if case == "absence_stdout" else ""
    stderr = {
        "absence_wrapper": f"Bad request.\n{missing}",
        "wrong_absence_label": missing.replace(label, label + "-other"),
        "wrong_uid": missing.replace("501", "502"),
        "absence_extra_error": missing + "\nPermission denied",
    }.get(case, missing)
    override = {
        "malformed_contradiction": target + f'\n"{label}" => enabled extra',
        "override_wrapper": f"disabled services = {{\n  {target}\n}}\n",
        "other_labels": f'"{label}-other" => enabled\n{target}\n"other.{label}" => disabled',
        "malformed_other_label": f'{target}\n"{label}-other" => enabled extra',
        "malformed_target": f'"{label}" => disabled extra',
        "malformed_target_first": f'"{label}" => enabled extra\n{target}',
        "duplicate_disabled": target + "\n" + target,
        "duplicate_contradictory": target + f'\n"{label}" => enabled',
        "wrong_override_label": f'"{label}-other" => disabled',
        "enabled": f'"{label}" => enabled',
    }.get(case, target)
    monkeypatch.setattr(monitor.sys, "platform", "darwin")
    monkeypatch.setattr(monitor.os, "getuid", lambda: 501)
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": monitor.SERVICE_CHECK_TIMEOUT_S,
        }
        if cmd == ["launchctl", "print-disabled", "gui/501"]:
            return SimpleNamespace(
                returncode=0,
                stdout=override,
                stderr="Permission denied" if case == "override_stderr" else "",
            )
        assert cmd == ["launchctl", "print", f"gui/501/{label}"]
        return SimpleNamespace(returncode=113, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(monitor.subprocess, "run", run)
    runtime = routing.scheduler_runtime_state()
    rows = [
        doctor._dispatch_scheduler_check(),
        doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer"),
    ]
    retired = case in {
        "clean",
        "absence_wrapper",
        "override_wrapper",
        "other_labels",
        "malformed_other_label",
    }
    state, status = ("retired", "DORMANT") if retired else ("unknown", "UNKNOWN")
    assert runtime == {"enabled": False, "real_spawn": False, "status": state, "mode": state}
    assert [row["status"] for row in rows] == [status, status]
    rejected_absence = case in {
        "absence_stdout",
        "wrong_absence_label",
        "wrong_uid",
        "absence_extra_error",
    }
    assert len(calls) == (3 if rejected_absence else 6)
    report = {
        "checks": rows,
        "summary": {
            s.lower(): sum(row["status"] == s for row in rows)
            for s in ("LIVE", "DORMANT", "UNKNOWN", "MISCONFIGURED", "DEGRADED")
        },
    }
    monkeypatch.setattr(bootstrap, "system_doctor", lambda: report)
    for args in ([], ["--json"]):
        assert bootstrap.doctor_main(args) == 0
        ordinary = capsys.readouterr().out
        assert bootstrap.doctor_main([*args, "--strict"]) == 1
        strict = capsys.readouterr().out
        assert ordinary == strict
        if args:
            assert json.loads(strict) == report
        else:
            assert status in strict


@pytest.mark.parametrize(
    "case,expected",
    [
        ("nonzero_not_found", "UNKNOWN"),
        ("timeout", "UNKNOWN"),
        ("permission", "UNKNOWN"),
        ("nonzero_empty", "UNKNOWN"),
        ("nonzero_loaded", "UNKNOWN"),
        ("malformed", "UNKNOWN"),
        ("absent", "DORMANT"),
        ("active", "MISCONFIGURED"),
        ("inactive", "MISCONFIGURED"),
    ],
)
def test_real_systemd_probe_across_surfaces(monkeypatch, case, expected):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(monitor.sys, "platform", "linux")
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd == [
            "systemctl",
            "--user",
            "show",
            "ua-dispatch-tick.timer",
            "--property=LoadState,ActiveState,NRestarts",
        ]
        if case == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        if case == "permission":
            raise PermissionError("denied")
        stdout = {
            "nonzero_not_found": "LoadState=not-found\nActiveState=inactive\n",
            "nonzero_empty": "",
            "nonzero_loaded": "LoadState=loaded\nActiveState=active\nNRestarts=0\n",
            "malformed": "malformed successful response",
            "absent": "LoadState=not-found\nActiveState=inactive\n",
            "active": "LoadState=loaded\nActiveState=active\nNRestarts=0\n",
            "inactive": "LoadState=loaded\nActiveState=inactive\nNRestarts=0\n",
        }[case]
        failed = case.startswith("nonzero_")
        return SimpleNamespace(
            returncode=1 if failed else 0,
            stdout=stdout,
            stderr="Permission denied" if failed else "",
        )

    monkeypatch.setattr(monitor.subprocess, "run", run)
    runtime = routing.scheduler_runtime_state()
    scheduler_check = doctor._dispatch_scheduler_check()
    unit_check = doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer")
    status = (
        "unknown"
        if expected == "UNKNOWN"
        else "retired"
        if expected == "DORMANT"
        else "misconfigured_" + case
    )
    assert (runtime["status"], scheduler_check["status"], unit_check["status"]) == (
        status,
        expected,
        expected,
    )
    enabled = case == "active"
    assert runtime["enabled"] is enabled
    assert runtime["real_spawn"] is enabled
    mode = "native_cli" if enabled else "disabled" if case == "inactive" else status
    assert runtime["mode"] == mode
    assert f"mode={mode}" in scheduler_check["detail"]
    assert f"enabled={enabled}" in scheduler_check["detail"]
    assert f"real_spawn={enabled}" in scheduler_check["detail"]
    assert len(calls) == 3
