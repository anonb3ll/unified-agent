"""vault_sync.missing_target_settings (#1813): the sync unit's target as it resolves it."""

from __future__ import annotations

from pathlib import Path

import pytest

from unified_agent import vault_sync

FULL = (
    'schema_version = 1\n[paths]\nvault = "/vault"\n'
    '[vault_sync]\nremote_host = "hub"\nremote_root = "vaults/main"\n'
)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A UA_HOME with a private settings.toml, found the way the unit finds it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ua_home = tmp_path / ".unified_agent"
    ua_home.mkdir()
    return ua_home


def _write(home: Path, settings: str | None = None, service_env: str | None = None) -> Path:
    if settings is not None:
        path = home / "settings.toml"
        path.write_text(settings)
        path.chmod(0o600)
    env = home / "service.env"
    if service_env is not None:
        env.write_text(service_env)
    return env


def test_nothing_configured_reports_every_key(home):
    assert vault_sync.missing_target_settings(_write(home)) == list(vault_sync.TARGET_SETTINGS)


def test_settings_file_alone_is_enough(home):
    assert vault_sync.missing_target_settings(_write(home, FULL)) == []


def test_service_env_alone_is_enough(home):
    env = "UA_VAULT_SYNC_REMOTE_HOST=hub\nUA_VAULT_SYNC_REMOTE_ROOT=v\nUA_VAULT_PATH=/v\n"
    assert vault_sync.missing_target_settings(_write(home, service_env=env)) == []


@pytest.mark.parametrize("blank", ["", "   ", '""', "''"])
def test_blank_service_env_value_masks_settings(home, blank):
    """service.env beats settings.toml: a blank override leaves the sync unconfigured."""
    env = _write(home, FULL, f"UA_VAULT_SYNC_REMOTE_HOST={blank}\n")
    assert vault_sync.missing_target_settings(env) == ["vault_sync.remote_host"]


def test_explicit_local_root_needs_no_vault_path(home):
    """UA_VAULT_SYNC_LOCAL_ROOT is a whole destination; paths.vault stays unset."""
    settings = FULL.replace('[paths]\nvault = "/vault"\n', "")
    env = _write(home, settings, "UA_VAULT_SYNC_LOCAL_ROOT=/sync\n")
    assert vault_sync.missing_target_settings(env) == []


def test_blank_local_root_in_service_env_masks_settings_env_entry(home):
    """A settings [env] local root behind a blank service.env override is no destination."""
    settings = FULL.replace('[paths]\nvault = "/vault"\n', "") + (
        '[env]\nUA_VAULT_SYNC_LOCAL_ROOT = "/sync"\n'
    )
    env = _write(home, settings, "UA_VAULT_SYNC_LOCAL_ROOT=\n")
    assert vault_sync.missing_target_settings(env) == ["paths.vault"]


def test_service_env_can_point_at_another_settings_file(home, tmp_path):
    other = tmp_path / "other.toml"
    other.write_text(FULL)
    other.chmod(0o600)
    env = _write(home, service_env=f"UA_SETTINGS_FILE={other}\n")
    assert vault_sync.missing_target_settings(env) == []


def test_cli_prints_missing_keys(home, capsys):
    env = _write(home, service_env="UA_VAULT_SYNC_REMOTE_HOST=hub\nUA_VAULT_PATH=/v\n")
    assert vault_sync.main(["--missing-target", str(env)]) == 0
    assert capsys.readouterr().out.strip() == "vault_sync.remote_root"


def test_settings_env_local_root_counts_without_an_override(home):
    settings = FULL.replace('[paths]\nvault = "/vault"\n', "") + (
        '[env]\nUA_VAULT_SYNC_LOCAL_ROOT = "/sync"\n'
    )
    assert vault_sync.missing_target_settings(_write(home, settings)) == []


def test_target_settings_file_follows_service_env(home, tmp_path, capsys):
    assert vault_sync.target_settings_file(_write(home)) == home / "settings.toml"
    other = tmp_path / "other.toml"
    env = _write(home, service_env=f"UA_SETTINGS_FILE={other}\n")
    assert vault_sync.target_settings_file(env) == other
    env = _write(home, service_env="UA_SETTINGS_FILE=\n")
    assert vault_sync.target_settings_file(env) is None
    assert vault_sync.main(["--target-settings-file", str(env)]) == 0
    assert capsys.readouterr().out == "\n"
