"""Explicit integration targets must precede local state and external IO."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from unified_agent import config, github_app_worker, lane_b_telemetry, vault_sync
from unified_agent.tools import doctor

ROOT = Path(__file__).parent.parent


def no_io(*args, **kwargs):
    pytest.fail("unconfigured integration attempted IO")


@pytest.mark.parametrize(
    "repo", ["", " ", "owner", "owner/../repo", "https://github.com/o/r", "o/.."]
)
def test_github_missing_or_invalid_repo_is_unconfigured(monkeypatch, repo):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", repo)
    monkeypatch.setattr(github_app_worker.storage, "init_db", no_io)
    monkeypatch.setattr(github_app_worker.github_app, "build_transport", no_io)
    result = github_app_worker.run_once(run_gh=no_io)
    assert result == [
        {"status": "unconfigured", "detail": "set UA_GITHUB_CONNECTOR_REPO to an owner/repository"}
    ]


def test_disabled_github_worker_needs_no_target(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    monkeypatch.delenv("UA_GITHUB_CONNECTOR_REPO", raising=False)
    monkeypatch.setattr(github_app_worker.storage, "init_db", no_io)
    assert github_app_worker.run_once(run_gh=no_io) == []


def test_github_poll_captures_repository_and_resets_context(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/first")

    def poll(**kwargs):
        monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/next")
        assert github_app_worker._repo() == "example/first"
        return []

    monkeypatch.setattr(github_app_worker, "_run_once_configured", poll)
    assert github_app_worker.run_once() == []
    assert github_app_worker._repo() == "example/next"


@pytest.mark.parametrize("value", ["", " ", "[]", "bad-json", "{}", '[{"slug":"../escape"}]'])
def test_telemetry_without_valid_targets_is_a_gap_without_io(monkeypatch, value, capsys):
    monkeypatch.setenv("UA_LANE_B_REPOS", value)
    monkeypatch.setattr(lane_b_telemetry.config, "vault_root", no_io)
    monkeypatch.setattr(lane_b_telemetry.storage, "init_db", no_io)
    monkeypatch.setattr(lane_b_telemetry, "_default_fetch", no_io)
    report = lane_b_telemetry.build_report()
    assert report["repos"] == {}
    assert all(
        gate["status"] == "unavailable" and gate["count"] is None
        for gate in report["totals"].values()
    )
    assert lane_b_telemetry.main([]) == 0
    assert json.loads(capsys.readouterr().out)["repos"] == {}


@pytest.mark.parametrize("missing", ["UA_VAULT_SYNC_REMOTE_HOST", "UA_VAULT_SYNC_REMOTE_ROOT"])
def test_vault_sync_missing_target_has_no_side_effects(monkeypatch, tmp_path, missing):
    monkeypatch.setenv("UA_VAULT_SYNC_REMOTE_HOST", "vault-host")
    monkeypatch.setenv("UA_VAULT_SYNC_REMOTE_ROOT", "vault")
    monkeypatch.setenv(missing, " ")
    monkeypatch.setattr(vault_sync.config, "vault_sync_state_path", no_io)
    monkeypatch.setattr(vault_sync.config, "vault_sync_local_root", no_io)
    monkeypatch.setattr(vault_sync, "SSHTransport", no_io)
    assert vault_sync.run()["status"] == "unconfigured"
    assert not (Path(os.environ["UA_HOME"]) / "vault-sync-state.json").exists()


def test_doctor_cannot_certify_cached_sync_without_target(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "_service_probe", lambda *a: {"LoadState": "loaded"})
    # The unit's env names its own UA_HOME, so the host's real settings.toml
    # (which may set a sync target) cannot leak into the check.
    monkeypatch.setattr(doctor, "_service_env", lambda *a: {"UA_HOME": str(tmp_path)})
    monkeypatch.setattr(
        vault_sync, "read_state", lambda *a: {"last_success_utc": doctor.storage.now(), "ok": True}
    )
    result = doctor._vault_sync_check()
    assert result["status"] == "MISCONFIGURED"
    assert "UA_VAULT_SYNC_REMOTE_HOST" in result["detail"]


def test_vault_sync_target_getters_have_no_built_in_destination(monkeypatch):
    monkeypatch.delenv("UA_VAULT_SYNC_REMOTE_HOST", raising=False)
    monkeypatch.delenv("UA_VAULT_SYNC_REMOTE_ROOT", raising=False)
    assert config.vault_sync_remote_host() == config.vault_sync_remote_root() == ""
    explicit = {"UA_VAULT_SYNC_REMOTE_HOST": "vault-host", "UA_VAULT_SYNC_REMOTE_ROOT": "vault"}
    assert config.vault_sync_remote_host(explicit) == "vault-host"
    assert config.vault_sync_remote_root(explicit) == "vault"


def test_hook_without_ruff_skips_without_retaining_payload(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "privacy_hook", ROOT / "scripts/hook_post_write.py"
    )
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    hook.__file__ = str(tmp_path / "scripts/hook_post_write.py")
    source = tmp_path / "sample.py"
    source.write_text("secret = 'private fixture'\n")
    payload = json.dumps({"tool_input": {"file_path": str(source), "content": "private fixture"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.delenv("UA_RUFF_BIN", raising=False)
    monkeypatch.setattr(hook.shutil, "which", lambda *a: None)
    monkeypatch.setattr(hook.subprocess, "run", no_io)
    hook.main()
    assert not (tmp_path / "hook_post_write_debug.log").exists()


def fake_tools(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "called"
    for name in ("uv", "curl", "git", "gcloud"):
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\nexit 77\n")
        path.chmod(0o755)
    # Preserve root refusal while making these tests usable in root CI containers.
    path = bin_dir / "id"
    path.write_text("#!/bin/sh\necho 1000\n")
    path.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("UA_")}
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["HOME"] = str(tmp_path)
    return env, marker


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        [
            "--repo",
            "https://" + "fixture-user:fixture-password" + chr(64) + "github.com/example/project",
        ],
        ["--local", "/missing-checkout"],
    ],
)
def test_installer_rejects_missing_or_private_source_before_tools(tmp_path, arguments):
    env, marker = fake_tools(tmp_path)
    proc = subprocess.run(
        ["bash", str(ROOT / "install.sh"), *arguments], env=env, capture_output=True, text=True
    )
    assert proc.returncode != 0
    assert not marker.exists()
    assert "fixture-user:fixture-password" not in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "values",
    [
        {},
        {
            "UA_GCP_PROJECT": "example-project",
            "UA_BUILD_REGION": "us-central1",
            "UA_IMAGE_REPOSITORY": "us-central1-docker.pkg.dev/wrong-project/repo/image",
        },
    ],
)
def test_image_publication_rejects_absent_or_mismatched_targets_before_tools(tmp_path, values):
    env, marker = fake_tools(tmp_path)
    env.update(values)
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts/publish-image.sh"), "v0.3.2"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize(
    "path",
    [
        "releases/..",
        "releases/.",
        "releases/agent/",
        "releases//agent",
        "releases/.agent",
        "releases/agent..v2",
        "releases/agent/.hidden",
        "releases/agent__",
        "releases/agent---",
        "releases/agent/..",
    ],
)
def test_image_publication_rejects_invalid_path_before_tools(tmp_path, path):
    env, marker = fake_tools(tmp_path)
    env.update(
        UA_GCP_PROJECT="example-project",
        UA_BUILD_REGION="us-central1",
        UA_IMAGE_REPOSITORY=f"us-central1-docker.pkg.dev/example-project/{path}",
    )
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts/publish-image.sh"), "v0.3.2"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize(
    "path", ["releases/agent", "releases/team/my_agent-name.v2", "releases/agent__v2"]
)
def test_configured_image_publication_passes_exact_targets_and_tag(tmp_path, path):
    env, marker = fake_tools(tmp_path)
    bin_dir = tmp_path / "bin"
    args_file = tmp_path / "arguments"
    # Only git archive produces a stream; no checkout or network is used.
    (bin_dir / "git").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *rev-parse*) echo "{ROOT}" ;;\n'
        "  *fetch*) exit 0 ;;\n"
        "  *rev-list*) echo abc123 ;;\n"
        "  *archive*) tar -cf - -T /dev/null ;;\n"
        "  *) exit 77 ;;\n"
        "esac\n"
    )
    (bin_dir / "gcloud").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args_file}"\n')
    env.update(
        UA_GCP_PROJECT="example-project",
        UA_BUILD_REGION="us-central1",
        UA_IMAGE_REPOSITORY=f"us-central1-docker.pkg.dev/example-project/{path}",
        UA_REPO_URL="https://github.com/example/project",
    )
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts/publish-image.sh"), "v0.3.2"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    args = args_file.read_text().splitlines()
    assert "--project=example-project" in args
    assert "--region=us-central1" in args
    substitutions = next(value for value in args if value.startswith("--substitutions="))
    assert f"_IMAGE=us-central1-docker.pkg.dev/example-project/{path}" in substitutions
    assert "_SOURCE_URL=https://github.com/example/project" in substitutions
    assert "_TAG=v0.3.2,_VERSION=0.3.2,_VCS_REF=abc123" in substitutions
    assert not Path(args[2]).exists()  # disposable archive removed by the exit trap
    assert not marker.exists()


def test_hook_uses_explicit_ruff_and_preserves_fix_arguments(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "configured_hook", ROOT / "scripts/hook_post_write.py"
    )
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    hook.__file__ = str(tmp_path / "scripts/hook_post_write.py")
    tool = tmp_path / "custom-ruff"
    tool.touch()
    source = tmp_path / "sample.py"
    source.touch()
    monkeypatch.setenv("UA_RUFF_BIN", str(tool))
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"toolCall": {"args": {"TargetFile": str(source)}}}))
    )
    called = []
    monkeypatch.setattr(hook.subprocess, "run", lambda args, **kwargs: called.append(args))
    with pytest.raises(SystemExit) as stopped:
        hook.main()
    assert stopped.value.code == 0
    assert called == [[str(tool), "check", "--fix", str(source)]]
    assert not (tmp_path / "hook_post_write_debug.log").exists()


@pytest.mark.parametrize("value", [None, "", " "])
def test_unconfigured_vault_does_not_discover_personal_notes(monkeypatch, value):
    from unified_agent.safety import ToolError, resolve_in_vault
    from unified_agent.tools import vault

    if value is None:
        monkeypatch.delenv("UA_VAULT_PATH", raising=False)
    else:
        monkeypatch.setenv("UA_VAULT_PATH", value)
    assert config.vault_root() is None
    monkeypatch.setattr(vault.trust_presets, "check_path", no_io)
    with pytest.raises(ToolError, match="disabled"):
        resolve_in_vault("Wiki/private.md")
    with pytest.raises(ToolError, match="disabled"):
        vault.vault_read("Wiki/private.md")
    with pytest.raises(ToolError, match="disabled"):
        vault.vault_briefing_digest()


def test_sync_doctor_uses_typed_service_settings(monkeypatch, tmp_path):
    service_home = tmp_path / "sync-install"
    service_home.mkdir()
    (service_home / "settings.toml").write_text(
        'schema_version = 1\n[paths]\nvault = "~/configured-vault"\n'
        '[vault_sync]\nremote_host = "vault-host"\nremote_root = "vault"\n'
    )
    monkeypatch.setattr(doctor, "_service_probe", lambda *a: {"LoadState": "loaded"})
    monkeypatch.setattr(doctor, "_service_env", lambda *a: {"UA_HOME": str(service_home)})
    monkeypatch.setattr(vault_sync, "read_state", lambda *a: None)
    monkeypatch.delenv("UA_VAULT_SYNC_REMOTE_HOST", raising=False)
    monkeypatch.delenv("UA_VAULT_SYNC_REMOTE_ROOT", raising=False)
    result = doctor._vault_sync_check()
    assert result["status"] == "UNKNOWN"
    assert "has not completed a run" in result["detail"]
    assert "UA_VAULT_SYNC_REMOTE_HOST" not in os.environ


def test_sync_doctor_unknown_service_does_not_borrow_caller_target(monkeypatch):
    monkeypatch.setattr(doctor, "_service_probe", lambda *a: {"LoadState": "loaded"})
    monkeypatch.setattr(doctor, "_service_env", lambda *a: None)
    monkeypatch.setattr(vault_sync, "read_state", no_io)
    monkeypatch.setenv("UA_VAULT_SYNC_REMOTE_HOST", "caller-host")
    monkeypatch.setenv("UA_VAULT_SYNC_REMOTE_ROOT", "caller-vault")
    result = doctor._vault_sync_check()
    assert result["status"] == "UNKNOWN"
    assert "cannot determine" in result["detail"]


def test_vault_sync_missing_local_target_does_not_write_state(monkeypatch):
    monkeypatch.setenv("UA_VAULT_SYNC_REMOTE_HOST", "vault-host")
    monkeypatch.setenv("UA_VAULT_SYNC_REMOTE_ROOT", "vault")
    monkeypatch.delenv("UA_VAULT_SYNC_LOCAL_ROOT", raising=False)
    monkeypatch.delenv("UA_VAULT_PATH", raising=False)
    monkeypatch.setattr(vault_sync.config, "vault_sync_state_path", no_io)
    monkeypatch.setattr(vault_sync, "SSHTransport", no_io)
    assert vault_sync.run()["status"] == "unconfigured"


def test_sync_doctor_cannot_certify_success_without_local_target(monkeypatch):
    monkeypatch.setattr(doctor, "_service_probe", lambda *a: {"LoadState": "loaded"})
    monkeypatch.setattr(
        doctor,
        "_service_env",
        lambda *a: {
            "UA_VAULT_SYNC_REMOTE_HOST": "vault-host",
            "UA_VAULT_SYNC_REMOTE_ROOT": "vault",
            "UA_SETTINGS_FILE": "",
        },
    )
    monkeypatch.setattr(
        vault_sync, "read_state", lambda *a: {"last_success_utc": doctor.storage.now(), "ok": True}
    )
    result = doctor._vault_sync_check()
    assert result["status"] == "MISCONFIGURED"
    assert "UA_VAULT_SYNC_LOCAL_ROOT" in result["detail"]
