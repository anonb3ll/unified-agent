"""Tests for service.env merge helpers (task #1406)."""

from pathlib import Path

from unified_agent.bootstrap import init_main
from unified_agent.service_env import (
    PROTECTED_GCS_KEYS,
    merge_env_updates,
    merge_write_env_file,
    parse_env_file,
)


def test_merge_refuses_to_blank_protected_gcs_keys():
    existing = {
        "UA_REPLICA_GCS_URI": "gs://bucket/litestream/memory",
        "UA_BACKUP_GCS_URI": "gs://bucket/memory-db",
        "GOOGLE_APPLICATION_CREDENTIALS": "/secure/gcs-backup-sa.json",
        "UA_TRANSPORT": "http",
    }
    updates = {
        "UA_REPLICA_GCS_URI": "",
        "UA_BACKUP_GCS_URI": "",
        "GOOGLE_APPLICATION_CREDENTIALS": "",
        "UA_TRANSPORT": "stdio",
        "UA_HTTP_PORT": "8765",
    }
    merged = merge_env_updates(existing, updates, protected=PROTECTED_GCS_KEYS)
    assert merged["UA_REPLICA_GCS_URI"] == "gs://bucket/litestream/memory"
    assert merged["UA_BACKUP_GCS_URI"] == "gs://bucket/memory-db"
    assert merged["GOOGLE_APPLICATION_CREDENTIALS"] == "/secure/gcs-backup-sa.json"
    assert merged["UA_TRANSPORT"] == "stdio"
    assert merged["UA_HTTP_PORT"] == "8765"


def test_merge_allows_nonempty_protected_key_replacement():
    existing = {"UA_REPLICA_GCS_URI": "gs://old/path"}
    updates = {"UA_REPLICA_GCS_URI": "gs://new/path"}
    merged = merge_env_updates(existing, updates)
    assert merged["UA_REPLICA_GCS_URI"] == "gs://new/path"


def test_merge_write_round_trip(tmp_path: Path):
    path = tmp_path / "service.env"
    path.write_text(
        'UA_REPLICA_GCS_URI="gs://bucket/litestream/memory"\nUA_TRANSPORT=http\n',
        encoding="utf-8",
    )
    merge_write_env_file(
        path,
        {"UA_REPLICA_GCS_URI": "", "UA_TRANSPORT": "stdio", "UA_NEW": "1"},
    )
    parsed = parse_env_file(path)
    assert parsed["UA_REPLICA_GCS_URI"] == "gs://bucket/litestream/memory"
    assert parsed["UA_TRANSPORT"] == "stdio"
    assert parsed["UA_NEW"] == "1"
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_init_force_preserves_live_gcs_uris(isolated_env, monkeypatch, tmp_path):
    home = tmp_path / "ua-home"
    monkeypatch.setenv("UA_HOME", str(home))
    init_main([])
    env_path = home / "service.env"
    body = env_path.read_text()
    body += (
        '\nUA_REPLICA_GCS_URI="gs://ua-worker-backups/litestream/memory"\n'
        'UA_BACKUP_GCS_URI="gs://ua-worker-backups/memory-db"\n'
        "GOOGLE_APPLICATION_CREDENTIALS=/Users/x/.unified_agent/gcs-backup-sa.json\n"
    )
    env_path.write_text(body)

    init_main(["--force"])
    parsed = parse_env_file(env_path)
    assert parsed["UA_REPLICA_GCS_URI"] == "gs://ua-worker-backups/litestream/memory"
    assert parsed["UA_BACKUP_GCS_URI"] == "gs://ua-worker-backups/memory-db"
    assert parsed["GOOGLE_APPLICATION_CREDENTIALS"] == "/Users/x/.unified_agent/gcs-backup-sa.json"
    # Token rotated (force still regenerates auth).
    assert "UA_AGENT_TOKENS=" in env_path.read_text()


def test_targeted_merge_preserves_expansion_exports_duplicates_and_protected_keys(tmp_path):
    path = tmp_path / "fixture.env"
    raw = "# user shell configuration\nUA_WORKSPACE=$HOME/workspace\nUA_BACKUP_GCS_URI=$BACKUP_TARGET\n"
    path.write_text(raw + "export UA_OPERATOR_NAME=Old\nUA_OPERATOR_NAME=Older")
    merge_write_env_file(
        path,
        {
            "UA_OPERATOR_NAME": "Sam Example",
            "UA_BACKUP_GCS_URI": "",
            "UA_OPERATOR_LABEL": "Desktop_Sam",
        },
        preserve_lines=True,
    )
    body = path.read_text()
    assert body.startswith(raw)
    assert "export UA_OPERATOR_NAME='Sam Example'\n" in body
    assert "UA_OPERATOR_NAME='Sam Example'\nUA_OPERATOR_LABEL=Desktop_Sam\n" in body
    assert parse_env_file(path)["UA_OPERATOR_NAME"] == "Sam Example"
    assert path.stat().st_mode & 0o777 == 0o600


def test_merge_write_updates_a_symlinked_env_target_and_keeps_the_link(tmp_path):
    tmp_path = tmp_path / "env-dir"
    tmp_path.mkdir()
    shared = tmp_path / "shared.env"
    shared.write_text("UA_OPERATOR_NAME=Old\n")
    link = tmp_path / "service.env"
    link.symlink_to(shared)
    for preserve_lines in (True, False):
        merge_write_env_file(link, {"UA_OPERATOR_NAME": "Sam"}, preserve_lines=preserve_lines)
        assert link.is_symlink() and link.resolve() == shared
        assert parse_env_file(shared)["UA_OPERATOR_NAME"] == "Sam"
        assert shared.stat().st_mode & 0o777 == 0o600
        shared.write_text("UA_OPERATOR_NAME=Old\n")
    assert {p.name for p in tmp_path.iterdir()} == {"shared.env", "service.env"}
