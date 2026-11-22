"""Real cross-home probes keep raw bearer configuration off argv and env."""

import json
import secrets

import pytest

from unified_agent import config
from unified_agent.tools import doctor


@pytest.mark.parametrize("raw_kind", ["none", "empty", "explicit"])
def test_cross_home_pipe_preserves_effective_raw_and_caller_home(monkeypatch, tmp_path, raw_kind):
    caller_home = tmp_path / "caller"
    hub_home = tmp_path / "hub"
    caller_bearer = secrets.token_urlsafe(32)
    hub_bearer = secrets.token_urlsafe(32)
    monkeypatch.setenv("UA_HOME", str(caller_home))
    monkeypatch.setenv("UA_AGENT_TOKENS", "server_agent1:" + caller_bearer)
    hub_env = {"UA_HOME": str(hub_home), "UA_MANAGED_SECRETS": "0"}
    monkeypatch.setattr(doctor, "_service_env", lambda unit: hub_env)
    raw = {"none": None, "empty": "", "explicit": "server_agent2:" + hub_bearer}[raw_kind]
    expected_raw = raw or ""  # A configured hub home never borrows caller tokens.
    real_run = doctor.subprocess.run
    calls = []

    def inspect_transport(cmd, **kwargs):
        # Inspect the real child invocation, then let the actual config reader run.
        exposed = json.dumps([cmd, kwargs["env"]])
        safe_metadata = caller_bearer not in exposed and hub_bearer not in exposed
        assert safe_metadata, "bearer leaked into child metadata"
        matches_payload = json.loads(kwargs["input"]) == expected_raw
        assert matches_payload, "pipe payload changed effective raw semantics"
        assert kwargs["timeout"] == doctor._HUB_TOKEN_PROBE_TIMEOUT_S
        calls.append(cmd[-1])
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", inspect_transport)
    resolved = doctor._hub_service_tokens(raw)
    expected = {hub_bearer: "server_agent2"} if raw_kind == "explicit" else {}
    matches_scope = resolved == expected
    assert matches_scope, "isolated child resolved the wrong credential scope"
    assert calls == ["agent_tokens"]
    assert config.data_home() == caller_home
