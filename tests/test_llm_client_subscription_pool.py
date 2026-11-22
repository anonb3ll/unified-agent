import httpx
import pytest

from unified_agent import llm_client


def _response(text: str, model: str | None = None) -> httpx.Response:
    payload: dict = {"choices": [{"message": {"content": text}}]}
    if model is not None:
        payload["model"] = model
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "http://subscription-pool.test/v1/chat/completions"),
    )


def test_fresh_install_chain_is_unchanged_by_the_subscription_leg(monkeypatch):
    """Off-by-default evidence for task #1154: with no UA_SUBSCRIPTION_POOL_*
    env set and no per-call opt-in (the state of a fresh install), `complete`
    runs exactly the default two-leg claude_cli -> openrouter chain
    (task #2025) — the subscription leg is absent from the route
    entirely, not merely unreachable."""
    monkeypatch.delenv("UA_SUBSCRIPTION_POOL_ENABLED", raising=False)
    monkeypatch.delenv("UA_SUBSCRIPTION_POOL_URL", raising=False)
    monkeypatch.delenv("UA_SUBSCRIPTION_POOL_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def fake_post(url, **kwargs):
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(llm_client.LLMConnectionError) as exc_info:
        llm_client.complete("prompt")  # subscription_pool flag defaults False

    assert [attempt["target"] for attempt in exc_info.value.route] == [
        "claude_cli",
        "openrouter",
    ]


def test_subscription_pool_is_not_contacted_without_per_call_opt_in(monkeypatch):
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://subscription-pool.test/v1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(llm_client.LLMConnectionError) as exc_info:
        llm_client.complete("prompt")

    assert [attempt["target"] for attempt in exc_info.value.route] == [
        "claude_cli",
        "openrouter",
    ]
    assert all("subscription-pool.test" not in url for url in calls)


def test_subscription_pool_requires_global_and_per_call_opt_in(monkeypatch):
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://subscription-pool.test/v1")
    monkeypatch.setattr(
        llm_client,
        "_PROVIDER_CHAIN",
        [(name, lambda *args, **kwargs: None) for name in ("ollama", "ai_studio", "openrouter")],
    )
    monkeypatch.setattr(
        llm_client,
        "_call_subscription_pool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pool was contacted")),
    )

    with pytest.raises(llm_client.LLMConnectionError) as exc_info:
        llm_client.complete("prompt")

    assert [attempt["target"] for attempt in exc_info.value.route] == [
        "ollama",
        "ai_studio",
        "openrouter",
    ]


def test_subscription_pool_is_inserted_before_openrouter_when_explicitly_enabled(monkeypatch):
    """task #1154: the leg sits immediately ahead of openrouter, so a
    successful subscription-pool call short-circuits the chain and
    openrouter — and its spend — is never reached."""
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://subscription-pool.test/v1")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_MODEL", "claude-subscription")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_API_KEY", "test-management-key")
    monkeypatch.setattr(
        llm_client,
        "_PROVIDER_CHAIN",
        [(name, lambda *args, **kwargs: None) for name in ("ollama", "ai_studio", "openrouter")],
    )

    def fail_openrouter(*args, **kwargs):
        raise AssertionError("openrouter should not be reached: subscription pool succeeded")

    monkeypatch.setattr(llm_client, "_call_openrouter", fail_openrouter)

    def fake_post(url, **kwargs):
        assert url == "http://subscription-pool.test/v1/chat/completions"
        assert kwargs["json"]["model"] == "claude-subscription"
        assert kwargs["headers"]["Authorization"] == "Bearer test-management-key"
        return _response("from subscription pool", kwargs["json"]["model"])

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm_client.complete("prompt", subscription_pool=True)

    assert result["provider"] == "subscription_pool"
    assert result["text"] == "from subscription pool"
    # subscription_pool comes third, before openrouter, and the chain stops
    # there on success — openrouter is never attempted (no spend).
    assert [attempt["target"] for attempt in result["route"]] == [
        "ollama",
        "ai_studio",
        "subscription_pool",
    ]


def test_subscription_pool_down_falls_through_to_openrouter_and_logs_it(monkeypatch):
    """task #1154 failure mode: proxy unreachable -> llm_client falls through
    to the next leg (openrouter), and that fallthrough is visible in the
    returned `route` log — subscription_pool recorded ok=False, openrouter
    recorded ok=True, in that order."""
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://subscription-pool.test/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(
        llm_client,
        "_PROVIDER_CHAIN",
        [
            ("ollama", lambda *args, **kwargs: None),
            ("ai_studio", lambda *args, **kwargs: None),
            ("openrouter", llm_client._call_openrouter),
        ],
    )

    def fake_post(url, **kwargs):
        if "subscription-pool.test" in url:
            raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))
        assert "openrouter.ai" in url
        return _response("from openrouter")

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm_client.complete("prompt", subscription_pool=True)

    assert result["provider"] == "openrouter"
    assert result["text"] == "from openrouter"
    route_by_target = {attempt["target"]: attempt["ok"] for attempt in result["route"]}
    assert [attempt["target"] for attempt in result["route"]] == [
        "ollama",
        "ai_studio",
        "subscription_pool",
        "openrouter",
    ]
    assert route_by_target["subscription_pool"] is False
    assert route_by_target["openrouter"] is True
