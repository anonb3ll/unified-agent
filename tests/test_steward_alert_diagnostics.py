"""Offline wrapper diagnostics: only a stub notifier runs in a temporary home."""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from export_guard import require_exported_paths


@pytest.fixture
def audit_fixture(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    # The public export drops deploy/mac-hub/ (task #2615). Guard at the
    # fixture, because every test in this module drives the same wrappers.
    require_exported_paths(
        repo,
        repo / "deploy/mac-hub/run-steward-audit.sh",
        repo / "deploy/steward-audit-cron.sh",
    )
    root = tmp_path / "dev/unified_agent"
    (root / "deploy/mac-hub").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    for name in (
        "deploy/mac-hub/run-steward-audit.sh",
        "deploy/steward-audit-status.sh",
        "deploy/steward-audit-cron.sh",
    ):
        shutil.copy2(repo / name, root / name)
    audit = root / ".venv/bin/unified-agent-audit"
    audit.write_text("#!/bin/sh\necho fixture-high-finding\nexit 1\n")
    audit.chmod(0o755)
    (tmp_path / "vaults/main/00 - Inbox").mkdir(parents=True)
    notify = tmp_path / "dev/agent/projects/imessage-bridge/notify.sh"
    notify.parent.mkdir(parents=True)

    def run(error, exit_code=1, *, noise_bytes=0, direct=False, scheduled=False, run_id=""):
        noise = ""
        if noise_bytes:
            producer = (
                "import sys; chunk = b'x' * 65536; "
                f"[sys.stderr.buffer.write(chunk) for _ in range({noise_bytes // 65536})]; "
                "sys.stderr.buffer.flush()"
            )
            noise = shlex.quote(sys.executable) + " -c " + shlex.quote(producer) + " || exit $?\n"
        command = ["bash", str(root / "deploy/mac-hub/run-steward-audit.sh")]
        if direct:
            command = ["bash", str(root / "deploy/steward-audit-cron.sh")]
            if scheduled:
                command.append("--scheduled")
        # Synthetic sensitive text exists only in this test stub. The real
        # notifier, credentials and provider are never loaded or contacted.
        notify.write_text(
            "#!/bin/sh\ncat >/dev/null\n"
            + "printf '%s' 'stdout-private-sentinel recipient=+15551234567'\n"
            + "printf '%s' '"
            + error.replace("'", "'\\''")
            + "' >&2\n"
            + noise
            + 'printf done >"$HOME/notifier-drained"\n'
            + f"exit {exit_code}\n"
        )
        notify.chmod(0o755)
        return subprocess.run(
            command,
            env={**os.environ, "HOME": str(tmp_path), "UA_STEWARD_AUDIT_RUN_ID": run_id},
            capture_output=True,
            text=True,
            timeout=10,
        )

    return tmp_path, run


@pytest.mark.parametrize(
    ("error", "exit_code", "kind"),
    [
        ("RateLimitError: extended cooling", 1, "rate_limited"),
        ("send timeout", 124, "timeout"),
        ("missing SPECTRUM_PROJECT_ID / SPECTRUM_PROJECT_SECRET", 2, "configuration_error"),
        ("AuthenticationError", 1, "authentication_error"),
        ("ECONNREFUSED", 1, "transport_error"),
        ("unrecognized sender problem", 1, "sender_failed"),
    ],
)
def test_failed_sender_retains_only_safe_correlated_metadata(audit_fixture, error, exit_code, kind):
    home, run = audit_fixture
    sensitive = " token=private-key-sentinel recipient=+15551234567 message=private-body-sentinel"
    result = run(error + sensitive, exit_code)
    assert result.returncode == 2
    state = home / ".unified_agent"
    pointer = dict(
        line.split("=", 1) for line in (state / "steward-audit-status").read_text().splitlines()
    )
    status = (state / pointer["run_id"] / "status").read_text()
    assert "state=delivery_fail" in status and "exit_code=2" in status
    log = (state / "audit.log").read_text()
    diagnostic = next(
        line for line in log.splitlines() if "ERROR audit findings alert failed" in line
    )
    assert f"run_id={pointer['run_id']}" in diagnostic
    assert f"notify_exit={exit_code}" in diagnostic
    assert f"error_kind={kind}" in diagnostic
    assert re.search(r"started_at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", diagnostic)
    assert re.search(r"finished_at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", diagnostic)
    persisted = log + status + result.stdout + result.stderr
    for value in (
        "private-key-sentinel",
        "+15551234567",
        "private-body-sentinel",
        "stdout-private-sentinel",
        error,
    ):
        assert value not in persisted
    assert not (state / "steward-audit-last-alert.date").exists()
    assert (state / "steward-audit-last-report.date").exists()


def test_large_unknown_error_does_not_become_a_raw_error_log(audit_fixture):
    home, run = audit_fixture
    assert run("private-error-sentinel" * 10000).returncode == 2
    log = (home / ".unified_agent/audit.log").read_text()
    assert "error_kind=sender_failed" in log
    assert "private-error-sentinel" not in log
    assert len(log) < 1024


def test_success_preserves_the_alert_receipt_and_deduplication(audit_fixture):
    home, run = audit_fixture
    assert run("private-success-sentinel", exit_code=0).returncode == 1
    state = home / ".unified_agent"
    assert (state / "steward-audit-last-alert.date").exists()
    assert "ERROR audit findings alert failed" not in (state / "audit.log").read_text()
    assert run("RateLimitError private-unexpected-retry", exit_code=1).returncode == 1
    assert "ERROR audit findings alert failed" not in (state / "audit.log").read_text()


def test_capture_is_bounded_and_excess_stderr_is_drained(audit_fixture):
    home, run = audit_fixture
    # Instrument only the offline copied wrapper: persist the size, never the
    # captured text. This checks capture itself, not just final log length.
    wrapper = home / "dev/unified_agent/deploy/steward-audit-cron.sh"
    source = wrapper.read_text()
    boundary = "      alert_finished_at=$(date -u +%FT%TZ)"
    assert source.count(boundary) == 1
    wrapper.write_text(
        source.replace(
            boundary,
            '      printf "%s" "${#alert_error}" >"$STATE_DIR/test-captured-size"\n' + boundary,
        )
    )
    result = run("RateLimitError: noisy sender", 23, noise_bytes=16 * 1024 * 1024)
    assert result.returncode == 2
    assert (home / "notifier-drained").read_text() == "done"
    assert int((home / ".unified_agent/test-captured-size").read_text()) <= 4096
    log = (home / ".unified_agent/audit.log").read_text()
    assert "notify_exit=23 error_kind=rate_limited" in log
    assert "RateLimitError" not in log


@pytest.mark.parametrize(
    ("padding", "kind"),
    [
        (4096 - len("RateLimitError"), "rate_limited"),
        (4096 - len("RateLimitError") + 1, "sender_failed"),
        (4096, "sender_failed"),
    ],
)
def test_classification_uses_only_the_captured_prefix(audit_fixture, padding, kind):
    home, run = audit_fixture
    assert run(" " * padding + "RateLimitError", 17).returncode == 2
    log = (home / ".unified_agent/audit.log").read_text()
    assert f"notify_exit=17 error_kind={kind}" in log
    assert "RateLimitError" not in log


@pytest.mark.parametrize(
    ("scheduled", "run_id"),
    [
        (False, "steward-audit-run.ABC123"),
        (True, ""),
        (True, "steward-audit-run.invalid!"),
        (True, "../private-id-sentinel"),
        (True, "steward-audit-run.ABC123\nprivate-id-sentinel"),
    ],
)
def test_manual_or_invalid_run_id_is_not_persisted(audit_fixture, scheduled, run_id):
    home, run = audit_fixture
    assert run("send timeout", 124, direct=True, scheduled=scheduled, run_id=run_id).returncode == 2
    log = (home / ".unified_agent/audit.log").read_text()
    diagnostic = next(
        line for line in log.splitlines() if "ERROR audit findings alert failed" in line
    )
    assert "run_id=manual notify_exit=124 error_kind=timeout" in diagnostic
    assert "private-id-sentinel" not in log
    assert "steward-audit-run." not in diagnostic


@pytest.mark.parametrize("error", ["", "unrelated sender details", "RateLimitError: echoed text"])
def test_exit_124_classifies_timeout_without_stderr_evidence(audit_fixture, error):
    home, run = audit_fixture
    assert run(error, 124).returncode == 2
    log = (home / ".unified_agent/audit.log").read_text()
    assert "notify_exit=124 error_kind=timeout" in log


@pytest.mark.parametrize(
    "error",
    [
        "echoed message: RateLimitError: test text",
        "send: failed — Error: message contains send timeout",
        "send: failed — Error: unrecognized failure\nRateLimitError: echoed message",
        "echoed message: missing SPECTRUM_PROJECT_ID / SPECTRUM_PROJECT_SECRET",
        "echoed message: AuthenticationError ECONNREFUSED",
    ],
)
def test_echoed_message_phrases_do_not_override_error_kind(audit_fixture, error):
    home, run = audit_fixture
    assert run(error).returncode == 2
    log = (home / ".unified_agent/audit.log").read_text()
    assert "error_kind=sender_failed" in log
    assert "echoed message" not in log


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ("send: failed — RateLimitError: extended cooling", "rate_limited"),
        ("send: failed — Error: send timeout", "timeout"),
        ("notify.sh: bun not found (set BUN_BIN)", "configuration_error"),
    ],
)
def test_known_sender_error_prefixes_are_classified(audit_fixture, error, kind):
    home, run = audit_fixture
    assert run(error).returncode == 2
    assert f"error_kind={kind}" in (home / ".unified_agent/audit.log").read_text()
