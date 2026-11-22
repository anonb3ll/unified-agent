import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import gate_replay  # noqa: E402


@pytest.fixture
def gate_replay_conn(isolated_env):
    """Open/close a real read-only connection for tests that query the DB directly."""
    conn = gate_replay._connect_ro()
    try:
        yield conn
    finally:
        conn.close()


class TestClassifyCommand:
    def test_empty_payload_defaults_to_hard_block(self):
        assert gate_replay.classify_command("") == "hard-block"

    def test_unrecognized_payload_defaults_to_hard_block(self):
        """Explicit default -- never a silent auto-approve (Test review 3A)."""
        assert gate_replay.classify_command('{"foo": "some totally novel thing"}') == "hard-block"

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /tmp/x",
            "rm somefile",
            "dd if=/dev/zero of=/dev/sda",
            "mkfs.ext4 /dev/sda1",
            "echo x > /dev/null",
            "shred -u secret.txt",
            "git push --force origin main",
            "git reset --hard HEAD~5",
        ],
    )
    def test_hard_block_patterns(self, command):
        assert gate_replay.classify_command(f'{{"command": "{command}"}}') == "hard-block"

    @pytest.mark.parametrize(
        "command",
        [
            "echo x >/dev/sda",  # no space before /dev/ -- adversarial review finding 1
            "echo x  >   /dev/sda",  # extra whitespace, still must match
            "find . -name x -delete",  # recursive delete, previously uncaught
        ],
    )
    def test_hard_block_patterns_no_space_and_find_delete(self, command):
        """Regression tests for adversarial review finding 1: idiomatic no-space redirect
        syntax and find -delete previously evaded the substring-based hard-block check."""
        assert gate_replay.classify_command(f'{{"command": "{command}"}}') == "hard-block"

    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m x",
            "mv a.txt b.txt",
            "chmod +x script.sh",
            "curl https://x.com",
            "wget https://x.com",
            "mkdir newdir",
        ],
    )
    def test_notify_patterns(self, command):
        assert gate_replay.classify_command(f'{{"command": "{command}"}}') == "notify"

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "cat file.txt",
            "grep foo bar.txt",
            "find . -name x",
            "echo hello",
            "git status",
            "git diff",
            "git log",
        ],
    )
    def test_auto_patterns(self, command):
        assert gate_replay.classify_command(f'{{"command": "{command}"}}') == "auto"

    def test_hard_block_takes_precedence_over_notify(self):
        """Bucket precedence: hard-block is checked first in classify_command's loop order."""
        assert (
            gate_replay.classify_command('{"command": "git commit -m x && rm -rf /"}')
            == "hard-block"
        )


class TestSummarizeApproval:
    def test_no_raw_payload_in_summary(self):
        """The whole point of the allowlist-summary redesign (tension 3) -- verify it."""
        secret_payload = '{"command": "curl -H \\"Authorization: Bearer sk-supersecret123\\""}'
        row = {
            "tool": "run_cmd",
            "status": "denied",
            "payload": secret_payload,
            "created_at": "2026-07-18T12:01:30+00:00",
            "resolved_at": "2026-07-18T12:02:00+00:00",
        }
        summary = gate_replay.summarize_approval(row)
        serialized = str(summary)
        assert "sk-supersecret123" not in serialized
        assert secret_payload not in serialized
        assert set(summary.keys()) == {
            "tool",
            "status",
            "created_at",
            "resolved_at",
            "bucket",
        }

    def test_no_payload_fingerprint_in_summary(self):
        """Red-team finding: hash+length together are a dictionary-attack confirmation
        oracle against low-entropy JSON. Neither field should exist at all."""
        row = {
            "tool": "run_cmd",
            "status": "denied",
            "payload": '{"command": "git status"}',
            "created_at": "2026-07-18T12:01:30+00:00",
            "resolved_at": None,
        }
        summary = gate_replay.summarize_approval(row)
        assert "content_sha256" not in summary
        assert "payload_length" not in summary

    def test_summary_fields_populated(self):
        row = {
            "tool": "delegate",
            "status": "denied",
            "payload": '{"task": "x"}',
            "created_at": "2026-07-21T09:37:02+00:00",
            "resolved_at": None,
        }
        summary = gate_replay.summarize_approval(row)
        assert summary["tool"] == "delegate"
        assert summary["status"] == "denied"
        assert summary["resolved_at"] is None
        assert summary["bucket"] == gate_replay._NON_COMMAND_BUCKET

    def test_null_payload_falls_back_to_empty_string(self):
        """approvals.payload is NOT NULL DEFAULT '{}' in the schema, but a plain dict
        (as sqlite3.Row supports) can still yield None -- the `or ""` fallback covers it."""
        row = {
            "tool": "run_cmd",
            "status": "pending",
            "payload": None,
            "created_at": "2026-07-18T12:01:30+00:00",
            "resolved_at": None,
        }
        summary = gate_replay.summarize_approval(row)
        assert summary["bucket"] == "hard-block"

    def test_only_run_cmd_gets_shell_verb_classification(self):
        """Adversarial review finding 2: delegate/git_commit_staged payloads are free
        text (task descriptions, commit messages), not shell commands -- running the
        shell-verb heuristic over them produces noise. A delegate task_spec containing
        the word "echo" must NOT be misclassified as "auto"."""
        row = {
            "tool": "delegate",
            "status": "pending",
            "payload": '{"task_spec": "review the report and echo status back when done"}',
            "created_at": "2026-07-21T09:37:02+00:00",
            "resolved_at": None,
        }
        summary = gate_replay.summarize_approval(row)
        assert summary["bucket"] == gate_replay._NON_COMMAND_BUCKET

    def test_git_commit_staged_gets_non_command_bucket_regardless_of_message_content(self):
        row = {
            "tool": "git_commit_staged",
            "status": "pending",
            "payload": '{"message": "chore: mkdir build output dir and mv artifacts in"}',
            "created_at": "2026-07-21T09:37:02+00:00",
            "resolved_at": None,
        }
        summary = gate_replay.summarize_approval(row)
        assert summary["bucket"] == gate_replay._NON_COMMAND_BUCKET

    def test_run_cmd_still_gets_real_classification(self):
        row = {
            "tool": "run_cmd",
            "status": "approved",
            "payload": '{"command": "git status"}',
            "created_at": "2026-07-21T09:37:02+00:00",
            "resolved_at": None,
        }
        summary = gate_replay.summarize_approval(row)
        assert summary["bucket"] == "auto"


class TestFormatReport:
    def test_no_raw_payload_text_in_rendered_report(self):
        summaries = [
            gate_replay.summarize_approval(
                {
                    "tool": "run_cmd",
                    "status": "denied",
                    "payload": '{"command": "curl -H \\"Authorization: Bearer sk-topsecret\\""}',
                    "created_at": "2026-07-18T12:01:30+00:00",
                    "resolved_at": "2026-07-18T12:02:00+00:00",
                }
            )
        ]
        report = gate_replay.format_report(summaries, {"fs_write": 3}, "2026-07-18", ("run_cmd",))
        assert "sk-topsecret" not in report
        assert "curl -H" not in report

    def test_fs_write_cross_tab_caveat_present(self):
        report = gate_replay.format_report([], {"fs_write": 5}, "2026-07-18", ())
        assert "no path/payload field" in report
        assert "not a worktree-boundary rate" in report

    def test_sample_size_stated(self):
        report = gate_replay.format_report([], {}, "2026-07-18", ())
        assert "Sample size: 0" in report

    def test_empty_summaries_still_produces_valid_report(self):
        """Zero-rows edge case (Test review gap) -- must not crash."""
        report = gate_replay.format_report([], {}, "2026-07-18", ())
        assert "(no rows in window)" in report

    def test_workflow_run_caveat_present(self):
        """My own discovery while fixing the adversarial findings: workflow_run's own
        invocation is never gated by name (tests/test_workflow.py confirms step-level
        rows are attributed to the step's own tool). Rows will structurally always be
        zero -- the report must say so, not imply it's live-data evidence of safety."""
        report = gate_replay.format_report([], {}, "2026-07-18", ("workflow_run",))
        assert "workflow_run" in report
        assert "always be zero" in report

    def test_heading_tracks_tool_count(self):
        """Heading text interpolates len(tools) instead of hardcoding a number (maintainability finding)."""
        report = gate_replay.format_report([], {}, "2026-07-18", ("run_cmd", "delegate"))
        assert "## Approval History (all 2 currently-gated tools)" in report

    def test_bucket_summary_tallies_mixed_rows(self):
        summaries = [
            gate_replay.summarize_approval(
                {
                    "tool": "run_cmd",
                    "status": "denied",
                    "payload": '{"command": "rm -rf /"}',
                    "created_at": "2026-07-18T12:00:00+00:00",
                    "resolved_at": None,
                }
            ),
            gate_replay.summarize_approval(
                {
                    "tool": "run_cmd",
                    "status": "denied",
                    "payload": '{"command": "git commit -m x"}',
                    "created_at": "2026-07-21T09:37:02+00:00",
                    "resolved_at": None,
                }
            ),
            gate_replay.summarize_approval(
                {
                    "tool": "run_cmd",
                    "status": "approved",
                    "payload": '{"command": "git status"}',
                    "created_at": "2026-07-19T20:37:44+00:00",
                    "resolved_at": None,
                }
            ),
            gate_replay.summarize_approval(
                {
                    "tool": "delegate",
                    "status": "denied",
                    "payload": '{"task_spec": "anything"}',
                    "created_at": "2026-07-21T09:37:02+00:00",
                    "resolved_at": None,
                }
            ),
        ]
        report = gate_replay.format_report(summaries, {}, "2026-07-18", ("run_cmd", "delegate"))
        assert "| hard-block | 1 |" in report
        assert "| notify | 1 |" in report
        assert "| auto | 1 |" in report
        assert f"| {gate_replay._NON_COMMAND_BUCKET} | 1 |" in report

    def test_other_gated_tools_volume_table_has_all_four(self):
        perf_counts = {
            "run_cmd": 6,
            "workflow_run": 0,
            "git_commit_staged": 3,
            "delegate": 1,
            "fs_write": 0,
        }
        tools = ("run_cmd", "workflow_run", "git_commit_staged", "delegate")
        report = gate_replay.format_report([], perf_counts, "2026-07-18", tools)
        assert "| run_cmd | 6 |" in report
        assert "| workflow_run | 0 |" in report
        assert "| git_commit_staged | 3 |" in report
        assert "| delegate | 1 |" in report


class TestWriteReport:
    def test_writes_to_vault_inbox(self, isolated_env):
        from unified_agent import config

        config.vault_root().mkdir(parents=True, exist_ok=True)
        path = gate_replay.write_report("# test report\n")
        assert path is not None
        assert Path(path).exists()
        assert "test report" in Path(path).read_text()

    def test_falls_back_to_stdout_on_vault_failure(self, isolated_env, monkeypatch, capsys):
        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("unified_agent.safety.resolve_in_vault", _boom)
        result = gate_replay.write_report("# test report\n")
        assert result is None
        captured = capsys.readouterr()
        assert "test report" in captured.out


class TestConnectRo:
    def test_missing_db_raises_operational_error(self, isolated_env):
        """DB-open failure must be a clear, catchable error -- never a silent empty report.

        isolated_env's autouse fixture already ran storage.init_db(), so simulate
        the failure by removing the file mode=ro refuses to create.
        """
        from unified_agent import config

        Path(config.db_path()).unlink()
        with pytest.raises(sqlite3.OperationalError):
            gate_replay._connect_ro()

    def test_success_path_opens_with_row_factory(self, gate_replay_conn):
        assert gate_replay_conn.row_factory is sqlite3.Row
        # A read-only connection can still SELECT against the real schema.
        gate_replay_conn.execute("SELECT COUNT(*) FROM approvals").fetchone()


class TestFetchApprovals:
    def _seed_approval(self, tool, payload, status="pending"):
        """Seed a real approvals row the same way check_approval_gate does."""
        import unified_agent.tools.approvals as approvals_mod

        canon = approvals_mod._canonical_payload(payload)
        conn = approvals_mod.storage.connect()
        ts = approvals_mod.storage.now()
        conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tool, canon, "test-agent", status, ts, ts),
        )
        conn.commit()

    def test_fetches_only_requested_tools_in_window(self, gate_replay_conn):
        self._seed_approval("run_cmd", {"command": "git status"}, status="approved")
        self._seed_approval("delegate", {"task": "x"}, status="denied")
        # fs_write is not in the requested tool set -- must not appear in results.
        self._seed_approval("fs_write", {"path": "x"}, status="pending")

        summaries = gate_replay.fetch_approvals(
            gate_replay_conn, "2020-01-01", ("run_cmd", "delegate")
        )

        tools = {s["tool"] for s in summaries}
        assert tools == {"run_cmd", "delegate"}
        assert "fs_write" not in tools

    def test_since_filter_excludes_older_rows(self, gate_replay_conn):
        self._seed_approval("run_cmd", {"command": "git status"}, status="approved")

        # Filtering on a future date should exclude the row just seeded.
        summaries = gate_replay.fetch_approvals(gate_replay_conn, "2099-01-01", ("run_cmd",))
        assert summaries == []

    def test_empty_table_returns_empty_list(self, gate_replay_conn):
        summaries = gate_replay.fetch_approvals(gate_replay_conn, "2020-01-01", ("run_cmd",))
        assert summaries == []

    def test_empty_tools_tuple_returns_empty_list_without_querying(self, gate_replay_conn):
        """Guards the empty-IN-clause SQL edge case now that tools comes from the live,
        possibly-empty UA_APPROVAL_TOOLS config instead of a hardcoded non-empty tuple."""
        self._seed_approval("run_cmd", {"command": "git status"}, status="approved")
        summaries = gate_replay.fetch_approvals(gate_replay_conn, "2020-01-01", ())
        assert summaries == []


class TestFetchPerfCounts:
    def test_groups_by_tool(self, gate_replay_conn):
        from unified_agent import storage

        storage.record_perf_sample("fs_write", 12.3, ok=True)
        storage.record_perf_sample("fs_write", 8.1, ok=True)
        storage.record_perf_sample("run_cmd", 50.0, ok=False, error="denied")

        counts = gate_replay.fetch_perf_counts(gate_replay_conn, "2020-01-01")
        assert counts["fs_write"] == 2
        assert counts["run_cmd"] == 1

    def test_empty_table_returns_empty_dict(self, gate_replay_conn):
        counts = gate_replay.fetch_perf_counts(gate_replay_conn, "2020-01-01")
        assert counts == {}


class TestGatedTools:
    def test_derives_from_live_config_not_a_hardcoded_snapshot(self, monkeypatch):
        """Maintainability finding: the tool list must track UA_APPROVAL_TOOLS live,
        not drift from it as a separate hardcoded tuple."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd:fs_write")
        assert gate_replay.gated_tools() == ("fs_write", "run_cmd")

    def test_empty_env_returns_empty_tuple(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        assert gate_replay.gated_tools() == ()


class TestMain:
    def test_end_to_end_success_writes_report_and_returns_zero(
        self, isolated_env, monkeypatch, capsys
    ):
        from unified_agent import config, storage

        config.vault_root().mkdir(parents=True, exist_ok=True)
        storage.record_perf_sample("fs_write", 5.0, ok=True)
        monkeypatch.setattr(sys, "argv", ["gate_replay.py", "--since", "2020-01-01"])

        exit_code = gate_replay.main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Gate replay report written to:" in captured.out

    def test_db_open_failure_returns_nonzero(self, isolated_env, monkeypatch, capsys):
        from unified_agent import config

        Path(config.db_path()).unlink()
        monkeypatch.setattr(sys, "argv", ["gate_replay.py"])

        exit_code = gate_replay.main()

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "error:" in captured.err

    def test_falls_back_to_stdout_and_still_returns_zero(self, isolated_env, monkeypatch, capsys):
        """isolated_env leaves the vault dir unconfigured by default, so write_report's
        vault write fails and main() must fall back to stdout without erroring out."""
        monkeypatch.setattr(sys, "argv", ["gate_replay.py", "--since", "2020-01-01"])

        exit_code = gate_replay.main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Gate replay report written to:" not in captured.out
        assert "generated:" in captured.out
