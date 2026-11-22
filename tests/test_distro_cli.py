"""Tests for ``unified-agent-distro`` (task #1733)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from export_guard import is_public_export

from unified_agent import distro_cli
from unified_agent.audit.rules import check_mcp_config
from unified_agent.distro_cli import FILE_SPECS, HARNESSES, main

HUB_URL = "http://hub.invalid:8765/mcp"

COMMON = {
    "AGENTS.md",
    ".unified-agent/operating.yaml",
    ".agents/skills/hub-session-start/SKILL.md",
}
PER_HARNESS = {
    "claude": {
        "CLAUDE.md",
        ".claude/skills/hub-session-start/SKILL.md",
        ".unified-agent/bin/ua-register-claude",
    },
    "codex": {".codex/config.toml"},
    "cursor": {".cursor/mcp.json"},
    "gemini": {"GEMINI.md"},
    "opencode": {"opencode.json", ".unified-agent/bin/ua-opencode"},
    "copilot": {".vscode/mcp.json", ".unified-agent/bin/ua-copilot"},
}


# Built by concatenation so no literal credential URL sits in this file.
_FAKE_PASS = "s3cret" + "pass"


def _fake_token() -> str:
    # Built by concatenation so no literal token sits in this file.
    return "ua_" + "Q7x" * 4 + "m2Kp" * 4 + "9"


@pytest.fixture(autouse=True)
def _hub_url(monkeypatch):
    monkeypatch.setenv("UA_HTTP_HUB_URL", HUB_URL)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "project"
    target.mkdir()
    return target


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _snapshot(root: Path) -> dict[str, bytes]:
    return {rel: (root / rel).read_bytes() for rel in _files(root)}


# --- init -------------------------------------------------------------------------


def test_dry_run_writes_nothing(project: Path, capsys):
    assert main(["init", str(project)]) == 0
    assert _files(project) == set()
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "create     AGENTS.md" in out
    assert "--write" in out


def test_spec_table_covers_every_harness():
    assert {s.harness for s in FILE_SPECS} - {None} == set(HARNESSES)
    assert {s.dest for s in FILE_SPECS} == COMMON.union(*PER_HARNESS.values())


@pytest.mark.parametrize("harness", HARNESSES)
def test_write_creates_expected_tree_per_harness(project: Path, harness: str):
    assert main(["init", str(project), "--harness", harness, "--write"]) == 0
    assert _files(project) == COMMON | PER_HARNESS[harness]
    for rel in PER_HARNESS[harness]:
        if rel.startswith(".unified-agent/bin/"):
            path = project / rel
            assert os.access(path, os.X_OK)
            subprocess.run(["bash", "-n", str(path)], check=True)


def test_write_all_harnesses_and_rendered_shapes(project: Path):
    assert main(["init", str(project), "--write", "--profile", "careful"]) == 0
    assert _files(project) == COMMON.union(*PER_HARNESS.values())

    assert "operating_profile: careful" in (project / ".unified-agent/operating.yaml").read_text()

    cursor = json.loads((project / ".cursor/mcp.json").read_text())
    entry = cursor["mcpServers"]["unified-agent"]
    assert entry["url"] == HUB_URL
    assert entry["headers"]["Authorization"] == "Bearer ${env:UA_CURSOR_HUB_TOKEN}"

    codex = tomllib.loads((project / ".codex/config.toml").read_text())
    server = codex["mcp_servers"]["unified-agent"]
    assert server == {"url": HUB_URL, "bearer_token_env_var": "UA_AGENT_BEARER_TOKEN"}

    opencode = json.loads((project / "opencode.json").read_text())
    oc = opencode["mcp"]["unified-agent"]
    assert oc["type"] == "remote" and oc["url"] == HUB_URL
    assert oc["headers"]["Authorization"] == "Bearer {env:UA_AGENT_BEARER_TOKEN}"

    vscode = json.loads((project / ".vscode/mcp.json").read_text())
    vs = vscode["servers"]["unified-agent"]
    assert vs["type"] == "http" and vs["url"] == HUB_URL
    assert vs["headers"]["Authorization"] == "Bearer ${env:UA_AGENT_BEARER_TOKEN}"

    register = (project / ".unified-agent/bin/ua-register-claude").read_text()
    assert "mcp add --scope user --transport http" in register
    assert "UA_AGENT_BEARER_TOKEN" in register

    assert "@AGENTS.md" in (project / "CLAUDE.md").read_text()
    assert "@./AGENTS.md" in (project / "GEMINI.md").read_text()
    skill = (project / ".agents/skills/hub-session-start/SKILL.md").read_text()
    assert skill.startswith("---\nname: hub-session-start\ndescription: ")
    # Claude's skill copy is the same text, so check reports either drifting.
    assert skill == (project / ".claude/skills/hub-session-start/SKILL.md").read_text()

    # No project .mcp.json: it would shadow the user-scope hub registration.
    assert not (project / ".mcp.json").exists()


def test_harness_filter_accepts_comma_list(project: Path):
    assert main(["init", str(project), "--harness", "codex,cursor", "--write"]) == 0
    assert _files(project) == COMMON | PER_HARNESS["codex"] | PER_HARNESS["cursor"]


def test_unknown_harness_is_rejected(project: Path, capsys):
    assert main(["init", str(project), "--harness", "notepad"]) == 2
    assert "unknown harness" in capsys.readouterr().err


def test_second_run_is_idempotent(project: Path, capsys):
    assert main(["init", str(project), "--write"]) == 0
    before = _snapshot(project)
    capsys.readouterr()
    assert main(["init", str(project), "--write"]) == 0
    out = capsys.readouterr().out
    assert "Wrote 0 file(s)" in out
    assert _snapshot(project) == before
    assert main(["init", str(project)]) == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_existing_different_file_is_a_conflict_and_nothing_is_written(project: Path, capsys):
    (project / "AGENTS.md").write_text("# my own rules\n")
    (project / ".cursor").mkdir()
    (project / ".cursor/mcp.json").write_text('{"mcpServers": {}}\n')

    assert main(["init", str(project), "--write"]) == 1
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err
    assert "AGENTS.md" in err and ".cursor/mcp.json" in err
    # Refusal is all-or-nothing: no other file was created either.
    assert _files(project) == {"AGENTS.md", ".cursor/mcp.json"}
    assert (project / "AGENTS.md").read_text() == "# my own rules\n"

    # The dry run reports the same conflicts.
    assert main(["init", str(project)]) == 1
    assert "CONFLICT" in capsys.readouterr().out


def test_force_overwrites_conflicts_without_backups(project: Path):
    (project / "AGENTS.md").write_text("# my own rules\n")
    assert main(["init", str(project), "--write", "--force"]) == 0
    assert (project / "AGENTS.md").read_text().startswith("# Agent constitution")
    assert not any(p.endswith((".bak", ".distro-tmp", ".distro-orig")) for p in _files(project))


def test_symlinked_directory_is_refused(project: Path, tmp_path: Path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".cursor").symlink_to(outside, target_is_directory=True)

    assert main(["init", str(project), "--write"]) == 2
    assert "symlink" in capsys.readouterr().err
    assert list(outside.iterdir()) == []
    assert _files(project) == set()


def test_symlinked_file_is_refused(project: Path, tmp_path: Path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")
    (project / "AGENTS.md").symlink_to(victim)
    assert main(["init", str(project), "--write", "--force"]) == 2
    assert victim.read_text() == "keep me\n"


def test_safe_dest_rejects_traversal(project: Path):
    for bad in ("../escape.md", "/etc/passwd", "a/../../b", ""):
        with pytest.raises(distro_cli.DistroError):
            distro_cli.safe_dest(project, bad)


def test_missing_target_is_refused(tmp_path: Path, capsys):
    assert main(["init", str(tmp_path / "nope")]) == 2
    assert "does not exist" in capsys.readouterr().err


@pytest.mark.parametrize("profile", ["Bad Name", "../x", "x" * 65, "a:b"])
def test_invalid_profile_is_refused(project: Path, profile: str):
    assert main(["init", str(project), "--profile", profile]) == 2


@pytest.mark.parametrize(
    "url",
    [
        'http://h/mcp"; rm -rf /',
        "ftp://h/mcp",
        "http://h/$X/mcp",
        "http:///mcp",
        "https://agent:" + _FAKE_PASS + "@hub.invalid/mcp",
        "https://agent" + "@hub.invalid/mcp",
        "https://hub.invalid/mcp?token=s3cretpass",
        "https://hub.invalid/mcp#s3cretpass",
    ],
)
def test_unsafe_hub_url_is_refused_without_echoing_it(project: Path, url: str, capsys):
    assert main(["init", str(project), "--hub-url", url]) == 2
    assert main(["init", str(project), "--hub-url", url, "--write"]) == 2
    assert _files(project) == set()
    captured = capsys.readouterr()
    assert "s3cretpass" not in captured.out + captured.err
    assert url not in captured.out + captured.err


def test_hub_url_with_userinfo_from_environment_is_refused(project: Path, monkeypatch, capsys):
    monkeypatch.setenv("UA_HTTP_HUB_URL", "https://agent:" + _FAKE_PASS + "@hub.invalid/mcp")
    assert main(["init", str(project)]) == 2
    captured = capsys.readouterr()
    assert "UA_HTTP_HUB_URL" in captured.err
    assert "s3cretpass" not in captured.out + captured.err


# --- no hub URL configured ------------------------------------------------------------


def test_write_without_hub_url_is_refused(project: Path, monkeypatch, capsys):
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    assert main(["init", str(project), "--write"]) == 2
    assert "no hub URL" in capsys.readouterr().err
    assert _files(project) == set()


def test_dry_run_without_hub_url_shows_placeholder(project: Path, monkeypatch, capsys):
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    assert main(["init", str(project)]) == 0
    out = capsys.readouterr().out
    assert distro_cli.HUB_URL_UNSET in out and "required for --write" in out
    assert "127.0.0.1" not in out and "localhost" not in out
    assert _files(project) == set()


def test_gemini_only_write_needs_no_hub_url(project: Path, monkeypatch):
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    assert main(["init", str(project), "--harness", "gemini", "--write"]) == 0
    assert _files(project) == COMMON | PER_HARNESS["gemini"]


def test_check_without_hub_url_skips_drift_only(project: Path, monkeypatch, capsys):
    assert main(["init", str(project), "--harness", "cursor", "--write"]) == 0
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    capsys.readouterr()
    assert main(["check", str(project), "--harness", "cursor"]) == 0
    assert "hub URL unset" in capsys.readouterr().out
    (project / ".cursor/mcp.json").unlink()
    assert main(["check", str(project), "--harness", "cursor"]) == 1
    assert "missing" in capsys.readouterr().out


# --- all-or-nothing writes ------------------------------------------------------------


def test_parent_that_is_a_file_blocks_without_partial_write(project: Path, capsys):
    (project / ".cursor").write_text("not a directory\n")
    for extra in ([], ["--force"]):
        assert main(["init", str(project), "--harness", "cursor", "--write", *extra]) == 1
        err = capsys.readouterr().err
        assert "blocked" in err and ".cursor/mcp.json" in err
        assert _files(project) == {".cursor"}
    assert (project / ".cursor").read_text() == "not a directory\n"
    # The dry run names the blocker too.
    assert main(["init", str(project), "--harness", "cursor"]) == 1
    assert "BLOCKED" in capsys.readouterr().out


def test_destination_that_is_a_directory_blocks(project: Path, capsys):
    (project / "AGENTS.md").mkdir()
    assert main(["init", str(project), "--write", "--force"]) == 1
    assert "AGENTS.md is a directory" in capsys.readouterr().err
    assert _files(project) == set()


def test_staging_failure_leaves_nothing_behind(project: Path, monkeypatch, capsys):
    real_mkstemp = distro_cli.tempfile.mkstemp
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError(28, "No space left on device")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(distro_cli.tempfile, "mkstemp", flaky)
    assert main(["init", str(project), "--write"]) == 2
    assert "nothing was written" in capsys.readouterr().err
    assert list(project.iterdir()) == []


def test_planted_temp_name_symlink_is_not_followed(project: Path, tmp_path: Path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")
    # The name an earlier version used for its fixed temp file.
    planted = project / ".AGENTS.md.distro-tmp"
    planted.symlink_to(victim)
    assert main(["init", str(project), "--write"]) == 0
    assert victim.read_text() == "keep me\n"
    assert planted.is_symlink()
    assert (project / "AGENTS.md").read_text().startswith("# Agent constitution")
    # Only the planted link remains; no staged temp file is left behind.
    leftovers = [p for p in _files(project) if p.endswith(".distro-tmp")]
    assert leftovers == [".AGENTS.md.distro-tmp"]


def test_hub_url_flag_overrides_environment(project: Path):
    other = "https://hub.example.invalid/mcp"
    assert main(["init", str(project), "--harness", "codex", "--hub-url", other, "--write"]) == 0
    assert other in (project / ".codex/config.toml").read_text()


# --- rendered text hygiene ----------------------------------------------------------


def test_no_literal_secret_in_any_rendered_file(project: Path):
    assert main(["init", str(project), "--write"]) == 0
    for rel in _files(project):
        text = (project / rel).read_text()
        assert distro_cli.scan_secrets(rel, text) == [], rel
        # Every bearer reference is an interpolation, never a value.
        for m in re.finditer(r"Bearer (\S+)", text):
            # ua-copilot's printf fills %s from the token file at launch.
            assert m.group(1).startswith(("${", "{env:", "$UA_", "%s")), (rel, m.group(0))
    files = [project / rel for rel in _files(project)]
    assert list(check_mcp_config(project, files)) == []


def test_templates_carry_no_personal_or_host_details():
    # The public export scrubs the owner's name and machine out of this file
    # too (into ordinary words), so it checks only the generic markers there.
    personal = "" if is_public_export(Path(__file__).resolve().parents[1]) else r"|operator|mac"
    banned = re.compile(
        r"(?i)/home/|/Users/|anonb3ll|tailnet|ts\.net|localhost"
        + personal
        + r"|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
    )
    # Fleet label prefixes are case-sensitive (SERVER_NAME is fine).
    labels = re.compile(r"\b(?:Desktop|Laptop|server|gcp|cloud)_[A-Za-z]")
    for path in distro_cli.template_dir().iterdir():
        text = path.read_text()
        for rx in (banned, labels):
            hit = rx.search(text)
            assert hit is None, (path.name, hit.group(0) if hit else None)


def test_template_sources_avoid_red_tier_names():
    from unified_agent.reviewer import _red_tier_re

    for path in distro_cli.template_dir().iterdir():
        rel = f"src/unified_agent/templates/distro/{path.name}"
        assert not _red_tier_re.search(rel), rel


# --- check ------------------------------------------------------------------------


def test_check_clean_after_init(project: Path, capsys):
    assert main(["init", str(project), "--write", "--profile", "careful"]) == 0
    capsys.readouterr()
    # check reads the profile back from operating.yaml.
    assert main(["check", str(project)]) == 0
    assert "ok:" in capsys.readouterr().out


def test_check_reports_missing_files(project: Path, capsys):
    assert main(["init", str(project), "--harness", "codex", "--write"]) == 0
    capsys.readouterr()
    assert main(["check", str(project), "--harness", "codex,cursor"]) == 1
    out = capsys.readouterr().out
    assert "missing" in out and ".cursor/mcp.json" in out


def test_check_reports_out_of_date_and_not_executable(project: Path, capsys):
    assert main(["init", str(project), "--harness", "opencode", "--write"]) == 0
    with (project / "AGENTS.md").open("a") as fh:
        fh.write("\nLocal addition.\n")
    (project / ".unified-agent/bin/ua-opencode").chmod(0o644)
    capsys.readouterr()
    assert main(["check", str(project), "--harness", "opencode"]) == 1
    out = capsys.readouterr().out
    assert "out-of-date" in out and "AGENTS.md" in out
    assert "not-executable" in out

    # init repairs the executable bit without a conflict.
    (project / "AGENTS.md").write_text(
        distro_cli.render("constitution.md.tmpl", distro_cli.render_values("standard", HUB_URL))
    )
    assert main(["init", str(project), "--harness", "opencode", "--write"]) == 0
    assert os.access(project / ".unified-agent/bin/ua-opencode", os.X_OK)


def test_check_flags_literal_hub_token_without_printing_it(project: Path, capsys):
    assert main(["init", str(project), "--harness", "cursor", "--write"]) == 0
    token = _fake_token()
    path = project / ".cursor/mcp.json"
    data = json.loads(path.read_text())
    data["mcpServers"]["unified-agent"]["headers"]["Authorization"] = "Bearer " + token
    path.write_text(json.dumps(data, indent=2) + "\n")
    capsys.readouterr()

    assert main(["check", str(project), "--harness", "cursor"]) == 1
    out = capsys.readouterr().out
    assert "secret" in out
    assert token not in out
    assert token[:6] in out  # masked prefix only


def test_check_flags_literal_bearer_in_codex_toml(project: Path, capsys):
    assert main(["init", str(project), "--harness", "codex", "--write"]) == 0
    secret = "Zq" + "8vLm" * 5
    with (project / ".codex/config.toml").open("a") as fh:
        fh.write(f'bearer_token = "{secret}"\n')
    capsys.readouterr()
    assert main(["check", str(project), "--harness", "codex"]) == 1
    out = capsys.readouterr().out
    assert "secret" in out and secret not in out


def test_check_scans_unmanaged_mcp_config_and_flags_shadowing(project: Path, capsys):
    assert main(["init", str(project), "--harness", "claude", "--write"]) == 0
    token = _fake_token()
    (project / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "unified-agent": {
                        "type": "http",
                        "url": HUB_URL,
                        "headers": {"Authorization": "Bearer " + token},
                    }
                }
            }
        )
    )
    capsys.readouterr()
    assert main(["check", str(project), "--harness", "claude"]) == 1
    out = capsys.readouterr().out
    assert "shadow" in out
    assert "secret" in out and token not in out


def test_check_on_symlinked_managed_path_is_refused(project: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (project / ".codex").symlink_to(outside, target_is_directory=True)
    assert main(["check", str(project), "--harness", "codex"]) == 2


def test_malformed_ipv6_hub_url_is_refused(project: Path, capsys):
    assert main(["init", str(project), "--hub-url", "http://[bad]/mcp"]) == 2
    err = capsys.readouterr().err
    assert "plain http(s) URL" in err and "Traceback" not in err
    assert _files(project) == set()


def test_hub_url_help_promises_no_local_default(capsys):
    with pytest.raises(SystemExit):
        main(["init", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "local hub default" not in out
    assert "no local fallback" in out


def test_failed_move_rolls_back_replaced_and_new_files(project: Path, monkeypatch, capsys):
    original = "# my own rules\n"
    (project / "AGENTS.md").write_text(original)
    (project / "AGENTS.md").chmod(0o600)
    real_replace = distro_cli.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:  # AGENTS.md and operating.yaml are already in place
            raise OSError(5, "Input/output error")
        return real_replace(src, dst)

    monkeypatch.setattr(distro_cli.os, "replace", flaky)
    assert main(["init", str(project), "--write", "--force"]) == 2
    err = capsys.readouterr().err
    assert "rolled back, nothing was changed" in err
    assert (project / "AGENTS.md").read_text() == original
    assert (project / "AGENTS.md").stat().st_mode & 0o777 == 0o600
    # New files and the directories this run created are gone; no temp files remain.
    assert sorted(p.name for p in project.iterdir()) == ["AGENTS.md"]


# --- review round 3 --------------------------------------------------------------------


def _fake_bin(dir_: Path, name: str, body: str) -> Path:
    path = dir_ / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def test_double_failure_keeps_unrestored_backup_and_reports_it(project: Path, monkeypatch, capsys):
    original = "# my own rules\n"
    (project / "AGENTS.md").write_text(original)
    real_replace = distro_cli.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:  # third commit move fails...
            raise OSError(5, "Input/output error")
        if calls["n"] == 4:  # ...and restoring AGENTS.md from its backup fails too
            raise PermissionError(13, "Permission denied")
        return real_replace(src, dst)

    monkeypatch.setattr(distro_cli.os, "replace", flaky)
    assert main(["init", str(project), "--write", "--force"]) == 2
    err = capsys.readouterr().err
    backups = [p for p in project.iterdir() if p.name.endswith(".distro-orig")]
    assert len(backups) == 1, backups
    assert backups[0].read_text() == original
    assert str(backups[0]) in err
    assert "rollback failed" in err and "AGENTS.md" in err


def _run_copilot_wrapper(project: Path, tmp_path: Path, **env_extra: str):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    report = tmp_path / "report.json"
    report.unlink(missing_ok=True)
    # The fake CLI reads its @file argument and records what it saw; it never
    # prints the token.
    fake = _fake_bin(
        tmp_path,
        "copilot",
        'cfg="${2#@}"\n'
        f'python3 - "$cfg" "$@" > "{report}" <<"PY"\n'
        "import json, os, sys\n"
        "cfg = sys.argv[1]\n"
        "data = json.load(open(cfg))\n"
        "print(json.dumps({'args': sys.argv[2:], 'cfg': cfg, 'config': data,"
        " 'env': dict(os.environ)}))\n"
        "PY\n",
    )
    env = {
        **os.environ,
        "COPILOT_BIN": str(fake),
        "XDG_RUNTIME_DIR": str(scratch),
        "TMPDIR": str(scratch),
    }
    for name in ("UA_DISTRO_HUB_URL_OVERRIDE", "UA_COPILOT_TOKEN_SOURCE", "UA_AGENT_BEARER_TOKEN"):
        env.pop(name, None)
    env.update(env_extra)
    res = subprocess.run(
        ["bash", "project/.unified-agent/bin/ua-copilot", "--version"],
        cwd=project.parent,
        env=env,
        capture_output=True,
        text=True,
    )
    return res, (json.loads(report.read_text()) if report.exists() else None), scratch


def _assert_token_not_in_env(env: dict, token: str) -> None:
    # Assert on names only: a failing assertion must not print the environment.
    names = ("UA_AGENT_BEARER_TOKEN", "UA_COPILOT_HUB_TOKEN", "UA_TOKEN", "config_json")
    present = [n for n in names if n in env]
    assert not present, present
    carrying = [k for k, v in env.items() if token in v or token in k]
    assert not carrying, len(carrying)


def test_copilot_wrapper_passes_config_through_process_substitution(project: Path, tmp_path: Path):
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    token = "test-only-" + "Hb7" * 6
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")

    res, seen, scratch = _run_copilot_wrapper(
        project, tmp_path, UA_COPILOT_TOKEN_FILE=str(token_file)
    )
    assert res.returncode == 0, res.stderr
    assert token not in res.stdout + res.stderr
    assert seen["args"][0] == "--additional-mcp-config"
    assert seen["args"][1] == "@" + seen["cfg"] and seen["args"][2] == "--version"
    # A pipe, not a file: nothing on disk, and neither the JSON nor the token is in argv.
    assert seen["cfg"].startswith("/dev/fd/")
    assert not any(token in a or "mcpServers" in a for a in seen["args"])
    entry = seen["config"]["mcpServers"]["unified-agent"]
    assert entry["type"] == "http" and entry["url"] == HUB_URL
    assert entry["headers"]["Authorization"] == "Bearer " + token
    assert list(scratch.iterdir()) == []
    _assert_token_not_in_env(seen["env"], token)  # not exported into Copilot's environment
    for rel in _files(project):
        assert token not in (project / rel).read_text(errors="replace"), rel


@pytest.mark.parametrize("source", ["file", "env"])
def test_copilot_wrapper_does_not_export_inherited_token_variables(
    project: Path, tmp_path: Path, source: str
):
    # A parent that exported UA_TOKEN / config_json (and the other token names)
    # must not make the wrapper's own assignments flow into Copilot's environment.
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    token = "test-only-" + "Qm4" * 6
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    inherited = {
        "UA_TOKEN": "inherited-placeholder",
        "config_json": "inherited-placeholder",
        "UA_COPILOT_HUB_TOKEN": "inherited-placeholder",
    }
    extra = {"UA_COPILOT_TOKEN_FILE": str(token_file), "UA_AGENT_BEARER_TOKEN": token}
    if source == "env":
        extra["UA_COPILOT_TOKEN_SOURCE"] = "env"
    res, seen, _ = _run_copilot_wrapper(project, tmp_path, **inherited, **extra)
    assert res.returncode == 0, res.stderr
    assert seen["config"]["mcpServers"]["unified-agent"]["headers"]["Authorization"] == (
        "Bearer " + token
    )
    _assert_token_not_in_env(seen["env"], token)
    assert "inherited-placeholder" not in json.dumps(seen["env"])
    # Nor is Copilot told where the bearer file lives.
    assert "UA_COPILOT_TOKEN_FILE" not in seen["env"]
    assert str(token_file) not in json.dumps(seen["env"])


def test_copilot_wrapper_execs_so_signals_reach_copilot(project: Path, tmp_path: Path):
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    token_file = tmp_path / "token"
    token_file.write_text("test-only-value\n")
    fake = _fake_bin(tmp_path, "copilot-sleeper", 'echo "$$"\nexec sleep 30\n')
    env = {**os.environ, "COPILOT_BIN": str(fake), "UA_COPILOT_TOKEN_FILE": str(token_file)}
    proc = subprocess.Popen(
        ["bash", str(project / ".unified-agent/bin/ua-copilot")],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        child_pid = int(proc.stdout.readline())
        assert child_pid == proc.pid  # the wrapper became Copilot: no shell in between
        proc.terminate()
        assert proc.wait(timeout=10) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()


def test_copilot_wrapper_never_falls_back_to_inherited_token(project: Path, tmp_path: Path):
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    missing = str(tmp_path / "no-such-token")

    # Default (file) mode: a missing token file is an error even when the
    # parent shell carries a token, which may belong to another agent.
    res, seen, _ = _run_copilot_wrapper(
        project, tmp_path, UA_COPILOT_TOKEN_FILE=missing, UA_AGENT_BEARER_TOKEN="test-inherited"
    )
    assert res.returncode == 1 and seen is None
    assert "missing or unreadable" in res.stderr and "UA_COPILOT_TOKEN_SOURCE=env" in res.stderr
    assert "test-inherited" not in res.stdout + res.stderr

    # Explicit env mode uses it.
    res, seen, _ = _run_copilot_wrapper(
        project,
        tmp_path,
        UA_COPILOT_TOKEN_FILE=missing,
        UA_COPILOT_TOKEN_SOURCE="env",
        UA_AGENT_BEARER_TOKEN="test-only-env",
    )
    assert res.returncode == 0, res.stderr
    auth = seen["config"]["mcpServers"]["unified-agent"]["headers"]["Authorization"]
    assert auth == "Bearer test-only-env"
    _assert_token_not_in_env(seen["env"], "test-only-env")  # even in env mode

    res, _, _ = _run_copilot_wrapper(project, tmp_path, UA_COPILOT_TOKEN_SOURCE="env")
    assert res.returncode == 1 and "UA_AGENT_BEARER_TOKEN is not set" in res.stderr
    res, _, _ = _run_copilot_wrapper(project, tmp_path, UA_COPILOT_TOKEN_SOURCE="other")
    assert res.returncode == 1

    res, _, _ = _run_copilot_wrapper(
        project,
        tmp_path,
        UA_COPILOT_TOKEN_SOURCE="env",
        UA_AGENT_BEARER_TOKEN='bad"token',
    )
    assert res.returncode == 1 and 'bad"token' not in res.stdout + res.stderr


@pytest.mark.parametrize(
    "override",
    [
        "https:///mcp",
        "https://:8765/mcp",
        "https://[]/mcp",
        "ftp://hub.invalid/mcp",
        "https://hub.invalid/mcp?x=1",
        "https://agent:" + _FAKE_PASS + "@hub.invalid/mcp",
        "https://[hub/mcp",
        "https://[::1]x/mcp",
        "https://[::1]:/mcp",
        "https://h]x/mcp",
        "https://[hub]/mcp",
        "https://[1:2:3]/mcp",
        "https://[1:2:3:4:5:6:7:8:9]/mcp",
        "https://[::1::2]/mcp",
        "https://[:::1]/mcp",
        "https://[:1::2]/mcp",
        "https://[::g]/mcp",
        "https://[1::2:]/mcp",
        "https://[::1.2.3]/mcp",
        "https://[1.2.3.4::]/mcp",
        "https://[fe80::1%25eth0]/mcp",
        "https://[::1]:abc/mcp",
        "https://[::1]:0/mcp",
        "https://[::1]:65536/mcp",
        "https://[::1]:123456/mcp",
        "https://hub.invalid:/mcp",
        "https://hub.invalid:abc/mcp",
        "https://hub.invalid:0/mcp",
        "https://hub.invalid:65536/mcp",
        "https://hub.invalid:99999/mcp",
        "https://hub.invalid:80:90/mcp",
        "https://999.1.1.1/mcp",
        "https://1.2.3/mcp",
        "https://01.2.3.4/mcp",
        "https://1.2.3.4.5/mcp",
        "https://hub..invalid/mcp",
        "https://.hub.invalid/mcp",
        "https://hu%62.invalid/mcp",
        "https://agent@hub.invalid/mcp",
        # Built at runtime: a literal URL with userinfo trips the pre-push
        # credential scanner even though it is a synthetic test value.
        "https://" + "@".join(("[::1]", "hub.invalid/mcp")),
    ],
)
def test_wrappers_refuse_bad_override_urls(project: Path, tmp_path: Path, override: str):
    assert main(["init", str(project), "--harness", "copilot,claude", "--write"]) == 0
    token_file = tmp_path / "token"
    token_file.write_text("test-only-value\n")
    res, seen, _ = _run_copilot_wrapper(
        project,
        tmp_path,
        UA_COPILOT_TOKEN_FILE=str(token_file),
        UA_DISTRO_HUB_URL_OVERRIDE=override,
    )
    assert res.returncode == 1 and seen is None
    assert "with a host" in res.stderr and _FAKE_PASS not in res.stdout + res.stderr
    res = _register(project, tmp_path, UA_DISTRO_HUB_URL_OVERRIDE=override)
    assert res.returncode == 1 and _FAKE_PASS not in res.stdout + res.stderr


GOOD_OVERRIDES = [
    "https://[::1]:9000/mcp",
    "https://[2001:db8::1]/mcp",
    "http://[::ffff:192.0.2.1]:80/mcp",
    "http://[1:2:3:4:5:6:7:8]:65535/mcp",
    "http://127.0.0.1:8765/mcp",
    "https://hub.example.com:443/mcp",
    "https://localhost",
    "https://a-b.c_d:1/x",
]


@pytest.mark.parametrize("other", GOOD_OVERRIDES)
def test_wrapper_accepts_good_override(project: Path, tmp_path: Path, other: str):
    assert main(["init", str(project), "--harness", "copilot,claude", "--write"]) == 0
    token_file = tmp_path / "token"
    token_file.write_text("test-only-value\n")
    res, seen, _ = _run_copilot_wrapper(
        project, tmp_path, UA_COPILOT_TOKEN_FILE=str(token_file), UA_DISTRO_HUB_URL_OVERRIDE=other
    )
    assert res.returncode == 0, res.stderr
    assert seen["config"]["mcpServers"]["unified-agent"]["url"] == other
    res = _register(project, tmp_path, UA_DISTRO_HUB_URL_OVERRIDE=other)
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines()[-1] == other


def _old_copilot_config(url: str = HUB_URL) -> str:
    entry = {
        "type": "http",
        "url": url,
        "headers": {"Authorization": "Bearer ${UA_AGENT_BEARER_TOKEN}"},
        "tools": ["*"],
    }
    return json.dumps({"mcpServers": {"unified-agent": entry}}, indent=2) + "\n"


def test_obsolete_copilot_config_is_reported_and_removed(project: Path, capsys):
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    old = project / ".unified-agent/copilot-mcp.json"
    old.write_text(_old_copilot_config("https://older-hub.invalid/mcp"))
    capsys.readouterr()

    assert main(["check", str(project), "--harness", "copilot"]) == 1
    assert "obsolete" in capsys.readouterr().out
    assert main(["init", str(project), "--harness", "copilot"]) == 0
    assert "remove     .unified-agent/copilot-mcp.json" in capsys.readouterr().out
    assert old.exists()  # dry run
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    assert not old.exists()
    assert main(["check", str(project), "--harness", "copilot"]) == 0


def test_user_file_at_obsolete_path_is_left_alone(project: Path, capsys):
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    mine = project / ".unified-agent/copilot-mcp.json"
    data = json.loads(_old_copilot_config())
    data["mcpServers"]["mine"] = {"type": "stdio", "command": "x"}
    mine.write_text(json.dumps(data))
    before = mine.read_text()
    assert main(["init", str(project), "--write"]) == 0
    assert mine.read_text() == before
    capsys.readouterr()
    assert main(["check", str(project), "--harness", "copilot"]) == 0


def test_failed_removal_is_rolled_back(project: Path, monkeypatch, capsys):
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    old = project / ".unified-agent/copilot-mcp.json"
    old.write_text(_old_copilot_config())
    (project / "AGENTS.md").unlink()  # one new file to commit before the removal
    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self == old:
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 2
    assert "rolled back" in capsys.readouterr().err
    assert old.read_text() == _old_copilot_config()
    assert not (project / "AGENTS.md").exists()


def test_force_creates_missing_server_table_and_keeps_content(project: Path):
    (project / ".codex").mkdir()
    (project / ".codex/config.toml").write_text('model = "some-model"\n')
    (project / "opencode.json").write_text('{"theme": "dark"}\n')
    args = ["init", str(project), "--harness", "codex,opencode", "--write"]
    assert main(args) == 1  # changing a user file still needs --force
    assert main([*args, "--force"]) == 0

    codex = tomllib.loads((project / ".codex/config.toml").read_text())
    assert codex["model"] == "some-model"
    assert codex["mcp_servers"]["unified-agent"]["url"] == HUB_URL
    opencode = json.loads((project / "opencode.json").read_text())
    assert opencode["theme"] == "dark"
    assert opencode["mcp"]["unified-agent"]["url"] == HUB_URL
    assert main(["check", str(project), "--harness", "codex,opencode"]) == 0


def test_dry_run_without_hub_url_on_current_project_reports_unknown(
    project: Path, monkeypatch, capsys
):
    assert main(["init", str(project), "--write"]) == 0
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    capsys.readouterr()
    assert main(["init", str(project)]) == 0
    out = capsys.readouterr().out
    assert "CONFLICT" not in out
    assert "unknown    .codex/config.toml" in out
    assert "unchanged  AGENTS.md" in out


def test_init_defaults_to_recorded_profile(project: Path, capsys):
    assert main(["init", str(project), "--write", "--profile", "careful"]) == 0
    capsys.readouterr()
    assert main(["init", str(project), "--write"]) == 0
    out = capsys.readouterr().out
    assert "operating profile: careful" in out and "Wrote 0 file(s)" in out


def _register(project: Path, tmp_path: Path, **extra_env: str) -> subprocess.CompletedProcess:
    fake = _fake_bin(tmp_path, "claude", 'printf "%s\\n" "$8"\n')
    env = {**os.environ, "CLAUDE_BIN": str(fake), "UA_AGENT_BEARER_TOKEN": "test-only-value"}
    env.pop("UA_DISTRO_HUB_URL_OVERRIDE", None)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(project / ".unified-agent/bin/ua-register-claude")],
        env=env,
        capture_output=True,
        text=True,
    )


def test_register_claude_uses_baked_url_and_validated_override(project: Path, tmp_path: Path):
    assert main(["init", str(project), "--harness", "claude", "--write"]) == 0

    # An ambient hub variable does not redirect the bearer.
    res = _register(project, tmp_path, UA_HTTP_HUB_URL="https://elsewhere.invalid/mcp")
    assert res.returncode == 0 and res.stdout.splitlines()[-1] == HUB_URL

    other = "https://other-hub.invalid:8765/mcp"
    res = _register(project, tmp_path, UA_DISTRO_HUB_URL_OVERRIDE=other)
    assert res.returncode == 0 and res.stdout.splitlines()[-1] == other

    for bad in (
        "https://agent:" + _FAKE_PASS + "@hub.invalid/mcp",
        "https://hub.invalid/mcp?t=" + _FAKE_PASS,
        "ftp://hub.invalid/mcp",
    ):
        res = _register(project, tmp_path, UA_DISTRO_HUB_URL_OVERRIDE=bad)
        assert res.returncode == 1
        assert _FAKE_PASS not in res.stdout + res.stderr
        assert "UA_DISTRO_HUB_URL_OVERRIDE" in res.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file_is_a_clean_error(project: Path, capsys):
    assert main(["init", str(project), "--write"]) == 0
    agents = project / "AGENTS.md"
    agents.chmod(0)
    try:
        capsys.readouterr()
        assert main(["init", str(project)]) == 2
        err = capsys.readouterr().err
        assert "cannot access" in err and "AGENTS.md" in err and "Traceback" not in err
        assert main(["check", str(project)]) == 2
        assert "cannot access" in capsys.readouterr().err
    finally:
        agents.chmod(0o644)


def test_force_merges_codex_toml_and_keeps_other_servers(project: Path):
    codex = project / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text(
        'model = "some-model"\n\n'
        '[mcp_servers.other]\ncommand = "other-server"\n\n'
        '[mcp_servers.unified-agent]\nurl = "https://stale.invalid/mcp"\n\n'
        '[mcp_servers.unified-agent.env]\nX = "1"\n\n'
        '[profiles.fast]\nmodel = "fast"\n'
    )
    assert main(["init", str(project), "--harness", "codex", "--write"]) == 1
    assert main(["init", str(project), "--harness", "codex", "--write", "--force"]) == 0
    data = tomllib.loads((codex / "config.toml").read_text())
    assert data["model"] == "some-model"
    assert data["mcp_servers"]["other"] == {"command": "other-server"}
    assert data["profiles"] == {"fast": {"model": "fast"}}
    assert data["mcp_servers"]["unified-agent"] == {
        "url": HUB_URL,
        "bearer_token_env_var": "UA_AGENT_BEARER_TOKEN",
    }
    # Now current: a second run changes nothing, and check is clean.
    assert main(["init", str(project), "--harness", "codex", "--write"]) == 0
    assert main(["check", str(project), "--harness", "codex"]) == 0


@pytest.mark.parametrize(
    ("rel", "key", "harness"),
    [
        (".cursor/mcp.json", "mcpServers", "cursor"),
        ("opencode.json", "mcp", "opencode"),
        (".vscode/mcp.json", "servers", "copilot"),
    ],
)
def test_force_merges_json_and_keeps_other_servers(project: Path, rel: str, key: str, harness):
    path = project / rel
    path.parent.mkdir(exist_ok=True)
    other = {"type": "stdio", "command": "other-server"}
    path.write_text(json.dumps({"theme": "dark", key: {"other": other}}))
    assert main(["init", str(project), "--harness", harness, "--write"]) == 1
    assert main(["init", str(project), "--harness", harness, "--write", "--force"]) == 0
    data = json.loads(path.read_text())
    assert data["theme"] == "dark"
    assert data[key]["other"] == other
    assert data[key]["unified-agent"]["url"] == HUB_URL
    assert main(["check", str(project), "--harness", harness]) == 0


def test_unmergeable_mcp_config_is_blocked_even_with_force(project: Path, capsys):
    (project / ".cursor").mkdir()
    (project / ".cursor/mcp.json").write_text("{ not json")
    assert main(["init", str(project), "--harness", "cursor", "--write", "--force"]) == 1
    assert "cannot merge" in capsys.readouterr().err
    assert (project / ".cursor/mcp.json").read_text() == "{ not json"


def test_write_under_dev_root_needs_allow_flag(tmp_path: Path, monkeypatch, capsys):
    home = tmp_path / "home"
    target = home / "dev" / "proj"
    target.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    assert main(["init", str(target)]) == 0
    assert "shadow" in capsys.readouterr().err
    assert main(["init", str(target), "--write"]) == 2
    assert "--allow-dev-root" in capsys.readouterr().err
    assert _files(target) == set()
    # Harnesses without project-scoped hub config are fine there.
    assert main(["init", str(target), "--harness", "claude,gemini", "--write"]) == 0
    assert main(["init", str(target), "--write", "--allow-dev-root"]) == 0


def test_obsolete_file_is_only_touched_for_its_harness(project: Path, capsys):
    assert main(["init", str(project), "--write"]) == 0
    old = project / ".unified-agent/copilot-mcp.json"
    old.write_text(_old_copilot_config())
    capsys.readouterr()
    assert main(["check", str(project), "--harness", "codex"]) == 0
    assert "generated by an earlier version" not in capsys.readouterr().out
    assert main(["init", str(project), "--harness", "codex", "--write"]) == 0
    assert old.read_text() == _old_copilot_config()
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    assert not old.exists()


def test_obsolete_file_changed_after_planning_is_not_removed(project: Path):
    assert main(["init", str(project), "--harness", "copilot", "--write"]) == 0
    old = project / ".unified-agent/copilot-mcp.json"
    old.write_text(_old_copilot_config())
    (project / "AGENTS.md").unlink()  # a pending write that must not happen either
    specs = distro_cli.select_specs(["copilot"])
    plan = distro_cli.build_plan(project, specs, distro_cli.render_values("standard", HUB_URL))
    assert [i.status for i in plan if i.spec.dest == ".unified-agent/copilot-mcp.json"] == [
        "remove"
    ]
    mine = '{"mcpServers": {"mine": {"type": "stdio", "command": "x"}}}\n'
    old.write_text(mine)  # the user replaces it between plan and write
    with pytest.raises(distro_cli.DistroError, match="changed since it was planned"):
        distro_cli.apply_plan(project, plan)
    assert old.read_text() == mine
    assert not (project / "AGENTS.md").exists()
    assert not [p for p in _files(project) if p.endswith((".distro-orig", ".distro-tmp"))]
