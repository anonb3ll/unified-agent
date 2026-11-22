"""Opt-in OpenAI-compatible LLM route backed by a hub-only provider key.

This J7 route supports bounded chat completions through OpenRouter. The caller supplies a device-bound hub OAuth token; the provider
key is loaded only inside the hub from the managed-secret store.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import defaultdict, deque
from threading import Lock

import anyio
import httpx
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import config, edition, llm_spend, managed_secrets, usage_ledger

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_SECRET_NAME = "llm:openrouter"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_BODY_LIMIT = 256 * 1024
_RESPONSE_LIMIT = 4 * 1024 * 1024
_MAX_OUTPUT_TOKENS = 4096
_RATE_LIMIT = 10
_BODY_FIELDS = {
    "model",
    "messages",
    "stream",
    "stream_options",
    "max_tokens",
    "n",
    "temperature",
    "top_p",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "user",
}
_rate_lock = Lock()
_requests: dict[str, deque[float]] = defaultdict(deque)
_log = logging.getLogger(__name__)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "broker_error"}}, status_code=status)


def _enabled() -> bool:
    return os.environ.get("UA_LLM_BROKER_ENABLED", "0") == "1"


def _allowed_models() -> set[str]:
    return {
        model.strip()
        for model in os.environ.get("UA_LLM_BROKER_MODELS", "").split(",")
        if model.strip()
    }


def _provider(model: str) -> tuple[str, str, str, str]:
    """Map an allowlisted model to one fixed endpoint and managed key."""
    prefix = "openai-direct/"
    if model.startswith(prefix) and model != prefix:
        return "openai", _OPENAI_URL, "llm:openai", model[len(prefix) :]
    return "openrouter", _OPENROUTER_URL, _SECRET_NAME, model


def _take_slot(identity: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        recent = _requests[identity]
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if len(recent) >= _RATE_LIMIT:
            return False
        recent.append(now)
        return True


async def _read_body(request: Request) -> dict | Response:
    length = request.headers.get("content-length")
    try:
        if length is not None and (int(length) < 0 or int(length) > _BODY_LIMIT):
            return _error("request body exceeds limit", 413)
    except ValueError:
        return _error("invalid Content-Length", 400)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _BODY_LIMIT:
            return _error("request body exceeds limit", 413)
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError):
        return _error("invalid JSON", 400)
    if not isinstance(body, dict):
        return _error("request body must be a JSON object", 400)
    return body


def _device_identity(request: Request) -> tuple[str, str] | None:
    if request.scope.get("ua_identity_via") != "oauth":
        return None
    agent = request.scope.get("ua_identity")
    device = request.scope.get("ua_device") or {}
    claims = config.current_auth_metadata() or {}
    binding = claims.get("device_node")
    if not isinstance(agent, str) or not isinstance(binding, dict):
        return None
    if not isinstance(device, dict):
        return None
    node = binding.get("node")
    node_id = binding.get("node_id")
    if not isinstance(node, str) or not node or device.get("node") != node:
        return None
    if node_id and (not isinstance(node_id, str) or device.get("node_id") != node_id):
        return None
    return agent, node


def _safe_reasoning_details(value: object) -> list[dict] | None:
    """Keep only OpenRouter's documented continuation fields."""
    if not isinstance(value, list):
        return None
    safe = []
    content_field = {
        "reasoning.summary": "summary",
        "reasoning.encrypted": "data",
        "reasoning.text": "text",
    }
    for detail in value:
        if not isinstance(detail, dict):
            return None
        kind = detail.get("type")
        if not isinstance(kind, str):
            return None
        field = content_field.get(kind)
        if field is None or not isinstance(detail.get(field), str):
            return None
        item = {"type": kind, field: detail[field]}
        for name in ("id", "format"):
            if name in detail and (detail[name] is None or isinstance(detail[name], str)):
                item[name] = detail[name]
        if (
            kind == "reasoning.text"
            and (detail.get("signature") is None or isinstance(detail.get("signature"), str))
            and "signature" in detail
        ):
            item["signature"] = detail["signature"]
        index = detail.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            item["index"] = index
        safe.append(item)
    return safe


def _safe_completion(result: dict) -> dict | None:
    """Expose only chat completion fields understood by this broker."""
    choices = result.get("choices")
    if not isinstance(choices, list):
        return None
    safe_choices = []
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            return None
        message = choice["message"]
        if message.get("role") != "assistant" or not isinstance(
            message.get("content"), (str, type(None))
        ):
            return None
        safe_message = {
            name: message[name] for name in ("role", "content", "refusal") if name in message
        }
        if isinstance(message.get("reasoning"), str):
            safe_message["reasoning"] = message["reasoning"]
        if "reasoning_details" in message:
            details = _safe_reasoning_details(message["reasoning_details"])
            if details is None:
                return None
            safe_message["reasoning_details"] = details
        if isinstance(message.get("tool_calls"), list):
            safe_calls = []
            for call in message["tool_calls"]:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    return None
                function = call["function"]
                safe_calls.append(
                    {
                        "id": call.get("id"),
                        "type": call.get("type"),
                        "function": {
                            name: function[name]
                            for name in ("name", "arguments")
                            if name in function
                        },
                    }
                )
            safe_message["tool_calls"] = safe_calls
        safe_choice = {name: choice[name] for name in ("index", "finish_reason") if name in choice}
        safe_choice["message"] = safe_message
        safe_choices.append(safe_choice)
    safe = {
        name: result[name]
        for name in ("id", "object", "created", "model", "system_fingerprint")
        if name in result
    }
    safe["choices"] = safe_choices
    if isinstance(result.get("usage"), dict):
        safe["usage"] = {
            name: result["usage"][name]
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            if name in result["usage"]
        }
    return safe


def _safe_chunk(packet: dict) -> dict | None:
    """Strip provider diagnostics from each streaming choice and delta."""
    choices = packet.get("choices")
    if not isinstance(choices, list):
        return None
    safe_choices = []
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
            return None
        delta = choice["delta"]
        safe_delta = {
            name: delta[name]
            for name in ("role", "content", "refusal", "reasoning")
            if name in delta and (delta[name] is None or isinstance(delta[name], str))
        }
        if "reasoning_details" in delta:
            details = _safe_reasoning_details(delta["reasoning_details"])
            if details is None:
                return None
            safe_delta["reasoning_details"] = details
        if isinstance(delta.get("tool_calls"), list):
            safe_calls = []
            for call in delta["tool_calls"]:
                if not isinstance(call, dict):
                    return None
                safe_call = {name: call[name] for name in ("index", "id", "type") if name in call}
                if isinstance(call.get("function"), dict):
                    safe_call["function"] = {
                        name: call["function"][name]
                        for name in ("name", "arguments")
                        if name in call["function"]
                    }
                safe_calls.append(safe_call)
            safe_delta["tool_calls"] = safe_calls
        safe_choice = {name: choice[name] for name in ("index", "finish_reason") if name in choice}
        safe_choice["delta"] = safe_delta
        safe_choices.append(safe_choice)
    safe = {
        name: packet[name]
        for name in ("id", "object", "created", "model", "system_fingerprint")
        if name in packet
    }
    safe["choices"] = safe_choices
    if isinstance(packet.get("usage"), dict):
        safe["usage"] = {
            name: packet["usage"][name]
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            if name in packet["usage"]
        }
    return safe


def _valid_messages(messages: object) -> bool:
    """Accept text chat and tool turns, never remote media or extra payloads."""
    if not isinstance(messages, list) or not messages:
        return False
    fields = {
        "role",
        "content",
        "name",
        "tool_call_id",
        "tool_calls",
        "reasoning",
        "reasoning_details",
        "refusal",
    }
    for message in messages:
        if not isinstance(message, dict) or set(message) - fields:
            return False
        role = message.get("role")
        if not isinstance(role, str) or role not in {
            "system",
            "developer",
            "user",
            "assistant",
            "tool",
        }:
            return False
        content = message.get("content")
        if role == "assistant":
            if content is not None and not isinstance(content, str):
                return False
        elif not isinstance(content, str):
            return False
        if "name" in message and not isinstance(message["name"], str):
            return False
        if "tool_call_id" in message and (
            role != "tool" or not isinstance(message["tool_call_id"], str)
        ):
            return False
        if "reasoning" in message and (
            role != "assistant" or not isinstance(message["reasoning"], str)
        ):
            return False
        if "refusal" in message and (
            role != "assistant"
            or (message["refusal"] is not None and not isinstance(message["refusal"], str))
        ):
            return False
        if "reasoning_details" in message and (
            role != "assistant"
            or _safe_reasoning_details(message["reasoning_details"]) != message["reasoning_details"]
        ):
            return False
        if "tool_calls" in message:
            calls = message["tool_calls"]
            if role != "assistant" or not isinstance(calls, list):
                return False
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or set(call) - {"id", "type", "function"}
                    or not isinstance(call.get("id"), str)
                    or call.get("type") != "function"
                    or not isinstance(call.get("function"), dict)
                ):
                    return False
                function = call["function"]
                if (
                    set(function) - {"name", "arguments"}
                    or not isinstance(function.get("name"), str)
                    or not isinstance(function.get("arguments"), str)
                ):
                    return False
    return True


async def chat_completions(request: Request) -> Response:
    """Serve one bounded completion; all failure paths keep provider keys private."""
    if not _enabled():
        return _error("LLM broker is disabled", 503)
    identity = _device_identity(request)
    if identity is None:
        return _error("device-bound OAuth token required", 401)
    agent, node = identity
    body = await _read_body(request)
    if isinstance(body, Response):
        return body
    if set(body) - _BODY_FIELDS:
        return _error("unsupported completion parameter", 422)
    model = body.get("model")
    if not isinstance(model, str) or model not in _allowed_models():
        return _error("model is not allowed", 403)
    provider, upstream_url, secret_name, upstream_model = _provider(model)
    if not _valid_messages(body.get("messages")):
        return _error("messages must contain text chat or valid tool turns", 400)
    streaming = body.get("stream", False)
    if type(streaming) is not bool:
        return _error("stream must be a boolean", 400)
    max_tokens = body.get("max_tokens", 512)
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= _MAX_OUTPUT_TOKENS
    ):
        return _error("max_tokens must be between 1 and 4096", 400)
    if "max_completion_tokens" in body:
        return _error("use max_tokens for this broker route", 400)
    completions = body.get("n", 1)
    if type(completions) is not int or completions != 1 or "best_of" in body:
        return _error("only one completion per request is allowed", 400)
    body["max_tokens"] = max_tokens
    body["model"] = upstream_model
    body["n"] = 1
    body["stream"] = streaming
    if streaming:
        body["stream_options"] = {"include_usage": True}
    if not _take_slot(agent):
        return _error("agent request rate limit exceeded", 429)
    try:
        key = await run_in_threadpool(
            managed_secrets._load_for_consumer,
            secret_name,
            "llm",
            org_id=edition.tenant_org_id(),
        )
    except Exception:  # noqa: BLE001 - never echo secret-store details
        return _error("provider key is not configured", 503)

    # A durable attempt is required before a billable provider call. A failed
    # event append refuses the spend; no request or response content is logged.
    try:
        attempt = await run_in_threadpool(
            llm_spend.reserve,
            org_id=edition.tenant_org_id(),
            agent=agent,
            node=node,
            model=model,
            provider=provider,
        )
    except llm_spend.BudgetError as exc:
        return _error(str(exc), 429)
    except Exception:  # noqa: BLE001 - audit unavailable, fail closed
        return _error("broker audit is unavailable", 503)

    # Snapshot the actor's open run now, before the provider call — not after
    # it returns. A task can finish (clearing tasks.current_run_id) or a new
    # claim can land on the same agent while this request is in flight; either
    # would make a post-hoc lookup attribute the call to the wrong run, or to
    # none (task #1526 follow-up, Devin review of PR #875).
    run_id_snapshot = await run_in_threadpool(usage_ledger.resolve_run_id, agent)

    if streaming:
        return StreamingResponse(
            _stream_completion(
                body, key, attempt, agent, node, model, upstream_url, run_id_snapshot
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "POST",
                upstream_url,
                json=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            ) as upstream:
                if upstream.status_code != 200:
                    recorded = await _record_result(
                        attempt, agent, node, model, "provider_error", run_id=run_id_snapshot
                    )
                    response = _error("provider request failed", 502)
                    if not recorded:
                        response.headers["x-ua-broker-audit"] = "result-unavailable"
                    return response
                chunks: list[bytes] = []
                size = 0
                async for chunk in upstream.aiter_bytes():
                    size += len(chunk)
                    if size > _RESPONSE_LIMIT:
                        recorded = await _record_result(
                            attempt,
                            agent,
                            node,
                            model,
                            "response_too_large",
                            run_id=run_id_snapshot,
                        )
                        response = _error("provider response exceeds limit", 502)
                        if not recorded:
                            response.headers["x-ua-broker-audit"] = "result-unavailable"
                        return response
                    chunks.append(chunk)
    except httpx.HTTPError:
        recorded = await _record_result(
            attempt, agent, node, model, "transport_error", run_id=run_id_snapshot
        )
        response = _error("provider request failed", 502)
        if not recorded:
            response.headers["x-ua-broker-audit"] = "result-unavailable"
        return response
    finally:
        key = ""

    try:
        result = json.loads(b"".join(chunks))
    except ValueError:
        result = None
    safe_result = _safe_completion(result) if isinstance(result, dict) else None
    if safe_result is None:
        recorded = await _record_result(
            attempt, agent, node, model, "invalid_response", run_id=run_id_snapshot
        )
        response = _error("provider response was invalid", 502)
        if not recorded:
            response.headers["x-ua-broker-audit"] = "result-unavailable"
        return response
    recorded = await _record_result(
        attempt, agent, node, model, "completed", usage=result.get("usage"), run_id=run_id_snapshot
    )
    response = JSONResponse(safe_result)
    if not recorded:
        response.headers["x-ua-broker-audit"] = "result-unavailable"
    return response


async def _stream_completion(
    body: dict,
    key: str,
    attempt: tuple[str, str, str],
    agent: str,
    node: str,
    model: str,
    upstream_url: str,
    run_id_snapshot: str | None,
):
    """Relay bounded SSE while retaining the final usage and reconciliation gate."""
    outcome = "interrupted"
    usage = None
    terminal = b"data: [DONE]\n\n"
    size = 0
    source_size = 0
    skip_event = False
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "POST",
                upstream_url,
                json=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            ) as upstream:
                if upstream.status_code != 200:
                    outcome = "provider_error"
                    yield b'data: {"error":{"message":"provider request failed","type":"broker_error"}}\n\n'
                    return
                async for line in upstream.aiter_lines():
                    source_size += len(line.encode()) + 1
                    if source_size > _RESPONSE_LIMIT:
                        outcome = "response_too_large"
                        yield b'data: {"error":{"message":"provider response exceeds limit","type":"broker_error"}}\n\n'
                        return
                    # Only OpenAI chat chunk data crosses this boundary.
                    # Provider comments, named diagnostic events, and extra
                    # metadata are never relayed to the device.
                    if not line:
                        skip_event = False
                        continue
                    if line.startswith("event:"):
                        skip_event = line[6:].strip() not in {"", "message"}
                        continue
                    if skip_event:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        outcome = "completed"
                        break
                    try:
                        packet = json.loads(data)
                    except ValueError:
                        packet = None
                    if isinstance(packet, dict) and "error" in packet:
                        outcome = "provider_error"
                        yield b'data: {"error":{"message":"provider request failed","type":"broker_error"}}\n\n'
                        return
                    if (
                        not isinstance(packet, dict)
                        or packet.get("object") != "chat.completion.chunk"
                        or not isinstance(packet.get("choices"), list)
                    ):
                        outcome = "invalid_response"
                        yield b'data: {"error":{"message":"provider stream was invalid","type":"broker_error"}}\n\n'
                        return
                    if isinstance(packet.get("usage"), dict):
                        usage = packet["usage"]
                    safe = _safe_chunk(packet)
                    if safe is None:
                        outcome = "invalid_response"
                        yield b'data: {"error":{"message":"provider stream was invalid","type":"broker_error"}}\n\n'
                        return
                    encoded = b"data: " + json.dumps(safe, separators=(",", ":")).encode() + b"\n\n"
                    size += len(encoded)
                    if size > _RESPONSE_LIMIT:
                        outcome = "response_too_large"
                        yield b'data: {"error":{"message":"provider response exceeds limit","type":"broker_error"}}\n\n'
                        return
                    yield encoded
        if outcome != "completed":
            outcome = "incomplete_stream"
            yield b'data: {"error":{"message":"provider stream ended early","type":"broker_error"}}\n\n'
            return
    except httpx.HTTPError:
        outcome = "transport_error"
        yield b'data: {"error":{"message":"provider request failed","type":"broker_error"}}\n\n'
        return
    finally:
        key = ""
        # Disconnect cancellation must not skip the outcome write. An audit
        # failure leaves the full reservation in place for later reconciliation.
        with anyio.CancelScope(shield=True):
            recorded = await _record_result(
                attempt, agent, node, model, outcome, usage=usage, run_id=run_id_snapshot
            )
    if recorded:
        yield terminal
    else:
        yield b'data: {"error":{"message":"broker audit is unavailable","type":"broker_error"}}\n\n'


async def _record_result(
    attempt: tuple[str, str, str],
    agent: str,
    node: str,
    model: str,
    outcome: str,
    *,
    usage: object = None,
    run_id: str | None = None,
) -> bool:
    reported: dict[str, int | float] = {}
    if isinstance(usage, dict):
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                reported[name] = value
        cost = usage.get("cost")
        if (
            isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
        ):
            reported["cost_usd"] = float(cost)
    try:
        await run_in_threadpool(
            llm_spend.finish,
            attempt[0],
            attempt[1],
            agent=agent,
            node=node,
            model=model,
            outcome=outcome,
            usage=reported or None,
            provider=attempt[2],
        )
    except Exception as exc:  # noqa: BLE001 - provider already answered; surface audit gap
        _log.error("LLM broker outcome audit unavailable: %s", type(exc).__name__)
        return False
    if outcome == "completed":
        # Hub-observed usage (task #1526), off the event loop: this opens a
        # SQLite connection (busy_timeout=5s), neither of which belongs on the
        # broker's async request path. Keyed on the reservation's request_id
        # so a retried result can never write a second row for the same call
        # — llm_spend already treats request_id as the one-reservation-per-
        # call primary key. `run_id` is the snapshot resolved before the
        # provider call started (task #1526 follow-up, Devin review of PR
        # #875): a lookup done now, after the response came back, could land
        # on a run the task finished into, or on an unrelated new claim by the
        # same agent — passing the snapshot through skips that lookup.
        await run_in_threadpool(
            usage_ledger.record_observed,
            provider=attempt[2],
            model=model,
            input_tokens=usage_ledger.token_count(reported.get("prompt_tokens")),
            output_tokens=usage_ledger.token_count(reported.get("completion_tokens")),
            cost_usd=usage_ledger.money(reported.get("cost_usd")),
            actor=agent,
            run_id=run_id,
            dedupe_key=f"llm_broker:{attempt[0]}",
        )
    return True
