import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from unified_agent import backup, config, storage
from unified_agent.tools.memory import memory_save
from unified_agent.tools.tasks import task_add


def test_online_backup_is_complete_and_integrity_checked(isolated_env, tmp_path):
    storage.init_db()
    memory_save("backup proof", title="Backup proof", author="Desktop_Agent2")
    task_add("backup task")

    result = backup.create_backup(
        directory=tmp_path / "backups", now=datetime(2026, 8, 12, tzinfo=UTC)
    )

    assert result["verification"]["ok"] is True
    assert not list((tmp_path / "backups").glob("*.tmp-*"))
    assert result["verification"]["schema_version"] == str(storage.SCHEMA_VERSION)
    assert backup.backup_status(tmp_path / "backups", now=datetime(2026, 8, 12, 1, tzinfo=UTC))[
        "exists"
    ]
    assert not list((tmp_path / "backups").glob("*.db-wal"))
    assert not list((tmp_path / "backups").glob("*.db-shm"))
    assert result["gcs_upload"]["status"] == "skipped"


def test_retention_keeps_seven_days_and_four_weekly_generations(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    latest = datetime(2026, 8, 12, tzinfo=UTC)
    for days_ago in range(80):
        when = latest - timedelta(days=days_ago)
        (directory / f"memory-{when.strftime('%Y%m%dT%H%M%SZ')}.db").touch()

    removed = backup.prune_backups(directory)
    remaining = backup._backup_files(directory)

    assert len(remaining) == 11
    assert len(removed) == 69
    assert {timestamp.date() for timestamp, _ in remaining[:7]} == {
        (latest - timedelta(days=days)).date() for days in range(7)
    }


def test_retention_weekly_slots_skip_days_already_kept_as_daily(tmp_path):
    """Two snapshots a day must not spend weekly generations on a daily's twin."""
    directory = tmp_path / "backups"
    directory.mkdir()
    latest = datetime(2026, 8, 12, 15, tzinfo=UTC)
    for days_ago in range(60):
        for hours in (0, 12):
            when = latest - timedelta(days=days_ago, hours=hours)
            (directory / f"memory-{when.strftime('%Y%m%dT%H%M%SZ')}.db").touch()

    backup.prune_backups(directory)
    days = [timestamp.date() for timestamp, _ in backup._backup_files(directory)]

    assert len(days) == len(set(days)) == 11


def test_backup_status_reports_corrupt_latest_snapshot(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    path = directory / "memory-20260812T000000Z.db"
    path.write_text("not sqlite")

    status = backup.backup_status(directory, now=datetime(2026, 8, 12, 1, tzinfo=UTC))

    assert status["exists"] is True
    assert status["verification"]["ok"] is False


def test_backup_gcs_uri_unset_returns_none(monkeypatch):
    monkeypatch.delenv("UA_BACKUP_GCS_URI", raising=False)
    assert config.backup_gcs_uri() is None
    monkeypatch.setenv("UA_BACKUP_GCS_URI", "   ")
    assert config.backup_gcs_uri() is None


def test_backup_gcs_uri_reads_env(monkeypatch):
    monkeypatch.setenv("UA_BACKUP_GCS_URI", "gs://ua-memory-backups-x/memory-db")
    assert config.backup_gcs_uri() == "gs://ua-memory-backups-x/memory-db"


def test_upload_backup_to_gcs_skipped_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("UA_BACKUP_GCS_URI", raising=False)
    path = tmp_path / "memory-20260812T000000Z.db"
    path.write_bytes(b"x")
    result = backup.upload_backup_to_gcs(path)
    assert result == {"status": "skipped", "reason": "UA_BACKUP_GCS_URI unset"}


def test_upload_backup_to_gcs_ok_via_gsutil(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_BACKUP_GCS_URI", "gs://bucket/memory-db")
    path = tmp_path / "memory-20260812T000000Z.db"
    path.write_bytes(b"x")

    def fake_which(name):
        return "/usr/bin/gsutil" if name == "gsutil" else None

    with (
        patch("unified_agent.backup.shutil.which", side_effect=fake_which),
        patch("unified_agent.backup.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gsutil", "cp", str(path), "gs://bucket/memory-db/memory-20260812T000000Z.db"],
            returncode=0,
            stdout="",
            stderr="",
        )
        result = backup.upload_backup_to_gcs(path)

    assert result["status"] == "ok"
    assert result["tool"] == "gsutil"
    assert result["uri"] == "gs://bucket/memory-db/memory-20260812T000000Z.db"
    mock_run.assert_called_once()
    assert Path(mock_run.call_args.args[0][0]).name == "gsutil"


def test_upload_backup_to_gcs_falls_back_to_gcloud(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_BACKUP_GCS_URI", "gs://bucket/prefix/")
    path = tmp_path / "memory-20260812T000000Z.db"
    path.write_bytes(b"x")
    # Ignore documented SDK install dirs so this case only exercises PATH fallback.
    monkeypatch.setattr(backup, "_GCS_SDK_BIN_CANDIDATES", ())

    def fake_which(name):
        return "/usr/bin/gcloud" if name == "gcloud" else None

    with (
        patch("unified_agent.backup.shutil.which", side_effect=fake_which),
        patch("unified_agent.backup.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        result = backup.upload_backup_to_gcs(path)

    assert result["status"] == "ok"
    assert result["tool"] == "gcloud"
    assert result["uri"] == "gs://bucket/prefix/memory-20260812T000000Z.db"
    cmd = mock_run.call_args.args[0]
    assert Path(cmd[0]).name == "gcloud"
    assert cmd[1:3] == ["storage", "cp"]


def test_resolve_gcs_tool_checks_documented_sdk_dirs(monkeypatch, tmp_path):
    sdk_bin = tmp_path / "google-cloud-sdk" / "bin"
    sdk_bin.mkdir(parents=True)
    gsutil = sdk_bin / "gsutil"
    gsutil.write_text("#!/bin/sh\n")
    gsutil.chmod(0o755)
    monkeypatch.setattr(backup, "_GCS_SDK_BIN_CANDIDATES", (sdk_bin,))
    monkeypatch.setattr(backup.shutil, "which", lambda _name: None)

    assert backup._resolve_gcs_tool("gsutil") == str(gsutil)


def test_upload_backup_to_gcs_error_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_BACKUP_GCS_URI", "gs://bucket/memory-db")
    path = tmp_path / "memory-20260812T000000Z.db"
    path.write_bytes(b"x")

    with (
        patch("unified_agent.backup.shutil.which", return_value="/usr/bin/gsutil"),
        patch("unified_agent.backup.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="AccessDeniedException: 403",
        )
        result = backup.upload_backup_to_gcs(path)

    assert result["status"] == "error"
    assert "403" in result["detail"]
    assert result["tool"] == "gsutil"


def test_create_backup_gcs_failure_still_succeeds_locally(isolated_env, tmp_path, monkeypatch):
    storage.init_db()
    monkeypatch.setenv("UA_BACKUP_GCS_URI", "gs://bucket/memory-db")

    with patch(
        "unified_agent.backup.upload_backup_to_gcs",
        return_value={"status": "error", "detail": "boom", "tool": "gsutil"},
    ):
        result = backup.create_backup(
            directory=tmp_path / "backups",
            now=datetime(2026, 8, 13, tzinfo=UTC),
            upload_to_gcs=True,
        )

    assert result["verification"]["ok"] is True
    assert Path(result["path"]).is_file()
    assert result["gcs_upload"]["status"] == "error"
    assert result["gcs_upload"]["detail"] == "boom"


def test_create_backup_skips_gcs_unless_opted_in(isolated_env, tmp_path, monkeypatch):
    storage.init_db()
    monkeypatch.setenv("UA_BACKUP_GCS_URI", "gs://bucket/memory-db")
    called = []

    with patch(
        "unified_agent.backup.upload_backup_to_gcs",
        side_effect=lambda *a, **k: called.append(True) or {"status": "ok"},
    ):
        result = backup.create_backup(
            directory=tmp_path / "backups", now=datetime(2026, 8, 14, tzinfo=UTC)
        )

    assert called == []
    assert result["gcs_upload"] == {"status": "skipped", "reason": "upload not requested"}


def test_backup_runner_prunes_perf_samples(isolated_env, tmp_path, monkeypatch, capsys):
    import json as _json
    from datetime import timedelta

    from unified_agent import backup_runner

    storage.init_db()
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="seconds")
    fresh = datetime.now(UTC).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        for ts in (old, old, fresh):
            conn.execute(
                "INSERT INTO agent_perf_samples (ts, tool, latency_ms, ok) VALUES (?, 'x', 1.0, 1)",
                (ts,),
            )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("UA_PERF_SAMPLE_RETENTION_DAYS", "14")
    monkeypatch.setattr(
        backup_runner,
        "create_backup",
        lambda **kw: backup.create_backup(directory=tmp_path / "b", **kw),
    )

    assert backup_runner.main() == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["perf_samples_pruned"] == 2

    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM agent_perf_samples").fetchone()[0] == 1
    finally:
        conn.close()


def test_upload_rejects_non_gs_uri(tmp_path):
    path = tmp_path / "memory-20260812T000000Z.db"
    path.write_bytes(b"x")
    result = backup.upload_backup_to_gcs(path, gcs_uri="s3://not-gcs/prefix")
    assert result["status"] == "error"
    assert "gs://" in result["detail"]


def test_stage_backup_snapshot_refuses_ua_home(isolated_env, tmp_path):
    src = tmp_path / "memory-20260812T000000Z.db"
    src.write_bytes(b"x")
    home = config.data_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "memory.db").write_bytes(b"live")

    try:
        backup.stage_backup_snapshot(src, home)
    except backup.RestoreDestinationError as exc:
        assert "UA_HOME" in str(exc)
    else:
        raise AssertionError("expected RestoreDestinationError")
    assert (home / "memory.db").read_bytes() == b"live"


def test_stage_backup_snapshot_refuses_path_inside_ua_home(isolated_env, tmp_path):
    src = tmp_path / "memory-20260812T000000Z.db"
    src.write_bytes(b"x")
    nested = config.data_home() / "scratch"
    nested.mkdir(parents=True, exist_ok=True)

    try:
        backup.stage_backup_snapshot(src, nested)
    except backup.RestoreDestinationError as exc:
        assert "inside UA_HOME" in str(exc)
    else:
        raise AssertionError("expected RestoreDestinationError")


def test_stage_backup_snapshot_copies_to_scratch_and_counts(isolated_env, tmp_path):
    storage.init_db()
    memory_save("restore proof", title="Restore proof", author="Desktop_Agent4")
    task_add("restore task")
    snapshot = Path(
        backup.create_backup(directory=tmp_path / "backups", now=datetime(2026, 8, 25, tzinfo=UTC))[
            "path"
        ]
    )
    dest = tmp_path / "restore-scratch"

    result = backup.stage_backup_snapshot(snapshot, dest, as_ua_home=True)

    assert Path(result["path"]).name == "memory.db"
    assert result["verification"]["ok"] is True
    assert result["counts"]["tasks"] >= 1
    assert result["counts"]["memory"] >= 1
    assert config.db_path().exists()
    assert Path(result["path"]).resolve() != config.db_path().resolve()


def test_stage_backup_snapshot_in_place_skips_same_file_copy(isolated_env, tmp_path):
    storage.init_db()
    memory_save("in place", title="In place", author="Desktop_Agent4")
    snapshot = Path(
        backup.create_backup(directory=tmp_path, now=datetime(2026, 8, 25, tzinfo=UTC))["path"]
    )
    result = backup.stage_backup_snapshot(snapshot, tmp_path)
    assert Path(result["path"]).resolve() == snapshot.resolve()
    assert result["verification"]["ok"] is True


def test_assert_private_failover_bind_allows_loopback_and_tailscale():
    assert backup.assert_private_failover_bind("127.0.0.1") == "127.0.0.1"
    assert backup.assert_private_failover_bind("::1") == "::1"
    assert backup.assert_private_failover_bind("100.64.0.20") == "100.64.0.20"


def test_assert_private_failover_bind_refuses_public_and_hostnames():
    for host in ("0.0.0.0", "::", "*", "8.8.8.8", "localhost", "ua-worker"):
        try:
            backup.assert_private_failover_bind(host)
        except backup.FailoverBindError:
            continue
        raise AssertionError(f"expected FailoverBindError for {host!r}")
    try:
        backup.assert_private_failover_bind("")
    except backup.FailoverBindError:
        pass
    else:
        raise AssertionError("expected FailoverBindError for empty host")


def _require_unexported(path: Path) -> None:
    """Skip in a public export, where scripts/public_release/exclude.txt drops ``path``.

    The private repo ships the release tooling, so a missing file there still fails.
    """
    repo = Path(__file__).resolve().parents[1]
    if not path.exists() and not (repo / "scripts" / "public_release").is_dir():
        pytest.skip(f"{path.relative_to(repo)} is excluded from the public export")


def test_failover_rehearsal_script_check_bind():
    script = Path(__file__).resolve().parents[1] / "deploy" / "gcp" / "failover-rehearsal.sh"
    _require_unexported(script)
    accepted = subprocess.run(
        [str(script), "check-bind", "100.64.0.20"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "100.64.0.20"
    refused = subprocess.run(
        [str(script), "check-bind", "0.0.0.0"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert refused.returncode == 2
    assert "public bind" in refused.stderr


# --- S3-compatible offsite copy (task #2585) --------------------------------

_S3_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_S3_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_SIGV4_DATE = datetime(2013, 5, 24, tzinfo=UTC)


def test_sigv4_matches_the_aws_documented_get_object_vector():
    # AWS SigV4 for S3 "GET Object" example; botocore reproduces it too.
    signed = backup.sigv4_headers(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/test.txt",
        headers={"Range": "bytes=0-9"},
        payload_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        access_key=_S3_KEY_ID,
        secret_key=_S3_SECRET,
        region="us-east-1",
        now=_SIGV4_DATE,
    )
    assert signed["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert signed["x-amz-date"] == "20130524T000000Z"


def test_sigv4_path_style_r2_put_matches_botocore():
    # Reference computed with botocore.auth.S3SigV4Auth for the same request.
    signed = backup.sigv4_headers(
        method="PUT",
        url="https://acct.r2.cloudflarestorage.com/ua-hub-backups/hub/memory-20260812T000000Z.db",
        headers={"Content-Type": "application/octet-stream"},
        payload_sha256="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        access_key=_S3_KEY_ID,
        secret_key=_S3_SECRET,
        region="auto",
        now=_SIGV4_DATE,
    )
    assert signed["Authorization"].endswith(
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, "
        "Signature=7354746dc5b446ea4073f93b4085753506ae085195c69c22a92a3ad7c920e1fa"
    )


def _s3_env(monkeypatch, **overrides):
    values = {
        "UA_BACKUP_S3_URI": "s3://ua-hub-backups/hub/",
        "UA_BACKUP_S3_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
        "UA_BACKUP_S3_ACCESS_KEY_ID": _S3_KEY_ID,
        "UA_BACKUP_S3_SECRET_ACCESS_KEY": _S3_SECRET,
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _recording_client(status=200, raise_exc=None):
    import httpx

    seen = []

    def handler(request):
        if raise_exc is not None:
            raise raise_exc
        seen.append((request, request.read()))
        return httpx.Response(status, headers={"etag": '"abc"'}, text="<Error>denied</Error>")

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_upload_backup_to_s3_skipped_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("UA_BACKUP_S3_URI", raising=False)
    client, seen = _recording_client()
    result = backup.upload_backup_to_s3(tmp_path / "x.db", client=client)
    assert result == {"status": "skipped", "reason": "UA_BACKUP_S3_URI unset"}
    assert seen == []


def test_upload_backup_to_s3_puts_a_signed_streamed_snapshot(monkeypatch, tmp_path):
    _s3_env(monkeypatch)
    snapshot = tmp_path / "memory-20260812T000000Z.db"
    snapshot.write_bytes(b"x" * (3 * 1024 * 1024 + 7))  # spans several read chunks
    client, seen = _recording_client()

    result = backup.upload_backup_to_s3(snapshot, client=client)

    assert result["status"] == "ok"
    assert result["uri"] == "s3://ua-hub-backups/hub/memory-20260812T000000Z.db"
    assert result["bytes"] == snapshot.stat().st_size
    ((request, body),) = seen
    assert request.method == "PUT"
    assert str(request.url) == (
        "https://acct.r2.cloudflarestorage.com/ua-hub-backups/hub/memory-20260812T000000Z.db"
    )
    assert body == snapshot.read_bytes()
    assert request.headers["content-length"] == str(len(body))
    assert "transfer-encoding" not in request.headers
    import hashlib

    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
    assert "/auto/s3/aws4_request" in request.headers["authorization"]
    # Nothing a caller logs or stores carries the secret or the signature.
    assert _S3_SECRET not in repr(result) and "Signature=" not in repr(result)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"UA_BACKUP_S3_URI": "gs://ua-hub-backups"}, "must look like s3://"),
        ({"UA_BACKUP_S3_ENDPOINT": "http://acct.r2.cloudflarestorage.com"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": None}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://["}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://storage.invalid?region=auto"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://storage.invalid#fragment"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "userinfo"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://storage.invalid:bad"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://storage.invalid:"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://storage.invalid:99999"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://storage.invalid?"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://storage.invalid/path"}, "https://"),
        ({"UA_BACKUP_S3_ENDPOINT": "https://stor\nage.invalid"}, "https://"),
        ({"UA_BACKUP_S3_SECRET_ACCESS_KEY": None}, "SECRET_ACCESS_KEY unset"),
    ],
)
def test_upload_backup_to_s3_refuses_bad_config_without_a_request(
    monkeypatch, tmp_path, overrides, expected
):
    if overrides.get("UA_BACKUP_S3_ENDPOINT") == "userinfo":
        import secrets

        import httpx

        overrides = {
            "UA_BACKUP_S3_ENDPOINT": str(
                httpx.URL("https://storage.invalid").copy_with(
                    username=secrets.token_hex(8), password=secrets.token_hex(12)
                )
            )
        }
    _s3_env(monkeypatch, **overrides)
    snapshot = tmp_path / "memory-20260812T000000Z.db"
    snapshot.write_bytes(b"db")
    client, seen = _recording_client()

    result = backup.upload_backup_to_s3(snapshot, client=client)

    assert result["status"] == "error"
    assert expected in result["detail"]
    assert seen == []


def test_upload_backup_to_s3_failures_are_reported_not_raised(monkeypatch, tmp_path):
    import httpx

    _s3_env(monkeypatch)
    snapshot = tmp_path / "memory-20260812T000000Z.db"
    snapshot.write_bytes(b"db")

    client, _ = _recording_client(status=403)
    denied = backup.upload_backup_to_s3(snapshot, client=client)
    assert denied["status"] == "error"
    assert denied["detail"].startswith("HTTP 403")

    client, _ = _recording_client(raise_exc=httpx.ConnectError("no route"))
    unreachable = backup.upload_backup_to_s3(snapshot, client=client)
    assert unreachable["status"] == "error"
    assert "ConnectError" in unreachable["detail"]


@pytest.mark.parametrize("failure", ["response", "transport", "request_config"])
def test_s3_failure_details_never_echo_authentication_material(tmp_path, caplog, failure):
    import secrets

    import httpx

    key, secret = secrets.token_hex(12), secrets.token_hex(24)
    material = []
    snapshot = tmp_path / "memory-20260812T000000Z.db"
    snapshot.write_bytes(b"synthetic backup")

    def handler(request):
        auth = request.headers["authorization"]
        signature = auth.split("Signature=")[1]
        material.extend((key, secret, signature))
        detail = (
            "<Error><Code>SignatureDoesNotMatch</Code>"
            f"<AWSAccessKeyId>{key}</AWSAccessKeyId>"
            f"<SignatureProvided>{signature}</SignatureProvided></Error>"
        )
        if failure == "transport":
            raise httpx.ConnectError(detail)
        if failure == "request_config":
            raise ValueError(detail)
        return httpx.Response(403, text=detail)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = backup.upload_backup_to_s3(
            snapshot,
            s3_uri="s3://review-bucket/hub",
            endpoint="https://storage.invalid",
            access_key=key,
            secret_key=secret,
            client=client,
        )
    assert result["status"] == "error"
    assert material, "the upload did not exercise its failure path"
    leaked = any(value in repr(result) or value in caplog.text for value in material)
    assert not leaked, "authentication material escaped through upload diagnostics"
    if failure == "response":
        # The allowlisted S3 error code is the one part of the body reported.
        assert result["detail"] == "HTTP 403 SignatureDoesNotMatch"


@pytest.mark.parametrize("port", [443, 8443])
def test_s3_explicit_port_signs_the_host_the_client_sends(tmp_path, port):
    from urllib.parse import urlsplit

    snapshot = tmp_path / "memory-20260812T000000Z.db"
    snapshot.write_bytes(b"synthetic backup")
    client, seen = _recording_client()
    with client:
        result = backup.upload_backup_to_s3(
            snapshot,
            s3_uri="s3://review-bucket/hub",
            endpoint=f"https://storage.invalid:{port}",
            access_key=_S3_KEY_ID,
            secret_key=_S3_SECRET,
            now=_SIGV4_DATE,
            client=client,
        )
    assert result["status"] == "ok"
    ((request, _),) = seen
    sent_url = str(request.url)
    assert urlsplit(sent_url).netloc == request.headers["host"]
    expected = backup.sigv4_headers(
        method="PUT",
        url=sent_url,
        headers={"Content-Type": "application/octet-stream"},
        payload_sha256=request.headers["x-amz-content-sha256"],
        access_key=_S3_KEY_ID,
        secret_key=_S3_SECRET,
        region="auto",
        now=_SIGV4_DATE,
    )
    assert request.headers["authorization"] == expected["Authorization"]


def test_create_backup_reports_local_success_for_an_invalid_s3_endpoint(
    isolated_env, monkeypatch, tmp_path
):
    storage.init_db()
    _s3_env(monkeypatch, UA_BACKUP_S3_ENDPOINT="https://[")
    result = backup.create_backup(directory=tmp_path / "backups", upload_to_s3=True)
    assert result["verification"]["ok"] is True
    assert Path(result["path"]).is_file()
    assert result["s3_upload"]["status"] == "error"


def test_create_backup_copies_offsite_only_when_opted_in(isolated_env, tmp_path, monkeypatch):
    storage.init_db()
    calls = []
    monkeypatch.setattr(
        backup, "upload_backup_to_s3", lambda path: calls.append(path) or {"status": "ok"}
    )

    local_only = backup.create_backup(
        directory=tmp_path / "b", now=datetime(2026, 8, 12, tzinfo=UTC)
    )
    offsite = backup.create_backup(
        directory=tmp_path / "b", now=datetime(2026, 8, 13, tzinfo=UTC), upload_to_s3=True
    )

    assert local_only["s3_upload"]["status"] == "skipped"
    assert offsite["s3_upload"] == {"status": "ok"}
    assert calls == [Path(offsite["path"])]


def test_local_retention_follows_the_keep_variables(isolated_env, tmp_path, monkeypatch):
    storage.init_db()
    monkeypatch.setenv("UA_BACKUP_KEEP_DAILY", "2")
    monkeypatch.setenv("UA_BACKUP_KEEP_WEEKLY", "0")
    for day in range(1, 6):
        backup.create_backup(directory=tmp_path / "b", now=datetime(2026, 8, day, tzinfo=UTC))
    assert [p.name for _, p in backup._backup_files(tmp_path / "b")] == [
        "memory-20260805T000000Z.db",
        "memory-20260804T000000Z.db",
    ]


@pytest.mark.parametrize(
    ("daily", "weekly", "expected"),
    [("", "", (7, 4)), ("0", "-3", (1, 0)), ("nope", "2", (7, 2))],
)
def test_backup_local_keep_clamps_and_defaults(monkeypatch, daily, weekly, expected):
    monkeypatch.setenv("UA_BACKUP_KEEP_DAILY", daily)
    monkeypatch.setenv("UA_BACKUP_KEEP_WEEKLY", weekly)
    assert config.backup_local_keep() == expected


def test_backup_loop_keeps_running_after_a_failed_snapshot(capsys):
    from unified_agent import backup_loop

    outcomes = iter([RuntimeError("Signature=deadbeef"), 0, 3])
    sleeps = []

    def runner():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert (
        backup_loop.run(
            interval=7200, first_delay=60, runner=runner, sleep=sleeps.append, max_runs=3
        )
        == 0
    )
    assert sleeps == [60, 7200, 7200]
    logged = capsys.readouterr().out
    assert '"error": "RuntimeError"' in logged
    assert "deadbeef" not in logged  # exception text can quote a signed request
    assert '"exit_code": 3' in logged


@pytest.mark.parametrize(
    ("raw", "expected"), [("", None), ("0", None), ("abc", None), ("60", 3600), ("86400", 86400)]
)
def test_backup_loop_interval_is_opt_in_with_an_hour_floor(monkeypatch, raw, expected):
    from unified_agent import backup_loop

    monkeypatch.setenv("UA_BACKUP_INTERVAL_S", raw)
    assert backup_loop.interval_from_env() == expected
