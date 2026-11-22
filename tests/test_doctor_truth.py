"""Doctor rows that must not report LIVE over a broken job or setting (#1783, #1771).

#1783: ``github_approval_backlog`` read LIVE for ~40 minutes while the
pr-watch LaunchAgent exited 1 on every run, because a code default the
installation relied on had been removed. #1771: the live hub checkout moved
with an unrecorded ``git pull`` while the running hub kept the old code.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import github_app_worker, hub_runtime, pr_watch
from unified_agent.tools import doctor

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture(autouse=True)
def _hermetic_host(monkeypatch, tmp_path):
    """No real hub unit, LaunchAgent or connector setting may sway these rows."""
    monkeypatch.setattr(doctor, "_service_env", lambda unit: {})
    monkeypatch.setattr(doctor, "_mac_home", lambda: tmp_path / "mac-home")
    for name in (
        "UA_GITHUB_APP_ENABLED",
        "UA_GITHUB_CONNECTOR_REPO",
        "UA_GITHUB_TOKEN_MODE",
        "UA_GITHUB_USER_TOKEN_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)


def _install_pr_watch_plist(tmp_path):
    agents = tmp_path / "mac-home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "com.unified-agent.pr-watch.plist").write_text("<plist/>")


def _ago(**delta) -> datetime:
    return datetime.now(UTC) - timedelta(**delta)


def _write_launch_failure(when: datetime, exit_code: int) -> None:
    """The record deploy/mac-hub/run-pr-watch.sh writes when it fails early."""
    path = pr_watch.launch_failure_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"failed_at": when.isoformat(), "exit": exit_code}))


def _install_pr_watch_job(tmp_path, env: dict[str, str], service_env: str = "") -> None:
    """A real pr-watch LaunchAgent plus the service.env its launcher sources."""
    home = tmp_path / "mac-home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    launcher = tmp_path / "run-pr-watch.sh"
    launcher.write_text("#!/usr/bin/env bash\nexec true\n")
    with (agents / "com.unified-agent.pr-watch.plist").open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.unified-agent.pr-watch",
                "ProgramArguments": [str(launcher)],
                "EnvironmentVariables": env,
            },
            handle,
        )
    if service_env:
        (home / ".unified_agent").mkdir(parents=True, exist_ok=True)
        (home / ".unified_agent" / "service.env").write_text(service_env)


# ----------------------------------------------------------------- pr_watch_job


class TestPrWatchJob:
    def test_dormant_without_record_when_connector_off(self):
        row = doctor._pr_watch_job_check()
        assert row["capability"] == "pr_watch_job"
        assert row["status"] == "DORMANT"

    def test_unknown_without_record_when_connector_on(self, monkeypatch):
        monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
        row = doctor._pr_watch_job_check()
        assert row["status"] == "UNKNOWN"
        assert "no pr-watch run recorded" in row["detail"]

    def test_unknown_when_installed_but_no_run_recorded(self, tmp_path):
        """An installed journal job runs with the connector off; unverified is not dormant."""
        _install_pr_watch_plist(tmp_path)
        row = doctor._pr_watch_job_check()
        assert row["status"] == "UNKNOWN", row
        assert "installed" in row["detail"]

    def test_degraded_when_launcher_failed_before_the_entrypoint(self):
        """run-pr-watch.sh failing before Python starts must not read as healthy."""
        pr_watch.record_run_status(ok=True, now=_ago(minutes=20))
        _write_launch_failure(_ago(minutes=2), exit_code=127)
        row = doctor._pr_watch_job_check()
        assert row["status"] == "DEGRADED", row
        assert "launcher" in row["detail"] and "127" in row["detail"]

    def test_degraded_when_launcher_failed_and_no_run_recorded(self):
        _write_launch_failure(_ago(minutes=2), exit_code=1)
        assert doctor._pr_watch_job_check()["status"] == "DEGRADED"

    def test_a_later_recorded_run_supersedes_a_launcher_failure(self):
        _write_launch_failure(_ago(minutes=40), exit_code=127)
        pr_watch.record_run_status(ok=True, now=_ago(minutes=5))
        assert doctor._pr_watch_job_check()["status"] == "LIVE"

    def test_live_after_recent_success(self):
        pr_watch.record_run_status(ok=True, now=_ago(minutes=5))
        row = doctor._pr_watch_job_check()
        assert row["status"] == "LIVE", row

    def test_degraded_when_last_run_failed(self):
        pr_watch.record_run_status(ok=True, now=_ago(minutes=40))
        pr_watch.record_run_status(
            ok=False,
            error="vault tools are disabled; set UA_VAULT_PATH",
            now=_ago(minutes=5),
        )
        row = doctor._pr_watch_job_check()
        assert row["status"] == "DEGRADED", row
        assert "exited 1" in row["detail"]
        assert "UA_VAULT_PATH" in row["detail"]
        assert "last success" in row["detail"]

    def test_degraded_when_last_success_is_older_than_interval_plus_grace(self):
        stale = pr_watch.SCHEDULE_INTERVAL_S + pr_watch.SCHEDULE_GRACE_S + 60
        pr_watch.record_run_status(ok=True, now=_ago(seconds=stale))
        row = doctor._pr_watch_job_check()
        assert row["status"] == "DEGRADED", row
        assert "no successful run" in row["detail"]

    def test_within_grace_stays_live(self):
        pr_watch.record_run_status(ok=True, now=_ago(seconds=pr_watch.SCHEDULE_INTERVAL_S + 60))
        assert doctor._pr_watch_job_check()["status"] == "LIVE"

    def test_malformed_record_is_unknown(self):
        path = pr_watch.status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{oops")
        assert doctor._pr_watch_job_check()["status"] == "UNKNOWN"

    def test_degraded_while_a_skipped_repository_is_unresolved(self):
        """Task #2486: a later tick with no journal pass must not read as healthy."""
        pr_watch.record_run_status(
            ok=True, skipped=["example/x: gh pr list timed out 2 times"], now=_ago(minutes=6)
        )
        pr_watch.record_run_status(ok=True, connector=True, now=_ago(minutes=1))
        row = doctor._pr_watch_job_check()
        assert row["status"] == "DEGRADED", row
        assert "1 skip(s) unresolved" in row["detail"] and "example/x" in row["detail"]


# ------------------------------------------------------- github_approval_backlog


class TestApprovalBacklogTrustsOnlyARunningPoller:
    @pytest.fixture(autouse=True)
    def _connector_on(self, monkeypatch):
        monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
        monkeypatch.setattr(
            github_app_worker,
            "unconsumed_github_passes",
            lambda: {"fresh": 0, "stale": 0, "expired": 0},
        )

    def test_degraded_not_live_while_poller_fails(self):
        """The #1783 incident: zero stale passes because nothing polled."""
        pr_watch.record_run_status(
            ok=False,
            error="github-app connector poll failed; legacy PR journal completed",
            now=_ago(minutes=2),
        )
        row = doctor._github_approval_backlog_check()
        assert row["status"] == "DEGRADED", row
        assert "pr-watch" in row["detail"]

    def test_unknown_when_no_poller_run_recorded(self):
        assert doctor._github_approval_backlog_check()["status"] == "UNKNOWN"

    def test_live_when_poller_healthy_and_no_backlog(self):
        pr_watch.record_run_status(ok=True, now=_ago(minutes=1))
        assert doctor._github_approval_backlog_check()["status"] == "LIVE"

    def test_judged_on_the_connector_poll_not_the_journal(self):
        """Task #2486: a skipped journal repository does not stop the ledger poll."""
        pr_watch.record_run_status(
            ok=True, skipped=["example/x: timed out"], connector=True, now=_ago(minutes=1)
        )
        assert doctor._github_approval_backlog_check()["status"] == "LIVE"
        pr_watch.record_run_status(ok=False, error="poll failed", connector=False)
        pr_watch.record_run_status(ok=True, skipped=[], polled=["example/x"])
        row = doctor._github_approval_backlog_check()
        assert row["status"] == "DEGRADED" and "poll failed" in row["detail"], row


# ------------------------------------------------------------ required_settings


class TestRequiredSettings:
    def test_dormant_when_nothing_that_needs_settings_is_enabled(self):
        row = doctor._required_settings_check()
        assert row["capability"] == "required_settings"
        assert row["status"] == "DORMANT", row

    def test_misconfigured_when_connector_enabled_without_repo(self, monkeypatch):
        monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
        row = doctor._required_settings_check()
        assert row["status"] == "MISCONFIGURED", row
        assert "UA_GITHUB_CONNECTOR_REPO" in row["detail"]
        assert "github.connector_repo" in row["detail"]

    def test_misconfigured_when_connector_repo_is_malformed(self, monkeypatch):
        monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
        monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "not-a-repo")
        row = doctor._required_settings_check()
        assert row["status"] == "MISCONFIGURED", row
        assert "invalid" in row["detail"]

    def test_live_when_connector_repo_set(self, monkeypatch):
        monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
        monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
        assert doctor._required_settings_check()["status"] == "LIVE"

    def test_hub_service_env_supplies_the_setting(self, monkeypatch):
        monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
        monkeypatch.setattr(
            doctor, "_service_env", lambda unit: {"UA_GITHUB_CONNECTOR_REPO": "example/project"}
        )
        assert doctor._required_settings_check()["status"] == "LIVE"

    def test_misconfigured_when_vault_job_installed_without_vault(self, monkeypatch, tmp_path):
        _install_pr_watch_plist(tmp_path)
        monkeypatch.setenv("UA_VAULT_PATH", "")
        row = doctor._required_settings_check()
        assert row["status"] == "MISCONFIGURED", row
        assert "UA_VAULT_PATH" in row["detail"]
        assert "paths.vault" in row["detail"]
        assert "pr-watch" in row["detail"]

    def test_recorded_pr_watch_run_counts_as_installed(self, monkeypatch):
        pr_watch.record_run_status(ok=True)
        monkeypatch.setenv("UA_VAULT_PATH", "")
        assert doctor._required_settings_check()["status"] == "MISCONFIGURED"

    def test_misconfigured_when_vault_path_is_not_a_directory(self, monkeypatch, tmp_path):
        _install_pr_watch_plist(tmp_path)
        monkeypatch.setenv("UA_VAULT_PATH", str(tmp_path / "missing-vault"))
        row = doctor._required_settings_check()
        assert row["status"] == "MISCONFIGURED", row
        assert "invalid" in row["detail"]

    def test_live_when_vault_job_has_a_vault(self, monkeypatch, tmp_path):
        _install_pr_watch_plist(tmp_path)
        vault = tmp_path / "vault-ok"
        vault.mkdir()
        monkeypatch.setenv("UA_VAULT_PATH", str(vault))
        assert doctor._required_settings_check()["status"] == "LIVE"

    def test_hub_vault_does_not_cover_the_pr_watch_job(self, monkeypatch, tmp_path):
        """pr-watch runs from its own LaunchAgent and service.env, not the hub's plist."""
        vault = tmp_path / "vault-ok"
        vault.mkdir()
        monkeypatch.setenv("UA_VAULT_PATH", str(vault))  # the hub (caller) has a vault
        _install_pr_watch_job(tmp_path, env={"UA_HOME": str(tmp_path / "ua")})
        row = doctor._required_settings_check()
        assert row["status"] == "MISCONFIGURED", row
        assert "UA_VAULT_PATH" in row["detail"]
        assert "pr-watch" in row["detail"]

    def test_pr_watch_service_env_supplies_its_vault(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault-ok"
        vault.mkdir()
        monkeypatch.delenv("UA_VAULT_PATH", raising=False)
        _install_pr_watch_job(
            tmp_path,
            env={"UA_HOME": str(tmp_path / "ua")},
            service_env=f"UA_VAULT_PATH={vault}\n",
        )
        assert doctor._required_settings_check()["status"] == "LIVE"

    def test_pr_watch_settings_toml_supplies_its_vault(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault-ok"
        vault.mkdir()
        ua_home = tmp_path / "ua"
        ua_home.mkdir()
        (ua_home / "settings.toml").write_text(f'[paths]\nvault = "{vault}"\n')
        monkeypatch.delenv("UA_VAULT_PATH", raising=False)
        _install_pr_watch_job(tmp_path, env={"UA_HOME": str(ua_home)})
        assert doctor._required_settings_check()["status"] == "LIVE"

    def test_connector_repo_is_judged_where_the_poller_runs(self, monkeypatch, tmp_path):
        """The connector polls inside pr-watch; the hub's repo setting does not reach it."""
        vault = tmp_path / "vault-ok"
        vault.mkdir()
        monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
        monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
        _install_pr_watch_job(
            tmp_path,
            env={
                "UA_HOME": str(tmp_path / "ua"),
                "UA_GITHUB_APP_ENABLED": "1",
                "UA_VAULT_PATH": str(vault),
            },
        )
        row = doctor._required_settings_check()
        assert row["status"] == "MISCONFIGURED", row
        assert "UA_GITHUB_CONNECTOR_REPO" in row["detail"]

    def test_broker_only_pr_watch_does_not_need_the_vault(self, monkeypatch, tmp_path):
        _install_pr_watch_plist(tmp_path)
        monkeypatch.setenv("UA_VAULT_PATH", "")
        monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
        monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
        monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
        monkeypatch.setenv("UA_GITHUB_USER_TOKEN_SOURCE", "connector_oauth")
        assert doctor._required_settings_check()["status"] == "LIVE"


# ------------------------------------------------------------ hub_checkout_drift


def _repo_at(tmp_path, sha: str):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "HEAD").write_text(sha + "\n")
    return repo


def _write_start(record: dict) -> None:
    path = hub_runtime.start_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


class TestHubCheckoutDrift:
    def test_dormant_without_start_record(self):
        row = doctor._hub_checkout_drift_check()
        assert row["capability"] == "hub_checkout_drift"
        assert row["status"] == "DORMANT"

    def test_live_when_running_sha_matches_checkout(self, tmp_path):
        hub_runtime.record_hub_start(root=_repo_at(tmp_path, SHA_A))
        row = doctor._hub_checkout_drift_check()
        assert row["status"] == "LIVE", row
        assert SHA_A[:12] in row["detail"]

    def test_degraded_when_checkout_moved_without_restart(self, tmp_path):
        repo = _repo_at(tmp_path, SHA_A)
        hub_runtime.record_hub_start(root=repo)
        (repo / ".git" / "HEAD").write_text(SHA_B + "\n")  # an unrecorded pull
        row = doctor._hub_checkout_drift_check()
        assert row["status"] == "DEGRADED", row
        assert "checkout moved without a deploy/restart" in row["detail"]
        assert SHA_A[:12] in row["detail"] and SHA_B[:12] in row["detail"]

    def test_unknown_when_recorded_hub_is_not_running(self, tmp_path):
        repo = _repo_at(tmp_path, SHA_A)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        _write_start(
            {
                "sha": SHA_A,
                "checkout": str(repo),
                "pid": dead.pid,
                "started_at": datetime.now(UTC).isoformat(),
            }
        )
        row = doctor._hub_checkout_drift_check()
        assert row["status"] == "UNKNOWN", row
        assert "not running" in row["detail"]

    def test_unknown_when_hub_started_outside_a_checkout(self, tmp_path):
        hub_runtime.record_hub_start(root=tmp_path / "not-a-repo")
        assert doctor._hub_checkout_drift_check()["status"] == "UNKNOWN"

    def test_unknown_when_checkout_head_unreadable(self, tmp_path):
        repo = _repo_at(tmp_path, SHA_A)
        hub_runtime.record_hub_start(root=repo)
        (repo / ".git" / "HEAD").unlink()
        assert doctor._hub_checkout_drift_check()["status"] == "UNKNOWN"


def test_system_doctor_includes_the_new_rows():
    capabilities = {row["capability"] for row in doctor.system_doctor()["checks"]}
    assert {
        "pr_watch_job",
        "required_settings",
        "hub_checkout_drift",
        "fleet_nodes",
        "fleet_node_versions",
    } <= capabilities


def test_pid_alive_treats_self_as_running():
    assert hub_runtime.pid_alive(os.getpid())
