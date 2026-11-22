from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.genai import errors
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config as ua_config
from unified_agent import run_records, storage
from unified_agent.tools import gemini
from unified_agent.tools.tasks import task_add, task_claim, task_update


def _fake_genai_response(
    text: str = "hi from gemini",
    finish_reason: str = "STOP",
    prompt_tokens: int = 5,
    candidates_tokens: int = 3,
    candidates: list | None = None,
) -> Any:
    if candidates is None:
        cand = SimpleNamespace(
            finish_reason=finish_reason,
            content=SimpleNamespace(parts=[SimpleNamespace(text=text)]),
        )
        candidates = [cand]
    return SimpleNamespace(
        text=text,
        candidates=candidates,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidates_tokens,
        ),
    )


class TestGeminiAsk:
    def test_empty_prompt_refused(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        with pytest.raises(ToolError, match="must not be empty"):
            gemini.gemini_ask("")
        with pytest.raises(ToolError, match="must not be empty"):
            gemini.gemini_ask("   ")

    def test_missing_key_refused(self, isolated_env, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr("unified_agent.config.load_gemini_env", lambda: None)
        with pytest.raises(ToolError, match="GEMINI_API_KEY"):
            gemini.gemini_ask("hello")

    def test_success_returns_text_model_and_usage(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_genai_response(
            text="hi from gemini", prompt_tokens=5, candidates_tokens=3
        )
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        result = gemini.gemini_ask("hello")
        assert result == {
            "text": "hi from gemini",
            "model": gemini.DEFAULT_MODEL,
            "usage": {"prompt_tokens": 5, "output_tokens": 3},
        }
        mock_client.models.generate_content.assert_called_once()
        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        assert call_kwargs["model"] == gemini.DEFAULT_MODEL
        assert call_kwargs["contents"] == "hello"

    def test_system_prompt_and_schema_included(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_genai_response(text='{"ok": true}')
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        result = gemini.gemini_ask(
            "hello", system="be terse", response_schema=schema, thinking_budget=0
        )
        assert result["text"] == '{"ok": true}'
        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        config = call_kwargs["config"]
        assert config.system_instruction == "be terse"
        assert config.response_schema == schema
        assert config.response_mime_type == "application/json"
        assert config.thinking_config.thinking_budget == 0

    def test_model_override_via_param(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_genai_response(text="ok")
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        result = gemini.gemini_ask("hello", model="gemini-2.5-pro")
        assert result["model"] == "gemini-2.5-pro"
        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        assert call_kwargs["model"] == "gemini-2.5-pro"

    def test_model_override_via_env(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("UA_GEMINI_MODEL", "gemini-2.5-flash-lite")
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_genai_response(text="ok")
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        result = gemini.gemini_ask("hello")
        assert result["model"] == "gemini-2.5-flash-lite"
        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        assert call_kwargs["model"] == "gemini-2.5-flash-lite"

    def test_api_error_wrapped(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = errors.APIError(
            403, {"error": {"message": "Permission denied"}}
        )
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        with pytest.raises(ToolError, match="Gemini request failed: 403 Permission denied"):
            gemini.gemini_ask("hello")

    def test_error_message_never_contains_the_key(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key-12345")
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = errors.APIError(
            400, {"error": {"message": "invalid key super-secret-key-12345 in header"}}
        )
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        with pytest.raises(ToolError) as exc_info:
            gemini.gemini_ask("hello")
        assert "super-secret-key-12345" not in str(exc_info.value)
        assert "[REDACTED]" in str(exc_info.value)

    def test_no_candidates_raises(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_genai_response(candidates=[])
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        with pytest.raises(ToolError, match="no candidates"):
            gemini.gemini_ask("hello")

    def test_empty_content_raises_with_finish_reason(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_genai_response(
            text="", finish_reason="SAFETY"
        )
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        with pytest.raises(ToolError, match="finishReason=SAFETY"):
            gemini.gemini_ask("hello")

    def test_agy_shortcut_skipped_for_structured_requests(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("UA_PREFER_AGY_CLI", "1")
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/agy" if name == "agy" else None)

        def fail_run(*args, **kwargs):
            raise AssertionError("agy CLI must not run for structured output requests")

        monkeypatch.setattr(gemini.subprocess, "run", fail_run)
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_genai_response(
            text='{"verdict":"DUPLICATE","reason":"same fact"}'
        )
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        schema = {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["DUPLICATE", "CONTRADICTION", "UNRELATED"],
                },
                "reason": {"type": "string"},
            },
            "required": ["verdict", "reason"],
        }
        result = gemini.gemini_ask("hello", response_schema=schema)
        assert result["text"] == '{"verdict":"DUPLICATE","reason":"same fact"}'
        mock_client.models.generate_content.assert_called_once()


class TestUsageRecording:
    """Task #1526: gemini_ask records hub-observed usage for its own callers
    and for llm_client's ai_studio leg (which delegates to it — recording
    lives here so that delegation never double-counts a single call)."""

    def _rows(self) -> list[dict]:
        conn = storage.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM usage_events").fetchall()]
        finally:
            conn.close()

    def test_success_records_one_observed_row(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_genai_response(
            prompt_tokens=5, candidates_tokens=3
        )
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        gemini.gemini_ask("hello")
        [row] = self._rows()
        assert row["provider"] == "ai_studio"
        assert row["model"] == gemini.DEFAULT_MODEL
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (5, 3, None)

    def test_missing_usage_metadata_records_null_tokens_not_zero(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        response = _fake_genai_response()
        response.usage_metadata = None
        mock_client.models.generate_content.return_value = response
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        result = gemini.gemini_ask("hello")
        # The tool's own return contract still reports 0 (pre-existing shape)...
        assert result["usage"] == {"prompt_tokens": 0, "output_tokens": 0}
        # ...but the hub-observed row never fabricates that 0 as an observation.
        [row] = self._rows()
        assert (row["input_tokens"], row["output_tokens"]) == (None, None)

    def test_agy_cli_shortcut_records_nothing(self, isolated_env, monkeypatch):
        """The local agy CLI path has no reliable usage/cost; it must not
        record a fabricated row for a call the hub didn't make to the API."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("UA_PREFER_AGY_CLI", "1")
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/agy" if name == "agy" else None)

        class _Result:
            returncode = 0
            stdout = "hi from agy"

        monkeypatch.setattr(gemini.subprocess, "run", lambda *a, **k: _Result())
        result = gemini.gemini_ask("hello")
        assert result["model"] == "agy-cli"
        assert self._rows() == []

    def test_failed_request_records_nothing(self, isolated_env, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = errors.APIError(
            500, {"error": {"message": "boom"}}
        )
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        with pytest.raises(ToolError):
            gemini.gemini_ask("hello")
        assert self._rows() == []

    def test_run_id_is_snapshotted_before_the_call_finishes_mid_flight(
        self, isolated_env, monkeypatch
    ):
        """Task #1526 follow-up (Devin review of PR #875): resolve run_id
        before calling the SDK, not after — a task that finishes while the
        call is in flight must not null out the already-in-flight call's
        attribution."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        tid = task_add("in-flight task", lane="dispatch-ready")["id"]
        task_claim(tid, "usage-gemini-agent", expected_lane="dispatch-ready")
        run_id = run_records.run_history(task_id=tid)["runs"][0]["run_id"]
        token = ua_config.agent_identity_var.set("usage-gemini-agent")

        mock_client = MagicMock()

        def fake_generate_content(**kwargs):
            # The task finishes strictly after gemini_ask resolved run_id and
            # strictly before the (fake) SDK "answers".
            task_update(tid, status="done", actor="usage-gemini-agent")
            return _fake_genai_response(prompt_tokens=4, candidates_tokens=2)

        mock_client.models.generate_content.side_effect = fake_generate_content
        monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: mock_client)

        try:
            gemini.gemini_ask("hello")
        finally:
            ua_config.agent_identity_var.reset(token)

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
