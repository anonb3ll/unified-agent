"""Agy print-mode teardown auto-resume (task #1172).

After #1158 detects ``agy_background_teardown``, the worktree often still holds
uncommitted edits (the killed background write landed on disk). Auto-resume
re-invokes ``agy --continue --output-format stream-json`` up to twice when the
tree is dirty; a clean tree must never resume (completed delivery, not a fumble).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from unified_agent.executor import backends
from unified_agent.quota_observe import (
    FAILURE_AGY_TEARDOWN_RESUME_EXHAUSTED,
    classify_failure_reason,
)

# Matches scripts/agy_teardown_canary.sh's verdict line ("KILLED agy=... teardown=...",
# "SURVIVED agy=...", or "ERROR agy=..."). Used to find the verdict robustly rather than
# assuming it is stdout's last physical line (task #1229: a SIGPIPE race in the canary's
# `head -n1 || echo unknown` version probe could split the verdict across two lines,
# pushing the KILLED/SURVIVED prefix off of stdout.splitlines()[-1]).
_VERDICT_RE = re.compile(r"^(SURVIVED|KILLED|ERROR)\b.*$", re.MULTILINE)


def _verdict_line(stdout: str) -> str:
    """Return the canary's single verdict line, regardless of its position in stdout.

    Also serves as a regression guard: if the verdict line is ever emitted
    non-deterministically (e.g. split across lines, or printed more than once),
    this raises instead of silently matching the wrong fragment.
    """
    matches = _VERDICT_RE.findall(stdout)
    lines = [m for m in stdout.splitlines() if _VERDICT_RE.match(m)]
    assert len(lines) == 1, (
        f"expected exactly one verdict line, found {len(lines)} "
        f"(verdict words: {matches!r}); full stdout={stdout!r}"
    )
    return lines[0]


# Same live signature as tests/test_executor_backends.py (task #1158).
AGY_TEARDOWN_RACE_STDOUT = "\n".join(
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
                "result": {
                    "status": "SUCCESS",
                    "response": "I have launched the test run and will wait for it to complete",
                    "error": "",
                },
            }
        ),
    ]
)

AGY_CLEAN_SUCCESS = "\n".join(
    [
        json.dumps({"event": "init", "init": {"cwd": "/tmp"}}),
        json.dumps(
            {
                "event": "result",
                "result": {"status": "SUCCESS", "response": "finished", "error": ""},
            }
        ),
    ]
)


def _git_init_dirty(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "wip.txt").write_text("uncommitted work survived teardown\n")


def _git_init_clean(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


class TestWorktreeDirtyHelper:
    def test_porcelain_nonempty_is_dirty(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_dirty(repo)
        assert backends.worktree_has_uncommitted_changes(str(repo)) is True

    def test_clean_tree_is_not_dirty(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_clean(repo)
        assert backends.worktree_has_uncommitted_changes(str(repo)) is False

    def test_nongit_dir_is_not_dirty(self, tmp_path):
        assert backends.worktree_has_uncommitted_changes(str(tmp_path / "empty")) is False


class TestClassifyAgyTeardownResumeExhausted:
    def test_classify_failure_reason(self):
        msg = (
            f"{backends.AGY_TEARDOWN_RESUME_EXHAUSTED_REASON}: still torn down "
            "after resumes; resume_count=2"
        )
        assert classify_failure_reason(msg) == FAILURE_AGY_TEARDOWN_RESUME_EXHAUSTED


class TestAgyTeardownAutoResume:
    def _run(self, monkeypatch, repo: Path, *, stdout_seq, capture=None):
        monkeypatch.setenv("UA_WORKSPACE", str(repo))
        calls: list[list[str]] = []

        def fake_run(argv, *, cwd, env, timeout_s, stdin, kill_check=None, poll_interval_s=1.0):
            calls.append(list(argv))
            if capture is not None:
                capture["calls"] = calls
                capture["cwd"] = cwd
                capture["env"] = env
            idx = min(len(calls) - 1, len(stdout_seq) - 1)
            return subprocess.CompletedProcess(argv, 0, stdout_seq[idx], "")

        monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)
        return backends.AgyBackend().run(
            prompt="ship it",
            cwd=".",
            session_dir="d",
            session_id="s",
            agent_label="Desktop_agent3",
            timeout_s=30,
        ), calls

    def test_teardown_dirty_auto_resumes_with_continue(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_dirty(repo)
        capture: dict = {}
        res, calls = self._run(
            monkeypatch,
            repo,
            stdout_seq=[AGY_TEARDOWN_RACE_STDOUT, AGY_CLEAN_SUCCESS],
            capture=capture,
        )
        assert res["status"] == "success"
        assert res["text"] == "finished"
        assert res["error"] is None
        assert res["resume_count"] == 1
        assert len(calls) == 2
        assert calls[0][1:3] == ["-p", "ship it"]
        assert "--continue" in calls[1]
        assert "stream-json" in calls[1]
        assert capture["cwd"] == str(repo.resolve())

    def test_teardown_clean_never_resumes(self, monkeypatch, tmp_path):
        """#1158 refusal stays: clean tree = completed delivery, not a fumble."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_clean(repo)
        res, calls = self._run(
            monkeypatch,
            repo,
            stdout_seq=[AGY_TEARDOWN_RACE_STDOUT],
        )
        assert res["status"] == "error"
        assert backends.AGY_BACKGROUND_TEARDOWN_REASON in (res["error"] or "")
        assert backends.AGY_TEARDOWN_RESUME_EXHAUSTED_REASON not in (res["error"] or "")
        assert res["resume_count"] == 0
        assert len(calls) == 1
        assert "--continue" not in calls[0]

    def test_max_two_resumes_then_exhausted(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_dirty(repo)
        res, calls = self._run(
            monkeypatch,
            repo,
            stdout_seq=[AGY_TEARDOWN_RACE_STDOUT],  # every invoke tears down
        )
        assert res["status"] == "error"
        assert backends.AGY_TEARDOWN_RESUME_EXHAUSTED_REASON in (res["error"] or "")
        assert res["resume_count"] == 2
        # initial + 2 resumes
        assert len(calls) == 3
        assert all("--continue" in c for c in calls[1:])
        assert classify_failure_reason(res["error"]) == FAILURE_AGY_TEARDOWN_RESUME_EXHAUSTED

    def test_success_without_teardown_has_resume_count_zero(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_clean(repo)
        res, calls = self._run(
            monkeypatch,
            repo,
            stdout_seq=[AGY_CLEAN_SUCCESS],
        )
        assert res["status"] == "success"
        assert res["resume_count"] == 0
        assert len(calls) == 1


class TestAgyTeardownCanaryScript:
    """Canary exercises a fixture binary — live agy is not required in CI."""

    def test_canary_reports_killed_on_teardown_without_write(self, tmp_path, monkeypatch):
        script = Path(__file__).resolve().parents[1] / "scripts" / "agy_teardown_canary.sh"
        assert script.is_file()
        fake = tmp_path / "fake-agy"
        fake.write_text(
            "#!/bin/sh\n"
            "echo 'root agent idle; waiting up to 5s for 1 background task(s)'\n"
            "echo 'terminating 1 background task(s) on exit'\n"
            "exit 0\n"
        )
        fake.chmod(0o755)
        out = subprocess.run(
            ["bash", str(script), "--agy-bin", str(fake), "--workdir", str(tmp_path / "repo")],
            capture_output=True,
            text=True,
            timeout=60,
            env=_sandbox_confirmed_env(tmp_path),
        )
        assert out.returncode == 1, out.stdout + out.stderr
        verdict = _verdict_line(out.stdout)
        assert verdict.startswith("KILLED")
        assert "teardown=1" in verdict
        assert "marker_missing=1" in verdict

    def test_canary_reports_survived_when_write_lands(self, tmp_path):
        script = Path(__file__).resolve().parents[1] / "scripts" / "agy_teardown_canary.sh"
        fake = tmp_path / "fake-agy"
        # Mimic a background write that survives process exit (the live failure
        # mode leaves the file even when the CLI prints the teardown signature).
        fake.write_text(
            "#!/bin/sh\n"
            "printf 'survived\\n' > canary_marker.txt\n"
            "echo 'root agent idle; waiting up to 5s for 1 background task(s)'\n"
            "exit 0\n"
        )
        fake.chmod(0o755)
        out = subprocess.run(
            ["bash", str(script), "--agy-bin", str(fake), "--workdir", str(tmp_path / "repo")],
            capture_output=True,
            text=True,
            timeout=60,
            env=_sandbox_confirmed_env(tmp_path),
        )
        assert out.returncode == 0, out.stdout + out.stderr
        verdict = _verdict_line(out.stdout)
        assert verdict.startswith("SURVIVED")
        assert "marker=canary_marker.txt" in verdict


_CANARY = Path(__file__).resolve().parents[1] / "scripts" / "agy_teardown_canary.sh"
# The live agy 1.2.7 capture (task #1143): run_command for the canary job DONE,
# result SUCCESS, empty stderr, marker never written, no teardown text.
_LIVE_STREAM = Path(__file__).parent / "fixtures" / "agy_teardown_1143" / "live_1.2.7_run1.stdout"
_CANARY_DONE_STEP = (
    '{"event":"step_update","step_update":{"state":"DONE","step_type":"tool",'
    '"tool_name":"run_command","tool_info":{"name":"run_command","parameters":'
    '{"CommandLine":"(sleep 8; printf \'survived\\\\n\' \\u003e canary_marker.txt) \\u0026"}}}}'
)
_RESULT_SUCCESS = '{"event":"result","result":{"status":"SUCCESS","response":"LAUNCHED\\n"}}'


def _sandbox_confirmed_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """PATH whose python3 answers the canary's posture query with ``--sandbox``.

    The real chokepoint refuses agy (BypassSandbox cannot be switched off).
    These verdict tests still need agy to run, so they stub only that query.
    """
    real = Path(sys.executable).resolve()
    bin_dir = tmp_path / "py-bin"
    bin_dir.mkdir(exist_ok=True)
    wrapper = bin_dir / "python3"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-c" ] && printf \'%s\\n\' "${2:-}" | grep -q ua_harness_sandbox; then\n'
        "  printf '%s' '--sandbox'\n"
        "  exit 0\n"
        "fi\n"
        f'exec {shlex.quote(str(real))} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    if extra:
        env.update(extra)
    return env


def _run_canary(tmp_path: Path, stream: str, *, rc: int = 0, wait: str = "0"):
    """Run the canary against a fake agy that replays ``stream`` and exits ``rc``."""
    (tmp_path / "stream.jsonl").write_text(stream)
    fake = tmp_path / "fake-agy"
    fake.write_text(f"#!/bin/sh\ncat '{tmp_path / 'stream.jsonl'}'\nexit {rc}\n")
    fake.chmod(0o755)
    return subprocess.run(
        ["bash", str(_CANARY), "--agy-bin", str(fake), "--workdir", str(tmp_path / "repo")],
        capture_output=True,
        text=True,
        timeout=60,
        env=_sandbox_confirmed_env(tmp_path, {"UA_AGY_CANARY_MARKER_WAIT_S": wait}),
    )


class TestAgyTeardownCanarySilentLoss:
    """Task #1791: agy 1.2.7+ drops the job silently; the canary must say KILLED."""

    def test_live_capture_replay_is_killed_silent_not_error(self, tmp_path):
        out = _run_canary(tmp_path, _LIVE_STREAM.read_text())
        assert out.returncode == 1, out.stdout + out.stderr
        verdict = _verdict_line(out.stdout)
        assert verdict.startswith("KILLED")
        assert "teardown=0" in verdict
        assert "silent=1" in verdict
        assert "exit=0" in verdict
        assert "marker_missing=1" in verdict

    def test_compact_done_step_is_killed_silent(self, tmp_path):
        out = _run_canary(tmp_path, _CANARY_DONE_STEP + "\n" + _RESULT_SUCCESS + "\n")
        assert out.returncode == 1, out.stdout + out.stderr
        assert "silent=1" in _verdict_line(out.stdout)

    def test_extra_whitespace_in_the_launch_command_still_matches(self, tmp_path):
        step = _CANARY_DONE_STEP.replace('"CommandLine":"(sleep 8;', '"CommandLine":"  (sleep 8; ')
        out = _run_canary(tmp_path, step + "\n" + _RESULT_SUCCESS + "\n")
        assert out.returncode == 1, out.stdout + out.stderr
        assert "silent=1" in _verdict_line(out.stdout)

    @pytest.mark.parametrize(
        "stream",
        [
            # agy exited 0 but its terminal result reports an error.
            _LIVE_STREAM.read_text().replace('"status":"SUCCESS"', '"status":"ERROR"'),
            # agy exited 0 with no terminal result event at all.
            "".join(
                line
                for line in _LIVE_STREAM.read_text().splitlines(keepends=True)
                if '"event":"result"' not in line
            ),
            # A SUCCESS result followed by a later ERROR result: the last one counts.
            _LIVE_STREAM.read_text()
            + '{"event":"result","result":{"status":"ERROR","response":""}}\n',
        ],
        ids=["result-error", "result-missing", "last-result-error"],
    )
    def test_exit_zero_without_a_terminal_success_stays_error(self, tmp_path, stream):
        """Devin #1138: an agy-reported failure is an invocation ERROR, not a silent kill."""
        out = _run_canary(tmp_path, stream)
        assert out.returncode == 2, out.stdout + out.stderr
        assert "no_marker no_teardown_signature" in out.stderr
        assert not out.stdout.strip().startswith("KILLED")

    @pytest.mark.parametrize(
        "command",
        [
            "ls canary_marker.txt",
            "cat canary_marker.txt || true",
            # The canary's command run in the foreground launched nothing in the background.
            "(sleep 8; printf 'survived\\n' > canary_marker.txt)",
            "echo '(sleep 8; printf survived > canary_marker.txt) &'",
        ],
        ids=["ls-marker", "cat-marker", "foreground-launch", "echo-mention"],
    )
    def test_a_done_command_that_only_mentions_the_marker_stays_error(self, tmp_path, command):
        """Devin #1138: only the exact canary launch command counts as the launch."""
        step = json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": command},
                        "output": "ls: cannot access 'canary_marker.txt': No such file",
                    },
                },
            }
        )
        out = _run_canary(tmp_path, step + "\n" + _RESULT_SUCCESS + "\n")
        assert out.returncode == 2, out.stdout + out.stderr
        assert "no_marker no_teardown_signature" in out.stderr

    def test_nonzero_agy_exit_stays_error(self, tmp_path):
        out = _run_canary(tmp_path, _LIVE_STREAM.read_text(), rc=3)
        assert out.returncode == 2, out.stdout + out.stderr
        assert "no_marker no_teardown_signature" in out.stderr

    @pytest.mark.parametrize(
        "stream",
        [
            "",
            '{"event":"result","result":{"status":"SUCCESS","response":"LAUNCHED\\n"}}\n',
            # The canary step started but never finished.
            _CANARY_DONE_STEP.replace('"state":"DONE"', '"state":"ACTIVE"')
            + "\n"
            + _RESULT_SUCCESS
            + "\n",
            # A different command finished; the canary job was never launched.
            _CANARY_DONE_STEP.replace("canary_marker.txt", "other.txt")
            + "\n"
            + _RESULT_SUCCESS
            + "\n",
        ],
        ids=["empty", "result-only", "step-active", "other-command"],
    )
    def test_canary_command_never_completed_stays_error(self, tmp_path, stream):
        out = _run_canary(tmp_path, stream)
        assert out.returncode == 2, out.stdout + out.stderr
        assert "no_marker no_teardown_signature" in out.stderr

    @pytest.mark.parametrize("wait", ["5", "08", "09"])
    def test_a_marker_that_lands_after_agy_exits_is_survived(self, tmp_path, wait):
        """The wait lets a job that outlived agy's exit write its marker.

        ``08`` and ``09`` are decimal, not invalid octal (Devin #1138): the wait
        must still run rather than being skipped.
        """
        repo = tmp_path / "repo"
        fake = tmp_path / "fake-agy"
        fake.write_text(
            "#!/bin/sh\n"
            f"cat '{_LIVE_STREAM}'\n"
            f"(sleep 1; printf 'survived\\n' > '{repo / 'canary_marker.txt'}') &\n"
            "exit 0\n"
        )
        fake.chmod(0o755)
        out = subprocess.run(
            ["bash", str(_CANARY), "--agy-bin", str(fake), "--workdir", str(repo)],
            capture_output=True,
            text=True,
            timeout=60,
            env=_sandbox_confirmed_env(tmp_path, {"UA_AGY_CANARY_MARKER_WAIT_S": wait}),
        )
        assert out.returncode == 0, out.stdout + out.stderr
        assert _verdict_line(out.stdout).startswith("SURVIVED")

    @pytest.mark.parametrize("wait", ["soon", "-1", "1.5", "123456"])
    def test_invalid_wait_is_a_setup_error(self, tmp_path, wait):
        out = _run_canary(tmp_path, "", wait=wait)
        assert out.returncode == 2
        assert "UA_AGY_CANARY_MARKER_WAIT_S" in out.stderr
