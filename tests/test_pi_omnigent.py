"""Unit tests for Pi runner vault-journal integration."""

from unittest.mock import MagicMock, patch

from test_pi_runner import _stub_pi

from unified_agent.tools.pi_runner import pi_run_task


@patch("subprocess.Popen")
@patch("unified_agent.tools.vault.vault_journal")
def test_pi_run_task_with_journal_logging(mock_journal, mock_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    _stub_pi(monkeypatch)

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = ['{"type": "agent_end"}\n']
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = ("", "")
    mock_popen.return_value = mock_proc

    result = pi_run_task(
        prompt="Test task with journal",
        cwd="",
        agent_label="pi_worker",
        log_journal=True,
        timeout_s=5,
    )

    assert result["status"] == "success"
    mock_journal.assert_called_once()
    call_kwargs = mock_journal.call_args.kwargs
    assert call_kwargs["agent"] == "pi_worker"
    assert "pi-worker" in call_kwargs["tags"]
