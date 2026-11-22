"""Child credential probes expose failure categories, never child diagnostics."""

import json
import secrets
import subprocess
import traceback
from types import SimpleNamespace

import pytest

from unified_agent.tools import doctor


@pytest.mark.parametrize(
    ("check_name", "expected_status"),
    [
        ("_hub_auth_check", "MISCONFIGURED"),
        ("_agent_token_collisions_check", "UNKNOWN"),
        ("_deployment_mode_check", "MISCONFIGURED"),
    ],
)
@pytest.mark.parametrize(
    ("failure", "expected_detail"),
    [
        ("timeout", "hub token probe timed out (TimeoutExpired)"),
        ("chained_spawn_error", "hub token probe failed (OSError)"),
        ("runtime_error", "hub token probe failed (RuntimeError)"),
        ("stderr", "hub token probe exited with status 7"),
        ("stdout", "hub token probe exited with status 7"),
        ("invalid_json", "hub token probe returned invalid JSON (JSONDecodeError)"),
    ],
)
def test_child_failure_detail_cannot_reflect_credentials(
    monkeypatch, tmp_path, check_name, expected_status, failure, expected_detail
):
    bearer = secrets.token_urlsafe(32)
    raw = f"Desktop_Agent2:{bearer}"
    diagnostic = f"private-child-diagnostic:{bearer}"
    hub_env = {
        "UA_HOME": str(tmp_path / "separate-hub"),
        "UA_AGENT_TOKENS": raw,
        "UA_DEPLOYMENT_MODE": "authenticated_public",
        "UA_ENFORCE_AUTH": "1",
        "UA_TRANSPORT": "http",
    }
    monkeypatch.setattr(doctor, "_service_env", lambda unit: hub_env)
    command_seen = False

    def child_failure(cmd, **kwargs):
        nonlocal command_seen
        command_seen = (
            cmd[-1] in ("agent_tokens", "agent_token_collisions")
            and json.loads(kwargs["input"]) == raw
        )
        safe_argv = bearer not in json.dumps(cmd)
        safe_env = bearer not in json.dumps(kwargs["env"])
        assert safe_argv, "credential reached child argv"
        assert safe_env, "credential reached child environment"
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=diagnostic)
        if failure == "chained_spawn_error":
            try:
                raise ValueError(diagnostic)
            except ValueError as exc:
                raise OSError(diagnostic) from exc
        if failure == "runtime_error":
            raise RuntimeError(diagnostic)
        if failure == "invalid_json":
            return SimpleNamespace(returncode=0, stdout=diagnostic, stderr="")
        return SimpleNamespace(
            returncode=7,
            stdout=diagnostic,
            stderr=diagnostic if failure == "stderr" else "",
        )

    # Patch only the child adapter; the real doctor check must still resolve
    # the distinct hub home and turn the helper failure into its public row.
    monkeypatch.setattr(doctor.subprocess, "run", child_failure)
    row = getattr(doctor, check_name)()
    leaked = any(value in json.dumps(row) for value in (bearer, raw, diagnostic, "import json"))
    assert not leaked, "doctor returned sensitive child diagnostics"
    assert command_seen, "the regression must exercise the credential pipe"
    assert row["status"] == expected_status
    assert expected_detail in row["detail"]

    # Logging the helper exception must also remain safe: TimeoutExpired and
    # the chained spawn error retain sensitive arguments in their contexts.
    with pytest.raises(RuntimeError) as caught:
        doctor._hub_service_tokens()
    rendered = "".join(traceback.format_exception(caught.value))
    traceback_leaked = any(value in rendered for value in (bearer, raw, diagnostic, "import json"))
    assert not traceback_leaked, "credential probe traceback includes sensitive diagnostics"
    assert caught.value.__cause__ is None
