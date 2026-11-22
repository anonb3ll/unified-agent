"""Vendor-text-independent detection of agy's silent background-work drop (task #1247).

Task #1143 established (PR #681, fixtures in ``tests/fixtures/agy_teardown_1143/``)
that on the live ``agy 1.2.7`` binary, print-mode teardown drops backgrounded
work with *zero* teardown-related text on stdout or stderr — the #1158
four-phrase regex (``detect_agy_background_teardown``) finds nothing, so
``AgyBackend.run()`` reports a clean ``success`` for an attempt that lost
work. Repro was 5/5 deterministic.

This module reuses those same fixtures (not new ones) as the regression
baseline, and adds the structural, vendor-text-independent detector this
task introduces: ``detect_agy_unconfirmed_background_launch``. It reads the
tool-call *requests* the session itself made (a `run_command` containing an
unquoted shell "&") rather than anything agy prints, so it is not defeated by
a vendor CLI text change the way the #1158 regex was.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from unified_agent.executor import backends

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "agy_teardown_1143"


def _load(name: str) -> str:
    path = FIXTURE_DIR / name
    return path.read_text() if path.exists() else ""


LIVE_1_2_7_STDOUT = _load("live_1.2.7_run1.stdout")
LIVE_1_2_7_STDERR = _load("live_1.2.7_run1.stderr")


class TestRedFirstAgainstCurrentDetection:
    """Pin down the gap #1143 found, using the shipped #1158 detector alone."""

    def test_1158_text_signature_detector_finds_nothing_on_1_2_7(self) -> None:
        assert LIVE_1_2_7_STDOUT, "fixture missing — see tests/fixtures/agy_teardown_1143/README.md"
        assert backends.detect_agy_background_teardown(LIVE_1_2_7_STDOUT, LIVE_1_2_7_STDERR) is None

    def test_parser_alone_reports_clean_success_on_1_2_7(self) -> None:
        parsed = backends.parse_agy_stream_json(LIVE_1_2_7_STDOUT)
        assert parsed["error"] is None
        assert parsed["text"] == "LAUNCHED\n"


class TestStructuralDetectorCatchesTheSilentDrop:
    """The new, vendor-text-independent detector this task adds."""

    def test_unconfirmed_background_launch_detected_on_live_1_2_7_trace(self) -> None:
        assert LIVE_1_2_7_STDOUT, "fixture missing"
        reason = backends.detect_agy_unconfirmed_background_launch(LIVE_1_2_7_STDOUT)
        assert reason is not None
        assert backends.AGY_BACKGROUND_TEARDOWN_REASON in reason

    def test_detector_does_not_depend_on_any_teardown_text(self) -> None:
        """The live trace's stderr is empty and stdout carries none of the four
        #1158 phrases (see the two RED tests above) — the structural detector
        must still catch it from the tool-call shape alone."""
        for phrase in (
            "root agent idle",
            "terminating",
            "drain grace period expired",
            "background tasks have been stopped",
        ):
            assert phrase not in LIVE_1_2_7_STDOUT
            assert phrase not in LIVE_1_2_7_STDERR
        assert backends.detect_agy_unconfirmed_background_launch(LIVE_1_2_7_STDOUT) is not None


class TestAgyRunNoLongerReportsSuccessOnTheSilentDrop:
    """End-to-end: AgyBackend.run() over the exact #1143 live capture."""

    def _patch_run(self, monkeypatch, *, stdout: str, stderr: str = "", returncode: int = 0):
        def fake_run(argv, *, cwd, env, timeout_s, stdin, kill_check=None, poll_interval_s=1.0):
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)

    def test_live_1_2_7_capture_no_longer_yields_success(self, tmp_path, monkeypatch):
        assert LIVE_1_2_7_STDOUT, "fixture missing"
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
        self._patch_run(monkeypatch, stdout=LIVE_1_2_7_STDOUT, stderr=LIVE_1_2_7_STDERR)

        res = backends.AgyBackend().run(
            prompt="background a write and reply LAUNCHED",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )

        assert res["status"] != "success"
        assert backends.AGY_BACKGROUND_TEARDOWN_REASON in (res["error"] or "")


class TestNoFalsePositives:
    """A genuinely successful agy run — no backgrounding at all — must still
    report success. This is the DoD #4 guard: don't import PR #681's
    surviving-but-slow-job over-reporting flaw."""

    ORDINARY_SUCCESS_STDOUT = "\n".join(
        [
            json.dumps({"event": "init", "init": {"cwd": "/tmp"}}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {
                            "name": "run_command",
                            "parameters": {"CommandLine": "pytest -q"},
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {"status": "SUCCESS", "response": "all green", "error": ""},
                }
            ),
        ]
    )

    # A run that backgrounds work but then calls generic wait/status tools.
    # Those calls are not correlated to the detached job and cannot prove it
    # landed, so ambiguity must remain a failure.
    UNRESOLVED_BACKGROUND_STDOUT = "\n".join(
        [
            json.dumps({"event": "init", "init": {"cwd": "/tmp"}}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {
                            "name": "run_command",
                            "parameters": {"CommandLine": "(sleep 1; echo done > out.txt) &"},
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "wait_5_seconds",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "command_status",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {"status": "SUCCESS", "response": "done", "error": ""},
                }
            ),
        ]
    )

    def test_ordinary_foreground_command_is_not_flagged(self):
        assert (
            backends.detect_agy_unconfirmed_background_launch(self.ORDINARY_SUCCESS_STDOUT) is None
        )

    def test_generic_wait_does_not_confirm_background_launch(self):
        assert (
            backends.detect_agy_unconfirmed_background_launch(self.UNRESOLVED_BACKGROUND_STDOUT)
            is not None
        )

    def test_embedded_background_operator_is_flagged(self):
        stdout = json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {"parameters": {"CommandLine": "pytest -q & echo launched"}},
                },
            }
        )
        assert backends.detect_agy_unconfirmed_background_launch(stdout) is not None

    @pytest.mark.parametrize(
        "command_line",
        [
            "echo '&'",
            "echo \\&",
            "echo value # comment &",
            "echo value &> output.txt",
            "echo value 2>&1",
            "ruff check . && ruff format --check .",
        ],
    )
    def test_literal_comments_and_redirections_are_not_flagged(self, command_line):
        stdout = json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {"parameters": {"CommandLine": command_line}},
                },
            }
        )
        assert backends.detect_agy_unconfirmed_background_launch(stdout) is None

    def test_boolean_and_is_not_mistaken_for_backgrounding(self):
        stdout = "\n".join(
            [
                json.dumps({"event": "init", "init": {"cwd": "/tmp"}}),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "step_type": "tool",
                            "tool_name": "run_command",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {
                                    "CommandLine": "ruff check . && ruff format --check ."
                                },
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "clean", "error": ""},
                    }
                ),
            ]
        )
        assert backends.detect_agy_unconfirmed_background_launch(stdout) is None

    def test_ordinary_run_via_agybackend_run_reports_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))

        def fake_run(argv, *, cwd, env, timeout_s, stdin, kill_check=None, poll_interval_s=1.0):
            return subprocess.CompletedProcess(argv, 0, self.ORDINARY_SUCCESS_STDOUT, "")

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)

        res = backends.AgyBackend().run(
            prompt="run the tests",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert res["status"] == "success"
        assert res["error"] is None

    def test_unresolved_background_run_via_agybackend_run_reports_error(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))

        def fake_run(argv, *, cwd, env, timeout_s, stdin, kill_check=None, poll_interval_s=1.0):
            return subprocess.CompletedProcess(argv, 0, self.UNRESOLVED_BACKGROUND_STDOUT, "")

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)

        res = backends.AgyBackend().run(
            prompt="background it and check on it",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        )
        assert res["status"] == "error"
        assert backends.AGY_BACKGROUND_TEARDOWN_REASON in (res["error"] or "")


class TestExisting1158And1172BehaviourUnchanged:
    """DoD #3: the four known signatures still detect, and auto-resume
    semantics (AGY_MAX_TEARDOWN_RESUMES, dirty-worktree gate) hold."""

    TEARDOWN_TEXT_STDOUT = "\n".join(
        [
            json.dumps({"event": "init", "init": {"cwd": "/tmp"}}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "run_command",
                    },
                }
            ),
            "root agent idle; waiting up to 5s for 1 background task(s)",
            "terminating 1 background task(s) on exit",
            json.dumps(
                {
                    "event": "result",
                    "result": {"status": "SUCCESS", "response": "done", "error": ""},
                }
            ),
        ]
    )

    def test_four_signatures_still_caught(self):
        for phrase in (
            "root agent idle; waiting up to 5s for 1 background task(s)",
            "terminating 1 background task(s) on exit",
            "Print mode: drain grace period expired",
            "All your background tasks have been stopped",
        ):
            assert backends.detect_agy_background_teardown(phrase) is not None

    def test_clean_tree_never_triggers_resume_contract_holds(self, tmp_path):
        # worktree_has_uncommitted_changes must still return False on a clean
        # (non-git or empty) directory — resume gate is untouched by this task.
        empty = tmp_path / "clean"
        empty.mkdir()
        assert backends.worktree_has_uncommitted_changes(str(empty)) is False

    def test_max_teardown_resumes_constant_unchanged(self):
        assert backends.AGY_MAX_TEARDOWN_RESUMES == 2
