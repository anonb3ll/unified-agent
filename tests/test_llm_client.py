import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, llm_client, run_records, storage
from unified_agent.tools import gemini as gemini_tool
from unified_agent.tools.tasks import task_add, task_claim, task_update


class TestBatteryCheck:
    def test_no_power_supply_info_is_not_on_battery(self, monkeypatch):
        monkeypatch.setenv(
            "UA_POWER_SUPPLY_PATH", ""
        )  # conftest default; explicit here for clarity
        assert llm_client.is_on_battery() is False

    def _fake_power_supply(self, tmp_path, kind: str, status: str | None = None) -> None:
        base = tmp_path / "power_supply"
        entry = base / "BAT0" if kind == "Battery" else base / "AC0"
        entry.mkdir(parents=True)
        (entry / "type").write_text(kind)
        if status is not None:
            (entry / "status").write_text(status)

    def test_discharging_battery_is_on_battery(self, monkeypatch, tmp_path):
        self._fake_power_supply(tmp_path, "Battery", "Discharging")
        monkeypatch.setenv("UA_POWER_SUPPLY_PATH", str(tmp_path / "power_supply"))
        assert llm_client.is_on_battery() is True

    def test_charging_battery_is_not_on_battery(self, monkeypatch, tmp_path):
        self._fake_power_supply(tmp_path, "Battery", "Charging")
        monkeypatch.setenv("UA_POWER_SUPPLY_PATH", str(tmp_path / "power_supply"))
        assert llm_client.is_on_battery() is False

    def test_ac_only_is_not_on_battery(self, monkeypatch, tmp_path):
        self._fake_power_supply(tmp_path, "Mains", "")
        monkeypatch.setenv("UA_POWER_SUPPLY_PATH", str(tmp_path / "power_supply"))
        assert llm_client.is_on_battery() is False

    def test_missing_directory_is_not_on_battery(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_POWER_SUPPLY_PATH", str(tmp_path / "does-not-exist"))
        assert llm_client.is_on_battery() is False


def _with_explicit_ai_studio(monkeypatch) -> None:
    """The pre-#2025 order with ai_studio, for tests of that leg's behaviour.

    ai_studio left the default chain (task #2025) but `_call_ai_studio` is kept
    for explicit callers, so its failure classification is still tested.
    """
    monkeypatch.setattr(
        llm_client,
        "_PROVIDER_CHAIN",
        [
            ("ollama", llm_client._call_ollama),
            ("ai_studio", llm_client._call_ai_studio),
            ("openrouter", llm_client._call_openrouter),
        ],
    )


def _with_explicit_ollama(monkeypatch) -> None:
    """Ollama left the default chain (task #2025); its leg is tested explicitly."""
    monkeypatch.setattr(llm_client, "_PROVIDER_CHAIN", [("ollama", llm_client._call_ollama)])


def _fake_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200, json=payload, request=httpx.Request("POST", "https://example.invalid")
    )


class TestProviderChain:
    """Default order (task #2025): claude_cli -> openrouter, nothing else. conftest
    stubs the claude binary search, so claude_cli reads as missing here; its
    own behaviour is covered in test_llm_client_claude_cli.py."""

    def test_openrouter_answers_when_claude_cli_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

        def fake_post(url, **kwargs):
            assert "openrouter.ai" in url
            assert kwargs["headers"]["Authorization"] == "Bearer test-key"
            assert kwargs["json"]["model"] == "deepseek/deepseek-v4.1-flash"
            return _fake_response(
                {
                    "choices": [{"message": {"content": "hi from openrouter"}}],
                    "model": kwargs["json"]["model"],
                }
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        result = llm_client.complete("prompt")
        route = result.pop("route")
        assert result == {
            "text": "hi from openrouter",
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4.1-flash",
        }
        assert [attempt["target"] for attempt in route] == ["claude_cli", "openrouter"]
        assert route[0]["reason"] == llm_client.CLAUDE_CLI_MISSING_REASON

    def test_openrouter_model_override(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setenv("UA_OPENROUTER_MODEL", "z-ai/glm-5.3-flash")
        seen = {}

        def fake_post(url, **kwargs):
            seen["model"] = kwargs["json"]["model"]
            return _fake_response(
                {"choices": [{"message": {"content": "hi"}}], "model": seen["model"]}
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        assert llm_client.complete("prompt")["model"] == "z-ai/glm-5.3-flash"
        assert seen["model"] == "z-ai/glm-5.3-flash"

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_openrouter_model_uses_the_pinned_default(self, monkeypatch, raw):
        """A blank UA_OPENROUTER_MODEL means the default, as doctor reads it,
        never an empty model id sent to OpenRouter."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setenv("UA_OPENROUTER_MODEL", raw)
        seen = {}

        def fake_post(url, **kwargs):
            seen["model"] = kwargs["json"]["model"]
            return _fake_response(
                {"choices": [{"message": {"content": "hi"}}], "model": seen["model"]}
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        assert llm_client.complete("prompt")["model"] == llm_client.DEFAULT_OPENROUTER_MODEL
        assert seen["model"] == llm_client.DEFAULT_OPENROUTER_MODEL

    def test_local_ollama_is_never_tried(self, monkeypatch):
        """Operator decision 2026-10-13: no local model in the default chain."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        urls = []

        def fake_post(url, **kwargs):
            urls.append(url)
            raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt")
        assert [a["target"] for a in exc_info.value.route] == ["claude_cli", "openrouter"]
        assert not any("11434" in url for url in urls)

    def test_gemini_key_does_not_put_ai_studio_back_in_the_chain(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr(
            gemini_tool,
            "gemini_ask",
            MagicMock(side_effect=AssertionError("ai_studio must not be tried")),
        )
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kwargs: _fake_response({"choices": [{"message": {"content": "hi"}}]}),
        )
        assert llm_client.complete("prompt")["provider"] == "openrouter"

    def test_all_providers_unavailable_raises(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        def fake_post(url, **kwargs):
            raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(llm_client.LLMConnectionError, match="claude_cli, openrouter$"):
            llm_client.complete("prompt")


class TestAiStudioProvider:
    def test_none_when_key_unset(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert llm_client._call_ai_studio("prompt", None) is None

    def test_none_on_request_failure(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(
            gemini_tool,
            "gemini_ask",
            MagicMock(side_effect=ToolError("Gemini request failed")),
        )
        assert llm_client._call_ai_studio("prompt", None) is None

    def test_success_shape(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(
            gemini_tool,
            "gemini_ask",
            lambda prompt, system=None, **kwargs: {
                "text": "hi",
                "model": gemini_tool.DEFAULT_MODEL,
                "usage": {"prompt_tokens": 5, "output_tokens": 3},
            },
        )
        result = llm_client._call_ai_studio("prompt", None)
        assert result == {"text": "hi", "provider": "ai_studio", "model": gemini_tool.DEFAULT_MODEL}

    def test_propagates_system_prompt(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        seen = {}

        def fake_ask(prompt, system=None, **kwargs):
            seen["system"] = system
            return {
                "text": "hi",
                "model": gemini_tool.DEFAULT_MODEL,
                "usage": {"prompt_tokens": 5, "output_tokens": 3},
            }

        monkeypatch.setattr(gemini_tool, "gemini_ask", fake_ask)
        llm_client._call_ai_studio("prompt", "be terse")
        assert seen["system"] == "be terse"


class TestStructuredOutput:
    def test_explicit_ai_studio_leg_passes_schema_to_gemini_ask(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        _with_explicit_ai_studio(monkeypatch)
        seen = {}

        def fake_post(url, **kwargs):
            if "localhost:11434" in url:
                raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))
            return _fake_response({"choices": [{"message": {"content": "unused"}}]})

        def fake_ask(prompt, system=None, response_schema=None, response_mime_type=None):
            seen["response_schema"] = response_schema
            seen["response_mime_type"] = response_mime_type
            return {
                "text": '{"verdict":"DUPLICATE","reason":"same fact"}',
                "model": gemini_tool.DEFAULT_MODEL,
                "usage": {"prompt_tokens": 5, "output_tokens": 3},
            }

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr(gemini_tool, "gemini_ask", fake_ask)
        result = llm_client.complete(
            "prompt",
            response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA,
        )
        assert seen["response_schema"] == llm_client.ADJUDICATION_VERDICT_SCHEMA
        assert seen["response_mime_type"] is None
        assert result["provider"] == "ai_studio"
        assert json.loads(result["text"])["verdict"] == "DUPLICATE"

    def test_ollama_valid_json_with_schema_is_accepted(self, monkeypatch):
        _with_explicit_ollama(monkeypatch)

        def fake_post(url, **kwargs):
            assert "localhost:11434" in url
            return _fake_response(
                {
                    "choices": [
                        {"message": {"content": '{"verdict":"CONTRADICTION","reason":"conflict"}'}}
                    ]
                }
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr(
            llm_client,
            "_call_ai_studio",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("ai_studio should not run")),
        )
        result = llm_client.complete(
            "prompt",
            response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA,
        )
        assert result["provider"] == "ollama"
        assert json.loads(result["text"])["verdict"] == "CONTRADICTION"

    def test_openrouter_invalid_json_with_schema_is_a_schema_mismatch(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

        def fake_post(url, **kwargs):
            assert "openrouter.ai" in url
            assert kwargs["json"]["response_format"] == {"type": "json_object"}
            return _fake_response({"choices": [{"message": {"content": "maybe these are alike"}}]})

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA)
        by_target = {a["target"]: a for a in exc_info.value.route}
        assert by_target["openrouter"]["reason"] == "schema mismatch"

    def test_openrouter_invalid_enum_with_schema_is_a_schema_mismatch(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kwargs: _fake_response(
                {"choices": [{"message": {"content": '{"verdict":"MERGE","reason":"same fact"}'}}]}
            ),
        )
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA)
        assert exc_info.value.route[-1]["reason"] == "schema mismatch"

    def test_deeply_nested_reply_is_a_schema_mismatch_not_a_crash(self):
        nested = '{"a":' * 50000 + "1" + "}" * 50000
        assert not llm_client._response_matches_schema(
            nested, llm_client.ADJUDICATION_VERDICT_SCHEMA
        )
        assert llm_client.parse_adjudication_response(nested) is None


class TestParseAdjudicationResponse:
    def test_parses_structured_json(self):
        text = '{"verdict":"DUPLICATE","reason":"same rollout strategy"}'
        assert llm_client.parse_adjudication_response(text) == (
            "DUPLICATE",
            "same rollout strategy",
        )

    def test_parses_legacy_verdict_lines(self):
        text = "VERDICT: UNRELATED\nREASON: Different tasks."
        assert llm_client.parse_adjudication_response(text) == ("UNRELATED", "Different tasks.")

    def test_returns_none_for_garbage(self):
        assert llm_client.parse_adjudication_response("maybe these are alike") is None

    def test_response_matches_schema_rejects_invalid_enum(self):
        text = '{"verdict":"MERGE","reason":"same fact"}'
        assert not llm_client._response_matches_schema(text, llm_client.ADJUDICATION_VERDICT_SCHEMA)


class TestRouteMetadata:
    """Switchyard-inspired routing proposal Phase 1: target registry (item 1)
    and normalized per-call metadata (item 4). Wiki/LLM-Routing-Proposal.md."""

    def test_route_targets_registry_covers_every_chain_leg(self):
        chain_names = {name for name, _ in llm_client._PROVIDER_CHAIN}
        # ai_studio stays registered: explicit callers can still reach it.
        # ollama and ai_studio stay registered as retained, unwired helpers.
        assert set(llm_client.ROUTE_TARGETS) == chain_names | {
            "subscription_pool",
            "ai_studio",
            "ollama",
        }
        for meta in llm_client.ROUTE_TARGETS.values():
            assert {"cost_tier", "local", "capability"} <= meta.keys()

    def test_all_providers_unavailable_route_records_every_attempt(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        def fake_post(url, **kwargs):
            raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt")

        route = exc_info.value.route
        assert [attempt["target"] for attempt in route] == ["claude_cli", "openrouter"]
        assert all(attempt["ok"] is False for attempt in route)
        assert all(attempt["elapsed_s"] >= 0 for attempt in route)

    def test_each_attempt_carries_nonnegative_elapsed_time(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

        def fake_post(url, **kwargs):
            return _fake_response({"choices": [{"message": {"content": "hi"}}]})

        monkeypatch.setattr(httpx, "post", fake_post)
        result = llm_client.complete("prompt")
        for attempt in result["route"]:
            assert attempt["elapsed_s"] >= 0


class TestVertexLegIsGone:
    """Task #197: the Vertex leg was removed, not capped at zero.

    The distinction is the whole point. `UA_VERTEX_PROJECT` is still set by the
    installed `ua-sleep-pass.service` unit, and ADC exists on this machine, so a
    leg that merely *defaulted* to a $0 cap would resume billing the moment
    someone raised the cap or the unit was re-read. These tests assert the code
    path does not exist at all.
    """

    def test_no_vertex_call_even_when_project_env_is_set(self, monkeypatch):
        # The exact condition on the live machine: project id present (from the
        # sleep-pass unit), ADC available, ollama down. Pre-#197 this billed.
        monkeypatch.setenv("UA_VERTEX_PROJECT", "example-project")
        monkeypatch.setenv("UA_VERTEX_SPEND_CAP_USD", "300")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

        def fake_post(url, **kwargs):
            assert "aiplatform.googleapis.com" not in url, f"Vertex was called: {url}"
            if "localhost:11434" in url:
                raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))
            return _fake_response({"choices": [{"message": {"content": "hi"}}]})

        monkeypatch.setattr(httpx, "post", fake_post)
        result = llm_client.complete("prompt")
        assert result["provider"] == "openrouter"

    def test_chain_has_exactly_two_legs_in_order(self):
        assert [name for name, _ in llm_client._PROVIDER_CHAIN] == ["claude_cli", "openrouter"]

    @pytest.mark.parametrize(
        "symbol",
        [
            "_call_vertex",
            "_vertex_ledger_path",
            "_vertex_spend_so_far",
            "_record_vertex_spend",
            "vertex_cap_usd",
            "_estimate_vertex_cost_usd",
            "VERTEX_PROJECT_ENV",
            "VERTEX_SPEND_CAP_ENV",
            "DEFAULT_VERTEX_SPEND_CAP_USD",
        ],
    )
    def test_vertex_symbols_are_absent(self, symbol):
        assert not hasattr(llm_client, symbol), (
            f"{symbol} is back — re-adding a paid GCP leg needs a new billing "
            "fact and the operator's call (task #197, memory 3af245c0ff07)"
        )


def _fake_get(responses):
    """httpx.get stub: {url_substring: httpx.Response | Exception instance}."""

    def fake_get(url, headers=None, timeout=None):
        for needle, outcome in responses.items():
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected GET {url}")

    return fake_get


def _get_response(url: str, status: int = 200, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload or {}, request=httpx.Request("GET", url))


def _models_payload(*ids: str) -> dict:
    return {"object": "list", "data": [{"id": model_id, "object": "model"} for model_id in ids]}


def _legs_by_name(legs: list[dict]) -> dict:
    return {leg["leg"]: leg for leg in legs}


class TestLegStatus:
    """Task #1178: a read-only per-leg health surface — config presence plus a
    cheap metadata GET per leg, never a generation call, so a doctor-style
    check can report chain health without spending.
    """

    def test_reports_every_leg_and_probes_nothing_when_unconfigured(self, monkeypatch):
        """With no keys set nothing gets an endpoint probe — an auth check
        against a key that does not exist is meaningless."""
        calls = []

        def fake_get(url, headers=None, timeout=None):
            calls.append(url)
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        legs = _legs_by_name(llm_client.leg_status(env={}))
        # Task #2025: ollama is no longer a chain leg, so it is neither
        # reported nor probed; with nothing configured nothing is probed.
        assert set(legs) == {"claude_cli", "openrouter", "subscription_pool"}
        assert calls == []
        for name in ("claude_cli", "openrouter", "subscription_pool"):
            assert legs[name]["configured"] is False
            assert legs[name]["reachable"] is None

    def test_ollama_up(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "get",
            _fake_get(
                {
                    "localhost:11434": _get_response(
                        "http://localhost:11434/v1/models",
                        payload=_models_payload(llm_client.DEFAULT_OLLAMA_MODEL),
                    )
                }
            ),
        )
        leg = llm_client._ollama_leg_status({}, timeout_s=2.0)
        assert leg["configured"] is True
        assert leg["reachable"] is True

    def test_ollama_server_up_but_model_not_pulled_is_down(self, monkeypatch):
        """Task #1639: /models answered 200 for weeks while every chat call
        404'd on a model that was never pulled, and doctor said 'up'."""
        monkeypatch.setattr(
            httpx,
            "get",
            _fake_get(
                {
                    "localhost:11434": _get_response(
                        "http://localhost:11434/v1/models",
                        payload=_models_payload("nomic-embed-text:latest"),
                    )
                }
            ),
        )
        leg = llm_client._ollama_leg_status(
            {"UA_OLLAMA_MODEL": "qwen2.5-coder:1.5b"}, timeout_s=2.0
        )
        assert leg["reachable"] is False
        assert "qwen2.5-coder:1.5b not pulled" in leg["detail"]

    def test_ollama_untagged_model_matches_latest(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "get",
            _fake_get(
                {
                    "localhost:11434": _get_response(
                        "http://localhost:11434/v1/models",
                        payload=_models_payload("mistral:latest"),
                    )
                }
            ),
        )
        leg = llm_client._ollama_leg_status({"UA_OLLAMA_MODEL": "mistral"}, timeout_s=2.0)
        assert leg["reachable"] is True

    def test_ollama_unreadable_model_list_is_down(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "get",
            _fake_get({"localhost:11434": _get_response("http://localhost:11434/v1/models")}),
        )
        leg = llm_client._ollama_leg_status({}, timeout_s=2.0)
        assert leg["reachable"] is False
        assert "unreadable" in leg["detail"]

    def test_ai_studio_is_not_probed_even_with_a_key(self, monkeypatch):
        """Task #2025: ai_studio left the chain, so doctor neither probes nor
        reports it, and a valid key cannot make the chain look usable."""

        def fake_get(url, headers=None, timeout=None):
            assert "generativelanguage" not in url
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        legs = _legs_by_name(llm_client.leg_status(env={"GEMINI_API_KEY": "test-key"}))
        assert "ai_studio" not in legs
        assert "test-key" not in str(legs)

    def _openrouter_get(self, model_ids):
        def fake_get(url, headers=None, timeout=None):
            if url.endswith("/auth/key"):
                assert headers["Authorization"] == "Bearer or-key"
                return _get_response(url, payload={"data": {"label": "x"}})
            if url.endswith("/api/v1/models"):
                assert "Authorization" not in (headers or {})
                return _get_response(url, payload=_models_payload(*model_ids))
            raise AssertionError(f"unexpected probe {url}")

        return fake_get

    def test_openrouter_up_only_when_pinned_model_is_listed_exactly(self, monkeypatch):
        monkeypatch.setattr(
            httpx, "get", self._openrouter_get(["deepseek/deepseek-v4.1-flash", "other/x"])
        )
        leg = _legs_by_name(llm_client.leg_status(env={"OPENROUTER_API_KEY": "or-key"}))[
            "openrouter"
        ]
        assert leg["reachable"] is True
        assert "deepseek/deepseek-v4.1-flash present" in leg["detail"]
        assert "or-key" not in str(leg)

    @pytest.mark.parametrize(
        "listed",
        [
            ["deepseek/deepseek-v4.1-flash:free"],
            ["deepseek/deepseek-v4.1-flash-latest"],
            ["deepseek/deepseek-v4.1"],
            [],
        ],
    )
    def test_openrouter_down_when_pinned_model_is_not_listed_exactly(self, monkeypatch, listed):
        monkeypatch.setattr(httpx, "get", self._openrouter_get(listed))
        leg = _legs_by_name(llm_client.leg_status(env={"OPENROUTER_API_KEY": "or-key"}))[
            "openrouter"
        ]
        assert leg["reachable"] is False
        assert "deepseek/deepseek-v4.1-flash not listed" in leg["detail"]

    def test_openrouter_probe_checks_the_override_model(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", self._openrouter_get(["z-ai/glm-5.3-flash"]))
        env = {"OPENROUTER_API_KEY": "or-key", "UA_OPENROUTER_MODEL": "z-ai/glm-5.3-flash"}
        leg = _legs_by_name(llm_client.leg_status(env=env))["openrouter"]
        assert leg["reachable"] is True

    def test_openrouter_unconfigured_without_key_is_not_probed(self, monkeypatch):
        monkeypatch.setattr(
            httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed"))
        )
        leg = _legs_by_name(llm_client.leg_status(env={}))["openrouter"]
        assert (leg["configured"], leg["reachable"]) == (False, None)

    def test_openrouter_non_2xx_is_down_not_raising(self, monkeypatch):
        def fake_get(url, headers=None, timeout=None):
            if "openrouter.ai" in url:
                assert headers["Authorization"] == "Bearer or-key"
                return _get_response(url, status=401)
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        legs = _legs_by_name(llm_client.leg_status(env={"OPENROUTER_API_KEY": "or-key"}))
        assert legs["openrouter"]["configured"] is True
        assert legs["openrouter"]["reachable"] is False
        assert "401" in legs["openrouter"]["detail"]

    def test_subscription_pool_enabled_without_url_is_configured_but_down(self, monkeypatch):
        """Opted in but incomplete: enabled without a URL is a misconfiguration
        worth surfacing, not a silent skip."""
        monkeypatch.setattr(
            httpx,
            "get",
            _fake_get(
                {
                    "localhost:11434": httpx.ConnectError(
                        "refused", request=httpx.Request("GET", "x")
                    )
                }
            ),
        )
        env = {"UA_SUBSCRIPTION_POOL_ENABLED": "1"}
        legs = _legs_by_name(llm_client.leg_status(env=env))
        assert legs["subscription_pool"]["configured"] is True
        assert legs["subscription_pool"]["reachable"] is False
        assert "URL" in legs["subscription_pool"]["detail"]

    def test_subscription_pool_probed_when_configured(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "get",
            _fake_get(
                {
                    "pool.example": _get_response("https://pool.example/v1/models"),
                    "localhost:11434": httpx.ConnectError(
                        "refused", request=httpx.Request("GET", "x")
                    ),
                }
            ),
        )
        env = {
            "UA_SUBSCRIPTION_POOL_ENABLED": "1",
            "UA_SUBSCRIPTION_POOL_URL": "https://pool.example/v1",
        }
        legs = _legs_by_name(llm_client.leg_status(env=env))
        assert legs["subscription_pool"]["reachable"] is True

    def test_pool_disabled_is_not_configured_even_with_url(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "get",
            _fake_get(
                {
                    "localhost:11434": httpx.ConnectError(
                        "refused", request=httpx.Request("GET", "x")
                    )
                }
            ),
        )
        env = {"UA_SUBSCRIPTION_POOL_URL": "https://pool.example/v1"}
        legs = _legs_by_name(llm_client.leg_status(env=env))
        assert legs["subscription_pool"]["configured"] is False
        assert legs["subscription_pool"]["reachable"] is None

    def test_probe_errors_never_raise(self, monkeypatch):
        def fake_get(url, headers=None, timeout=None):
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        legs = llm_client.leg_status(
            env={
                "OPENROUTER_API_KEY": "k",
                "UA_SUBSCRIPTION_POOL_ENABLED": "1",
                "UA_SUBSCRIPTION_POOL_URL": "https://pool.example/v1",
            }
        )
        configured = [leg for leg in legs if leg["configured"]]
        assert {leg["leg"] for leg in configured} == {"openrouter", "subscription_pool"}
        assert all(leg["reachable"] is False for leg in configured)

    def test_explicit_marks_opted_in_legs(self, monkeypatch):
        """`explicit` distinguishes 'the operator configured this leg' from
        ollama's always-on default — doctor needs it to tell 'nothing
        configured' (DORMANT) apart from 'configured but dead'
        (MISCONFIGURED)."""
        monkeypatch.setattr(
            httpx,
            "get",
            _fake_get(
                {
                    "11434": httpx.ConnectError("refused", request=httpx.Request("GET", "x")),
                    "openrouter.ai": httpx.ConnectError(
                        "refused", request=httpx.Request("GET", "x")
                    ),
                }
            ),
        )
        assert llm_client._ollama_leg_status({}, timeout_s=2.0)["explicit"] is False
        explicit = llm_client._ollama_leg_status(
            {"UA_OLLAMA_BASE_URL": "http://gpu:11434/v1"}, timeout_s=2.0
        )
        assert explicit["explicit"] is True
        legs = _legs_by_name(llm_client.leg_status(env={}))
        assert legs["openrouter"]["explicit"] is False
        legs = _legs_by_name(llm_client.leg_status(env={"OPENROUTER_API_KEY": "k"}))
        assert legs["openrouter"]["explicit"] is True

    def test_env_parameter_overrides_os_environ(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            httpx,
            "get",
            _fake_get(
                {
                    "openrouter.ai": _get_response("https://openrouter.ai/api/v1/auth/key"),
                    "localhost:11434": httpx.ConnectError(
                        "refused", request=httpx.Request("GET", "x")
                    ),
                }
            ),
        )
        legs = _legs_by_name(llm_client.leg_status(env={"OPENROUTER_API_KEY": "k"}))
        assert legs["openrouter"]["configured"] is True


class TestUsageRecording:
    """Task #1526: hub-observed usage for every leg that talks to a provider
    directly. Never a fabricated token count or a fabricated $0 cost."""

    def _rows(self) -> list[dict]:
        conn = storage.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM usage_events").fetchall()]
        finally:
            conn.close()

    def test_ollama_records_tokens_with_null_cost(self, monkeypatch):
        _with_explicit_ollama(monkeypatch)

        def fake_post(url, **kwargs):
            return _fake_response(
                {
                    "choices": [{"message": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                }
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        assert llm_client.complete("prompt")["provider"] == "ollama"
        [row] = self._rows()
        assert row["provider"] == "ollama"
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (11, 4, None)

    def test_ollama_without_usage_field_records_null_tokens_not_zero(self, monkeypatch):
        _with_explicit_ollama(monkeypatch)

        def fake_post(url, **kwargs):
            return _fake_response({"choices": [{"message": {"content": "hi"}}]})

        monkeypatch.setattr(httpx, "post", fake_post)
        llm_client.complete("prompt")
        [row] = self._rows()
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (None, None, None)

    def test_ollama_malformed_reply_records_reported_usage_once(self, monkeypatch):
        _with_explicit_ollama(monkeypatch)
        usage = {"prompt_tokens": 11, "completion_tokens": 4}
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kwargs: _fake_response(
                {"model": "qwen2.5-coder:1.5b", "choices": [], "usage": usage}
            ),
        )

        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt")

        [attempt] = exc_info.value.route
        assert (attempt["error"], attempt["reason"]) == ("MalformedReply", "malformed reply")
        [row] = self._rows()
        assert row["provider"] == "ollama"
        assert row["model"] == "llama3:8b"
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (11, 4, None)

    def test_openrouter_records_reported_cost(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

        def fake_post(url, **kwargs):
            if "localhost:11434" in url:
                raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
            return _fake_response(
                {
                    "choices": [{"message": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 2, "cost": 0.0031},
                }
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        assert llm_client.complete("prompt")["provider"] == "openrouter"
        [row] = self._rows()
        assert row["provider"] == "openrouter"
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (7, 2, 0.0031)

    def test_ai_studio_leg_records_exactly_one_row_not_two(self, monkeypatch):
        """_call_ai_studio delegates to gemini_ask; recording must live in
        exactly one of the two, or a single completion double-counts."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        _with_explicit_ai_studio(monkeypatch)

        def fake_post(url, **kwargs):
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = SimpleNamespace(
            text="hi from ai studio",
            candidates=[
                SimpleNamespace(
                    finish_reason="STOP",
                    content=SimpleNamespace(parts=[SimpleNamespace(text="hi from ai studio")]),
                )
            ],
            usage_metadata=SimpleNamespace(prompt_token_count=9, candidates_token_count=6),
        )
        monkeypatch.setattr(gemini_tool.genai, "Client", lambda **kwargs: mock_client)

        result = llm_client.complete("prompt")
        assert result["provider"] == "ai_studio"
        rows = self._rows()
        assert len(rows) == 1
        assert rows[0]["provider"] == "ai_studio"
        assert (rows[0]["input_tokens"], rows[0]["output_tokens"], rows[0]["cost_usd"]) == (
            9,
            6,
            None,
        )

    def test_subscription_pool_records_null_cost(self, monkeypatch):
        monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
        monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://pool.local")

        def fake_post(url, **kwargs):
            if "localhost:11434" in url:
                raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
            assert "pool.local" in url
            return _fake_response(
                {
                    "choices": [{"message": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        result = llm_client.complete("prompt", subscription_pool=True)
        assert result["provider"] == "subscription_pool"
        [row] = self._rows()
        assert row["provider"] == "subscription_pool"
        assert row["cost_usd"] is None

    def test_failed_leg_records_nothing(self, monkeypatch):
        def fake_post(url, **kwargs):
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(llm_client.LLMConnectionError):
            llm_client.complete("prompt")
        assert self._rows() == []

    def test_run_id_is_snapshotted_before_the_call_finishes_mid_flight(self, monkeypatch):
        """Task #1526 follow-up (Devin review of PR #875): resolve run_id
        before contacting the provider, not after — a task that finishes
        while the synchronous HTTP call is in flight must not null out the
        already-in-flight call's attribution."""
        tid = task_add("in-flight task", lane="dispatch-ready")["id"]
        task_claim(tid, "usage-client-agent", expected_lane="dispatch-ready")
        run_id = run_records.run_history(task_id=tid)["runs"][0]["run_id"]
        token = config.agent_identity_var.set("usage-client-agent")

        def fake_post(url, **kwargs):
            # The task finishes strictly after llm_client resolved run_id and
            # strictly before the (fake) provider "answers".
            task_update(tid, status="done", actor="usage-client-agent")
            return _fake_response(
                {
                    "choices": [{"message": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            )

        try:
            _with_explicit_ollama(monkeypatch)
            monkeypatch.setattr(httpx, "post", fake_post)
            assert llm_client.complete("prompt")["provider"] == "ollama"
        finally:
            config.agent_identity_var.reset(token)

        conn = storage.connect()
        try:
            cache_row = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id=?", (tid,)
            ).fetchone()
        finally:
            conn.close()
        assert cache_row["current_run_id"] is None  # sanity: it really cleared
        [row] = self._rows()
        assert row["run_id"] == run_id


def _usage_rows() -> list[dict]:
    conn = storage.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_events").fetchall()]
    finally:
        conn.close()


def _chat_reply(content: str, model: str | None, usage: dict | None = None) -> httpx.Response:
    payload: dict = {"choices": [{"message": {"content": content}}]}
    if model is not None:
        payload["model"] = model
    if usage is not None:
        payload["usage"] = usage
    return _fake_response(payload)


class TestHttpModelEvidence:
    """Task #2025 review: the OpenAI-shaped routes keep the model the provider
    reports, refuse one that does not match the pin, and ledger what was
    reported rather than what was asked for."""

    def _pin_openrouter(self, monkeypatch, model="anthropic/claude-haiku-5.5"):
        monkeypatch.setenv("UA_MEMORY_PROVIDER", "openrouter")
        monkeypatch.setenv("UA_MEMORY_MODEL", model)
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def _pin_pool(self, monkeypatch, model="claude-haiku-5-5"):
        monkeypatch.setenv("UA_MEMORY_PROVIDER", "subscription_pool")
        monkeypatch.setenv("UA_MEMORY_MODEL", model)
        monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
        monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://pool.local/v1")

    def test_pinned_openrouter_refuses_a_substituted_model_and_ledgers_it(self, monkeypatch):
        self._pin_openrouter(monkeypatch)
        usage = {"prompt_tokens": 30, "completion_tokens": 6, "cost": 0.002}
        monkeypatch.setattr(
            httpx, "post", lambda url, **kw: _chat_reply("ok", "openai/gpt-5-mini", usage)
        )
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", memory_agent=True)
        [attempt] = exc_info.value.route
        assert (attempt["target"], attempt["ok"]) == ("openrouter", False)
        assert (attempt["error"], attempt["reason"]) == ("ModelMismatch", "model mismatch")
        assert attempt["model"] == "anthropic/claude-haiku-5.5"
        [row] = _usage_rows()
        assert row["provider"] == "openrouter"
        assert row["model"] == "openai/gpt-5-mini"
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (30, 6, 0.002)

    @pytest.mark.parametrize(
        "reply",
        [
            {"model": "anthropic/claude-haiku-5.5"},
            {"choices": [], "model": "anthropic/claude-haiku-5.5"},
            {
                "choices": [{"message": {"content": None}}],
                "model": "anthropic/claude-haiku-5.5",
            },
            {
                "choices": [{"message": {"content": 7}}],
                "model": "anthropic/claude-haiku-5.5",
            },
        ],
        ids=["missing-choices", "empty-choices", "null-content", "non-string-content"],
    )
    def test_malformed_http_reply_preserves_and_ledgers_usage_once(self, monkeypatch, reply):
        self._pin_openrouter(monkeypatch)
        usage = {"prompt_tokens": 30, "completion_tokens": 6, "cost": 0.002}
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kw: _fake_response({**reply, "usage": usage}),
        )

        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete(
                "prompt",
                memory_agent=True,
                response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA,
            )

        [attempt] = exc_info.value.route
        assert (attempt["target"], attempt["ok"]) == ("openrouter", False)
        assert attempt["error"] == "MalformedReply"
        assert attempt["reason"] == "malformed reply"
        assert attempt["model"] == "anthropic/claude-haiku-5.5"
        [row] = _usage_rows()
        assert (row["provider"], row["model"]) == (
            "openrouter",
            "anthropic/claude-haiku-5.5",
        )
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (
            30,
            6,
            0.002,
        )

    def test_pinned_subscription_pool_refuses_a_substituted_model(self, monkeypatch):
        self._pin_pool(monkeypatch)
        usage = {"prompt_tokens": 9, "completion_tokens": 3}
        monkeypatch.setattr(
            httpx, "post", lambda url, **kw: _chat_reply("ok", "claude-sonnet-5-5", usage)
        )
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", subscription_pool=True, memory_agent=True)
        [attempt] = exc_info.value.route
        assert (attempt["target"], attempt["reason"]) == ("subscription_pool", "model mismatch")
        [row] = _usage_rows()
        assert (row["provider"], row["model"]) == ("subscription_pool", "claude-sonnet-5-5")
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (9, 3, None)

    @pytest.mark.parametrize(
        "reported",
        [
            "anthropic/claude-haiku-5",
            "anthropic/claude-haiku-5.5-fast",
            "anthropic/claude-haiku-5.5-2026",
            "anthropic/claude-haiku-5.5:free",
            "anthropic/claude-sonnet-5.5",
            "openai/claude-haiku-5.5",
        ],
    )
    def test_a_lookalike_is_not_the_pinned_model(self, monkeypatch, reported):
        self._pin_openrouter(monkeypatch)
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _chat_reply("ok", reported))
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", memory_agent=True)
        assert exc_info.value.route[0]["reason"] == "model mismatch"

    @pytest.mark.parametrize("payload_model", [None, "", "   ", 42])
    def test_pinned_route_without_model_evidence_fails_closed(self, monkeypatch, payload_model):
        self._pin_pool(monkeypatch)
        payload = {"choices": [{"message": {"content": "ok"}}], "model": payload_model}
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _fake_response(payload))
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", subscription_pool=True, memory_agent=True)
        assert exc_info.value.route[0]["reason"] == "model mismatch"

    def test_dated_snapshot_of_the_pin_passes_and_is_ledgered_as_reported(self, monkeypatch):
        """OpenRouter's catalog lists a dated canonical slug for an undated id
        (anthropic/claude-haiku-5.5 -> anthropic/claude-haiku-5.5-20261007); a
        reply naming that snapshot is the pinned model, recorded as reported."""
        self._pin_openrouter(monkeypatch)
        reported = "anthropic/claude-haiku-5.5-20261007"
        usage = {"prompt_tokens": 4, "completion_tokens": 2}
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _chat_reply("ok", reported, usage))
        result = llm_client.complete("prompt", memory_agent=True)
        assert (result["provider"], result["model"]) == ("openrouter", reported)
        assert result["route"][0]["model"] == "anthropic/claude-haiku-5.5"
        [row] = _usage_rows()
        assert row["model"] == reported

    @pytest.mark.parametrize(
        "pin_route,pin,reported",
        [
            ("openrouter", "anthropic/claude-haiku-5.5", "claude-haiku-5-5"),
            ("subscription_pool", "claude-haiku-5-5", "anthropic/claude-haiku-5.5"),
        ],
    )
    def test_documented_native_and_openrouter_ids_name_the_same_model(
        self, monkeypatch, pin_route, pin, reported
    ):
        """docs/memory-agent-model.md: claude-haiku-5-5 (native) and
        anthropic/claude-haiku-5.5 (OpenRouter) are one model."""
        if pin_route == "openrouter":
            self._pin_openrouter(monkeypatch, pin)
        else:
            self._pin_pool(monkeypatch, pin)
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _chat_reply("ok", reported))
        result = llm_client.complete("prompt", subscription_pool=True, memory_agent=True)
        assert (result["provider"], result["model"]) == (pin_route, reported)

    def test_exact_match_on_the_pin_passes(self, monkeypatch):
        self._pin_pool(monkeypatch)
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _chat_reply("ok", kw["json"]["model"]))
        result = llm_client.complete("prompt", subscription_pool=True, memory_agent=True)
        assert (result["provider"], result["model"]) == ("subscription_pool", "claude-haiku-5-5")
        [row] = _usage_rows()
        assert row["model"] == "claude-haiku-5-5"

    def test_env_pinned_openrouter_model_is_enforced_like_the_cli_pin(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setenv("UA_OPENROUTER_MODEL", "z-ai/glm-5.3-flash")
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _chat_reply("ok", None))
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt")
        assert exc_info.value.route[-1]["reason"] == "model mismatch"

    def test_unpinned_default_without_evidence_is_recorded_as_unknown(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _chat_reply("ok", None))
        assert llm_client.complete("prompt")["model"] == "unknown"
        [row] = _usage_rows()
        assert row["model"] == "unknown"

    def test_unpinned_default_reporting_another_model_fails_closed(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _chat_reply("ok", "openai/gpt-5"))
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt")
        assert exc_info.value.route[-1]["reason"] == "model mismatch"

    def test_reported_model_text_is_never_stored_verbatim(self, monkeypatch):
        self._pin_openrouter(monkeypatch)
        reported = "PRIVATE NOTE BODY echoed as a model"
        usage = {"prompt_tokens": 1, "completion_tokens": 1}
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _chat_reply("ok", reported, usage))
        with pytest.raises(llm_client.LLMConnectionError):
            llm_client.complete("prompt", memory_agent=True)
        [row] = _usage_rows()
        assert row["model"] == "unrecognized"

    def test_schema_rejected_reply_still_records_its_usage_once(self, monkeypatch):
        """A paid reply that fails the schema was still generated and billed."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        usage = {"prompt_tokens": 12, "completion_tokens": 8, "cost": 0.001}
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kw: _chat_reply("not json", kw["json"]["model"], usage),
        )
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA)
        assert exc_info.value.route[-1]["reason"] == "schema mismatch"
        [row] = _usage_rows()
        assert (row["provider"], row["model"]) == (
            "openrouter",
            llm_client.DEFAULT_OPENROUTER_MODEL,
        )
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (12, 8, 0.001)

    def test_rejected_reply_without_usage_evidence_adds_no_row(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr(
            httpx, "post", lambda url, **kw: _chat_reply("not json", kw["json"]["model"])
        )
        with pytest.raises(llm_client.LLMConnectionError):
            llm_client.complete("prompt", response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA)
        assert _usage_rows() == []


PRIVATE_NOTE = "PRIVATE-NOTE-BODY: the operator's medical appointment is on Tuesday"


def _status_error(url: str, status: int, message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(
        status, json={"error": {"message": message, "type": "x"}}, request=request
    )
    return httpx.HTTPStatusError(f"{status} for {url}", request=request, response=response)


class TestLegFailureReasons:
    """Task #1639: every failed leg records why — as fixed facts (an error
    class, the HTTP status, one category from LEG_REASONS), never provider
    text, which can echo the private notes in the prompt."""

    def _route(self, **complete_kwargs) -> list[dict]:
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete(PRIVATE_NOTE, **complete_kwargs)
        return exc_info.value.route

    def test_missing_model_depleted_credit_and_unconfigured(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        _with_explicit_ai_studio(monkeypatch)

        def fake_post(url, **kwargs):
            raise _status_error(url, 404, "model 'llama3:8b' not found")

        def fake_ask(prompt, system=None, response_schema=None, response_mime_type=None):
            class FakeAPIError(Exception):
                code = 402  # the SDK APIError's integer HTTP status

            raise ToolError(
                f"Gemini request failed: 402 credits depleted; echoed: {prompt}"
            ) from FakeAPIError()

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr(gemini_tool, "gemini_ask", fake_ask)
        by_target = {a["target"]: a for a in self._route()}

        assert {k: by_target["ollama"][k] for k in ("error", "status", "reason")} == {
            "error": "HTTPStatusError",
            "status": 404,
            "reason": "model not found",
        }
        assert {k: by_target["ai_studio"][k] for k in ("error", "status", "reason")} == {
            "error": "ToolError",
            "status": 402,
            "reason": "insufficient credits",
        }
        assert by_target["openrouter"]["error"] == "Unconfigured"
        assert by_target["openrouter"]["reason"] == "OPENROUTER_API_KEY unset"

    def test_echoed_note_body_never_reaches_the_route(self, monkeypatch):
        """Security (Devin review, PR #932): providers and gemini_ask can echo
        the prompt — here, a private memory note — in their error text."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        _with_explicit_ai_studio(monkeypatch)

        def fake_post(url, **kwargs):
            prompt = kwargs["json"]["messages"][-1]["content"]
            if "11434" in url:
                raise _status_error(url, 400, f"invalid request near: {prompt}")
            raise httpx.ConnectError(
                f"connect failed for {url}?q={prompt}", request=httpx.Request("POST", url)
            )

        def fake_ask(prompt, system=None, response_schema=None, response_mime_type=None):
            raise ToolError(f"Gemini request failed: ValueError: bad input {prompt}")

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr(gemini_tool, "gemini_ask", fake_ask)
        route = self._route()
        dumped = json.dumps(route)
        assert "PRIVATE-NOTE-BODY" not in dumped
        assert "medical" not in dumped
        assert "openrouter.ai" not in dumped
        for attempt in route:
            assert attempt["reason"] in llm_client.LEG_REASONS
        by_target = {a["target"]: a for a in route}
        assert by_target["ollama"]["reason"] == "bad request"
        assert by_target["ai_studio"]["reason"] == "request failed"
        assert by_target["openrouter"] == {
            **by_target["openrouter"],
            "error": "ConnectError",
            "reason": "connection failed",
        }
        assert "status" not in by_target["openrouter"]

    def test_transport_errors_record_class_only(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        _with_explicit_ai_studio(monkeypatch)
        monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
        monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://127.0.0.1:8317/v1")

        def fake_post(url, **kwargs):
            if "11434" in url:
                raise httpx.ReadTimeout("read timed out", request=httpx.Request("POST", url))
            raise httpx.ConnectError(
                "[Errno 61] Connection refused", request=httpx.Request("POST", url)
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        by_target = {a["target"]: a for a in self._route(subscription_pool=True)}
        assert by_target["ollama"]["error"] == "ReadTimeout"
        assert by_target["ollama"]["reason"] == "timeout"
        assert by_target["ai_studio"]["error"] == "Unconfigured"
        assert by_target["ai_studio"]["reason"] == "GEMINI_API_KEY unset"
        assert by_target["subscription_pool"]["error"] == "ConnectError"
        assert by_target["subscription_pool"]["reason"] == "connection failed"
        assert "127.0.0.1" not in json.dumps(by_target)

    @pytest.mark.parametrize("target", ["ollama", "subscription_pool"])
    def test_malformed_generation_url_falls_back_without_leaking(self, monkeypatch, target):
        malformed_url = "http://localhost:x/v1"
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
        if target == "ollama":
            monkeypatch.setenv("UA_OLLAMA_BASE_URL", malformed_url)
            chain = [("ollama", llm_client._call_ollama)]
        else:
            monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
            monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", malformed_url)
            chain = []  # complete inserts the opted-in pool before OpenRouter.
        monkeypatch.setattr(
            llm_client,
            "_PROVIDER_CHAIN",
            [*chain, ("openrouter", llm_client._call_openrouter)],
        )
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            httpx.URL(url)  # Real HTTPX parsing rejects the invalid port before transport.
            return _fake_response({"choices": [{"message": {"content": "healthy fallback"}}]})

        monkeypatch.setattr(httpx, "post", fake_post)
        result = llm_client.complete(PRIVATE_NOTE, subscription_pool=target == "subscription_pool")
        assert result["provider"] == "openrouter"
        assert result["text"] == "healthy fallback"
        assert calls == [
            f"{malformed_url}/chat/completions",
            "https://openrouter.ai/api/v1/chat/completions",
        ]
        failed, succeeded = result["route"]
        assert failed == {
            "target": target,
            "ok": False,
            "elapsed_s": failed["elapsed_s"],
            "error": "InvalidURL",
            "reason": "transport error",
        }
        assert succeeded["target"] == "openrouter"
        assert succeeded["ok"] is True
        dumped = json.dumps(result["route"])
        assert malformed_url not in dumped
        assert "localhost" not in dumped
        assert PRIVATE_NOTE not in dumped
        assert "status" not in failed
        assert failed["reason"] in llm_client.LEG_REASONS

    def test_schema_mismatch_is_categorised(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kwargs: _fake_response(
                {"choices": [{"message": {"content": '{"VERDICT": "UNRELATED"}'}}]}
            ),
        )
        monkeypatch.setattr(llm_client, "_PROVIDER_CHAIN", [("ollama", llm_client._call_ollama)])
        route = self._route(response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA)
        assert route[0]["error"] == "SchemaMismatch"
        assert route[0]["reason"] == "schema mismatch"

    def test_status_categories(self):
        assert llm_client._status_reason(429) == "rate limited"
        assert llm_client._status_reason(401) == "auth rejected"
        assert llm_client._status_reason(503) == "provider error"
        assert llm_client._status_reason(404) == "not found"
        assert llm_client._status_reason(404, 'model "x" does not exist') == "model not found"
        assert llm_client._status_reason(418) == "http error"

    def test_free_text_reason_is_never_stored(self):
        token = llm_client._LEG_FAILURE.set(None)
        try:
            llm_client._note_failure("ToolError", f"something {PRIVATE_NOTE}")
            assert llm_client._LEG_FAILURE.get() == {
                "error": "ToolError",
                "reason": "request failed",
            }
        finally:
            llm_client._LEG_FAILURE.reset(token)

    def test_successful_leg_has_no_error_fields(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kwargs: _fake_response({"choices": [{"message": {"content": "hi"}}]}),
        )
        route = llm_client.complete("prompt")["route"]
        assert route[-1]["ok"] is True
        assert not {"error", "status", "reason"} & set(route[-1])

    def test_leg_without_noted_reason_still_records_one(self, monkeypatch):
        monkeypatch.setattr(llm_client, "_PROVIDER_CHAIN", [("custom", lambda *a, **k: None)])
        route = self._route()
        assert route == [
            {
                "target": "custom",
                "ok": False,
                "elapsed_s": route[0]["elapsed_s"],
                "error": "Unavailable",
                "reason": "no result",
            }
        ]

    def test_reason_does_not_leak_between_legs(self, monkeypatch):
        def noted(*args, **kwargs):
            llm_client._note_failure("Boom", "timeout")
            return None

        monkeypatch.setattr(
            llm_client,
            "_PROVIDER_CHAIN",
            [("first", noted), ("second", lambda *a, **k: None)],
        )
        route = self._route()
        assert route[0]["reason"] == "timeout"
        assert route[1]["error"] == "Unavailable"

    def test_openai_shaped_legs_are_told_the_schema(self, monkeypatch):
        """The adjudication prompt asks for VERDICT:/REASON: lines; JSON mode
        alone made a local model answer {"VERDICT": ...} and fail the schema
        on every cluster. The schema must reach the model."""
        seen = {}

        def fake_post(url, **kwargs):
            seen["messages"] = kwargs["json"]["messages"]
            return _fake_response(
                {"choices": [{"message": {"content": '{"verdict":"UNRELATED","reason":"x"}'}}]}
            )

        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setattr(httpx, "post", fake_post)
        result = llm_client.complete(
            "prompt",
            system="Reply as VERDICT: ...",
            response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA,
        )
        assert result["provider"] == "openrouter"
        system = seen["messages"][0]
        assert system["role"] == "system"
        assert system["content"].startswith("Reply as VERDICT: ...")
        assert '"required": ["verdict", "reason"]' in system["content"]


class TestArraySchemaProperties:
    """`_response_matches_schema` checks array properties and their item types."""

    @pytest.mark.parametrize(
        ("attributes", "ok"),
        [
            ([], True),
            (["deploy host"], True),
            ([""], True),
            ("bogus", False),
            ({}, False),
            (None, False),
            ([1], False),
            ([["deploy host"]], False),
            ([True], False),
        ],
    )
    def test_conflicting_attributes_is_type_checked(self, attributes, ok):
        text = json.dumps(
            {"verdict": "CONTRADICTION", "reason": "x", "conflicting_attributes": attributes}
        )
        schema = llm_client.ADJUDICATION_VERDICT_SCHEMA
        assert llm_client._response_matches_schema(text, schema) is ok

    def test_an_array_without_items_checks_only_the_container(self):
        schema = {"type": "object", "properties": {"xs": {"type": "array"}}}
        assert llm_client._response_matches_schema('{"xs": [1, "a", null]}', schema)
        assert not llm_client._response_matches_schema('{"xs": "a"}', schema)

    def test_a_duplicate_key_fails_the_schema(self):
        text = '{"verdict": "CONTRADICTION", "verdict": "DUPLICATE", "reason": "x"}'
        schema = llm_client.ADJUDICATION_VERDICT_SCHEMA
        assert not llm_client._response_matches_schema(text, schema)
