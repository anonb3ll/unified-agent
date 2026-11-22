"""Completion journals are lookupable indexes accepted by the real vault cap."""

from pathlib import Path

import pytest
from test_executor import TestWorktreeBindsToTheWorkspaceRepo as _ExecutorCases
from test_executor import setup_db as setup_db

from unified_agent import config
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.tools import delegate as delegates


@pytest.mark.parametrize("product", [False, True])
def test_long_isolated_checkout_completion_writes_a_real_bounded_journal(
    isolated_env, monkeypatch, product
):
    Path(config.vault_root()).mkdir(parents=True, exist_ok=True)
    cases = _ExecutorCases()
    cases._workspace_repo(isolated_env)
    if product:
        cases._product_repo()
    created = delegates.delegate(
        "[code-task] [project: unified-agent] Change product source."
        if product
        else "[code-task] Change workspace source.",
        "Desktop_Agent1",
        "Desktop_Agent2",
        callback="none",
    )
    daemon = ExecutorDaemon(
        agent_label="Desktop_Agent2", shadow_mode=False, backend=cases.FakeBackend(write=True)
    )
    real_call = daemon.hub_client.call_tool
    accepted = []

    def capture(tool, arguments):
        result = real_call(tool, arguments)
        if tool == "vault_journal":
            accepted.append((arguments, result))
        return result

    monkeypatch.setattr(daemon.hub_client, "call_tool", capture)
    result = daemon.poll_once()
    assert result["waiting_for_review"] is True
    assert len(accepted) == 1
    arguments, journal = accepted[0]
    assert len(arguments["entry"]) <= 240
    assert f"task #{created['task_id']}" in arguments["entry"]
    assert f"delegation #{created['delegation_id']}" in arguments["entry"]
    task = cases._task_row(created["task_id"])
    assert task["current_run_id"] in arguments["entry"]
    assert "generation=1" in arguments["entry"]
    assert "success" in arguments["entry"]
    assert arguments["entry"] in journal["entry"]
    assert arguments["entry"] in (Path(config.vault_root()) / journal["path"]).read_text()
