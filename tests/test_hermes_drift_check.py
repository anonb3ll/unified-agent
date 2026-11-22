"""Tests for scripts/hermes_drift_check.sh — the Hermes governing-file drift audit.

Two boundaries under test. First, detection: a change to SOUL.md / config.yaml /
skills / hooks must fail the check, and a rebaseline must require explicit owner
approval. Second, and the reason this file exists (task #205): the key-level
detail added to config.yaml reports key NAMES only. Values are hashed on the way
past and never stored, so neither ~/.unified_agent/audit.log nor the vault inbox
note can leak a credential added to config.yaml later.

Every run is fully sandboxed by pointing HOME at tmp_path; the suite never reads
or writes the real ~/.hermes.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hermes_drift_check.sh"

_CONFIG = """\
model:
  provider: ollama
  default: hermes-4
agent:
  max_turns: 40
  reasoning_effort: high
mcp_servers:
  unified-agent:
    url: http://127.0.0.1:8765/mcp
"""


@pytest.fixture
def home(tmp_path):
    """A fake HOME with a minimal ~/.hermes and the vault inbox the script writes to."""
    hermes = tmp_path / ".hermes"
    (hermes / "skills" / "demo").mkdir(parents=True)
    (hermes / "hooks").mkdir()
    (hermes / "SOUL.md").write_text("# Soul\n\nBoundaries.\n")
    (hermes / "config.yaml").write_text(_CONFIG)
    (hermes / "skills" / "demo" / "SKILL.md").write_text("# Demo skill\n")
    (tmp_path / ".unified_agent").mkdir()
    (tmp_path / "vaults" / "main" / "00 - Inbox").mkdir(parents=True)
    return tmp_path


def _run(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        capture_output=True,
        text=True,
    )


def _audit_log(home: Path) -> str:
    p = home / ".unified_agent" / "audit.log"
    return p.read_text() if p.is_file() else ""


def _inbox_note(home: Path) -> str:
    notes = list((home / "vaults" / "main" / "00 - Inbox").glob("Hermes Drift *.md"))
    return notes[0].read_text() if notes else ""


def _baseline(home: Path) -> Path:
    return home / ".unified_agent" / "hermes-drift.baseline"


def _key_baseline(home: Path) -> Path:
    return home / ".unified_agent" / "hermes-drift.keys.baseline"


class TestRebaselineGate:
    def test_rebaseline_requires_explicit_approver(self, home):
        res = _run(home, "--rebaseline")
        assert res.returncode == 2
        assert not _baseline(home).exists()
        assert "DRIFT-REBASELINE-REFUSED" in _audit_log(home)

    def test_rebaseline_writes_both_manifests_and_records_the_approver(self, home):
        res = _run(home, "--rebaseline", "--approved-by", "Operator")
        assert res.returncode == 0
        assert _baseline(home).is_file()
        assert _key_baseline(home).is_file()
        assert "approved-by=Operator" in _audit_log(home)

    def test_rebaseline_archives_both_prior_manifests(self, home):
        _run(home, "--rebaseline", "--approved-by", "Operator")
        (home / ".hermes" / "config.yaml").write_text(_CONFIG + "extra: 1\n")
        _run(home, "--rebaseline", "--approved-by", "Operator")

        d = home / ".unified_agent"
        assert list(d.glob("hermes-drift.baseline.*")), "prior file baseline not archived"
        assert list(d.glob("hermes-drift.keys.baseline.*")), "prior key baseline not archived"

    def test_no_baseline_is_an_error_not_a_pass(self, home):
        res = _run(home)
        assert res.returncode == 2
        assert "no baseline" in _audit_log(home)


class TestDetection:
    def test_unchanged_tree_passes(self, home):
        _run(home, "--rebaseline", "--approved-by", "Operator")
        res = _run(home)
        assert res.returncode == 0
        assert "DRIFT-PASS" in _audit_log(home)
        assert _inbox_note(home) == ""

    def test_modified_soul_fails_and_escalates(self, home):
        _run(home, "--rebaseline", "--approved-by", "Operator")
        (home / ".hermes" / "SOUL.md").write_text("# Soul\n\nBoundaries, edited.\n")

        res = _run(home)
        assert res.returncode == 1
        assert "MODIFIED" in _audit_log(home)
        assert "SOUL.md" in _inbox_note(home)

    def test_missing_imessage_notifier_is_logged(self, home):
        _run(home, "--rebaseline", "--approved-by", "Operator")
        (home / ".hermes" / "SOUL.md").write_text("# Soul\n\nBoundaries, edited.\n")

        assert _run(home).returncode == 1
        assert "DRIFT-NOTIFY-ERR" in _audit_log(home)

    def test_failed_imessage_notifier_is_logged(self, home):
        notifier = home / "Projects" / "agent" / "projects" / "imessage-bridge" / "notify.sh"
        notifier.parent.mkdir(parents=True)
        notifier.write_text("#!/usr/bin/env bash\nexit 1\n")
        notifier.chmod(0o755)
        _run(home, "--rebaseline", "--approved-by", "Operator")
        (home / ".hermes" / "SOUL.md").write_text("# Soul\n\nBoundaries, edited.\n")

        assert _run(home).returncode == 1
        assert "DRIFT-NOTIFY-ERR" in _audit_log(home)

    def test_added_security_skill_key_is_reported(self, home):
        _run(home, "--rebaseline", "--approved-by", "Operator")
        new = home / ".hermes" / "skills" / "sneaky"
        new.mkdir()
        (new / "SKILL.md").write_text("---\nallowed-tools: [exec]\n---\n# Sneaky\n")

        assert _run(home).returncode == 1
        assert "SKILL-SECURITY-KEY" in _audit_log(home) or "SKILL-SECURITY-KEY" in _inbox_note(home)


class TestKeyLevelDetail:
    """The #205 change: a modified config.yaml says which keys moved."""

    def _rebaseline_then_edit(self, home, new_config: str):
        _run(home, "--rebaseline", "--approved-by", "Operator")
        (home / ".hermes" / "config.yaml").write_text(new_config)
        return _run(home)

    def test_changed_value_names_the_key_path(self, home):
        res = self._rebaseline_then_edit(home, _CONFIG.replace("hermes-4", "hermes-5"))
        assert res.returncode == 1
        assert "keys changed: model.default" in _inbox_note(home)

    def test_added_and_removed_keys_are_named(self, home):
        added = _CONFIG + "vertex:\n  enabled: false\n"
        self._rebaseline_then_edit(home, added)
        assert "keys added:" in _inbox_note(home)
        assert "vertex.enabled" in _inbox_note(home)

        home2_cfg = _CONFIG.replace("  reasoning_effort: high\n", "")
        self._rebaseline_then_edit(home, home2_cfg)
        note = _inbox_note(home)
        assert "keys removed:" in note
        assert "agent.reasoning_effort" in note

    def test_nested_mcp_server_addition_is_legible(self, home):
        # The real 2026-07-27 drift: an MCP server registered under mcp_servers.
        added = _CONFIG + "  codex:\n    url: http://127.0.0.1:9000/mcp\n"
        self._rebaseline_then_edit(home, added)
        assert "mcp_servers.codex.url" in _inbox_note(home)

    def test_a_changed_yaml_list_names_the_key_once_not_once_per_item(self, home):
        # The real 2026-08-02/03 drift: every item under a YAML list shares one
        # key path in the flattener (a list item line starts with "-", so it
        # never pushes a new key — see _YAML_KEYS_AWK). A 20-item list changing
        # therefore produces 20 manifest lines with an identical path, and the
        # pre-fix `strip()` both failed to dedupe them AND joined with a broken
        # delimiter (`paste -sd', ' -` cycles single-char delimiters instead of
        # using the literal string), corrupting the report into
        # "known_builtin_toolsets.cli,known_builtin_toolsets.cli known_..." —
        # exactly what the 2026-08-02 and 2026-08-03 Hermes Drift captures show.
        base = _CONFIG + "known_builtin_toolsets:\n  cli:\n    - a\n    - b\n    - c\n"
        changed = _CONFIG + "known_builtin_toolsets:\n  cli:\n    - a\n    - b\n    - z\n"

        _run(home, "--rebaseline", "--approved-by", "Operator")
        (home / ".hermes" / "config.yaml").write_text(base)
        _run(home, "--rebaseline", "--approved-by", "Operator")
        (home / ".hermes" / "config.yaml").write_text(changed)
        res = _run(home)
        note = _inbox_note(home)

        assert res.returncode == 1
        assert "keys changed: known_builtin_toolsets.cli" in note
        # The key path must appear exactly once per section line — not repeated
        # per list item, and not glued together with the wrong delimiter.
        assert note.count("known_builtin_toolsets.cli") == 1
        assert "known_builtin_toolsets.cli,known_builtin_toolsets.cli" not in note

    def test_values_never_appear_in_the_log_or_the_note(self, home):
        """The security property: names are reported, values are not.

        A credential added to config.yaml must not be copied into the audit log
        or into a vault note that syncs to every device.
        """
        # ua:allow — a fabricated, never-valid string. It has to be credential-
        # shaped for the test to mean anything, which is also what makes the
        # secrets rule fire on this line; the exemption is per-line and stated,
        # rather than the rule being narrowed to let it through.
        secret = "sk-live-DO-NOT-LEAK-4f9a2b"  # ua:allow
        leaked = _CONFIG.replace("provider: ollama", f"provider: ollama\n  api_key: {secret}")

        res = self._rebaseline_then_edit(home, leaked)
        assert res.returncode == 1

        note, log = _inbox_note(home), _audit_log(home)
        assert "model.api_key" in note, "the key name should still be reported"
        assert secret not in note, "value leaked into the vault note"
        assert secret not in log, "value leaked into the audit log"
        assert secret not in _key_baseline(home).read_text(), "value stored in the key baseline"

    def test_missing_key_baseline_reports_itself_rather_than_silence(self, home):
        # Upgrade path: a file baseline exists from the old script, no key one yet.
        _run(home, "--rebaseline", "--approved-by", "Operator")
        _key_baseline(home).unlink()
        (home / ".hermes" / "config.yaml").write_text(_CONFIG.replace("hermes-4", "hermes-5"))

        assert _run(home).returncode == 1
        assert "no key baseline yet" in _inbox_note(home)

    def test_non_yaml_change_gets_no_key_section(self, home):
        _run(home, "--rebaseline", "--approved-by", "Operator")
        (home / ".hermes" / "SOUL.md").write_text("# Soul\n\nRewritten.\n")
        assert _run(home).returncode == 1

        note = _inbox_note(home)
        assert "SOUL.md" in note
        assert "keys changed:" not in note

    def test_detail_degrades_but_never_hides_a_change(self, home, monkeypatch):
        """A config the awk flattener cannot parse still fails the check.

        Detection is the whole-file sha256; the key list is advisory. Flow-style
        YAML is exactly the shape the flattener does not model.
        """
        _run(home, "--rebaseline", "--approved-by", "Operator")
        (home / ".hermes" / "config.yaml").write_text("model: {provider: ollama, default: x}\n")

        res = _run(home)
        assert res.returncode == 1, "an unparseable config must still register as drift"
        assert "config.yaml" in _inbox_note(home)


class TestSkillKeyMonitoring:
    """The Tier 2 skill frontmatter monitoring."""

    def test_rebaseline_creates_skill_key_baseline(self, home):
        res = _run(home, "--rebaseline", "--approved-by", "Operator")
        assert res.returncode == 0
        skill_base = home / ".unified_agent" / "hermes-drift.skills.baseline"
        assert skill_base.is_file()

    def test_non_security_skill_key_edit_auto_accepted(self, home):
        _run(home, "--rebaseline", "--approved-by", "Operator")
        skill_md = home / ".hermes" / "skills" / "demo" / "SKILL.md"
        skill_md.write_text("---\nname: demo\ndescription: edited description\n---\n# Demo skill\n")

        res = _run(home)
        assert res.returncode == 0
        assert "DRIFT-PASS" in _audit_log(home)

    def test_security_skill_key_edit_fails_and_flags(self, home):
        _run(home, "--rebaseline", "--approved-by", "Operator")
        skill_md = home / ".hermes" / "skills" / "demo" / "SKILL.md"
        skill_md.write_text("---\nname: demo\nallowed-tools: [all]\n---\n# Demo skill\n")

        res = _run(home)
        assert res.returncode == 1
        assert "SKILL-SECURITY-KEY" in _inbox_note(home) or "SKILL-SECURITY-KEY" in _audit_log(home)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_script_is_executable_and_syntactically_valid():
    assert SCRIPT.is_file()
    res = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
