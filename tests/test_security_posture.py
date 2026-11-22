"""Default-posture regressions: HTTP enforces, stdio stays open, delegate is gated."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, safety


def _reset(monkeypatch) -> None:
    for name in (
        "UA_ENFORCE_AUTH",
        "UA_TRANSPORT",
        "UA_LEGACY_OPEN_POSTURE",
        "UA_TRUST_STDIO",
        "UA_REQUIRE_CALLER_IDENTITY",
        "UA_APPROVAL_BASELINE",
        "UA_APPROVAL_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    config._POSTURE_WARNED.clear()


@pytest.fixture(autouse=True)
def _restore_posture_warnings():
    """Don't re-arm once-per-process warnings for tests that run after these."""
    saved = set(config._POSTURE_WARNED)
    yield
    config._POSTURE_WARNED.clear()
    config._POSTURE_WARNED.update(saved)


class TestEnforceAuth:
    def test_unset_http_enforces_once(self, monkeypatch, capsys):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        assert config.enforce_auth() is True
        err = capsys.readouterr().err
        assert "UA_LEGACY_OPEN_POSTURE" in err
        assert config.enforce_auth() is True
        assert capsys.readouterr().err == ""

    def test_unset_stdio_is_open_and_warns_once(self, monkeypatch, capsys):
        _reset(monkeypatch)
        assert config.enforce_auth() is False
        err = capsys.readouterr().err
        assert "[Warning]" in err
        assert "UA_TRUST_STDIO" in err
        assert "UA_REQUIRE_CALLER_IDENTITY" in err
        assert config.enforce_auth() is False
        assert capsys.readouterr().err == ""

    def test_trust_stdio_acknowledges_once(self, monkeypatch, capsys):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRUST_STDIO", "1")
        assert config.enforce_auth() is False
        err = capsys.readouterr().err
        assert "UA_TRUST_STDIO=1" in err
        assert "[Warning]" not in err
        assert config.enforce_auth() is False
        assert capsys.readouterr().err == ""

    def test_legacy_open_posture_is_warn_mode(self, monkeypatch, capsys):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setenv("UA_LEGACY_OPEN_POSTURE", "1")
        assert config.enforce_auth() is False
        err = capsys.readouterr().err
        assert "[Warning]" in err
        assert "UA_LEGACY_OPEN_POSTURE=1" in err
        assert "warn-mode" in err

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", True),
            ("true", True),
            ("YES", True),
            ("0", False),
            ("false", False),
            ("No", False),
        ],
    )
    def test_explicit_values_win_on_http(self, monkeypatch, capsys, raw, expected):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setenv("UA_ENFORCE_AUTH", raw)
        assert config.enforce_auth() is expected
        assert capsys.readouterr().err == ""

    def test_explicit_off_wins_over_http_default(self, monkeypatch, capsys):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        assert config.enforce_auth() is False
        assert capsys.readouterr().err == ""

    def test_explicit_on_wins_on_stdio(self, monkeypatch, capsys):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        assert config.enforce_auth() is True
        assert capsys.readouterr().err == ""


class TestProdActionGate:
    def test_http_anonymous_caller_is_denied(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        with pytest.raises(ToolError, match="anonymous callers cannot run prod action 'merge'"):
            safety.check_prod_action_gate("git_merge_task", {"task_id": 42})

    def test_stdio_anonymous_caller_skips(self, monkeypatch, capsys):
        _reset(monkeypatch)
        safety.check_prod_action_gate("git_merge_task", {"task_id": 42})
        assert "UA_TRUST_STDIO" in capsys.readouterr().err

    def test_stdio_require_caller_identity_denies(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_REQUIRE_CALLER_IDENTITY", "1")
        with pytest.raises(ToolError, match="anonymous"):
            safety.check_prod_action_gate("task_delete", {"id": 7})

    def test_legacy_open_posture_skips_on_http(self, monkeypatch, capsys):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setenv("UA_LEGACY_OPEN_POSTURE", "1")
        safety.check_prod_action_gate("node_dispatch", {})
        assert "UA_LEGACY_OPEN_POSTURE=1" in capsys.readouterr().err

    def test_unknown_tool_is_not_a_prod_action(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        safety.check_prod_action_gate("not_a_prod_tool", {})


class TestApprovalBaseline:
    def test_delegate_is_gated_by_default(self, monkeypatch, capsys):
        _reset(monkeypatch)
        assert config.approval_gated_tools() == {"delegate"}
        assert "UA_APPROVAL_BASELINE=0" not in capsys.readouterr().err

    def test_explicit_tools_are_unioned(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        assert config.approval_gated_tools() == {"delegate", "run_cmd"}

    def test_baseline_off_is_empty_and_warns_once(self, monkeypatch, capsys):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_APPROVAL_BASELINE", "0")
        assert config.approval_gated_tools() == set()
        err = capsys.readouterr().err
        assert "UA_APPROVAL_BASELINE=0" in err
        assert "[Warning]" in err
        assert config.approval_gated_tools() == set()
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("raw", ["false", "no", "FALSE"])
    def test_baseline_off_tokens(self, monkeypatch, raw):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_APPROVAL_BASELINE", raw)
        assert "delegate" not in config.approval_gated_tools()

    def test_baseline_off_keeps_explicit_delegate(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_APPROVAL_BASELINE", "0")
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate:run_cmd")
        assert config.approval_gated_tools() == {"delegate", "run_cmd"}

    def test_legacy_open_posture_suppresses_baseline(self, monkeypatch, capsys):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_LEGACY_OPEN_POSTURE", "1")
        assert config.approval_gated_tools() == set()
        assert "UA_LEGACY_OPEN_POSTURE=1" in capsys.readouterr().err

    def test_legacy_open_posture_keeps_explicit_entries(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_LEGACY_OPEN_POSTURE", "1")
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd:delegate")
        assert config.approval_gated_tools() == {"run_cmd", "delegate"}

    def test_pi_run_task_is_not_in_the_baseline(self, monkeypatch):
        _reset(monkeypatch)
        assert "pi_run_task" not in config.approval_gated_tools()
        assert "git_merge_task" not in config.approval_gated_tools()
        assert "node_dispatch" not in config.approval_gated_tools()


class TestVerifyWriteIdentity:
    def test_stdio_still_trusts_a_claimed_author(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        token = config.agent_identity_var.set(None)
        try:
            assert config.verify_write_identity("test-agent") == "test-agent"
        finally:
            config.agent_identity_var.reset(token)

    def test_http_rejects_an_unverified_claim(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        token = config.agent_identity_var.set(None)
        try:
            assert config.verify_write_identity("test-agent") is None
        finally:
            config.agent_identity_var.reset(token)

    def test_require_caller_identity_rejects_on_stdio(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_REQUIRE_CALLER_IDENTITY", "1")
        token = config.agent_identity_var.set(None)
        try:
            assert config.verify_write_identity("test-agent") is None
        finally:
            config.agent_identity_var.reset(token)

    def test_http_mismatch_uses_the_enforcing_default(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setenv("UA_TRANSPORT", "http")
        token = config.agent_identity_var.set("AgentA")
        try:
            with pytest.raises(ToolError, match="Auth identity mismatch"):
                config.verify_write_identity("AgentB")
        finally:
            config.agent_identity_var.reset(token)
