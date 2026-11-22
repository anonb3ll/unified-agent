"""Tests for the PTY-driven agy-interactive executor backend (task #1177).

Print-mode teardown is covered by test_executor_backends / test_agy_teardown_resume.
These tests cover approval fail-closed, '>' completion detection, settings seeding,
and a fixture e2e where a fake interactive binary finishes with zero teardown.
"""

from __future__ import annotations

import json
import os
import textwrap

from unified_agent.executor import agy_interactive as ai
from unified_agent.executor import backends

FAKE_INTERACTIVE_AGY = textwrap.dedent(
    r"""
    #!/usr/bin/env python3
    # Fake interactive agy for task #1177 e2e.
    # Speaks the same surface the PTY driver expects: print '>', read a turn
    # (Enter-terminated), show an allowlisted approval menu, write+check a
    # file, return to '>', then exit cleanly on /exit. Never emits teardown.
    import os
    import sys
    import time

    def _flush(msg: str) -> None:
        sys.stdout.write(msg)
        sys.stdout.flush()

    def _read_line() -> str:
        return sys.stdin.readline()

    cwd = os.getcwd()
    out_path = os.path.join(cwd, "agy_interactive_probe.txt")

    _flush("Antigravity interactive (fixture)\n")
    _flush(">\n")
    prompt = _read_line()
    if not prompt:
        sys.exit(2)
    _flush(f"turn received ({len(prompt)} chars)\n")

    # Simulate a per-command approval for an allowlisted class (git).
    _flush("Allow this command?\n")
    _flush("command(git status --porcelain)\n")
    _flush("1. Yes\n")
    _flush("2. Yes, and don't ask again\n")
    _flush("3. No\n")
    decision = _read_line().strip()
    if decision != "1":
        _flush(f"unexpected approval keystroke: {decision!r}\n")
        sys.exit(3)
    _flush("approved: git status --porcelain\n")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("ok\n")
    # Local check — no network.
    if not os.path.isfile(out_path):
        sys.exit(4)
    with open(out_path, encoding="utf-8") as f:
        if f.read().strip() != "ok":
            sys.exit(5)
    _flush(f"wrote and verified {out_path}\n")
    time.sleep(0.05)
    _flush(">\n")

    exit_cmd = _read_line()
    if exit_cmd.strip() != "/exit":
        _flush(f"expected /exit, got {exit_cmd!r}\n")
        sys.exit(6)
    _flush("goodbye\n")
    sys.exit(0)
    """
).lstrip()


class TestApprovalDriver:
    def test_allowlisted_git_approves(self):
        screen = (
            "Allow this command?\n"
            "command(git status --porcelain)\n"
            "1. Yes\n"
            "2. Yes, and don't ask again\n"
            "3. No\n"
        )
        assert ai.decide_approval(screen) == "approve"
        assert ai.approval_keystroke("approve") == "1\n"

    def test_allowlisted_pytest_ruff_gh(self):
        for cmd in ("pytest -q", "ruff check .", "gh pr create --fill"):
            screen = f"Do you want to allow this command?\n`{cmd}`\n1. Yes\n"
            assert ai.decide_approval(screen) == "approve", cmd

    def test_destructive_rm_denies(self):
        screen = "Allow this command?\n`rm -rf /tmp/x`\n1. Yes\n3. No\n"
        assert ai.decide_approval(screen) == "deny"
        assert ai.approval_keystroke("deny") == "3\n"

    def test_unknown_command_denies(self):
        screen = "Allow this command?\n`curl https://evil.example`\n1. Yes\n"
        assert ai.decide_approval(screen) == "deny"

    def test_menu_without_parseable_command_denies(self):
        screen = "Allow this command?\n1. Yes\n3. No\n"
        assert ai.decide_approval(screen) == "deny"

    def test_no_menu_is_ignore(self):
        assert ai.decide_approval("just some agent text\n") == "ignore"

    def test_is_allowlisted_rejects_shell_wrappers_to_destructive(self):
        assert not ai.is_allowlisted_command("sudo git status")
        assert not ai.is_allowlisted_command("bash -c 'git status'")
        assert ai.is_allowlisted_command("git push -u origin HEAD")


class TestCompletionDetection:
    def test_detects_bare_prompt(self):
        assert ai.detect_interactive_prompt("done\n>\n") is True

    def test_ignores_prompt_char_mid_line(self):
        assert ai.detect_interactive_prompt("score > 0.5\n") is False

    def test_ignores_empty(self):
        assert ai.detect_interactive_prompt("") is False


class TestSettingsSeeding:
    def test_seeds_class_allows_without_clobbering(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"permissions": {"allow": ["command(ls)"]}, "other": 1}),
            encoding="utf-8",
        )
        added = ai.seed_agy_settings_allows(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["other"] == 1
        allow = data["permissions"]["allow"]
        assert "command(ls)" in allow
        for entry in ("command(git)", "command(pytest)", "command(ruff)", "command(gh)"):
            assert entry in allow
            assert entry in added
        # Idempotent.
        assert ai.seed_agy_settings_allows(path) == []


class TestBackendSelection:
    def test_selects_agy_interactive(self, monkeypatch):
        monkeypatch.setenv("UA_EXECUTOR_BACKEND", "agy-interactive")
        b = backends.get_backend()
        assert b.name == "agy-interactive"
        assert b.session_kind == "agy_interactive_run"

    def test_label_still_defaults_to_print_agy(self, monkeypatch):
        monkeypatch.delenv("UA_EXECUTOR_BACKEND", raising=False)
        assert backends.get_backend(agent_label="Desktop_agent3").name == "agy"


class TestInteractiveE2E:
    def test_fixture_completes_without_teardown(self, tmp_path, monkeypatch):
        """No-network analog: write file, run check, /exit — zero teardown."""
        fake = tmp_path / "fake-agy-interactive"
        fake.write_text(FAKE_INTERACTIVE_AGY, encoding="utf-8")
        fake.chmod(0o755)
        settings = tmp_path / "settings.json"
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("UA_AGY_SETTINGS_PATH", str(settings))

        progress: list[str] = []

        def on_chunk(chunk: str) -> None:
            progress.append(chunk)

        res = ai.run_interactive_session(
            agy_bin=str(fake),
            prompt="Write agy_interactive_probe.txt with ok and verify it.",
            cwd=str(work),
            env={**os.environ, "TERM": "xterm"},
            timeout_s=30,
            settings_path=settings,
            progress_cb=on_chunk,
        )

        assert res["status"] == "success", res
        assert res["error"] is None
        assert backends.detect_agy_background_teardown(res["text"]) is None
        assert "root agent idle; waiting up to 5s" not in (res["text"] or "")
        assert (
            "terminating" not in (res["text"] or "").lower()
            or "background task" not in (res["text"] or "").lower()
        )
        assert (work / "agy_interactive_probe.txt").read_text(encoding="utf-8") == "ok\n"
        assert progress, "progress_cb should receive streamed chunks"
        # Settings must be pre-seeded for the #1146 allowlisted classes.
        allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
        for entry in ai.settings_allow_entries():
            assert entry in allow

    def test_backend_run_wires_through(self, tmp_path, monkeypatch):
        fake = tmp_path / "fake-agy-interactive"
        fake.write_text(FAKE_INTERACTIVE_AGY, encoding="utf-8")
        fake.chmod(0o755)
        settings = tmp_path / "settings.json"
        work = tmp_path / "work"
        work.mkdir()
        (tmp_path / ".keep").write_text("")
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))

        backend = backends.AgyInteractiveBackend(
            agy_bin=str(fake),
            settings_path=settings,
        )
        res = backend.run(
            prompt="probe",
            cwd="work",
            session_dir="sessions",
            session_id="task-1177-e2e",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert res["status"] == "success", res
        assert backends.AGY_BACKGROUND_TEARDOWN_REASON not in (res.get("error") or "")
        assert (work / "agy_interactive_probe.txt").is_file()

    def test_denies_non_allowlisted_mid_session(self, tmp_path, monkeypatch):
        script = textwrap.dedent(
            r"""
            #!/usr/bin/env python3
            import sys
            def flush(m):
                sys.stdout.write(m); sys.stdout.flush()
            flush(">\n")
            sys.stdin.readline()
            flush("Allow this command?\n")
            flush("`rm -rf /tmp/not-allowed`\n")
            flush("1. Yes\n3. No\n")
            # Driver should send 3 (deny) and abort.
            decision = sys.stdin.readline().strip()
            flush(f"got {decision}\n")
            flush(">\n")
            sys.stdin.readline()
            """
        ).lstrip()
        fake = tmp_path / "fake-deny"
        fake.write_text(script, encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        res = ai.run_interactive_session(
            agy_bin=str(fake),
            prompt="do bad thing",
            cwd=str(tmp_path),
            env={**os.environ, "TERM": "xterm"},
            timeout_s=20,
            settings_path=tmp_path / "settings.json",
        )
        assert res["status"] == "error"
        assert ai.AGY_INTERACTIVE_APPROVAL_DENIED_REASON in (res["error"] or "")
