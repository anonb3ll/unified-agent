"""One doctor run asks systemctl for each unit property once (task #1905).

``_deployment_mode_check`` reads ~22 hub settings, each through
``_service_env``, which spawned ``systemctl --user show`` twice per setting:
~46 processes and ~0.3 s per run. A per-run memo answers repeats from the
first reading, so the results are the same and the processes are not.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from types import SimpleNamespace

import pytest

from unified_agent.tools import doctor

_HUB = doctor._SERVICE_UNITS["hub_service"]


@pytest.fixture
def systemctl(monkeypatch, tmp_path):
    """A fake ``systemctl --user show`` for the hub unit; counts every spawn."""
    env_file = tmp_path / "service.env"
    env_file.write_text("UA_AGENT_TOKENS=worker:fixture-token\n")
    props = {
        "Environment": "UA_DEPLOYMENT_MODE=authenticated_private UA_ENFORCE_AUTH=1",
        "EnvironmentFiles": f"{env_file} (ignore_errors=no)",
    }
    calls: Counter = Counter()
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if not (isinstance(cmd, list) and cmd[:3] == ["systemctl", "--user", "show"]):
            return real_run(cmd, *args, **kwargs)
        unit, prop = cmd[3], cmd[5]
        calls[(unit, prop)] += 1
        value = props.get(prop, "") if unit == _HUB else ""
        return SimpleNamespace(returncode=0, stdout=value + "\n", stderr="")

    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    return calls


def test_deployment_mode_check_spawns_systemctl_once_per_unit_property(systemctl):
    result = doctor._deployment_mode_check()

    assert systemctl, "the check should have read the hub unit"
    assert max(systemctl.values()) == 1, dict(systemctl)
    assert result["capability"] == "deployment_mode"


def test_memo_lasts_one_run_and_results_are_identical(systemctl):
    first = doctor._deployment_mode_check()
    per_run = sum(systemctl.values())
    second = doctor._deployment_mode_check()

    # A later run takes a new reading: the memo does not outlive its run.
    assert sum(systemctl.values()) == 2 * per_run
    assert first == second
    # Outside a run nothing is memoized.
    before = sum(systemctl.values())
    doctor._unit_property(_HUB, "Environment")
    doctor._unit_property(_HUB, "Environment")
    assert sum(systemctl.values()) == before + 2


def test_memoized_run_matches_an_unmemoized_reading(systemctl):
    memoized = doctor._deployment_mode_check()
    plain = doctor._deployment_mode_check.__wrapped__()

    assert memoized == plain
    assert max(systemctl.values()) > 1  # the unwrapped body re-spawned


def test_full_doctor_run_spawns_each_unit_property_once(systemctl, monkeypatch):
    """The memo spans the whole run, not just the deployment-mode row."""
    rows: list[dict] = []

    def some_rows():
        rows.extend(
            [
                doctor._hub_auth_check(),
                doctor._deployment_mode_check(),
                doctor._approval_gates_check(),
            ]
        )
        return {"checks": rows}

    monkeypatch.setattr(doctor, "_run_system_doctor", some_rows)
    run = doctor._DoctorRun(background=False, generation=doctor._report_generation)
    doctor._execute_run(run)

    assert run.error is None
    assert len(rows) == 3
    assert max(systemctl.values()) == 1, dict(systemctl)
