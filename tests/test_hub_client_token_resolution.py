"""HubClient bearer resolution: environment first, managed store last (#1638).

#904 made ``config.agent_tokens()`` merge managed tokens, which opens the
local live store. A remote client must never do that (test_platform_cli),
yet an executor on the hub host may be provisioned only through managed
secrets (Devin review on PR #926). So the managed store is consulted only
for a loopback hub, and only when nothing else supplied a credential.
"""

from __future__ import annotations

import pytest

from unified_agent import config, device_login
from unified_agent.executor.daemon import HubClient

LABEL = "server_agent3"


@pytest.fixture
def token_sources(monkeypatch):
    for name in ("UA_EXECUTOR_TOKEN", "UA_AGENT_BEARER_TOKEN", "UA_HTTP_HUB_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(device_login, "stored_access_token", lambda **_kw: None)
    calls: list[bool] = []
    sources = {False: {}, True: {"managed-token": LABEL}}

    def fake_agent_tokens(raw=None, *, include_managed=None):
        calls.append(bool(include_managed))
        return dict(sources[bool(include_managed)])

    monkeypatch.setattr(config, "agent_tokens", fake_agent_tokens)
    return sources, calls


def test_env_token_wins_and_managed_store_is_never_opened(token_sources):
    sources, calls = token_sources
    sources[False]["env-token"] = LABEL
    client = HubClient(agent_label=LABEL, hub_url="http://127.0.0.1:8765/mcp")
    assert client.bearer_token == "env-token"
    assert calls == [False]


def test_loopback_hub_falls_back_to_managed_token(token_sources):
    _sources, calls = token_sources
    client = HubClient(agent_label=LABEL, hub_url="http://127.0.0.1:8765/mcp")
    assert client.bearer_token == "managed-token"
    assert calls == [False, True]


def test_remote_hub_never_opens_the_managed_store(token_sources):
    _sources, calls = token_sources
    client = HubClient(agent_label=LABEL, hub_url="https://hub.example.test/mcp")
    assert not client.bearer_token
    assert calls == [False]


def test_explicit_bearer_skips_the_managed_store(token_sources):
    _sources, calls = token_sources
    client = HubClient(
        agent_label=LABEL, hub_url="http://127.0.0.1:8765/mcp", bearer_token="explicit"
    )
    assert client.bearer_token == "explicit"
    assert calls == [False]
