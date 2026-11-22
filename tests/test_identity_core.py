"""identity_core holds the WorkOS hub leg and the OAuth provider lookup (#1721).

The old paths (``workos_hub_auth.<name>``, ``oauth.get_provider``) keep
working, and a patch made through either module reaches every caller.
"""

import pytest

from unified_agent import config, identity_core, oauth, workos_hub_auth


def test_moved_names_resolve_to_the_same_objects():
    for name in workos_hub_auth.MOVED_TO_IDENTITY_CORE:
        assert getattr(workos_hub_auth, name) is getattr(identity_core, name)
    from unified_agent.workos_hub_auth import WorkOSHubAuth, hub_auth

    assert WorkOSHubAuth is identity_core.WorkOSHubAuth
    assert hub_auth is identity_core.hub_auth


def test_a_patch_through_the_old_module_reaches_the_startup_guard(monkeypatch, capsys):
    for name in ("UA_AGENT_TOKENS", "UA_MCP_OAUTH", "UA_HUB_WORKOS_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    original = identity_core.enabled

    with monkeypatch.context() as m:
        m.setattr(workos_hub_auth, "enabled", lambda: True)
        assert identity_core.enabled() is True
        config.require_configured_http_credentials()  # the WorkOS leg counts

    # Undoing the patch restores both modules, and the guard refuses again.
    assert identity_core.enabled is original
    assert workos_hub_auth.enabled is original
    with pytest.raises(SystemExit):
        config.require_configured_http_credentials()
    assert "refusing to start" in capsys.readouterr().err


def test_a_patch_on_identity_core_is_seen_through_the_old_module(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(identity_core, "hub_auth", lambda: sentinel)
    assert workos_hub_auth.hub_auth() is sentinel
    assert config.mcp_oauth_human_approver_configured() is True


def test_a_patched_build_reaches_the_cached_leg(monkeypatch):
    identity_core.reset_hub_auth_cache()
    leg = object()
    monkeypatch.setattr(workos_hub_auth, "build_hub_auth", lambda: leg)
    try:
        assert identity_core.hub_auth() is leg
    finally:
        workos_hub_auth.reset_hub_auth_cache()
    assert identity_core._cache is None


def test_oauth_provider_lookup_follows_oauth_get_provider(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(oauth, "get_provider", lambda: sentinel)
    assert identity_core.oauth_provider() is sentinel
    monkeypatch.setattr(oauth, "get_provider", lambda: None)
    assert identity_core.oauth_provider() is None


def test_oauth_provider_is_none_when_oauth_is_off(monkeypatch):
    monkeypatch.delenv("UA_MCP_OAUTH", raising=False)
    oauth.reset_provider()
    assert identity_core.oauth_provider() is None


def test_the_web_entry_point_registers_the_oauth_lookup():
    """connector_oauth.begin is reached through webui routes; loading them alone
    (no server, no explicit oauth import) must register the provider lookup."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys\n"
        "import unified_agent.webui\n"
        "from unified_agent import identity_core\n"
        "sys.modules['unified_agent.oauth'].get_provider = lambda: 'hub-provider'\n"
        "print(identity_core.oauth_provider())\n"
    )
    src = str(Path(identity_core.__file__).parents[1])
    out = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        timeout=120,
        capture_output=True,
        env={"PYTHONPATH": src},
    )
    assert out.stdout.strip().splitlines()[-1] == b"hub-provider"
