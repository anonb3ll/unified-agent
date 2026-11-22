"""Scheduled signed evidence export tests (task #1266).

``LocalPathDestination`` is exercised end-to-end (export -> sign -> write ->
``evidence_signing.verify_export`` on the written bytes), plus idempotency
via the commit marker, prefix-scoped retention (including uncommitted orphan
pairs), the complete/uncapped read, concurrent-pair safety, local file/dir
permissions, and destination/signing readiness reporting.
``GCSDestination``/``S3Destination`` are exercised only against small fakes
standing in for their SDKs — no network, and neither SDK is a project
dependency (see the module docstring).
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from export_guard import needs_exported_paths

from unified_agent import config, storage
from unified_agent.audit import evidence_export_sink as sink
from unified_agent.audit import evidence_signing

_REPO_ROOT = Path(__file__).resolve().parents[1]
# The public export drops deploy/mac-hub/ (task #2615): the launchd wrapper
# body skips there and still runs, and fails on a rename, in the private repo.
needs_mac_hub_assets = needs_exported_paths(
    _REPO_ROOT / "deploy" / "mac-hub" / "com.unified-agent.evidence-export.plist.example",
    _REPO_ROOT / "deploy" / "mac-hub" / "run-evidence-export.sh",
    repo=_REPO_ROOT,
)


def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()


def _insert_done_tasks(n: int, *, completed_at: str = "2026-09-23T10:00:00+00:00") -> None:
    conn = storage.connect()
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO tasks (title, status, priority, created_at, updated_at, "
                "completed_at, source_host, claimed_by, evidence_ref, notes, project, domain, lane) "
                "VALUES (?, 'done', 'med', ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
                (
                    f"bulk task {i}",
                    completed_at,
                    completed_at,
                    completed_at,
                    "test-host",
                    "Desktop_Agent2",
                    f"commit:test{i}",
                    "unified-agent",
                    "professional",
                    "default",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _local_env(tmp_path) -> dict[str, str]:
    return {
        "UA_EVIDENCE_EXPORT_ENABLED": "1",
        "UA_EVIDENCE_EXPORT_DESTINATION": "local",
        "UA_EVIDENCE_EXPORT_LOCAL_PATH": str(tmp_path / "export-root"),
        "UA_EVIDENCE_SIGNING_KEY": "test-signing-key-not-real",
        "UA_EVIDENCE_SIGNING_KEY_ID": "test-key-1",
    }


def _apply_signing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key-not-real")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1")


# ---------------------------------------------------------------------------
# Object naming
# ---------------------------------------------------------------------------


def test_object_key_is_content_addressed_and_deterministic():
    key = sink.object_key("acme", "2026-09-23", "abc123", "deadbeefcafef00d", ".json")
    assert key == "evidence/acme/2026-09-23/abc123-deadbeefcafef00d.json"
    assert sink.object_key("acme", "2026-09-23", "abc123", "deadbeefcafef00d", ".sig.json") == (
        "evidence/acme/2026-09-23/abc123-deadbeefcafef00d.sig.json"
    )
    # Same inputs -> same key, every time.
    assert sink.object_key("acme", "2026-09-23", "abc123", "deadbeefcafef00d", ".json") == key


def test_object_key_rejects_path_traversal_in_org_id():
    with pytest.raises(sink.DestinationConfigError):
        sink.object_key("../escape", "2026-09-23", "abc123", "deadbeef", ".json")


def test_marker_key_is_not_content_addressed():
    assert (
        sink.marker_key("acme", "2026-09-23", "abc123")
        == "evidence/acme/2026-09-23/abc123.committed"
    )


def test_different_bodies_never_collide_at_the_same_object_key():
    key_a = sink.object_key("acme", "2026-09-23", "abc123", "aaaaaaaaaaaaaaaa", ".json")
    key_b = sink.object_key("acme", "2026-09-23", "abc123", "bbbbbbbbbbbbbbbb", ".json")
    assert key_a != key_b


# ---------------------------------------------------------------------------
# LocalPathDestination: end-to-end export -> sign -> write -> verify
# ---------------------------------------------------------------------------


def test_local_destination_export_sign_write_verify_roundtrip(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    for name, value in _local_env(tmp_path).items():
        monkeypatch.setenv(name, value)

    result = sink.run_once()

    assert result["status"] == "exported"
    assert result["complete"] is True
    destination = sink.LocalPathDestination(tmp_path / "export-root")
    body = destination.get(result["key"])
    sidecar_bytes = destination.get(result["sig_key"])
    marker_bytes = destination.get(result["marker_key"])
    assert body is not None and sidecar_bytes is not None and marker_bytes is not None

    sidecar = json.loads(sidecar_bytes)
    valid, reason = evidence_signing.verify_export(body, sidecar)
    assert (valid, reason) == (True, "valid")

    report = json.loads(body)
    assert report["complete"] is True
    assert report["schema_version"] == "1.8"
    assert report["signature"]["key_id"] == "test-key-1"

    marker = json.loads(marker_bytes)
    assert marker["key"] == result["key"]
    assert marker["sig_key"] == result["sig_key"]


def test_run_once_uses_injected_signing_environment(monkeypatch, tmp_path):
    """Finding 6: an injected settings map must not sign with unrelated process keys."""
    _isolated_db(monkeypatch, tmp_path)
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "process-only-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "process-key")
    env = _local_env(tmp_path)
    env["UA_EVIDENCE_SIGNING_KEY"] = "injected-key"
    env["UA_EVIDENCE_SIGNING_KEY_ID"] = "injected-key-id"

    result = sink.run_once(env=env)

    assert result["status"] == "exported"
    destination = sink.LocalPathDestination(tmp_path / "export-root")
    body = destination.get(result["key"])
    sidecar = json.loads(destination.get(result["sig_key"]))
    report = json.loads(body)
    assert sidecar["key_id"] == "injected-key-id"
    assert report["signature"]["key_id"] == "injected-key-id"
    assert report["attestation"]["key_id"] == "injected-key-id"
    assert evidence_signing.verify_export(body, sidecar) == (False, "signing key id mismatch")

    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "injected-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "injected-key-id")
    assert evidence_signing.verify_export(body, sidecar) == (True, "valid")


def test_complete_export_does_not_truncate_past_the_default_clamp(monkeypatch, tmp_path):
    """Truncation regression (task #1266 review finding 1): more than 50
    done tasks in the window must all appear in the signed export."""
    _isolated_db(monkeypatch, tmp_path)
    for name, value in _local_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    _insert_done_tasks(75)

    result = sink.run_once(now=datetime(2026, 9, 24, 2, 0, tzinfo=UTC))
    assert result["status"] == "exported"
    assert result["records"] >= 75

    destination = sink.LocalPathDestination(tmp_path / "export-root")
    report = json.loads(destination.get(result["key"]))
    assert report["complete"] is True
    assert report["total_records"] >= 75
    assert len(report["records"]) == report["total_records"]


def test_local_destination_put_creates_private_dirs_and_files(tmp_path):
    """Local permissions (task #1266 review finding 7): 0700 dirs, 0600 files."""
    destination = sink.LocalPathDestination(tmp_path / "root")
    destination.put("evidence/org/2026-09-23/head-abcd.json", b"hello")

    file_path = tmp_path / "root" / "evidence" / "org" / "2026-09-23" / "head-abcd.json"
    assert stat.S_IMODE(file_path.stat().st_mode) == 0o600
    for dir_path in (
        tmp_path / "root" / "evidence" / "org" / "2026-09-23",
        tmp_path / "root" / "evidence" / "org",
        tmp_path / "root" / "evidence",
    ):
        assert stat.S_IMODE(dir_path.stat().st_mode) == 0o700
    # No stray .tmp-* file left behind after an atomic replace.
    assert destination.list_prefix("evidence/org/2026-09-23") == [
        "evidence/org/2026-09-23/head-abcd.json"
    ]


def test_local_destination_fails_closed_when_private_directory_mode_cannot_be_set(
    monkeypatch, tmp_path
):
    destination = sink.LocalPathDestination(tmp_path / "root")

    def deny_chmod(path, mode):
        raise PermissionError("synthetic chmod denial")

    monkeypatch.setattr(sink.os, "chmod", deny_chmod)
    with pytest.raises(PermissionError, match="synthetic chmod denial"):
        destination.put("evidence/org/2026-09-23/head-abcd.json", b"private")

    assert not (tmp_path / "root/evidence/org/2026-09-23/head-abcd.json").exists()


def test_local_destination_refuses_key_escaping_root(tmp_path):
    destination = sink.LocalPathDestination(tmp_path)
    with pytest.raises(sink.DestinationError):
        destination.put("../escape.json", b"x")


def test_local_destination_rejects_symlinked_directories(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "evidence").mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o777)
    (root / "evidence/org").symlink_to(outside, target_is_directory=True)

    destination = sink.LocalPathDestination(root)
    with pytest.raises(sink.DestinationError, match="symlink"):
        destination.put("evidence/org/2026-09-23/head-abcd.json", b"private")

    assert list(outside.iterdir()) == []


def test_local_destination_rejects_preexisting_permissive_directory(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o755)
    destination = sink.LocalPathDestination(root)

    with pytest.raises(sink.DestinationError, match="not private"):
        destination.put("evidence/org/2026-09-23/head-abcd.json", b"private")


def test_local_destination_allows_public_mode_ancestors_outside_export_root(tmp_path):
    parent = tmp_path / "shared-parent"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    destination = sink.LocalPathDestination(parent / "new-export-root")

    destination.put("evidence/org/2026-09-23/head-abcd.json", b"private")

    assert stat.S_IMODE((parent / "new-export-root").stat().st_mode) == 0o700
    assert destination.exists("evidence/org/2026-09-23/head-abcd.json")


@needs_mac_hub_assets
def test_launchd_wrapper_sources_service_env_and_plist_targets_wrapper(tmp_path):
    """The launchd plist must execute the wrapper that loads service.env."""
    repo = Path(__file__).resolve().parents[1]
    plist_path = repo / "deploy/mac-hub/com.unified-agent.evidence-export.plist.example"
    with plist_path.open("rb") as stream:
        plist = plistlib.load(stream)
    assert plist["ProgramArguments"][0].endswith("run-evidence-export.sh")

    fake_home = tmp_path / "home"
    env_dir = fake_home / ".unified_agent"
    env_dir.mkdir(parents=True)
    (env_dir / "service.env").write_text(
        "UA_EVIDENCE_EXPORT_ENABLED=1\nUA_EVIDENCE_SIGNING_KEY_ID=wrapper-test\n",
        encoding="utf-8",
    )
    executable = fake_home / "dev/unified_agent/.venv/bin/unified-agent-evidence-export"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env bash\nprintf '%s|%s' \"$UA_EVIDENCE_EXPORT_ENABLED\" "
        '"$UA_EVIDENCE_SIGNING_KEY_ID"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)

    wrapper = repo / "deploy/mac-hub/run-evidence-export.sh"
    env = dict(os.environ, HOME=str(fake_home))
    env.pop("UA_EVIDENCE_EXPORT_ENABLED", None)
    env.pop("UA_EVIDENCE_SIGNING_KEY_ID", None)
    result = subprocess.run(
        ["bash", str(wrapper)], check=True, capture_output=True, text=True, env=env
    )
    assert result.stdout == "1|wrapper-test"


# ---------------------------------------------------------------------------
# Idempotency via the commit marker: a rerun with no new events writes
# nothing new, and skip still applies retention (findings 2 and 3).
# ---------------------------------------------------------------------------


def test_rerun_with_no_new_events_is_skipped_via_marker_and_writes_nothing(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    for name, value in _local_env(tmp_path).items():
        monkeypatch.setenv(name, value)

    first = sink.run_once()
    assert first["status"] == "exported"

    destination = sink.LocalPathDestination(tmp_path / "export-root")
    before_body = destination.get(first["key"])
    before_sidecar = destination.get(first["sig_key"])
    before_marker = destination.get(first["marker_key"])

    second = sink.run_once()
    assert second["status"] == "skipped"
    assert second["chain_head"] == first["chain_head"]
    assert second["marker_key"] == first["marker_key"]
    assert "retention" in second

    # Bytes at every key are untouched — never rewritten (a second build
    # would carry a different generated_at/attestation timestamp).
    assert destination.get(first["key"]) == before_body
    assert destination.get(first["sig_key"]) == before_sidecar
    assert destination.get(first["marker_key"]) == before_marker


def test_same_day_retry_ignores_advancing_report_clock(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    env = _local_env(tmp_path)
    first_clock = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    report_clock = first_clock
    monkeypatch.setattr(
        storage,
        "now",
        lambda: report_clock.isoformat(timespec="seconds"),
    )

    first = sink.run_once(env=env, now=first_clock)
    assert first["status"] == "exported"
    destination = sink.LocalPathDestination(env["UA_EVIDENCE_EXPORT_LOCAL_PATH"])
    first_body = destination.get(first["key"])
    report = json.loads(first_body)
    assert report["attribution_coverage"]["window_end"] == ("2026-09-22T23:59:59.999999+00:00")

    report_clock += timedelta(hours=1)
    second = sink.run_once(env=env, now=first_clock + timedelta(hours=1))

    assert second["status"] == "skipped"
    assert second["committed"]["key"] == first["key"]
    assert destination.get(first["key"]) == first_body


def test_skip_path_still_applies_retention(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    env = _local_env(tmp_path)
    env["UA_EVIDENCE_EXPORT_RETENTION_DAYS"] = "1"
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    first = sink.run_once(now=now)
    assert first["status"] == "exported"

    destination = sink.LocalPathDestination(tmp_path / "export-root")
    # Plant an old, unrelated orphan pair that a retention-aware skip should reap.
    destination.put("evidence/default/2020-01-01/deadbeef-cafef00d.json", b"old")
    destination.put("evidence/default/2020-01-01/deadbeef-cafef00d.sig.json", b"old-sig")

    second = sink.run_once(now=now)
    assert second["status"] == "skipped"
    assert second["retention"]["applied"] is True
    assert "evidence/default/2020-01-01/deadbeef-cafef00d.json" in second["retention"]["deleted"]
    assert not destination.exists("evidence/default/2020-01-01/deadbeef-cafef00d.json")


def test_retention_failure_reports_successful_publication_as_partial(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    env = _local_env(tmp_path)
    env["UA_EVIDENCE_EXPORT_RETENTION_DAYS"] = "1"
    destination = sink.LocalPathDestination(env["UA_EVIDENCE_EXPORT_LOCAL_PATH"])
    monkeypatch.setattr(
        destination, "list_prefix", lambda _prefix: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(sink, "destination_from_env", lambda _source: destination)

    result = sink.run_once(env=env, now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC))

    assert result["status"] == "exported_with_retention_error"
    assert result["retention"]["error"] == "retention failed (OSError)"
    assert destination.exists(result["key"])
    assert destination.exists(result["sig_key"])
    assert destination.exists(result["marker_key"])


def test_long_evidence_window_does_not_make_new_export_expired(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    report_windows = []
    build_report = sink.evidence_report.build_evidence_report

    def capture_window(**kwargs):
        report_windows.append(kwargs["since"])
        return build_report(**kwargs)

    monkeypatch.setattr(sink.evidence_report, "build_evidence_report", capture_window)
    env = _local_env(tmp_path)
    env["UA_EVIDENCE_EXPORT_WINDOW_DAYS"] = "365"
    env["UA_EVIDENCE_EXPORT_RETENTION_DAYS"] = "90"
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    result = sink.run_once(env=env, now=now)

    assert result["status"] == "exported"
    assert "/2026-09-23/" in result["key"]
    destination = sink.LocalPathDestination(env["UA_EVIDENCE_EXPORT_LOCAL_PATH"])
    assert destination.exists(result["key"])
    assert destination.exists(result["sig_key"])
    assert destination.exists(result["marker_key"])
    assert report_windows == ["2025-09-23T00:00:00+00:00"]


@pytest.mark.parametrize(("window_days", "expected_records"), [(0, 0), (1, 1), (2, 2)])
def test_window_days_counts_inclusive_utc_dates(
    monkeypatch, tmp_path, window_days, expected_records
):
    _isolated_db(monkeypatch, tmp_path)
    env = _local_env(tmp_path)
    env["UA_EVIDENCE_EXPORT_WINDOW_DAYS"] = str(window_days)
    _insert_done_tasks(1, completed_at="2026-09-21T23:59:59+00:00")
    _insert_done_tasks(1, completed_at="2026-09-22T00:00:00+00:00")

    result = sink.run_once(env=env, now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC))

    report = json.loads(
        sink.LocalPathDestination(env["UA_EVIDENCE_EXPORT_LOCAL_PATH"]).get(result["key"])
    )
    assert report["total_records"] == expected_records
    assert report["attribution_coverage"]["denominator"] == expected_records


def test_daily_run_exports_the_last_completed_utc_date(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    env = _local_env(tmp_path)
    _insert_done_tasks(1, completed_at="2026-09-23T15:00:00+00:00")

    result = sink.run_once(env=env, now=datetime(2026, 9, 24, 2, 0, tzinfo=UTC))

    report = json.loads(
        sink.LocalPathDestination(env["UA_EVIDENCE_EXPORT_LOCAL_PATH"]).get(result["key"])
    )
    assert "/2026-09-24/" in result["key"]  # Publication date.
    assert report["total_records"] == 1
    assert report["attribution_coverage"]["window_start"] == "2026-09-23T00:00:00+00:00"
    assert report["attribution_coverage"]["window_end"] == ("2026-09-23T23:59:59.999999+00:00")


def test_window_override_recovers_completed_dates_after_missed_runs(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    env = _local_env(tmp_path)
    _insert_done_tasks(1, completed_at="2026-09-23T15:00:00+00:00")
    destination = sink.LocalPathDestination(env["UA_EVIDENCE_EXPORT_LOCAL_PATH"])

    prior_run = sink.run_once(env=env, now=datetime(2026, 9, 24, 2, 0, tzinfo=UTC))
    prior_body = destination.get(prior_run["key"])
    assert prior_run["records"] == 1

    # The Sep 25 and Sep 26 timer firings were missed. A four-date override
    # on Sep 27 republishes the full Sep 23-26 interval, including late rows.
    _insert_done_tasks(1, completed_at="2026-09-24T15:00:00+00:00")
    _insert_done_tasks(1, completed_at="2026-09-25T15:00:00+00:00")
    _insert_done_tasks(1, completed_at="2026-09-26T15:00:00+00:00")
    env["UA_EVIDENCE_EXPORT_WINDOW_DAYS"] = "4"
    recovered = sink.run_once(env=env, now=datetime(2026, 9, 27, 2, 0, tzinfo=UTC))

    recovered_report = json.loads(destination.get(recovered["key"]))
    prior_marker = json.loads(destination.get(prior_run["marker_key"]))
    recovered_marker = json.loads(destination.get(recovered["marker_key"]))
    assert recovered_report["total_records"] == 4
    assert recovered_report["attribution_coverage"]["window_start"] == ("2026-09-23T00:00:00+00:00")
    assert recovered_report["attribution_coverage"]["window_end"] == (
        "2026-09-26T23:59:59.999999+00:00"
    )
    assert prior_marker["key"] == prior_run["key"]
    assert recovered_marker["key"] == recovered["key"]
    assert destination.get(prior_run["key"]) == prior_body
    assert sink._verify_export_with_key(
        destination.get(recovered["key"]),
        json.loads(destination.get(recovered["sig_key"])),
        evidence_signing._key_material(env),
    )


@pytest.mark.parametrize("change", ["window", "report_input", "signing_key_id"])
def test_same_day_changed_export_inputs_replace_snapshot(monkeypatch, tmp_path, change):
    _isolated_db(monkeypatch, tmp_path)
    env = _local_env(tmp_path)
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    first = sink.run_once(env=env, now=now)
    assert first["status"] == "exported"

    if change == "window":
        env["UA_EVIDENCE_EXPORT_WINDOW_DAYS"] = "30"
    elif change == "report_input":
        _insert_done_tasks(1, completed_at="2026-09-22T10:00:00+00:00")
    else:
        env["UA_EVIDENCE_SIGNING_KEY_ID"] = "rotated-test-key"

    updated = sink.run_once(env=env, now=now)
    assert updated["status"] == "exported"
    assert updated["key"] != first["key"]
    assert updated["marker_key"] == first["marker_key"]


def test_read_transaction_keeps_chain_head_and_report_rows_on_one_snapshot(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    env = _local_env(tmp_path)
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    original_chain_head = sink._chain_head_only
    writer_ran = False

    def add_row_after_snapshot(conn):
        nonlocal writer_ran
        head = original_chain_head(conn)
        if not writer_ran:
            writer_ran = True
            _insert_done_tasks(1)
        return head

    monkeypatch.setattr(sink, "_chain_head_only", add_row_after_snapshot)
    result = sink.run_once(env=env, now=now)

    report = json.loads(
        sink.LocalPathDestination(env["UA_EVIDENCE_EXPORT_LOCAL_PATH"]).get(result["key"])
    )
    assert str(report["chain_integrity"]["head_hash"] or "") == result["chain_head"]
    assert report["total_records"] == 0
    assert writer_ran is True


@pytest.mark.parametrize(
    "damage", ["body", "sidecar", "marker", "tampered_body", "tampered_sidecar"]
)
def test_incomplete_or_malformed_marker_is_repaired(monkeypatch, tmp_path, damage):
    _isolated_db(monkeypatch, tmp_path)
    env = _local_env(tmp_path)
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    first = sink.run_once(env=env, now=now)
    assert first["status"] == "exported"
    destination = sink.LocalPathDestination(env["UA_EVIDENCE_EXPORT_LOCAL_PATH"])
    if damage == "body":
        destination.delete(first["key"])
    elif damage == "sidecar":
        destination.delete(first["sig_key"])
    elif damage == "tampered_body":
        destination.put(first["key"], b"tampered body")
    elif damage == "tampered_sidecar":
        destination.put(first["sig_key"], b"not a signature")
    else:
        destination.put(first["marker_key"], b"{not-json")

    repaired = sink.run_once(env=env, now=now)

    assert repaired["status"] == "exported"
    assert destination.exists(repaired["key"])
    assert destination.exists(repaired["sig_key"])
    marker = json.loads(destination.get(repaired["marker_key"]))
    assert marker["key"] == repaired["key"]
    assert marker["sig_key"] == repaired["sig_key"]
    assert marker["snapshot_fingerprint"]
    body = destination.get(repaired["key"])
    sidecar = json.loads(destination.get(repaired["sig_key"]))
    assert sink._verify_export_with_key(body, sidecar, evidence_signing._key_material(env))


@pytest.mark.parametrize(
    "setting", ["UA_EVIDENCE_EXPORT_WINDOW_DAYS", "UA_EVIDENCE_EXPORT_RETENTION_DAYS"]
)
def test_malformed_schedule_setting_is_rejected_without_echoing_value(tmp_path, setting):
    env = _local_env(tmp_path)
    env[setting] = "not-an-int"

    with pytest.raises(sink.DestinationConfigError, match=setting) as exc_info:
        sink.run_once(env=env)

    assert "not-an-int" not in str(exc_info.value)


def test_negative_window_setting_is_rejected(tmp_path):
    env = _local_env(tmp_path)
    env["UA_EVIDENCE_EXPORT_WINDOW_DAYS"] = "-1"

    with pytest.raises(sink.DestinationConfigError, match="WINDOW_DAYS"):
        sink.run_once(env=env)


# ---------------------------------------------------------------------------
# Concurrency: interleaved puts from two independently-built pairs must
# never cross, and both must verify (task #1266 review finding 2).
# ---------------------------------------------------------------------------


def test_interleaved_concurrent_runs_produce_two_self_consistent_pairs(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    _apply_signing_env(monkeypatch)
    destination = sink.LocalPathDestination(tmp_path / "export-root")
    conn = storage.connect()
    try:
        window_start = datetime(2026, 9, 22, tzinfo=UTC)
        date_segment = "2026-09-23"
        report_times = iter(("2026-09-23T12:00:00+00:00", "2026-09-23T12:00:01+00:00"))
        build_report = sink.evidence_report.build_evidence_report

        def build_with_distinct_timestamps(**kwargs):
            report = build_report(**kwargs)
            generated_at = next(report_times)
            report["generated_at"] = generated_at
            if isinstance(report.get("attestation"), dict):
                report["attestation"]["attested_at"] = generated_at
            return report

        monkeypatch.setattr(
            sink.evidence_report, "build_evidence_report", build_with_distinct_timestamps
        )

        # Two independent builds simulate two concurrent run_once() callers.
        # Each build's generated_at/attestation timestamp differs, so their
        # bodies (and therefore their content hashes/keys) differ even
        # though nothing about the underlying ledger changed between them.
        pair_a = sink._build_export_pair(
            conn, org_id="acme", date_segment=date_segment, window_start=window_start
        )
        pair_b = sink._build_export_pair(
            conn, org_id="acme", date_segment=date_segment, window_start=window_start
        )
        assert pair_a["json_key"] != pair_b["json_key"], "concurrent builds must not collide"

        # Deliberately interleave the puts out of pair order, bypassing
        # run_lock entirely, to prove the CONTENT-ADDRESSED KEYS (not the
        # lock) are what keeps the pairs from crossing.
        destination.put(pair_a["json_key"], pair_a["body"])
        destination.put(pair_b["json_key"], pair_b["body"])
        destination.put(pair_b["sig_key"], pair_b["sidecar_body"])
        destination.put(pair_a["sig_key"], pair_a["sidecar_body"])

        marker_a = sink.marker_key("acme", date_segment, pair_a["chain_head"])
        marker_b = sink.marker_key("acme", date_segment, pair_b["chain_head"])
        # Same chain head (nothing changed the ledger between builds) -> same
        # marker key; B's write "wins" because it lands last.
        assert marker_a == marker_b
        destination.put(
            marker_a,
            json.dumps({"key": pair_a["json_key"], "sig_key": pair_a["sig_key"]}).encode(),
        )
        destination.put(
            marker_b,
            json.dumps({"key": pair_b["json_key"], "sig_key": pair_b["sig_key"]}).encode(),
        )

        # Every pair ever written still verifies on its own — nothing is a
        # mix of one run's body and the other's signature.
        for pair in (pair_a, pair_b):
            body = destination.get(pair["json_key"])
            sidecar = json.loads(destination.get(pair["sig_key"]))
            assert evidence_signing.verify_export(body, sidecar) == (True, "valid")
            # Cross-checking against the OTHER pair's sidecar must fail.
        other_sidecar = json.loads(destination.get(pair_a["sig_key"]))
        crossed_body = destination.get(pair_b["json_key"])
        assert evidence_signing.verify_export(crossed_body, other_sidecar)[0] is False

        # The marker that landed last is the one currently readable.
        committed = json.loads(destination.get(marker_b))
        assert committed["key"] == pair_b["json_key"]
    finally:
        conn.close()


def test_run_lock_is_a_real_fcntl_lock_reentrant_across_sequential_calls(tmp_path):
    destination = sink.LocalPathDestination(tmp_path)
    with destination.run_lock():
        pass
    # A second acquisition after release must not deadlock.
    with destination.run_lock():
        pass
    lock_path = tmp_path / ".evidence-export.lock"
    assert lock_path.is_file()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Retention: only ever deletes old objects under its own prefix, treating a
# marker + its pair as one unit, and reaping uncommitted orphan pairs too.
# ---------------------------------------------------------------------------


def test_retention_deletes_old_marker_and_pair_together_and_orphans(tmp_path):
    destination = sink.LocalPathDestination(tmp_path)
    # Old, committed pair + marker: all three should be deleted together.
    destination.put("evidence/acme/2020-01-01/deadbeef-cafe0001.json", b"old")
    destination.put("evidence/acme/2020-01-01/deadbeef-cafe0001.sig.json", b"old-sig")
    destination.put("evidence/acme/2020-01-01/deadbeef.committed", b'{"key": "x"}')
    # Old, UNCOMMITTED orphan pair (no marker): must also be reaped.
    destination.put("evidence/acme/2020-01-01/orphanhead-cafe0002.json", b"orphan")
    destination.put("evidence/acme/2020-01-01/orphanhead-cafe0002.sig.json", b"orphan-sig")
    # Recent, in-scope: should survive.
    destination.put("evidence/acme/2026-09-20/cafef00d-cafe0003.json", b"recent")
    # A different org's prefix: must never be touched by acme's retention run.
    destination.put("evidence/other-org/2020-01-01/aaaa-cafe0004.json", b"other-org-old")
    # Something outside the evidence/ tree entirely: must never be touched.
    destination.put("unrelated/2020-01-01/not-evidence.json", b"unrelated")

    now = datetime(2026, 9, 23, tzinfo=UTC)
    result = sink.apply_retention(destination, org_id="acme", retention_days=30, now=now)

    assert result["applied"] is True
    assert set(result["deleted"]) == {
        "evidence/acme/2020-01-01/deadbeef-cafe0001.json",
        "evidence/acme/2020-01-01/deadbeef-cafe0001.sig.json",
        "evidence/acme/2020-01-01/deadbeef.committed",
        "evidence/acme/2020-01-01/orphanhead-cafe0002.json",
        "evidence/acme/2020-01-01/orphanhead-cafe0002.sig.json",
    }
    assert not destination.exists("evidence/acme/2020-01-01/deadbeef-cafe0001.json")
    assert not destination.exists("evidence/acme/2020-01-01/deadbeef.committed")
    assert destination.exists("evidence/acme/2026-09-20/cafef00d-cafe0003.json")
    assert destination.exists("evidence/other-org/2020-01-01/aaaa-cafe0004.json")
    assert destination.exists("unrelated/2020-01-01/not-evidence.json")


def test_retention_disabled_when_non_positive(tmp_path):
    destination = sink.LocalPathDestination(tmp_path)
    destination.put("evidence/acme/2020-01-01/deadbeef-cafe0001.json", b"old")
    now = datetime(2026, 9, 23, tzinfo=UTC)
    result = sink.apply_retention(destination, org_id="acme", retention_days=0, now=now)
    assert result == {"applied": False, "cutoff": None, "deleted": []}
    assert destination.exists("evidence/acme/2020-01-01/deadbeef-cafe0001.json")


# ---------------------------------------------------------------------------
# Disabled-by-default refusal
# ---------------------------------------------------------------------------


def test_disabled_by_default_never_touches_storage(monkeypatch, tmp_path):
    monkeypatch.delenv("UA_EVIDENCE_EXPORT_ENABLED", raising=False)
    monkeypatch.delenv("UA_EVIDENCE_EXPORT_DESTINATION", raising=False)
    result = sink.run_once(env={})
    assert result == {
        "status": "disabled",
        "reason": "UA_EVIDENCE_EXPORT_ENABLED is not set to '1'",
    }


def test_explicit_env_zero_is_also_disabled():
    assert sink.run_once(env={"UA_EVIDENCE_EXPORT_ENABLED": "0"})["status"] == "disabled"


# ---------------------------------------------------------------------------
# Missing-secret / missing-config reporting by name, signing included
# ---------------------------------------------------------------------------


def test_destination_from_env_names_the_missing_var():
    with pytest.raises(sink.DestinationConfigError, match="UA_EVIDENCE_EXPORT_DESTINATION"):
        sink.destination_from_env(env={})
    with pytest.raises(sink.DestinationConfigError, match="UA_EVIDENCE_EXPORT_LOCAL_PATH"):
        sink.destination_from_env(env={"UA_EVIDENCE_EXPORT_DESTINATION": "local"})
    with pytest.raises(sink.DestinationConfigError, match="UA_EVIDENCE_EXPORT_GCS_BUCKET"):
        sink.destination_from_env(env={"UA_EVIDENCE_EXPORT_DESTINATION": "gcs"})
    with pytest.raises(sink.DestinationConfigError, match="UA_EVIDENCE_EXPORT_S3_BUCKET"):
        sink.destination_from_env(env={"UA_EVIDENCE_EXPORT_DESTINATION": "s3"})


@pytest.mark.parametrize(
    "env,expected_missing",
    [
        (
            {},
            [
                "UA_EVIDENCE_EXPORT_DESTINATION",
                "UA_EVIDENCE_SIGNING_KEY",
                "UA_EVIDENCE_SIGNING_SECRET_NAME",
            ],
        ),
        (
            {"UA_EVIDENCE_EXPORT_DESTINATION": "local"},
            [
                "UA_EVIDENCE_EXPORT_LOCAL_PATH",
                "UA_EVIDENCE_SIGNING_KEY",
                "UA_EVIDENCE_SIGNING_SECRET_NAME",
            ],
        ),
        (
            {"UA_EVIDENCE_EXPORT_DESTINATION": "gcs"},
            [
                "UA_EVIDENCE_EXPORT_GCS_BUCKET",
                "UA_EVIDENCE_SIGNING_KEY",
                "UA_EVIDENCE_SIGNING_SECRET_NAME",
            ],
        ),
        (
            {
                "UA_EVIDENCE_EXPORT_DESTINATION": "local",
                "UA_EVIDENCE_EXPORT_LOCAL_PATH": "/tmp/wherever",
            },
            ["UA_EVIDENCE_SIGNING_KEY", "UA_EVIDENCE_SIGNING_SECRET_NAME"],
        ),
        (
            {
                "UA_EVIDENCE_EXPORT_DESTINATION": "local",
                "UA_EVIDENCE_EXPORT_LOCAL_PATH": "/tmp/wherever",
                "UA_EVIDENCE_SIGNING_KEY": "x",
            },
            [],
        ),
        (
            {
                "UA_EVIDENCE_EXPORT_DESTINATION": "local",
                "UA_EVIDENCE_EXPORT_LOCAL_PATH": "/tmp/wherever",
                "UA_EVIDENCE_SIGNING_SECRET_NAME": "prod-evidence-key",
            },
            [],
        ),
    ],
)
def test_manifest_secret_status_reports_names_only_and_requires_signing(env, expected_missing):
    status = sink.manifest_secret_status(env=env)
    assert status["missing"] == expected_missing
    assert status["satisfied"] == (not expected_missing)
    # Never echoes a value — only the names above ever appear.
    assert "wherever" not in str(status["missing"])
    assert "prod-evidence-key" not in str(status["missing"])


def test_destination_only_configured_reads_as_not_satisfied(tmp_path):
    """Finding 4: a destination alone (no signing configured) must not read
    as a satisfied/ready connector — run_once would refuse to write."""
    status = sink.manifest_secret_status(
        env={
            "UA_EVIDENCE_EXPORT_DESTINATION": "local",
            "UA_EVIDENCE_EXPORT_LOCAL_PATH": str(tmp_path),
        }
    )
    assert status["satisfied"] is False


def test_run_once_requires_signing_configured(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    env = {
        "UA_EVIDENCE_EXPORT_ENABLED": "1",
        "UA_EVIDENCE_EXPORT_DESTINATION": "local",
        "UA_EVIDENCE_EXPORT_LOCAL_PATH": str(tmp_path / "export-root"),
    }
    monkeypatch.delenv("UA_EVIDENCE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("UA_EVIDENCE_SIGNING_SECRET_NAME", raising=False)
    with pytest.raises(evidence_signing.SigningUnavailable):
        sink.run_once(env=env)


# ---------------------------------------------------------------------------
# GCS / S3 destinations: fakes only, no network, no SDK dependency required
# ---------------------------------------------------------------------------


class _FakeGCSBlob:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store = store
        self.name = name

    def upload_from_string(self, body: bytes, content_type: str | None = None) -> None:
        self._store[self.name] = body

    def exists(self) -> bool:
        return self.name in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self.name]

    def delete(self) -> None:
        if self.name not in self._store:
            raise RuntimeError("404 No such object")
        del self._store[self.name]


class _FakeGCSBlobHandle:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeGCSBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def blob(self, name: str) -> _FakeGCSBlob:
        return _FakeGCSBlob(self._store, name)


class _FakeGCSClient:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def bucket(self, _name: str) -> _FakeGCSBucket:
        return _FakeGCSBucket(self._store)

    def list_blobs(self, _bucket_name: str, prefix: str | None = None):
        prefix = prefix or ""
        return [_FakeGCSBlobHandle(name) for name in sorted(self._store) if name.startswith(prefix)]


def _install_fake_gcs(monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]) -> None:
    fake_storage_module = types.ModuleType("google.cloud.storage")
    fake_storage_module.Client = lambda: _FakeGCSClient(store)  # type: ignore[attr-defined]
    fake_cloud_module = types.ModuleType("google.cloud")
    fake_cloud_module.storage = fake_storage_module  # type: ignore[attr-defined]
    fake_google_module = types.ModuleType("google")
    fake_google_module.cloud = fake_cloud_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google_module)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud_module)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage_module)


def test_gcs_destination_put_exists_get_list_delete(monkeypatch):
    store: dict[str, bytes] = {}
    _install_fake_gcs(monkeypatch, store)
    destination = sink.GCSDestination("test-bucket", prefix="ua-evidence")

    destination.put("evidence/acme/2026-09-23/head-abcd.json", b"body")
    assert destination.exists("evidence/acme/2026-09-23/head-abcd.json") is True
    assert destination.get("evidence/acme/2026-09-23/head-abcd.json") == b"body"
    assert destination.list_prefix("evidence/acme/2026-09-23") == [
        "evidence/acme/2026-09-23/head-abcd.json"
    ]
    destination.delete("evidence/acme/2026-09-23/head-abcd.json")
    assert destination.exists("evidence/acme/2026-09-23/head-abcd.json") is False
    assert destination.get("evidence/acme/2026-09-23/head-abcd.json") is None
    destination.delete("evidence/acme/2026-09-23/head-abcd.json")  # Missing deletes are idempotent.


@pytest.mark.parametrize("operation", ["put", "exists", "get", "list", "delete"])
def test_gcs_destination_normalizes_provider_failures(monkeypatch, operation):
    _install_fake_gcs(monkeypatch, {"evidence/acme/object.json": b"body"})
    destination = sink.GCSDestination("test-bucket")
    exception = RuntimeError("provider detail must not reach CLI output")

    def fail(*_args, **_kwargs):
        raise exception

    method_by_operation = {
        "put": (_FakeGCSBlob, "upload_from_string"),
        "exists": (_FakeGCSBlob, "exists"),
        "get": (_FakeGCSBlob, "download_as_bytes"),
        "list": (_FakeGCSClient, "list_blobs"),
        "delete": (_FakeGCSBlob, "delete"),
    }
    target, method_name = method_by_operation[operation]
    monkeypatch.setattr(target, method_name, fail)

    with pytest.raises(sink.DestinationError, match=f"GCS {operation}") as exc_info:
        if operation == "put":
            destination.put("evidence/acme/object.json", b"new")
        elif operation == "exists":
            destination.exists("evidence/acme/object.json")
        elif operation == "get":
            destination.get("evidence/acme/object.json")
        elif operation == "list":
            destination.list_prefix("evidence/acme")
        else:
            destination.delete("evidence/acme/object.json")

    assert "provider detail" not in str(exc_info.value)
    assert exc_info.value.__cause__ is exception


def test_gcs_destination_without_package_raises_clear_error(monkeypatch):
    monkeypatch.delitem(sys.modules, "google.cloud.storage", raising=False)
    monkeypatch.delitem(sys.modules, "google.cloud", raising=False)
    monkeypatch.delitem(sys.modules, "google", raising=False)
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "google.cloud" or name.startswith("google.cloud."):
            raise ImportError("No module named 'google.cloud'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    destination = sink.GCSDestination("test-bucket")
    with pytest.raises(sink.DestinationError, match="google-cloud-storage"):
        destination.exists("evidence/acme/2026-09-23/head-abcd.json")


class _FakeS3Client:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def put_object(self, *, Bucket, Key, Body, ContentType=None):  # noqa: N803 - boto3's own casing
        self._store[Key] = Body

    def head_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self._store:
            raise _ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self._store:
            raise _ClientError({"Error": {"Code": "404"}}, "GetObject")
        return {"Body": types.SimpleNamespace(read=lambda: self._store[Key])}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self._store.pop(Key, None)

    def get_paginator(self, _operation_name: str):
        store = self._store

        class _Paginator:
            def paginate(self, *, Bucket, Prefix=""):  # noqa: N803
                matches = [k for k in sorted(store) if k.startswith(Prefix or "")]
                yield {"Contents": [{"Key": k} for k in matches]}

        return _Paginator()


class _ClientError(Exception):
    def __init__(self, response: dict, operation_name: str) -> None:
        super().__init__(f"{operation_name} failed: {response}")
        self.response = response


def _install_fake_boto3(monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]) -> None:
    fake_boto3_module = types.ModuleType("boto3")
    fake_boto3_module.client = lambda service, region_name=None: _FakeS3Client(store)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3_module)


def test_s3_destination_put_exists_get_list_delete(monkeypatch):
    store: dict[str, bytes] = {}
    _install_fake_boto3(monkeypatch, store)
    destination = sink.S3Destination("test-bucket", prefix="ua-evidence")

    destination.put("evidence/acme/2026-09-23/head-abcd.json", b"body")
    assert destination.exists("evidence/acme/2026-09-23/head-abcd.json") is True
    assert destination.get("evidence/acme/2026-09-23/head-abcd.json") == b"body"
    assert destination.list_prefix("evidence/acme/2026-09-23") == [
        "evidence/acme/2026-09-23/head-abcd.json"
    ]
    destination.delete("evidence/acme/2026-09-23/head-abcd.json")
    assert destination.exists("evidence/acme/2026-09-23/head-abcd.json") is False
    assert destination.get("evidence/acme/2026-09-23/head-abcd.json") is None


@pytest.mark.parametrize("operation", ["put", "exists", "get", "list", "delete"])
def test_s3_destination_normalizes_provider_failures(monkeypatch, operation):
    _install_fake_boto3(monkeypatch, {"evidence/acme/object.json": b"body"})
    destination = sink.S3Destination("test-bucket")
    exception = RuntimeError("provider detail must not reach CLI output")

    def fail(*_args, **_kwargs):
        raise exception

    method_by_operation = {
        "put": "put_object",
        "exists": "head_object",
        "get": "get_object",
        "list": "get_paginator",
        "delete": "delete_object",
    }
    monkeypatch.setattr(_FakeS3Client, method_by_operation[operation], fail)

    with pytest.raises(sink.DestinationError, match=f"S3 {operation}") as exc_info:
        if operation == "put":
            destination.put("evidence/acme/object.json", b"new")
        elif operation == "exists":
            destination.exists("evidence/acme/object.json")
        elif operation == "get":
            destination.get("evidence/acme/object.json")
        elif operation == "list":
            destination.list_prefix("evidence/acme")
        else:
            destination.delete("evidence/acme/object.json")

    assert "provider detail" not in str(exc_info.value)
    assert exc_info.value.__cause__ is exception


def test_s3_destination_without_package_raises_clear_error(monkeypatch):
    monkeypatch.delitem(sys.modules, "boto3", raising=False)
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    destination = sink.S3Destination("test-bucket")
    with pytest.raises(sink.DestinationError, match="boto3"):
        destination.exists("evidence/acme/2026-09-23/head-abcd.json")
