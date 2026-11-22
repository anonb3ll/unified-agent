"""A private ACP constructor must obey the same process-level locality guard."""

import sys
import time

import pytest

from unified_agent.acp_client import AcpClient, AcpError


@pytest.mark.parametrize(
    "setting,value",
    [("UA_EXECUTION_LOCALITY", "devices"), ("UA_HOSTED", "1"), ("RAILWAY_ENVIRONMENT_ID", "test")],
)
def test_wrapped_acp_constructor_cannot_start_a_local_agent(tmp_path, monkeypatch, setting, value):
    for key in ("UA_EXECUTION_LOCALITY", "UA_HOSTED", "RAILWAY_ENVIRONMENT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(setting, value)
    marker = tmp_path / "started"
    argv = ["env", sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    client = None
    refused = False
    try:
        client = AcpClient(argv, cwd=str(tmp_path), env={}, session_dir=str(tmp_path / "session"))
        deadline = time.monotonic() + 1
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    except AcpError as exc:
        assert "UA_EXECUTION_LOCALITY=devices" in str(exc)
        refused = True
    finally:
        if client is not None:
            client.close()
    assert refused, "an env wrapper reached the ACP process constructor"
    assert not marker.exists()
