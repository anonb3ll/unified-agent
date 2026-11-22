import json
import subprocess
import sys
from pathlib import Path

import pytest

from unified_agent import scaffold_cli
from unified_agent.scaffold_cli import find_sibling_binary, main, validate_name, write_mcp_json


def test_find_sibling_binary_finds_current_interpreter():
    assert find_sibling_binary(Path(sys.executable).name) == Path(sys.executable)


def test_find_sibling_binary_missing():
    assert find_sibling_binary("definitely-not-a-real-binary-xyz") is None


@pytest.mark.parametrize("name", ["", "   ", ".", "..", "a/b", "a\\b", "../escape"])
def test_validate_name_rejects_unsafe_names(name):
    assert validate_name(name) is not None


@pytest.mark.parametrize("name", ["myagent", "my-agent", "my_agent2", "a"])
def test_validate_name_accepts_safe_names(name):
    assert validate_name(name) is None


def test_write_mcp_json(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    server_name = write_mcp_json(target, "proj", Path("/fake/bin/unified-agent"))
    assert server_name == "unified-agent-proj"

    config = json.loads((target / ".mcp.json").read_text())
    entry = config["mcpServers"]["unified-agent-proj"]
    assert entry["command"] == "/fake/bin/unified-agent"
    assert entry["env"]["UA_WORKSPACE"] == str(target)


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    target = tmp_path / "newproj"
    monkeypatch.setattr(
        sys, "argv", ["unified-agent-new-project", "newproj", "--path", str(target), "--dry-run"]
    )

    main()

    captured = capsys.readouterr()
    assert "Would create" in captured.out
    assert "Would write" in captured.out
    assert not target.exists()


def test_main_refuses_to_overwrite_existing_mcp_json(tmp_path, monkeypatch):
    target = tmp_path / "existing"
    target.mkdir()
    (target / ".mcp.json").write_text("{}")
    monkeypatch.setattr(
        sys, "argv", ["unified-agent-new-project", "existing", "--path", str(target)]
    )

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1


def test_main_creates_project_and_wires_mcp(isolated_env, monkeypatch, tmp_path):
    target = tmp_path / "brandnew"
    monkeypatch.setattr(
        sys,
        "argv",
        ["unified-agent-new-project", "brandnew", "--path", str(target), "--client", "claude"],
    )

    main()

    assert target.is_dir()
    assert (target / ".git").is_dir()

    config = json.loads((target / ".mcp.json").read_text())
    entry = config["mcpServers"]["unified-agent-brandnew"]
    assert entry["env"]["UA_WORKSPACE"] == str(target)

    # unified-agent-sync ran as part of scaffolding and wrote the managed block
    claude_md = target / "CLAUDE.md"
    assert claude_md.exists()
    assert "ua-sync:begin" in claude_md.read_text()


def test_main_reuses_existing_directory(isolated_env, monkeypatch, tmp_path):
    target = tmp_path / "already-here"
    target.mkdir()
    (target / "README.md").write_text("pre-existing content\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["unified-agent-new-project", "already-here", "--path", str(target), "--client", "claude"],
    )

    main()

    # existing content untouched, no git re-init over an existing plain dir's content
    assert (target / "README.md").read_text() == "pre-existing content\n"
    assert (target / ".mcp.json").exists()


def test_main_requires_explicit_client_before_scaffold_mutation(monkeypatch, tmp_path, capsys):
    target = tmp_path / "identity-required"

    monkeypatch.setattr(scaffold_cli, "find_sibling_binary", lambda name: Path(sys.executable))
    monkeypatch.setattr(scaffold_cli, "detect_client", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["unified-agent-new-project", "identity-required", "--path", str(target)],
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert not target.exists()
    assert "explicit --client" in capsys.readouterr().err


def test_main_passes_reliably_detected_client_to_sync(monkeypatch, tmp_path):
    target = tmp_path / "detected-client"
    calls = []

    monkeypatch.setattr(scaffold_cli, "find_sibling_binary", lambda name: Path(sys.executable))
    monkeypatch.setattr(scaffold_cli, "detect_client", lambda: "claude")
    monkeypatch.setattr(
        scaffold_cli.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["unified-agent-new-project", "detected-client", "--path", str(target)],
    )

    main()

    sync_call = next(call for call in calls if "--path" in call)
    assert sync_call[sync_call.index("--client") + 1] == "claude"


def test_main_rejects_unsafe_name(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["unified-agent-new-project", "a/b"])

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1


def test_main_target_exists_as_plain_file(tmp_path, monkeypatch):
    target = tmp_path / "not-a-dir"
    target.write_text("i am a file, not a directory\n")

    monkeypatch.setattr(
        sys, "argv", ["unified-agent-new-project", "not-a-dir", "--path", str(target)]
    )

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1


def test_main_missing_ua_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold_cli, "find_sibling_binary", lambda name: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["unified-agent-new-project", "newproj", "--path", str(tmp_path / "newproj")],
    )

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1


def test_main_missing_sync_binary_still_scaffolds(monkeypatch, tmp_path, capsys):
    """No unified-agent-sync alongside the interpreter: scaffold proceeds, warns, skips sync."""
    target = tmp_path / "no-sync"

    def fake_find(name):
        if name == "unified-agent":
            return Path(sys.executable)  # any real, executable path works as a stand-in
        return None

    monkeypatch.setattr(scaffold_cli, "find_sibling_binary", fake_find)
    monkeypatch.setattr(
        sys, "argv", ["unified-agent-new-project", "no-sync", "--path", str(target)]
    )

    main()

    assert target.is_dir()
    assert (target / ".mcp.json").exists()
    assert not (target / "CLAUDE.md").exists()  # sync never ran, no managed block written
    captured = capsys.readouterr()
    assert "skipping context sync" in captured.err


def test_main_git_init_failure_exits_cleanly(monkeypatch, tmp_path):
    target = tmp_path / "git-fails"

    monkeypatch.setattr(scaffold_cli, "find_sibling_binary", lambda name: Path(sys.executable))

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "init"]:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(scaffold_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["unified-agent-new-project", "git-fails", "--path", str(target), "--client", "claude"],
    )

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1


def test_main_sync_failure_exits_cleanly(monkeypatch, tmp_path):
    target = tmp_path / "sync-fails"

    monkeypatch.setattr(scaffold_cli, "find_sibling_binary", lambda name: Path(sys.executable))

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "init"]:
            return subprocess.CompletedProcess(args, 0)
        raise subprocess.CalledProcessError(1, args)  # the sync invocation

    monkeypatch.setattr(scaffold_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["unified-agent-new-project", "sync-fails", "--path", str(target), "--client", "claude"],
    )

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
    # the directory and .mcp.json were already written before the failing sync call
    assert (target / ".mcp.json").exists()
