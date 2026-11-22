"""Tests for system_doctor, the dormant-capability report (Tier 1 easy win,
System Leverage Plan 2026-07-20).
"""

import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import urllib.error
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from unified_agent import config, hub_client, scheduled_status
from unified_agent.executor import daemon as doctor_daemon
from unified_agent.monitor import launchd_label_for_unit
from unified_agent.tools import doctor, zulip
from unified_agent.tools.doctor import system_doctor

pytestmark = pytest.mark.smoke

# Captured before any fixture stubs it, for tests about host isolation (#1373).
_REAL_UNIT_ENVIRONMENT_FILES = doctor._unit_environment_files


@pytest.fixture(autouse=True)
def _host_units_invisible(monkeypatch):
    """The host's real systemd units must not sway test results.

    Since task #176 the checks read a service's own Environment=/EnvironmentFile=
    rather than the caller's env, so on this machine the live hub unit would leak
    its three tokens and four gated tools into every assertion. Returning "" (not
    None) means "the unit is readable and sets nothing", so the checks fall back
    to the process env exactly as the older tests expect. Tests that are about
    unit resolution stub these themselves.
    """
    monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: "")
    monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [])


def _by_capability(checks: list[dict]) -> dict:
    return {c["capability"]: c for c in checks}


def _write_steward_audit_status(
    home, state, exit_code=1, run_id="steward-audit-run.test", audit_exit_code=None
):
    state_dir = home / ".unified_agent"
    run_dir = state_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "steward-audit-status").write_text(f"kind=scheduled\nrun_id={run_id}\n")
    record = f"kind=scheduled\nrun_id={run_id}\nstate={state}\n"
    if exit_code is not None:
        record += f"exit_code={exit_code}\n"
    if audit_exit_code is not None:
        record += f"audit_exit_code={audit_exit_code}\n"
    (run_dir / "status").write_text(record)


class TestHubAuthCheck:
    def test_dormant_with_no_tokens(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["hub_auth"]["status"] == "DORMANT"
        assert checks["hub_auth"]["gate"] == "UA_AGENT_TOKENS"

    def test_live_warn_mode(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:tok123")
        monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["hub_auth"]["status"] == "LIVE"
        assert "warn-mode" in checks["hub_auth"]["detail"]

    def test_live_enforced(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:tok123")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        checks = _by_capability(system_doctor()["checks"])
        assert checks["hub_auth"]["status"] == "LIVE"
        assert "enforced" in checks["hub_auth"]["detail"]


class TestMcpOauthCheck:
    def test_dormant_when_unset(self, monkeypatch):
        monkeypatch.delenv("UA_MCP_OAUTH", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "DORMANT"

    def test_misconfigured_when_flag_without_issuer(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.delenv("UA_MCP_OAUTH_ISSUER", raising=False)
        monkeypatch.delenv("UA_MCP_OAUTH_PASSPHRASE", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "MISCONFIGURED"
        assert "UA_MCP_OAUTH_ISSUER" in checks["mcp_oauth"]["detail"]

    def test_passphrase_without_fallback_is_degraded(self, monkeypatch):
        """Task #1322: a bare passphrase keeps tokens alive but approves nothing."""
        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://ua.example.test")
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "not-a-real-secret")
        monkeypatch.delenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", raising=False)
        monkeypatch.delenv("UA_MCP_OAUTH_APPROVER_LOGINS", raising=False)
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "DEGRADED"
        assert "UA_MCP_OAUTH_PASSPHRASE_FALLBACK" in checks["mcp_oauth"]["detail"]

    def test_live_with_tailnet_approver(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://ua.example.test")
        monkeypatch.delenv("UA_MCP_OAUTH_PASSPHRASE", raising=False)
        monkeypatch.setenv("UA_MCP_OAUTH_APPROVER_LOGINS", "operator@example.com")
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "LIVE"
        assert "tailnet+passkey" in checks["mcp_oauth"]["detail"]

    def test_workos_org_without_identity_map_is_not_an_approver(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://ua.example.test")
        monkeypatch.delenv("UA_MCP_OAUTH_PASSPHRASE", raising=False)
        monkeypatch.delenv("UA_MCP_OAUTH_APPROVER_LOGINS", raising=False)
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_1")
        monkeypatch.delenv("UA_HUB_WORKOS_IDENTITY_MAP", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "MISCONFIGURED"
        assert config.mcp_oauth_human_approver_configured() is False

    def test_workos_map_without_provider_settings_is_not_an_approver(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://ua.example.test")
        monkeypatch.delenv("UA_MCP_OAUTH_PASSPHRASE", raising=False)
        monkeypatch.delenv("UA_MCP_OAUTH_APPROVER_LOGINS", raising=False)
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_1")
        monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", '{"human@example.com":"Operator"}')
        monkeypatch.delenv("WORKOS_API_KEY", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "MISCONFIGURED"
        assert config.mcp_oauth_human_approver_configured() is False

    def test_workos_approver_in_hub_unit_is_live_from_operator_shell(self, monkeypatch):
        for name in (
            "UA_MCP_OAUTH",
            "UA_MCP_OAUTH_ISSUER",
            "UA_HUB_WORKOS_ORGANIZATION_ID",
            "UA_HUB_WORKOS_IDENTITY_MAP",
            "WORKOS_API_KEY",
            "WORKOS_CLIENT_ID",
            "WORKOS_ISSUER",
            "WORKOS_REDIRECT_URI",
            "WORKOS_COOKIE_PASSWORD",
        ):
            monkeypatch.delenv(name, raising=False)
        hub_env = {
            "UA_MCP_OAUTH": "1",
            "UA_MCP_OAUTH_ISSUER": "https://ua.example.test",
            "UA_MCP_OAUTH_AGENT": "Desktop_Agent1",
            "UA_EDITION": "enterprise",
            "UA_HUB_WORKOS_ORGANIZATION_ID": "org_1",
            "UA_HUB_WORKOS_IDENTITY_MAP": '{"human@example.com":"Desktop_Person"}',
            "WORKOS_API_KEY": "test-key",
            "WORKOS_CLIENT_ID": "client_1",
            "WORKOS_ISSUER": "https://auth.example.test",
            "WORKOS_REDIRECT_URI": "https://ua.example.test/callback",
            "WORKOS_COOKIE_PASSWORD": Fernet.generate_key().decode(),
        }
        monkeypatch.setattr(
            doctor,
            "_service_env",
            lambda unit: hub_env if unit == doctor._SERVICE_UNITS["hub_service"] else {},
        )
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "LIVE"
        assert "approvers: workos" in checks["mcp_oauth"]["detail"]
        # Task #2114: the API key is never contacted, so it is never called valid.
        assert "not verified live" in checks["mcp_oauth"]["detail"]

    def test_hub_auth_recognises_a_workos_only_hub(self, monkeypatch):
        """Task #2114: a hub whose humans sign in with WorkOS is not 'anonymous'."""
        hub_env = {
            "UA_ENFORCE_AUTH": "1",
            "UA_EDITION": "enterprise",
            "UA_HUB_WORKOS_ORGANIZATION_ID": "org_1",
            "UA_HUB_WORKOS_IDENTITY_MAP": '{"user_1":"Desktop_Person"}',
            "WORKOS_API_KEY": "test-key",
            "WORKOS_CLIENT_ID": "client_1",
            "WORKOS_ISSUER": "https://api.workos.com/user_management/client_1",
            "WORKOS_REDIRECT_URI": "https://ua.example.test/oauth/approver/workos/callback",
            "WORKOS_COOKIE_PASSWORD": Fernet.generate_key().decode(),
        }
        monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
        monkeypatch.setattr(
            doctor,
            "_service_env",
            lambda unit: hub_env if unit == doctor._SERVICE_UNITS["hub_service"] else {},
        )
        res = doctor._hub_auth_check()
        assert res["status"] == "LIVE", res
        assert "no bearer tokens" in res["detail"]
        assert "WorkOS hub leg configured for organization org_1" in res["detail"]
        assert "not verified live" in res["detail"]
        assert hub_env["WORKOS_API_KEY"] not in res["detail"]

        hub_env["WORKOS_COOKIE_PASSWORD"] = "not-a-fernet-key"
        res = doctor._hub_auth_check()
        assert res["status"] == "MISCONFIGURED"
        assert "unusable" in res["detail"]
        assert "not-a-fernet-key" not in res["detail"]

        hub_env["UA_AGENT_TOKENS"] = "Desktop_Agent2:tok1"
        res = doctor._hub_auth_check()
        assert res["status"] == "DEGRADED"
        assert "1 token(s) configured" in res["detail"]

    def test_invalid_workos_cookie_key_is_not_a_usable_approver(self, monkeypatch):
        hub_env = {
            "UA_MCP_OAUTH": "1",
            "UA_MCP_OAUTH_ISSUER": "https://ua.example.test",
            "UA_MCP_OAUTH_AGENT": "Desktop_Agent1",
            "UA_EDITION": "enterprise",
            "UA_HUB_WORKOS_ORGANIZATION_ID": "org_1",
            "UA_HUB_WORKOS_IDENTITY_MAP": '{"human@example.com":"Desktop_Agent1"}',
            "WORKOS_API_KEY": "test-key",
            "WORKOS_CLIENT_ID": "client_1",
            "WORKOS_ISSUER": "https://auth.example.test",
            "WORKOS_REDIRECT_URI": "https://ua.example.test/callback",
            "WORKOS_COOKIE_PASSWORD": "invalid-cookie-key",
        }
        monkeypatch.setattr(
            doctor,
            "_service_env",
            lambda unit: hub_env if unit == doctor._SERVICE_UNITS["hub_service"] else {},
        )
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "MISCONFIGURED"

    def test_live_when_complete(self, monkeypatch):
        monkeypatch.setenv("UA_MCP_OAUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://ua.example.test")
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "not-a-real-secret")
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
        monkeypatch.delenv("UA_MCP_OAUTH_AGENT", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "LIVE"
        assert "Remote_Agent" in checks["mcp_oauth"]["detail"]

        monkeypatch.setenv("UA_MCP_OAUTH_AGENT", "Desktop_Agent1")
        checks = _by_capability(system_doctor()["checks"])
        assert checks["mcp_oauth"]["status"] == "LIVE"
        assert "Desktop_Agent1" in checks["mcp_oauth"]["detail"]


class TestApprovalGatesCheck:
    def test_dormant_when_unset(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["approval_gates"]["status"] == "DORMANT"

    def test_live_when_set(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd:workflow_run")
        checks = _by_capability(system_doctor()["checks"])
        assert checks["approval_gates"]["status"] == "LIVE"
        assert "run_cmd" in checks["approval_gates"]["detail"]


class TestSearchProviderCheck:
    def test_live_keyless_fallback(self, monkeypatch):
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("UA_SEARCH_PROVIDER", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["search_provider"]["status"] == "LIVE"
        assert "ddg" in checks["search_provider"]["detail"]

    def test_live_keyed(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
        monkeypatch.delenv("UA_SEARCH_PROVIDER", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["search_provider"]["status"] == "LIVE"
        assert "tavily" in checks["search_provider"]["detail"]


class TestPerAgentWorkspacesCheck:
    def test_dormant_when_unset(self, monkeypatch):
        monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["per_agent_workspaces"]["status"] == "DORMANT"

    def test_live_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_AGENT_WORKSPACES", f"Desktop_Agent5:{tmp_path}")
        checks = _by_capability(system_doctor()["checks"])
        assert checks["per_agent_workspaces"]["status"] == "LIVE"
        assert "Desktop_Agent5" in checks["per_agent_workspaces"]["detail"]


# The public export drops deploy/ (scripts/public_release/exclude.txt), so the
# tests that run the steward-audit wrappers skip there instead of failing on a
# missing script. They still run in the private repo, where the scripts exist.
# The skip is gated on being an export (#1597 review): in the private repo a
# renamed wrapper must fail these tests, not read as a green skip.
_STEWARD_SCRIPTS = (
    "deploy/mac-hub/run-steward-audit.sh",
    "deploy/steward-audit-status.sh",
    "deploy/steward-audit-cron.sh",
)
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PUBLIC_EXPORT = not (_REPO_ROOT / "scripts" / "public_release").is_dir()
needs_steward_scripts = pytest.mark.skipif(
    _PUBLIC_EXPORT and not all((_REPO_ROOT / name).is_file() for name in _STEWARD_SCRIPTS),
    reason="the steward-audit scripts are not in this tree (public export)",
)


class TestUnitChecks:
    def test_unavailable_launchagent_is_unknown(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(doctor, "_service_probe", lambda unit, timeout: None)
        check = doctor._unit_check("active_executor_service", "unified-agent-executor.service")
        assert check["status"] == "UNKNOWN"
        assert "probe unavailable" in check["detail"]

    def test_healthy_idle_dispatch_launchagent_is_live(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "active"},
        )
        check = doctor._unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer")
        assert check["status"] == "LIVE"
        assert "com.unified-agent.dispatch-tick" in check["detail"]

    def test_retired_dispatch_tick_failed_probe_is_unknown(self, monkeypatch):
        """Monitor None means probe failure; exact absence is now not-found."""
        from unified_agent import monitor

        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(monitor, "_service_probe", lambda unit, timeout: None)
        check = doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer")
        assert check["status"] == "UNKNOWN"
        assert "retired by #1435" in check["detail"]
        assert "com.unified-agent.dispatch-tick" in check["detail"]

    def test_retired_dispatch_tick_not_installed_is_dormant(self, monkeypatch):
        from unified_agent import monitor

        monkeypatch.setattr(doctor.sys, "platform", "linux")
        monkeypatch.setattr(
            monitor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "not-found", "ActiveState": "inactive"},
        )
        check = doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer")
        assert check["status"] == "DORMANT"
        assert "retired by #1435" in check["detail"]

    def test_retired_dispatch_tick_still_loaded_is_misconfigured(self, monkeypatch):
        from unified_agent import monitor

        monkeypatch.setattr(doctor.sys, "platform", "linux")
        monkeypatch.setattr(
            monitor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "active"},
        )
        check = doctor._retired_unit_check("dispatch_tick_timer", "ua-dispatch-tick.timer")
        assert check["status"] == "MISCONFIGURED"
        assert "still loaded" in check["detail"]

    @pytest.mark.parametrize(
        ("props", "disabled", "status"),
        [
            ({"LoadState": "not-found", "ActiveState": "inactive"}, True, "DORMANT"),
            # Absent but not disabled: an installed plist loads at next login.
            ({"LoadState": "not-found", "ActiveState": "inactive"}, False, "UNKNOWN"),
            ({"LoadState": "not-found", "ActiveState": "inactive"}, None, "UNKNOWN"),
            ({"LoadState": "loaded", "ActiveState": "active"}, True, "MISCONFIGURED"),
            (None, True, "UNKNOWN"),
        ],
    )
    def test_retired_cluster_briefing_reports_its_retirement(
        self, monkeypatch, props, disabled, status
    ):
        """#1888: the daily briefing moved to the chief of staff. On the Mac the
        job is retired only when it is absent AND launchd holds the disabled
        override; loaded is a regression."""
        from unified_agent import monitor

        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(doctor, "_service_probe", lambda unit, timeout: props)
        seen = []

        def override(label, timeout):
            seen.append(label)
            return disabled

        monkeypatch.setattr(monitor, "_launchd_disabled_override", override)
        check = doctor._retired_unit_check("cluster_briefing_timer", "cluster-briefing.timer")
        assert check["status"] == status
        assert "retired by #1888" in check["detail"]
        assert "com.unified-agent.cluster-briefing" in check["detail"]
        if props and props["LoadState"] == "not-found":
            assert seen == ["com.unified-agent.cluster-briefing"]

    @pytest.mark.parametrize(
        ("platform", "retired"), [("darwin", True), ("linux", False), ("win32", False)]
    )
    def test_cluster_briefing_retirement_is_mac_only(self, monkeypatch, platform, retired):
        """A Linux host's own optional cluster-briefing.timer keeps the normal check."""
        monkeypatch.setattr(doctor.sys, "platform", platform)
        assert doctor._retirement_applies("cluster_briefing_timer") is retired
        assert doctor._retirement_applies("dispatch_tick_timer") is True
        assert doctor._retirement_applies("anomaly_cron") is False

    def test_linux_cluster_briefing_timer_still_reads_live(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "linux")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "active"},
        )
        checks = {c["capability"]: c for c in doctor.system_doctor(refresh=True)["checks"]}
        assert checks["cluster_briefing_timer"]["status"] == "LIVE"

    def test_ambiguous_zero_runs_launchagent_is_unknown_not_misconfigured(self, monkeypatch):
        """A zero-runs timer that's overdue only within the install-then-
        enable-later grace window (monitor._launchd_zero_runs_status) must
        surface as UNKNOWN, never a confident MISCONFIGURED false alarm."""
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "unknown"},
        )
        check = doctor._unit_check("sleep_pass_timer", "ua-sleep-pass.timer")
        assert check["status"] == "UNKNOWN"
        assert "com.unified-agent.sleep-pass" in check["detail"]

    def test_steward_audit_designed_exit_1_is_degraded_not_misconfigured(
        self, monkeypatch, tmp_path
    ):
        """#1484: steward-audit exits 1 by design when `unified-agent-audit
        surfaces --fail-on high` finds HIGH-severity findings. That must not
        read the same as a broken timer."""
        monkeypatch.setenv("HOME", str(tmp_path))
        log = tmp_path / ".unified_agent" / "audit.log"
        log.parent.mkdir()
        log.write_text(
            "2026-09-28T09:00:00Z START steward-audit kind=scheduled\n"
            "2026-09-28T09:00:01Z PASS surfaces\n"
            "2026-09-29T09:00:00Z START steward-audit kind=scheduled\n"
            "2026-09-29T09:00:01Z FAIL surfaces rc=1\n"
        )
        _write_steward_audit_status(tmp_path, "surfaces_fail")
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "1",
            },
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "DEGRADED"
        assert "exited 1" in check["detail"]
        assert "findings" in check["detail"]

    @pytest.mark.parametrize(
        "log_text",
        [
            None,
            "",
            "2026-09-29T09:00:00Z PASS surfaces\n",
            "2026-09-28T09:00:00Z START steward-audit kind=scheduled\n"
            "2026-09-28T09:00:01Z FAIL surfaces rc=1\n"
            "2026-09-29T09:00:00Z START steward-audit kind=scheduled\n",
            "2026-09-28T09:00:00Z START steward-audit kind=scheduled\n"
            "2026-09-28T09:00:01Z FAIL surfaces rc=1\n"
            "2026-09-29T09:00:00Z START steward-audit kind=scheduled\n"
            "2026-09-29T09:00:01Z PASS surfaces\n",
        ],
    )
    def test_steward_audit_exit_1_without_a_tripped_gate_stays_misconfigured(
        self, monkeypatch, tmp_path, log_text
    ):
        """Exit 1 before the scan ran (wrapper or interpreter failure) leaves
        no fresh FAIL line in audit.log; that is a broken timer, not findings."""
        monkeypatch.setenv("HOME", str(tmp_path))
        if log_text is not None:
            log = tmp_path / ".unified_agent" / "audit.log"
            log.parent.mkdir()
            log.write_text(log_text)
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "1",
            },
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "MISCONFIGURED"

    def test_steward_audit_delivery_failure_is_degraded_not_misconfigured(
        self, monkeypatch, tmp_path
    ):
        """#1856: the audit ran and found findings, then the alert channel
        refused the send (an iMessage cooldown). Exit 2 backed by this run's
        delivery_fail record is a delivery problem, not a broken timer."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_steward_audit_status(tmp_path, "delivery_fail", exit_code=2, audit_exit_code=1)
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "2",
            },
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "DEGRADED"
        assert "exited 2" in check["detail"]
        assert "delivering the report or alert failed" in check["detail"]
        assert not doctor._steward_audit_gate_tripped()

    @pytest.mark.parametrize(
        ("state", "exit_code", "audit_exit_code"),
        [
            # Audit binary missing/not executable: exits 2 before completing.
            ("started", None, None),
            # Exit code and record disagree: the record is not this outcome.
            ("surfaces_fail", 1, None),
            ("delivery_fail", 1, 1),
            # The scan itself errored (exit 2), then delivery failed too.
            ("delivery_fail", 2, 2),
            # A record from before the scan code was recorded proves nothing.
            ("delivery_fail", 2, None),
        ],
    )
    def test_steward_audit_exit_2_without_delivery_record_stays_misconfigured(
        self, monkeypatch, tmp_path, state, exit_code, audit_exit_code
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_steward_audit_status(
            tmp_path, state, exit_code=exit_code, audit_exit_code=audit_exit_code
        )
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "2",
            },
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "MISCONFIGURED"
        assert "installed but inactive" in check["detail"]

    def test_steward_audit_exit_2_with_no_status_file_stays_misconfigured(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "2",
            },
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "MISCONFIGURED"

    def test_steward_audit_installed_but_not_loaded_is_not_live(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor, "_service_probe", lambda unit, timeout: {"LoadState": "not-found"}
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "DORMANT"
        assert "not installed" in check["detail"]

    def test_steward_audit_calendar_job_idle_between_fires_is_live(self, monkeypatch):
        """The launchd probe maps a calendar job that last exited 0 to active."""
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "active",
                "LastExitCode": "0",
            },
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "LIVE"

    def test_steward_audit_duplicate_report_run_still_counts_as_findings(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        log = tmp_path / ".unified_agent" / "audit.log"
        log.parent.mkdir()
        log.write_text(
            "2026-09-29T09:00:00Z START steward-audit kind=scheduled\n"
            "2026-09-29T09:00:01Z FAIL surfaces rc=1\n"
            "2026-09-29T09:00:00Z SKIP-DUPLICATE (report unchanged since last run today)\n"
        )
        _write_steward_audit_status(tmp_path, "surfaces_fail")
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "1",
            },
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "DEGRADED"

    def test_manual_audit_after_scheduled_failure_does_not_change_timer_evidence(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        log = tmp_path / ".unified_agent" / "audit.log"
        log.parent.mkdir()
        log.write_text(
            "2026-09-29T09:00:00Z START steward-audit kind=scheduled\n"
            "2026-09-29T09:00:01Z FAIL surfaces rc=1\n"
            "2026-09-29T09:30:00Z START steward-audit kind=manual\n"
            "2026-09-29T09:30:01Z PASS surfaces\n"
        )
        _write_steward_audit_status(tmp_path, "surfaces_fail")
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "1",
            },
        )

        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")

        assert check["status"] == "DEGRADED"
        assert "findings" in check["detail"]

    def test_steward_audit_reads_wrapper_path_and_latest_surfaces_entry(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("UA_HOME", str(tmp_path / "elsewhere"))
        log = tmp_path / ".unified_agent" / "audit.log"
        log.parent.mkdir()
        log.write_text(
            "2026-09-29T08:00:00Z START steward-audit kind=scheduled\n"
            "2026-09-29T08:00:01Z PASS surfaces\n"
            "2026-09-29T09:00:00Z START steward-audit kind=scheduled\n"
            "2026-09-29T09:00:01Z FAIL surfaces rc=1\n"
            "2026-09-29T09:01:00Z PASS Hermes drift\n"
        )
        _write_steward_audit_status(tmp_path, "surfaces_fail")
        assert doctor._steward_audit_gate_tripped()
        log.write_text(log.read_text() + "2026-09-29T10:00:00Z PASS surfaces\n")
        assert doctor._steward_audit_gate_tripped()
        _write_steward_audit_status(tmp_path, "started", exit_code=None)
        assert not doctor._steward_audit_gate_tripped()

    def test_steward_audit_evidence_survives_large_shared_log_tail(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        log = tmp_path / ".unified_agent" / "audit.log"
        log.parent.mkdir()
        _write_steward_audit_status(tmp_path, "surfaces_fail")
        log.write_text("unrelated PASS entry\n" * 500)
        assert log.stat().st_size > 4096
        assert doctor._steward_audit_gate_tripped()

    def test_steward_audit_status_requires_run_completion(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_steward_audit_status(tmp_path, "started", exit_code=None)
        assert not doctor._steward_audit_gate_tripped()
        (tmp_path / ".unified_agent" / "steward-audit-status").write_text(
            "kind=manual\nrun_id=steward-audit-run.test\n"
        )
        assert not doctor._steward_audit_gate_tripped()

    def test_late_concurrent_run_cannot_replace_latest_scheduled_evidence(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        helper = Path(__file__).resolve().parents[1] / "deploy/steward-audit-status.sh"
        script = f'''
          set -euo pipefail
          source "{helper}"
          ua_steward_audit_status_begin "$1"
          older=$UA_STEWARD_AUDIT_RUN_ID
          ua_steward_audit_status_begin "$1"
          latest=$UA_STEWARD_AUDIT_RUN_ID
          ua_steward_audit_status_finish "$1" surfaces_pass 0
          UA_STEWARD_AUDIT_RUN_ID=$older
          export UA_STEWARD_AUDIT_RUN_ID
          ua_steward_audit_status_finish "$1" surfaces_fail 1
          cat "$1/steward-audit-status"
          cat "$1/$latest/status"
        '''
        result = subprocess.run(
            ["bash", "-c", script, "test", str(tmp_path / ".unified_agent")],
            check=True,
            capture_output=True,
            text=True,
        )
        lines = result.stdout.splitlines()
        pointer = dict(line.split("=", 1) for line in lines[:2])
        latest_record = dict(line.split("=", 1) for line in lines[2:])
        assert pointer["run_id"] == latest_record["run_id"]
        assert latest_record["state"] == "surfaces_pass"
        assert not doctor._steward_audit_gate_tripped()

    def test_audit_lock_timeout_does_not_mistake_another_publishers_lock_for_ours(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        state_dir = tmp_path / ".unified_agent"
        lock_dir = state_dir / "steward-audit-status.lock"
        lock_dir.mkdir(parents=True)
        _write_steward_audit_status(tmp_path, "surfaces_fail")
        pointer = state_dir / "steward-audit-status"
        pointer_before = pointer.read_text()
        sleep_log = tmp_path / "retry-sleeps"
        helper = Path(__file__).resolve().parents[1] / "deploy/steward-audit-status.sh"
        # #2052/#2023: the helper retries mkdir 100 times with `sleep 0.01`, so the
        # real path costs ~1.6 s idle and over a minute on a loaded host, which
        # tripped the old 5 s timeout. A shell function shadows `sleep` (it runs
        # in-process and only logs), so the retry budget is spent without
        # wall-clock waits. The real `mkdir` on the real lock dir still decides
        # ownership. The timeout below only guards against a hang.
        # The stub must not hide a blocking wait the real `sleep` would perform
        # (PR #1211 review): anything other than one short finite duration exits
        # 97, and the exact retry log below pins the count and each duration.
        result = subprocess.run(
            [
                "bash",
                "-c",
                'ua_test_sleep_log="$2"; '
                "sleep() { "
                "[[ $# -eq 1 && $1 =~ ^0\\.[0-9]+$ ]] || "
                '{ echo "unexpected retry sleep: $*" >&2; exit 97; }; '
                'printf "%s\\n" "$1" >>"$ua_test_sleep_log"; }; '
                f'source "{helper}"; ua_steward_audit_status_begin "$1"',
                "test",
                str(state_dir),
                str(sleep_log),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # The helper's own lock-timeout return, not the stub's refusal (97).
        assert result.returncode == 1, result.stderr
        # The lock-wait path ran to exhaustion at the helper's retry budget:
        # 100 attempts, each followed by exactly `sleep 0.01`. A changed
        # duration, an extra sleep or an early exit from the loop fails here.
        assert sleep_log.read_text().splitlines() == ["0.01"] * 100
        # Another publisher's lock stays theirs: not removed, and the pointer
        # it guards is not rewritten by a caller that never acquired it.
        assert lock_dir.is_dir()
        assert pointer.read_text() == pointer_before
        assert not doctor._steward_audit_gate_tripped()

    def test_audit_mktemp_failure_invalidates_previous_findings_evidence(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        state_dir = tmp_path / ".unified_agent"
        _write_steward_audit_status(tmp_path, "surfaces_fail")
        fake_bin = tmp_path / "fake-bin"
        fake_bin.mkdir()
        mktemp = fake_bin / "mktemp"
        mktemp.write_text("#!/bin/sh\nexit 1\n")
        mktemp.chmod(0o755)
        helper = Path(__file__).resolve().parents[1] / "deploy/steward-audit-status.sh"
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{helper}"; ua_steward_audit_status_begin "$1"',
                "test",
                str(state_dir),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert not (state_dir / "steward-audit-status.lock").exists()
        assert not doctor._steward_audit_gate_tripped()

    def test_audit_pointer_publish_failure_releases_its_lock(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        state_dir = tmp_path / ".unified_agent"
        state_dir.mkdir()
        fake_bin = tmp_path / "fake-bin"
        fake_bin.mkdir()
        mv = fake_bin / "mv"
        mv.write_text("#!/bin/sh\nexit 1\n")
        mv.chmod(0o755)
        helper = Path(__file__).resolve().parents[1] / "deploy/steward-audit-status.sh"
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{helper}"; ua_steward_audit_status_begin "$1"',
                "test",
                str(state_dir),
            ],
            env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert not (state_dir / "steward-audit-status.lock").exists()

    @needs_steward_scripts
    def test_failed_audit_delivery_is_not_reported_as_findings(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = Path(__file__).resolve().parents[1]
        root = tmp_path / "dev/unified_agent"
        (root / "deploy/mac-hub").mkdir(parents=True)
        (root / ".venv/bin").mkdir(parents=True)
        for source in (
            "deploy/mac-hub/run-steward-audit.sh",
            "deploy/steward-audit-status.sh",
            "deploy/steward-audit-cron.sh",
        ):
            target = root / source
            shutil.copy2(repo / source, target)
        audit_bin = root / ".venv/bin/unified-agent-audit"
        audit_bin.write_text("#!/bin/sh\necho high finding\nexit 1\n")
        audit_bin.chmod(0o755)
        notify = tmp_path / "dev/agent/projects/imessage-bridge/notify.sh"
        notify.parent.mkdir(parents=True)
        notify.write_text("#!/bin/sh\nexit 1\n")
        notify.chmod(0o755)
        (tmp_path / "vaults/main/00 - Inbox").mkdir(parents=True)
        result = subprocess.run(
            ["bash", str(root / "deploy/mac-hub/run-steward-audit.sh")],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert not doctor._steward_audit_gate_tripped()
        report_date = (tmp_path / ".unified_agent/steward-audit-last-report.date").read_text()
        note = tmp_path / "vaults/main/00 - Inbox" / f"Security Audit {report_date}.md"
        first_note = note.read_text()
        assert first_note.count("## surfaces") == 1
        assert (tmp_path / ".unified_agent/steward-audit-last-report.date").exists()
        assert not (tmp_path / ".unified_agent/steward-audit-last-alert.date").exists()
        retry = subprocess.run(
            ["bash", str(root / "deploy/mac-hub/run-steward-audit.sh")],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert retry.returncode == 2
        assert note.read_text() == first_note

    @needs_steward_scripts
    def test_missing_audit_inbox_still_sends_alert_and_records_delivery_failure(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = Path(__file__).resolve().parents[1]
        root = tmp_path / "dev/unified_agent"
        (root / "deploy/mac-hub").mkdir(parents=True)
        (root / ".venv/bin").mkdir(parents=True)
        for source in (
            "deploy/mac-hub/run-steward-audit.sh",
            "deploy/steward-audit-status.sh",
            "deploy/steward-audit-cron.sh",
        ):
            shutil.copy2(repo / source, root / source)
        audit_bin = root / ".venv/bin/unified-agent-audit"
        audit_bin.write_text("#!/bin/sh\necho high finding\nexit 1\n")
        audit_bin.chmod(0o755)
        notify = tmp_path / "dev/agent/projects/imessage-bridge/notify.sh"
        notify.parent.mkdir(parents=True)
        notify.write_text('#!/bin/sh\ncat >"$HOME/alert-sent"\n')
        notify.chmod(0o755)
        result = subprocess.run(
            ["bash", str(root / "deploy/mac-hub/run-steward-audit.sh")],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "Security Audit Failed" in (tmp_path / "alert-sent").read_text()
        assert not doctor._steward_audit_gate_tripped()
        assert doctor._steward_audit_delivery_failed()
        assert (tmp_path / ".unified_agent/steward-audit-last-alert.date").exists()
        assert not (tmp_path / ".unified_agent/steward-audit-last-report.date").exists()

    @needs_steward_scripts
    def test_errored_scan_with_failed_delivery_is_not_findings_evidence(
        self, monkeypatch, tmp_path
    ):
        """PR #1026 review: a scan that errors (exit 2) and then fails delivery
        records audit_exit_code=2, so the doctor cannot read it as findings."""
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = Path(__file__).resolve().parents[1]
        root = tmp_path / "dev/unified_agent"
        (root / "deploy/mac-hub").mkdir(parents=True)
        (root / ".venv/bin").mkdir(parents=True)
        for source in (
            "deploy/mac-hub/run-steward-audit.sh",
            "deploy/steward-audit-status.sh",
            "deploy/steward-audit-cron.sh",
        ):
            shutil.copy2(repo / source, root / source)
        audit_bin = root / ".venv/bin/unified-agent-audit"
        audit_bin.write_text("#!/bin/sh\necho scan error\nexit 2\n")
        audit_bin.chmod(0o755)
        result = subprocess.run(
            ["bash", str(root / "deploy/mac-hub/run-steward-audit.sh")],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        record = doctor._steward_audit_current_status()
        assert record["state"] == "delivery_fail"
        assert record["audit_exit_code"] == "2"
        assert not doctor._steward_audit_delivery_failed()
        assert not doctor._steward_audit_gate_tripped()

    @needs_steward_scripts
    def test_non_executable_audit_binary_exit_2_is_not_delivery_evidence(
        self, monkeypatch, tmp_path
    ):
        """#1856: exit 2 from a missing/non-executable audit binary records no
        completion, so the doctor cannot mistake it for a delivery failure."""
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = Path(__file__).resolve().parents[1]
        root = tmp_path / "dev/unified_agent"
        (root / "deploy/mac-hub").mkdir(parents=True)
        (root / ".venv/bin").mkdir(parents=True)
        for source in (
            "deploy/mac-hub/run-steward-audit.sh",
            "deploy/steward-audit-status.sh",
            "deploy/steward-audit-cron.sh",
        ):
            shutil.copy2(repo / source, root / source)
        audit_bin = root / ".venv/bin/unified-agent-audit"
        audit_bin.write_text("#!/bin/sh\nexit 0\n")
        audit_bin.chmod(0o644)
        result = subprocess.run(
            ["bash", str(root / "deploy/mac-hub/run-steward-audit.sh")],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "not executable" in result.stderr
        assert not doctor._steward_audit_delivery_failed()
        assert not doctor._steward_audit_gate_tripped()

    def test_manual_scheduled_flag_does_not_publish_timer_evidence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "vaults/main/00 - Inbox").mkdir(parents=True)
        state_dir = tmp_path / ".unified_agent"
        _write_steward_audit_status(tmp_path, "surfaces_fail")
        pointer = state_dir / "steward-audit-status"
        before = pointer.read_text()
        audit_bin = tmp_path / "dev/unified_agent/.venv/bin/unified-agent-audit"
        audit_bin.parent.mkdir(parents=True)
        audit_bin.write_text("#!/bin/sh\nexit 1\n")
        audit_bin.chmod(0o755)
        result = subprocess.run(
            [
                "bash",
                str(Path(__file__).resolve().parents[1] / "deploy/steward-audit-cron.sh"),
                "--scheduled",
            ],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert pointer.read_text() == before
        assert doctor._steward_audit_gate_tripped()

    @needs_steward_scripts
    def test_mac_wrapper_resets_evidence_before_log_append(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = Path(__file__).resolve().parents[1]
        temp_root = tmp_path / "dev" / "unified_agent"
        (temp_root / "deploy/mac-hub").mkdir(parents=True)
        shutil.copy2(
            repo / "deploy/mac-hub/run-steward-audit.sh",
            temp_root / "deploy/mac-hub/run-steward-audit.sh",
        )
        shutil.copy2(repo / "deploy/steward-audit-status.sh", temp_root / "deploy/")
        _write_steward_audit_status(tmp_path, "surfaces_fail")
        (tmp_path / ".unified_agent" / "audit.log").mkdir()
        env = os.environ.copy()
        env["HOME"] = str(tmp_path)
        result = subprocess.run(
            ["bash", str(temp_root / "deploy/mac-hub/run-steward-audit.sh")],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert not doctor._steward_audit_gate_tripped()

    def test_steward_audit_bad_usage_exit_2_stays_misconfigured(self, monkeypatch):
        """Exit 2 (bad usage, per audit/cli.py) is a real misconfiguration,
        not a designed exit code, and must keep paging as one."""
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "2",
            },
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "MISCONFIGURED"
        assert "installed but inactive" in check["detail"]

    def test_sleep_pass_designed_exit_2_is_degraded(self, monkeypatch, tmp_path):
        """sleep_pass.py exits 2 for a deferred-preflight/partial run — a
        deliberate outcome, not a broken timer."""
        monkeypatch.setenv("HOME", str(tmp_path))
        run_id = scheduled_status.begin_scheduled_run("sleep-pass")
        scheduled_status.finish_scheduled_run("sleep-pass", run_id, "deferred-preflight", 2)
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "2",
            },
        )
        check = doctor._unit_check("sleep_pass_timer", "ua-sleep-pass.timer")
        assert check["status"] == "DEGRADED"
        assert "exited 2" in check["detail"]

    def test_sleep_pass_exit_2_without_run_evidence_stays_misconfigured(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "2",
            },
        )
        check = doctor._unit_check("sleep_pass_timer", "ua-sleep-pass.timer")
        assert check["status"] == "MISCONFIGURED"

    def test_sleep_pass_real_error_exit_1_stays_misconfigured(self, monkeypatch, tmp_path):
        """An exit 1 with no llm_unavailable record of its own (maintenance_failed,
        a wrapper failure, or no record at all) must still page as MISCONFIGURED."""
        monkeypatch.setenv("HOME", str(tmp_path))
        run_id = scheduled_status.begin_scheduled_run("sleep-pass")
        scheduled_status.finish_scheduled_run("sleep-pass", run_id, "error", 1)
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "1",
            },
        )
        check = doctor._unit_check("sleep_pass_timer", "ua-sleep-pass.timer")
        assert check["status"] == "MISCONFIGURED"

    def _sleep_pass_exited_1(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "1",
            },
        )
        return doctor._unit_check("sleep_pass_timer", "ua-sleep-pass.timer")

    def test_sleep_pass_llm_unavailable_exit_1_is_degraded(self, monkeypatch, tmp_path):
        """#1502: a scheduled run that recorded "no model answered" ran; the
        row names the model route instead of calling the timer inactive."""
        monkeypatch.setenv("HOME", str(tmp_path))
        run_id = scheduled_status.begin_scheduled_run("sleep-pass")
        scheduled_status.finish_scheduled_run("sleep-pass", run_id, "llm_unavailable", 1)
        check = self._sleep_pass_exited_1(monkeypatch)
        assert check["status"] == "DEGRADED"
        assert "exited 1" in check["detail"]
        assert "llm_provider_chain" in check["detail"]
        assert "installed but inactive" not in check["detail"]

    def test_sleep_pass_llm_unavailable_needs_the_latest_run(self, monkeypatch, tmp_path):
        """An older run's llm_unavailable record cannot excuse a newer exit 1."""
        monkeypatch.setenv("HOME", str(tmp_path))
        older = scheduled_status.begin_scheduled_run("sleep-pass")
        scheduled_status.begin_scheduled_run("sleep-pass")
        assert not scheduled_status.finish_scheduled_run("sleep-pass", older, "llm_unavailable", 1)
        check = self._sleep_pass_exited_1(monkeypatch)
        assert check["status"] == "MISCONFIGURED"

    def test_designed_exit_code_does_not_apply_to_other_timers(self, monkeypatch):
        """The designed-exit-code table is per-unit: a timer with no entry
        keeps the old MISCONFIGURED behavior for any nonzero exit."""
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "LastExitCode": "1",
            },
        )
        check = doctor._unit_check("anomaly_cron", "ua-anomaly-check.timer")
        assert check["status"] == "MISCONFIGURED"

    def test_never_run_is_still_unknown_not_degraded(self, monkeypatch):
        """A timer that has genuinely never run (no LastExitCode) must not be
        confused with one that ran and exited by design."""
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {
                "LoadState": "loaded",
                "ActiveState": "unknown",
                "LastExitCode": None,
            },
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "UNKNOWN"

    def test_not_installed_stays_dormant(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "not-found", "ActiveState": "inactive"},
        )
        check = doctor._unit_check("steward_audit_cron", "ua-steward-audit.timer")
        assert check["status"] == "DORMANT"

    @pytest.mark.parametrize(
        ("timer_unit", "launchd_label"),
        [
            ("ua-review-pipeline.timer", "com.unified-agent.review-pipeline"),
            ("ua-anomaly-check.timer", "com.unified-agent.anomaly-check"),
            ("ua-consolidate-proposal.timer", "com.unified-agent.consolidate-proposal"),
            ("ua-sleep-pass.timer", "com.unified-agent.sleep-pass"),
            ("ua-planning-loop.timer", "com.unified-agent.planning-loop"),
            ("ua-steward-audit.timer", "com.unified-agent.steward-audit"),
            ("cluster-briefing.timer", "com.unified-agent.cluster-briefing"),
        ],
    )
    def test_mac_timer_launchd_labels(self, timer_unit, launchd_label):
        assert launchd_label_for_unit(timer_unit) == launchd_label

    def test_planning_loop_timer_is_in_system_doctor_inventory(self):
        assert doctor._TIMER_UNITS["planning_loop_timer"] == "ua-planning-loop.timer"

    @pytest.mark.parametrize(
        ("platform", "expected_status"),
        [("linux", "DORMANT"), ("darwin", "UNKNOWN")],
    )
    def test_units_report_unavailable_probe_status(self, monkeypatch, platform, expected_status):
        """Unavailable probes must produce the platform-specific non-live state."""
        monkeypatch.setattr(doctor.sys, "platform", platform)
        monkeypatch.setattr(doctor, "_service_probe", lambda unit, timeout: None)
        checks = _by_capability(system_doctor()["checks"])
        for capability in (
            "anomaly_cron",
            "sleep_pass_timer",
            "review_pipeline_cron",
            "steward_audit_cron",
            "hub_service",
        ):
            assert checks[capability]["status"] == expected_status
            assert checks[capability]["gate"].endswith((".timer", ".service"))
        assert "consolidate_timer" not in checks


TEMPLATE = """\
[Unit]
Description=unified-agent MCP hub (streamable HTTP)

[Service]
ExecStartPre=-%h/dev/unified_agent/deploy/check-behind-warning.sh
ExecStart=%h/dev/unified_agent/.venv/bin/unified-agent
ExecStartPost=-%h/dev/unified_agent/deploy/broadcast-restart.sh
Environment=UA_TRANSPORT=http
Environment=UA_WORKSPACE=%h/Projects/agent
# commented directives must not count as required:
#Environment=UA_HTTP_ALLOWED_HOSTS=127.0.0.1:*,localhost:*
Restart=on-failure

[Install]
WantedBy=default.target
"""


class TestUnitDriftCheck:
    def _install(
        self, monkeypatch, tmp_path, installed_text: str | None, unit_name="unified-agent.service"
    ):
        """Fake repo deploy dir + installed unit dir; None skips installing."""
        deploy = tmp_path / "deploy"
        (deploy / "systemd").mkdir(parents=True)
        (deploy / "unified-agent.service").write_text(TEMPLATE)
        monkeypatch.setattr(doctor, "_repo_deploy_dir", lambda: deploy)
        unit_dir = tmp_path / "xdg" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        if installed_text is not None:
            (unit_dir / unit_name).write_text(installed_text)

    def test_live_when_installed_matches_template(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path, TEMPLATE)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["unit_template_drift"]["status"] == "LIVE"
        assert "1 installed unit(s)" in checks["unit_template_drift"]["detail"]

    def test_missing_exec_hook_is_drift(self, monkeypatch, tmp_path):
        # The 2026-07-21 incident shape: installed unit predates the repo
        # template's ExecStartPre/ExecStartPost hooks — they were never live.
        installed = "\n".join(
            line
            for line in TEMPLATE.splitlines()
            if not line.startswith(("ExecStartPre=", "ExecStartPost="))
        )
        self._install(monkeypatch, tmp_path, installed)
        check = _by_capability(system_doctor()["checks"])["unit_template_drift"]
        assert check["status"] == "MISCONFIGURED"
        assert "unified-agent.service" in check["detail"]
        assert "ExecStartPre" in check["detail"]
        assert "ExecStartPost" in check["detail"]

    def test_local_environment_values_are_not_drift(self, monkeypatch, tmp_path):
        # Real installed units carry local values (moved workspace, extra env
        # like a real UA_HTTP_ALLOWED_HOSTS) — name-only comparison keeps those
        # quiet as long as every template variable is still set.
        installed = TEMPLATE.replace(
            "Environment=UA_WORKSPACE=%h/Projects/agent",
            "Environment=UA_WORKSPACE=%h/elsewhere/agent\n"
            "Environment=UA_HTTP_ALLOWED_HOSTS=desktop-host.real-tailnet.ts.net:*",
        )
        self._install(monkeypatch, tmp_path, installed)
        check = _by_capability(system_doctor()["checks"])["unit_template_drift"]
        assert check["status"] == "LIVE"

    def test_missing_environment_variable_is_drift(self, monkeypatch, tmp_path):
        installed = "\n".join(line for line in TEMPLATE.splitlines() if "UA_TRANSPORT" not in line)
        self._install(monkeypatch, tmp_path, installed)
        check = _by_capability(system_doctor()["checks"])["unit_template_drift"]
        assert check["status"] == "MISCONFIGURED"
        assert "Environment=UA_TRANSPORT=" in check["detail"]

    def test_dormant_when_no_units_installed(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path, None)
        check = _by_capability(system_doctor()["checks"])["unit_template_drift"]
        assert check["status"] == "DORMANT"
        assert "no repo-templated units installed" in check["detail"]

    def test_dormant_when_repo_templates_unreachable(self, monkeypatch):
        monkeypatch.setattr(doctor, "_repo_deploy_dir", lambda: None)
        check = _by_capability(system_doctor()["checks"])["unit_template_drift"]
        assert check["status"] == "DORMANT"
        assert "not checkable" in check["detail"]

    def test_real_repo_templates_parse(self):
        # Guard the parser against the actual shipped templates: every repo
        # unit file must parse into at least one section with directives.
        deploy = doctor._repo_deploy_dir()
        assert deploy is not None
        unit_files = [deploy / "unified-agent.service"]
        unit_files += sorted((deploy / "systemd").glob("*.service"))
        unit_files += sorted((deploy / "systemd").glob("*.timer"))
        assert len(unit_files) > 1
        for path in unit_files:
            parsed = doctor._parse_unit(path.read_text())
            assert parsed, f"{path.name} parsed to nothing"


class TestExtrasChecks:
    def test_missing_extra_reports_dormant(self, monkeypatch):
        checks = _by_capability(system_doctor()["checks"])
        # pypdf/tree_sitter_language_pack may or may not be installed in the
        # dev venv — assert the shape holds either way rather than assuming.
        pdf = checks["pdf_extract"]
        assert pdf["status"] in ("LIVE", "DORMANT")
        assert "uv sync --extra pdf" in pdf["gate"]
        tree = checks["tree_sitter_index"]
        assert tree["status"] in ("LIVE", "DORMANT")
        assert "--extra tree-sitter" not in tree["gate"]
        assert "tree-sitter-language-pack" in tree["gate"]


class TestSummary:
    def test_summary_counts_match_checks(self):
        result = system_doctor()
        checks = result["checks"]
        summary = result["summary"]
        assert summary["live"] == sum(1 for c in checks if c["status"] == "LIVE")
        assert summary["dormant"] == sum(1 for c in checks if c["status"] == "DORMANT")
        assert summary["degraded"] == sum(1 for c in checks if c["status"] == "DEGRADED")
        assert summary["misconfigured"] == sum(1 for c in checks if c["status"] == "MISCONFIGURED")
        assert summary["unknown"] == sum(1 for c in checks if c["status"] == "UNKNOWN")
        counted = (
            summary["live"]
            + summary["dormant"]
            + summary["degraded"]
            + summary["misconfigured"]
            + summary["unknown"]
        )
        assert counted == len(checks)

    def test_every_check_has_required_fields(self):
        for c in system_doctor()["checks"]:
            # ``hosts`` is fleet_nodes' per-host breakdown (task #1393).
            assert set(c.keys()) <= {"capability", "status", "detail", "gate", "health", "hosts"}
            assert {"capability", "status", "detail", "gate"} <= set(c.keys())
            assert c["status"] in ("LIVE", "DORMANT", "DEGRADED", "MISCONFIGURED", "UNKNOWN")


class TestGcpConnectorHealth:
    def test_unconfigured_registered_cloud_is_unknown_without_io(self, isolated_env, monkeypatch):
        from unified_agent import storage
        from unified_agent.tools import gcloud

        monkeypatch.delenv("UA_GCP_WORKER", raising=False)
        monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
        monkeypatch.setattr(
            gcloud,
            "gcloud_inventory",
            lambda **kwargs: pytest.fail("unconfigured doctor probed cloud"),
        )
        conn = storage.connect()
        try:
            now = storage.now()
            conn.execute(
                "INSERT INTO governed_servers "
                "(server_id, connector, provenance, scopes_json, reviewed_by, review_ref, "
                "revoked_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    "configured-gcp",
                    "int-gcp-agent20",
                    "test",
                    "[]",
                    "reviewer",
                    "review:test",
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        row = next(
            check
            for check in doctor._connectors_check()
            if check["capability"] == "connector:int-gcp-agent20"
        )
        assert row["status"] == "UNKNOWN"
        assert row["health"] == "UNKNOWN"
        assert "UA_GCP_WORKER" in row["detail"]

    def test_unreadable_inventory_is_degraded(self, isolated_env, monkeypatch):
        from unified_agent import storage
        from unified_agent.tools import doctor

        storage.init_db()
        conn = storage.connect()
        try:
            now = storage.now()
            conn.execute(
                "INSERT INTO governed_servers "
                "(server_id, connector, provenance, scopes_json, reviewed_by, review_ref, "
                "revoked_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                ("gcp-test", "int-gcp-agent20", "test", "[]", "tester", "review:test", now, now),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            "unified_agent.tools.gcloud.gcp_Agent20_health",
            lambda: {"status": "DEGRADED", "detail": "compute.instances.list permission denied"},
        )
        row = next(
            check
            for check in doctor._connectors_check()
            if check["capability"] == "connector:int-gcp-agent20"
        )
        assert row["status"] == "DEGRADED"
        assert row["health"] == "DEGRADED"
        assert "permission denied" in row["detail"]

    def test_live_worker_does_not_imply_connector_registration(self, isolated_env, monkeypatch):
        from unified_agent.tools import doctor

        monkeypatch.setattr(
            "unified_agent.tools.gcloud.gcp_Agent20_health",
            lambda: pytest.fail("unregistered connector must not probe cloud runtime"),
        )
        row = next(
            check
            for check in doctor._connectors_check()
            if check["capability"] == "connector:int-gcp-agent20"
        )
        assert row["status"] == "DORMANT"
        assert "health" not in row

    def test_deny_caller_still_reads_connector_registration(self, isolated_env, monkeypatch):
        # Task #1317: a UA_AGENT_WORKSPACES DENY caller used to get every
        # connector row as UNKNOWN because init_db() -> ensure_dirs() raised.
        from unified_agent import config
        from unified_agent.tools import doctor

        monkeypatch.setattr(
            "unified_agent.tools.gcloud.gcp_Agent20_health",
            lambda: pytest.fail("unregistered connector must not probe cloud runtime"),
        )
        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            rows = [
                c for c in doctor._connectors_check() if c["capability"].startswith("connector:")
            ]
        finally:
            config.workspace_root_var.reset(token)
        assert rows
        assert not [r for r in rows if "cannot determine active registration" in r["detail"]]

    def test_connector_check_initializes_the_store_once(self, isolated_env, monkeypatch):
        """init_db() ran once per connector; each call reran schema and post-init
        hooks, and stacked background backfill threads (hub fd exhaustion).
        Since #1850 the check uses init_db_once(): a store this process already
        initialized is not re-initialized by any later doctor run, so a polled
        doctor no longer restarts finished backfill scans."""
        from unified_agent import storage
        from unified_agent.tools import doctor

        calls = []
        real = storage.init_db
        monkeypatch.setattr(storage, "init_db", lambda: calls.append(1) or real())
        # A store this process has not initialized yet is initialized once.
        storage._initialized_db_paths.discard(str(storage.config.db_path()))
        rows = [c for c in doctor._connectors_check() if c["capability"].startswith("connector:")]
        assert len(rows) > 1
        assert len(calls) == 1
        # Every later run reuses it.
        doctor._connectors_check()
        doctor._connectors_check()
        assert len(calls) == 1


class TestOpenFilesLimitCheck:
    """PR #1001: the hub reports the descriptor limit it actually got."""

    def test_low_limit_on_http_hub_is_degraded(self, monkeypatch):
        import resource

        from unified_agent.tools import doctor

        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 4096))
        row = doctor._open_files_limit_check()
        assert row["status"] == "DEGRADED"
        assert "256" in row["detail"]

    def test_raised_limit_is_live(self, monkeypatch):
        import resource

        from unified_agent.tools import doctor

        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setattr(resource, "getrlimit", lambda which: (8192, 8192))
        assert doctor._open_files_limit_check()["status"] == "LIVE"

    def test_stdio_process_is_dormant(self, monkeypatch):
        from unified_agent.tools import doctor

        monkeypatch.setenv("UA_TRANSPORT", "stdio")
        assert doctor._open_files_limit_check()["status"] == "DORMANT"


class TestStaleTaskLocksCheck:
    def test_reclamation_does_not_renew_a_former_claimants_other_lease(
        self, isolated_env, monkeypatch
    ):
        from datetime import UTC, datetime, timedelta

        from unified_agent import storage
        from unified_agent.tools.tasks import task_add, task_claim

        current = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(storage, "now", lambda: current.isoformat(timespec="seconds"))
        task_ids = [task_add(title)["id"] for title in ("Expired first", "Expires later")]
        for task_id in task_ids:
            task_claim(task_id, agent="DeadAgent", timeout_seconds=3600)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET claimed_at=?, updated_at=?, lock_expires_at=? WHERE id=?",
                (
                    (current - timedelta(hours=2)).isoformat(),
                    current.isoformat(),
                    (current - timedelta(minutes=1)).isoformat(),
                    task_ids[0],
                ),
            )
            conn.execute(
                "UPDATE tasks SET claimed_at=?, updated_at=?, lock_expires_at=? WHERE id=?",
                (
                    (current - timedelta(hours=2)).isoformat(),
                    current.isoformat(),
                    (current + timedelta(minutes=30)).isoformat(),
                    task_ids[1],
                ),
            )
            # No agent row: the owner probes absent, so the lease is released. If
            # releasing recreated or bumped the row, the second lease would renew.
            conn.execute("DELETE FROM agents WHERE label='DeadAgent'")
            conn.commit()
        finally:
            conn.close()
        first = doctor.reclaim_stale_task_locks()
        assert [row["id"] for row in first["reclaimed_tasks"]] == [task_ids[0]]
        current += timedelta(minutes=31)
        second = doctor.reclaim_stale_task_locks()
        assert second["renewed_count"] == 0
        assert [row["id"] for row in second["reclaimed_tasks"]] == [task_ids[1]]

    def test_reclamation_persists_activity_and_bus_events(self, isolated_env, monkeypatch):
        from unified_agent import events, registry, run_records, storage
        from unified_agent.tools.tasks import task_add, task_claim

        monkeypatch.setenv("UA_EVENT_BUS", "1")
        task_ids = []
        run_ids = {}
        for title in ("Stale activity one", "Stale activity two"):
            task_id = task_add(title)["id"]
            claim = task_claim(task_id, agent="DeadAgent", timeout_seconds=1)
            run_ids[str(task_id)] = claim["current_run_id"]
            task_ids.append(task_id)

        conn = storage.connect()
        try:
            conn.executemany(
                "UPDATE tasks SET lock_expires_at='2020-01-01T00:00:00Z' WHERE id=?",
                [(task_id,) for task_id in task_ids],
            )
            # Maintenance attribution must not resurrect a former claimant.
            conn.execute("DELETE FROM agents WHERE label='DeadAgent'")
            conn.commit()
        finally:
            conn.close()
        registry.reset_throttle()

        # Keep a regression quick: a nested writer still loses its events, but
        # need not wait the production five-second busy timeout to demonstrate it.
        connect = storage.connect

        def bounded_connect():
            connection = connect()
            connection.execute("PRAGMA busy_timeout=50")
            return connection

        monkeypatch.setattr(storage, "connect", bounded_connect)
        mirror = events.mirror_agent_event

        def mirror_after_run_closure(kind, **kwargs):
            if (kwargs.get("summary") or "").startswith("stale lock reclaimed:"):
                run = run_records.get_run(run_ids[kwargs["ref_id"]])
                assert run["state"] == "finished"
                assert run["outcome"] == "abandoned"
            return mirror(kind, **kwargs)

        monkeypatch.setattr(events, "mirror_agent_event", mirror_after_run_closure)
        result = doctor.reclaim_stale_task_locks()
        assert result["reclaimed_count"] == 2

        conn = storage.connect()
        try:
            activity = conn.execute(
                "SELECT ref_id FROM agent_events WHERE kind='task' AND agent='DeadAgent' "
                "AND summary LIKE 'stale lock reclaimed:%'"
            ).fetchall()
            assert len(activity) == 2
            assert {row["ref_id"] for row in activity} == {str(i) for i in task_ids}
            bus = conn.execute(
                "SELECT payload, originator FROM event_log WHERE channel='agent.task.updated' "
                "AND json_extract(payload, '$.summary') LIKE 'stale lock reclaimed:%'"
            ).fetchall()
            assert len(bus) == 2
            assert {json.loads(row["payload"])["ref_id"] for row in bus} == {
                str(i) for i in task_ids
            }
            assert {row["originator"] for row in bus} == {"DeadAgent"}
            agent = conn.execute(
                "SELECT last_seen_via FROM agents WHERE label='DeadAgent'"
            ).fetchone()
            assert agent is None
            tasks = conn.execute(
                "SELECT status, claimed_by, lock_expires_at FROM tasks ORDER BY id"
            ).fetchall()
            # Each claim had an open run and a gone owner: interrupted, held blocked.
            assert all(
                row["status"] == "blocked"
                and row["claimed_by"] is None
                and row["lock_expires_at"] is None
                for row in tasks
            )
        finally:
            conn.close()

    def test_failed_reclamation_does_not_publish_events(self, isolated_env, monkeypatch):
        import sqlite3

        from unified_agent import storage
        from unified_agent.tools.tasks import task_add, task_claim

        task_ids = []
        for title in ("Rollback first", "Rollback second"):
            task_id = task_add(title)["id"]
            task_claim(task_id, agent="DeadAgent", timeout_seconds=1)
            task_ids.append(task_id)
        conn = storage.connect()
        try:
            conn.executemany(
                "UPDATE tasks SET lock_expires_at='2020-01-01T00:00:00Z' WHERE id=?",
                [(task_id,) for task_id in task_ids],
            )
            conn.execute("DELETE FROM agents WHERE label='DeadAgent'")
            conn.execute(
                "CREATE TRIGGER fail_reclaim BEFORE UPDATE ON tasks "
                f"WHEN OLD.id={task_ids[1]} AND NEW.status!='doing' "
                "BEGIN SELECT RAISE(ABORT, 'reclaim failed'); END"
            )
            conn.commit()
        finally:
            conn.close()

        # Publishing even one attempted release before commit would claim a
        # state change that the subsequent failure rolls back.
        monkeypatch.setattr(
            storage,
            "record_agent_event",
            lambda *args, **kwargs: pytest.fail("must not publish an uncommitted release"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="reclaim failed"):
            doctor.reclaim_stale_task_locks()
        conn = storage.connect()
        try:
            rows = conn.execute("SELECT status, claimed_by FROM tasks").fetchall()
            assert all(
                row["status"] == "doing" and row["claimed_by"] == "DeadAgent" for row in rows
            )
        finally:
            conn.close()

    def test_stale_lock_auto_reclaim(self, isolated_env, monkeypatch):
        from unified_agent import storage
        from unified_agent.tools.tasks import task_add, task_claim

        # Add a task
        t = task_add("Stale task test")
        task_id = t["id"]

        # Claim with immediate expiration timestamp in the past
        task_claim(task_id, agent="DeadAgent", timeout_seconds=1)

        # Manually set lock_expires_at into past
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET lock_expires_at='2020-01-01T00:00:00Z' WHERE id=?",
                (task_id,),
            )
            conn.commit()
        finally:
            conn.close()

        # Run system_doctor
        res = system_doctor()
        checks = _by_capability(res["checks"])
        assert checks["stale_task_locks"]["status"] == "MISCONFIGURED"
        assert f"#{task_id}" in checks["stale_task_locks"]["detail"]

        # Verify the task is unclaimed; its open run makes it interrupted work, held blocked
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status, claimed_by, lock_expires_at FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            assert row["status"] == "blocked"
            assert row["claimed_by"] is None
            assert row["lock_expires_at"] is None
        finally:
            conn.close()


class TestDatabaseBackupCheck:
    def test_dormant_without_backup(self, isolated_env):
        checks = _by_capability(system_doctor()["checks"])
        assert checks["database_backup"]["status"] == "DORMANT"

    def test_live_after_fresh_integrity_checked_backup(self, isolated_env):
        from unified_agent import backup, storage

        storage.init_db()
        backup.create_backup()
        checks = _by_capability(system_doctor()["checks"])
        assert checks["database_backup"]["status"] == "LIVE"


class TestZulipRelayCheck:
    @pytest.fixture(autouse=True)
    def _explicit_site(self, monkeypatch):
        monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.test")

    def test_dormant_when_nothing_configured(self, monkeypatch, tmp_path):
        # A foreign install (or this repo before the fleet zuliprc files land)
        # has no bot credentials at all — the relay is simply not in use.
        monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
        res = doctor._zulip_relay_check()
        assert res["status"] == "DORMANT"
        assert res["capability"] == "zulip_relay"

    def test_misconfigured_when_partially_configured(self, monkeypatch, tmp_path):
        # Some bots configured, some missing → a real misconfiguration.
        first = next(iter(zulip._BOT_CONFIG_FILES.values()))
        cred = tmp_path / first
        cred.write_text(
            "[api]\nemail=bot@zulip.example.test\nkey=x\nsite=https://zulip.example.test\n"
        )
        cred.chmod(0o600)
        monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
        res = doctor._zulip_relay_check()
        assert res["status"] == "MISCONFIGURED"
        assert "partial" in res["detail"].lower()

    def test_misconfigured_when_all_credentials_are_invalid(self, monkeypatch, tmp_path):
        for filename in zulip._BOT_CONFIG_FILES.values():
            path = tmp_path / filename
            path.write_text("not a zulip config")
            path.chmod(0o600)
        monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)

        res = doctor._zulip_relay_check()

        assert res["status"] == "MISCONFIGURED"

    def test_live_when_all_credentials_present(self, monkeypatch, tmp_path):
        for filename in zulip._BOT_CONFIG_FILES.values():
            _write = tmp_path / filename
            _write.write_text(
                "[api]\nemail=bot@zulip.example.test\nkey=x\nsite=https://zulip.example.test\n"
            )
            _write.chmod(0o600)
        monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
        res = doctor._zulip_relay_check()
        assert res["status"] == "LIVE"


class TestZulipRelayWithoutSite:
    """#1772: after #946 there is no built-in Zulip site."""

    def _write_all(self, tmp_path):
        for filename in zulip._BOT_CONFIG_FILES.values():
            path = tmp_path / filename
            path.write_text(
                "[api]\nemail=bot@zulip.example.test\nkey=x\nsite=https://zulip.example.test\n"
            )
            path.chmod(0o600)

    def test_retired_without_site_is_dormant(self, monkeypatch, tmp_path):
        self._write_all(tmp_path)
        monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
        monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
        monkeypatch.delenv("UA_ZULIP_RETIRED", raising=False)
        res = doctor._zulip_relay_check()
        assert res["status"] == "DORMANT"
        assert "retired" in res["detail"]

    def test_unretired_without_site_is_misconfigured(self, monkeypatch, tmp_path):
        self._write_all(tmp_path)
        monkeypatch.setattr(zulip.config, "data_home", lambda: tmp_path)
        monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
        monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
        res = doctor._zulip_relay_check()
        assert res["status"] == "MISCONFIGURED"
        assert "UA_ZULIP_SITE" in res["detail"]


class TestTuiDashboardCheck:
    def test_live_when_tui_importable(self):
        checks = _by_capability(system_doctor()["checks"])
        assert checks["tui_dashboard"]["status"] == "LIVE"
        assert "unified_agent.tui:main" in checks["tui_dashboard"]["detail"]
        assert checks["tui_dashboard"]["gate"] == "unified-agent-tui entry point"

    def test_misconfigured_when_entry_point_missing(self, monkeypatch):
        """The entry point names tui:main — a module without it would fail at launch."""
        from unified_agent import tui

        monkeypatch.delattr(tui, "main")
        res = doctor._tui_dashboard_check()
        assert res["status"] == "MISCONFIGURED"
        assert "no callable main()" in res["detail"]


class TestWebUiCheck:
    def test_reports_routes_and_assets(self, monkeypatch, tmp_path):
        from unified_agent.webui import spa as web_spa

        monkeypatch.setenv("UA_TRANSPORT", "http")
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", dist)
        res = doctor._web_ui_check()
        assert res["status"] == "LIVE"
        assert "routes mounted" in res["detail"]

    def test_dormant_on_stdio_transport(self, monkeypatch):
        """Routes can be perfectly fine and still serve nothing."""
        monkeypatch.setenv("UA_TRANSPORT", "stdio")
        res = doctor._web_ui_check()
        assert res["status"] == "DORMANT"
        assert "stdio" in res["detail"]

    def test_misconfigured_when_spa_dist_is_missing(self, monkeypatch, tmp_path):
        """The deploy failure this check exists for: a hub that never received
        packages/web/dist serves a hard 503 on /ui while dev works perfectly."""
        from unified_agent.webui import spa as web_spa

        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.delenv("UA_WEB_DIST_DIR", raising=False)
        monkeypatch.setattr(web_spa, "MODERN_DIST_DIR", tmp_path / "empty")
        res = doctor._web_ui_check()
        assert res["status"] == "MISCONFIGURED"
        assert "packages/web/dist" in res["detail"]
        assert res["reason"] == doctor.UI_NOT_BUILT

    def test_missing_override_build_is_not_the_unbuilt_install_case(self, monkeypatch, tmp_path):
        """An explicit UA_WEB_DIST_DIR without a build is an operator error the
        installer must not tolerate, unlike a package built without the UI."""
        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setenv("UA_WEB_DIST_DIR", str(tmp_path / "custom"))
        res = doctor._web_ui_check()
        assert res["status"] == "MISCONFIGURED"
        assert "reason" not in res


@pytest.fixture
def short_socket_dir():
    """A short private dir for a Unix socket (task #2179).

    ``sun_path`` holds 108 bytes on Linux and 104 on macOS. ``tmp_path`` sits
    under pytest's basetemp, and a deep checkout or a basetemp set by a hook
    pushes ``<tmp_path>/events.sock`` past that, failing ``bind`` with
    ``OSError: AF_UNIX path too long``. Only the socket goes here; it is
    removed with the dir.
    """
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="ua-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class TestEventBusCheck:
    def test_dormant_when_socket_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_EVENT_SOCKET", str(tmp_path / "nope.sock"))
        res = doctor._event_bus_check()
        assert res["status"] == "DORMANT"

    def test_dormant_when_disabled(self, monkeypatch):
        monkeypatch.setenv("UA_EVENT_BUS", "0")
        assert doctor._event_bus_check()["status"] == "DORMANT"

    def test_misconfigured_when_path_is_not_a_socket(self, tmp_path, monkeypatch):
        stray = tmp_path / "events.sock"
        stray.write_text("not a socket")
        monkeypatch.setenv("UA_EVENT_SOCKET", str(stray))
        res = doctor._event_bus_check()
        assert res["status"] == "MISCONFIGURED"
        assert "not a socket" in res["detail"]

    def test_live_when_a_bus_is_actually_listening(self, short_socket_dir, monkeypatch):
        """LIVE must mean 'accepting connections', not merely 'a file exists' —
        the bus was importable for weeks while publishing nothing."""
        from unified_agent import events

        monkeypatch.setenv("UA_EVENT_SOCKET", str(short_socket_dir / "events.sock"))
        bus = events.EventBusServer()
        bus.start()
        try:
            res = doctor._event_bus_check()
            assert res["status"] == "LIVE"
        finally:
            bus.stop()

    def test_misconfigured_on_a_stale_socket(self, short_socket_dir, monkeypatch):
        """A socket file left behind by a dead process must not read as healthy."""
        import socket as _socket

        path = short_socket_dir / "events.sock"
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.close()  # file remains, nothing listening
        monkeypatch.setenv("UA_EVENT_SOCKET", str(path))
        res = doctor._event_bus_check()
        assert res["status"] == "MISCONFIGURED"
        assert "stale socket" in res["detail"]


class TestAgentLabelDriftCheck:
    def test_live_when_every_active_label_is_configured(self, monkeypatch):
        from unified_agent import registry, storage

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        storage.init_db()
        registry.touch_agent("Desktop_Agent1", via="test", force=True)
        assert doctor._agent_label_drift_check()["status"] == "LIVE"

    def test_dormant_lists_unregistered_labels(self, monkeypatch):
        from unified_agent import registry, storage

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        storage.init_db()
        registry.touch_agent("rogue-cron", via="test", force=True)
        res = doctor._agent_label_drift_check()
        assert res["status"] == "DORMANT"
        assert "rogue-cron" in res["detail"]

    def test_shipped_cron_identity_does_not_pin_the_check_dormant(self, monkeypatch):
        """`unified-agent-cron` is stamped by monitor.py on every scheduled run.

        Before the exemption it was permanently in the drift set, so this check
        could never report LIVE on any host running the timers this repo installs.
        """
        from unified_agent import registry, storage

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        monkeypatch.delenv("UA_EXPECTED_UNREGISTERED_LABELS", raising=False)
        storage.init_db()
        registry.touch_agent("unified-agent-cron", via="test", force=True)
        res = doctor._agent_label_drift_check()
        assert res["status"] == "LIVE"
        assert "unified-agent-cron" in res["detail"], "an exemption must stay visible"

    def test_weekly_export_cron_identity_does_not_pin_the_check_dormant(self, monkeypatch):
        """`weekly-export-cron` is stamped by deploy/weekly-export-cron.sh (task #920)."""
        from unified_agent import registry, storage

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        monkeypatch.delenv("UA_EXPECTED_UNREGISTERED_LABELS", raising=False)
        storage.init_db()
        registry.touch_agent("weekly-export-cron", via="test", force=True)
        registry.touch_agent("weekly-export-manual", via="test", force=True)
        res = doctor._agent_label_drift_check()
        assert res["status"] == "LIVE"
        assert "weekly-export-cron" in res["detail"]
        assert "weekly-export-manual" in res["detail"]

    def test_configured_exemption_is_honoured(self, monkeypatch):
        from unified_agent import registry, storage

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        monkeypatch.setenv("UA_EXPECTED_UNREGISTERED_LABELS", "Desktop_Person")
        storage.init_db()
        registry.touch_agent("Desktop_Person", via="test", force=True)
        res = doctor._agent_label_drift_check()
        assert res["status"] == "LIVE"
        assert "Desktop_Person" in res["detail"]

    def test_configured_exemption_unions_with_the_default(self, monkeypatch):
        """Replace-semantics here would silently drop the shipped cron identity.

        That is the failure UA_AUDIT_ROOTS actually suffered, so this variable
        unions instead — asserted, not merely documented.
        """
        from unified_agent import registry, storage

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        monkeypatch.setenv("UA_EXPECTED_UNREGISTERED_LABELS", "Desktop_Person")
        storage.init_db()
        registry.touch_agent("Desktop_Person", via="test", force=True)
        registry.touch_agent("unified-agent-cron", via="test", force=True)
        assert doctor._agent_label_drift_check()["status"] == "LIVE"

    def test_an_unexpected_label_still_trips_alongside_exemptions(self, monkeypatch):
        """The point of the exemption: a real unknown must not be camouflaged."""
        from unified_agent import registry, storage

        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        monkeypatch.setenv("UA_EXPECTED_UNREGISTERED_LABELS", "Desktop_Person")
        storage.init_db()
        registry.touch_agent("Desktop_Person", via="test", force=True)
        registry.touch_agent("unified-agent-cron", via="test", force=True)
        registry.touch_agent("rogue-cron", via="test", force=True)
        res = doctor._agent_label_drift_check()
        assert res["status"] == "DORMANT"
        assert "rogue-cron" in res["detail"]
        assert "1 unregistered label(s)" in res["detail"], "exempt labels must not be counted"


class FakeHub:
    """Minimal MCP streamable-HTTP hub, driven at the urlopen boundary.

    Stubbing here rather than stubbing _hub_auth_probe means the tests exercise
    the REAL HubClient: Accept header, initialize handshake, Mcp-Session-Id, SSE
    decoding. The previous version of these tests stubbed doctor's own urlopen,
    which is precisely why they could not see that the daemon's client was
    broken.
    """

    def __init__(self, *, auth_status=None, require_accept=True, require_session=True):
        self.auth_status = auth_status
        self.require_accept = require_accept
        self.require_session = require_session
        self.requests = []
        self.session_id = "sess-abc"

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.requests.append((body.get("method"), dict(req.headers)))
        headers = {k.lower(): v for k, v in req.headers.items()}

        if self.auth_status:
            raise urllib.error.HTTPError(req.full_url, self.auth_status, "Unauthorized", {}, None)
        if self.require_accept and "text/event-stream" not in (headers.get("accept") or ""):
            raise urllib.error.HTTPError(req.full_url, 406, "Not Acceptable", {}, None)

        if body.get("method") == "initialize":
            return _FakeResp(
                200, {"mcp-session-id": self.session_id}, '{"jsonrpc":"2.0","id":1,"result":{}}'
            )
        if body.get("method") == "notifications/initialized":
            return _FakeResp(202, {}, "")
        if self.require_session and not headers.get("mcp-session-id"):
            raise urllib.error.HTTPError(req.full_url, 400, "Missing session ID", {}, None)
        payload = '{"jsonrpc":"2.0","id":3,"result":{"tools":[]}}'
        return _FakeResp(
            200, {"content-type": "text/event-stream"}, f"event: message\r\ndata: {payload}\r\n\r\n"
        )


class _FakeResp:
    def __init__(self, status, headers, text):
        self.status = status
        self.headers = headers
        self._text = text

    def read(self):
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestActiveExecutorCheck:
    """The executor check must report capability, not liveness.

    Unit-active alone gave a six-day false all-clear (2026-07-19 -> 07-25) while
    every poll was 401-rejected. See #173/#174 and PR #82.
    """

    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch):
        # The real host has a populated executor.env; tests must not read it.
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: None)
        monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [])
        monkeypatch.setenv("UA_EXECUTOR_AGENT", "Desktop_agent3")
        monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:8765/mcp")

    @staticmethod
    def _unit_active(monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "active"},
        )

    @staticmethod
    def _hub(monkeypatch, **kw):
        hub = FakeHub(**kw)
        monkeypatch.setattr(hub_client.urllib.request, "urlopen", hub)
        return hub

    def test_live_when_the_daemons_own_client_completes_a_round_trip(self, monkeypatch):
        self._unit_active(monkeypatch)
        hub = self._hub(monkeypatch)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")

        res = doctor._active_executor_check()

        assert res["status"] == "LIVE"
        assert "MCP session established" in res["detail"]
        # It really handshook rather than firing a bare tools/list.
        assert [m for m, _ in hub.requests][0] == "initialize"

    def test_misconfigured_when_hub_rejects_credential(self, monkeypatch):
        self._unit_active(monkeypatch)
        self._hub(monkeypatch, auth_status=401)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-wrong")

        res = doctor._active_executor_check()
        assert res["status"] == "MISCONFIGURED"
        assert "401" in res["detail"]

    def test_missing_accept_header_is_NOT_reported_as_healthy(self, monkeypatch):
        """The blind spot this check shipped with, now pinned.

        The first version built its own request and sent Accept, so it reported
        LIVE while every one of the daemon's calls died on 406. Sharing
        HubClient means a transport defect fails the check.
        """
        self._unit_active(monkeypatch)
        self._hub(monkeypatch, require_accept=True)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")
        # Simulate the pre-fix client: strip the Accept header it now sends.
        # Patched on the transport the executor's HubClient inherits (#1722).
        original = hub_client.HubClient._headers
        monkeypatch.setattr(
            hub_client.HubClient,
            "_headers",
            lambda self, extra=None: {
                k: v for k, v in original(self, extra).items() if k != "Accept"
            },
        )

        res = doctor._active_executor_check()
        assert res["status"] == "MISCONFIGURED"
        assert "406" in res["detail"]

    def test_missing_session_handshake_is_NOT_reported_as_healthy(self, monkeypatch):
        self._unit_active(monkeypatch)
        self._hub(monkeypatch, require_session=True)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")
        monkeypatch.setattr(hub_client.HubClient, "_ensure_session", lambda self: None)

        res = doctor._active_executor_check()
        assert res["status"] == "MISCONFIGURED"
        assert "400" in res["detail"]

    def test_misconfigured_when_no_token_resolves(self, monkeypatch):
        self._unit_active(monkeypatch)
        monkeypatch.delenv("UA_EXECUTOR_TOKEN", raising=False)
        monkeypatch.delenv("UA_AGENT_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)

        res = doctor._active_executor_check()
        assert res["status"] == "MISCONFIGURED"
        assert "no bearer token" in res["detail"]

    def test_misconfigured_when_primary_hub_url_is_missing(self, monkeypatch):
        self._unit_active(monkeypatch)
        monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")

        res = doctor._active_executor_check()

        assert res["status"] == "MISCONFIGURED"
        assert "UA_HTTP_HUB_URL is required" in res["detail"]
        assert "UA_HTTP_HOST/UA_HTTP_PORT" in res["detail"]

    def test_token_is_read_from_the_units_environment_file(self, monkeypatch, tmp_path):
        """The doctor rarely shares the executor's environment.

        Reading only os.environ made it assert 'no bearer token resolves' about
        a correctly-credentialed daemon — a misconfiguration report caused by
        the doctor looking in the wrong place.
        """
        env_file = tmp_path / "executor.env"
        env_file.write_text("UA_EXECUTOR_AGENT=Desktop_agent3\nUA_EXECUTOR_TOKEN=tok-from-unit\n")
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: "")
        monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [env_file])
        monkeypatch.delenv("UA_EXECUTOR_TOKEN", raising=False)
        monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
        self._unit_active(monkeypatch)
        hub = self._hub(monkeypatch)

        assert doctor._active_executor_check()["status"] == "LIVE"
        assert hub.requests[0][1]["Authorization"] == "Bearer tok-from-unit"

    def test_agent_label_is_read_from_the_units_environment_file(self, monkeypatch, tmp_path):
        """Task #1215: a live, correctly-configured executor read as MISCONFIGURED.

        The desktop unit set UA_EXECUTOR_AGENT=Desktop_agent3 in its own
        Environment=, but the doctor was invoked from a shell that never
        exported the variable, so `config.executor_agent()` (os.environ-only)
        came back empty and the check asserted "UA_EXECUTOR_AGENT is unset"
        about a unit that was actively polling and authenticating fine. Same
        failure class as the token-resolution bug this class already guards
        (test_token_is_read_from_the_units_environment_file).
        """
        env_file = tmp_path / "executor.env"
        env_file.write_text("UA_EXECUTOR_AGENT=Desktop_agent3\nUA_EXECUTOR_TOKEN=tok-real\n")
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: "")
        monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [env_file])
        monkeypatch.delenv("UA_EXECUTOR_AGENT", raising=False)
        self._unit_active(monkeypatch)
        self._hub(monkeypatch)

        res = doctor._active_executor_check()

        assert res["status"] == "LIVE"

    def test_misconfigured_when_agent_label_truly_unset(self, monkeypatch):
        """Preserve the real misconfiguration: neither unit env nor caller env has it."""
        monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [])
        monkeypatch.setattr(doctor, "_service_env", lambda unit: {})
        monkeypatch.delenv("UA_EXECUTOR_AGENT", raising=False)
        self._unit_active(monkeypatch)

        res = doctor._active_executor_check()

        assert res["status"] == "MISCONFIGURED"
        assert "UA_EXECUTOR_AGENT is unset" in res["detail"]

    def test_misconfigured_when_hub_unreachable(self, monkeypatch):
        self._unit_active(monkeypatch)

        def refuse(req, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr(hub_client.urllib.request, "urlopen", refuse)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")

        res = doctor._active_executor_check()
        assert res["status"] == "MISCONFIGURED"
        assert "round-trip failed" in res["detail"]

    def test_dormant_when_unit_not_installed(self, monkeypatch):
        monkeypatch.setattr(
            doctor, "_service_probe", lambda unit, timeout: {"LoadState": "not-found"}
        )
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")
        assert doctor._active_executor_check()["status"] == "DORMANT"

    def test_probe_never_falls_back_to_in_process_dispatch(self, monkeypatch):
        """A doctor that satisfies itself through the bypass PR #82 removed
        would report LIVE for the exact broken state."""
        self._unit_active(monkeypatch)
        self._hub(monkeypatch, auth_status=401)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-wrong")
        called = []
        for cls in (doctor_daemon.HubClient, hub_client.HubClient):
            monkeypatch.setattr(
                cls,
                "_direct_tool_fallback",
                lambda self, name, args: called.append(name),
            )

        assert doctor._active_executor_check()["status"] == "MISCONFIGURED"
        assert called == []

    def test_probe_failure_does_not_abort_the_report(self, monkeypatch):
        self._unit_active(monkeypatch)

        def boom(*a, **kw):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(doctor, "_hub_auth_probe", boom)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")

        res = doctor._active_executor_check()
        assert res["status"] == "MISCONFIGURED"
        assert "probe exploded" in res["detail"]

    def test_in_hub_invocation_reports_unknown_instead_of_self_call_failure(self, monkeypatch):
        self._unit_active(monkeypatch)
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")
        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setitem(__import__("sys").modules, "unified_agent.server", object())

        def timeout(*a, **kw):
            raise TimeoutError("timed out")

        monkeypatch.setattr(doctor, "_hub_auth_probe", timeout)

        res = doctor._active_executor_check()
        assert res["status"] == "UNKNOWN"
        assert "hub-served system_doctor call" in res["detail"]


class TestChecksReadTheServiceConfigNotTheCallers:
    """system_doctor describes the SERVICE, not whoever invoked it.

    hub_auth and approval_gates called config readers that consult os.environ,
    so running the doctor from a shell reported "no bearer tokens configured —
    every HTTP caller is anonymous" about a hub running UA_ENFORCE_AUTH=1 with
    three tokens, and "no tools gated" about a hub gating four. Task #176; the
    same defect as the executor token in #88, which had been fixed in only one
    of the three places that needed it.
    """

    @staticmethod
    def _hub_unit(monkeypatch, env_line, env_file=None):
        monkeypatch.setattr(
            doctor,
            "_unit_property",
            lambda unit, prop: env_line if prop == "Environment" else "",
        )
        monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: env_file or [])

    @staticmethod
    def _caller_env_empty(monkeypatch):
        for var in ("UA_AGENT_TOKENS", "UA_ENFORCE_AUTH", "UA_APPROVAL_TOOLS"):
            monkeypatch.delenv(var, raising=False)

    def test_hub_auth_live_from_the_units_env_with_an_empty_caller_env(self, monkeypatch):
        self._caller_env_empty(monkeypatch)
        self._hub_unit(monkeypatch, "UA_AGENT_TOKENS=A:tok1,B:tok2 UA_ENFORCE_AUTH=1")

        res = doctor._hub_auth_check()

        assert res["status"] == "LIVE"
        assert "2 token(s)" in res["detail"]
        assert "enforced" in res["detail"]

    def test_approval_gates_lists_what_the_hub_gates(self, monkeypatch):
        self._caller_env_empty(monkeypatch)
        self._hub_unit(monkeypatch, "UA_APPROVAL_TOOLS=run_cmd:delegate")

        res = doctor._approval_gates_check()

        assert res["status"] == "LIVE"
        assert "run_cmd" in res["detail"] and "delegate" in res["detail"]

    def test_a_token_in_an_environment_file_is_found(self, monkeypatch, tmp_path):
        self._caller_env_empty(monkeypatch)
        env_file = tmp_path / "hub.env"
        env_file.write_text('UA_AGENT_TOKENS="A:tok1"\n# a comment\n\n')
        self._hub_unit(monkeypatch, "UA_ENFORCE_AUTH=1", env_file=[env_file])

        assert doctor._hub_auth_check()["status"] == "LIVE"

    def test_a_genuinely_unconfigured_hub_still_reports_dormant(self, monkeypatch):
        """The check must not become unfalsifiable in the process of being accurate."""
        self._caller_env_empty(monkeypatch)
        self._hub_unit(monkeypatch, "UA_HTTP_PORT=8765")

        assert doctor._hub_auth_check()["status"] == "DORMANT"
        # A readable unit that omits UA_APPROVAL_BASELINE uses the default,
        # which gates delegate. The caller's UA_APPROVAL_BASELINE must not
        # override that omission.
        gates = doctor._approval_gates_check()
        assert gates["status"] == "LIVE"
        assert "delegate" in gates["detail"]

    def test_undeterminable_config_is_not_reported_as_off(self, monkeypatch):
        """'I cannot tell' and 'it is off' are different findings."""
        self._caller_env_empty(monkeypatch)
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: None)

        for check in (doctor._hub_auth_check(), doctor._approval_gates_check()):
            assert check["status"] == "MISCONFIGURED"
            assert "cannot determine" in check["detail"]

    def test_caller_env_is_a_fallback_not_the_source_of_truth(self, monkeypatch):
        """When the unit says nothing, the caller's env may still answer — but the
        unit wins when both are set."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "only_from_caller")
        self._hub_unit(monkeypatch, "UA_APPROVAL_TOOLS=from_the_unit")

        assert "from_the_unit" in doctor._approval_gates_check()["detail"]
        assert "only_from_caller" not in doctor._approval_gates_check()["detail"]

    def test_http_unit_without_enforce_auth_is_reported_enforced(self, monkeypatch):
        """Unset UA_ENFORCE_AUTH on an HTTP hub is the new default, not warn-mode."""
        self._caller_env_empty(monkeypatch)
        monkeypatch.delenv("UA_TRANSPORT", raising=False)
        monkeypatch.delenv("UA_LEGACY_OPEN_POSTURE", raising=False)
        self._hub_unit(monkeypatch, "UA_AGENT_TOKENS=A:tok1 UA_TRANSPORT=http")

        res = doctor._hub_auth_check()

        assert res["status"] == "LIVE"
        assert "enforced" in res["detail"]

    def test_legacy_open_posture_on_the_unit_stays_warn_mode(self, monkeypatch):
        self._caller_env_empty(monkeypatch)
        monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
        self._hub_unit(
            monkeypatch,
            "UA_AGENT_TOKENS=A:tok1 UA_TRANSPORT=http UA_LEGACY_OPEN_POSTURE=1",
        )

        assert "warn-mode" in doctor._hub_auth_check()["detail"]

    def test_unit_baseline_off_is_not_replaced_by_the_caller_default(self, monkeypatch):
        """The doctor's own unset baseline must not invent a delegate gate."""
        self._caller_env_empty(monkeypatch)
        monkeypatch.delenv("UA_APPROVAL_BASELINE", raising=False)
        monkeypatch.delenv("UA_LEGACY_OPEN_POSTURE", raising=False)
        self._hub_unit(monkeypatch, "UA_APPROVAL_BASELINE=0")

        assert doctor._approval_gates_check()["status"] == "DORMANT"

    def test_readable_unit_ignores_caller_posture_flags(self, monkeypatch):
        """An omitted unit flag is the default, not the operator shell's value."""
        self._hub_unit(monkeypatch, "UA_AGENT_TOKENS=A:tok1 UA_TRANSPORT=http")
        monkeypatch.setenv("UA_LEGACY_OPEN_POSTURE", "1")
        monkeypatch.setenv("UA_APPROVAL_BASELINE", "0")
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "only_from_caller")

        assert "enforced" in doctor._hub_auth_check()["detail"]
        gates = doctor._approval_gates_check()
        assert gates["status"] == "LIVE"
        assert "delegate" in gates["detail"]
        assert "only_from_caller" not in gates["detail"]


class TestDarwinExecutorEnv:
    @staticmethod
    def _executor_launchagent(
        monkeypatch,
        tmp_path,
        *,
        env_file_text=None,
        launcher_text=None,
        plist_env=None,
    ):
        """A LaunchAgent plist whose ProgramArguments delegate to a .sh launcher."""
        home = tmp_path / "home"
        launch_agents = home / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        launcher = tmp_path / "run-executor-agy.sh"
        if launcher_text is not None:
            launcher.write_text(launcher_text)
        plist = launch_agents / "com.unified-agent.executor-agy.plist"
        with plist.open("wb") as fh:
            plistlib.dump(
                {
                    "Label": "com.unified-agent.executor-agy",
                    "EnvironmentVariables": plist_env or {},
                    "ProgramArguments": [str(launcher)],
                },
                fh,
            )
        files = []
        if env_file_text is not None:
            env_file = tmp_path / "executor.env"
            env_file.write_text(env_file_text)
            files.append(env_file)
        monkeypatch.setattr(doctor, "_mac_home", lambda: home)
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: None)
        monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: files)

    def test_service_env_layers_plist_env_files_then_launcher_exports(self, monkeypatch, tmp_path):
        self._executor_launchagent(
            monkeypatch,
            tmp_path,
            plist_env={"UA_PLIST_ONLY": "plist", "UA_OVERLAP": "plist"},
            env_file_text="UA_OVERLAP=env-file\nUA_EXECUTOR_AGENT=env_file_agent\n",
            launcher_text="#!/bin/sh\nexport UA_EXECUTOR_AGENT=server_agent3\n",
        )
        env = doctor._service_env("unified-agent-executor.service")
        assert env["UA_PLIST_ONLY"] == "plist"
        assert env["UA_OVERLAP"] == "env-file"
        assert env["UA_EXECUTOR_AGENT"] == "server_agent3"

    def test_active_executor_check_uses_agent_from_delegated_launcher(self, monkeypatch, tmp_path):
        """Task #1233: the Mac executor's identity lives in the repo launcher
        the plist delegates to, not in any env file — the doctor must read it."""
        self._executor_launchagent(
            monkeypatch,
            tmp_path,
            env_file_text="UA_EXECUTOR_TOKEN=tok-mac\n",
            launcher_text="#!/bin/sh\nexport UA_EXECUTOR_AGENT=server_agent3\n",
        )
        monkeypatch.delenv("UA_EXECUTOR_AGENT", raising=False)
        monkeypatch.delenv("UA_EXECUTOR_TOKEN", raising=False)
        monkeypatch.delenv("UA_AGENT_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "active"},
        )
        seen = []
        monkeypatch.setattr(
            doctor.config,
            "http_hub_url",
            lambda agent: seen.append(agent) or "http://127.0.0.1:8765/mcp",
        )
        monkeypatch.setattr(doctor, "_hub_auth_probe", lambda url, token, timeout: (True, "ok"))

        res = doctor._active_executor_check()

        assert res["status"] == "LIVE"
        assert seen == ["server_agent3"]

    def test_missing_delegated_launcher_makes_agent_label_undetermined(self, monkeypatch, tmp_path):
        """The plist exists but its .sh launcher is unreadable — 'cannot tell',
        not 'unset'."""
        self._executor_launchagent(
            monkeypatch,
            tmp_path,
            env_file_text="UA_EXECUTOR_TOKEN=tok-mac\n",
            launcher_text=None,
        )
        monkeypatch.delenv("UA_EXECUTOR_AGENT", raising=False)
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "active"},
        )

        res = doctor._active_executor_check()

        assert res["status"] == "UNKNOWN"
        assert "cannot determine UA_EXECUTOR_AGENT" in res["detail"]

    def test_dynamic_agent_export_makes_label_undetermined(self, monkeypatch, tmp_path):
        """An export the launcher does not pin literally (a ${...} expansion)
        cannot be known by inspection — report 'cannot tell', not 'unset'."""
        self._executor_launchagent(
            monkeypatch,
            tmp_path,
            env_file_text="UA_EXECUTOR_TOKEN=tok-mac\n",
            launcher_text='#!/bin/sh\nexport UA_EXECUTOR_AGENT="${UA_EXECUTOR_AGENT:-server_agent3}"\n',
        )
        monkeypatch.delenv("UA_EXECUTOR_AGENT", raising=False)
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "active"},
        )

        res = doctor._active_executor_check()

        assert res["status"] == "UNKNOWN"
        assert "cannot determine UA_EXECUTOR_AGENT" in res["detail"]

    def test_service_env_reads_launchagent_file_when_systemctl_absent(self, monkeypatch, tmp_path):
        env_file = tmp_path / "executor.env"
        env_file.write_text("UA_EXECUTOR_TOKEN=tok-mac\n")
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: None)
        monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [env_file])
        env = doctor._service_env("unified-agent-executor.service")
        assert env is not None
        assert env["UA_EXECUTOR_TOKEN"] == "tok-mac"

    @pytest.mark.parametrize(
        "body",
        [
            b"<plist/>",
            b'<plist version="1.0"><array/></plist>',
            b'<plist version="1.0"><string>x</string></plist>',
        ],
    )
    def test_non_dict_plist_root_is_an_empty_undetermined_layer(self, monkeypatch, tmp_path, body):
        """Task #1803: a plist whose root is not a dict (``<plist/>`` loads as
        None) used to raise AttributeError out of every _service_env caller.
        It is read like an unparseable plist: an empty layer, 'cannot tell'."""
        home = tmp_path / "home"
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.unified-agent.executor-agy.plist").write_bytes(body)
        env_file = tmp_path / "executor.env"
        env_file.write_text("UA_EXECUTOR_TOKEN=tok-mac\n")
        monkeypatch.setattr(doctor, "_mac_home", lambda: home)
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: None)
        monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [env_file])

        layers = doctor._mac_launchagent_environment_layers("unified-agent-executor.service")

        assert layers == ({}, {}, False)
        assert doctor._service_env("unified-agent-executor.service") is None

    def test_unit_check_names_the_launchagent(self, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "active"},
        )
        res = doctor._unit_check("active_executor_service", "unified-agent-executor.service")
        assert res["status"] == "LIVE"
        assert "com.unified-agent.executor-agy" in res["detail"]


class TestHostLaunchAgentsAreInvisible:
    """Task #1373: on the Mac hub host two tests failed because, with
    ``_unit_property`` stubbed to None (systemctl absent), ``_service_env``
    still found the REAL ``~/Library/LaunchAgents/com.unified-agent.*.plist``
    and ``~/.unified_agent/*.env`` and reported LIVE. These simulate that host
    on any OS: HOME holds a populated hub + executor LaunchAgent, platform is
    darwin, and the checks must still see only the test's own configuration.
    """

    @staticmethod
    def _populated_host_home(monkeypatch, tmp_path):
        home = tmp_path / "host_home"
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        launcher = home / ".unified_agent" / "run-hub-live-test.sh"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexport UA_ENFORCE_AUTH=1\n")
        (home / ".unified_agent" / "service.env").write_text("UA_APPROVAL_TOOLS=run_cmd\n")
        (home / ".config" / "unified-agent").mkdir(parents=True)
        (home / ".config" / "unified-agent" / "executor.env").write_text(
            "UA_HTTP_HUB_URL=http://127.0.0.1:8765/mcp\n"
        )
        for label, env in (
            ("com.unified-agent.hub", {"UA_AGENT_TOKENS": "A:tok1", "UA_APPROVAL_TOOLS": "x"}),
            (
                "com.unified-agent.executor-agy",
                {"UA_HTTP_HUB_URL": "http://127.0.0.1:8765/mcp", "UA_EXECUTOR_AGENT": "a"},
            ),
        ):
            with (agents / f"{label}.plist").open("wb") as fh:
                plistlib.dump(
                    {
                        "Label": label,
                        "EnvironmentVariables": env,
                        "ProgramArguments": [str(launcher)],
                    },
                    fh,
                )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        return home

    def test_hub_plist_does_not_make_undeterminable_config_live(self, monkeypatch, tmp_path):
        self._populated_host_home(monkeypatch, tmp_path)
        for var in ("UA_AGENT_TOKENS", "UA_ENFORCE_AUTH", "UA_APPROVAL_TOOLS"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: None)

        for check in (doctor._hub_auth_check(), doctor._approval_gates_check()):
            assert check["status"] == "MISCONFIGURED"
            assert "cannot determine" in check["detail"]

    def test_executor_plist_does_not_supply_a_missing_hub_url(self, monkeypatch, tmp_path):
        self._populated_host_home(monkeypatch, tmp_path)
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: None)
        monkeypatch.setenv("UA_EXECUTOR_AGENT", "Desktop_agent3")
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "tok-real")
        monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "active"},
        )

        res = doctor._active_executor_check()

        assert res["status"] == "MISCONFIGURED"
        assert "UA_HTTP_HUB_URL is required" in res["detail"]

    def test_mac_config_paths_hang_off_the_mac_home_seam(self, monkeypatch, tmp_path):
        monkeypatch.setattr(doctor, "_mac_home", lambda: tmp_path)

        assert doctor._mac_environment_files("unified-agent.service") == [
            tmp_path / ".unified_agent" / "service.env"
        ]
        assert doctor._mac_launchagent_plist("unified-agent.service") == (
            tmp_path / "Library" / "LaunchAgents" / "com.unified-agent.hub.plist"
        )

    def test_mac_env_file_fallbacks_do_not_resolve_under_the_host_home(self, monkeypatch, tmp_path):
        """The unpatched EnvironmentFile resolver must not reach ~/.unified_agent."""
        home = self._populated_host_home(monkeypatch, tmp_path)
        monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: None)

        for unit in doctor._MAC_ENVIRONMENT_FILES:
            for path in _REAL_UNIT_ENVIRONMENT_FILES(unit):
                assert home not in path.parents, path


class TestLlmProviderChainCheck:
    """Task #1178: llm_client's per-leg read surface inside system_doctor —
    the only prior way to learn a prq-* leg's state was a `complete()` call
    that could bill the metered ai_studio leg."""

    def _run(
        self,
        monkeypatch,
        legs,
        service_env=None,
        captured=None,
        nightly_legs=None,
        sleep_pass_installed=True,
    ):
        """Hub legs are `legs`; the nightly chain's are `nightly_legs` (default: the same)."""
        from unified_agent import llm_client, sleep_pass

        monkeypatch.setattr(doctor, "_service_env", lambda unit: service_env or {})
        monkeypatch.setattr(doctor, "_sleep_pass_installed", lambda: sleep_pass_installed)
        nightly_env = {"__nightly__": "1"}
        monkeypatch.setattr(sleep_pass, "scheduled_env", lambda: nightly_env)

        def fake_leg_status(env=None, timeout_s=None, memory_agent=False):
            if env is nightly_env:
                assert memory_agent is True
                return legs if nightly_legs is None else nightly_legs
            if captured is not None:
                captured.append(env)
            return legs

        monkeypatch.setattr(llm_client, "leg_status", fake_leg_status)
        return doctor._llm_provider_chain_check()

    def _leg(self, name, configured, reachable, explicit=None, detail=""):
        return {
            "leg": name,
            "configured": configured,
            "explicit": configured if explicit is None else explicit,
            "reachable": reachable,
            "detail": detail,
        }

    def test_live_when_any_leg_reachable(self, monkeypatch):
        check = self._run(
            monkeypatch,
            [
                self._leg("ollama", True, True),
                self._leg("ai_studio", True, False, detail="models list: HTTP 401"),
                self._leg("openrouter", False, None),
                self._leg("subscription_pool", False, None),
            ],
        )
        assert check["capability"] == "llm_provider_chain"
        assert check["status"] == "LIVE"
        assert "ollama:up" in check["detail"]
        assert "ai_studio:down" in check["detail"]

    def test_misconfigured_when_explicitly_configured_legs_all_down(self, monkeypatch):
        check = self._run(
            monkeypatch,
            [
                self._leg("ollama", True, False, explicit=True),
                self._leg("ai_studio", True, False),
                self._leg("openrouter", False, None),
                self._leg("subscription_pool", False, None),
            ],
        )
        assert check["status"] == "MISCONFIGURED"

    def test_dormant_when_nothing_explicitly_configured(self, monkeypatch):
        """A host with no keys and no local ollama is not misconfigured — it
        simply has no chain; the sleep-pass timer check is the one that says
        whether that matters here."""
        check = self._run(
            monkeypatch,
            [
                self._leg("ollama", True, False, explicit=False),
                self._leg("ai_studio", False, None),
                self._leg("openrouter", False, None),
                self._leg("subscription_pool", False, None),
            ],
        )
        assert check["status"] == "DORMANT"

    def test_probe_failure_is_misconfigured_not_a_crash(self, monkeypatch):
        from unified_agent import llm_client

        monkeypatch.setattr(doctor, "_service_env", lambda unit: {})
        monkeypatch.setattr(
            llm_client,
            "leg_status",
            lambda env=None, timeout_s=None, memory_agent=False: (_ for _ in ()).throw(
                RuntimeError("boom")
            ),
        )
        check = doctor._llm_provider_chain_check()
        assert check["status"] == "MISCONFIGURED"
        assert "boom" in check["detail"]

    def test_hub_service_env_reaches_leg_status(self, monkeypatch):
        """The legs run inside the hub (gemini_ask) and timer units that source
        the same env — the probe must see the unit's environment, not just the
        caller's, same rule as _hub_setting."""
        captured = []
        self._run(
            monkeypatch,
            [self._leg("ollama", True, True)],
            service_env={"GEMINI_API_KEY": "hub-key"},
            captured=captured,
        )
        assert captured and captured[0]["GEMINI_API_KEY"] == "hub-key"

    def test_healthy_ai_studio_shows_the_credit_caveat(self, monkeypatch):
        """Task #1639: a models-list 200 proves the key, not the prepaid
        balance; depleted credit 402s only on generation. 'up' alone hid that."""
        ai = self._leg("ai_studio", True, True, detail="models list: HTTP 200")
        ai["caveat"] = "key valid; prepaid credit not probed"
        check = self._run(monkeypatch, [self._leg("ollama", True, False), ai])
        assert "ai_studio:up (key valid; prepaid credit not probed)" in check["detail"]

    def test_nightly_chain_gets_its_own_rows(self, monkeypatch):
        """Task #1639: the nightly pass has its own provider settings, so its
        configured legs are listed beside the hub's."""
        check = self._run(
            monkeypatch,
            [self._leg("ollama", True, True)],
            nightly_legs=[
                self._leg("ollama", True, False, detail="qwen2.5-coder:1.5b not pulled"),
                self._leg("ai_studio", False, None),
            ],
        )
        assert "ollama:up" in check["detail"]
        assert "ollama[sleep-pass]:down (qwen2.5-coder:1.5b not pulled)" in check["detail"]
        assert "ai_studio[sleep-pass]" not in check["detail"]

    def test_working_nightly_model_is_not_reported_unavailable(self, monkeypatch):
        """Devin on PR #932: hub model unpulled and no remote leg, but the
        nightly model works — the scheduled pass can run, so not MISCONFIGURED."""
        check = self._run(
            monkeypatch,
            [self._leg("ollama", True, False), self._leg("ai_studio", True, False)],
            nightly_legs=[self._leg("ollama", True, True)],
        )
        assert check["status"] == "DEGRADED"
        assert "nightly sleep pass usable" in check["detail"]
        assert "ollama[sleep-pass]:up" in check["detail"]

    def test_working_hub_model_does_not_mask_a_broken_nightly_target(self, monkeypatch):
        check = self._run(
            monkeypatch,
            [self._leg("ollama", True, True), self._leg("ai_studio", True, False)],
            nightly_legs=[self._leg("ollama", True, False)],
        )
        assert check["status"] == "DEGRADED"
        assert "nightly sleep pass has no usable leg" in check["detail"]

    def test_hub_remote_leg_does_not_serve_the_nightly_pass(self, monkeypatch):
        """Independent review of PR #932: the nightly pass takes remote keys
        only from sleep-pass.env, so a live hub AI Studio key with a dead
        nightly model is still a nightly outage."""
        ai = self._leg("ai_studio", True, True)
        ai["caveat"] = "key valid; prepaid credit not probed"
        check = self._run(
            monkeypatch,
            [self._leg("ollama", True, False), ai],
            nightly_legs=[self._leg("ollama", True, False), self._leg("ai_studio", False, None)],
        )
        assert check["status"] == "DEGRADED"
        assert "nightly sleep pass has no usable leg" in check["detail"]

    def test_host_without_the_sleep_pass_reports_the_hub_chain_alone(self, monkeypatch):
        """#1731: a desktop that never runs the sleep pass was DEGRADED because
        its (absent) nightly chain had no leg; only judge it where it runs."""
        check = self._run(
            monkeypatch,
            [self._leg("ollama", True, False), self._leg("ai_studio", True, True)],
            nightly_legs=[self._leg("ollama", True, False)],
            sleep_pass_installed=False,
        )
        assert check["status"] == "LIVE"
        assert "[sleep-pass]" not in check["detail"]

    def test_indeterminate_sleep_pass_probe_still_judges_the_nightly_chain(self, monkeypatch):
        """A probe that cannot tell (systemctl/launchctl unavailable or timed
        out) is not an absent timer: a broken nightly chain must not hide
        behind a working hub chain."""
        check = self._run(
            monkeypatch,
            [self._leg("ollama", True, True)],
            nightly_legs=[self._leg("ollama", True, False)],
            sleep_pass_installed=None,
        )
        assert check["status"] == "DEGRADED"
        assert "nightly sleep pass has no usable leg" in check["detail"]

    @pytest.mark.parametrize(
        "props,expected",
        [(None, None), ({"LoadState": "loaded"}, True), ({"LoadState": "not-found"}, False)],
    )
    def test_sleep_pass_installed_keeps_an_indeterminate_probe(self, monkeypatch, props, expected):
        monkeypatch.setattr(doctor, "_service_probe", lambda unit, timeout: props)
        assert doctor._sleep_pass_installed() is expected

    def test_both_chains_down_stays_misconfigured(self, monkeypatch):
        check = self._run(
            monkeypatch,
            [self._leg("ollama", True, False), self._leg("ai_studio", True, False)],
            nightly_legs=[self._leg("ollama", True, False)],
        )
        assert check["status"] == "MISCONFIGURED"

    def test_check_is_wired_into_system_doctor(self, monkeypatch):
        from unified_agent import llm_client

        monkeypatch.setattr(
            llm_client,
            "leg_status",
            lambda env=None, timeout_s=None, memory_agent=False: [self._leg("ollama", True, True)],
        )
        checks = _by_capability(system_doctor()["checks"])
        assert checks["llm_provider_chain"]["status"] == "LIVE"


class TestVaultSyncCheck:
    """task #1288: freshness + conflict-count report for the hub -> desktop pull."""

    @pytest.fixture(autouse=True)
    def _sync_unit_uses_test_home(self, isolated_env, monkeypatch):
        from unified_agent import config

        original = doctor._service_env
        monkeypatch.setattr(
            doctor,
            "_service_env",
            lambda unit: (
                {
                    "UA_HOME": str(config.data_home()),
                    "UA_VAULT_SYNC_LOCAL_ROOT": str(config.data_home() / "vault"),
                    "UA_VAULT_SYNC_REMOTE_HOST": "vault-host",
                    "UA_VAULT_SYNC_REMOTE_ROOT": "vault",
                }
                if unit == "ua-vault-sync.service"
                else original(unit)
            ),
        )

    def _write_state(self, tmp_path, **overrides):
        from unified_agent import config

        payload = {
            "ok": True,
            "reachable": True,
            "dry_run": False,
            "pulled": [],
            "pulled_count": 0,
            "conflicts": [],
            "conflict_count": 0,
            "retired_skipped": [],
            "retired_skipped_count": 0,
            "error": None,
            "started_at": doctor.storage.now(),
            "finished_at": doctor.storage.now(),
            "last_run_utc": doctor.storage.now(),
            "last_success_utc": doctor.storage.now(),
        }
        payload.update(overrides)
        state_path = config.vault_sync_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_dormant_when_not_installed_and_no_state(self, isolated_env, monkeypatch):
        monkeypatch.setattr(doctor, "_service_probe", lambda unit, timeout: None)
        check = doctor._vault_sync_check()
        assert check["status"] == "DORMANT"
        assert "not configured on this host" in check["detail"]

    def test_dormant_reads_quietly_on_a_non_syncing_host(self, isolated_env, monkeypatch):
        """The Mac hub never runs this timer (direction is hub -> desktop only);
        doctor must not turn red there just because nothing is installed."""
        monkeypatch.setattr(
            doctor, "_service_probe", lambda unit, timeout: {"LoadState": "not-found"}
        )
        check = doctor._vault_sync_check()
        assert check["status"] == "DORMANT"

    def test_unknown_when_installed_but_never_run(self, isolated_env, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        check = doctor._vault_sync_check()
        assert check["status"] == "UNKNOWN"
        assert "has not completed a run yet" in check["detail"]

    def test_corrupt_state_is_misconfigured_even_without_timer(self, isolated_env, monkeypatch):
        from unified_agent import config

        monkeypatch.setattr(doctor, "_service_probe", lambda unit, timeout: None)
        state_path = config.vault_sync_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("not json", encoding="utf-8")
        check = doctor._vault_sync_check()
        assert check["status"] == "MISCONFIGURED"
        assert "cannot read sync state" in check["detail"]

    def test_malformed_nested_state_is_misconfigured(self, isolated_env, monkeypatch):
        from unified_agent import config

        monkeypatch.setattr(doctor, "_service_probe", lambda unit, timeout: None)
        state_path = config.vault_sync_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text('{"conflicts": null}', encoding="utf-8")
        check = doctor._vault_sync_check()
        assert check["status"] == "MISCONFIGURED"
        assert "invalid conflicts" in check["detail"]

    def test_live_when_fresh_and_no_conflicts(self, isolated_env, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        self._write_state(isolated_env)
        check = doctor._vault_sync_check()
        assert check["status"] == "LIVE"
        assert "0 unresolved conflict(s)" in check["detail"]

    def test_legacy_filed_upstream_cannot_certify_a_clean_sync(self, isolated_env, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        self._write_state(
            isolated_env,
            filed_upstream=["00 - Inbox/filed.md"],
            filed_upstream_count=1,
        )
        check = doctor._vault_sync_check()
        assert check["status"] == "MISCONFIGURED"
        assert "1 legacy filed-upstream" in check["detail"]
        assert "requires fresh sync" in check["detail"]

    def test_future_success_is_not_fresh(self, isolated_env, monkeypatch):
        from datetime import UTC, datetime, timedelta

        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        self._write_state(isolated_env, last_success_utc=future)
        assert doctor._vault_sync_check()["status"] == "MISCONFIGURED"

    def test_reads_timer_environment_not_doctor_environment(self, isolated_env, monkeypatch):
        from unified_agent import config

        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        state = self._write_state(isolated_env)
        timer_home = isolated_env / "timer-home"
        timer_home.mkdir()
        (timer_home / "vault-sync-state.json").write_text(json.dumps(state), encoding="utf-8")
        monkeypatch.setattr(
            doctor,
            "_service_env",
            lambda unit: (
                {
                    "UA_HOME": str(timer_home),
                    "UA_VAULT_SYNC_LOCAL_ROOT": str(timer_home / "vault"),
                    "UA_VAULT_SYNC_INTERVAL_MINUTES": "60",
                    "UA_VAULT_SYNC_REMOTE_HOST": "vault-host",
                    "UA_VAULT_SYNC_REMOTE_ROOT": "vault",
                }
                if unit == "ua-vault-sync.service"
                else {}
            ),
        )

        assert config.vault_sync_state_path() != timer_home / "vault-sync-state.json"
        check = doctor._vault_sync_check()
        assert check["status"] == "LIVE"
        assert "60min interval" in check["detail"]

    def test_interval_from_service_settings_file_sets_freshness(self, isolated_env, monkeypatch):
        # The sync service's settings.toml supplies the interval; doctor must
        # judge freshness against it, not the 10-minute code default.
        from datetime import UTC, datetime, timedelta

        from unified_agent import config

        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        forty_ago = (datetime.now(UTC) - timedelta(minutes=40)).isoformat()
        self._write_state(isolated_env, last_success_utc=forty_ago, last_run_utc=forty_ago)
        (config.data_home() / "settings.toml").write_text(
            "[env]\nUA_VAULT_SYNC_INTERVAL_MINUTES = 60\n", encoding="utf-8"
        )
        check = doctor._vault_sync_check()
        assert check["status"] == "LIVE", check["detail"]
        assert "60min interval" in check["detail"]

    def test_failed_latest_run_or_missing_timer_is_not_live(self, isolated_env, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        self._write_state(isolated_env, last_run_ok=False)
        assert doctor._vault_sync_check()["status"] == "MISCONFIGURED"
        self._write_state(isolated_env, last_run_ok=True)
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "not-found"},
        )
        assert doctor._vault_sync_check()["status"] == "MISCONFIGURED"

    def test_dry_run_does_not_certify_previous_success(self, isolated_env, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        self._write_state(isolated_env, dry_run=True)
        assert doctor._vault_sync_check()["status"] == "MISCONFIGURED"

    def test_misconfigured_degraded_when_stale(self, isolated_env, monkeypatch):
        from datetime import UTC, datetime, timedelta

        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        stale = (datetime.now(UTC) - timedelta(hours=6)).isoformat(timespec="seconds")
        self._write_state(isolated_env, last_success_utc=stale)
        check = doctor._vault_sync_check()
        assert check["status"] == "MISCONFIGURED"
        assert check["detail"].startswith("degraded:")

    def test_misconfigured_degraded_when_conflicts_outstanding(self, isolated_env, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        self._write_state(
            isolated_env,
            conflict_count=2,
            conflicts=[
                {
                    "relpath": "Wiki/a.md",
                    "reason": "local_newer",
                    "conflict_file": "a.conflict-x.md",
                    "detected_at": doctor.storage.now(),
                },
                {
                    "relpath": "Wiki/b.md",
                    "reason": "local_newer",
                    "conflict_file": "b.conflict-x.md",
                    "detected_at": doctor.storage.now(),
                },
            ],
        )
        check = doctor._vault_sync_check()
        assert check["status"] == "MISCONFIGURED"
        assert "2 unresolved conflict(s)" in check["detail"]

    def test_malformed_sync_counts_degrade_without_aborting_doctor(self, isolated_env, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        self._write_state(isolated_env, conflict_count="not-a-number")
        assert doctor._vault_sync_check()["status"] == "MISCONFIGURED"
        checks = _by_capability(system_doctor()["checks"])
        assert checks["vault_sync"]["status"] == "MISCONFIGURED"

    def test_misconfigured_when_state_present_but_never_succeeded(self, isolated_env, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_service_probe",
            lambda unit, timeout: {"LoadState": "loaded", "ActiveState": "inactive"},
        )
        self._write_state(isolated_env, last_success_utc=None, error="remote unreachable")
        check = doctor._vault_sync_check()
        assert check["status"] == "MISCONFIGURED"
        assert "no successful sync" in check["detail"]

    def test_check_is_wired_into_system_doctor(self, isolated_env, monkeypatch):
        monkeypatch.setattr(doctor, "_service_probe", lambda unit, timeout: None)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["vault_sync"]["status"] == "DORMANT"
        assert checks["vault_sync"]["gate"].startswith("ua-vault-sync.timer")


class TestDispatchUnsettledSpawnsCheck:
    """An unsettled spawn holding its task degrades the doctor past a grace (#2512)."""

    @staticmethod
    def _held(monkeypatch, past_lease_s: int) -> None:
        from unified_agent.dispatch import coordinator

        row = {"run_id": "run-7-abc", "task_id": 7, "past_lease_s": past_lease_s}
        monkeypatch.setattr(coordinator, "unsettled_spawns", lambda **_: [row])

    def test_live_within_the_grace_degraded_past_it_naming_the_run(self, isolated_env, monkeypatch):
        from unified_agent.dispatch import coordinator

        assert doctor._dispatch_unsettled_spawns_check()["status"] == "LIVE"
        self._held(monkeypatch, coordinator.UNSETTLED_SPAWN_GRACE_S - 1)
        within = doctor._dispatch_unsettled_spawns_check()
        assert (within["status"], within["detail"].endswith("1 within it")) == ("LIVE", True)
        self._held(monkeypatch, coordinator.UNSETTLED_SPAWN_GRACE_S)
        stuck = doctor._dispatch_unsettled_spawns_check()
        assert stuck["status"] == "DEGRADED"
        assert "run-7-abc (task #7)" in stuck["detail"] and "settle-spawn" in stuck["detail"]

    def test_check_is_wired_into_system_doctor(self, isolated_env, monkeypatch):
        monkeypatch.setattr(doctor, "_service_probe", lambda unit, timeout: None)
        checks = _by_capability(system_doctor()["checks"])
        assert checks["dispatch_unsettled_spawns"]["status"] == "LIVE"


class TestQueueBackpressureCheck:
    """Queue flow as an operator signal (task #1430)."""

    def test_empty_queue_is_live(self, isolated_env):
        check = doctor._queue_backpressure_check()
        assert check["capability"] == "queue_backpressure"
        assert check["status"] == "LIVE"
        assert check["detail"].startswith("queue flowing: open 0")

    def test_signals_degrade_and_are_named(self, isolated_env, monkeypatch):
        from unified_agent import queue_hygiene

        monkeypatch.setattr(
            queue_hygiene,
            "queue_pressure",
            lambda: {
                "signals": ["1 doing task(s) without a claimant (no lease protects them)"],
                "depth": {"open": 3, "doing": 1, "waiting_review": 0, "blocked": 0},
                "claims": {"oldest_claim_hours": None},
            },
        )
        check = doctor._queue_backpressure_check()
        assert check["status"] == "DEGRADED"
        assert "without a claimant" in check["detail"]

    def test_backlog_notes_stay_live_and_are_shown(self, isolated_env, monkeypatch):
        """An old unclaimed task is backlog, not a broken queue: LIVE, with the note."""
        from unified_agent import queue_hygiene

        monkeypatch.setattr(
            queue_hygiene,
            "queue_pressure",
            lambda: {
                "signals": [],
                "notes": ["dispatch-ready task #7 unclaimed for 96h"],
                "depth": {"open": 3, "doing": 0, "waiting_review": 0, "blocked": 0},
                "claims": {"oldest_claim_hours": None},
            },
        )
        check = doctor._queue_backpressure_check()
        assert check["status"] == "LIVE"
        assert "unclaimed for 96h" in check["detail"]

    def test_a_failing_read_is_unknown_not_raised(self, isolated_env, monkeypatch):
        from unified_agent import queue_hygiene

        def boom():
            raise RuntimeError("db gone")

        monkeypatch.setattr(queue_hygiene, "queue_pressure", boom)
        check = doctor._queue_backpressure_check()
        assert check["status"] == "UNKNOWN"
        assert "db gone" in check["detail"]

    def test_system_doctor_includes_the_row(self, isolated_env):
        names = {c["capability"] for c in system_doctor()["checks"]}
        assert "queue_backpressure" in names


class TestWorkspacePinCheck:
    """`workspace_pin` (#1806, #1825): service.env owns UA_WORKSPACE after init."""

    @pytest.fixture
    def install(self, tmp_path, monkeypatch):
        home = config.data_home()
        home.mkdir(parents=True, exist_ok=True)
        settings_file = home / "settings.toml"
        monkeypatch.setenv("UA_SETTINGS_FILE", str(settings_file))
        return home / "service.env", settings_file

    @staticmethod
    def _settings(path, workspace=None):
        body = "schema_version = 1\n"
        if workspace is not None:
            body += f'[paths]\nworkspace = "{workspace}"\n'
        path.write_text(body)
        path.chmod(0o600)

    def test_dormant_without_service_env(self, install):
        check = doctor._workspace_pin_check()
        assert check["capability"] == "workspace_pin"
        assert check["status"] == "DORMANT"

    def test_live_when_pinned_and_settings_silent(self, install, tmp_path):
        env_file, settings_file = install
        env_file.write_text(f"UA_WORKSPACE={tmp_path / 'ws'}\n")
        self._settings(settings_file)
        check = doctor._workspace_pin_check()
        assert check["status"] == "LIVE"
        assert str(tmp_path / "ws") in check["detail"]

    def test_live_when_settings_agrees(self, install, tmp_path, monkeypatch):
        env_file, settings_file = install
        monkeypatch.setenv("HOME", str(tmp_path))
        env_file.write_text(f"UA_WORKSPACE={tmp_path / 'ws'}\n")
        self._settings(settings_file, "~/ws")
        assert doctor._workspace_pin_check()["status"] == "LIVE"

    def test_live_with_settings_disabled(self, install, tmp_path, monkeypatch):
        env_file, _ = install
        monkeypatch.setenv("UA_SETTINGS_FILE", "")
        env_file.write_text(f"UA_WORKSPACE={tmp_path / 'ws'}\n")
        assert doctor._workspace_pin_check()["status"] == "LIVE"

    def test_degraded_when_settings_names_another_path(self, install, tmp_path):
        env_file, settings_file = install
        env_file.write_text(f"UA_WORKSPACE={tmp_path / 'ws'}\n")
        self._settings(settings_file, tmp_path / "elsewhere")
        check = doctor._workspace_pin_check()
        assert check["status"] == "DEGRADED"
        assert str(tmp_path / "ws") in check["detail"]
        assert str(tmp_path / "elsewhere") in check["detail"]
        assert "ignored" in check["detail"]

    @pytest.mark.parametrize(
        "body",
        [
            "# UA_WORKSPACE=/commented/out\nUA_TRANSPORT=stdio\n",
            "UA_TRANSPORT=stdio\n",
            "UA_WORKSPACE=\n",
        ],
    )
    def test_misconfigured_without_an_uncommented_pin(self, install, tmp_path, body):
        env_file, settings_file = install
        env_file.write_text(body)
        self._settings(settings_file, tmp_path / "ws")
        check = doctor._workspace_pin_check()
        assert check["status"] == "MISCONFIGURED"
        assert "unified-agent init" in check["detail"]

    def test_detail_never_carries_other_env_values(self, install, tmp_path):
        env_file, _ = install
        secret = "fixture-" + "bearer-value-0123456789"
        env_file.write_text(f"UA_AGENT_TOKENS=worker:{secret}\nUA_WORKSPACE={tmp_path / 'ws'}\n")
        for body in (env_file.read_text(), f"UA_AGENT_TOKENS=worker:{secret}\n"):
            env_file.write_text(body)
            check = doctor._workspace_pin_check()
            assert secret not in json.dumps(check)

    def test_system_doctor_reports_it(self, install):
        checks = _by_capability(system_doctor()["checks"])
        assert checks["workspace_pin"]["status"] == "DORMANT"

    @pytest.mark.parametrize(
        "expression", ["$UA_TEST_UNSET_WS_ROOT/ws", "${UA_TEST_UNSET_WS_ROOT}", "`pwd`/ws"]
    )
    def test_misconfigured_when_init_would_refuse_the_pin(
        self, install, tmp_path, monkeypatch, expression
    ):
        """Doctor and init share one resolver: a pin init rejects never reads LIVE."""
        from unified_agent import bootstrap, settings

        env_file, settings_file = install
        monkeypatch.delenv("UA_TEST_UNSET_WS_ROOT", raising=False)
        secret = "fixture-" + "bearer-value-9876543210"
        env_file.write_text(f"UA_AGENT_TOKENS=worker:{secret}\nUA_WORKSPACE={expression}\n")
        self._settings(settings_file)
        check = doctor._workspace_pin_check()
        assert check["status"] == "MISCONFIGURED"
        assert secret not in json.dumps(check)
        assert "UA_TEST_UNSET_WS_ROOT" not in check["detail"]
        with pytest.raises(settings.SettingsError):
            bootstrap._resolve_workspace_pin(expression)

    def test_live_when_the_pin_references_a_set_variable(self, install, tmp_path, monkeypatch):
        env_file, _ = install
        monkeypatch.setenv("UA_TEST_WS_ROOT", str(tmp_path))
        env_file.write_text("UA_WORKSPACE=$UA_TEST_WS_ROOT/ws\n")
        check = doctor._workspace_pin_check()
        assert check["status"] == "LIVE"
        assert str(tmp_path / "ws") in check["detail"]


class TestWipPolicyCapability:
    """The doctor reports the WIP numbers the hub will enforce (task #2759)."""

    _VARS = (
        "UA_AGENT_WIP_MAX",
        "UA_AGENT_WIP_OVERRIDES",
        "UA_FLEET_WIP_MAX",
        "UA_FLEET_WIP_HARD_MAX",
        "UA_LANE_WIP",
    )

    def test_live_with_the_documented_defaults(self, monkeypatch):
        for var in self._VARS:
            monkeypatch.delenv(var, raising=False)
        check = doctor._wip_policy_check()
        assert check["capability"] == "wip_policy"
        assert check["status"] == "LIVE"
        assert "per agent 20" in check["detail"]
        assert "fleet soft 40, hard 80" in check["detail"]

    def test_degraded_when_the_fleet_ceiling_is_not_above_the_per_agent_cap(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_WIP_MAX", "20")
        monkeypatch.setenv("UA_FLEET_WIP_MAX", "20")
        monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "20")
        check = doctor._wip_policy_check()
        assert check["status"] == "DEGRADED"
        assert "does not sit above" in check["detail"]
